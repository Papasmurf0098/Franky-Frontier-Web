"""The news wire: every fresh item becomes a headline, and the radar is the scored subset."""

import json
import re
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from franky import adapters, state
from franky.parsing import parse_feed, parse_timestamp
from franky.scanner import DESKS, WIRE_KEEP_DAYS, ConfigError, load_sources, run_scan
from franky.store import HEADLINES_PER_DESK, Store
from franky.timeutil import iso

from .helpers import FEED_HEADERS, FIXED_NOW, FIXED_TODAY, LONG_TEXT, FakeSite, quick_fetcher, rss, source_for, temp_store

REPO = Path(__file__).resolve().parent.parent
HTML = (REPO / "site" / "index.html").read_text(encoding="utf-8")
ROBOTS_OK = {"/robots.txt": (200, "User-agent: *\nAllow: /\n", {"Content-Type": "text/plain"})}
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
FRESH = "Tue, 03 Nov 2026 10:00:00 GMT"


def scan(store, sources, **kwargs):
    kwargs.setdefault("fetcher", quick_fetcher())
    kwargs.setdefault("now", FIXED_NOW)
    kwargs.setdefault("today", FIXED_TODAY)
    return run_scan(store, sources, **kwargs)


class Wire(unittest.TestCase):
    def setUp(self):
        self.store = temp_store(self)
        self.site = FakeSite(dict(ROBOTS_OK))
        self.addCleanup(self.site.close)

    def feed(self, path, *items):
        self.site.routes[path] = (200, rss(*items), FEED_HEADERS)
        return self.site.base + path

    def item(self, path, title="A new agent app", date=FRESH, text=LONG_TEXT):
        return (title, self.site.base + path, date, text)

    def rows(self, table):
        with self.store.reader() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM %s" % table)]

    def test_a_scored_item_is_also_a_headline(self):
        source = source_for(self.feed("/feed", self.item("/one")), desk="Labs")
        result = scan(self.store, [source])
        self.assertEqual((result["findings"], result["headlines"]), (1, 1))
        row = self.rows("headlines")[0]
        self.assertEqual((row["headline"], row["desk"], row["source_name"]), ("A new agent app", "Labs", "Test Source"))
        self.assertEqual((row["date"], row["published_at"]), ("2026-11-03", "2026-11-03T10:00:00Z"))
        self.assertIn("1 new on the news wire", result["summary"])

    def test_a_wire_only_source_stays_off_the_radar(self):
        source = source_for(self.feed("/feed", self.item("/one")), wire_only=True, desk="Industry")
        result = scan(self.store, [source])
        self.assertEqual((result["findings"], result["watchlist"], result["headlines"]), (0, 0, 1))
        self.assertEqual(self.rows("findings") + self.rows("watchlist"), [])
        self.assertEqual(self.rows("seen_items")[0]["outcome"], "wire")

    def test_a_wire_only_source_never_reads_article_pages(self):
        source = source_for(self.feed("/feed", self.item("/one", text="Short")), wire_only=True)
        scan(self.store, [source])
        self.assertEqual(self.site.hits("/one"), 0)
        self.assertEqual(len(self.rows("headlines")), 1)

    def test_rescanning_adds_no_headlines(self):
        source = source_for(self.feed("/feed", self.item("/one")), wire_only=True)
        scan(self.store, [source])
        again = scan(self.store, [source], force=True, now=FIXED_NOW + timedelta(minutes=5))
        self.assertEqual(again["headlines"], 0)
        self.assertEqual(len(self.rows("headlines")), 1)

    def test_an_edited_headline_is_updated_in_place(self):
        source = source_for(self.feed("/feed", self.item("/one")), wire_only=True)
        scan(self.store, [source])
        self.feed("/feed", self.item("/one", title="A new agent app, now free"))
        again = scan(self.store, [source], force=True, now=FIXED_NOW + timedelta(minutes=5))
        rows = self.rows("headlines")
        self.assertEqual((again["headlines"], len(rows), rows[0]["headline"]), (0, 1, "A new agent app, now free"))

    def test_discussion_the_radar_sets_aside_is_still_news(self):
        source = source_for(self.feed("/feed", self.item("/one", title="Thoughts on the industry", text="An essay about where things are going next year and why.")), require_usable=True, read_articles=False)
        result = scan(self.store, [source])
        self.assertEqual((result["findings"], result["set_aside"], result["headlines"]), (0, 1, 1))

    def test_stale_items_stay_off_the_wire(self):
        source = source_for(self.feed("/feed", self.item("/old", date="Mon, 03 Aug 2026 10:00:00 GMT")), wire_only=True)
        result = scan(self.store, [source])
        self.assertEqual((result["stale"], result["headlines"]), (1, 0))

    def test_the_first_source_to_report_a_page_keeps_it(self):
        first = source_for(self.feed("/a", self.item("/one")), name="First", wire_only=True, desk="Labs")
        second = source_for(self.feed("/b", self.item("/one", title="Same page, other words")), name="Second", wire_only=True, desk="Industry")
        scan(self.store, [first])
        scan(self.store, [second], now=FIXED_NOW + timedelta(minutes=5))
        rows = self.rows("headlines")
        self.assertEqual([(row["source_name"], row["headline"]) for row in rows], [("First", "A new agent app")])

    def test_a_general_feed_keeps_only_what_is_on_topic(self):
        url = self.feed("/feed", self.item("/ai", title="A chatbot for your garden"), self.item("/tv", title="The best TVs this year", text="Screens, sizes and prices compared for the living room this season."))
        scan(self.store, [source_for(url, wire_only=True, require_keywords=["chatbot"])])
        self.assertEqual([row["headline"] for row in self.rows("headlines")], ["A chatbot for your garden"])

    def test_a_feed_with_nothing_on_topic_is_not_a_broken_feed(self):
        url = self.feed("/feed", self.item("/tv", title="The best TVs this year", text="Screens and prices."))
        result = scan(self.store, [source_for(url, wire_only=True, require_keywords=["chatbot"])])
        self.assertEqual(result["sources"][0]["status"], "ok")

    def test_old_headlines_are_pruned(self):
        source = source_for(self.feed("/feed", self.item("/one")), wire_only=True, cadence_hours=1)
        scan(self.store, [source])
        later = FIXED_NOW + timedelta(days=WIRE_KEEP_DAYS + 1)
        self.feed("/feed", self.item("/two", title="Something newer", date="Thu, 03 Dec 2026 10:00:00 GMT"))
        scan(self.store, [source], now=later, today=later.date())
        self.assertEqual([row["headline"] for row in self.rows("headlines")], ["Something newer"])

    def test_a_scan_with_only_headlines_counts_them_on_the_run(self):
        source = source_for(self.feed("/feed", self.item("/one")), wire_only=True)
        scan(self.store, [source])
        summary = self.store.dashboard_data()["summaries"][0]
        self.assertEqual((summary["num_findings"], summary["num_headlines"]), (0, 1))

    def test_data_json_caps_each_desk(self):
        with self.store.transaction() as conn:
            for index in range(HEADLINES_PER_DESK + 5):
                conn.execute(
                    "INSERT INTO headlines (url, run_id, date, captured_at, headline, desk) VALUES (?, 'r', '2026-11-03', ?, ?, 'Industry')",
                    ("https://x.test/%d" % index, "2026-11-03T10:%02d:%02dZ" % (index // 60, index % 60), "Story %d" % index),
                )
            conn.execute("INSERT INTO headlines (url, run_id, date, captured_at, headline, desk) VALUES ('https://x.test/lab', 'r', '2026-10-20', '2026-10-20T10:00:00Z', 'Old lab story', 'Labs')")
        headlines = self.store.dashboard_data()["headlines"]
        self.assertEqual(len(headlines), HEADLINES_PER_DESK + 1)
        # A busy desk can't push a quiet desk's only story out.
        self.assertIn("Old lab story", [row["headline"] for row in headlines])
        self.assertEqual(headlines[0]["headline"], "Story %d" % (HEADLINES_PER_DESK + 4))

    def test_data_json_keeps_the_headline_contract(self):
        scan(self.store, [source_for(self.feed("/feed", self.item("/one")), wire_only=True)])
        row = self.store.dashboard_data()["headlines"][0]
        self.assertTrue({"url", "headline", "summary", "source_name", "company", "desk", "source_tier", "date", "published_at", "captured_at", "signal"} <= set(row))


class Backfill(unittest.TestCase):
    def setUp(self):
        self.store = temp_store(self)
        self.site = FakeSite(dict(ROBOTS_OK))
        self.addCleanup(self.site.close)
        self.site.routes["/feed"] = (200, rss(("A new agent app", self.site.base + "/one", FRESH, LONG_TEXT)), FEED_HEADERS)
        self.source = source_for(self.site.base + "/feed", desk="Builders")
        scan(self.store, [self.source])
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM headlines")

    def count(self):
        with self.store.reader() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM headlines")]

    def test_findings_from_before_the_wire_are_copied_once(self):
        cutoff = iso(FIXED_NOW - timedelta(days=WIRE_KEEP_DAYS))
        self.assertEqual(self.store.backfill_headlines([self.source], cutoff), 1)
        self.assertEqual(self.store.backfill_headlines([self.source], cutoff), 0)
        row = self.count()[0]
        self.assertEqual((row["headline"], row["desk"], row["date"]), ("A new agent app", "Builders", "2026-11-03"))

    def test_old_findings_and_removed_sources_are_left_out(self):
        self.assertEqual(self.store.backfill_headlines([self.source], iso(FIXED_NOW + timedelta(days=1))), 0)
        self.assertEqual(self.store.backfill_headlines([], iso(FIXED_NOW - timedelta(days=1))), 0)


class OldHistory(unittest.TestCase):
    def test_history_saved_before_the_wire_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            dump = state.save(store)
            text = dump.read_text(encoding="utf-8")
            # What scan-state held before this change: no headlines table, no num_headlines column.
            text = re.sub(r"CREATE TABLE headlines \(.*?\);\n", "", text, flags=re.S).replace("  num_headlines INTEGER DEFAULT 0,\n", "")
            self.assertNotIn("headlines", text)
            dump.write_text(text, encoding="utf-8")
            self.assertTrue(state.restore(Path(folder)))
            store = Store(Path(folder))
            conn = sqlite3.connect(str(store.db_path))
            try:
                columns = [row[1] for row in conn.execute("PRAGMA table_info(runs)")]
                conn.execute("SELECT COUNT(*) FROM headlines").fetchone()
            finally:
                conn.close()
            self.assertIn("num_headlines", columns)
            self.assertEqual(store.dashboard_data()["headlines"], [])


class Timestamps(unittest.TestCase):
    def test_reads_feed_and_api_times_as_utc(self):
        self.assertEqual(parse_timestamp("Tue, 03 Nov 2026 10:00:00 +0100"), "2026-11-03T09:00:00Z")
        self.assertEqual(parse_timestamp("2026-11-03T10:00:00.000Z"), "2026-11-03T10:00:00Z")

    def test_a_bare_date_is_not_a_time(self):
        self.assertEqual(parse_timestamp("2026-11-03"), "")
        self.assertEqual(parse_timestamp("soon"), "")
        self.assertEqual(parse_timestamp(None), "")

    def test_feed_entries_carry_the_time(self):
        entry = parse_feed(rss(("T", "https://a.test/1", FRESH, "x")), "https://a.test/", 5)[0]
        self.assertEqual(entry["published_at"], "2026-11-03T10:00:00Z")


class Papers(unittest.TestCase):
    def paper(self, **overrides):
        paper = {"id": "2611.01234", "title": "A  small\nlocal model", "summary": "We <b>train</b> it.", "upvotes": 40, "submittedOnDailyAt": "2026-11-03T00:00:00.000Z"}
        paper.update(overrides)
        return {"paper": paper, "numComments": 2, "publishedAt": "2026-10-30T00:00:00.000Z"}

    def test_maps_a_paper(self):
        entry = adapters.hugging_face_papers([self.paper()])[0]
        self.assertEqual(entry["title"], "A small local model")
        self.assertEqual(entry["link"], "https://huggingface.co/papers/2611.01234")
        self.assertEqual((entry["published"], entry["summary"], entry["strength"]), ("2026-11-03", "We train it.", 1))
        self.assertEqual(entry["signal"], "40 upvotes and 2 comments on Hugging Face Papers")

    def test_an_id_that_could_change_the_link_is_dropped(self):
        self.assertEqual(adapters.hugging_face_papers([self.paper(id="../../evil?x=1"), self.paper(id=""), {"paper": "nope"}, "junk"]), [])

    def test_the_wrong_shape_is_an_error(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.hugging_face_papers({"error": "nope"})


class Config(unittest.TestCase):
    def setUp(self):
        self.sources = load_sources(REPO / "config" / "sources.json")

    def test_every_source_files_under_a_known_desk(self):
        for source in self.sources:
            self.assertIn(source.get("desk"), DESKS, source["name"])
        self.assertEqual({source["desk"] for source in self.sources}, set(DESKS))

    def test_the_page_knows_every_desk(self):
        for desk in DESKS:
            self.assertIn('{ id: "%s"' % desk, HTML)
            self.assertIn(".tone-%s {" % desk.lower(), HTML)

    def test_source_names_are_unique(self):
        # Source health and the backfill both look sources up by name.
        names = [source["name"] for source in self.sources]
        self.assertEqual(len(names), len(set(names)))

    def test_an_unknown_desk_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sources.json"
            for bad in ({"desk": "Gossip"}, {"wire_only": "yes"}):
                path.write_text(json.dumps({"sources": [source_for("https://x.test/feed", **bad)]}), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_sources(path)


if __name__ == "__main__":
    unittest.main()
