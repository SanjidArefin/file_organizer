"""Isolated filesystem fixtures for organizer logic tests."""

from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from typing import Iterator

import pytest

BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))

import organizer_logic


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Provide a temporary tree and keep undo history out of user data."""
    with tempfile.TemporaryDirectory(prefix="file-organizer-tests-") as directory:
        root = Path(directory)
        undo_log = root / "isolated-undo" / "undo.jsonl"
        monkeypatch.setattr(organizer_logic, "_undo_log_path", lambda: undo_log)
        yield root
