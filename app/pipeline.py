import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from app.graph import content_graph
from app.logging_config import logger
from app.models import ProductionPackage, SlotType
from app.storage import storage


def generate_run_id(slot: SlotType) -> Tuple[str, str, str]:
    """Generate unique run_id, UTC timestamp, and local IST timestamp string."""
    now_utc = datetime.now(timezone.utc)
    try:
        ist_zone = ZoneInfo(settings.TIMEZONE)
    except Exception:
        ist_zone = timezone.utc

    now_local = now_utc.astimezone(ist_zone)
    date_str = now_local.strftime("%Y%m%d")
    short_suffix = secrets.token_hex(3)
    run_id = f"{date_str}_{slot}_{short_suffix}"

    return run_id, now_utc.isoformat(), now_local.strftime("%Y-%m-%d %H:%M:%S %Z")


def run_content_pipeline(slot: SlotType, force_mock: bool = False) -> ProductionPackage:
    """Execute the LangGraph content generation pipeline for a specific slot."""
    settings.ensure_directories()
    run_id, created_at_utc, scheduled_at_local = generate_run_id(slot)
    logger.info("Starting content generation pipeline: Run ID=%s, Slot=%s", run_id, slot)

    # 1. Fetch recent topics from history to prevent duplication
    history = storage.get_topic_history()
    recent_topics = [h["topic"] for h in history if "topic" in h]

    initial_state = {
        "run_id": run_id,
        "slot": slot,
        "created_at_utc": created_at_utc,
        "scheduled_at_local": scheduled_at_local,
        "recent_topics": recent_topics,
        "retry_count": 0,
        "validation_errors": [],
        "force_mock": force_mock,
    }

    # 2. Invoke LangGraph
    final_state = content_graph.invoke(initial_state)

    package: Optional[ProductionPackage] = final_state.get("package")
    if not package:
        errors = final_state.get("validation_errors", ["Pipeline failed to produce a valid package."])
        error_msg = "; ".join(errors)
        logger.error("Content pipeline failed for %s: %s", run_id, error_msg)
        raise RuntimeError(f"Package generation failed for {run_id}: {error_msg}")

    # 3. Save package JSON
    storage.save_package(package)

    # 4. Create Flow export directory & FLOW_PROMPTS.txt
    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    run_export_dir.mkdir(parents=True, exist_ok=True)
    flow_file = run_export_dir / "FLOW_PROMPTS.txt"
    flow_content = final_state.get("flow_prompts_file_content", "")
    with open(flow_file, "w", encoding="utf-8") as f:
        f.write(flow_content)
    logger.info("Created Flow prompts brief: %s", flow_file)

    # 5. Record topic in history
    storage.record_topic(package.topic, run_id)

    # 6. Advance status to awaiting_flow_export
    storage.update_package_status(run_id, "awaiting_flow_export")
    package.status = "awaiting_flow_export"

    logger.info("Pipeline completed successfully for %s. Status is awaiting_flow_export.", run_id)
    return package
