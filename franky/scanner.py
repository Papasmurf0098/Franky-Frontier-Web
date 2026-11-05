"""The scan pipeline: collect, score, merge, store. It runs inside GitHub Actions.

Each source fails on its own. One broken site never stops the others, and a failing site
is reported in source health rather than hidden. A re-scan is idempotent: an item is
identified by its URL and changes only when its content fingerprint changes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .adapters import ADAPTERS, AdapterError
from .fetcher import FetchError, Fetcher, RobotsBlocked, host_in_domain, site_domain
from .parsing import FeedError, canonical_url, external_url, key_sentences, looks_like_feed, parse_feed, parse_page
from .report import render_report
from .scoring import assess, has_any, similarity
from .store import Store
from .timeutil import iso, now_utc, parse_ts

CLUSTER_THRESHOLD = 0.55
CIRCUIT_FAILURE_LIMIT = 3
CIRCUIT_COOLDOWN = timedelta(hours=24)
ERROR_RETRY_HOURS = 1
SHORT_SUMMARY_CHARS = 80
SUMMARY_LIMIT = 420
TITLE_LIMIT = 240
# After this many article failures in a row, the site is struggling, so stop for this scan.
ARTICLE_FAILURE_LIMIT = 3
VALID_TIERS = {"Tier 1", "Tier 2", "Tier 3"}
PRIORITY_RANK = {"High": 0, "Medium": 1, "Low": 2}
FEED_READ_LIMIT = 50
URL_DATE_TOKEN = re.compile(r"\{(epoch|date)_days_ago:(\d+)\}")
# API sources must work with no account. A key in the config would also end up in a public repo.
CREDENTIAL_IN_URL = re.compile(r"(api_?key|access_?token|client_secret|auth|password|token)=", re.I)
FAILED_STATUSES = {"error", "robots_blocked", "empty", "offline"}


class ConfigError(ValueError):
    pass


class ScanInProgress(RuntimeError):
    pass


def load_sources(path: Path) -> List[Dict]:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("could not read %s: %s" % (path, exc)) from exc
    sources = config.get("sources") if isinstance(config, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ConfigError("%s has no sources" % path)
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ConfigError("%s has a source that isn't an object: %r" % (path, source))
        name = source.get("name") or "(unnamed)"
        for key in ("name", "company", "url", "kind", "category", "trust_tier"):
            if not source.get(key):
                raise ConfigError("source %s is missing %s" % (name, key))
        if not str(source["url"]).startswith(("http://", "https://")):
            raise ConfigError("source %s needs a web address, not %s" % (name, source["url"]))
        if source["url"] in seen:
            raise ConfigError("duplicate source url: %s" % source["url"])
        seen.add(source["url"])
        for key, default in (("cadence_hours", 24), ("max_items", 8)):
            value = source.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError("source %s needs %s to be a whole number above 0" % (name, key))
        if source["kind"] not in ("feed", "page", "api"):
            raise ConfigError("source %s has unknown kind %s" % (name, source["kind"]))
        if source["kind"] == "api" and source.get("adapter") not in ADAPTERS:
            raise ConfigError("source %s needs an adapter, one of: %s" % (name, ", ".join(sorted(ADAPTERS))))
        if CREDENTIAL_IN_URL.search(source["url"]) or source.get("headers"):
            raise ConfigError("source %s looks like it uses a credential. Sources must not need an account" % name)
        for key in ("require_keywords", "exclude_keywords"):
            value = source.get(key, [])
            if not isinstance(value, list) or not all(isinstance(term, str) for term in value):
                raise ConfigError("source %s needs %s to be a list of words" % (name, key))
        if source["trust_tier"] not in VALID_TIERS:
            raise ConfigError("source %s has unknown trust tier %s" % (name, source["trust_tier"]))
        if source["kind"] == "page" and not source.get("article_path"):
            raise ConfigError("page source %s needs article_path" % name)
        for key in ("article_path", "article_path_exclude", "exclude_title_pattern"):
            try:
                re.compile(source.get(key) or "")
            except re.error as exc:
                raise ConfigError("source %s has a bad %s: %s" % (name, key, exc)) from exc
    return sources


def load_tools(path: Path) -> List[Dict]:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("could not read %s: %s" % (path, exc)) from exc
    tools = config.get("tools", []) if isinstance(config, dict) else None
    if not isinstance(tools, list):
        raise ConfigError("%s needs a list of tools" % path)
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("tool_id") or not tool.get("tool_name"):
            raise ConfigError("%s has a tool without tool_id and tool_name: %r" % (path, tool))
    return tools


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


def stable_id(prefix: str, url: str) -> str:
    return "%s-%s" % (prefix, hashlib.sha256(url.encode("utf-8")).hexdigest()[:12])


def _shorten(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def source_due(source: Dict, state: Dict, now: datetime) -> Tuple[bool, str]:
    last_checked = parse_ts(state.get("last_checked"))
    if not last_checked:
        return True, "first check"
    hours = ERROR_RETRY_HOURS if state.get("status") in FAILED_STATUSES else int(source.get("cadence_hours", 24))
    due_at = last_checked + timedelta(hours=hours)
    if now >= due_at:
        return True, "due"
    return False, "next check %s" % iso(due_at)


def circuit_open(state: Dict, now: datetime) -> bool:
    until = parse_ts(state.get("circuit_open_until"))
    return bool(until and until > now)


def _description(info: Dict, source: Dict, listing_description: str = "") -> str:
    """The page's own summary, unless it is the site's stock text on every page."""
    description = str(info.get("description") or "")
    stock = source.get("boilerplate_description")
    is_stock = description == listing_description or bool(stock and re.search(stock, description))
    if description and not is_stock:
        return description
    sentences = key_sentences(str(info.get("text") or ""), limit=3)
    return " ".join([s for s in sentences if s != info.get("title")][:2])


def expand_url(url: str, now: datetime) -> str:
    """Fill in rolling time windows, such as {epoch_days_ago:7} or {date_days_ago:30}."""

    def replace(match) -> str:
        moment = now - timedelta(days=int(match.group(2)))
        return str(int(moment.timestamp())) if match.group(1) == "epoch" else moment.strftime("%Y-%m-%d")

    return URL_DATE_TOKEN.sub(replace, url)


def _without_excluded_titles(entries: List[Dict], source: Dict) -> List[Dict]:
    pattern = source.get("exclude_title_pattern")
    return [entry for entry in entries if not re.search(pattern, entry["title"])] if pattern else entries


def _collect_api(source: Dict, text: str, store: Store) -> Dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AdapterError("response is not valid JSON: %s" % exc) from exc
    entries = _without_excluded_titles(ADAPTERS[source["adapter"]](data), source)

    required = source.get("require_keywords") or []
    excluded = source.get("exclude_keywords") or []
    kept = []
    for entry in entries:
        haystack = ("%s %s" % (entry["title"], entry["summary"])).lower()
        if required and not has_any(haystack, required):
            continue
        if has_any(haystack, excluded):
            continue
        kept.append(entry)
    kept = kept[: int(source.get("max_items", 8))]

    items: List[Dict] = []
    for entry in kept:
        try:
            url = external_url(entry["link"])
        except ValueError:
            # Not a web address, so it never becomes a link on the dashboard.
            continue
        # Attention isn't in the fingerprint, or every new upvote would count as an edit.
        mark = fingerprint(entry["title"], entry["summary"])
        seen = store.seen_item(url)
        if seen and (seen["fingerprint"] == mark or seen["source_url"] != source["url"]):
            # Another source already has this page, and a community repost must not replace it.
            continue
        items.append(
            {
                "title": entry["title"],
                "description": entry["summary"],
                "text": "",
                "url": url,
                "published": entry["published"],
                "published_source": "api" if entry["published"] else "unknown",
                "status": "updated" if seen else "new",
                "fingerprint": mark,
                "signal": entry["signal"],
                "strength": entry["strength"],
            }
        )
    return {"items": items, "seen": len(kept), "article_errors": 0, "first_error": "", "deferred": 0, "off_domain": 0}


def _collect_feed(source: Dict, text: str, final_url: str, domain: str, store: Store, fetcher: Fetcher) -> Dict:
    # A generous read first, because excluded titles shouldn't use up the item limit.
    entries = _without_excluded_titles(parse_feed(text, final_url, max_items=FEED_READ_LIMIT), source)
    entries = entries[: int(source.get("max_items", 8))]
    items: List[Dict] = []
    article_errors = 0
    failures_in_a_row = 0
    deferred = 0
    first_error = ""
    skipped_off_domain = 0
    for entry in entries:
        url = canonical_url(entry["link"])
        if not host_in_domain(_host(url), domain):
            skipped_off_domain += 1
            continue
        mark = fingerprint(entry["title"], entry["summary"])
        seen = store.seen_item(url)
        if seen and seen["fingerprint"] == mark:
            continue
        item = {
            # A prefix gives bare titles meaning: a release feed's "v0.9" becomes "Ollama v0.9".
            "title": "%s%s" % (source.get("title_prefix", ""), entry["title"]),
            "description": entry["summary"],
            "text": "",
            "url": url,
            "published": entry["published"],
            "published_source": "feed:entry" if entry["published"] else "unknown",
            "status": "updated" if seen else "new",
            "fingerprint": mark,
        }
        # Some feeds carry only a title. Read the article only when the feed isn't enough.
        if len(entry["summary"]) < SHORT_SUMMARY_CHARS and source.get("read_articles", True):
            if failures_in_a_row >= ARTICLE_FAILURE_LIMIT:
                if not entry["summary"]:
                    deferred += 1
                    continue
            else:
                try:
                    info = parse_page(fetcher.get(url, allowed_domain=domain).text, url)
                    failures_in_a_row = 0
                    item["text"] = str(info["text"])
                    item["description"] = entry["summary"] or _description(info, source)
                    if not item["published"] and info["published"]:
                        item["published"] = str(info["published"])
                        item["published_source"] = str(info["published_source"])
                except FetchError as exc:
                    article_errors += 1
                    failures_in_a_row += 1
                    first_error = first_error or exc.reason
                    # A temporary failure shouldn't store a blank summary for good. Try next scan.
                    if exc.retryable and not entry["summary"]:
                        deferred += 1
                        continue
        items.append(item)
    return {
        "items": items,
        "seen": len(entries),
        "article_errors": article_errors,
        "first_error": first_error,
        "deferred": deferred,
        "off_domain": skipped_off_domain,
    }


def _collect_page(source: Dict, text: str, final_url: str, domain: str, store: Store, fetcher: Fetcher, force: bool) -> Dict:
    listing = parse_page(text, final_url)
    include = re.compile(source["article_path"])
    exclude = re.compile(source["article_path_exclude"]) if source.get("article_path_exclude") else None
    listing_url = canonical_url(final_url)
    candidates: List[str] = []
    for href, _label in listing["links"]:
        url = canonical_url(href)
        path = re.sub(r"^https?://[^/]+", "", url)
        if url == listing_url or url in candidates:
            continue
        if not host_in_domain(_host(url), domain) or not include.search(path):
            continue
        if exclude and exclude.search(path):
            continue
        candidates.append(url)
    candidates = candidates[: int(source.get("max_items", 8))]

    items: List[Dict] = []
    article_errors = 0
    failures_in_a_row = 0
    first_error = ""
    attempted = 0
    for url in candidates:
        seen = store.seen_item(url)
        if seen and not force:
            continue
        if failures_in_a_row >= ARTICLE_FAILURE_LIMIT:
            break
        attempted += 1
        try:
            info = parse_page(fetcher.get(url, allowed_domain=domain).text, url)
            failures_in_a_row = 0
        except FetchError as exc:
            article_errors += 1
            failures_in_a_row += 1
            first_error = first_error or exc.reason
            continue
        if not info["title"]:
            article_errors += 1
            first_error = first_error or "page has no title"
            continue
        description = _description(info, source, listing_description=str(listing["description"]))
        mark = fingerprint(str(info["title"]), description)
        if seen and seen["fingerprint"] == mark:
            continue
        items.append(
            {
                "title": str(info["title"]),
                "description": description,
                "text": str(info["text"]),
                "url": url,
                "published": str(info["published"]),
                "published_source": str(info["published_source"]),
                "status": "updated" if seen else "new",
                "fingerprint": mark,
            }
        )
    return {
        "items": items,
        "seen": len(candidates),
        "article_errors": article_errors,
        "first_error": first_error,
        "attempted": attempted,
        "deferred": 0,
        "off_domain": 0,
    }


def _host(url: str) -> str:
    match = re.match(r"^https?://([^/:]+)", url)
    return match.group(1) if match else ""


def collect_source(source: Dict, state: Dict, store: Store, fetcher: Fetcher, force: bool, now: datetime) -> Dict:
    domain = site_domain(source["url"])
    result: Dict = {"source": source, "status": "ok", "detail": "", "items": [], "seen": 0, "network_error": False}
    try:
        fetched = fetcher.get(
            expand_url(source["url"], now),
            allowed_domain=domain,
            etag="" if force else (state.get("etag") or ""),
            last_modified="" if force else (state.get("last_modified") or ""),
        )
        if fetched.not_modified:
            result.update(status="not_modified", detail="Unchanged since last check.", etag=state.get("etag"), last_modified=state.get("last_modified"))
            return result
        result.update(etag=fetched.etag, last_modified=fetched.last_modified)
        if source["kind"] == "api":
            # No results is a normal answer here, unlike a feed or page with nothing readable.
            collected = _collect_api(source, fetched.text, store)
        elif source["kind"] == "feed" or looks_like_feed(fetched.text):
            collected = _collect_feed(source, fetched.text, fetched.final_url, domain, store, fetcher)
            if not collected["seen"]:
                result.update(status="empty", detail="The feed had no readable entries. The feed format may have changed.")
                return result
        else:
            collected = _collect_page(source, fetched.text, fetched.final_url, domain, store, fetcher, force)
            if not collected["seen"]:
                result.update(status="empty", detail="No article links found. The page layout may have changed.")
                return result
            if collected["attempted"] and collected["article_errors"] >= collected["attempted"]:
                result.update(status="error", detail="All %d article pages failed to load (%s)." % (collected["attempted"], collected["first_error"]))
                return result
        result.update(items=collected["items"], seen=collected["seen"])
        if collected["article_errors"] or collected["deferred"]:
            # Otherwise the next scan gets a 304 and never retries what failed here.
            result.update(etag="", last_modified="")
        notes = []
        if collected["article_errors"]:
            notes.append("%d article pages failed (%s)" % (collected["article_errors"], collected["first_error"]))
        if collected["off_domain"]:
            notes.append("%d off-domain links ignored" % collected["off_domain"])
        result["detail"] = "; ".join(notes)
    except RobotsBlocked as exc:
        result.update(status="robots_blocked", detail=exc.reason)
    except FetchError as exc:
        result.update(status="error", detail=exc.reason, network_error=exc.network)
    except (FeedError, AdapterError) as exc:
        result.update(status="error", detail=str(exc))
    except Exception as exc:  # A bug in one source must not take down the whole scan.
        result.update(status="error", detail="Unexpected %s: %s" % (type(exc).__name__, exc))
    return result


def _cluster(assessed: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Merge the same story from different sources. Strongest source's copy wins."""
    ordered = sorted(assessed, key=lambda entry: entry["assessment"]["score"], reverse=True)
    kept: List[Dict] = []
    merged: List[Dict] = []
    for entry in ordered:
        for existing in kept:
            same_page = existing["item"]["url"] == entry["item"]["url"]
            if same_page or similarity(existing["item"]["title"], entry["item"]["title"]) >= CLUSTER_THRESHOLD:
                if entry["source"]["name"] != existing["source"]["name"]:
                    existing["also"].append(entry["source"]["name"])
                merged.append(entry)
                break
        else:
            entry["also"] = []
            kept.append(entry)
    return kept, merged


def _watch_category(assessment: Dict) -> Tuple[str, str]:
    if assessment["frontier"]:
        return "Frontier Watch", "Is it actually available at home yet? Check access, price, and device support."
    if assessment["repeat_marker"]:
        return "Momentum", "Keeps coming up. Is there a real use behind the repeat coverage?"
    return "Confirmation Gap", "Weak signal so far. Wait for a clear at-home use or a stronger source."


def _save_entry(conn, entry: Dict, run_id: str, run_date: str, now: datetime) -> Dict:
    item, source, a = entry["item"], entry["source"], entry["assessment"]
    url = item["url"]
    access_parts = [a["device_access"], a["technical_barrier"]]
    if a["on_device"]:
        access_parts.append("Runs locally")
    access = " | ".join(access_parts)
    summary = _shorten(item["description"])
    signal = item.get("signal", "")
    if not summary and signal:
        # API results link to pages we never read, so all we can report is the attention.
        summary, signal = "%s." % signal, ""
    summary = summary or "No summary on the source page."
    note_parts = []
    if a["flags"]:
        note_parts.append("Flags: %s" % ", ".join(a["flags"]))
    if signal:
        note_parts.append(signal)
    if source.get("caution"):
        note_parts.append(source["caution"])
    if entry["also"]:
        note_parts.append("Also reported by %s" % ", ".join(sorted(set(entry["also"]))))
    notes = ". ".join(part.rstrip(".") for part in note_parts)
    shown_date = item["published"] or run_date
    created: Dict = {"finding": None, "updated": None, "watch": None}

    existing = conn.execute("SELECT finding_id, first_run_id FROM findings WHERE source_url = ?", (url,)).fetchone()
    if a["priority"] in ("High", "Medium") or existing:
        row = {
            "finding_id": stable_id("F", url),
            "source_url": url,
            "run_id": run_id,
            "first_run_id": existing["first_run_id"] if existing else run_id,
            "date": shown_date,
            "captured_at": iso(now),
            "headline": item["title"],
            "category": source["category"],
            "tool_or_company": source["company"],
            "summary": summary,
            "at_home_use": a["at_home_use"],
            "source_name": source["name"],
            "source_tier": source["trust_tier"],
            "device_access_technical_barrier": access,
            "priority": a["priority"],
            "score": a["score"],
            "score_breakdown": json.dumps(a["breakdown"], sort_keys=True),
            "repeat_relevance_marker": a["repeat_marker"],
            "status": "Updated" if existing else "New",
            "notes": notes,
        }
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        conn.execute("INSERT OR REPLACE INTO findings (%s) VALUES (%s)" % (columns, placeholders), tuple(row.values()))
        created["updated" if existing else "finding"] = row

    if a["priority"] == "Low" or a["frontier"] or a["repeat_marker"]:
        category, needs = _watch_category(a)
        known = conn.execute("SELECT run_id, date_added FROM watchlist WHERE source_url = ?", (url,)).fetchone()
        watch = {
            "watch_id": stable_id("W", url),
            "source_url": url,
            "run_id": known["run_id"] if known else run_id,
            "date_added": known["date_added"] if known else run_date,
            "item_trend": item["title"],
            "category": category,
            "why_watched": summary,
            "what_needs_confirmation": needs,
            "source_lead": source["name"],
            "priority": a["priority"],
            "status": "Watching",
            "last_reviewed": run_date,
            "notes": notes,
        }
        columns = ", ".join(watch)
        placeholders = ", ".join("?" for _ in watch)
        conn.execute("INSERT OR REPLACE INTO watchlist (%s) VALUES (%s)" % (columns, placeholders), tuple(watch.values()))
        if not known:
            created["watch"] = watch
    return created


def _remember(conn, entry: Dict, outcome: str, now: datetime) -> None:
    item = entry["item"]
    conn.execute(
        """
        INSERT INTO seen_items (url, source_url, fingerprint, outcome, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET fingerprint = excluded.fingerprint, outcome = excluded.outcome, last_seen = excluded.last_seen
        """,
        (item["url"], entry["source"]["url"], item["fingerprint"], outcome, iso(now), iso(now)),
    )


def _save_source_state(conn, result: Dict, state: Dict, now: datetime) -> None:
    source = result["source"]
    failed = result["status"] in FAILED_STATUSES
    if result["status"] == "offline":
        # The laptop being offline says nothing about the site, so it doesn't count against it.
        failures = state.get("consecutive_failures") or 0
        circuit_until = state.get("circuit_open_until")
    else:
        failures = (state.get("consecutive_failures") or 0) + 1 if failed else 0
        # A circuit breaker stops a broken site being hit on every scan. It reopens after the cooldown.
        circuit_until = iso(now + CIRCUIT_COOLDOWN) if failures >= CIRCUIT_FAILURE_LIMIT else None
    conn.execute(
        """
        INSERT INTO source_state (url, name, status, detail, last_checked, last_success, etag, last_modified,
                                  consecutive_failures, circuit_open_until, items_seen, items_emitted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          name = excluded.name, status = excluded.status, detail = excluded.detail,
          last_checked = excluded.last_checked, last_success = excluded.last_success,
          etag = excluded.etag, last_modified = excluded.last_modified,
          consecutive_failures = excluded.consecutive_failures, circuit_open_until = excluded.circuit_open_until,
          items_seen = excluded.items_seen, items_emitted = excluded.items_emitted
        """,
        (
            source["url"],
            source["name"],
            result["status"],
            result.get("detail", ""),
            iso(now),
            state.get("last_success") if failed else iso(now),
            state.get("etag") if failed else (result.get("etag") or ""),
            state.get("last_modified") if failed else (result.get("last_modified") or ""),
            failures,
            circuit_until,
            result.get("seen", 0),
            len(result.get("items", [])),
        ),
    )


class _ScanLock:
    """One scan at a time across the server and the command line. The OS frees it on a crash."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, "w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise ScanInProgress("another scan is already running") from exc
        return self

    def __exit__(self, *exc_info):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def _new_run_id(conn, moment: datetime) -> str:
    # UTC, because IDs are sorted as text to find the latest run. Local time would sort
    # wrongly after a clock change or a flight west.
    base = "FF-%s" % moment.astimezone(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    run_id, suffix = base, 2
    while conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone():
        run_id, suffix = "%s-%d" % (base, suffix), suffix + 1
    return run_id


def _tidy_item(item: Dict, today: date) -> None:
    """Fix what a source got wrong before scoring trusts it."""
    item["title"] = _shorten(item["title"], TITLE_LIMIT)
    published = item.get("published")
    # One day ahead is normal, because feeds use UTC. Further ahead is a typo or scheduling.
    if published and published > (today + timedelta(days=1)).isoformat():
        item["published"] = ""
        item["published_source"] = "unknown"


def _write_report(store: Store, run_row: Dict, findings: List[Dict], watch: List[Dict], failed: List[Dict]) -> Tuple[Optional[Path], str]:
    """A report problem must never undo a scan whose findings are already saved."""
    name = "%s.md" % run_row["run_id"]
    path = store.reports_dir / name
    try:
        path.write_text(render_report(run_row, findings, watch, failed), encoding="utf-8")
    except OSError as exc:
        return None, "Could not write the report: %s" % exc
    return path, ""


def run_scan(
    store: Store,
    sources: List[Dict],
    fetcher: Optional[Fetcher] = None,
    trigger: str = "manual",
    force: bool = False,
    now: Optional[datetime] = None,
    today: Optional[date] = None,
) -> Dict:
    fetcher = fetcher or Fetcher()
    moment = now or now_utc()
    local_day = today or moment.astimezone().date()
    with _ScanLock(store.lock_path):
        return _run_locked(store, sources, fetcher, trigger, force, moment, local_day)


def _run_locked(store, sources, fetcher, trigger, force, moment, local_day) -> Dict:
    enabled = [s for s in sources if s.get("enabled", True)]
    states = {s["url"]: store.source_state(s["url"]) for s in enabled}

    to_check: List[Dict] = []
    skipped: List[Dict] = []
    for source in enabled:
        state = states[source["url"]]
        if not force and circuit_open(state, moment):
            skipped.append({"name": source["name"], "url": source["url"], "status": "circuit_open", "detail": "Paused after repeated failures until %s." % state.get("circuit_open_until")})
            continue
        due, reason = source_due(source, state, moment)
        if not due and not force:
            skipped.append({"name": source["name"], "url": source["url"], "status": "not_due", "detail": reason})
            continue
        to_check.append(source)

    if not to_check:
        return {"run_id": None, "status": "nothing_due", "sources": skipped, "findings": 0, "updated": 0, "watchlist": 0}

    run_date = local_day.isoformat()
    with store.transaction() as conn:
        # The lock is held, so a row still marked running belongs to a scan that died.
        conn.execute("UPDATE runs SET status = 'interrupted' WHERE status = 'running'")
        previous = conn.execute(
            "SELECT run_date FROM runs WHERE status LIKE 'completed%' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        run_id = _new_run_id(conn, moment)
        conn.execute(
            "INSERT INTO runs (run_id, trigger, status, started_at, run_date) VALUES (?, ?, 'running', ?, ?)",
            (run_id, trigger, iso(moment), run_date),
        )

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(collect_source, source, states[source["url"]], store, fetcher, force, moment)
                for source in to_check
            ]
            results = [future.result() for future in futures]

        # If nothing at all answered, the problem is the scan's own connection, not the sites.
        offline = all(result["network_error"] for result in results)
        if offline:
            for result in results:
                result.update(status="offline", detail="Couldn't reach it. The scan itself looks offline, so the next one will try again.")

        with store.reader() as conn:
            ledger = conn.execute("SELECT source_url, headline FROM findings").fetchall()

        assessed: List[Dict] = []
        stale: List[Dict] = []
        set_aside: List[Dict] = []
        for result in results:
            for item in result["items"]:
                _tidy_item(item, local_day)
                prior = [row["headline"] for row in ledger if row["source_url"] != item["url"]]
                assessment = assess(item, result["source"], prior, local_day)
                entry = {"item": item, "source": result["source"], "assessment": assessment, "also": []}
                if assessment["stale"]:
                    stale.append(entry)
                elif result["source"].get("require_usable") and not assessment["usable"]:
                    # A busy forum is mostly opinion, which isn't an opportunity.
                    set_aside.append(entry)
                else:
                    assessed.append(entry)
        kept, merged = _cluster(assessed)

        new_findings: List[Dict] = []
        updated_findings: List[Dict] = []
        new_watch: List[Dict] = []
        failed = [r for r in results if r["status"] in FAILED_STATUSES]
        checked = len(results)
        failed_note = " %d could not be read." % len(failed) if failed else ""
        with store.transaction() as conn:
            # The same order the dashboard uses, so the memo's top item matches its lead story.
            ranked = sorted(kept, key=lambda e: e["assessment"]["score"], reverse=True)
            ranked.sort(key=lambda e: e["item"].get("published") or "", reverse=True)
            ranked.sort(key=lambda e: PRIORITY_RANK[e["assessment"]["priority"]])
            for entry in ranked:
                created = _save_entry(conn, entry, run_id, run_date, moment)
                if created["finding"]:
                    new_findings.append(created["finding"])
                if created["updated"]:
                    updated_findings.append(created["updated"])
                if created["watch"]:
                    new_watch.append(created["watch"])
                _remember(conn, entry, "stored", moment)
            for entry in merged:
                _remember(conn, entry, "merged", moment)
            for entry in stale:
                _remember(conn, entry, "stale", moment)
            for entry in set_aside:
                _remember(conn, entry, "set_aside", moment)
            for result in results:
                _save_source_state(conn, result, states[result["source"]["url"]], moment)
            for item in skipped:
                if item["status"] == "circuit_open":
                    conn.execute("UPDATE source_state SET status = 'circuit_open', detail = ? WHERE url = ?", (item["detail"], item["url"]))

            highlights = new_findings or updated_findings
            top = "%s (%s)" % (highlights[0]["headline"], highlights[0]["source_name"]) if highlights else "Nothing new since the last scan."
            summary = (
                "Checked %d sources.%s Found %d new, %d updated, and %d on the watchlist. %d too old to count."
                % (checked, failed_note, len(new_findings), len(updated_findings), len(new_watch), len(stale))
            )
            if set_aside:
                summary += " %d set aside as discussion with no clear use." % len(set_aside)
            if offline:
                summary = "Couldn't reach any of the %d sources. The scan itself looks offline, so nothing was checked." % checked
            start_day = previous["run_date"] if previous else (local_day - timedelta(days=60)).isoformat()
            status = "offline" if offline else "failed" if len(failed) == checked else "completed_with_errors" if failed else "completed"
            conn.execute(
                """
                UPDATE runs SET status = ?, finished_at = ?, time_range = ?, summary = ?, top_takeaway = ?,
                       num_findings = ?, num_updated = ?, num_watchlist_items = ?, sources_json = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    iso(now_utc()),
                    "%s to %s" % (start_day, run_date),
                    summary,
                    top,
                    len(new_findings),
                    len(updated_findings),
                    len(new_watch),
                    json.dumps(
                        [{"name": r["source"]["name"], "status": r["status"], "detail": r.get("detail", ""), "seen": r.get("seen", 0), "emitted": len(r["items"])} for r in results] + skipped
                    ),
                    run_id,
                ),
            )

        report_path, report_warning = None, ""
        if new_findings or updated_findings or new_watch:
            run_row = {"run_id": run_id, "time_range": "%s to %s" % (start_day, run_date), "summary": summary}
            failed_names = [{"name": r["source"]["name"]} for r in failed]
            report_path, report_warning = _write_report(store, run_row, new_findings + updated_findings, new_watch, failed_names)
            if report_path:
                with store.transaction() as conn:
                    conn.execute("UPDATE runs SET report_name = ? WHERE run_id = ?", (report_path.name, run_id))
    except BaseException:
        with store.transaction() as conn:
            conn.execute("UPDATE runs SET status = 'failed', finished_at = ? WHERE run_id = ?", (iso(now_utc()), run_id))
        raise

    return {
        "run_id": run_id,
        "status": status,
        "summary": summary,
        "top_takeaway": top,
        "findings": len(new_findings),
        "updated": len(updated_findings),
        "watchlist": len(new_watch),
        "stale": len(stale),
        "set_aside": len(set_aside),
        "merged": len(merged),
        "report": str(report_path) if report_path else "",
        "report_warning": report_warning,
        "sources": [
            {"name": r["source"]["name"], "status": r["status"], "detail": r.get("detail", ""), "seen": r.get("seen", 0), "emitted": len(r["items"])}
            for r in results
        ] + skipped,
    }

