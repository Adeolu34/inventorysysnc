from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "inventory_sync",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.sync_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=86400,  # 24 hours
    broker_connection_retry_on_startup=True,
)

# Beat schedule
celery_app.conf.beat_schedule = {
    # Full inventory sync every 60 minutes
    "run-full-sync": {
        "task": "app.tasks.sync_tasks.run_full_sync_task",
        "schedule": crontab(minute=f"*/{settings.sync_interval_minutes}"),
        "options": {"queue": "sync"},
    },
    # Daily report at 07:00 UTC
    "generate-daily-report": {
        "task": "app.tasks.sync_tasks.generate_daily_report_task",
        "schedule": crontab(hour=7, minute=0),
        "options": {"queue": "reports"},
    },
    # Low-stock alert check every 30 minutes
    "check-low-stock-alerts": {
        "task": "app.tasks.sync_tasks.check_low_stock_alerts_task",
        "schedule": crontab(minute="*/30"),
        "options": {"queue": "alerts"},
    },
}

celery_app.conf.task_queues = {
    "sync": {},
    "reports": {},
    "alerts": {},
    "default": {},
}
celery_app.conf.task_default_queue = "default"
