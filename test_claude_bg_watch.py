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

    def test_hidden_agent_list(self):
        _, counts = w.parse_screen(screen(
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ⧉ 2 · /tasks to see subagents · ← 1 agent"))
        self.assertEqual(counts, {"hidden_agents": 1})

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
            self.assertEqual([d for d, _, _ in session.live("monitor", 5)], ["main monitor", "subagent monitor"])
            self.assertEqual(session.live("shell", 5), [("subagent shell", w.epoch("2026-10-14T10:01:03Z"), 0)])
            self.assertEqual([d for d, _, _ in session.live("monitor", 1)], ["subagent monitor"])
            with open(main, "a") as f:  # the main agent stops the subagent's shell
                f.write(entry("02:00", use("u4", "TaskStop", task_id="s1")) + "\n")
            session.update()
            self.assertEqual(session.live("shell", 5), [])


def queued(ts, tid, status=None, note=""):
    body = f"<task-id>{tid}</task-id>\n" + (f"<status>{status}</status>\n" if status else "") + note
    return json.dumps({"type": "queue-operation", "operation": "enqueue",
                       "timestamp": f"2026-10-14T10:{ts}Z", "content": f"<task-notification>\n{body}</task-notification>"})


INTERIM_NOTE = ("<note>This agent stopped with background work of its own still running. "
                "It may resume on its own when that work completes or reports</note>")


class QueuedNotifications(unittest.TestCase):
    """Claude Code 2.1.299 queues task notifications as entries of their own."""

    def write(self, path, *lines, mode="w"):
        with open(path, mode) as f:
            f.write("".join(line + "\n" for line in lines))

    def test_queued_status_ends_a_task_and_events_mark_it_fired(self):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            self.write(main,
                       entry("00:00", use("u1", "Monitor", description="watch CI")),
                       entry("00:01", result("u1", "Monitor started (task m1, timeout 3600000ms)")),
                       queued("00:30", "m1"))
            session = w.Session(main)
            session.update()
            [(desc, _, fired)] = session.live("monitor", 5)
            self.assertEqual(desc, "watch CI")
            self.assertEqual(fired, w.epoch("2026-10-14T10:00:30Z"))
            self.write(main, queued("01:00", "m1", "completed"), mode="a")
            session.update()
            self.assertEqual(session.live("monitor", 5), [])

    def test_background_agents_from_transcripts(self):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            subs = os.path.join(d, "sid", "subagents")
            os.makedirs(subs)

            def agent(aid, desc, shape="background", mtime=None):
                with open(os.path.join(subs, f"agent-{aid}.meta.json"), "w") as f:
                    json.dump({"agentType": "general-purpose", "description": desc, "requestShape": shape}, f)
                log = os.path.join(subs, f"agent-{aid}.jsonl")
                self.write(log, entry("00:00", {"type": "text", "text": "working"}))
                if mtime:
                    os.utime(log, (mtime, mtime))

            agent("a1", "Verify M1 refresh race")
            agent("a2", "Angle B removed-behavior audit")
            agent("a3", "Waiting on its own blind readers")
            agent("a4", "Foreground helper", shape="foreground")
            agent("a5", "Left over from before a resume", mtime=1000)
            self.write(main,
                       queued("01:00", "a2", "completed"),
                       queued("01:05", "a3", "completed", INTERIM_NOTE))
            session = w.Session(main, started=2000)
            session.update()
            self.assertEqual(sorted(desc for _, desc in session.agents()),
                             ["Verify M1 refresh race", "Waiting on its own blind readers"])
            self.write(main, queued("02:00", "a3", "completed"), mode="a")
            session.update()
            self.assertEqual([desc for _, desc in session.agents()], ["Verify M1 refresh race"])


class StartedOnlyAtResultStart(unittest.TestCase):
    """A foreground command whose output quotes a start line (grep over this
    repo's tests) once became a shell that never ended, hiding a real one."""

    def test_quoted_start_line_is_not_a_task(self):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            with open(main, "w") as f:
                f.write(entry("00:00", use("u1", "Bash", description="grep tests", command="grep -n ID test_claude_bg_watch.py")) + "\n")
                f.write(entry("00:01", result("u1", '17: result("u3", "Command running in background with ID: s1. Output")')) + "\n")
                f.write(entry("00:02", use("u2", "Bash", description="slow build", command="make")) + "\n")
                f.write(entry("00:03", result("u2", "<bash-stdout>Command did not complete within its 120s timeout "
                                                    "and was moved to the background (ID: b2)")) + "\n")
            session = w.Session(main)
            session.update()
            self.assertEqual(session.live("self", 5), [])
            self.assertEqual([d for d, _, _ in session.live("shell", 5)], ["slow build"])


class ShellAge(unittest.TestCase):
    """A background shell's row leads with how long it has run, so a hung one
    (a `gh run view` stuck for 13h) is told apart from a fresh one."""

    def test_age(self):
        self.assertEqual([w.age(s) for s in (5, 59, 60, 59 * 60 + 59, 3600, 13 * 3600 + 1800, 49 * 3600)],
                         ["<1m", "<1m", "1m", "59m", "1h", "13h", "2d"])

    def test_etime(self):
        self.assertEqual([w.etime(s) for s in ("00:07", "12:34", "01:02:03", "2-01:02:03", "bad")],
                         [7, 754, 3723, 176523, None])

    def test_shell_rows_lead_with_age(self):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            with open(main, "w") as f:
                f.write(entry("00:00", use("u1", "Bash", description="Watch the promotion", command="./watch.sh")) + "\n")
                f.write(entry("00:01", result("u1", "Command running in background with ID: s1. Output")) + "\n")
            session = w.Session(main)
            session.update()
            started = w.epoch("2026-10-14T10:00:01Z")
            orig = (w.transcript_for, w.shell_commands, w.time.time)
            w.transcript_for = lambda pane: session
            w.shell_commands = lambda pane: [("gh run view 123 --log-failed", started - 600)]
            w.time.time = lambda: started + 13 * 3600
            try:
                items, _ = w.render("p1", [], {"shell": 3})
            finally:
                w.transcript_for, w.shell_commands, w.time.time = orig
            self.assertEqual(items, (("working", "▶", "13h Watch the promotion"),
                                     ("working", "▶", "13h gh run view 123 --log-failed"),
                                     ("working", "▶", "background shell")))


if __name__ == "__main__":
    unittest.main()
