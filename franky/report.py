"""Markdown memo for a run, using the six sections from the original Franky report."""

from __future__ import annotations

from typing import Dict, List


def _bullets(lines: List[str], empty: str) -> List[str]:
    return ["- %s" % line for line in lines] or ["- %s" % empty]


def render_report(run: Dict, findings: List[Dict], watch_items: List[Dict], failed_sources: List[Dict]) -> str:
    uses: List[str] = []
    for finding in findings:
        use = finding.get("at_home_use")
        if use and use not in uses:
            uses.append(use)
    opportunities = [
        "%s: %s" % (f["headline"], f["at_home_use"])
        for f in findings
        if f.get("priority") == "High"
    ]
    confidence = (
        "Rule-based scoring, not an editor. Tier 1 is the company's own channel, Tier 2 is a "
        "trusted vendor blog, and Tier 3 is a community source that anyone can post to. "
        "Check the source before acting on anything."
    )
    if failed_sources:
        names = ", ".join(s["name"] for s in failed_sources)
        confidence += " These sources could not be checked this run, so coverage is incomplete: %s." % names

    parts = [
        "# Franky Frontier Report: %s" % run["run_id"],
        "",
        "Coverage: %s" % run["time_range"],
        "",
        "## Overall Summary",
        run["summary"],
        "",
        "## What's New Since Last Run",
        *_bullets(["%s [%s], from %s" % (f["headline"], f["priority"], f["source_name"]) for f in findings], "Nothing new."),
        "",
        "## Key Things to Watch Going Forward",
        *_bullets(["%s: %s" % (w["item_trend"], w["what_needs_confirmation"]) for w in watch_items], "Nothing added."),
        "",
        "## At-Home Uses",
        *_bullets(uses, "None this run."),
        "",
        "## Emerging Opportunities",
        *_bullets(opportunities, "No high-priority items this run."),
        "",
        "## Confidence / Notes",
        confidence,
        "",
        "## Findings",
    ]
    for finding in findings:
        parts.append(
            "- **[%s](%s)**: %s (%s)"
            % (finding["headline"], finding["source_url"], finding["summary"], finding["device_access_technical_barrier"])
        )
    if not findings:
        parts.append("- None.")
    return "\n".join(parts).strip() + "\n"
