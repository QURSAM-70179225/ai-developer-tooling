"""Tests for tools/publish_to_github.py (offline: GitHub is a recording fake)."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from test_pr_reviewer import FakeSession, resp  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "publish_to_github", Path(__file__).resolve().parent.parent / "tools" / "publish_to_github.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


def make_tree(d, files):
    for rel, text in files.items():
        p = Path(d) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


class TestPublish(unittest.TestCase):
    def test_this_project_contains_no_secrets(self):
        files = P.collect_files()
        self.assertGreater(len(files), 20)
        self.assertEqual(P.find_secrets(files), [], "a real-looking key/token is in the project!")

    def test_skips_venv_caches_and_env(self):
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, {"a.py": "x", ".venv/lib/x.py": "x", "pkg/__pycache__/a.pyc": "x", ".env": "SECRET=1",
                          "repo_analyzer_cli.egg-info/PKG-INFO": "x", "tests/t.py": "x", ".git/config": "x"})
            rels = [f.relative_to(d).as_posix() for f in P.collect_files(Path(d))]
        self.assertEqual(rels, ["a.py", "tests/t.py"])

    def test_detects_real_looking_secrets_but_not_placeholders(self):
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, {
                "ok.md": '$env:GITHUB_TOKEN = "github_pat_PASTE_YOURS"\n$env:OPENAI_API_KEY = "gsk_PASTE_YOURS"',
                "groq.txt": "key = gsk_" + "A1b2C3d4" * 6,
                "gh.txt": "token github_pat_" + "11ABCDEFG0" * 5,
                "ant.txt": "sk-ant-api03-" + "x" * 40,
            })
            hits = dict(P.find_secrets(P.collect_files(Path(d)), Path(d)))
        self.assertNotIn("ok.md", hits)
        self.assertEqual(hits["groq.txt"], "Groq API key")
        self.assertEqual(hits["gh.txt"], "GitHub fine-grained token")
        self.assertEqual(hits["ant.txt"], "Anthropic API key")

    def routes(self, empty=False):
        n = {"blob": 0}

        def blob(kw):
            n["blob"] += 1
            return resp(201, {"sha": f"b{n['blob']}"})
        ref = resp(409, {"message": "Git Repository is empty."}) if empty else resp(200, {"object": {"sha": "p0"}})
        r = {
            ("GET", "/repos/me/proj"): resp(200, {"default_branch": "main"}),
            ("GET", "/git/ref/heads/main"): ref,
            ("POST", "/git/blobs"): blob,
            ("POST", "/git/trees"): resp(201, {"sha": "t1"}),
            ("POST", "/git/commits"): resp(201, {"sha": "c1"}),
            ("PATCH", "/git/refs/heads/main"): resp(200, {}),
            ("PUT", "/contents/README.md"): resp(201, {}),
        }
        return r

    def test_one_commit_with_exactly_the_listed_files(self):
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, {"README.md": "# hi", "src/a.py": "print(1)"})
            files = P.collect_files(Path(d))
            sess = FakeSession(self.routes())
            url = P.publish(P.GitHub("t", sess), "me/proj", files, "msg", root=Path(d), log=lambda m: None)
        self.assertEqual(url, "https://github.com/me/proj/tree/main")
        tree = sess.made("POST", "/git/trees")[0][2]["json"]["tree"]
        self.assertEqual(sorted(e["path"] for e in tree), ["README.md", "src/a.py"])
        commit = sess.made("POST", "/git/commits")[0][2]["json"]
        self.assertEqual((commit["parents"], commit["tree"], commit["message"]), (["p0"], "t1", "msg"))
        self.assertEqual(sess.made("PATCH", "/git/refs/heads/main")[0][2]["json"]["sha"], "c1")
        self.assertEqual(sess.made("PUT", "/contents/README.md"), [])

    def test_empty_repository_gets_an_initial_commit_first(self):
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, {"README.md": "# hi"})
            routes = self.routes(empty=True)
            calls = {"n": 0}

            def ref(kw):
                calls["n"] += 1
                return resp(409, {"message": "empty"}) if calls["n"] == 1 else resp(200, {"object": {"sha": "init"}})
            routes[("GET", "/git/ref/heads/main")] = ref
            sess = FakeSession(routes)
            P.publish(P.GitHub("t", sess), "me/proj", P.collect_files(Path(d)), "m", root=Path(d), log=lambda m: None)
        self.assertEqual(len(sess.made("PUT", "/contents/README.md")), 1)
        self.assertEqual(sess.made("POST", "/git/commits")[0][2]["json"]["parents"], ["init"])

    def test_missing_repo_and_permission_errors_are_explained(self):
        sess = FakeSession({("GET", "/repos/me/proj"): resp(404, {"message": "Not Found"})})
        with self.assertRaises(P.PublishError) as c:
            P.publish(P.GitHub("t", sess), "me/proj", [], "m", log=lambda m: None)
        self.assertIn("github.com/new", str(c.exception))
        r = resp(403, {"message": "Resource not accessible by personal access token"})
        r.headers["X-Accepted-GitHub-Permissions"] = "contents=write"
        with self.assertRaises(P.PublishError) as c:
            P.GitHub("t", FakeSession({("POST", "/git/blobs"): r})).call("POST", "/repos/me/proj/git/blobs", json={})
        self.assertIn("token needs: contents=write", str(c.exception))


if __name__ == "__main__":
    unittest.main()
