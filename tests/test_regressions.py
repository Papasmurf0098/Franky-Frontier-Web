"""Bugs found by the debug pass on the on-device version. Each test failed before its fix."""

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest import mock

from franky import __main__ as cli
from franky.fetcher import FetchError
from franky.parsing import FeedError, key_sentences, parse_date, parse_feed, parse_page, strip_tags
from franky.scanner import ConfigError, load_sources, load_tools, run_scan

from .helpers import FEED_HEADERS, FIXED_NOW, FIXED_TODAY, LONG_TEXT, FakeSite, quick_fetcher, rss, source_for, temp_store

REPO = Path(__file__).resolve().parent.parent
ROBOTS_OK = {"/robots.txt": (200, "User-agent: *\nAllow: /\n", {"Content-Type": "text/plain"})}
FRESH = "Tue, 03 Nov 2026 10:00:00 GMT"


def scan(store, sources, **kwargs):
    kwargs.setdefault("fetcher", quick_fetcher())
    kwargs.setdefault("now", FIXED_NOW)
    kwargs.setdefault("today", FIXED_TODAY)
    return run_scan(store, sources, **kwargs)


def article(title="Build a new agent app", description="This free guide shows how you can build an agent at home today."):
    return (
        '<html><head><meta property="og:title" content="%s"/><meta name="description" content="%s"/>'
        '<meta property="article:published_time" content="2026-11-03"/></head><body><article><p>%s</p></article></body></html>'
        % (title, description, description)
    )


class FetcherBugs(unittest.TestCase):
    def setUp(self):
        self.site = FakeSite(dict(ROBOTS_OK))
        self.addCleanup(self.site.close)

    def test_308_redirects_are_followed(self):
        # Python 3.9 does not follow 308, and Next.js sites use it for trailing slashes.
        # This was the real cause of the Zapier and Google article failures.
        self.site.routes["/old"] = (308, "", {"Location": "/new"})
        self.site.routes["/new"] = "arrived"
        self.assertEqual(quick_fetcher().get(self.site.base + "/old").text, "arrived")

    def test_308_off_domain_is_still_refused(self):
        self.site.routes["/old"] = (308, "", {"Location": "http://evil.example/x"})
        with self.assertRaises(FetchError):
            quick_fetcher().get(self.site.base + "/old", allowed_domain="127.0.0.1")

    def test_spaces_and_non_ascii_in_urls_are_encoded(self):
        self.site.routes["/a%20b"] = "spaced"
        self.site.routes["/caf%C3%A9"] = "accented"
        self.assertEqual(quick_fetcher().get(self.site.base + "/a b").text, "spaced")
        self.assertEqual(quick_fetcher().get(self.site.base + "/café").text, "accented")

    def test_malformed_url_is_a_fetch_error(self):
        with self.assertRaises(FetchError):
            quick_fetcher().get("http://[oops/x")

    def test_garbage_from_a_server_is_a_fetch_error(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        self.addCleanup(listener.close)

        def serve():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.recv(4096)
                conn.sendall(b"this is not http\r\n\r\n")
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        fetcher = quick_fetcher(respect_robots=False, attempts=1)
        with self.assertRaises(FetchError):
            fetcher.get("http://127.0.0.1:%d/x" % listener.getsockname()[1])

    def test_declared_xml_encoding_is_used_when_the_header_has_none(self):
        body = '<?xml version="1.0" encoding="windows-1252"?><rss><channel><title>It\x92s</title></channel></rss>'.encode("latin-1")
        self.site.routes["/feed"] = (200, body, {"Content-Type": "application/rss+xml"})
        self.assertIn("It’s", quick_fetcher().get(self.site.base + "/feed").text)


class ParsingBugs(unittest.TestCase):
    def feed(self, item):
        return '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>%s</channel></rss>' % item

    def test_doctype_inside_article_content_is_not_refused(self):
        entry = "<item><title>T</title><link>https://a.test/t</link><description><![CDATA[<!DOCTYPE html><p>Hello</p>]]></description></item>"
        self.assertEqual(len(parse_feed(self.feed(entry), "https://a.test/", 5)), 1)

    def test_article_about_xml_entities_is_not_refused(self):
        entry = "<item><title>XXE</title><link>https://a.test/t</link><description><![CDATA[Never allow <!ENTITY x>]]></description></item>"
        self.assertEqual(len(parse_feed(self.feed(entry), "https://a.test/", 5)), 1)

    def test_real_dtds_are_still_refused_however_they_are_hidden(self):
        padded = "<!-- <a> " + "x" * 5000 + ' --><!DOCTYPE r [<!ENTITY a "b">]><rss><channel/></rss>'
        for bomb in (padded, '<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://evil.example/x"><rss><channel/></rss>'):
            with self.assertRaises(FeedError):
                parse_feed(bomb, "https://a.test/", 5)

    def test_malformed_href_does_not_kill_the_page(self):
        info = parse_page('<a href="http://[oops">Bad</a><a href="/good">Good</a><p>text</p>', "https://a.test/")
        self.assertEqual([url for url, _ in info["links"]], ["https://a.test/good"])

    def test_malformed_feed_link_only_loses_that_entry(self):
        entries = (
            "<item><title>Bad</title><link>http://[oops</link></item>"
            "<item><title>Good</title><link>https://a.test/good</link></item>"
        )
        self.assertEqual([e["title"] for e in parse_feed(self.feed(entries), "https://a.test/", 5)], ["Good"])

    def test_subtitle_after_a_dash_is_kept(self):
        info = parse_page('<head><meta property="og:title" content="Claude 5 — a faster model for everyone"/></head>', "https://a.test/")
        self.assertEqual(info["title"], "Claude 5 — a faster model for everyone")
        info = parse_page("<body><h1>Granite 4 – update models like software</h1></body>", "https://a.test/")
        self.assertEqual(info["title"], "Granite 4 – update models like software")

    def test_site_name_is_still_trimmed_from_titles(self):
        self.assertEqual(parse_page("<head><title>My long post \\ Anthropic</title></head>", "https://a.test/")["title"], "My long post")
        self.assertEqual(parse_page("<head><title>My long post — Example Site</title></head>", "https://a.test/")["title"], "My long post")
        self.assertEqual(parse_page('<head><meta property="og:title" content="My long post | Example"/></head>', "https://a.test/")["title"], "My long post")

    def test_words_starting_like_months_are_not_dates(self):
        self.assertIsNone(parse_date("Marathon 12, 2026"))
        self.assertIsNone(parse_date("Decision 3, 2026"))
        self.assertEqual(parse_page("<body><p>Marathon 12, 2026 was great for everyone involved.</p></body>", "https://a.test/")["published"], "")

    def test_real_month_spellings_still_parse(self):
        for value in ("Sept 4, 2026", "September 4, 2026", "Sep. 4, 2026"):
            self.assertEqual(parse_date(value), "2026-09-04", value)

    def test_iso_variants_python_3_9_cannot_read(self):
        self.assertEqual(parse_date("2026-10-18T00:00:00+0000"), "2026-10-18")
        self.assertEqual(parse_date("2026-10-18T00:00:00.12Z"), "2026-10-18")

    def test_blank_description_falls_through_to_content(self):
        entry = "<item><title>T</title><link>https://a.test/t</link><description> </description><content:encoded>Real text</content:encoded></item>"
        self.assertEqual(parse_feed(self.feed(entry), "https://a.test/", 5)[0]["summary"], "Real text")

    def test_atom_xhtml_summary_is_read(self):
        feed = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link href="https://a.test/t"/>'
            '<summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Inner <b>text</b></div></summary></entry></feed>'
        )
        self.assertEqual(parse_feed(feed, "https://a.test/", 5)[0]["summary"], "Inner text")

    def test_rss_permalink_guid_is_used_when_there_is_no_link(self):
        entry = '<item><title>T</title><guid isPermaLink="true">https://a.test/p</guid></item>'
        self.assertEqual(parse_feed(self.feed(entry), "https://a.test/", 5)[0]["link"], "https://a.test/p")

    def test_guid_that_is_not_a_permalink_is_not_used_as_a_link(self):
        entry = '<item><title>T</title><guid isPermaLink="false">https://a.test/p</guid></item>'
        self.assertEqual(parse_feed(self.feed(entry), "https://a.test/", 5), [])

    def test_a_line_of_author_names_is_not_a_summary(self):
        # Found on a real Hugging Face post: long enough to pass, but not a sentence.
        text = "Gabriel Pimenta de Freitas Cardoso GabrielPimenta99 Follow\nThis tool reads any document and returns structured fields."
        self.assertEqual(key_sentences(text), ["This tool reads any document and returns structured fields."])

    def test_math_in_a_title_is_not_eaten_as_a_tag(self):
        self.assertEqual(strip_tags("Why 5 < 6 and 7 > 3"), "Why 5 < 6 and 7 > 3")
        self.assertEqual(strip_tags("Keep <b>this</b> text"), "Keep this text")


class ScanBugs(unittest.TestCase):
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

    def page_source(self):
        return source_for(self.site.base + "/news", kind="page", article_path="^/news/[^/]+$")

    def test_failed_article_is_retried_even_if_the_listing_says_unchanged(self):
        # An ETag saved after a partial failure made the next scan a 304, stranding the article.
        def listing(handler):
            if handler.headers.get("If-None-Match") == '"v1"':
                return 304, "", {}
            return 200, '<a href="/news/one">One</a><a href="/news/two">Two</a>', {"ETag": '"v1"'}

        self.site.routes["/news"] = listing
        self.site.routes["/news/one"] = (500, "down")
        self.site.routes["/news/two"] = article("Another new agent app")
        first = scan(self.store, [self.page_source()])
        self.assertEqual(first["findings"], 1)
        self.assertEqual(first["sources"][0]["status"], "ok")
        self.site.routes["/news/one"] = article()
        second = scan(self.store, [self.page_source()], now=FIXED_NOW + timedelta(hours=25))
        self.assertEqual(second["findings"], 1)

    def test_title_only_item_waits_for_a_temporary_article_failure(self):
        # It used to be stored for good with no summary and a lower score.
        self.set_feed(("Build a new agent app", self.site.base + "/one", FRESH, ""))
        self.site.routes["/one"] = (503, "busy")
        first = scan(self.store, [self.source])
        self.assertEqual(first["findings"] + first["watchlist"], 0)
        self.site.routes["/one"] = article()
        second = scan(self.store, [self.source], now=FIXED_NOW + timedelta(hours=25))
        self.assertEqual(second["findings"], 1)
        self.assertIn("free guide", self.rows("findings")[0]["summary"])

    def test_title_only_item_is_kept_when_the_article_refuses_for_good(self):
        self.set_feed(("Build a new agent app", self.site.base + "/one", FRESH, ""))
        self.site.routes["/one"] = (403, "no bots")
        result = scan(self.store, [self.source])
        self.assertEqual(result["findings"] + result["watchlist"], 1)

    def test_one_bad_link_does_not_fail_the_whole_source(self):
        self.set_feed(
            ("Odd link", self.site.base + "/has space", FRESH, LONG_TEXT),
            ("Bad link", "http://[oops", FRESH, LONG_TEXT),
            ("Build a new agent app", self.site.base + "/one", FRESH, LONG_TEXT),
        )
        result = scan(self.store, [self.source])
        self.assertEqual(result["sources"][0]["status"], "ok")
        self.assertEqual(result["findings"], 2)

    def test_two_scans_in_the_same_second_both_work(self):
        self.set_feed(("Build a new agent app", self.site.base + "/one", FRESH, LONG_TEXT))
        first = scan(self.store, [self.source], force=True)
        second = scan(self.store, [self.source], force=True)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertGreater(second["run_id"], first["run_id"])

    def test_run_ids_sort_by_real_time_across_time_zones(self):
        # Local-time IDs sort wrongly after a clock change or a flight west.
        self.set_feed(("Build a new agent app", self.site.base + "/one", FRESH, LONG_TEXT))
        original = os.environ.get("TZ")

        def restore():
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        first = scan(self.store, [self.source], force=True)
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        second = scan(self.store, [self.source], force=True, now=FIXED_NOW + timedelta(hours=1))
        self.assertGreater(second["run_id"], first["run_id"])

    def test_being_offline_does_not_pause_every_source(self):
        # Three hours without wifi used to pause all sources for a day after it came back.
        dead = [source_for("http://127.0.0.1:9/a", name="A"), source_for("http://127.0.0.1:9/b", name="B")]
        moment = FIXED_NOW
        for _ in range(4):
            result = scan(self.store, dead, now=moment)
            moment += timedelta(hours=2)
        self.assertEqual(result["status"], "offline")
        for source in dead:
            state = self.store.source_state(source["url"])
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertIsNone(state["circuit_open_until"])
            self.assertEqual(state["status"], "offline")

    def test_offline_sources_retry_within_the_hour(self):
        dead = source_for("http://127.0.0.1:9/a", cadence_hours=24)
        scan(self.store, [dead])
        again = scan(self.store, [dead], now=FIXED_NOW + timedelta(hours=2))
        self.assertEqual(again["sources"][0]["status"], "offline")

    def test_a_network_failure_counts_when_other_sources_are_reachable(self):
        self.set_feed(("Build a new agent app", self.site.base + "/one", FRESH, LONG_TEXT))
        dead = source_for("http://127.0.0.1:9/a", name="Dead")
        scan(self.store, [dead, self.source])
        state = self.store.source_state(dead["url"])
        self.assertEqual((state["status"], state["consecutive_failures"]), ("error", 1))

    def test_updated_watch_item_is_not_counted_as_new_and_keeps_its_date(self):
        title = "Acme appoints a new chief of staff"
        self.set_feed((title, self.site.base + "/one", FRESH, LONG_TEXT))
        scan(self.store, [self.source])
        self.set_feed((title, self.site.base + "/one", FRESH, LONG_TEXT + " Edited."))
        second = scan(self.store, [self.source], force=True, now=FIXED_NOW + timedelta(days=1), today=FIXED_TODAY + timedelta(days=1))
        self.assertEqual(second["watchlist"], 0)
        row = self.rows("watchlist")[0]
        self.assertEqual(row["date_added"], FIXED_TODAY.isoformat())
        self.assertEqual(row["last_reviewed"], (FIXED_TODAY + timedelta(days=1)).isoformat())

    def test_far_future_dates_are_not_treated_as_fresh(self):
        self.set_feed(("Build a new agent app", self.site.base + "/one", "Fri, 01 Jan 2100 10:00:00 GMT", LONG_TEXT))
        scan(self.store, [self.source])
        row = self.rows("findings")[0]
        self.assertEqual(row["date"], FIXED_TODAY.isoformat())
        self.assertIn("publish_date_unknown", row["notes"])

    def test_tomorrow_is_allowed_because_feeds_use_utc(self):
        self.set_feed(("Build a new agent app", self.site.base + "/one", "Thu, 05 Nov 2026 01:00:00 GMT", LONG_TEXT))
        scan(self.store, [self.source])
        self.assertEqual(self.rows("findings")[0]["date"], "2026-11-05")

    def test_scan_stops_hammering_a_site_whose_articles_all_fail(self):
        links = "".join('<a href="/news/%d">x</a>' % n for n in range(8))
        self.site.routes["/news"] = links
        result = scan(self.store, [self.page_source()], fetcher=quick_fetcher(attempts=1))
        hits = sum(self.site.hits("/news/%d" % n) for n in range(8))
        self.assertEqual(hits, 3)
        self.assertEqual(result["sources"][0]["status"], "error")

    def test_duplicate_within_one_source_is_not_reported_as_another_source(self):
        self.set_feed(
            ("Acme launches agent builder app", self.site.base + "/one", FRESH, LONG_TEXT),
            ("Acme launches agent builder app today", self.site.base + "/two", FRESH, LONG_TEXT),
        )
        scan(self.store, [self.source])
        self.assertNotIn("Also reported", self.rows("findings")[0]["notes"])

    def test_top_takeaway_is_the_highest_priority_not_the_highest_score(self):
        # Corporate news keeps its high score but is capped at Low, and a Medium can outscore
        # a High that has a sign of usability. Priority is what the reader was promised.
        self.set_feed(
            ("Thoughts on workflow automation for small business teams", self.site.base + "/medium", FRESH, "A long essay on business workflow automation for small business owners and creators, new this week."),
            ("Free guide you can use", self.site.base + "/high", FRESH, "Now available for everyone, a free guide for an agent."),
        )
        with mock.patch("franky.scanner.assess") as assess:
            def fake(item, source, prior, today):
                is_high = item["url"].endswith("/high")
                return {
                    "score": 4.3 if is_high else 4.9, "priority": "High" if is_high else "Medium",
                    "device_access": "Both", "technical_barrier": "Low-code", "on_device": False,
                    "at_home_use": "x", "repeat_marker": "", "frontier": False, "freshness_days": 1,
                    "stale": False, "flags": [], "breakdown": {},
                }
            assess.side_effect = fake
            result = scan(self.store, [self.source])
        self.assertIn("Free guide", result["top_takeaway"])

    def test_top_takeaway_is_the_newest_of_the_highest_priority(self):
        # The dashboard's lead story sorts this way, and the two shouldn't disagree.
        self.set_feed(
            ("Older but high scoring", self.site.base + "/old", "Mon, 02 Nov 2026 10:00:00 GMT", LONG_TEXT),
            ("Newer and lower scoring", self.site.base + "/new", "Tue, 03 Nov 2026 10:00:00 GMT", LONG_TEXT),
        )
        with mock.patch("franky.scanner.assess") as assess:
            def fake(item, source, prior, today):
                return {
                    "score": 4.9 if item["url"].endswith("/old") else 4.3, "priority": "High",
                    "device_access": "Both", "technical_barrier": "Low-code", "on_device": False, "usable": True,
                    "at_home_use": "x", "repeat_marker": "", "frontier": False, "freshness_days": 1,
                    "stale": False, "flags": [], "breakdown": {},
                }
            assess.side_effect = fake
            result = scan(self.store, [self.source])
        self.assertIn("Newer and lower scoring", result["top_takeaway"])

    def test_absurdly_long_titles_are_cut_down(self):
        self.set_feed(("Build " + "very " * 2000 + "long", self.site.base + "/one", FRESH, LONG_TEXT))
        scan(self.store, [self.source])
        self.assertLessEqual(len(self.rows("findings")[0]["headline"]), 241)

    def test_relative_links_resolve_against_where_the_feed_really_is(self):
        self.site.routes["/feed"] = (301, "", {"Location": "/sub/feed.xml"})
        self.site.routes["/sub/feed.xml"] = (200, rss(("Build a new agent app", "post", FRESH, LONG_TEXT)), FEED_HEADERS)
        scan(self.store, [self.source])
        self.assertEqual(self.rows("findings")[0]["source_url"], self.site.base + "/sub/post")


class SetupBugs(unittest.TestCase):
    def write(self, folder, text):
        path = Path(folder) / "config.json"
        path.write_text(text)
        return path

    def test_malformed_sources_are_a_config_error(self):
        good = {"name": "x", "company": "x", "url": "https://x.test", "kind": "feed", "category": "c", "trust_tier": "Tier 1"}
        cases = ['{"sources":["x"]}', json.dumps({"sources": [{**good, "cadence_hours": "soon"}]}), json.dumps({"sources": [{**good, "max_items": "many"}]}), json.dumps({"sources": [{**good, "url": "ftp://x.test"}]}), "[]"]
        with tempfile.TemporaryDirectory() as folder:
            for text in cases:
                with self.assertRaises(ConfigError, msg=text):
                    load_sources(self.write(folder, text))

    def test_malformed_tools_are_a_config_error(self):
        with tempfile.TemporaryDirectory() as folder:
            for text in ("not json", '{"tools":[{"tool_id":"x"}]}', '{"tools":"x"}'):
                with self.assertRaises(ConfigError, msg=text):
                    load_tools(self.write(folder, text))

    def test_cli_explains_a_broken_config_instead_of_a_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = self.write(folder, '{"sources":["x"]}')
            stderr = io.StringIO()
            with mock.patch.object(cli, "SOURCES_PATH", bad), redirect_stderr(stderr):
                self.assertEqual(cli.main(["run", "--state-dir", folder + "/state", "--out", folder + "/dist"]), 1)
            self.assertIn("config", stderr.getvalue().lower())




if __name__ == "__main__":
    unittest.main()
