import subprocess
from pathlib import Path
import pytest
from app.assemble import assemble_video, calculate_sha256
from app.config import settings
from app.intake import validate_flow_assets
from app.models import ProductionPackage, Scene, SEOMetadata
from app.storage import storage


def create_synthetic_clip(output_path: Path, duration_s: int = 7) -> None:
    """Generate a lightweight synthetic MP4 test clip using ffmpeg color filter."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"color=c=navy:s=1080x1920:d={duration_s}",
        "-f", "lavfi",
        "-i", f"anullsrc=r=44100:cl=stereo",
        "-c:v", "libx264",
        "-t", str(duration_s),
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def test_asset_intake_validation(tmp_path):
    run_dir = tmp_path / "flow_exports" / "test_run"
    run_dir.mkdir(parents=True)

    metadata = SEOMetadata(
        title="Valid Test Title for Short",
        description="Valid test description for testing asset intake validation.",
        hashtags=["#AI", "#Engineering", "#LLM"],
        tags=["ai", "engineering", "llm", "rag"]
    )
    scenes = [
        Scene(
            scene_number=1,
            duration_seconds=7,
            narration="First scene narration.",
            visual_prompt="Flow prompt 1 with 9:16 vertical render",
            export_filename="scene_01.mp4"
        ),
        Scene(
            scene_number=2,
            duration_seconds=7,
            narration="Second scene narration.",
            visual_prompt="Flow prompt 2 with 9:16 vertical render",
            export_filename="scene_02.mp4"
        ),
        Scene(
            scene_number=3,
            duration_seconds=7,
            narration="Third scene narration.",
            visual_prompt="Flow prompt 3 with 9:16 vertical render",
            export_filename="scene_03.mp4"
        )
    ]
    pkg = ProductionPackage(
        run_id="test_run",
        slot="morning",
        created_at_utc="2026-11-25T01:30:00Z",
        scheduled_at_local="2026-11-25 07:00:00 IST",
        topic="Test Topic",
        hook="Test Hook Line for AI Engineering.",
        narration_full="Complete narration for test package running more than thirty characters.",
        scenes=scenes,
        total_duration_seconds=35,
        metadata=metadata
    )

    # 1. Missing clips -> validation fails
    is_valid, errors, clips = validate_flow_assets(run_dir, pkg)
    assert not is_valid
    assert len(errors) > 0

    # 2. Create scene 1, 2 & 3
    (run_dir / "scene_01.mp4").write_bytes(b"A" * 2048)
    (run_dir / "scene_02.mp4").write_bytes(b"B" * 2048)
    (run_dir / "scene_03.mp4").write_bytes(b"C" * 2048)

    is_valid, errors, clips = validate_flow_assets(run_dir, pkg)
    assert is_valid
    assert len(clips) == 3
    assert clips[0].name == "scene_01.mp4"
    assert clips[1].name == "scene_02.mp4"
    assert clips[2].name == "scene_03.mp4"


def test_ffmpeg_assembly_end_to_end(tmp_path, monkeypatch):
    test_data = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data / "approvals.json")

    settings.ensure_directories()

    run_id = "test_assembly_run"
    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    run_export_dir.mkdir(parents=True)

    # Create 5 synthetic 7-second clips = 35 seconds total
    scenes = []
    for i in range(1, 6):
        clip_file = run_export_dir / f"scene_0{i}.mp4"
        create_synthetic_clip(clip_file, duration_s=7)
        scenes.append(
            Scene(
                scene_number=i,
                duration_seconds=7,
                narration=f"Narration for synthetic scene {i}.",
                visual_prompt=f"Futuristic tech rendering scene {i}, 9:16 vertical video",
                export_filename=f"scene_0{i}.mp4"
            )
        )

    metadata = SEOMetadata(
        title="Testing FFmpeg Assembly Engine",
        description="Testing complete video assembly pipeline.",
        hashtags=["#AIEngineering", "#FFmpeg", "#Python"],
        tags=["ffmpeg", "video assembly", "ai engineering", "python"]
    )
    pkg = ProductionPackage(
        run_id=run_id,
        slot="morning",
        created_at_utc="2026-11-25T01:30:00Z",
        scheduled_at_local="2026-11-25 07:00:00 IST",
        topic="FFmpeg Assembly Architecture",
        hook="Here is how our assembly engine builds 9:16 Shorts flawlessly.",
        narration_full="Here is how our assembly engine builds 9:16 Shorts flawlessly with Python and FFmpeg.",
        scenes=scenes,
        total_duration_seconds=35,
        metadata=metadata,
        status="awaiting_flow_export"
    )
    storage.save_package(pkg)

    # Run assembly
    final_video = assemble_video(run_id)
    assert final_video.exists()
    assert final_video.stat().st_size > 50_000

    sha256_file = settings.FINAL_OUTPUT_DIR / run_id / "final_short.sha256"
    assert sha256_file.exists()
    calculated_hash = calculate_sha256(final_video)
    assert sha256_file.read_text(encoding="utf-8").strip() == calculated_hash

    # Check updated status
    updated_pkg = storage.load_package(run_id)
    assert updated_pkg.status == "awaiting_approval"


def test_ffmpeg_assembly_heterogeneous_clips_and_tts_word_boundary(tmp_path, monkeypatch):
    test_data = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data / "approvals.json")

    settings.ensure_directories()

    run_id = "test_hetero_run"
    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    run_export_dir.mkdir(parents=True)

    # Create 3 clips with different resolutions/framerates and no audio stream (11s each = 33s)
    specs = [("720x1280", "24"), ("1080x1920", "30"), ("540x960", "25")]
    scenes = []
    for i, (size, fps) in enumerate(specs, start=1):
        clip_file = run_export_dir / f"scene_0{i}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=teal:s={size}:r={fps}:d=11",
            "-c:v", "libx264", "-t", "11", "-pix_fmt", "yuv420p",
            str(clip_file),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        scenes.append(
            Scene(
                scene_number=i,
                duration_seconds=11,
                narration=f"Heterogeneous scene {i} narration for FFmpeg normalization.",
                visual_prompt=f"Futuristic 9:16 vertical scene {i} render",
                export_filename=f"scene_0{i}.mp4",
            )
        )

    pkg = ProductionPackage(
        run_id=run_id,
        slot="evening",
        created_at_utc="2026-11-25T01:30:00Z",
        scheduled_at_local="2026-11-25 16:30:00 IST",
        topic="Heterogeneous Clip Normalization",
        hook="Testing mixed resolution clips with neural voiceover.",
        narration_full="Testing mixed resolution clips with neural voiceover and accurate stream mapping.",
        scenes=scenes,
        total_duration_seconds=33,
        metadata=SEOMetadata(
            title="Heterogeneous Clip Assembly Test",
            description="Testing mixed clip resolutions and framerates.",
            hashtags=["#AIEngineering", "#FFmpeg", "#Shorts"],
            tags=["ffmpeg", "shorts", "video", "automation"],
        ),
        status="awaiting_flow_export",
    )
    storage.save_package(pkg)

    final_video = assemble_video(run_id)
    assert final_video.exists()
    srt_file = settings.FINAL_OUTPUT_DIR / run_id / "captions.srt"
    assert srt_file.exists()
    assert "00:00:" in srt_file.read_text(encoding="utf-8")
