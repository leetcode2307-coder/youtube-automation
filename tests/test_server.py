from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.emailer import create_approval_record
from app.models import ProductionPackage, Scene, SEOMetadata
from app.server import app
from app.storage import storage

client = TestClient(app)


@pytest.fixture
def test_package(tmp_path, monkeypatch):
    test_data = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data / "approvals.json")

    settings.ensure_directories()

    run_id = "test_server_run"
    scenes = [
        Scene(
            scene_number=i,
            duration_seconds=7,
            narration=f"Narration for scene {i} testing server flow.",
            visual_prompt=f"Visual prompt {i} with 9:16 vertical video layout",
            export_filename=f"scene_0{i}.mp4"
        )
        for i in range(1, 6)
    ]
    metadata = SEOMetadata(
        title="Server API & Approval Security Test",
        description="Testing FastAPI endpoints for secure human-in-the-loop approval.",
        hashtags=["#AIEngineering", "#FastAPI", "#Security"],
        tags=["fastapi", "security", "ai engineering", "approval"]
    )
    pkg = ProductionPackage(
        run_id=run_id,
        slot="morning",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        scheduled_at_local="2026-11-25 07:00:00 IST",
        topic="FastAPI Approval Service",
        hook="How to securely gate AI video uploads with single-use tokens.",
        narration_full="Complete narration for server testing exceeding thirty characters.",
        scenes=scenes,
        total_duration_seconds=35,
        metadata=metadata,
        status="awaiting_approval"
    )
    storage.save_package(pkg)

    # Create dummy final_short.mp4
    final_dir = settings.FINAL_OUTPUT_DIR / run_id
    final_dir.mkdir(parents=True)
    (final_dir / "final_short.mp4").write_bytes(b"TEST_MP4_CONTENT" * 1000)

    return pkg


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_approval_workflow_and_replay_protection(test_package):
    run_id = test_package.run_id

    # 1. Trigger /ready/{run_id}
    res_ready = client.post(f"/ready/{run_id}")
    assert res_ready.status_code == 200
    data = res_ready.json()
    assert data["status"] == "awaiting_approval"
    review_url = data["review_url"]
    assert "token=" in review_url
    raw_token = review_url.split("token=")[1]

    # 2. GET /review/{run_id}?token=... (Review portal render, NO upload)
    res_portal = client.get(f"/review/{run_id}?token={raw_token}")
    assert res_portal.status_code == 200
    assert "Review & Approval Gate" in res_portal.text
    assert test_package.metadata.title in res_portal.text
    # Ensure GET did NOT change status to approved
    pkg_check = storage.load_package(run_id)
    assert pkg_check.status == "awaiting_approval"

    # 3. Explicit POST /approve/{run_id}
    res_approve = client.post(f"/approve/{run_id}", data={"token": raw_token})
    assert res_approve.status_code == 200
    assert "Video Approved" in res_approve.text

    # Verify status in store
    pkg_approved = storage.load_package(run_id)
    assert pkg_approved.status in ("approved", "uploaded_private")

    # 4. REPLAY ATTACK TEST: Attempting to reuse the same token must fail with 409 Conflict
    res_replay = client.post(f"/approve/{run_id}", data={"token": raw_token})
    assert res_replay.status_code == 409
    assert "already been used" in res_replay.json()["detail"]


def test_invalid_and_expired_tokens(test_package):
    run_id = test_package.run_id

    # 1. Invalid token -> 403 Forbidden
    res_bad = client.get(f"/review/{run_id}?token=completely_fake_token_123")
    assert res_bad.status_code == 403

    # 2. Expired token -> 410 Gone
    raw_token, record = create_approval_record(run_id)
    # Manually expire the token
    record.expires_at_utc = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    storage.save_approval_record(record)

    res_expired = client.get(f"/review/{run_id}?token={raw_token}")
    assert res_expired.status_code == 410
    assert "expired" in res_expired.json()["detail"].lower()


def test_dashboard_ui_and_api(test_package):
    # Test dashboard HTML render
    res_dash = client.get("/")
    assert res_dash.status_code == 200
    assert "AI Engineering Shorts Studio" in res_dash.text
    assert test_package.run_id in res_dash.text

    # Test JSON status API
    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    assert res_status.json()["total_packages"] >= 1
