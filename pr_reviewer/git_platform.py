"""
Component 1b (Code Fetcher) and Component 3 (Action Executor) for GitHub.

  get_diff     raw unified diff of the PR      (Accept: application/vnd.github.diff)
  set_status   commit status "failure"/"success" (with branch protection requiring the
               "ai-pr-reviewer" check, "failure" blocks the Merge button)
  post_review  PR Review API, REQUEST_CHANGES with the feedback; falls back to a main PR
               comment when GitHub refuses (you can't request changes on your own PR)
  merge        Merge API, pinned to the reviewed head SHA so a newer push is never merged
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

from .errors import PlatformError

API = "https://api.github.com"
RELEVANT_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review", "edited"}


@dataclass
class PREvent:
    platform: str
    repo: str          # owner/name
    number: int
    title: str
    branch: str        # head ref, e.g. feature/JIRA-123-login-page
    base: str          # target branch
    head_sha: str
    url: str = ""
    author: str = ""


def parse_github_event(event_type: str, payload: Dict[str, Any]) -> Tuple[Optional[PREvent], str]:
    """Return (event, "") for a PR worth reviewing, or (None, reason it was ignored)."""
    if event_type == "ping":
        return None, "ping received, webhook is connected"
    if event_type != "pull_request":
        return None, f"ignored event type '{event_type}'"
    action = payload.get("action")
    if action not in RELEVANT_ACTIONS:
        return None, f"ignored pull_request action '{action}'"
    pr = payload.get("pull_request") or {}
    if pr.get("state") != "open":
        return None, "pull request is not open"
    if pr.get("draft"):
        return None, "pull request is a draft"
    try:
        return PREvent(
            platform="github",
            repo=payload["repository"]["full_name"],
            number=int(pr["number"]),
            title=pr.get("title") or "",
            branch=(pr.get("head") or {}).get("ref") or "",
            base=(pr.get("base") or {}).get("ref") or "",
            head_sha=(pr.get("head") or {})["sha"],
            url=pr.get("html_url") or "",
            author=(pr.get("user") or {}).get("login") or "",
        ), ""
    except (KeyError, TypeError, ValueError):
        return None, "payload is missing pull request fields"


class GitHubPlatform:
    name = "github"

    def __init__(self, token: Optional[str], session: Optional[requests.Session] = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28",
                                     "User-Agent": "ai-pr-reviewer/1.0"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _call(self, method: str, path: str, ok=(200, 201), **kw) -> requests.Response:
        try:
            resp = self.session.request(method, f"{API}{path}", timeout=30, **kw)
        except requests.RequestException as exc:
            raise PlatformError(f"Could not reach GitHub: {exc.__class__.__name__}.") from None
        if resp.status_code not in ok:
            try:
                msg = resp.json().get("message") or resp.reason
            except ValueError:
                msg = resp.reason or ""
            text = f"GitHub {method} {path} → HTTP {resp.status_code}: {msg}"
            needed = resp.headers.get("X-Accepted-GitHub-Permissions")
            if resp.status_code == 403 and needed:  # GitHub says exactly which token permission is missing
                text += (f"\nThis token needs: {needed.replace('=', ': ')} on this repository. Edit the token at "
                         "github.com/settings/personal-access-tokens (Repository access must include this repo), "
                         "or check that $env:GITHUB_TOKEN holds the new token.")
            raise PlatformError(text, resp.status_code)
        return resp

    # -------------------------------------------------------------- reading
    def get_pull(self, repo: str, number: int) -> PREvent:
        pr = self._call("GET", f"/repos/{repo}/pulls/{number}").json()
        return PREvent("github", repo, int(pr["number"]), pr.get("title") or "", pr["head"]["ref"],
                       pr["base"]["ref"], pr["head"]["sha"], pr.get("html_url") or "",
                       (pr.get("user") or {}).get("login") or "")

    def get_diff(self, repo: str, number: int) -> str:
        resp = self._call("GET", f"/repos/{repo}/pulls/{number}",
                          headers={"Accept": "application/vnd.github.diff"})
        return resp.text

    # -------------------------------------------------------------- actions
    def set_status(self, repo: str, sha: str, state: str, description: str, context: str) -> None:
        self._call("POST", f"/repos/{repo}/statuses/{sha}",
                   json={"state": state, "description": description[:140], "context": context})

    def post_comment(self, repo: str, number: int, body: str) -> None:
        self._call("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    def post_review(self, repo: str, number: int, body: str, sha: str) -> str:
        """REQUEST_CHANGES review; returns 'review' or 'comment' (fallback) for the log."""
        try:
            self._call("POST", f"/repos/{repo}/pulls/{number}/reviews",
                       json={"body": body, "event": "REQUEST_CHANGES", "commit_id": sha})
            return "review"
        except PlatformError as exc:
            if exc.status != 422:  # 422 = e.g. "Can not request changes on your own pull request"
                raise
            self.post_comment(repo, number, body)
            return "comment"

    def merge(self, repo: str, number: int, sha: str, method: str, title: str) -> str:
        resp = self._call("PUT", f"/repos/{repo}/pulls/{number}/merge",
                          json={"sha": sha, "merge_method": method, "commit_title": f"{title} (#{number})"})
        return (resp.json() or {}).get("sha", "")
