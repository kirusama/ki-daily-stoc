# app.py
from flask import Flask, render_template, jsonify, redirect, request, send_from_directory
import yfinance as yf
import pandas as pd
import threading
import requests
from io import StringIO
from datetime import datetime
import os
from openpyxl import Workbook, load_workbook
import time as time_module

app = Flask(__name__)

# Configuration
LOG_FILE = "target_hit_log.xlsx"
DATA_FILE = "dailystock.xlsx"
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/19zWk7yGv7G3YIwP9x6-bC6YEV39yRqNidkGhXzYYyGk/edit?usp=drive_link"  # Change this or make dynamic
USE_GOOGLE_SHEETS = True  # Set via /configure later if needed

# In-memory storage
watchlists = {}  # {sheet_name: [stocks]}
target_hit_logged = {}  # {sheet_name: {scrip_name: bool}}

# Background monitoring
monitoring_active = True


def load_watchlists():
    global watchlists, target_hit_logged
    watchlists.clear()
    target_hit_logged.clear()

    try:
        if USE_GOOGLE_SHEETS:
            if not GOOGLE_SHEET_URL:
                raise ValueError("Google Sheets URL not set")
            
            # Convert Google Sheets link to CSV export URL
            csv_url = GOOGLE_SHEET_URL.replace("/edit?usp=sharing", "/export?format=csv")
            response = requests.get(csv_url)
            response.raise_for_status()

            # Use Python engine + skip bad lines to avoid parsing crashes
            df = pd.read_csv(
                StringIO(response.text),
                on_bad_lines='skip',      # Skip problematic rows
                engine='python',          # Use Python parser (handles quotes/newlines better)
                skipinitialspace=True,    # Ignore spaces after commas
                encoding='utf-8'
            )
            sheets = {"Watchlist 1": df}

        else:
            if not os.path.exists(DATA_FILE):
                raise FileNotFoundError(f"{DATA_FILE} not found")
            xls = pd.ExcelFile(DATA_FILE)
            sheets = {sheet: xls.parse(sheet) for sheet in xls.sheet_names}

        # Process each sheet
        for sheet_name, df in sheets.items():
            df.columns = df.columns.str.strip().str.lower()
            if 'scrip name' not in df or 'target price' not in df:
                print(f"Sheet '{sheet_name}' missing 'Scrip Name' or 'Target Price' column")
                continue

            stocks = []
            target_hit_logged[sheet_name] = {}
            for _, row in df.iterrows():
                name = str(row['scrip name']).strip()
                if not name or name.lower() in ('nan', ''):
                    continue
                try:
                    target = float(row['target price'])
                except (ValueError, TypeError):
                    continue

                yf_symbol = name if name.endswith(('.NS', '.BO')) else f"{name}.NS"
                stocks.append({
                    "Scrip Name": name,
                    "Target Price": target,
                    "Current Price": 0.0,
                    "Status": "Loading...",
                    "yf_symbol": yf_symbol
                })
                target_hit_logged[sheet_name][name] = False

            if stocks:
                watchlists[sheet_name] = stocks
                print(f"✅ Loaded {len(stocks)} stocks from sheet '{sheet_name}'")
            else:
                print(f"⚠️ No valid stocks found in sheet '{sheet_name}'")

    except Exception as e:
        print(f"Error loading data: {e}")


def fetch_stock_price(symbol):
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="2d", interval="15m")
        if not hist.empty:
            price = hist['Close'].iloc[-2] if len(hist) >= 2 else hist['Close'].iloc[-1]
            return round(float(price), 2)
        return 0.0
    except:
        return 0.0


def check_and_log_target_hit(sheet_name, scrip, target, current):
    if current >= target:
        if not target_hit_logged[sheet_name][scrip]:
            log_target_hit(sheet_name, scrip, target, current)
            target_hit_logged[sheet_name][scrip] = True
        return "🎯 Target Hit!"
    return "Below Target"


def log_target_hit(sheet_name, scrip_name, target_price, current_price):
    try:
        wb = load_workbook(LOG_FILE) if os.path.exists(LOG_FILE) else Workbook()
        if 'Sheet' in wb.sheetnames:
            wb.remove(wb['Sheet'])

        if sheet_name not in wb.sheetnames:
            ws = wb.create_sheet(sheet_name)
            ws.append(['Scrip Name', 'Target Price', 'Hit Price', 'Date', 'Time', 'Timestamp'])
        else:
            ws = wb[sheet_name]

        now = datetime.now()
        ws.append([
            scrip_name,
            target_price,
            current_price,
            now.strftime('%Y-%m-%d'),
            now.strftime('%H:%M:%S'),
            now.strftime('%Y-%m-%d %H:%M:%S')
        ])
        wb.save(LOG_FILE)
        print(f"Logged: {scrip_name} hit target in {sheet_name}")
    except Exception as e:
        print(f"Log error: {e}")


def background_monitor():
    """Run every minute during market hours"""
    while monitoring_active:
        now = datetime.now()
        minute = now.minute
        weekday = now.weekday()  # 0=Mon, 4=Fri
        current_time = now.time()
        market_start = datetime.strptime("09:15", "%H:%M").time()
        market_end = datetime.strptime("15:30", "%H:%M").time()

        should_run = (
            weekday < 5 and
            market_start <= current_time <= market_end and
            minute in [1, 16, 31, 46]
        )

        if should_run:
            print(f"[{now.strftime('%H:%M:%S')}] Auto-fetching all stocks...")
            for sheet_name, stocks in watchlists.items():
                for stock in stocks:
                    price = fetch_stock_price(stock["yf_symbol"])
                    stock["Current Price"] = price
                    status = check_and_log_target_hit(
                        sheet_name,
                        stock["Scrip Name"],
                        stock["Target Price"],
                        price
                    )
                    stock["Status"] = status
            print("Auto-fetch complete.")

        time_module.sleep(60)  # Sleep 60 seconds


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/data")
def get_data():
    return jsonify(watchlists)


@app.route("/refresh")
def refresh():
    sheet = request.args.get("sheet")  # Now valid
    for sheet_name, stocks in watchlists.items():
        if sheet and sheet_name != sheet:
            continue
        for stock in stocks:
            price = fetch_stock_price(stock["yf_symbol"])
            stock["Current Price"] = price
            status = check_and_log_target_hit(
                sheet_name,
                stock["Scrip Name"],
                stock["Target Price"],
                price
            )
            stock["Status"] = status
    return "", 204


@app.route("/reload")
def reload():
    load_watchlists()
    return "", 204


@app.route("/log")
def view_log():
    if os.path.exists(LOG_FILE):
        return redirect(f"/static/{LOG_FILE}")
    return "No log file yet.", 200


# Make log file accessible
@app.route("/static/<path:filename>")
def static_file(filename):
    return send_from_directory(".", filename)  # Now valid


if __name__ == "__main__":
    # Load initial data
    load_watchlists()

    # Start background monitor
    thread = threading.Thread(target=background_monitor, daemon=True)
    thread.start()

    # Run Flask
    port = int(os.environ.get("PORT", 5000))

    app.run(host="0.0.0.0", port=port)

