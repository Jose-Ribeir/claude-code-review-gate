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
# parsing ports _looks_like_real_push/_parse_push in scripts/review-gate.py
# (the AUTHORITATIVE master classifier -- this bash copy must track its
# behaviour, not cite its line numbers, which go stale on every edit)
# adapted for Kimchi's recursive command parsing: quoted strings are DATA of
# the enclosing command, because Kimchi fires this hook again on the inner
# segment when the quotes actually delimit a command.
# set -e is SAFE here only because every deliberate nonzero return is
# captured below: a bare `fn; rc=$?` would abort this hook mid-payload and,
# because hook failure = ALLOW in Kimchi, silently wave a push through.
# Keep it that way: any new function call that can return nonzero must be
# guarded (`|| rc=$?`, `if`, `&&`, `|| _pass`, ...).
set -euo pipefail

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
BLOCK_SENDPACK='review-gate: git send-pack is plumbing-level push transport; git never runs its pre-push hook for it, so it would bypass the review entirely. Use: git push <remote> <ref>. Emergency bypass: OCR_FAIL_OPEN=1 (or git push --no-verify from a terminal).'
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
# Gated-repo detection: does the pre-push hook git would actually run reach
# the review gate? The entry hook may be a shim (husky 9: .husky/_/pre-push
# sources ./h, which runs .husky/pre-push), so every script the chain
# sources/execs is checked for "review-gate", not just the entry file.
# _hook_gated <entry-pre-push> — needs the globals hooksdir (the git-resolved
# hooks dir) and hookcwd (the payload cwd); returns 0 when gated.
# ---------------------------------------------------------------------------
_hook_gated() {
  local entry="$1" n="${1##*/}" f d text tok cand prev
  local -a queue=("$entry")
  local seen="" idx=0 total=1
  # Bound the walk: a shim chain is short; a pathological script must not
  # turn the guard into a file crawler.
  while [ "$idx" -lt "$total" ] && [ "$idx" -lt 16 ]; do
    f="${queue[$idx]}"
    idx=$((idx + 1))
    [ -f "$f" ] || continue
    case " $seen " in *" $f "*) continue ;; esac
    seen="$seen $f"
    grep -q "review-gate" "$f" 2>/dev/null && return 0
    # BSD-safe: dirname takes no `--` on macOS (scripts/ convention: quote the
    # operand instead of end-of-options).
    d="$(dirname "$f")"
    text="$(<"$f")"
    # Resolve the $0-relative dirname/basename idioms to literal paths so the
    # scripts a shim sources/execs become findable tokens (husky 9 writes
    # exactly these shapes: . "$(dirname "$0")/h" and, in h,
    # n=$(basename "$0") / s=$(dirname "$(dirname "$0")")/$n).
    for prev in 1 2 3; do
      tok="$text"
      text="${text//'$(dirname -- "$0")'/$d}"
      text="${text//'$(dirname "$0")'/$d}"
      text="${text//'$(basename -- "$0")'/$n}"
      text="${text//'$(basename "$0")'/$n}"
      text="${text//'${0%/*}'/$d}"
      [ "$text" = "$tok" ] && break
    done
    # Husky 9 layout: the shim dir is named "_" and the real user hook is its
    # SIBLING — h runs "$(dirname "$(dirname "$0")")/$n", whose $n stays
    # symbolic here, so enqueue the sibling hook directly.
    if [ "${d##*/}" = "_" ]; then
      queue+=("$d/../$n")
      total=$((total + 1))
    fi
    # Every path-looking token is a candidate referenced script; try it
    # against the shim's own dir, the hooks dir, and the payload cwd.
    while IFS= read -r tok; do
      [ -n "$tok" ] || continue
      case "$tok" in
        '~'/*) tok="$HOME${tok#'~'}" ;;
      esac
      case "$tok" in
        /*) [ -f "$tok" ] && { queue+=("$tok"); total=$((total + 1)); } ;;
        *)
          for cand in "$d/$tok" "${hooksdir:-$d}/$tok" "${hookcwd:-$d}/$tok"; do
            [ -f "$cand" ] && { queue+=("$cand"); total=$((total + 1)); break; }
          done
          ;;
      esac
    done < <(printf '%s\n' "$text" | grep -oE '[A-Za-z0-9_./~+-]+' | sort -u)
  done
  return 1
}

# ---------------------------------------------------------------------------
# Heredoc stripping (port of _strip_heredocs): a heredoc BODY is data, never
# code. Only strips when a terminator is found, matching upstream.
# ---------------------------------------------------------------------------
strip_heredocs() {
  local in="$1" out="" marker found rest
  [ "${in#*"<<"}" = "$in" ] && { REPLY="$in"; return 0; }
  local -a lines=() openers=()
  local _line
  while IFS= read -r _line; do lines+=("$_line"); done <<< "$in"
  local i=0 m=${#lines[@]}
  while [ "$i" -lt "$m" ]; do
    local line="${lines[i]}"
    out+="$line"$'\n'
    i=$((i + 1))
    # Collect EVERY opener on this line, left to right: `cat <<A <<B` opens
    # two bodies, which arrive in order, each closed by its own terminator.
    # Mirrors the finditer + per-opener loop of _strip_heredocs in
    # scripts/review-gate.py -- matching only the first opener leaves the
    # later bodies in `out`, still scanned for cds/pushes.
    # An opener match is valid only where a redirection operator can legally
    # start: preceded by start-of-line/whitespace/`&`/`|`/`;`/`{` -- i.e. NOT
    # glued onto a word and not inside arithmetic or quoting (`$((n << bits))`,
    # `echo 'a << b shift'`). And, mirroring _strip_heredocs, the marker must
    # END its line bar a redirection or pipe/separator. A spurious marker is
    # doubly dangerous in the loop below: when it has no terminator line, i
    # runs to the end and the ENTIRE remainder of the command would silently
    # drop out of `out` (never scanned for cds/pushes).
    local _hd_re _tl_re _after _cand
    _hd_re='(^|[^A-Za-z0-9_$()'"'"'"=<])<<-?[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)'
    # Bash ERE has no lookahead: the 'ends its line bar a redirection or
    # pipe/separator' trailer is checked against the text AFTER the consumed
    # match, so a second opener's `<<` is never eaten mid-line.
    _tl_re='^[[:space:]]*[0-9]*[><|&;]'
    openers=()
    rest="$line"
    while [[ "$rest" =~ $_hd_re ]]; do
      _cand="${BASH_REMATCH[2]}"
      _after="${rest#*"${BASH_REMATCH[0]}"}"
      if [ -z "${_after//[[:space:]]/}" ] || [[ "$_after" =~ $_tl_re ]]; then
        openers+=("$_cand")
      fi
      rest="$_after"
    done
    [ "${#openers[@]}" -eq 0 ] && continue
    for marker in ${openers[@]+"${openers[@]}"}; do
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
    done
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
# separator, redirections dropped. PUSH_PLUMBING=1 for `git send-pack`:
# a push by plumbing git never runs pre-push for (A1).
#
# `bash -c '<script>'` / `eval '<script>'` execute their argument, so the
# script text is re-classified recursively (depth-capped): a push hidden in a
# quoted argument is still a push (A2 — proven live: `bash -c "git push ..."`
# used to pass the raw-text prefilter and the classifier alike).
# ---------------------------------------------------------------------------
PUSH_FOUND=0
PUSH_COUNT=0
PUSH_WORDS=""
PUSH_PLUMBING=0

_INTERPRETERS='bash sh zsh dash ksh ash'

classify_command() {
  local cmd="$1" depth="${2:-0}"
  strip_heredocs "$cmd"
  local code="$REPLY"
  local -a words=()
  local word="" i=0 n=${#code} ch quote="" word_started=0
  PUSH_FOUND=0
  PUSH_COUNT=0
  PUSH_WORDS=""
  PUSH_PLUMBING=0
  while [ "$i" -lt "$n" ]; do
    ch="${code:i:1}"
    if [ -n "$quote" ]; then
      if [ "$ch" = '\' ] && [ "$quote" = '"' ]; then
        word+="${code:i:2}"; i=$((i + 2)); continue
      fi
      if [ "$ch" = "$quote" ]; then
        # Do NOT flush here: a closing quote does not end the word in shell
        # (`git pu""sh` is the single word `push`). The word flushes only on
        # whitespace, a separator, or end of input. Flushing here would
        # tokenize `git pu""sh origin main` as `git pu sh ...` -- no push
        # found -- and fail OPEN (proven live).
        quote=""
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
          # Shell semantics: a comment ENDS AT THE NEWLINE. Skip to it instead
          # of ending the scan -- breaking here truncates a multi-line call at
          # the '#', so `git status # note\ngit push origin main' would never
          # classify (and gate) the push on the following line.
          while [ "$i" -lt "$n" ] && [ "${code:i:1}" != $'\n' ]; do
            i=$((i + 1))
          done
          continue   # the newline itself is handled as a separator next turn
        fi
        word+='#'; i=$((i + 1)); continue ;;
      *)
        word+="$ch"; word_started=1; i=$((i + 1)); continue ;;
    esac
  done
  [ -n "$word" ] && words+=("$word")

  # Interpreters that execute a quoted argument (A2): `bash -c '...'`,
  # `sh -c '...'`, `eval '...'`. The tokenizer already decoded the quotes
  # into the word's content, so re-classify that content directly.
  local m=${#words[@]}
  if [ "$depth" -lt 3 ]; then
    local j=0 w2
    while [ "$j" -lt "$m" ]; do
      w2="${words[j]}"
      local recurse=""
      # NOTE: membership tests here must use $w2 as the case SUBJECT -- the
      # old `case " $_INTERPRETERS " in ... eval)' had eval as a PATTERN
      # against the interpreter LIST, so the eval arm was unreachable and
      # every eval form (even the quoted one) slipped past the recursion.
      if [ "$w2" = "eval" ]; then
        if [ "$((j + 1))" -lt "$m" ]; then
          # eval joins ALL of its arguments with spaces and runs the result,
          # so re-classify the whole rest of the command (up to the next
          # separator), not just the next word: `eval git push origin main'
          # tokenizes as four words and only re-reading `git' would miss the
          # push. The quoted single-argument form lands here as one word and
          # is unchanged. j then resumes at the separator, so the same words
          # are not re-scanned as a second interpreter invocation.
          local jj=$((j + 1)) joined=""
          while [ "$jj" -lt "$m" ] && [ "${words[jj]}" != ";" ]; do
            joined+="${words[jj]} "
            jj=$((jj + 1))
          done
          recurse="${joined% }"
          j=$((jj - 1))   # the loop's own j+1 lands back on the separator
        fi
      else
        case " $_INTERPRETERS " in
          *" $w2 "*)
            if [ "$((j + 2))" -lt "$m" ] && [ "${words[j+1]}" = "-c" ]; then
              recurse="${words[j+2]}"
            fi ;;
        esac
      fi
      if [ -n "$recurse" ]; then
        local sf=$PUSH_FOUND sc=$PUSH_COUNT sw="$PUSH_WORDS" sp=$PUSH_PLUMBING
        classify_command "$recurse" $((depth + 1))
        PUSH_COUNT=$((sc + PUSH_COUNT))
        [ "$PUSH_FOUND" = 1 ] || PUSH_FOUND=$sf
        [ "$PUSH_PLUMBING" = 1 ] || PUSH_PLUMBING=$sp
        [ -n "$PUSH_WORDS" ] || PUSH_WORDS="$sw"
      fi
      j=$((j + 1))
    done
  fi

  # Walk the word list looking for `git ... push` at command positions.
  local k=0 m2=$m w found_at=-1
  while [ "$k" -lt "$m2" ]; do
    # skip `env` (and its assignments/options) and NAME=VALUE prefixes at a
    # command position: `env OCR_IN_REVIEW=1 git push ...` is still a push.
    while [ "$k" -lt "$m2" ]; do
      w="${words[k]}"
      if [ "$w" = "env" ] || [[ "$w" == -i ]] || [ "$w" = "--ignore-environment" ] \
         || [[ "$w" == --unset=* ]] || [[ "$w" == -u ]]; then
        if [ "$w" = "-u" ]; then k=$((k + 2)); else k=$((k + 1)); fi
        continue
      fi
      if [[ "$w" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then k=$((k + 1)); else break; fi
    done
    if [ "$k" -lt "$m2" ] && [ "${words[k]}" = "git" ]; then
      k=$((k + 1))
      # git global options
      while [ "$k" -lt "$m2" ]; do
        w="${words[k]}"
        [ "$w" = ";" ] && break
        [ "$w" = "--" ] && { k=$((k + 1)); break; }
        if [ "$w" = "-C" ] || [ "$w" = "-c" ]; then
          k=$((k + 2)); continue
        fi
        [[ "$w" == -* ]] && { k=$((k + 1)); continue; }
        break
      done
      if [ "$k" -lt "$m2" ]; then
        # A1: `git send-pack` is push transport git runs NO pre-push hook
        # for — positively parsed as plumbing, never as a non-push.
        if [ "${words[k]}" = "send-pack" ]; then
          PUSH_PLUMBING=1
        elif [ "${words[k]}" = "push" ]; then
          PUSH_COUNT=$((PUSH_COUNT + 1))
          found_at="$k"
        fi
      fi
    fi
    # advance to the next separator
    while [ "$k" -lt "$m2" ] && [ "${words[k]}" != ";" ]; do k=$((k + 1)); done
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

# Read the command (Kimchi puts it under input.command; the re-wrapped
# tool_input.command shape from the git adapter is accepted too).
cmd=""
json_get "$payload" "command" && cmd="$REPLY"
[ -z "$cmd" ] && _pass

# Prefilter (A2): OVER-approximate. The raw text merely has to contain `git`
# for the strict classifier to run; the decision is NEVER made on the raw
# text. The old `*git*push*` literal under-matched quoted / `bash -c` /
# spliced-subcommand shapes (proven live: `git pu""sh origin main`,
# `bash -c 'git push ...'` both slipped past it). Never under-match.
case "$cmd" in
  *git*) ;;
  *) _pass ;;
esac

classify_command "$cmd"
[ "$PUSH_COUNT" -gt 1 ] && { _fail_open_requested && _pass; _emit_block "$BLOCK_MULTI"; }
# A1: `git send-pack` never reaches git's pre-push hook. Block it outright.
[ "$PUSH_PLUMBING" = 1 ] && { _fail_open_requested && _pass; _emit_block "$BLOCK_SENDPACK"; }

# Inside the review session: the gate's own worker must not re-gate itself —
# file-writing shapes inside a review go to review-gate.py --mode guard. But
# OCR_IN_REVIEW is a SPOOFABLE env var (any gated command can export it), so
# it NEVER disables the push gate (A2): a positively-parsed push falls
# through to the same repo guard and reviewer as outside the review.
if [ "${OCR_IN_REVIEW:-}" = "1" ] && [ "$PUSH_FOUND" != 1 ]; then
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

[ "$PUSH_FOUND" = 1 ] || _pass

# --- from here on the command performs exactly one real `git push` ----------

# Parse inversion (D14): positively-parsed shapes go on; push-shaped but
# unparseable shapes block with the retry message.
# push_shape returns 1/2 as DECISIONS, not failures -- capture the status
# explicitly so `set -e` cannot read a deliberate unparseable/dry-run result
# as an abort (which would exit with no output = ALLOW = unreviewed push).
shape_rc=0
push_shape "$PUSH_WORDS" || shape_rc=$?
[ "$shape_rc" = 1 ] && { _fail_open_requested && _pass; _emit_block "$BLOCK_UNPARSEABLE"; }
[ "$shape_rc" = 2 ] && _pass                       # dry-run / delete: nothing to review

# Repository guard: this hook is INERT outside repositories that installed the
# gate (i.e. whose pre-push hook chain reaches review-gate). Resolves the
# payload cwd, not the process cwd. The hook location comes from git's own
# resolution (rev-parse --git-path hooks), which honours core.hooksPath and
# the linked-worktree common dir — the old <gitdir>/hooks/pre-push check
# never fired from a linked worktree. The effective chain is followed too:
# a gate behind a husky shim counts as gated.
json_get "$payload" "cwd" && hookcwd="$REPLY" || hookcwd=""
[ -z "$hookcwd" ] && hookcwd="$PWD"
hooksdir="$(git -C "$hookcwd" rev-parse --git-path hooks 2>/dev/null)" || _pass
[ -n "$hooksdir" ] || _pass
case "$hooksdir" in
  /*) ;;
  *) hooksdir="$hookcwd/$hooksdir" ;;
esac
_hook_gated "$hooksdir/pre-push" || _pass          # not a gated repository

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
