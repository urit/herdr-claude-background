# Claude background work for herdr

A [herdr](https://herdr.dev) plugin that lists what each Claude Code agent is
running in the background — subagents, monitors and background shells — as
rows under that agent in the sidebar.

Claude Code runs all of that inside one process, so herdr sees a single agent
that often looks idle while work is still going on:

```
◐ Refactoring the auth middleware
  working · auth
  ◇ Comparing task-runner signatures
  ⌕ Find every caller of report()
  ◎ CI run for the auth PR
  ▶ 13h npm test -- --watch
  ↻ every 5m Check Slack for new messages
```

Each row starts with an icon for its kind and is colored by state:

| Icon | Kind |
| --- | --- |
| `◇` `⌕` `▤` `⑂` … | subagent (general-purpose, Explore, Plan, fork; other types get a fixed icon of their own) |
| `◎` | monitor |
| `▶` | background shell, then how long it has run (`<1m`, `42m`, `13h`, `2d`) so a hung one stands out |
| `↻` | armed cron job (`CronCreate`, a fixed-interval `/loop`) with its schedule, or a self-paced `/loop`'s next wakeup (`at 10:46`) |
| `▣` | workflow (the Workflow tool), named after its script, with how long it has run |
| `⊙` | artifacts this session watches for comments and new versions, collapsed into one row |

Amber means working, teal is a monitor, cron job or artifact watch that is armed and waiting
(a cron row turns amber for a minute after it fires), grey is idle. Up to five rows are shown; past that, the fifth reads `+N more`. While any of
it runs, the agent's state reads `working` even when Claude itself is idle;
an armed cron job or watched artifact alone doesn't count.

The agent's `working` label is colored by the highest `/effort` running in
it, its own or an active subagent's: red for `high`,
grenadine for `xhigh`, magenta for `max` (lower levels keep the usual yellow).
herdr can't recolor its own state icon by anything but state, so the word
carries it.

Under each workspace in the spaces list, a red row warns about processes that
Claude Code started and that outlived whatever started them (a `nohup`ed
command, a hung `git` whose Claude exited):

```
✓ workspace
  ⚠ orphaned: git 6d 166% CPU +1
```

`herdr plugin action invoke urit.claude-background.orphans` lists them with
their pids and the `kill` command.

## Install

```sh
herdr plugin install urit/herdr-claude-background
herdr plugin action invoke urit.claude-background.install-rows
herdr plugin action invoke urit.claude-background.start
```

`install-rows` adds a Claude layout with the five rows to `config.toml` and
reloads herdr. Plugins can't ship config, so this is a one-time step on each
machine. If you already have a `claude` entry under
`[ui.sidebar.agents.rows_by_agent]`, it leaves it alone and prints the rows
to paste into it (or run `python3 install_rows.py --print` in the plugin
directory to see the whole layout).

The orphan warning needs one row in `[ui.sidebar.spaces]`, which
`install-rows` prints rather than writes (a spaces `rows` list replaces
herdr's default one):

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  [{ token = "$orphans", fg = "#f38ba8" }],
]
```

`start` is only needed the first time: after that, herdr starts the watcher
every time its server starts.

Requires herdr 0.9.1 or newer, `python3` (3.8+) on the `PATH` herdr runs
with, and Claude Code. macOS and Linux.

## How it works

Every 3 seconds the watcher reads each Claude pane's screen for the footer
Claude draws (`1 shell · 2 monitors`, and the list of running subagents
under `⏺ main`; agents started together share a line, shown as
`code-review ×10: …`), reads the session's transcripts — its own and its
subagents' — for what each monitor and shell was started to do, and reports
the result as `$bg1`..`$bg5` pane metadata. When the subagent list is toggled
off (`/tasks to see subagents`), the running background agents come from the
transcripts instead: those started by the current Claude process with no
final completion notice yet. Workflows, cron jobs, `/loop` wakeups and watched
artifacts have no footer count, so they come from the transcripts only.

About once a minute it also looks for orphans: processes whose environment
says Claude Code started them (`CLAUDECODE=1`, `CLAUDE_CODE_ENTRYPOINT`), whose
parent is gone, and that have run for over 10 minutes, minus agents meant to
outlive their parent (`gpg-agent`, `ssh-agent`, …). It reads only those two
variables, and reports the result as `$orphans` workspace metadata.

The watcher runs under a small supervisor that restarts it if it crashes or is
killed, with a lock so only one runs per herdr server (each named session gets
its own). It clears its rows when stopped, clears rows left behind by a copy
that died without clearing them, re-sends every row once a minute in case
something else cleared them, and exits once herdr stops answering — the next
herdr start launches it again.

## Troubleshooting

It reads Claude Code's screen, so a Claude Code update that changes the footer
or the transcript format makes the rows silently stop appearing. To check,
start something in the background in a Claude session and run, from the
plugin directory:

```sh
python3 claude_bg_watch.py --once
```

It prints what each Claude pane would show. The log is `watch.log` in the
plugin's state directory (`~/.local/state/herdr/plugins/urit.claude-background/`).

```sh
herdr plugin action invoke urit.claude-background.stop    # clears the rows
herdr plugin action invoke urit.claude-background.start
```

## Uninstall

```sh
herdr plugin action invoke urit.claude-background.stop
herdr plugin uninstall urit.claude-background
```

Then remove the `$bg1`..`$bg5` rows from `config.toml` if you like; rows with
no value take no space.
