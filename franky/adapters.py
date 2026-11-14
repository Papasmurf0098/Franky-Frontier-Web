"""Turn JSON from public, no-account APIs into plain entries the scanner can score.

Each adapter is a pure function: JSON in, entries out, no network. API responses are
untrusted, so a wrong type on one field costs that entry, not the whole source. Only a
response with the wrong overall shape raises AdapterError, because that means the API
changed, and source health needs to say so.

An entry has: title, link, published (YYYY-MM-DD or ""), published_at (a UTC timestamp or
""), summary, signal (how much attention
it has, in words), and strength (0 to 2, used as a small scoring bonus). These results can
link anywhere on the web. The scanner only shows those links and never fetches them.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable, Dict, List

from .parsing import normalize_space, parse_date, parse_timestamp, strip_tags

URL_IN_TEXT = re.compile(r"https?://\S+")


class AdapterError(ValueError):
    """The response wasn't the shape this API is documented to return."""


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int:
    # bool is an int in Python, and neither it nor a negative number is a real count.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return 0
    return int(value)


def _rows(data: object, key: str = "") -> List[Dict]:
    if key:
        rows = data.get(key) if isinstance(data, dict) else None
    else:
        rows = data
    if not isinstance(rows, list):
        raise AdapterError("expected a list%s in the response" % (" under '%s'" % key if key else ""))
    return [row for row in rows if isinstance(row, dict)]


def _strength(value: int, strong: int, solid: int) -> int:
    return 2 if value >= strong else 1 if value >= solid else 0


def _plural(count: int, word: str) -> str:
    return "%s %s%s" % (format(count, ","), word, "" if count == 1 else "s")


def hacker_news(data: object) -> List[Dict]:
    """https://hn.algolia.com/api"""
    entries = []
    for hit in _rows(data, "hits"):
        title = strip_tags(_text(hit.get("title")))
        story_id = _text(hit.get("objectID"))
        if not title or not story_id.isdigit():
            continue
        discussion = "https://news.ycombinator.com/item?id=%s" % story_id
        points, comments = _count(hit.get("points")), _count(hit.get("num_comments"))
        entries.append(
            {
                "title": title,
                # Ask HN and some Show HN posts have no outside link, only the discussion.
                "link": _text(hit.get("url")).strip() or discussion,
                "published": parse_date(_text(hit.get("created_at"))) or "",
                "published_at": parse_timestamp(_text(hit.get("created_at"))),
                "summary": normalize_space(URL_IN_TEXT.sub("", strip_tags(_text(hit.get("story_text"))))),
                "signal": "%s and %s on Hacker News" % (_plural(points, "point"), _plural(comments, "comment")),
                "strength": _strength(points, 300, 100),
            }
        )
    return entries


def github_repositories(data: object) -> List[Dict]:
    """https://docs.github.com/en/rest/search/search#search-repositories"""
    entries = []
    for repo in _rows(data, "items"):
        name = _text(repo.get("full_name"))
        link = _text(repo.get("html_url"))
        if not name or not link or repo.get("fork") is True or repo.get("archived") is True:
            continue
        stars = _count(repo.get("stargazers_count"))
        language = _text(repo.get("language"))
        entries.append(
            {
                "title": name,
                "link": link,
                "published": parse_date(_text(repo.get("created_at"))) or "",
                "published_at": parse_timestamp(_text(repo.get("created_at"))),
                "summary": strip_tags(_text(repo.get("description"))),
                "signal": "%s on GitHub%s" % (_plural(stars, "star"), ", written in %s" % language if language else ""),
                "strength": _strength(stars, 2000, 500),
            }
        )
    return entries


def hugging_face_models(data: object) -> List[Dict]:
    """https://huggingface.co/docs/hub/api"""
    entries = []
    for model in _rows(data):
        model_id = _text(model.get("id") or model.get("modelId"))
        if "/" not in model_id:
            continue
        likes, downloads = _count(model.get("likes")), _count(model.get("downloads"))
        tags = [tag for tag in model.get("tags", []) if isinstance(tag, str)] if isinstance(model.get("tags"), list) else []
        licence = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("license:")), "")
        # "gguf" in the text is what marks it as something that runs on a laptop.
        format_name = "GGUF" if "gguf" in tags else "open"
        summary = "Trending %s model on Hugging Face%s." % (format_name, ", license %s" % licence if licence else "")
        entries.append(
            {
                "title": model_id,
                "link": "https://huggingface.co/%s" % model_id,
                "published": parse_date(_text(model.get("createdAt"))) or "",
                "published_at": parse_timestamp(_text(model.get("createdAt"))),
                "summary": summary,
                "signal": "%s and %s on Hugging Face" % (_plural(likes, "like"), _plural(downloads, "download")),
                "strength": _strength(likes, 300, 50),
            }
        )
    return entries


def _is_free(pricing: object) -> bool:
    if not isinstance(pricing, dict):
        return False
    try:
        # Not "<= 0": OpenRouter's auto-router lists -1, which means variable, not free.
        return float(pricing.get("prompt")) == 0 and float(pricing.get("completion")) == 0
    except (TypeError, ValueError):
        return False


def openrouter_free_models(data: object) -> List[Dict]:
    """https://openrouter.ai/docs/api-reference/list-available-models"""
    entries = []
    for model in _rows(data, "data"):
        model_id = _text(model.get("id"))
        if not model_id or not _is_free(model.get("pricing")):
            continue
        name = normalize_space(_text(model.get("name")).replace("(free)", "")) or model_id
        created = _count(model.get("created"))
        try:
            published = datetime.fromtimestamp(created, timezone.utc).date().isoformat() if created else ""
        except (OverflowError, OSError, ValueError):
            published = ""
        entries.append(
            {
                "title": "%s is free on OpenRouter" % name,
                "link": "https://openrouter.ai/%s" % model_id,
                "published": published,
                "summary": strip_tags(_text(model.get("description"))),
                # Listing the models needs no account, but actually calling one does.
                "signal": "Free to call, but calling it needs an OpenRouter account",
                "strength": 0,
            }
        )
    entries.sort(key=lambda entry: entry["published"], reverse=True)
    return entries


def dev_articles(data: object) -> List[Dict]:
    """https://developers.forem.com/api"""
    entries = []
    for article in _rows(data):
        title = strip_tags(_text(article.get("title")))
        link = _text(article.get("url"))
        if not title or not link:
            continue
        reactions, comments = _count(article.get("public_reactions_count")), _count(article.get("comments_count"))
        entries.append(
            {
                "title": title,
                "link": link,
                "published": parse_date(_text(article.get("published_timestamp"))) or "",
                "published_at": parse_timestamp(_text(article.get("published_timestamp"))),
                "summary": strip_tags(_text(article.get("description"))),
                "signal": "%s and %s on DEV" % (_plural(reactions, "reaction"), _plural(comments, "comment")),
                "strength": _strength(reactions, 150, 50),
            }
        )
    return entries


PAPER_ID = re.compile(r"^[\w.\-]{1,40}$")


def hugging_face_papers(data: object) -> List[Dict]:
    """https://huggingface.co/api/daily_papers: research papers the community picked that day."""
    entries = []
    for row in _rows(data):
        paper = row.get("paper") if isinstance(row.get("paper"), dict) else {}
        paper_id = _text(paper.get("id"))
        title = normalize_space(strip_tags(_text(paper.get("title")) or _text(row.get("title"))))
        if not title or not PAPER_ID.match(paper_id):
            continue
        upvotes, comments = _count(paper.get("upvotes")), _count(row.get("numComments"))
        # The day it was featured, not the day it was written, because that is when it became news.
        featured = _text(paper.get("submittedOnDailyAt")) or _text(row.get("publishedAt"))
        entries.append(
            {
                "title": title,
                "link": "https://huggingface.co/papers/%s" % paper_id,
                "published": parse_date(featured) or "",
                "published_at": parse_timestamp(featured),
                "summary": strip_tags(_text(paper.get("summary")) or _text(row.get("summary"))),
                "signal": "%s and %s on Hugging Face Papers" % (_plural(upvotes, "upvote"), _plural(comments, "comment")),
                "strength": _strength(upvotes, 100, 30),
            }
        )
    return entries


ADAPTERS: Dict[str, Callable[[object], List[Dict]]] = {
    "hacker_news": hacker_news,
    "github_repositories": github_repositories,
    "hugging_face_models": hugging_face_models,
    "hugging_face_papers": hugging_face_papers,
    "openrouter_free_models": openrouter_free_models,
    "dev_articles": dev_articles,
}
