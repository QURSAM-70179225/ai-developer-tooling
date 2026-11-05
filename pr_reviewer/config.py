"""
Settings, read only from environment variables, so no key or token ever lives in code.

| Variable              | Used for                                   | Default            |
|-----------------------|--------------------------------------------|--------------------|
| GITHUB_TOKEN          | Git platform API (diff, status, review, merge) | required        |
| WEBHOOK_SECRET        | Verifying webhook signatures (serve)       | required for serve |
| TICKET_SOURCE         | jira | azure | local                       | jira               |
| JIRA_BASE_URL         | e.g. https://your-site.atlassian.net       |                    |
| JIRA_EMAIL            | Jira account e-mail                         |                    |
| JIRA_API_TOKEN        | Jira API token                              |                    |
| JIRA_AC_FIELD         | custom field id holding acceptance criteria | (search description) |
| AZDO_ORG / AZDO_PROJECT / AZDO_PAT | Azure DevOps work items        |                    |
| LOCAL_TICKETS_FILE    | JSON file of tickets (TICKET_SOURCE=local) | tickets.json       |
| LLM_PROVIDER          | anthropic | openai (any OpenAI-compatible API) | anthropic       |
| ANTHROPIC_API_KEY / OPENAI_API_KEY | LLM key (or LLM_API_KEY)      |                    |
| LLM_MODEL             | model name                                  | per provider       |
| LLM_BASE_URL          | OpenAI-compatible endpoint (Groq, OpenRouter, Ollama) | api.openai.com |
| MAX_DIFF_CHARS        | larger diffs are refused, not truncated    | 60000              |
| DRY_RUN               | 1 = print actions, never post or merge     | 1                  |
| MERGE_METHOD          | merge | squash | rebase                    | squash             |
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Mapping, Optional

from .errors import ConfigError

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4o"}


def _flag(value: Optional[str], default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class Settings:
    github_token: Optional[str] = None
    webhook_secret: Optional[str] = None
    ticket_source: str = "jira"
    jira_base_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None
    jira_ac_field: Optional[str] = None
    azdo_org: Optional[str] = None
    azdo_project: Optional[str] = None
    azdo_pat: Optional[str] = None
    local_tickets_file: str = "tickets.json"
    llm_provider: str = "anthropic"
    llm_api_key: Optional[str] = None
    llm_model: str = DEFAULT_MODELS["anthropic"]
    llm_base_url: Optional[str] = None
    llm_json_mode: bool = True
    max_diff_chars: int = 60_000
    dry_run: bool = True
    merge_method: str = "squash"
    status_context: str = "ai-pr-reviewer"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        g = lambda k: (env.get(k) or "").strip() or None  # noqa: E731
        provider = (g("LLM_PROVIDER") or "anthropic").lower()
        if provider not in DEFAULT_MODELS:
            raise ConfigError(f"LLM_PROVIDER must be 'anthropic' or 'openai', not '{provider}'.")
        key = g("LLM_API_KEY") or (g("ANTHROPIC_API_KEY") if provider == "anthropic" else g("OPENAI_API_KEY"))
        source = (g("TICKET_SOURCE") or "jira").lower()
        if source not in ("jira", "azure", "local"):
            raise ConfigError(f"TICKET_SOURCE must be jira, azure or local, not '{source}'.")
        method = (g("MERGE_METHOD") or "squash").lower()
        if method not in ("merge", "squash", "rebase"):
            raise ConfigError(f"MERGE_METHOD must be merge, squash or rebase, not '{method}'.")
        try:
            max_diff = int(g("MAX_DIFF_CHARS") or 60_000)
        except ValueError:
            raise ConfigError("MAX_DIFF_CHARS must be a whole number.") from None
        return cls(
            github_token=g("GITHUB_TOKEN") or g("GH_TOKEN"),
            webhook_secret=g("WEBHOOK_SECRET"),
            ticket_source=source,
            jira_base_url=(g("JIRA_BASE_URL") or "").rstrip("/") or None,
            jira_email=g("JIRA_EMAIL"),
            jira_api_token=g("JIRA_API_TOKEN"),
            jira_ac_field=g("JIRA_AC_FIELD"),
            azdo_org=g("AZDO_ORG"),
            azdo_project=g("AZDO_PROJECT"),
            azdo_pat=g("AZDO_PAT"),
            local_tickets_file=g("LOCAL_TICKETS_FILE") or "tickets.json",
            llm_provider=provider,
            llm_api_key=key,
            llm_model=g("LLM_MODEL") or DEFAULT_MODELS[provider],
            llm_base_url=(g("LLM_BASE_URL") or "").rstrip("/") or None,
            llm_json_mode=_flag(env.get("LLM_JSON_MODE"), True),
            max_diff_chars=max_diff,
            dry_run=_flag(env.get("DRY_RUN"), True),
            merge_method=method,
        )

    def missing(self, serving: bool = False) -> List[str]:
        """Names of required variables that are not set, for a friendly pre-flight message."""
        out = []
        if not self.github_token:
            out.append("GITHUB_TOKEN")
        if serving and not self.webhook_secret:
            out.append("WEBHOOK_SECRET")
        if not self.llm_api_key and not (self.llm_provider == "openai" and self.llm_base_url):
            out.append("ANTHROPIC_API_KEY" if self.llm_provider == "anthropic" else "OPENAI_API_KEY")
        if self.ticket_source == "jira":
            out += [n for n, v in (("JIRA_BASE_URL", self.jira_base_url), ("JIRA_EMAIL", self.jira_email),
                                   ("JIRA_API_TOKEN", self.jira_api_token)) if not v]
        elif self.ticket_source == "azure":
            out += [n for n, v in (("AZDO_ORG", self.azdo_org), ("AZDO_PROJECT", self.azdo_project),
                                   ("AZDO_PAT", self.azdo_pat)) if not v]
        return out
