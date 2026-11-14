"""Command line used by GitHub Actions: python3 -m franky run | build | doctor. See .github/workflows/scan.yml."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__, state
from .parsing import normalize_space
from datetime import timedelta

from .scanner import WIRE_KEEP_DAYS, ConfigError, load_sources, load_tools, run_scan
from .site import build_site
from .store import Store
from .timeutil import iso, now_utc

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "config" / "sources.json"
TOOLS_PATH = REPO_ROOT / "config" / "foundational_tools.json"
SITE_SOURCE = REPO_ROOT / "site"
FAILED = ("error", "empty", "robots_blocked", "offline", "circuit_open")


def _print_result(result: Dict) -> None:
    # Feed headlines stay out of the log on purpose. GitHub runs "::" lines in a job's output as
    # commands, so text from the web doesn't get printed anywhere it could start a line.
    print("%s: %s" % (result.get("run_id") or "no run", result["status"]))
    print(
        "new=%s updated=%s watchlist=%s headlines=%s too_old=%s set_aside=%s"
        % (result.get("findings", 0), result.get("updated", 0), result.get("watchlist", 0), result.get("headlines", 0), result.get("stale", 0), result.get("set_aside", 0))
    )
    for source in result["sources"]:
        print("  %-28s %-15s %s" % (source["name"], source["status"], normalize_space(source.get("detail", ""))))
    if os.environ.get("GITHUB_ACTIONS"):
        for source in result["sources"]:
            if source["status"] in FAILED:
                print("::warning::Source %s: %s" % (source["name"], source["status"]))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        rows = "\n".join("| %s | %s |" % (source["name"], source["status"]) for source in result["sources"])
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(
                "### Scan %s\n\n%s new, %s updated, %s on the watchlist, %s new headlines.\n\n| Source | Status |\n|---|---|\n%s\n"
                % (result["status"], result.get("findings", 0), result.get("updated", 0), result.get("watchlist", 0), result.get("headlines", 0), rows)
            )


def cmd_run(args: argparse.Namespace) -> int:
    """Restore history, scan, save history, build the site. Failed sources still publish, honestly."""
    sources = load_sources(SOURCES_PATH)
    tools = load_tools(TOOLS_PATH)
    state_dir = Path(args.state_dir)
    print("History restored." if state.restore(state_dir) else "No history yet. This is the first scan.")
    store = Store(state_dir)
    store.seed_tools(tools, now_utc().date().isoformat())
    store.backfill_headlines(sources, iso(now_utc() - timedelta(days=WIRE_KEEP_DAYS)))
    result = run_scan(store, sources, trigger=os.environ.get("GITHUB_EVENT_NAME", "manual"), force=args.force)
    state.save(store)
    build_site(store, SITE_SOURCE, Path(args.out), repository=os.environ.get("GITHUB_REPOSITORY", ""))
    _print_result(result)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Rebuild the site from saved history without reading any source."""
    state_dir = Path(args.state_dir)
    state.restore(state_dir)
    store = Store(state_dir)
    store.seed_tools(load_tools(TOOLS_PATH), now_utc().date().isoformat())
    store.backfill_headlines(load_sources(SOURCES_PATH), iso(now_utc() - timedelta(days=WIRE_KEEP_DAYS)))
    build_site(store, SITE_SOURCE, Path(args.out), repository=os.environ.get("GITHUB_REPOSITORY", ""))
    print("Built %s" % args.out)
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    problems: List[str] = []
    print("Franky Frontier %s on Python %s" % (__version__, sys.version.split()[0]))
    if sys.version_info < (3, 9):
        problems.append("Python 3.9 or newer is required.")
    try:
        sources = load_sources(SOURCES_PATH)
        print("Config: %d sources, %d enabled" % (len(sources), sum(1 for s in sources if s.get("enabled", True))))
        load_tools(TOOLS_PATH)
    except ConfigError as exc:
        problems.append(str(exc))
    if not (SITE_SOURCE / "index.html").is_file():
        problems.append("site/index.html is missing.")
    for problem in problems:
        print("PROBLEM: %s" % problem)
    print("Ready." if not problems else "Not ready.")
    return 1 if problems else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m franky", description="Franky Frontier scan and site build, run by GitHub Actions")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="scan the sources that are due, then build the site")
    run.add_argument("--state-dir", default=".state", help="where the scan history lives")
    run.add_argument("--out", default="dist", help="where to write the site")
    run.add_argument("--force", action="store_true", help="check every source, even if not due or paused")
    run.set_defaults(func=cmd_run)

    build = commands.add_parser("build", help="rebuild the site from saved history, with no scan")
    build.add_argument("--state-dir", default=".state")
    build.add_argument("--out", default="dist")
    build.set_defaults(func=cmd_build)

    doctor = commands.add_parser("doctor", help="check the setup")
    doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print("Config problem: %s" % exc, file=sys.stderr)
    except (sqlite3.Error, OSError) as exc:
        print("Could not use the scan history: %s" % exc, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
