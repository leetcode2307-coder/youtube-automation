import hashlib
import os
import re
import secrets
import shutil
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.logging_config import logger
from app.media_validator import get_media_info
from app.models import ProductionPackage, ZipImportItem, ZipImportRecord

MAX_ZIP_MEMBERS = 60
MAX_SINGLE_MEMBER_BYTES = 250 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 600 * 1024 * 1024


class ZipImportValidationError(ValueError):
    """Raised when a ZIP archive fails integrity, security, or mapping validation."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_scene_number(filename: str) -> int:
    """Extract integer scene number from filename (e.g. scene_01.mp4 -> 1)."""
    match = re.search(r"scene_?0*(\d+)", filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return -1


def _natural_sort_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def _validate_zip_member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    """Reject path traversal, drive-rooted paths, NUL bytes, and ZIP symlinks."""
    raw = info.filename
    if not raw or "\x00" in raw:
        raise ZipImportValidationError("Unsafe empty or null-byte path inside ZIP archive.")

    # Block POSIX symlinks stored via external_attr (mode 0120000)
    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
        raise ZipImportValidationError(f"Unsafe symlink entry '{raw}' rejected inside ZIP archive.")

    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ZipImportValidationError(f"Unsafe absolute path '{raw}' rejected inside ZIP archive.")

    posix_path = PurePosixPath(normalized)
    if ".." in posix_path.parts:
        raise ZipImportValidationError(f"Unsafe path traversal entry '{raw}' rejected inside ZIP archive.")
    return posix_path


def _sanitize_archive_filename(filename: str) -> str:
    base = Path(filename.replace("\\", "/")).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if not cleaned:
        cleaned = "flow_export.zip"
    if not cleaned.lower().endswith(".zip"):
        cleaned += ".zip"
    return cleaned


def probe_mp4_integrity(video_path: Path) -> Tuple[bool, float, int, int, str]:
    """Validate an MP4 clip non-destructively via ffprobe (checks container + video stream + duration)."""
    if not video_path.exists():
        return False, 0.0, 0, 0, "Extracted clip file is missing."
    if video_path.stat().st_size < 1024:
        return False, 0.0, 0, 0, f"Clip file is empty or too small ({video_path.stat().st_size} bytes)."
    try:
        info = get_media_info(video_path)
    except Exception as exc:
        return False, 0.0, 0, 0, f"Corrupted or unreadable MP4 video ({exc})."

    streams = info.get("streams", [])
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v_stream:
        return False, 0.0, 0, 0, "MP4 contains no valid video stream."

    width = int(v_stream.get("width", 0) or 0)
    height = int(v_stream.get("height", 0) or 0)
    fmt_dur = float(info.get("format", {}).get("duration", 0.0) or 0.0)
    stream_dur = float(v_stream.get("duration", 0.0) or 0.0)
    duration = max(fmt_dur, stream_dur)
    if width <= 0 or height <= 0 or duration <= 0.05:
        return False, duration, width, height, f"Invalid video geometry/duration ({width}x{height}, {duration:.2f}s)."
    return True, round(duration, 2), width, height, ""


def _validate_zip_container(zip_path: Path) -> List[Tuple[zipfile.ZipInfo, PurePosixPath, str, str]]:
    """
    Validate ZIP header, CRC integrity (zf.testzip()), path traversal safety,
    and size/member limits BEFORE extracting anything to disk.
    """
    if not zip_path.exists() or not zipfile.is_zipfile(zip_path):
        raise ZipImportValidationError(
            f"Uploaded ZIP '{zip_path.name}' is corrupted or not a valid ZIP archive."
        )

    candidates: List[Tuple[zipfile.ZipInfo, PurePosixPath, str, str]] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            corrupt_member = zf.testzip()
            if corrupt_member is not None:
                raise ZipImportValidationError(
                    f"ZIP archive failed integrity check at corrupted entry '{corrupt_member}'."
                )

            infos = zf.infolist()
            if len(infos) > MAX_ZIP_MEMBERS * 4:
                raise ZipImportValidationError(
                    f"ZIP archive contains too many entries ({len(infos)})."
                )

            total_uncompressed = 0
            for info in infos:
                posix_path = _validate_zip_member_path(info)
                if info.is_dir():
                    continue
                if "__MACOSX" in posix_path.parts:
                    continue
                base = posix_path.name
                if not base or base.startswith("."):
                    continue
                if info.file_size > MAX_SINGLE_MEMBER_BYTES:
                    raise ZipImportValidationError(
                        f"ZIP entry '{info.filename}' exceeds 250 MB safety limit."
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise ZipImportValidationError(
                        "ZIP archive uncompressed size exceeds 600 MB limit."
                    )
                lower_base = base.lower()
                candidates.append((info, posix_path, base, lower_base))
    except ZipImportValidationError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError) as exc:
        raise ZipImportValidationError(f"ZIP validation failed ({exc}).") from exc

    return candidates


def inspect_and_stage_zip_import(
    run_id: str,
    source_zip_path: Path,
    original_zip_filename: str,
    package: ProductionPackage,
    *,
    import_id: Optional[str] = None,
) -> ZipImportRecord:
    """
    Preserve original ZIP & raw clips cleanly inside FLOW_EXPORTS_DIR/<run_id>/imports/<import_id>/,
    validate integrity BEFORE extraction, probe every MP4 clip, sort deterministically,
    map to scene_01.mp4.., and detect missing or duplicate clips without auto-processing yet.
    """
    from app.storage import storage

    now_iso = _utc_now_iso()
    eff_import_id = import_id or f"zip_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    import_dir = run_export_dir / "imports" / eff_import_id
    original_dir = import_dir / "original"
    raw_dir = import_dir / "raw"
    original_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    safe_zip_name = _sanitize_archive_filename(original_zip_filename or source_zip_path.name)
    preserved_zip_path = original_dir / safe_zip_name
    if source_zip_path.resolve() != preserved_zip_path.resolve():
        tmp_zip = original_dir / f".{safe_zip_name}.part.{secrets.token_hex(4)}"
        shutil.copy2(source_zip_path, tmp_zip)
        os.replace(tmp_zip, preserved_zip_path)

    zip_sha = _sha256_file(preserved_zip_path)
    zip_size = preserved_zip_path.stat().st_size

    try:
        candidates = _validate_zip_container(preserved_zip_path)
    except ZipImportValidationError as exc:
        failed_rec = ZipImportRecord(
            import_id=eff_import_id,
            run_id=run_id,
            original_zip_filename=safe_zip_name,
            original_zip_rel_path=str(preserved_zip_path.relative_to(run_export_dir)).replace("\\", "/"),
            zip_sha256=zip_sha,
            zip_size_bytes=zip_size,
            status="failed",
            error_message=str(exc),
            created_at_utc=now_iso,
            updated_at_utc=_utc_now_iso(),
        )
        storage.save_zip_import(failed_rec)
        raise

    media_candidates = [
        c for c in candidates if c[3].endswith(".mp4") or c[3] == "voiceover.mp3"
    ]
    ignored_files = [
        str(c[1]) for c in candidates if not (c[3].endswith(".mp4") or c[3] == "voiceover.mp3")
    ]

    if not media_candidates:
        err = "ZIP archive contains no .mp4 video clips or voiceover.mp3."
        failed_rec = ZipImportRecord(
            import_id=eff_import_id,
            run_id=run_id,
            original_zip_filename=safe_zip_name,
            original_zip_rel_path=str(preserved_zip_path.relative_to(run_export_dir)).replace("\\", "/"),
            zip_sha256=zip_sha,
            zip_size_bytes=zip_size,
            status="failed",
            total_entries_inspected=len(candidates),
            ignored_files=ignored_files,
            error_message=err,
            created_at_utc=now_iso,
            updated_at_utc=_utc_now_iso(),
        )
        storage.save_zip_import(failed_rec)
        raise ZipImportValidationError(err)

    if len(media_candidates) > MAX_ZIP_MEMBERS:
        err = f"ZIP archive contains too many media files ({len(media_candidates)} > {MAX_ZIP_MEMBERS})."
        failed_rec = ZipImportRecord(
            import_id=eff_import_id,
            run_id=run_id,
            original_zip_filename=safe_zip_name,
            original_zip_rel_path=str(preserved_zip_path.relative_to(run_export_dir)).replace("\\", "/"),
            zip_sha256=zip_sha,
            zip_size_bytes=zip_size,
            status="failed",
            total_entries_inspected=len(candidates),
            error_message=err,
            created_at_utc=now_iso,
            updated_at_utc=_utc_now_iso(),
        )
        storage.save_zip_import(failed_rec)
        raise ZipImportValidationError(err)

    vo_candidates = sorted([c for c in media_candidates if c[3] == "voiceover.mp3"], key=lambda c: _natural_sort_key(str(c[1])))
    mp4_candidates = sorted(
        [c for c in media_candidates if c[3].endswith(".mp4")],
        key=lambda c: (
            0 if extract_scene_number(c[2]) > 0 else 1,
            extract_scene_number(c[2]) if extract_scene_number(c[2]) > 0 else 999999,
            _natural_sort_key(str(c[1])),
        ),
    )

    items: List[ZipImportItem] = []
    corrupted_files: List[str] = []

    with zipfile.ZipFile(preserved_zip_path, "r") as zf:
        for idx, (info, posix_path, base, lower_base) in enumerate([*mp4_candidates, *vo_candidates], start=1):
            member_id = f"m_{idx:02d}"
            raw_safe_name = f"{idx:02d}_{re.sub(r'[^A-Za-z0-9._-]+', '_', base)}"
            raw_dest = raw_dir / raw_safe_name
            tmp_raw = raw_dir / f".{raw_safe_name}.part.{secrets.token_hex(4)}"
            try:
                with zf.open(info, "r") as src, open(tmp_raw, "wb") as dst:
                    copied = 0
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > MAX_SINGLE_MEMBER_BYTES:
                            raise ZipImportValidationError(f"ZIP entry '{info.filename}' exceeded size limit.")
                        dst.write(chunk)
                os.replace(tmp_raw, raw_dest)
            finally:
                tmp_raw.unlink(missing_ok=True)

            file_sha = _sha256_file(raw_dest)
            raw_rel = str(raw_dest.relative_to(run_export_dir)).replace("\\", "/")

            if lower_base == "voiceover.mp3":
                valid_vo = raw_dest.stat().st_size >= 1024
                item = ZipImportItem(
                    member_id=member_id,
                    archive_path=str(posix_path),
                    original_filename=base,
                    raw_rel_path=raw_rel,
                    media_kind="voiceover_mp3",
                    size_bytes=raw_dest.stat().st_size,
                    sha256=file_sha,
                    status="valid" if valid_vo else "corrupted",
                    issue="" if valid_vo else "Audio file is empty or too small.",
                )
                if not valid_vo:
                    corrupted_files.append(base)
                items.append(item)
                continue

            is_ok, dur, w, h, issue = probe_mp4_integrity(raw_dest)
            detected_num = extract_scene_number(base)
            item = ZipImportItem(
                member_id=member_id,
                archive_path=str(posix_path),
                original_filename=base,
                raw_rel_path=raw_rel,
                media_kind="mp4",
                size_bytes=raw_dest.stat().st_size,
                sha256=file_sha,
                duration_seconds=dur,
                width=w,
                height=h,
                detected_scene_number=detected_num,
                status="valid" if is_ok else "corrupted",
                issue=issue,
            )
            if not is_ok:
                corrupted_files.append(base)
            items.append(item)

    # Deterministic mapping + duplicate/missing detection on valid MP4 clips
    expected_scenes = [s.scene_number for s in package.scenes]
    valid_mp4s = [it for it in items if it.media_kind == "mp4" and it.status == "valid"]
    assigned_scenes_set: set[int] = set()
    duplicate_scenes: set[int] = set()
    seen_hashes: Dict[str, int] = {}

    # Pass 1: honor explicit valid scene numbers cleanly and detect explicit duplicates
    for it in valid_mp4s:
        if it.detected_scene_number > 0:
            if it.detected_scene_number in assigned_scenes_set:
                it.status = "duplicate"
                it.issue = f"Duplicate clip targeting scene_{it.detected_scene_number:02d}.mp4."
                duplicate_scenes.add(it.detected_scene_number)
            elif it.detected_scene_number <= max(len(expected_scenes), 20):
                it.assigned_scene_number = it.detected_scene_number
                it.assigned_filename = f"scene_{it.detected_scene_number:02d}.mp4"
                assigned_scenes_set.add(it.detected_scene_number)
                seen_hashes.setdefault(it.sha256, it.detected_scene_number)

    # Pass 2: assign unnumbered clips deterministically to remaining expected slots & flag byte-identical duplicates
    next_slot = 1
    for it in valid_mp4s:
        if it.status == "duplicate" or it.assigned_scene_number is not None:
            continue
        if it.sha256 in seen_hashes:
            dup_target = seen_hashes[it.sha256]
            it.status = "duplicate"
            it.issue = f"Duplicate clip content identical to scene_{dup_target:02d}.mp4."
            duplicate_scenes.add(dup_target)
            continue
        while next_slot in assigned_scenes_set:
            next_slot += 1
        it.assigned_scene_number = next_slot
        it.assigned_filename = f"scene_{next_slot:02d}.mp4"
        assigned_scenes_set.add(next_slot)
        seen_hashes[it.sha256] = next_slot
        next_slot += 1

    missing_scenes = [num for num in expected_scenes if num not in assigned_scenes_set]

    record = ZipImportRecord(
        import_id=eff_import_id,
        run_id=run_id,
        original_zip_filename=safe_zip_name,
        original_zip_rel_path=str(preserved_zip_path.relative_to(run_export_dir)).replace("\\", "/"),
        zip_sha256=zip_sha,
        zip_size_bytes=zip_size,
        status="preview_ready",
        total_entries_inspected=len(candidates),
        items=items,
        missing_scenes=missing_scenes,
        duplicate_scenes=sorted(duplicate_scenes),
        corrupted_files=corrupted_files,
        ignored_files=ignored_files,
        created_at_utc=now_iso,
        updated_at_utc=_utc_now_iso(),
    )
    storage.save_zip_import(record)
    logger.info(
        "Staged ZIP preview for %s (%s): %d items, missing=%s, duplicates=%s, corrupted=%s",
        run_id, eff_import_id, len(items), missing_scenes, sorted(duplicate_scenes), corrupted_files,
    )
    return record


def apply_zip_mapping_and_stage_clips(
    run_id: str,
    import_id: str,
    package: ProductionPackage,
    mappings: Optional[Dict[str, Optional[int]]] = None,
    *,
    allow_partial: bool = False,
) -> ZipImportRecord:
    """
    Validate user-confirmed preview mapping, detect missing/duplicate conflicts,
    copy validated original clips into FLOW_EXPORTS_DIR/<run_id>/scene_01..0N.mp4
    WITHOUT deleting original ZIP or raw extracted clips.
    """
    from app.storage import storage

    record = storage.load_zip_import(run_id, import_id)
    if not record:
        raise ZipImportValidationError(f"ZIP import '{import_id}' not found for run '{run_id}'.")

    run_export_dir = settings.FLOW_EXPORTS_DIR / run_id
    run_export_dir.mkdir(parents=True, exist_ok=True)
    items_by_id = {it.member_id: it for it in record.items}

    # Apply mapping overrides when supplied (allowing duplicate resolution or reordering)
    if mappings is not None:
        for member_id, target_scene in mappings.items():
            item = items_by_id.get(member_id)
            if not item or item.media_kind != "mp4":
                continue
            if target_scene is not None:
                if item.status == "corrupted":
                    raise ZipImportValidationError(
                        f"Cannot map corrupted clip '{item.original_filename}' to scene_{int(target_scene):02d}.mp4."
                    )
                if int(target_scene) < 1 or int(target_scene) > max(len(package.scenes), 20):
                    raise ZipImportValidationError(
                        f"Invalid target scene number '{target_scene}' for '{item.original_filename}'."
                    )
                item.assigned_scene_number = int(target_scene)
                item.assigned_filename = f"scene_{int(target_scene):02d}.mp4"
                item.status = "valid"
                item.issue = ""
            else:
                item.assigned_scene_number = None
                item.assigned_filename = None

    # Validate zero duplicate target slots across mapped items
    chosen_targets: Dict[int, str] = {}
    for it in record.items:
        if it.media_kind == "mp4" and it.assigned_scene_number is not None:
            if it.status == "corrupted":
                raise ZipImportValidationError(
                    f"Clip '{it.original_filename}' is corrupted ({it.issue}). Exclude it before confirming."
                )
            slot = int(it.assigned_scene_number)
            if slot in chosen_targets:
                raise ZipImportValidationError(
                    f"Duplicate mapping conflict: both '{chosen_targets[slot]}' and '{it.original_filename}' map to scene_{slot:02d}.mp4."
                )
            chosen_targets[slot] = it.original_filename

    if not chosen_targets and not any(it.media_kind == "voiceover_mp3" and it.status == "valid" for it in record.items):
        raise ZipImportValidationError("No valid clips were selected in mapping confirmation.")

    # Copy selected clips atomically while preserving raw/ and original/ completely intact
    confirmed_names: List[str] = []
    for it in record.items:
        raw_src = run_export_dir / it.raw_rel_path
        if not raw_src.exists():
            raise ZipImportValidationError(
                f"Preserved raw clip '{it.original_filename}' could not be read for staging."
            )
        if it.media_kind == "voiceover_mp3" and it.status == "valid":
            dest = run_export_dir / "voiceover.mp3"
            tmp = run_export_dir / f".stage.vo.{secrets.token_hex(4)}.mp3"
            try:
                shutil.copy2(raw_src, tmp)
                os.replace(tmp, dest)
                (run_export_dir / ".custom_voiceover").write_text("1", encoding="utf-8")
                (run_export_dir / ".tts_meta.json").unlink(missing_ok=True)
                confirmed_names.append("voiceover.mp3")
            finally:
                tmp.unlink(missing_ok=True)
        elif it.media_kind == "mp4" and it.assigned_scene_number is not None:
            dest_name = f"scene_{int(it.assigned_scene_number):02d}.mp4"
            dest = run_export_dir / dest_name
            tmp = run_export_dir / f".stage.{dest_name}.part.{secrets.token_hex(4)}"
            try:
                shutil.copy2(raw_src, tmp)
                os.replace(tmp, dest)
                confirmed_names.append(dest_name)
            finally:
                tmp.unlink(missing_ok=True)

    found_numbers = {
        extract_scene_number(f.name)
        for f in run_export_dir.glob("*.mp4")
        if extract_scene_number(f.name) > 0 and f.stat().st_size >= 1024
    }
    missing = [s.scene_number for s in package.scenes if s.scene_number not in found_numbers]

    if not allow_partial and missing:
        record.status = "failed"
        record.missing_scenes = missing
        record.confirmed_clips = sorted(confirmed_names)
        record.error_message = f"Missing scene clip(s): {', '.join(f'scene_{n:02d}.mp4' for n in missing)}. Staged partial clips; upload remaining scenes or enable partial save."
        record.updated_at_utc = _utc_now_iso()
        storage.save_zip_import(record)
        raise ZipImportValidationError(record.error_message)

    record.status = "confirmed"
    record.missing_scenes = missing
    record.duplicate_scenes = []
    record.confirmed_clips = sorted(confirmed_names)
    record.error_message = None
    record.updated_at_utc = _utc_now_iso()
    storage.save_zip_import(record)
    return record


def extract_and_normalize_flow_archive(
    zip_path: Path,
    run_export_dir: Path,
    expected_scene_count: Optional[int] = None,
) -> List[str]:
    """
    Safely extract .mp4 clips and optional voiceover.mp3 from a Google Flow ZIP archive,
    preserving original archive & renaming clips deterministically to scene_01.mp4 .. scene_0N.mp4.
    """
    candidates = _validate_zip_container(zip_path)
    media_candidates = [
        c for c in candidates if c[3].endswith(".mp4") or c[3] == "voiceover.mp3"
    ]
    if not media_candidates:
        raise ValueError("ZIP archive contains no valid .mp4 clips (>= 1 KB) or voiceover.mp3.")
    if len(media_candidates) > MAX_ZIP_MEMBERS:
        raise ValueError(f"ZIP archive contains too many media files ({len(media_candidates)} > {MAX_ZIP_MEMBERS}).")

    run_export_dir.mkdir(parents=True, exist_ok=True)
    vo_infos = [(c[0], c[2], c[3]) for c in media_candidates if c[3] == "voiceover.mp3"]
    mp4_infos = sorted(
        [(c[0], c[2], c[3]) for c in media_candidates if c[3].endswith(".mp4")],
        key=lambda item: _natural_sort_key(item[1]),
    )
    explicit_nums = {extract_scene_number(base) for _, base, _ in mp4_infos if extract_scene_number(base) > 0}
    next_auto_idx = 1
    extracted_names: List[str] = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info, base, lower_base in [*vo_infos, *mp4_infos]:
            if info.file_size < 1024:
                continue
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
                    copied = 0
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > MAX_SINGLE_MEMBER_BYTES:
                            raise ValueError(f"ZIP entry '{base}' exceeded maximum allowed size.")
                        dst.write(chunk)
                os.replace(tmp_entry, dest_path)
                if dest_name == "voiceover.mp3":
                    (run_export_dir / ".custom_voiceover").write_text("1", encoding="utf-8")
                    (run_export_dir / ".tts_meta.json").unlink(missing_ok=True)
                extracted_names.append(dest_name)
            finally:
                tmp_entry.unlink(missing_ok=True)

    return sorted(extracted_names)


def validate_flow_assets(
    run_export_dir: Path,
    package: ProductionPackage,
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
    scene_clips = [f for f in mp4_files if extract_scene_number(f.name) > 0]

    expected_count = len(package.scenes)
    if len(scene_clips) < expected_count:
        errors.append(
            f"Missing scene clips. Expected {expected_count} clips (scene_01..scene_0{expected_count}), found {len(scene_clips)}."
        )

    sorted_clips = sorted(scene_clips, key=lambda f: extract_scene_number(f.name))
    found_numbers = {extract_scene_number(f.name) for f in sorted_clips}
    for scene in package.scenes:
        if scene.scene_number not in found_numbers:
            errors.append(f"Missing expected clip for scene {scene.scene_number} ({scene.export_filename}).")

    for clip in sorted_clips:
        if clip.stat().st_size < 1024:
            errors.append(f"Clip {clip.name} is corrupted or empty ({clip.stat().st_size} bytes).")

    is_valid = len(errors) == 0
    if is_valid:
        logger.info("Asset intake validated for %s: %d clips found in numerical sequence.", package.run_id, len(sorted_clips))
    else:
        logger.warning("Asset intake failed for %s: %s", package.run_id, errors)

    return is_valid, errors, sorted_clips
