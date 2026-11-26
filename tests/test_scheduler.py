from app.config import settings
from app.scheduler import configure_scheduler_jobs, create_scheduler


def test_scheduler_jobs_configuration():
    scheduler = create_scheduler()
    jobs = scheduler.get_jobs()

    assert len(jobs) == 2

    job_ids = {j.id for j in jobs}
    assert "morning_shorts_job" in job_ids
    assert "evening_shorts_job" in job_ids

    morning_job = scheduler.get_job("morning_shorts_job")
    assert list(morning_job.args) == ["morning"]
    assert morning_job.max_instances == 1
    assert morning_job.misfire_grace_time == 300

    evening_job = scheduler.get_job("evening_shorts_job")
    assert list(evening_job.args) == ["evening"]
    assert evening_job.max_instances == 1
    assert evening_job.misfire_grace_time == 300
    assert str(scheduler.timezone) in ("Asia/Kolkata", "IST")


def test_scheduler_prevents_duplicate_jobs():
    scheduler = create_scheduler()
    # Add an extraneous job and reconfigure repeatedly
    scheduler.add_job(lambda: None, "interval", seconds=60, id="stale_duplicate_job")
    assert len(scheduler.get_jobs()) == 3

    configure_scheduler_jobs(scheduler)
    configure_scheduler_jobs(scheduler)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 2
    assert {j.id for j in jobs} == {"morning_shorts_job", "evening_shorts_job"}


def test_dynamic_ui_cron_dispatcher_window_and_dedup(tmp_path):
    """Verify UI-edited StudioConfig times trigger once inside a 6-minute window without duplicates."""
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.scheduler_check import find_due_slots, mark_slot_dispatched

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "studio_settings.json").write_text(
        json.dumps({
            "morning_cron": "0 7 * * *",
            "evening_cron": "37 14 * * *",
            "timezone": "Asia/Kolkata",
        }),
        encoding="utf-8",
    )

    ist = ZoneInfo("Asia/Kolkata")
    # 14:35 IST -> neither 07:00 nor 14:37 is due yet
    assert find_due_slots(data_dir, now_utc=datetime(2026, 11, 26, 14, 35, tzinfo=ist)) == []

    # 14:40 IST (3 min after 14:37) -> evening slot is due
    due = find_due_slots(data_dir, now_utc=datetime(2026, 11, 26, 14, 40, tzinfo=ist))
    assert len(due) == 1
    slot, fire_iso = due[0]
    assert slot == "evening"
    assert "14:37:00" in fire_iso

    # Once marked, checking again at 14:42 IST must NOT re-trigger
    mark_slot_dispatched(data_dir, slot, fire_iso)
    assert find_due_slots(data_dir, now_utc=datetime(2026, 11, 26, 14, 42, tzinfo=ist)) == []

    # If user edits evening_cron in UI to 14:43, at 14:45 IST the new time triggers cleanly
    (data_dir / "studio_settings.json").write_text(
        json.dumps({
            "morning_cron": "0 7 * * *",
            "evening_cron": "43 14 * * *",
            "timezone": "Asia/Kolkata",
        }),
        encoding="utf-8",
    )
    due_updated = find_due_slots(data_dir, now_utc=datetime(2026, 11, 26, 14, 45, tzinfo=ist))
    assert len(due_updated) == 1
    assert due_updated[0][0] == "evening"
    assert "14:43:00" in due_updated[0][1]
