"""Offline tests for Track A (AI PR Reviewer). No network, tokens or LLM needed.

The central safety property, checked in every failure scenario: NO merge call is ever made.
"""

import hashlib
import hmac
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

import requests

from pr_reviewer import cli
from pr_reviewer.config import Settings
from pr_reviewer.engine import Reviewer
from pr_reviewer.errors import ConfigError, ReviewHalt
from pr_reviewer.git_platform import GitHubPlatform, PREvent, parse_github_event
from pr_reviewer.llm import SYSTEM_PROMPT, LLMClient, Verdict, parse_verdict
from pr_reviewer.server import Dispatcher, handle_webhook, make_server, verify_signature
from pr_reviewer.tickets import (AzureDevOpsFetcher, JiraFetcher, LocalFetcher, Ticket, adf_to_text,
                                 criteria_from_description, extract_ticket_id, html_to_text)


# ---------------------------------------------------------------- fakes
def resp(status=200, body=None, text=None):
    r = requests.Response()
    r.status_code = status
    r._content = (text if text is not None else json.dumps(body if body is not None else {})).encode()
    return r


class FakeSession(requests.Session):
    """Answers (METHOD, url-suffix) routes and records every call."""

    def __init__(self, routes=None):
        super().__init__()
        self.routes = routes or {}
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, suffix), r in self.routes.items():
            if m == method and url.split("?")[0].endswith(suffix):
                out = r(kw) if callable(r) else r
                if isinstance(out, Exception):
                    raise out
                return out
        return resp(404, {"message": "Not Found"})

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def made(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].split("?")[0].endswith(suffix)]


DIFF = "diff --git a/app.py b/app.py\n+def greet(name):\n+    return f'Hello, {name}!'\n"
PR = PREvent("github", "me/test-repo", 7, "DEMO-1 Add greeting", "feature/DEMO-1-greeting", "main", "abc1234def")
TICKET = Ticket("DEMO-1", "Add greeting", "Say hello", "- greet(name) returns 'Hello, <name>!'")


def github(diff=DIFF, merge=None, review=None):
    return FakeSession({
        ("GET", "/repos/me/test-repo/pulls/7"): resp(200, text=diff),
        ("POST", "/statuses/abc1234def"): resp(201, {}),
        # "is None", not "or": a requests.Response with a 4xx/5xx status is falsy
        ("POST", "/pulls/7/reviews"): resp(200, {}) if review is None else review,
        ("POST", "/issues/7/comments"): resp(201, {}),
        ("PUT", "/pulls/7/merge"): resp(200, {"sha": "m3rg3d", "merged": True}) if merge is None else merge,
    })


class FakeFetcher:
    def __init__(self, ticket=TICKET, error=None):
        self.ticket, self.error, self.asked = ticket, error, []

    def fetch(self, key):
        self.asked.append(key)
        if self.error:
            raise self.error
        return self.ticket


class FakeLLM:
    def __init__(self, text=None, verdict=None, error=None):
        self.text, self.verdict, self.error, self.calls = text, verdict, error, 0

    def evaluate(self, ticket, diff):
        self.calls += 1
        if self.error:
            raise self.error
        return self.verdict or parse_verdict(self.text)


def run(llm, gh=None, fetcher=None, pr=PR, dry_run=False, **settings):
    gh = gh or github()
    s = Settings(github_token="t", llm_api_key="k", ticket_source="local", dry_run=dry_run, **settings)
    out = Reviewer(s, GitHubPlatform("t", gh), fetcher or FakeFetcher(), llm).review(pr)
    return out, gh


YES = '{"fulfilled": true, "feedback": "All criteria verified: greet() returns the greeting."}'
NO = '{"fulfilled": false, "feedback": "- Missing: greet() is never exported\\n- Bug: no test"}'


# ---------------------------------------------------------------- ticket ids
class TestTicketId(unittest.TestCase):
    def test_from_branch_like_the_brief(self):
        self.assertEqual(extract_ticket_id("Login page", "feature/JIRA-123-login-page"), "JIRA-123")

    def test_lowercase_branch_and_title(self):
        self.assertEqual(extract_ticket_id("Add login", "feature/jira-123-login"), "JIRA-123")
        self.assertEqual(extract_ticket_id("PROJ-9: fix", "main"), "PROJ-9")

    def test_github_default_branch_names_are_not_tickets(self):
        for branch in ("patch-1", "patch-12", "dependabot-3"):
            with self.assertRaises(ReviewHalt, msg=branch):
                extract_ticket_id("Update readme", branch)
        self.assertEqual(extract_ticket_id("Fix", "user/JIRA-5-x"), "JIRA-5")

    def test_same_id_in_title_and_branch_is_fine(self):
        self.assertEqual(extract_ticket_id("PROJ-9 fix", "bugfix/PROJ-9-x"), "PROJ-9")

    def test_no_id_halts(self):
        with self.assertRaises(ReviewHalt) as c:
            extract_ticket_id("Update readme", "patch-1")
        self.assertEqual(c.exception.stage, "ticket-id")

    def test_conflicting_ids_halt_instead_of_guessing(self):
        with self.assertRaises(ReviewHalt) as c:
            extract_ticket_id("PROJ-1 thing", "feature/PROJ-2-other")
        self.assertIn("refusing to guess", c.exception.reason)

    def test_azure_ids(self):
        self.assertEqual(extract_ticket_id("Fix AB#42", "main", "azure"), "42")
        self.assertEqual(extract_ticket_id("Fix", "feature/42-login", "azure"), "42")
        with self.assertRaises(ReviewHalt):
            extract_ticket_id("Fix", "feature/login", "azure")


# ---------------------------------------------------------------- tickets
ADF = {"type": "doc", "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "Users can log in."}]},
    {"type": "heading", "content": [{"type": "text", "text": "Acceptance Criteria"}]},
    {"type": "bulletList", "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Form has email + password"}]}]},
        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Wrong password shows an error"}]}]}]}]}


class TestTickets(unittest.TestCase):
    def test_adf_and_criteria_from_description(self):
        text = adf_to_text(ADF)
        self.assertIn("- Form has email + password", text)
        ac = criteria_from_description(text)
        self.assertTrue(ac.startswith("- Form has email"))
        self.assertNotIn("Users can log in", ac)

    def test_jira_fetch(self):
        s = Settings(jira_base_url="https://x.atlassian.net", jira_email="a@b.c", jira_api_token="t")
        sess = FakeSession({("GET", "/rest/api/3/issue/JIRA-123"): resp(200, {"fields": {"summary": "Login page", "description": ADF}})})
        t = JiraFetcher(s, sess).fetch("JIRA-123")
        self.assertEqual((t.id, t.title), ("JIRA-123", "Login page"))
        self.assertIn("Wrong password", t.acceptance_criteria)
        self.assertEqual(t.url, "https://x.atlassian.net/browse/JIRA-123")
        self.assertEqual(sess.calls[0][2]["auth"], ("a@b.c", "t"))

    def test_jira_custom_field(self):
        s = Settings(jira_base_url="https://x", jira_ac_field="customfield_10050")
        sess = FakeSession({("GET", "/issue/P-1"): resp(200, {"fields": {"summary": "S", "description": None,
                                                                       "customfield_10050": "Must do X"}})})
        self.assertEqual(JiraFetcher(s, sess).fetch("P-1").acceptance_criteria, "Must do X")

    def test_ticket_not_found_and_no_criteria_halt(self):
        s = Settings(jira_base_url="https://x")
        with self.assertRaises(ReviewHalt) as c:
            JiraFetcher(s, FakeSession()).fetch("P-404")
        self.assertEqual(c.exception.stage, "ticket-fetch")
        sess = FakeSession({("GET", "/issue/P-1"): resp(200, {"fields": {"summary": "S", "description": "No criteria here"}})})
        with self.assertRaises(ReviewHalt) as c:
            JiraFetcher(s, sess).fetch("P-1")
        self.assertEqual(c.exception.stage, "acceptance-criteria")

    def test_jira_unreachable_and_auth(self):
        s = Settings(jira_base_url="https://x")
        boom = FakeSession({("GET", "/issue/P-1"): requests.exceptions.ConnectionError()})
        with self.assertRaises(ReviewHalt):
            JiraFetcher(s, boom).fetch("P-1")
        with self.assertRaises(ReviewHalt) as c:
            JiraFetcher(s, FakeSession({("GET", "/issue/P-1"): resp(401, {})})).fetch("P-1")
        self.assertIn("credentials", c.exception.reason)

    def test_azure_fetch(self):
        s = Settings(ticket_source="azure", azdo_org="org", azdo_project="proj", azdo_pat="p")
        sess = FakeSession({("GET", "/_apis/wit/workitems/42"): resp(200, {"fields": {
            "System.Title": "Login", "System.Description": "<div>Users log in</div>",
            "Microsoft.VSTS.Common.AcceptanceCriteria": "<ul><li>Email field</li><li>Error on bad password</li></ul>"}})})
        t = AzureDevOpsFetcher(s, sess).fetch("42")
        self.assertIn("- Email field", t.acceptance_criteria)
        self.assertIn("dev.azure.com/org/proj", sess.calls[0][1])
        self.assertEqual(html_to_text("<p>a</p><p>b</p>"), "a\nb")

    def test_local_tickets(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tickets.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"DEMO-1": {"title": "T", "acceptance_criteria": ["a", "b"]},
                           "DEMO-2": {"title": "No AC"}}, fh)
            f = LocalFetcher(Settings(local_tickets_file=path))
            self.assertEqual(f.fetch("demo-1").acceptance_criteria, "- a\n- b")
            with self.assertRaises(ReviewHalt):
                f.fetch("DEMO-2")
            with self.assertRaises(ReviewHalt):
                f.fetch("DEMO-3")
        with self.assertRaises(ReviewHalt):
            LocalFetcher(Settings(local_tickets_file="/nope/x.json")).fetch("DEMO-1")


# ---------------------------------------------------------------- verdict parsing
class TestVerdict(unittest.TestCase):
    def test_valid_plain_fenced_and_with_prose(self):
        self.assertTrue(parse_verdict(YES).fulfilled)
        self.assertFalse(parse_verdict("```json\n" + NO + "\n```").fulfilled)
        self.assertTrue(parse_verdict("Here is my review:\n" + YES + "\nThanks").fulfilled)

    def test_unsafe_answers_halt(self):
        for bad, why in [("I think it's fine!", "not valid JSON"), ("[1, 2]", "not an object"),
                         ('{"feedback": "ok"}', "no 'fulfilled'"), ('{"fulfilled": "true", "feedback": "x"}', "must be true or false"),
                         ('{"fulfilled": 1, "feedback": "x"}', "must be true or false"), ('{"fulfilled": true}', "feedback"),
                         ('{"fulfilled": true, "feedback": "  "}', "feedback"), ("", "not valid JSON"), (None, "not valid JSON")]:
            with self.assertRaises(ReviewHalt, msg=bad) as c:
                parse_verdict(bad)
            self.assertEqual(c.exception.stage, "llm-response")
            self.assertIn(why, c.exception.reason)


# ---------------------------------------------------------------- engine: the brief's scenarios
class TestEngine(unittest.TestCase):
    def assertNoMerge(self, gh):
        self.assertEqual(gh.made("PUT", "/merge"), [], "a merge was attempted!")

    def test_fulfilled_true_merges_pinned_to_reviewed_sha(self):
        out, gh = run(FakeLLM(YES))
        self.assertEqual(out.decision, "merged")
        merge = gh.made("PUT", "/pulls/7/merge")
        self.assertEqual(len(merge), 1)
        self.assertEqual(merge[0][2]["json"]["sha"], "abc1234def")
        self.assertEqual(merge[0][2]["json"]["merge_method"], "squash")
        states = [c[2]["json"]["state"] for c in gh.made("POST", "/statuses/abc1234def")]
        self.assertEqual(states, ["pending", "success"])

    def test_fulfilled_false_blocks_with_review_and_failed_status(self):
        out, gh = run(FakeLLM(NO))
        self.assertEqual(out.decision, "blocked")
        self.assertNoMerge(gh)
        self.assertEqual([c[2]["json"]["state"] for c in gh.made("POST", "/statuses/abc1234def")], ["pending", "failure"])
        review = gh.made("POST", "/pulls/7/reviews")[0][2]["json"]
        self.assertEqual(review["event"], "REQUEST_CHANGES")
        self.assertIn("never exported", review["body"])

    def test_own_pr_review_refused_falls_back_to_comment(self):
        gh = github(review=resp(422, {"message": "Can not request changes on your own pull request"}))
        out, gh = run(FakeLLM(NO), gh)
        self.assertEqual(out.decision, "blocked")
        self.assertIn("never exported", gh.made("POST", "/issues/7/comments")[0][2]["json"]["body"])
        self.assertNoMerge(gh)

    def _halted(self, out, gh, stage):
        self.assertEqual(out.decision, "halted")
        self.assertEqual(out.stage, stage)
        self.assertNoMerge(gh)
        self.assertEqual(gh.made("POST", "/statuses/abc1234def")[-1][2]["json"]["state"], "failure")
        body = gh.made("POST", "/issues/7/comments")[-1][2]["json"]["body"]
        self.assertIn("will not be merged", body)
        self.assertIn(stage, body)

    def test_no_ticket_id(self):
        llm = FakeLLM(YES)
        out, gh = run(llm, pr=PREvent("github", "me/test-repo", 7, "Update stuff", "patch-1", "main", "abc1234def"))
        self._halted(out, gh, "ticket-id")
        self.assertEqual(llm.calls, 0)

    def test_ticket_cannot_be_fetched(self):
        out, gh = run(FakeLLM(YES), fetcher=FakeFetcher(error=ReviewHalt("ticket-fetch", "Ticket DEMO-1 was not found")))
        self._halted(out, gh, "ticket-fetch")

    def test_ticket_without_acceptance_criteria(self):
        out, gh = run(FakeLLM(YES), fetcher=FakeFetcher(error=ReviewHalt("acceptance-criteria", "no acceptance criteria")))
        self._halted(out, gh, "acceptance-criteria")

    def test_llm_not_json(self):
        self._halted(*run(FakeLLM("Looks good to me, ship it!")), "llm-response")

    def test_llm_missing_fulfilled_key(self):
        self._halted(*run(FakeLLM('{"feedback": "great work"}')), "llm-response")

    def test_llm_api_error(self):
        self._halted(*run(FakeLLM(error=ReviewHalt("llm", "LLM API returned HTTP 500"))), "llm")

    def test_empty_diff(self):
        llm = FakeLLM(YES)
        out, gh = run(llm, github(diff="  \n"))
        self._halted(out, gh, "diff")
        self.assertEqual(llm.calls, 0)

    def test_diff_too_large_is_refused_not_truncated(self):
        llm = FakeLLM(YES)
        out, gh = run(llm, github(diff="+x\n" * 50_000), max_diff_chars=60_000)
        self._halted(out, gh, "diff")
        self.assertIn("too large", out.reason)
        self.assertEqual(llm.calls, 0)  # never sent to the LLM

    def test_diff_fetch_error(self):
        gh = github()
        gh.routes[("GET", "/repos/me/test-repo/pulls/7")] = resp(500, {"message": "boom"})
        self._halted(*run(FakeLLM(YES), gh), "diff")

    def test_merge_refused_by_github_is_reported(self):
        out, gh = run(FakeLLM(YES), github(merge=resp(405, {"message": "Pull Request is not mergeable"})))
        self.assertEqual(out.decision, "halted")
        self.assertEqual(out.stage, "merge")
        self.assertIn("not mergeable", out.reason)

    def test_status_write_failure_prevents_merge(self):
        gh = github()
        gh.routes[("POST", "/statuses/abc1234def")] = resp(403, {"message": "Resource not accessible"})
        out, gh = run(FakeLLM(YES), gh)
        self.assertEqual(out.decision, "halted")
        self.assertNoMerge(gh)

    def test_dry_run_makes_no_writes_at_all(self):
        for text in (YES, NO, "garbage"):
            out, gh = run(FakeLLM(text), dry_run=True)
            writes = [c for c in gh.calls if c[0] in ("POST", "PUT", "PATCH", "DELETE")]
            self.assertEqual(writes, [], text)
            self.assertTrue(all(a.startswith("[dry-run] would") for a in out.actions))
        out, _ = run(FakeLLM(YES), dry_run=True)
        self.assertEqual(out.decision, "merged")
        self.assertTrue(any("would merge into 'main'" in a for a in out.actions))


# ---------------------------------------------------------------- LLM client
class TestLLMClient(unittest.TestCase):
    def test_anthropic_request_uses_blueprint_prompt(self):
        sess = FakeSession({("POST", "/v1/messages"): resp(200, {"content": [{"type": "text", "text": YES}]})})
        v = LLMClient(Settings(llm_provider="anthropic", llm_api_key="sk", llm_model="m"), sess).evaluate(TICKET, DIFF)
        self.assertTrue(v.fulfilled)
        kw = sess.calls[0][2]
        self.assertEqual(kw["json"]["system"], SYSTEM_PROMPT)
        self.assertIn("Compare the provided Code Diff against the provided Jira/Azure Acceptance Criteria", SYSTEM_PROMPT)
        self.assertIn(DIFF, kw["json"]["messages"][0]["content"])
        self.assertIn(TICKET.acceptance_criteria, kw["json"]["messages"][0]["content"])
        self.assertEqual(kw["headers"]["x-api-key"], "sk")

    def test_openai_compatible_with_custom_base_url(self):
        sess = FakeSession({("POST", "/chat/completions"): resp(200, {"choices": [{"message": {"content": NO}}]})})
        s = Settings(llm_provider="openai", llm_api_key="k", llm_model="llama", llm_base_url="http://localhost:11434/v1")
        self.assertFalse(LLMClient(s, sess).evaluate(TICKET, DIFF).fulfilled)
        self.assertTrue(sess.calls[0][1].startswith("http://localhost:11434/v1"))
        self.assertEqual(sess.calls[0][2]["json"]["response_format"], {"type": "json_object"})

    def test_http_errors_become_halts(self):
        sess = FakeSession({("POST", "/v1/messages"): resp(401, {"error": {"message": "invalid x-api-key"}})})
        with self.assertRaises(ReviewHalt) as c:
            LLMClient(Settings(llm_api_key="bad"), sess).evaluate(TICKET, DIFF)
        self.assertIn("invalid x-api-key", c.exception.reason)


# ---------------------------------------------------------------- webhook
def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_payload(action="opened", draft=False):
    return {"action": action, "repository": {"full_name": "me/test-repo"},
            "pull_request": {"number": 7, "state": "open", "draft": draft, "title": "DEMO-1 Add greeting",
                             "html_url": "u", "user": {"login": "me"},
                             "head": {"ref": "feature/DEMO-1-greeting", "sha": "abc1234def"}, "base": {"ref": "main"}}}


class TestWebhook(unittest.TestCase):
    def test_signature(self):
        body = b'{"a":1}'
        self.assertTrue(verify_signature("s3cret", body, sign("s3cret", body)))
        self.assertFalse(verify_signature("s3cret", body, sign("wrong", body)))
        self.assertFalse(verify_signature("s3cret", body, None))

    def test_handler_paths(self):
        got = []
        submit = lambda d, e: (got.append(e), True)[1]  # noqa: E731
        body = json.dumps(pr_payload()).encode()
        h = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign("s", body), "X-GitHub-Delivery": "1"}
        self.assertEqual(handle_webhook(h, body, "s", submit)[0], 202)
        self.assertEqual(got[0].branch, "feature/DEMO-1-greeting")
        self.assertEqual(handle_webhook({**h, "X-Hub-Signature-256": "sha256=00"}, body, "s", submit)[0], 401)
        ping = json.dumps({"zen": "hi"}).encode()
        self.assertEqual(handle_webhook({"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign("s", ping)}, ping, "s", submit),
                         (200, {"ignored": "ping received, webhook is connected"}))
        self.assertEqual(handle_webhook({"X-GitHub-Event": "pull_request"}, b"not json", None, submit)[0], 400)

    def test_irrelevant_events_ignored(self):
        self.assertIsNone(parse_github_event("pull_request", pr_payload("closed"))[0])
        self.assertIsNone(parse_github_event("pull_request", pr_payload(draft=True))[0])
        self.assertIsNone(parse_github_event("push", {})[0])
        self.assertIsNotNone(parse_github_event("pull_request", pr_payload("synchronize"))[0])

    def test_real_http_server_and_duplicate_delivery(self):
        reviewed = threading.Event()
        seen = []

        def handle(e):
            seen.append(e)
            reviewed.set()

        server = make_server("127.0.0.1", 0, "s", Dispatcher(handle))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/webhook"
            body = json.dumps(pr_payload()).encode()
            hdr = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign("s", body),
                   "X-GitHub-Delivery": "d-1", "Content-Type": "application/json"}
            for expected in (202, 200):  # second identical delivery is ignored
                r = urllib.request.urlopen(urllib.request.Request(url, body, hdr))
                self.assertEqual(r.status, expected)
            self.assertTrue(reviewed.wait(5))
            self.assertEqual(len(seen), 1)
            health = urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/health")
            self.assertEqual(json.loads(health.read()), {"status": "ok"})
        finally:
            server.shutdown()


# ---------------------------------------------------------------- config + CLI
class TestConfigAndCli(unittest.TestCase):
    def test_dry_run_is_the_default(self):
        self.assertTrue(Settings.from_env({}).dry_run)
        self.assertFalse(Settings.from_env({"DRY_RUN": "0"}).dry_run)

    def test_bad_values(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"LLM_PROVIDER": "gemini"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"MAX_DIFF_CHARS": "lots"})

    def test_missing_settings_are_listed(self):
        s = Settings.from_env({"TICKET_SOURCE": "jira"})
        self.assertEqual(s.missing(serving=True)[:3], ["GITHUB_TOKEN", "WEBHOOK_SECRET", "ANTHROPIC_API_KEY"])
        self.assertIn("JIRA_API_TOKEN", s.missing())

    def test_cli_config_error_exit_3(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(buf):
            with mock.patch.object(cli, "console", cli.Console(file=buf, width=120)):
                self.assertEqual(cli.main(["review", "--repo", "me/x", "--pr", "1"]), 3)
        self.assertIn("Missing environment variables", buf.getvalue())

    def test_cli_replay_end_to_end_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            tickets, event = os.path.join(d, "t.json"), os.path.join(d, "e.json")
            json.dump({"DEMO-1": {"title": "Add greeting", "acceptance_criteria": ["greet(name) works"]}}, open(tickets, "w"))
            json.dump(pr_payload(), open(event, "w"))
            gh, llm = github(), FakeLLM(NO)
            env = {"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "TICKET_SOURCE": "local", "LOCAL_TICKETS_FILE": tickets}
            buf = io.StringIO()
            real_init = GitHubPlatform.__init__
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(GitHubPlatform, "__init__", lambda self, token, session=None: real_init(self, token, gh)), \
                    mock.patch.object(cli, "LLMClient", lambda s: llm), \
                    mock.patch.object(cli, "console", cli.Console(file=buf, width=140)):
                code = cli.main(["replay", event])
            out = buf.getvalue()
            self.assertEqual(code, 0, out)
            self.assertIn("BLOCKED", out)
            self.assertIn("dry-run", out)
            self.assertIn("would post REQUEST_CHANGES review", out)
            self.assertEqual([c for c in gh.calls if c[0] != "GET"], [])


class TestPermissionHint(unittest.TestCase):
    def test_403_names_the_missing_permission(self):
        from pr_reviewer.errors import PlatformError
        r = resp(403, {"message": "Resource not accessible by personal access token"})
        r.headers["X-Accepted-GitHub-Permissions"] = "contents=write"
        gh = GitHubPlatform("t", FakeSession({("POST", "/git/refs"): r}))
        with self.assertRaises(PlatformError) as c:
            gh._call("POST", "/repos/me/x/git/refs", json={})
        self.assertIn("This token needs: contents: write", c.exception.message)


class TestSetupDemoAndOutput(unittest.TestCase):
    def demo_session(self, repo_status=200, ref_status=200, branch_exists=False, pr_exists=False):
        import base64  # noqa: F401
        n = iter(range(1, 10))
        return FakeSession({
            ("GET", "/repos/me/pr-test"): resp(repo_status, {"default_branch": "main"} if repo_status == 200 else {"message": "Not Found"}),
            ("GET", "/git/ref/heads/main"): resp(ref_status, {"object": {"sha": "base123"}} if ref_status == 200 else {"message": "Git Repository is empty."}),
            ("POST", "/git/refs"): resp(422, {"message": "Reference already exists"}) if branch_exists else resp(201, {}),
            ("GET", "/contents/greeting.py"): resp(200, {"sha": "old"}),
            ("PUT", "/contents/greeting.py"): resp(200, {}),
            ("PUT", "/contents/tests/test_greeting.py"): resp(201, {}),
            ("PUT", "/contents/calc.py"): resp(201, {}),
            ("PUT", "/contents/NOTES.md"): resp(201, {}),
            ("POST", "/repos/me/pr-test/pulls"): (lambda kw: resp(422, {"message": "A pull request already exists"}))
            if pr_exists else (lambda kw: resp(201, {"number": next(n), "html_url": "u"})),
            ("GET", "/repos/me/pr-test/pulls"): resp(200, [{"number": 5, "html_url": "u"}]),
        })

    def test_creates_three_prs_with_expected_outcomes(self):
        from pr_reviewer.demo_setup import setup_demo
        sess = self.demo_session()
        res = setup_demo(GitHubPlatform("t", sess), "me/pr-test", log=lambda m: None)
        self.assertEqual([r["number"] for r in res], [1, 2, 3])
        self.assertEqual([r["expect"].split()[0] for r in res], ["MERGED", "BLOCKED", "HALTED"])
        puts = {c[1].split("/contents/")[1]: c[2]["json"] for c in sess.made("PUT", "")}
        self.assertEqual(puts["greeting.py"]["sha"], "old")          # existing file is updated, not duplicated
        self.assertNotIn("sha", puts["calc.py"])
        self.assertEqual(puts["calc.py"]["branch"], "feature/DEMO-2-add")
        heads = [c[2]["json"]["head"] for c in sess.made("POST", "/pulls")]
        # the branches the reviewer will see -> the ticket ids it will extract
        self.assertEqual(extract_ticket_id(res[0]["title"], heads[0]), "DEMO-1")
        self.assertEqual(extract_ticket_id(res[1]["title"], heads[1]), "DEMO-2")
        with self.assertRaises(ReviewHalt):
            extract_ticket_id(res[2]["title"], heads[2])

    def test_demo_code_matches_the_tickets(self):
        from pr_reviewer import demo_setup as D
        ns = {}
        exec(D.GREETING, ns)
        self.assertEqual(ns["greet"]("Qursam"), "Hello, Qursam!")
        with self.assertRaises(ValueError):
            ns["greet"]("")
        tickets = json.load(open(os.path.join(os.path.dirname(__file__), "..", "examples", "tickets.json")))
        self.assertIn("tests/test_calc.py", " ".join(tickets["DEMO-2"]["acceptance_criteria"]))
        self.assertNotIn("tests/test_calc.py", D.DEMOS[1]["files"])  # DEMO-2 is deliberately incomplete

    def test_rerun_reuses_branches_and_prs(self):
        from pr_reviewer.demo_setup import setup_demo
        res = setup_demo(GitHubPlatform("t", self.demo_session(branch_exists=True, pr_exists=True)), "me/pr-test", lambda m: None)
        self.assertEqual([r["number"] for r in res], [5, 5, 5])

    def test_missing_or_empty_repo_explained(self):
        from pr_reviewer.demo_setup import setup_demo
        from pr_reviewer.errors import PlatformError
        with self.assertRaises(PlatformError) as c:
            setup_demo(GitHubPlatform("t", self.demo_session(repo_status=404)), "me/pr-test", lambda m: None)
        self.assertIn("Add a README", c.exception.message)
        with self.assertRaises(PlatformError) as c:
            setup_demo(GitHubPlatform("t", self.demo_session(ref_status=409)), "me/pr-test", lambda m: None)
        self.assertIn("is empty", c.exception.message)

    def test_cli_setup_demo_needs_only_github_token_and_saves_output(self):
        sess = self.demo_session()
        real_init = GitHubPlatform.__init__
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "setup.txt")
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True), \
                    mock.patch.object(GitHubPlatform, "__init__", lambda self, token, session=None: real_init(self, token, sess)), \
                    redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                code = cli.main(["setup-demo", "--repo", "me/pr-test", "-o", out])
            text = open(out, encoding="utf-8").read()
        self.assertEqual(code, 0)
        self.assertIn("$ pr-review setup-demo --repo me/pr-test", text)
        self.assertIn("pr-review review --repo me/pr-test --pr 2", text)
        self.assertNotIn("\x1b[", text)

    def test_live_and_dry_run_together_is_an_error(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["review", "--repo", "a/b", "--pr", "1", "--live", "--dry-run"])

    def test_review_output_file_after_subcommand(self):
        with tempfile.TemporaryDirectory() as d:
            tickets, out = os.path.join(d, "t.json"), os.path.join(d, "review.txt")
            json.dump({"DEMO-1": {"title": "Add greeting", "acceptance_criteria": ["greet works"]}}, open(tickets, "w"))
            gh = github()
            gh.routes[("GET", "/repos/me/test-repo/pulls/7")] = lambda kw: (
                resp(200, text=DIFF) if "diff" in str(kw.get("headers", {})) else
                resp(200, {"number": 7, "title": PR.title, "head": {"ref": PR.branch, "sha": PR.head_sha},
                           "base": {"ref": "main"}, "html_url": "u", "user": {"login": "me"}}))
            env = {"GITHUB_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "TICKET_SOURCE": "local", "LOCAL_TICKETS_FILE": tickets}
            real_init = GitHubPlatform.__init__
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(GitHubPlatform, "__init__", lambda self, token, session=None: real_init(self, token, gh)), \
                    mock.patch.object(cli, "LLMClient", lambda s: FakeLLM(YES)), \
                    redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                code = cli.main(["review", "--repo", "me/test-repo", "--pr", "7", "--live", "-o", out])
            text = open(out, encoding="utf-8").read()
        self.assertEqual(code, 0)
        self.assertIn("MERGED", text)
        self.assertIn("LIVE", text)
        self.assertEqual(len(gh.made("PUT", "/pulls/7/merge")), 1)


class TestTeacherTask(unittest.TestCase):
    """The teacher's CFG-1 ticket and the two controller files in examples/task-files."""
    HERE = os.path.dirname(os.path.abspath(__file__))
    EX = os.path.join(HERE, "..", "examples")

    def test_cfg1_ticket_loads_with_all_nine_criteria(self):
        t = LocalFetcher(Settings(local_tickets_file=os.path.join(self.EX, "tickets.json"))).fetch("CFG-1")
        self.assertIn("Configuration Management", t.title)
        self.assertEqual(t.acceptance_criteria.count("\n- ") + 1, 9)
        self.assertIn("next(error)", t.description)

    def test_branches_map_to_cfg1(self):
        for branch in ("feature/CFG-1-user-controller", "feature/CFG-1-wuser-controller"):
            self.assertEqual(extract_ticket_id("CFG-1 controller", branch), "CFG-1")

    def test_files_fit_and_reach_the_llm_with_the_criteria(self):
        from pr_reviewer.llm import build_user_message
        t = LocalFetcher(Settings(local_tickets_file=os.path.join(self.EX, "tickets.json"))).fetch("CFG-1")
        for name in ("userController.js", "WUserController.js"):
            code = open(os.path.join(self.EX, "task-files", name), encoding="utf-8").read()
            diff = "".join("+" + line + "\n" for line in code.splitlines())
            self.assertLess(len(diff), 60_000, name)          # not refused as too large
            msg = build_user_message(t, diff)
            self.assertIn("A user cannot read, update or delete another user's configuration.", msg)
            self.assertIn("userRegisteration", msg)

    def test_make_pr_command(self):
        sess = TestSetupDemoAndOutput().demo_session()
        sess.routes[("PUT", "/contents/controllers/userController.js")] = resp(201, {})
        real_init = GitHubPlatform.__init__
        local = os.path.join(self.EX, "task-files", "userController.js")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True), \
                mock.patch.object(GitHubPlatform, "__init__", lambda self, token, session=None: real_init(self, token, sess)), \
                mock.patch.object(cli, "console", cli.Console(file=buf, width=140)):
            code = cli.main(["make-pr", "--repo", "me/pr-test", "--branch", "feature/CFG-1-user-controller",
                             "--title", "CFG-1 User controller", "--file", f"{local}=controllers/userController.js"])
        self.assertEqual(code, 0, buf.getvalue())
        put = sess.made("PUT", "/contents/controllers/userController.js")[0][2]["json"]
        self.assertEqual(put["branch"], "feature/CFG-1-user-controller")
        import base64
        self.assertIn("userRegisteration", base64.b64decode(put["content"]).decode())
        pr = sess.made("POST", "/repos/me/pr-test/pulls")[0][2]["json"]
        self.assertEqual((pr["head"], pr["title"]), ("feature/CFG-1-user-controller", "CFG-1 User controller"))
        self.assertIn("pr-review review --repo me/pr-test --pr 1", buf.getvalue())

    def test_make_pr_missing_file(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True), \
                mock.patch.object(cli, "console", cli.Console(file=buf, width=140)):
            code = cli.main(["make-pr", "--repo", "me/x", "--branch", "b", "--title", "t", "--file", "nope.js"])
        self.assertEqual(code, 3)
        self.assertIn("Could not read 'nope.js'", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
