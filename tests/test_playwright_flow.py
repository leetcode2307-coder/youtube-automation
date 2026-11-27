import http.server
import io
import json
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import flow_playwright as fp
from app.config import settings
from app.intake import extract_and_normalize_flow_archive, normalize_flow_clips_sequence
from app.models import CharacterProfile, ProductionPackage, Scene, SEOMetadata, StudioConfig
from app.server import app
from app.storage import storage

client = TestClient(app)


def _create_tiny_mp4(path: Path, duration_s: int = 11) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=navy:s=360x640:r=24:d={duration_s}",
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
def isolated_env(tmp_path, monkeypatch):
    test_data = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data / "flow_exports")
    monkeypatch.setattr(settings, "FLOW_SESSIONS_DIR", test_data / "flow_sessions")
    monkeypatch.setattr(settings, "FLOW_JOBS_DIR", test_data / "flow_jobs")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data / "approvals.json")
    monkeypatch.setattr(settings, "STUDIO_SETTINGS_FILE", test_data / "studio_settings.json")
    settings.ensure_directories()
    return test_data


def _build_sample_package(run_id: str = "20261127_morning_pwtest") -> ProductionPackage:
    return ProductionPackage(
        run_id=run_id,
        slot="morning",
        created_at_utc="2026-11-27T06:00:00Z",
        scheduled_at_local="2026-11-27 07:00:00 IST",
        niche="AI Engineering",
        language="english",
        topic="Playwright Flow Automation",
        hook="Watch scenes turn into finished Shorts automatically.",
        narration_full=(
            "First scene shows the creator drafting character sheets in Google Flow. "
            "Second scene shows Omni generating cohesive vertical frames. "
            "Third scene exports and assembles the finished Short."
        ),
        characters=[
            CharacterProfile(
                name="Maya",
                role="Host",
                visual_description="South Asian creative director in dark indigo jacket, rim light, 9:16 cinema",
            )
        ],
        scenes=[
            Scene(
                scene_number=1,
                duration_seconds=11,
                narration="First scene shows the creator drafting character sheets in Google Flow.",
                visual_prompt="Maya sketching holographic storyboard frames in dark studio, push-in shot",
                on_screen_text="Scene 1: Storyboard",
                export_filename="scene_01.mp4",
            ),
            Scene(
                scene_number=2,
                duration_seconds=11,
                narration="Second scene shows Omni generating cohesive vertical frames.",
                visual_prompt="Maya reviewing glowing 9:16 video timeline monitors, smooth orbit shot",
                on_screen_text="Scene 2: Render",
                export_filename="scene_02.mp4",
            ),
            Scene(
                scene_number=3,
                duration_seconds=11,
                narration="Third scene exports and assembles the finished Short.",
                visual_prompt="Maya smiling as completed Short waveform renders, crisp cyan lighting",
                on_screen_text="Scene 3: Publish",
                export_filename="scene_03.mp4",
            ),
        ],
        total_duration_seconds=33,
        metadata=SEOMetadata(
            title="Playwright Automates Google Flow Shorts End to End",
            description="End-to-end Shorts automation from prompts to assembled video.",
            hashtags=["#Shorts", "#AIEngineering", "#Playwright"],
            tags=["playwright", "google flow", "ai shorts", "automation"],
        ),
        status="awaiting_flow_export",
    )


# ---------------- UNIT TESTS ----------------

def test_google_pro_email_default_and_validation(isolated_env):
    cfg = storage.get_studio_config()
    assert cfg.google_pro_email == "hanumamaruthichenji@gmail.com"

    updated = StudioConfig(google_pro_email="  HanumaMaruthiChenji@Gmail.com ")
    assert updated.google_pro_email == "hanumamaruthichenji@gmail.com"

    with pytest.raises(ValueError, match="google_pro_email"):
        StudioConfig(google_pro_email="not-an-email")

    dir_a = fp.get_session_dir("hanumamaruthichenji@gmail.com")
    dir_b = fp.get_session_dir("other.user@gmail.com")
    assert dir_a != dir_b


def test_session_storage_crud_and_secret_hygiene(isolated_env):
    email = "hanumamaruthichenji@gmail.com"
    assert fp.has_flow_session(email) is False

    with pytest.raises(ValueError, match="cookies"):
        fp.save_flow_session_state(email, {"bad": "shape"})

    info = fp.save_flow_session_state(
        email,
        {
            "cookies": [{"name": "SID", "value": "TOP_SECRET_COOKIE_123", "domain": ".google.com", "path": "/"}],
            "origins": [],
        },
    )
    assert info["has_session"] is True
    assert info["has_auth_cookie"] is True
    assert info["email"] == email
    assert info["cookie_count"] == 1
    assert "TOP_SECRET_COOKIE_123" not in json.dumps(info)

    # Also support Chrome Cookie-Editor array format and detect incomplete anonymous sessions
    anon_info = fp.save_flow_session_state(
        email,
        [{"name": "_ga", "value": "GA1.1.123", "domain": ".flow.google.com", "sameSite": "no_restriction"}],
    )
    assert anon_info["has_session"] is True
    assert anon_info["has_auth_cookie"] is False

    # Incomplete anonymous session (like 3 landing-page cookies) fails fast with actionable auth_required
    pkg_incomplete = _build_sample_package("20261127_morning_incomplete")
    storage.save_package(pkg_incomplete)
    with pytest.raises(fp.FlowAuthRequiredError, match="missing Google Sign-In cookies"):
        fp.automate_google_flow_run(
            pkg_incomplete.run_id,
            google_pro_email=email,
            flow_url="https://flow.google.com",
            headless=True,
        )
    assert storage.load_flow_job(pkg_incomplete.run_id).step == "session_incomplete"

    assert fp.delete_flow_session(email) is True
    assert fp.has_flow_session(email) is False


def test_archive_extraction_and_clip_renaming_order(isolated_env, tmp_path):
    run_dir = settings.FLOW_EXPORTS_DIR / "rename_run"
    raw_dir = tmp_path / "raw_clips"
    raw_dir.mkdir(parents=True)

    # Arbitrarily named downloads sorted naturally should rename strictly to scene_01..scene_03
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("zebra_clip.mp4", b"Z_MP4_" * 300)
        zf.writestr("alpha_clip.mp4", b"A_MP4_" * 300)
        zf.writestr("beta_clip.mp4", b"B_MP4_" * 300)
        zf.writestr("voiceover.mp3", b"VO_MP3_" * 300)

    extracted = extract_and_normalize_flow_archive(zip_path, run_dir, expected_scene_count=3)
    assert set(extracted) == {"scene_01.mp4", "scene_02.mp4", "scene_03.mp4", "voiceover.mp3"}
    assert (run_dir / "scene_01.mp4").read_bytes().startswith(b"A_MP4_")
    assert (run_dir / "scene_02.mp4").read_bytes().startswith(b"B_MP4_")
    assert (run_dir / "scene_03.mp4").read_bytes().startswith(b"Z_MP4_")

    loose_a = raw_dir / "render_1.mp4"
    loose_b = raw_dir / "render_2.mp4"
    loose_a.write_bytes(b"LOOSE_1_" * 300)
    loose_b.write_bytes(b"LOOSE_2_" * 300)
    out_dir2 = settings.FLOW_EXPORTS_DIR / "loose_run"
    normalized = normalize_flow_clips_sequence([loose_b, loose_a], out_dir2)
    assert normalized == ["scene_01.mp4", "scene_02.mp4"]


# ---------------- INTEGRATION TESTS ----------------

def test_flow_session_and_automation_api_endpoints(isolated_env, monkeypatch):
    pkg = _build_sample_package("20261127_morning_apitest")
    storage.save_package(pkg)

    # 1. Dashboard renders configured Google Pro email & Playwright controls
    res_dash = client.get("/")
    assert res_dash.status_code == 200
    assert "hanumamaruthichenji@gmail.com" in res_dash.text
    assert "Send Scenes to Google Flow" in res_dash.text
    assert "cfg-google_pro_email" in res_dash.text

    # 2. Session upload (multipart file) + status check + secret hygiene + delete
    state_json = json.dumps({"cookies": [{"name": "SAPISID", "value": "SECRET_VAL"}], "origins": []}).encode("utf-8")
    res_up = client.post(
        "/api/flow-session/upload",
        data={"email": "hanumamaruthichenji@gmail.com"},
        files={"file": ("storage_state.json", state_json, "application/json")},
    )
    assert res_up.status_code == 200
    up_payload = res_up.json()
    assert up_payload["has_session"] is True
    assert "SECRET_VAL" not in json.dumps(up_payload)

    res_sess = client.get("/api/flow-session?email=hanumamaruthichenji@gmail.com")
    assert res_sess.status_code == 200
    assert res_sess.json()["has_session"] is True

    # 3. Async job start + poll integration (mocking runner function cleanly)
    def _fake_runner(run_id, **kwargs):
        fp._update_job(
            run_id,
            status="completed",
            google_pro_email=kwargs.get("google_pro_email") or "hanumamaruthichenji@gmail.com",
            step="done",
            progress_percent=100,
            clips_extracted=["scene_01.mp4", "scene_02.mp4", "scene_03.mp4"],
            message="Completed mocked Playwright run.",
        )
        return {"status": "completed"}

    monkeypatch.setattr(fp, "automate_google_flow_run", _fake_runner)
    res_start = client.post(
        f"/api/flow-automate/{pkg.run_id}",
        json={"google_pro_email": "hanumamaruthichenji@gmail.com"},
    )
    assert res_start.status_code == 202
    assert res_start.json()["google_pro_email"] == "hanumamaruthichenji@gmail.com"

    # Wait briefly for background worker thread
    for _ in range(30):
        res_poll = client.get(f"/api/flow-automate/{pkg.run_id}")
        if res_poll.json()["job"]["status"] == "completed":
            break
        time.sleep(0.05)

    assert res_poll.status_code == 200
    assert res_poll.json()["job"]["progress_percent"] == 100

    res_del = client.delete("/api/flow-session?email=hanumamaruthichenji@gmail.com")
    assert res_del.status_code == 200
    assert res_del.json()["removed"] is True


# ---------------- SYSTEM TEST (REAL HEADLESS CHROMIUM + HTTP SERVER + ZIP + FFMPEG) ----------------

def test_playwright_google_flow_end_to_end_system(isolated_env, tmp_path):
    """
    Full system test:
    Real headless Chromium Playwright -> simulated Google Flow web studio ->
    verifies account mismatch & auth gate protections ->
    submits consistent characters & every scene prompt sequentially ->
    downloads ZIP, extracts & renames clips to scene_01..scene_03.mp4 ->
    validates & assembles final_short.mp4 with AI voiceover -> awaiting_approval!
    """
    pkg = _build_sample_package("20261127_morning_systest")
    storage.save_package(pkg)

    # Pre-generate 3 real compact MP4 clips & bundle inside a ZIP with non-scene filenames
    zip_buffer = io.BytesIO()
    sample_mp4_dir = tmp_path / "mp4_sources"
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, raw_name in enumerate(["flow_cut_a.mp4", "flow_cut_b.mp4", "flow_cut_c.mp4"], start=1):
            src_clip = sample_mp4_dir / raw_name
            _create_tiny_mp4(src_clip, duration_s=11)
            zf.write(src_clip, arcname=raw_name)
    zip_bytes = zip_buffer.getvalue()

    server_log = {"submitted_prompts": [], "characters": [], "aspect": None, "model": None}

    class MockFlowHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            if self.path.startswith("/about"):
                html = (
                    b"<html><body><h1>Google Flow</h1>"
                    b"<span>Create with Google Flow</span>"
                    b"<p>Explore Google AI Subscriptions</p></body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            if self.path.startswith("/auth-gate"):
                html = b"<html><body><h1>Sign in with Google</h1><p>Verify it's you to continue</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            if self.path.startswith("/wrong-account"):
                html = (
                    b'<html><body><span data-email="other.person@gmail.com">other.person@gmail.com</span>'
                    b'<textarea placeholder="Describe scene"></textarea><button>Generate</button></body></html>'
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            if self.path.startswith("/download-project.zip"):
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="google_flow_project.zip"')
                self.send_header("Content-Length", str(len(zip_bytes)))
                self.end_headers()
                self.wfile.write(zip_bytes)
                return

            html = """<!doctype html>
<html>
<head><title>Google Flow Studio (Simulated)</title></head>
<body>
  <div class="account-pill" data-email="hanumamaruthichenji@gmail.com">hanumamaruthichenji@gmail.com</div>
  <div>
    <button onclick="document.body.dataset.project='created'">New project</button>
    <button onclick="fetch('/event?k=aspect&v=9:16')">9:16</button>
    <button onclick="fetch('/event?k=model&v=Omni')">Omni</button>
  </div>
  <div>
    <button onclick="document.getElementById('charForm').style.display='block'">Characters</button>
    <div id="charForm" style="display:none;">
      <input name="character_name" placeholder="Character name">
      <textarea name="character_description" placeholder="Character description"></textarea>
      <button onclick="saveChar()">Save character</button>
    </div>
  </div>
  <div>
    <textarea data-testid="flow-prompt-input" placeholder="Describe your scene prompt"></textarea>
    <button data-testid="flow-generate-btn" onclick="submitPrompt()">Generate</button>
  </div>
  <div id="clips"></div>
  <button id="dlZipBtn" data-testid="download-zip-btn" style="display:none;" onclick="window.location='/download-project.zip'">Download ZIP</button>
  <script>
    function saveChar() {
      const name = document.querySelector('input[name="character_name"]').value;
      const desc = document.querySelector('textarea[name="character_description"]').value;
      fetch('/character', { method: 'POST', body: JSON.stringify({ name, desc }) });
    }
    async function submitPrompt() {
      const prompt = document.querySelector('[data-testid="flow-prompt-input"]').value;
      await fetch('/prompt', { method: 'POST', body: JSON.stringify({ prompt }) });
      const clipsDiv = document.getElementById('clips');
      const card = document.createElement('div');
      card.className = 'flow-clip-card ready';
      card.setAttribute('data-scene-ready', 'true');
      card.textContent = 'Clip ' + (clipsDiv.children.length + 1) + ': ' + prompt;
      clipsDiv.appendChild(card);
      document.querySelector('[data-testid="flow-prompt-input"]').value = '';
      if (clipsDiv.children.length >= 3) {
        document.getElementById('dlZipBtn').style.display = 'inline-block';
      }
    }
  </script>
</body>
</html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length).decode("utf-8", errors="ignore")
            data = json.loads(body or "{}")
            if self.path == "/prompt":
                server_log["submitted_prompts"].append(data.get("prompt", ""))
            elif self.path == "/character":
                server_log["characters"].append(data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockFlowHandler)
    port = httpd.server_port
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    try:
        base_url = f"http://127.0.0.1:{port}"

        # 1. Verify Playwright detects /about marketing page and auth gate without bypassing
        with pytest.raises(fp.FlowAuthRequiredError, match="marketing page"):
            fp.automate_google_flow_run(
                pkg.run_id,
                google_pro_email="hanumamaruthichenji@gmail.com",
                flow_url=f"{base_url}/about",
                headless=True,
                auto_assemble=False,
                scene_timeout_s=15,
            )
        assert (settings.FLOW_JOBS_DIR / f"{pkg.run_id}_debug.png").exists()
        assert client.get(f"/api/flow-automate/{pkg.run_id}/screenshot").status_code == 200

        with pytest.raises(fp.FlowAuthRequiredError):
            fp.automate_google_flow_run(
                pkg.run_id,
                google_pro_email="hanumamaruthichenji@gmail.com",
                flow_url=f"{base_url}/auth-gate",
                headless=True,
                auto_assemble=False,
                scene_timeout_s=15,
            )
        assert storage.load_flow_job(pkg.run_id).status == "auth_required"

        # 2. Verify Playwright detects mismatched Google account cleanly
        with pytest.raises(fp.FlowAuthRequiredError, match="other.person@gmail.com"):
            fp.automate_google_flow_run(
                pkg.run_id,
                google_pro_email="hanumamaruthichenji@gmail.com",
                flow_url=f"{base_url}/wrong-account",
                headless=True,
                auto_assemble=False,
                scene_timeout_s=15,
            )

        # 3. Full successful end-to-end Playwright flow -> ZIP download -> extract & rename -> FFmpeg assemble!
        result = fp.automate_google_flow_run(
            pkg.run_id,
            google_pro_email="hanumamaruthichenji@gmail.com",
            flow_url=base_url,
            headless=True,
            auto_assemble=True,
            scene_timeout_s=30,
        )

        assert result["status"] == "completed"
        assert result["google_pro_email"] == "hanumamaruthichenji@gmail.com"
        assert result["clips"] == ["scene_01.mp4", "scene_02.mp4", "scene_03.mp4"]
        assert len(server_log["submitted_prompts"]) == 3
        assert "Maya sketching" in server_log["submitted_prompts"][0]
        assert any(c.get("name") == "Maya" for c in server_log["characters"])

        run_export_dir = settings.FLOW_EXPORTS_DIR / pkg.run_id
        for idx in range(1, 4):
            assert (run_export_dir / f"scene_0{idx}.mp4").exists()
        # Ensure staging ZIP archive was cleaned up immediately
        assert list(run_export_dir.glob("*.zip")) == []

        final_mp4 = settings.FINAL_OUTPUT_DIR / pkg.run_id / "final_short.mp4"
        assert final_mp4.exists()
        assert final_mp4.stat().st_size > 10_000
        assert (settings.FINAL_OUTPUT_DIR / pkg.run_id / "captions.srt").exists()
        assert storage.load_package(pkg.run_id).status == "awaiting_approval"
        assert storage.load_flow_job(pkg.run_id).progress_percent == 100
    finally:
        httpd.shutdown()
        httpd.server_close()
