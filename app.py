"""
Repo Analyzer: Streamlit web demo.

A browser front-end for the same analysis engine the CLI uses. It calls the exact same
`run_pipeline()`, so the numbers here always match `analyze <url>` in the terminal.

Run:   streamlit run app.py
Token: set GITHUB_TOKEN in the shell before starting, or paste one in the sidebar
       (it stays in this browser session only and is never written to disk).
"""

from __future__ import annotations

import dataclasses
import io
import os
import traceback

import pandas as pd
import streamlit as st

from repo_analyzer import __version__, render
from repo_analyzer.analyzers import parse_github_url
from repo_analyzer.cli import make_report, report_as_text, run_pipeline
from repo_analyzer.errors import RepoAnalyzerError
from repo_analyzer.github_client import GitHubClient

st.set_page_config(page_title="Repo Analyzer", page_icon="🔍", layout="wide")

SAMPLES = ["https://github.com/fastapi/fastapi", "https://github.com/expressjs/express",
           "https://github.com/pallets/flask"]
STATUS_ICON = {"Healthy": "✅", "Warning": "⚠️", "At Risk": "🚨", "No Dependencies Found": "ℹ️"}
CHECK_ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}


# ---------------------------------------------------------------- helpers
def _use_sample(url: str) -> None:
    """Button callback: runs before the rerun, so it may set widget state safely."""
    st.session_state["url"] = url
    st.session_state["run"] = True


def _plain(renderable, width: int = 120) -> str:
    """Render any Rich object to plain text (used for the tree and error details)."""
    buf = io.StringIO()
    render.make_file_console(buf, width=width).print(renderable)
    return buf.getvalue()


def analyze(url: str, token: str | None, weeks: int) -> None:
    """Run the shared pipeline with live progress; store the result in session state."""
    st.session_state.pop("analysis", None)
    try:
        owner, repo = parse_github_url(url)
        client = GitHubClient(token=token)
        with st.status(f"Analyzing {owner}/{repo}…", expanded=True) as status:
            result = run_pipeline(owner, repo, client, weeks,
                                  step=lambda msg: status.update(label=msg),
                                  done=lambda msg: status.write("✓ " + msg.replace("[bold]", "").replace("[/]", "")))
            status.update(label=f"Analysis of {owner}/{repo} complete", state="complete", expanded=False)
        rate = {"remaining": client.rate_remaining, "limit": client.rate_limit,
                "requests": client.requests_made, "authenticated": bool(token)}
        st.session_state["analysis"] = dict(url=url, owner=owner, repo=repo, result=result, rate=rate)
    except RepoAnalyzerError as err:  # the same friendly errors as the CLI, never a stack trace
        st.error(f"**{err.title}**: {err.message}")
        if err.hint:
            st.info(f"💡 {err.hint}")
    except Exception as exc:  # last-resort safety net
        st.error(f"**Unexpected error**: {exc.__class__.__name__}: {exc}")
        with st.expander("Technical details (please report this)"):
            st.code(traceback.format_exc(), language="text")


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("🔍 Repo Analyzer")
    st.caption(f"Track B: GitHub Repository Analyzer · v{__version__}")
    url = st.text_input("GitHub repository URL", key="url", placeholder="https://github.com/owner/repo")
    token_input = st.text_input("GitHub token (optional)", type="password",
                                help="Leave empty to use the GITHUB_TOKEN environment variable. "
                                     "Without a token GitHub allows 60 requests/hour.")
    weeks = st.slider("Weeks of commit history", 4, 52, 26)
    clicked = st.button("Analyze", type="primary")
    st.caption("Try a sample:")
    for sample in SAMPLES:
        st.button(sample.removeprefix("https://github.com/"), on_click=_use_sample, args=(sample,),
                  key=f"sample-{sample}")

token = token_input or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
if clicked or st.session_state.pop("run", False):
    if not (url or "").strip():
        st.warning("Enter a GitHub repository URL first.")
    else:
        analyze(url, token, weeks)

data = st.session_state.get("analysis")
if not data:
    st.title("🔍 Repo Analyzer")
    st.markdown("Enter a public GitHub repository in the sidebar, or click a sample, to see its "
                "**tech stack, structure, complexity, dependency health and contribution activity**.")
    if not token:
        st.caption("No GitHub token detected: unauthenticated requests are limited to 60/hour.")
    st.stop()

# ---------------------------------------------------------------- report
r = data["result"]
report = make_report(data["owner"], data["repo"], r, data["rate"])
meta, health, cx, act = r["meta"], r["health"], r["complexity"], r["activity"]

st.title(f"📦 {meta.get('full_name', data['repo'])}")
if meta.get("description"):
    st.caption(meta["description"])
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Stars", f"{meta.get('stargazers_count', 0):,}")
c2.metric("Forks", f"{meta.get('forks_count', 0):,}")
c3.metric("Open issues", f"{meta.get('open_issues_count', 0):,}")
c4.metric("Complexity", f"{cx.score}/100", cx.label, delta_color="off")
c5.metric("Dependency health", f"{STATUS_ICON.get(health.status, '')} {health.status}",
          f"{health.score}/100" if health.score is not None else "not scored", delta_color="off")
if meta.get("archived"):
    st.warning("This repository is archived.")

tabs = st.tabs(["🧰 Tech stack", "🌳 Structure", "🧮 Complexity", "🩺 Dependency health",
                "📈 Contribution activity", "📄 Full report"])

with tabs[0]:  # ---- Tech stack
    st.subheader("Tech stack (with evidence)")
    if r["tech"]:
        st.dataframe(pd.DataFrame([{
            "Technology": t.name, "Category": t.category,
            "Evidence": " · ".join(t.evidence), "Dev/test only": "yes" if t.dev_only else "",
        } for t in r["tech"]]), hide_index=True)
    else:
        st.info("No languages, frameworks or tooling could be identified.")
    st.caption('Evidence = the file or manifest entry that proves each item. "Dev/test only" = found only '
               "in development/test dependencies.")

with tabs[1]:  # ---- Structure
    s = cx.stats
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Files", f"{int(s['total_files']):,}")
    m2.metric("Directories", f"{int(s['directories']):,}")
    m3.metric("Code files", f"{int(s['code_files']):,}")
    m4.metric("Config files", f"{int(s['config_files']):,}")
    m5.metric("Docs", f"{int(s['doc_files']):,}")
    st.subheader("Major areas")
    st.dataframe(pd.DataFrame([{
        "Folder": a.name if a.name.startswith(("(", "…")) else a.name + "/", "Files": a.files,
        "Main file types": " · ".join(f"{e} {n:,}" for e, n in a.top_exts),
        "Purpose": a.purpose, "Basis": a.basis,
    } for a in r["areas"]]), hide_index=True)
    st.caption("Basis: by name = well-known folder name · by repo name = matches the repository · "
               "by contents = ≥70% one kind of file · unknown = not determined (never guessed).")
    st.subheader("File tree")
    tree_only = dataclasses.replace(report, areas=[])
    st.code(_plain(render.render_file_tree(tree_only), width=110), language="text")
    if r["truncated"]:
        st.warning("GitHub truncated this tree (very large repository): counts are partial.")

with tabs[2]:  # ---- Complexity
    st.subheader(f"Complexity score: {cx.score}/100 ({cx.label})")
    st.progress(cx.score / 100)
    st.dataframe(pd.DataFrame([{
        "Component": c.name, "Measured": c.measured, "Points": f"{c.points:.1f} / {c.max_points:.0f}",
        "Rule (what earns full points)": c.explanation,
    } for c in cx.components]), hide_index=True)
    top = sorted(cx.components, key=lambda x: -x.points)[:2]
    st.markdown("**Main reasons:** " + ("; ".join(f"{x.name.lower()} ({x.measured})" for x in top if x.points > 0)
                                        or "small, simple repository"))
    st.markdown("**Method:** score = sum of 5 capped components (35 + 20 + 15 + 15 + 15 = 100). Size and "
                "dependencies use log scales so huge repos don't max out instantly. "
                "0–25 Simple · 26–50 Moderate · 51–75 Complex · 76–100 Very Complex.")

with tabs[3]:  # ---- Dependency health + dependency list
    title = f"{STATUS_ICON.get(health.status, '')} {health.status}"
    st.subheader(title + (f" ({health.score}/100)" if health.score is not None else ""))
    if health.score is not None:
        st.progress(health.score / 100)
    for c in health.checks:
        st.markdown(f"{CHECK_ICON[c.status]} {c.text}" + (f"  **(−{c.penalty})**" if c.penalty else ""))
    st.markdown('**What "healthy" means here:** installs are reproducible (a lockfile, or every version '
                "pinned), updates are automated (Dependabot/Renovate) and tested (CI), versions are "
                "constrained, a security policy exists, and the project is still maintained. "
                "Score starts at 100; each failed check subtracts the penalty shown. "
                "≥75 Healthy · 50–74 Warning · <50 At Risk.")
    st.markdown("**Not checked:** whether packages are outdated or have known vulnerabilities (that needs a "
                "per-package lookup on PyPI/npm/OSV), so the report makes no claim either way.")
    if r["deps"]:
        st.subheader(f"Dependencies ({len(r['deps'])} unique)")
        order = {"runtime": 0, "optional": 1, "peer": 2, "dev": 3, "indirect": 4}
        st.dataframe(pd.DataFrame([{
            "Package": d.name, "Declared version": d.spec or "(none)", "Pinning": d.pin,
            "Scope": d.scope, "Manifest": d.source,
        } for d in sorted(r["deps"], key=lambda d: (order.get(d.scope, 9), d.name.lower()))]),
            hide_index=True)
    if r["preview"]:
        with st.expander(f"Manifest preview: {r['preview'][0]}"):
            lang = "json" if r["preview"][0].endswith(".json") else "toml" if r["preview"][0].endswith(".toml") else "text"
            st.code("\n".join(r["preview"][1].splitlines()[:60]), language=lang)

with tabs[4]:  # ---- Contribution activity
    st.subheader("Commits per week")
    if act is None:
        st.info(r["activity_warning"] or "Commit activity is unavailable for this repository.")
    elif not act.weeks or act.total == 0:
        st.info(f"No commits in the last {len(act.weeks)} weeks.")
    else:
        weekly = pd.DataFrame({"Commits": [c for _, c in act.weeks]},
                              index=pd.Index([w.strftime("%Y-%m-%d") for w, _ in act.weeks], name="Week starting"))
        st.bar_chart(weekly)
        a1, a2, a3, a4, a5 = st.columns(5)
        a1.metric("Total", f"{act.total:,}{'+' if act.capped else ''}")
        a2.metric("Avg / week", f"{act.average:.1f}")
        a3.metric("Peak week", f"{act.peak[1]}", act.peak[0].strftime("%b %d") if act.peak[0] else None,
                  delta_color="off")
        a4.metric("Active weeks", f"{act.active_weeks}/{len(act.weeks)}")
        a5.metric("Trend (recent vs older half)", f"{act.trend_pct:+.0f}%" if act.trend_pct is not None else "n/a")
        st.caption(f"Default branch, last {len(act.weeks)} weeks · source: {act.source}")
    st.subheader("Top contributors (all-time commits)")
    contribs = r["contributors"]
    if contribs is None:
        st.info(r["contributors_warning"] or "Contributor data unavailable.")
    elif not contribs:
        st.info("No contributors found.")
    else:
        people = pd.DataFrame({"Commits": [c["commits"] for c in contribs]},
                              index=pd.Index([c["login"] for c in contribs], name="Contributor"))
        st.bar_chart(people)
        st.caption("From GitHub's contributor list (default branch, all time; anonymous commits excluded). "
                   "Automated accounts end in [bot].")

with tabs[5]:  # ---- the exact CLI report, for download / evidence
    text = f"$ analyze {data['url']}\n\n" + report_as_text(report, r["activity_warning"])
    st.download_button("⬇️ Download full report (.txt)", text, file_name=f"report-{data['repo']}.txt",
                       mime="text/plain")
    st.code(text, language="text")

for w in r["warnings"]:
    st.warning(w)
rate = data["rate"]
st.caption(f"API quota: {rate.get('remaining', '?')}/{rate.get('limit', '?')} left · "
           f"{rate['requests']} requests used · {'authenticated' if rate['authenticated'] else 'unauthenticated'}")
