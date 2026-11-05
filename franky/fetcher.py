"""Polite, bounded HTTP fetching for the scanner.

Integration rules applied here:
- retry only what can recover (network errors, 429, 5xx), with exponential backoff and jitter
- honor Retry-After, capped so one host can't stall a scan
- send conditional requests so unchanged feeds cost almost nothing
- obey robots.txt
- never follow a link or redirect off the source's own domain
- every failure surfaces as a FetchError, so one bad URL or response never ends a whole source
"""

from __future__ import annotations

import http.client
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from .parsing import URL_PATH_SAFE

USER_AGENT = "FrankyFrontier/0.1 (personal news scanner)"
ROBOTS_AGENT = "FrankyFrontier"
TEXT_TYPES = ("text/", "application/xml", "application/rss", "application/atom", "application/xhtml", "application/json")
URL_QUERY_SAFE = URL_PATH_SAFE + "?"
DECLARED_CHARSET = re.compile(rb"""(?:encoding|charset)\s*=\s*["']?([A-Za-z0-9_.:-]+)""")
# Network errors mean "couldn't reach it". The server never answered, so it may be us.
NETWORK_ERRORS = (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError)


class FetchError(Exception):
    def __init__(self, reason: str, status: Optional[int] = None, retryable: bool = False, network: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.retryable = retryable
        # True when no server answered at all, which is what being offline looks like.
        self.network = network


class RobotsBlocked(FetchError):
    """robots.txt asks us not to fetch this URL."""


class OffDomain(FetchError):
    """A link or redirect tried to leave the source's domain."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str = ""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False
    attempts: int = 1
    headers: Dict[str, str] = field(default_factory=dict)


def host_in_domain(host: str, domain: str) -> bool:
    host = (host or "").lower().rstrip(".")
    domain = (domain or "").lower().rstrip(".")
    return bool(host) and bool(domain) and (host == domain or host.endswith("." + domain))


def site_domain(url: str) -> str:
    """The domain a source may stay inside: its host without a leading www."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _check_url(url: str, allowed_domain: Optional[str]) -> None:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError as exc:
        raise FetchError("malformed URL: %s" % url) from exc
    if parts.scheme not in ("http", "https"):
        raise OffDomain("refusing non-web URL: %s" % url)
    if allowed_domain and not host_in_domain(host, allowed_domain):
        raise OffDomain("refusing off-domain URL: %s" % url)


def _encode_url(url: str) -> str:
    """Percent-encode spaces and non-ASCII, which real feeds contain and http.client refuses."""
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, quote(parts.path, safe=URL_PATH_SAFE), quote(parts.query, safe=URL_QUERY_SAFE), "")
    )


class _SameDomainRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_domain: Optional[str]):
        self.allowed_domain = allowed_domain

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl, self.allowed_domain)
        # Python 3.9's base class refuses 308, but for a GET it means the same as 307.
        return super().redirect_request(req, fp, min(code, 307), msg, headers, newurl)

    # Python 3.9 has no handler for 308, which Next.js sites use for trailing slashes.
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


class Fetcher:
    def __init__(
        self,
        timeout: float = 20.0,
        max_bytes: int = 5_000_000,
        attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        per_host_delay: float = 0.5,
        respect_robots: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.per_host_delay = per_host_delay
        self.respect_robots = respect_robots
        self.sleep = sleep
        self._robots: Dict[str, urllib.robotparser.RobotFileParser] = {}
        self._robots_lock = threading.Lock()
        self._last_hit: Dict[str, float] = {}
        self._hit_lock = threading.Lock()

    def _throttle(self, host: str) -> None:
        if self.per_host_delay <= 0:
            return
        with self._hit_lock:
            now = time.monotonic()
            ready_at = self._last_hit.get(host, 0.0) + self.per_host_delay
            wait = max(0.0, ready_at - now)
            self._last_hit[host] = now + wait
        if wait:
            self.sleep(wait)

    def _open(self, url: str, allowed_domain: Optional[str], headers: Dict[str, str]):
        opener = urllib.request.build_opener(_SameDomainRedirect(allowed_domain))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        return opener.open(request, timeout=self.timeout)

    def _robots_for(self, url: str, allowed_domain: Optional[str]) -> urllib.robotparser.RobotFileParser:
        parts = urlsplit(url)
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        with self._robots_lock:
            cached = self._robots.get(origin)
        if cached is not None:
            return cached
        parser = urllib.robotparser.RobotFileParser()
        try:
            with self._open(origin + "/robots.txt", allowed_domain, {}) as response:
                body = response.read(500_000).decode("utf-8", "replace")
            parser.parse(body.splitlines())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                parser.disallow_all = True
            elif exc.code >= 500:
                raise FetchError("robots.txt returned %d" % exc.code, exc.code, retryable=True) from exc
            else:
                parser.allow_all = True
        except OffDomain:
            # robots.txt redirected away from the site, so the site publishes no rules of its own.
            parser.allow_all = True
        except http.client.HTTPException as exc:
            raise FetchError("could not read robots.txt: %s" % exc, retryable=True) from exc
        except NETWORK_ERRORS as exc:
            raise FetchError("could not read robots.txt: %s" % _reason(exc), retryable=True, network=True) from exc
        except (ValueError, OverflowError) as exc:
            raise FetchError("could not read robots.txt: %s" % exc) from exc
        # can_fetch() answers False until a parser is marked as read, even with no rules.
        parser.modified()
        with self._robots_lock:
            self._robots[origin] = parser
        return parser

    def _backoff(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after and retry_after.strip().isdigit():
            return min(self.max_delay, float(retry_after.strip()))
        ceiling = min(self.max_delay, self.base_delay * (2 ** attempt))
        return random.uniform(ceiling / 2, ceiling)

    def get(
        self,
        url: str,
        allowed_domain: Optional[str] = None,
        etag: str = "",
        last_modified: str = "",
    ) -> FetchResult:
        _check_url(url, allowed_domain)
        url = _encode_url(url)
        if self.respect_robots:
            parser = self._robots_for(url, allowed_domain)
            if not parser.can_fetch(ROBOTS_AGENT, url):
                raise RobotsBlocked("robots.txt disallows %s" % url)

        headers: Dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        host = urlsplit(url).netloc
        last_error: Optional[FetchError] = None
        for attempt in range(self.attempts):
            self._throttle(host)
            retry_after: Optional[str] = None
            try:
                with self._open(url, allowed_domain, headers) as response:
                    return self._read(url, response, attempt + 1)
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return FetchResult(url, url, 304, not_modified=True, attempts=attempt + 1)
                retryable = exc.code == 429 or exc.code >= 500
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                last_error = FetchError("HTTP %d" % exc.code, exc.code, retryable=retryable)
            except FetchError:
                raise
            except http.client.InvalidURL as exc:
                raise FetchError("malformed URL: %s" % url) from exc
            except http.client.HTTPException as exc:
                # A server answered, but not with valid HTTP.
                last_error = FetchError("bad response: %s" % (exc or type(exc).__name__), retryable=True)
            except NETWORK_ERRORS as exc:
                if isinstance(getattr(exc, "reason", None), FetchError):
                    raise exc.reason
                last_error = FetchError("network error: %s" % _reason(exc), retryable=True, network=True)
            except (ValueError, OverflowError) as exc:
                # For example a non-ASCII host name or an out-of-range port.
                raise FetchError("malformed URL: %s" % url) from exc

            if not last_error.retryable or attempt == self.attempts - 1:
                break
            self.sleep(self._backoff(attempt, retry_after))

        assert last_error is not None
        raise last_error

    def _read(self, url: str, response, attempts: int) -> FetchResult:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith(TEXT_TYPES):
            raise FetchError("unsupported content type: %s" % content_type)
        body = response.read(self.max_bytes + 1)
        if len(body) > self.max_bytes:
            raise FetchError("response larger than %d bytes" % self.max_bytes)
        charset = response.headers.get_content_charset()
        if not charset:
            # No charset in the header, so use what the feed or page declares about itself.
            declared = DECLARED_CHARSET.search(body[:1024])
            charset = declared.group(1).decode("ascii") if declared else "utf-8"
        try:
            text = body.decode(charset, "replace")
        except LookupError:
            text = body.decode("utf-8", "replace")
        return FetchResult(
            url=url,
            final_url=response.geturl(),
            status=response.status,
            text=text,
            etag=response.headers.get("ETag") or "",
            last_modified=response.headers.get("Last-Modified") or "",
            attempts=attempts,
        )


def _reason(exc: Exception) -> object:
    return getattr(exc, "reason", None) or exc
