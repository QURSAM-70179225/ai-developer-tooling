"""
Upload this project to a GitHub repository as ONE commit, using only the GitHub API (no git needed).

    python tools/publish_to_github.py --repo QURSAM-70179225/ai-developer-tooling
    python tools/publish_to_github.py --repo OWNER/NAME --dry-run      # list what would be uploaded

Needs GITHUB_TOKEN with "Contents: Read and write" on that repository. The repository must already
exist; it may be empty, or have only a README.

Safety:
  * skips .venv, __pycache__, *.egg-info, .git, caches and anything in .gitignore-style SKIP rules
  * refuses to upload if ANY file looks like it contains a real key or token
    (GitHub, Groq, OpenAI, Anthropic, private keys); nothing is uploaded in that case
  * the commit REPLACES the branch contents with exactly the files listed by --dry-run
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "venv", "env", "__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache",
             "node_modules", "build", "dist", ".idea", ".vscode"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".egg-info")
SKIP_NAMES = {".env", ".DS_Store", "Thumbs.db"}
MAX_FILE_BYTES = 20 * 1024 * 1024

SECRET_PATTERNS = [
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{30,}")),
    ("GitHub classic token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Groq API key", re.compile(r"\bgsk_[A-Za-z0-9]{30,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{30,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


class PublishError(Exception):
    pass


# ------------------------------------------------------------------ files
def collect_files(root: Path = ROOT) -> List[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES) or name.startswith(".env"):
                continue
            path = Path(dirpath) / name
            if path.stat().st_size <= MAX_FILE_BYTES:
                out.append(path)
    return out


def find_secrets(files: List[Path], root: Path = ROOT) -> List[Tuple[str, str]]:
    """(relative path, kind) for every file that seems to contain a real secret."""
    hits = []
    for f in files:
        try:
            text = f.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        for kind, rx in SECRET_PATTERNS:
            if rx.search(text):
                hits.append((f.relative_to(root).as_posix(), kind))
                break
    return hits


# ------------------------------------------------------------------ github
class GitHub:
    def __init__(self, token: str, session: Optional[requests.Session] = None) -> None:
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "task4-publisher"})

    def call(self, method: str, path: str, ok=(200, 201), **kw) -> requests.Response:
        try:
            r = self.s.request(method, API + path, timeout=60, **kw)
        except requests.RequestException as exc:
            raise PublishError(f"Could not reach GitHub: {exc.__class__.__name__}") from None
        if r.status_code not in ok:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = r.reason
            need = r.headers.get("X-Accepted-GitHub-Permissions")
            hint = f" (token needs: {need})" if need else ""
            raise PublishError(f"{method} {path} → HTTP {r.status_code}: {msg}{hint}")
        return r


def publish(gh: GitHub, repo: str, files: List[Path], message: str, branch: Optional[str] = None,
            root: Path = ROOT, log=print) -> str:
    try:
        info = gh.call("GET", f"/repos/{repo}").json()
    except PublishError as exc:
        if "404" in str(exc):
            raise PublishError(f"Repository {repo} not found, or the token can't see it. Create it on "
                               "github.com/new, and give the token access to it.") from None
        raise
    branch = branch or info.get("default_branch") or "main"

    parent = None
    ref = gh.call("GET", f"/repos/{repo}/git/ref/heads/{branch}", ok=(200, 404, 409))
    if ref.status_code == 200:
        parent = ref.json()["object"]["sha"]
    else:  # empty repository: the Git Data API needs one commit first, so create it with the README
        log("repository is empty, creating the first commit")
        readme = root / "README.md"
        gh.call("PUT", f"/repos/{repo}/contents/README.md",
                json={"message": "Initial commit", "branch": branch,
                      "content": base64.b64encode(readme.read_bytes()).decode()})
        parent = gh.call("GET", f"/repos/{repo}/git/ref/heads/{branch}").json()["object"]["sha"]

    tree = []
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root).as_posix()
        blob = gh.call("POST", f"/repos/{repo}/git/blobs",
                       json={"content": base64.b64encode(f.read_bytes()).decode(), "encoding": "base64"}).json()
        mode = "100755" if rel.endswith(".sh") else "100644"
        tree.append({"path": rel, "mode": mode, "type": "blob", "sha": blob["sha"]})
        log(f"  [{i}/{len(files)}] {rel}")
    tree_sha = gh.call("POST", f"/repos/{repo}/git/trees", json={"tree": tree}).json()["sha"]  # full replace
    commit = gh.call("POST", f"/repos/{repo}/git/commits",
                     json={"message": message, "tree": tree_sha, "parents": [parent]}).json()["sha"]
    gh.call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": commit, "force": False})
    return f"https://github.com/{repo}/tree/{branch}"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Upload this project to GitHub as one commit (secret-scanned).")
    p.add_argument("--repo", required=True, help="OWNER/NAME of an existing repository")
    p.add_argument("--branch", help="target branch (default: the repository's default branch)")
    p.add_argument("--message", default="Task 4: Track A (AI PR Reviewer) + Track B (Repo Analyzer CLI)")
    p.add_argument("--dry-run", action="store_true", help="only list the files that would be uploaded")
    args = p.parse_args(argv)

    files = collect_files()
    secrets = find_secrets(files)
    if secrets:
        print("STOPPED: these files look like they contain a secret. Nothing was uploaded:")
        for rel, kind in secrets:
            print(f"  - {rel}: {kind}")
        print("Remove the secret from the file (and revoke it), then run this again.")
        return 4
    total = sum(f.stat().st_size for f in files)
    print(f"{len(files)} files, {total / 1024:.0f} KB, secret scan: clean")
    if args.dry_run:
        for f in files:
            print("  " + f.relative_to(ROOT).as_posix())
        print("Dry run: nothing uploaded.")
        return 0
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("Set $env:GITHUB_TOKEN first (a token with Contents: Read and write on the repository).")
        return 3
    try:
        url = publish(GitHub(token), args.repo, files, args.message, args.branch)
    except PublishError as exc:
        print(f"Upload failed: {exc}")
        return 2
    print(f"\nDone: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
