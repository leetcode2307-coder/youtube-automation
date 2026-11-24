from pathlib import Path
import pytest
from app.config import settings
from app.pipeline import run_content_pipeline
from app.storage import storage


def test_content_pipeline_execution(tmp_path, monkeypatch):
    test_data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data_dir)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data_dir / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data_dir / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data_dir / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data_dir / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data_dir / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data_dir / "approvals.json")

    settings.ensure_directories()

    package = run_content_pipeline("morning", force_mock=True)

    assert package is not None
    assert "morning" in package.run_id
    assert package.status == "awaiting_flow_export"
    assert 30 <= package.total_duration_seconds <= 45
    assert len(package.scenes) >= 3
    assert package.metadata.title
    assert len(package.metadata.hashtags) >= 3

    # Check files created on disk
    pkg_file = settings.PACKAGES_DIR / f"{package.run_id}.json"
    assert pkg_file.exists()

    flow_file = settings.FLOW_EXPORTS_DIR / package.run_id / "FLOW_PROMPTS.txt"
    assert flow_file.exists()
    content = flow_file.read_text(encoding="utf-8")
    assert "GOOGLE FLOW VIDEO GENERATION BRIEF" in content
    assert package.topic in content
    assert "SCENE 1" in content

    # Check topic history
    history = storage.get_topic_history()
    assert any(h["topic"] == package.topic for h in history)
