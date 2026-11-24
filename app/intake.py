import re
from pathlib import Path
from typing import List, Tuple
from app.logging_config import logger
from app.models import ProductionPackage


def extract_scene_number(filename: str) -> int:
    """Extract integer scene number from filename (e.g. scene_01.mp4 -> 1)."""
    match = re.search(r"scene_?0*(\d+)", filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return -1


def validate_flow_assets(
    run_export_dir: Path,
    package: ProductionPackage
) -> Tuple[bool, List[str], List[Path]]:
    """
    Validate that all required scene clips exist in the run folder,
    are non-empty, and return them sorted in strict numerical order.
    """
    errors: List[str] = []
    if not run_export_dir.exists():
        errors.append(f"Export directory does not exist: {run_export_dir}")
        return False, errors, []

    mp4_files = list(run_export_dir.glob("*.mp4"))
    # Filter to scene clips matching scene_XX pattern
    scene_clips = [f for f in mp4_files if extract_scene_number(f.name) > 0]

    expected_count = len(package.scenes)
    if len(scene_clips) < expected_count:
        errors.append(
            f"Missing scene clips. Expected {expected_count} clips (scene_01..scene_0{expected_count}), found {len(scene_clips)}."
        )

    # Sort strictly by integer scene number (prevents lexical sort bugs: scene_10 before scene_2)
    sorted_clips = sorted(scene_clips, key=lambda f: extract_scene_number(f.name))

    # Verify each expected scene number exists and is non-empty
    found_numbers = {extract_scene_number(f.name) for f in sorted_clips}
    for scene in package.scenes:
        if scene.scene_number not in found_numbers:
            errors.append(f"Missing expected clip for scene {scene.scene_number} ({scene.export_filename}).")

    for clip in sorted_clips:
        if clip.stat().st_size < 1024:  # Less than 1 KB
            errors.append(f"Clip {clip.name} is corrupted or empty ({clip.stat().st_size} bytes).")

    is_valid = len(errors) == 0
    if is_valid:
        logger.info("Asset intake validated for %s: %d clips found in numerical sequence.", package.run_id, len(sorted_clips))
    else:
        logger.warning("Asset intake failed for %s: %s", package.run_id, errors)

    return is_valid, errors, sorted_clips
