import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import normalize_language, settings
from app.graph import content_graph
from app.logging_config import logger
from app.models import ProductionPackage, SlotType
from app.storage import storage


def generate_run_id(slot: SlotType, tz_name: Optional[str] = None) -> Tuple[str, str, str]:
    """Generate unique run_id, UTC timestamp, and local timestamp string."""
    now_utc = datetime.now(timezone.utc)
    selected_tz = tz_name or settings.TIMEZONE
    try:
        local_zone = ZoneInfo(selected_tz)
    except Exception:
        local_zone = timezone.utc

    now_local = now_utc.astimezone(local_zone)
    date_str = now_local.strftime("%Y%m%d")
    short_suffix = secrets.token_hex(3)
    run_id = f"{date_str}_{slot}_{short_suffix}"

    return run_id, now_utc.isoformat(), now_local.strftime("%Y-%m-%d %H:%M:%S %Z")


def run_content_pipeline(
    slot: SlotType,
    force_mock: bool = False,
    custom_topic: Optional[str] = None,
    niche_override: Optional[str] = None,
    language_override: Optional[str] = None,
    style_examples_override: Optional[str] = None,
    remember_style: bool = False,
    temperature_override: Optional[float] = None,
) -> ProductionPackage:
    """Execute the LangGraph content generation pipeline for a specific slot."""
    settings.ensure_directories()
    studio_cfg = storage.get_studio_config()
    if style_examples_override is not None:
        active_style_examples = style_examples_override.strip()[:3000]
        if remember_style and active_style_examples != (studio_cfg.prompt_style_examples or "").strip():
            studio_cfg = storage.save_studio_config(
                studio_cfg.model_copy(update={"prompt_style_examples": active_style_examples})
            )
    else:
        active_style_examples = (studio_cfg.prompt_style_examples or "").strip()

    raw_temp = temperature_override if temperature_override is not None else studio_cfg.llm_temperature
    active_temperature = max(0.0, min(1.5, float(raw_temp)))

    run_id, created_at_utc, scheduled_at_local = generate_run_id(slot, studio_cfg.timezone)
    active_niche = (niche_override or "").strip() or studio_cfg.niche
    active_language = normalize_language(language_override or studio_cfg.language)
    logger.info(
        "Starting content generation pipeline: Run ID=%s, Slot=%s, Niche=%s, Language=%s, Temp=%.2f, StyleMemory=%s",
        run_id, slot, active_niche, active_language, active_temperature, bool(active_style_examples),
    )

    # 1. Fetch recent topics from history to prevent duplication
    history = storage.get_topic_history()
    recent_topics = [h["topic"] for h in history if "topic" in h]

    initial_state = {
        "run_id": run_id,
        "slot": slot,
        "created_at_utc": created_at_utc,
        "scheduled_at_local": scheduled_at_local,
        "recent_topics": recent_topics,
        "niche": active_niche,
        "language": active_language,
        "niche_domains": studio_cfg.niche_domains,
        "audience_tone": studio_cfg.audience_tone,
        "default_cta": studio_cfg.default_cta,
        "category_id": studio_cfg.category_id,
        "groq_model": studio_cfg.groq_model,
        "llm_temperature": active_temperature,
        "style_examples": active_style_examples,
        "min_duration_seconds": studio_cfg.min_duration_seconds,
        "max_duration_seconds": studio_cfg.max_duration_seconds,
        "min_scenes": studio_cfg.min_scenes,
        "max_scenes": studio_cfg.max_scenes,
        "synthetic_media_default": studio_cfg.synthetic_media_default,
        "privacy_status": studio_cfg.default_privacy_status,
        "custom_topic": (custom_topic or "").strip(),
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
