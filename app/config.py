from pathlib import Path
from typing import Literal
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
    DEFAULT_PRIVACY_STATUS: Literal["private", "unlisted", "public"] = "private"

    # TTS Settings
    TTS_VOICE: str = "en-US-ChristopherNeural"
    MAX_RETRIES: int = 3

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
