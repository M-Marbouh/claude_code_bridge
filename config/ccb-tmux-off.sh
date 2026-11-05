#!/usr/bin/env bash
set -euo pipefail
command -v tmux >/dev/null 2>&1 || exit 0
session=""; window=""; owner=""
if (($#)) && [[ "${1:-}" != --* ]]; then session="$1"; shift; fi
if (($# == 0)) && [[ -z "$session" && -z "${TMUX:-}" ]]; then exit 0; fi
while (($#)); do case "$1" in --session) session="${2:-}"; shift 2;; --window) window="${2:-}"; shift 2;; --owner) owner="${2:-}"; shift 2;; *) exit 2;; esac; done
[[ -n "$session" ]] || session="$(tmux display-message -p '#{session_id}' 2>/dev/null || true)"
[[ -n "$session" ]] || exit 0
# Canonicalise so `off <name>` and `off --session <id>` take the same lock as on.
session="$(tmux display-message -p -t "$session" '#{session_id}' 2>/dev/null || true)"
server_pid="$(tmux display-message -p '#{pid}' 2>/dev/null || true)"
[[ -n "$session" ]] || exit 0
lock_dir="${CCB_RUNTIME_TMP:-${TMPDIR:-/tmp}}/ccb-tmux-locks"; mkdir -p "$lock_dir"
lock_key="${server_pid}_${session//[^a-zA-Z0-9_-]/_}"
lock_file="$lock_dir/$lock_key.lock"
lock_owned=0
if command -v flock >/dev/null 2>&1; then
  exec 9>"$lock_file"
  flock -w 10 9 || exit 1
  lock_owned=1
else
  mkdir_lock="$lock_file.d"
  deadline=$((SECONDS + 10))
  while ! mkdir "$mkdir_lock" 2>/dev/null; do
    holder="$(cat "$mkdir_lock/pid" 2>/dev/null || true)"
    if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then rm -rf "$mkdir_lock"; continue; fi
    if [[ -z "$holder" ]]; then
      mtime="$(stat -c %Y "$mkdir_lock" 2>/dev/null || stat -f %m "$mkdir_lock" 2>/dev/null || echo 0)"
      (( $(date +%s) - mtime > 1 )) && { rm -rf "$mkdir_lock"; continue; }
    fi
    ((SECONDS < deadline)) || exit 1
    sleep 0.05
  done
  printf '%s\n' "$$" > "$mkdir_lock/pid"
  lock_owned=2
fi
release_lock() { if ((lock_owned == 1)); then flock -u 9; elif ((lock_owned == 2)); then rm -rf "$mkdir_lock"; fi; }
trap release_lock EXIT
active="$(tmux show-options -qv -t "$session" @ccb_active 2>/dev/null || true)"
complete="$(tmux show-options -qv -t "$session" @ccb_snapshot_complete 2>/dev/null || true)"
[[ "$complete" == 1 && -n "$active" ]] || exit 0
[[ -z "$owner" || "$owner" == "$active" ]] || exit 0
window="$(tmux show-options -qv -t "$session" @ccb_snapshot_window 2>/dev/null || true)"
[[ -n "$window" ]] || exit 0
decode() { printf %s "$1" | base64 -d; }
restore_option() {
  local scope="$1" target="$2" opt="$3" key="$4" present value
  present="$(tmux show-options -qv -t "$session" "@ccb_prev_${key}_set" 2>/dev/null || true)"
  [[ "$present" == 0 || "$present" == 1 ]] || return 0
  value="$(tmux show-options -qv -t "$session" "@ccb_prev_${key}" 2>/dev/null || true)"
  if [[ "$present" == 0 ]]; then
    if [[ "$scope" == session ]]; then tmux set-option -u -t "$target" "$opt"; else tmux set-window-option -u -t "$target" "$opt"; fi
  else
    value="$(decode "$value")"
    if [[ "$scope" == session ]]; then tmux set-option -t "$target" "$opt" "$value"; else tmux set-window-option -t "$target" "$opt" "$value"; fi
  fi
}
session_opts=(status status-position status-justify status-interval status-style status-left status-left-length status-right status-right-length)
for opt in "${session_opts[@]}"; do restore_option session "$session" "$opt" "$opt"; done
format_present="$(tmux show-options -qv -t "$session" @ccb_prev_status_format_set 2>/dev/null || true)"
format_indices="$(tmux show-options -qv -t "$session" @ccb_prev_status_format_indices 2>/dev/null || true)"
if [[ "$format_present" == 0 ]]; then
  tmux set-option -u -t "$session" status-format
elif [[ "$format_present" == 1 ]]; then
  tmux set-option -u -t "$session" status-format
  for i in $format_indices; do restore_option session "$session" "status-format[$i]" "status_format_$i"; done
fi
restore_option session "$session" @ccb_version version
window_opts=(pane-border-status pane-border-format pane-border-style pane-active-border-style window-status-format window-status-current-format window-status-separator)
# The launch window may have been closed while the session lives on: restore
# what still exists and finish the session-level cleanup regardless.
# (display-message -t on a dead window id can silently resolve to another window.)
if tmux list-windows -t "$session" -F '#{window_id}' 2>/dev/null | grep -qxF -- "$window"; then
  for opt in "${window_opts[@]}"; do restore_option window "$window" "$opt" "window_${opt//-/_}"; done
fi
hook_set="$(tmux show-options -qv -t "$session" @ccb_prev_hook_set 2>/dev/null || true)"
hook_data="$(tmux show-options -qv -t "$session" @ccb_prev_hook 2>/dev/null || true)"
tmux set-hook -t "$session" -u after-select-pane
if [[ "$hook_set" == 1 ]]; then
  hook_data="$(decode "$hook_data")"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    hook_name="${line%% *}"
    cmd="${line#* }"
    tmux set-hook -t "$session" "$hook_name" "$cmd"
  done <<< "$hook_data"
fi
"$(dirname "${BASH_SOURCE[0]}")/ccb-border.sh" --restore "$session"
tmux set-option -t "$session" -u @ccb_active
tmux set-option -t "$session" -u @ccb_snapshot_complete
tmux set-option -t "$session" -u @ccb_snapshot_window
for key in status status-position status-justify status-interval status-style status-left status-left-length status-right status-right-length version window_pane_border_status window_pane_border_format window_pane_border_style window_pane_active_border_style window_window_status_format window_window_status_current_format window_window_status_separator hook_set hook status_format_set status_format_indices; do
  tmux set-option -t "$session" -u "@ccb_prev_${key}" 2>/dev/null || true
  tmux set-option -t "$session" -u "@ccb_prev_${key}_set" 2>/dev/null || true
done
for i in $format_indices; do
  tmux set-option -t "$session" -u "@ccb_prev_status_format_$i" 2>/dev/null || true
  tmux set-option -t "$session" -u "@ccb_prev_status_format_${i}_set" 2>/dev/null || true
done
