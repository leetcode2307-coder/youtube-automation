import os
from pathlib import Path
from typing import Literal
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # LLM Settings (Groq)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # Timezone & Scheduling
    TIMEZONE: str = "Asia/Kolkata"
    MORNING_CRON: str = "0 7 * * *"
    EVENING_CRON: str = "30 16 * * *"
    ENABLE_EMBEDDED_SCHEDULER: bool = False

    # Directory Structure
    DATA_DIR: Path = Path("data")
    PACKAGES_DIR: Path = Path("data/packages")
    FLOW_EXPORTS_DIR: Path = Path("data/flow_exports")
    FINAL_OUTPUT_DIR: Path = Path("data/final")
    TOPIC_HISTORY_FILE: Path = Path("data/topic_history.json")
    UPLOAD_LEDGER_FILE: Path = Path("data/upload_ledger.json")
    APPROVALS_FILE: Path = Path("data/approvals.json")
    LOGS_DIR: Path = Path("logs")

    # Approval & Web Server
    APPROVAL_SECRET_KEY: str = "super_secret_approval_key_default_32_bytes"
    APPROVAL_BASE_URL: str = "http://127.0.0.1:8000"
    APPROVAL_TOKEN_EXPIRE_HOURS: int = 24
    SERVER_HOST: str = "127.0.0.1"
    SERVER_PORT: int = 8000

    # SMTP Configuration
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    APPROVAL_RECIPIENT_EMAIL: str = ""
    SENDER_EMAIL: str = ""

    # YouTube Publishing
    YOUTUBE_CLIENT_SECRETS_FILE: Path = Path("client_secret.json")
    YOUTUBE_TOKEN_FILE: Path = Path("token.json")
    YOUTUBE_CLIENT_SECRET_JSON: str = ""
    YOUTUBE_TOKEN_JSON: str = ""
    DEFAULT_PRIVACY_STATUS: Literal["private", "unlisted", "public"] = "private"

    # TTS & FFmpeg Settings
    TTS_VOICE: str = "en-US-ChristopherNeural"
    MAX_RETRIES: int = 3
    FFMPEG_THREADS: int = 2

    @model_validator(mode="after")
    def _apply_cloud_and_dir_defaults(self) -> "Settings":
        # If DATA_DIR was customized (e.g. Render Persistent Disk /var/data),
        # keep sub-paths aligned unless explicitly overridden via env.
        if self.DATA_DIR != Path("data"):
            if "PACKAGES_DIR" not in os.environ and self.PACKAGES_DIR == Path("data/packages"):
                self.PACKAGES_DIR = self.DATA_DIR / "packages"
            if "FLOW_EXPORTS_DIR" not in os.environ and self.FLOW_EXPORTS_DIR == Path("data/flow_exports"):
                self.FLOW_EXPORTS_DIR = self.DATA_DIR / "flow_exports"
            if "FINAL_OUTPUT_DIR" not in os.environ and self.FINAL_OUTPUT_DIR == Path("data/final"):
                self.FINAL_OUTPUT_DIR = self.DATA_DIR / "final"
            if "TOPIC_HISTORY_FILE" not in os.environ and self.TOPIC_HISTORY_FILE == Path("data/topic_history.json"):
                self.TOPIC_HISTORY_FILE = self.DATA_DIR / "topic_history.json"
            if "UPLOAD_LEDGER_FILE" not in os.environ and self.UPLOAD_LEDGER_FILE == Path("data/upload_ledger.json"):
                self.UPLOAD_LEDGER_FILE = self.DATA_DIR / "upload_ledger.json"
            if "APPROVALS_FILE" not in os.environ and self.APPROVALS_FILE == Path("data/approvals.json"):
                self.APPROVALS_FILE = self.DATA_DIR / "approvals.json"

        # Render / cloud platform PORT and RENDER_EXTERNAL_URL support
        if "PORT" in os.environ and "SERVER_PORT" not in os.environ:
            try:
                self.SERVER_PORT = int(os.environ["PORT"])
            except ValueError:
                pass

        if ("RENDER" in os.environ or "PORT" in os.environ) and "SERVER_HOST" not in os.environ:
            self.SERVER_HOST = "0.0.0.0"

        render_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        if render_url and self.APPROVAL_BASE_URL in ("http://127.0.0.1:8000", "http://localhost:8000", ""):
            self.APPROVAL_BASE_URL = render_url.rstrip("/")

        return self

    def ensure_directories(self) -> None:
        """Create required runtime directories if they do not exist."""
        for directory in [
            self.DATA_DIR,
            self.PACKAGES_DIR,
            self.FLOW_EXPORTS_DIR,
            self.FINAL_OUTPUT_DIR,
            self.LOGS_DIR,
        ]:
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
