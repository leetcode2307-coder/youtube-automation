import time
from typing import List
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from app.config import settings
from app.logging_config import logger
from app.pipeline import run_content_pipeline


def scheduled_job_runner(slot: str) -> None:
    """Callback triggered by APScheduler at 07:00 or 16:30 IST."""
    logger.info("=" * 60)
    logger.info("APScheduler triggered %s production run in %s", slot.upper(), settings.TIMEZONE)
    logger.info("=" * 60)
    try:
        package = run_content_pipeline(slot=slot)  # type: ignore
        logger.info("Scheduled production package generated successfully: %s", package.run_id)
        logger.info("Flow brief is ready in data/flow_exports/%s/FLOW_PROMPTS.txt", package.run_id)
    except Exception as e:
        logger.error("Error executing scheduled %s run: %s", slot, e, exc_info=True)


def create_scheduler() -> BackgroundScheduler:
    """Create and configure APScheduler with Asia/Kolkata timezone cron triggers."""
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        logger.warning("Could not load ZoneInfo for %s, falling back to local system timezone", settings.TIMEZONE)
        tz = None

    scheduler = BackgroundScheduler(timezone=tz)

    # 1. Morning Job (defaults to 07:00 AM IST)
    scheduler.add_job(
        scheduled_job_runner,
        CronTrigger.from_crontab(settings.MORNING_CRON, timezone=tz),
        id="morning_shorts_job",
        name=f"Morning AI Engineering Short ({settings.MORNING_CRON})",
        args=["morning"],
        max_instances=1,
        coalesce=True,
    )

    # 2. Evening Job (defaults to 04:30 PM / 16:30 IST)
    scheduler.add_job(
        scheduled_job_runner,
        CronTrigger.from_crontab(settings.EVENING_CRON, timezone=tz),
        id="evening_shorts_job",
        name=f"Evening AI Engineering Short ({settings.EVENING_CRON})",
        args=["evening"],
        max_instances=1,
        coalesce=True,
    )

    logger.info(
        "Configured APScheduler with 2 daily jobs (morning='%s', evening='%s', tz='%s')",
        settings.MORNING_CRON,
        settings.EVENING_CRON,
        settings.TIMEZONE,
    )
    return scheduler


def start_scheduler_forever() -> None:
    """Start scheduler in blocking process loop for daemon execution."""
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("APScheduler running. Press Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down APScheduler...")
        scheduler.shutdown()
