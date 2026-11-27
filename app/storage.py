import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.logging_config import logger
from app.models import ApprovalRecord, FlowJobState, ProductionPackage, RunStatus, StudioConfig, UploadRecord

_LOCK = threading.RLock()


class StorageManager:
    """Thread-safe persistent storage manager for packages, history, approvals, and upload ledger."""

    def __init__(self):
        settings.ensure_directories()
        self._init_file_if_missing(settings.TOPIC_HISTORY_FILE, [])
        self._init_file_if_missing(settings.UPLOAD_LEDGER_FILE, {})
        self._init_file_if_missing(settings.APPROVALS_FILE, {})

    @staticmethod
    def _init_file_if_missing(path: Path, default_data: Any) -> None:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default_data, f, indent=2)

    # ---------------- Packages ----------------

    def save_package(self, package: ProductionPackage) -> Path:
        """Save production package to data/packages/<run_id>.json."""
        with _LOCK:
            package_path = settings.PACKAGES_DIR / f"{package.run_id}.json"
            with open(package_path, "w", encoding="utf-8") as f:
                f.write(package.model_dump_json(indent=2))
            logger.info("Saved production package: %s", package.run_id)
            return package_path

    def load_package(self, run_id: str) -> Optional[ProductionPackage]:
        """Load production package by run_id."""
        package_path = settings.PACKAGES_DIR / f"{run_id}.json"
        if not package_path.exists():
            return None
        with open(package_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return ProductionPackage.model_validate(data)

    def update_package_status(self, run_id: str, status: RunStatus, error_message: Optional[str] = None) -> None:
        """Update the status and optional error message of an existing package."""
        with _LOCK:
            pkg = self.load_package(run_id)
            if not pkg:
                raise FileNotFoundError(f"Package {run_id} not found.")
            pkg.status = status
            if error_message is not None:
                pkg.error_message = error_message
            self.save_package(pkg)
            logger.info("Updated package %s status to %s", run_id, status)

    def list_packages(self) -> List[ProductionPackage]:
        """List all packages in data/packages/."""
        packages = []
        for path in sorted(settings.PACKAGES_DIR.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    packages.append(ProductionPackage.model_validate(data))
            except Exception as e:
                logger.warning("Failed to parse package %s: %s", path, e)
        return packages

    # ---------------- Topic History (Anti-duplication) ----------------

    def get_topic_history(self) -> List[Dict[str, Any]]:
        """Return history of previously covered topics."""
        with _LOCK:
            try:
                with open(settings.TOPIC_HISTORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []

    def record_topic(self, topic: str, run_id: str) -> None:
        """Record topic into history to prevent duplicate generation."""
        with _LOCK:
            history = self.get_topic_history()
            history.append({
                "topic": topic.strip(),
                "run_id": run_id,
                "recorded_at_utc": datetime.now(timezone.utc).isoformat()
            })
            with open(settings.TOPIC_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
            logger.info("Recorded topic to history: '%s' for run %s", topic, run_id)

    # ---------------- Approvals ----------------

    def save_approval_record(self, record: ApprovalRecord) -> None:
        """Persist or update an approval record."""
        with _LOCK:
            try:
                with open(settings.APPROVALS_FILE, "r", encoding="utf-8") as f:
                    approvals = json.load(f)
            except Exception:
                approvals = {}

            approvals[record.approval_id] = record.model_dump()
            with open(settings.APPROVALS_FILE, "w", encoding="utf-8") as f:
                json.dump(approvals, f, indent=2)
            logger.info("Saved approval record %s for run %s", record.approval_id, record.run_id)

    def get_approval_by_token_hash(self, token_hash: str) -> Optional[ApprovalRecord]:
        """Lookup an approval record by the SHA-256 hash of its token."""
        with _LOCK:
            try:
                with open(settings.APPROVALS_FILE, "r", encoding="utf-8") as f:
                    approvals = json.load(f)
            except Exception:
                return None

            for item in approvals.values():
                if item.get("token_hash") == token_hash:
                    return ApprovalRecord.model_validate(item)
            return None

    def get_approval_by_run_id(self, run_id: str) -> Optional[ApprovalRecord]:
        """Lookup an approval record by run_id."""
        with _LOCK:
            try:
                with open(settings.APPROVALS_FILE, "r", encoding="utf-8") as f:
                    approvals = json.load(f)
            except Exception:
                return None

            for item in approvals.values():
                if item.get("run_id") == run_id:
                    return ApprovalRecord.model_validate(item)
            return None

    # ---------------- Upload Ledger (Idempotency) ----------------

    def get_upload_record(self, run_id: str) -> Optional[UploadRecord]:
        """Lookup an upload record by run_id to prevent duplicate uploads."""
        with _LOCK:
            try:
                self._init_file_if_missing(settings.UPLOAD_LEDGER_FILE, {})
                with open(settings.UPLOAD_LEDGER_FILE, "r", encoding="utf-8") as f:
                    ledger = json.load(f)
                data = ledger.get(run_id)
                if data:
                    return UploadRecord.model_validate(data)
            except Exception as e:
                logger.error("Error reading upload ledger: %s", e)
            return None

    def record_upload(self, record: UploadRecord) -> None:
        """Record a successful upload into the idempotency ledger."""
        with _LOCK:
            try:
                self._init_file_if_missing(settings.UPLOAD_LEDGER_FILE, {})
                with open(settings.UPLOAD_LEDGER_FILE, "r", encoding="utf-8") as f:
                    ledger = json.load(f)
            except Exception:
                ledger = {}

            ledger[record.run_id] = record.model_dump()
            settings.UPLOAD_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(settings.UPLOAD_LEDGER_FILE, "w", encoding="utf-8") as f:
                json.dump(ledger, f, indent=2)
            logger.info("Recorded upload in ledger: run %s -> video %s", record.run_id, record.youtube_video_id)

    # ---------------- Studio Configuration (UI-editable) ----------------

    @staticmethod
    def default_studio_config() -> StudioConfig:
        """Build default StudioConfig from environment/Settings values."""
        return StudioConfig(
            studio_name=settings.STUDIO_NAME,
            language=settings.DEFAULT_LANGUAGE,
            niche=settings.DEFAULT_NICHE,
            niche_domains=settings.DEFAULT_NICHE_DOMAINS,
            audience_tone=settings.DEFAULT_AUDIENCE_TONE,
            default_cta=settings.DEFAULT_CTA,
            category_id=settings.DEFAULT_YOUTUBE_CATEGORY_ID,
            default_privacy_status=settings.DEFAULT_PRIVACY_STATUS,
            synthetic_media_default=settings.DEFAULT_SYNTHETIC_MEDIA,
            groq_model=settings.GROQ_MODEL,
            llm_temperature=settings.GROQ_TEMPERATURE,
            prompt_style_examples="",
            tts_voice=settings.TTS_VOICE,
            use_ai_voiceover=settings.DEFAULT_USE_AI_VOICEOVER,
            audio_mode="ai_voiceover" if settings.DEFAULT_USE_AI_VOICEOVER else "clip_audio_only",
            morning_cron=settings.MORNING_CRON,
            evening_cron=settings.EVENING_CRON,
            timezone=settings.TIMEZONE,
            min_duration_seconds=settings.MIN_VIDEO_DURATION,
            max_duration_seconds=settings.MAX_VIDEO_DURATION,
            google_pro_email=settings.GOOGLE_PRO_EMAIL,
            flow_auto_automate=False,
        )

    def get_studio_config(self) -> StudioConfig:
        """Load persisted UI studio settings or initialize from Settings defaults."""
        with _LOCK:
            path = settings.STUDIO_SETTINGS_FILE
            if not path.exists():
                cfg = self.default_studio_config()
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(cfg.model_dump_json(indent=2))
                return cfg
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return StudioConfig.model_validate(data)
            except Exception as e:
                logger.warning("Failed to parse studio settings (%s); falling back to defaults.", e)
                return self.default_studio_config()

    def save_studio_config(self, config: StudioConfig) -> StudioConfig:
        """Persist updated UI studio settings."""
        with _LOCK:
            path = settings.STUDIO_SETTINGS_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(config.model_dump_json(indent=2))
            logger.info("Saved studio configuration (niche='%s', privacy='%s')", config.niche, config.default_privacy_status)
            return config

    def delete_package(self, run_id: str) -> bool:
        """Delete a production package and its local export/final folders."""
        with _LOCK:
            package_path = settings.PACKAGES_DIR / f"{run_id}.json"
            existed = package_path.exists()
            if existed:
                package_path.unlink()
            export_dir = settings.FLOW_EXPORTS_DIR / run_id
            if export_dir.exists():
                shutil.rmtree(export_dir, ignore_errors=True)
            final_dir = settings.FINAL_OUTPUT_DIR / run_id
            if final_dir.exists():
                shutil.rmtree(final_dir, ignore_errors=True)
            job_file = settings.FLOW_JOBS_DIR / f"{run_id}.json"
            if job_file.exists():
                job_file.unlink(missing_ok=True)
            return existed

    # ---------------- Google Flow Playwright Job Tracking ----------------

    def save_flow_job(self, job: FlowJobState) -> Path:
        """Persist Google Flow Playwright automation job state safely."""
        with _LOCK:
            settings.FLOW_JOBS_DIR.mkdir(parents=True, exist_ok=True)
            job_path = settings.FLOW_JOBS_DIR / f"{job.run_id}.json"
            with open(job_path, "w", encoding="utf-8") as f:
                f.write(job.model_dump_json(indent=2))
            return job_path

    def load_flow_job(self, run_id: str) -> Optional[FlowJobState]:
        """Load persisted Flow automation job state for a run_id."""
        job_path = settings.FLOW_JOBS_DIR / f"{run_id}.json"
        if not job_path.exists():
            return None
        try:
            with open(job_path, "r", encoding="utf-8") as f:
                return FlowJobState.model_validate(json.load(f))
        except Exception as exc:
            logger.warning("Failed to parse Flow job state for %s: %s", run_id, exc)
            return None


storage = StorageManager()
