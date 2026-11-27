"""
Interactive local Google Flow sign-in helper for capturing a Playwright session cleanly.

Usage:
  python -m app.flow_login --email hanumamaruthichenji@gmail.com
  python -m app.flow_login --email hanumamaruthichenji@gmail.com --push-url https://your-app.modal.run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from app.config import settings
from app.flow_playwright import (
    get_session_storage_path,
    normalize_google_email,
    save_flow_session_state,
)


def capture_flow_session_interactive(
    email: str,
    *,
    flow_url: Optional[str] = None,
    push_url: Optional[str] = None,
    timeout_seconds: int = 300,
) -> Path:
    """Open a visible browser for manual Google sign-in, then save storage_state.json."""
    from playwright.sync_api import sync_playwright

    target_email = normalize_google_email(email)
    target_url = (flow_url or settings.GOOGLE_FLOW_URL).rstrip("/")
    if target_email:
        target_url = f"{target_url}/?authuser={target_email}"

    print(f"[flow-login] Opening visible Chromium for Google Pro account: {target_email}")
    print("[flow-login] Sign in manually inside the browser until Google Flow Studio opens.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded")

        deadline = time.monotonic() + max(60, timeout_seconds)
        logged_in = False
        while time.monotonic() < deadline:
            cur_url = (page.url or "").lower()
            if ("flow.google" in cur_url or "labs.google" in cur_url) and "accounts.google" not in cur_url:
                try:
                    has_studio_ui = page.locator("textarea, button").count() >= 2
                except Exception:
                    has_studio_ui = False
                if has_studio_ui:
                    # Wait briefly for auth cookies to settle
                    page.wait_for_timeout(2000)
                    logged_in = True
                    break
            page.wait_for_timeout(1500)

        if not logged_in:
            browser.close()
            raise TimeoutError("Timed out waiting for manual Google Flow sign-in.")

        state_data = context.storage_state()
        browser.close()

    save_flow_session_state(target_email, state_data)
    saved_path = get_session_storage_path(target_email)
    print(f"[flow-login] Saved Playwright storage_state.json cleanly to: {saved_path}")

    if push_url:
        endpoint = f"{push_url.rstrip('/')}/api/flow-session/upload"
        payload = json.dumps({"email": target_email, "storage_state": state_data}).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            print(f"[flow-login] Uploaded session to {endpoint} (HTTP {resp.status}): {body}")

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
