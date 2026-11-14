"""SQLite storage for one scan. It only exists while GitHub Actions runs; state.py keeps it between runs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from . import DATA_SCHEMA_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  run_date TEXT NOT NULL,
  time_range TEXT,
  summary TEXT,
  top_takeaway TEXT,
  num_findings INTEGER DEFAULT 0,
  num_updated INTEGER DEFAULT 0,
  num_watchlist_items INTEGER DEFAULT 0,
  num_headlines INTEGER DEFAULT 0,
  report_name TEXT,
  sources_json TEXT
);

CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY,
  source_url TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  first_run_id TEXT NOT NULL,
  date TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  headline TEXT NOT NULL,
  category TEXT,
  tool_or_company TEXT,
  summary TEXT,
  at_home_use TEXT,
  source_name TEXT,
  source_tier TEXT,
  device_access_technical_barrier TEXT,
  priority TEXT NOT NULL,
  score REAL NOT NULL,
  score_breakdown TEXT,
  repeat_relevance_marker TEXT,
  status TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
  watch_id TEXT PRIMARY KEY,
  source_url TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  date_added TEXT NOT NULL,
  item_trend TEXT NOT NULL,
  category TEXT,
  why_watched TEXT,
  what_needs_confirmation TEXT,
  source_lead TEXT,
  priority TEXT,
  status TEXT,
  last_reviewed TEXT,
  notes TEXT
);

-- The news wire: every fresh item a source reported, scored or not. Pruned after 30 days.
CREATE TABLE IF NOT EXISTS headlines (
  url TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  date TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  captured_at TEXT NOT NULL,
  headline TEXT NOT NULL,
  summary TEXT,
  source_name TEXT,
  company TEXT,
  desk TEXT,
  source_tier TEXT,
  signal TEXT
);

CREATE TABLE IF NOT EXISTS foundational_tools (
  tool_id TEXT PRIMARY KEY,
  date_added TEXT,
  tool_name TEXT NOT NULL,
  category TEXT,
  core_use_case TEXT,
  accessibility TEXT,
  why_foundational TEXT,
  status TEXT
);

CREATE TABLE IF NOT EXISTS seen_items (
  url TEXT PRIMARY KEY,
  source_url TEXT NOT NULL,
  fingerprint TEXT,
  outcome TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_state (
  url TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT,
  detail TEXT,
  last_checked TEXT,
  last_success TEXT,
  etag TEXT,
  last_modified TEXT,
  consecutive_failures INTEGER DEFAULT 0,
  circuit_open_until TEXT,
  items_seen INTEGER DEFAULT 0,
  items_emitted INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
"""

# data.json carries at most this many headlines per desk, newest first, so one busy desk
# can't crowd out the others and the page stays quick to load.
HEADLINES_PER_DESK = 150


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "franky.sqlite3"
        self.reports_dir = self.data_dir / "reports"
        self.lock_path = self.data_dir / "scan.lock"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        # executescript commits on its own, so it can't run inside transaction().
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            # A history saved before the news wire existed has a runs table without this column.
            if "num_headlines" not in [row["name"] for row in conn.execute("PRAGMA table_info(runs)")]:
                conn.execute("ALTER TABLE runs ADD COLUMN num_headlines INTEGER DEFAULT 0")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('data_schema_version', ?)",
                (str(DATA_SCHEMA_VERSION),),
            )
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic write. Findings and their outbox events commit together or not at all."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def latest_run_id(self) -> Optional[str]:
        with self.reader() as conn:
            row = conn.execute("SELECT MAX(run_id) AS run_id FROM runs WHERE status != 'running'").fetchone()
        return row["run_id"]

    def source_state(self, url: str) -> Dict:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM source_state WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else {}

    def seen_item(self, url: str) -> Optional[Dict]:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM seen_items WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None

    def seed_tools(self, tools: List[Dict], today: str) -> None:
        with self.transaction() as conn:
            for tool in tools:
                conn.execute(
                    """
                    INSERT INTO foundational_tools
                      (tool_id, date_added, tool_name, category, core_use_case, accessibility, why_foundational, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'Shelf')
                    ON CONFLICT(tool_id) DO UPDATE SET
                      tool_name = excluded.tool_name,
                      category = excluded.category,
                      core_use_case = excluded.core_use_case,
                      accessibility = excluded.accessibility,
                      why_foundational = excluded.why_foundational
                    """,
                    (
                        tool["tool_id"],
                        today,
                        tool["tool_name"],
                        tool.get("category", ""),
                        tool.get("core_use_case", ""),
                        tool.get("accessibility", ""),
                        tool.get("why_foundational", ""),
                    ),
                )

    def backfill_headlines(self, sources: List[Dict], cutoff: str) -> int:
        """Copy recent findings onto the wire, for history saved before the wire existed.

        Only findings whose source is still in the config are copied, because the desk comes
        from there. Safe to repeat: a headline already on the wire is left alone.
        """
        desks = {source["name"]: source.get("desk") or "Industry" for source in sources}
        added = 0
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM findings WHERE captured_at >= ? ORDER BY captured_at, source_url", (cutoff,)).fetchall()
            for row in rows:
                if row["source_name"] not in desks:
                    continue
                date_known = "publish_date_unknown" not in (row["notes"] or "")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO headlines (url, run_id, date, published_at, captured_at, headline, summary,
                                                     source_name, company, desk, source_tier, signal)
                    VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, '')
                    """,
                    (
                        row["source_url"],
                        row["first_run_id"],
                        row["date"] if date_known else "",
                        row["captured_at"],
                        row["headline"],
                        (row["summary"] or "")[:240],
                        row["source_name"],
                        row["tool_or_company"],
                        desks[row["source_name"]],
                        row["source_tier"],
                    ),
                )
                added += cursor.rowcount
        return added

    def dashboard_data(self) -> Dict:
        """The body of data.json. site/index.html depends on these exact keys. See AGENT.md."""
        with self.reader() as conn:
            findings = [dict(r) for r in conn.execute("SELECT * FROM findings ORDER BY date DESC, score DESC")]
            watchlist = [dict(r) for r in conn.execute("SELECT * FROM watchlist ORDER BY date_added DESC")]
            tools = [dict(r) for r in conn.execute("SELECT * FROM foundational_tools ORDER BY tool_name")]
            # Most scans find nothing new. Keep every run with news, plus only the newest quiet one,
            # so a run of quiet scans can't push the last real memo off the list.
            runs = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM runs
                    WHERE status != 'running'
                      AND (num_findings + num_updated + num_watchlist_items > 0
                           OR run_id = (SELECT MAX(run_id) FROM runs WHERE status != 'running'))
                    ORDER BY run_id DESC LIMIT 50
                    """
                )
            ]
            sources = [dict(r) for r in conn.execute("SELECT * FROM source_state ORDER BY name")]
            wire = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM headlines
                    ORDER BY COALESCE(NULLIF(published_at, ''), NULLIF(date, ''), captured_at) DESC, captured_at DESC, url
                    """
                )
            ]

        per_desk: Dict[str, int] = {}
        headlines = []
        for row in wire:
            per_desk[row["desk"]] = per_desk.get(row["desk"], 0) + 1
            if per_desk[row["desk"]] <= HEADLINES_PER_DESK:
                headlines.append(row)

        for row in findings:
            row["score_breakdown"] = json.loads(row["score_breakdown"] or "{}")
        summaries = []
        for run in runs:
            run.pop("sources_json", None)
            report_name = run.pop("report_name", None)
            run["link_to_report"] = "reports/%s" % report_name if report_name else ""
            summaries.append(run)
        for source in sources:
            source.pop("etag", None)
            source.pop("last_modified", None)
        return {
            "schema_version": DATA_SCHEMA_VERSION,
            "updatedAt": runs[0]["finished_at"] if runs else "",
            "headlines": headlines,
            "findings": findings,
            "summaries": summaries,
            "watchlist": watchlist,
            "foundationalTools": tools,
            "sources": sources,
        }
