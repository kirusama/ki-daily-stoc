from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, time

from .views import fetch_stock_prices

# Indian timezone
IST = pytz.timezone("Asia/Kolkata")

def start_scheduler():
    scheduler = BackgroundScheduler(timezone=IST)

    # Job: every 15 mins between 09:15:15 and 15:30:15, only on weekdays
    scheduler.add_job(
        fetch_stock_prices,
        trigger=CronTrigger(
            day_of_week="mon-fri",   # ✅ only weekdays
            hour="9-15",             # 09:00–15:59
            minute="0,15,30,45",     # every 15 minutes
            second=15,               # at :15 seconds
            timezone=IST
        ),
        id="fetch_prices_job",
        replace_existing=True,
    )

    scheduler.start()
    print("✅ Scheduler started: fetch_stock_prices every 15 mins (Mon–Fri, 09:15:15–15:30:15 IST)")
