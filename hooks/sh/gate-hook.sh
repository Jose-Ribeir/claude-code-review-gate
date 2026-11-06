#!/usr/bin/env bash
#
# review-gate — Kimchi Studio bash hook (the UX layer).
#
# Kimchi feeds this hook JSON on stdin:
#   {"tool_name":"bash","input":{"command":"...","cwd":"..."},"cwd":"..."}
# and expects one of:
#   {"decision":"block","reason":"..."}   on stdout  -> block the call
#   (no output, exit 0)                               -> pass through
#
# Hook failure = ALLOW in Kimchi, so this script NEVER exits nonzero on an
# internal error without first emitting an explicit decision JSON. Every
# error path below prints a block decision and exits 0.
#
# Layers (see README "Two enforcement layers"):
#   - the git pre-push hook is the PRIMARY gate; this hook is the UX layer and
#     is deliberately INERT outside repositories that installed it (the repo
#     guard below). A global install must not gate unrelated repositories.
#
# Parse inversion (D14 of the port plan): a positively-parsed non-push command
# passes through; a parsed push goes to review-gate.py; a push-SHAPED command
# this hook cannot positively parse (bare `git push`, `--all`, `--mirror`,
# `--tags`, `git -c k=v push`, ...) is BLOCKED with "re-run with an explicit
# refspec". Unknown is never allowed.
#
# Budget: Kimchi kills a hook after 5 s. This script is pure bash (<100 ms
# typical) and only execs Python once a push is positively parsed; Python's
# own hook-mode budget is 3 s (see OCR_INLINE_BUDGET in scripts/review-gate.py).
#
# Ported from scripts/gate-hook.sh (claude-code-review-gate); the strict push
# parsing ports _looks_like_real_push/_parse_push (review-gate.py:1584/:1710)
# adapted for Kimchi's recursive command parsing: quoted strings are DATA of
# the enclosing command, because Kimchi fires this hook again on the inner
# segment when the quotes actually delimit a command.
set -uo pipefail

# Where this script lives: the repo checkout (hooks/sh/). The installed copy
# is stamped with __GATE_DIR__ by install-kimchi-hook.sh; the runtime override
# is OCR_GATE_DIR, then the pointer review-gate.py refreshes on every run.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE_DIR="${OCR_GATE_DIR:-__GATE_DIR__}"
if [ ! -f "$GATE_DIR/review-gate.py" ]; then
  if [ -f "$SELF_DIR/../../scripts/review-gate.py" ]; then
    GATE_DIR="$SELF_DIR/../../scripts"
  elif [ -f "$SELF_DIR/review-gate.py" ]; then
    GATE_DIR="$SELF_DIR"
  else
    ptr="${OCR_STATE_DIR:-$HOME/.local/state/review-gate}/gate-dir"
    if [ -f "$ptr" ] && [ -f "$(cat "$ptr" 2>/dev/null)/review-gate.py" ]; then
      GATE_DIR="$(cat "$ptr")"
    fi
  fi
fi

BLOCK_UNPARSEABLE='review-gate: could not parse push command — re-run with an explicit refspec (git push <remote> <ref>)'
BLOCK_MULTI='review-gate: this command runs more than one `git push`. A review covers one push; reviewing the first would leave the rest unreviewed. Run them as separate commands.'
BLOCK_NOPYTHON='review-gate: no working Python 3.7+ interpreter found, so the push could not be reviewed. Blocking, because a gate that cannot run must not wave a push through. Fix: install Python 3, then retry. Emergency bypass: OCR_FAIL_OPEN=1 (or git push --no-verify from a terminal).'
BLOCK_NOGATE='review-gate: review-gate.py not found (broken install), so the push could not be reviewed. Blocking. Re-run scripts/install-kimchi-hook.sh. Emergency bypass: OCR_FAIL_OPEN=1 (or git push --no-verify from a terminal).'

_emit_block() {  # $1 = reason
  printf '{"decision":"block","reason":"%s"}' "$1"
  exit 0
}

_pass() {  # pass through: no output, exit 0
  exit 0
}

_fail_open_requested() {
  case "${OCR_FAIL_OPEN:-}" in 1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]) return 0 ;; esac
  return 1
}

# ---------------------------------------------------------------------------
# Minimal JSON string extraction (pure bash). Sets REPLY to the decoded value
# of "$2" in object "$1"; returns 1 when absent/not a string. Handles the JSON
# escapes that can appear in a command string.
# ---------------------------------------------------------------------------
json_get() {
  local j="$1" key="\"$2\"" prefix rest ch esc out
  prefix="${j%%"$key"*}"
  [ "$prefix" = "$j" ] && return 1
  rest="${j:$((${#prefix} + ${#key}))}"
  while [ -n "$rest" ]; do
    ch="${rest:0:1}"
    case "$ch" in
      ' '|$'\t'|$'\r'|$'\n') rest="${rest:1}" ;;
      ':') rest="${rest:1}"; break ;;
      *) return 1 ;;
    esac
  done
  while [ -n "$rest" ]; do
    ch="${rest:0:1}"
    case "$ch" in
      ' '|$'\t'|$'\r'|$'\n') rest="${rest:1}" ;;
      *) break ;;
    esac
  done
  [ "${rest:0:1}" = '"' ] || return 1
  rest="${rest:1}"
  out=""
  while [ -n "$rest" ]; do
    ch="${rest:0:1}"
    if [ "$ch" = '\' ]; then
      esc="${rest:1:1}"
      case "$esc" in
        '"') out+='"' ;;
        '\') out+='\' ;;
        '/')  out+='/' ;;
        'n')  out+=$'\n' ;;
        't')  out+=$'\t' ;;
        'r')  out+=$'\r' ;;
        'b')  out+=$'\b' ;;
        'f')  out+=$'\f' ;;
        *)    out+="$esc" ;;
      esac
      rest="${rest:2}"
    elif [ "$ch" = '"' ]; then
      REPLY="$out"
      return 0
    else
      out+="$ch"
      rest="${rest:1}"
    fi
  done
  return 1
}

# JSON-escape a bash string for re-emitting (control chars that appear in
# commands are \n \t \r; anything else unprintable is dropped).
json_escape() {
  local s="$1" out="" ch
  while [ -n "$s" ]; do
    ch="${s:0:1}"
    case "$ch" in
      '\\') out+='\\\\' ;;
      '"')  out+='\"' ;;
      $'\n') out+='\n' ;;
      $'\t') out+='\t' ;;
      $'\r') out+='\r' ;;
      $'\b'|$'\f') : ;;
      *)    out+="$ch" ;;
    esac
    s="${s:1}"
  done
  REPLY="$out"
}

# ---------------------------------------------------------------------------
# Heredoc stripping (port of _strip_heredocs): a heredoc BODY is data, never
# code. Only strips when a terminator is found, matching upstream.
# ---------------------------------------------------------------------------
strip_heredocs() {
  local in="$1" out="" marker found
  [ "${in#*"<<"}" = "$in" ] && { REPLY="$in"; return 0; }
  local -a lines=()
  local _line
  while IFS= read -r _line; do lines+=("$_line"); done <<< "$in"
  local i=0 m=${#lines[@]}
  while [ "$i" -lt "$m" ]; do
    local line="${lines[i]}"
    out+="$line"$'\n'
    i=$((i + 1))
    if [[ "$line" =~ \<\<-?[[:space:]]*([A-Za-z_][A-Za-z0-9_]*) ]]; then
      marker="${BASH_REMATCH[1]}"
      found=0
      while [ "$i" -lt "$m" ]; do
        if [ "$(printf '%s' "${lines[i]}" | tr -d ' \t')" = "$marker" ]; then
          found=1
          i=$((i + 1))
          break
        fi
        i=$((i + 1))   # body dropped
      done
      [ "$found" = 1 ] && out+="$marker"$'\n'
    fi
  done
  REPLY="${out%$'\n'}"
}

# ---------------------------------------------------------------------------
# Tokenizer + classifier (port of _looks_like_real_push for Kimchi's payload).
#
# Splits the command into top-level words, tracking quote state. Quoted spans
# become single words (their content is DATA). At each command position
# (start, or after ; & | ( ) { } ` $( or a newline) the word sequence is:
#   [NAME=VALUE...] git [global options] push
# `git`'s global options: -C <dir>, -c <k=v> (both take a separate value), and
# any other -flag. `--` ends them.
#
# Sets PUSH_FOUND=1 when the command performs a real `git push` (count in
# PUSH_COUNT), and PUSH_WORDS = the tokens AFTER `push` up to the next
# separator, redirections dropped.
# ---------------------------------------------------------------------------
PUSH_FOUND=0
PUSH_COUNT=0
PUSH_WORDS=""

classify_command() {
  local cmd="$1"
  strip_heredocs "$cmd"
  local code="$REPLY"
  local -a words=()
  local word="" i=0 n=${#code} ch quote="" word_started=0
  PUSH_FOUND=0
  PUSH_COUNT=0
  PUSH_WORDS=""
  while [ "$i" -lt "$n" ]; do
    ch="${code:i:1}"
    if [ -n "$quote" ]; then
      if [ "$ch" = '\' ] && [ "$quote" = '"' ]; then
        word+="${code:i:2}"; i=$((i + 2)); continue
      fi
      if [ "$ch" = "$quote" ]; then
        quote=""
        words+=("$word"); word=""   # a closed quoted span is one word
      else
        word+="$ch"
      fi
      i=$((i + 1)); continue
    fi
    case "$ch" in
      '"'|"'")
        quote="$ch"; word_started=1; i=$((i + 1)); continue ;;
      '\')
        word+="${code:i+1:1}"; i=$((i + 2)); word_started=1; continue ;;
      ' '|$'\t'|$'\r')
        if [ -n "$word" ]; then words+=("$word"); word=""; fi
        word_started=0
        i=$((i + 1)); continue ;;
      ';'|'&'|'|'|'('|')'|'{'|'}'|'`'|$'\n')
        # command separator ( && || $( are all covered by these chars )
        if [ -n "$word" ]; then words+=("$word"); word=""; fi
        words+=(";")   # normalized separator
        word_started=0
        i=$((i + 1)); continue ;;
      '$')
        # '$(' opens a command substitution; a bare $ is part of the word
        if [ "${code:i+1:1}" = '(' ]; then
          if [ -n "$word" ]; then words+=("$word"); word=""; fi
          words+=(";")
          word_started=0
          i=$((i + 2)); continue
        fi
        word+='$'; word_started=1; i=$((i + 1)); continue ;;
      '#')
        # comment: only when it starts a word (shell rule)
        if [ "$word_started" = 0 ]; then
          break   # rest of the command is a comment
        fi
        word+='#'; i=$((i + 1)); continue ;;
      *)
        word+="$ch"; word_started=1; i=$((i + 1)); continue ;;
    esac
  done
  [ -n "$word" ] && words+=("$word")

  # Walk the word list looking for `git ... push` at command positions.
  local k=0 m=${#words[@]} w found_at=-1
  while [ "$k" -lt "$m" ]; do
    # skip env prefixes at a command position
    while [ "$k" -lt "$m" ]; do
      w="${words[k]}"
      if [[ "$w" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then k=$((k + 1)); else break; fi
    done
    if [ "$k" -lt "$m" ] && [ "${words[k]}" = "git" ]; then
      k=$((k + 1))
      # git global options
      while [ "$k" -lt "$m" ]; do
        w="${words[k]}"
        [ "$w" = ";" ] && break
        [ "$w" = "--" ] && { k=$((k + 1)); break; }
        if [ "$w" = "-C" ] || [ "$w" = "-c" ]; then
          k=$((k + 2)); continue
        fi
        [[ "$w" == -* ]] && { k=$((k + 1)); continue; }
        break
      done
      if [ "$k" -lt "$m" ] && [ "${words[k]}" = "push" ]; then
        PUSH_COUNT=$((PUSH_COUNT + 1))
        found_at="$k"
      fi
    fi
    # advance to the next separator
    while [ "$k" -lt "$m" ] && [ "${words[k]}" != ";" ]; do k=$((k + 1)); done
    k=$((k + 1))
  done
  if [ "$PUSH_COUNT" -gt 0 ] && [ "$found_at" -ge 0 ]; then
    PUSH_FOUND=1
    # tokens after `push` until the next separator, redirections dropped
    local -a toks=()
    local skip=0
    k=$((found_at + 1))
    while [ "$k" -lt "$m" ] && [ "${words[k]}" != ";" ]; do
      w="${words[k]}"
      if [ "$skip" = 1 ]; then skip=0; k=$((k + 1)); continue; fi
      if [[ "$w" =~ ^[0-9]*(\>\>|\<) ]] || [ "$w" = ">" ] || [ "$w" = "<" ]; then
        case "$w" in
          *'&'*) : ;;                     # 2>&1: no value to swallow
          *) skip=1 ;;
        esac
        k=$((k + 1)); continue
      fi
      toks+=("$w")
      k=$((k + 1))
    done
    if [ "${#toks[@]}" -gt 0 ]; then
      PUSH_WORDS="${toks[*]}"
    else
      PUSH_WORDS=""
    fi
  fi
}

# ---------------------------------------------------------------------------
# D14 pre-check on the tokens after `push` (redirections already dropped):
#   return 0 = positively-parsed push (route to review-gate.py)
#   return 1 = push-shaped but unparseable -> block with the retry message
#   return 2 = nothing to review (dry-run / deletion) -> pass
# ---------------------------------------------------------------------------
push_shape() {
  local toks="$1"
  [ -z "$toks" ] && return 1                      # bare `git push`
  local -a t
  read -r -a t <<< "$toks"
  local pos=() w skip=0
  for w in "${t[@]}"; do
    case "$w" in
      --all|--branches|--mirror|--tags) return 1 ;;
      -n|--dry-run) return 2 ;;
      -d|--delete) return 2 ;;
    esac
  done
  for w in "${t[@]}"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$w" in
      --repo|-o|--push-option|--receive-pack|--exec) skip=1; continue ;;
      -*) continue ;;
    esac
    pos+=("$w")
  done
  [ "${#pos[@]}" -ge 2 ] || return 1              # remote but no refspec
  for w in "${pos[@]:1}"; do
    case "$w" in
      :*) continue ;;                              # deletion refspec
      *) if [[ "$w" == *'$'* || "$w" == *'`'* ]]; then return 1; fi ;;
    esac
  done
  return 0
}

# ---------------------------------------------------------------------------
# Python resolution (fail closed when absent).
# ---------------------------------------------------------------------------
_find_python() {
  local _c _p
  for _c in python3 python; do
    _p="$(command -v "$_c" 2>/dev/null)" || continue
    case "$_p" in *[Ww]indows[Aa]pps*) continue ;; esac
    if "$_p" -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" >/dev/null 2>&1; then
      printf '%s' "$_p"; return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
payload="$(cat 2>/dev/null || true)"

# Inside the review session: the gate's own worker must not re-gate itself.
# File-writing shapes inside a review go to review-gate.py --mode guard (the
# same policy as upstream's adapter); everything else passes through.
if [ "${OCR_IN_REVIEW:-}" = "1" ]; then
  case "$payload" in
    *--o*|*'>'*)
      if PY="$(_find_python)" && [ -f "$GATE_DIR/review-gate.py" ]; then
        printf '%s' "$payload" | "$PY" "$GATE_DIR/review-gate.py" --mode guard
        exit 0
      fi
      _emit_block "review-gate: cannot vet a file-writing command inside the review (no Python); refusing it."
      ;;
  esac
  _pass
fi

# Read the command (Kimchi puts it under input.command; the re-wrapped
# tool_input.command shape from the git adapter is accepted too).
cmd=""
json_get "$payload" "command" && cmd="$REPLY"
[ -z "$cmd" ] && _pass

# Loose substring pre-filter before any classification work.
case "$cmd" in
  *git*push*) ;;
  *) _pass ;;
esac

classify_command "$cmd"
[ "$PUSH_COUNT" -gt 1 ] && { _fail_open_requested && _pass; _emit_block "$BLOCK_MULTI"; }
[ "$PUSH_FOUND" = 1 ] || _pass

# --- from here on the command performs exactly one real `git push` ----------

# Parse inversion (D14): positively-parsed shapes go on; push-shaped but
# unparseable shapes block with the retry message.
push_shape "$PUSH_WORDS"
shape_rc=$?
[ "$shape_rc" = 1 ] && { _fail_open_requested && _pass; _emit_block "$BLOCK_UNPARSEABLE"; }
[ "$shape_rc" = 2 ] && _pass                       # dry-run / delete: nothing to review

# Repository guard: this hook is INERT outside repositories that installed the
# gate (i.e. have our pre-push hook). Resolves the payload cwd, not the
# process cwd.
json_get "$payload" "cwd" && hookcwd="$REPLY" || hookcwd=""
[ -z "$hookcwd" ] && hookcwd="$PWD"
gitdir="$(git -C "$hookcwd" rev-parse --absolute-git-dir 2>/dev/null)" || _pass
if [ ! -f "$gitdir/hooks/pre-push" ] || ! grep -q "review-gate" "$gitdir/hooks/pre-push" 2>/dev/null; then
  _pass                                            # not a gated repository
fi

# Re-wrap Kimchi's payload into the upstream shape review-gate.py reads
# ({tool_name, tool_input:{command}, cwd}) and hand over.
PY="$(_find_python)" || PY=""
if [ -z "$PY" ]; then
  _fail_open_requested && _pass
  _emit_block "$BLOCK_NOPYTHON"
fi
if [ ! -f "$GATE_DIR/review-gate.py" ]; then
  _fail_open_requested && _pass
  _emit_block "$BLOCK_NOGATE"
fi
json_escape "$cmd"
esc_cmd="$REPLY"
json_escape "$hookcwd"
esc_cwd="$REPLY"
session=""
json_get "$payload" "session_id" && session="$REPLY"
if [ -n "$session" ]; then
  json_escape "$session"
  rewrapped='{"session_id":"'"$REPLY"'","tool_name":"Bash","cwd":"'"$esc_cwd"'","tool_input":{"command":"'"$esc_cmd"'"}}'
else
  rewrapped='{"tool_name":"Bash","cwd":"'"$esc_cwd"'","tool_input":{"command":"'"$esc_cmd"'"}}'
fi
exec "$PY" "$GATE_DIR/review-gate.py" --mode hook <<< "$rewrapped"
# If exec itself failed (cannot happen when the checks above passed), deny.
_emit_block "$BLOCK_NOGATE"
