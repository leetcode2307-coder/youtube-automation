import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.assemble import assemble_video
from app.config import settings
from app.intake import ZipImportValidationError, inspect_and_stage_zip_import
from app.models import ProductionPackage, Scene, SEOMetadata, StudioConfig
from app.server import app
from app.storage import storage
from app.youtube import cleanup_intermediate_scene_clips

client = TestClient(app)


def _make_real_mp4(path: Path, duration_s: int = 11, color: str = "navy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1080x1920:r=24:d={duration_s}",
            "-c:v",
            "libx264",
            "-t",
            str(duration_s),
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def zip_workflow_env(tmp_path, monkeypatch):
    test_data = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data / "approvals.json")
    monkeypatch.setattr(settings, "STUDIO_SETTINGS_FILE", test_data / "studio_settings.json")
    monkeypatch.setattr(settings, "YOUTUBE_MOCK_MODE", True)
    settings.ensure_directories()

    run_id = "20261127_morning_ziptest"
    scenes = [
        Scene(
            scene_number=1,
            duration_seconds=11,
            narration="Scene one opens with a dramatic lightning storm over the ocean.",
            visual_prompt="9:16 vertical cinematic ocean storm opening shot",
            on_screen_text="Scene 1",
            export_filename="scene_01.mp4",
        ),
        Scene(
            scene_number=2,
            duration_seconds=11,
            narration="Scene two zooms closely onto the brass compass glowing in rain.",
            visual_prompt="9:16 vertical macro compass glow shot",
            on_screen_text="Scene 2",
            export_filename="scene_02.mp4",
        ),
        Scene(
            scene_number=3,
            duration_seconds=11,
            narration="Scene three reveals the golden horizon at sunrise for the payoff.",
            visual_prompt="9:16 vertical sunrise ocean reveal",
            on_screen_text="Scene 3",
            export_filename="scene_03.mp4",
        ),
    ]
    pkg = ProductionPackage(
        run_id=run_id,
        slot="morning",
        created_at_utc="2026-11-27T08:00:00Z",
        scheduled_at_local="2026-11-27 07:00:00 IST",
        niche="Storytelling",
        language="english",
        topic="The Storm Compass Lesson",
        hook="Why sailors never trust calm waters before a storm.",
        narration_full=" ".join(s.narration for s in scenes),
        scenes=scenes,
        total_duration_seconds=33,
        metadata=SEOMetadata(
            title="The Storm Compass Lesson in 33 Seconds",
            description="A visual short story assembled from Google Flow clips.",
            hashtags=["#Shorts", "#Storytelling", "#Morals"],
            tags=["storytelling", "shorts", "moral", "cinematic"],
        ),
        status="awaiting_flow_export",
    )
    storage.save_package(pkg)
    return pkg


def test_playwright_dependency_and_controls_removed(zip_workflow_env):
    reqs = Path("requirements.txt").read_text(encoding="utf-8").lower()
    assert "playwright" not in reqs
    modal_src = Path("modal_app.py").read_text(encoding="utf-8").lower()
    assert "playwright" not in modal_src

    assert not Path("app/flow_login.py").exists()
    assert not Path("app/flow_playwright.py").exists()

    # Removed Playwright API endpoints must not exist
    assert client.get("/api/flow-session").status_code == 404
    assert client.post(f"/api/flow-automate/{zip_workflow_env.run_id}").status_code == 404

    # Legacy settings JSON containing removed Playwright keys loads gracefully without breaking
    cfg = StudioConfig.model_validate({"studio_name": "Manual Flow Studio", "google_pro_email": "user@example.com"})
    assert cfg.studio_name == "Manual Flow Studio"

    res_dash = client.get("/")
    assert res_dash.status_code == 200
    assert "Import Flow Project ZIP" in res_dash.text
    assert "Send Scenes to Google Flow" not in res_dash.text
    assert "flowSessionFileInput" not in res_dash.text


def test_zip_security_corruption_duplicates_and_missing_detection(zip_workflow_env, tmp_path):
    run_id = zip_workflow_env.run_id
    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id

    # 1. Invalid / corrupted ZIP rejected gracefully and original bad ZIP preserved in imports
    bad_zip = io.BytesIO(b"PK_CORRUPT_ZIP_ARCHIVE_DATA" * 10)
    res_bad = client.post(
        f"/api/zip-import/{run_id}/inspect",
        files=[("file", ("broken_flow.zip", bad_zip.getvalue(), "application/zip"))],
    )
    assert res_bad.status_code == 400
    assert list((run_export_dir / "imports").glob("*/original/broken_flow.zip"))

    # 2. Path traversal attack rejected BEFORE extracting anything
    trav_buf = io.BytesIO()
    with zipfile.ZipFile(trav_buf, "w") as zf:
        zf.writestr("../../outside_evil.mp4", b"EVIL" * 500)
    res_trav = client.post(
        f"/api/zip-import/{run_id}/inspect",
        files=[("file", ("traversal.zip", trav_buf.getvalue(), "application/zip"))],
    )
    assert res_trav.status_code == 400
    assert "traversal" in res_trav.json()["detail"].lower()
    assert not (run_export_dir.parent / "outside_evil.mp4").exists()

    # 3. Duplicate scenes + corrupted MP4 + missing scene detection
    valid_clip = tmp_path / "real_valid.mp4"
    _make_real_mp4(valid_clip, duration_s=11, color="navy")

    mixed_buf = io.BytesIO()
    with zipfile.ZipFile(mixed_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(valid_clip, arcname="folder_a/scene_01.mp4")
        zf.write(valid_clip, arcname="folder_b/scene_01.mp4")
        zf.writestr("folder_c/corrupt_scene_02.mp4", b"NOT_A_VALID_MP4_CONTAINER" * 100)

    res_mixed = client.post(
        f"/api/zip-import/{run_id}/inspect",
        files=[("file", ("mixed_duplicates_and_corrupt.zip", mixed_buf.getvalue(), "application/zip"))],
    )
    assert res_mixed.status_code == 200
    rec = res_mixed.json()["import_record"]
    assert rec["status"] == "preview_ready"
    assert rec["duplicate_scenes"] == [1]
    assert "corrupt_scene_02.mp4" in rec["corrupted_files"]
    assert 2 in rec["missing_scenes"] and 3 in rec["missing_scenes"]

    # Attempting to confirm corrupt clip or missing scenes without allow_partial fails cleanly
    corrupt_item = next(it for it in rec["items"] if it["original_filename"] == "corrupt_scene_02.mp4")
    res_confirm_corrupt = client.post(
        f"/api/zip-import/{run_id}/confirm",
        json={
            "import_id": rec["import_id"],
            "mappings": {corrupt_item["member_id"]: 2},
            "process_video": False,
        },
    )
    assert res_confirm_corrupt.status_code == 400
    assert "corrupted" in res_confirm_corrupt.json()["detail"].lower()


def test_full_zip_import_preview_confirm_retry_and_preservation(zip_workflow_env, tmp_path, monkeypatch):
    run_id = zip_workflow_env.run_id
    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    src_dir = tmp_path / "good_clips"

    c1 = src_dir / "flow_take_01_alpha.mp4"
    c2 = src_dir / "flow_take_02_beta.mp4"
    c3 = src_dir / "flow_take_03_gamma.mp4"
    _make_real_mp4(c1, duration_s=11, color="navy")
    _make_real_mp4(c2, duration_s=11, color="teal")
    _make_real_mp4(c3, duration_s=11, color="maroon")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Deliberately out of archive insertion order to verify deterministic sort & mapping
        zf.write(c3, arcname="clips/flow_take_03_gamma.mp4")
        zf.write(c1, arcname="clips/flow_take_01_alpha.mp4")
        zf.write(c2, arcname="clips/flow_take_02_beta.mp4")

    # 1. Upload & inspect ZIP for preview (does NOT auto-process yet!)
    res_inspect = client.post(
        f"/api/zip-import/{run_id}/inspect",
        files=[("file", ("google_flow_storm_project.zip", zip_buf.getvalue(), "application/zip"))],
    )
    assert res_inspect.status_code == 200
    rec = res_inspect.json()["import_record"]
    import_id = rec["import_id"]
    assert rec["status"] == "preview_ready"
    assert rec["missing_scenes"] == []
    assert rec["duplicate_scenes"] == []
    assert rec["corrupted_files"] == []

    # Verify deterministic mapping to scene_01, scene_02, scene_03
    mapped = {it["original_filename"]: it["assigned_filename"] for it in rec["items"]}
    assert mapped == {
        "flow_take_01_alpha.mp4": "scene_01.mp4",
        "flow_take_02_beta.mp4": "scene_02.mp4",
        "flow_take_03_gamma.mp4": "scene_03.mp4",
    }

    # Verify original ZIP and raw clips are preserved BEFORE confirm
    orig_zip = run_export_dir / rec["original_zip_rel_path"]
    assert orig_zip.exists()
    raw_files = [run_export_dir / it["raw_rel_path"] for it in rec["items"]]
    assert all(p.exists() for p in raw_files)
    # Final Short must NOT exist yet until user confirms mapping
    assert not (settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4").exists()

    # 2. Simulate an intermediate assembly failure, confirm intermediate & original files stay preserved, then RETRY
    import app.server as server_module
    real_assemble = server_module.assemble_video

    def _failing_assemble(_run_id):
        raise RuntimeError("Transient FFmpeg error for retry test")

    monkeypatch.setattr(server_module, "assemble_video", _failing_assemble)
    res_fail = client.post(
        f"/api/zip-import/{run_id}/confirm",
        json={"import_id": import_id, "process_video": True},
    )
    assert res_fail.status_code == 400
    assert "preserved for retry" in res_fail.json()["detail"].lower()

    failed_state = storage.load_zip_import(run_id, import_id)
    assert failed_state.status == "failed"
    assert orig_zip.exists()
    assert all(p.exists() for p in raw_files)
    assert (run_export_dir / "scene_01.mp4").exists()
    assert (run_export_dir / "scene_03.mp4").exists()

    # 3. Retry without re-uploading ZIP -> executes voiceover + captions + FFmpeg + validation!
    monkeypatch.setattr(server_module, "assemble_video", real_assemble)
    res_retry = client.post(
        f"/api/zip-import/{run_id}/retry",
        json={"import_id": import_id, "process_video": True},
    )
    assert res_retry.status_code == 200
    retry_data = res_retry.json()
    assert retry_data["assembled"] is True
    assert retry_data["import_record"]["status"] == "completed"
    assert retry_data["clips"] == ["scene_01.mp4", "scene_02.mp4", "scene_03.mp4"]
    assert "token=" in retry_data["review_url"]

    final_mp4 = settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4"
    assert final_mp4.exists()
    assert (settings.FINAL_OUTPUT_DIR / run_id / "captions.srt").exists()
    assert storage.load_package(run_id).status == "awaiting_approval"

    # 4. Verify original ZIP and untouched original raw clips are NEVER deleted even after publish cleanup
    cleanup_intermediate_scene_clips(run_id)
    assert orig_zip.exists()
    assert all(p.exists() for p in raw_files)


def test_app_type_annotations_resolvable_at_runtime():
    """Ensure all type hints in app.* resolve at runtime (catches Python 3.11 vs 3.15 drift)."""
    import importlib
    import pkgutil
    import typing
    import app as app_pkg

    failures = []
    for info in pkgutil.walk_packages(app_pkg.__path__, "app."):
        mod = importlib.import_module(info.name)
        for name, obj in vars(mod).items():
            if getattr(obj, "__module__", None) == mod.__name__ and callable(obj):
                try:
                    typing.get_type_hints(obj)
                except Exception as exc:
                    failures.append(f"{mod.__name__}.{name}: {type(exc).__name__}: {exc}")
    assert failures == []
