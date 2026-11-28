import os
from pathlib import Path
from typing import Dict, Literal, Optional
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


LANGUAGE_PROFILES: Dict[str, Dict[str, str]] = {
    "english": {
        "label": "English",
        "iso": "en",
        "default_voice": "en-US-ChristopherNeural",
        "script_instruction": (
            "Write spoken narration, hook, on-screen text, CTA, and YouTube SEO metadata "
            "in clear, punchy English."
        ),
    },
    "telugu": {
        "label": "Telugu (తెలుగు)",
        "iso": "te",
        "default_voice": "te-IN-MohanNeural",
        "script_instruction": (
            "Write spoken narration, hook, on-screen text, CTA, and YouTube title/description/hashtags "
            "in natural conversational Telugu script (తెలుగు) suitable for voiceover and Telugu Shorts viewers "
            "(you may include bilingual Telugu + English search keywords in tags)."
        ),
    },
    "hindi": {
        "label": "Hindi (हिन्दी)",
        "iso": "hi",
        "default_voice": "hi-IN-MadhurNeural",
        "script_instruction": (
            "Write spoken narration, hook, on-screen text, CTA, and YouTube title/description/hashtags "
            "in natural conversational Hindi (हिन्दी Devanagari script) suitable for voiceover and Hindi Shorts viewers "
            "(you may include bilingual Hindi + English search keywords in tags)."
        ),
    },
    "tamil": {
        "label": "Tamil (தமிழ்)",
        "iso": "ta",
        "default_voice": "ta-IN-ValluvarNeural",
        "script_instruction": (
            "Write spoken narration, hook, on-screen text, CTA, and YouTube title/description/hashtags "
            "in natural conversational Tamil script (தமிழ்) suitable for voiceover and Tamil Shorts viewers "
            "(you may include bilingual Tamil + English search keywords in tags)."
        ),
    },
    "malayalam": {
        "label": "Malayalam (മലയാളം)",
        "iso": "ml",
        "default_voice": "ml-IN-MidhunNeural",
        "script_instruction": (
            "Write spoken narration, hook, on-screen text, CTA, and YouTube title/description/hashtags "
            "in natural conversational Malayalam script (മലയാളം) suitable for voiceover and Malayalam Shorts viewers "
            "(you may include bilingual Malayalam + English search keywords in tags)."
        ),
    },
}


def normalize_language(language: Optional[str]) -> str:
    cleaned = (language or "english").strip().lower()
    return cleaned if cleaned in LANGUAGE_PROFILES else "english"


def resolve_tts_voice(language: Optional[str], configured_voice: Optional[str] = None) -> str:
    """Return a matching Edge-TTS voice for the package language when configured_voice is for a different locale."""
    lang_key = normalize_language(language)
    profile = LANGUAGE_PROFILES[lang_key]
    voice = (configured_voice or "").strip()
    if voice and voice.lower().startswith(f"{profile['iso']}-"):
        return voice
    return profile["default_voice"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # LLM Settings (Groq)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GROQ_MAX_TOKENS: int = 900
    GROQ_TEMPERATURE: float = 0.9

    # Default Studio & Niche Settings (Overridable via UI in data/studio_settings.json)
    STUDIO_NAME: str = "AI Engineering Shorts Studio"
    DEFAULT_LANGUAGE: Literal["english", "telugu", "hindi", "tamil", "malayalam"] = "english"
    DEFAULT_NICHE: str = "AI Engineering"
    DEFAULT_NICHE_DOMAINS: str = "LLMs, RAG, AI Agents, Model Context Protocol, Inference Optimization, Vector Databases"
    DEFAULT_AUDIENCE_TONE: str = "Clear, engaging, high-retention storytelling"
    DEFAULT_CTA: str = "Subscribe for more daily insights!"
    DEFAULT_YOUTUBE_CATEGORY_ID: str = "28"
    DEFAULT_SYNTHETIC_MEDIA: bool = True
    MIN_VIDEO_DURATION: int = 30
    MAX_VIDEO_DURATION: int = 45

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
    STUDIO_SETTINGS_FILE: Path = Path("data/studio_settings.json")
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
    YOUTUBE_MOCK_MODE: bool = False
    DEFAULT_PRIVACY_STATUS: Literal["private", "unlisted", "public"] = "private"

    # TTS & FFmpeg Settings
    TTS_VOICE: str = "en-US-ChristopherNeural"
    DEFAULT_USE_AI_VOICEOVER: bool = True
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
            if "STUDIO_SETTINGS_FILE" not in os.environ and self.STUDIO_SETTINGS_FILE == Path("data/studio_settings.json"):
                self.STUDIO_SETTINGS_FILE = self.DATA_DIR / "studio_settings.json"

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
