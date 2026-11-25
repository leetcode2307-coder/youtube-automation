import hashlib
import html
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple

from app.config import settings
from app.logging_config import logger
from app.models import ApprovalRecord, ProductionPackage
from app.storage import storage


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        not lowered
        or "your_email@" in lowered
        or "your_app_password" in lowered
        or "example.com" in lowered
    )


def generate_approval_token(run_id: str) -> Tuple[str, str, str, str]:
    """
    Generate a cryptographically secure, single-use token.
    Returns: (raw_token, token_hash, created_at_utc, expires_at_utc)
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=settings.APPROVAL_TOKEN_EXPIRE_HOURS)

    return raw_token, token_hash, now.isoformat(), expires.isoformat()


def create_approval_record(run_id: str, approver_email: Optional[str] = None) -> Tuple[str, ApprovalRecord]:
    """Create and persist an approval record with a hashed token."""
    raw_token, token_hash, created_at, expires_at = generate_approval_token(run_id)
    approval_id = f"appr_{run_id}_{secrets.token_hex(2)}"

    record = ApprovalRecord(
        approval_id=approval_id,
        run_id=run_id,
        token_hash=token_hash,
        approver_email=approver_email or settings.APPROVAL_RECIPIENT_EMAIL or "operator@local",
        created_at_utc=created_at,
        expires_at_utc=expires_at,
    )
    storage.save_approval_record(record)
    return raw_token, record


def build_approval_email_html(package: ProductionPackage, review_url: str) -> str:
    """Render a clean, responsive HTML email for video review and approval."""
    esc = html.escape
    studio_cfg = storage.get_studio_config()
    scenes_html = "".join(
        f"<tr>"
        f"<td style='padding:8px;border:1px solid #ddd;'>Scene {s.scene_number} ({s.duration_seconds}s)</td>"
        f"<td style='padding:8px;border:1px solid #ddd;'><b>{esc(s.on_screen_text)}</b><br>{esc(s.narration)}</td>"
        f"</tr>"
        for s in package.scenes
    )

    tags_str = esc(", ".join(package.metadata.tags))
    hashtags_str = esc(" ".join(package.metadata.hashtags))
    safe_review_url = esc(review_url, quote=True)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background:#f4f6f8; margin:0; padding:20px; }}
        .card {{ max-width:640px; margin:0 auto; background:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.08); }}
        .header {{ background:#1a237e; color:#ffffff; padding:24px; text-align:center; }}
        .header h1 {{ margin:0 0 8px 0; font-size:22px; }}
        .content {{ padding:24px; color:#333333; }}
        .btn {{ display:inline-block; background:#2e7d32; color:#ffffff !important; text-decoration:none; padding:14px 28px; border-radius:6px; font-weight:bold; font-size:16px; margin:20px 0; text-align:center; }}
        .meta-box {{ background:#f8f9fa; border-left:4px solid #1a237e; padding:12px 16px; margin:16px 0; font-size:14px; }}
        table {{ width:100%; border-collapse:collapse; margin-top:12px; font-size:14px; }}
        .footer {{ padding:16px; text-align:center; font-size:12px; color:#888888; background:#fbfbfb; border-top:1px solid #eeeeee; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="header">
          <h1>Approval Required: YouTube Short</h1>
          <p style="margin:0; opacity:0.85;">Run ID: {esc(package.run_id)} | Slot: {esc(package.slot.upper())}</p>
        </div>
        <div class="content">
          <h2 style="margin-top:0;">{esc(package.metadata.title)}</h2>
          <div class="meta-box">
            <b>Topic:</b> {esc(package.topic)}<br>
            <b>Target Duration:</b> {package.total_duration_seconds}s | <b>Scenes:</b> {len(package.scenes)}<br>
            <b>Scheduled Local Time:</b> {esc(package.scheduled_at_local)}
          </div>

          <p><b>Hook:</b> <i>"{esc(package.hook)}"</i></p>
          <p><b>Hashtags:</b> {hashtags_str}</p>
          <p><b>Tags:</b> {tags_str}</p>

          <h3>Scene Breakdown:</h3>
          <table>
            <thead>
              <tr style="background:#f1f3f5;">
                <th style="padding:8px;border:1px solid #ddd;text-align:left;">Scene</th>
                <th style="padding:8px;border:1px solid #ddd;text-align:left;">Narration & Overlay</th>
              </tr>
            </thead>
            <tbody>
              {scenes_html}
            </tbody>
          </table>

          <div style="text-align:center;">
            <a href="{safe_review_url}" class="btn">Review & Authorize Video</a>
            <p style="font-size:12px; color:#666;">
              Security Notice: Clicking this link opens the secure review portal.<br>
              Approval requires an explicit interactive confirmation on the portal.
            </p>
          </div>
        </div>
        <div class="footer">
          {esc(studio_cfg.studio_name)} &bull; Approval Gate
        </div>
      </div>
    </body>
    </html>
    """


def send_approval_email(package: ProductionPackage, raw_token: str) -> bool:
    """Send review notification email with secure portal link via SMTP."""
    review_url = f"{settings.APPROVAL_BASE_URL.rstrip('/')}/review/{package.run_id}?token={raw_token}"
    recipient = settings.APPROVAL_RECIPIENT_EMAIL or "operator@local"

    # Check if SMTP credentials are fully provided (not empty or placeholder)
    if (
        not settings.SMTP_HOST
        or _is_placeholder(settings.SMTP_USERNAME)
        or _is_placeholder(settings.SMTP_PASSWORD)
        or _is_placeholder(recipient)
    ):
        logger.warning(
            "SMTP credentials not configured. Dry-run mode: Review URL is: %s",
            review_url
        )
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Approval Required] {package.metadata.title} ({package.run_id})"
    msg["From"] = settings.SENDER_EMAIL or settings.SMTP_USERNAME
    msg["To"] = recipient

    plain_text = (
        f"YouTube Short Ready for Review\n"
        f"Run ID: {package.run_id} ({package.slot.upper()})\n"
        f"Title: {package.metadata.title}\n"
        f"Topic: {package.topic}\n"
        f"Duration: {package.total_duration_seconds}s\n\n"
        f"To review and approve/reject, visit:\n{review_url}\n\n"
        f"Token expires in {settings.APPROVAL_TOKEN_EXPIRE_HOURS} hours.\n"
    )

    html_content = build_approval_email_html(package, review_url)

    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        logger.info("Connecting to SMTP server %s:%d...", settings.SMTP_HOST, settings.SMTP_PORT)
        if settings.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
            if settings.SMTP_USE_TLS:
                server.starttls()
        server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info("Approval email delivered to %s for run %s", recipient, package.run_id)
        return True
    except Exception as e:
        logger.error("Failed to send approval email via SMTP: %s", e)
        return False
