import pytest
from app.models import ApprovalRecord, ProductionPackage, Scene, SEOMetadata, UploadRecord
from app.storage import StorageManager


def test_storage_manager_package_and_ledger(tmp_path, monkeypatch):
    test_data_dir = tmp_path / "data"
    test_pkg_dir = test_data_dir / "packages"
    test_topic_file = test_data_dir / "topic_history.json"
    test_ledger_file = test_data_dir / "upload_ledger.json"
    test_approvals_file = test_data_dir / "approvals.json"

    from app.config import settings
    monkeypatch.setattr(settings, "DATA_DIR", test_data_dir)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_pkg_dir)
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_topic_file)
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_ledger_file)
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_approvals_file)

    storage = StorageManager()

    # 1. Topic history
    storage.record_topic("Model Context Protocol (MCP)", "run_01")
    history = storage.get_topic_history()
    assert len(history) == 1
    assert history[0]["topic"] == "Model Context Protocol (MCP)"

    # 2. Package save and load
    metadata = SEOMetadata(
        title="Test Short Title",
        description="Test Short Description",
        hashtags=["#AI", "#Engineering", "#LLM"],
        tags=["ai", "engineering", "llm", "rag"]
    )
    scenes = [
        Scene(
            scene_number=i,
            duration_seconds=7,
            narration=f"Scene {i} narration.",
            visual_prompt=f"A dynamic futuristic flow of code and data for scene {i}, 9:16 portrait",
            export_filename=f"scene_0{i}.mp4"
        )
        for i in range(1, 6)
    ]
    pkg = ProductionPackage(
        run_id="run_01",
        slot="morning",
        created_at_utc="2026-11-25T01:30:00Z",
        scheduled_at_local="2026-11-25 07:00:00 IST",
        topic="Model Context Protocol (MCP)",
        hook="Stop building brittle agents.",
        narration_full="Full narration across scenes for AI engineering test breakdown.",
        scenes=scenes,
        total_duration_seconds=35,
        metadata=metadata
    )
    saved_path = storage.save_package(pkg)
    assert saved_path.exists()

    loaded = storage.load_package("run_01")
    assert loaded is not None
    assert loaded.run_id == "run_01"
    assert loaded.status == "generated"

    storage.update_package_status("run_01", "awaiting_approval")
    loaded2 = storage.load_package("run_01")
    assert loaded2.status == "awaiting_approval"

    # 3. Approval record
    appr = ApprovalRecord(
        approval_id="appr_01",
        run_id="run_01",
        token_hash="fake_hash_123",
        approver_email="approver@example.com",
        created_at_utc="2026-11-25T01:35:00Z",
        expires_at_utc="2026-11-26T01:35:00Z"
    )
    storage.save_approval_record(appr)
    found_appr = storage.get_approval_by_token_hash("fake_hash_123")
    assert found_appr is not None
    assert found_appr.run_id == "run_01"

    # 4. Upload ledger
    upload = UploadRecord(
        run_id="run_01",
        video_sha256="abc123hash",
        youtube_video_id="yt_video_999",
        privacy_status="private",
        uploaded_at_utc="2026-11-25T02:00:00Z"
    )
    storage.record_upload(upload)
    ledger_record = storage.get_upload_record("run_01")
    assert ledger_record is not None
    assert ledger_record.youtube_video_id == "yt_video_999"
