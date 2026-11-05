# ▶️ How to run everything (final guide, Windows PowerShell)

This project has **both tracks** of Task 4:
- **Track B:** `analyze` (Repo Analyzer CLI) plus the optional web demo
- **Track A:** `pr-review` (AI PR Reviewer)

Run the commands **one line at a time**, in order.

> 🔒 **Never** paste a token or API key into the AI chat, into code, or into a file you submit.
> Type them only into PowerShell, as shown below.

---

## 0. One-time setup

```powershell
cd $HOME\Desktop\repo-analyzer-cli
python -m venv .venv                       # skip if .venv already exists
.\.venv\Scripts\Activate.ps1               # prompt now starts with (.venv)
pip install -r requirements-web.txt        # rich, requests, streamlit, pandas
pip install -e .                           # registers the `analyze` and `pr-review` commands
python -m unittest discover tests          # expect: OK  (115 tests)
```

**Every time you open a new PowerShell window**, run only:
`cd $HOME\Desktop\repo-analyzer-cli` and `.\.venv\Scripts\Activate.ps1`.

If activation says "running scripts is disabled", run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

---

## 1. Get the keys you need (about 5 minutes)

### 1a. Test repository (for Track A)
1. Go to **https://github.com/new**.
2. Name it **`pr-reviewer-test`** and make it **Public**.
3. ✅ Tick **"Add a README file"**, then click **Create repository**.

### 1b. GitHub token (one token works for both tracks)
1. Go to **https://github.com/settings/personal-access-tokens/new**.
2. **Repository access:** *Only select repositories* → `pr-reviewer-test`.
3. **Permissions → Repository permissions:**
   - **Contents:** Read and write
   - **Pull requests:** Read and write
   - **Commit statuses:** Read and write
4. Click **Generate** and copy the token (`github_pat_...`).

This token can still *read* any public repository, so Track B works with it too.

### 1c. LLM key (for Track A)
- **Free option:** create a key at **https://console.groq.com/keys** (`gsk_...`).
- **Or** use an Anthropic key (`sk-ant-...`) or an OpenAI key.

---

## 2. Set the environment variables (in your `(.venv)` window)

```powershell
$env:GITHUB_TOKEN   = "github_pat_PASTE_YOURS"

# LLM, option 1: Groq (free)
$env:LLM_PROVIDER   = "openai"
$env:LLM_BASE_URL   = "https://api.groq.com/openai/v1"
$env:LLM_MODEL      = "llama-3.3-70b-versatile"
$env:OPENAI_API_KEY = "gsk_PASTE_YOURS"
# LLM, option 2: Anthropic. Use these two lines INSTEAD of the four Groq lines above.
# $env:LLM_PROVIDER = "anthropic"; $env:ANTHROPIC_API_KEY = "sk-ant-PASTE_YOURS"

# Tickets: the demo tickets in examples\tickets.json (DEMO-1, DEMO-2, DEMO-3)
$env:TICKET_SOURCE      = "local"
$env:LOCAL_TICKETS_FILE = "examples\tickets.json"
```

These only last for this window. If you open a new window, set them again.

<details><summary>Using real Jira instead of the local tickets file (optional)</summary>

```powershell
$env:TICKET_SOURCE  = "jira"
$env:JIRA_BASE_URL  = "https://YOUR-SITE.atlassian.net"
$env:JIRA_EMAIL     = "you@example.com"
$env:JIRA_API_TOKEN = "PASTE_YOURS"      # from https://id.atlassian.com/manage-profile/security/api-tokens
```
Create tickets whose description has an **"Acceptance Criteria"** heading followed by the criteria. Name the branches after the Jira keys, e.g. `feature/KAN-1-greeting`.
</details>

---

## 3. Track B: Repo Analyzer

```powershell
analyze https://github.com/fastapi/fastapi                         # full report in the terminal
analyze https://github.com/fastapi/fastapi       -o demo-1-fastapi.txt
analyze https://github.com/expressjs/express     -o demo-2-express.txt
analyze https://gitlab.com/foo/bar               -o demo-3-invalid-url.txt
analyze https://github.com/fastapi/nope-not-real -o demo-4-not-found.txt
```

**Web demo:**
```powershell
streamlit run app.py
```
It opens http://localhost:8501. Click a sample, look through the tabs, then press **Ctrl + C** in PowerShell to stop it.

---

## 4. Track A: AI PR Reviewer

### 4a. Create the three demo PRs (automatic, no git needed)
```powershell
pr-review setup-demo --repo YOUR-GITHUB-USERNAME/pr-reviewer-test -o demo-A0-setup.txt
```

| PR | Branch | Ticket | Expected |
|---|---|---|---|
| #1 | `feature/DEMO-1-greeting` | DEMO-1 | **MERGED**: meets every criterion |
| #2 | `feature/DEMO-2-add` | DEMO-2 | **BLOCKED**: no TypeError check, no tests |
| #3 | `patch-1` | none | **HALTED**: no ticket ID, never merged |

### 4b. Dry-run first (nothing is posted or merged)
```powershell
pr-review review --repo YOUR-GITHUB-USERNAME/pr-reviewer-test --pr 2
pr-review review --repo YOUR-GITHUB-USERNAME/pr-reviewer-test --pr 3
pr-review review --repo YOUR-GITHUB-USERNAME/pr-reviewer-test --pr 1
```
Each run shows the decision and the actions it **would** take.

### 4c. Live runs (these are your demo evidence)
**First, the blocked PR:**
```powershell
pr-review review --repo YOUR-GITHUB-USERNAME/pr-reviewer-test --pr 2 --live -o demo-A1-blocked.txt
```

**Now make "Failed" really block the Merge button.** The `ai-pr-reviewer` check exists now, so you can require it:
1. In `pr-reviewer-test`, go to **Settings → Branches → Add branch protection rule** (or **Add classic branch protection rule**). Set **Branch name pattern** to `main`.
2. ✅ **Require status checks to pass before merging** → search **`ai-pr-reviewer`** → add it → **Create**.
3. Don't tick "Require a pull request before merging / approvals". The tool merges on its own.

On PR #2 the **Merge** button is now blocked by the red ❌ check.

**Then the other two:**
```powershell
pr-review review --repo YOUR-GITHUB-USERNAME/pr-reviewer-test --pr 3 --live -o demo-A2-halted.txt
pr-review review --repo YOUR-GITHUB-USERNAME/pr-reviewer-test --pr 1 --live -o demo-A3-merged.txt
```

Then check on **github.com → pr-reviewer-test → Pull requests** and take screenshots:

**PR #2**
- a 🤖 comment listing the missing requirements;
- a red ❌ `ai-pr-reviewer` check: "Acceptance criteria of DEMO-2 not met";
- the Merge button blocked.

GitHub doesn't let you "request changes" on your *own* PR, so the tool automatically posts the feedback as a main PR comment instead. Both are allowed by the brief.

**PR #3:** a ⚠️ comment "review could not be completed … will not be merged", and a red check.

**PR #1:** a green ✅ check, "Merged", and a ✅ summary comment.

### 4e. Checking the teacher's task files (ticket CFG-1)
The teacher's Jira ticket ("Configuration Management APIs") is saved as **`CFG-1`** in `examples\tickets.json`, with all 9 acceptance criteria. The document has no Jira key, so the tool needs one to link PRs to it. The original `.docx` and the two controller files are in `examples\task-files\`.

Open one PR per file, then review each one:
```powershell
pr-review make-pr --repo QURSAM-70179225/pr-reviewer-test1 --branch feature/CFG-1-user-controller  --title "CFG-1 User controller"  --file examples\task-files\userController.js=controllers/userController.js
pr-review make-pr --repo QURSAM-70179225/pr-reviewer-test1 --branch feature/CFG-1-wuser-controller --title "CFG-1 WUser controller" --file examples\task-files\WUserController.js=controllers/WUserController.js
```
Each command prints its PR number. Review each PR (dry-run first, then live):
```powershell
pr-review review --repo QURSAM-70179225/pr-reviewer-test1 --pr <NUMBER>
pr-review review --repo QURSAM-70179225/pr-reviewer-test1 --pr <NUMBER> --live -o demo-A5-userController.txt
pr-review review --repo QURSAM-70179225/pr-reviewer-test1 --pr <NUMBER> --live -o demo-A6-WUserController.txt
```

**Expected:** both **❌ BLOCKED**.
- The ticket asks for a *configuration* controller (`addNewConfiguration`, `getAllConfigurations`, …).
- Both files are *user/auth* controllers, so the acceptance criteria aren't met.
- `WUserController.js` is also broken: `userRegisteration` is never closed, its email code is missing, and it exports `validateConfirmationToken`, which isn't defined.

A correct reviewer must not merge either one.

### 4d. As a real webhook listener (optional; this is the "Universal Webhook Listener" part)
GitHub must be able to reach your PC, so you need a tunnel. You need [Node.js](https://nodejs.org) for `npx`.

1. Open **https://smee.io** → **Start a new channel** and copy the URL.
2. In a **second** PowerShell window, run:
   `npx smee-client --url https://smee.io/YOUR-CHANNEL --target http://127.0.0.1:8000/webhook`
3. In the test repo, go to **Settings → Webhooks → Add webhook**:
   - **Payload URL:** the smee URL
   - **Content type:** `application/json`
   - **Secret:** any random text, e.g. `demo-secret-123`
   - **Which events:** *Let me select* → ✅ **Pull requests** only
4. In your main `(.venv)` window, run:
```powershell
$env:WEBHOOK_SECRET = "demo-secret-123"          # the same text as the webhook Secret
pr-review serve --live -o demo-A4-webhook.txt
```
5. Push a commit to PR #2's branch, or open a new PR. The listener logs `<- pull_request: 202` and reviews it automatically. Stop it with **Ctrl + C**.

---

## 5. Troubleshooting

| You see | Fix |
|---|---|
| `analyze` / `pr-review` not recognized | Activate the venv, then run `pip install -e .` again |
| `Missing environment variables: ...` | Set the variables in step 2 again (they don't carry over to new windows) |
| `HTTP 401` / `Bad credentials` | The token is wrong or expired: make a new one (step 1b) |
| `HTTP 403 ... Resource not accessible` | The token lacks a permission from step 1b, or doesn't include `pr-reviewer-test` |
| `LLM API returned HTTP 401` | Wrong LLM key; check `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` |
| `LLM API returned HTTP 404 / model not found` | Set `LLM_MODEL` to a model from https://console.groq.com/docs/models |
| `HALTED ... merging failed ... not mergeable` | The PR has conflicts, or a check is pending; open it on GitHub to see why |
| Rate limit panel in `analyze` | `$env:GITHUB_TOKEN` isn't set in this window |

---

## 6. Publish the project to GitHub (private repository)

1. Go to **https://github.com/new**:
   - **Name:** `ai-developer-tooling`
   - **Visibility:** **Private**
   - Do **not** add a README, .gitignore or license (the project has its own).
   - Click **Create repository**.
2. Give your token access to it: **https://github.com/settings/personal-access-tokens** → your token → **Edit** → *Repository access* → add `ai-developer-tooling` → **Contents: Read and write** → **Update**.
3. In your `(.venv)` window:
   ```powershell
   python tools\publish_to_github.py --repo QURSAM-70179225/ai-developer-tooling --dry-run
   python tools\publish_to_github.py --repo QURSAM-70179225/ai-developer-tooling
   ```
   The dry-run lists every file and must say **"secret scan: clean"**. The second command uploads everything as one commit. Running it again later replaces the repository contents with your current files.
4. Share the private repository with your teacher: **Settings → Collaborators → Add people**.

## 7. Submit

1. **Zip the project.** Stop Streamlit first, then run:
   ```powershell
   Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
   $items = Get-ChildItem -Path . -Exclude .venv, repo_analyzer_cli.egg-info
   Compress-Archive -Path $items -DestinationPath "$HOME\Desktop\task4-submission.zip" -Force
   ```
   The zip includes the code, tests, `README.md`, this `RUN.md`, and all `demo-*.txt` evidence files. Put screenshots in a `screenshots` folder first so they're included too.
2. **Export the AI chat** in full and unedited, e.g. **Ctrl + P → Save as PDF**.
3. **Submission text:**
   > **Task 4: Track A (Automated PR Reviewer) and Track B (GitHub Repository Analyzer CLI)**, as instructed by my teacher. Python 3 with `rich` and `requests` (web demo: `streamlit`, `pandas`). See README.md and RUN.md.
   > Evidence, Track B: `demo-1-fastapi.txt`, `demo-2-express.txt`, `demo-3-invalid-url.txt`, `demo-4-not-found.txt`.
   > Evidence, Track A: `demo-A1-blocked.txt` ("Failed" with comments), `demo-A3-merged.txt` (merge), `demo-A2-halted.txt` (safe halt), `demo-A5/A6` (teacher's CFG-1 files), plus screenshots.
4. Afterwards, **revoke your tokens** at https://github.com/settings/personal-access-tokens (and the Groq or Anthropic key, if you like).
