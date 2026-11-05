"""
Pure analysis logic: no network, no Rich. Everything here takes plain data
(paths, file contents, numbers) and returns dataclasses, which keeps it easy
to unit-test and lets the renderer stay purely presentational.

Sections:
  1. URL parsing
  2. File classification & tree building
  3. Dependency manifest parsing
  4. Tech-stack detection (with evidence)
  5. Complexity score
  6. Dependency health
  7. Commit activity summary
"""

from __future__ import annotations

import json
import math
import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .errors import InvalidURLError

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - older Pythons
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:
        _toml = None  # TOML manifests are then detected but not parsed


# =====================================================================
# 1. URL parsing
# =====================================================================
_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")
_GITHUB_HOSTS = {"github.com", "www.github.com"}


def parse_github_url(url: str) -> Tuple[str, str]:
    """Return (owner, repo) from any common form of GitHub repo URL.

    Accepts https/http URLs (with or without www, trailing slash, ``.git``, or
    extra path like ``/tree/main/src``), SSH ``git@github.com:owner/repo.git``,
    and bare ``owner/repo`` shorthand.
    """
    if not url or not url.strip():
        raise InvalidURLError("No URL was provided.")
    raw = url.strip()

    ssh = re.match(r"^git@github\.com:(?P<path>.+)$", raw)
    if ssh:
        path = ssh.group("path")
    elif re.match(r"^[\w.-]+/[\w.-]+/?$", raw) and "." not in raw.split("/")[0]:
        path = raw  # shorthand owner/repo
    else:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
            raw = "https://" + raw  # allow "github.com/owner/repo"
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https"):
            raise InvalidURLError(f"Unsupported URL scheme '{parsed.scheme}'.")
        host = (parsed.hostname or "").lower()
        if host not in _GITHUB_HOSTS:
            if host.endswith("github.com") or host == "gist.github.com":
                raise InvalidURLError(f"'{host}' is not a repository URL.")
            raise InvalidURLError(f"'{host or url}' is not a GitHub URL. Only github.com repositories are supported.")
        path = parsed.path

    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise InvalidURLError(f"'{url}' does not include both an owner and a repository name.")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    reserved = {"orgs", "settings", "marketplace", "explore", "topics", "features", "sponsors", "login"}
    if owner.lower() in reserved:
        raise InvalidURLError(f"'{url}' points to a GitHub page, not a repository.")
    if not _NAME_RE.match(owner) or not _NAME_RE.match(repo):
        raise InvalidURLError(f"'{owner}/{repo}' is not a valid GitHub owner/repository name.")
    return owner, repo


# =====================================================================
# 2. File classification & tree
# =====================================================================
IGNORED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", ".env", "__pycache__", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", "bower_components",
    "vendor", "dist", "build", "target", ".next", ".nuxt", ".svelte-kit", ".gradle",
    ".idea", ".vscode", "coverage", ".cache", ".parcel-cache", "Pods", ".terraform",
}

CODE_EXT = {
    ".py": "Python", ".pyi": "Python", ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue", ".svelte": "Svelte",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".scala": "Scala", ".groovy": "Groovy", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++",
    ".cxx": "C++", ".hpp": "C++", ".cs": "C#", ".fs": "F#", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C", ".dart": "Dart",
    ".lua": "Lua", ".r": "R", ".jl": "Julia", ".ex": "Elixir", ".exs": "Elixir",
    ".erl": "Erlang", ".hs": "Haskell", ".clj": "Clojure", ".ml": "OCaml", ".zig": "Zig",
    ".nim": "Nim", ".pl": "Perl", ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".ps1": "PowerShell", ".sql": "SQL", ".sol": "Solidity", ".html": "HTML",
    ".css": "CSS", ".scss": "SCSS", ".sass": "SCSS", ".less": "Less",
}
CONFIG_EXT = {
    ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml",
    ".lock", ".properties", ".gradle", ".env", ".editorconfig", ".plist", ".tf", ".hcl",
}
CONFIG_NAMES = {
    "dockerfile", "makefile", "procfile", "jenkinsfile", "vagrantfile", "gemfile", "rakefile",
    ".gitignore", ".gitattributes", ".dockerignore", ".npmrc", ".nvmrc", ".prettierrc",
    ".eslintrc", ".babelrc", ".editorconfig", "pipfile", "brewfile", "justfile", "caddyfile",
}
DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc", ".org", ".tex"}


def is_ignored(path: str) -> bool:
    return any(part in IGNORED_DIRS for part in path.split("/")[:-1]) or path.split("/")[0] in IGNORED_DIRS


def classify(path: str) -> str:
    """'code' | 'config' | 'doc' | 'other'"""
    name = posixpath.basename(path).lower()
    ext = posixpath.splitext(name)[1]
    if name in CONFIG_NAMES or name.startswith("dockerfile") or name.startswith(".env"):
        return "config"
    if re.match(r"^(requirements|constraints)[\w.-]*\.(txt|in)$", name) or name == "cmakelists.txt":
        return "config"
    if ext in CODE_EXT:
        return "code"
    if ext in CONFIG_EXT or name.endswith("rc"):
        return "config"
    if ext in DOC_EXT or name in {"license", "readme", "changelog", "authors", "notice"}:
        return "doc"
    return "other"


@dataclass
class RepoFiles:
    """The repository tree split into analysable buckets (ignored dirs removed)."""

    files: List[str]                       # blob paths
    dirs: List[str]                        # tree paths
    sizes: Dict[str, int] = field(default_factory=dict)
    all_files: List[str] = field(default_factory=list)  # including ignored dirs (for evidence)

    @classmethod
    def from_tree(cls, entries: Iterable[dict]) -> "RepoFiles":
        files, dirs, sizes, all_files = [], [], {}, []
        for e in entries:
            path, typ = e.get("path", ""), e.get("type")
            if not path:
                continue
            if typ == "blob":
                all_files.append(path)
                if not is_ignored(path):
                    files.append(path)
                    sizes[path] = int(e.get("size") or 0)
            elif typ == "tree" and not is_ignored(path + "/x") and posixpath.basename(path) not in IGNORED_DIRS:
                dirs.append(path)
        return cls(files=files, dirs=dirs, sizes=sizes, all_files=all_files)

    def by_kind(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = defaultdict(list)
        for f in self.files:
            out[classify(f)].append(f)
        return out


@dataclass
class TreeNode:
    name: str
    is_dir: bool
    children: Dict[str, "TreeNode"] = field(default_factory=dict)
    file_count: int = 0  # recursive count of files below (dirs only)


def build_tree(files: Sequence[str], dirs: Sequence[str] = ()) -> TreeNode:
    """Build a nested TreeNode structure from flat paths."""
    root = TreeNode(name="", is_dir=True)
    for d in dirs:
        node = root
        for part in d.split("/"):
            node = node.children.setdefault(part, TreeNode(part, True))
    for f in files:
        parts = f.split("/")
        node = root
        node.file_count += 1
        for part in parts[:-1]:
            node = node.children.setdefault(part, TreeNode(part, True))
            node.file_count += 1
        node.children.setdefault(parts[-1], TreeNode(parts[-1], False))
    return root


def sorted_children(node: TreeNode) -> List[TreeNode]:
    """Directories first, then files, both alphabetical (case-insensitive)."""
    return sorted(node.children.values(), key=lambda n: (not n.is_dir, n.name.lower()))


# ---- Purpose of each major (top-level) area --------------------------------
# Well-known folder names -> purpose. Matched case-insensitively on the exact name.
AREA_PURPOSES: Dict[str, str] = {}
for _names, _purpose in [
    (("src", "source", "lib", "libs", "pkg", "internal", "core"), "Main source code"),
    (("app", "apps"), "Application code"),
    (("cmd",), "Command-line entry points (Go convention)"),
    (("packages", "modules", "crates"), "Monorepo packages / sub-projects"),
    (("tests", "test", "spec", "specs", "__tests__", "e2e", "testing", "integration_tests"), "Automated tests"),
    (("docs", "doc", "documentation", "site", "website", "book", "manual"), "Documentation"),
    (("docs_src", "examples", "example", "samples", "sample", "demo", "demos", "tutorials"), "Example / tutorial code"),
    (("scripts", "script", "bin", "tools", "tooling", "hack", "utils"), "Helper scripts & developer tooling"),
    ((".github",), "GitHub config: CI workflows, issue/PR templates, Dependabot"),
    ((".circleci", ".gitlab", "ci", ".buildkite"), "CI pipeline configuration"),
    ((".devcontainer",), "Dev-container (VS Code / Codespaces) setup"),
    (("config", "configs", "conf", "settings", "etc"), "Configuration files"),
    (("public", "static", "assets", "images", "img", "media", "resources", "res", "fonts"), "Static assets (images, fonts, public files)"),
    (("components", "ui", "views", "pages", "layouts", "templates", "screens"), "UI components / page templates"),
    (("api", "server", "backend", "routes", "handlers", "controllers"), "API / server-side code"),
    (("client", "frontend", "web"), "Front-end / client code"),
    (("migrations", "db", "database", "schema", "prisma", "sql"), "Database schema & migrations"),
    (("deploy", "deployment", "infra", "infrastructure", "k8s", "kubernetes", "helm", "terraform", "charts", "docker", "ops"), "Deployment & infrastructure"),
    (("benchmarks", "benchmark", "bench", "perf"), "Performance benchmarks"),
    (("i18n", "locales", "locale", "translations", "lang"), "Translations / localisation"),
    (("data", "datasets", "fixtures", "testdata"), "Data files / fixtures"),
    (("notebooks",), "Jupyter notebooks"),
    (("models",), "Data models / ML models"),
    (("android", "ios", "macos", "windows", "linux"), "Platform-specific build project"),
    (("third_party", "external", "extern"), "Bundled third-party code"),
    ((".husky",), "Git hooks"),
]:
    for _n in _names:
        AREA_PURPOSES[_n] = _purpose


@dataclass
class AreaSummary:
    name: str
    files: int
    kinds: Dict[str, int]
    top_exts: List[Tuple[str, int]]
    purpose: str
    basis: str  # 'name' | 'repo name' | 'contents' | 'unknown'


def _ext_label(path: str) -> str:
    name = posixpath.basename(path)
    ext = posixpath.splitext(name)[1].lower()
    if ext:
        return ext
    return name if classify(path) == "config" else "(no ext)"


def describe_areas(repo_files: RepoFiles, repo_name: str = "", limit: int = 12) -> List[AreaSummary]:
    """Summarise each top-level folder: size, file types and purpose.

    Purpose comes from, in order:
      1. a folder named like the repository (or its Python-import form) -> its main package,
      2. a well-known folder name (AREA_PURPOSES),
      3. what it contains, only when one kind of file is a clear majority (>= 70%),
      4. otherwise "Purpose not determined" — we never guess.
    Root-level files are summarised as one extra pseudo-area "(root files)".
    """
    groups: Dict[str, List[str]] = defaultdict(list)
    root_files: List[str] = []
    for f in repo_files.files:
        if "/" in f:
            groups[f.split("/", 1)[0]].append(f)
        else:
            root_files.append(f)

    repo_keys = {repo_name.lower(), repo_name.lower().replace("-", "_"), repo_name.lower().replace("-", "")} - {""}
    content_purpose = {"code": "Mostly source code", "doc": "Mostly documentation / text files",
                       "config": "Mostly configuration files", "other": "Mostly data / asset files"}

    out: List[AreaSummary] = []
    for name, files in groups.items():
        kinds = Counter(classify(f) for f in files)
        exts = Counter(_ext_label(f) for f in files).most_common(3)
        key = name.lower()
        if key in repo_keys:
            purpose, basis = "Main package source", "repo name"
        elif key in AREA_PURPOSES:
            purpose, basis = AREA_PURPOSES[key], "name"
        else:
            kind, n = kinds.most_common(1)[0]
            if n / len(files) >= 0.7:
                purpose, basis = content_purpose[kind], "contents"
            else:
                purpose, basis = "Purpose not determined (mixed contents)", "unknown"
        out.append(AreaSummary(name, len(files), dict(kinds), exts, purpose, basis))

    out.sort(key=lambda a: -a.files)
    hidden = out[limit:]
    out = out[:limit]
    if hidden:
        n_files = sum(a.files for a in hidden)
        out.append(AreaSummary(f"… {len(hidden)} more folder(s)", n_files, {}, [], "Smaller folders, not summarised", "unknown"))
    if root_files:
        kinds = Counter(classify(f) for f in root_files)
        exts = Counter(_ext_label(f) for f in root_files).most_common(3)
        priority = ("readme", "license", "licence", "pyproject.toml", "package.json", "go.mod", "cargo.toml",
                    "requirements.txt", "dockerfile", "makefile", "contributing", "security", "changelog")
        notable = sorted(root_files, key=lambda f: next((i for i, p in enumerate(priority)
                                                         if f.lower().startswith(p)), 99))[:4]
        out.append(AreaSummary("(root files)", len(root_files), dict(kinds), exts,
                               "Top-level files, e.g. " + ", ".join(notable), "contents"))
    return out


# =====================================================================
# 3. Dependency manifests
# =====================================================================
MANIFEST_ECOSYSTEM = {
    "package.json": "npm",
    "requirements.txt": "pip",
    "requirements-dev.txt": "pip",
    "dev-requirements.txt": "pip",
    "pyproject.toml": "pip",
    "setup.py": "pip",
    "setup.cfg": "pip",
    "pipfile": "pip",
    "go.mod": "go",
    "cargo.toml": "cargo",
    "gemfile": "bundler",
    "composer.json": "composer",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "pubspec.yaml": "pub",
    "mix.exs": "hex",
    "package.swift": "swiftpm",
}
LOCKFILES = {
    "npm": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "npm-shrinkwrap.json"],
    "pip": ["poetry.lock", "pipfile.lock", "uv.lock", "pdm.lock", "requirements.lock", "conda-lock.yml"],
    "go": ["go.sum"],
    "cargo": ["cargo.lock"],
    "bundler": ["gemfile.lock"],
    "composer": ["composer.lock"],
    "gradle": ["gradle.lockfile"],
    "pub": ["pubspec.lock"],
    "hex": ["mix.lock"],
    "swiftpm": ["package.resolved"],
    "maven": [],  # Maven has no standard lockfile; pinned versions in pom.xml are the norm
}
PARSABLE_MANIFESTS = {"package.json", "requirements.txt", "requirements-dev.txt", "dev-requirements.txt",
                      "pyproject.toml", "go.mod", "cargo.toml", "composer.json", "pipfile"}


def find_manifests(files: Sequence[str], limit: int = 8) -> List[str]:
    """Manifest paths worth downloading: shallowest first, capped to save API quota."""
    hits = [f for f in files if posixpath.basename(f).lower() in PARSABLE_MANIFESTS]
    hits.sort(key=lambda p: (p.count("/"), p))
    return hits[:limit]


@dataclass
class Dependency:
    name: str
    spec: str
    ecosystem: str
    source: str        # manifest path
    pin: str           # 'exact' | 'range' | 'unpinned' | 'url'
    scope: str = "runtime"   # runtime | optional | dev | peer | indirect

    @property
    def dev(self) -> bool:
        """True for dev/test/build-only dependencies."""
        return self.scope == "dev"


def _classify_npm_spec(spec: str) -> str:
    s = spec.strip()
    if not s or s in {"*", "latest", "x", "next"}:
        return "unpinned"
    if re.match(r"^(git\+|git:|https?:|github:|file:|link:|workspace:)", s) or "/" in s and not s[0].isdigit():
        return "url"
    if re.match(r"^v?\d+\.\d+\.\d+([-+][\w.]+)?$", s):
        return "exact"
    return "range"


_PEP508_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


def _classify_pep508(rest: str) -> str:
    rest = rest.split(";")[0].strip()  # drop environment markers
    if rest.startswith("@") or "://" in rest:
        return "url"
    if not rest:
        return "unpinned"
    if re.match(r"^\(?\s*===?\s*[\w.*+!-]+\s*\)?$", rest) and "*" not in rest:
        return "exact"
    return "range"


def parse_requirements_txt(text: str, source: str) -> List[Dependency]:
    deps = []
    for line in text.splitlines():
        line = line.split(" #")[0].strip()
        if not line or line.startswith(("#", "-r", "-c", "--", "-i", "-f")):
            continue
        if line.startswith(("-e", "git+", "http://", "https://")):
            name = re.sub(r".*#egg=", "", line) if "#egg=" in line else line.split("/")[-1]
            deps.append(Dependency(name[:60], line, "pip", source, "url"))
            continue
        m = _PEP508_RE.match(line)
        if m:
            deps.append(Dependency(m.group(1), (m.group(3) or "").strip(), "pip", source, _classify_pep508(m.group(3) or "")))
    return deps


def parse_package_json(text: str, source: str) -> List[Dependency]:
    data = json.loads(text)
    deps = []
    for key, scope in (("dependencies", "runtime"), ("devDependencies", "dev"), ("peerDependencies", "peer"), ("optionalDependencies", "optional")):
        section = data.get(key) or {}
        if isinstance(section, dict):
            for name, spec in section.items():
                spec = str(spec)
                deps.append(Dependency(name, spec, "npm", source, _classify_npm_spec(spec), scope=scope))
    return deps


def parse_pyproject(text: str, source: str) -> List[Dependency]:
    if _toml is None:
        return []
    data = _toml.loads(text)
    deps: List[Dependency] = []
    project = data.get("project") or {}
    for req in project.get("dependencies") or []:
        m = _PEP508_RE.match(str(req))
        if m:
            deps.append(Dependency(m.group(1), m.group(3).strip(), "pip", source, _classify_pep508(m.group(3))))
    for group in (project.get("optional-dependencies") or {}).values():
        for req in group or []:
            m = _PEP508_RE.match(str(req))
            if m:
                deps.append(Dependency(m.group(1), m.group(3).strip(), "pip", source, _classify_pep508(m.group(3)), scope="optional"))
    # PEP 735 dependency groups (used by uv, pdm, FastAPI…): {"dev": ["pytest>=8", {"include-group": "x"}]}
    for group in (data.get("dependency-groups") or {}).values():
        for req in group or []:
            if not isinstance(req, str):  # {"include-group": ...} references another group
                continue
            m = _PEP508_RE.match(req)
            if m:
                deps.append(Dependency(m.group(1), m.group(3).strip(), "pip", source, _classify_pep508(m.group(3)), scope="dev"))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    sections = [(poetry.get("dependencies") or {}, "runtime"), (poetry.get("dev-dependencies") or {}, "dev")]
    for grp in (poetry.get("group") or {}).values():
        sections.append(((grp or {}).get("dependencies") or {}, "dev"))
    for section, scope in sections:
        for name, spec in section.items():
            if name.lower() == "python":
                continue
            if isinstance(spec, dict):
                pin = "url" if ("git" in spec or "path" in spec or "url" in spec) else _classify_poetry(str(spec.get("version", "")))
                spec = str(spec.get("version", spec.get("git", "")))
            else:
                pin = _classify_poetry(str(spec))
            deps.append(Dependency(name, str(spec), "pip", source, pin, scope=scope))
    return deps


def _classify_poetry(spec: str) -> str:
    s = spec.strip()
    if not s or s == "*":
        return "unpinned"
    if re.match(r"^=?=?\d+(\.\d+)*$", s):
        return "exact"
    return "range"


def parse_go_mod(text: str, source: str) -> List[Dependency]:
    deps, in_block = [], False
    for raw in text.splitlines():
        line = raw.split("//")[0].strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        m = None
        if in_block:
            m = re.match(r"^(\S+)\s+(v\S+)", line)
        elif line.startswith("require "):
            m = re.match(r"^require\s+(\S+)\s+(v\S+)", line)
        if m:
            indirect = "indirect" in raw
            deps.append(Dependency(m.group(1), m.group(2), "go", source, "exact", scope="indirect" if indirect else "runtime"))
    return deps


def parse_cargo_toml(text: str, source: str) -> List[Dependency]:
    if _toml is None:
        return []
    data = _toml.loads(text)
    deps = []
    for key, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev"), ("build-dependencies", "dev")):
        for name, spec in (data.get(key) or {}).items():
            if isinstance(spec, dict):
                if "git" in spec or "path" in spec:
                    pin, s = "url", str(spec.get("git") or spec.get("path"))
                elif spec.get("workspace"):
                    pin, s = "range", "workspace"
                else:
                    s = str(spec.get("version", ""))
                    pin = _classify_cargo(s)
            else:
                s = str(spec)
                pin = _classify_cargo(s)
            deps.append(Dependency(name, s, "cargo", source, pin, scope=scope))
    return deps


def _classify_cargo(s: str) -> str:
    s = s.strip()
    if not s or s == "*":
        return "unpinned"
    return "exact" if s.startswith("=") else "range"  # Cargo "1.2" means ^1.2


def parse_composer_json(text: str, source: str) -> List[Dependency]:
    data = json.loads(text)
    deps = []
    for key, scope in (("require", "runtime"), ("require-dev", "dev")):
        for name, spec in (data.get(key) or {}).items():
            if name == "php" or name.startswith("ext-"):
                continue
            deps.append(Dependency(name, str(spec), "composer", source, _classify_npm_spec(str(spec)), scope=scope))
    return deps


def parse_pipfile(text: str, source: str) -> List[Dependency]:
    if _toml is None:
        return []
    data = _toml.loads(text)
    deps = []
    for key, scope in (("packages", "runtime"), ("dev-packages", "dev")):
        for name, spec in (data.get(key) or {}).items():
            s = spec if isinstance(spec, str) else str((spec or {}).get("version", ""))
            pin = "url" if isinstance(spec, dict) and ("git" in spec or "path" in spec) else (
                "unpinned" if s in ("", "*") else ("exact" if s.startswith("==") else "range"))
            deps.append(Dependency(name, s, "pip", source, pin, scope=scope))
    return deps


def dedupe_dependencies(deps: Sequence[Dependency]) -> List[Dependency]:
    """Collapse repeats of the same requirement (e.g. pydantic listed in several groups).

    Two entries are the same if ecosystem, name and declared version match; the most
    "shipped" scope wins (runtime > optional > peer > dev > indirect). Every count in the
    report (health, complexity, dependency table) uses this unique list.
    """
    rank = {"runtime": 0, "optional": 1, "peer": 2, "dev": 3, "indirect": 4}
    best: Dict[Tuple[str, str, str], Dependency] = {}
    for d in deps:
        key = (d.ecosystem, d.name.lower().replace("_", "-"), d.spec.replace(" ", ""))
        if key not in best or rank.get(d.scope, 9) < rank.get(best[key].scope, 9):
            best[key] = d
    return list(best.values())


_PARSERS = {
    "package.json": parse_package_json,
    "requirements.txt": parse_requirements_txt,
    "requirements-dev.txt": parse_requirements_txt,
    "dev-requirements.txt": parse_requirements_txt,
    "pyproject.toml": parse_pyproject,
    "go.mod": parse_go_mod,
    "cargo.toml": parse_cargo_toml,
    "composer.json": parse_composer_json,
    "pipfile": parse_pipfile,
}


def parse_manifests(manifests: Dict[str, str]) -> Tuple[List[Dependency], List[str]]:
    """Parse every downloaded manifest. Returns (dependencies, warnings)."""
    deps: List[Dependency] = []
    warnings: List[str] = []
    for path, text in manifests.items():
        parser = _PARSERS.get(posixpath.basename(path).lower())
        if not parser:
            continue
        try:
            deps.extend(parser(text, path))
        except Exception as exc:  # malformed manifest must never break the report
            warnings.append(f"Could not parse {path}: {exc.__class__.__name__}")
    return deps, warnings


# =====================================================================
# 4. Tech-stack detection
# =====================================================================
@dataclass
class TechFinding:
    name: str
    category: str
    evidence: List[str] = field(default_factory=list)
    dev_only: bool = False   # every piece of evidence is a dev/test-only dependency


# (regex on full path, technology, category). Matched case-insensitively.
FILE_RULES: List[Tuple[str, str, str]] = [
    (r"(^|/)package\.json$", "Node.js / npm", "Runtime & Build"),
    (r"(^|/)tsconfig(\.[\w-]+)?\.json$", "TypeScript", "Language"),
    (r"(^|/)(requirements[\w-]*\.txt|setup\.py|setup\.cfg)$", "Python (pip)", "Runtime & Build"),
    (r"(^|/)pyproject\.toml$", "Python (pyproject)", "Runtime & Build"),
    (r"(^|/)poetry\.lock$", "Poetry", "Package Manager"),
    (r"(^|/)uv\.lock$", "uv", "Package Manager"),
    (r"(^|/)pipfile(\.lock)?$", "Pipenv", "Package Manager"),
    (r"(^|/)(environment\.ya?ml)$", "Conda", "Package Manager"),
    (r"(^|/)yarn\.lock$", "Yarn", "Package Manager"),
    (r"(^|/)pnpm-(lock|workspace)\.yaml$", "pnpm", "Package Manager"),
    (r"(^|/)bun\.lockb?$", "Bun", "Runtime & Build"),
    (r"(^|/)deno\.jsonc?$", "Deno", "Runtime & Build"),
    (r"(^|/)go\.mod$", "Go modules", "Runtime & Build"),
    (r"(^|/)cargo\.toml$", "Cargo (Rust)", "Runtime & Build"),
    (r"(^|/)pom\.xml$", "Maven", "Runtime & Build"),
    (r"(^|/)build\.gradle(\.kts)?$", "Gradle", "Runtime & Build"),
    (r"(^|/)gemfile$", "Bundler (Ruby)", "Runtime & Build"),
    (r"(^|/)composer\.json$", "Composer (PHP)", "Runtime & Build"),
    (r"(^|/)pubspec\.yaml$", "Flutter / Dart pub", "Framework"),
    (r"\.(csproj|sln|fsproj)$", ".NET / MSBuild", "Runtime & Build"),
    (r"(^|/)package\.swift$", "Swift Package Manager", "Runtime & Build"),
    (r"(^|/)mix\.exs$", "Mix (Elixir)", "Runtime & Build"),
    (r"(^|/)cmakelists\.txt$", "CMake", "Runtime & Build"),
    (r"(^|/)(makefile|gnumakefile)$", "Make", "Tooling"),
    (r"(^|/)dockerfile([.\w-]*)?$|\.dockerfile$", "Docker", "DevOps & CI"),
    (r"(^|/)(docker-)?compose(\.[\w-]+)?\.ya?ml$", "Docker Compose", "DevOps & CI"),
    (r"^\.github/workflows/[^/]+\.ya?ml$", "GitHub Actions", "DevOps & CI"),
    (r"(^|/)\.gitlab-ci\.yml$", "GitLab CI", "DevOps & CI"),
    (r"(^|/)jenkinsfile$", "Jenkins", "DevOps & CI"),
    (r"^\.circleci/config\.ya?ml$", "CircleCI", "DevOps & CI"),
    (r"(^|/)\.travis\.yml$", "Travis CI", "DevOps & CI"),
    (r"\.tf$", "Terraform", "DevOps & CI"),
    (r"(^|/)chart\.yaml$", "Helm", "DevOps & CI"),
    (r"(^|/)(k8s|kubernetes)/.*\.ya?ml$", "Kubernetes manifests", "DevOps & CI"),
    (r"(^|/)vercel\.json$", "Vercel", "DevOps & CI"),
    (r"(^|/)netlify\.toml$", "Netlify", "DevOps & CI"),
    (r"(^|/)firebase\.json$", "Firebase", "DevOps & CI"),
    (r"(^|/)next\.config\.(js|mjs|cjs|ts)$", "Next.js", "Framework"),
    (r"(^|/)nuxt\.config\.(js|ts|mjs)$", "Nuxt", "Framework"),
    (r"(^|/)angular\.json$", "Angular", "Framework"),
    (r"(^|/)svelte\.config\.(js|ts|mjs)$", "SvelteKit", "Framework"),
    (r"(^|/)astro\.config\.(mjs|js|ts)$", "Astro", "Framework"),
    (r"(^|/)vite\.config\.(js|ts|mjs|cjs)$", "Vite", "Tooling"),
    (r"(^|/)webpack\.config\.(js|ts|cjs)$", "Webpack", "Tooling"),
    (r"(^|/)tailwind\.config\.(js|ts|cjs|mjs)$", "Tailwind CSS", "Framework"),
    (r"(^|/)manage\.py$", "Django", "Framework"),
    (r"(^|/)(\.eslintrc(\.\w+)?|eslint\.config\.(js|mjs|cjs|ts))$", "ESLint", "Tooling"),
    (r"(^|/)\.prettierrc(\.\w+)?$", "Prettier", "Tooling"),
    (r"(^|/)jest\.config\.(js|ts|cjs|mjs)$", "Jest", "Testing"),
    (r"(^|/)vitest\.config\.(js|ts|mjs)$", "Vitest", "Testing"),
    (r"(^|/)(pytest\.ini|conftest\.py)$", "pytest", "Testing"),
    (r"(^|/)tox\.ini$", "tox", "Testing"),
    (r"(^|/)playwright\.config\.(js|ts)$", "Playwright", "Testing"),
    (r"(^|/)cypress\.config\.(js|ts)$", "Cypress", "Testing"),
    (r"(^|/)\.pre-commit-config\.yaml$", "pre-commit", "Tooling"),
    (r"(^|/)(ruff\.toml|\.ruff\.toml)$", "Ruff", "Tooling"),
    (r"(^|/)mkdocs\.ya?ml$", "MkDocs", "Tooling"),
    (r"(^|/)schema\.prisma$", "Prisma", "Database & ORM"),
    (r"(^|/)alembic\.ini$", "Alembic", "Database & ORM"),
]
_COMPILED_RULES = [(re.compile(p, re.I), n, c) for p, n, c in FILE_RULES]

# Dependency name -> (technology, category), per ecosystem.
DEP_RULES: Dict[str, Dict[str, Tuple[str, str]]] = {
    "npm": {
        "react": ("React", "Framework"), "next": ("Next.js", "Framework"), "vue": ("Vue.js", "Framework"),
        "nuxt": ("Nuxt", "Framework"), "svelte": ("Svelte", "Framework"), "@sveltejs/kit": ("SvelteKit", "Framework"),
        "@angular/core": ("Angular", "Framework"), "solid-js": ("SolidJS", "Framework"), "astro": ("Astro", "Framework"),
        "express": ("Express", "Framework"), "fastify": ("Fastify", "Framework"), "koa": ("Koa", "Framework"),
        "@nestjs/core": ("NestJS", "Framework"), "hono": ("Hono", "Framework"), "electron": ("Electron", "Framework"),
        "react-native": ("React Native", "Framework"), "expo": ("Expo", "Framework"),
        "typescript": ("TypeScript", "Language"), "tailwindcss": ("Tailwind CSS", "Framework"),
        "vite": ("Vite", "Tooling"), "webpack": ("Webpack", "Tooling"), "esbuild": ("esbuild", "Tooling"),
        "jest": ("Jest", "Testing"), "vitest": ("Vitest", "Testing"), "mocha": ("Mocha", "Testing"),
        "@playwright/test": ("Playwright", "Testing"), "cypress": ("Cypress", "Testing"),
        "prisma": ("Prisma", "Database & ORM"), "@prisma/client": ("Prisma", "Database & ORM"),
        "mongoose": ("Mongoose / MongoDB", "Database & ORM"), "sequelize": ("Sequelize", "Database & ORM"),
        "typeorm": ("TypeORM", "Database & ORM"), "drizzle-orm": ("Drizzle ORM", "Database & ORM"),
        "graphql": ("GraphQL", "Library"), "socket.io": ("Socket.IO", "Library"), "three": ("Three.js", "Library"),
        "@reduxjs/toolkit": ("Redux", "Library"), "redux": ("Redux", "Library"),
        "openai": ("OpenAI SDK", "AI / ML"), "@anthropic-ai/sdk": ("Anthropic SDK", "AI / ML"),
        "langchain": ("LangChain", "AI / ML"), "@tensorflow/tfjs": ("TensorFlow.js", "AI / ML"),
    },
    "pip": {
        "django": ("Django", "Framework"), "flask": ("Flask", "Framework"), "fastapi": ("FastAPI", "Framework"),
        "starlette": ("Starlette", "Framework"), "tornado": ("Tornado", "Framework"), "aiohttp": ("aiohttp", "Framework"),
        "streamlit": ("Streamlit", "Framework"), "gradio": ("Gradio", "Framework"),
        "pydantic": ("Pydantic", "Library"), "celery": ("Celery", "Library"), "uvicorn": ("Uvicorn", "Runtime & Build"),
        "sqlalchemy": ("SQLAlchemy", "Database & ORM"), "alembic": ("Alembic", "Database & ORM"),
        "pytest": ("pytest", "Testing"), "hypothesis": ("Hypothesis", "Testing"),
        "numpy": ("NumPy", "AI / ML"), "pandas": ("pandas", "AI / ML"), "scikit-learn": ("scikit-learn", "AI / ML"),
        "torch": ("PyTorch", "AI / ML"), "tensorflow": ("TensorFlow", "AI / ML"), "jax": ("JAX", "AI / ML"),
        "transformers": ("Hugging Face Transformers", "AI / ML"), "langchain": ("LangChain", "AI / ML"),
        "openai": ("OpenAI SDK", "AI / ML"), "anthropic": ("Anthropic SDK", "AI / ML"),
        "typer": ("Typer", "Library"), "click": ("Click", "Library"), "rich": ("Rich", "Library"),
        "ruff": ("Ruff", "Tooling"), "mypy": ("mypy", "Tooling"), "black": ("Black", "Tooling"),
    },
    "go": {
        "github.com/gin-gonic/gin": ("Gin", "Framework"), "github.com/labstack/echo": ("Echo", "Framework"),
        "github.com/gofiber/fiber": ("Fiber", "Framework"), "github.com/go-chi/chi": ("chi", "Framework"),
        "github.com/spf13/cobra": ("Cobra", "Library"), "gorm.io/gorm": ("GORM", "Database & ORM"),
        "google.golang.org/grpc": ("gRPC", "Library"), "github.com/stretchr/testify": ("testify", "Testing"),
    },
    "cargo": {
        "tokio": ("Tokio", "Runtime & Build"), "actix-web": ("Actix Web", "Framework"), "axum": ("Axum", "Framework"),
        "rocket": ("Rocket", "Framework"), "serde": ("Serde", "Library"), "clap": ("clap", "Library"),
        "bevy": ("Bevy", "Framework"), "tauri": ("Tauri", "Framework"), "diesel": ("Diesel", "Database & ORM"),
        "sqlx": ("SQLx", "Database & ORM"),
    },
    "composer": {"laravel/framework": ("Laravel", "Framework"), "symfony/framework-bundle": ("Symfony", "Framework"),
                 "phpunit/phpunit": ("PHPUnit", "Testing")},
}

CATEGORY_ORDER = ["Language", "Framework", "Library", "AI / ML", "Database & ORM", "Runtime & Build",
                  "Package Manager", "Testing", "DevOps & CI", "Tooling"]


def _dep_rule(ecosystem: str, name: str) -> Optional[Tuple[str, str]]:
    rules = DEP_RULES.get(ecosystem, {})
    key = name.lower()
    if ecosystem == "go":  # match module path prefixes such as github.com/labstack/echo/v4
        for prefix, val in rules.items():
            if key == prefix or key.startswith(prefix + "/"):
                return val
        return None
    return rules.get(key) or rules.get(key.replace("_", "-"))


def detect_tech_stack(
    repo_files: RepoFiles,
    languages: Dict[str, int],
    deps: Sequence[Dependency],
    max_evidence: int = 3,
) -> List[TechFinding]:
    findings: Dict[str, TechFinding] = {}
    has_non_dev: Dict[str, bool] = {}

    def add(name: str, category: str, evidence: str, dev: bool = False) -> None:
        f = findings.setdefault(name, TechFinding(name, category))
        has_non_dev[name] = has_non_dev.get(name, False) or not dev
        if evidence not in f.evidence:
            f.evidence.append(evidence)

    # (a) Languages: GitHub Linguist byte counts + our own extension counts.
    ext_counts: Counter = Counter()
    for path in repo_files.files:
        lang = CODE_EXT.get(posixpath.splitext(path.lower())[1])
        if lang:
            ext_counts[lang] += 1
    total_bytes = sum(languages.values()) or 0
    for lang, size in sorted(languages.items(), key=lambda kv: -kv[1]):
        share = 100 * size / total_bytes if total_bytes else 0
        if share < 0.5:
            continue
        ev = f"GitHub Linguist: {share:.1f}% of code ({_human_bytes(size)})"
        add(lang, "Language", ev)
        if ext_counts.get(lang):
            add(lang, "Language", f"{ext_counts[lang]} source file(s) in tree")
    if not languages:  # Linguist unavailable -> rely on extensions alone
        for lang, n in ext_counts.most_common(8):
            add(lang, "Language", f"{n} source file(s) by extension")

    # (b) Marker files.
    for path in repo_files.files:
        for rx, name, cat in _COMPILED_RULES:
            if rx.search(path):
                add(name, cat, path)

    # (c) Frameworks/libraries declared in manifests.
    for d in deps:
        rule = _dep_rule(d.ecosystem, d.name)
        if rule:
            spec = f"@{d.spec}" if d.spec and d.pin != "url" else ""
            tag = "" if d.scope == "runtime" else f" ({d.scope})"
            add(rule[0], rule[1], f"{d.source} → {d.name}{spec}{tag}", dev=d.dev)

    # Trim evidence and order by category, then by evidence weight.
    out = []
    for f in findings.values():
        f.dev_only = not has_non_dev.get(f.name, True)
        if len(f.evidence) > max_evidence:
            extra = len(f.evidence) - max_evidence
            f.evidence = f.evidence[:max_evidence] + [f"…and {extra} more"]
        out.append(f)
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    lang_rank = {k: i for i, k in enumerate(sorted(languages, key=lambda k: -languages[k]))}
    out.sort(key=lambda f: (order.get(f.category, 99), lang_rank.get(f.name, 999), f.name.lower()))
    return out


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"  # pragma: no cover


# =====================================================================
# 5. Complexity score
# =====================================================================
@dataclass
class ScoreComponent:
    name: str
    measured: str
    points: float
    max_points: float
    explanation: str


@dataclass
class ComplexityResult:
    score: int
    label: str
    color: str
    components: List[ScoreComponent]
    stats: Dict[str, float]


def compute_complexity(repo_files: RepoFiles, languages: Dict[str, int], dep_count: int) -> ComplexityResult:
    """Complexity score (0-100) = sum of five capped, documented components.

    | Component            | Max | Formula                                                     |
    |----------------------|-----|-------------------------------------------------------------|
    | Codebase size        | 35  | 35 * log10(code_files + 1) / log10(5001)   (5,000 files = max) |
    | Nesting depth        | 20  | 20 * min(1, (avg_depth - 1) / 5)           (avg depth 6 = max)  |
    | Language diversity   | 15  | 15 * min(1, (langs_over_2% - 1) / 4)       (5+ languages = max) |
    | Code density         | 15  | 15 * code_files / (code + config files)    (more logic, less glue) |
    | Dependency surface   | 15  | 15 * log10(deps + 1) / log10(301)          (300 deps = max)     |

    Depth = number of path segments for a code file (``src/app/main.py`` = 3).
    Logarithmic scales stop huge monorepos from saturating everything while still
    separating 20-file repos from 2,000-file ones.
    """
    kinds = repo_files.by_kind()
    code = kinds.get("code", [])
    config = kinds.get("config", [])
    n_code, n_cfg = len(code), len(config)

    depths = [p.count("/") + 1 for p in code] or [1]
    avg_depth = sum(depths) / len(depths)
    max_depth = max(depths)

    total = sum(languages.values())
    if total:
        n_langs = sum(1 for v in languages.values() if v / total >= 0.02)
    else:
        n_langs = len({CODE_EXT[posixpath.splitext(p.lower())[1]] for p in code})
    n_langs = max(n_langs, 1 if n_code else 0)

    density = n_code / (n_code + n_cfg) if (n_code + n_cfg) else 0.0

    comps = [
        ScoreComponent("Codebase size", f"{n_code:,} code files", 35 * math.log10(n_code + 1) / math.log10(5001), 35,
                       "log scale, 5,000 code files = max"),
        ScoreComponent("Nesting depth", f"avg {avg_depth:.1f} / max {max_depth}", 20 * min(1.0, max(0.0, avg_depth - 1) / 5), 20,
                       "average path depth of code files, 6 = max"),
        ScoreComponent("Language diversity", f"{n_langs} language(s) ≥2%", 15 * min(1.0, max(0, n_langs - 1) / 4), 15,
                       "5+ significant languages = max"),
        ScoreComponent("Code density", f"{density:.0%} code vs config", 15 * density, 15,
                       "share of code files among code + config files"),
        ScoreComponent("Dependency surface", f"{dep_count} declared deps", 15 * math.log10(dep_count + 1) / math.log10(301), 15,
                       "log scale, 300 dependencies = max"),
    ]
    for c in comps:
        c.points = round(min(c.max_points, c.points), 1)
    score = int(round(sum(c.points for c in comps)))
    score = max(0, min(100, score))
    if score <= 25:
        label, color = "Simple", "green"
    elif score <= 50:
        label, color = "Moderate", "yellow"
    elif score <= 75:
        label, color = "Complex", "dark_orange"
    else:
        label, color = "Very Complex", "red"
    stats = {"code_files": n_code, "config_files": n_cfg, "doc_files": len(kinds.get("doc", [])),
             "other_files": len(kinds.get("other", [])), "total_files": len(repo_files.files),
             "directories": len(repo_files.dirs), "avg_depth": avg_depth, "max_depth": max_depth}
    return ComplexityResult(score, label, color, comps, stats)


# =====================================================================
# 6. Dependency health
# =====================================================================
@dataclass
class HealthCheck:
    status: str   # 'pass' | 'warn' | 'fail' | 'info'
    text: str
    penalty: int = 0


@dataclass
class HealthResult:
    status: str               # 'Healthy' | 'Warning' | 'At Risk' | 'No Dependencies Found'
    color: str
    score: Optional[int]
    checks: List[HealthCheck]
    ecosystems: List[str]
    pin_counts: Dict[str, int]
    dep_total: int


def assess_dependency_health(
    repo_files: RepoFiles,
    deps: Sequence[Dependency],
    repo_meta: Dict,
    now: Optional[datetime] = None,
) -> HealthResult:
    """Score dependency hygiene out of 100 (start at 100, subtract penalties).

    "Healthy" means:
      * every detected ecosystem has a lockfile (reproducible installs)   -25 per missing, max -40
      * automated update tooling (Dependabot / Renovate) is configured     -15
      * a SECURITY.md policy exists                                        -5
      * CI is configured (so updates get tested)                           -10
      * few risk indicators: unpinned (`*`, `latest`, bare names) deps     up to -20
        and VCS/URL deps that bypass the registry                          up to -10
      * the project is maintained: not archived (-40), pushed < 1 year (-15)

    Status: >= 75 Healthy, 50-74 Warning, < 50 At Risk.
    """
    now = now or datetime.now(timezone.utc)
    names_lower = {f.lower() for f in repo_files.files}
    base_lower = Counter(posixpath.basename(f).lower() for f in repo_files.files)
    checks: List[HealthCheck] = []

    ecosystems = sorted({MANIFEST_ECOSYSTEM[posixpath.basename(f).lower()]
                         for f in repo_files.files if posixpath.basename(f).lower() in MANIFEST_ECOSYSTEM})
    pin_counts = Counter(d.pin for d in deps)

    if not ecosystems:
        checks.append(HealthCheck("info", "No recognizable dependency manifests (package.json, requirements.txt, "
                                          "pyproject.toml, go.mod, Cargo.toml, …) were found."))
        _maintenance_checks(repo_meta, now, checks)
        return HealthResult("No Dependencies Found", "grey62", None, checks, [], dict(pin_counts), 0)

    # Lockfiles
    lock_penalty = 0
    for eco in ecosystems:
        locks = LOCKFILES.get(eco, [])
        found = [l for l in locks if base_lower.get(l)]
        if found:
            checks.append(HealthCheck("pass", f"{eco}: lockfile present ({', '.join(found)})"))
        elif eco == "pip" and deps and all(d.pin in ("exact", "url") for d in deps if d.ecosystem == "pip"):
            checks.append(HealthCheck("pass", "pip: no lockfile, but all requirements are pinned with =="))
        elif not locks:
            checks.append(HealthCheck("info", f"{eco}: no standard lockfile for this ecosystem"))
        else:
            lock_penalty += 25
            checks.append(HealthCheck("fail", f"{eco}: no lockfile (expected one of {', '.join(locks[:3])})", 25))
    if lock_penalty > 40:  # cap the total lockfile penalty
        excess = lock_penalty - 40
        for c in checks:
            if c.status == "fail" and excess > 0:
                cut = min(c.penalty, excess)
                c.penalty -= cut
                excess -= cut

    # Automated updates
    bots = [p for p in (".github/dependabot.yml", ".github/dependabot.yaml", "renovate.json", "renovate.json5",
                        ".renovaterc", ".renovaterc.json", ".github/renovate.json", ".github/renovate.json5")
            if p in names_lower]
    if bots:
        checks.append(HealthCheck("pass", f"Automated dependency updates configured ({bots[0]})"))
    else:
        checks.append(HealthCheck("warn", "No Dependabot or Renovate config found", 15))

    # Security policy
    sec = [p for p in ("security.md", ".github/security.md", "docs/security.md") if p in names_lower]
    checks.append(HealthCheck("pass", f"Security policy present ({sec[0]})") if sec
                  else HealthCheck("warn", "No SECURITY.md policy", 5))

    # CI
    ci = any(f.startswith(".github/workflows/") for f in names_lower) or any(
        p in names_lower for p in (".gitlab-ci.yml", ".circleci/config.yml", ".travis.yml", "jenkinsfile",
                                   "azure-pipelines.yml"))
    checks.append(HealthCheck("pass", "CI pipeline configured") if ci
                  else HealthCheck("warn", "No CI configuration detected", 10))

    # Risk indicators from parsed manifests
    if deps:
        total = len(deps)
        unpinned = [d for d in deps if d.pin == "unpinned"]
        urls = [d for d in deps if d.pin == "url"]
        if unpinned:
            pen = min(20, round(40 * len(unpinned) / total) + 2)
            sample = ", ".join(d.name for d in unpinned[:4])
            checks.append(HealthCheck("warn" if pen < 15 else "fail",
                                      f"{len(unpinned)}/{total} deps have no version constraint ({sample}{'…' if len(unpinned) > 4 else ''})", pen))
        else:
            checks.append(HealthCheck("pass", f"All {total} unique deps declare a version constraint"))
        if urls:
            pen = min(10, 3 * len(urls))
            checks.append(HealthCheck("warn", f"{len(urls)} dep(s) installed from git/URL/path, bypassing the registry", pen))
    else:
        checks.append(HealthCheck("info", "Manifests found but none could be parsed for versions"))

    _maintenance_checks(repo_meta, now, checks)

    score = max(0, 100 - sum(c.penalty for c in checks))
    if score >= 75:
        status, color = "Healthy", "green"
    elif score >= 50:
        status, color = "Warning", "yellow"
    else:
        status, color = "At Risk", "red"
    return HealthResult(status, color, score, checks, ecosystems, dict(pin_counts), len(deps))


def _maintenance_checks(meta: Dict, now: datetime, checks: List[HealthCheck]) -> None:
    if meta.get("archived"):
        checks.append(HealthCheck("fail", "Repository is archived — dependencies will not receive updates", 40))
    pushed = parse_iso(meta.get("pushed_at"))
    if pushed:
        age = (now - pushed).days
        if age > 365:
            checks.append(HealthCheck("fail", f"Last push was {age} days ago (> 1 year) — likely unmaintained", 15))
        elif age > 180:
            checks.append(HealthCheck("warn", f"Last push was {age} days ago", 0))
        else:
            checks.append(HealthCheck("pass", f"Actively maintained (last push {age} day(s) ago)"))


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# =====================================================================
# 7. Commit activity
# =====================================================================
@dataclass
class ActivityResult:
    weeks: List[Tuple[datetime, int]]   # (week start, commits), oldest first
    total: int
    average: float
    peak: Tuple[Optional[datetime], int]
    active_weeks: int
    trend_pct: Optional[float]           # recent half vs older half
    source: str                          # which endpoint produced the data
    capped: bool = False


def week_start(dt: datetime) -> datetime:
    """Sunday 00:00 UTC of dt's week (GitHub's stats endpoints use Sunday-based weeks)."""
    dt = dt.astimezone(timezone.utc)
    start = dt - timedelta(days=(dt.weekday() + 1) % 7)
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def activity_from_stats(stats: Sequence[Dict], n_weeks: int) -> List[Tuple[datetime, int]]:
    rows = [(datetime.fromtimestamp(int(w["week"]), tz=timezone.utc), int(w.get("total", 0))) for w in stats if "week" in w]
    rows.sort()
    return rows[-n_weeks:]


def activity_from_dates(dates: Sequence[datetime], n_weeks: int, now: Optional[datetime] = None) -> List[Tuple[datetime, int]]:
    now = now or datetime.now(timezone.utc)
    current = week_start(now)
    starts = [current - timedelta(weeks=i) for i in range(n_weeks - 1, -1, -1)]
    counts = Counter(week_start(d) for d in dates)
    return [(s, counts.get(s, 0)) for s in starts]


def summarize_activity(weeks: List[Tuple[datetime, int]], source: str, capped: bool = False) -> ActivityResult:
    counts = [c for _, c in weeks]
    total = sum(counts)
    avg = total / len(counts) if counts else 0.0
    peak = max(weeks, key=lambda w: w[1]) if weeks and total else (None, 0)
    half = len(counts) // 2
    trend = None
    if half:
        old, new = sum(counts[:half]), sum(counts[-half:])
        if old:
            trend = 100 * (new - old) / old
        elif new:
            trend = 100.0
    return ActivityResult(weeks, total, avg, peak, sum(1 for c in counts if c), trend, source, capped)
