from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime
import logging
import requests

# Indian timezone
IST = pytz.timezone("Asia/Kolkata")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BACKEND_URL = "http://127.0.0.1:8000/api/refresh-all-prices/"  # 🔑 change if deployed

def start_scheduler():
    scheduler = BackgroundScheduler(timezone=IST)

    def scheduled_job():
        current_time = datetime.now(IST)
        logger.info(f"🕐 Scheduler executing at: {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # Market hours check
        market_start = current_time.replace(hour=9, minute=15, second=0, microsecond=0)
        market_end = current_time.replace(hour=15, minute=30, second=0, microsecond=0)

        if market_start <= current_time <= market_end:
            try:
                # 🔥 Call the Django API endpoint
                response = requests.post(BACKEND_URL, timeout=60)
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"✅ Prices refreshed via API. Stocks: {data.get('total_stocks')} "
                                f"Time: {data.get('processing_time')}s")
                else:
                    logger.error(f"❌ API call failed. Status: {response.status_code}, Body: {response.text}")
            except Exception as e:
                logger.error(f"❌ Error calling refresh API: {e}")
        else:
            logger.info("⏰ Outside market hours, skipping execution")

    scheduler.add_job(
        scheduled_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="15,30,45,0",   # every 15 minutes
            second=15,
            timezone=IST
        ),
        id="fetch_prices_job",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()

    current_time = datetime.now(IST)
    server_time = datetime.now()
    logger.info(f"✅ Scheduler started at IST: {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    logger.info(f"📍 Server time: {server_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("📅 Schedule: Every 15 mins (Mon–Fri, 09:15:15–15:30:15 IST)")

    return scheduler
