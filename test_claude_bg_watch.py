"""Parsing against real Claude Code screens and transcripts: python3 -m unittest"""
import contextlib
import io
import json
import os
import tempfile
import time
import types
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

    def test_viewing_a_subagent(self):
        # Claude Code 2.1.302: while a subagent's view is open, main goes hollow
        # and the viewed agent is the filled one.
        agents, counts = w.parse_screen(screen(
            "  ⏵⏵ bypass permissions on · 1 shell · ⧉ 6 · ← 1 agent",
            "",
            "  ◯ main",
            "  ⏺ general-purpose  Initializing ai-factory-explained HyperFrames project"
            "              2m 38s · ↓ 138.4k tokens",
            "  ◯ general-purpose  Reading ensure_ops in run_cli.py"
            "                                    1m 52s · ↓ 96.4k tokens",
        ))
        self.assertEqual(counts, {"shell": 1})
        self.assertEqual(agents, [
            ("general-purpose", "Initializing ai-factory-explained HyperFrames project"),
            ("general-purpose", "Reading ensure_ops in run_cli.py"),
        ])

    def test_selection_cursor_in_agent_list(self):
        # Arrowing down into the list puts ❯ before the selected line.
        for selected in (0, 1):
            lines = ["  ⏺ main",
                     "  ◯ general-purpose  Reading spatial-pan-stations.md blueprint"
                     "                           9m 9s · ↓ 210.5k tokens"]
            lines[selected] = "❯" + lines[selected][1:]
            agents, _ = w.parse_screen(screen(
                "  ⏵⏵ bypass permissions on · 1 shell · ⧉ 6 · ← 1 agent", "", *lines))
            self.assertEqual(agents, [("general-purpose", "Reading spatial-pan-stations.md blueprint")])

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


def fire(ts, tid, content="Running scheduled task (Oct 14 10:05am)"):
    """A cron or /loop wakeup firing, as Claude Code 2.1.300 writes it."""
    return json.dumps({"type": "system", "subtype": "scheduled_task_fire", "taskId": tid,
                       "timestamp": f"2026-10-14T10:{ts}Z", "content": content})


class CronJobs(unittest.TestCase):
    """CronCreate jobs and /loop wakeups: armed until deleted, fired (one-shot),
    expired, or the Claude process that held them exits."""

    def session(self, d, *lines, started=0):
        main = os.path.join(d, "sid.jsonl")
        with open(main, "a") as f:
            f.write("".join(line + "\n" for line in lines))
        s = w.Session(main, started=started)
        s.update()
        return s

    def test_recurring_job_until_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("c1", "CronCreate", cron="*/5 * * * *", recurring=True,
                                   prompt="Check Slack for new messages\nthat need Uri")),
                entry("00:01", result("c1", "Scheduled recurring job bf6b5dca (Every 5 minutes). Next run: Oct 15 "
                                            "10:21am. Session-only (not written to disk, dies when Claude exits). "
                                            "Auto-expires after 7 days. Use CronDelete to cancel sooner.")))
            now = w.epoch("2026-10-14T10:02:00Z")
            self.assertEqual(s.crons(now), [("watching", "every 5m Check Slack for new messages that need Uri")])
            s = self.session(d, fire("05:00", "bf6b5dca"))
            self.assertEqual(s.crons(now + 190)[0][0], "working")  # fired 10s ago
            self.assertEqual(s.crons(now + 300)[0][0], "watching")
            self.assertEqual(s.crons(now + 8 * 86400), [])  # auto-expired
            s = self.session(d, entry("06:00", use("c2", "CronDelete", id="bf6b5dca")))
            self.assertEqual(s.crons(now), [])

    def test_request_blob_copies_are_skipped(self):
        """2.1.300 also writes the request, tool results included, as an
        untimed api-request-blob entry before the real one."""
        with tempfile.TemporaryDirectory() as d:
            text = "Scheduled recurring job bf6b5dca (Every 5 minutes). Auto-expires after 7 days."
            blob = json.dumps({"type": "api-request-blob", "sessionId": "sid", "hash": "28d8",
                               "message": {"role": "user", "content": [result("c1", text)]}})
            s = self.session(d,
                entry("00:00", use("c1", "CronCreate", cron="*/5 * * * *", prompt="Check Slack")),
                blob, entry("00:01", result("c1", text)),
                started=w.epoch("2026-10-14T09:00:00Z"))
            self.assertEqual(s.crons(w.epoch("2026-10-14T10:02:00Z")), [("watching", "every 5m Check Slack")])

    def test_one_shot_ends_when_it_fires(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("c1", "CronCreate", cron="30 10 14 10 *", recurring=False, prompt="Remind me")),
                entry("00:01", result("c1", "Scheduled one-shot task 1a2b3c4d (At 10:30 AM, on day 14 of the month, "
                                            "only in October). It will fire once then auto-delete.")))
            now = w.epoch("2026-10-14T10:02:00Z")
            self.assertEqual(s.crons(now), [("watching", "At 10:30 AM, on day 14 of the month, only in October Remind me")])
            s = self.session(d, fire("30:00", "1a2b3c4d"))
            self.assertEqual(s.crons(now), [])

    def test_session_only_jobs_die_with_their_process(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("c1", "CronCreate", cron="*/5 * * * *", prompt="old")),
                entry("00:01", result("c1", "Scheduled recurring job aaaa1111 (Every 5 minutes). Auto-expires after 7 days.")),
                entry("00:02", use("c2", "CronCreate", cron="0 9 * * *", prompt="kept", durable=True)),
                entry("00:03", result("c2", "Scheduled recurring job bbbb2222 (Every day at 9:00 AM). Auto-expires after 7 days.")),
                started=w.epoch("2026-10-14T10:30:00Z"))
            self.assertEqual(s.crons(w.epoch("2026-10-14T10:31:00Z")), [("watching", "Every day at 9:00 AM kept")])

    def test_loop_wakeup_until_it_fires_or_stops(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("u1", "ScheduleWakeup", delaySeconds=1200, reason="fallback", prompt="/work-the-board")),
                entry("00:01", result("u1", "Next wakeup scheduled for 13:20:00 (in 1199s). Nothing more to do this turn.")))
            now = w.epoch("2026-10-14T10:02:00Z")
            at = time.strftime("%H:%M", time.localtime(w.epoch("2026-10-14T10:20:00Z")))
            self.assertEqual(s.crons(now), [("watching", f"at {at} /work-the-board")])
            s = self.session(d, fire("20:00", "fc394f03", "Claude resuming /loop wakeup (Oct 14 1:20pm)"))
            self.assertEqual(s.crons(now), [])
            s = self.session(d,
                entry("21:00", use("u2", "ScheduleWakeup", delaySeconds=60, reason="tick", prompt="<<autonomous-loop-dynamic>>")),
                entry("21:01", result("u2", "Next wakeup scheduled for 13:22:01 (in 60s).")))
            self.assertEqual([t for _, t in s.crons(now)], [f"at {time.strftime('%H:%M', time.localtime(w.epoch('2026-10-14T10:22:01Z')))} tick"])
            s = self.session(d, entry("21:30", use("u3", "ScheduleWakeup", stop=True)))
            self.assertEqual(s.crons(now), [])

    def test_cron_rows_do_not_make_an_idle_agent_working(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("c1", "CronCreate", cron="*/5 * * * *", prompt="Check Slack")),
                entry("00:01", result("c1", "Scheduled recurring job bf6b5dca (Every 5 minutes). Auto-expires after 7 days.")))
            orig = (w.transcript_for, w.time.time)
            w.transcript_for = lambda pane: s
            w.time.time = lambda: w.epoch("2026-10-14T10:02:00Z")
            try:
                self.assertEqual(w.render("p1", [], {}), ((("watching", "↻", "every 5m Check Slack"),), None))
            finally:
                w.transcript_for, w.time.time = orig


class MoreBackgroundKinds(unittest.TestCase):
    def session(self, d, *lines, started=0):
        main = os.path.join(d, "sid.jsonl")
        with open(main, "a") as f:
            f.write("".join(line + "\n" for line in lines))
        s = w.Session(main, started=started)
        s.update()
        return s

    def test_shell_backgrounded_with_ctrl_b(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("u1", "Bash", description="Run the e2e suite", command="make e2e")),
                entry("00:01", result("u1", "Command was manually backgrounded by user with ID: b7k2. "
                                            "Output is being written to: /tmp/x.output.")))
            self.assertEqual([d for d, _, _ in s.live("shell", 5)], ["Run the e2e suite"])

    def test_workflow_named_after_its_script_until_it_ends(self):
        script = "export const meta = {\n  name: 'review-changes',\n  description: 'Review changed files',\n}\n"
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("u1", "Workflow", script=script)),
                entry("00:01", result("u1", "Workflow launched in background. Task ID: w4f9a. Watch it with /workflows.")),
                entry("00:02", {"type": "tool_use", "id": "u2", "name": "Workflow", "input": {"name": "nightly-audit"}}),
                entry("00:03", result("u2", "Workflow launched in a remote CCR session. Task ID: w8c1d.")))
            started = w.epoch("2026-10-14T10:00:01Z")
            orig = (w.transcript_for, w.time.time)
            w.transcript_for = lambda pane: s
            w.time.time = lambda: started + 600
            try:
                items, label = w.render("p1", [], {})
            finally:
                w.transcript_for, w.time.time = orig
            self.assertEqual(items, (("working", "▣", "10m review-changes"), ("working", "▣", "9m nightly-audit")))
            self.assertEqual(label, "working")
            s = self.session(d, queued("05:00", "w4f9a", "completed"))
            self.assertEqual([d for d, _, _ in s.live("workflow", 5)], ["nightly-audit"])

    def test_artifact_watches_collapse_into_one_row(self):
        url, deck = "https://claude.ai/artifact/TCHngRkrRv2yPEBotamMgg", "https://claude.ai/artifact/3bKYPdxpVd7urVa4gZmNeT"
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("a1", "Artifact", file_path="/tmp/scratch/ai-factory-plan.html", icon="factory")),
                entry("00:01", result("a1", f"Published /tmp/scratch/ai-factory-plan.html at {url} (Version 1)\n\n"
                                            "Live subscription: arming in the background — not connected yet")),
                entry("00:02", use("a2", "Artifact", type_url="https://claude.ai/artifact/Rp9n", title="Q4 deck")),
                entry("00:03", result("a2", f"Created a new Artifact at {deck} (version 1)\n\n"
                                            "Live subscription: already connected from earlier in this session")),
                entry("00:04", use("a3", "Artifact", action="read", url="https://claude.ai/artifact/other")),
                entry("00:05", result("a3", "<html>Live subscription: arming</html>")))
            self.assertEqual([t for _, t in s.watches()], ["ai-factory-plan", "Q4 deck"])
            s = self.session(d, entry("01:00", use("a4", "ArtifactComments", action="unwatch", url=url)))
            self.assertEqual([t for _, t in s.watches()], ["Q4 deck"])
            s = self.session(d, started=w.epoch("2026-10-14T11:00:00Z"))
            self.assertEqual([t for _, t in s.watches()], [])  # a live subscription ends with its process

    def test_artifact_named_by_its_ticket(self):
        # start-ticket writes every plan page to review.html in a start-<ticket> folder.
        url = "https://claude.ai/artifact/2y8s7jNHabBDAR1xEyUbeB"
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("a1", "Artifact", file_path="/tmp/claude-502/scratchpad/start-IN-3087/review.html")),
                entry("00:01", result("a1", f"Published /tmp/start-IN-3087/review.html at {url} (Version 1)\n\n"
                                            "Live subscription: arming in the background — not connected yet")))
            self.assertEqual([t for _, t in s.watches()], ["IN-3087"])
            s = self.session(d,
                entry("01:00", use("a2", "Artifact", file_path="/tmp/scratchpad/deck-test/review.html")),
                entry("01:01", result("a2", "Published /tmp/deck-test/review.html at https://claude.ai/artifact/YKuw\n\n"
                                            "Live subscription: arming in the background")))
            self.assertEqual([t for _, t in s.watches()], ["IN-3087", "deck-test"])

    def test_artifact_row_only_while_its_tab_is_open(self):
        open_url, closed_url = "https://claude.ai/artifact/5rKoYo4ojduC5J8ev5zrdW", "https://claude.ai/artifact/2y8s7jNHabBDAR1xEyUbeB"
        with tempfile.TemporaryDirectory() as d:
            s = self.session(d,
                entry("00:00", use("a1", "Artifact", file_path="/tmp/start-IN-2920/review.html")),
                entry("00:01", result("a1", f"Published at {open_url}\n\nLive subscription: arming")),
                entry("00:02", use("a2", "Artifact", file_path="/tmp/start-IN-3087/review.html")),
                entry("00:03", result("a2", f"Published at {closed_url}\n\nLive subscription: arming")))
            orig = (w.transcript_for, w.open_artifacts)
            w.transcript_for = lambda pane: s
            try:
                # Chrome's tab URL carries a share key after the id.
                w.open_artifacts = lambda: set(w.ARTIFACT_ID_RE.findall(f"{open_url}?sk=ZuKK, https://x.test"))
                self.assertEqual(w.render("p1", [], {})[0], (("watching", "⊙", "waiting review IN-2920"),))
                w.open_artifacts = lambda: set()  # Chrome closed
                self.assertEqual(w.render("p1", [], {})[0], ())
                w.open_artifacts = lambda: None  # Chrome can't be asked: show them all
                self.assertEqual(w.render("p1", [], {})[0], (("watching", "⊙", "waiting review IN-2920, IN-3087"),))
            finally:
                w.transcript_for, w.open_artifacts = orig

    def test_goal_row_while_a_goal_is_active(self):
        # The goal_status attachments Claude Code 2.1.309 writes for /goal.
        def status(ts, **att):
            return json.dumps({"type": "attachment", "timestamp": f"2026-10-24T10:{ts}Z",
                               "attachment": {"type": "goal_status", "condition": "tests pass", **att}})
        with tempfile.TemporaryDirectory() as d:
            orig = w.transcript_for
            try:
                s = self.session(d, status("16:05", met=False, sentinel=True))  # /goal tests pass
                w.transcript_for = lambda pane: s
                self.assertEqual(w.render("p1", [], {}), ((("goal", "⚑", "goal: tests pass"),), None))
                s = self.session(d, status("16:28", met=False, reason="2 tests still fail"))
                self.assertEqual(s.goal(), "tests pass")  # checked after a turn, not met yet
                s = self.session(d, status("16:45", met=True, reason="all pass", iterations=3))
                self.assertIsNone(s.goal())
                s = self.session(d, status("17:44", met=False, sentinel=True), status("17:51", met=True, sentinel=True))
                self.assertIsNone(s.goal())  # /goal clear
                s = self.session(d, status("18:00", met=False, sentinel=True), status("18:09", met=False, failed=True))
                self.assertIsNone(s.goal())  # judged impossible
            finally:
                w.transcript_for = orig

    def test_open_review_reuses_an_open_tab(self):
        open_url, closed_url = "https://claude.ai/artifact/5rKoYo4ojduC5J8ev5zrdW", "https://claude.ai/artifact/2y8s7jNHabBDAR1xEyUbeB"
        s = types.SimpleNamespace(watches=lambda: [(open_url, "IN-2920"), (closed_url, "IN-3087")])
        opened = []
        orig = (w.transcript_for, w.focus_tab, w.subprocess.run)
        w.transcript_for, w.focus_tab = lambda pane: s, lambda url: url == open_url
        w.subprocess.run = lambda args, **kw: opened.append(args[-1])
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                w.open_review("p1")
        finally:
            w.transcript_for, w.focus_tab, w.subprocess.run = orig
        self.assertEqual(opened, [closed_url])  # the open page's tab is focused, not duplicated

    def test_artifact_row_text(self):
        self.assertEqual(w.watch_row(["IN-3087"]), "waiting review IN-3087")
        self.assertEqual(w.watch_row(["IN-2920", "Q4 deck", "IN-2920"]), "waiting review IN-2920, Q4 deck")


PS = """\
50954     1 21:12:18   0.0 /usr/bin/Python claude_bg_watch.py --start CLAUDECODE=1 HOME=/u
60372     1 06-03:24:12 166.5 /opt/homebrew/bin/git -c core.fsmonitor= fetch CLAUDE_CODE_ENTRYPOINT=sdk-cli HOME=/u
86450     1 08-17:44:53   0.0 gpg-agent --homedir /u/.gnupg --daemon CLAUDECODE=1 HOME=/u
70001     1    03:10   0.0 /bin/sleep 600 CLAUDECODE=1 HOME=/u
70002     1  1-00:00:00   2.0 /usr/bin/ssh -N tunnel CLAUDECODE=1 HOME=/u
70003   999  2-00:00:00   0.0 /bin/zsh -c x CLAUDECODE=1 HOME=/u
70004     1  3-00:00:00   0.0 /usr/sbin/cfprefsd agent HOME=/u
"""


class Orphans(unittest.TestCase):
    """Processes started under Claude Code that lost their parent: what a hung
    marketplace `git` looked like after 6 days at 170% CPU."""

    def test_orphans_from_ps(self):
        self.assertEqual(w.orphans(PS), [("git", 60372, 530652, 166.5), ("ssh", 70002, 86400, 2.0)])

    def test_orphan_row(self):
        self.assertEqual(w.orphan_row([]), None)
        self.assertEqual(w.orphan_row([("git", 60372, 530652, 166.5)]), "⚠ orphaned: git 6d 166% CPU")
        self.assertEqual(w.orphan_row([("git", 60372, 530652, 166.5), ("ssh", 70002, 86400, 2.0)]),
                         "⚠ orphaned: git 6d 166% CPU +1")


def said(ts, effort, per_turn=None):
    """An assistant entry as 2.1.300 writes it, with the session's /effort."""
    return json.dumps({"type": "assistant", "timestamp": f"2026-10-14T10:{ts}Z", "effort": effort,
                       "perTurnEffort": per_turn or effort,
                       "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}})


class Effort(unittest.TestCase):
    """The state label "working" is colored by the session's effort level."""

    def test_latest_turn_effort(self):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            with open(main, "w") as f:
                f.write(said("00:00", "high") + "\n" + said("01:00", "high", "xhigh") + "\n")
            s = w.Session(main)
            s.update()
            self.assertEqual(s.effort, "xhigh")
            with open(main, "a") as f:
                f.write(entry("02:00", result("u1", "a tool result")) + "\n")  # no effort: keep the last
            s.update()
            self.assertEqual(s.effort, "xhigh")

    def test_an_active_subagents_higher_effort_wins(self):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            subs = os.path.join(d, "sid", "subagents")
            os.makedirs(subs)
            with open(main, "w") as f:
                f.write(said("00:00", "high") + "\n")

            def sub(aid, effort, mtime=None):
                log = os.path.join(subs, f"agent-{aid}.jsonl")
                with open(log, "w") as f:
                    f.write(said("00:30", effort) + "\n")
                if mtime:
                    os.utime(log, (mtime, mtime))

            sub("a1", "medium")
            sub("a2", "max", mtime=time.time() - 600)  # went quiet long ago
            s = w.Session(main)
            s.update()
            self.assertEqual(s.effort, "high")
            sub("a3", "xhigh")
            s.update()
            self.assertEqual(s.effort, "xhigh")
            with open(main, "a") as f:
                f.write(queued("02:00", "a3", "completed") + "\n")
            s.update()
            self.assertEqual(s.effort, "high")

    def test_a_main_session_waiting_on_subagents_takes_their_effort(self):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            subs = os.path.join(d, "sid", "subagents")
            os.makedirs(subs)
            with open(main, "w") as f:
                f.write(said("00:00", "high") + "\n")
            os.utime(main, (time.time() - 600,) * 2)  # waiting, not writing
            with open(os.path.join(subs, "agent-a1.jsonl"), "w") as f:
                f.write(said("00:30", "medium") + "\n")
            s = w.Session(main)
            s.update()
            self.assertEqual(s.effort, "medium")
            os.remove(os.path.join(subs, "agent-a1.jsonl"))
            s = w.Session(main)
            s.update()
            self.assertEqual(s.effort, "high")  # nothing active: the session's own

    def test_labels(self):
        self.assertEqual(w.state_labels(None, None), [])
        self.assertEqual(w.state_labels(None, "medium"), [])
        self.assertEqual(w.state_labels(None, "high"), ["working=\u200bworking"])
        self.assertEqual(w.state_labels("working", "xhigh"),
                         ["idle=\u2060working", "done=\u2060working", "working=\u2060working"])
        self.assertEqual(w.state_labels("working", "max"),
                         ["idle=\u200cworking", "done=\u200cworking", "working=\u200cworking"])
        self.assertEqual(w.state_labels("working", "low"), ["idle=working", "done=working"])

    def test_report_sends_every_label_at_once_or_clears(self):
        sent = []
        orig = w.herdr
        w.herdr = lambda *args: sent.append(args) or ""
        try:
            w.report("p1", [], None, "high")
            w.report("p1", [], None, None)
        finally:
            w.herdr = orig
        self.assertIn("working=\u200bworking", sent[0])
        self.assertNotIn("--clear-state-labels", sent[0])
        self.assertIn("--clear-state-labels", sent[1])


class Waiters(unittest.TestCase):
    """Watcher shells and monitors wait, so they don't make an idle agent read
    "working"; their rows are teal. Work shells and subagents still do."""

    def render(self, *lines, counts, ps=()):
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "sid.jsonl")
            with open(main, "w") as f:
                f.write("".join(line + "\n" for line in lines))
            s = w.Session(main)
            s.update()
            orig = (w.transcript_for, w.shell_commands, w.time.time)
            w.transcript_for = lambda pane: s
            w.shell_commands = lambda pane: list(ps)
            w.time.time = lambda: w.epoch("2026-10-14T10:10:00Z")
            try:
                return w.render("p1", [], counts)
            finally:
                w.transcript_for, w.shell_commands, w.time.time = orig

    def shell(self, uid, ts, desc, command="x"):
        return (entry(ts, use(uid, "Bash", description=desc, command=command)),
                entry(ts, result(uid, f"Command running in background with ID: {uid}. Output")))

    def test_watcher_shell_and_monitor_leave_the_agent_idle(self):
        items, label = self.render(*self.shell("s1", "00:00", "Re-arm the failure watch, ignoring pauses made by hand"),
                                   entry("00:01", use("m1", "Monitor", description="CI for PR 556")),
                                   entry("00:02", result("m1", "Monitor started (task m1, timeout 3600000ms)")),
                                   counts={"shell": 1, "monitor": 1})
        self.assertEqual(label, None)
        self.assertEqual([state for state, _, _ in items], ["watching", "watching"])

    def test_work_shell_still_working(self):
        items, label = self.render(*self.shell("s1", "00:00", "Run the e2e suite", "make e2e"), counts={"shell": 1})
        self.assertEqual((label, items[0][0]), ("working", "working"))

    def test_waiting_told_by_command_too(self):
        items, label = self.render(*self.shell("s1", "00:00", "PR 556 checks", "until gh pr checks 556; do sleep 30; done"),
                                   counts={"shell": 2},
                                   ps=[("tail -f /tmp/deploy.log", w.epoch("2026-10-14T10:00:00Z"))])
        self.assertEqual(label, None)
        self.assertEqual([state for state, _, _ in items], ["watching", "watching"])

    def test_is_waiter(self):
        for text in ("Wait for PR 556 checks to finish", "Watch drill run until it finishes",
                     "Keep the Mac awake for the tick", "Run the watcher for this session only",
                     "Poll the deploy", "caffeinate -i python3 board.py"):
            self.assertTrue(w.is_waiter(text), text)
        for text in ("Run the e2e suite", "Review stacks#514 deletion diff", "make check"):
            self.assertFalse(w.is_waiter(text), text)


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
            self.assertEqual(items, (("watching", "▶", "13h Watch the promotion"),
                                     ("working", "▶", "13h gh run view 123 --log-failed"),
                                     ("working", "▶", "background shell")))


if __name__ == "__main__":
    unittest.main()
