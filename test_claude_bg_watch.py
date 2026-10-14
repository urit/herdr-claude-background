"""Parsing against real Claude Code screens and transcripts: python3 -m unittest"""
import json
import os
import tempfile
import unittest

import claude_bg_watch as w

RULE = "─" * 80


def screen(*footer):
    return "\n".join(["some output", RULE, "❯", RULE, "", *footer]) + "\n"


class ParseScreen(unittest.TestCase):
    def test_shells_monitors_and_agents(self):
        agents, counts = w.parse_screen(screen(
            "  ⏵⏵ auto mode on · 1 shell · 2 monitors · ← 1 agent",
            "",
            "  ⏺ main",
            "  ◯ general-purpose  Comparing task-runner signatures   3m 51s · ↓ 4k tokens",
            "  ◯ Explore  Find every caller of rep… 1m 2s · ↓ 2k tokens",
        ))
        self.assertEqual(counts, {"shell": 1, "monitor": 2})
        self.assertEqual(agents, [
            ("general-purpose", "Comparing task-runner signatures"),
            ("Explore", "Find every caller of rep…"),
        ])

    def test_grouped_agents(self):
        # Claude Code 2.1.299: ten agents of one type share a line.
        agents, counts = w.parse_screen(screen(
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ⧉ 2 · ← 1 agent",
            "",
            "  ⏺ main",
            "  ◯ code-review (+9)  Awaiting finder angle results"
            "                    13m 10s · ↓ 175.8k tokens",
        ))
        self.assertEqual(agents, [("code-review", "code-review ×10: Awaiting finder angle results")])
        self.assertEqual(counts, {}, "⧉ 2 counts artifacts, not background work")

    def test_collapsed_background_count(self):
        _, counts = w.parse_screen(screen("  ⏵⏵ bypass permissions on · 3 bg · ← 1 agent"))
        self.assertEqual(counts, {"background": 3})
        _, counts = w.parse_screen(screen("  ⏵⏵ auto mode on · 2 background"))
        self.assertEqual(counts, {"background": 2})

    def test_idle_footer(self):
        self.assertEqual(w.parse_screen(screen("  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent")), ([], {}))


def entry(ts, *blocks):
    return json.dumps({"timestamp": f"2026-10-14T10:{ts}Z", "message": {"content": list(blocks)}})


def use(uid, name, **inp):
    return {"type": "tool_use", "id": uid, "name": name, "input": inp}


def result(uid, text):
    return {"type": "tool_result", "tool_use_id": uid, "content": text}


class SessionTasks(unittest.TestCase):
    def test_subagent_tasks_count_and_stops_cross_transcripts(self):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            os.makedirs(os.path.join(d, "sid", "subagents"))
            sub = os.path.join(d, "sid", "subagents", "agent-a1.jsonl")
            with open(main, "w") as f:
                f.write(entry("00:00", use("u1", "Monitor", description="main monitor")) + "\n")
                f.write(entry("00:01", result("u1", "Monitor started (task m1, timeout 3600000ms)")) + "\n")
            with open(sub, "w") as f:
                f.write(entry("01:00", use("u2", "Monitor", description="subagent monitor")) + "\n")
                f.write(entry("01:01", result("u2", "Monitor started (task m2, timeout 3600000ms)")) + "\n")
                f.write(entry("01:02", use("u3", "Bash", description="subagent shell", command="sleep 99")) + "\n")
                f.write(entry("01:03", result("u3", "Command running in background with ID: s1. Output")) + "\n")
            session = w.Session(main)
            session.update()
            self.assertEqual([d for d, _ in session.live("monitor", 5)], ["main monitor", "subagent monitor"])
            self.assertEqual([d for d, _ in session.live("shell", 5)], ["subagent shell"])
            self.assertEqual([d for d, _ in session.live("monitor", 1)], ["subagent monitor"])
            with open(main, "a") as f:  # the main agent stops the subagent's shell
                f.write(entry("02:00", use("u4", "TaskStop", task_id="s1")) + "\n")
            session.update()
            self.assertEqual(session.live("shell", 5), [])


if __name__ == "__main__":
    unittest.main()
