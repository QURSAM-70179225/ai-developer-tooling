"""
Typed exceptions for Repo Analyzer.

Every failure the user can reasonably hit is mapped to one of these classes.
The CLI catches ``RepoAnalyzerError`` and renders it as a friendly Rich panel
(title + message + hint) with a distinct exit code, so the tool never ends in
a raw stack trace.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


class RepoAnalyzerError(Exception):
    """Base class for all expected, user-facing errors."""

    title: str = "Error"
    hint: Optional[str] = None
    exit_code: int = 1

    def __init__(self, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint


class InvalidURLError(RepoAnalyzerError):
    title = "Invalid GitHub URL"
    hint = "Use a URL like https://github.com/<owner>/<repo>  (e.g. https://github.com/fastapi/fastapi)."
    exit_code = 2


class RepoNotFoundError(RepoAnalyzerError):
    title = "Repository not found"
    hint = (
        "Check the spelling of the owner and repository name. GitHub also returns 404 for "
        "private repositories you can't see. If it's private, pass --token with a token that has access."
    )
    exit_code = 3


class AccessDeniedError(RepoAnalyzerError):
    title = "Access denied"
    hint = "The repository may be private, blocked, or your token lacks the 'repo' / 'Contents: read' permission."
    exit_code = 3


class AuthenticationError(RepoAnalyzerError):
    title = "Authentication failed"
    hint = "Your GitHub token was rejected. Check that it is correct and not expired, or run without --token."
    exit_code = 4


class RateLimitError(RepoAnalyzerError):
    title = "GitHub API rate limit reached"
    exit_code = 5

    def __init__(self, message: str, reset_at: Optional[datetime] = None, authenticated: bool = False) -> None:
        self.reset_at = reset_at
        if reset_at is not None:
            mins = max(0, int((reset_at - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
            local = reset_at.astimezone().strftime("%H:%M")
            when = f"The limit resets at {local} (in about {mins} min)."
        else:
            when = "Wait a few minutes and try again."
        if authenticated:
            hint = when
        else:
            hint = (
                f"{when} Unauthenticated requests are limited to 60/hour. Pass --token <github_token> "
                "(or set GITHUB_TOKEN) to raise the limit to 5,000/hour."
            )
        super().__init__(message, hint)


class EmptyRepositoryError(RepoAnalyzerError):
    title = "Repository is empty"
    hint = "This repository has no commits yet, so there is nothing to analyze."
    exit_code = 6


class NetworkError(RepoAnalyzerError):
    title = "Network error"
    hint = "Check your internet connection, proxy or firewall settings, then try again."
    exit_code = 7


class GitHubAPIError(RepoAnalyzerError):
    title = "GitHub API error"
    hint = "GitHub returned an unexpected response. Try again shortly; check https://www.githubstatus.com if it persists."
    exit_code = 8


class OutputFileError(RepoAnalyzerError):
    title = "Cannot save the report"
    hint = "Check the folder exists and the file isn't open in another program (e.g. Excel or Notepad), then try again."
    exit_code = 9
