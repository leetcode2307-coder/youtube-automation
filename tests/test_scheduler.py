from app.config import settings
from app.scheduler import create_scheduler


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

    evening_job = scheduler.get_job("evening_shorts_job")
    assert list(evening_job.args) == ["evening"]
    assert evening_job.max_instances == 1
