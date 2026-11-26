import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _get_zoneinfo(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def find_due_slots(
    data_dir: Path,
    now_utc: Optional[datetime] = None,
    window_minutes: int = 6,
) -> List[Tuple[str, str]]:
    """
    Return [(slot, fire_iso), ...] for configured morning/evening crons that fired
    within the last `window_minutes` and have not yet been recorded in scheduler_state.json.
    """
    cfg = _load_json(data_dir / "studio_settings.json", {})
    if not isinstance(cfg, dict):
        cfg = {}

    tz_name = str(cfg.get("timezone") or "Asia/Kolkata")
    morning_cron = str(cfg.get("morning_cron") or "0 7 * * *")
    evening_cron = str(cfg.get("evening_cron") or "30 16 * * *")

    state = _load_json(data_dir / "scheduler_state.json", {})
    if not isinstance(state, dict):
        state = {}

    tz = _get_zoneinfo(tz_name)
    ref_utc = now_utc or datetime.now(timezone.utc)
    if ref_utc.tzinfo is None:
        ref_utc = ref_utc.replace(tzinfo=timezone.utc)
    now_local = ref_utc.astimezone(tz)
    window_start = now_local - timedelta(minutes=window_minutes)

    due: List[Tuple[str, str]] = []
    for slot, expr in (("morning", morning_cron), ("evening", evening_cron)):
        try:
            trigger = CronTrigger.from_crontab(expr, timezone=tz)
        except Exception:
            continue

        latest_fire = None
        cursor = window_start
        prev_fire = None
        for _ in range(10):
            nxt = trigger.get_next_fire_time(prev_fire, cursor)
            if nxt is None or nxt > now_local:
                break
            latest_fire = nxt
            prev_fire = nxt
            cursor = nxt + timedelta(seconds=1)

        if latest_fire is not None:
            fire_iso = latest_fire.isoformat()
            if state.get(slot) != fire_iso:
                due.append((slot, fire_iso))

    return due


def mark_slot_dispatched(data_dir: Path, slot: str, fire_iso: str) -> None:
    """Persist the last dispatched occurrence timestamp for a slot to prevent duplicates."""
    state_path = data_dir / "scheduler_state.json"
    state = _load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    state[slot] = fire_iso
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
