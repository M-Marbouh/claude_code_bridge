#!/usr/bin/env bash
set -euo pipefail
command -v tmux >/dev/null 2>&1 || exit 0
encode() { printf %s "$1" | base64 | tr -d '\n'; }
decode() { printf %s "$1" | base64 -d; }
# pane-active-border-style is a window option: `set-option -p` on a pane writes
# its window's value. Snapshots are therefore per window, taken before the first
# mutation. The launch window is covered by ccb-tmux-on.sh's own snapshot.
if [[ "${1:-}" == --restore ]]; then
  session="${2:-}"
  [[ -n "$session" ]] || exit 0
  while IFS= read -r win; do
    [[ -n "$win" ]] || continue
    marked="$(tmux show-options -w -qv -t "$win" @ccb_prev_pabs_set 2>/dev/null || true)"
    [[ -n "$marked" ]] || continue
    present="$(tmux show-options -w -qv -t "$win" @ccb_prev_pabs_present 2>/dev/null || true)"
    value="$(tmux show-options -w -qv -t "$win" @ccb_prev_pabs 2>/dev/null || true)"
    if [[ "$present" == 1 ]]; then tmux set-option -w -t "$win" pane-active-border-style "$(decode "$value")"; else tmux set-option -wu -t "$win" pane-active-border-style; fi
    tmux set-option -wu -t "$win" @ccb_prev_pabs_set
    tmux set-option -wu -t "$win" @ccb_prev_pabs_present
    tmux set-option -wu -t "$win" @ccb_prev_pabs
  done < <(tmux list-windows -t "$session" -F '#{window_id}' 2>/dev/null || true)
  exit 0
fi
pane_id="${1:-${TMUX_PANE:-}}"
[[ "$pane_id" == %* ]] || exit 0
agent="$(tmux display-message -p -t "$pane_id" '#{@ccb_agent}' 2>/dev/null | tr -d '\r')"
title="$(tmux display-message -p -t "$pane_id" '#{pane_title}' 2>/dev/null | tr -d '\r')"
win="$(tmux display-message -p -t "$pane_id" '#{window_id}' 2>/dev/null || true)"
sess="$(tmux display-message -p -t "$pane_id" '#{session_id}' 2>/dev/null || true)"
launch_win="$(tmux show-options -qv -t "$sess" @ccb_snapshot_window 2>/dev/null || true)"
if [[ -n "$win" && "$win" != "$launch_win" ]]; then
  marked="$(tmux show-options -w -qv -t "$win" @ccb_prev_pabs_set 2>/dev/null || true)"
  if [[ -z "$marked" ]]; then
    raw="$(tmux show-options -w -q -t "$win" pane-active-border-style 2>/dev/null || true)"
    present=0; value=""
    if [[ -n "$raw" ]]; then
      present=1
      value="$(tmux show-options -w -qv -t "$win" pane-active-border-style 2>/dev/null || true)"
    fi
    tmux set-option -w -t "$win" @ccb_prev_pabs_present "$present"
    tmux set-option -w -t "$win" @ccb_prev_pabs "$(encode "$value")"
    tmux set-option -w -t "$win" @ccb_prev_pabs_set 1
  fi
fi
set_border() { tmux set-option -p -t "$pane_id" pane-active-border-style "$1" 2>/dev/null || true; }
case "$agent" in
  Codex) set_border 'fg=#ff9e64,bold' ;;
  Gemini) set_border 'fg=#7dcfff,bold' ;;
  Claude) set_border 'fg=#bb9af7,bold' ;;
  OpenCode) set_border 'fg=#9ece6a,bold' ;;
  Cmd) set_border 'fg=#7dcfff,bold' ;;
  *)
    case "$title" in
      CCB-Codex*) set_border 'fg=#ff9e64,bold' ;;
      CCB-Gemini*) set_border 'fg=#7dcfff,bold' ;;
      Claude*) set_border 'fg=#bb9af7,bold' ;;
      CCB-OpenCode*) set_border 'fg=#9ece6a,bold' ;;
      CCB-Cmd*) set_border 'fg=#7dcfff,bold' ;;
      *) set_border 'fg=#7aa2f7,bold' ;;
    esac
    ;;
esac
