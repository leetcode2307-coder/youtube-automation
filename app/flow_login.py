"""
Interactive local Google Flow sign-in helper for capturing a Playwright session cleanly.

Uses Playwright's official ``channel="chrome"`` with a dedicated persistent profile directory
and visible browser window (`headless=False`) without stealth plugins, fingerprint spoofing,
or security bypasses.

Usage:
  python -m app.flow_login --email hanumamaruthichenji@gmail.com
  python -m app.flow_login --email hanumamaruthichenji@gmail.com --push-url https://your-app.modal.run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from app.config import settings
from app.flow_playwright import (
    _GOOGLE_AUTH_COOKIE_NAMES,
    get_session_dir,
    get_session_storage_path,
    normalize_google_email,
    save_flow_session_state,
)


def get_persistent_profile_dir(email: Optional[str]) -> Path:
    """Return dedicated persistent Chrome user_data_dir for Google Flow authentication."""
    norm = normalize_google_email(email)
    profile_dir = get_session_dir(norm) / "chrome_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _is_flow_workspace_ready(page: Any, context: Any) -> bool:
    """Return True only when the browser is in an authenticated Google Flow Studio workspace."""
    cur_url = (page.url or "").lower()
    parsed = urlparse(cur_url)
    host = parsed.netloc
    path = parsed.path

    allowed_host = (
        "flow.google" in host
        or "labs.google" in host
        or host.startswith("127.0.0.1")
        or host.startswith("localhost")
    )
    if not allowed_host:
        return False
    if "/about" in path or "accounts.google" in host or "/signin" in path:
        return False

    try:
        cookies = context.cookies()
        cookie_names = {str(c.get("name", "")) for c in cookies if isinstance(c, dict)}
        has_google_auth = bool(cookie_names & _GOOGLE_AUTH_COOKIE_NAMES)
        has_about_cta = page.get_by_text("Create with Google Flow", exact=False).count() > 0
        has_workspace = (
            page.locator(
                'textarea, [contenteditable="true"], [role="textbox"], '
                'button:has-text("New project"), button:has-text("Create project")'
            ).count()
            > 0
        )
    except Exception:
        return False

    return has_google_auth and has_workspace and not has_about_cta


def _is_google_rejection_page(page: Any) -> bool:
    """Detect Google Sign-In rejection ('This browser or app may not be secure')."""
    cur_url = (page.url or "").lower()
    if "/signin/rejected" in cur_url:
        return True
    try:
        body = (page.locator("body").inner_text(timeout=800) or "").lower()
        return "this browser or app may not be secure" in body or "couldn't sign you in" in body
    except Exception:
        return False


def _push_storage_state(push_url: str, email: str, state_data: Dict[str, Any]) -> str:
    """Upload captured Playwright storage_state.json to the deployed app API."""
    endpoint = f"{push_url.rstrip('/')}/api/flow-session/upload"
    payload = json.dumps({"email": email, "storage_state": state_data}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            print(f"[flow-login] Uploaded session to {endpoint} (HTTP {resp.status}): {body}")
            return body
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(
            f"Session saved locally, but upload to {endpoint} failed with HTTP {exc.code}: {err_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Session saved locally, but could not reach {endpoint}: {exc.reason}"
        ) from exc


def capture_flow_session_interactive(
    email: str,
    *,
    flow_url: Optional[str] = None,
    push_url: Optional[str] = None,
    timeout_seconds: int = 300,
) -> Path:
    """
    Launch local Google Chrome (`channel="chrome"`, `headless=False`) with a dedicated persistent
    browser profile, allow manual sign-in & verification, save `storage_state.json`, and
    optionally upload to `--push-url`.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0.")

    target_email = normalize_google_email(email)
    if not target_email or "@" not in target_email:
        raise ValueError("A valid Google Pro email address is required.")

    target_url = (flow_url or settings.GOOGLE_FLOW_URL).rstrip("/")
    if target_email and "?" not in target_url:
        target_url = f"{target_url}/?authuser={target_email}"

    profile_dir = get_persistent_profile_dir(target_email)

    from playwright.sync_api import sync_playwright

    print(f"[flow-login] Launching local Google Chrome (channel='chrome') for: {target_email}")
    print(f"[flow-login] Dedicated persistent profile: {profile_dir}")
    print("[flow-login] Sign in manually inside Chrome until the authenticated Google Flow Studio workspace opens.")

    state_data: Optional[Dict[str, Any]] = None
    last_url = ""
    saw_rejection = False

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=False,
                viewport={"width": 1366, "height": 900},
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to launch installed Google Chrome (channel='chrome') with profile '{profile_dir}': {exc}. "
                "Verify Google Chrome is installed and close any other Chrome automation window using this profile."
            ) from exc

        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise RuntimeError(f"Could not open '{target_url}' in Google Chrome: {exc}") from exc

            # If Google lands on /about while unauthenticated, click the Sign-In CTA once cleanly.
            try:
                page.wait_for_timeout(1500)
                if "/about" in (urlparse(page.url or "").path or "").lower():
                    cta = page.get_by_text("Create with Google Flow", exact=False).first
                    if cta.is_visible(timeout=2000):
                        cta.click()
            except Exception:
                pass

            deadline = time.monotonic() + float(timeout_seconds)
            logged_in = False
            while time.monotonic() < deadline:
                if page.is_closed():
                    if not context.pages:
                        raise RuntimeError(
                            "Google Chrome window was closed before Flow Studio login finished."
                        )
                    page = context.pages[0]

                last_url = str(page.url or "")
                if not saw_rejection and _is_google_rejection_page(page):
                    saw_rejection = True
                    print(
                        "[flow-login] WARNING: Google rejected sign-in ('This browser or app may not be secure'). "
                        "You can retry in Chrome; waiting for timeout...",
                        file=sys.stderr,
                    )

                if _is_flow_workspace_ready(page, context):
                    page.wait_for_timeout(1500)
                    logged_in = True
                    break

                remaining_ms = int(max(100, min(1500, (deadline - time.monotonic()) * 1000)))
                page.wait_for_timeout(remaining_ms)

            if not logged_in:
                if saw_rejection:
                    raise TimeoutError(
                        f"Google rejected automated Chrome login ('This browser or app may not be secure' at {last_url}). "
                        "Playwright's WebDriver flag cannot be bypassed without stealth tools."
                    )
                raise TimeoutError(
                    f"Timed out after {timeout_seconds}s waiting for authenticated Google Flow Studio "
                    f"(last URL: {last_url or 'unknown'})."
                )

            state_data = context.storage_state()
        finally:
            try:
                context.close()
            except Exception:
                pass

    save_flow_session_state(target_email, state_data)
    saved_path = get_session_storage_path(target_email)
    print(f"[flow-login] Saved Playwright storage_state.json cleanly to: {saved_path}")

    if push_url:
        _push_storage_state(push_url, target_email, state_data)

    return saved_path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Capture authenticated Google Flow Playwright session.")
    parser.add_argument("--email", default=settings.GOOGLE_PRO_EMAIL, help="Google Pro subscription email")
    parser.add_argument("--flow-url", default=settings.GOOGLE_FLOW_URL, help="Google Flow base URL")
    parser.add_argument("--push-url", default="", help="Optional deployed app URL to upload session state")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait for login")
    args = parser.parse_args(argv)

    try:
        capture_flow_session_interactive(
            args.email,
            flow_url=args.flow_url,
            push_url=args.push_url.strip() or None,
            timeout_seconds=args.timeout,
        )
        return 0
    except Exception as exc:
        print(f"[flow-login] Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
