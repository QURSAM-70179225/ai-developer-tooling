"""
Rich rendering for the analysis report. Purely presentational: it receives a
``Report`` and turns each section into Panels / Tables / Trees / Syntax.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.tree import Tree

from .analyzers import (
    ActivityResult,
    ComplexityResult,
    HealthResult,
    TechFinding,
    TreeNode,
    parse_iso,
    sorted_children,
)
from .errors import RepoAnalyzerError

THEME = Theme(
    {
        "title": "bold bright_cyan",
        "muted": "grey62",
        "accent": "bold magenta",
        "ok": "bold green",
        "warn": "bold yellow",
        "bad": "bold red",
        "dir": "bold blue",
        "file": "white",
        "label": "bold white",
    }
)

CATEGORY_STYLE = {
    "Language": "bright_cyan", "Framework": "magenta", "Library": "orchid", "AI / ML": "bright_magenta",
    "Database & ORM": "dark_orange", "Runtime & Build": "green", "Package Manager": "spring_green3",
    "Testing": "yellow", "DevOps & CI": "deep_sky_blue1", "Tooling": "grey70",
}
FILE_ICONS = {"code": "📄", "config": "⚙️ ", "doc": "📝", "other": "📦"}


@dataclass
class Report:
    owner: str
    repo: str
    meta: Dict
    tech: List[TechFinding]
    tree: TreeNode
    complexity: ComplexityResult
    health: HealthResult
    activity: Optional[ActivityResult]
    manifest_preview: Optional[tuple] = None   # (path, text)
    warnings: List[str] = field(default_factory=list)
    truncated_tree: bool = False
    rate: Dict = field(default_factory=dict)
    tree_depth: int = 3
    tree_max_children: int = 10
    tree_max_lines: int = 60
    areas: List = field(default_factory=list)   # List[AreaSummary]
    deps: List = field(default_factory=list)    # List[Dependency]
    contributors: Optional[List[Dict]] = None   # [{"login", "commits", "bot"}]
    contributors_warning: Optional[str] = None


def make_console(no_color: bool = False, stderr: bool = False, width: Optional[int] = None) -> Console:
    return Console(theme=THEME, no_color=no_color, highlight=False, stderr=stderr, width=width)


def make_file_console(fh, width: int = 160) -> Console:
    """Plain-text (no ANSI colour codes) console that writes the report to an open UTF-8 file."""
    return Console(file=fh, theme=THEME, highlight=False, width=width, color_system=None,
                   force_terminal=False, legacy_windows=False, soft_wrap=False)


# ------------------------------------------------------------------ header
def render_header(r: Report) -> Panel:
    m = r.meta
    title = Text.assemble(("📦 ", ""), (m.get("full_name", f"{r.owner}/{r.repo}"), "title"))
    if m.get("archived"):
        title.append("  [ARCHIVED]", style="bad")
    if m.get("fork"):
        title.append("  [fork]", style="muted")

    desc = Text(m.get("description") or "No description provided.", style="italic")

    stats = Table.grid(padding=(0, 3))
    for _ in range(4):
        stats.add_column()
    license_name = (m.get("license") or {}).get("spdx_id") or "None"
    created, pushed = parse_iso(m.get("created_at")), parse_iso(m.get("pushed_at"))
    stats.add_row(
        f"⭐ [label]{m.get('stargazers_count', 0):,}[/] stars",
        f"🍴 [label]{m.get('forks_count', 0):,}[/] forks",
        f"🐛 [label]{m.get('open_issues_count', 0):,}[/] open issues",
        f"👀 [label]{m.get('subscribers_count', m.get('watchers_count', 0)):,}[/] watchers",
    )
    stats.add_row(
        f"🌿 branch [label]{m.get('default_branch', '?')}[/]",
        f"📜 license [label]{license_name}[/]",
        f"🗓  created [label]{created:%Y-%m-%d}[/]" if created else "🗓  created ?",
        f"🚀 pushed [label]{_ago(pushed)}[/]" if pushed else "🚀 pushed ?",
    )
    topics = m.get("topics") or []
    parts: List[RenderableType] = [title, desc, Text(), stats]
    if topics:
        parts.append(Text("  ".join(f"#{t}" for t in topics[:10]), style="cyan"))
    parts.append(Text(m.get("html_url", ""), style="link " + m.get("html_url", "") if m.get("html_url") else "muted"))
    return Panel(Group(*parts), box=box.HEAVY, border_style="bright_cyan", padding=(1, 2),
                 title="[title]Repo Analyzer[/]", subtitle="[muted]GitHub public REST API[/]")


def _ago(dt: datetime) -> str:
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    if days < 730:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


# -------------------------------------------------------------- tech stack
def render_tech_stack(tech: List[TechFinding]) -> Panel:
    if not tech:
        return Panel("[muted]No languages, frameworks or tooling could be identified.[/]",
                     title="🧰 Tech Stack", border_style="magenta")
    table = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold magenta", show_edge=False)
    table.add_column("Technology", style="bold", no_wrap=True, ratio=2)
    table.add_column("Category", no_wrap=True, ratio=2)
    table.add_column("Evidence", style="muted", ratio=6)
    last_cat = None
    for f in tech:
        if last_cat is not None and f.category != last_cat:
            table.add_section()
        style = CATEGORY_STYLE.get(f.category, "white")
        evidence = Text()
        for i, ev in enumerate(f.evidence):
            if i:
                evidence.append("\n")
            evidence.append("• ", style=style)
            evidence.append(ev, style="grey70" if not ev.startswith("…") else "italic grey50")
        name = Text(f.name, style=f"bold {style}")
        if f.dev_only:
            name.append("\n dev/test only", style="italic grey50")
        table.add_row(name, Text(f.category, style=style), evidence)
        last_cat = f.category
    note = Text("Evidence = the file or manifest entry that proves each item. \"dev/test only\" = found only in "
                "development/test dependencies, so it is a tool used to build or test the project, not part of what it ships.",
                style="italic grey50")
    return Panel(Group(table, note), title="🧰 Tech Stack [muted](with evidence)[/]", border_style="magenta", padding=(0, 1))


# ----------------------------------------------------------- file structure
def count_tree_lines(node: TreeNode, depth_limit: int, top_limit: int, nested_limit: int, depth: int = 1) -> int:
    """Number of lines walk() would print for these limits (mirrors its logic exactly)."""
    children = sorted_children(node)
    shown = children[: top_limit if depth == 1 else nested_limit]
    lines = len(shown) + (1 if len(children) > len(shown) else 0)
    for child in shown:
        if child.is_dir and child.children and depth < depth_limit:
            lines += count_tree_lines(child, depth_limit, top_limit, nested_limit, depth + 1)
    return lines


def fit_tree(tree: TreeNode, depth: int, max_children: int, max_lines: int):
    """Pick (depth, nested_limit, condensed) so the tree fits in ``max_lines``.

    Top-level entries always keep ``max_children`` (they matter most). We first
    shrink how many entries nested folders show, then drop a level, and repeat.
    ``max_lines <= 0`` disables the cap.
    """
    if max_lines <= 0 or count_tree_lines(tree, depth, max_children, max_children) <= max_lines:
        return depth, max_children, False
    roomy = [n for n in (max_children, 8, 6, 5, 4) if n <= max_children] or [max_children]
    tight = [n for n in (3, 2) if n < min(roomy)]
    # Prefer readable folders (>= 4 entries) even at the cost of a level; squeeze to 3/2 only as a last resort.
    for options in (roomy, tight):
        for d in range(depth, 0, -1):
            for n in options:
                if count_tree_lines(tree, d, max_children, n) <= max_lines:
                    return d, n, True
    return 1, (tight or roomy)[-1], True  # even depth 1 is too big: show top level only


def render_areas(areas) -> Table:
    """'Major areas' table: one row per top-level folder with size, types and purpose."""
    table = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False, header_style="bold blue",
                  title="Major areas", title_style="bold", title_justify="left")
    table.add_column("Folder", style="dir", no_wrap=True)
    table.add_column("Files", justify="right")
    table.add_column("Main file types", style="grey70")
    table.add_column("Purpose")
    basis_style = {"name": "grey50", "repo name": "grey50", "contents": "grey50", "unknown": "yellow"}
    for a in areas:
        types = " · ".join(f"{ext} {n:,}" for ext, n in a.top_exts) or "—"
        folder = a.name if a.name.startswith(("(", "…")) else a.name + "/"
        purpose = Text(a.purpose, style="yellow" if a.basis == "unknown" else "white")
        if a.basis != "unknown":
            purpose.append(f"  (by {a.basis})", style=basis_style[a.basis])
        table.add_row(folder, f"{a.files:,}", types, purpose)
    return table


def render_file_tree(r: Report) -> Panel:
    from .analyzers import classify  # local import keeps render deps explicit

    root = Tree(Text(f"📁 {r.repo}/", style="dir"), guide_style="grey50")
    depth_limit, nested_limit, condensed = fit_tree(r.tree, r.tree_depth, r.tree_max_children, r.tree_max_lines)

    def walk(node: TreeNode, branch: Tree, depth: int, prefix: str) -> None:
        children = sorted_children(node)
        shown = children[: r.tree_max_children if depth == 1 else nested_limit]
        for child in shown:
            path = f"{prefix}{child.name}"
            if child.is_dir:
                label = Text.assemble(("📁 ", ""), (child.name + "/", "dir"), (f"  {child.file_count:,} file{'s' if child.file_count != 1 else ''}", "muted"))
                sub = branch.add(label)
                if depth < depth_limit and child.children:
                    walk(child, sub, depth + 1, path + "/")
                # Beyond the depth limit the "N files" label already summarises the folder.
            else:
                kind = classify(path)
                style = {"code": "file", "config": "yellow", "doc": "green"}.get(kind, "grey70")
                branch.add(Text.assemble((FILE_ICONS.get(kind, "📄") + " ", ""), (child.name, style)))
        hidden = len(children) - len(shown)
        if hidden > 0:
            n_dirs = sum(1 for c in children[len(shown):] if c.is_dir)
            n_files = hidden - n_dirs
            branch.add(Text(f"… {hidden} more ({n_dirs} dir{'s' if n_dirs != 1 else ''}, "
                            f"{n_files} file{'s' if n_files != 1 else ''})", style="italic grey50"))

    walk(r.tree, root, 1, "")
    s = r.complexity.stats
    footer = Text.assemble(
        (f"{int(s['total_files']):,} file{'s' if s['total_files'] != 1 else ''}", "label"), " · ",
        (f"{int(s['directories']):,} dir{'s' if s['directories'] != 1 else ''}", "label"), "   ",
        ("📄 code ", ""), (f"{int(s['code_files']):,}", "bold"), "  ",
        ("⚙️  config ", ""), (f"{int(s['config_files']):,}", "bold yellow"), "  ",
        ("📝 docs ", ""), (f"{int(s['doc_files']):,}", "bold green"), "  ",
        ("📦 other ", ""), (f"{int(s['other_files']):,}", "bold grey70"),
    )
    if condensed:
        shown_txt = (f"Condensed to fit {r.tree_max_lines} lines: {depth_limit} level(s), {r.tree_max_children} top-level "
                     f"/ {nested_limit} nested entries per folder (--max-lines 0 for the full view). ")
    else:
        shown_txt = f"Showing {depth_limit} levels, max {r.tree_max_children} entries per folder. "
    note = Text(shown_txt + "Ignored: .git, node_modules, venv, dist, build, vendor, caches…", style="italic grey50")
    body: List[RenderableType] = []
    if r.areas:
        body += [render_areas(r.areas), Text()]
    body += [root, Text(), footer, note]
    if r.truncated_tree:
        body.append(Text("⚠ GitHub truncated this tree (very large repo) — counts are partial.", style="warn"))
    return Panel(Group(*body), title="🌳 File Structure", border_style="blue", padding=(0, 1))


# --------------------------------------------------------------- complexity
def render_complexity(c: ComplexityResult) -> Panel:
    big = Text.assemble((f"{c.score}", f"bold {c.color}"), (" / 100", "muted"), "   ",
                        (f" {c.label.upper()} ", f"bold white on {c.color}"))
    bar = ProgressBar(total=100, completed=c.score, width=None, complete_style=c.color, finished_style=c.color)

    table = Table(box=box.MINIMAL, expand=True, show_edge=False, header_style="bold")
    table.add_column("Component")
    table.add_column("Measured", style="muted")
    table.add_column("Points", justify="right", no_wrap=True)
    table.add_column("Rule (what earns full points)", style="grey50")
    for comp in c.components:
        ratio = comp.points / comp.max_points if comp.max_points else 0
        col = "green" if ratio < 0.34 else "yellow" if ratio < 0.67 else "red"
        table.add_row(comp.name, comp.measured, Text(f"{comp.points:>4.1f}/{comp.max_points:.0f}", style=col),
                      comp.explanation)
    # The main reasons: the components that contributed the most points.
    top = sorted(c.components, key=lambda x: -x.points)[:2]
    reasons = Text.assemble(("Main reasons: ", "label"),
                            "; ".join(f"{x.name.lower()} ({x.measured})" for x in top if x.points > 0) or "small, simple repository")
    method = Text("Method: score = sum of 5 capped components (35 + 20 + 15 + 15 + 15 = 100). Size and dependencies use "
                  "log scales so huge repos don't max out instantly. Code = source files by extension; "
                  "config = manifests, YAML/JSON/TOML, Dockerfiles; docs/assets don't count as code.", style="italic grey50")
    legend = Text("0–25 Simple · 26–50 Moderate · 51–75 Complex · 76–100 Very Complex", style="italic grey50")
    return Panel(Group(Align.center(big), bar, Text(), table, reasons, Text(), method, legend),
                 title="🧮 Complexity Score", border_style=c.color, padding=(1, 1))


# ------------------------------------------------------- dependency health
def render_health(h: HealthResult) -> Panel:
    icon = {"Healthy": "✅", "Warning": "⚠️ ", "At Risk": "🚨"}.get(h.status, "ℹ️ ")
    head = Text.assemble((f"{icon} ", ""), (f" {h.status.upper()} ", f"bold white on {h.color}"))
    if h.score is not None:
        head.append(f"   {h.score}/100", style=f"bold {h.color}")
    parts: List[RenderableType] = [Align.center(head)]
    if h.score is not None:
        parts.append(ProgressBar(total=100, completed=h.score, complete_style=h.color, finished_style=h.color))
    parts.append(Text())

    marks = {"pass": ("✔", "green"), "warn": ("▲", "yellow"), "fail": ("✖", "red"), "info": ("ℹ", "cyan")}
    checks = Table.grid(padding=(0, 1))
    checks.add_column(width=2)
    checks.add_column(ratio=1)
    checks.add_column(justify="right", style="muted")
    for c in h.checks:
        mark, col = marks[c.status]
        checks.add_row(Text(mark, style=f"bold {col}"), Text(c.text), f"-{c.penalty}" if c.penalty else "")
    parts.append(checks)

    if h.dep_total:
        pc = h.pin_counts
        parts.append(Text())
        parts.append(Text.assemble(
            ("Declared deps: ", "label"), (f"{h.dep_total}", "bold"), "   ",
            ("exact ", "muted"), (str(pc.get("exact", 0)), "green"), "  ",
            ("range ", "muted"), (str(pc.get("range", 0)), "yellow"), "  ",
            ("unpinned ", "muted"), (str(pc.get("unpinned", 0)), "red"), "  ",
            ("url/git ", "muted"), (str(pc.get("url", 0)), "magenta"),
        ))
    if h.ecosystems:
        parts.append(Text("Ecosystems: " + ", ".join(h.ecosystems), style="muted"))
    parts.append(Text())
    parts.append(Text.assemble(("What \"healthy\" means here: ", "label"), (
        "installs are reproducible (a lockfile, or every version pinned), updates are automated (Dependabot/Renovate) "
        "and tested (CI), versions are constrained, a security policy exists, and the project is still maintained. "
        "Score starts at 100; each failed check subtracts the penalty shown.", "grey70")))
    parts.append(Text.assemble(("Not checked: ", "label"), (
        "whether packages are outdated or have known vulnerabilities. That needs a lookup per package on "
        "PyPI/npm/OSV, which this tool does not do, so it makes no claim either way.", "grey70")))
    parts.append(Text("≥75 Healthy · 50–74 Warning · <50 At Risk", style="italic grey50"))
    return Panel(Group(*parts), title="🩺 Dependency Health", border_style=h.color, padding=(1, 1))


SCOPE_ORDER = {"runtime": 0, "optional": 1, "peer": 2, "dev": 3, "indirect": 4}
PIN_STYLE = {"exact": ("exact", "green"), "range": ("range", "yellow"), "unpinned": ("UNPINNED", "bold red"),
             "url": ("git/url", "magenta")}


def render_dependencies(deps, max_rows: int = 25) -> Panel:
    """Table of the declared dependencies: runtime first, risky (unpinned / url) ones always shown."""
    seen, unique = set(), []
    for d in deps:  # the same requirement can appear in several groups/manifests
        key = (d.ecosystem, d.name.lower(), d.spec, d.scope)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    risky = [d for d in unique if d.pin in ("unpinned", "url")]
    rest = sorted((d for d in unique if d.pin not in ("unpinned", "url")),
                  key=lambda d: (SCOPE_ORDER.get(d.scope, 9), d.name.lower()))
    rows = risky + rest[: max(0, max_rows - len(risky))]
    table = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False, header_style="bold")
    table.add_column("Package", style="bold", no_wrap=True)
    table.add_column("Declared version")
    table.add_column("Pinning", no_wrap=True)
    table.add_column("Scope", no_wrap=True)
    table.add_column("Manifest", style="grey62")
    for d in rows:
        label, style = PIN_STYLE.get(d.pin, (d.pin, "white"))
        table.add_row(d.name, d.spec or "(none)", Text(label, style=style),
                      Text(d.scope, style="grey62" if d.scope != "runtime" else "white"), d.source)
    scopes = Counter(d.scope for d in unique)
    summary = Text.assemble(("Showing ", "grey50"), (f"{len(rows)} of {len(unique)}", "bold"),
                            (" unique dependencies (" + ", ".join(f"{n} {s}" for s, n in sorted(
                                scopes.items(), key=lambda kv: SCOPE_ORDER.get(kv[0], 9))) + ")", "grey50"))
    if len(rows) < len(unique):
        summary.append("  ·  full list: analyze <url> --json", style="grey50")
    legend = Text("Pinning: exact = one version (==1.2.3) · range = allowed span (>=1.2, ^1.2) · UNPINNED = any "
                  "version, risky · git/url = installed outside the package registry.", style="italic grey50")
    return Panel(Group(table, summary, legend), title="📦 Dependencies", border_style="grey50", padding=(0, 1))


def render_manifest(path: str, text: str, max_lines: int = 30) -> Panel:
    lines = text.splitlines()
    snippet = "\n".join(lines[:max_lines])
    # Explicit mapping: cheaper and more predictable than Pygments' guess_lexer.
    name = posixpath.basename(path).lower()
    lexer = {"package.json": "json", "composer.json": "json", "pyproject.toml": "toml", "cargo.toml": "toml",
             "pipfile": "toml", "go.mod": "go"}.get(name, "text")
    if name.endswith(".txt"):
        lexer = "text"
    syntax = Syntax(snippet, lexer, theme="monokai", line_numbers=True, word_wrap=False, background_color="default")
    sub = f"[muted]first {min(max_lines, len(lines))} of {len(lines)} lines[/]"
    return Panel(syntax, title=f"📄 Manifest Preview — [bold]{path}[/]", subtitle=sub, border_style="grey50")


# ---------------------------------------------------------- commit activity
_BLOCKS = " ▁▂▃▄▅▆▇█"


def render_contributors(contribs: Optional[List[Dict]], warning: Optional[str], width: int) -> RenderableType:
    """Horizontal bar chart of the top contributors (all-time commits on the default branch)."""
    title = Text("Top contributors (all-time commits)", style="bold")
    if contribs is None:
        return Group(title, Text(warning or "Contributor data unavailable.", style="muted"))
    if not contribs:
        return Group(title, Text("No contributors found.", style="muted"))
    top = max(c["commits"] for c in contribs) or 1
    name_w = min(24, max(len(c["login"]) for c in contribs))
    bar_w = max(10, min(50, width - name_w - 20))
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True, width=3, justify="right", style="grey50")
    table.add_column(no_wrap=True, width=name_w)
    table.add_column(no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    for i, c in enumerate(contribs, 1):
        n = max(1, round(c["commits"] / top * bar_w)) if c["commits"] else 0
        style = "grey50" if c["bot"] else ("bright_cyan" if i == 1 else "cyan")
        name = Text(c["login"][:name_w], style="grey62 italic" if c["bot"] else "bold")
        table.add_row(f"{i}.", name, Text("█" * n, style=style), Text(f"{c['commits']:,}", style="bold"))
    note = Text("Counts come from GitHub's contributor list (default branch, all time; anonymous commits excluded). "
                "Automated accounts end in [bot].", style="italic grey50")
    return Group(title, table, note)


def render_activity(a: Optional[ActivityResult], width: int, warning: Optional[str] = None,
                    contributors: Optional[List[Dict]] = None, contributors_warning: Optional[str] = None,
                    show_contributors: bool = True) -> Panel:
    title = "📈 Contribution Activity"
    extra: List[RenderableType] = []
    if show_contributors:
        extra = [Text(), render_contributors(contributors, contributors_warning, width)]
    if a is None:
        msg = warning or "Commit activity is unavailable for this repository."
        return Panel(Group(Text(msg, style="muted"), *extra), title=title, border_style="green", padding=(1, 1))
    if not a.weeks or a.total == 0:
        return Panel(Group(Text(f"No commits in the last {len(a.weeks)} weeks.", style="muted"), *extra),
                     title=title, border_style="green", padding=(1, 1))

    counts = [c for _, c in a.weeks]
    peak = max(counts) or 1
    height = 8
    y_w = len(str(peak)) + 1
    avail = max(10, width - y_w - 8)
    col_w = max(1, min(3, avail // len(counts)))
    gap = 1 if col_w >= 2 else 0
    bar_w = col_w - gap

    def color(v: int) -> str:
        r = v / peak
        return "grey35" if v == 0 else "green4" if r < .25 else "green3" if r < .5 else "spring_green2" if r < .75 else "bright_green"

    chart = Text()
    for row in range(height - 1, -1, -1):
        label = str(peak) if row == height - 1 else (str(peak // 2) if row == height // 2 - 1 else "")
        chart.append(label.rjust(y_w) + " ┤" if label else " " * y_w + " │", style="muted")
        for v in counts:
            eighths = round(v / peak * height * 8)
            level = max(0, min(8, eighths - row * 8))
            ch = _BLOCKS[level] if v else (" " if row else "·")
            if v and row == 0 and level == 0:
                ch = "▁"  # always show at least a sliver for non-zero weeks
            chart.append(ch * bar_w, style=color(v))
            chart.append(" " * gap)
        chart.append("\n")
    chart.append(" " * y_w + " └" + "─" * (len(counts) * col_w) + "\n", style="muted")

    # Month labels under the first week of each month.
    axis = [" "] * (len(counts) * col_w)
    last_month = None
    for i, (ws, _) in enumerate(a.weeks):
        if ws.month != last_month:
            lab = ws.strftime("%b")
            pos = i * col_w
            if pos + len(lab) <= len(axis) and all(c == " " for c in axis[max(0, pos - 1):pos + len(lab)]):
                axis[pos:pos + len(lab)] = list(lab)
            last_month = ws.month
    chart.append(" " * (y_w + 2) + "".join(axis), style="muted")

    stats = Table.grid(padding=(0, 3))
    for _ in range(5):
        stats.add_column()
    if a.trend_pct is None:
        trend = Text("n/a", style="muted")
    else:
        arrow, col = ("▲", "green") if a.trend_pct > 5 else ("▼", "red") if a.trend_pct < -5 else ("▶", "yellow")
        trend = Text(f"{arrow} {a.trend_pct:+.0f}%", style=f"bold {col}")
    peak_label = f"{a.peak[1]} (wk of {a.peak[0]:%b %d})" if a.peak[0] else "—"
    stats.add_row(
        Text.assemble(("Total ", "muted"), (f"{a.total:,}{'+' if a.capped else ''}", "bold")),
        Text.assemble(("Avg/week ", "muted"), (f"{a.average:.1f}", "bold")),
        Text.assemble(("Peak ", "muted"), (peak_label, "bold")),
        Text.assemble(("Active weeks ", "muted"), (f"{a.active_weeks}/{len(a.weeks)}", "bold")),
        Text.assemble(("Trend ", "muted"), trend),
    )
    note = f"Weekly commits on the default branch, last {len(a.weeks)} weeks · source: {a.source}"
    if a.capped:
        note += " · capped sample"
    return Panel(Group(Text("Commits per week", style="bold"), chart, Text(), stats,
                       Text(note, style="italic grey50"), *extra),
                 title=title, border_style="green", padding=(1, 1))


# ------------------------------------------------------------------- report
def render_report(console: Console, r: Report, activity_warning: Optional[str] = None) -> None:
    console.print(render_header(r))
    console.print(render_tech_stack(r.tech))

    side = Table.grid(expand=True, padding=(0, 1))
    if console.width >= 220:  # each panel carries a rule table + method text; side by side only on very wide screens
        side.add_column(ratio=1)
        side.add_column(ratio=1)
        side.add_row(render_complexity(r.complexity), render_health(r.health))
        console.print(side)
    else:
        console.print(render_complexity(r.complexity))
        console.print(render_health(r.health))

    if r.deps:
        console.print(render_dependencies(r.deps))
    if r.manifest_preview:
        try:  # cosmetic section: a highlighter problem must never sink the report
            console.print(render_manifest(*r.manifest_preview))
        except Exception:
            pass
    console.print(render_file_tree(r))
    console.print(render_activity(r.activity, console.width - 6, activity_warning,
                                  r.contributors, r.contributors_warning))

    footer_bits = []
    rate = r.rate
    if rate.get("remaining") is not None:
        footer_bits.append(f"API quota: {rate['remaining']}/{rate.get('limit', '?')} left")
    footer_bits.append(f"{rate.get('requests', 0)} requests used")
    footer_bits.append("authenticated" if rate.get("authenticated") else "unauthenticated — use --token for 5,000 req/h")
    console.print(Align.center(Text(" · ".join(footer_bits), style="grey50")))
    for w in r.warnings:
        console.print(f"[warn]⚠[/] [yellow]{w}[/]")


def render_error(console: Console, err: RepoAnalyzerError) -> None:
    body = Text.assemble((err.message, "bold"), "\n")
    if err.hint:
        body.append("\n💡 ", style="")
        body.append(err.hint, style="grey70")
    console.print(Panel(body, title=f"[bad]✖ {err.title}[/]", border_style="red", box=box.HEAVY,
                        padding=(1, 2), expand=False))


__all__ = ["Report", "make_console", "render_report", "render_error", "Columns"]
