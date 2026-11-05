"""
pr-review: command line for the AI PR Reviewer.

  pr-review serve  [--port 8000]                 webhook listener (POST /webhook)
  pr-review review --repo owner/name --pr 12     review one PR now (same engine, no webhook needed)
  pr-review replay event.json                    replay a saved GitHub webhook payload

Dry-run is the DEFAULT: decisions are printed, nothing is posted or merged.
Add --live to really comment, set statuses and merge (use a TEST repository).
  pr-review setup-demo --repo owner/name        create the 3 demo PRs in a TEST repository
  pr-review make-pr --repo R --branch B --title T --file LOCAL[=REPO_PATH]   open a PR from local files

Exit codes: 0 review completed (merged or blocked) · 2 halted (nothing merged) · 3 configuration problem.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from rich.console import Console, Group
try:  # renders the LLM's markdown feedback nicely; plain text if unavailable
    from rich.markdown import Markdown
except ImportError:  # pragma: no cover
    Markdown = None
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import Settings
from .engine import Outcome, Reviewer
from .errors import ConfigError, PlatformError
from .git_platform import GitHubPlatform, parse_github_event
from .llm import LLMClient
from .tickets import make_fetcher

console = Console(highlight=False)
STYLE = {"merged": ("✅ MERGED", "green"), "blocked": ("❌ BLOCKED: requirements not met", "red"),
         "halted": ("⚠️  HALTED: not merged", "yellow")}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pr-review", description="AI PR Reviewer (Task 4, Track A)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="really post comments/statuses and merge")
    mode.add_argument("--dry-run", action="store_true", help="only print the actions (default)")
    p.add_argument("--output", "-o", metavar="FILE",
                   help="also save everything printed to FILE as UTF-8 text (demo evidence; use instead of > on Windows)")
    # The same flags are also accepted AFTER the subcommand ("pr-review review ... --live").
    # SUPPRESS keeps a flag given before the subcommand from being reset by the subparser.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    cmode = common.add_mutually_exclusive_group()
    cmode.add_argument("--live", action="store_true")
    cmode.add_argument("--dry-run", action="store_true")
    common.add_argument("--output", "-o", metavar="FILE")
    sub = p.add_subparsers(dest="command", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **kw: _add(*a, parents=[common], **kw)  # type: ignore[assignment]
    s = sub.add_parser("serve", help="run the webhook listener")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-verify", action="store_true", help="accept unsigned webhooks (local testing only)")
    r = sub.add_parser("review", help="review one pull request now")
    r.add_argument("--repo", required=True, help="owner/name of a TEST repository")
    r.add_argument("--pr", type=int, required=True, help="pull request number")
    e = sub.add_parser("replay", help="replay a saved GitHub webhook payload (JSON file)")
    e.add_argument("file")
    e.add_argument("--event", default="pull_request", help="X-GitHub-Event value (default pull_request)")
    d = sub.add_parser("setup-demo", help="create the 3 demo PRs in an existing TEST repository")
    d.add_argument("--repo", required=True, help="owner/name of an existing TEST repository (with a README)")
    m = sub.add_parser("make-pr", help="open a PR in a TEST repository from local file(s)")
    m.add_argument("--repo", required=True, help="owner/name of an existing TEST repository")
    m.add_argument("--branch", required=True, help="new branch name; include the ticket ID, e.g. feature/CFG-1-user-controller")
    m.add_argument("--title", required=True, help="PR title; include the ticket ID, e.g. 'CFG-1 User controller'")
    m.add_argument("--file", action="append", required=True, metavar="LOCAL[=REPO_PATH]",
                   help="file to put in the PR; repeatable. REPO_PATH defaults to the file name")
    m.add_argument("--body", default="", help="PR description")
    return p


def render_outcome(o: Outcome) -> Panel:
    label, color = STYLE[o.decision]
    if o.dry_run:
        label = f"{label}  (dry-run: nothing was posted or merged)"
    info = Table.grid(padding=(0, 2))
    info.add_column(style="bold")
    info.add_column()
    info.add_row("Pull request", f"{o.pr.repo}#{o.pr.number}: {o.pr.title}")
    info.add_row("Branch", f"{o.pr.branch} → {o.pr.base}")
    if o.ticket:
        info.add_row("Ticket", f"{o.ticket.id}: {o.ticket.title}")
    if o.diff_chars:
        info.add_row("Diff", f"{o.diff_chars:,} characters")
    if o.stage:
        info.add_row("Halted at", Text(o.stage, style="yellow"))
        info.add_row("Problem", o.reason or "")
        if o.hint:
            info.add_row("How to fix", Text(o.hint, style="grey70"))
    parts = [Text(label, style=f"bold {color}"), Text(), info]
    if o.verdict:
        parts += [Text(), Text(f"LLM verdict: fulfilled = {str(o.verdict.fulfilled).lower()}", style="bold"),
                  Markdown(o.verdict.feedback) if Markdown else Text(o.verdict.feedback)]
    if o.actions:
        parts += [Text(), Text("Actions", style="bold")] + [Text(f"  • {a}") for a in o.actions]
    if o.action_errors:
        parts += [Text("Action errors", style="bold red")] + [Text(f"  • {a}", style="red") for a in o.action_errors]
    return Panel(Group(*parts), title="🤖 AI PR Review", border_style=color, padding=(1, 2))


def _preflight(settings: Settings, serving: bool) -> None:
    missing = settings.missing(serving)
    if missing:
        raise ConfigError("Missing environment variables: " + ", ".join(missing))


def main(argv: Optional[List[str]] = None) -> int:
    global console
    args = build_parser().parse_args(argv)
    if args.live and args.dry_run:
        build_parser().error("--live and --dry-run cannot be used together")
    if args.output:  # record everything printed, saved as UTF-8 at the end
        console = Console(highlight=False, record=True, width=max(100, console.width))
        console.print(f"$ pr-review {' '.join(argv if argv is not None else sys.argv[1:])}")
    try:
        return _main(args)
    finally:
        if args.output:
            try:
                with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(console.export_text())
                Console(stderr=True, highlight=False).print(f"[green]✓[/] Output saved to [bold]{args.output}[/]")
            except OSError as exc:
                Console(stderr=True).print(f"[red]Could not save output to {args.output}: {exc.strerror}[/]")


def _main(args) -> int:
    try:
        settings = Settings.from_env()
        if args.live:
            settings.dry_run = False
        elif args.dry_run:
            settings.dry_run = True
        if args.command == "serve" and args.no_verify:
            settings.webhook_secret = None
        if args.command in ("setup-demo", "make-pr"):
            if not settings.github_token:
                raise ConfigError("Missing environment variables: GITHUB_TOKEN")
        else:
            _preflight(settings, serving=args.command == "serve" and not args.no_verify)
    except ConfigError as exc:
        console.print(Panel(f"{exc}\n\nSee README → \"Track A: Automated PR Reviewer\" for every variable.",
                            title="[bold red]Configuration problem[/]", border_style="red"))
        return 3

    platform = GitHubPlatform(settings.github_token)
    if args.command == "setup-demo":
        return _setup_demo(platform, args.repo)
    if args.command == "make-pr":
        return _make_pr(platform, args)
    reviewer = Reviewer(settings, platform, make_fetcher(settings), LLMClient(settings),
                        log=lambda m: console.print(f"  [grey50]·[/] {m}"))
    mode = "[yellow]DRY-RUN[/] (nothing is posted or merged)" if settings.dry_run else "[bold red]LIVE[/] (comments, statuses and merges are real)"
    console.print(f"🤖 AI PR Reviewer · ticket source: {settings.ticket_source} · LLM: "
                  f"{settings.llm_provider}/{settings.llm_model} · mode: {mode}")

    try:
        if args.command == "serve":
            from .server import Dispatcher, make_server

            def handle(event):
                console.print(f"\n[bold]→ reviewing {event.repo}#{event.number}[/] ({event.branch})")
                console.print(render_outcome(reviewer.review(event)))

            server = make_server(args.host, args.port, settings.webhook_secret, Dispatcher(handle))
            console.print(f"Listening on http://{args.host}:{args.port}/webhook  (Ctrl+C to stop)")
            if not settings.webhook_secret:
                console.print("[yellow]⚠ signature verification is OFF (--no-verify): local testing only[/]")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                console.print("\nStopped.")
            return 0

        if args.command == "review":
            event = platform.get_pull(args.repo, args.pr)
        else:
            with open(args.file, encoding="utf-8") as fh:
                payload = json.load(fh)
            event, reason = parse_github_event(args.event, payload)
            if event is None:
                console.print(f"[yellow]Nothing to review: {reason}[/]")
                return 0
        outcome = reviewer.review(event)
        console.print(render_outcome(outcome))
        return 2 if outcome.decision == "halted" else 0
    except PlatformError as exc:
        console.print(Panel(exc.message, title="[bold red]GitHub error[/]", border_style="red"))
        return 3
    except (OSError, ValueError) as exc:
        console.print(Panel(f"{exc.__class__.__name__}: {exc}", title="[bold red]Could not read input[/]",
                            border_style="red"))
        return 3


def _setup_demo(platform, repo: str) -> int:
    from .demo_setup import setup_demo
    console.print(f"Setting up demo pull requests in [bold]{repo}[/] ...")
    try:
        results = setup_demo(platform, repo, log=lambda m: console.print(f"  [grey50]·[/] {m}"))
    except PlatformError as exc:
        console.print(Panel(exc.message, title="[bold red]Demo setup failed[/]", border_style="red"))
        return 3
    t = Table(title="Demo pull requests", header_style="bold")
    t.add_column("PR", no_wrap=True)
    t.add_column("Branch", no_wrap=True)
    t.add_column("Title")
    t.add_column("Expected result")
    for r in results:
        t.add_row(f"#{r['number']}" if r["number"] else "-", r["branch"], r["title"], r["expect"])
    console.print(t)
    console.print("\n[bold]Next:[/] review each PR, first as a dry-run, then add [bold]--live[/] at the end:")
    for r in results:
        if r["number"]:
            console.print(f"  pr-review review --repo {repo} --pr {r['number']}", soft_wrap=True, highlight=False)
    return 0


def _make_pr(platform, args) -> int:
    import os
    from .demo_setup import create_pr
    files = {}
    for spec in args.file:
        local, _, target = spec.partition("=")
        try:
            with open(local, encoding="utf-8") as fh:
                files[(target or os.path.basename(local)).replace("\\", "/")] = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            console.print(Panel(f"Could not read '{local}': {exc}", title="[bold red]make-pr failed[/]", border_style="red"))
            return 3
    console.print(f"Opening a PR in [bold]{args.repo}[/] from {len(files)} file(s) ...")
    try:
        r = create_pr(platform, args.repo, args.branch, args.title, files, args.body,
                      log=lambda msg: console.print(f"  [grey50]·[/] {msg}"))
    except PlatformError as exc:
        console.print(Panel(exc.message, title="[bold red]make-pr failed[/]", border_style="red"))
        return 3
    if r["number"]:
        console.print(f"\n[bold]Next:[/] pr-review review --repo {args.repo} --pr {r['number']}", soft_wrap=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
