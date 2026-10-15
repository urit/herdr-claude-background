#!/usr/bin/env python3
"""Show each Claude Code agent's background work as rows in the herdr sidebar.

Claude Code runs subagents, monitors and background shells inside one process,
so herdr only ever sees one agent per pane. Every few seconds this watcher:
  - reads the pane's footer, which Claude draws as
        ⏵⏵ auto mode on · 1 shell · 2 monitors · ← 1 agent
        ⏺ main
        ◯ general-purpose  Comparing task-runner signatures   3m 51s · ↓ 4k
    (subagent types and titles, and how many shells/monitors are live now);
  - reads the session transcript (pane pgid → <claude dir>/sessions/<pid>.json
    → <claude dir>/projects/*/<sessionId>.jsonl) and its subagents'
    (<sessionId>/subagents/*.jsonl) for what each monitor and shell is: the
    description it was started with, minus the ones that ended;
and reports it back as pane metadata:
  $bg1..$bg5    rows under the agent, one per subagent/monitor/shell, each led
                by a fixed icon for its kind (◇ general-purpose, ⌕ Explore,
                ▤ Plan, ◎ monitor, ▶ shell, …) and colored by state; a
                shell's row then says how long it has run (▶ 13h …), as does
                a workflow's (▣); armed cron jobs and /loop wakeups come next
                (↻ every 5m …), then the artifacts the session watches (⊙)
  $orphans      on each workspace: processes started under Claude Code that
                outlived their parent (⚠ orphaned: git 6d 167% CPU)
  state labels  idle/done read "working" while any of it runs
The rows are declared in herdr's config.toml (rows_by_agent.claude); the
install-rows action adds them.

Usage:
  claude_bg_watch.py           run the watcher in the foreground
  claude_bg_watch.py --start   detach a supervisor that keeps one watcher
                               running (the plugin's startup hook)
  claude_bg_watch.py --stop    stop the supervisor; the watcher clears its rows
  claude_bg_watch.py --once    print what each Claude pane would show, and exit
  claude_bg_watch.py --clear   clear every row this plugin reported
  claude_bg_watch.py --orphans list the orphaned processes, with their pids
"""
import fcntl
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
import zlib

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PLUGIN_ID = "urit.claude-background"
# Same place herdr gives the plugin, so a start from a plain shell finds the
# lock of the one herdr started.
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "herdr", "plugins", PLUGIN_ID)
# One watcher per herdr server, so each named session gets its own; a shell
# outside herdr talks to the default session's socket.
SOCKET = os.environ.get("HERDR_SOCKET_PATH") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr", "herdr.sock")
LOCK = os.path.join(STATE_DIR, f"watch-{zlib.crc32(SOCKET.encode()):08x}.lock")
LOG = os.path.join(STATE_DIR, "watch.log")
SELF_NAME = os.path.basename(__file__)
SOURCE = "claude-background"
SLOTS = 5
INTERVAL = 3
# Clear only after this many consecutive empty reads, so a transient screen
# (ctrl+o transcript view, a dialog) doesn't make the rows flicker.
CLEAR_AFTER = 3
# Exit after herdr has not answered for this many polls; the next herdr
# server's startup hook starts a new watcher.
GONE_AFTER = 20
HERDR_GONE = 3
# Report every row again this often (in polls), so rows cleared by anything
# else (a herdr restart, another tool) come back within a minute.
REFRESH_EVERY = 20
RESTART_DELAY = 5

MAIN_RE = re.compile(r"^\s*[⏺●◉]\s+main\s*$")
# "◯ general-purpose  Comparing signatures   3m 51s · ↓ 4k"; agents of one
# type started together share a line: "◯ code-review (+9)  Awaiting results".
AGENT_RE = re.compile(r"^(\s*)[◯○●⏺◐◉✻✽✶·]\s+(\S+)(?:\s+\(\+(\d+)\))?\s{2,}(.+?)(?:\s{2,}\S.*)?$")
ELAPSED_RE = re.compile(r"\s+(?:\d+h\s*)?(?:\d+m\s*)?\d+s(?:\s+·.*)?$")
# "1 shell · 2 monitors", or collapsed to "3 background" ("3 bg" when narrow).
# "⧉ 2" in the same footer counts artifacts, not background work.
COUNT_RE = re.compile(r"(\d+)\s+(shell|monitor|background|bg)s?\b")
# Matched at the start of a tool result only: a foreground command's output
# can quote these lines, and such a task would never end.
STARTED_RES = [
    (re.compile(r"Monitor started \(task (\w+)"), "monitor"),
    (re.compile(r"(?:<bash-stdout>)?Command running in background with ID: (\w+)"), "shell"),
    (re.compile(r"(?:<bash-stdout>)?Command was manually backgrounded by user with ID: (\w+)"), "shell"),
    (re.compile(r"Workflow launched (?:in background|in a remote CCR session)\. Task ID: (\w+)"), "workflow"),
    (re.compile(r"(?:<bash-stdout>)?Command (?:did not complete .*? and )?was moved to the background \(ID: (\w+)\)"), "shell"),
]
NOTIF_ID_RE = re.compile(r"<task-id>(\w+)</task-id>")
NOTIF_STATUS_RE = re.compile(r"<status>(\w+)</status>")
# An agent waiting on background work of its own reports "completed" with this
# note, then notifies again when it really finishes.
INTERIM = "with background work of its own still running"
GLYPH = {"monitor": "◎", "shell": "▶", "cron": "↻", "workflow": "▣", "artifact": "⊙"}
WORKFLOW_NAME_RE = re.compile(r"""\bname:\s*['"]([^'"]+)['"]""")
# An Artifact publish result says whether this session now watches the page.
WATCH_RE = re.compile(r"^(?:Live subscription: (?:arming|already connected|connected)"
                      r"|Durable wake subscription registered)", re.M)
ARTIFACT_URL_RE = re.compile(r"https://claude\.ai/(?:code/)?artifact/[\w-]+")
# CronCreate's result, and ScheduleWakeup's (a self-paced /loop).
CRON_RE = re.compile(r"Scheduled (recurring job|one-shot task) (\w+) \(([^)]*)\)")
WAKEUP_RE = re.compile(r"Next wakeup scheduled for .*?\(in (\d+)s\)")
LOOP_FIRE = "Claude resuming /loop wakeup"
# Recurring jobs auto-expire this long after they are created.
CRON_EXPIRES = 7 * 86400
# Orphans: processes with Claude Code's environment whose parent is gone,
# older than this, minus daemons that are meant to outlive what started them.
ORPHAN_AFTER = 600
ORPHAN_DAEMONS = {"gpg-agent", "ssh-agent", "scdaemon", "keyboxd", "dirmngr"}
ORPHAN_ENV_RE = re.compile(r"(?:^|\s)(?:CLAUDECODE=1|CLAUDE_CODE_ENTRYPOINT=)")
ORPHANS_EVERY = 20  # polls, about a minute
EVERY_RE = re.compile(r"^Every (?:(\d+) )?(minute|hour|day)s?$")
# Row color = state: subagents and shells are "working" while listed; a
# monitor is "watching" while armed and "working" for a minute after it last
# fired an event. herdr draws " · " between tokens, so a row is one token and
# its color comes from an invisible leading marker matched by the rules in
# config.toml.
MARKER = {"working": "​", "watching": "⁠", "idle": ""}
MONITOR_FIRED_FOR = 60
# One fixed icon per subagent type; types not listed hash into the pool, so a
# given type always gets the same icon.
AGENT_GLYPH = {
    "general-purpose": "◇",
    "Explore": "⌕",
    "Plan": "▤",
    "claude-code-guide": "?",
    "fork": "⑂",
}
AGENT_GLYPH_POOL = "◆△▲□■○●◈◐◑✦✧⬡⬢"


def log(msg):
    print(f"{time.strftime('%F %T')} {msg}", file=sys.stderr, flush=True)


def agent_glyph(kind):
    if kind in AGENT_GLYPH:
        return AGENT_GLYPH[kind]
    return AGENT_GLYPH_POOL[zlib.crc32(kind.encode()) % len(AGENT_GLYPH_POOL)]


def herdr(*args):
    r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=10)
    return r.stdout


def claude_panes():
    """Pane ids of Claude agents, or None when herdr does not answer."""
    try:
        r = subprocess.run([HERDR, "agent", "list"], capture_output=True, text=True, timeout=10)
        agents = json.loads(r.stdout)["result"]["agents"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    return [a["pane_id"] for a in agents if a.get("agent") == "claude"]


def parse_screen(screen):
    lines = screen.rstrip("\n").splitlines()[-40:]
    agents, counts = [], {}
    # Footer: the last line carrying shell/monitor counts.
    for line in reversed(lines):
        if "⏵⏵" in line or COUNT_RE.search(line) and " · " in line:
            for n, kind in COUNT_RE.findall(line):
                counts["background" if kind == "bg" else kind] = int(n)
            if "/tasks to see subagents" in line:
                counts["hidden_agents"] = 1  # the list is toggled off
            break
    try:
        start = max(i for i, l in enumerate(lines) if MAIN_RE.match(l))
    except ValueError:
        return agents, counts
    for line in lines[start + 1:]:
        m = AGENT_RE.match(line)
        if not m:
            break
        # A truncated title ("…") leaves one space before the timer column.
        kind, more, title = m.group(2), m.group(3), ELAPSED_RE.sub("", m.group(4)).strip()
        agents.append((kind, f"{kind} ×{int(more) + 1}: {title}" if more else title))
    return agents, counts


class Transcript:
    """Incrementally reads one transcript's monitors and background shells."""

    def __init__(self, path):
        self.path, self.offset = path, 0
        self.uses = {}  # tool_use_id -> (description, command)
        self.tasks = {}  # task id -> (kind, description, start epoch)
        self.ended = set()  # task ids stopped or finished
        self.fired = {}  # task id -> epoch of the monitor's latest event
        self.inputs = {}  # tool_use_id -> (tool name, input), for tools read from their result
        self.crons = {}  # job id -> (prompt, schedule, created, recurring, durable)
        self.cron_ended = set()  # job ids deleted or fired once
        self.cron_fired = {}  # job id -> epoch it last fired
        self.wakeup = None  # pending /loop wakeup: (text, fires at, scheduled at)
        self.watched = {}  # artifact url -> (title, epoch the watch began)

    def _text(self, content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return ""

    def update(self):
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # leave a half-written last line for later
        self.offset += end
        for raw in data[:end].splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("type") == "api-request-blob":
                continue  # an untimed copy of a request whose entries come anyway
            when = epoch(entry.get("timestamp"))
            if entry.get("subtype") == "scheduled_task_fire":
                self._fired(entry.get("taskId"), str(entry.get("content") or ""), when)
                continue
            if entry.get("type") == "queue-operation":
                # Task notifications are queued as entries of their own.
                self._notifications(self._text(entry.get("content")), when)
                continue
            msg = entry.get("message") or {}
            content = msg.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") == "TaskStop":
                        self.ended.add(inp.get("task_id") or inp.get("shell_id"))
                    elif b.get("name") == "CronDelete":
                        self.cron_ended.add(inp.get("id"))
                    elif b.get("name") == "ScheduleWakeup" and inp.get("stop"):
                        self.wakeup = None
                    elif b.get("name") == "ArtifactComments" and inp.get("action") == "unwatch" \
                            or b.get("name") == "Artifact" and inp.get("action") == "delete":
                        self.watched.pop(inp.get("url"), None)
                    elif b.get("name") in ("CronCreate", "ScheduleWakeup", "Artifact"):
                        self.inputs[b.get("id")] = (b.get("name"), inp)
                    desc = workflow_name(inp) if b.get("name") == "Workflow" else inp.get("description") or ""
                    self.uses[b.get("id")] = (desc, inp.get("command") or "")
                elif b.get("type") == "tool_result":
                    text = self._text(b.get("content"))
                    if b.get("tool_use_id") in self.inputs:
                        name, inp = self.inputs.pop(b.get("tool_use_id"))
                        if name == "Artifact":
                            self._published(inp, text, when)
                        else:
                            self._scheduled(inp, text, when)
                        continue
                    for rx, kind in STARTED_RES:
                        m = rx.match(text)
                        if m:
                            desc, command = self.uses.get(b.get("tool_use_id"), ("", ""))
                            if SELF_NAME in command:
                                kind = "self"  # this watcher, run from a Claude session
                            self.tasks[m.group(1)] = (kind, desc, when)
                            break
                    self._notifications(text, when)
                elif b.get("type") == "text":
                    self._notifications(b.get("text") or "", when)

    def _scheduled(self, inp, text, when):
        prompt = " ".join(str(inp.get("prompt") or "").split())
        m = CRON_RE.match(text)
        if m:
            self.crons[m.group(2)] = (prompt, m.group(3), when, m.group(1) == "recurring job",
                                      bool(inp.get("durable")))
            return
        m = WAKEUP_RE.match(text)
        if m and when:
            if prompt.startswith("<<"):  # an autonomous loop's sentinel says nothing
                prompt = " ".join(str(inp.get("reason") or "").split())
            self.wakeup = (prompt, when + int(m.group(1)), when)

    def _published(self, inp, text, when):
        if inp.get("action") not in (None, "publish") or not WATCH_RE.search(text):
            return
        m = ARTIFACT_URL_RE.search(text)
        url = m.group(0) if m else inp.get("url")
        if not url:
            return
        path = inp.get("file_path") or ""
        title = inp.get("title") or os.path.splitext(os.path.basename(path))[0] or url.rsplit("/", 1)[-1]
        if url not in self.watched:
            self.watched[url] = (title, when)

    def _fired(self, tid, content, when):
        if content.startswith(LOOP_FIRE):
            self.wakeup = None  # the loop's next turn schedules the one after
            return
        self.cron_fired[tid] = when
        job = self.crons.get(tid)
        if job and not job[3]:
            self.cron_ended.add(tid)  # a one-shot task deletes itself once it fires

    def _notifications(self, text, when):
        # Each <task-notification> with a <status> means that task is over;
        # event-only notifications (a monitor's output line) carry no status.
        for chunk in text.split("<task-notification>")[1:]:
            tid, status = NOTIF_ID_RE.search(chunk), NOTIF_STATUS_RE.search(chunk)
            if tid and status and INTERIM not in chunk:
                self.ended.add(tid.group(1))
            elif tid and when:
                self.fired[tid.group(1)] = when


class Session:
    """A session's transcript plus its subagents' (<session>/subagents/*.jsonl):
    a subagent's monitors and shells count in the same footer."""

    def __init__(self, path, started=0):
        self.path, self.started = path, started  # epoch the Claude process started
        self.subagents = os.path.join(os.path.splitext(path)[0], "subagents")
        self.transcripts = {path: Transcript(path)}

    def update(self):
        for p in glob.glob(os.path.join(self.subagents, "*.jsonl")):
            if p not in self.transcripts:
                self.transcripts[p] = Transcript(p)
        for t in self.transcripts.values():
            t.update()

    def _ended(self):
        return set().union(*(t.ended for t in self.transcripts.values()))

    def agents(self):
        """Background subagents still running, newest first: started by this
        Claude process (an earlier one's can't be) and not reported finished."""
        ended, found = self._ended(), []
        for meta in glob.glob(os.path.join(self.subagents, "agent-*.meta.json")):
            aid = os.path.basename(meta)[len("agent-"):-len(".meta.json")]
            try:
                if aid in ended or os.path.getmtime(meta[:-len(".meta.json")] + ".jsonl") < self.started:
                    continue
                with open(meta) as f:
                    m = json.load(f)
                if m.get("requestShape") != "background":
                    continue  # a foreground agent's end is not notified
                found.append((os.path.getmtime(meta), m.get("agentType") or "agent", m.get("description") or ""))
            except (OSError, ValueError):
                continue
        return [(kind, desc) for _, kind, desc in sorted(found, reverse=True)]

    def live(self, kind, n, since=0):
        """The n most recently started live tasks of a kind (the footer's count
        wins), as (description, start epoch, epoch it last fired)."""
        if n <= 0:
            return []
        ended = self._ended()
        fired = {}
        for t in self.transcripts.values():
            fired.update(t.fired)
        tasks = sorted((when, desc, fired.get(tid, 0))
                       for t in self.transcripts.values()
                       for tid, (k, desc, when) in t.tasks.items()
                       if k == kind and tid not in ended and when >= since)
        return [(desc, when, last) for when, desc, last in tasks[-n:]]

    def watches(self):
        """Titles of the artifacts this Claude process watches, oldest first; a
        live subscription ends with the process."""
        found = {}
        for t in self.transcripts.values():
            found.update(t.watched)
        return [title for title, when in sorted(found.values(), key=lambda v: v[1]) if when >= self.started]

    def crons(self, now):
        """(state, text) of each armed cron job, oldest first, then the pending
        /loop wakeup. Session-only jobs die with the Claude process that made
        them, so an earlier process's are gone."""
        ended, fired, jobs = set(), {}, []
        for t in self.transcripts.values():
            ended |= t.cron_ended
            fired.update(t.cron_fired)
            jobs += t.crons.items()
        rows = []
        for jid, (prompt, schedule, created, recurring, durable) in sorted(jobs, key=lambda j: j[1][2]):
            if jid in ended or (created < self.started and not durable):
                continue
            if recurring and now - created > CRON_EXPIRES:
                continue
            state = "working" if 0 <= now - fired.get(jid, 0) <= MONITOR_FIRED_FOR else "watching"
            rows.append((state, f"{every(schedule)} {prompt}".strip()))
        wakeup = self.transcripts[self.path].wakeup
        if wakeup and wakeup[2] >= self.started:
            at = time.strftime("%H:%M", time.localtime(wakeup[1]))
            rows.append(("watching", f"at {at} {wakeup[0]}".strip()))
        return rows


sessions = {}


def transcript_for(pane):
    try:
        pid = claude_pid(pane)
        with open(os.path.join(CLAUDE_DIR, "sessions", f"{pid}.json")) as f:
            info = json.load(f)
        sid, started = info["sessionId"], (info.get("startedAt") or 0) / 1000
        path = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", f"{sid}.jsonl"))[0]
    except (ValueError, KeyError, OSError, IndexError):
        return None
    t = sessions.get(pane)
    if not t or t.path != path:  # /clear or a new session in the same pane
        t = sessions[pane] = Session(path)
    t.started = started  # a resumed session keeps its path but not its process
    t.update()
    return t


EVAL_RE = re.compile(r"&& eval '(.*?)' < /dev/null")


def claude_pid(pane):
    try:
        info = json.loads(herdr("pane", "process-info", "--pane", pane))
        return info["result"]["process_info"]["foreground_process_group_id"]
    except (ValueError, KeyError):
        return None


def shell_commands(pane):
    """(command, start epoch) of the background shells Claude is running, oldest first.

    Claude starts each as a child `zsh -c "… && eval '<command>' < /dev/null …"`.
    """
    pid = claude_pid(pane)
    if not pid:
        return []
    out = subprocess.run(["ps", "-A", "-ww", "-o", "ppid=,etime=,command="],
                         capture_output=True, text=True, timeout=10).stdout
    now, cmds = time.time(), []
    for line in out.splitlines():
        ppid, elapsed, command = (line.split(None, 2) + ["", ""])[:3]
        m = EVAL_RE.search(command)
        if ppid == str(pid) and m and "shell-snapshots" in command:
            ran = etime(elapsed)
            cmds.append((m.group(1).replace("'\\''", "'").replace(os.path.expanduser("~"), "~"),
                         now - ran if ran is not None else None))
    return sorted(cmds, key=lambda c: c[1] or 0)


def etime(text):
    """Seconds from ps's elapsed time ([[dd-]hh:]mm:ss), or None."""
    days, _, rest = text.rpartition("-")
    try:
        secs = 0
        for part in rest.split(":"):
            secs = secs * 60 + int(part)
        return secs + int(days or 0) * 86400
    except ValueError:
        return None


def workflow_name(inp):
    m = WORKFLOW_NAME_RE.search(str(inp.get("script") or ""))
    if m:
        return m.group(1)
    return inp.get("name") or os.path.splitext(os.path.basename(inp.get("scriptPath") or ""))[0] or "workflow"


def watch_row(titles):
    if len(titles) == 1:
        return f"watching {titles[0]}"
    return f"watching {len(titles)} artifacts: {', '.join(titles)}"


def orphans(ps_out):
    """-> [(name, pid, seconds running, %cpu)], busiest first, from
    `ps -A -E -ww -o pid=,ppid=,etime=,%cpu=,command=` (command then environment)."""
    found = []
    for line in ps_out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or parts[1] != "1" or not ORPHAN_ENV_RE.search(parts[4]):
            continue
        name = os.path.basename(parts[4].split()[0])
        ran = etime(parts[2])
        if name in ORPHAN_DAEMONS or SELF_NAME in parts[4] or ran is None or ran < ORPHAN_AFTER:
            continue
        try:
            found.append((name, int(parts[0]), ran, float(parts[3])))
        except ValueError:
            continue
    return sorted(found, key=lambda o: -o[3])


def orphan_row(found):
    if not found:
        return None
    name, _, ran, cpu = found[0]
    more = f" +{len(found) - 1}" if len(found) > 1 else ""
    return f"⚠ orphaned: {name} {age(ran)} {cpu:.0f}% CPU{more}"


def find_orphans():
    out = subprocess.run(["ps", "-A", "-E", "-ww", "-o", "pid=,ppid=,etime=,%cpu=,command="],
                         capture_output=True, text=True, timeout=10).stdout
    return orphans(out)


def report_orphans(row):
    """Show the orphan warning on every workspace (they're machine-wide)."""
    try:
        spaces = json.loads(herdr("workspace", "list"))["result"]["workspaces"]
    except (ValueError, KeyError, TypeError):
        return
    for ws in spaces:
        args = ["workspace", "report-metadata", ws["workspace_id"], "--source", SOURCE]
        args += ["--token", f"orphans={row}"] if row else ["--clear-token", "orphans"]
        out = herdr(*args)
        if '"error"' in out:
            log(f"{ws['workspace_id']}: {out.strip()}")


def every(schedule):
    """Compact cron schedule: "Every 5 minutes" -> "every 5m"; others as given."""
    m = EVERY_RE.match(schedule)
    return f"every {m.group(1) or 1}{m.group(2)[0]}" if m else schedule


def age(seconds):
    """Compact age for a row: <1m, 59m, 13h, 2d."""
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def render(pane, agents, counts):
    """-> (items, label); items are (state, icon, text) rows under the agent."""
    t = transcript_for(pane)  # cron jobs have no footer count, so always read it
    if t and "background" in counts and not {"shell", "monitor"} & set(counts):
        # The collapsed footer doesn't say which kinds are running, so take the
        # transcript's live ones, capped by its total.
        for kind in ("monitor", "shell"):
            counts[kind] = len(t.live(kind, counts["background"]))
    if t and counts.get("hidden_agents") and not agents:
        # The footer hides the list ("/tasks to see subagents"), so take the
        # running agents from the transcripts.
        agents = t.agents()
    now = time.time()
    items = [("working", agent_glyph(kind), desc) for kind, desc in agents]
    if t:  # the footer doesn't count workflows; this process's live ones
        items += [("working", GLYPH["workflow"], f"{age(now - started)} {desc}")
                  for desc, started, _ in t.live("workflow", SLOTS, since=t.started)]
    for kind in ("monitor", "shell"):
        n = counts.get(kind, 0)
        if kind == "shell" and t:
            # The footer counts this watcher too when a session started it.
            n -= len(t.live("self", n))
        named = t.live(kind, n) if t else []
        for desc, started, fired in named:
            state = "working"
            if kind == "monitor" and now - fired > MONITOR_FIRED_FOR:
                state = "watching"
            text = desc or f"background {kind}"
            if kind == "shell" and started:
                text = f"{age(now - started)} {text}"
            items.append((state, GLYPH[kind], text))
        # Counted in the footer but not found in the transcript: name shells
        # after the command their process is running, else stay generic.
        missing = n - len(named)
        found = shell_commands(pane)[-missing:] if kind == "shell" and missing > 0 else []
        found = [(c, started) for c, started in found if SELF_NAME not in c][:missing]
        items += [("working", GLYPH[kind], f"{age(now - started)} {c}" if started else c)
                  for c, started in found]
        items += [("working", GLYPH[kind], f"background {kind}")] * (missing - len(found))
    # An armed cron job or a watched artifact alone doesn't make an idle agent "working".
    label = "working" if items else None
    if t:
        items += [(state, GLYPH["cron"], text) for state, text in t.crons(now)]
        watched = t.watches()
        if watched:
            items.append(("watching", GLYPH["artifact"], watch_row(watched)))
    if len(items) > SLOTS:
        items = items[: SLOTS - 1] + [("idle", "", f"+{len(items) - SLOTS + 1} more")]
    return tuple(items), label


def report(pane, items, label):
    args = ["pane", "report-metadata", pane, "--source", SOURCE]
    for i in range(SLOTS):
        if i < len(items):
            state, icon, text = items[i]
            row = f"{MARKER[state]}{icon + ' ' if icon else ''}{text}"
            args += ["--token", f"bg{i + 1}={row}"]
        else:
            args += ["--clear-token", f"bg{i + 1}"]
    if label:
        args += ["--state-label", f"idle={label}", "--state-label", f"done={label}"]
    else:
        args += ["--clear-state-labels"]
    out = herdr(*args)
    if '"error"' in out:
        log(f"{pane}: {out.strip()}")


def clear_all(*_):
    for pane in claude_panes() or []:
        report(pane, [], None)
    report_orphans(None)
    sys.exit(0)


def watch():
    signal.signal(signal.SIGTERM, clear_all)
    signal.signal(signal.SIGINT, clear_all)
    # Start with every pane "shown" so rows left behind by a watcher that died
    # without clearing them (kill -9, crash) get cleared on the first quiet reads.
    shown, empty, down, last_error = {p: None for p in claude_panes() or []}, {}, 0, None
    polls, parent, orphans_shown = 0, os.getppid(), ""  # "" = not reported yet
    while os.path.exists(__file__):  # gone = the plugin was uninstalled
        if os.getppid() != parent:  # the supervisor died; the next start brings a new pair
            log("supervisor gone; exiting")
            break
        try:
            panes = claude_panes()
            polls += 1
            if panes is None:
                down += 1
                if down >= GONE_AFTER:
                    log("herdr stopped answering; exiting")
                    sys.exit(HERDR_GONE)
                time.sleep(INTERVAL)
                continue
            if down or polls % REFRESH_EVERY == 0:
                # A restarted herdr server has none of the rows we reported.
                shown, down, orphans_shown = {p: None for p in panes}, 0, ""
            for pane in panes:
                screen = herdr("pane", "read", pane, "--source", "visible")
                state = render(pane, *parse_screen(screen))
                if not state[0]:
                    empty[pane] = empty.get(pane, 0) + 1
                    if pane not in shown or empty[pane] < CLEAR_AFTER:
                        continue
                else:
                    empty[pane] = 0
                if shown.get(pane) != state:
                    report(pane, *state)
                    if state[0]:
                        shown[pane] = state
                    else:
                        shown.pop(pane, None)
            for pane in set(shown) - set(panes):
                shown.pop(pane, None)
            if polls % ORPHANS_EVERY == 1:
                row = orphan_row(find_orphans())
                if row != orphans_shown:
                    report_orphans(row)
                    orphans_shown = row
            last_error = None
        except Exception as e:  # keep the loop alive across herdr restarts
            if repr(e) != last_error:
                log(repr(e))
                last_error = repr(e)
        time.sleep(INTERVAL)
    clear_all()


def lock_holder():
    """-> pid of the running supervisor, or None."""
    try:
        with open(LOCK) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        pass
    return None


def start():
    """Detach a supervisor and return at once (startup hooks must exit)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    pid = lock_holder()
    if pid:
        print(f"already running (supervisor pid {pid}); log: {LOG}")
        return
    if os.fork():
        print(f"started; log: {LOG}")
        return
    os.setsid()
    if os.fork():
        os._exit(0)
    out = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    lock = open(LOCK, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os._exit(0)  # another start won the race
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    supervise()
    os._exit(0)


def supervise():
    """Keep one watcher running; restart it if it crashes or is killed."""
    child, stopping = [None], [False]

    # Only signal here: the loop below is already in wait(), and a second
    # wait() from inside this handler blocks on it until its timeout.
    def stop(*_):
        stopping[0] = True
        if child[0] and child[0].poll() is None:
            child[0].terminate()  # the watcher clears its rows on SIGTERM
            signal.alarm(15)  # then SIGKILL it if it hangs

    def kill(*_):
        if child[0] and child[0].poll() is None:
            child[0].kill()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    signal.signal(signal.SIGALRM, kill)
    log(f"supervisor started (pid {os.getpid()}, herdr {HERDR}, socket {SOCKET})")
    while os.path.exists(__file__) and not stopping[0]:
        child[0] = subprocess.Popen([sys.executable, __file__])
        code = child[0].wait()
        if stopping[0]:
            log("supervisor stopped")
            return
        if code in (0, HERDR_GONE):
            log(f"watcher exited ({code}); supervisor done")
            return
        log(f"watcher exited ({code}); restarting in {RESTART_DELAY}s")
        time.sleep(RESTART_DELAY)
    if stopping[0]:
        log("supervisor stopped")


def stop():
    pid = lock_holder()
    if not pid:
        print("not running")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"stopped supervisor pid {pid}")


if __name__ == "__main__":
    arg = sys.argv[1:]
    if arg == ["--start"]:
        start()
    elif arg == ["--stop"]:
        stop()
    elif arg == ["--once"]:
        for p in claude_panes() or []:
            print(p, render(p, *parse_screen(herdr("pane", "read", p, "--source", "visible"))))
    elif arg == ["--orphans"]:
        found = find_orphans()
        for name, pid, ran, cpu in found:
            print(f"{pid:>7}  {age(ran):>4}  {cpu:5.1f}% CPU  {name}")
        print(f"kill {' '.join(str(o[1]) for o in found)}" if found else "no orphaned processes")
    elif arg == ["--clear"]:
        clear_all()
    elif not arg:
        watch()
    else:
        sys.exit(__doc__)
