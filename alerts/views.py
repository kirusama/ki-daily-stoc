import os
import csv
import re
import traceback
from datetime import datetime
import json
import pandas as pd
import requests
import yfinance as yf
from django.conf import settings
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt


# --- Config ---
GOOGLE_SHEET_ID = "1qPeDQOzgiCrfp1h32KUyn5CHD509yR8E_ggxfjFtJOc"  # <-- your public Google Sheet ID
CSV_SHEET_BASE_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/gviz/tq?tqx=out:csv&sheet="

# In-memory store
watchlists = {}
target_hit_logged = {}

# Map of sheet tab name → gid
SHEET_TABS = {
    "Intraday": "0",        # usually first tab has gid=0
    "SwingRiskyBuy": "1087261693",
    # "FIBOST": "1298523822",
    # "FIBOMT": "1261523394",
    # "FIBOLT": "774037465",
}

# Log file path
BASE_DIR = getattr(settings, "BASE_DIR", os.getcwd())
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "target_hits.csv")
os.makedirs(LOG_DIR, exist_ok=True)


# ----------------- Utility helpers -----------------
def normalize_symbol(scrip_name: str) -> str:
    """Return a yfinance-friendly symbol. If user didn't include exchange suffix, add .NS"""
    s = scrip_name.strip()
    if "." in s:
        return s
    return s + ".NS"


def discover_sheet_tabs():
    """Discover all tab names in the Google Sheet by parsing the gviz response."""
    global SHEET_TABS
    url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/gviz/tq?gid=0"
    try:
        resp = requests.get(url, timeout=10)
        text = resp.text
        m = re.search(r"setResponse\((.*)\);", text, re.S)
        if not m:
            print("❌ Could not parse gviz response")
            return []

        data = json.loads(m.group(1))
        tab_names = re.findall(r'"name":"(.*?)"', json.dumps(data))

        SHEET_TABS = sorted(set(tab_names))
        print("✅ Discovered sheet tabs:", SHEET_TABS)
        return SHEET_TABS

    except Exception as e:
        print("❌ Could not discover sheet tabs:", e)
        return []


def log_target_hit(sheet_name, scrip_name, target_price, hit_price):
    """Append a target-hit row to a CSV and ensure directory exists."""
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
        print("Failed to log target hit:")
        traceback.print_exc()


# ----------------- Sheet reading -----------------
def fetch_sheet():
    """Fetch all tabs into watchlists."""
    global watchlists, target_hit_logged
    if not SHEET_TABS:
        discover_sheet_tabs()

    new_watchlists = {}
    for tab in SHEET_TABS:
        try:
            url = CSV_SHEET_BASE_URL + tab
            df = pd.read_csv(url)
            df.columns = [c.strip() for c in df.columns]

            rows = []
            for _, r in df.iterrows():
                scrip = str(r.get("Scrip Name", "")).strip()
                try:
                    tp = float(r.get("Target Price", ""))
                except:
                    continue

                yf_sym = scrip if "." in scrip else scrip + ".NS"
                rows.append({
                    "scrip_name": scrip,
                    "target_price": tp,
                    "yf_symbol": yf_sym,
                    "current_price": 0.0,
                    "status": "Not Fetched",
                })

            new_watchlists[tab] = rows
            if tab not in target_hit_logged:
                target_hit_logged[tab] = {r["scrip_name"]: False for r in rows}

        except Exception as e:
            print(f"⚠️ Error reading {tab}:", e)
            new_watchlists[tab] = []

    watchlists = new_watchlists
    return watchlists


# ----------------- Core fetching logic -----------------
def fetch_stock_prices(sheet_name=None, scrips=None):
    """
    Fetch current stock prices using yfinance.
    Optionally restrict to a subset of scrips.
    """
    global watchlists, target_hit_logged

    sheets_to_update = [sheet_name] if sheet_name else list(watchlists.keys())

    for current_sheet in sheets_to_update:
        if current_sheet not in watchlists:
            continue

        stocks = watchlists[current_sheet]

        # Restrict to subset if batching
        if scrips is not None:
            scrip_names = {s["scrip_name"] for s in scrips}
            stocks = [s for s in stocks if s["scrip_name"] in scrip_names]

        for stock in stocks:
            try:
                ticker = yf.Ticker(stock["yf_symbol"])
                hist = ticker.history(period="2d", interval="15m")

                if not hist.empty:
                    if len(hist) >= 2:
                        current_price = hist["Close"].iloc[-2]
                    else:
                        current_price = hist["Close"].iloc[-1]

                    stock["current_price"] = round(float(current_price), 2)

                    if stock["current_price"] >= stock["target_price"]:
                        stock["status"] = "Target Hit!"
                        scrip_name = stock["scrip_name"]

                        if current_sheet not in target_hit_logged:
                            target_hit_logged[current_sheet] = {}
                        if scrip_name not in target_hit_logged[current_sheet]:
                            target_hit_logged[current_sheet][scrip_name] = False

                        if not target_hit_logged[current_sheet][scrip_name]:
                            try:
                                log_target_hit(
                                    current_sheet,
                                    scrip_name,
                                    stock["target_price"],
                                    stock["current_price"],
                                )
                                target_hit_logged[current_sheet][scrip_name] = True
                            except Exception:
                                print("Failed to log target hit for", scrip_name)
                                traceback.print_exc()
                    else:
                        stock["status"] = "Below Target"
                else:
                    stock["current_price"] = 0.0
                    stock["status"] = "No Data"

            except Exception as e:
                print(f"Error fetching {stock.get('scrip_name')}: {e}")
                traceback.print_exc()
                stock["current_price"] = 0.0
                stock["status"] = "Error"

    return watchlists


# ----------------- Views / API endpoints -----------------
def home(request):
    """Serve the SPA index page"""
    return render(request, "index.html")


def get_watchlists(request):
    """Return the in-memory watchlists. Fetch sheet first if empty."""
    global watchlists
    if not watchlists:
        fetch_sheet()
    return JsonResponse({"watchlists": watchlists})


@csrf_exempt
def refresh_sheet(request):
    """Refresh watchlists from Google Sheets tabs"""
    global watchlists
    watchlists.clear()

    for tab_name, gid in SHEET_TABS.items():
        try:
            url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export?format=csv&gid={gid}"
            df = pd.read_csv(url)
            df.columns = [c.strip() for c in df.columns]

            if "Scrip Name" not in df.columns or "Target Price" not in df.columns:
                print(f"⚠️ {tab_name} missing required columns")
                continue

            watchlists[tab_name] = [
                {
                    "scrip_name": row["Scrip Name"],
                    "target_price": float(row["Target Price"]),
                    "yf_symbol": str(row["Scrip Name"]) + ".NS",
                    "current_price": None,
                    "status": "Not Checked",
                }
                for _, row in df.iterrows()
            ]
        except Exception as e:
            print(f"❌ Error loading {tab_name}: {e}")

    return JsonResponse({"status": "ok", "watchlists": watchlists})


@csrf_exempt
def refresh_all_prices(request):
    """Fetch prices for all sheets and return updated watchlists."""
    print("refreshing all prices started")
    try:
        updated = fetch_stock_prices()
        return JsonResponse({"watchlists": updated})
    except Exception as e:
        print("refresh_all_prices error:", e)
        traceback.print_exc()
        return HttpResponseBadRequest(str(e))


@csrf_exempt
def refresh_tab_prices(request, tab_name):
    """Fetch prices for a single tab/watchlist and return ONLY that tab's data in batches if >100 scrips."""
    global watchlists
    print(f"refreshing started for {tab_name}")

    try:
        all_scrips = watchlists.get(tab_name, [])

        if not all_scrips:
            fetch_stock_prices(sheet_name=tab_name)
            all_scrips = watchlists.get(tab_name, [])

        BATCH_SIZE = 100
        batched_results = []
        for i in range(0, len(all_scrips), BATCH_SIZE):
            batch = all_scrips[i:i + BATCH_SIZE]
            fetch_stock_prices(sheet_name=tab_name, scrips=batch)
            batched_results.extend(watchlists.get(tab_name, [])[i:i + BATCH_SIZE])

        return JsonResponse({
            "tab_name": tab_name,
            "count": len(all_scrips),
            "data": batched_results
        })

    except Exception as e:
        print("refresh_tab_prices error:", e)
        traceback.print_exc()
        return HttpResponseBadRequest(str(e))

