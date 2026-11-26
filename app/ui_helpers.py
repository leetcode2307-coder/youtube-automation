import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.emailer import _is_placeholder
from app.models import ProductionPackage, StudioConfig
from app.youtube import _load_client_secrets_config, _load_existing_credentials

STATUS_META: Dict[str, Dict[str, Any]] = {
    "scheduled": {
        "label": "Scheduled",
        "tone": "info",
        "step": 1,
        "next_action": "Waiting for scheduled start time.",
    },
    "generating": {
        "label": "Creating your script",
        "tone": "warning",
        "step": 1,
        "next_action": "AI is writing your script and visual prompts.",
    },
    "generated": {
        "label": "Script ready",
        "tone": "info",
        "step": 1,
        "next_action": "Review your visual prompts and prepare clips.",
    },
    "awaiting_flow_export": {
        "label": "Waiting for your clips",
        "tone": "amber",
        "step": 2,
        "next_action": "Upload your scene video clips so the system can assemble your Short.",
    },
    "assembling": {
        "label": "Preparing your video",
        "tone": "warning",
        "step": 3,
        "next_action": "Combining clips, voiceover, and captions into a 9:16 video.",
    },
    "awaiting_approval": {
        "label": "Waiting for your approval",
        "tone": "purple",
        "step": 4,
        "next_action": "Watch the preview and approve to upload to YouTube.",
    },
    "approved": {
        "label": "Approved (upload pending)",
        "tone": "info",
        "step": 4,
        "next_action": "Click Retry Upload to finish sending to YouTube.",
    },
    "rejected": {
        "label": "Rejected",
        "tone": "danger",
        "step": 4,
        "next_action": "Closed without uploading. You can delete or generate a new run.",
    },
    "uploaded_private": {
        "label": "Published privately",
        "tone": "success",
        "step": 4,
        "next_action": "Ready in YouTube Studio as Private.",
    },
    "uploaded": {
        "label": "Published to YouTube",
        "tone": "success",
        "step": 4,
        "next_action": "Live on your YouTube channel.",
    },
    "failed": {
        "label": "Something went wrong",
        "tone": "danger",
        "step": 2,
        "next_action": "Check the error summary below and retry the step.",
    },
}


def explain_error(error_message: Optional[str]) -> str:
    """Provide a plain-English explanation for common pipeline/assembly/upload errors."""
    if not error_message:
        return ""
    low = error_message.lower()
    if "missing scene clips" in low or "missing expected clip" in low:
        return "Some scene clips are missing. Upload all numbered scene MP4 files (for example scene_01.mp4, scene_02.mp4) and try again."
    if "duration" in low and ("outside" in low or "range" in low):
        return "The combined video length fell outside your configured duration bounds (usually 30–45 seconds). Adjust clip lengths or duration limits in Settings."
    if "rate_limit" in low or "429" in low:
        return "Your AI model hit a temporary rate limit. Wait a minute or switch to another Groq model in Settings."
    if "ffmpeg" in low:
        return "Video processing encountered a problem with one of the uploaded MP4 files. Re-export the clips and upload again."
    if "youtube" in low or "token" in low or "oauth" in low:
        return "YouTube authorization needs attention. Connect or refresh your YouTube OAuth token."
    if "tts" in low or "voiceover" in low:
        return "Voiceover generation could not complete. Retry assembly or upload a custom voiceover.mp3 file."
    if "tool_use_failed" in low or "otpm" in low:
        return "The AI model could not finish formatting its structured response cleanly. Try running again or switch to llama-3.3-70b-versatile in Settings."
    if "validationerror" in low or "string_too_short" in low:
        return "Generated metadata or script fields did not meet required length bounds. Regenerate the run or edit the fields directly."
    return "An unexpected issue interrupted this step. Review the details below and retry."


def cron_to_hhmm(cron_expr: str) -> str:
    """Convert 'M H * * *' cron string to 'HH:MM', or '' if custom."""
    parts = cron_expr.strip().split()
    if len(parts) == 5 and parts[2:] == ["*", "*", "*"] and parts[0].isdigit() and parts[1].isdigit():
        return f"{int(parts[1]):02d}:{int(parts[0]):02d}"
    return ""


def format_cron_human(cron_expr: str) -> str:
    hhmm = cron_to_hhmm(cron_expr)
    return hhmm if hhmm else cron_expr


def _format_local_dt(iso_str: Optional[str], tz_name: str) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(tz_name)
        local_dt = dt.astimezone(tz)
        abbr = local_dt.tzname() or tz_name
        return local_dt.strftime(f"%b %d, %Y at %H:%M {abbr}")
    except Exception:
        return iso_str[:16].replace("T", " ")


def _next_fire_info(cron_expr: str, tz_name: str) -> Dict[str, str]:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now_local = datetime.now(tz)
    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)
        next_dt = trigger.get_next_fire_time(None, now_local)
        if not next_dt:
            return {"formatted": "Not scheduled", "relative": "—", "iso": ""}
        delta_mins = max(0, int((next_dt - now_local).total_seconds() // 60))
        hrs, mins = divmod(delta_mins, 60)
        if delta_mins < 60:
            rel = f"in {mins}m"
        elif hrs < 24:
            rel = f"in {hrs}h {mins}m"
        else:
            days = hrs // 24
            rel = f"in {days}d {hrs % 24}h"
        day_label = "Today" if next_dt.date() == now_local.date() else next_dt.strftime("%a, %b %d")
        abbr = next_dt.tzname() or "IST"
        return {
            "formatted": f"{day_label} at {next_dt.strftime('%H:%M')} {abbr}",
            "relative": rel,
            "iso": next_dt.isoformat(),
        }
    except Exception as e:
        return {"formatted": f"Invalid schedule ({e})", "relative": "—", "iso": ""}


def get_scheduler_summary(studio_cfg: StudioConfig, active_scheduler: Any = None) -> Dict[str, Any]:
    """Compute real scheduler state and next execution times in configured timezone."""
    is_active = bool(
        (active_scheduler is not None and getattr(active_scheduler, "running", False))
        or settings.ENABLE_EMBEDDED_SCHEDULER
    )
    tz_label = f"{studio_cfg.timezone} (IST)" if studio_cfg.timezone == "Asia/Kolkata" else studio_cfg.timezone

    morning_next = _next_fire_info(studio_cfg.morning_cron, studio_cfg.timezone)
    evening_next = _next_fire_info(studio_cfg.evening_cron, studio_cfg.timezone)

    candidates = [
        {"slot": "Morning", "cron": studio_cfg.morning_cron, **morning_next},
        {"slot": "Evening", "cron": studio_cfg.evening_cron, **evening_next},
    ]
    valid_candidates = [c for c in candidates if c["iso"]]
    soonest = min(valid_candidates, key=lambda c: c["iso"]) if valid_candidates else candidates[0]

    return {
        "active": is_active,
        "status_label": "Active (Automatic)" if is_active else "Inactive (Manual / External Cron)",
        "timezone_label": tz_label,
        "morning_time": format_cron_human(studio_cfg.morning_cron),
        "evening_time": format_cron_human(studio_cfg.evening_cron),
        "morning_next": morning_next,
        "evening_next": evening_next,
        "soonest": soonest,
    }


def get_system_health(studio_cfg: StudioConfig, active_scheduler: Any = None) -> List[Dict[str, str]]:
    """Return safe, secret-free integration status checks."""
    groq_ok = bool(settings.GROQ_API_KEY and "your_groq_api_key" not in settings.GROQ_API_KEY)
    yt_secrets = _load_client_secrets_config() is not None
    yt_creds = _load_existing_credentials() if yt_secrets else None
    yt_ready = bool(yt_creds and (yt_creds.valid or yt_creds.refresh_token))

    smtp_ok = (
        bool(settings.SMTP_HOST)
        and not _is_placeholder(settings.SMTP_USERNAME)
        and not _is_placeholder(settings.SMTP_PASSWORD)
        and not _is_placeholder(settings.APPROVAL_RECIPIENT_EMAIL)
    )
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    scheduler_active = bool(
        (active_scheduler is not None and getattr(active_scheduler, "running", False))
        or settings.ENABLE_EMBEDDED_SCHEDULER
    )

    if yt_ready:
        yt_state, yt_tone, yt_detail = "Connected", "success", "OAuth token ready for private/unlisted/public uploads."
    elif yt_secrets:
        yt_state, yt_tone, yt_detail = "Authorization required", "amber", "Client secrets found, but OAuth token is missing or expired. Run 'python cli.py youtube-auth'."
    else:
        yt_state, yt_tone, yt_detail = "Dry-run mode", "info", "No YouTube credentials configured; approvals simulate uploads safely."

    return [
        {
            "name": "AI Script Writer (Groq)",
            "status": "Connected" if groq_ok else "Mock Mode",
            "tone": "success" if groq_ok else "amber",
            "detail": f"Model: {studio_cfg.groq_model}" if groq_ok else "No Groq API key configured; using offline mock responses.",
        },
        {
            "name": "YouTube Data API v3",
            "status": yt_state,
            "tone": yt_tone,
            "detail": yt_detail,
        },
        {
            "name": "Email Approval Alerts (SMTP)",
            "status": "Configured" if smtp_ok else "Dry-run mode",
            "tone": "success" if smtp_ok else "info",
            "detail": f"Server: {settings.SMTP_HOST}:{settings.SMTP_PORT}" if smtp_ok else "SMTP credentials not set; review links appear in the portal and logs.",
        },
        {
            "name": "Video Engine (FFmpeg)",
            "status": "Available" if ffmpeg_ok else "Missing",
            "tone": "success" if ffmpeg_ok else "danger",
            "detail": f"Voiceover: {studio_cfg.tts_voice}" if ffmpeg_ok else "FFmpeg binary was not found on PATH.",
        },
        {
            "name": "Daily Scheduler",
            "status": "Running" if scheduler_active else "Manual Only",
            "tone": "success" if scheduler_active else "info",
            "detail": f"{studio_cfg.timezone} ({format_cron_human(studio_cfg.morning_cron)} & {format_cron_human(studio_cfg.evening_cron)})",
        },
    ]


def load_upload_ledger() -> Dict[str, Dict[str, Any]]:
    if not settings.UPLOAD_LEDGER_FILE.exists():
        return {}
    try:
        with open(settings.UPLOAD_LEDGER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_enriched_ledger(packages: List[ProductionPackage], tz_name: str) -> List[Dict[str, Any]]:
    """Join upload ledger entries with package metadata and local video preview availability."""
    ledger = load_upload_ledger()
    pkg_map = {p.run_id: p for p in packages}
    rows: List[Dict[str, Any]] = []

    for run_id, rec in ledger.items():
        pkg = pkg_map.get(run_id)
        video_exists = (settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4").exists()
        yt_id = str(rec.get("youtube_video_id", ""))
        is_mock = yt_id.startswith("mock_yt_")
        privacy = str(rec.get("privacy_status", "private")).lower()
        synthetic = bool(
            rec.get("response_json", {}).get("containsSyntheticMedia")
            if isinstance(rec.get("response_json"), dict)
            else (pkg.metadata.synthetic_media_disclosure if pkg else True)
        )
        rows.append({
            "run_id": run_id,
            "title": pkg.metadata.title if pkg else f"Run {run_id}",
            "niche": (pkg.niche or "") if pkg else "",
            "youtube_video_id": yt_id,
            "is_mock": is_mock,
            "privacy_status": privacy,
            "privacy_label": privacy.capitalize(),
            "upload_status": "Simulated (Dry-run)" if is_mock else "Uploaded to YouTube",
            "uploaded_at_utc": str(rec.get("uploaded_at_utc", "")),
            "uploaded_at_display": _format_local_dt(rec.get("uploaded_at_utc"), tz_name),
            "video_sha256": str(rec.get("video_sha256", "")),
            "has_preview": video_exists,
            "synthetic_media": synthetic,
            "studio_url": "https://studio.youtube.com" if is_mock else f"https://studio.youtube.com/video/{yt_id}/edit",
        })

    rows.sort(key=lambda r: r["uploaded_at_utc"], reverse=True)
    return rows


def build_activity_timeline(
    packages: List[ProductionPackage],
    ledger_rows: List[Dict[str, Any]],
    tz_name: str,
) -> List[Dict[str, str]]:
    """Build a human-readable chronological timeline from real runs and uploads."""
    events: List[Dict[str, str]] = []
    ledger_by_run = {r["run_id"]: r for r in ledger_rows}

    for p in packages:
        events.append({
            "timestamp_iso": p.created_at_utc,
            "timestamp_display": _format_local_dt(p.created_at_utc, tz_name),
            "run_id": p.run_id,
            "title": p.metadata.title,
            "event": f"Created {p.slot} video brief ({p.niche or 'Default niche'})",
            "tone": "info",
            "detail": f"Topic: {p.topic}",
        })
        if p.status == "awaiting_approval":
            events.append({
                "timestamp_iso": p.created_at_utc,
                "timestamp_display": _format_local_dt(p.created_at_utc, tz_name),
                "run_id": p.run_id,
                "title": p.metadata.title,
                "event": "Video assembled and waiting for your approval",
                "tone": "purple",
                "detail": f"Duration: {p.total_duration_seconds}s across {len(p.scenes)} scenes",
            })
        elif p.status == "rejected":
            events.append({
                "timestamp_iso": p.created_at_utc,
                "timestamp_display": _format_local_dt(p.created_at_utc, tz_name),
                "run_id": p.run_id,
                "title": p.metadata.title,
                "event": "Video rejected before upload",
                "tone": "danger",
                "detail": p.error_message or "Rejected by reviewer.",
            })
        elif p.status == "failed":
            events.append({
                "timestamp_iso": p.created_at_utc,
                "timestamp_display": _format_local_dt(p.created_at_utc, tz_name),
                "run_id": p.run_id,
                "title": p.metadata.title,
                "event": "Something went wrong",
                "tone": "danger",
                "detail": explain_error(p.error_message),
            })

        if p.run_id in ledger_by_run:
            rec = ledger_by_run[p.run_id]
            events.append({
                "timestamp_iso": rec["uploaded_at_utc"] or p.created_at_utc,
                "timestamp_display": rec["uploaded_at_display"],
                "run_id": p.run_id,
                "title": p.metadata.title,
                "event": f"Uploaded to YouTube ({rec['privacy_label']})",
                "tone": "success",
                "detail": f"Video ID: {rec['youtube_video_id']}",
            })

    events.sort(key=lambda e: e["timestamp_iso"], reverse=True)
    return events[:40]


def read_recent_logs(limit: int = 120) -> List[str]:
    """Return the last `limit` lines from logs/automation.log safely."""
    log_path: Path = settings.LOGS_DIR / "automation.log"
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except Exception:
        return []
