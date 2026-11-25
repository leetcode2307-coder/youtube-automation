import asyncio
from contextlib import asynccontextmanager
import hashlib
import html as html_lib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.assemble import assemble_video
from app.config import settings
from app.emailer import create_approval_record, send_approval_email
from app.intake import extract_scene_number
from app.logging_config import logger
from app.models import ApprovalRecord, PrivacyStatus, SlotType, StudioConfig
from app.pipeline import run_content_pipeline
from app.scheduler import configure_scheduler_jobs, create_scheduler
from app.storage import storage
from app.ui_helpers import (
    STATUS_META,
    build_activity_timeline,
    build_enriched_ledger,
    cron_to_hhmm,
    explain_error,
    get_scheduler_summary,
    get_system_health,
    read_recent_logs,
)

_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,80}$")
_ACTIVE_SCHEDULER = None
_APP_DIR = Path(__file__).resolve().parent

YOUTUBE_CATEGORIES = [
    ("28", "Science & Technology (28)"),
    ("27", "Education (27)"),
    ("22", "People & Blogs (22)"),
    ("24", "Entertainment (24)"),
    ("26", "Howto & Style (26)"),
    ("20", "Gaming (20)"),
    ("25", "News & Politics (25)"),
    ("10", "Music (10)"),
    ("17", "Sports (17)"),
    ("1", "Film & Animation (1)"),
]

POPULAR_TTS_VOICES = [
    ("en-US-ChristopherNeural", "English (US) - Christopher (Male)"),
    ("en-US-AriaNeural", "English (US) - Aria (Female)"),
    ("en-US-GuyNeural", "English (US) - Guy (Male)"),
    ("en-US-JennyNeural", "English (US) - Jenny (Female)"),
    ("en-GB-RyanNeural", "English (UK) - Ryan (Male)"),
    ("en-GB-SoniaNeural", "English (UK) - Sonia (Female)"),
    ("en-IN-PrabhatNeural", "English (India) - Prabhat (Male)"),
    ("en-IN-NeerjaNeural", "English (India) - Neerja (Female)"),
]

POPULAR_GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3.8-27b",
]


class PackageEditPayload(BaseModel):
    title: str = Field(min_length=5, max_length=100)
    description: str = Field(min_length=10, max_length=5000)
    tags: List[str] = Field(min_length=4, max_length=15)
    hashtags: List[str] = Field(min_length=3, max_length=8)
    narration_full: Optional[str] = None
    category_id: str = "28"
    privacy_status: PrivacyStatus = "private"
    synthetic_media_disclosure: bool = True


def _validate_run_id(run_id: str) -> str:
    if not _SAFE_RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run_id format.")
    return run_id


def _resolve_base_url(request: Optional[Request] = None) -> str:
    base = settings.APPROVAL_BASE_URL.rstrip("/")
    if request and base in ("http://127.0.0.1:8000", "http://localhost:8000", ""):
        return str(request.base_url).rstrip("/")
    return base


def _install_windows_asyncio_exception_filter(loop: asyncio.AbstractEventLoop) -> None:
    """Suppress harmless Windows Proactor WinError 10054 client disconnect tracebacks during video streaming."""
    previous_handler = loop.get_exception_handler()

    def _custom_exception_handler(active_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        winerror = getattr(exc, "winerror", None)
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)) and winerror in (10054, 10053, None):
            msg = str(context.get("message", ""))
            if "_ProactorBasePipeTransport" in msg or "connection_lost" in msg or winerror == 10054:
                logger.debug("Client aborted streaming connection (WinError %s): %s", winerror, exc)
                return
        if previous_handler is not None:
            previous_handler(active_loop, context)
        else:
            active_loop.default_exception_handler(context)

    loop.set_exception_handler(_custom_exception_handler)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _ACTIVE_SCHEDULER
    if sys.platform == "win32":
        _install_windows_asyncio_exception_filter(asyncio.get_running_loop())
    if settings.ENABLE_EMBEDDED_SCHEDULER:
        _ACTIVE_SCHEDULER = create_scheduler()
        _ACTIVE_SCHEDULER.start()
        logger.info("Embedded APScheduler started inside FastAPI web service.")
    try:
        yield
    finally:
        if _ACTIVE_SCHEDULER is not None:
            _ACTIVE_SCHEDULER.shutdown(wait=False)
            _ACTIVE_SCHEDULER = None
            logger.info("Embedded APScheduler stopped.")


app = FastAPI(
    title="YouTube Shorts Studio & Review Portal",
    version="2.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))


def verify_token(run_id: str, raw_token: str) -> ApprovalRecord:
    """Validate that the token matches the stored hash, is unexpired, and unconsumed."""
    _validate_run_id(run_id)
    token_hash = hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()
    record = storage.get_approval_by_token_hash(token_hash)

    if not record or record.run_id != run_id:
        logger.warning("Invalid approval token attempt for run %s", run_id)
        raise HTTPException(status_code=403, detail="Invalid approval token or run ID mismatch.")

    if record.consumed_at_utc is not None:
        logger.warning("Token replay attempt for run %s (already consumed at %s)", run_id, record.consumed_at_utc)
        raise HTTPException(
            status_code=409,
            detail=f"This approval link has already been used on {record.consumed_at_utc} (Decision: {record.decision})."
        )

    now_utc = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(record.expires_at_utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now_utc > expires_at:
        logger.warning("Expired token attempt for run %s (expired at %s)", run_id, record.expires_at_utc)
        raise HTTPException(status_code=410, detail="This approval token has expired. Please request a new review link.")

    return record


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Serve browser tab icon so browsers do not log 404 requests."""
    icon_path = _APP_DIR / "static" / "favicon.svg"
    if not icon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found.")
    return FileResponse(
        icon_path,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/health")
def health_check():
    studio_cfg = storage.get_studio_config()
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "embedded_scheduler": settings.ENABLE_EMBEDDED_SCHEDULER,
        "timezone": studio_cfg.timezone,
        "niche": studio_cfg.niche,
    }


def _build_dashboard_context(studio_cfg: StudioConfig) -> dict:
    """Assemble real application data for the Studio Dashboard."""
    packages = storage.list_packages()
    ledger_rows = build_enriched_ledger(packages, studio_cfg.timezone)
    scheduler_info = get_scheduler_summary(studio_cfg, _ACTIVE_SCHEDULER)
    health_items = get_system_health(studio_cfg, _ACTIVE_SCHEDULER)
    timeline = build_activity_timeline(packages, ledger_rows, studio_cfg.timezone)
    log_lines = read_recent_logs(120)

    run_items = []
    attention_runs = []
    counts = {
        "total": len(packages),
        "needs_clips": 0,
        "awaiting_approval": 0,
        "published": len(ledger_rows),
        "failed": 0,
    }

    for p in reversed(packages):
        meta = STATUS_META.get(
            p.status,
            {"label": p.status.replace("_", " ").capitalize(), "tone": "neutral", "step": 1, "next_action": ""},
        )
        run_export_dir = settings.FLOW_EXPORTS_DIR / p.run_id
        existing_clips = {
            extract_scene_number(f.name)
            for f in (run_export_dir.glob("*.mp4") if run_export_dir.exists() else [])
            if extract_scene_number(f.name) > 0 and f.stat().st_size >= 1024
        }
        has_custom_vo = (run_export_dir / "voiceover.mp3").exists()
        video_exists = (settings.FINAL_OUTPUT_DIR / p.run_id / "final_short.mp4").exists()
        is_published = p.status in ("uploaded_private", "uploaded")

        groups = ["all"]
        if p.status in ("generated", "awaiting_flow_export"):
            groups.append("needs_clips")
            counts["needs_clips"] += 1
        if p.status == "awaiting_approval":
            groups.append("awaiting_approval")
            counts["awaiting_approval"] += 1
        if is_published:
            groups.append("published")
        if p.status == "failed":
            groups.append("failed")
            counts["failed"] += 1

        item = {
            "pkg": p,
            "niche": p.niche or studio_cfg.niche,
            "privacy": (p.privacy_status or studio_cfg.default_privacy_status or "private").lower(),
            "status_label": meta["label"],
            "status_tone": meta["tone"],
            "step": meta["step"],
            "next_action": meta["next_action"],
            "error_friendly": explain_error(p.error_message),
            "existing_clips": existing_clips,
            "clips_ready": len(existing_clips),
            "has_custom_vo": has_custom_vo,
            "video_exists": video_exists,
            "is_published": is_published,
            "filter_groups": ",".join(groups),
        }
        run_items.append(item)

        if p.status in ("awaiting_approval", "awaiting_flow_export", "failed", "approved"):
            attention_runs.append(item)

    return {
        "studio_cfg": studio_cfg,
        "run_items": run_items,
        "attention_runs": attention_runs,
        "ledger_rows": ledger_rows,
        "scheduler_info": scheduler_info,
        "health_items": health_items,
        "timeline": timeline,
        "log_lines": log_lines,
        "counts": counts,
        "categories": YOUTUBE_CATEGORIES,
        "tts_voices": POPULAR_TTS_VOICES,
        "tts_voice_values": [v for v, _ in POPULAR_TTS_VOICES],
        "groq_models": POPULAR_GROQ_MODELS,
        "morning_hhmm": cron_to_hhmm(studio_cfg.morning_cron) or "07:00",
        "evening_hhmm": cron_to_hhmm(studio_cfg.evening_cron) or "16:30",
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_view(request: Request):
    """Render the redesigned Shorts Studio SaaS Dashboard."""
    studio_cfg = storage.get_studio_config()
    context = _build_dashboard_context(studio_cfg)
    return templates.TemplateResponse(request, "dashboard.html", context)


# =====================================================================
# Studio Settings, Run Editing, Upload & Workflow APIs
# =====================================================================

@app.get("/api/settings")
def api_get_settings():
    """Return current UI-editable StudioConfig."""
    return storage.get_studio_config().model_dump()


@app.post("/api/settings")
def api_save_settings(config: StudioConfig):
    """Persist StudioConfig changes and live-update embedded scheduler if running."""
    saved = storage.save_studio_config(config)
    if _ACTIVE_SCHEDULER is not None:
        try:
            configure_scheduler_jobs(_ACTIVE_SCHEDULER)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid cron/timezone expression: {e}")
    return {"status": "success", "settings": saved.model_dump()}


@app.get("/api/logs")
def api_get_logs(limit: int = Query(120, ge=10, le=500)):
    """Return recent redacted log lines from logs/automation.log."""
    return {"lines": read_recent_logs(limit)}


@app.post("/api/package/{run_id}")
def api_update_package(run_id: str, payload: PackageEditPayload):
    """Update editable metadata, narration, privacy, and AI-content disclosure for a run."""
    _validate_run_id(run_id)
    pkg = storage.load_package(run_id)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")

    pkg.metadata.title = payload.title.strip()
    pkg.metadata.description = payload.description.strip()
    pkg.metadata.tags = [t.strip() for t in payload.tags if t.strip()]
    pkg.metadata.hashtags = [t if t.startswith("#") else f"#{t}" for t in (h.strip() for h in payload.hashtags) if t]
    pkg.metadata.category_id = payload.category_id.strip() or pkg.metadata.category_id
    pkg.metadata.synthetic_media_disclosure = payload.synthetic_media_disclosure
    pkg.privacy_status = payload.privacy_status
    if payload.narration_full and payload.narration_full.strip():
        pkg.narration_full = payload.narration_full.strip()

    try:
        validated = pkg.model_validate(pkg.model_dump())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    storage.save_package(validated)
    return {"status": "success", "run_id": run_id, "package": validated.model_dump()}


@app.delete("/api/package/{run_id}")
def api_delete_package(run_id: str):
    """Delete a run package and its temporary media files."""
    _validate_run_id(run_id)
    if not storage.delete_package(run_id):
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")
    return {"status": "success", "run_id": run_id}


@app.post("/api/generate")
def api_generate_run(
    slot: SlotType = Query("morning"),
    mock: bool = Query(False),
    niche: Optional[str] = Query(None),
    topic: Optional[str] = Query(None),
):
    """Trigger content pipeline via UI with optional niche/topic override."""
    try:
        pkg = run_content_pipeline(
            slot=slot,
            force_mock=mock,
            custom_topic=topic,
            niche_override=niche,
        )
        return {
            "status": "success",
            "run_id": pkg.run_id,
            "niche": pkg.niche,
            "topic": pkg.topic,
            "title": pkg.metadata.title,
            "duration": pkg.total_duration_seconds,
        }
    except Exception as e:
        logger.error("UI generation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/trigger-scheduled/{slot}")
def api_trigger_scheduled(slot: SlotType):
    """Trigger a scheduled slot generation run (useful for Render Cron Jobs)."""
    try:
        pkg = run_content_pipeline(slot=slot)
        return {
            "status": "success",
            "run_id": pkg.run_id,
            "slot": slot,
            "topic": pkg.topic,
        }
    except Exception as e:
        logger.error("Scheduled endpoint generation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/upload-clips/{run_id}")
async def api_upload_clips(run_id: str, files: List[UploadFile] = File(...)):
    """Upload exported Google Flow scene clips (scene_01.mp4..) and optional voiceover.mp3."""
    _validate_run_id(run_id)
    pkg = storage.load_package(run_id)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")

    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    run_export_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    for upload in files:
        raw_name = Path(upload.filename or "").name
        if not raw_name:
            raise HTTPException(status_code=400, detail="Uploaded file is missing a filename.")
        lower_name = raw_name.lower()
        if not (lower_name.endswith(".mp4") or lower_name == "voiceover.mp3"):
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file '{raw_name}'. Upload scene_01.mp4..scene_0N.mp4 or voiceover.mp3."
            )
        if lower_name.endswith(".mp4") and extract_scene_number(raw_name) <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"MP4 filename '{raw_name}' must follow scene_01.mp4 naming convention."
            )

        target_path = run_export_dir / raw_name
        content = await upload.read()
        if len(content) < 1024:
            raise HTTPException(status_code=400, detail=f"File '{raw_name}' is too small or empty.")
        target_path.write_bytes(content)
        uploaded.append(raw_name)

    found_numbers = {
        extract_scene_number(f.name)
        for f in run_export_dir.glob("*.mp4")
        if extract_scene_number(f.name) > 0 and f.stat().st_size >= 1024
    }
    missing_scenes = [s.scene_number for s in pkg.scenes if s.scene_number not in found_numbers]

    logger.info("Uploaded %d clip(s) for run %s: %s", len(uploaded), run_id, uploaded)
    return {
        "status": "success",
        "run_id": run_id,
        "uploaded": uploaded,
        "found_scenes": sorted(found_numbers),
        "missing_scenes": missing_scenes,
    }


@app.get("/api/clips/{run_id}")
def api_list_clips(run_id: str):
    """List uploaded scene clips and missing scene numbers for a run."""
    _validate_run_id(run_id)
    pkg = storage.load_package(run_id)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")

    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    mp4_files = sorted(f.name for f in run_export_dir.glob("*.mp4")) if run_export_dir.exists() else []
    found_numbers = {extract_scene_number(f) for f in mp4_files if extract_scene_number(f) > 0}
    missing_scenes = [s.scene_number for s in pkg.scenes if s.scene_number not in found_numbers]
    return {
        "run_id": run_id,
        "clips": mp4_files,
        "has_custom_voiceover": (run_export_dir / "voiceover.mp3").exists(),
        "missing_scenes": missing_scenes,
    }


@app.post("/api/assemble/{run_id}")
def api_assemble_run(run_id: str):
    """Trigger video assembly via UI."""
    _validate_run_id(run_id)
    try:
        mp4_path = assemble_video(run_id)
        return {"status": "success", "run_id": run_id, "video_path": str(mp4_path)}
    except Exception as e:
        logger.error("UI assembly failed for %s: %s", run_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/notify/{run_id}")
def api_notify_run(run_id: str, request: Request):
    """Generate review link and optionally send email notification via UI."""
    _validate_run_id(run_id)
    pkg = storage.load_package(run_id)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")

    final_mp4 = settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4"
    if not final_mp4.exists():
        raise HTTPException(
            status_code=400,
            detail="Final assembled MP4 not found. Upload clips and run FFmpeg assembly first."
        )

    studio_cfg = storage.get_studio_config()
    raw_token, record = create_approval_record(run_id)
    email_sent = send_approval_email(pkg, raw_token) if studio_cfg.send_email_on_ready else False
    storage.update_package_status(run_id, "awaiting_approval")
    base_url = _resolve_base_url(request)
    review_url = f"{base_url}/review/{run_id}?token={raw_token}"

    return {
        "status": "success",
        "run_id": run_id,
        "email_sent": email_sent,
        "review_url": review_url,
        "expires_at": record.expires_at_utc,
    }


@app.post("/api/retry-upload/{run_id}")
def api_retry_upload(run_id: str):
    """Retry YouTube upload for an already-approved run."""
    _validate_run_id(run_id)
    pkg = storage.load_package(run_id)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")
    if pkg.status not in ("approved", "uploaded_private", "uploaded"):
        raise HTTPException(
            status_code=400,
            detail=f"Run {run_id} must be approved before uploading (current status: {pkg.status})."
        )
    try:
        from app.youtube import upload_video_to_youtube
        upload_record = upload_video_to_youtube(run_id)
        return {
            "status": "success",
            "run_id": run_id,
            "youtube_video_id": upload_record.youtube_video_id,
            "privacy_status": upload_record.privacy_status,
        }
    except Exception as e:
        logger.error("Retry YouTube upload failed for %s: %s", run_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/brief/{run_id}", response_class=PlainTextResponse)
def api_get_brief(run_id: str):
    """Retrieve raw FLOW_PROMPTS.txt content."""
    _validate_run_id(run_id)
    brief_file = settings.FLOW_EXPORTS_DIR / run_id / "FLOW_PROMPTS.txt"
    if not brief_file.exists():
        raise HTTPException(status_code=404, detail=f"Flow brief not found for {run_id}")
    return brief_file.read_text(encoding="utf-8")


@app.get("/api/status")
def api_get_status():
    """Retrieve full system status as JSON."""
    studio_cfg = storage.get_studio_config()
    packages = storage.list_packages()
    history = storage.get_topic_history()
    scheduler_info = get_scheduler_summary(studio_cfg, _ACTIVE_SCHEDULER)
    return {
        "total_packages": len(packages),
        "recent_packages": [p.model_dump() for p in packages[-5:]],
        "history_count": len(history),
        "groq_model": studio_cfg.groq_model,
        "timezone": studio_cfg.timezone,
        "niche": studio_cfg.niche,
        "default_privacy_status": studio_cfg.default_privacy_status,
        "embedded_scheduler": settings.ENABLE_EMBEDDED_SCHEDULER,
        "scheduler": scheduler_info,
    }


# =====================================================================
# Video Streaming & Review Gate Endpoints
# =====================================================================

@app.post("/ready/{run_id}")
def mark_run_ready(run_id: str, request: Request):
    """Notify system that assembly is complete and send approval email."""
    _validate_run_id(run_id)
    package = storage.load_package(run_id)
    if not package:
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")

    final_mp4 = settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4"
    if not final_mp4.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Final assembled MP4 not found at {final_mp4}. Run assembly first."
        )

    studio_cfg = storage.get_studio_config()
    raw_token, record = create_approval_record(run_id)
    email_sent = send_approval_email(package, raw_token) if studio_cfg.send_email_on_ready else False
    storage.update_package_status(run_id, "awaiting_approval")

    base_url = _resolve_base_url(request)
    review_url = f"{base_url}/review/{run_id}?token={raw_token}"
    return {
        "status": "awaiting_approval",
        "run_id": run_id,
        "email_sent": email_sent,
        "review_url": review_url
    }


@app.get("/video/{run_id}")
def stream_video(run_id: str):
    """Serve the assembled MP4 video file for browser preview with byte-range support."""
    _validate_run_id(run_id)
    final_mp4 = settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4"
    if not final_mp4.exists():
        raise HTTPException(status_code=404, detail="Video file not found.")
    return FileResponse(
        final_mp4,
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        },
    )


@app.get("/review/{run_id}", response_class=HTMLResponse)
def review_page(request: Request, run_id: str, token: str = Query(..., description="Approval secret token")):
    """
    Render human-friendly video review page with editable privacy & AI disclosure controls.
    NOTE: GET requests NEVER trigger approval or upload!
    """
    verify_token(run_id, token)
    package = storage.load_package(run_id)
    if not package:
        raise HTTPException(status_code=404, detail="Package not found.")

    studio_cfg = storage.get_studio_config()
    selected_privacy = (package.privacy_status or studio_cfg.default_privacy_status or "private").lower()
    is_synthetic = bool(package.metadata.synthetic_media_disclosure)

    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "package": package,
            "studio_cfg": studio_cfg,
            "token": token,
            "selected_privacy": selected_privacy,
            "is_synthetic": is_synthetic,
        },
    )


@app.post("/approve/{run_id}", response_class=HTMLResponse)
def approve_run(
    request: Request,
    run_id: str,
    token: str = Form(...),
    privacy_status: Optional[PrivacyStatus] = Form(None),
    title: Optional[str] = Form(None),
    synthetic_media: Optional[str] = Form(None),
    synthetic_media_present: Optional[str] = Form(None),
):
    """Authorize video upload with single-use token consumption and chosen privacy/AI flags."""
    record = verify_token(run_id, token)
    pkg = storage.load_package(run_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found.")

    if title and title.strip():
        pkg.metadata.title = title.strip()[:100]
    if privacy_status in ("private", "unlisted", "public"):
        pkg.privacy_status = privacy_status
    if synthetic_media_present is not None:
        pkg.metadata.synthetic_media_disclosure = bool(synthetic_media)
    storage.save_package(pkg)

    now_utc = datetime.now(timezone.utc).isoformat()
    record.consumed_at_utc = now_utc
    record.decision = "approved"
    storage.save_approval_record(record)
    storage.update_package_status(run_id, "approved")

    upload_result_msg = ""
    studio_url = ""
    try:
        from app.youtube import upload_video_to_youtube
        upload_record = upload_video_to_youtube(
            run_id,
            privacy_override=pkg.privacy_status,
            synthetic_media_override=pkg.metadata.synthetic_media_disclosure,
        )
        yt_id = upload_record.youtube_video_id
        if not yt_id.startswith("mock_yt_"):
            studio_url = f"https://studio.youtube.com/video/{yt_id}/edit"
        upload_result_msg = (
            f"Uploaded with visibility <b>{html_lib.escape(upload_record.privacy_status.upper())}</b> "
            f"(AI content disclosed: <b>{'Yes' if pkg.metadata.synthetic_media_disclosure else 'No'}</b>).<br>"
            f"YouTube Video ID: <span class='mono'>{html_lib.escape(yt_id)}</span>"
        )
    except Exception as e:
        logger.error("YouTube upload invocation failed for %s: %s", run_id, e)
        upload_result_msg = (
            f"Approval recorded, but YouTube upload could not complete: "
            f"{html_lib.escape(str(e))}"
        )

    return templates.TemplateResponse(
        request,
        "decision.html",
        {
            "page_title": "Video Approved",
            "decision": "approved",
            "status_badge": "Approved",
            "heading": "Video Approved & Processed",
            "run_id": run_id,
            "message": upload_result_msg,
            "studio_url": studio_url,
            "timestamp": now_utc,
        },
    )


@app.post("/reject/{run_id}", response_class=HTMLResponse)
def reject_run(request: Request, run_id: str, token: str = Form(...), comments: Optional[str] = Form(None)):
    """Reject video, cancel upload, and consume token."""
    record = verify_token(run_id, token)

    now_utc = datetime.now(timezone.utc).isoformat()
    record.consumed_at_utc = now_utc
    record.decision = "rejected"
    record.review_comments = comments
    storage.save_approval_record(record)
    storage.update_package_status(run_id, "rejected", f"Rejected by approver: {comments or 'No comment'}")

    safe_comments = html_lib.escape(comments or "No comment provided.")
    return templates.TemplateResponse(
        request,
        "decision.html",
        {
            "page_title": "Video Rejected",
            "decision": "rejected",
            "status_badge": "Rejected",
            "heading": "Upload Cancelled",
            "run_id": run_id,
            "message": f"This run was closed without uploading to YouTube.<br><b>Note:</b> {safe_comments}",
            "studio_url": "",
            "timestamp": now_utc,
        },
    )
