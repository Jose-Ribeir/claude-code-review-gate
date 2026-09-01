#!/usr/bin/env bash
#
# Installs the "everywhere" push gate: sets a global core.hooksPath whose
# pre-push hook runs review-gate on EVERY push (terminal, IDE, or Claude Code),
# in every repo. This is OPTIONAL — the plugin already gates pushes made
# through Claude Code without it.
#
# Revert with uninstall-git-hook.sh.
set -euo pipefail

SCR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="${SCR_HOOKS_DIR:-$HOME/.config/review-gate/hooks}"

# --chain-into <repo>: print the snippet that runs the gate from a repo which
# sets its OWN core.hooksPath.
#
# Why this exists: git resolves a repo-local core.hooksPath before the global
# one, so a repo managing its own hooks (husky, lefthook, a hand-rolled
# scripts/git-hooks) silently takes this gate out of the chain -- pushes from a
# plain terminal there are not gated at all, and nothing announces it.
# /review-gate:doctor reports the condition; this prints the cure.
#
# It PRINTS and does not edit. That hook file belongs to the repo and is under
# its version control; a generated edit landing in someone's commit unannounced
# is not a trade this installer gets to make.
if [ "${1:-}" = "--chain-into" ]; then
  target="${2:-}"
  if [ -z "$target" ] || [ ! -d "$target" ]; then
    echo "usage: install-git-hook.sh --chain-into <path-to-repo>" >&2
    exit 2
  fi
  local_hp="$(git -C "$target" config --local --get core.hooksPath || true)"
  # core.hooksPath may be absolute or repo-relative; joining an absolute one
  # onto $target yields nonsense like /repo/C:/repo/hooks.
  case "$local_hp" in
    "")            hookdir="$target/.git/hooks" ;;
    /*|[A-Za-z]:*) hookdir="$local_hp" ;;
    *)             hookdir="$target/$local_hp" ;;
  esac
  hookfile="$hookdir/pre-push"

  if [ -f "$hookfile" ] && grep -q "review-gate.py" "$hookfile" 2>/dev/null; then
    echo "Already chained: $hookfile invokes review-gate.py. Nothing to do."
    exit 0
  fi
  if [ -z "$local_hp" ]; then
    echo "$target does not set a repo-local core.hooksPath, so the global gate"
    echo "already applies there. Nothing to chain."
    exit 0
  fi

  cat <<'SNIPPET'
Add this to the repo's own pre-push, at the point where it has decided to let
the push proceed -- immediately BEFORE its success `exit 0`, not at the end of
the file, since a hook that exits early would never reach an appended block.

Run it after the repo's own checks so a review is only paid for on code that
already passed them.

One thing to wire up by hand: the block forwards the ref updates git gave your
hook on stdin, via `_rg_refs`. It is preset to `$PUSH_REFS`; change that to
whatever variable your hook stored them in. Without them the gate compares HEAD
against its own upstream, which misses a push aimed at a different ref.

    # --- review-gate: AI review of the commits being pushed ---
    # Resolves the reviewer through the pointer the plugin refreshes on every
    # run, so this keeps working across plugin upgrades (the versioned cache
    # path does not). Calls review-gate.py directly rather than the global
    # pre-push wrapper: that wrapper also chains $GITDIR/hooks/pre-push, which
    # in a repo like this one would re-run the checks that just passed.
    #
    # Skipped inside the headless review session, matching the plugin's other
    # two adapters: review-gate.py sets OCR_IN_REVIEW=1 for its child, and
    # without this every push the reviewer makes would spawn a Python process
    # just to be told to stop.
    if [ "${OCR_IN_REVIEW:-}" != "1" ]; then
        _rg_dir=""
        for _p in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/data/*/gate-dir; do
            [ -f "$_p" ] || continue
            _d="$(cat "$_p" 2>/dev/null || true)"
            if [ -n "$_d" ] && [ -f "$_d/review-gate.py" ]; then _rg_dir="$_d"; break; fi
        done
        _rg_py=""
        for _c in python3 python py; do
            _p2="$(command -v "$_c" 2>/dev/null)" || continue
            case "$_p2" in *[Ww]indows[Aa]pps*) continue ;; esac   # Store alias stubs
            if "$_p2" -c "import sys" >/dev/null 2>&1; then _rg_py="$_p2"; break; fi
        done
        if [ -n "$_rg_dir" ] && [ -n "$_rg_py" ]; then
            # Pass the ref updates through: they say what is being sent and where,
        # which is what stops a `branch:main` push from being skipped.
        # git feeds pre-push the ref updates on stdin, and your hook has
        # already consumed them by this point -- that is why this block sits
        # at a success exit. Point _rg_refs at wherever your hook saved them.
        # Leave it empty and the gate falls back to comparing HEAD against its
        # own upstream, which MISSES a push to a different ref
        # (`git push origin mybranch:main`).
        _rg_refs="${PUSH_REFS-}"
        echo "$_rg_refs" | "$_rg_py" "$_rg_dir/review-gate.py" --mode git || exit $?
        else
            # FAIL CLOSED, matching the gate's own pre-push. Skipping here is
            # the one place it would be least visible: this repo's
            # core.hooksPath shadows the global hook, so nothing else reviews
            # these commits.
            case "${OCR_FAIL_OPEN:-}" in
                1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]) : ;;
                *)
                    echo "[review-gate] BLOCKED: no reviewer or no working Python 3, so" >&2
                    echo "  these commits were not reviewed. This repo sets its own" >&2
                    echo "  core.hooksPath, so the global gate does not run here either --" >&2
                    echo "  nothing else catches it. Fix: install Python 3, or re-run" >&2
                    echo "  install-git-hook.sh. Bypass once: OCR_FAIL_OPEN=1 git push" >&2
                    exit 1
                    ;;
            esac
        fi
    fi
    # --- end review-gate ---
SNIPPET
  echo
  echo "Target hook: $hookfile"
  echo "Verify afterwards with /review-gate:doctor -- it should stop reporting"
  echo "the git adapter as shadowed in that repo."
  exit 0
fi

# Stamped into the installed hook purely so /review-gate:doctor can report
# version skew between this copy and the running plugin.
SCR_VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$SCR_DIR/../.claude-plugin/plugin.json" 2>/dev/null | head -1)"
SCR_VERSION="${SCR_VERSION:-unknown}"

mkdir -p "$HOOKS_DIR"

# Canonicalize before comparing: git stores whatever string was set (e.g.
# "C:/Users/me/..." on Windows) while $HOME expands to "/c/Users/me/..." under
# Git Bash. A plain string compare sees those as different directories, so
# re-running the installer would "back up" our own hooks dir over itself and
# uninstall would then restore core.hooksPath to us instead of unsetting it.
_canon() { [ -d "$1" ] && (cd "$1" 2>/dev/null && pwd) || printf '%s' "$1"; }

PREV="$(git config --global --get core.hooksPath || true)"
if [ -n "$PREV" ] && [ "$(_canon "$PREV")" != "$(_canon "$HOOKS_DIR")" ]; then
  git config --global reviewGate.prevHooksPath "$PREV"
  echo "Backed up existing global core.hooksPath: $PREV"
fi

# Versions of this plugin before the commit->push migration installed a
# pre-commit hook here. Left in place it still fires on every commit, and
# because the gate now reviews `@{u}..HEAD` it would review the WRONG state at
# commit time (HEAD is the parent; the staged changes are invisible). Remove
# ours if we recognize it; leave anything unrecognized alone and warn.
STALE="$HOOKS_DIR/pre-commit"
if [ -f "$STALE" ]; then
  if grep -q "review-gate" "$STALE" 2>/dev/null; then
    rm -f "$STALE"
    echo "Removed stale review-gate pre-commit hook (superseded by pre-push)."
  else
    echo "WARNING: $STALE exists and is not ours - leaving it in place."
  fi
fi

# Materialize the pre-push. Both substitutions are ADVISORY ONLY -- the copy
# resolves the reviewer at runtime (see _resolve_gate_dir there). That matters
# because this file is written once and never updated: baking the path in as a
# contract is what made every pre-0.3.0 install silently fail open when the
# plugin moved. The directory is kept as a last-resort hint, and the version is
# only ever read back by /review-gate:doctor.
sed -e "s|__SCR_DIR__|$SCR_DIR|g" -e "s|__SCR_VERSION__|$SCR_VERSION|g" \
  "$SCR_DIR/pre-push" > "$HOOKS_DIR/pre-push"
chmod +x "$HOOKS_DIR/pre-push"

git config --global core.hooksPath "$HOOKS_DIR"

cat <<EOF
Installed the global pre-push gate.
  hooks dir : $HOOKS_DIR
  reviewer  : $SCR_DIR/review-gate.py
  version   : $SCR_VERSION

WARNING: global core.hooksPath now applies to ALL your repositories and
overrides each repo's .git/hooks/pre-push (this hook still RUNS a repo-local
pre-push if one exists, so existing hooks keep working).

Modes:
  default              block confident high-severity findings
  OCR_ADVISORY=1       warn only, never block
  OCR_MODEL=haiku      cheaper review model (default: sonnet)
  git push --no-verify bypass entirely

The gate FAILS CLOSED: a review that times out (OCR_TIMEOUT, default 1800s),
crashes, or returns unparseable output BLOCKS the push rather than letting it
through. So does a missing Python 3 or a reviewer this hook cannot locate.

It still fails OPEN in these cases:
  - 'claude' is not installed        (deliberate: there is no gate without it)
  - OCR_FAIL_OPEN=1 / OCR_ADVISORY=1 (the escape hatches)
and it is BYPASSED entirely by 'git push --no-verify'.

If a broken reviewer ever traps you, OCR_FAIL_OPEN=1 is the one-shot bypass and
OCR_ADVISORY=1 downgrades to warn-only permanently.

Uninstall: $SCR_DIR/uninstall-git-hook.sh
EOF
