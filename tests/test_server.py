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
    monkeypatch.setattr(settings, "STUDIO_SETTINGS_FILE", test_data / "studio_settings.json")
    monkeypatch.setattr(settings, "YOUTUBE_CLIENT_SECRETS_FILE", test_data / "client_secret.json")
    monkeypatch.setattr(settings, "YOUTUBE_TOKEN_FILE", test_data / "token.json")
    monkeypatch.setattr(settings, "YOUTUBE_CLIENT_SECRET_JSON", "")
    monkeypatch.setattr(settings, "YOUTUBE_TOKEN_JSON", "")
    monkeypatch.setattr(settings, "YOUTUBE_MOCK_MODE", True)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")

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
    assert client.head("/health").status_code == 200


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
    import html as html_lib
    assert html_lib.escape(test_package.metadata.title) in res_portal.text
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
    # Test dashboard HTML render (including HEAD probes used by Render/health checks)
    res_head = client.head("/")
    assert res_head.status_code == 200
    assert client.head("/dashboard").status_code == 200

    res_dash = client.get("/")
    assert res_dash.status_code == 200
    assert "AI Engineering Shorts Studio" in res_dash.text
    assert test_package.run_id in res_dash.text
    assert "themeToggleBtn" in res_dash.text
    assert "Telugu" in res_dash.text
    assert "Hindi" in res_dash.text
    assert "Tamil" in res_dash.text
    assert "Malayalam" in res_dash.text
    assert "Omni" in res_dash.text
    assert "project ZIP" in res_dash.text
    assert "/static/styles.css?v=" in res_dash.text
    assert "/static/app.js?v=" in res_dash.text
    assert "cfg-use_ai_voiceover" in res_dash.text
    assert "cfg-audio_mode" in res_dash.text

    # Test JSON status API
    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    assert res_status.json()["total_packages"] >= 1


def test_clip_upload_notify_and_retry_upload(test_package):
    run_id = test_package.run_id

    # 1. Upload clips via multipart API
    files = [
        ("files", (f"scene_0{i}.mp4", b"FAKE_MP4_DATA_" * 200, "video/mp4"))
        for i in range(1, 6)
    ]
    res_up = client.post(f"/api/upload-clips/{run_id}", files=files)
    assert res_up.status_code == 200
    up_data = res_up.json()
    assert len(up_data["uploaded"]) == 5
    assert up_data["missing_scenes"] == []

    # 2. Check clips listing endpoint
    res_clips = client.get(f"/api/clips/{run_id}")
    assert res_clips.status_code == 200
    assert len(res_clips.json()["clips"]) == 5

    # 3. Reject invalid extension upload and verify no temp .part files remain
    res_bad_up = client.post(
        f"/api/upload-clips/{run_id}",
        files=[("files", ("bad_clip.avi", b"A" * 2048, "video/x-msvideo"))],
    )
    assert res_bad_up.status_code == 400
    assert list((settings.FLOW_EXPORTS_DIR / run_id).glob("*.part*")) == []

    # 3b. Reject too-small mp4 and verify cleanup of .part file
    res_small = client.post(
        f"/api/upload-clips/{run_id}",
        files=[("files", ("scene_01.mp4", b"tiny", "video/mp4"))],
    )
    assert res_small.status_code == 400
    assert list((settings.FLOW_EXPORTS_DIR / run_id).glob("*.part*")) == []

    # 3c. Single-file per-request uploads (used by parallel frontend XHR queue)
    res_single = client.post(
        f"/api/upload-clips/{run_id}",
        files=[("files", ("scene_01.mp4", b"UPDATED_MP4_DATA_" * 200, "video/mp4"))],
    )
    assert res_single.status_code == 200
    assert res_single.json()["uploaded"] == ["scene_01.mp4"]
    assert res_single.json()["missing_scenes"] == []
    assert list((settings.FLOW_EXPORTS_DIR / run_id).glob("*.part*")) == []

    # 4. Path traversal protection
    res_trav = client.get("/api/clips/..evil")
    assert res_trav.status_code == 400

    # 5. Notify endpoint updates status and returns review URL
    res_notify = client.post(f"/api/notify/{run_id}")
    assert res_notify.status_code == 200
    assert "token=" in res_notify.json()["review_url"]
    assert storage.load_package(run_id).status == "awaiting_approval"

    # 6. Retry upload endpoint works once approved
    storage.update_package_status(run_id, "approved")
    res_retry = client.post(f"/api/retry-upload/{run_id}")
    assert res_retry.status_code == 200
    assert res_retry.json()["youtube_video_id"].startswith("mock_yt_")


def test_api_generate_mock(tmp_path, monkeypatch):
    test_data = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data / "approvals.json")
    settings.ensure_directories()

    res = client.post("/api/generate?slot=evening&mock=true&language=telugu")
    assert res.status_code == 200
    data = res.json()
    assert "evening" in data["run_id"]
    assert data["language"] == "telugu"
    assert 30 <= data["duration"] <= 45


def test_api_generate_non_mock_exercises_get_llm(tmp_path, monkeypatch):
    """Regression test for POST /api/generate with configured GROQ_API_KEY (non-mock)."""
    from langchain_groq import ChatGroq
    from app.llm import MockStructuredOutput

    test_data = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data / "approvals.json")
    monkeypatch.setattr(settings, "STUDIO_SETTINGS_FILE", test_data / "studio_settings.json")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk_test_key_valid_format")
    settings.ensure_directories()

    monkeypatch.setattr(
        ChatGroq,
        "with_structured_output",
        lambda self, schema, **kwargs: MockStructuredOutput(schema),
    )

    res = client.post("/api/generate?slot=morning&niche=AI%20Engineering")
    assert res.status_code == 200
    data = res.json()
    assert "morning" in data["run_id"]
    assert data["niche"] == "AI Engineering"
    assert 30 <= data["duration"] <= 45


def test_studio_settings_niche_package_edit_and_privacy_approval(test_package):
    run_id = test_package.run_id

    # 1. Update Studio & Niche settings via API (custom niche, public default, AI content enabled)
    new_settings = {
        "studio_name": "Space & Physics Studio",
        "niche": "Astrophysics & Space Exploration",
        "niche_domains": "Black Holes, Exoplanets, James Webb Telescope",
        "audience_tone": "Mind-blowing visual storytelling",
        "default_cta": "Follow for daily space mysteries!",
        "category_id": "27",
        "default_privacy_status": "unlisted",
        "synthetic_media_default": True,
        "groq_model": "llama-3.3-70b-versatile",
        "tts_voice": "en-US-AriaNeural",
        "morning_cron": "15 8 * * *",
        "evening_cron": "45 18 * * *",
        "timezone": "Asia/Kolkata",
        "min_duration_seconds": 30,
        "max_duration_seconds": 45,
        "min_scenes": 4,
        "max_scenes": 6,
        "auto_assemble_on_upload": True,
        "auto_open_review_on_assemble": True,
        "send_email_on_ready": False,
    }
    res_set = client.post("/api/settings", json=new_settings)
    assert res_set.status_code == 200
    assert res_set.json()["settings"]["niche"] == "Astrophysics & Space Exploration"

    # Dashboard reflects updated non-hardcoded settings
    res_dash = client.get("/")
    assert res_dash.status_code == 200
    assert "Space &amp; Physics Studio" in res_dash.text
    assert "Astrophysics &amp; Space Exploration" in res_dash.text
    assert "en-US-AriaNeural" in res_dash.text

    # 2. Generate a mock run using custom niche & custom topic
    res_gen = client.post("/api/generate?slot=morning&mock=true&niche=Personal%20Finance&topic=Index%20Fund%20Compounding")
    assert res_gen.status_code == 200
    gen_data = res_gen.json()
    assert gen_data["niche"] == "Personal Finance"
    assert gen_data["topic"] == "Index Fund Compounding"

    # 3. Edit package metadata, privacy, and synthetic flag via UI API
    res_edit = client.post(
        f"/api/package/{run_id}",
        json={
            "title": "Updated Custom Short Title",
            "description": "Updated description from the Studio UI editor.",
            "tags": ["space", "physics", "astronomy", "shorts"],
            "hashtags": ["Space", "#Astrophysics", "#Science"],
            "privacy_status": "public",
            "category_id": "27",
            "synthetic_media_disclosure": True,
        },
    )
    assert res_edit.status_code == 200
    updated_pkg = storage.load_package(run_id)
    assert updated_pkg.privacy_status == "public"
    assert updated_pkg.metadata.category_id == "27"
    assert updated_pkg.metadata.hashtags == ["#Space", "#Astrophysics", "#Science"]

    # 4. Approve via Review Portal with explicit privacy & AI-generated checkbox ticked
    res_ready = client.post(f"/ready/{run_id}")
    assert res_ready.status_code == 200
    raw_token = res_ready.json()["review_url"].split("token=")[1]

    res_portal = client.get(f"/review/{run_id}?token={raw_token}")
    assert res_portal.status_code == 200
    assert 'name="synthetic_media" value="true" checked' in res_portal.text

    res_approve = client.post(
        f"/approve/{run_id}",
        data={
            "token": raw_token,
            "privacy_status": "public",
            "synthetic_media_present": "1",
            "synthetic_media": "true",
        },
    )
    assert res_approve.status_code == 200
    assert "PUBLIC" in res_approve.text

    ledger_rec = storage.get_upload_record(run_id)
    assert ledger_rec is not None
    assert ledger_rec.privacy_status == "public"
    assert ledger_rec.response_json.get("containsSyntheticMedia") is True


def test_static_assets_logs_and_scheduler_summary(test_package):
    res_css = client.get("/static/styles.css")
    assert res_css.status_code == 200
    assert "--color-primary" in res_css.text
    assert '[data-theme="dark"]' in res_css.text

    res_js = client.get("/static/app.js")
    assert res_js.status_code == 200
    assert "navigateTo" in res_js.text
    assert "toggleTheme" in res_js.text

    res_logs = client.get("/api/logs?limit=20")
    assert res_logs.status_code == 200
    assert isinstance(res_logs.json()["lines"], list)

    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    sched = res_status.json()["scheduler"]
    assert "timezone_label" in sched
    assert "soonest" in sched


def test_favicon_and_video_range_headers(test_package):
    res_icon = client.get("/favicon.ico")
    assert res_icon.status_code == 200
    assert "svg" in res_icon.headers.get("content-type", "").lower()

    res_vid = client.get(f"/video/{test_package.run_id}")
    assert res_vid.status_code == 200
    assert res_vid.headers.get("accept-ranges") == "bytes"
    assert res_vid.headers.get("cache-control") == "no-store"


def test_windows_asyncio_connection_reset_filter():
    import asyncio
    from app.server import _install_windows_asyncio_exception_filter

    loop = asyncio.new_event_loop()
    try:
        forwarded = []
        loop.set_exception_handler(lambda _l, ctx: forwarded.append(ctx))
        _install_windows_asyncio_exception_filter(loop)
        handler = loop.get_exception_handler()

        benign = ConnectionResetError("client closed")
        benign.winerror = 10054
        handler(loop, {
            "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost",
            "exception": benign,
        })
        assert forwarded == []

        real_err = RuntimeError("unexpected failure")
        handler(loop, {"message": "Unhandled error", "exception": real_err})
        assert len(forwarded) == 1
        assert forwarded[0]["exception"] is real_err
    finally:
        loop.close()
