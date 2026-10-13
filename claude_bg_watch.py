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
    → <claude dir>/projects/*/<sessionId>.jsonl) for what each monitor and
    shell is: the description it was started with, minus the ones that ended;
and reports it back as pane metadata:
  $bg1..$bg5    rows under the agent, one per subagent/monitor/shell, each led
                by a fixed icon for its kind (◇ general-purpose, ⌕ Explore,
                ▤ Plan, ◎ monitor, ▶ shell, …) and colored by state
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
AGENT_RE = re.compile(r"^(\s*)[◯○●⏺◐◉✻✽✶·]\s+(\S+)\s{2,}(.+?)(?:\s{2,}\S.*)?$")
ELAPSED_RE = re.compile(r"\s+(?:\d+h\s*)?(?:\d+m\s*)?\d+s(?:\s+·.*)?$")
COUNT_RE = re.compile(r"(\d+)\s+(shell|monitor)s?\b")
STARTED_RES = [
    (re.compile(r"Monitor started \(task (\w+)"), "monitor"),
    (re.compile(r"running in background with ID: (\w+)"), "shell"),
    (re.compile(r"moved to the background \(ID: (\w+)\)"), "shell"),
]
NOTIF_ID_RE = re.compile(r"<task-id>(\w+)</task-id>")
NOTIF_STATUS_RE = re.compile(r"<status>(\w+)</status>")
GLYPH = {"monitor": "◎", "shell": "▶"}
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
                counts[kind] = int(n)
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
        agents.append((m.group(2), ELAPSED_RE.sub("", m.group(3)).strip()))
    return agents, counts


class Transcript:
    """Incrementally tracks a session's live monitors and background shells."""

    def __init__(self, path):
        self.path, self.offset = path, 0
        self.uses = {}  # tool_use_id -> (description, command)
        self.tasks = {}  # task id -> (kind, description), in start order
        self.fired = {}  # task id -> epoch of the monitor's latest event

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
            msg = entry.get("message") or {}
            when = epoch(entry.get("timestamp"))
            content = msg.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") == "TaskStop":
                        self.tasks.pop(inp.get("task_id") or inp.get("shell_id"), None)
                    self.uses[b.get("id")] = (inp.get("description") or "", inp.get("command") or "")
                elif b.get("type") == "tool_result":
                    text = self._text(b.get("content"))
                    for rx, kind in STARTED_RES:
                        m = rx.search(text)
                        if m:
                            desc, command = self.uses.get(b.get("tool_use_id"), ("", ""))
                            if SELF_NAME in command:
                                kind = "self"  # this watcher, run from a Claude session
                            self.tasks[m.group(1)] = (kind, desc)
                            break
                    self._notifications(text, when)
                elif b.get("type") == "text":
                    self._notifications(b.get("text") or "", when)

    def _notifications(self, text, when):
        # Each <task-notification> with a <status> means that task is over;
        # event-only notifications (a monitor's output line) carry no status.
        for chunk in text.split("<task-notification>")[1:]:
            tid, status = NOTIF_ID_RE.search(chunk), NOTIF_STATUS_RE.search(chunk)
            if tid and status:
                self.tasks.pop(tid.group(1), None)
            elif tid and when:
                self.fired[tid.group(1)] = when

    def live(self, kind, n):
        """The n most recently started live tasks of a kind (the footer's count wins)."""
        if n <= 0:
            return []
        return [(d, self.fired.get(tid, 0)) for tid, (k, d) in self.tasks.items() if k == kind][-n:]


transcripts = {}


def transcript_for(pane):
    try:
        pid = claude_pid(pane)
        with open(os.path.join(CLAUDE_DIR, "sessions", f"{pid}.json")) as f:
            sid = json.load(f)["sessionId"]
        path = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", f"{sid}.jsonl"))[0]
    except (ValueError, KeyError, OSError, IndexError):
        return None
    t = transcripts.get(pane)
    if not t or t.path != path:  # /clear or a new session in the same pane
        t = transcripts[pane] = Transcript(path)
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
    """Commands of the background shells Claude is running, oldest first.

    Claude starts each as a child `zsh -c "… && eval '<command>' < /dev/null …"`.
    """
    pid = claude_pid(pane)
    if not pid:
        return []
    out = subprocess.run(["ps", "-A", "-ww", "-o", "ppid=,command="],
                         capture_output=True, text=True).stdout
    cmds = []
    for line in out.splitlines():
        ppid, _, command = line.strip().partition(" ")
        m = EVAL_RE.search(command)
        if ppid == str(pid) and m and "shell-snapshots" in command:
            cmds.append(m.group(1).replace("'\\''", "'").replace(os.path.expanduser("~"), "~"))
    return cmds


def epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def render(pane, agents, counts):
    """-> (items, label); items are (state, icon, text) rows under the agent."""
    t = transcript_for(pane) if counts else None
    now = time.time()
    items = [("working", agent_glyph(kind), desc) for kind, desc in agents]
    for kind in ("monitor", "shell"):
        n = counts.get(kind, 0)
        if kind == "shell" and t:
            # The footer counts this watcher too when a session started it.
            n -= len(t.live("self", n))
        named = t.live(kind, n) if t else []
        for desc, fired in named:
            state = "working"
            if kind == "monitor" and now - fired > MONITOR_FIRED_FOR:
                state = "watching"
            items.append((state, GLYPH[kind], desc or f"background {kind}"))
        # Counted in the footer but not found in the transcript: name shells
        # after the command their process is running, else stay generic.
        missing = n - len(named)
        found = shell_commands(pane)[-missing:] if kind == "shell" and missing > 0 else []
        found = [c for c in found if SELF_NAME not in c][:missing]
        items += [("working", GLYPH[kind], c) for c in found]
        items += [("working", GLYPH[kind], f"background {kind}")] * (missing - len(found))
    if len(items) > SLOTS:
        items = items[: SLOTS - 1] + [("idle", "", f"+{len(items) - SLOTS + 1} more")]
    return tuple(items), "working" if items else None


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
    sys.exit(0)


def watch():
    signal.signal(signal.SIGTERM, clear_all)
    signal.signal(signal.SIGINT, clear_all)
    # Start with every pane "shown" so rows left behind by a watcher that died
    # without clearing them (kill -9, crash) get cleared on the first quiet reads.
    shown, empty, down, last_error = {p: None for p in claude_panes() or []}, {}, 0, None
    polls, parent = 0, os.getppid()
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
                shown, down = {p: None for p in panes}, 0
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
    elif arg == ["--clear"]:
        clear_all()
    elif not arg:
        watch()
    else:
        sys.exit(__doc__)
