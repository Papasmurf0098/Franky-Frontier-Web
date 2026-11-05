"""Deterministic scoring, ported from the original franky_frontier.py engine.

The weights match the original: freshness, trust, commercial relevance and solo
usefulness at 0.2 each; novelty and anti-hype at 0.1 each. Changes from the original,
each fixing something that made its results unusable:

- Terms match whole words. The original's substring checks read "new" in "news",
  "ops" in "stops", "api" in "rapid" and "ai" in "email", so nearly everything scored high.
- Only the title, description and opening text are read, not a whole page of site chrome.
- An unknown publish date no longer counts as published today.
- A "practicality" adjustment rewards things you can use. On top of the score, corporate
  news can't rise above Low, and nothing is High without a sign that you can use it.
  That is how the "practical and allergic to hype" goal in the original dashboard copy
  becomes something the score can measure.
- Corporate and hype terms are only read from the headline and description, because
  article bodies mention "enterprise" or "soon" in passing all the time.
- "Desktop" became "Laptop", because the dashboard filter only ever offered "Laptop".

Community sources (Tier 3) are new since the original. They lose points on trust, can win a
little back through attention (stars, upvotes), and need strong attention to reach High.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from functools import lru_cache
from typing import Dict, Iterable, List, Sequence

MAX_FRESHNESS_DAYS = 60
HIGH_THRESHOLD = 4.2
MEDIUM_THRESHOLD = 3.4
SCORED_TEXT_CHARS = 2000
ENGAGEMENT_BONUS = {1: 0.15, 2: 0.3}

COMMERCIAL_HIGH = ("automation", "automate", "workflow", "workflows", "client", "clients", "business", "sales", "leads")
COMMERCIAL_MID = ("productivity", "creator", "creators", "marketing")
SOLO_HIGH = ("solo", "freelancer", "freelancers", "small business", "creator", "creators", "individual", "individuals")
SOLO_MID = ("team", "teams", "workspace")
NOVELTY = ("launch", "launches", "launching", "new", "introducing", "release", "released", "now available", "announcing")
HYPE = ("coming soon", "waitlist", "soon", "roadmap", "research preview", "limited preview", "early access", "private beta")
PHONE = ("iphone", "ios", "android", "mobile app", "phone", "smartphone")
LAPTOP = ("laptop", "desktop", "macos", "mac", "windows", "linux", "terminal", "cli", "command line", "browser extension")
NO_CODE = ("no-code", "no code", "drag-and-drop", "template", "templates", "no coding")
CODE = ("api", "sdk", "python", "javascript", "cli", "github", "open-source", "open source", "pip install")
ON_DEVICE = (
    "on-device", "on device", "runs locally", "run locally", "running locally", "local model", "local models",
    "offline", "open-weight", "open weight", "open weights", "mlx", "gguf", "ollama", "llama.cpp",
)
USABLE = (
    "now available", "available today", "free", "how to", "guide", "tutorial", "template", "templates",
    "open-source", "open source", "you can", "try it", "get started", "step-by-step", "app", "integration",
    "should you use", "connector", "create", "build", "built", "run", "how i", "show hn",
)
CORPORATE = (
    "acquire", "acquires", "acquisition", "funding", "investment", "investing", "valuation", "partnership",
    "partners with", "government", "governance", "lawsuit", "earnings", "board of directors", "compliance",
    "public benefit", "hiring", "memorandum", "regulation", "enterprise", "ipo", "initial public offering",
    "appoints", "appointed", "joins", "chief", "trust", "govcloud", "approved customers", "customers",
    "discover how", "utilities", "foundations", "institutions", "evaluators", "digital economy",
    "guest investigators", "xprize",
)
AT_HOME_THEMES = (
    (("image", "images", "video", "videos", "photo", "photos", "animation"), "Make images or video for a project at home."),
    (("voice", "speech", "audio", "transcription", "transcribe", "podcast"), "Work with voice or audio from home."),
    (("agent", "agents", "automation", "automate", "workflow", "workflows", "integration", "zap"), "Hand a routine task to an automation or agent."),
    (("code", "coding", "api", "sdk", "developer", "developers"), "Build a small tool or script around it."),
    (("document", "documents", "notebook", "research", "search", "summarize", "writing"), "Speed up research, writing, or document work."),
)
NO_CLEAR_USE = "No clear at-home use yet. Skim it, then decide."
TRY_IT = "Worth trying hands-on at home."
STOP_WORDS = {"the", "and", "for", "with", "from", "that", "this", "new", "how", "your", "you", "are", "now", "into"}


@lru_cache(maxsize=512)
def _term_pattern(term: str):
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


def has_term(text: str, term: str) -> bool:
    return _term_pattern(term).search(text) is not None


def has_any(text: str, terms: Iterable[str]) -> bool:
    return any(has_term(text, term) for term in terms)


def similarity(a: str, b: str) -> float:
    def tokens(value: str) -> set:
        found = re.findall(r"[a-z0-9]+", value.lower())
        return {word for word in found if len(word) > 2 and word not in STOP_WORDS}

    left, right = tokens(a), tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def repeat_marker(headline: str, prior_headlines: Sequence[str]) -> str:
    count = 1 + sum(1 for prior in prior_headlines if similarity(headline, prior) >= 0.45)
    if count <= 1:
        return ""
    return "R%d" % min(count, 4)


def priority_from_score(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "High"
    if score >= MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"


def _device(text: str, default: str) -> str:
    phone, laptop = has_any(text, PHONE), has_any(text, LAPTOP)
    if phone and laptop:
        return "Both"
    if phone:
        return "Phone"
    if laptop:
        return "Laptop"
    return "Laptop" if default == "Desktop" else (default or "Both")


def _barrier(text: str, default: str) -> str:
    if has_any(text, NO_CODE):
        return "No-code"
    if has_any(text, CODE):
        return "Code"
    return default or "Low-code"


def _at_home_use(headline: str, text: str, on_device: bool, usable: bool) -> str:
    if on_device:
        return "Try running it locally on your laptop."
    # The headline says what the item is about. The body is only a fallback.
    for scope in (headline, text):
        for terms, line in AT_HOME_THEMES:
            if has_any(scope, terms):
                return line
    return TRY_IT if usable else NO_CLEAR_USE


def assess(item: Dict[str, str], source: Dict, prior_headlines: Sequence[str], today: date) -> Dict:
    """Score one collected item. Pure function: same input, same output."""
    headline = " ".join((item.get("title", ""), item.get("description", ""))).lower()
    text = " ".join((headline, item.get("text", "")[:SCORED_TEXT_CHARS].lower()))
    tier = source.get("trust_tier", "Tier 2")

    positive = [kw for kw in source.get("positive_keywords", []) if has_term(text, kw)]
    negative = [kw for kw in source.get("negative_keywords", []) if has_term(text, kw)]
    pos, neg = min(1, len(positive)), min(1, len(negative))

    commercial = 5 if has_any(text, COMMERCIAL_HIGH) else 4 if has_any(text, COMMERCIAL_MID) else 3
    solo = 5 if has_any(text, SOLO_HIGH) else 4 if has_any(text, SOLO_MID) else 3
    novelty = 5 if has_any(text, NOVELTY) else 3
    commercial = min(5, max(1, commercial + pos - neg))
    solo = min(5, max(1, solo - neg))
    novelty = min(5, max(1, novelty + (1 if item.get("status") == "new" else 0) - neg))
    frontier = has_any(headline, HYPE)
    hype_risk = 4 if frontier else 2
    trust = 5 if tier == "Tier 1" else 4 if tier == "Tier 2" else 3

    published = item.get("published") or ""
    freshness_days = None
    if published:
        freshness_days = max(0, (today - datetime.strptime(published, "%Y-%m-%d").date()).days)
        freshness = 5 if freshness_days <= 7 else 4 if freshness_days <= 30 else 3 if freshness_days <= MAX_FRESHNESS_DAYS else 2
    else:
        freshness = 3

    # A hosted API can serve an open-weight model, but using it there isn't running it locally.
    on_device = has_any(text, ON_DEVICE) and not source.get("hosted_only")
    usable = has_any(headline, USABLE) or on_device
    corporate = has_any(headline, CORPORATE)
    practicality = 0.25 if usable else 0.0

    publish_bonus = 0.15 if item.get("published_source", "unknown") != "unknown" else 0.0
    keyword_bonus = min(0.2, 0.05 * len(positive))
    keyword_penalty = min(0.2, 0.05 * len(negative))
    marker = repeat_marker(item.get("title", ""), prior_headlines)
    # Community sources score low on trust, so real attention can win some of that back.
    engagement = ENGAGEMENT_BONUS.get(item.get("strength", 0), 0.0)

    total = (
        freshness * 0.2
        + trust * 0.2
        + commercial * 0.2
        + solo * 0.2
        + novelty * 0.1
        + (5 - hype_risk) * 0.1
        + publish_bonus
        + keyword_bonus
        - keyword_penalty
        + practicality
        + engagement
        + (0.15 if marker else 0.0)
    )

    flags: List[str] = []
    if not published:
        flags.append("publish_date_unknown")
    if frontier:
        flags.append("speculative")
    if corporate:
        flags.append("corporate_news")

    priority = priority_from_score(total)
    if corporate:
        priority = "Low"
    elif priority == "High" and not usable:
        priority = "Medium"
    elif priority == "High" and tier == "Tier 3" and item.get("strength", 0) < 2:
        # Anyone can post to a community source, so High takes strong attention as well.
        priority = "Medium"

    return {
        "score": round(total, 2),
        "priority": priority,
        "usable": usable,
        "device_access": _device(text, source.get("device_access", "Both")),
        "technical_barrier": _barrier(text, source.get("technical_barrier", "Low-code")),
        "on_device": on_device,
        "at_home_use": _at_home_use(headline, text, on_device, usable),
        "repeat_marker": marker,
        "frontier": frontier,
        "freshness_days": freshness_days,
        "stale": freshness_days is not None and freshness_days > MAX_FRESHNESS_DAYS,
        "flags": flags,
        "breakdown": {
            "freshness": freshness,
            "trust": trust,
            "commercial": commercial,
            "solo": solo,
            "novelty": novelty,
            "anti_hype": 5 - hype_risk,
            "publish_bonus": round(publish_bonus, 2),
            "keyword_bonus": round(keyword_bonus - keyword_penalty, 2),
            "practicality": round(practicality, 2),
            "engagement": engagement,
        },
    }
