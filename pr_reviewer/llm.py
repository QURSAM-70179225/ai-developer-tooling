"""
Component 2: AI Evaluation Engine.

Sends the assignment's prompt blueprint (word for word) with the acceptance criteria and
the diff to an LLM, then reads the JSON answer *strictly*:

  * must parse as a JSON object (code fences / surrounding prose are tolerated and stripped)
  * "fulfilled" must exist and be a real boolean (the string "true" is rejected)
  * "feedback" must be a non-empty string
Anything else raises ReviewHalt("llm-response", ...), so a malformed answer can never merge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

import requests

from .config import Settings
from .errors import ReviewHalt
from .tickets import Ticket

# The prompt blueprint from the assignment brief, verbatim.
SYSTEM_PROMPT = (
    "You are an expert Senior Code Reviewer. Compare the provided Code Diff against the provided "
    "Jira/Azure Acceptance Criteria. Answer in JSON with two keys: fulfilled (true/false) and feedback "
    "(markdown string). If fulfilled is false, provide a precise list of missing requirements or code bugs. "
    "If true, provide a brief summary of the successful verification."
)


@dataclass
class Verdict:
    fulfilled: bool
    feedback: str
    raw: str = ""


def build_user_message(ticket: Ticket, diff: str) -> str:
    return (
        f"## Ticket {ticket.id}: {ticket.title}\n\n"
        f"### Acceptance Criteria\n{ticket.acceptance_criteria}\n\n"
        f"### Description\n{ticket.description or '(none)'}\n\n"
        f"## Code Diff\n```diff\n{diff}\n```\n\n"
        "Respond with only the JSON object."
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_verdict(text: Optional[str]) -> Verdict:
    raw = text or ""
    body = _FENCE.sub("", raw.strip()).strip()
    if not body.startswith("{"):  # tolerate prose around the object, never inside it
        start, end = body.find("{"), body.rfind("}")
        body = body[start:end + 1] if 0 <= start < end else body
    try:
        data = json.loads(body)
    except ValueError:
        raise ReviewHalt("llm-response", "The LLM answer is not valid JSON.",
                         f"Answer began with: {raw[:120]!r}") from None
    if not isinstance(data, dict):
        raise ReviewHalt("llm-response", "The LLM answer is JSON but not an object with keys.")
    if "fulfilled" not in data:
        raise ReviewHalt("llm-response", "The LLM answer has no 'fulfilled' key.")
    if not isinstance(data["fulfilled"], bool):
        raise ReviewHalt("llm-response", f"'fulfilled' must be true or false, got {data['fulfilled']!r}.")
    feedback = data.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        raise ReviewHalt("llm-response", "The LLM answer has no usable 'feedback' text.")
    return Verdict(data["fulfilled"], feedback.strip(), raw)


class LLMClient:
    """Anthropic Messages API, or any OpenAI-compatible Chat Completions API (OpenAI, Groq, Ollama...)."""

    def __init__(self, settings: Settings, session: Optional[requests.Session] = None) -> None:
        self.s = settings
        self.session = session or requests.Session()

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        try:
            resp = self.session.post(url, headers=headers, json=payload, timeout=120)
        except requests.RequestException as exc:
            raise ReviewHalt("llm", f"Could not reach the LLM API: {exc.__class__.__name__}.") from None
        if resp.status_code != 200:
            try:
                err = resp.json().get("error")
                msg = err.get("message") if isinstance(err, dict) else err
            except (ValueError, AttributeError):
                msg = None
            hint = "Check the API key environment variable." if resp.status_code in (401, 403) else None
            raise ReviewHalt("llm", f"LLM API returned HTTP {resp.status_code}: {msg or resp.reason}", hint)
        try:
            return resp.json()
        except ValueError:
            raise ReviewHalt("llm", "LLM API returned a response that is not JSON.") from None

    def evaluate(self, ticket: Ticket, diff: str) -> Verdict:
        user = build_user_message(ticket, diff)
        if self.s.llm_provider == "anthropic":
            data = self._post("https://api.anthropic.com/v1/messages",
                              {"x-api-key": self.s.llm_api_key or "", "anthropic-version": "2023-06-01"},
                              {"model": self.s.llm_model, "max_tokens": 2000, "temperature": 0,
                               "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": user}]})
            try:
                text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            except (KeyError, TypeError, AttributeError):
                raise ReviewHalt("llm-response", "Unexpected response shape from the Anthropic API.") from None
        else:
            base = self.s.llm_base_url or "https://api.openai.com/v1"
            payload = {"model": self.s.llm_model, "temperature": 0,
                       "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                    {"role": "user", "content": user}]}
            if self.s.llm_json_mode:
                payload["response_format"] = {"type": "json_object"}
            headers = {"Authorization": f"Bearer {self.s.llm_api_key}"} if self.s.llm_api_key else {}
            data = self._post(f"{base}/chat/completions", headers, payload)
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise ReviewHalt("llm-response", "Unexpected response shape from the LLM API.") from None
        return parse_verdict(text)
