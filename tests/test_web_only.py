"""What makes this the web-only version: no server, history in git, a static site, a scheduled workflow."""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from franky import __main__ as cli
from franky import state
from franky.scanner import run_scan
from franky.site import MAX_REPORTS, build_site
from franky.store import Store

from .helpers import FEED_HEADERS, FIXED_NOW, FIXED_TODAY, LONG_TEXT, FakeSite, quick_fetcher, rss, source_for

REPO = Path(__file__).resolve().parent.parent
HTML = (REPO / "site" / "index.html").read_text(encoding="utf-8")
WORKFLOW = (REPO / ".github" / "workflows" / "scan.yml").read_text(encoding="utf-8")
ROBOTS_OK = {"/robots.txt": (200, "User-agent: *\nAllow: /\n", {"Content-Type": "text/plain"})}
FRESH = "Tue, 03 Nov 2026 10:00:00 GMT"


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state_dir = self.root / "state"
        self.site = FakeSite(dict(ROBOTS_OK))
        self.addCleanup(self.site.close)
        self.site.routes["/feed"] = (200, rss(("Build a new agent app", self.site.base + "/one", FRESH, LONG_TEXT)), FEED_HEADERS)
        self.source = source_for(self.site.base + "/feed")

    def scan_once(self, now=FIXED_NOW, **kwargs):
        state.restore(self.state_dir)
        store = Store(self.state_dir)
        result = run_scan(store, [self.source], fetcher=quick_fetcher(), now=now, today=FIXED_TODAY, **kwargs)
        state.save(store)
        return store, result


class NothingOnDevice(unittest.TestCase):
    def test_the_local_server_and_webhooks_are_gone(self):
        for name in ("franky/server.py", "franky/events.py", "server.js", "package.json"):
            self.assertFalse((REPO / name).exists(), name)
        for path in (REPO / "franky").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for banned in ("http.server", "ThreadingHTTPServer", "127.0.0.1", "webbrowser", "FRANKY_DATA_DIR", "from . import events"):
                self.assertNotIn(banned, text, path.name)

    def test_the_page_saves_nothing_in_the_browser(self):
        for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "serviceWorker"):
            self.assertNotIn(banned, HTML)

    def test_the_cli_has_no_serve_command(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            cli.main(["serve"])


class History(Base):
    def test_history_survives_between_runs(self):
        # Every GitHub runner starts empty, so all that survives is the saved text file.
        _, first = self.scan_once()
        self.assertEqual(first["findings"], 1)
        database = self.state_dir / "franky.sqlite3"
        database.unlink()
        store, second = self.scan_once(now=FIXED_NOW + timedelta(hours=1), force=True)
        self.assertEqual((second["findings"], second["updated"]), (0, 0))
        self.assertEqual(len(store.dashboard_data()["findings"]), 1)
        self.assertEqual(len(store.dashboard_data()["summaries"]), 2)

    def test_source_timing_survives_too(self):
        self.scan_once()
        hits = self.site.hits("/feed")
        _, again = self.scan_once(now=FIXED_NOW + timedelta(hours=1))
        self.assertEqual(again["status"], "nothing_due")
        self.assertEqual(self.site.hits("/feed"), hits)

    def test_history_is_readable_text_and_the_database_stays_out_of_git(self):
        self.scan_once()
        text = (self.state_dir / "franky.sql").read_text(encoding="utf-8")
        self.assertIn("Build a new agent app", text)
        self.assertIn("*.sqlite3", (self.state_dir / ".gitignore").read_text(encoding="utf-8"))

    def test_saving_twice_gives_the_same_file(self):
        store, _ = self.scan_once()
        first = (self.state_dir / "franky.sql").read_bytes()
        state.save(store)
        self.assertEqual((self.state_dir / "franky.sql").read_bytes(), first)

    def test_a_damaged_history_stops_the_run_instead_of_starting_over(self):
        self.scan_once()
        dump = self.state_dir / "franky.sql"
        dump.write_text("this is not sql;", encoding="utf-8")
        with self.assertRaises(Exception):
            state.restore(self.state_dir)
        self.assertEqual(dump.read_text(encoding="utf-8"), "this is not sql;")

    def test_first_run_has_no_history(self):
        self.assertFalse(state.restore(self.state_dir))


class SiteBuild(Base):
    def build(self, **kwargs):
        store, _ = self.scan_once()
        out = self.root / "dist"
        build_site(store, REPO / "site", out, now=FIXED_NOW, **kwargs)
        return out, store

    def test_contains_the_page_data_and_reports(self):
        out, _ = self.build(repository="owner/repo")
        data = json.loads((out / "data.json").read_text(encoding="utf-8"))
        self.assertTrue((out / "index.html").is_file())
        self.assertTrue((out / ".nojekyll").is_file())
        self.assertEqual(data["findings"][0]["headline"], "Build a new agent app")
        self.assertEqual(data["repository"], "owner/repo")
        self.assertEqual(data["generated_at"], "2026-11-04T12:00:00Z")
        self.assertTrue(data["latest_run_id"])
        report = data["summaries"][0]["link_to_report"]
        self.assertTrue(report.startswith("reports/"))
        self.assertTrue((out / report).is_file())

    def test_keeps_the_data_contract_the_page_reads(self):
        out, _ = self.build()
        data = json.loads((out / "data.json").read_text(encoding="utf-8"))
        self.assertTrue({"schema_version", "headlines", "findings", "summaries", "watchlist", "foundationalTools", "sources", "generated_at", "repository"} <= set(data))
        self.assertEqual(data["schema_version"], 2)
        self.assertTrue({"url", "headline", "summary", "source_name", "desk", "date", "published_at", "captured_at", "signal"} <= set(data["headlines"][0]))
        self.assertTrue({"finding_id", "date", "run_id", "headline", "summary", "at_home_use", "source_name", "source_tier", "source_url", "device_access_technical_barrier", "priority", "score", "status", "notes"} <= set(data["findings"][0]))
        self.assertTrue({"run_id", "run_date", "finished_at", "summary", "top_takeaway", "num_findings", "num_updated", "num_watchlist_items", "num_headlines", "status", "link_to_report"} <= set(data["summaries"][0]))

    def test_publishes_no_caching_details_or_database(self):
        out, _ = self.build()
        data = json.loads((out / "data.json").read_text(encoding="utf-8"))
        self.assertNotIn("etag", data["sources"][0])
        self.assertEqual(list(out.glob("*.sql*")), [])

    def test_only_recent_reports_are_published_and_old_links_are_blanked(self):
        store, _ = self.scan_once()
        with store.transaction() as conn:
            for number in range(MAX_REPORTS + 5):
                name = "FF-2020-01-01-%06d.md" % number
                (store.reports_dir / name).write_text("old", encoding="utf-8")
                conn.execute(
                    "INSERT INTO runs (run_id, trigger, status, started_at, run_date, num_findings, report_name) VALUES (?, 'x', 'completed', 'x', 'x', 1, ?)",
                    (name[:-3], name),
                )
        out = self.root / "dist"
        build_site(store, REPO / "site", out)
        self.assertEqual(len(list((out / "reports").glob("*.md"))), MAX_REPORTS)
        data = json.loads((out / "data.json").read_text(encoding="utf-8"))
        for summary in data["summaries"]:
            if summary["link_to_report"]:
                self.assertTrue((out / summary["link_to_report"]).is_file(), summary["run_id"])

    def test_a_bad_repository_name_is_dropped(self):
        store, _ = self.scan_once()
        out = self.root / "dist"
        for bad in ('x"><script>', "not a repo", "a/b/c"):
            build_site(store, REPO / "site", out, repository=bad)
            self.assertEqual(json.loads((out / "data.json").read_text(encoding="utf-8"))["repository"], "")

    def test_rebuilding_clears_old_files(self):
        out, store = self.build()
        (out / "leftover.txt").write_text("x", encoding="utf-8")
        build_site(store, REPO / "site", out)
        self.assertFalse((out / "leftover.txt").exists())


class StaticPage(unittest.TestCase):
    def test_every_path_is_relative(self):
        # GitHub Pages serves from /<repo>/, so a path starting with / would miss the site.
        self.assertIn('dataUrl: "data.json"', HTML)
        self.assertNotIn("/api/", HTML)
        self.assertNotIn('fetch("/', HTML)

    def test_sets_its_own_security_policy(self):
        # GitHub Pages can't send custom headers, so the policy has to be in the page.
        self.assertIn('http-equiv="Content-Security-Policy"', HTML)
        self.assertIn("default-src 'none'", HTML)
        self.assertIn("connect-src 'self'", HTML)

    def test_notices_new_data_and_says_when_it_is_old(self):
        self.assertIn("visibilitychange", HTML)
        self.assertIn("generatedAt", HTML)
        self.assertIn("function staleNotice", HTML)

    def test_has_no_scan_button_that_would_need_a_server(self):
        self.assertNotIn('id="scanBtn"', HTML)
        self.assertNotIn('method: "POST"', HTML)


class Workflow(unittest.TestCase):
    def test_scan_is_scheduled_off_the_hour(self):
        # GitHub's docs say runs at the start of an hour are the most likely to be delayed or dropped.
        cron = re.search(r'cron: "(\S+) ', WORKFLOW).group(1)
        self.assertNotEqual(cron, "0")

    def test_every_action_is_pinned_to_a_full_commit(self):
        uses = re.findall(r"uses: (\S+)", WORKFLOW)
        self.assertGreaterEqual(len(uses), 1)
        for action in uses:
            self.assertRegex(action, r"@[0-9a-f]{40}$", action)

    def test_publishes_then_asks_for_a_build(self):
        # GitHub's docs say this token's pushes don't start a Pages build, so the order matters.
        self.assertLess(WORKFLOW.index("ci/publish.sh dist"), WORKFLOW.index("pages/builds"))
        self.assertLess(WORKFLOW.index("ci/state.sh save"), WORKFLOW.index("ci/publish.sh dist"))

    def test_runs_one_at_a_time(self):
        self.assertIn("concurrency:", WORKFLOW)
        self.assertIn("cancel-in-progress: false", WORKFLOW)

    def test_uses_minimal_permissions_and_a_time_limit(self):
        self.assertRegex(WORKFLOW, r"(?m)^permissions:\n  contents: read$")
        self.assertIn("timeout-minutes:", WORKFLOW)
        self.assertNotIn("pull_request", WORKFLOW)

    def test_user_input_reaches_the_shell_through_an_env_var(self):
        # ${{ }} inside a run: block pastes text into the script, which is an injection risk.
        for block in re.findall(r"run: \|\n((?:\s{12}.*\n)+)", WORKFLOW):
            self.assertNotIn("${{", block)


class CommandLine(Base):
    def run_cli(self, env=None):
        stdout = io.StringIO()
        fetcher = quick_fetcher()
        with mock.patch.object(cli, "load_sources", return_value=[self.source]), mock.patch("franky.scanner.Fetcher", return_value=fetcher), mock.patch.dict(os.environ, env or {}), redirect_stdout(stdout):
            code = cli.main(["run", "--state-dir", str(self.state_dir), "--out", str(self.root / "dist")])
        return code, stdout.getvalue()

    def test_run_scans_saves_and_builds(self):
        code, output = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue((self.state_dir / "franky.sql").is_file())
        self.assertTrue((self.root / "dist" / "data.json").is_file())
        self.assertIn("first scan", output)

    def test_a_failed_source_warns_but_still_publishes(self):
        self.site.routes.pop("/feed")
        code, output = self.run_cli({"GITHUB_ACTIONS": "true"})
        self.assertEqual(code, 0)
        self.assertIn("::warning::Source Test Source: error", output)
        self.assertTrue((self.root / "dist" / "data.json").is_file())

    def test_web_text_cant_become_a_workflow_command(self):
        # GitHub treats a line starting with "::" as a command, and feeds control their headlines.
        self.site.routes["/feed"] = (200, rss(("Build a new agent app\n::add-mask::x", self.site.base + "/one", FRESH, LONG_TEXT + "\n::set-output name=a::b")), FEED_HEADERS)
        _, output = self.run_cli({"GITHUB_ACTIONS": "true"})
        for line in output.splitlines():
            self.assertFalse(line.startswith("::") and not line.startswith("::warning::Source "), line)
        self.assertNotIn("agent app", output)

    def test_job_summary_is_written(self):
        summary = self.root / "summary.md"
        self.run_cli({"GITHUB_STEP_SUMMARY": str(summary)})
        self.assertIn("| Test Source | ok |", summary.read_text(encoding="utf-8"))

    def test_repository_comes_from_github(self):
        self.run_cli({"GITHUB_REPOSITORY": "owner/repo"})
        self.assertEqual(json.loads((self.root / "dist" / "data.json").read_text(encoding="utf-8"))["repository"], "owner/repo")


@unittest.skipUnless(shutil.which("git"), "needs git")
class StateBranch(unittest.TestCase):
    """ci/state.sh against a real git remote, which is a folder here instead of GitHub."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.remote = self.root / "remote.git"
        self.git("init", "--quiet", "--bare", "-b", "main", str(self.remote), cwd=self.root)
        self.work = self.fresh_checkout("first")
        (self.work / "code.txt").write_text("code", encoding="utf-8")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t.test", "commit", "--quiet", "-m", "code")
        self.git("push", "--quiet", "origin", "HEAD:refs/heads/main")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.work), check=True, capture_output=True, text=True).stdout

    def fresh_checkout(self, name):
        path = self.root / name
        path.mkdir()
        self.git("init", "--quiet", "-b", "main", cwd=path)
        self.git("remote", "add", "origin", "file://%s" % self.remote, cwd=path)
        if self.remote.joinpath("refs/heads/main").exists() or "main" in self.git("ls-remote", "origin", cwd=path):
            self.git("fetch", "--quiet", "origin", "main", cwd=path)
            self.git("checkout", "--quiet", "FETCH_HEAD", cwd=path)
        return path

    def state(self, action, cwd=None, env=None):
        return subprocess.run(
            [str(REPO / "ci" / "state.sh"), action, ".state"],
            cwd=str(cwd or self.work), capture_output=True, text=True, env={**os.environ, **(env or {})},
        )

    def test_first_run_creates_the_branch_with_only_history_in_it(self):
        result = self.state("restore")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Starting a new history", result.stdout)
        (self.work / ".state" / "franky.sql").write_text("one", encoding="utf-8")
        result = self.state("save")
        self.assertEqual(result.returncode, 0, result.stderr)
        files = self.git("ls-tree", "-r", "--name-only", "scan-state", cwd=self.remote).split()
        self.assertEqual(files, ["franky.sql"])

    def test_a_new_runner_gets_the_history_back_and_adds_to_it(self):
        self.state("restore")
        (self.work / ".state" / "franky.sql").write_text("one", encoding="utf-8")
        self.state("save")
        runner = self.fresh_checkout("second")
        result = self.state("restore", cwd=runner)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((runner / ".state" / "franky.sql").read_text(encoding="utf-8"), "one")
        (runner / ".state" / "franky.sql").write_text("two", encoding="utf-8")
        result = self.state("save", cwd=runner)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "scan-state:franky.sql", cwd=self.remote), "two")
        self.assertEqual(len(self.git("log", "--oneline", "scan-state", cwd=self.remote).splitlines()), 2)

    def test_no_changes_means_no_commit(self):
        self.state("restore")
        (self.work / ".state" / "franky.sql").write_text("one", encoding="utf-8")
        self.state("save")
        result = self.state("save")
        self.assertIn("unchanged", result.stdout)
        self.assertEqual(len(self.git("log", "--oneline", "scan-state", cwd=self.remote).splitlines()), 1)

    def test_main_is_never_touched(self):
        before = self.git("rev-parse", "main", cwd=self.remote)
        self.state("restore")
        (self.work / ".state" / "franky.sql").write_text("one", encoding="utf-8")
        self.state("save")
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), before)

    def publish(self, files, cwd=None):
        site = self.root / "site-build"
        shutil.rmtree(str(site), ignore_errors=True)
        site.mkdir()
        for name, text in files.items():
            (site / name).write_text(text, encoding="utf-8")
        return subprocess.run([str(REPO / "ci" / "publish.sh"), str(site)], cwd=str(cwd or self.work), capture_output=True, text=True)

    def test_publishing_creates_the_pages_branch_with_only_the_site(self):
        result = self.publish({"index.html": "<p>one</p>", "data.json": "{}", ".nojekyll": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        files = self.git("ls-tree", "-r", "--name-only", "gh-pages", cwd=self.remote).split()
        self.assertEqual(sorted(files), [".nojekyll", "data.json", "index.html"])

    def test_publishing_replaces_the_branch_including_old_files(self):
        # Removes anything else committed there, such as a stray .DS_Store from Finder.
        self.publish({"index.html": "one", ".DS_Store": "junk"})
        result = self.publish({"index.html": "two"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "gh-pages", cwd=self.remote).split(), ["index.html"])
        self.assertEqual(self.git("show", "gh-pages:index.html", cwd=self.remote), "two")
        self.assertEqual(len(self.git("log", "--oneline", "gh-pages", cwd=self.remote).splitlines()), 1)

    def test_publishing_never_touches_main_or_the_history_branch(self):
        self.state("restore")
        (self.work / ".state" / "franky.sql").write_text("one", encoding="utf-8")
        self.state("save")
        main = self.git("rev-parse", "main", cwd=self.remote)
        history = self.git("rev-parse", "scan-state", cwd=self.remote)
        self.publish({"index.html": "site"})
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), main)
        self.assertEqual(self.git("rev-parse", "scan-state", cwd=self.remote), history)

    def test_publishing_leaves_the_checkout_alone(self):
        self.publish({"index.html": "site"})
        self.assertFalse((self.work / ".pages").exists())
        self.assertEqual((self.work / "code.txt").read_text(encoding="utf-8"), "code")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_publishing_refuses_a_missing_site(self):
        result = self.publish({"other.txt": "no page here"})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("gh-pages", self.git("ls-remote", "origin"))

    def test_unreachable_remote_fails_instead_of_starting_a_new_history(self):
        self.git("remote", "set-url", "origin", "file://%s/nope.git" % self.root)
        result = self.state("restore")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Starting a new history", result.stdout)

    def test_a_rejected_push_fails_loudly(self):
        self.state("restore")
        (self.work / ".state" / "franky.sql").write_text("one", encoding="utf-8")
        self.state("save")
        other = self.fresh_checkout("other")
        self.state("restore", cwd=other)
        (self.work / ".state" / "franky.sql").write_text("mine", encoding="utf-8")
        self.state("save")
        (other / ".state" / "franky.sql").write_text("theirs", encoding="utf-8")
        result = self.state("save", cwd=other)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("show", "scan-state:franky.sql", cwd=self.remote), "mine")


if __name__ == "__main__":
    unittest.main()
