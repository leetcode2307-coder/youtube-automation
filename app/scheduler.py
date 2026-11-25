import threading
import time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from app.logging_config import logger
from app.pipeline import run_content_pipeline
from app.storage import storage

_SCHEDULER_LOCK = threading.RLock()


def scheduled_job_runner(slot: str) -> None:
    """Callback triggered by APScheduler at configured morning/evening cron times."""
    studio_cfg = storage.get_studio_config()
    logger.info("=" * 60)
    logger.info("APScheduler triggered %s production run in %s", slot.upper(), studio_cfg.timezone)
    logger.info("=" * 60)
    try:
        package = run_content_pipeline(slot=slot)  # type: ignore
        logger.info("Scheduled production package generated successfully: %s", package.run_id)
        logger.info("Flow brief is ready in data/flow_exports/%s/FLOW_PROMPTS.txt", package.run_id)
    except Exception as e:
        logger.error("Error executing scheduled %s run: %s", slot, e, exc_info=True)


def _get_zoneinfo(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("Could not load ZoneInfo for %s, falling back to local system timezone", tz_name)
        return None


def configure_scheduler_jobs(scheduler: BackgroundScheduler) -> None:
    """Register or update morning and evening jobs from persisted StudioConfig without duplicates."""
    with _SCHEDULER_LOCK:
        studio_cfg = storage.get_studio_config()
        tz = _get_zoneinfo(studio_cfg.timezone)

        morning_trigger = CronTrigger.from_crontab(studio_cfg.morning_cron, timezone=tz)
        evening_trigger = CronTrigger.from_crontab(studio_cfg.evening_cron, timezone=tz)

        expected_ids = {"morning_shorts_job", "evening_shorts_job"}
        if not scheduler.running:
            # Before start(), APScheduler stores jobs in _pending_jobs where replace_existing is not applied.
            scheduler.remove_all_jobs()
        else:
            for existing in list(scheduler.get_jobs()):
                if existing.id not in expected_ids:
                    scheduler.remove_job(existing.id)

        scheduler.add_job(
            scheduled_job_runner,
            morning_trigger,
            id="morning_shorts_job",
            name=f"Morning Short ({studio_cfg.morning_cron})",
            args=["morning"],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            replace_existing=True,
        )

        scheduler.add_job(
            scheduled_job_runner,
            evening_trigger,
            id="evening_shorts_job",
            name=f"Evening Short ({studio_cfg.evening_cron})",
            args=["evening"],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            replace_existing=True,
        )

        logger.info(
            "Configured APScheduler jobs (morning='%s', evening='%s', tz='%s')",
            studio_cfg.morning_cron,
            studio_cfg.evening_cron,
            studio_cfg.timezone,
        )


def create_scheduler() -> BackgroundScheduler:
    """Create and configure APScheduler with persisted timezone and cron triggers."""
    studio_cfg = storage.get_studio_config()
    tz = _get_zoneinfo(studio_cfg.timezone)
    scheduler = BackgroundScheduler(
        timezone=tz,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        },
    )
    configure_scheduler_jobs(scheduler)
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
