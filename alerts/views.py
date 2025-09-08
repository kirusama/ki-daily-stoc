import os
import csv
import re
import traceback
import threading
from datetime import datetime
import json
import pandas as pd
import requests
import yfinance as yf
from django.conf import settings
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
import pytz
from django.http import JsonResponse

IST = pytz.timezone("Asia/Kolkata")

# --- Config ---
GOOGLE_SHEET_ID = "1qPeDQOzgiCrfp1h32KUyn5CHD509yR8E_ggxfjFtJOc"  # <-- your public Google Sheet ID
CSV_EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export?format=csv&gid="

# In-memory cache (not persistent across Koyeb workers)
watchlists = {}
watchlists_lock = threading.Lock()

# Sheet tab → gid mapping
SHEET_TABS = {
    "Intraday": "0",
    "SwingRiskyBuy": "1087261693",
    # "FIBOST": "1298523822",
}

# Log file
BASE_DIR = getattr(settings, "BASE_DIR", os.getcwd())
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "target_hits.csv")
os.makedirs(LOG_DIR, exist_ok=True)


# ----------------- Helpers -----------------
def normalize_symbol(scrip_name: str) -> str:
    s = scrip_name.strip()
    return s if "." in s else s + ".NS"


def log_target_hit(sheet_name, scrip_name, target_price, hit_price):
    try:
        header = ["sheet_name", "scrip_name", "target_price", "hit_price", "date", "time"]
        now = datetime.now()
        row = [
            sheet_name,
            scrip_name,
            str(target_price),
            str(hit_price),
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M:%S"),
        ]
        write_header = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(header)
            writer.writerow(row)
    except Exception:
        print("❌ Failed to log target hit")
        traceback.print_exc()


def fetch_sheet():
    """Always fetch fresh sheet data from Google Sheets."""
    new_watchlists = {}
    for tab_name, gid in SHEET_TABS.items():
        try:
            url = CSV_EXPORT_URL + gid
            df = pd.read_csv(url)
            df.columns = [c.strip() for c in df.columns]

            if "Scrip Name" not in df.columns or "Target Price" not in df.columns:
                print(f"⚠️ {tab_name} missing required columns")
                continue

            new_watchlists[tab_name] = [
                {
                    "scrip_name": str(row["Scrip Name"]).strip(),
                    "target_price": float(row["Target Price"]),
                    "yf_symbol": normalize_symbol(str(row["Scrip Name"])),
                    "current_price": None,
                    "status": "Not Checked",
                }
                for _, row in df.iterrows()
                if str(row.get("Scrip Name", "")).strip()
            ]
        except Exception as e:
            print(f"❌ Error loading {tab_name}: {e}")
            new_watchlists[tab_name] = []
    return new_watchlists


def fetch_stock_prices(watchlists_data=None, sheet_name=None, scrips=None):
    """
    Fetch stock prices and return updated copy of watchlists_data.
    If no watchlists_data passed, reload fresh from Google Sheets.
    """
    global watchlists
    if watchlists_data is None:
        with watchlists_lock:
            if not watchlists:
                watchlists = fetch_sheet()
            watchlists_data = watchlists

    updated = json.loads(json.dumps(watchlists_data))  # deep copy via json
    sheets_to_update = [sheet_name] if sheet_name else list(updated.keys())

    for current_sheet in sheets_to_update:
        stocks = updated.get(current_sheet, [])
        if scrips is not None:
            scrip_names = {s["scrip_name"] for s in scrips}
            stocks = [s for s in stocks if s["scrip_name"] in scrip_names]

        for stock in stocks:
            try:
                ticker = yf.Ticker(stock["yf_symbol"])
                hist = ticker.history(period="2d", interval="15m")

                if not hist.empty:
                    price = hist["Close"].iloc[-2] if len(hist) >= 2 else hist["Close"].iloc[-1]
                    stock["current_price"] = round(float(price), 2)

                    if stock["current_price"] >= stock["target_price"]:
                        stock["status"] = "Target Hit!"
                        log_target_hit(current_sheet, stock["scrip_name"],
                                    stock["target_price"], stock["current_price"])
                    else:
                        stock["status"] = "Below Target"
                else:
                    stock["current_price"] = 0.0
                    stock["status"] = "No Data"

            except Exception as e:
                print(f"⚠️ Error fetching {stock.get('scrip_name')}: {e}")
                stock["current_price"] = 0.0
                stock["status"] = "Error"

    return updated


# ----------------- Views -----------------
def home(request):
    return render(request, "index.html")


def get_watchlists(request):
    """Return cached watchlists, fetch fresh if empty."""
    global watchlists
    with watchlists_lock:
        if not watchlists:
            watchlists = fetch_sheet()
        return JsonResponse({"watchlists": watchlists})


@csrf_exempt
def refresh_sheet(request):
    """Refresh all sheets from Google Sheets."""
    global watchlists
    with watchlists_lock:
        watchlists = fetch_sheet()
    return JsonResponse({"status": "ok", "watchlists": watchlists})


@csrf_exempt
def refresh_all_prices(request):
    """Fetch prices for all tabs."""
    global watchlists
    print("🔄 Refreshing all prices")
    try:
        with watchlists_lock:
            if not watchlists:
                watchlists = fetch_sheet()
            watchlists = fetch_stock_prices(watchlists)
        return JsonResponse({"watchlists": watchlists})
    except Exception as e:
        print("❌ refresh_all_prices error:", e)
        traceback.print_exc()
        return HttpResponseBadRequest(str(e))


@csrf_exempt
def refresh_tab_prices(request, tab_name):
    """Fetch prices for a single tab, batching if >100 scrips."""
    global watchlists
    print(f"🔄 Refreshing prices for {tab_name}")

    try:
        with watchlists_lock:
            if not watchlists:
                watchlists = fetch_sheet()
            all_scrips = watchlists.get(tab_name, [])
            if not all_scrips:
                return JsonResponse({"tab_name": tab_name, "count": 0, "data": []})

            BATCH_SIZE = 100
            batched_results = []
            for i in range(0, len(all_scrips), BATCH_SIZE):
                batch = all_scrips[i:i + BATCH_SIZE]
                watchlists = fetch_stock_prices(watchlists, sheet_name=tab_name, scrips=batch)
                batched_results.extend(watchlists.get(tab_name, [])[i:i + BATCH_SIZE])

        return JsonResponse({
            "tab_name": tab_name,
            "count": len(all_scrips),
            "data": batched_results
        })
    except Exception as e:
        print("❌ refresh_tab_prices error:", e)
        traceback.print_exc()
        return HttpResponseBadRequest(str(e))
    

def scheduler_status(request):
    """Debug endpoint to check scheduler status and timing"""
    current_ist = datetime.now(IST)
    current_server = datetime.now()
    
    # Check market hours
    market_start = current_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = current_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    
    is_market_hours = market_start <= current_ist <= market_end
    is_weekday = current_ist.weekday() < 5
    
    # Get next scheduled times (if you have access to scheduler instance)
    next_run_times = []
    try:
        from django.apps import apps
        # If you store scheduler instance in app config
        scheduler = getattr(apps.get_app_config('your_app_name'), 'scheduler', None)
        if scheduler:
            jobs = scheduler.get_jobs()
            for job in jobs:
                next_run_times.append({
                    'job_id': job.id,
                    'next_run': job.next_run_time.strftime('%Y-%m-%d %H:%M:%S %Z') if job.next_run_time else 'None'
                })
    except Exception as e:
        next_run_times = [{'error': str(e)}]
    
    return JsonResponse({
        'current_ist_time': current_ist.strftime('%Y-%m-%d %H:%M:%S %Z'),
        'current_server_time': current_server.strftime('%Y-%m-%d %H:%M:%S'),
        'market_hours': {
            'start': market_start.strftime('%H:%M:%S'),
            'end': market_end.strftime('%H:%M:%S'),
            'is_active': is_market_hours
        },
        'is_weekday': is_weekday,
        'should_run': is_market_hours and is_weekday,
        'timezone_offset': current_ist.strftime('%z'),
        'next_scheduled_runs': next_run_times
    })

def manual_price_fetch(request):
    """Manual trigger for testing"""
    try:
        from .tasks import check_and_fetch_prices  # Import the function
        result = check_and_fetch_prices()
        return JsonResponse({
            'status': 'success',
            'message': 'Price fetch completed',
            'timestamp': datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S %Z')
        })
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': str(e),
            'timestamp': datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S %Z')
        })
