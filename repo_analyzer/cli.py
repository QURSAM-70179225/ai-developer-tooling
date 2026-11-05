"""
Command-line entry point:  analyze <github-url> [--token TOKEN] [options]

Flow:
  1. parse + validate the URL (no network)
  2. run the fetch/analyze pipeline behind a live Rich progress display
  3. render the report — or a friendly error panel; never a raw traceback
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from . import __version__
from .analyzers import (
    RepoFiles,
    activity_from_dates,
    activity_from_stats,
    assess_dependency_health,
    build_tree,
    compute_complexity,
    dedupe_dependencies,
    describe_areas,
    detect_tech_stack,
    find_manifests,
    parse_github_url,
    parse_manifests,
    summarize_activity,
)
from .errors import (
    AuthenticationError,
    OutputFileError,
    EmptyRepositoryError,
    RateLimitError,
    RepoAnalyzerError,
)
from .github_client import GitHubClient

# Rich is imported lazily inside main() so that `--help` works and a missing
# dependency produces a clear message instead of an ImportError traceback.


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyze",
        description="Analyze a public GitHub repository: tech stack, structure, complexity, "
                    "dependency health and commit activity — right in your terminal.",
        epilog="Examples:\n  analyze https://github.com/fastapi/fastapi\n"
               "  analyze github.com/pallets/flask --token ghp_xxx --weeks 52\n"
               "  analyze vercel/next.js --json > report.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", help="GitHub repository URL (https://github.com/owner/repo, git@github.com:owner/repo.git, or owner/repo)")
    p.add_argument("--token", "-t", help="GitHub personal access token (default: $GITHUB_TOKEN or $GH_TOKEN)")
    p.add_argument("--depth", type=int, default=3, choices=range(1, 6), metavar="{1-5}", help="File tree depth to display (default 3)")
    p.add_argument("--max-children", type=int, default=10, metavar="N", help="Max entries shown per folder in the tree (default 10)")
    p.add_argument("--max-lines", type=int, default=60, metavar="N",
                   help="Condense the file tree to about N lines on big repos (default 60; 0 = no limit)")
    p.add_argument("--weeks", type=int, default=26, choices=range(4, 53), metavar="{4-52}", help="Weeks of commit history to chart (default 26)")
    p.add_argument("--no-preview", action="store_true", help="Hide the syntax-highlighted manifest preview")
    p.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of the TUI report")
    p.add_argument("--output", "-o", metavar="FILE",
                   help="Save the report (or --json) to FILE as UTF-8 plain text; errors are saved too. "
                        "Use this instead of > or | on Windows PowerShell")
    p.add_argument("--width", type=int, metavar="N",
                   help="Report width in columns (default: terminal width; 160 with --output)")
    p.add_argument("--no-color", action="store_true", help="Disable colors")
    p.add_argument("--debug", action="store_true", help="Show full tracebacks for unexpected errors")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


# --------------------------------------------------------------------------
# Pipeline (UI-agnostic: progress is reported through the `step` callback)
# --------------------------------------------------------------------------
def run_pipeline(owner: str, repo: str, client: GitHubClient, weeks: int, step: Callable[[str], None],
                 done: Callable[[str], None]) -> dict:
    warnings: List[str] = []

    # Pre-flight quota check (the /rate_limit endpoint is free).
    step("Checking GitHub API quota...")
    try:
        remaining, reset = client.check_rate_limit()
        if remaining is not None and remaining < 5:
            raise RateLimitError(f"Only {remaining} GitHub API request(s) left — a full analysis needs ~10-15.",
                                 reset_at=reset, authenticated=bool(client.token))
        done(f"API quota OK ({remaining} requests left)")
    except (RateLimitError, AuthenticationError):
        raise
    except RepoAnalyzerError:
        done("API quota check skipped")

    step(f"Fetching repository metadata for {owner}/{repo}...")
    meta = client.get_repo(owner, repo)
    branch = meta.get("default_branch") or "main"
    if meta.get("size", 1) == 0:
        raise EmptyRepositoryError(f"{owner}/{repo} has no content yet.")
    done(f"Found [bold]{meta.get('full_name')}[/] (default branch: {branch})")

    step("Fetching repository tree...")
    entries, truncated = client.get_tree(owner, repo, branch)
    files = RepoFiles.from_tree(entries)
    if not files.files:
        raise EmptyRepositoryError(f"{owner}/{repo} contains no files outside ignored folders.")
    done(f"Fetched tree: {len(files.files):,} files, {len(files.dirs):,} directories")

    step("Fetching language breakdown...")
    try:
        languages = client.get_languages(owner, repo)
        done(f"Languages: {', '.join(list(languages)[:5]) or 'none reported'}")
    except (AuthenticationError,):
        raise
    except RepoAnalyzerError as exc:
        languages = {}
        warnings.append(f"Language breakdown unavailable: {exc.message}")
        done("Language breakdown unavailable (using file extensions)")

    manifest_paths = find_manifests(files.files)
    manifests = {}
    if manifest_paths:
        step(f"Downloading {len(manifest_paths)} dependency manifest(s)...")
        for path in manifest_paths:
            try:
                text = client.get_file_text(owner, repo, path, branch)
                if text is not None:
                    manifests[path] = text
            except RepoAnalyzerError as exc:
                warnings.append(f"Could not download {path}: {exc.message}")
                if isinstance(exc, RateLimitError):
                    break
        done(f"Downloaded manifests: {', '.join(manifests) or 'none'}")
    else:
        done("No dependency manifests found")

    step("Analyzing dependencies...")
    deps, parse_warnings = parse_manifests(manifests)
    deps = dedupe_dependencies(deps)  # one consistent count everywhere in the report
    warnings.extend(parse_warnings)
    health = assess_dependency_health(files, deps, meta)
    done(f"Dependency health: {health.status}" + (f" ({len(deps)} unique deps parsed)" if deps else ""))

    step("Detecting tech stack...")
    tech = detect_tech_stack(files, languages, deps)
    done(f"Detected {len(tech)} technologies")

    step("Calculating complexity...")
    complexity = compute_complexity(files, languages, len(deps))
    tree = build_tree(files.files, files.dirs)
    areas = describe_areas(files, repo)
    done(f"Complexity score: {complexity.score}/100 ({complexity.label})")

    step("Fetching commit activity (GitHub may need a moment to compute stats)...")
    activity, activity_warning = None, None
    try:
        stats = client.get_weekly_commit_activity(owner, repo)
        if stats:
            activity = summarize_activity(activity_from_stats(stats, weeks), "stats/commit_activity")
        else:
            step("Stats not ready — counting recent commits instead...")
            since = datetime.now(timezone.utc) - timedelta(weeks=weeks)
            dates, capped = client.list_commit_dates(owner, repo, since, branch)
            activity = summarize_activity(activity_from_dates(dates, weeks), "commits list", capped)
        done(f"Commit activity: {activity.total:,} commits in {weeks} weeks")
    except RepoAnalyzerError as exc:
        activity_warning = f"Commit activity unavailable: {exc.message}"
        done("Commit activity unavailable")

    step("Fetching top contributors...")
    contributors, contributors_warning = None, None
    try:
        contributors = client.get_top_contributors(owner, repo)
        if contributors is None:
            contributors_warning = "GitHub declined to list contributors for this repository (history too large)."
            done("Top contributors unavailable (repository too large for GitHub to list)")
        else:
            done(f"Top contributors: {len(contributors)} fetched")
    except RepoAnalyzerError as exc:
        contributors_warning = f"Top contributors unavailable: {exc.message}"
        done("Top contributors unavailable")

    preview = None
    for path in manifest_paths:  # shallowest parsed manifest first
        if path in manifests:
            preview = (path, manifests[path])
            break

    return dict(meta=meta, files=files, truncated=truncated, languages=languages, deps=deps, health=health,
                tech=tech, complexity=complexity, tree=tree, areas=areas, activity=activity,
                activity_warning=activity_warning, contributors=contributors,
                contributors_warning=contributors_warning, preview=preview, warnings=warnings)


# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
        from . import render
    except ModuleNotFoundError as exc:  # pragma: no cover
        print(f"Missing dependency '{exc.name}'. Install with: pip install -r requirements.txt", file=sys.stderr)
        return 1

    # ---- consoles -------------------------------------------------------------
    # Screen console (stderr): spinner, step log, errors. Its width comes from the real
    # terminal, never from $COLUMNS, so the live spinner can always erase itself.
    err_console = render.make_console(no_color=args.no_color, stderr=True, width=_real_terminal_width())
    # Report console: the terminal, or a UTF-8 file when --output is given. Writing the file
    # ourselves avoids shell redirection, which on Windows PowerShell 5.1 re-encodes the
    # output (garbled box characters) and wraps stderr in "NativeCommandError" records.
    out_fh = None
    if args.output:
        try:
            out_fh = open(args.output, "w", encoding="utf-8", newline="\n")
        except OSError as exc:
            render.render_error(err_console, OutputFileError(f"Cannot write to '{args.output}': {exc.strerror}."))
            return OutputFileError.exit_code
        console = render.make_file_console(out_fh, width=args.width or 160)
    else:
        console = render.make_console(no_color=args.no_color, width=args.width)
    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    def fail(err: RepoAnalyzerError) -> int:
        render.render_error(err_console, err)
        if out_fh:  # the saved file should show the error too (useful as demo evidence)
            console.print(f"$ analyze {args.url}")
            render.render_error(console, err)
            err_console.print(f"[muted]Error also saved to {args.output}[/]")
        return err.exit_code

    try:
        owner, repo = parse_github_url(args.url)
        client = GitHubClient(token=token)

        progress = Progress(
            SpinnerColumn("dots", style="bright_cyan"),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=err_console,
            transient=True,
            disable=not err_console.is_terminal,
        )
        with progress:
            task = progress.add_task("Starting...", total=None)

            def step(msg: str) -> None:
                progress.update(task, description=f"[bright_cyan]{msg}")

            def done(msg: str) -> None:
                if not args.json:
                    progress.console.print(f"  [green]✓[/] {msg}")

            result = run_pipeline(owner, repo, client, args.weeks, step, done)

        rate = {"remaining": client.rate_remaining, "limit": client.rate_limit,
                "requests": client.requests_made, "authenticated": bool(token)}

        if args.json:
            text = json.dumps(_to_json(owner, repo, result, rate), indent=2, default=str)
            if out_fh:
                out_fh.write(text + "\n")
                err_console.print(f"[green]✓[/] JSON saved to [bold]{args.output}[/]")
            else:
                print(text)
            return 0

        report = make_report(owner, repo, result, rate, depth=args.depth, max_children=args.max_children,
                             max_lines=args.max_lines, preview=not args.no_preview)
        if out_fh:
            console.print(f"$ analyze {args.url}")
        console.print()
        render.render_report(console, report, result["activity_warning"])
        if out_fh:
            err_console.print(f"[green]✓[/] Report saved to [bold]{args.output}[/] "
                              f"[muted](UTF-8, {console.width} columns)[/]")
        return 0

    except RepoAnalyzerError as err:
        return fail(err)
    except KeyboardInterrupt:
        err_console.print("\n[warn]Aborted by user.[/]")
        return 130
    except Exception as exc:  # last-resort safety net: still no ugly trace unless --debug
        if args.debug:
            err_console.print_exception(show_locals=False)
            return 1
        e = RepoAnalyzerError(f"{exc.__class__.__name__}: {exc}",
                              hint="This looks like a bug. Re-run with --debug to see the full traceback and please report it.")
        e.title = "Unexpected error"
        fail(e)
        return 1
    finally:
        if out_fh:
            out_fh.close()


def make_report(owner: str, repo: str, result: dict, rate: dict, depth: int = 3, max_children: int = 10,
                max_lines: int = 60, preview: bool = True):
    """Turn run_pipeline()'s result into a render.Report. Shared by the CLI and the Streamlit app."""
    from . import render
    return render.Report(
        owner=owner, repo=repo, meta=result["meta"], tech=result["tech"], tree=result["tree"],
        complexity=result["complexity"], health=result["health"], activity=result["activity"],
        manifest_preview=result["preview"] if preview else None, warnings=result["warnings"],
        truncated_tree=result["truncated"], rate=rate, tree_depth=depth,
        tree_max_children=max(1, max_children), tree_max_lines=max_lines, areas=result["areas"],
        deps=result["deps"], contributors=result["contributors"],
        contributors_warning=result["contributors_warning"],
    )


def report_as_text(report, activity_warning=None, width: int = 160) -> str:
    """The full terminal report as plain UTF-8 text (used for --output-style downloads)."""
    import io
    from . import render
    buf = io.StringIO()
    render.render_report(render.make_file_console(buf, width=width), report, activity_warning)
    return buf.getvalue()


def _real_terminal_width() -> Optional[int]:
    """Width of the terminal attached to stderr, ignoring $COLUMNS (None if not a terminal)."""
    try:
        return os.get_terminal_size(sys.stderr.fileno()).columns
    except (OSError, ValueError, AttributeError):
        return None


def _to_json(owner: str, repo: str, r: dict, rate: dict) -> dict:
    act = r["activity"]
    return {
        "repository": f"{owner}/{repo}",
        "url": r["meta"].get("html_url"),
        "tech_stack": [asdict(t) for t in r["tech"]],
        "complexity": asdict(r["complexity"]),
        "dependency_health": asdict(r["health"]),
        "dependencies": [asdict(d) for d in r["deps"]],
        "commit_activity": None if act is None else {
            **{k: v for k, v in asdict(act).items() if k not in ("weeks", "peak")},
            "weeks": [{"week": w.date().isoformat(), "commits": c} for w, c in act.weeks],
        },
        "top_contributors": r["contributors"],
        "file_stats": r["complexity"].stats,
        "major_areas": [asdict(a) for a in r["areas"]],
        "tree_truncated": r["truncated"],
        "warnings": r["warnings"] + [w for w in (r["activity_warning"], r["contributors_warning"]) if w],
        "api": rate,
    }


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
