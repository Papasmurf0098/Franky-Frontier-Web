"""Turn untrusted feed and page text into plain dictionaries. Standard library only."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from xml.parsers import expat

MONTHS = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
TEXT_DATE = re.compile(r"\b(?:%s)\.? \d{1,2},? 20\d\d\b" % MONTHS, re.I)
ISO_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]")
# <title> usually ends with the site name after any of these separators.
TITLE_TAG_SUFFIX = re.compile(r"\s+(?:\||\\|—|–)\s+[^|\\—–]{2,60}$")
# og:title is the article's own title, so a dash there is usually a real subtitle.
OG_TITLE_SUFFIX = re.compile(r"\s+(?:\||\\)\s+[^|\\]{2,60}$")
# Must look like a tag, so "5 < 6 and 7 > 3" survives.
TAG = re.compile(r"<(?:/?[a-zA-Z][^<>]*|![^<>]*)>")
MEDIA_NAMESPACE = "search.yahoo.com/mrss"
URL_PATH_SAFE = "/%:@!$&'()*+,;=~-._"
TRACKING_PARAM = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid)$")

PUBLISH_META_KEYS = (
    "article:published_time",
    "og:published_time",
    "datepublished",
    "publish-date",
    "publishdate",
    "pubdate",
    "parsely-pub-date",
    "date",
)


class FeedError(ValueError):
    """The feed could not be parsed safely."""


def normalize_space(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_tags(text: Optional[str]) -> str:
    return normalize_space(unescape(TAG.sub(" ", text or "")))


def canonical_url(url: str) -> str:
    """Drop query and fragment so the same article is one record however it is linked.

    The path is percent-encoded, because this URL is also fetched and shown as a link.
    The trailing slash stays as the site wrote it, which saves a redirect on every fetch.
    """
    parts = urlsplit(url.strip())
    path = quote(parts.path, safe=URL_PATH_SAFE) or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def external_url(url: str) -> str:
    """Clean a link from an API result. Raises ValueError if it isn't a web address.

    Unlike canonical_url, this keeps the query string, because these links go to any site
    on the web, where it often names the page (youtube.com/watch?v=..., item?id=...).
    Only tracking parameters are removed.
    """
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("not a web address: %s" % url)
    query = "&".join(
        pair for pair in parts.query.split("&")
        if pair and not TRACKING_PARAM.match(pair.split("=", 1)[0].lower())
    )
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            quote(parts.path, safe=URL_PATH_SAFE) or "/",
            quote(query, safe=URL_PATH_SAFE + "?"),
            "",
        )
    )


def parse_date(value: Optional[str]) -> Optional[str]:
    """Return an ISO date (YYYY-MM-DD), or None when the value can't be trusted."""
    value = normalize_space(value)
    if not value:
        return None
    parsed: Optional[date] = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(value).date()
        except (TypeError, ValueError, IndexError):
            pass
    if parsed is None:
        # Python 3.9's fromisoformat refuses "+0000" and unusual fractions of a second.
        match = ISO_PREFIX.match(value)
        if match:
            try:
                parsed = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                pass
    if parsed is None:
        match = TEXT_DATE.search(value)
        if match:
            cleaned = match.group(0).replace(".", "").replace(",", "")
            month, day, year = cleaned.split()
            try:
                parsed = datetime.strptime(
                    "%s %s %s" % (month[:3].title(), day, year), "%b %d %Y"
                ).date()
            except ValueError:
                pass
    if parsed is None or not (2000 <= parsed.year <= 2100):
        return None
    return parsed.isoformat()


def parse_timestamp(value: Optional[str]) -> str:
    """Return a UTC timestamp (2026-11-03T10:00:00Z) when the value carries a time of day, else "".

    The news wire sorts by this within a day. A bare date isn't a time, so it stays empty.
    """
    value = normalize_space(value)
    if not value or len(value) < 16:
        return ""
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        parsed = parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    if not (2000 <= parsed.year <= 2100):
        return ""
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def looks_like_feed(text: str) -> bool:
    head = text[:1500].lower()
    return "<rss" in head or "<feed" in head or "<rdf:rdf" in head


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, *names: str) -> str:
    for name in names:
        for child in node:
            # media:description is an image caption, not the article's summary.
            if MEDIA_NAMESPACE in child.tag or _local(child.tag) != name:
                continue
            # itertext also reads Atom's XHTML form, where the text sits inside a <div>.
            text = "".join(child.itertext())
            if text.strip():
                return text
    return ""


def _entry_link(node: ET.Element) -> str:
    fallback = ""
    for child in node:
        if _local(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href and child.attrib.get("rel", "alternate") in ("alternate", ""):
            return href.strip()
        if child.text and child.text.strip():
            fallback = child.text.strip()
    if fallback:
        return fallback
    # RSS allows a guid to be the permalink, and it is one unless it says otherwise.
    for child in node:
        if _local(child.tag) == "guid" and child.attrib.get("isPermaLink", "true").lower() != "false":
            value = (child.text or "").strip()
            if value.startswith(("http://", "https://")):
                return value
    return ""


def _refuse_dtd(text: str) -> None:
    """Refuse any DOCTYPE or entity declaration, stopping before an entity can expand.

    Python's XML parser relies on expat for entity-bomb protection, and macOS system
    Python ships an old expat (2.2.8 when checked), so a hostile feed could otherwise
    exhaust memory. Asking the parser, rather than searching the text, means an article
    that merely mentions "<!DOCTYPE" inside CDATA isn't refused.
    """

    def refuse(*_args):
        raise FeedError("feed declares a DOCTYPE or entities, refusing to parse it")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(text, True)
    except expat.ExpatError as exc:
        raise FeedError("feed is not valid XML: %s" % exc) from exc


def parse_feed(text: str, feed_url: str, max_items: int = 8) -> List[Dict[str, str]]:
    """Parse RSS, Atom, or RDF into entries, newest first."""
    text = text.lstrip("﻿ \r\n\t")
    _refuse_dtd(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FeedError("feed is not valid XML: %s" % exc) from exc

    entries: List[Dict[str, str]] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = strip_tags(_child_text(node, "title"))
        link = _entry_link(node)
        if not title or not link:
            continue
        try:
            link = urljoin(feed_url, link)
        except ValueError:
            # One malformed link only costs that entry.
            continue
        raw_date = _child_text(node, "pubdate", "published", "date", "updated")
        entries.append(
            {
                "id": normalize_space(_child_text(node, "guid", "id")) or link,
                "title": title,
                "link": link,
                "published": parse_date(raw_date) or "",
                "published_at": parse_timestamp(raw_date),
                "summary": strip_tags(
                    _child_text(node, "description", "summary", "encoded", "content")
                ),
            }
        )
    entries.sort(key=lambda entry: entry["published"], reverse=True)
    return entries[: max(0, max_items)]


class _PageParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}
    CHROME = {"nav", "footer", "header", "aside", "form"}
    CONTENT = {"article", "main"}
    # A closing paragraph-like tag ends a sentence even when markup has no period.
    BREAKS = {"p", "li", "h1", "h2", "h3", "h4", "br", "div", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.jsonld: List[str] = []
        self.title_parts: List[str] = []
        self.h1_parts: List[str] = []
        self.first_h1 = ""
        self.body_parts: List[str] = []
        self.content_parts: List[str] = []
        self.links: List[Tuple[str, str]] = []
        self.time_values: List[str] = []
        self._skip = 0
        self._chrome = 0
        self._content = 0
        self._in_title = False
        self._in_h1 = False
        self._in_jsonld = False
        self._jsonld_parts: List[str] = []
        self._link_href: Optional[str] = None
        self._link_parts: List[str] = []
        self._time_parts: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        data = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            key = (data.get("property") or data.get("name") or data.get("itemprop") or "").lower()
            if key and data.get("content") and key not in self.meta:
                self.meta[key] = data["content"]
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "time":
            if data.get("datetime"):
                self.time_values.append(data["datetime"])
            else:
                self._time_parts = []
        elif tag == "script" and "ld+json" in data.get("type", "").lower():
            self._in_jsonld = True
            self._jsonld_parts = []
        elif tag == "a" and data.get("href") and not self._chrome:
            self._link_href = data["href"]
            self._link_parts = []

        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.CHROME:
            self._chrome += 1
        elif tag in self.CONTENT:
            self._content += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
            if not self.first_h1:
                self.first_h1 = normalize_space(" ".join(self.h1_parts))
        elif tag == "time" and self._time_parts is not None:
            self.time_values.append(" ".join(self._time_parts))
            self._time_parts = None
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.append("".join(self._jsonld_parts))
        elif tag == "a" and self._link_href is not None:
            self.links.append((self._link_href, normalize_space(" ".join(self._link_parts))))
            self._link_href = None

        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.CHROME:
            self._chrome = max(0, self._chrome - 1)
        elif tag in self.CONTENT:
            self._content = max(0, self._content - 1)
        if tag in self.BREAKS and not self._skip and not self._chrome:
            self.body_parts.append("\n")
            if self._content:
                self.content_parts.append("\n")

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_parts.append(data)
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        if self._time_parts is not None:
            self._time_parts.append(data)
        if self._link_href is not None:
            self._link_parts.append(data)
        if self._skip or self._chrome:
            return
        self.body_parts.append(data)
        if self._content:
            self.content_parts.append(data)


def _walk_jsonld(blocks: Iterable[str]) -> Iterable[dict]:
    for block in blocks:
        try:
            stack = [json.loads(block)]
        except ValueError:
            continue
        while stack:
            current = stack.pop()
            if isinstance(current, list):
                stack.extend(current)
            elif isinstance(current, dict):
                yield current
                stack.extend(v for v in current.values() if isinstance(v, (list, dict)))


def _trim_site_name(title: str, suffix) -> str:
    title = normalize_space(title)
    trimmed = suffix.sub("", title)
    return trimmed if len(trimmed) >= 8 else title


def key_sentences(text: str, limit: int = 2) -> List[str]:
    picked: List[str] = []
    for line in text.split("\n"):
        for sentence in re.split(r"(?<=[.!?])\s+", normalize_space(line)):
            # Ending in punctuation is what separates a sentence from a byline or a menu.
            if 40 <= len(sentence) <= 280 and sentence[-1] in ".!?":
                picked.append(sentence)
            if len(picked) >= limit:
                return picked
    return picked


def parse_page(html: str, base_url: str) -> Dict[str, object]:
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):
        # HTMLParser can raise on badly broken markup. Keep whatever was collected.
        pass

    jsonld = list(_walk_jsonld(parser.jsonld))
    raw_text = "".join(parser.content_parts) or "".join(parser.body_parts)
    text = "\n".join(line for line in (normalize_space(l) for l in raw_text.split("\n")) if line)

    title = ""
    if parser.meta.get("og:title"):
        title = _trim_site_name(parser.meta["og:title"], OG_TITLE_SUFFIX)
    elif parser.first_h1:
        title = parser.first_h1
    else:
        for node in jsonld:
            if isinstance(node.get("headline"), str):
                title = normalize_space(node["headline"])
                break
    title = title or _trim_site_name(" ".join(parser.title_parts), TITLE_TAG_SUFFIX)

    description = strip_tags(
        parser.meta.get("og:description") or parser.meta.get("description") or ""
    )

    published, published_source = "", "unknown"
    for key in PUBLISH_META_KEYS:
        found = parse_date(parser.meta.get(key))
        if found:
            published, published_source = found, "meta:" + key
            break
    if not published:
        for node in jsonld:
            found = parse_date(node.get("datePublished") if isinstance(node.get("datePublished"), str) else "")
            if found:
                published, published_source = found, "jsonld:datePublished"
                break
    if not published:
        for value in parser.time_values:
            found = parse_date(value)
            if found:
                published, published_source = found, "time"
                break
    if not published:
        match = TEXT_DATE.search(text[:1500])
        if match:
            found = parse_date(match.group(0))
            if found:
                published, published_source = found, "text"

    links: List[Tuple[str, str]] = []
    seen = set()
    for href, label in parser.links:
        try:
            parts = urlsplit(urljoin(base_url, href.strip()))
        except ValueError:
            # One malformed link only costs that link.
            continue
        if parts.scheme not in ("http", "https"):
            continue
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if url not in seen:
            seen.add(url)
            links.append((url, label))

    return {
        "title": title,
        "description": description,
        "published": published,
        "published_source": published_source,
        "text": text,
        "links": links,
    }
