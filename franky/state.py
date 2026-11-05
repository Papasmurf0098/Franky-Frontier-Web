"""Carry scan history from one GitHub Actions run to the next.

A runner is thrown away after every job, so nothing it writes survives on its own. The history
lives as a plain SQL text file on the repo's scan-state branch (ci/state.sh fetches and pushes
it). Text instead of a database file means each scan is a small, readable diff, and git can show
what changed and when.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .store import Store

DUMP_NAME = "franky.sql"
DB_NAME = "franky.sqlite3"
# The working database is rebuilt every run, so only the text version belongs in git.
STATE_GITIGNORE = "*.sqlite3\n*.sqlite3-*\nscan.lock\n"


def restore(state_dir: Path) -> bool:
    """Rebuild the working database from the saved text. Returns False on the very first run.

    A damaged file raises instead of starting over, because the next save would otherwise
    replace the whole history with an empty one.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    for leftover in state_dir.glob(DB_NAME + "*"):
        leftover.unlink()
    dump = state_dir / DUMP_NAME
    if not dump.exists():
        return False
    conn = sqlite3.connect(str(state_dir / DB_NAME))
    try:
        conn.executescript(dump.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return True


def save(store: Store) -> Path:
    """Write the database out as text, replacing the old file only once the new one is complete."""
    state_dir = Path(store.data_dir)
    conn = sqlite3.connect(str(store.db_path))
    try:
        text = "\n".join(conn.iterdump()) + "\n"
    finally:
        conn.close()
    dump = state_dir / DUMP_NAME
    partial = state_dir / (DUMP_NAME + ".partial")
    partial.write_text(text, encoding="utf-8")
    partial.replace(dump)
    (state_dir / ".gitignore").write_text(STATE_GITIGNORE, encoding="utf-8")
    return dump
