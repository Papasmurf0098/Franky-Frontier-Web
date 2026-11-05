"""A fake website on localhost, so tests never touch the real internet."""

from __future__ import annotations

import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Tuple, Union

from franky.fetcher import Fetcher
from franky.store import Store

Response = Union[str, Tuple[int, str], Tuple[int, str, Dict[str, str]], Callable[[BaseHTTPRequestHandler], Tuple[int, str, Dict[str, str]]]]


class FakeSite:
    """Serve a dict of path -> response. Records every request as (path, headers)."""

    def __init__(self, routes: Dict[str, Response]):
        self.routes = routes
        self.requests: List[Tuple[str, Dict[str, str]]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.requests.append((self.path, dict(self.headers)))
                route = site.routes.get(self.path)
                if route is None:
                    code, body, headers = 404, "not found", {}
                elif callable(route):
                    code, body, headers = route(self)
                elif isinstance(route, str):
                    code, body, headers = 200, route, {}
                elif len(route) == 2:
                    code, body, headers = route[0], route[1], {}
                else:
                    code, body, headers = route
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                headers = {"Content-Type": "text/html; charset=utf-8", **headers}
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def hits(self, path: str) -> int:
        return sum(1 for seen, _ in self.requests if seen == path)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def temp_store(test) -> Store:
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    return Store(Path(folder.name))


def quick_fetcher(**overrides) -> Fetcher:
    """No real waiting: backoff and throttle sleeps are recorded instead of slept."""
    sleeps: List[float] = []
    settings = dict(per_host_delay=0, timeout=5, sleep=sleeps.append)
    settings.update(overrides)
    fetcher = Fetcher(**settings)
    fetcher.recorded_sleeps = sleeps
    return fetcher


def source_for(url: str, kind: str = "feed", **overrides) -> Dict:
    source = {
        "name": "Test Source",
        "company": "TestCo",
        "url": url,
        "kind": kind,
        "enabled": True,
        "trust_tier": "Tier 1",
        "category": "Model / Tool Launches",
        "cadence_hours": 24,
        "max_items": 8,
        "device_access": "Both",
        "technical_barrier": "Low-code",
        "positive_keywords": ["agent"],
        "negative_keywords": ["careers"],
    }
    source.update(overrides)
    return source


FIXED_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)
FIXED_TODAY = FIXED_NOW.date()


def rss(*items: Tuple[str, str, str, str]) -> str:
    """Build a feed from (title, link, date, description) tuples."""
    body = "".join(
        "<item><title>%s</title><link>%s</link><pubDate>%s</pubDate><description>%s</description></item>"
        % item
        for item in items
    )
    return '<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>%s</channel></rss>' % body


FEED_HEADERS = {"Content-Type": "application/rss+xml; charset=utf-8"}
LONG_TEXT = "Now available for everyone, this free tool lets you build an agent workflow at home in minutes."
