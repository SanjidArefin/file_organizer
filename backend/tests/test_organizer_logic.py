"""Filesystem-level regression tests for the organizer sidecar logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import organizer_logic


def _write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def _organize(folder: Path, *, recursive: bool = False) -> dict:
    return organizer_logic._organize(
        {
            "sourceFolder": str(folder),
            "includeSubfolders": recursive,
            "verifyHash": False,
        },
        None,
    )


def _snapshot(root: Path) -> tuple[tuple[str, ...], tuple[tuple[str, bytes], ...]]:
    directories = tuple(sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir()
    ))
    files = tuple(sorted(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    ))
    return directories, files


def test_non_recursive_organize_leaves_subfolders_and_unmatched_files(workspace: Path) -> None:
    source = workspace / "source"
    _write(source / "movie.mp4", "video bytes")
    _write(source / "notes.txt", "notes")
    _write(source / "unknown.xyz", "unknown")
    _write(source / "sub" / "nested.mp4", "nested video")

    result = _organize(source)

    assert (source / "video" / "movie.mp4").read_bytes() == b"video bytes"
    assert (source / "text" / "notes.txt").read_bytes() == b"notes"
    assert (source / "unknown.xyz").read_bytes() == b"unknown"
    assert (source / "sub" / "nested.mp4").read_bytes() == b"nested video"
    assert not (source / "sub" / "video").exists()
    assert any(str(source / "unknown.xyz") in report["unmatched"]
               for report in result["folders"])


def test_recursive_organize_preserves_folder_structure(workspace: Path) -> None:
    source = workspace / "source"
    _write(source / "root.mp4", "root video")
    _write(source / "sub" / "image.jpg", "sub image")
    _write(source / "sub" / "deep" / "clip.mp4", "deep video")

    _organize(source, recursive=True)

    assert (source / "video" / "root.mp4").read_bytes() == b"root video"
    assert (source / "sub" / "image" / "image.jpg").read_bytes() == b"sub image"
    assert (source / "sub" / "deep" / "video" / "clip.mp4").read_bytes() == b"deep video"
    assert (source / "sub" / "deep").is_dir()


def test_already_sorted_tree_reuses_existing_category_folders(workspace: Path) -> None:
    source = workspace / "source"
    _write(source / "first.mp4", "first video")
    _write(source / "picture.jpg", "picture")
    _organize(source)

    _write(source / "second.mp4", "second video")
    second_run = _organize(source)

    assert (source / "video" / "first.mp4").is_file()
    assert (source / "video" / "second.mp4").read_bytes() == b"second video"
    assert (source / "image" / "picture.jpg").is_file()
    assert not (source / "video" / "video").exists()
    assert not (source / "image" / "image").exists()
    assert not any(error["path"].endswith(("video", "image"))
                   for error in second_run["errors"])


def test_category_name_collision_uses_suffix_and_queues_review(workspace: Path) -> None:
    source = workspace / "source"
    _write(source / "video" / "unrelated.jpg", "image in wrong folder")
    _write(source / "movie.mp4", "video")

    result = _organize(source)

    assert (source / "video" / "unrelated.jpg").is_file()
    assert (source / "video2" / "movie.mp4").read_bytes() == b"video"
    assert any(item["type"] == "category_name_collision"
               and item["targetPath"] == str(source / "video2")
               for item in result["reviewItems"])


def test_dedupe_removes_duplicate_and_undo_restores_it(workspace: Path) -> None:
    source = workspace / "source"
    survivor = _write(source / "image" / "photo.jpg", b"identical image")
    duplicate = _write(source / "photo.jpg", b"identical image")

    result = _organize(source)

    assert result["folders"][0]["deduplicated"] == 1
    assert not duplicate.exists()
    assert survivor.read_bytes() == b"identical image"
    log_entries = organizer_logic._read_undo_entries()
    assert any(entry["op"] == "dedupe_delete" for entry in log_entries)

    undo_result = organizer_logic._undo()

    assert undo_result["errors"] == []
    assert undo_result["remainingCount"] == 0
    assert duplicate.read_bytes() == b"identical image"
    assert survivor.read_bytes() == b"identical image"


def test_filename_collision_gets_numeric_suffix(workspace: Path) -> None:
    source = workspace / "source"
    _write(source / "video" / "clip.mp4", b"existing")
    _write(source / "clip.mp4", b"new content")

    _organize(source)

    assert (source / "video" / "clip.mp4").read_bytes() == b"existing"
    assert (source / "video" / "clip_1.mp4").read_bytes() == b"new content"
    assert not (source / "clip.mp4").exists()


def test_nonexistent_source_raises_clean_value_error(workspace: Path) -> None:
    missing = workspace / "missing"

    with pytest.raises(ValueError, match="source folder does not exist"):
        organizer_logic._plan_run({"sourceFolder": str(missing)}, None)


def test_undo_round_trip_restores_tree_and_removes_created_categories(workspace: Path) -> None:
    source = workspace / "source"
    _write(source / "movie.mp4", b"video")
    _write(source / "sub" / "image.jpg", b"image")
    _write(source / "unknown.xyz", b"unknown")
    before = _snapshot(source)

    _organize(source, recursive=True)
    assert (source / "video").is_dir()
    assert (source / "sub" / "image").is_dir()

    result = organizer_logic._undo()

    assert result["errors"] == []
    assert result["remainingCount"] == 0
    assert _snapshot(source) == before
    assert not (source / "video").exists()
    assert not (source / "sub" / "image").exists()


def test_undo_reports_missing_dedupe_survivor_and_keeps_entry(workspace: Path) -> None:
    source = workspace / "source"
    survivor = _write(source / "image" / "photo.jpg", b"identical image")
    duplicate = _write(source / "photo.jpg", b"identical image")
    _organize(source)
    survivor.unlink()

    result = organizer_logic._undo()

    assert len(result["errors"]) == 1
    assert "survivor is unavailable" in result["errors"][0]["message"]
    assert not duplicate.exists()
    assert result["remainingCount"] == 1
    assert organizer_logic._read_undo_entries()[0]["op"] == "dedupe_delete"


def test_scan_planning_does_not_clear_existing_undo_log(workspace: Path) -> None:
    source = workspace / "source"
    _write(source / "movie.mp4", b"video")
    undo_log = organizer_logic._undo_log_path()
    undo_log.parent.mkdir(parents=True)
    seeded_entry = {"op": "move", "src": "original", "dst": "organized", "kind": "file"}
    seeded_line = json.dumps(seeded_entry) + "\n"
    undo_log.write_text(seeded_line, encoding="utf-8")

    organizer_logic._plan_run({"sourceFolder": str(source)}, None)

    assert undo_log.read_text(encoding="utf-8") == seeded_line
    assert organizer_logic._read_undo_entries() == [seeded_entry]


def test_new_organize_run_clears_previous_undo_log(workspace: Path) -> None:
    source = workspace / "empty-source"
    source.mkdir()
    undo_log = organizer_logic._undo_log_path()
    undo_log.parent.mkdir(parents=True)
    undo_log.write_text(
        json.dumps({"op": "move", "src": "old", "dst": "old", "kind": "file"}) + "\n",
        encoding="utf-8",
    )

    _organize(source)

    assert undo_log.read_text(encoding="utf-8") == ""
    assert organizer_logic._undo_line_count() == 0


def test_start_session_detects_prior_undo_entries(workspace: Path) -> None:
    undo_log = organizer_logic._undo_log_path()
    undo_log.parent.mkdir(parents=True)
    undo_log.write_text(
        json.dumps({"op": "mkdir", "path": "previous-category"}) + "\n",
        encoding="utf-8",
    )

    assert organizer_logic._start_session() == {"priorSession": True, "entryCount": 1}
