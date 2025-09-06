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

# In-memory store (simple prototype)
watchlists = {}
target_hit_logged = {}
# Map of sheet tab name → gid
SHEET_TABS = {
    "Intraday": "0",        # usually first tab has gid=0
    "SwingRiskyBuy": "1087261693",
    "FIBOST": "1298523822",
    "FIBOMT": "1261523394",
    "FIBOLT": "774037465",
}


# Log file path (relative to project base dir if available)
BASE_DIR = getattr(settings, "BASE_DIR", os.getcwd())
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "target_hits.csv")

os.makedirs(LOG_DIR, exist_ok=True)


# ----------------- Utility helpers -----------------
def normalize_symbol(scrip_name: str) -> str:
    """Return a yfinance-friendly symbol. If user didn't include exchange suffix, add .NS"""
    s = scrip_name.strip()
    # If contains a dot (e.g., 'RELIANCE.NS' or 'TCS.BO'), assume user provided exchange
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

        # Remove JS wrapper: google.visualization.Query.setResponse(<json>);
        m = re.search(r"setResponse\((.*)\);", text, re.S)
        if not m:
            print("❌ Could not parse gviz response")
            return []

        data = json.loads(m.group(1))
        sheets = data.get("table", {}).get("cols", [])

        # Extract available sheet names from the response metadata
        # (this varies by Google Sheets, sometimes in "table.cols", sometimes in "reqId"...)
        tab_names = []
        if "reqId" in data:  # sanity check
            for entry in data.get("table", {}).get("cols", []):
                if "label" in entry and entry["label"]:
                    tab_names.append(entry["label"])

        # If still empty, try plan B: look in the whole JSON for "name" keys
        if not tab_names:
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


# ----------------- Core fetching logic (adapted from your reference) -----------------
def fetch_stock_prices(sheet_name=None):
    """
    Fetch current stock prices using yfinance using the exact logic you provided:
    - 15-minute candles, last 2 days
    - take the second-last completed candle as the 'current_price' when available
    - set status and log first-time target hits
    This is synchronous (suitable for web request).
    """
    global watchlists, target_hit_logged

    sheets_to_update = [sheet_name] if sheet_name else list(watchlists.keys())

    for current_sheet in sheets_to_update:
        if current_sheet not in watchlists:
            continue

        # iterate stocks in this sheet
        for stock in watchlists[current_sheet]:
            try:
                ticker = yf.Ticker(stock["yf_symbol"])
                hist = ticker.history(period="2d", interval="15m")

                if not hist.empty:
                    # choose the most recent completed candle (second last)
                    if len(hist) >= 2:
                        current_price = hist["Close"].iloc[-2]
                    else:
                        current_price = hist["Close"].iloc[-1]

                    stock["current_price"] = round(float(current_price), 2)

                    # Update status based on price comparison
                    if stock["current_price"] >= stock["target_price"]:
                        stock["status"] = "Target Hit!"

                        scrip_name = stock["scrip_name"]
                        # initialize map entries if missing
                        if current_sheet not in target_hit_logged:
                            target_hit_logged[current_sheet] = {}
                        if scrip_name not in target_hit_logged[current_sheet]:
                            target_hit_logged[current_sheet][scrip_name] = False

                        # If not yet logged, log and mark
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
    """Serve the SPA index page (make sure index.html exists in templates/)"""
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

            # Normalize column names
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
    try:
        updated = fetch_stock_prices()  # synchronous; will return after completion
        return JsonResponse({"watchlists": updated})
    except Exception as e:
        print("refresh_all_prices error:", e)
        traceback.print_exc()
        return HttpResponseBadRequest(str(e))


@csrf_exempt
def refresh_tab_prices(request, tab_name):
    """Fetch prices for a single tab/watchlist and return updated watchlists."""
    try:
        updated = fetch_stock_prices(sheet_name=tab_name)
        return JsonResponse({"watchlists": updated})
    except Exception as e:
        print("refresh_tab_prices error:", e)
        traceback.print_exc()
        return HttpResponseBadRequest(str(e))


