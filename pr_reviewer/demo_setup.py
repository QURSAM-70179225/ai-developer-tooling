"""
`pr-review setup-demo --repo you/pr-reviewer-test`

Fills an existing TEST repository with three demo pull requests, through the GitHub API
(no git needed), matching the tickets in examples/tickets.json:

  PR  branch                     ticket   expected result
  1   feature/DEMO-1-greeting    DEMO-1   MERGED   (meets every acceptance criterion)
  2   feature/DEMO-2-add         DEMO-2   BLOCKED  (no TypeError check, no tests/test_calc.py)
  3   patch-1                    (none)   HALTED   (no ticket ID -> never merged)

Safe to re-run: existing branches and open PRs are reused.
"""

from __future__ import annotations

import base64
from typing import Callable, Dict, List, Optional

from .errors import PlatformError

GREETING = '''"""Greeting helper for the welcome screen (ticket DEMO-1)."""


def greet(name: str) -> str:
    """Return 'Hello, <name>!'. Raises ValueError for an empty name."""
    if not name:
        raise ValueError("name must not be empty")
    return f"Hello, {name}!"
'''

GREETING_TEST = '''import unittest

from greeting import greet


class TestGreet(unittest.TestCase):
    def test_returns_greeting(self):
        self.assertEqual(greet("Qursam"), "Hello, Qursam!")

    def test_empty_name_raises(self):
        with self.assertRaises(ValueError):
            greet("")


if __name__ == "__main__":
    unittest.main()
'''

# Deliberately incomplete for DEMO-2: no TypeError check and no tests/test_calc.py.
CALC = '''"""Calculator helpers (ticket DEMO-2)."""


def add(a, b):
    return a + b
'''

NOTES = "# Project notes\n\nA small docs-only change made on a branch without a ticket ID.\n"

DEMOS = [
    {"branch": "feature/DEMO-1-greeting", "title": "DEMO-1 Greeting function", "expect": "MERGED",
     "files": {"greeting.py": GREETING, "tests/test_greeting.py": GREETING_TEST},
     "body": "Implements DEMO-1: greet(name) plus unit tests."},
    {"branch": "feature/DEMO-2-add", "title": "DEMO-2 Add two numbers", "expect": "BLOCKED",
     "files": {"calc.py": CALC}, "body": "Implements DEMO-2: add(a, b)."},
    {"branch": "patch-1", "title": "Update project notes", "expect": "HALTED (no ticket ID)",
     "files": {"NOTES.md": NOTES}, "body": "Docs-only change."},
]


def _base(platform, repo: str):
    """(default branch, its head sha), with friendly errors for a missing or empty repo."""
    call = platform._call
    try:
        info = call("GET", f"/repos/{repo}").json()
    except PlatformError as exc:
        if exc.status == 404:
            raise PlatformError(f"Repository '{repo}' not found. Create it first on github.com/new "
                                "(tick 'Add a README file'), and check the token can access it.", 404) from None
        raise
    base = info.get("default_branch") or "main"
    try:
        sha = call("GET", f"/repos/{repo}/git/ref/heads/{base}").json()["object"]["sha"]
    except PlatformError as exc:
        if exc.status in (404, 409):
            raise PlatformError(f"'{repo}' is empty. Add a README on github.com (or recreate it with "
                                "'Add a README file' ticked), then run the command again.", exc.status) from None
        raise
    return base, sha


def create_pr(platform, repo: str, branch: str, title: str, files: Dict[str, str], body: str = "",
              log: Callable[[str], None] = print, base_info=None) -> Dict:
    """Create (or reuse) `branch`, write `files` {repo_path: text} on it, and open (or reuse) a PR."""
    call = platform._call
    base, base_sha = base_info or _base(platform, repo)
    owner = repo.split("/")[0]
    try:
        call("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        log(f"created branch {branch}")
    except PlatformError as exc:
        if exc.status != 422:  # 422 = branch already exists -> reuse it
            raise
        log(f"branch {branch} already exists, reusing it")
    for path, text in files.items():
        existing = call("GET", f"/repos/{repo}/contents/{path}", ok=(200, 404), params={"ref": branch})
        sha: Optional[str] = existing.json().get("sha") if existing.status_code == 200 else None
        payload = {"message": f"{title.split(' ')[0]}: add {path}", "branch": branch,
                   "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        if sha:
            payload["sha"] = sha
        call("PUT", f"/repos/{repo}/contents/{path}", json=payload)
        log(f"  wrote {path} on {branch}")
    try:
        pr = call("POST", f"/repos/{repo}/pulls",
                  json={"title": title, "head": branch, "base": base, "body": body}).json()
        log(f"opened PR #{pr['number']}: {title}")
    except PlatformError as exc:
        if exc.status != 422:
            raise
        open_prs = call("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}).json()
        if not open_prs:
            log(f"no open PR for {branch} (already merged or closed); skipped")
            return {"number": None, "title": title, "branch": branch, "url": ""}
        pr = open_prs[0]
        log(f"PR #{pr['number']} already open for {branch}, reusing it")
    return {"number": pr["number"], "title": title, "branch": branch, "url": pr.get("html_url", "")}


def setup_demo(platform, repo: str, log: Callable[[str], None] = print) -> List[Dict]:
    base_info = _base(platform, repo)
    results = []
    for demo in DEMOS:
        r = create_pr(platform, repo, demo["branch"], demo["title"], demo["files"], demo["body"], log, base_info)
        r["expect"] = demo["expect"]
        results.append(r)
    return results
