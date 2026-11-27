import os
import re
import secrets
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple
from app.logging_config import logger
from app.models import ProductionPackage


def extract_scene_number(filename: str) -> int:
    """Extract integer scene number from filename (e.g. scene_01.mp4 -> 1)."""
    match = re.search(r"scene_?0*(\d+)", filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return -1


def _natural_sort_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def extract_and_normalize_flow_archive(
    zip_path: Path,
    run_export_dir: Path,
    expected_scene_count: Optional[int] = None,
) -> List[str]:
    """
    Safely extract .mp4 clips and optional voiceover.mp3 from a Google Flow ZIP archive,
    renaming clips deterministically to scene_01.mp4 .. scene_0N.mp4.
    """
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Uploaded ZIP '{zip_path.name}' is corrupted or not a valid ZIP archive.")

    max_members = 50
    max_single_bytes = 250 * 1024 * 1024
    max_total_bytes = 600 * 1024 * 1024
    extracted_names: List[str] = []
    total_extracted = 0
    run_export_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        valid_infos = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            posix_path = PurePosixPath(info.filename.replace("\\", "/"))
            if "__MACOSX" in posix_path.parts:
                continue
            base = posix_path.name
            if not base or base.startswith("."):
                continue
            lower_base = base.lower()
            if not (lower_base.endswith(".mp4") or lower_base == "voiceover.mp3"):
                continue
            if info.file_size < 1024:
                continue
            if info.file_size > max_single_bytes:
                raise ValueError(f"ZIP entry '{base}' exceeds 250 MB safety limit.")
            valid_infos.append((info, base, lower_base))

        if not valid_infos:
            raise ValueError("ZIP archive contains no valid .mp4 clips (>= 1 KB) or voiceover.mp3.")
        if len(valid_infos) > max_members:
            raise ValueError(f"ZIP archive contains too many media files ({len(valid_infos)} > {max_members}).")

        vo_infos = [item for item in valid_infos if item[2] == "voiceover.mp3"]
        mp4_infos = sorted(
            [item for item in valid_infos if item[2].endswith(".mp4")],
            key=lambda item: _natural_sort_key(item[1]),
        )
        explicit_nums = {extract_scene_number(base) for _, base, _ in mp4_infos if extract_scene_number(base) > 0}
        next_auto_idx = 1

        for info, base, lower_base in [*vo_infos, *mp4_infos]:
            total_extracted += info.file_size
            if total_extracted > max_total_bytes:
                raise ValueError("ZIP archive uncompressed size exceeds 600 MB limit.")

            if lower_base == "voiceover.mp3":
                dest_name = "voiceover.mp3"
            else:
                scene_num = extract_scene_number(base)
                if scene_num > 0 and (expected_scene_count is None or scene_num <= max(expected_scene_count, 20)):
                    dest_name = f"scene_{scene_num:02d}.mp4"
                else:
                    while next_auto_idx in explicit_nums:
                        next_auto_idx += 1
                    dest_name = f"scene_{next_auto_idx:02d}.mp4"
                    explicit_nums.add(next_auto_idx)
                    next_auto_idx += 1

            dest_path = run_export_dir / dest_name
            tmp_entry = run_export_dir / f".zip.{dest_name}.part.{secrets.token_hex(4)}"
            try:
                with zf.open(info, "r") as src, open(tmp_entry, "wb") as dst:
                    bytes_copied = 0
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        bytes_copied += len(chunk)
                        if bytes_copied > max_single_bytes:
                            raise ValueError(f"ZIP entry '{base}' exceeded maximum allowed size.")
                        dst.write(chunk)
                os.replace(tmp_entry, dest_path)
                if dest_name == "voiceover.mp3":
                    (run_export_dir / ".custom_voiceover").write_text("1", encoding="utf-8")
                    (run_export_dir / ".tts_meta.json").unlink(missing_ok=True)
                extracted_names.append(dest_name)
            finally:
                if tmp_entry.exists():
                    tmp_entry.unlink(missing_ok=True)

    return sorted(extracted_names)


def normalize_flow_clips_sequence(raw_files: List[Path], run_export_dir: Path) -> List[str]:
    """Normalize individually downloaded Flow .mp4 clips into scene_01.mp4 .. scene_0N.mp4."""
    run_export_dir.mkdir(parents=True, exist_ok=True)
    mp4_paths = sorted(
        [p for p in raw_files if p.exists() and p.suffix.lower() == ".mp4" and p.stat().st_size >= 1024],
        key=lambda p: _natural_sort_key(p.name),
    )
    explicit_nums = {extract_scene_number(p.name) for p in mp4_paths if extract_scene_number(p.name) > 0}
    next_idx = 1
    normalized: List[str] = []

    for src in mp4_paths:
        scene_num = extract_scene_number(src.name)
        if scene_num > 0:
            dest_name = f"scene_{scene_num:02d}.mp4"
        else:
            while next_idx in explicit_nums:
                next_idx += 1
            dest_name = f"scene_{next_idx:02d}.mp4"
            explicit_nums.add(next_idx)
            next_idx += 1

        dest_path = run_export_dir / dest_name
        tmp_path = run_export_dir / f".norm.{dest_name}.part.{secrets.token_hex(4)}"
        try:
            shutil.copy2(src, tmp_path)
            os.replace(tmp_path, dest_path)
            normalized.append(dest_name)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    return sorted(normalized)


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
