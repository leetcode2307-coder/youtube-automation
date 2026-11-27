from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from app.assemble import assemble_video
from app.config import settings
from app.emailer import create_approval_record, send_approval_email
from app.intake import (
    extract_and_normalize_flow_archive,
    normalize_flow_clips_sequence,
    validate_flow_assets,
)
from app.logging_config import logger
from app.models import FlowJobState, ProductionPackage
from app.storage import storage

_RUNNING_LOCK = threading.RLock()
_ACTIVE_JOBS: Dict[str, threading.Thread] = {}
_BLOCKED_ANALYTICS_HOSTS = (
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "analytics.google.com",
)


class FlowAutomationError(RuntimeError):
    """Raised when Google Flow Playwright automation encounters an operational failure."""


class FlowAuthRequiredError(FlowAutomationError):
    """Raised cleanly when Google Flow session authentication or human verification is required."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_google_email(email: Optional[str]) -> str:
    """Resolve and normalize Google Pro account email."""
    candidate = (email or "").strip().lower()
    if candidate:
        return candidate
    try:
        cfg_email = (storage.get_studio_config().google_pro_email or "").strip().lower()
        if cfg_email:
            return cfg_email
    except Exception:
        pass
    return (settings.GOOGLE_PRO_EMAIL or "").strip().lower()


def get_session_dir(email: Optional[str]) -> Path:
    """Return isolated directory for an account's Playwright browser session."""
    norm = normalize_google_email(email) or "default"
    slug = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return settings.FLOW_SESSIONS_DIR / slug


def get_session_storage_path(email: Optional[str]) -> Path:
    """Return Playwright storage_state.json path for the specified Google Pro email."""
    return get_session_dir(email) / "storage_state.json"


def _session_meta_path(email: Optional[str]) -> Path:
    return get_session_dir(email) / ".meta.json"


def has_flow_session(email: Optional[str]) -> bool:
    path = get_session_storage_path(email)
    return path.exists() and path.stat().st_size >= 16


def get_flow_session_info(email: Optional[str]) -> Dict[str, Any]:
    """Return secret-free metadata about whether a Playwright Flow session is stored."""
    norm = normalize_google_email(email)
    path = get_session_storage_path(norm)
    meta_path = _session_meta_path(norm)
    cookie_count = 0
    updated_at = ""

    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cookies = raw.get("cookies", [])
            cookie_count = len(cookies) if isinstance(cookies, list) else 0
        except Exception:
            cookie_count = 0

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            updated_at = str(meta.get("updated_at_utc", ""))
        except Exception:
            updated_at = ""

    return {
        "email": norm,
        "has_session": has_flow_session(norm),
        "cookie_count": cookie_count,
        "updated_at_utc": updated_at,
        "session_key": get_session_dir(norm).name if norm else "",
    }


def save_flow_session_state(email: str, state_data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and persist Playwright storage_state JSON cleanly without leaking secrets."""
    norm = normalize_google_email(email)
    if not norm or "@" not in norm:
        raise ValueError("A valid Google Pro email is required to save a Flow session.")
    if not isinstance(state_data, dict) or not isinstance(state_data.get("cookies"), list):
        raise ValueError("Invalid Playwright storage_state JSON: expected top-level object with 'cookies' list.")

    session_dir = get_session_dir(norm)
    session_dir.mkdir(parents=True, exist_ok=True)
    state_path = get_session_storage_path(norm)
    meta_path = _session_meta_path(norm)

    tmp_state = session_dir / f".state.{secrets.token_hex(4)}.json"
    tmp_meta = session_dir / f".meta.{secrets.token_hex(4)}.json"
    try:
        tmp_state.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
        os.replace(tmp_state, state_path)
        tmp_meta.write_text(
            json.dumps({"email": norm, "updated_at_utc": _utc_now_iso()}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_meta, meta_path)
    finally:
        tmp_state.unlink(missing_ok=True)
        tmp_meta.unlink(missing_ok=True)

    logger.info("Saved Google Flow Playwright session for %s (%d cookies)", norm, len(state_data["cookies"]))
    return get_flow_session_info(norm)


def delete_flow_session(email: Optional[str]) -> bool:
    """Delete persisted Playwright session data for an email."""
    session_dir = get_session_dir(email)
    if not session_dir.exists():
        return False
    shutil.rmtree(session_dir, ignore_errors=True)
    logger.info("Cleared Google Flow Playwright session for %s", normalize_google_email(email))
    return True


def _update_job(
    run_id: str,
    *,
    status: Optional[str] = None,
    google_pro_email: Optional[str] = None,
    step: Optional[str] = None,
    progress_percent: Optional[int] = None,
    message: Optional[str] = None,
    clips_extracted: Optional[List[str]] = None,
    error: Optional[str] = None,
    started_at_utc: Optional[str] = None,
    finished_at_utc: Optional[str] = None,
) -> FlowJobState:
    existing = storage.load_flow_job(run_id) or FlowJobState(run_id=run_id)
    updates: Dict[str, Any] = {"updated_at_utc": _utc_now_iso()}
    if status is not None:
        updates["status"] = status
    if google_pro_email is not None:
        updates["google_pro_email"] = google_pro_email
    if step is not None:
        updates["step"] = step
    if progress_percent is not None:
        updates["progress_percent"] = max(0, min(100, int(progress_percent)))
    if message is not None:
        updates["message"] = message
    if clips_extracted is not None:
        updates["clips_extracted"] = list(clips_extracted)
    if error is not None:
        updates["error"] = error
    if started_at_utc is not None:
        updates["started_at_utc"] = started_at_utc
    if finished_at_utc is not None:
        updates["finished_at_utc"] = finished_at_utc
    job = existing.model_copy(update=updates)
    storage.save_flow_job(job)
    return job


def _install_network_optimizer(context: Any) -> None:
    """Abort analytics/tracker bloat without blocking Flow app, media, or local test traffic."""
    def _route_handler(route: Any) -> None:
        try:
            url = route.request.url
            host = (urlparse(url).hostname or "").lower()
            if any(blocked in host for blocked in _BLOCKED_ANALYTICS_HOSTS):
                route.abort()
                return
        except Exception:
            pass
        route.continue_()

    context.route("**/*", _route_handler)


def _is_auth_or_bot_gate(page: Any) -> Optional[str]:
    """Detect Google sign-in redirects or human-verification challenges cleanly without bypassing them."""
    current_url = (page.url or "").lower()
    if "accounts.google.com" in current_url or "/servicelogin" in current_url:
        return "Redirected to Google Sign-In."

    try:
        body_text = (page.locator("body").inner_text(timeout=1500) or "").lower()
    except Exception:
        body_text = ""

    gate_markers = (
        "sign in with google",
        "verify it's you",
        "verify you are human",
        "i'm not a robot",
        "unusual traffic",
        "choose an account",
    )
    for marker in gate_markers:
        if marker in body_text and "generate" not in body_text:
            return f"Authentication or verification prompt detected ('{marker}')."
    return None


def _verify_google_account(page: Any, expected_email: str) -> None:
    """Check visible Google account indicator in Flow DOM when exposed."""
    if not expected_email:
        return
    try:
        emails_found = page.evaluate(
            """() => {
                const attrs = ['aria-label', 'title', 'data-email'];
                const out = [];
                document.querySelectorAll('[aria-label*="@"], [title*="@"], [data-email], .account-email').forEach((el) => {
                    for (const a of attrs) {
                        const val = el.getAttribute(a);
                        if (val && val.includes('@')) out.push(val);
                    }
                    if (el.textContent && el.textContent.includes('@')) out.push(el.textContent.trim());
                });
                return out.slice(0, 8);
            }"""
        )
    except Exception:
        return

    if not emails_found:
        return
    normalized_expected = expected_email.lower()
    if any(normalized_expected in str(candidate).lower() for candidate in emails_found):
        return
    raise FlowAuthRequiredError(
        f"Active Google Flow browser session is signed in as '{emails_found[0]}' instead of '{expected_email}'. "
        f"Please link/sign in with {expected_email} and retry."
    )


def _click_first_visible(page: Any, selectors: List[str], timeout_ms: int = 2200) -> bool:
    """Click the first visible, enabled element matching any selector in order."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=min(700, timeout_ms)):
                loc.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


def _fill_first_visible(page: Any, selectors: List[str], value: str, timeout_ms: int = 3000) -> bool:
    """Fill or type into the first visible prompt/character editor."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=min(900, timeout_ms)):
                loc.click(timeout=timeout_ms)
                try:
                    loc.fill(value, timeout=timeout_ms)
                except Exception:
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.insert_text(value)
                return True
        except Exception:
            continue
    return False


def _configure_flow_project(page: Any, package: ProductionPackage) -> None:
    """Ensure a project workspace is open and select 9:16 + Omni preferences when available."""
    _click_first_visible(
        page,
        [
            'button:has-text("New project")',
            'button:has-text("Create project")',
            'button:has-text("New Project")',
            'button[aria-label*="New project" i]',
            '[data-testid="new-project-btn"]',
        ],
        timeout_ms=2500,
    )
    _click_first_visible(
        page,
        [
            'button:has-text("9:16")',
            '[data-value="9:16"]',
            'button[aria-label*="Portrait" i]',
            '[role="radio"]:has-text("9:16")',
        ],
        timeout_ms=1500,
    )
    _click_first_visible(
        page,
        [
            'button:has-text("Omni")',
            '[role="option"]:has-text("Omni")',
            'button[aria-label*="Omni" i]',
        ],
        timeout_ms=1500,
    )


def _add_flow_characters(page: Any, package: ProductionPackage) -> None:
    """Submit consistent characters in Google Flow UI when Characters panel exists."""
    if not package.characters:
        return
    _click_first_visible(
        page,
        [
            'button:has-text("Characters")',
            'button:has-text("Ingredients")',
            'button:has-text("Elements")',
            '[role="tab"]:has-text("Characters")',
        ],
        timeout_ms=1500,
    )
    for char in package.characters:
        _click_first_visible(
            page,
            [
                'button:has-text("Add character")',
                'button:has-text("New character")',
                '[data-testid="add-character-btn"]',
            ],
            timeout_ms=1200,
        )
        _fill_first_visible(
            page,
            [
                'input[name="character_name"]',
                'input[placeholder*="Character name" i]',
                '[data-testid="character-name-input"]',
            ],
            char.name,
            timeout_ms=1200,
        )
        _fill_first_visible(
            page,
            [
                'textarea[name="character_description"]',
                'textarea[placeholder*="character" i]',
                '[data-testid="character-prompt-input"]',
            ],
            char.visual_description,
            timeout_ms=1200,
        )
        _click_first_visible(
            page,
            [
                'button:has-text("Save character")',
                'button:has-text("Add")',
                '[data-testid="save-character-btn"]',
            ],
            timeout_ms=1200,
        )


def _count_rendered_clips(page: Any) -> int:
    try:
        return int(
            page.evaluate(
                """() => {
                    const readyNodes = document.querySelectorAll(
                        '[data-scene-ready="true"], [data-clip-status="ready"], .flow-clip-card.ready, video'
                    );
                    return readyNodes.length;
                }"""
            )
            or 0
        )
    except Exception:
        return 0


def _submit_and_wait_scene(
    page: Any,
    scene_num: int,
    total_scenes: int,
    prompt_text: str,
    timeout_s: int,
) -> None:
    """Submit a single scene prompt and wait adaptively for clip completion or progress increment."""
    before_count = _count_rendered_clips(page)
    prompt_selectors = [
        'textarea[data-testid="flow-prompt-input"]',
        'textarea[placeholder*="Describe" i]',
        'textarea[placeholder*="prompt" i]',
        'textarea[aria-label*="prompt" i]',
        'textarea',
        '[role="textbox"][contenteditable="true"]',
    ]
    if not _fill_first_visible(page, prompt_selectors, prompt_text, timeout_ms=5000):
        raise FlowAutomationError(
            f"Could not locate Google Flow prompt input while sending scene {scene_num}/{total_scenes}."
        )

    clicked_submit = _click_first_visible(
        page,
        [
            'button[data-testid="flow-generate-btn"]',
            'button:has-text("Generate")',
            'button:has-text("Create")',
            'button[aria-label*="Generate" i]',
            'button[type="submit"]',
        ],
        timeout_ms=3000,
    )
    if not clicked_submit:
        page.keyboard.press("Enter")

    deadline = time.monotonic() + max(15, timeout_s)
    poll_interval = 0.25
    while time.monotonic() < deadline:
        gate_reason = _is_auth_or_bot_gate(page)
        if gate_reason:
            raise FlowAuthRequiredError(gate_reason)

        current_count = _count_rendered_clips(page)
        if current_count > before_count or current_count >= scene_num:
            return

        # Check explicit completed card counter if page tracks data-completed-scenes
        try:
            done_attr = page.locator("[data-completed-scenes]").first.get_attribute("data-completed-scenes", timeout=200)
            if done_attr is not None and int(done_attr) >= scene_num:
                return
        except Exception:
            pass

        page.wait_for_timeout(int(poll_interval * 1000))
        poll_interval = min(1.0, poll_interval * 1.35)

    raise FlowAutomationError(
        f"Timed out after {timeout_s}s waiting for Google Flow scene {scene_num}/{total_scenes} to finish generating."
    )


def _download_project_clips(
    page: Any,
    run_export_dir: Path,
    package: ProductionPackage,
    timeout_s: int,
) -> List[str]:
    """Download project ZIP (or fallback per-scene clips) and normalize strictly to scene_01..0N.mp4."""
    staging_dir = run_export_dir / f".playwright_dl_{secrets.token_hex(4)}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    zip_download_selectors = [
        'button[data-testid="download-zip-btn"]',
        'button:has-text("Download ZIP")',
        'button:has-text("Download project")',
        'button:has-text("Export ZIP")',
        'a:has-text("Download ZIP")',
        'button[aria-label*="Download project" i]',
    ]

    try:
        # 1. Preferred path: Project ZIP download -> extract & rename scene_01..scene_0N.mp4
        for sel in zip_download_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible(timeout=1200):
                    with page.expect_download(timeout=max(30000, timeout_s * 1000)) as dl_info:
                        loc.click(timeout=5000)
                    download = dl_info.value
                    dl_name = download.suggested_filename or f"{package.run_id}_flow_export.zip"
                    dl_path = staging_dir / dl_name
                    download.save_as(str(dl_path))
                    if dl_path.suffix.lower() == ".zip":
                        extracted = extract_and_normalize_flow_archive(
                            dl_path,
                            run_export_dir,
                            expected_scene_count=len(package.scenes),
                        )
                        return extracted
                    return normalize_flow_clips_sequence([dl_path], run_export_dir)
            except Exception as exc:
                logger.debug("ZIP selector '%s' did not complete download (%s); trying next...", sel, exc)

        # 2. Fallback path: per-clip download controls -> normalize filenames to scene_01..scene_0N.mp4
        clip_btn_locators = page.locator(
            'button[data-testid*="download-clip" i], button[aria-label*="Download" i], a[download$=".mp4"]'
        )
        btn_count = clip_btn_locators.count()
        downloaded_files: List[Path] = []
        for idx in range(btn_count):
            btn = clip_btn_locators.nth(idx)
            try:
                if not btn.is_visible(timeout=500):
                    continue
                with page.expect_download(timeout=30000) as dl_info:
                    btn.click(timeout=5000)
                download = dl_info.value
                suggested = Path(download.suggested_filename or f"clip_{idx + 1:02d}.mp4").name
                target = staging_dir / f"{idx + 1:02d}_{suggested}"
                download.save_as(str(target))
                downloaded_files.append(target)
            except Exception:
                continue

        if downloaded_files:
            return normalize_flow_clips_sequence(downloaded_files, run_export_dir)

        raise FlowAutomationError(
            "All scenes were submitted, but no downloadable project ZIP or MP4 clips were exposed on the Google Flow page."
        )
    finally:
        # Clean staging ZIP / temporary downloads immediately after extraction
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def automate_google_flow_run(
    run_id: str,
    *,
    google_pro_email: Optional[str] = None,
    flow_url: Optional[str] = None,
    headless: Optional[bool] = None,
    auto_assemble: Optional[bool] = None,
    scene_timeout_s: Optional[int] = None,
    on_complete_sync: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """
    Execute end-to-end Google Flow automation via Playwright for a run package:
    1. Load persistent authenticated session for `google_pro_email`
    2. Set up project (9:16 / Omni) + recurring Characters
    3. Send each scene prompt sequentially and monitor generation
    4. Download project ZIP, extract & rename to scene_01.mp4..scene_0N.mp4
    5. Optionally auto-assemble video & prepare approval link
    """
    pkg = storage.load_package(run_id)
    if not pkg:
        raise FlowAutomationError(f"Package '{run_id}' not found.")

    studio_cfg = storage.get_studio_config()
    active_email = normalize_google_email(google_pro_email or studio_cfg.google_pro_email)
    target_url = (flow_url or settings.GOOGLE_FLOW_URL).strip()
    is_headless = settings.FLOW_AUTOMATION_HEADLESS if headless is None else bool(headless)
    should_assemble = studio_cfg.auto_assemble_on_upload if auto_assemble is None else bool(auto_assemble)
    per_scene_timeout = scene_timeout_s or settings.FLOW_SCENE_TIMEOUT_SECONDS
    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    run_export_dir.mkdir(parents=True, exist_ok=True)

    started_at = _utc_now_iso()
    _update_job(
        run_id,
        status="running",
        google_pro_email=active_email,
        step="session_check",
        progress_percent=10,
        message=f"Verifying Google Flow session for {active_email or 'configured account'}...",
        error="",
        started_at_utc=started_at,
        finished_at_utc="",
    )

    session_file = get_session_storage_path(active_email)
    is_local_mock_url = urlparse(target_url).hostname in ("127.0.0.1", "localhost", "testserver")
    if not is_local_mock_url and not has_flow_session(active_email):
        msg = (
            f"No saved Google Flow browser session found for '{active_email}'. "
            f"Upload a Playwright storage_state.json for {active_email} in Settings -> Google Flow Session "
            f"(or run 'python -m app.flow_login --email {active_email}') first."
        )
        _update_job(
            run_id,
            status="auth_required",
            step="session_missing",
            progress_percent=10,
            message=msg,
            error=msg,
            finished_at_utc=_utc_now_iso(),
        )
        raise FlowAuthRequiredError(msg)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        msg = f"Playwright is not installed cleanly in this environment: {exc}"
        _update_job(
            run_id,
            status="failed",
            step="playwright_import",
            progress_percent=10,
            message=msg,
            error=msg,
            finished_at_utc=_utc_now_iso(),
        )
        raise FlowAutomationError(msg) from exc

    extracted_clips: List[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=is_headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                ctx_kwargs: Dict[str, Any] = {
                    "accept_downloads": True,
                    "viewport": {"width": 1366, "height": 900},
                }
                if session_file.exists():
                    ctx_kwargs["storage_state"] = str(session_file)
                context = browser.new_context(**ctx_kwargs)
                _install_network_optimizer(context)
                page = context.new_page()

                nav_url = target_url
                if active_email and "?" not in nav_url and not is_local_mock_url:
                    nav_url = f"{nav_url.rstrip('/')}/?authuser={active_email}"

                _update_job(
                    run_id,
                    step="open_flow",
                    progress_percent=18,
                    message=f"Opening Google Flow studio for {active_email}...",
                )
                page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)

                gate_reason = _is_auth_or_bot_gate(page)
                if gate_reason:
                    raise FlowAuthRequiredError(
                        f"{gate_reason} Refresh the Google Flow session for {active_email} in a real browser."
                    )
                _verify_google_account(page, active_email)

                _update_job(
                    run_id,
                    step="setup_characters",
                    progress_percent=24,
                    message=f"Configuring 9:16 Omni project & {len(pkg.characters)} consistent character(s)...",
                )
                _configure_flow_project(page, pkg)
                _add_flow_characters(page, pkg)

                total_scenes = len(pkg.scenes)
                char_names = ", ".join(c.name for c in pkg.characters)
                for idx, scene in enumerate(pkg.scenes, start=1):
                    pct = 28 + int(((idx - 1) / max(1, total_scenes)) * 46)
                    _update_job(
                        run_id,
                        step=f"scene_{idx:02d}",
                        progress_percent=pct,
                        message=f"Sending Scene {idx}/{total_scenes} prompt ({scene.duration_seconds}s) to Google Flow ({active_email})...",
                    )
                    prompt_to_send = scene.visual_prompt.strip()
                    if char_names and not any(c.name.lower() in prompt_to_send.lower() for c in pkg.characters):
                        prompt_to_send = f"{prompt_to_send} (Consistent characters: {char_names}; {scene.duration_seconds}s 9:16)"
                    _submit_and_wait_scene(
                        page,
                        scene_num=idx,
                        total_scenes=total_scenes,
                        prompt_text=prompt_to_send,
                        timeout_s=per_scene_timeout,
                    )

                _update_job(
                    run_id,
                    step="download_extract",
                    progress_percent=80,
                    message="Downloading project ZIP from Google Flow & renaming clips to scene_01..0N.mp4...",
                )
                extracted_clips = _download_project_clips(
                    page,
                    run_export_dir=run_export_dir,
                    package=pkg,
                    timeout_s=per_scene_timeout,
                )

                # Refresh saved session cookies on success when session file already exists
                if session_file.exists():
                    try:
                        context.storage_state(path=str(session_file))
                    except Exception:
                        pass
            finally:
                browser.close()

        is_valid, errs, _ = validate_flow_assets(run_export_dir, pkg)
        if not is_valid:
            raise FlowAutomationError("Extracted Flow clips failed validation: " + "; ".join(errs))

        review_url = ""
        if should_assemble:
            _update_job(
                run_id,
                step="assemble_video",
                progress_percent=92,
                clips_extracted=extracted_clips,
                message="All Flow clips extracted! Assembling 9:16 Short with AI voiceover & captions...",
            )
            assemble_video(run_id)
            raw_token, _ = create_approval_record(run_id)
            latest_pkg = storage.load_package(run_id) or pkg
            if studio_cfg.send_email_on_ready:
                send_approval_email(latest_pkg, raw_token)
            storage.update_package_status(run_id, "awaiting_approval")
            review_url = f"/review/{run_id}?token={raw_token}"

        _update_job(
            run_id,
            status="completed",
            step="done",
            progress_percent=100,
            clips_extracted=extracted_clips,
            message=(
                f"Completed! Downloaded ZIP, renamed {len(extracted_clips)} clip(s) to scene_01..{len(extracted_clips):02d}.mp4"
                + (" & assembled Short for review." if should_assemble else ".")
            ),
            finished_at_utc=_utc_now_iso(),
        )
        if on_complete_sync:
            on_complete_sync()

        return {
            "status": "completed",
            "run_id": run_id,
            "google_pro_email": active_email,
            "clips": extracted_clips,
            "assembled": should_assemble,
            "review_url": review_url,
        }

    except FlowAuthRequiredError as auth_exc:
        _update_job(
            run_id,
            status="auth_required",
            step="auth_required",
            message=str(auth_exc),
            error=str(auth_exc),
            finished_at_utc=_utc_now_iso(),
        )
        if on_complete_sync:
            on_complete_sync()
        raise
    except Exception as exc:
        logger.error("Google Flow Playwright automation failed for %s: %s", run_id, exc)
        _update_job(
            run_id,
            status="failed",
            step="failed",
            message=f"Flow automation failed: {exc}",
            error=str(exc),
            finished_at_utc=_utc_now_iso(),
        )
        if on_complete_sync:
            on_complete_sync()
        raise


def start_background_flow_automation(
    run_id: str,
    *,
    google_pro_email: Optional[str] = None,
    flow_url: Optional[str] = None,
    auto_assemble: Optional[bool] = None,
    on_complete_sync: Optional[Callable[[], None]] = None,
) -> FlowJobState:
    """Launch Flow Playwright automation asynchronously without blocking HTTP requests."""
    pkg = storage.load_package(run_id)
    if not pkg:
        raise FlowAutomationError(f"Package '{run_id}' not found.")

    active_email = normalize_google_email(google_pro_email or storage.get_studio_config().google_pro_email)

    with _RUNNING_LOCK:
        active_thread = _ACTIVE_JOBS.get(run_id)
        if active_thread and active_thread.is_alive():
            existing = storage.load_flow_job(run_id)
            if existing:
                return existing

        job = _update_job(
            run_id,
            status="queued",
            google_pro_email=active_email,
            step="queued",
            progress_percent=5,
            message=f"Queued Google Flow Playwright run for {active_email}...",
            error="",
            clips_extracted=[],
            started_at_utc=_utc_now_iso(),
            finished_at_utc="",
        )

        def _worker() -> None:
            try:
                automate_google_flow_run(
                    run_id,
                    google_pro_email=active_email,
                    flow_url=flow_url,
                    auto_assemble=auto_assemble,
                    on_complete_sync=on_complete_sync,
                )
            except Exception:
                # Status and error details are already persisted on FlowJobState
                pass
            finally:
                with _RUNNING_LOCK:
                    _ACTIVE_JOBS.pop(run_id, None)

        thread = threading.Thread(target=_worker, name=f"flow-pw-{run_id}", daemon=True)
        _ACTIVE_JOBS[run_id] = thread
        thread.start()
        return job
