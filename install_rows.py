#!/usr/bin/env python3
"""Add the Claude agent layout with background-work rows to herdr's config.toml.

Plugins cannot ship config, so this writes [ui.sidebar.agents.rows_by_agent]
claude = [...] once, validates it with `herdr config check`, and reloads the
running server. An existing claude layout is left alone; the rows to add to it
by hand are printed instead.

Usage: install_rows.py [--print] [--config PATH]
  --print        only print the layout
  --config PATH  config file to edit (default: herdr's config.toml)
"""
import os
import re
import shutil
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
HEADER = "[ui.sidebar.agents.rows_by_agent]"

# One token per row (herdr draws " · " between tokens); an invisible leading
# marker picks the color: U+200B working (amber), U+2060 watching (teal),
# none = idle (grey).
BG_ROW = ('  [{ token = "$bg%d", fg = "#6c7086", rules = ['
          '{ starts_with = "\\u200B", fg = "#b7a788" }, '
          '{ starts_with = "\\u2060", fg = "#71a7a3" }] }],')
BG_ROWS = "\n".join(BG_ROW % i for i in range(1, 6))
# The workspace row for orphaned processes. A [ui.sidebar.spaces] rows list
# replaces herdr's default one, so it is printed for adding by hand.
ORPHANS_HINT = ('For orphaned-process warnings, add this row to [ui.sidebar.spaces] rows:\n'
                '  [{ token = "$orphans", fg = "#f38ba8" }],')

LAYOUT = f"""claude = [
  ["state_icon", {{ token = "terminal_title_stripped", fg = "#cdd6f4", bold = true }}],
  [
    {{ token = "state_text", fg = "#6c7086", rules = [
      {{ starts_with = "\\u200Bworking", fg = "#f04b4b" }},
      {{ starts_with = "\\u2060working", fg = "#c71f4e" }},
      {{ starts_with = "\\u200Cworking", fg = "#e64fd3" }},
      {{ starts_with = "working", fg = "#f9e2af" }},
      {{ starts_with = "blocked", fg = "#f38ba8", bold = true }},
      {{ starts_with = "done", fg = "#a6e3a1" }},
    ] }},
    {{ token = "tab", fg = "#89b4fa", rules = [{{ lt = 100, hide = true }}] }},
    "workspace",
  ],
  # Background work from the urit.claude-background plugin.
{BG_ROWS}
]
"""


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "config.toml")


def with_layout(text):
    """-> new config text, or None when a claude layout already exists."""
    lines = text.splitlines(keepends=True)
    try:
        at = next(i for i, l in enumerate(lines) if l.strip() == HEADER)
    except StopIteration:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{sep}{HEADER}\n{LAYOUT}"
    for line in lines[at + 1:]:
        if line.lstrip().startswith("["):
            break  # next table
        if re.match(r"\s*claude\s*=", line):
            return None
    return "".join(lines[: at + 1]) + LAYOUT + "".join(lines[at + 1:])


def main():
    args = sys.argv[1:]
    if "--print" in args:
        print(f"{HEADER}\n{LAYOUT}", end="")
        return 0
    path = args[args.index("--config") + 1] if "--config" in args else config_path()
    text = open(path).read() if os.path.exists(path) else ""
    if "$bg1" in text:
        print(f"{path} already has the background-work rows")
        if "$orphans" not in text:
            print(ORPHANS_HINT)
        return 0
    new = with_layout(text)
    if new is None:
        print(f"{path} already has its own claude layout; add these rows to it:\n{BG_ROWS}")
        return 1
    backup = None
    if os.path.exists(path):
        backup = f"{path}.bak-claude-background-{time.strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(path, backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(new)
    if "--config" not in args:
        # A broken config still exits 0 ("…; using defaults"), so match the line.
        check = subprocess.run([HERDR, "config", "check"], capture_output=True, text=True)
        if check.stdout.strip() != "config: ok":
            if backup:
                shutil.copy2(backup, path)
            print(f"herdr rejected the change, config restored:\n{check.stdout}{check.stderr}")
            return 1
        subprocess.run([HERDR, "server", "reload-config"], capture_output=True)
    print(f"added the claude layout to {path}" + (f" (backup: {backup})" if backup else ""))
    print(ORPHANS_HINT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
