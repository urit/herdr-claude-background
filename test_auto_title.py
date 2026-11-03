"""auto_title.py without the model or herdr: python3 -m unittest"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import auto_title as a


class SessionStart(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        for p in (mock.patch.object(a, "STATE_DIR", self.dir),
                  mock.patch.object(a, "ask_model", side_effect=AssertionError("model called"))):
            p.start()
            self.addCleanup(p.stop)

    def run_hook(self, source, state=None):
        if state is not None:
            with open(os.path.join(self.dir, "s1.json"), "w") as f:
                json.dump(state, f)
        hook = {"hook_event_name": "SessionStart", "session_id": "s1", "source": source}
        with mock.patch.object(a, "report_summary") as report, \
                mock.patch.object(sys, "argv", ["auto_title.py", "p1", "t1"]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook))):
            a.main()
        return report.call_args_list

    def test_resume_reports_saved_title(self):
        self.assertEqual(self.run_hook("resume", {"title": "Fixing the auth PR"}),
                         [mock.call("p1", "Fixing the auth PR")])

    def test_startup_without_state_reports_placeholder(self):
        self.assertEqual(self.run_hook("startup"), [mock.call("p1", "New session")])

    def test_resume_without_state_reports_nothing(self):
        self.assertEqual(self.run_hook("resume"), [])


class NewTab(unittest.TestCase):
    def test_named_tab_needs_two_prompts(self):
        state, moved = {}, {"same": False, "tab": "billing"}
        self.assertIsNone(a.new_tab(state, 3, "auth PR", moved))
        self.assertIsNone(a.new_tab(state, 3, "auth PR", moved))  # same prompt's Stop
        self.assertEqual(a.new_tab(state, 4, "auth PR", moved), "billing")
        self.assertNotIn("away_since", state)

    def test_tangent_resets(self):
        state = {}
        a.new_tab(state, 3, "auth PR", {"same": False, "tab": "billing"})
        self.assertIsNone(a.new_tab(state, 4, "auth PR", {"same": True, "tab": ""}))
        self.assertIsNone(a.new_tab(state, 5, "auth PR", {"same": False, "tab": "billing"}))

    def test_default_tab_renamed_at_once(self):
        self.assertEqual(a.new_tab({}, 1, "claude", {"same": False, "tab": "auth PR"}), "auth PR")


if __name__ == "__main__":
    unittest.main()
