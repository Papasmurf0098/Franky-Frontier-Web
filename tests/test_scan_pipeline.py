import sqlite3
import unittest
from datetime import timedelta
from unittest import mock

from franky.fetcher import FetchError, OffDomain, RobotsBlocked
from franky.scanner import ScanInProgress, _ScanLock, run_scan

from .helpers import FEED_HEADERS, FIXED_NOW, FIXED_TODAY, LONG_TEXT, FakeSite, quick_fetcher, rss, source_for, temp_store

ROBOTS_OK = {"/robots.txt": (200, "User-agent: *\nAllow: /\n", {"Content-Type": "text/plain"})}


def item_tuple(site, path, title="A new agent app", date="Tue, 03 Nov 2026 10:00:00 GMT", text=LONG_TEXT):
    return (title, site.base + path, date, text)


def scan(store, sources, **kwargs):
    kwargs.setdefault("fetcher", quick_fetcher())
    kwargs.setdefault("now", FIXED_NOW)
    kwargs.setdefault("today", FIXED_TODAY)
    return run_scan(store, sources, **kwargs)


class Fetching(unittest.TestCase):
    def setUp(self):
        self.site = FakeSite(dict(ROBOTS_OK))
        self.addCleanup(self.site.close)

    def test_retries_a_500_then_succeeds_with_backoff(self):
        calls = []

        def flaky(_handler):
            calls.append(1)
            return (503, "busy", {}) if len(calls) < 3 else (200, "fine", {})

        self.site.routes["/flaky"] = flaky
        fetcher = quick_fetcher()
        result = fetcher.get(self.site.base + "/flaky")
        self.assertEqual((result.text, result.attempts), ("fine", 3))
        self.assertEqual(len(fetcher.recorded_sleeps), 2)
        self.assertLessEqual(fetcher.recorded_sleeps[0], fetcher.recorded_sleeps[1] * 2)

    def test_a_404_is_not_retried(self):
        with self.assertRaises(FetchError) as caught:
            quick_fetcher().get(self.site.base + "/missing")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.site.hits("/missing"), 1)

    def test_retry_after_is_honored_and_capped(self):
        self.site.routes["/slow"] = (429, "wait", {"Retry-After": "9999"})
        fetcher = quick_fetcher(attempts=2, max_delay=60)
        with self.assertRaises(FetchError):
            fetcher.get(self.site.base + "/slow")
        self.assertEqual(fetcher.recorded_sleeps, [60])

    def test_robots_is_obeyed(self):
        self.site.routes["/robots.txt"] = (200, "User-agent: *\nDisallow: /private\n", {"Content-Type": "text/plain"})
        self.site.routes["/private"] = "secret"
        with self.assertRaises(RobotsBlocked):
            quick_fetcher().get(self.site.base + "/private")
        self.assertEqual(self.site.hits("/private"), 0)

    def test_a_missing_robots_file_allows_fetching(self):
        self.site.routes.pop("/robots.txt")
        self.site.routes["/page"] = "ok"
        self.assertEqual(quick_fetcher().get(self.site.base + "/page").text, "ok")

    def test_redirect_off_domain_is_refused(self):
        other = FakeSite({"/stolen": "x", **ROBOTS_OK})
        self.addCleanup(other.close)
        # 127.0.0.1 vs localhost are different hosts as far as the domain check goes.
        target = "http://localhost:%d/stolen" % other.port
        self.site.routes["/hop"] = (302, "", {"Location": target})
        with self.assertRaises(OffDomain):
            quick_fetcher().get(self.site.base + "/hop", allowed_domain="127.0.0.1")
        self.assertEqual(other.hits("/stolen"), 0)

    def test_non_text_response_is_refused(self):
        self.site.routes["/file.zip"] = (200, "PK", {"Content-Type": "application/zip"})
        with self.assertRaises(FetchError):
            quick_fetcher().get(self.site.base + "/file.zip")

    def test_oversized_response_is_refused(self):
        self.site.routes["/big"] = "x" * 2000
        with self.assertRaises(FetchError):
            quick_fetcher(max_bytes=1000).get(self.site.base + "/big")


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.store = temp_store(self)
        self.site = FakeSite(dict(ROBOTS_OK))
        self.addCleanup(self.site.close)
        self.source = source_for(self.site.base + "/feed")

    def set_feed(self, *items, headers=None):
        self.site.routes["/feed"] = (200, rss(*items), {**FEED_HEADERS, **(headers or {})})

    def rows(self, table):
        with self.store.reader() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM %s" % table)]

    def test_scan_stores_findings_and_summary(self):
        self.set_feed(item_tuple(self.site, "/one"))
        result = scan(self.store, [self.source])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["findings"], 1)
        data = self.store.dashboard_data()
        self.assertEqual(data["findings"][0]["headline"], "A new agent app")
        self.assertEqual(data["summaries"][0]["run_id"], result["run_id"])
        self.assertTrue(data["summaries"][0]["link_to_report"].endswith(".md"))
        self.assertEqual(data["sources"][0]["status"], "ok")

    def test_rescan_is_idempotent(self):
        self.set_feed(item_tuple(self.site, "/one"))
        scan(self.store, [self.source])
        second = scan(self.store, [self.source], force=True, now=FIXED_NOW + timedelta(minutes=5))
        self.assertEqual(second["findings"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(len(self.rows("findings")), 1)

    def test_changed_item_is_updated_not_duplicated(self):
        self.set_feed(item_tuple(self.site, "/one"))
        first = scan(self.store, [self.source])
        self.set_feed(item_tuple(self.site, "/one", text=LONG_TEXT + " Now with more."))
        second = scan(self.store, [self.source], force=True, now=FIXED_NOW + timedelta(minutes=5))
        self.assertEqual(second["updated"], 1)
        row = self.rows("findings")[0]
        self.assertEqual(row["status"], "Updated")
        self.assertEqual(row["first_run_id"], first["run_id"])
        self.assertEqual(row["run_id"], second["run_id"])

    def test_quiet_scans_do_not_bury_the_last_real_memo(self):
        # Regression: most scans find nothing, and each one blanked the summary and report link.
        self.set_feed(item_tuple(self.site, "/one"))
        news = scan(self.store, [self.source])
        with self.store.transaction() as conn:
            for number in range(60):
                conn.execute(
                    "INSERT INTO runs (run_id, trigger, status, started_at, run_date) VALUES (?, 'schedule', 'completed', 'x', 'x')",
                    ("FF-2099-01-01-%06d" % number,),
                )
        summaries = self.store.dashboard_data()["summaries"]
        self.assertEqual([row["run_id"] for row in summaries], ["FF-2099-01-01-000059", news["run_id"]])
        self.assertTrue(summaries[1]["link_to_report"])

    def test_cadence_skips_sources_not_due(self):
        self.set_feed(item_tuple(self.site, "/one"))
        scan(self.store, [self.source])
        hits = self.site.hits("/feed")
        result = scan(self.store, [self.source], now=FIXED_NOW + timedelta(hours=1))
        self.assertEqual(result["status"], "nothing_due")
        self.assertEqual(self.site.hits("/feed"), hits)
        scan(self.store, [self.source], now=FIXED_NOW + timedelta(hours=25))
        self.assertEqual(self.site.hits("/feed"), hits + 1)

    def test_conditional_request_when_unchanged(self):
        def feed(handler):
            if handler.headers.get("If-None-Match") == '"v1"':
                return 304, "", {}
            return 200, rss(item_tuple(self.site, "/one")), {**FEED_HEADERS, "ETag": '"v1"'}

        self.site.routes["/feed"] = feed
        scan(self.store, [self.source])
        result = scan(self.store, [self.source], now=FIXED_NOW + timedelta(hours=25))
        self.assertEqual(result["sources"][0]["status"], "not_modified")
        self.assertEqual(len(self.rows("findings")), 1)

    def test_one_failing_source_does_not_stop_the_others(self):
        self.set_feed(item_tuple(self.site, "/one"))
        broken = source_for(self.site.base + "/gone", name="Broken")
        result = scan(self.store, [broken, self.source])
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["findings"], 1)
        states = {s["name"]: s for s in self.store.dashboard_data()["sources"]}
        self.assertEqual(states["Broken"]["status"], "error")
        self.assertIn("404", states["Broken"]["detail"])
        with open(result["report"], encoding="utf-8") as report:
            self.assertIn("Broken", report.read())

    def test_all_sources_failing_is_reported_as_failed(self):
        result = scan(self.store, [source_for(self.site.base + "/gone")])
        self.assertEqual(result["status"], "failed")

    def test_circuit_opens_after_repeated_failures_then_recovers(self):
        broken = source_for(self.site.base + "/gone")
        moment = FIXED_NOW
        for _ in range(3):
            scan(self.store, [broken], now=moment)
            moment += timedelta(hours=2)
        hits = self.site.hits("/gone")
        paused = scan(self.store, [broken], now=moment)
        self.assertEqual(paused["sources"][0]["status"], "circuit_open")
        self.assertEqual(self.site.hits("/gone"), hits)
        self.site.routes["/gone"] = (200, rss(item_tuple(self.site, "/one")), FEED_HEADERS)
        later = scan(self.store, [broken], now=moment + timedelta(hours=25))
        self.assertEqual(later["sources"][0]["status"], "ok")
        self.assertEqual(self.store.source_state(broken["url"])["consecutive_failures"], 0)

    def test_force_overrides_an_open_circuit(self):
        broken = source_for(self.site.base + "/gone")
        moment = FIXED_NOW
        for _ in range(3):
            scan(self.store, [broken], now=moment)
            moment += timedelta(hours=2)
        hits = self.site.hits("/gone")
        scan(self.store, [broken], now=moment, force=True)
        self.assertEqual(self.site.hits("/gone"), hits + 1)

    def test_off_domain_feed_links_are_ignored(self):
        # A feed must not be able to steer the scanner at another host, including this laptop.
        self.set_feed(item_tuple(self.site, "/one"), ("Evil", "http://169.254.169.254/latest/", "Tue, 03 Nov 2026 10:00:00 GMT", ""))
        result = scan(self.store, [self.source])
        self.assertEqual(result["findings"], 1)
        self.assertIn("1 off-domain", result["sources"][0]["detail"])

    def test_hostile_feed_is_a_source_error(self):
        self.site.routes["/feed"] = (200, '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>', FEED_HEADERS)
        result = scan(self.store, [self.source])
        self.assertEqual(result["sources"][0]["status"], "error")

    def test_stale_items_are_dropped_and_not_rescored(self):
        self.set_feed(item_tuple(self.site, "/old", date="Wed, 01 Jul 2026 10:00:00 GMT"))
        result = scan(self.store, [self.source])
        self.assertEqual((result["stale"], result["findings"]), (1, 0))
        self.assertEqual(self.rows("seen_items")[0]["outcome"], "stale")

    def test_same_story_from_two_sources_is_merged(self):
        self.set_feed(item_tuple(self.site, "/one", title="Acme launches agent builder app"))
        self.site.routes["/feed2"] = (200, rss(item_tuple(self.site, "/two", title="Acme launches agent builder app today")), FEED_HEADERS)
        second = source_for(self.site.base + "/feed2", name="Second Source", trust_tier="Tier 2")
        result = scan(self.store, [self.source, second])
        self.assertEqual((result["findings"], result["merged"]), (1, 1))
        self.assertIn("Also reported by Second Source", self.rows("findings")[0]["notes"])

    def test_low_priority_goes_to_watchlist_only(self):
        self.set_feed(item_tuple(self.site, "/one", title="Acme appoints a new chief of staff"))
        result = scan(self.store, [self.source])
        self.assertEqual((result["findings"], result["watchlist"]), (0, 1))
        self.assertEqual(self.rows("watchlist")[0]["category"], "Confirmation Gap")

    def test_speculative_finding_also_lands_in_frontier_watch(self):
        self.set_feed(item_tuple(self.site, "/one", title="Build agents with Acme", text=LONG_TEXT + " Join the waitlist."))
        scan(self.store, [self.source])
        self.assertEqual(self.rows("watchlist")[0]["category"], "Frontier Watch")
        self.assertEqual(len(self.rows("findings")), 1)

    def test_page_source_reads_listing_then_articles(self):
        self.site.routes["/news"] = (
            '<html><head><meta name="description" content="Stock site text"></head><body>'
            '<a href="/news/first-post">First</a><a href="/news/category">Cat</a><a href="/about">About</a></body></html>'
        )
        self.site.routes["/news/first-post"] = (
            '<html><head><meta name="description" content="Stock site text">'
            '<meta property="og:title" content="Build a new agent app"/>'
            '<meta property="article:published_time" content="2026-11-03"/></head>'
            "<body><article><p>This free guide shows how you can build an agent at home with no fuss.</p></article></body></html>"
        )
        source = source_for(self.site.base + "/news", kind="page", article_path="^/news/[^/]+$", article_path_exclude="^/news/category$")
        result = scan(self.store, [source])
        self.assertEqual(result["findings"], 1)
        self.assertEqual(self.site.hits("/news/category"), 0)
        row = self.rows("findings")[0]
        self.assertEqual(row["headline"], "Build a new agent app")
        self.assertIn("free guide", row["summary"])

    def test_page_with_no_article_links_says_so(self):
        self.site.routes["/news"] = "<html><body><p>Redesigned, no links.</p></body></html>"
        source = source_for(self.site.base + "/news", kind="page", article_path="^/news/[^/]+$")
        result = scan(self.store, [source])
        self.assertEqual(result["sources"][0]["status"], "empty")
        self.assertIn("layout", result["sources"][0]["detail"])

    def test_title_only_feed_reads_the_article_for_a_summary(self):
        self.set_feed(item_tuple(self.site, "/one", text=""))
        self.site.routes["/one"] = "<html><body><article><p>%s</p></article></body></html>" % LONG_TEXT
        scan(self.store, [self.source])
        self.assertEqual(self.site.hits("/one"), 1)
        self.assertIn("free tool", self.rows("findings")[0]["summary"])

    def test_one_scan_at_a_time(self):
        self.set_feed(item_tuple(self.site, "/one"))
        with _ScanLock(self.store.lock_path):
            with self.assertRaises(ScanInProgress):
                scan(self.store, [self.source])

    def test_a_crash_while_saving_leaves_nothing_half_saved(self):
        self.set_feed(item_tuple(self.site, "/one"))
        with mock.patch("franky.scanner._save_source_state", side_effect=sqlite3.OperationalError("simulated crash")):
            with self.assertRaises(sqlite3.OperationalError):
                scan(self.store, [self.source])
        self.assertEqual(self.rows("findings"), [])
        self.assertEqual(self.rows("seen_items"), [])
        self.assertEqual(self.rows("runs")[0]["status"], "failed")

    def test_interrupted_run_is_cleaned_up_by_the_next_scan(self):
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO runs (run_id, trigger, status, started_at, run_date) VALUES ('FF-old', 'x', 'running', 'x', 'x')")
        self.set_feed(item_tuple(self.site, "/one"))
        scan(self.store, [self.source])
        statuses = {row["run_id"]: row["status"] for row in self.rows("runs")}
        self.assertEqual(statuses["FF-old"], "interrupted")


if __name__ == "__main__":
    unittest.main()
