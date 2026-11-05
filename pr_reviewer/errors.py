"""
Errors for the PR reviewer.

ReviewHalt is the core safety mechanism: any stage that cannot be completed with
certainty raises it, and the engine then reports the problem on the PR, sets the
build status to "failure" and NEVER merges. Nothing is guessed.
"""

from __future__ import annotations

from typing import Optional


class ReviewHalt(Exception):
    """A stage could not be completed safely. The PR must not be merged."""

    def __init__(self, stage: str, reason: str, hint: Optional[str] = None) -> None:
        super().__init__(f"[{stage}] {reason}")
        self.stage = stage
        self.reason = reason
        self.hint = hint


class PlatformError(Exception):
    """The Git platform, ticket system or LLM API returned an error."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ConfigError(Exception):
    """Required settings (environment variables) are missing or invalid."""
