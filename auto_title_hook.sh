#!/bin/bash
# SessionStart/UserPromptSubmit/Stop hook: inside herdr, hand the event to the
# background titler (auto_title.py, next to this script) and return immediately.
[ -n "$HERDR_AUTO_TITLE_CHILD" ] && exit 0
[ -z "$HERDR_PANE_ID" ] || [ -z "$HERDR_TAB_ID" ] && exit 0
mkdir -p "$HOME/.cache/herdr-auto-title"
input=$(cat)
nohup python3 "$(dirname "$0")/auto_title.py" "$HERDR_PANE_ID" "$HERDR_TAB_ID" \
  <<<"$input" >>"$HOME/.cache/herdr-auto-title/log" 2>&1 &
exit 0
