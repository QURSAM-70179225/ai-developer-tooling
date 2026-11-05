"""
Component 1a: Story Fetcher.

1. Extract the ticket ID from the PR title and branch name, e.g. "feature/JIRA-123-login-page".
   No ID -> halt. Two *different* IDs (title says ABC-1, branch says ABC-2) -> halt: never guess.
2. Fetch the ticket from Jira (REST v3), Azure DevOps (work items API) or a local JSON file
   (for demos without a ticket system), and extract its acceptance criteria.
   Ticket missing, unreachable, or without acceptance criteria -> halt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, List, Optional

import requests

from .config import Settings
from .errors import ReviewHalt

# Jira keys: PROJECT-123, uppercase anywhere in the title or branch. Branches are often lowercased
# ("feature/jira-123-x"); a lowercase key only counts at the start of a path segment, so GitHub's
# default branch names like "patch-1" are NOT mistaken for ticket PATCH-1.
_JIRA_TITLE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9_]+-\d+)(?![A-Za-z0-9])")
_JIRA_BRANCH_LOWER = re.compile(r"(?:(?<=/))([A-Za-z][A-Za-z0-9_]+-\d+)(?![A-Za-z0-9])")
# Azure Boards: "AB#123" anywhere, or a branch like "feature/123-login".
_AZDO_ANY = re.compile(r"\bAB#(\d+)\b", re.I)
_AZDO_BRANCH = re.compile(r"(?:^|/)(\d+)(?=[-_/]|$)")


@dataclass
class Ticket:
    id: str
    title: str
    description: str
    acceptance_criteria: str
    url: Optional[str] = None


# ------------------------------------------------------------------ ticket id
def extract_ticket_id(title: str, branch: str, source: str = "jira") -> str:
    title, branch = title or "", branch or ""
    if source == "azure":
        found = _AZDO_ANY.findall(title) + _AZDO_ANY.findall(branch) + _AZDO_BRANCH.findall(branch)
    else:  # jira and local both use PROJECT-123 style keys
        found = [m.upper() for m in _JIRA_TITLE.findall(title) + _JIRA_TITLE.findall(branch)
                 + _JIRA_BRANCH_LOWER.findall(branch)]
    unique: List[str] = list(dict.fromkeys(found))
    if not unique:
        example = "AB#123 or feature/123-login" if source == "azure" else "PROJ-123 or feature/PROJ-123-login"
        raise ReviewHalt("ticket-id", f"No ticket ID found in PR title {title!r} or branch {branch!r}.",
                         f"Name the branch or title with the ticket ID, e.g. {example}.")
    if len(unique) > 1:
        raise ReviewHalt("ticket-id", f"Found several different ticket IDs ({', '.join(unique)}); "
                                      "refusing to guess which one this PR implements.",
                         "Reference exactly one ticket in the PR title and branch.")
    return unique[0]


# ------------------------------------------------------------ text extraction
def adf_to_text(node: Any) -> str:
    """Flatten Jira's Atlassian Document Format (REST v3) into plain text with line breaks."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    inner = adf_to_text(node.get("content", []))
    if t == "listItem":
        return "- " + inner.strip() + "\n"
    if t in ("paragraph", "heading", "codeBlock", "blockquote", "rule"):
        return inner.rstrip() + "\n"
    return inner


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in ("br", "p", "div", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: Optional[str]) -> str:
    """Azure DevOps stores rich text as HTML."""
    if not html:
        return ""
    p = _HTMLText()
    p.feed(html)
    text = "".join(p.parts)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


_AC_HEADING = re.compile(r"(?im)^\s*#*\s*\**\s*acceptance\s+criteria\s*\**\s*:?\s*$|acceptance\s+criteria\s*:", re.I)


def criteria_from_description(description: str) -> str:
    """The text after an 'Acceptance Criteria' heading inside a description, if any."""
    m = _AC_HEADING.search(description or "")
    return description[m.end():].strip() if m else ""


def _check_criteria(ticket: Ticket) -> Ticket:
    if not ticket.acceptance_criteria.strip():
        raise ReviewHalt("acceptance-criteria", f"Ticket {ticket.id} has no acceptance criteria.",
                         "Add an 'Acceptance Criteria' section (or the configured field) to the ticket.")
    return ticket


# ------------------------------------------------------------------ fetchers
def _get(session: requests.Session, url: str, ticket_id: str, system: str, **kw) -> dict:
    try:
        resp = session.get(url, timeout=20, **kw)
    except requests.RequestException as exc:
        raise ReviewHalt("ticket-fetch", f"Could not reach {system}: {exc.__class__.__name__}.") from None
    if resp.status_code == 404:
        raise ReviewHalt("ticket-fetch", f"Ticket {ticket_id} was not found in {system} (HTTP 404).",
                         "Check the ID, and that the API user can see that project.")
    if resp.status_code in (401, 403):
        raise ReviewHalt("ticket-fetch", f"{system} rejected the credentials (HTTP {resp.status_code}).",
                         "Check the API token / PAT environment variables and their permissions.")
    if resp.status_code != 200:
        raise ReviewHalt("ticket-fetch", f"{system} returned HTTP {resp.status_code} for ticket {ticket_id}.")
    try:
        return resp.json()
    except ValueError:
        raise ReviewHalt("ticket-fetch", f"{system} returned a response that is not JSON.") from None


class JiraFetcher:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None) -> None:
        self.s = settings
        self.session = session or requests.Session()

    def fetch(self, key: str) -> Ticket:
        fields = "summary,description" + (f",{self.s.jira_ac_field}" if self.s.jira_ac_field else "")
        data = _get(self.session, f"{self.s.jira_base_url}/rest/api/3/issue/{key}", key, "Jira",
                    params={"fields": fields}, auth=(self.s.jira_email or "", self.s.jira_api_token or ""),
                    headers={"Accept": "application/json"})
        f = data.get("fields") or {}
        description = adf_to_text(f.get("description")).strip()
        criteria = adf_to_text(f.get(self.s.jira_ac_field)).strip() if self.s.jira_ac_field else ""
        criteria = criteria or criteria_from_description(description)
        return _check_criteria(Ticket(key, f.get("summary") or "", description, criteria,
                                      f"{self.s.jira_base_url}/browse/{key}"))


class AzureDevOpsFetcher:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None) -> None:
        self.s = settings
        self.session = session or requests.Session()

    def fetch(self, work_item_id: str) -> Ticket:
        base = f"https://dev.azure.com/{self.s.azdo_org}/{self.s.azdo_project}"
        data = _get(self.session, f"{base}/_apis/wit/workitems/{work_item_id}", work_item_id, "Azure DevOps",
                    params={"api-version": "7.1"}, auth=("", self.s.azdo_pat or ""))
        f = data.get("fields") or {}
        description = html_to_text(f.get("System.Description"))
        criteria = html_to_text(f.get("Microsoft.VSTS.Common.AcceptanceCriteria")) or criteria_from_description(description)
        return _check_criteria(Ticket(work_item_id, f.get("System.Title") or "", description, criteria,
                                      f"{base}/_workitems/edit/{work_item_id}"))


class LocalFetcher:
    """Tickets from a JSON file: {"DEMO-1": {"title": ..., "description": ..., "acceptance_criteria": ...}}.
    For demos and tests without a Jira/Azure account; behaves exactly like the real fetchers."""

    def __init__(self, settings: Settings) -> None:
        self.path = settings.local_tickets_file

    def fetch(self, key: str) -> Ticket:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise ReviewHalt("ticket-fetch", f"Local tickets file '{self.path}' does not exist.",
                             "Create it, or set LOCAL_TICKETS_FILE.") from None
        except ValueError as exc:
            raise ReviewHalt("ticket-fetch", f"'{self.path}' is not valid JSON: {exc}.") from None
        t = {k.upper(): v for k, v in data.items()}.get(key.upper())
        if not isinstance(t, dict):
            raise ReviewHalt("ticket-fetch", f"Ticket {key} was not found in '{self.path}'.")
        criteria = t.get("acceptance_criteria") or ""
        if isinstance(criteria, list):
            criteria = "\n".join(f"- {c}" for c in criteria)
        description = t.get("description") or ""
        return _check_criteria(Ticket(key.upper(), t.get("title") or "", description,
                                      criteria or criteria_from_description(description), t.get("url")))


def make_fetcher(settings: Settings, session: Optional[requests.Session] = None):
    if settings.ticket_source == "azure":
        return AzureDevOpsFetcher(settings, session)
    if settings.ticket_source == "local":
        return LocalFetcher(settings)
    return JiraFetcher(settings, session)
