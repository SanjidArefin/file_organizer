"""File scanning and organization logic for the File Organizer sidecar."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


CATEGORY_EXTENSIONS: dict[str, frozenset[str]] = {
    "video": frozenset({".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}),
    "audio": frozenset({".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"}),
    "image": frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".heic", ".svg"}),
    "gif": frozenset({".gif"}),
    "text": frozenset({".txt", ".md", ".rtf", ".csv", ".log"}),
    "docs": frozenset({".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}),
    "archives": frozenset({".zip", ".rar", ".7z", ".tar", ".gz"}),
    "code": frozenset({
        ".py", ".js", ".ts", ".java", ".c", ".cpp", ".cs", ".go", ".rs",
        ".json", ".xml", ".yml", ".yaml", ".html", ".css", ".sql",
    }),
}
EXTENSION_TO_CATEGORY = {
    extension: category
    for category, extensions in CATEGORY_EXTENSIONS.items()
    for extension in extensions
}
COPY_CHUNK_SIZE_BYTES = 8 * 1024 * 1024
HASH_CHUNK_SIZE_BYTES = 4 * 1024 * 1024
ProgressCallback = Callable[[dict[str, Any]], None]
UNDO_FILE_NAME = "undo.jsonl"
UNDO_OPERATIONS = frozenset({"move", "mkdir", "dedupe_delete"})


def _undo_log_path() -> Path:
    appdata = os.getenv("APPDATA") if os.name == "nt" else None
    if appdata:
        return Path(appdata) / "file-organizer" / UNDO_FILE_NAME
    return Path.home() / ".config" / "file-organizer" / UNDO_FILE_NAME


def _read_undo_entries() -> list[dict[str, Any]]:
    path = _undo_log_path()
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as log_file:
        for line_number, line in enumerate(log_file, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Undo log line {line_number} is invalid JSON: {error}") from error
            if not isinstance(entry, dict) or entry.get("op") not in UNDO_OPERATIONS:
                raise ValueError(f"Undo log line {line_number} has an unsupported entry.")
            entries.append(entry)
    return entries


def _write_undo_entries(entries: list[dict[str, Any]]) -> None:
    path = _undo_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as log_file:
            for entry in entries:
                log_file.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                log_file.write("\n")
            log_file.flush()
            os.fsync(log_file.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _clear_undo_log() -> None:
    _write_undo_entries([])


def _append_undo_entry(entry: dict[str, Any]) -> None:
    if entry.get("op") not in UNDO_OPERATIONS:
        raise ValueError("Unsupported undo entry.")
    path = _undo_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8", newline="\n") as log_file:
        log_file.seek(0, os.SEEK_END)
        original_end = log_file.tell()
        try:
            log_file.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            log_file.write("\n")
            log_file.flush()
            os.fsync(log_file.fileno())
        except OSError:
            log_file.seek(original_end)
            log_file.truncate()
            log_file.flush()
            os.fsync(log_file.fileno())
            raise


def _undo_line_count() -> int:
    path = _undo_log_path()
    if not path.is_file():
        return 0
    with path.open("rb") as log_file:
        return sum(1 for line in log_file if line.strip())


def _copy_without_overwrite(source: Path, destination: Path) -> None:
    created = False
    try:
        with source.open("rb") as source_file, destination.open("xb") as destination_file:
            created = True
            shutil.copyfileobj(source_file, destination_file, COPY_CHUNK_SIZE_BYTES)
            destination_file.flush()
            os.fsync(destination_file.fileno())
        shutil.copystat(source, destination, follow_symlinks=True)
    except OSError:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def _restore_move_after_log_error(destination: Path, source: Path) -> None:
    if os.path.lexists(source):
        raise FileExistsError(f"Cannot roll back because the original path is occupied: {source}")
    os.rename(destination, source)


def _delete_with_undo(source: Path, entry: dict[str, Any]) -> None:
    source.unlink()
    try:
        _append_undo_entry(entry)
    except OSError as log_error:
        try:
            if entry["op"] == "move":
                _restore_move_after_log_error(Path(entry["dst"]), Path(entry["src"]))
            else:
                _copy_without_overwrite(Path(entry["survivor"]), Path(entry["deleted"]))
        except OSError as rollback_error:
            raise OSError(
                f"Could not save undo history ({log_error}); rollback also failed ({rollback_error})."
            ) from log_error
        raise OSError(f"Could not save undo history; the original file was restored: {log_error}") from log_error


@dataclass
class DirectoryNode:
    path: Path
    scanned: bool = False
    complete: bool = False
    files: list[Path] = field(default_factory=list)
    children: list["DirectoryNode"] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path)).casefold()


def classify_file(path: Path) -> str | None:
    return EXTENSION_TO_CATEGORY.get(path.suffix.lower())


def _emit_progress(progress: ProgressCallback | None, **details: Any) -> None:
    if progress is not None:
        progress(details)


def _scan_directory(
    node: DirectoryNode,
    recursive: bool,
    progress: ProgressCallback | None,
    directory_counter: list[int],
) -> None:
    directory_counter[0] += 1
    node.scanned = True
    _emit_progress(
        progress,
        label="Scanning",
        message=f"Scanning {node.path}",
        subfolderIndex=directory_counter[0],
        subfolderTotal=0,
        fileIndex=0,
        fileTotal=0,
        indeterminate=True,
    )
    try:
        with os.scandir(node.path) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
    except OSError as error:
        node.errors.append({"path": str(node.path), "message": str(error)})
        node.complete = recursive
        return

    for entry in entries:
        entry_path = Path(entry.path)
        try:
            if entry.is_file(follow_symlinks=False):
                node.files.append(entry_path)
            elif entry.is_dir(follow_symlinks=False):
                node.children.append(DirectoryNode(path=entry_path))
        except OSError as error:
            node.errors.append({"path": str(entry_path), "message": str(error)})

    if recursive:
        for child in node.children:
            _scan_directory(child, True, progress, directory_counter)
        node.complete = True


def _iter_nodes(node: DirectoryNode) -> list[DirectoryNode]:
    result = [node]
    for child in node.children:
        result.extend(_iter_nodes(child))
    return result


def _node_contents(node: DirectoryNode) -> tuple[list[Path], list[dict[str, str]]]:
    files = list(node.files)
    errors = list(node.errors)
    for child in node.children:
        child_files, child_errors = _node_contents(child)
        files.extend(child_files)
        errors.extend(child_errors)
    return files, errors


def _inspect_directory(path: Path) -> tuple[list[Path], list[dict[str, str]]]:
    files: list[Path] = []
    errors: list[dict[str, str]] = []
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as error:
            errors.append({"path": str(current), "message": str(error)})
            continue
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                if entry.is_file(follow_symlinks=False):
                    files.append(entry_path)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry_path)
            except OSError as error:
                errors.append({"path": str(entry_path), "message": str(error)})
    return files, errors


def _category_from_folder_name(name: str) -> str | None:
    folded = name.casefold()
    for category in CATEGORY_EXTENSIONS:
        if folded == category:
            return category
        suffix = folded.removeprefix(category)
        if folded.startswith(category) and suffix.isdigit() and int(suffix) >= 2:
            return category
    return None


def _category_status(
    path: Path,
    category: str,
    node_index: dict[str, DirectoryNode],
) -> tuple[bool, list[Path], list[dict[str, str]]]:
    if _category_from_folder_name(path.name) != category:
        return False, [], []
    try:
        if not path.is_dir():
            return False, [], []
    except OSError as error:
        return False, [], [{"path": str(path), "message": str(error)}]

    node = node_index.get(_path_key(path))
    if node is not None and node.complete:
        files, errors = _node_contents(node)
    else:
        files, errors = _inspect_directory(path)
    mismatches = [file_path for file_path in files if classify_file(file_path) != category]
    return not errors and not mismatches, mismatches, errors


def _review_key(kind: str, path: Path, category: str) -> str:
    material = f"{kind}\0{_path_key(path)}\0{category}".encode("utf-8", errors="replace")
    return f"{kind}-{hashlib.sha256(material).hexdigest()[:16]}"


def _add_mixed_folder_review(
    reviews: dict[str, dict[str, Any]],
    folder: Path,
    category: str,
    mismatches: list[Path],
) -> None:
    if not mismatches:
        return
    review_id = _review_key("mixed-folder", folder, category)
    reviews.setdefault(
        review_id,
        {
            "id": review_id,
            "type": "mixed_category_folder",
            "description": (
                f"The '{folder.name}' folder contains files outside the {category} category. "
                "Leave those files in place?"
            ),
            "targetPath": str(folder),
            "category": category,
            "files": [str(path) for path in mismatches],
        },
    )


def _add_collision_review(
    reviews: dict[str, dict[str, Any]],
    existing: Path,
    chosen: Path,
    category: str,
) -> None:
    review_id = _review_key("category-collision", existing.parent, category)
    reviews.setdefault(
        review_id,
        {
            "id": review_id,
            "type": "category_name_collision",
            "description": (
                f"An existing '{category}' folder is not a valid category folder. "
                f"New files will use '{chosen.name}'. Keep this name?"
            ),
            "targetPath": str(chosen),
            "existingPath": str(existing),
            "category": category,
        },
    )


def _append_error(
    report: dict[str, Any],
    path: Path,
    message: str,
    *,
    count_failed: bool = True,
) -> None:
    record = {"path": str(path), "message": message}
    if record not in report["errors"]:
        report["errors"].append(record)
        if count_failed:
            report["failed"] += 1


def _new_report(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "scanned": 0,
        "copied": 0,
        "deduplicated": 0,
        "failed": 0,
        "unmatched": [],
        "errors": [],
        "actions": [],
    }


def _resolve_file_path(
    destination_dir: Path,
    file_name: str,
    reserved_paths: set[str],
) -> Path:
    original = Path(file_name)
    candidate = destination_dir / file_name
    index = 1
    while candidate.exists() or _path_key(candidate) in reserved_paths:
        candidate = destination_dir / f"{original.stem}_{index}{original.suffix}"
        index += 1
    reserved_paths.add(_path_key(candidate))
    return candidate


def _sha256(path: Path, on_chunk: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    bytes_hashed = 0
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(HASH_CHUNK_SIZE_BYTES):
            digest.update(chunk)
            bytes_hashed += len(chunk)
            if on_chunk is not None:
                on_chunk(bytes_hashed)
    return digest.hexdigest()


def _same_content(
    left: Path,
    right: Path,
    on_chunk: Callable[[int], None] | None = None,
) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return _sha256(left, on_chunk) == _sha256(right, on_chunk)
    except OSError:
        raise


def _undo_entry(entry: dict[str, Any]) -> None:
    operation = entry.get("op")
    if operation == "move":
        source = Path(entry["src"])
        destination = Path(entry["dst"])
        kind = entry.get("kind")
        if kind not in {"file", "dir"}:
            raise ValueError("Move entry has an unsupported kind.")
        source_exists = os.path.lexists(source)
        destination_exists = os.path.lexists(destination)
        if source_exists:
            raise FileExistsError(f"The original path is now occupied: {source}")
        if not destination_exists:
            raise FileNotFoundError(f"The moved item no longer exists: {destination}")
        os.rename(destination, source)
        return

    if operation == "mkdir":
        path = Path(entry["path"])
        if not os.path.lexists(path):
            return
        path.rmdir()
        return

    if operation == "dedupe_delete":
        deleted = Path(entry["deleted"])
        survivor = Path(entry["survivor"])
        if os.path.lexists(deleted):
            raise FileExistsError(f"The deleted file path is now occupied: {deleted}")
        if not survivor.is_file() or survivor.is_symlink():
            raise FileNotFoundError(f"The deduplication survivor is unavailable: {survivor}")
        _copy_without_overwrite(survivor, deleted)
        return

    raise ValueError("Unsupported undo operation.")


def _start_session() -> dict[str, Any]:
    path = _undo_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = _read_undo_entries()
        count = len(entries)
        return {"priorSession": count > 0, "entryCount": count}
    except (OSError, UnicodeError, ValueError) as error:
        try:
            count = _undo_line_count()
        except OSError:
            count = 0
        return {
            "priorSession": path.is_file() and count > 0,
            "entryCount": count,
            "error": str(error),
        }


def _undo() -> dict[str, Any]:
    try:
        entries = _read_undo_entries()
    except (OSError, UnicodeError, ValueError) as error:
        return {
            "undone": [],
            "errors": [{"message": f"Could not read the undo log: {error}"}],
            "remainingCount": _undo_line_count(),
        }

    undone: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    remaining_reverse: list[dict[str, Any]] = []
    for entry in reversed(entries):
        try:
            _undo_entry(entry)
            undone.append(entry)
        except (OSError, KeyError, TypeError, ValueError) as error:
            errors.append({"entry": entry, "message": str(error)})
            remaining_reverse.append(entry)

    remaining = list(reversed(remaining_reverse))
    try:
        _write_undo_entries(remaining)
    except OSError as error:
        errors.append({"message": f"Could not update the undo log: {error}"})
        return {"undone": undone, "errors": errors, "remainingCount": _undo_line_count()}
    return {"undone": undone, "errors": errors, "remainingCount": len(remaining)}


def _allocate_category_directory(
    parent: Path,
    category: str,
    node_index: dict[str, DirectoryNode],
    cache: dict[tuple[str, str], Path],
    report: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
    deferred_paths: set[str],
) -> Path:
    cache_key = (_path_key(parent), category)
    if cache_key in cache:
        return cache[cache_key]

    exact = parent / category
    try:
        exists = exact.exists()
    except OSError as error:
        _append_error(report, exact, str(error))
        exists = True
    if not exists:
        cache[cache_key] = exact
        return exact

    valid, mismatches, errors = _category_status(exact, category, node_index)
    for error in errors:
        _append_error(report, Path(error["path"]), error["message"])
    if valid:
        cache[cache_key] = exact
        return exact

    _add_mixed_folder_review(reviews, exact, category, mismatches)
    deferred_paths.update(_path_key(path) for path in mismatches)

    suffix = 2
    while True:
        candidate = parent / f"{category}{suffix}"
        try:
            candidate_exists = candidate.exists()
        except OSError as error:
            _append_error(report, candidate, str(error))
            candidate_exists = True
        if not candidate_exists:
            chosen = candidate
            break

        candidate_valid, candidate_mismatches, candidate_errors = _category_status(
            candidate, category, node_index
        )
        for error in candidate_errors:
            _append_error(report, Path(error["path"]), error["message"])
        _add_mixed_folder_review(reviews, candidate, category, candidate_mismatches)
        deferred_paths.update(_path_key(path) for path in candidate_mismatches)
        if candidate_valid:
            chosen = candidate
            break
        suffix += 1

    _add_collision_review(reviews, exact, chosen, category)
    cache[cache_key] = chosen
    return chosen


def _all_errors(reports: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for report in reports.values():
        for error in report["errors"]:
            if error not in errors:
                errors.append(error)
    return errors


def _plan_run(
    payload: dict[str, Any],
    progress: ProgressCallback | None,
) -> dict[str, Any]:
    raw_root = payload.get("sourceFolder")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ValueError("A source folder is required.")
    root = Path(raw_root).expanduser()
    if not root.exists():
        raise ValueError(f"The source folder does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"The selected path is not a folder: {root}")

    include_subfolders = payload.get("includeSubfolders", False)
    verify_hash = payload.get("verifyHash", False)
    if not isinstance(include_subfolders, bool) or not isinstance(verify_hash, bool):
        raise ValueError("Folder scan and verification options must be booleans.")

    tree = DirectoryNode(path=root)
    _scan_directory(tree, include_subfolders, progress, [0])
    all_nodes = _iter_nodes(tree)
    node_index = {_path_key(node.path): node for node in all_nodes}
    category_cache: dict[str, tuple[bool, list[Path], list[dict[str, str]]]] = {}

    for node in all_nodes:
        # The selected root is a source container, never a destination category.
        if node is tree:
            continue
        category = _category_from_folder_name(node.path.name)
        if category:
            category_cache[_path_key(node.path)] = _category_status(
                node.path, category, node_index
            )

    eligible: list[DirectoryNode] = []

    def visit(node: DirectoryNode) -> None:
        status = category_cache.get(_path_key(node.path))
        if status and status[0]:
            return
        eligible.append(node)
        if include_subfolders:
            for child in node.children:
                visit(child)

    visit(tree)
    reports = {_path_key(node.path): _new_report(node.path) for node in eligible}
    if not reports:
        reports[_path_key(root)] = _new_report(root)
    for node in eligible:
        report = reports[_path_key(node.path)]
        for error in node.errors:
            _append_error(report, Path(error["path"]), error["message"])

    reviews: dict[str, dict[str, Any]] = {}
    deferred_paths: set[str] = set()
    for node in eligible:
        category = _category_from_folder_name(node.path.name)
        status = category_cache.get(_path_key(node.path))
        if category and status and not status[0]:
            _, mismatches, _ = status
            _add_mixed_folder_review(reviews, node.path, category, mismatches)
            deferred_paths.update(_path_key(path) for path in mismatches)

    processing_nodes = sorted(
        eligible,
        key=lambda node: (-len(node.path.parts), _path_key(node.path)),
    )
    total_files = sum(len(node.files) for node in processing_nodes)
    total_folders = len(processing_nodes)
    processed_files = 0
    folder_destinations: dict[tuple[str, str], Path] = {}
    reserved_paths: set[str] = set()
    operations: list[dict[str, Any]] = []

    for folder_index, node in enumerate(processing_nodes, start=1):
        report = reports[_path_key(node.path)]
        for source in sorted(node.files, key=lambda path: path.name.casefold()):
            report["scanned"] += 1
            category = classify_file(source)
            source_key = _path_key(source)
            if category is None:
                report["unmatched"].append(str(source))
                if source_key in deferred_paths:
                    report["actions"].append(
                        {"operation": "deferred", "description": f"Deferred {source.name} for review."}
                    )
            elif source_key in deferred_paths:
                report["actions"].append(
                    {"operation": "deferred", "description": f"Deferred {source.name} for review."}
                )
            else:
                destination_dir = _allocate_category_directory(
                    node.path,
                    category,
                    node_index,
                    folder_destinations,
                    report,
                    reviews,
                    deferred_paths,
                )
                try:
                    same_name = destination_dir / source.name
                    if same_name.is_file() and not same_name.is_symlink() and _same_content(
                        source,
                        same_name,
                        on_chunk=lambda hashed: _emit_progress(
                            progress,
                            label="Scanning",
                            message=f"Comparing {source.name} ({hashed} bytes)",
                            subfolderIndex=folder_index,
                            subfolderTotal=total_folders,
                            fileIndex=processed_files,
                            fileTotal=total_files,
                            indeterminate=total_files == 0,
                        ),
                    ):
                        operation = "deduplicate"
                        destination = same_name
                        report["deduplicated"] += 1
                        description = f"Keep identical {same_name}; remove duplicate {source.name}."
                    else:
                        operation = "copy"
                        destination = _resolve_file_path(destination_dir, source.name, reserved_paths)
                        report["copied"] += 1
                        description = f"Copy {source.name} → {destination}"
                    operations.append(
                        {
                            "operation": operation,
                            "source": source,
                            "destination": destination,
                            "destination_dir": destination_dir,
                            "report": report,
                        }
                    )
                    report["actions"].append(
                        {
                            "operation": operation,
                            "source": str(source),
                            "destination": str(destination),
                            "description": description,
                        }
                    )
                except OSError as error:
                    report["failed"] += 1
                    _append_error(report, source, str(error), count_failed=False)
                    report["actions"].append(
                        {"operation": "failed", "description": f"Could not plan {source.name}: {error}"}
                    )

            processed_files += 1
            _emit_progress(
                progress,
                label="Scanning",
                message=f"Planned {source.name}",
                subfolderIndex=folder_index,
                subfolderTotal=total_folders,
                fileIndex=processed_files,
                fileTotal=total_files,
                indeterminate=total_files == 0,
            )

    ordered_reports = sorted(
        reports.values(),
        key=lambda report: _path_key(Path(report["path"])),
    )
    return {
        "root": root,
        "includeSubfolders": include_subfolders,
        "verifyHash": verify_hash,
        "reports": ordered_reports,
        "reportIndex": {_path_key(Path(report["path"])): report for report in ordered_reports},
        "operations": operations,
        "reviewItems": list(reviews.values()),
    }


def _serialize_plan(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "planned",
        "folders": context["reports"],
        "reviewItems": context["reviewItems"],
        "errors": _all_errors(context["reportIndex"]),
    }


def _copy_to_temp(
    source: Path,
    temp_path: Path,
    on_chunk: Callable[[int], None] | None = None,
) -> None:
    bytes_copied = 0
    with source.open("rb") as source_file, temp_path.open("wb") as temp_file:
        while chunk := source_file.read(COPY_CHUNK_SIZE_BYTES):
            temp_file.write(chunk)
            bytes_copied += len(chunk)
            if on_chunk is not None:
                on_chunk(bytes_copied)
        temp_file.flush()
        os.fsync(temp_file.fileno())


def _copy_temp_to_destination(temp_path: Path, destination: Path) -> None:
    created = False
    try:
        with temp_path.open("rb") as source_file, destination.open("xb") as destination_file:
            created = True
            shutil.copyfileobj(source_file, destination_file, COPY_CHUNK_SIZE_BYTES)
            destination_file.flush()
            os.fsync(destination_file.fileno())
    except OSError:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def _commit_without_overwrite(temp_path: Path, destination: Path) -> None:
    try:
        os.link(temp_path, destination)
    except FileExistsError:
        raise
    except OSError:
        _copy_temp_to_destination(temp_path, destination)


def _copy_verify_delete(
    source: Path,
    destination_dir: Path,
    verify_hash: bool,
    on_chunk: Callable[[int], None] | None = None,
) -> tuple[str, Path]:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{source.name}.",
        suffix=".part",
        dir=destination_dir,
    )
    os.close(descriptor)
    temp_path = Path(temp_name)

    try:
        _copy_to_temp(source, temp_path, on_chunk)
        source_size = source.stat().st_size
        if temp_path.stat().st_size != source_size:
            raise OSError(f"Copy verification failed for {source.name}: file sizes differ.")
        if verify_hash and _sha256(source, on_chunk) != _sha256(temp_path, on_chunk):
            raise OSError(f"SHA-256 verification failed for {source.name}.")

        original = Path(source.name)
        index = 0
        while True:
            candidate = destination_dir / (
                source.name if index == 0 else f"{original.stem}_{index}{original.suffix}"
            )
            if candidate.exists():
                if candidate.is_file() and not candidate.is_symlink() and _same_content(
                    source, candidate, on_chunk
                ):
                    _delete_with_undo(
                        source,
                        {
                            "op": "dedupe_delete",
                            "deleted": str(source),
                            "survivor": str(candidate),
                        },
                    )
                    return "deduplicate", candidate
                index += 1
                continue
            try:
                _commit_without_overwrite(temp_path, candidate)
                break
            except FileExistsError:
                index += 1

        temp_path.unlink()
        shutil.copystat(source, candidate, follow_symlinks=True)
        destination_size = candidate.stat().st_size
        if destination_size != source_size:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise OSError(f"Copy verification failed for {source.name}: destination size differs.")
        if verify_hash and _sha256(source, on_chunk) != _sha256(candidate, on_chunk):
            try:
                candidate.unlink()
            except OSError:
                pass
            raise OSError(f"SHA-256 verification failed for {source.name}.")

        _delete_with_undo(
            source,
            {"op": "move", "src": str(source), "dst": str(candidate), "kind": "file"},
        )
        return "copied", candidate
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _create_category_directory(path: Path) -> None:
    try:
        path.mkdir()
    except FileExistsError:
        if path.is_dir():
            return
        raise

    try:
        _append_undo_entry({"op": "mkdir", "path": str(path)})
    except OSError as log_error:
        try:
            path.rmdir()
        except OSError as rollback_error:
            raise OSError(
                f"Could not save undo history ({log_error}); removing the new folder also failed "
                f"({rollback_error})."
            ) from log_error
        raise OSError(f"Could not save undo history; the new folder was removed: {log_error}") from log_error


def _organize(payload: dict[str, Any], progress: ProgressCallback | None) -> dict[str, Any]:
    context = _plan_run(payload, progress)
    reports: dict[str, dict[str, Any]] = context["reportIndex"]
    for report in reports.values():
        report["copied"] = 0
        report["deduplicated"] = 0
        report["actions"] = [
            action for action in report["actions"] if action["operation"] == "deferred"
        ]

    total = len(context["operations"])
    _clear_undo_log()
    for index, operation in enumerate(context["operations"], start=1):
        source: Path = operation["source"]
        destination_dir: Path = operation["destination_dir"]
        report: dict[str, Any] = operation["report"]
        try:
            _create_category_directory(destination_dir)
        except OSError as error:
            report["failed"] += 1
            _append_error(report, destination_dir, str(error), count_failed=False)
            report["actions"].append(
                {"operation": "failed", "description": f"Could not create {destination_dir}: {error}"}
            )
            continue

        try:
            planned_destination: Path = operation["destination"]
            if operation["operation"] == "deduplicate" and planned_destination.exists():
                if _same_content(source, planned_destination):
                    _delete_with_undo(
                        source,
                        {
                            "op": "dedupe_delete",
                            "deleted": str(source),
                            "survivor": str(planned_destination),
                        },
                    )
                    outcome, final_destination = "deduplicate", planned_destination
                else:
                    outcome, final_destination = _copy_verify_delete(
                        source,
                        destination_dir,
                        context["verifyHash"],
                        on_chunk=lambda copied: _emit_progress(
                            progress,
                            label="Organizing",
                            message=f"Processing {source.name} ({copied} bytes)",
                            subfolderIndex=index,
                            subfolderTotal=total,
                            fileIndex=index - 1,
                            fileTotal=total,
                            indeterminate=total == 0,
                        ),
                    )
            else:
                outcome, final_destination = _copy_verify_delete(
                    source,
                    destination_dir,
                    context["verifyHash"],
                    on_chunk=lambda copied: _emit_progress(
                        progress,
                        label="Organizing",
                        message=f"Processing {source.name} ({copied} bytes)",
                        subfolderIndex=index,
                        subfolderTotal=total,
                        fileIndex=index - 1,
                        fileTotal=total,
                        indeterminate=total == 0,
                    ),
                )
            if outcome == "copied":
                report["copied"] += 1
                description = f"Copied {source.name} → {final_destination}"
            else:
                report["deduplicated"] += 1
                description = f"Removed duplicate {source.name}; kept {final_destination}"
            report["actions"].append(
                {
                    "operation": outcome,
                    "source": str(source),
                    "destination": str(final_destination),
                    "description": description,
                }
            )
        except OSError as error:
            report["failed"] += 1
            _append_error(report, source, str(error), count_failed=False)
            report["actions"].append(
                {"operation": "failed", "description": f"Failed {source.name}: {error}"}
            )

        _emit_progress(
            progress,
            label="Organizing",
            message=f"Processed {source.name}",
            subfolderIndex=index,
            subfolderTotal=total,
            fileIndex=index,
            fileTotal=total,
            indeterminate=total == 0,
        )

    return {
        "status": "completed",
        "folders": list(reports.values()),
        "reviewItems": context["reviewItems"],
        "errors": _all_errors(reports),
        "undoCount": _undo_line_count(),
    }


def handle_action(
    action: str,
    payload: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    if action == "start_session":
        return _start_session()
    if action == "undo":
        return _undo()
    if action == "scan":
        return _serialize_plan(_plan_run(payload, progress))
    if action == "organize":
        return _organize(payload, progress)
    if action == "confirm":
        item_id = payload.get("itemId")
        answer = payload.get("answer")
        if not isinstance(item_id, str) or answer not in {"yes", "no"}:
            raise ValueError("A review item ID and a yes/no answer are required.")
        return {"status": "recorded", "itemId": item_id, "answer": answer}
    raise ValueError(f"Unknown action: {action}")


SUPPORTED_ACTIONS = frozenset({"scan", "organize", "confirm", "start_session", "undo"})
