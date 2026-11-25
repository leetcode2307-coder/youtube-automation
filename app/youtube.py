import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.assemble import calculate_sha256
from app.config import settings
from app.logging_config import logger
from app.models import UploadRecord
from app.storage import storage

YOUTUBE_UPLOAD_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]


def _parse_json_secret(raw_value: str) -> Optional[Dict[str, Any]]:
    """Parse raw JSON or base64-encoded JSON from an environment variable."""
    raw = raw_value.strip()
    if not raw:
        return None
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)
    except Exception as e:
        logger.warning("Failed to parse YouTube JSON credential from environment: %s", e)
        return None


def _load_client_secrets_config() -> Optional[Dict[str, Any]]:
    """Load YouTube OAuth client secrets from env var, local path, or Render /etc/secrets."""
    env_cfg = _parse_json_secret(settings.YOUTUBE_CLIENT_SECRET_JSON)
    if env_cfg:
        return env_cfg

    candidates = [
        settings.YOUTUBE_CLIENT_SECRETS_FILE,
        Path("/etc/secrets") / settings.YOUTUBE_CLIENT_SECRETS_FILE.name,
    ]
    for path in candidates:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return None


def _load_existing_credentials() -> Optional[Credentials]:
    """Load OAuth user credentials from writable cache, configured file, Render secret, or env var."""
    cache_token_file = settings.DATA_DIR / "youtube_token.json"
    candidates = [
        cache_token_file,
        settings.YOUTUBE_TOKEN_FILE,
        Path("/etc/secrets") / settings.YOUTUBE_TOKEN_FILE.name,
    ]
    for path in candidates:
        if path.exists():
            try:
                return Credentials.from_authorized_user_file(str(path), YOUTUBE_UPLOAD_SCOPE)
            except Exception as e:
                logger.warning("Failed to load YouTube token from %s: %s", path, e)

    env_token = _parse_json_secret(settings.YOUTUBE_TOKEN_JSON)
    if env_token:
        try:
            return Credentials.from_authorized_user_info(env_token, YOUTUBE_UPLOAD_SCOPE)
        except Exception as e:
            logger.warning("Failed to parse YOUTUBE_TOKEN_JSON: %s", e)

    return None


def _save_credentials(creds: Credentials) -> Path:
    """Persist refreshed credentials to a writable path (falling back to DATA_DIR on read-only mounts)."""
    target = settings.YOUTUBE_TOKEN_FILE
    if str(target).startswith("/etc/secrets") or settings.YOUTUBE_TOKEN_JSON.strip():
        target = settings.DATA_DIR / "youtube_token.json"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as token_out:
            token_out.write(creds.to_json())
        return target
    except OSError:
        fallback = settings.DATA_DIR / "youtube_token.json"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        with open(fallback, "w", encoding="utf-8") as token_out:
            token_out.write(creds.to_json())
        return fallback


def authorize_youtube_interactive() -> Path:
    """Run local browser OAuth flow explicitly via CLI to generate token.json."""
    client_config = _load_client_secrets_config()
    if not client_config:
        raise FileNotFoundError(
            f"YouTube client secrets not found at {settings.YOUTUBE_CLIENT_SECRETS_FILE} "
            "or YOUTUBE_CLIENT_SECRET_JSON."
        )
    flow = InstalledAppFlow.from_client_config(client_config, YOUTUBE_UPLOAD_SCOPE)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    saved_path = _save_credentials(creds)
    logger.info("Saved YouTube OAuth token to %s", saved_path)
    return saved_path


def get_youtube_service(mock: bool = False, interactive: bool = False):
    """Retrieve authenticated YouTube Data API v3 service or None if mock/credentials missing."""
    if mock:
        return None

    client_config = _load_client_secrets_config()
    if not client_config:
        logger.warning(
            "YouTube client secrets not found at %s (or env/Render secrets). Running in dry-run/mock mode.",
            settings.YOUTUBE_CLIENT_SECRETS_FILE
        )
        return None

    creds = _load_existing_credentials()

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired YouTube OAuth token...")
            creds.refresh(Request())
            saved_path = _save_credentials(creds)
            logger.info("Saved refreshed YouTube token to %s", saved_path)
        elif interactive:
            logger.info("Initiating local YouTube OAuth browser authorization...")
            flow = InstalledAppFlow.from_client_config(client_config, YOUTUBE_UPLOAD_SCOPE)
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
            saved_path = _save_credentials(creds)
            logger.info("Saved YouTube token to %s", saved_path)
        else:
            raise RuntimeError(
                "YouTube OAuth token (token.json or YOUTUBE_TOKEN_JSON) is missing or lacks a refresh_token. "
                "Run 'python cli.py youtube-auth' on your local machine to generate token.json, then "
                "upload it to Render Secret Files (/etc/secrets/token.json) or set YOUTUBE_TOKEN_JSON."
            )

    return build("youtube", "v3", credentials=creds)


def upload_video_to_youtube(run_id: str, force_mock: bool = False) -> UploadRecord:
    """
    Upload an approved video to YouTube privately, guarded by an idempotency ledger.
    If the video has already been uploaded, returns the recorded video ID without duplicate uploads.
    """
    package = storage.load_package(run_id)
    if not package:
        raise FileNotFoundError(f"Package for run {run_id} not found.")

    final_mp4 = settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4"
    if not final_mp4.exists():
        raise FileNotFoundError(f"Assembled video file {final_mp4} not found.")

    video_sha256 = calculate_sha256(final_mp4)

    # 1. IDEMPOTENCY CHECK: Guard against duplicate uploads
    existing_upload = storage.get_upload_record(run_id)
    if existing_upload:
        logger.info(
            "IDEMPOTENCY GUARD: Run %s already uploaded as YouTube ID %s. Skipping duplicate upload.",
            run_id, existing_upload.youtube_video_id
        )
        return existing_upload

    # 2. Build metadata snippet
    title = package.metadata.title[:100]
    description = (
        f"{package.metadata.description}\n\n"
        f"{' '.join(package.metadata.hashtags)}\n\n"
        f"Altered or Synthetic Content: This video contains AI-generated visual animations."
    )
    tags = package.metadata.tags

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": package.metadata.category_id,
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": settings.DEFAULT_PRIVACY_STATUS,  # Strictly private initially
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        }
    }

    # 3. Handle Mock / Dry-Run Mode
    service = get_youtube_service(mock=force_mock)
    if service is None:
        mock_video_id = f"mock_yt_{run_id}"
        logger.info(
            "MOCK/DRY-RUN: Simulated YouTube private upload for run %s -> ID: %s",
            run_id, mock_video_id
        )
        record = UploadRecord(
            run_id=run_id,
            video_sha256=video_sha256,
            youtube_video_id=mock_video_id,
            privacy_status=settings.DEFAULT_PRIVACY_STATUS,
            uploaded_at_utc=datetime.now(timezone.utc).isoformat(),
            response_json={"status": "mock_upload", "title": title}
        )
        storage.record_upload(record)
        storage.update_package_status(run_id, "uploaded_private")
        return record

    # 4. Live YouTube Upload via Resumable Media
    logger.info("Initiating live YouTube upload for %s (Privacy: %s)...", run_id, settings.DEFAULT_PRIVACY_STATUS)
    media = MediaFileUpload(
        str(final_mp4),
        mimetype="video/mp4",
        chunksize=1024 * 1024,
        resumable=True
    )

    request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info("Uploaded %d%% of %s...", int(status.progress() * 100), final_mp4.name)

    video_id = response.get("id", "unknown_video_id")
    logger.info("Upload completed successfully! YouTube Video ID: %s", video_id)

    record = UploadRecord(
        run_id=run_id,
        video_sha256=video_sha256,
        youtube_video_id=video_id,
        privacy_status=settings.DEFAULT_PRIVACY_STATUS,
        uploaded_at_utc=datetime.now(timezone.utc).isoformat(),
        response_json=response
    )

    # Record in idempotency ledger & update package status
    storage.record_upload(record)
    storage.update_package_status(run_id, "uploaded_private")
    return record
