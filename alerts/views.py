import os
import csv
import re
from io import StringIO
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

# --- Config ---
GOOGLE_SHEET_ID = "1qPeDQOzgiCrfp1h32KUyn5CHD509yR8E_ggxfjFtJOc"  # <-- your public Google Sheet ID
CSV_SHEET_BASE_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/gviz/tq?tqx=out:csv&sheet="

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

# Batch processing configuration
BATCH_SIZE = 25  # Smaller batch size for cloud deployment
BATCH_DELAY = 2  # Longer delay between batches for cloud
MAX_WORKERS = 3  # Fewer workers for cloud deployment

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
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()  # Raise error if download failed
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
            url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export?format=csv&gid={SHEET_TABS[tab]}"
            print(f"📊 Fetching sheet data for {tab} from: {url}")
            
            df = pd.read_csv(StringIO(resp.text))
            df.columns = [c.strip() for c in df.columns]

            print(f"📋 Columns in {tab}: {list(df.columns)}")

            rows = []
            for _, r in df.iterrows():
                scrip = str(r.get("Scrip Name", "")).strip()
                if not scrip or scrip == 'nan':
                    continue
                    
                try:
                    tp = float(r.get("Target Price", ""))
                    if tp <= 0:
                        continue
                except (ValueError, TypeError):
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
            print(f"✅ Loaded {len(rows)} stocks for {tab}")
            
            if tab not in target_hit_logged:
                target_hit_logged[tab] = {r["scrip_name"]: False for r in rows}

        except Exception as e:
            print(f"⚠️ Error reading {tab}: {e}")
            traceback.print_exc()
            new_watchlists[tab] = []

    watchlists = new_watchlists
    print(f"🎯 Total watchlists loaded: {list(watchlists.keys())}")
    return watchlists


# ----------------- Core fetching logic with improved error handling -----------------
def fetch_single_stock_price(stock: Dict, sheet_name: str) -> Dict:
    """Fetch price for a single stock and update its status."""
    global target_hit_logged
    
    try:
        print(f"📈 Fetching price for {stock['scrip_name']} ({stock['yf_symbol']})")
        
        ticker = yf.Ticker(stock["yf_symbol"])
        # Use shorter period and interval for cloud deployment
        hist = ticker.history(period="1d", interval="5m", timeout=10)

        if not hist.empty:
            # Get the most recent price
            if len(hist) >= 2:
                current_price = hist["Close"].iloc[-2]
            else:
                current_price = hist["Close"].iloc[-1]

            stock["current_price"] = round(float(current_price), 2)

            # Update status based on price comparison
            if stock["current_price"] >= stock["target_price"]:
                stock["status"] = "Target Hit!"
                scrip_name = stock["scrip_name"]

                # Initialize logging structure if needed
                if sheet_name not in target_hit_logged:
                    target_hit_logged[sheet_name] = {}
                if scrip_name not in target_hit_logged[sheet_name]:
                    target_hit_logged[sheet_name][scrip_name] = False

                # Log target hit if not already logged
                if not target_hit_logged[sheet_name][scrip_name]:
                    try:
                        log_target_hit(
                            sheet_name,
                            scrip_name,
                            stock["target_price"],
                            stock["current_price"],
                        )
                        target_hit_logged[sheet_name][scrip_name] = True
                        print(f"🎯 Target hit logged: {scrip_name} at {stock['current_price']}")
                    except Exception:
                        print(f"Failed to log target hit for {scrip_name}")
                        traceback.print_exc()
            else:
                stock["status"] = "Below Target"
                
            print(f"✅ {stock['scrip_name']}: ₹{stock['current_price']} ({stock['status']})")
        else:
            stock["current_price"] = 0.0
            stock["status"] = "No Data"
            print(f"❌ No data for {stock['scrip_name']}")

    except Exception as e:
        print(f"❌ Error fetching {stock.get('scrip_name')}: {e}")
        stock["current_price"] = 0.0
        stock["status"] = "Error"

    return stock


def process_stock_batch(batch: List[Dict], sheet_name: str) -> List[Dict]:
    """Process a batch of stocks using ThreadPoolExecutor with enhanced timeout protection."""
    updated_stocks = []
    
    print(f"🔄 Processing batch of {len(batch)} stocks for {sheet_name}")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all stocks in the batch
        future_to_stock = {
            executor.submit(fetch_single_stock_price, stock.copy(), sheet_name): stock 
            for stock in batch
        }
        
        # Collect results as they complete with individual timeouts
        for future in as_completed(future_to_stock, timeout=60):  # 60 second batch timeout
            try:
                updated_stock = future.result(timeout=20)  # 20 second individual timeout
                updated_stocks.append(updated_stock)
            except Exception as e:
                original_stock = future_to_stock[future]
                scrip_name = original_stock.get('scrip_name', 'Unknown')
                print(f"❌ Batch processing error for {scrip_name}: {e}")
                
                # Add the original stock with appropriate error status
                original_stock = original_stock.copy()
                original_stock["current_price"] = 0.0
                
                # Set specific error status based on the exception
                if "timeout" in str(e).lower():
                    original_stock["status"] = "Timeout"
                elif "delisted" in str(e).lower():
                    original_stock["status"] = "Delisted"
                else:
                    original_stock["status"] = "Error"
                    
                updated_stocks.append(original_stock)
    
    print(f"✅ Batch completed: {len(updated_stocks)} stocks processed")
    return updated_stocks


def fetch_stock_prices(sheet_name=None):
    """Fetch current stock prices using yfinance with batch processing."""
    global watchlists

    sheets_to_update = [sheet_name] if sheet_name else list(watchlists.keys())
    print(f"📊 Starting price fetch for sheets: {sheets_to_update}")

    for current_sheet in sheets_to_update:
        if current_sheet not in watchlists:
            print(f"⚠️ Sheet {current_sheet} not found in watchlists")
            continue
        
        stocks = watchlists[current_sheet]
        total_stocks = len(stocks)
        
        if total_stocks == 0:
            print(f"📝 No stocks found in {current_sheet}")
            continue
        
        print(f"🔄 Processing {total_stocks} stocks in {current_sheet}")
        
        # Process stocks in batches
        updated_stocks = []
        
        for i in range(0, total_stocks, BATCH_SIZE):
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE
            
            batch = stocks[i:i + BATCH_SIZE]
            batch_size = len(batch)
            
            print(f"📦 Processing batch {batch_num}/{total_batches} ({batch_size} stocks) for {current_sheet}")
            
            # Process the batch
            batch_results = process_stock_batch(batch, current_sheet)
            updated_stocks.extend(batch_results)
            
            # Add delay between batches (except for the last batch)
            if i + BATCH_SIZE < total_stocks:
                print(f"⏳ Waiting {BATCH_DELAY} seconds before next batch...")
                time.sleep(BATCH_DELAY)
        
        # Update the global watchlists with processed results
        watchlists[current_sheet] = updated_stocks
        
        # Print summary
        target_hits = sum(1 for stock in updated_stocks if stock["status"] == "Target Hit!")
        errors = sum(1 for stock in updated_stocks if stock["status"] == "Error")
        print(f"✅ Completed {current_sheet}: {target_hits} targets hit, {errors} errors")

    return watchlists


# ----------------- Views / API endpoints -----------------
def home(request):
    """Serve the SPA index page"""
    return render(request, "index.html")


def get_watchlists(request):
    """Return the in-memory watchlists. Fetch sheet first if empty."""
    global watchlists
    print(f"📋 get_watchlists called. Current watchlists: {list(watchlists.keys())}")
    
    if not watchlists:
        print("📥 Watchlists empty, fetching from sheet...")
        fetch_sheet()
    
    # Count total stocks
    total_stocks = sum(len(stocks) for stocks in watchlists.values())
    print(f"📊 Returning {len(watchlists)} watchlists with {total_stocks} total stocks")
    
    return JsonResponse({
        "watchlists": watchlists,
        "total_watchlists": len(watchlists),
        "total_stocks": total_stocks
    })


@csrf_exempt
def refresh_sheet(request):
    """Refresh watchlists from Google Sheets tabs"""
    global watchlists
    print("🔄 refresh_sheet called")
    
    watchlists.clear()
    target_hit_logged.clear()
    
    try:
        fetch_sheet()
        total_stocks = sum(len(stocks) for stocks in watchlists.values())
        
        print(f"✅ Sheet refresh completed: {len(watchlists)} tabs, {total_stocks} stocks")
        
        return JsonResponse({
            "status": "ok", 
            "watchlists": watchlists,
            "total_watchlists": len(watchlists),
            "total_stocks": total_stocks
        })
        
    except Exception as e:
        print(f"❌ Error in refresh_sheet: {e}")
        traceback.print_exc()
        return HttpResponseBadRequest(f"Failed to refresh sheet: {str(e)}")


@csrf_exempt
def refresh_all_prices(request):
    """Fetch prices for all sheets and return updated watchlists."""
    try:
        print("🚀 refresh_all_prices called")
        start_time = time.time()
        
        # Ensure we have watchlists loaded
        if not watchlists:
            print("📥 No watchlists found, loading from sheet first...")
            fetch_sheet()
        
        total_stocks = sum(len(stocks) for stocks in watchlists.values())
        print(f"📊 Total stocks to process: {total_stocks}")
        
        updated = fetch_stock_prices()
        
        end_time = time.time()
        processing_time = round(end_time - start_time, 2)
        print(f"✅ refresh_all_prices completed in {processing_time} seconds")
        
        return JsonResponse({
            "watchlists": updated,
            "processing_time": processing_time,
            "total_stocks": total_stocks,
            "batch_size": BATCH_SIZE
        })
        
    except Exception as e:
        print(f"❌ refresh_all_prices error: {e}")
        traceback.print_exc()
        return HttpResponseBadRequest(str(e))


@csrf_exempt
def refresh_tab_prices(request, tab_name):
    """Fetch prices for a single tab/watchlist and return ONLY that tab's data."""
    global watchlists
    
    try:
        print(f"🚀 refresh_tab_prices called for {tab_name}")
        start_time = time.time()
        
        # Ensure we have watchlists loaded
        if not watchlists:
            print("📥 No watchlists found, loading from sheet first...")
            fetch_sheet()
        
        # Check if the tab exists
        if tab_name not in watchlists:
            available_tabs = list(watchlists.keys())
            error_msg = f"Tab '{tab_name}' not found. Available tabs: {available_tabs}"
            print(f"❌ {error_msg}")
            return HttpResponseBadRequest(error_msg)
        
        # Get stocks for this tab
        tab_stocks = watchlists[tab_name]
        stock_count = len(tab_stocks)
        
        print(f"📊 Processing {stock_count} stocks for {tab_name}")
        
        if stock_count == 0:
            print(f"⚠️ No stocks found in {tab_name}")
            return JsonResponse({
                "tab_name": tab_name,
                "data": [],
                "total_stocks": 0,
                "processing_time": 0,
                "message": "No stocks found in this watchlist"
            })
        
        # Process the tab
        fetch_stock_prices(sheet_name=tab_name)
        
        # Get updated data
        updated_tab_data = watchlists.get(tab_name, [])
        
        end_time = time.time()
        processing_time = round(end_time - start_time, 2)
        
        # Count results
        target_hits = sum(1 for stock in updated_tab_data if stock["status"] == "Target Hit!")
        errors = sum(1 for stock in updated_tab_data if stock["status"] == "Error")
        
        print(f"✅ refresh_tab_prices for {tab_name} completed: {target_hits} hits, {errors} errors, {processing_time}s")
        
        return JsonResponse({
            "tab_name": tab_name,
            "data": updated_tab_data,
            "total_stocks": len(updated_tab_data),
            "processing_time": processing_time,
            "batch_size": BATCH_SIZE,
            "target_hits": target_hits,
            "errors": errors
        })
        
    except Exception as e:
        print(f"❌ refresh_tab_prices error for {tab_name}: {e}")
        traceback.print_exc()
        return HttpResponseBadRequest(f"Error processing {tab_name}: {str(e)}")

