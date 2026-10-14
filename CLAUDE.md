# Working on this plugin

The author's machine runs this checkout linked (`herdr plugin link .`), so an
edit is live after a restart:

```sh
herdr plugin action invoke urit.claude-background.stop
herdr plugin action invoke urit.claude-background.start
tail -f ~/.local/state/herdr/plugins/urit.claude-background/watch.log
python3 claude_bg_watch.py --once   # what each Claude pane would show
```

Other machines install from GitHub, so a change reaches them only after a
push and a reinstall (`herdr plugin install urit/herdr-claude-background --yes`,
then the `start` action). Bump `version` in `herdr-plugin.toml` with each
change worth reinstalling for.

## Before pushing

- `python3 -m unittest`. When Claude Code changes its footer or transcript
  format, add the real lines to `test_claude_bg_watch.py` before fixing the
  parser.
- Look at the sidebar, not only `--once`: report test values on a pane with a
  different `--source` (`herdr pane report-metadata <pane> --source test
  --token 'bg1=…'`) and render it with
  `herdr pane read <pane> --source visible --ansi | freeze --language ansi`.
- If the change touches the supervisor or the watcher loop, rerun: a second
  `--start` (also from a shell outside herdr), `kill -9` of the watcher (back
  in 5s), `kill -9` of the supervisor (the watcher exits and clears its
  rows), and `--stop` (every row gone in about a second).
- `install_rows.py` changes: test against a throwaway config with
  `XDG_CONFIG_HOME=<tmp> python3 install_rows.py` — herdr reads that path
  too, so `herdr config check` validates it. Note that `config check` exits 0
  on a broken config; only the `config: ok` line means valid.

## herdr constraints that shape the code

- A row's tokens are joined with ` · `, so each row is one token and one
  color; the color comes from an invisible leading marker (U+200B, U+2060)
  matched by `starts_with` rules in config.
- One `report-metadata` call may set at most 16 tokens.
- Leading spaces in token values are trimmed.
- Startup hooks are one-shot, which is why `--start` detaches a supervisor.
