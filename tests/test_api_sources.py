"""No-account API sources. All responses here are hand-written, so no test touches the internet."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from franky import adapters
from franky.parsing import external_url
from franky.scanner import ConfigError, expand_url, load_sources, run_scan
from franky.scoring import assess

from .helpers import FEED_HEADERS, FIXED_NOW, FIXED_TODAY, LONG_TEXT, FakeSite, quick_fetcher, rss, source_for, temp_store

REPO = Path(__file__).resolve().parent.parent
ROBOTS_OK = {"/robots.txt": (200, "User-agent: *\nAllow: /\n", {"Content-Type": "text/plain"})}
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
FRESH = "2026-11-03T10:00:00.000Z"


def hn_hit(**overrides):
    hit = {
        "objectID": "123",
        "title": "Show HN: A free local agent app",
        "url": "https://example.test/tool",
        "created_at": FRESH,
        "points": 120,
        "num_comments": 40,
        "story_text": None,
    }
    hit.update(overrides)
    return hit


def api_source(url, **overrides):
    settings = dict(kind="api", adapter="hacker_news", trust_tier="Tier 3", company="Hacker News")
    settings.update(overrides)
    return source_for(url, **settings)


def scan(store, sources, **kwargs):
    kwargs.setdefault("fetcher", quick_fetcher())
    kwargs.setdefault("now", FIXED_NOW)
    kwargs.setdefault("today", FIXED_TODAY)
    return run_scan(store, sources, **kwargs)


class HackerNews(unittest.TestCase):
    def test_maps_a_story(self):
        entry = adapters.hacker_news({"hits": [hn_hit()]})[0]
        self.assertEqual(entry["title"], "Show HN: A free local agent app")
        self.assertEqual(entry["link"], "https://example.test/tool")
        self.assertEqual(entry["published"], "2026-11-03")
        self.assertEqual(entry["signal"], "120 points and 40 comments on Hacker News")
        self.assertEqual(entry["strength"], 1)

    def test_post_with_no_outside_link_uses_the_discussion(self):
        entry = adapters.hacker_news({"hits": [hn_hit(url=None)]})[0]
        self.assertEqual(entry["link"], "https://news.ycombinator.com/item?id=123")

    def test_story_text_is_cleaned(self):
        text = 'Hi HN, <a href="https://x.test/">https://x.test/</a> we built this.<p>Try https://y.test/a it.'
        summary = adapters.hacker_news({"hits": [hn_hit(story_text=text)]})[0]["summary"]
        self.assertEqual(summary, "Hi HN, we built this. Try it.")

    def test_strength_follows_points(self):
        strengths = [adapters.hacker_news({"hits": [hn_hit(points=p)]})[0]["strength"] for p in (50, 100, 300)]
        self.assertEqual(strengths, [0, 1, 2])

    def test_wrong_field_types_cost_one_entry_at_most(self):
        hits = [hn_hit(title=None), hn_hit(objectID=None), "junk", hn_hit(points="lots", num_comments=True, created_at=7)]
        entries = adapters.hacker_news({"hits": hits})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["signal"], "0 points and 0 comments on Hacker News")
        self.assertEqual(entries[0]["published"], "")

    def test_changed_shape_is_an_error(self):
        for data in ({}, [], {"hits": "nope"}, None):
            with self.assertRaises(adapters.AdapterError):
                adapters.hacker_news(data)


class GitHub(unittest.TestCase):
    def repo(self, **overrides):
        repo = {"full_name": "a/tool", "html_url": "https://github.com/a/tool", "description": "<b>Local</b> agent", "stargazers_count": 2500, "language": "Python", "created_at": FRESH, "fork": False, "archived": False}
        repo.update(overrides)
        return repo

    def test_maps_a_repository(self):
        entry = adapters.github_repositories({"items": [self.repo()]})[0]
        self.assertEqual((entry["title"], entry["summary"], entry["strength"]), ("a/tool", "Local agent", 2))
        self.assertEqual(entry["signal"], "2,500 stars on GitHub, written in Python")

    def test_skips_forks_and_archives(self):
        self.assertEqual(adapters.github_repositories({"items": [self.repo(fork=True), self.repo(archived=True)]}), [])


class HuggingFace(unittest.TestCase):
    def test_maps_a_model(self):
        model = {"id": "a/model-GGUF", "likes": 400, "downloads": 12000, "createdAt": FRESH, "tags": ["gguf", "license:mit"]}
        entry = adapters.hugging_face_models([model])[0]
        self.assertEqual(entry["link"], "https://huggingface.co/a/model-GGUF")
        self.assertEqual(entry["summary"], "Trending GGUF model on Hugging Face, license mit.")
        self.assertEqual(entry["signal"], "400 likes and 12,000 downloads on Hugging Face")
        self.assertEqual(entry["strength"], 2)

    def test_tolerates_odd_fields(self):
        entries = adapters.hugging_face_models([{"id": "noslash"}, {"id": "a/b", "tags": "gguf", "likes": None}])
        self.assertEqual([entry["title"] for entry in entries], ["a/b"])


class OpenRouter(unittest.TestCase):
    def model(self, **overrides):
        model = {"id": "a/m:free", "name": "A: M (free)", "created": 1793000000, "description": "A model.", "pricing": {"prompt": "0", "completion": "0"}}
        model.update(overrides)
        return model

    def test_keeps_only_free_models(self):
        paid = self.model(pricing={"prompt": "0.0001", "completion": "0"})
        # -1 is OpenRouter's auto-router meaning "variable", which must not read as free.
        variable = self.model(pricing={"prompt": "-1", "completion": "-1"})
        broken = self.model(pricing="free")
        entries = adapters.openrouter_free_models({"data": [paid, variable, broken, self.model()]})
        self.assertEqual([entry["title"] for entry in entries], ["A: M is free on OpenRouter"])

    def test_says_an_account_is_needed_to_call_it(self):
        entry = adapters.openrouter_free_models({"data": [self.model()]})[0]
        self.assertIn("needs an OpenRouter account", entry["signal"])

    def test_newest_first_and_bad_dates_survive(self):
        entries = adapters.openrouter_free_models({"data": [self.model(created=10 ** 30), self.model(id="b/m", created=1793000000)]})
        self.assertEqual(entries[0]["link"], "https://openrouter.ai/b/m")
        self.assertEqual(entries[1]["published"], "")


class Dev(unittest.TestCase):
    def test_maps_an_article(self):
        article = {"title": "How to run a local agent", "url": "https://dev.to/a/b", "description": "A guide.", "published_timestamp": FRESH, "public_reactions_count": 60, "comments_count": 1}
        entry = adapters.dev_articles([article])[0]
        self.assertEqual(entry["signal"], "60 reactions and 1 comment on DEV")
        self.assertEqual(entry["strength"], 1)


class Links(unittest.TestCase):
    def test_query_is_kept_but_tracking_is_removed(self):
        self.assertEqual(
            external_url("https://YouTube.com/watch?v=abc&utm_source=x&fbclid=y#t=3"),
            "https://youtube.com/watch?v=abc",
        )

    def test_only_web_addresses_are_allowed(self):
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x.test/a", "//x.test/a", "not a url"):
            with self.assertRaises(ValueError, msg=bad):
                external_url(bad)

    def test_rolling_dates_fill_in(self):
        now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        url = expand_url("https://x.test/a?since={epoch_days_ago:7}&from={date_days_ago:30}&q=keep", now)
        self.assertEqual(url, "https://x.test/a?since=%d&from=2026-10-05&q=keep" % int((now - timedelta(days=7)).timestamp()))


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.store = temp_store(self)
        self.site = FakeSite(dict(ROBOTS_OK))
        self.addCleanup(self.site.close)
        self.source = api_source(self.site.base + "/hn")

    def respond(self, *hits, path="/hn"):
        self.site.routes[path] = (200, json.dumps({"hits": list(hits)}), JSON_HEADERS)

    def rows(self, table):
        with self.store.reader() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM %s" % table)]

    def test_stores_a_finding_and_never_fetches_its_link(self):
        outside = FakeSite(dict(ROBOTS_OK))
        self.addCleanup(outside.close)
        self.respond(hn_hit(url=outside.base + "/page?id=7&utm_source=hn", story_text="We built this free app for anyone to try."))
        result = scan(self.store, [self.source])
        self.assertEqual(result["sources"][0]["status"], "ok")
        self.assertEqual(result["findings"], 1)
        row = self.rows("findings")[0]
        self.assertEqual(row["source_url"], outside.base + "/page?id=7")
        self.assertIn("120 points and 40 comments", row["notes"])
        self.assertEqual(outside.requests, [])

    def test_links_that_arent_web_addresses_are_dropped(self):
        self.respond(hn_hit(url="javascript:alert(1)", objectID="1"), hn_hit(objectID="2", url="https://example.test/ok"))
        scan(self.store, [self.source])
        self.assertEqual([row["source_url"] for row in self.rows("findings")], ["https://example.test/ok"])

    def test_rescan_with_new_upvotes_is_not_an_update(self):
        self.respond(hn_hit(points=120))
        scan(self.store, [self.source])
        self.respond(hn_hit(points=999, num_comments=500))
        second = scan(self.store, [self.source], force=True, now=FIXED_NOW + timedelta(minutes=5))
        self.assertEqual((second["findings"], second["updated"]), (0, 0))

    def test_changed_title_is_an_update(self):
        self.respond(hn_hit())
        scan(self.store, [self.source])
        self.respond(hn_hit(title="Show HN: A free local agent app, now with more"))
        second = scan(self.store, [self.source], force=True, now=FIXED_NOW + timedelta(minutes=5))
        self.assertEqual(second["updated"], 1)

    def test_community_repost_doesnt_replace_the_official_finding(self):
        article = self.site.base + "/news/post"
        self.site.routes["/feed"] = (200, rss(("Official new agent app", article, "Tue, 03 Nov 2026 10:00:00 GMT", LONG_TEXT)), FEED_HEADERS)
        official = source_for(self.site.base + "/feed", name="Official", trust_tier="Tier 1")
        scan(self.store, [official])
        self.respond(hn_hit(url=article, title="Show HN: this looks completely different"))
        scan(self.store, [self.source], force=True, now=FIXED_NOW + timedelta(minutes=5))
        rows = self.rows("findings")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source_name"], rows[0]["source_tier"]), ("Official", "Tier 1"))

    def test_same_page_from_two_sources_in_one_scan_is_one_finding(self):
        link = "https://example.test/same"
        self.respond(hn_hit(url=link, title="Show HN: one free app"), path="/a")
        self.respond(hn_hit(url=link, title="Show HN: wildly different words entirely"), path="/b")
        first = api_source(self.site.base + "/a", name="A")
        second = api_source(self.site.base + "/b", name="B")
        result = scan(self.store, [first, second])
        self.assertEqual((result["findings"], result["merged"]), (1, 1))

    def test_no_results_is_fine_and_doesnt_count_as_failing(self):
        self.respond()
        result = scan(self.store, [self.source])
        self.assertEqual(result["sources"][0]["status"], "ok")
        self.assertEqual(self.store.source_state(self.source["url"])["consecutive_failures"], 0)

    def test_a_changed_api_is_reported_and_the_others_carry_on(self):
        self.respond(hn_hit())
        self.site.routes["/moved"] = (200, json.dumps({"results": []}), JSON_HEADERS)
        self.site.routes["/broken"] = (200, "<html>maintenance</html>", {})
        moved = api_source(self.site.base + "/moved", name="Moved")
        broken = api_source(self.site.base + "/broken", name="Broken")
        result = scan(self.store, [moved, broken, self.source])
        self.assertEqual(result["status"], "completed_with_errors")
        states = {source["name"]: source for source in result["sources"]}
        self.assertIn("expected a list", states["Moved"]["detail"])
        self.assertIn("not valid JSON", states["Broken"]["detail"])
        self.assertEqual(result["findings"], 1)

    def test_being_rate_limited_is_visible_and_retried(self):
        self.site.routes["/hn"] = (429, "slow down", {"Retry-After": "1"})
        fetcher = quick_fetcher()
        result = scan(self.store, [self.source], fetcher=fetcher)
        self.assertEqual(result["sources"][0]["detail"], "HTTP 429")
        self.assertEqual(self.site.hits("/hn"), 3)
        self.assertEqual(fetcher.recorded_sleeps, [1.0, 1.0])

    def test_keyword_rules_filter_before_the_item_limit(self):
        hits = [hn_hit(objectID="1", title="Show HN: a casino app", url="https://x.test/1"), hn_hit(objectID="2", title="Show HN: unrelated thing", url="https://x.test/2"), hn_hit(objectID="3", title="Show HN: a free agent app", url="https://x.test/3")]
        self.respond(*hits)
        source = api_source(self.site.base + "/hn", max_items=1, require_keywords=["app"], exclude_keywords=["casino"])
        scan(self.store, [source])
        self.assertEqual([row["headline"] for row in self.rows("findings")], ["Show HN: a free agent app"])

    def test_title_pattern_excludes_release_candidates_from_a_feed(self):
        self.site.routes["/feed"] = (200, rss(("v1.2.0-rc1", self.site.base + "/r1", "Tue, 03 Nov 2026 10:00:00 GMT", LONG_TEXT), ("v1.2.0", self.site.base + "/r2", "Tue, 03 Nov 2026 10:00:00 GMT", "x")), FEED_HEADERS)
        source = source_for(self.site.base + "/feed", title_prefix="Ollama ", read_articles=False, exclude_title_pattern="-rc\\d*$")
        scan(self.store, [source])
        rows = self.rows("findings") + self.rows("watchlist")
        titles = {row.get("headline") or row.get("item_trend") for row in rows}
        self.assertEqual(titles, {"Ollama v1.2.0"})
        self.assertEqual(self.site.hits("/r2"), 0)

    def test_chatter_is_set_aside_and_not_scored_twice(self):
        self.respond(hn_hit(title="Thoughts on where the industry is going", objectID="9", url="https://x.test/9"))
        source = api_source(self.site.base + "/hn", require_usable=True)
        first = scan(self.store, [source])
        self.assertEqual((first["findings"], first["watchlist"], first["set_aside"]), (0, 0, 1))
        self.assertIn("1 set aside", first["summary"])
        self.assertEqual(self.rows("seen_items")[0]["outcome"], "set_aside")
        again = scan(self.store, [source], force=True, now=FIXED_NOW + timedelta(minutes=5))
        self.assertEqual(again["set_aside"], 0)

    def test_summary_falls_back_to_the_signal_not_a_false_claim(self):
        # We never read the linked page, so "no summary on the source page" would be untrue.
        self.respond(hn_hit())
        scan(self.store, [self.source])
        row = self.rows("findings")[0]
        self.assertEqual(row["summary"], "120 points and 40 comments on Hacker News.")
        self.assertNotIn("120 points", row["notes"] or "")

    def test_caution_reaches_the_card(self):
        self.respond(hn_hit(story_text="We built this free app for everyone to try at home today, and it is open source."))
        scan(self.store, [api_source(self.site.base + "/hn", caution="Check it before you install anything")])
        self.assertIn("Check it before you install anything", self.rows("findings")[0]["notes"])

    def test_rolling_url_is_fetched_but_state_stays_under_one_key(self):
        template = self.site.base + "/hn?since={date_days_ago:7}"
        self.site.routes["/hn?since=2026-10-28"] = (200, json.dumps({"hits": [hn_hit()]}), JSON_HEADERS)
        result = scan(self.store, [api_source(template)])
        self.assertEqual(result["findings"], 1)
        self.assertEqual(self.store.source_state(template)["status"], "ok")


class Scoring(unittest.TestCase):
    def item(self, **overrides):
        item = {"title": "Show HN: a free local agent app", "description": LONG_TEXT, "text": "", "url": "https://x.test/a", "published": "2026-11-03", "published_source": "api", "status": "new", "strength": 0}
        item.update(overrides)
        return item

    def test_community_item_needs_strong_attention_to_be_high(self):
        source = source_for("https://x.test/", trust_tier="Tier 3")
        self.assertEqual(assess(self.item(strength=1), source, [], FIXED_TODAY)["priority"], "Medium")
        self.assertEqual(assess(self.item(strength=2), source, [], FIXED_TODAY)["priority"], "High")

    def test_attention_raises_the_score(self):
        source = source_for("https://x.test/", trust_tier="Tier 3")
        scores = [assess(self.item(strength=s), source, [], FIXED_TODAY)["score"] for s in (0, 1, 2)]
        self.assertEqual(scores, sorted(scores))
        self.assertLess(scores[0], scores[2])

    def test_hosted_only_models_arent_called_local(self):
        item = self.item(description="An open-weight model you can use for free.")
        self.assertTrue(assess(item, source_for("https://x.test/"), [], FIXED_TODAY)["on_device"])
        result = assess(item, source_for("https://x.test/", hosted_only=True), [], FIXED_TODAY)
        self.assertFalse(result["on_device"])
        self.assertNotIn("locally", result["at_home_use"])


class Config(unittest.TestCase):
    def check_rejected(self, **overrides):
        source = {"name": "x", "company": "x", "url": "https://x.test/a", "kind": "api", "adapter": "hacker_news", "category": "c", "trust_tier": "Tier 3"}
        source.update(overrides)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sources.json"
            path.write_text(json.dumps({"sources": [source]}))
            with self.assertRaises(ConfigError):
                load_sources(path)

    def test_unknown_adapter(self):
        self.check_rejected(adapter="nope")

    def test_credentials_are_refused(self):
        # A key in this file would be public, and the point is that none are needed.
        self.check_rejected(url="https://x.test/a?api_key=abc")
        self.check_rejected(url="https://x.test/a?token=abc")
        self.check_rejected(headers={"Authorization": "Bearer x"})

    def test_bad_filters(self):
        self.check_rejected(require_keywords="ai")
        self.check_rejected(exclude_title_pattern="(")

    def test_shipped_sources_use_no_credentials_and_only_known_adapters(self):
        sources = load_sources(REPO / "config" / "sources.json")
        apis = [source for source in sources if source["kind"] == "api"]
        self.assertGreaterEqual(len(apis), 5)
        self.assertEqual({source["adapter"] for source in apis}, set(adapters.ADAPTERS))


if __name__ == "__main__":
    unittest.main()
