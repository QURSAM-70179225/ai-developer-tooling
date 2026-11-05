"""
Thin, defensive wrapper around the GitHub public REST API (v3).

Responsibilities:
  * attach auth / version headers,
  * translate every HTTP or network failure into a typed ``RepoAnalyzerError``,
  * track rate-limit headers so the report can show remaining quota,
  * hide GitHub quirks (202 "stats still computing", 409 "empty repo", raw file content).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from . import __version__
from .errors import (
    AccessDeniedError,
    AuthenticationError,
    EmptyRepositoryError,
    GitHubAPIError,
    NetworkError,
    RateLimitError,
    RepoNotFoundError,
)

API_ROOT = "https://api.github.com"
MAX_FILE_BYTES = 512 * 1024  # never pull huge manifests into memory


class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
        sleep=time.sleep,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self._sleep = sleep  # injectable so tests don't actually wait
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"repo-analyzer-cli/{__version__}",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

        # Rate-limit bookkeeping (updated from response headers).
        self.rate_limit: Optional[int] = None
        self.rate_remaining: Optional[int] = None
        self.rate_reset: Optional[datetime] = None
        self.requests_made = 0

    # ------------------------------------------------------------------ core
    def _request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        accept: Optional[str] = None,
        ok: Sequence[int] = (200,),
        not_found: Optional[Exception] = None,
    ) -> requests.Response:
        """GET ``path`` and return the response if its status is in ``ok``.

        Any other status is converted into a typed exception. ``not_found`` lets the
        caller choose what a 404 means (repo missing vs. a file that simply isn't there).
        """
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        headers = {"Accept": accept} if accept else None
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        except requests.exceptions.Timeout:
            raise NetworkError(f"GitHub did not respond within {self.timeout:.0f}s.") from None
        except requests.exceptions.SSLError as exc:
            raise NetworkError(f"TLS/SSL error talking to GitHub: {exc.__class__.__name__}.") from None
        except requests.exceptions.ConnectionError:
            raise NetworkError("Could not connect to api.github.com.") from None
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"Request failed: {exc.__class__.__name__}.") from None

        self.requests_made += 1
        self._track_rate_limit(resp)
        if resp.status_code in ok:
            return resp
        self._raise_for_status(resp, not_found)
        raise AssertionError("unreachable")  # pragma: no cover

    def _track_rate_limit(self, resp: requests.Response) -> None:
        h = resp.headers
        try:
            if "X-RateLimit-Limit" in h:
                self.rate_limit = int(h["X-RateLimit-Limit"])
            if "X-RateLimit-Remaining" in h:
                self.rate_remaining = int(h["X-RateLimit-Remaining"])
            if "X-RateLimit-Reset" in h:
                self.rate_reset = datetime.fromtimestamp(int(h["X-RateLimit-Reset"]), tz=timezone.utc)
        except (TypeError, ValueError):
            pass  # malformed headers are not worth crashing over

    @staticmethod
    def _message(resp: requests.Response) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
        except ValueError:
            pass
        return resp.reason or f"HTTP {resp.status_code}"

    def _raise_for_status(self, resp: requests.Response, not_found: Optional[Exception]) -> None:
        status = resp.status_code
        msg = self._message(resp)
        authed = bool(self.token)

        if status == 401:
            raise AuthenticationError(f"GitHub rejected the credentials ({msg}).")

        if status in (403, 429):
            is_primary = resp.headers.get("X-RateLimit-Remaining") == "0"
            is_rate_msg = "rate limit" in msg.lower()
            if is_primary or is_rate_msg or status == 429:
                reset = self.rate_reset
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():  # secondary (abuse) limit
                    reset = datetime.now(timezone.utc) + timedelta(seconds=int(retry_after))
                raise RateLimitError(f"GitHub says: {msg}", reset_at=reset, authenticated=authed)
            raise AccessDeniedError(f"GitHub refused the request ({msg}).")

        if status == 404:
            raise not_found or RepoNotFoundError(f"GitHub returned 404 for {resp.url}.")
        if status == 409:
            raise EmptyRepositoryError(f"GitHub says: {msg}")
        if status == 451:
            raise AccessDeniedError("This repository is unavailable for legal reasons (HTTP 451).")
        if status >= 500:
            raise GitHubAPIError(f"GitHub server error {status}: {msg}")
        raise GitHubAPIError(f"Unexpected HTTP {status}: {msg}")

    def _json(self, resp: requests.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise GitHubAPIError("GitHub returned a response that is not valid JSON.") from None

    # --------------------------------------------------------------- helpers
    def check_rate_limit(self) -> Tuple[Optional[int], Optional[datetime]]:
        """Query /rate_limit (free: does not count against the quota)."""
        resp = self._request("/rate_limit")
        core = self._json(resp).get("resources", {}).get("core", {})
        self.rate_limit = core.get("limit", self.rate_limit)
        self.rate_remaining = core.get("remaining", self.rate_remaining)
        if core.get("reset"):
            self.rate_reset = datetime.fromtimestamp(int(core["reset"]), tz=timezone.utc)
        return self.rate_remaining, self.rate_reset

    # ------------------------------------------------------------- endpoints
    def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        resp = self._request(
            f"/repos/{owner}/{repo}",
            not_found=RepoNotFoundError(f"Could not find github.com/{owner}/{repo} (HTTP 404)."),
        )
        return self._json(resp)

    def get_tree(self, owner: str, repo: str, ref: str) -> Tuple[List[Dict[str, Any]], bool]:
        """Return (entries, truncated) for the full recursive tree of ``ref``."""
        resp = self._request(
            f"/repos/{owner}/{repo}/git/trees/{ref}",
            params={"recursive": "1"},
            not_found=EmptyRepositoryError(f"Branch '{ref}' has no file tree (the repository may be empty)."),
        )
        data = self._json(resp)
        return data.get("tree", []) or [], bool(data.get("truncated"))

    def get_languages(self, owner: str, repo: str) -> Dict[str, int]:
        resp = self._request(f"/repos/{owner}/{repo}/languages")
        data = self._json(resp)
        return {k: int(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def get_file_text(self, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
        """Raw text of a file, or None if missing / too large / binary."""
        resp = self._request(
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
            accept="application/vnd.github.raw+json",
            ok=(200, 404),
        )
        if resp.status_code == 404 or len(resp.content) > MAX_FILE_BYTES:
            return None
        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def get_weekly_commit_activity(self, owner: str, repo: str, retries: int = 3) -> Optional[List[Dict[str, Any]]]:
        """Last 52 weeks of commit counts from /stats/commit_activity.

        GitHub computes this lazily and answers 202 while it works; we retry a few
        times, then return None so the caller can fall back to listing commits.
        """
        for attempt in range(retries + 1):
            resp = self._request(f"/repos/{owner}/{repo}/stats/commit_activity", ok=(200, 202, 204))
            if resp.status_code == 200:
                data = self._json(resp)
                return data if isinstance(data, list) and data else None
            if resp.status_code == 204:  # empty repo
                return []
            if attempt < retries:
                self._sleep(1.5 * (attempt + 1))
        return None

    def get_top_contributors(self, owner: str, repo: str, limit: int = 10) -> Optional[List[Dict[str, Any]]]:
        """Top contributors by all-time commit count (one request).

        Returns [] for an empty repo (204) and None when GitHub refuses to compute the
        list (403 "history or contributor list is too large" on giant repos).
        """
        resp = self._request(f"/repos/{owner}/{repo}/contributors",
                             params={"per_page": limit, "anon": "false"}, ok=(200, 204, 403))
        if resp.status_code == 204:
            return []
        if resp.status_code == 403:
            if resp.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in self._message(resp).lower():
                self._raise_for_status(resp, None)  # a real rate-limit error, not "too large"
            return None
        data = self._json(resp)
        out = []
        for c in data if isinstance(data, list) else []:
            login = c.get("login") or c.get("name") or "(unknown)"
            out.append({"login": login, "commits": int(c.get("contributions") or 0),
                        "bot": c.get("type") == "Bot" or login.endswith("[bot]")})
        return out

    def list_commit_dates(
        self, owner: str, repo: str, since: datetime, ref: Optional[str] = None, max_pages: int = 5
    ) -> Tuple[List[datetime], bool]:
        """Fallback: commit timestamps since ``since``. Returns (dates, capped)."""
        dates: List[datetime] = []
        params: Dict[str, Any] = {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "per_page": 100}
        if ref:
            params["sha"] = ref
        for page in range(1, max_pages + 1):
            params["page"] = page
            resp = self._request(f"/repos/{owner}/{repo}/commits", params=params, ok=(200, 409))
            if resp.status_code == 409:
                return [], False
            batch = self._json(resp)
            if not isinstance(batch, list) or not batch:
                return dates, False
            for item in batch:
                raw = ((item.get("commit") or {}).get("committer") or {}).get("date") or (
                    (item.get("commit") or {}).get("author") or {}
                ).get("date")
                if raw:
                    try:
                        dates.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
                    except ValueError:
                        continue
            if len(batch) < 100:
                return dates, False
        return dates, True
