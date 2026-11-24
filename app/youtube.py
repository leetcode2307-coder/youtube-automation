import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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


def get_youtube_service(mock: bool = False):
    """Retrieve authenticated YouTube Data API v3 service or None if mock/credentials missing."""
    if mock:
        return None

    secrets_file = settings.YOUTUBE_CLIENT_SECRETS_FILE
    token_file = settings.YOUTUBE_TOKEN_FILE

    if not secrets_file.exists():
        logger.warning(
            "YouTube client secrets file not found at %s. Running in dry-run/mock mode.",
            secrets_file
        )
        return None

    creds: Optional[Credentials] = None
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), YOUTUBE_UPLOAD_SCOPE)
        except Exception as e:
            logger.warning("Failed to load existing token.json: %s", e)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired YouTube OAuth token...")
            creds.refresh(Request())
        else:
            logger.info("Initiating local YouTube OAuth browser authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), YOUTUBE_UPLOAD_SCOPE)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w", encoding="utf-8") as token_out:
            token_out.write(creds.to_json())
        logger.info("Saved refreshed YouTube token to %s", token_file)

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
