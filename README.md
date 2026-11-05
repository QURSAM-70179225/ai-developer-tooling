# Task 4: AI-Assisted Developer Tooling

**Two developer tools built with an AI assistant, in one Python project:**

| | Track | What it does | Command |
|---|---|---|---|
| 🔍 | **B: GitHub Repository Analyzer CLI** | Takes any public GitHub repository URL and prints a full report: tech stack (with evidence), file structure, complexity score, dependency health and contribution activity. Includes an optional Streamlit web version. | `analyze <github-url>` |
| 🤖 | **A: Automated PR Reviewer (Universal Webhook Listener)** | Receives a pull request, finds its ticket, sends the diff and acceptance criteria to an LLM, then **blocks** the PR (review + failed status) or **merges** it. It never merges when anything is uncertain. | `pr-review serve / review / replay / setup-demo / make-pr` |

> Course: Task 4, *AI-Assisted Developer Tooling*. Both tracks were completed, as instructed by the course teacher.
> Author: **Qursam Bibi**, B.S. Information Engineering Technology, The University of Lahore.

**▶️ Want to run it right away? Follow [RUN.md](RUN.md): every Windows command for both tracks, in order.**

---

## Contents

1. [Results at a glance](#1-results-at-a-glance)
2. [Requirements and installation](#2-requirements-and-installation)
3. [Track B: Repository Analyzer](#3-track-b-repository-analyzer)
4. [Track A: Automated PR Reviewer](#4-track-a-automated-pr-reviewer)
5. [Configuration reference (environment variables)](#5-configuration-reference)
6. [Testing](#6-testing)
7. [Project structure](#7-project-structure)
8. [Design decisions](#8-design-decisions)
9. [How this was built with AI](#9-how-this-was-built-with-ai)
10. [Known limitations](#10-known-limitations)
11. [Security](#11-security)
12. [License](#12-license)

---

## 1. Results at a glance

These are real runs. The saved outputs are in the repository root (`demo-*.txt`).

### Track B: Repository Analyzer
| Evidence file | Repository | Shows |
|---|---|---|
| `demo-1-fastapi.txt` | [fastapi/fastapi](https://github.com/fastapi/fastapi) (Python, ~3,200 files) | Full report: Python/Starlette/Pydantic stack, condensed tree, complexity *Complex*, dependency health *Healthy*, 26-week commit chart, top contributors |
| `demo-2-express.txt` | [expressjs/express](https://github.com/expressjs/express) (JavaScript) | Full report for a second language: Mocha flagged *dev/test only*; health *Warning* because Express deliberately commits no lockfile |
| `demo-3-invalid-url.txt` | `https://gitlab.com/foo/bar` | Friendly *Invalid GitHub URL* panel, exit code 2 |
| `demo-4-not-found.txt` | `fastapi/nope-not-real` | Friendly *Repository not found* panel, exit code 3 |

### Track A: Automated PR Reviewer
These ran **live** against a private test repository with a real LLM (Groq, `openai/gpt-oss-120b`).

| Evidence file | Pull request | Result |
|---|---|---|
| `demo-A3-merged.txt` | DEMO-1: `greet()` + tests, meets every criterion | ✅ **MERGED**: status *success*, merged pinned to the reviewed commit |
| `demo-A1-blocked.txt` | DEMO-2: `add()` without type checks or tests | ❌ **BLOCKED**: status *failure*, feedback listing all three gaps |
| `demo-A2-halted.txt` | `patch-1` branch, no ticket ID | ⚠️ **HALTED**: not merged, comment explains why |
| `demo-A0-llm-error-halted.txt` | LLM model not available | ⚠️ **HALTED**: API error, not merged |
| `demo-A5-userController.txt` | Teacher's `userController.js` vs ticket **CFG-1** | ❌ **BLOCKED**: a user/auth controller; none of the 5 configuration endpoints exist |
| `demo-A6-WUserController.txt` | Teacher's `WUserController.js` vs **CFG-1** | ❌ **BLOCKED**: wrong feature, plus bugs (exports an undefined `validateConfirmationToken`, errors never passed to `next(error)`) |

---

## 2. Requirements and installation

- **Python 3.9+** (3.11+ recommended: `tomllib` is built in)
- Windows, macOS or Linux. The commands below are for Windows PowerShell; on macOS/Linux use `source .venv/bin/activate`.

```powershell
git clone https://github.com/QURSAM-70179225/ai-developer-tooling.git
cd ai-developer-tooling
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements-web.txt   # rich, requests, streamlit, pandas
pip install -e .                      # registers the `analyze` and `pr-review` commands
python -m unittest discover tests     # expect: OK (115 tests)
```

| Library | Used for |
|---|---|
| [`rich`](https://github.com/Textualize/rich) | Terminal UI: panels, tables, tree, progress spinner, syntax highlighting, terminal bar charts |
| [`requests`](https://requests.readthedocs.io) | GitHub REST API, Jira / Azure DevOps APIs, LLM APIs |
| [`streamlit`](https://streamlit.io) + [`pandas`](https://pandas.pydata.org) | *Optional* web version of Track B (`app.py`) |
| `tomli` | Only on Python < 3.11, as a fallback for `tomllib` |
| Python standard library | `argparse`, `json`, `re`, `tomllib`, `http.server` + `hmac` (webhook listener), `html.parser` (Azure DevOps HTML), `unittest` |

No web framework, LLM SDK or plotting library is needed. The webhook server uses the standard library, the LLM is called over plain HTTPS, and the charts are drawn with Rich.

---

## 3. Track B: Repository Analyzer

### 3.1 Usage
```powershell
analyze https://github.com/fastapi/fastapi
analyze github.com/pallets/flask --weeks 52
analyze expressjs/express -o report.txt          # save as clean UTF-8 text
analyze vercel/next.js --json -o report.json     # machine-readable
streamlit run app.py                             # web version at http://localhost:8501
```

Accepted URL forms: `https://github.com/owner/repo`, `github.com/owner/repo`, `git@github.com:owner/repo.git`, `owner/repo`. Extra path parts such as `/tree/main/src` are ignored.

| Option | Default | Description |
|---|---|---|
| `--token, -t` | `$GITHUB_TOKEN` / `$GH_TOKEN` | GitHub token: 5,000 requests/hour instead of 60 |
| `--depth {1-5}` | 3 | File-tree depth |
| `--max-children N` | 10 | Entries shown per folder |
| `--max-lines N` | 60 | Condense the tree on big repos (`0` = no limit) |
| `--weeks {4-52}` | 26 | Weeks of commit history in the chart |
| `--no-preview` | off | Hide the syntax-highlighted manifest preview |
| `--json` | off | JSON instead of the terminal report |
| `--output, -o FILE` | — | Save the report (or the error) to FILE as UTF-8 text. Use this instead of `>` on Windows PowerShell, which garbles box characters. |
| `--width N` | terminal / 160 | Report width in columns |
| `--no-color`, `--debug` | off | Plain output / show tracebacks for unexpected errors |

A run makes about **8–16 GitHub API requests** and never clones the repository.

### 3.2 Report sections

| Section | What it shows |
|---|---|
| 🧰 **Tech Stack** | Languages, frameworks, libraries, databases/ORMs, build tools, package managers, testing and CI, each with the **evidence** that proves it: language share from GitHub Linguist, marker files (`Dockerfile`, `tsconfig.json`, `.github/workflows/*.yml`, …), or the exact manifest entry (`package.json → react@^18.2.0`). Items found only in dev/test dependencies are tagged **dev/test only**. |
| 🌳 **File Structure** | A **Major areas** table: each top-level folder's file count, main file types and **purpose**. The purpose comes from the folder name (`docs/` → Documentation), the repo name (`fastapi/` → main package) or its contents (≥ 70% one kind of file); otherwise it says *Purpose not determined*. Then a tree that condenses itself on big repos, skipping `.git`, `node_modules`, `venv`, `dist`, `build`, `vendor` and caches. |
| 🧮 **Complexity Score** | 0–100 with a coloured bar, a breakdown table with the **rule** for each component, the **main reasons**, and the **method**, all inside the report. |
| 🩺 **Dependency Health** | Healthy / Warning / At Risk, every check with its penalty, what "healthy" means, and what is **not** checked. |
| 📦 **Dependencies** | Each declared package with its version, pinning (exact / range / **UNPINNED** / git-url), scope (runtime / optional / dev / peer / indirect) and manifest. Duplicates are merged, and risky entries are always listed. |
| 📄 **Manifest preview** | The main manifest, syntax-highlighted |
| 📈 **Contribution Activity** | Commits per week as a terminal bar chart (total, average, peak, active weeks, trend), plus a **top contributors** chart, with bots marked |

### 3.3 Complexity score: method
The score is the sum of five capped components (35 + 20 + 15 + 15 + 15 = 100):

| Component | Max | Formula | Full points at |
|---|---|---|---|
| Codebase size | 35 | `35 × log10(code_files + 1) / log10(5001)` | 5,000 code files |
| Nesting depth | 20 | `20 × min(1, (avg_depth − 1) / 5)` | average depth 6 |
| Language diversity | 15 | `15 × min(1, (languages ≥ 2% − 1) / 4)` | 5+ languages |
| Code density | 15 | `15 × code / (code + config)` | all code, no glue |
| Dependency surface | 15 | `15 × log10(deps + 1) / log10(301)` | 300 dependencies |

Labels: **0–25 Simple · 26–50 Moderate · 51–75 Complex · 76–100 Very Complex**. Log scales stop huge monorepos from maxing out instantly.

### 3.4 Dependency health: what "healthy" means
**Healthy** means the installs are reproducible, updates are automated and tested, versions are constrained, a security policy exists, and the project is maintained. The score starts at 100, and each failed check subtracts its penalty:

| Check | Penalty |
|---|---|
| An ecosystem has no lockfile (`package-lock.json`, `yarn.lock`, `poetry.lock`, `uv.lock`, `go.sum`, `Cargo.lock`, …). A pip project with every requirement pinned `==` passes. | −25 each (max −40) |
| No Dependabot / Renovate config | −15 |
| No CI configuration | −10 |
| No `SECURITY.md` | −5 |
| Dependencies with no version constraint | up to −20 |
| Dependencies installed from git / URL / path | −3 each (max −10) |
| Repository archived | −40 |
| No push for over a year | −15 |

**≥ 75 Healthy · 50–74 Warning · < 50 At Risk.** Parsed manifests: `package.json`, `requirements*.txt`, `pyproject.toml` (PEP 621, PEP 735 dependency groups, Poetry), `Pipfile`, `go.mod`, `Cargo.toml`, `composer.json`. Maven, Gradle, Bundler, pub, Mix and SwiftPM are detected but not parsed.

**Not checked:** whether packages are *outdated* or have *known vulnerabilities*. That needs a per-package lookup on PyPI, npm or OSV. The report says so instead of guessing.

### 3.5 Error handling
Every expected failure is a red panel with a plain-language message and a 💡 hint. There is never a stack trace.

| Situation | Exit code |
|---|---|
| Malformed / non-GitHub URL | 2 |
| Repository not found or private (404 / 403 / 451) | 3 |
| Bad or expired token (401) | 4 |
| Rate limit (shows the local reset time and suggests `--token`) | 5 |
| Empty repository | 6 |
| No internet, timeout, TLS/proxy problem | 7 |
| GitHub 5xx / unexpected response | 8 |
| `--output` file can't be written | 9 |
| Anything else: a friendly "Unexpected error" panel (`--debug` for the trace) | 1 |

The tool also:
- checks the quota before starting;
- keeps rendering the rest of the report when an optional section fails;
- shows *No Dependencies Found* when a repo has no dependency files, instead of a misleading score;
- falls back to listing commits when GitHub's statistics are still being computed (HTTP 202).

---

## 4. Track A: Automated PR Reviewer

### 4.1 How it works
```
GitHub PR event ─► 1 Webhook listener ─► 2 Story Fetcher ─► 3 Code Fetcher ─► 4 AI Evaluation ─► 5 Action Executor
 (opened/updated)   server.py (HMAC)     tickets.py          git_platform.py   llm.py             engine.py
                                         Jira/Azure/local    raw diff          strict JSON        block or merge
```

| # | Component | Module | What it does |
|---|---|---|---|
| 1 | Webhook listener | `server.py` | `POST /webhook` verifies GitHub's `X-Hub-Signature-256` (HMAC-SHA256). It ignores pings, closed or draft PRs and duplicate deliveries, answers **202** immediately, and reviews on a single worker thread, so two merges never race. `GET /health` reports liveness. |
| 2 | Story Fetcher | `tickets.py` | Extracts the ticket ID from the PR title or branch (`feature/JIRA-123-login-page` → `JIRA-123`; Azure `AB#42`). It fetches the ticket from the **Jira REST API v3**, the **Azure DevOps work-items API** or a local JSON file. Acceptance criteria come from a configured field, the Azure *Acceptance Criteria* field, or an "Acceptance Criteria" heading in the description. |
| 3 | Code Fetcher | `git_platform.py` | Downloads the PR's raw diff (`Accept: application/vnd.github.diff`). |
| 4 | AI Evaluation Engine | `llm.py` | Sends the assignment's **prompt blueprint word for word** as the system prompt, plus the criteria and diff, to Anthropic or any OpenAI-compatible API (OpenAI, Groq, OpenRouter, a local Ollama). The answer must be a JSON object with `fulfilled` as a **real boolean** and a non-empty `feedback`. |
| 5 | Action Executor | `engine.py` | `fulfilled: false` → status **failure** (blocks merging when required by branch protection) and a **REQUEST_CHANGES** review. GitHub doesn't allow that on your own PR, so it falls back to a main PR comment. `fulfilled: true` → status **success**, then the **Merge API**, pinned to the reviewed commit SHA. |

### 4.2 Safety: it never merges when in doubt
Every case below **halts**. The PR gets a comment explaining the problem and how to fix it, the status is set to *failure*, and **no merge request is ever sent**. Each case has an automated test that asserts the merge endpoint is never called.

| Situation | Behaviour |
|---|---|
| No ticket ID in title or branch (GitHub's `patch-1` style names are not mistaken for tickets) | halt `ticket-id` |
| Title and branch reference **different** tickets | halt `ticket-id` ("refusing to guess") |
| Ticket not found, unreachable, or credentials rejected | halt `ticket-fetch` |
| Ticket has no acceptance criteria | halt `acceptance-criteria` |
| LLM answer is not valid JSON, `fulfilled` missing or not `true`/`false`, `feedback` empty | halt `llm-response` |
| LLM API error (bad key, unknown model, timeout, 5xx) | halt `llm` |
| Diff empty | halt `diff` (the LLM is not called) |
| Diff larger than `MAX_DIFF_CHARS` (default 60,000) | halt `diff`: **refused, not truncated**, because a truncated diff could hide missing work |
| Writing the status fails before merging | halt, no merge |
| GitHub refuses the merge (conflict, required check, new commit pushed during review) | halt `merge`, with the reason |

Two extra protections:
- **Dry-run is the default.** Without `--live`, nothing is posted or merged; actions are printed as "would …".
- **Pinned merges.** The merge names the reviewed head SHA, so a commit pushed mid-review can never be merged unreviewed.

### 4.3 Commands
```powershell
pr-review setup-demo --repo OWNER/test-repo                  # create 3 demo PRs (merge / block / halt)
pr-review make-pr --repo OWNER/test-repo --branch feature/CFG-1-x --title "CFG-1 X" --file local.js=path/in/repo.js
pr-review review  --repo OWNER/test-repo --pr 2              # dry-run review
pr-review review  --repo OWNER/test-repo --pr 2 --live -o result.txt
pr-review serve   --live --port 8000                         # webhook listener (needs WEBHOOK_SECRET)
pr-review replay  examples/pull_request_event.json           # replay a saved webhook payload
```
`--live`, `--dry-run` and `-o FILE` work before or after the subcommand.
Exit codes: `0` review completed (merged or blocked) · `2` halted, nothing merged · `3` configuration or input problem.

### 4.4 The teacher's task files (ticket CFG-1)
The teacher's Jira ticket *"Implement Configuration Management APIs (Create, Read, Update, Delete) for Track Configurations"* and two controller files are in `examples/task-files/`. The ticket had no Jira key, so it was registered as **`CFG-1`** in `examples/tickets.json` with all 9 acceptance criteria. Each controller was opened as its own PR with `make-pr` and reviewed live; both were correctly **blocked** (see [Results](#1-results-at-a-glance)).

### 4.5 Webhook setup (real GitHub events)
1. Start a tunnel: `npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8000/webhook` (or `ngrok http 8000`).
2. In the test repo, go to **Settings → Webhooks**: set the Payload URL to the tunnel URL, the content type to `application/json`, the secret to `WEBHOOK_SECRET`, and the events to *Pull requests* only.
3. Run `pr-review serve --live`. Opening or updating a PR now triggers a review.
4. To make a failed review block the Merge button: **Settings → Branches → rule for `main` → Require status checks → `ai-pr-reviewer`**.

---

## 5. Configuration reference

All secrets are read from **environment variables**, never from code or files in this repository.

| Variable | Used by | Purpose / default |
|---|---|---|
| `GITHUB_TOKEN` (or `GH_TOKEN`) | both | GitHub API. Track B: optional (raises the rate limit). Track A: a fine-grained token on the test repo with **Contents**, **Pull requests** and **Commit statuses** set to read & write. |
| `WEBHOOK_SECRET` | A (`serve`) | HMAC secret shared with the GitHub webhook |
| `TICKET_SOURCE` | A | `jira` (default) · `azure` · `local` |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_AC_FIELD` | A | Jira Cloud; `JIRA_AC_FIELD` is an optional custom field ID holding the criteria |
| `AZDO_ORG`, `AZDO_PROJECT`, `AZDO_PAT` | A | Azure DevOps Boards |
| `LOCAL_TICKETS_FILE` | A | JSON tickets file, e.g. `examples/tickets.json` |
| `LLM_PROVIDER` | A | `anthropic` (default) or `openai` (any OpenAI-compatible API) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `LLM_API_KEY` | A | LLM key |
| `LLM_MODEL`, `LLM_BASE_URL`, `LLM_JSON_MODE` | A | Model, endpoint (Groq: `https://api.groq.com/openai/v1`; Ollama: `http://localhost:11434/v1`), JSON mode (default on) |
| `MAX_DIFF_CHARS` · `MERGE_METHOD` · `DRY_RUN` | A | 60000 · `squash` · `1` |

---

## 6. Testing
```powershell
python -m unittest discover tests -v
```
**115 offline tests**, needing no network, token or LLM. GitHub, Jira, Azure DevOps, the LLM and Streamlit are replaced by recording fakes:

| File | Covers |
|---|---|
| `tests/test_repo_analyzer.py` | URL parsing, manifest parsers, tech-stack evidence, scores, tree condensing, area purposes, dependency de-duplication, every error path and exit code, `--json`, `--output` |
| `tests/test_app.py` | The Streamlit app end to end against a fake Streamlit (every tab, the error messages, sample buttons); skipped if pandas is missing |
| `tests/test_publish.py` | The GitHub publisher: file selection, the secret scan (including a scan of this whole project), a one-commit upload, an empty repository |
| `tests/test_pr_reviewer.py` | Ticket-ID extraction, Jira/Azure/local fetchers, strict verdict parsing, **every safety case (asserting no merge call)**, dry-run (asserting no write calls), the webhook signature, a real local HTTP server with duplicate deliveries, `setup-demo`, `make-pr`, and the CFG-1 task files |

---

## 7. Project structure
```
├── README.md                  this file
├── RUN.md                     step-by-step commands for both tracks (Windows)
├── LICENSE                    MIT (see section 12 for third-party material)
├── pyproject.toml             package metadata, `analyze` + `pr-review` commands
├── requirements.txt           rich, requests
├── requirements-web.txt       + streamlit, pandas
├── app.py                     Track B web version (Streamlit)
├── repo_analyzer/             Track B
│   ├── cli.py                   arguments, progress, pipeline, error boundary, --json/--output
│   ├── github_client.py         REST client: auth, rate limits, HTTP status → typed errors
│   ├── analyzers.py             pure logic: tech stack, structure, scores, manifests, activity
│   ├── render.py                Rich panels, tables, tree, charts
│   └── errors.py                user-facing errors with hints and exit codes
├── pr_reviewer/               Track A
│   ├── server.py                webhook listener (HMAC, dedupe, worker thread)
│   ├── tickets.py               ticket-ID extraction, Jira / Azure DevOps / local fetchers
│   ├── git_platform.py          GitHub diff, status, review, comment, merge
│   ├── llm.py                   blueprint prompt, Anthropic / OpenAI-compatible clients, strict parsing
│   ├── engine.py                the five-step controller with safe halts and dry-run
│   ├── config.py                settings from environment variables
│   ├── demo_setup.py            `setup-demo` and `make-pr`
│   ├── cli.py                   `pr-review` commands
│   └── errors.py
├── examples/
│   ├── tickets.json             demo tickets DEMO-1..3 and the teacher's CFG-1
│   ├── pull_request_event.json  sample webhook payload for `replay`
│   └── task-files/              teacher-provided ticket (.docx) and controllers
├── tests/                     115 offline tests
├── tools/publish_to_github.py   uploads the project as one commit, with a secret scan
└── demo-*.txt                 saved evidence from real runs
```

---

## 8. Design decisions
- **Pure core, thin edges.** The analysis and decision logic (`analyzers.py`, `engine.py`) has no network or UI code, so every rule is unit-tested with plain data.
- **Never invent data.** Unknowns are labelled *not determined*, *unavailable* or *not checked* rather than estimated.
- **Fail closed.** In Track A every uncertainty halts, and merging lives in exactly one place, after a valid `fulfilled: true`.
- **Refuse, don't truncate.** An oversized diff is rejected, because half a diff can't prove a ticket is done.
- **Few dependencies.** The webhook server, HMAC checks and LLM calls use the standard library and `requests`, with no framework or SDK.
- **Windows-friendly output.** `-o FILE` writes UTF-8 itself, because PowerShell 5.1 redirection garbles box-drawing characters.

---

## 9. How this was built with AI
The tools were built in stages with an AI assistant (Claude). Each stage was specified, generated, **run on real data, reviewed and fixed** before moving on. The complete, unedited chat is part of the submission. Problems found by reviewing real output include:
- A large repo produced a ~280-line tree, so the tree now condenses itself to about 60 lines and always keeps top-level folders.
- `[dependency-groups]` (PEP 735) wasn't parsed, so pytest showed no manifest evidence; parsing was added.
- Flask appeared as a *framework* of FastAPI although it is only a test dependency, so scopes and a *dev/test only* tag were added.
- Dependency counts disagreed between sections (97 vs 71), so duplicates are now merged everywhere.
- PowerShell redirection garbled saved reports, so `--output` was added to both tools.
- GitHub's default branch name `patch-1` was being read as ticket *PATCH-1*. The ticket-ID rules were tightened, with a test.
- An HTTP 403 gave no hint, so the error now names the missing token permission (from GitHub's `X-Accepted-GitHub-Permissions` header).

---

## 10. Known limitations
- **Track B** doesn't check whether packages are outdated or vulnerable (the report says so). Very large repositories can exceed GitHub's tree limit; the report then warns that counts are partial.
- **Track A** implements **GitHub** as the Git platform. Jira and Azure DevOps are supported as ticket systems; Azure Repos would need an adapter with the same five methods as `GitHubPlatform`.
- The **webhook listener** is fully tested offline (signatures, a real local HTTP server, duplicate deliveries), but the recorded demos trigger the same engine with `pr-review review` instead of live GitHub webhooks.
- An **LLM reviewer can miss details**. In the CFG-1 run it didn't mention the missing `}` in `WUserController.js`. It complements tests and linters rather than replacing them; the fail-closed design means a miss can never cause a wrong merge.

---

## 11. Security
- No token or key is stored anywhere in this repository; everything comes from environment variables. `tools/publish_to_github.py` refuses to upload any file that looks like it contains a GitHub, Groq, OpenAI or Anthropic key.
- Webhook requests are verified with HMAC-SHA256 and a constant-time comparison.
- Use a **test repository** for Track A, and fine-grained tokens limited to that repository. Revoke them when you're done.

---

## 12. License
The code in this repository is released under the **MIT License**; see [LICENSE](LICENSE).
© 2026 Qursam Bibi.

**Third-party material, not covered by the MIT License:** the files in `examples/task-files/` (the Jira ticket document and the two controller files) were provided by the course instructor for this assignment. They are included only as test input, and their copyright remains with their owners.
