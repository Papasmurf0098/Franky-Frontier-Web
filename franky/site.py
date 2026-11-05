"""Build the static site that GitHub Pages serves: the dashboard, data.json, and the reports."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from .store import Store
from .timeutil import iso, now_utc

# Everything here is public, and Pages sites have a size limit, so old memos aren't kept forever.
MAX_REPORTS = 60
REPOSITORY = re.compile(r"^[\w.-]+/[\w.-]+$")


def build_site(store: Store, site_source: Path, out_dir: Path, repository: str = "", now: Optional[datetime] = None) -> Path:
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(str(out_dir))
    reports_out = out_dir / "reports"
    reports_out.mkdir(parents=True)

    shutil.copyfile(str(Path(site_source) / "index.html"), str(out_dir / "index.html"))
    # Tells GitHub Pages to serve these files as they are, with no Jekyll build.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    published = set()
    for report in sorted(store.reports_dir.glob("FF-*.md"), reverse=True)[:MAX_REPORTS]:
        shutil.copyfile(str(report), str(reports_out / report.name))
        published.add("reports/%s" % report.name)

    data = store.dashboard_data()
    for summary in data["summaries"]:
        if summary["link_to_report"] not in published:
            summary["link_to_report"] = ""
    data["latest_run_id"] = store.latest_run_id() or ""
    data["generated_at"] = iso(now or now_utc())
    data["repository"] = repository if REPOSITORY.match(repository or "") else ""
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return out_dir
