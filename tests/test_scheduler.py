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
