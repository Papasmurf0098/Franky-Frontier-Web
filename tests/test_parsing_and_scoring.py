import json
import unittest
from pathlib import Path

from franky.parsing import FeedError, canonical_url, parse_date, parse_feed, parse_page
from franky.scanner import ConfigError, load_sources
from franky.scoring import assess, has_term, priority_from_score, repeat_marker, similarity

from .helpers import FIXED_TODAY, LONG_TEXT, rss, source_for

REPO = Path(__file__).resolve().parent.parent


class FeedParsing(unittest.TestCase):
    def test_rss_newest_first_and_limited(self):
        feed = rss(
            ("Older", "https://a.test/old", "Mon, 02 Nov 2026 10:00:00 GMT", "x"),
            ("Newer", "https://a.test/new", "Wed, 04 Nov 2026 10:00:00 GMT", "y"),
            ("Oldest", "https://a.test/oldest", "Sun, 01 Nov 2026 10:00:00 GMT", "z"),
        )
        entries = parse_feed(feed, "https://a.test/feed", max_items=2)
        self.assertEqual([e["title"] for e in entries], ["Newer", "Older"])
        self.assertEqual(entries[0]["published"], "2026-11-04")

    def test_atom(self):
        feed = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>F</title><entry><id>tag:1</id>'
            '<title>Atom item</title><link rel="alternate" href="/post"/>'
            "<published>2026-11-01T08:00:00Z</published><summary>Hello</summary></entry></feed>"
        )
        entry = parse_feed(feed, "https://a.test/feed", 5)[0]
        self.assertEqual(entry["link"], "https://a.test/post")
        self.assertEqual(entry["published"], "2026-11-01")
        self.assertEqual(entry["summary"], "Hello")

    def test_image_caption_is_not_the_summary(self):
        # Regression: Google's feed puts an image caption in media:description first.
        feed = (
            '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><item>'
            "<title>T</title><link>https://a.test/t</link>"
            "<media:description>Animated carousel of images</media:description>"
            "<description>&lt;img src=\"x.png\"&gt;The real summary</description>"
            "</item></channel></rss>"
        )
        self.assertEqual(parse_feed(feed, "https://a.test/", 5)[0]["summary"], "The real summary")

    def test_entity_bomb_is_refused(self):
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY a "&lol;&lol;">]>'
            "<rss><channel><item><title>&a;</title></item></channel></rss>"
        )
        with self.assertRaises(FeedError):
            parse_feed(bomb, "https://a.test/", 5)

    def test_broken_xml_is_an_error_not_a_crash(self):
        with self.assertRaises(FeedError):
            parse_feed("<rss><channel><item>", "https://a.test/", 5)


class PageParsing(unittest.TestCase):
    PAGE = """<html><head><title>My Post \\ Example</title>
      <meta property="article:published_time" content="2026-10-18T00:00:00.000Z"/>
      <meta name="description" content="A useful summary."/></head>
      <body><nav><a href="/nav-link">Menu</a></nav>
      <article><h1>My Post</h1><p>First real sentence that is long enough to keep around here.</p>
      <a href="/news/story?utm=1#top">Story</a><a href="javascript:alert(1)">Bad</a></article>
      <script>var hidden = "do not read me";</script><footer>Footer text</footer></body></html>"""

    def test_fields(self):
        info = parse_page(self.PAGE, "https://a.test/news")
        self.assertEqual(info["title"], "My Post")
        self.assertEqual(info["published"], "2026-10-18")
        self.assertEqual(info["description"], "A useful summary.")
        self.assertIn("First real sentence", info["text"])
        self.assertNotIn("do not read me", info["text"])
        self.assertNotIn("Footer", info["text"])

    def test_links_are_clean_and_exclude_chrome_and_scripts(self):
        urls = [url for url, _ in parse_page(self.PAGE, "https://a.test/news")["links"]]
        self.assertEqual(urls, ["https://a.test/news/story"])

    def test_date_from_time_tag(self):
        info = parse_page('<html><body><time>Oct 28, 2026</time><p>x</p></body></html>', "https://a.test/")
        self.assertEqual((info["published"], info["published_source"]), ("2026-10-28", "time"))

    def test_no_date_stays_unknown(self):
        info = parse_page("<html><body><h1>Title here</h1></body></html>", "https://a.test/")
        self.assertEqual((info["published"], info["published_source"]), ("", "unknown"))

    def test_trailing_slash_survives_canonicalizing(self):
        # Regression: stripping it made every blog.google and zapier.com article fail.
        self.assertEqual(canonical_url("https://Zapier.com/blog/post/?x=1#y"), "https://zapier.com/blog/post/")

    def test_parse_date_rejects_junk(self):
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date(""))
        self.assertEqual(parse_date("Wed, 04 Nov 2026 17:50:10 GMT"), "2026-11-04")


class Scoring(unittest.TestCase):
    def item(self, **overrides):
        item = {
            "title": "New agent app",
            "description": LONG_TEXT,
            "text": "",
            "url": "https://a.test/x",
            "published": "2026-11-03",
            "published_source": "feed:entry",
            "status": "new",
        }
        item.update(overrides)
        return item

    def test_terms_match_whole_words_only(self):
        # Regression: the original matched "new" in "news" and "api" in "rapid".
        self.assertFalse(has_term("the latest news", "new"))
        self.assertFalse(has_term("rapid growth", "api"))
        self.assertTrue(has_term("a new api.", "api"))

    def test_useful_item_is_high_with_a_real_use(self):
        result = assess(self.item(), source_for("https://a.test/feed"), [], FIXED_TODAY)
        self.assertEqual(result["priority"], "High")
        self.assertIn("automation or agent", result["at_home_use"])

    def test_corporate_news_cannot_rise_above_low(self):
        item = self.item(title="OpenAI appoints a new Chief Corporate Affairs Officer", description="Now available free.")
        result = assess(item, source_for("https://a.test/feed"), [], FIXED_TODAY)
        self.assertEqual(result["priority"], "Low")
        self.assertIn("corporate_news", result["flags"])

    def test_nothing_is_high_without_a_sign_you_can_use_it(self):
        item = self.item(title="Thoughts on models", description="A workflow essay for small business owners.")
        result = assess(item, source_for("https://a.test/feed"), [], FIXED_TODAY)
        self.assertNotEqual(result["priority"], "High")

    def test_speculative_item_is_flagged_frontier(self):
        item = self.item(title="A new agent tool", description="Join the waitlist, coming soon.")
        result = assess(item, source_for("https://a.test/feed"), [], FIXED_TODAY)
        self.assertTrue(result["frontier"])

    def test_on_device_is_detected_and_steers_the_use(self):
        item = self.item(text="This model runs locally with MLX on a Mac laptop.")
        result = assess(item, source_for("https://a.test/feed"), [], FIXED_TODAY)
        self.assertTrue(result["on_device"])
        self.assertEqual(result["device_access"], "Laptop")
        self.assertIn("locally", result["at_home_use"])

    def test_unknown_date_is_not_treated_as_fresh(self):
        known = assess(self.item(), source_for("https://a.test/feed"), [], FIXED_TODAY)
        unknown = assess(self.item(published="", published_source="unknown"), source_for("https://a.test/feed"), [], FIXED_TODAY)
        self.assertLess(unknown["score"], known["score"])
        self.assertIn("publish_date_unknown", unknown["flags"])

    def test_stale_marked(self):
        result = assess(self.item(published="2026-07-01"), source_for("https://a.test/feed"), [], FIXED_TODAY)
        self.assertTrue(result["stale"])

    def test_scoring_is_deterministic(self):
        source = source_for("https://a.test/feed")
        self.assertEqual(assess(self.item(), source, [], FIXED_TODAY), assess(self.item(), source, [], FIXED_TODAY))

    def test_repeat_marker_counts_up_and_stops(self):
        prior = ["Kling video model launches widely"] * 5
        self.assertEqual(repeat_marker("Totally different thing", prior), "")
        self.assertEqual(repeat_marker("Kling video model launches widely", prior[:1]), "R2")
        self.assertEqual(repeat_marker("Kling video model launches widely", prior), "R4")

    def test_similarity_bounds(self):
        self.assertEqual(similarity("", "abc"), 0.0)
        self.assertEqual(similarity("Kling video", "video Kling"), 1.0)

    def test_thresholds(self):
        self.assertEqual(priority_from_score(4.2), "High")
        self.assertEqual(priority_from_score(3.4), "Medium")
        self.assertEqual(priority_from_score(3.39), "Low")


class Config(unittest.TestCase):
    def test_shipped_sources_are_valid_and_https(self):
        sources = load_sources(REPO / "config" / "sources.json")
        self.assertGreaterEqual(len(sources), 6)
        for source in sources:
            self.assertTrue(source["url"].startswith("https://"), source["url"])

    def test_bad_config_is_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sources.json"
            path.write_text(json.dumps({"sources": [{"name": "x", "company": "x", "url": "https://x.test", "kind": "page", "category": "c", "trust_tier": "Tier 1"}]}))
            with self.assertRaises(ConfigError):
                load_sources(path)


if __name__ == "__main__":
    unittest.main()
