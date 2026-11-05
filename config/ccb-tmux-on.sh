#!/usr/bin/env bash
set -euo pipefail

command -v tmux >/dev/null 2>&1 || exit 0
[[ -n "${TMUX:-}" ]] || exit 0

session=""; window=""; owner="${CCB_SESSION_ID:-manual}"
while (($#)); do
  case "$1" in
    --session) session="${2:-}"; shift 2 ;;
    --window) window="${2:-}"; shift 2 ;;
    --owner) owner="${2:-}"; shift 2 ;;
    *) exit 2 ;;
  esac
done
if [[ -z "$session" ]]; then
  session="$(tmux display-message -p -t "${TMUX_PANE:-}" '#{session_id}' 2>/dev/null || true)"
fi
if [[ -z "$window" ]]; then
  window="$(tmux display-message -p -t "${TMUX_PANE:-}" '#{window_id}' 2>/dev/null || true)"
fi
[[ -n "$session" && -n "$window" && -n "$owner" ]] || exit 0
# Canonicalise targets so every spelling of a session (name or id) shares one lock.
session="$(tmux display-message -p -t "$session" '#{session_id}' 2>/dev/null || true)"
window="$(tmux display-message -p -t "$window" '#{window_id}' 2>/dev/null || true)"
server_pid="$(tmux display-message -p '#{pid}' 2>/dev/null || true)"
[[ -n "$session" && -n "$window" ]] || exit 0
tmux list-windows -t "$session" -F '#{window_id}' 2>/dev/null | grep -qxF -- "$window" || exit 0
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
release_lock() {
  if ((lock_owned == 1)); then flock -u 9; elif ((lock_owned == 2)); then rm -rf "$mkdir_lock"; fi
}
trap release_lock EXIT

if [[ -n "$(tmux show-options -qv -t "$session" @ccb_active 2>/dev/null || true)" ]]; then exit 3; fi
encode() { printf %s "$1" | base64 | tr -d '\n'; }
save_option() {
  local scope="$1" target="$2" opt="$3" key="$4" raw="" value="" present=0
  if [[ "$scope" == session ]]; then
    raw="$(tmux show-options -q -t "$target" "$opt" 2>/dev/null || true)"
    value="$(tmux show-options -qv -t "$target" "$opt" 2>/dev/null || true)"
  else
    raw="$(tmux show-options -w -q -t "$target" "$opt" 2>/dev/null || true)"
    value="$(tmux show-options -w -qv -t "$target" "$opt" 2>/dev/null || true)"
  fi
  if [[ -n "$raw" ]]; then present=1; fi
  tmux set-option -t "$session" "@ccb_prev_${key}_set" "$present"
  tmux set-option -t "$session" "@ccb_prev_${key}" "$(encode "$value")"
}

session_opts=(status status-position status-justify status-interval status-style status-left status-left-length status-right status-right-length)
for opt in "${session_opts[@]}"; do save_option session "$session" "$opt" "$opt"; done
format_indices=(0 1)
format_listing="$(tmux show-options -q -t "$session" status-format 2>/dev/null || true)"
format_present=0
[[ -n "$format_listing" ]] && format_present=1
while IFS= read -r line; do
  if [[ "$line" =~ ^status-format\[([0-9]+)\] ]]; then
    index="${BASH_REMATCH[1]}"
    [[ " ${format_indices[*]} " == *" $index "* ]] || format_indices+=("$index")
  fi
done <<< "$format_listing"
for i in "${format_indices[@]}"; do save_option session "$session" "status-format[$i]" "status_format_$i"; done
tmux set-option -t "$session" @ccb_prev_status_format_set "$format_present"
tmux set-option -t "$session" @ccb_prev_status_format_indices "${format_indices[*]}"
save_option session "$session" @ccb_version version
window_opts=(pane-border-status pane-border-format pane-border-style pane-active-border-style window-status-format window-status-current-format window-status-separator)
for opt in "${window_opts[@]}"; do save_option window "$window" "$opt" "window_${opt//-/_}"; done
hook="$(tmux show-hooks -t "$session" after-select-pane 2>/dev/null || true)"
tmux set-option -t "$session" @ccb_prev_hook_set "$([[ -n "$hook" ]] && echo 1 || echo 0)"
tmux set-option -t "$session" @ccb_prev_hook "$(encode "$hook")"
tmux set-option -t "$session" @ccb_snapshot_window "$window"
tmux set-option -t "$session" @ccb_active "$owner"
tmux set-option -t "$session" @ccb_snapshot_complete 1
rollback_on_failure() {
  local rc=$?
  if ((rc != 0)); then
    release_lock
    "$(dirname "${BASH_SOURCE[0]}")/ccb-tmux-off.sh" --session "$session" --window "$window" --owner "$owner" || true
  else
    release_lock
  fi
  return "$rc"
}
trap rollback_on_failure EXIT

detect_theme() {
  local theme="${CCB_THEME:-auto}"
  case "$theme" in
    light|dark) echo "$theme" ;;
    auto|"")
      local bg_rgb=""
      if command -v timeout >/dev/null 2>&1; then
        bg_rgb="$(timeout 0.3s bash -c 'printf "\033]11;?\033\\"; read -t 0.2 -r bg; echo "${bg#*;}"' 2>/dev/null || true)"
      fi
      if [[ -n "$bg_rgb" && "$bg_rgb" =~ rgb: ]]; then
        local hex="${bg_rgb#rgb:}"
        hex="${hex//\//}"
        if [[ ${#hex} -ge 12 ]]; then
          local r=$((0x${hex:0:4} / 256)) g=$((0x${hex:4:4} / 256)) b=$((0x${hex:8:4} / 256))
          if (( (r * 299 + g * 587 + b * 114) / 1000 > 128 )); then echo light; else echo dark; fi
        else echo dark; fi
      else
        case "${COLORFGBG:-}" in *";15"|*";7") echo light;; *) echo dark;; esac
      fi
      ;;
    *) echo dark ;;
  esac
}
theme="$(detect_theme)"
if [[ "$theme" == light ]]; then
  bg="#f8f9fa"; fg="#2d3748"; muted="#718096"; blue="#3182ce"; purple="#805ad5"; red="#d53f8c"; orange="#dd6b20"; green="#38a169"; teal="#319795"
  border_inactive='fg=#cbd5e0,bold'; border_active="fg=$blue,bold"; pane_default_fg="#4a5568"
else
  bg="#1e1e2e"; fg="#cdd6f4"; muted="#6c7086"; blue="#89b4fa"; purple="#cba6f7"; red="#f38ba8"; orange="#fab387"; green="#a6e3a1"; teal="#94e2d5"
  border_inactive='fg=#3b4261,bold'; border_active='fg=#7aa2f7,bold'; pane_default_fg="#565f89"
fi
status_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ccb-status.sh"
git_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ccb-git.sh"
tmux set-option -t "$session" status-position bottom
tmux set-option -t "$session" status-interval "${CCB_TMUX_STATUS_INTERVAL:-5}"
tmux set-option -t "$session" status-style "bg=$bg fg=$fg"
tmux set-option -t "$session" status 2
tmux set-option -t "$session" status-left-length 80
tmux set-option -t "$session" status-right-length 120
tmux set-option -t "$session" 'status-format[1]' "#[align=centre,bg=$bg,fg=$muted]Copy: MouseDrag  Paste: Shift-Ctrl-v  Focus: Ctrl-b o"
tmux set-option -t "$session" 'status-format[0]' "#[align=left bg=$bg]#{T:status-left}#[align=centre fg=$muted]#{b:pane_current_path}#[align=right]#{T:status-right}"
git_info='-'; [[ -x "$git_script" ]] && git_info="#($git_script \"#{pane_current_path}\")"
accent="#{?client_prefix,$red,#{?pane_in_mode,$orange,$purple}}"
label='#{?client_prefix,KEY,#{?pane_in_mode,COPY,INPUT}}'
tmux set-option -t "$session" status-left "#[fg=$bg,bg=$accent,bold] $label #[fg=$accent,bg=$purple]#[fg=$bg,bg=$purple] $git_info #[fg=$purple,bg=$bg]"
ccb_version="$(ccb --print-version 2>/dev/null || echo '?')"
tmux set-option -t "$session" @ccb_version "$ccb_version"
status_right="#[fg=$red,bg=$bg]#[fg=$bg,bg=$red,bold] #{?#{@ccb_agent},#{@ccb_agent},-} #[fg=$purple,bg=$red]#[fg=$bg,bg=$purple,bold] CCB:#{@ccb_version} #[fg=$blue,bg=$purple]#[fg=$fg,bg=$blue] #($status_script modern) #[fg=$orange,bg=$blue]#[fg=$bg,bg=$orange,bold] %m/%d %a %H:%M #[default]#{T:@ccb_status_right_extra}"
tmux set-option -t "$session" status-right "$status_right"
tmux set-window-option -t "$window" window-status-format ''
tmux set-window-option -t "$window" window-status-current-format ''
tmux set-window-option -t "$window" window-status-separator ''
tmux set-window-option -t "$window" pane-border-status top
tmux set-window-option -t "$window" pane-border-style "$border_inactive"
tmux set-window-option -t "$window" pane-active-border-style "$border_active"
pane_format='#{?#{==:#{@ccb_agent},Claude},#[fg='${bg}']#[bg='${red}']#[bold] #P Claude #[default],'
pane_format+='#{?#{==:#{@ccb_agent},Codex},#[fg='${bg}']#[bg='${orange}']#[bold] #P Codex #[default],'
pane_format+='#{?#{==:#{@ccb_agent},Gemini},#[fg='${bg}']#[bg='${green}']#[bold] #P Gemini #[default],'
pane_format+='#{?#{==:#{@ccb_agent},OpenCode},#[fg='${bg}']#[bg='${purple}']#[bold] #P OpenCode #[default],'
pane_format+='#{?#{==:#{@ccb_agent},Cmd},#[fg='${bg}']#[bg='${teal}']#[bold] #P Cmd #[default],'
pane_format+='#[fg='${pane_default_fg}'] #P #{pane_title} #[default]}}}}}'
tmux set-window-option -t "$window" pane-border-format "$pane_format"
tmux set-hook -t "$session" after-select-pane "run-shell \"${BASH_SOURCE[0]%/*}/ccb-border.sh \\\"#{pane_id}\\\"\""
pane_id="$(tmux display-message -p -t "$window" '#{pane_id}')"
"$(dirname "${BASH_SOURCE[0]}")/ccb-border.sh" "$pane_id"
