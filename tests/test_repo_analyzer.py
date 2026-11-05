"""Offline test-suite: run with  `python -m unittest discover tests -v`.

GitHub is replaced by a FakeSession, so no network or token is needed.
"""

import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

from repo_analyzer import analyzers as A
from repo_analyzer import cli
from repo_analyzer.errors import (
    AuthenticationError, InvalidURLError, NetworkError, RateLimitError, RepoNotFoundError,
)
from repo_analyzer.github_client import GitHubClient


# ------------------------------------------------------------------ helpers
def make_response(status=200, body=None, headers=None, url="https://api.github.com/x", raw=None):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.headers.update(headers or {})
    r._content = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    return r


class FakeSession(requests.Session):
    """Routes GET requests by URL path suffix to canned responses."""

    def __init__(self, routes):
        super().__init__()
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                out = resp(url, params) if callable(resp) else resp
                if isinstance(out, Exception):
                    raise out
                return out
        return make_response(404, {"message": "Not Found"}, url=url)


NOW = datetime.now(timezone.utc)
RL = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "55", "X-RateLimit-Reset": str(int(time.time()) + 3000)}

TREE = [
    {"path": "README.md", "type": "blob", "size": 100},
    {"path": "package.json", "type": "blob", "size": 300},
    {"path": "package-lock.json", "type": "blob", "size": 9000},
    {"path": "tsconfig.json", "type": "blob", "size": 50},
    {"path": "Dockerfile", "type": "blob", "size": 50},
    {"path": ".github", "type": "tree"},
    {"path": ".github/workflows", "type": "tree"},
    {"path": ".github/workflows/ci.yml", "type": "blob", "size": 40},
    {"path": ".github/dependabot.yml", "type": "blob", "size": 40},
    {"path": "src", "type": "tree"},
    {"path": "src/index.ts", "type": "blob", "size": 400},
    {"path": "src/components", "type": "tree"},
    {"path": "src/components/App.tsx", "type": "blob", "size": 400},
    {"path": "src/components/Button.tsx", "type": "blob", "size": 400},
    {"path": "node_modules/react/index.js", "type": "blob", "size": 1},
    {"path": "api", "type": "tree"},
    {"path": "api/requirements.txt", "type": "blob", "size": 40},
    {"path": "api/main.py", "type": "blob", "size": 500},
]
PACKAGE_JSON = json.dumps({"dependencies": {"react": "^18.2.0", "next": "14.1.0", "left-pad": "*"},
                           "devDependencies": {"typescript": "~5.3.0", "jest": "^29.0.0"}})
REQS = "fastapi==0.110.0\nuvicorn>=0.27\npydantic\n# comment\n"


def default_routes(**overrides):
    stats = [{"week": int((NOW - timedelta(weeks=52 - i)).timestamp()), "total": (i * 7) % 13, "days": [0] * 7}
             for i in range(52)]
    routes = {
        "/rate_limit": make_response(200, {"resources": {"core": {"limit": 60, "remaining": 55,
                                                                   "reset": int(time.time()) + 3000}}}),
        "/repos/acme/web": make_response(200, {
            "full_name": "acme/web", "default_branch": "main", "size": 120, "description": "Demo repo",
            "stargazers_count": 1234, "forks_count": 56, "open_issues_count": 7, "license": {"spdx_id": "MIT"},
            "created_at": "2021-01-01T00:00:00Z", "pushed_at": (NOW - timedelta(days=3)).isoformat(),
            "html_url": "https://github.com/acme/web", "topics": ["react", "fastapi"]}, RL),
        "/repos/acme/web/git/trees/main": make_response(200, {"tree": TREE, "truncated": False}, RL),
        "/repos/acme/web/languages": make_response(200, {"TypeScript": 90000, "Python": 20000, "Dockerfile": 300}, RL),
        "/contents/package.json": make_response(200, raw=PACKAGE_JSON.encode(), headers=RL),
        "/contents/api/requirements.txt": make_response(200, raw=REQS.encode(), headers=RL),
        "/stats/commit_activity": make_response(200, stats, RL),
        "/repos/acme/web/contributors": make_response(200, [
            {"login": "alice", "contributions": 420, "type": "User"},
            {"login": "bob", "contributions": 130, "type": "User"},
            {"login": "dependabot[bot]", "contributions": 60, "type": "Bot"}], RL),
    }
    routes.update(overrides)
    return routes


def run_cli(argv, routes):
    session = FakeSession(routes)
    real_init = GitHubClient.__init__

    def patched(self, token=None, timeout=15.0, session_=None, sleep=None):
        real_init(self, token=token, timeout=timeout, session=session, sleep=lambda s: None)

    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(GitHubClient, "__init__", patched), redirect_stdout(out), redirect_stderr(err), \
            mock.patch.dict("os.environ", {"COLUMNS": "130", "GITHUB_TOKEN": "", "GH_TOKEN": ""}, clear=False):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue(), session


# -------------------------------------------------------------------- tests
class TestURLParsing(unittest.TestCase):
    def test_valid_forms(self):
        for url in ["https://github.com/fastapi/fastapi", "http://www.github.com/fastapi/fastapi/",
                    "https://github.com/fastapi/fastapi.git", "github.com/fastapi/fastapi",
                    "https://github.com/fastapi/fastapi/tree/master/docs", "git@github.com:fastapi/fastapi.git",
                    "fastapi/fastapi"]:
            self.assertEqual(A.parse_github_url(url), ("fastapi", "fastapi"), url)

    def test_invalid(self):
        for url in ["", "https://gitlab.com/a/b", "https://github.com/fastapi", "not a url at all",
                    "ftp://github.com/a/b", "https://github.com/orgs/foo", "https://github.com/a/b$c",
                    "https://gist.github.com/a/b"]:
            with self.assertRaises(InvalidURLError, msg=url):
                A.parse_github_url(url)


class TestAnalyzers(unittest.TestCase):
    def setUp(self):
        self.files = A.RepoFiles.from_tree(TREE)
        deps = A.parse_manifests({"package.json": PACKAGE_JSON, "api/requirements.txt": REQS})
        self.deps, self.warn = deps

    def test_ignores_node_modules(self):
        self.assertFalse(any("node_modules" in f for f in self.files.files))

    def test_dependency_parsing(self):
        pins = {d.name: d.pin for d in self.deps}
        self.assertEqual(pins["next"], "exact")
        self.assertEqual(pins["react"], "range")
        self.assertEqual(pins["left-pad"], "unpinned")
        self.assertEqual(pins["fastapi"], "exact")
        self.assertEqual(pins["pydantic"], "unpinned")
        self.assertEqual(self.warn, [])

    def test_tech_stack_has_evidence(self):
        tech = {t.name: t for t in A.detect_tech_stack(self.files, {"TypeScript": 9, "Python": 1}, self.deps)}
        for name in ["TypeScript", "React", "Next.js", "FastAPI", "Docker", "GitHub Actions", "Jest"]:
            self.assertIn(name, tech)
            self.assertTrue(tech[name].evidence)
        self.assertIn("package.json → react@^18.2.0", tech["React"].evidence)

    def test_complexity_bounds(self):
        c = A.compute_complexity(self.files, {"TypeScript": 9, "Python": 1}, len(self.deps))
        self.assertTrue(0 <= c.score <= 100)
        self.assertEqual(len(c.components), 5)
        big = A.RepoFiles(files=[f"a/b/c/d/e/f{i}.py" for i in range(20000)], dirs=[])
        self.assertLessEqual(A.compute_complexity(big, {"Python": 1}, 5000).score, 100)
        empty = A.RepoFiles(files=["README.md"], dirs=[])
        self.assertEqual(A.compute_complexity(empty, {}, 0).score, 0)

    def test_health(self):
        h = A.assess_dependency_health(self.files, self.deps, {"pushed_at": NOW.isoformat()})
        self.assertIn(h.status, {"Healthy", "Warning", "At Risk"})
        self.assertIn("pip", h.ecosystems)
        self.assertTrue(any(c.status == "fail" and "pip" in c.text for c in h.checks))  # no pip lockfile

    def test_health_no_manifests(self):
        files = A.RepoFiles(files=["README.md", "notes.txt"], dirs=[])
        h = A.assess_dependency_health(files, [], {})
        self.assertEqual(h.status, "No Dependencies Found")
        self.assertIsNone(h.score)

    def test_archived_is_at_risk(self):
        files = A.RepoFiles(files=["package.json"], dirs=[])
        h = A.assess_dependency_health(files, [], {"archived": True, "pushed_at": "2019-01-01T00:00:00Z"})
        self.assertEqual(h.status, "At Risk")

    def test_malformed_manifest_does_not_crash(self):
        deps, warnings = A.parse_manifests({"package.json": "{not json"})
        self.assertEqual(deps, [])
        self.assertEqual(len(warnings), 1)

    def test_activity_from_dates(self):
        dates = [NOW - timedelta(days=d) for d in (0, 1, 8, 8, 40)]
        weeks = A.activity_from_dates(dates, 8, now=NOW)
        self.assertEqual(len(weeks), 8)
        self.assertEqual(sum(c for _, c in weeks), 5)
        s = A.summarize_activity(weeks, "test")
        self.assertEqual(s.total, 5)


class TestFixes(unittest.TestCase):
    def test_pep735_dependency_groups(self):
        text = ('[project]\nname="x"\ndependencies=["starlette>=0.46"]\n'
                '[dependency-groups]\ndev=["pytest>=8.0", {include-group="lint"}]\nlint=["ruff==0.6.0"]\n')
        deps, warnings = A.parse_manifests({"pyproject.toml": text})
        by_name = {d.name: d for d in deps}
        self.assertEqual(warnings, [])
        self.assertTrue(by_name["pytest"].dev)
        self.assertEqual(by_name["ruff"].pin, "exact")
        files = A.RepoFiles(files=["pyproject.toml", "app/main.py"], dirs=["app"])
        tech = {t.name: t for t in A.detect_tech_stack(files, {"Python": 1}, deps)}
        self.assertIn("pyproject.toml → pytest@>=8.0 (dev)", tech["pytest"].evidence)

    def big_tree(self):
        files = []
        for top in ["docs", "docs_src", "fastapi", "scripts", "tests"]:
            for sub in range(40):
                for f in range(6):
                    files.append(f"{top}/sub{sub:02d}/deep/file{f}.py")
                files.append(f"{top}/sub{sub:02d}/mod.py")
        files += ["README.md", "pyproject.toml"]
        return A.build_tree(files)

    def test_tree_is_condensed_but_keeps_all_top_level(self):
        from rich.console import Console
        from repo_analyzer import render
        tree = self.big_tree()
        d, n, condensed = render.fit_tree(tree, 3, 10, 60)
        self.assertTrue(condensed)
        self.assertLessEqual(render.count_tree_lines(tree, d, 10, n), 60)
        report = mock.Mock(repo="big", tree=tree, tree_depth=3, tree_max_children=10, tree_max_lines=60,
                           truncated_tree=False, areas=[], complexity=mock.Mock(stats={
                               "total_files": 1, "directories": 1, "code_files": 1, "config_files": 0,
                               "doc_files": 0, "other_files": 0}))
        console = Console(record=True, width=120, file=io.StringIO())
        console.print(render.render_file_tree(report))
        text = console.export_text()
        for top in ["docs/", "docs_src/", "fastapi/", "scripts/", "tests/", "README.md"]:
            self.assertIn(top, text)
        self.assertIn("Condensed to fit 60 lines", text)

    def test_tree_cap_disabled_and_small_repo_untouched(self):
        from repo_analyzer import render
        self.assertFalse(render.fit_tree(self.big_tree(), 3, 10, 0)[2])
        small = A.build_tree(["a/b.py", "c.py"])
        self.assertEqual(render.fit_tree(small, 3, 10, 60), (3, 10, False))


class TestMajorAreas(unittest.TestCase):
    """Gap 1: purpose of each major area, with the basis shown and no guessing."""

    def areas(self):
        files = (["docs/en/index.md"] * 0 + [f"docs/{l}/p{i}.md" for l in ("en", "de") for i in range(20)]
                 + [f"docs_src/ex{i}.py" for i in range(10)]
                 + [f"fastapi/m{i}.py" for i in range(8)]
                 + [f"tests/test_{i}.py" for i in range(12)]
                 + [".github/workflows/ci.yml", ".github/dependabot.yml"]
                 + ["assetsish/a.py", "assetsish/b.md", "assetsish/c.json", "assetsish/d.png"]   # mixed -> unknown
                 + ["notes/a.md", "notes/b.md", "notes/c.md"]                                   # unknown name, 100% docs
                 + ["README.md", "LICENSE", "pyproject.toml", "uv.lock"])
        return {a.name: a for a in A.describe_areas(A.RepoFiles(files=files, dirs=[]), "fastapi")}

    def test_basis_for_each_kind_of_folder(self):
        a = self.areas()
        self.assertEqual((a["docs"].purpose, a["docs"].basis), ("Documentation", "name"))
        self.assertEqual(a["docs_src"].basis, "name")
        self.assertEqual(a["tests"].purpose, "Automated tests")
        self.assertEqual(a["fastapi"].basis, "repo name")
        self.assertIn("CI workflows", a[".github"].purpose)
        self.assertEqual((a["notes"].purpose, a["notes"].basis), ("Mostly documentation / text files", "contents"))
        self.assertEqual(a["assetsish"].basis, "unknown")
        self.assertIn("not determined", a["assetsish"].purpose)

    def test_counts_types_and_root_summary(self):
        a = self.areas()
        self.assertEqual(a["docs"].files, 40)
        self.assertEqual(a["docs"].top_exts[0], (".md", 40))
        self.assertEqual(a["(root files)"].files, 4)
        self.assertIn("README.md", a["(root files)"].purpose)
        self.assertNotIn("CONTRIBUTING", a["(root files)"].purpose)  # only names that really exist

    def test_hyphenated_repo_name_matches_package(self):
        files = A.RepoFiles(files=["repo_analyzer/cli.py", "repo_analyzer/x.py"], dirs=[])
        self.assertEqual(A.describe_areas(files, "repo-analyzer")[0].basis, "repo name")

    def test_area_limit(self):
        files = A.RepoFiles(files=[f"d{i}/f.py" for i in range(20)], dirs=[])
        areas = A.describe_areas(files, "x", limit=5)
        self.assertEqual(len(areas), 6)
        self.assertTrue(areas[-1].name.startswith("… 15 more"))


class TestRemainingGaps(unittest.TestCase):
    """Gaps 2-4: method/definitions in the report, dev-only labels + dependency table, contributors."""

    PYPROJECT = ('[project]\nname="x"\ndependencies=["starlette>=0.46", "anyio"]\n'
                 '[project.optional-dependencies]\nstandard=["uvicorn>=0.12"]\n'
                 '[dependency-groups]\ntests=["flask>=3.0.0,<4.0.0", "pytest>=8"]\n')

    def test_scopes(self):
        deps, _ = A.parse_manifests({"pyproject.toml": self.PYPROJECT})
        scope = {d.name: d.scope for d in deps}
        self.assertEqual(scope, {"starlette": "runtime", "anyio": "runtime", "uvicorn": "optional",
                                 "flask": "dev", "pytest": "dev"})
        npm, _ = A.parse_manifests({"package.json": PACKAGE_JSON})
        self.assertEqual({d.name: d.scope for d in npm}["jest"], "dev")

    def test_flask_in_test_group_is_labelled_dev_only(self):
        deps, _ = A.parse_manifests({"pyproject.toml": self.PYPROJECT})
        files = A.RepoFiles(files=["pyproject.toml", "x/main.py"], dirs=[])
        tech = {t.name: t for t in A.detect_tech_stack(files, {"Python": 1}, deps)}
        self.assertTrue(tech["Flask"].dev_only)
        self.assertIn("pyproject.toml → flask@>=3.0.0,<4.0.0 (dev)", tech["Flask"].evidence)
        self.assertFalse(tech["Starlette"].dev_only)
        self.assertFalse(tech["Uvicorn"].dev_only)  # optional extra, still shipped to users

    def test_dependency_table(self):
        from repo_analyzer import render
        deps, _ = A.parse_manifests({"pyproject.toml": self.PYPROJECT})
        deps = deps + deps  # duplicates must be collapsed
        buf = io.StringIO()
        console = render.make_file_console(buf, width=140)  # plain text: no ANSI codes on any OS
        console.print(render.render_dependencies(deps, max_rows=3))
        out = buf.getvalue()
        self.assertIn("UNPINNED", out)              # anyio has no version -> always listed
        self.assertIn("Showing 3 of 5 unique", out)
        self.assertIn("--json", out)

    def test_full_report_has_method_definitions_and_contributors(self):
        code, out, err, _ = run_cli(["acme/web"], default_routes())
        self.assertEqual(code, 0, err)
        for text in ["Rule (what earns", "Main reasons:", "Method: score = sum of 5", 'What "healthy" means',
                     "Not checked:", "📦 Dependencies", "Top contributors", "alice", "dependabot[bot]",
                     "Contribution Activity", "dev/test only"]:
            self.assertIn(text, out)

    def test_contributors_too_large_is_reported_not_crashed(self):
        routes = default_routes(**{"/repos/acme/web/contributors": make_response(
            403, {"message": "The history or contributor list is too large to list contributors"})})
        code, out, err, _ = run_cli(["acme/web"], routes)
        self.assertEqual(code, 0, err)
        self.assertIn("declined to list contributors", out)

    def test_contributors_empty(self):
        code, out, err, _ = run_cli(["acme/web"], default_routes(**{"/repos/acme/web/contributors": make_response(204)}))
        self.assertEqual(code, 0, err)
        self.assertIn("No contributors found", out)

    def test_json_has_contributors_and_scopes(self):
        code, out, err, _ = run_cli(["acme/web", "--json"], default_routes())
        data = json.loads(out)
        self.assertEqual(data["top_contributors"][0]["login"], "alice")
        self.assertIn("scope", data["dependencies"][0])


class TestConsistency(unittest.TestCase):
    """Every count in the report must agree (found reviewing the real FastAPI demo)."""

    def test_duplicates_collapse_and_runtime_scope_wins(self):
        text = ('[project]\ndependencies=["pydantic>=2.9.0"]\n'
                '[dependency-groups]\ntests=["pydantic>=2.9.0", "pytest>=8"]\nlint=["pytest>=8"]\n')
        deps, _ = A.parse_manifests({"pyproject.toml": text})
        self.assertEqual(len(deps), 4)
        unique = {d.name: d for d in A.dedupe_dependencies(deps)}
        self.assertEqual(len(unique), 2)
        self.assertEqual(unique["pydantic"].scope, "runtime")

    def test_different_versions_are_kept(self):
        text = '[dependency-groups]\na=["typer>=0.21.1"]\nb=["typer>=0.26.1"]\n'
        deps, _ = A.parse_manifests({"pyproject.toml": text})
        self.assertEqual(len(A.dedupe_dependencies(deps)), 2)

    def test_report_counts_agree(self):
        dup = PACKAGE_JSON  # react etc.; add a duplicated requirements file entry
        routes = default_routes(**{"/contents/api/requirements.txt": make_response(
            200, raw=(REQS + "fastapi==0.110.0\n").encode(), headers=RL)})
        code, out, err, _ = run_cli(["acme/web"], routes)
        self.assertEqual(code, 0, err)
        self.assertIn("8 declared deps", out)          # complexity
        self.assertIn("Declared deps: 8", out)         # health
        self.assertIn("Showing 8 of 8 unique", out)    # dependency table
        self.assertIn("8 unique deps parsed", err)


class TestOutputFile(unittest.TestCase):
    """--output writes clean UTF-8 itself (no shell redirection), for reports, JSON and errors."""

    def setUp(self):
        import tempfile
        global cell_len
        from rich.cells import cell_len  # display width, as the terminal shows it
        self.dir = tempfile.mkdtemp()

    def path(self, name):
        import os
        return os.path.join(self.dir, name)

    def read(self, name):
        with open(self.path(name), encoding="utf-8") as fh:
            return fh.read()

    def test_report_saved_as_plain_utf8(self):
        code, out, err, _ = run_cli(["acme/web", "--output", self.path("r.txt")], default_routes())
        self.assertEqual(code, 0, err)
        text = self.read("r.txt")
        self.assertTrue(text.startswith("$ analyze acme/web"))
        for section in ["🧰 Tech Stack", "📦 Dependencies", "🌳 File Structure", "Top contributors", "╭─"]:
            self.assertIn(section, text)
        self.assertNotIn("\x1b[", text)          # no ANSI colour codes
        self.assertEqual(out, "")                 # nothing went to stdout
        self.assertIn("Report saved to", err)
        self.assertLessEqual(max(cell_len(l) for l in text.splitlines()), 160)

    def test_error_saved_to_file_with_exit_code(self):
        code, out, err, _ = run_cli(["https://gitlab.com/foo/bar", "-o", self.path("e.txt")], default_routes())
        self.assertEqual(code, 2)
        text = self.read("e.txt")
        self.assertIn("Invalid GitHub URL", text)
        self.assertIn("$ analyze https://gitlab.com/foo/bar", text)
        self.assertIn("Invalid GitHub URL", err)  # still shown on screen

    def test_not_found_saved_to_file(self):
        code, _, err, _ = run_cli(["acme/missing", "-o", self.path("nf.txt")], default_routes())
        self.assertEqual(code, 3)
        self.assertIn("Repository not found", self.read("nf.txt"))

    def test_json_to_file(self):
        code, out, err, _ = run_cli(["acme/web", "--json", "-o", self.path("r.json")], default_routes())
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.read("r.json"))["repository"], "acme/web")

    def test_unwritable_path_is_a_friendly_error(self):
        code, _, err, _ = run_cli(["acme/web", "-o", self.path("no/such/dir/r.txt")], default_routes())
        self.assertEqual(code, 9)
        self.assertIn("Cannot save the report", err)
        self.assertNotIn("Traceback", err)

    def test_custom_width(self):
        code, _, err, _ = run_cli(["acme/web", "-o", self.path("w.txt"), "--width", "120"], default_routes())
        self.assertEqual(code, 0, err)
        self.assertLessEqual(max(cell_len(l) for l in self.read("w.txt").splitlines()), 120)


class TestClientErrors(unittest.TestCase):
    def client(self, resp, token=None):
        return GitHubClient(token=token, session=FakeSession({"/repos/a/b": resp}), sleep=lambda s: None)

    def test_404(self):
        with self.assertRaises(RepoNotFoundError):
            self.client(make_response(404, {"message": "Not Found"})).get_repo("a", "b")

    def test_rate_limit(self):
        resp = make_response(403, {"message": "API rate limit exceeded for 1.2.3.4."},
                             {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 600)})
        with self.assertRaises(RateLimitError) as ctx:
            self.client(resp).get_repo("a", "b")
        self.assertIn("--token", ctx.exception.hint)

    def test_bad_token(self):
        with self.assertRaises(AuthenticationError):
            self.client(make_response(401, {"message": "Bad credentials"}), token="x").get_repo("a", "b")

    def test_network(self):
        with self.assertRaises(NetworkError):
            self.client(requests.exceptions.ConnectionError("boom")).get_repo("a", "b")
        with self.assertRaises(NetworkError):
            self.client(requests.exceptions.Timeout("slow")).get_repo("a", "b")


class TestEndToEnd(unittest.TestCase):
    def test_full_report(self):
        code, out, err, _ = run_cli(["https://github.com/acme/web"], default_routes())
        self.assertEqual(code, 0, err)
        for section in ["Tech Stack", "File Structure", "Complexity Score", "Dependency Health",
                        "Contribution Activity", "Manifest Preview", "React", "FastAPI", "Major areas",
                        "Main source code", "(by name)"]:
            self.assertIn(section, out)
        self.assertNotIn("Traceback", out + err)

    def test_stats_202_falls_back_to_commits(self):
        commits = [{"commit": {"committer": {"date": (NOW - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")}}}
                   for i in range(0, 30, 2)]
        routes = default_routes(**{
            "/stats/commit_activity": make_response(202, {}),
            "/repos/acme/web/commits": make_response(200, commits),
        })
        code, out, err, _ = run_cli(["acme/web"], routes)
        self.assertEqual(code, 0, err)
        self.assertIn("commits list", out)

    def test_json_output(self):
        code, out, err, _ = run_cli(["acme/web", "--json"], default_routes())
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["repository"], "acme/web")
        self.assertIn(data["dependency_health"]["status"], {"Healthy", "Warning", "At Risk"})

    def test_no_dependency_files(self):
        routes = default_routes(**{
            "/repos/acme/web/git/trees/main": make_response(200, {"tree": [
                {"path": "README.md", "type": "blob"}, {"path": "notes/a.txt", "type": "blob"}]}),
        })
        code, out, err, _ = run_cli(["acme/web"], routes)
        self.assertEqual(code, 0, err)
        self.assertIn("NO DEPENDENCIES FOUND", out)

    def test_invalid_url_exit_code(self):
        code, out, err, _ = run_cli(["https://example.com/foo/bar"], default_routes())
        self.assertEqual(code, 2)
        self.assertIn("Invalid GitHub URL", err)

    def test_not_found_exit_code(self):
        code, out, err, _ = run_cli(["acme/missing"], default_routes())
        self.assertEqual(code, 3)
        self.assertIn("Repository not found", err)
        self.assertNotIn("Traceback", err)

    def test_rate_limited_preflight(self):
        routes = default_routes(**{"/rate_limit": make_response(200, {"resources": {"core": {
            "limit": 60, "remaining": 0, "reset": int(time.time()) + 900}}})})
        code, out, err, _ = run_cli(["acme/web"], routes)
        self.assertEqual(code, 5)
        self.assertIn("rate limit", err.lower())

    def test_rate_limit_mid_run_on_optional_section(self):
        limited = make_response(403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0"})
        routes = default_routes(**{"/stats/commit_activity": limited})
        code, out, err, _ = run_cli(["acme/web"], routes)
        self.assertEqual(code, 0, err)  # report still renders, with the section marked unavailable
        self.assertIn("Commit activity unavailable", out)


if __name__ == "__main__":
    unittest.main()
