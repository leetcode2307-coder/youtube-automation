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
