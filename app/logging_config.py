import logging
import re
import sys
from pathlib import Path

# Patterns to redact from logs for safety
SENSITIVE_PATTERNS = [
    re.compile(r"gsk_[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"(password|secret|token|api_key)=['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
]


class RedactingFormatter(logging.Formatter):
    """Formatter that automatically redacts API keys, secrets, and bearer tokens."""
    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        redacted = original
        for pattern in SENSITIVE_PATTERNS:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
        return redacted


def setup_logging(logs_dir: Path = Path("logs"), level: int = logging.INFO) -> logging.Logger:
    """Set up structured logging with both console and rotating file outputs."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "automation.log"

    formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
    )

    root_logger = logging.getLogger("ai_shorts")
    root_logger.setLevel(level)

    # Prevent duplicate handlers if called multiple times
    if not root_logger.handlers:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        root_logger.addHandler(console_handler)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root_logger.addHandler(file_handler)

    return root_logger


logger = setup_logging()
