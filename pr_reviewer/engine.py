"""
The controller: runs the five steps for one PR and decides what to do.

    ticket id → ticket + acceptance criteria → diff → LLM verdict → action

Decisions:
  merged    verdict fulfilled=true and the merge succeeded
  blocked   verdict fulfilled=false: REQUEST_CHANGES review (or comment) + status "failure"
  halted    something could not be determined safely: explanatory comment + status "failure"
In dry-run mode the same decisions are made, but actions are only printed ("would ...").
Merging happens in exactly one place, and only after a valid verdict with fulfilled == True.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import Settings
from .errors import PlatformError, ReviewHalt
from .git_platform import PREvent
from .llm import Verdict
from .tickets import Ticket, extract_ticket_id

FOOTER = "\n\n---\n<sub>🤖 AI PR Reviewer · automated review against the ticket's acceptance criteria</sub>"


@dataclass
class Outcome:
    decision: str                 # merged | blocked | halted
    pr: PREvent
    dry_run: bool
    ticket: Optional[Ticket] = None
    verdict: Optional[Verdict] = None
    stage: Optional[str] = None   # where a halt happened
    reason: Optional[str] = None
    hint: Optional[str] = None
    diff_chars: int = 0
    actions: List[str] = field(default_factory=list)
    action_errors: List[str] = field(default_factory=list)


def halt_comment(h: ReviewHalt) -> str:
    return (f"## 🤖 AI PR Review: ⚠️ review could not be completed\n\n"
            f"**This PR will not be merged automatically.**\n\n"
            f"- **Stage:** `{h.stage}`\n- **Problem:** {h.reason}\n"
            + (f"- **How to fix:** {h.hint}\n" if h.hint else "") + FOOTER)


def blocked_comment(ticket: Ticket, verdict: Verdict) -> str:
    link = f"[{ticket.id}]({ticket.url})" if ticket.url else ticket.id
    return (f"## 🤖 AI PR Review: ❌ acceptance criteria not met\n\n"
            f"Ticket: {link}: {ticket.title}\n\n{verdict.feedback}\n\n"
            f"Merging is blocked until the issues above are fixed and a new review passes." + FOOTER)


class Reviewer:
    def __init__(self, settings: Settings, platform, fetcher, llm, log: Callable[[str], None] = lambda m: None):
        self.s, self.platform, self.fetcher, self.llm, self.log = settings, platform, fetcher, llm, log

    # every write to the PR goes through here, so dry-run can never leak a real action
    def _act(self, out: Outcome, description: str, fn: Callable, *args) -> Optional[object]:
        if self.s.dry_run:
            out.actions.append(f"[dry-run] would {description}")
            self.log(f"[dry-run] would {description}")
            return None
        result = fn(*args)
        out.actions.append(description)
        self.log(description)
        return result

    def review(self, pr: PREvent) -> Outcome:
        out = Outcome("halted", pr, self.s.dry_run)
        ctx = self.s.status_context
        try:
            self._try(lambda: self._act(out, "set status 'pending'", self.platform.set_status,
                                        pr.repo, pr.head_sha, "pending", "AI review in progress", ctx), out)
            # 1-2. ticket
            ticket_id = extract_ticket_id(pr.title, pr.branch, self.s.ticket_source)
            self.log(f"ticket id: {ticket_id}")
            out.ticket = self.fetcher.fetch(ticket_id)
            self.log(f"ticket fetched: {out.ticket.title!r}")
            # 3. diff
            try:
                diff = self.platform.get_diff(pr.repo, pr.number)
            except PlatformError as exc:
                raise ReviewHalt("diff", f"Could not fetch the PR diff: {exc.message}") from None
            out.diff_chars = len(diff)
            if not diff.strip():
                raise ReviewHalt("diff", "The PR diff is empty: there is no code change to review.")
            if len(diff) > self.s.max_diff_chars:
                raise ReviewHalt("diff", f"The diff is too large to review in one request "
                                         f"({len(diff):,} characters > limit {self.s.max_diff_chars:,}).",
                                 "Split the PR into smaller PRs, or raise MAX_DIFF_CHARS if your model allows it.")
            self.log(f"diff fetched: {len(diff):,} characters")
            # 4. verdict (ReviewHalt on API failure or any malformed answer)
            out.verdict = self.llm.evaluate(out.ticket, diff)
            self.log(f"verdict: fulfilled={out.verdict.fulfilled}")
        except ReviewHalt as h:
            return self._halt(out, h)

        # 5. act on a valid verdict
        v, t = out.verdict, out.ticket
        if not v.fulfilled:
            out.decision = "blocked"
            self._try(lambda: self._act(out, "set status 'failure' (blocks merging)", self.platform.set_status,
                                        pr.repo, pr.head_sha, "failure", f"Acceptance criteria of {t.id} not met", ctx), out)
            self._try(lambda: self._act(out, "post REQUEST_CHANGES review with the feedback", self.platform.post_review,
                                        pr.repo, pr.number, blocked_comment(t, v), pr.head_sha), out)
            return out

        try:  # status first: branch protection may require this check before GitHub allows the merge
            self._act(out, "set status 'success'", self.platform.set_status,
                      pr.repo, pr.head_sha, "success", f"Acceptance criteria of {t.id} met", ctx)
            self._act(out, f"merge into '{pr.base}' ({self.s.merge_method}, head {pr.head_sha[:7]})",
                      self.platform.merge, pr.repo, pr.number, pr.head_sha, self.s.merge_method, pr.title)
            out.decision = "merged"
        except PlatformError as exc:
            out.action_errors.append(exc.message)
            hint = ("GitHub refused the merge, e.g. conflicts, a required check or review missing, or a new "
                    "commit pushed during the review (the merge is pinned to the reviewed commit).")
            return self._halt(out, ReviewHalt("merge", f"Verdict was fulfilled, but merging failed: {exc.message}", hint))
        if v.feedback:
            self._try(lambda: self._act(out, "post a summary comment", self.platform.post_comment, pr.repo, pr.number,
                                        f"## 🤖 AI PR Review: ✅ acceptance criteria met, merged\n\n{v.feedback}" + FOOTER), out)
        return out

    def _halt(self, out: Outcome, h: ReviewHalt) -> Outcome:
        out.decision, out.stage, out.reason, out.hint = "halted", h.stage, h.reason, h.hint
        self.log(f"HALT at {h.stage}: {h.reason}")
        pr = out.pr
        self._try(lambda: self._act(out, "set status 'failure' (blocks merging)", self.platform.set_status,
                                    pr.repo, pr.head_sha, "failure", f"Review incomplete: {h.stage}", self.s.status_context), out)
        self._try(lambda: self._act(out, "post a comment explaining what went wrong", self.platform.post_comment,
                                    pr.repo, pr.number, halt_comment(h)), out)
        return out

    @staticmethod
    def _try(fn: Callable, out: Outcome) -> None:
        """Run a non-merge action; record failures instead of crashing (merge is never retried here)."""
        try:
            fn()
        except PlatformError as exc:
            out.action_errors.append(exc.message)
