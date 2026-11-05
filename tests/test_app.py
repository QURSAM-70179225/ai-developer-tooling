"""Tests for the Streamlit demo (app.py) without a browser.

A fake `streamlit` module records every call, and GitHub is the same FakeSession used
by the CLI tests, so this runs offline and works whether or not Streamlit is installed.
"""

import os
import runpy
import sys
import unittest
from unittest import mock

try:
    import pandas as pd
except ImportError:  # the web demo is optional; the CLI tests don't need it
    raise unittest.SkipTest("pandas not installed: pip install -r requirements-web.txt to test the web demo")

from repo_analyzer.github_client import GitHubClient

sys.path.insert(0, os.path.dirname(__file__))
import test_repo_analyzer as T  # noqa: E402  (shared fake GitHub routes)

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


class StopApp(Exception):
    """Raised by the fake st.stop()."""


def fake_streamlit(url="", analyze_clicked=False):
    st = mock.MagicMock(name="streamlit")
    st.session_state = {"url": url}
    st.text_input.side_effect = lambda label, key=None, **kw: st.session_state.get(key, "") if key else ""
    st.slider.side_effect = lambda label, lo, hi, default, **kw: default
    st.button.side_effect = lambda label, **kw: analyze_clicked and label == "Analyze"
    st.columns.side_effect = lambda n: [mock.MagicMock(name=f"col{i}") for i in range(n)]
    st.tabs.side_effect = lambda names: [mock.MagicMock(name=n) for n in names]
    st.stop.side_effect = StopApp
    return st


def run_app(st, routes):
    """Execute app.py top to bottom with the fake Streamlit and fake GitHub."""
    session = T.FakeSession(routes)
    real_init = GitHubClient.__init__

    def patched(self, token=None, timeout=15.0, session_=None, sleep=None):
        real_init(self, token=token, timeout=timeout, session=session, sleep=lambda s: None)

    # Swap only the "streamlit" entry. patch.dict(sys.modules) would also drop every module
    # imported during the run, and the re-imported copies break Rich's isinstance checks.
    previous = sys.modules.get("streamlit")
    sys.modules["streamlit"] = st
    try:
        with mock.patch.object(GitHubClient, "__init__", patched), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}):
            try:
                runpy.run_path(APP, run_name="__main__")
            except StopApp:
                return "stopped"
        return "rendered"
    finally:
        if previous is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = previous


def texts(m):
    """All string arguments passed to a mocked function (e.g. st.markdown)."""
    return " ".join(str(a) for call in m.call_args_list for a in call.args)


class TestStreamlitApp(unittest.TestCase):
    def test_landing_page_before_any_analysis(self):
        st = fake_streamlit()
        self.assertEqual(run_app(st, T.default_routes()), "stopped")
        self.assertIn("Enter a public GitHub repository", texts(st.markdown))
        st.error.assert_not_called()

    def test_full_analysis_renders_every_section(self):
        st = fake_streamlit(url="https://github.com/acme/web", analyze_clicked=True)
        self.assertEqual(run_app(st, T.default_routes()), "rendered")
        st.error.assert_not_called()
        tab_names = st.tabs.call_args.args[0]
        self.assertEqual(len(tab_names), 6)
        for wanted in ["Tech stack", "Structure", "Complexity", "Dependency health", "Contribution activity"]:
            self.assertTrue(any(wanted in n for n in tab_names), wanted)

        frames = [c.args[0] for c in st.dataframe.call_args_list]
        self.assertTrue(all(isinstance(f, pd.DataFrame) for f in frames))
        cols = [set(f.columns) for f in frames]
        self.assertIn({"Technology", "Category", "Evidence", "Dev/test only"}, cols)
        self.assertIn({"Folder", "Files", "Main file types", "Purpose", "Basis"}, cols)
        self.assertIn({"Component", "Measured", "Points", "Rule (what earns full points)"}, cols)
        self.assertIn({"Package", "Declared version", "Pinning", "Scope", "Manifest"}, cols)
        tech = next(f for f in frames if "Technology" in f.columns)
        self.assertEqual(tech.loc[tech.Technology == "Jest", "Dev/test only"].item(), "yes")

        charts = [c.args[0] for c in st.bar_chart.call_args_list]
        self.assertEqual(len(charts), 2)                      # weekly commits + contributors
        self.assertEqual(int(charts[0]["Commits"].sum()), 156)
        self.assertEqual(list(charts[1].index[:2]), ["alice", "bob"])

        md = texts(st.markdown)
        self.assertIn('What "healthy" means here', md)
        self.assertIn("Not checked:", md)
        self.assertIn("Method:", md)
        code = texts(st.code)
        self.assertIn("📁 web/", code)                       # the file tree
        self.assertIn("🧰 Tech Stack", code)                 # full CLI report text
        dl = st.download_button.call_args
        self.assertEqual(dl.kwargs["file_name"], "report-web.txt")
        self.assertIn("$ analyze https://github.com/acme/web", dl.args[1])

    def test_invalid_url_is_a_friendly_error(self):
        st = fake_streamlit(url="https://gitlab.com/foo/bar", analyze_clicked=True)
        self.assertEqual(run_app(st, T.default_routes()), "stopped")
        self.assertIn("Invalid GitHub URL", texts(st.error))
        self.assertIn("💡", texts(st.info))

    def test_repo_not_found(self):
        st = fake_streamlit(url="acme/missing", analyze_clicked=True)
        run_app(st, T.default_routes())
        self.assertIn("Repository not found", texts(st.error))

    def test_rate_limit(self):
        import time
        routes = T.default_routes(**{"/rate_limit": T.make_response(200, {"resources": {"core": {
            "limit": 60, "remaining": 0, "reset": int(time.time()) + 900}}})})
        st = fake_streamlit(url="acme/web", analyze_clicked=True)
        run_app(st, routes)
        self.assertIn("rate limit", texts(st.error).lower())

    def test_empty_url_warns(self):
        st = fake_streamlit(url="", analyze_clicked=True)
        run_app(st, T.default_routes())
        self.assertIn("Enter a GitHub repository URL", texts(st.warning))

    def test_sample_button_triggers_analysis(self):
        st = fake_streamlit()
        run_app(st, T.default_routes())
        sample_calls = [c for c in st.button.call_args_list if "on_click" in c.kwargs]
        self.assertEqual(len(sample_calls), 3)
        callback, args = sample_calls[0].kwargs["on_click"], sample_calls[0].kwargs["args"]
        callback(*args)  # what Streamlit does when the user clicks it
        self.assertEqual(st.session_state["url"], "https://github.com/fastapi/fastapi")
        self.assertTrue(st.session_state["run"])


if __name__ == "__main__":
    unittest.main()
