from datetime import datetime, timezone
from pathlib import Path
import pytest

from app.config import settings
from app.models import ProductionPackage, Scene, SEOMetadata
from app.storage import storage
from app.youtube import upload_video_to_youtube


@pytest.fixture
def ready_package(tmp_path, monkeypatch):
    test_data = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data / "approvals.json")

    settings.ensure_directories()

    run_id = "test_yt_upload_run"
    final_dir = settings.FINAL_OUTPUT_DIR / run_id
    final_dir.mkdir(parents=True)
    (final_dir / "final_short.mp4").write_bytes(b"MOCK_MP4_VIDEO_BYTES" * 5000)

    metadata = SEOMetadata(
        title="Why vLLM PagedAttention Outperforms Naive Serving",
        description="Detailed technical breakdown of PagedAttention memory fragmentation.",
        hashtags=["#AIEngineering", "#vLLM", "#Inference"],
        tags=["vllm", "pagedattention", "inference", "ai engineering"]
    )
    scenes = [
        Scene(
            scene_number=i,
            duration_seconds=7,
            narration=f"Scene {i} narration.",
            visual_prompt=f"GPU memory allocation diagram, 9:16 vertical render scene {i}",
            export_filename=f"scene_0{i}.mp4"
        )
        for i in range(1, 6)
    ]
    pkg = ProductionPackage(
        run_id=run_id,
        slot="morning",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        scheduled_at_local="2026-11-25 07:00:00 IST",
        topic="vLLM Inference Optimization",
        hook="Why your LLM inference server is wasting 60% of GPU VRAM.",
        narration_full="Why your LLM inference server is wasting 60% of GPU VRAM on fragmentation.",
        scenes=scenes,
        total_duration_seconds=35,
        metadata=metadata,
        status="approved"
    )
    storage.save_package(pkg)
    return pkg


def test_youtube_upload_and_idempotency(ready_package):
    run_id = ready_package.run_id

    # 1. First upload call (mock mode)
    record1 = upload_video_to_youtube(run_id, force_mock=True)
    assert record1 is not None
    assert record1.run_id == run_id
    assert record1.privacy_status == "private"
    assert record1.youtube_video_id.startswith("mock_yt_")

    # Verify upload was recorded in ledger
    ledger_record = storage.get_upload_record(run_id)
    assert ledger_record is not None
    assert ledger_record.youtube_video_id == record1.youtube_video_id

    # Verify status in package was updated to uploaded_private
    pkg_updated = storage.load_package(run_id)
    assert pkg_updated.status == "uploaded_private"

    # 2. IDEMPOTENCY TEST: Call upload a second time
    record2 = upload_video_to_youtube(run_id, force_mock=True)
    # Must return identical record from ledger without creating another upload
    assert record2.youtube_video_id == record1.youtube_video_id
    assert record2.video_sha256 == record1.video_sha256


def test_headless_youtube_requires_token_when_secrets_present(tmp_path, monkeypatch):
    import json
    from app.youtube import get_youtube_service

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "YOUTUBE_CLIENT_SECRETS_FILE", tmp_path / "missing_secret.json")
    monkeypatch.setattr(settings, "YOUTUBE_TOKEN_FILE", tmp_path / "missing_token.json")
    monkeypatch.setattr(
        settings,
        "YOUTUBE_CLIENT_SECRET_JSON",
        json.dumps({"installed": {"client_id": "id", "client_secret": "sec"}}),
    )
    monkeypatch.setattr(settings, "YOUTUBE_TOKEN_JSON", "")

    with pytest.raises(RuntimeError, match="youtube-auth"):
        get_youtube_service(mock=False, interactive=False)


def test_youtube_upload_respects_privacy_and_synthetic_media_flags(ready_package):
    run_id = ready_package.run_id
    record = upload_video_to_youtube(
        run_id,
        force_mock=True,
        privacy_override="unlisted",
        synthetic_media_override=True,
    )
    assert record.privacy_status == "unlisted"
    assert record.response_json["containsSyntheticMedia"] is True
    assert storage.load_package(run_id).status == "uploaded"


def test_cleanup_intermediate_scene_clips_after_publish(ready_package):
    """After end-to-end YouTube publish, intermediate scene_xx.mp4 clips are purged while final Short & captions stay."""
    run_id = ready_package.run_id
    export_dir = settings.FLOW_EXPORTS_DIR / run_id
    final_dir = settings.FINAL_OUTPUT_DIR / run_id
    export_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 6):
        (export_dir / f"scene_0{i}.mp4").write_bytes(b"SCENE_CLIP_BYTES" * 200)
    (export_dir / "FLOW_PROMPTS.txt").write_text("Brief", encoding="utf-8")
    (export_dir / "voiceover.mp3").write_bytes(b"AUDIO" * 100)
    (final_dir / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    (final_dir / "final_short.sha256").write_text("abc123", encoding="utf-8")

    record = upload_video_to_youtube(run_id, force_mock=True)
    assert record.youtube_video_id.startswith("mock_yt_")

    # Intermediate scene_xx.mp4 clips must be deleted
    assert list(export_dir.glob("scene_*.mp4")) == []
    # Final Short, captions, sha256, prompt brief, and package JSON must remain
    assert (final_dir / "final_short.mp4").exists()
    assert (final_dir / "captions.srt").exists()
    assert (final_dir / "final_short.sha256").exists()
    assert (export_dir / "FLOW_PROMPTS.txt").exists()
    assert (settings.PACKAGES_DIR / f"{run_id}.json").exists()
