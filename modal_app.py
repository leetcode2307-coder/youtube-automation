import asyncio
import os
import time
from pathlib import Path
import modal

APP_NAME = "ai-shorts-automation"
VOLUME_NAME = "youtube-shorts-data"
SECRET_NAME = "youtube-shorts-secrets"

volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
app_secret = modal.Secret.from_name(SECRET_NAME)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "tzdata", "ca-certificates")
    .env({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "TZ": "Asia/Kolkata",
        "PYTHONPATH": "/app",
    })
    .pip_install_from_requirements("requirements.txt")
    .workdir("/app")
    .add_local_dir(
        "app",
        remote_path="/app/app",
        ignore=lambda p: "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo"},
    )
)

app = modal.App(APP_NAME)


def _safe_reload() -> None:
    try:
        volume.reload()
    except Exception as exc:
        print(f"[modal] volume.reload skipped: {exc}")


def _safe_commit() -> None:
    try:
        volume.commit()
    except Exception as exc:
        print(f"[modal] volume.commit warning: {exc}")


class _ModalVolumeSyncMiddleware:
    """Pure ASGI middleware (zero body buffering) coordinating Volume sync around I/O."""

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app
        self._active_writes = 0
        self._active_streams = 0
        self._sync_lock = asyncio.Lock()
        self._last_reload_ts = time.monotonic()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.asgi_app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        is_static = path.startswith("/static/") or path == "/favicon.ico"
        is_video = path.startswith("/video/")
        is_mutation = method in ("POST", "PUT", "PATCH", "DELETE")

        if is_static:
            await self.asgi_app(scope, receive, send)
            return

        # Never call volume.reload() while video streaming or file mutations are active
        if not is_mutation and not is_video and self._active_writes == 0 and self._active_streams == 0:
            now = time.monotonic()
            if now - self._last_reload_ts >= 30.0:
                async with self._sync_lock:
                    if self._active_writes == 0 and self._active_streams == 0 and (time.monotonic() - self._last_reload_ts >= 30.0):
                        try:
                            await volume.reload.aio()
                            self._last_reload_ts = time.monotonic()
                        except Exception:
                            pass

        if is_mutation:
            self._active_writes += 1
        if is_video:
            self._active_streams += 1

        try:
            await self.asgi_app(scope, receive, send)
        finally:
            if is_video:
                self._active_streams = max(0, self._active_streams - 1)
            if is_mutation:
                self._active_writes = max(0, self._active_writes - 1)
                if self._active_writes == 0:
                    async with self._sync_lock:
                        if self._active_writes == 0:
                            try:
                                await volume.commit.aio()
                                self._last_reload_ts = time.monotonic()
                            except Exception as exc:
                                print(f"[modal] async volume.commit warning: {exc}")


@app.function(
    image=image,
    volumes={"/data": volume},
    secrets=[app_secret],
    cpu=2.0,
    memory=2048,
    max_containers=1,
    timeout=600,
    routing_region="ap-south",
)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def web():
    # Ensure DATA_DIR points to mounted persistent volume before settings initialization
    os.environ.setdefault("DATA_DIR", "/data")
    os.environ.setdefault("ENABLE_EMBEDDED_SCHEDULER", "false")
    os.environ.setdefault("EXTERNAL_SCHEDULER_ACTIVE", "true")
    os.environ.setdefault("EXTERNAL_SCHEDULER_LABEL", "Modal Cron (UI Synced)")

    from app.server import app as fastapi_app
    from app.storage import storage

    # Reload persistent volume first, then ensure default studio_settings.json exists and commit
    _safe_reload()
    storage.get_studio_config()
    _safe_commit()

    return _ModalVolumeSyncMiddleware(fastapi_app)


@app.function(
    image=image,
    volumes={"/data": volume},
    secrets=[app_secret],
    cpu=0.25,
    memory=512,
    schedule=modal.Cron("*/5 * * * *"),
    timeout=600,
)
def scheduled_dispatcher():
    """Lightweight UI-synced cron dispatcher checking StudioConfig schedules every 5 minutes."""
    os.environ.setdefault("DATA_DIR", "/data")
    _safe_reload()

    from app.scheduler_check import find_due_slots, mark_slot_dispatched

    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    due_slots = find_due_slots(data_dir)
    if not due_slots:
        return []

    from app.pipeline import run_content_pipeline

    triggered = []
    for slot, fire_iso in due_slots:
        # Mark before running to avoid duplicate triggers across overlapping ticks
        mark_slot_dispatched(data_dir, slot, fire_iso)
        _safe_commit()
        pkg = run_content_pipeline(slot=slot)  # type: ignore[arg-type]
        _safe_commit()
        triggered.append({"slot": slot, "run_id": pkg.run_id, "scheduled_for": fire_iso})

    return triggered
