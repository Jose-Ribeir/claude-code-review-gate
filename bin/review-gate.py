#!/usr/bin/env python3
#
# review-gate — commit gate core.
#
# Runs the review skill headlessly via the official `claude` CLI (the compliant,
# subscription-friendly path — no token leaves Claude Code, no third-party tool),
# parses the JSON verdict, and converts it into either:
#   --mode hook : a Claude Code PreToolUse permissionDecision (deny/allow) on stdout
#   --mode git  : a process exit code (1 = block, 0 = allow)
#
# Failure policy: FAIL CLOSED on timeout, subprocess error, or unparseable
# output.  Only "claude not found" still fails open — there is no sensible gate
# when the tool is not installed.  Use OCR_FAIL_OPEN=1 for an emergency
# one-shot bypass; use OCR_ADVISORY=1 to downgrade permanently to warn-only.
# Raise OCR_TIMEOUT (default 600 s) if legitimate reviews routinely time out.
#
# The orchestrated review methodology this drives is adapted from open-code-review
# (ocr): https://github.com/alibaba/open-code-review (Apache-2.0). See NOTICE.
import json
import os
import shutil
import subprocess
import sys
import shlex
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_verdict import compute_verdict  # noqa: E402

PROMPT = "/review-gate:review --unpushed --json"

# The plugin's own root (bin/.. == the plugin dir). Passed explicitly so the
# review skill still resolves when we skip user settings below.
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Model for the headless review session. Pinned deliberately: without --model
# the spawned session inherits whatever model the PARENT Claude Code session is
# on, so a user on Opus pays Opus cache-read rates ($0.50/M) for every gate run
# -- ~5x Haiku and ~1.7x Sonnet, on a workload that re-reads its whole context
# on every tool call. Sonnet is the default because review quality matters (a
# gate that emits false positives gets bypassed, and a bypassed gate has zero
# recall); set OCR_MODEL=haiku to trade some precision for cost, or =opus if you
# want maximum depth and accept the bill.
_MODEL = os.environ.get("OCR_MODEL", "sonnet").strip() or "sonnet"

DEFAULT_CLAUDE_ARGS = [
    "--allowedTools", "Bash Read Grep Glob Task",
    # Pin the model rather than inheriting the parent session's (see above).
    "--model", _MODEL,
    # Load ONLY project settings. The user's ~/.claude/settings.json is where
    # global hooks live; in a headless review session those fire on every tool
    # call (each one a subprocess, and any that inject context add tokens to a
    # context that is already re-read on every call). Skipping user settings
    # also skips the plugin registry, hence --plugin-dir below. Auth is
    # unaffected -- OAuth/keychain is not a settings source.
    "--setting-sources", "project",
    # Load the review plugin from disk. Required because --setting-sources
    # above drops the user-level enabledPlugins registry.
    "--plugin-dir", _PLUGIN_ROOT,
    # No MCP servers. --mcp-config is given an empty object so there is nothing
    # to load; --strict-mcp-config makes that authoritative and ignores every
    # other MCP configuration. A code review needs Bash/Read/Grep/Glob and
    # nothing else, and each connected server's tool schemas cost context.
    "--mcp-config", '{"mcpServers":{}}',
    "--strict-mcp-config",
    # Move per-machine sections (cwd, env, git status) out of the system prompt
    # so the cached prefix stays stable across runs.
    "--exclude-dynamic-system-prompt-sections",
]
try:
    TIMEOUT = int(os.environ.get("OCR_TIMEOUT", "1800"))
    if TIMEOUT <= 0:
        TIMEOUT = 1800
except ValueError:
    TIMEOUT = 1800
MARKER_TTL = 3600  # seconds


class ReviewGateError(Exception):
    """Raised to fail the gate closed (timeout, subprocess crash, parse error).

    Only 'claude not found' is allowed to remain fail-open.
    """


def _warn(msg):
    sys.stderr.write("[review-gate] " + msg + "\n")


def _git(args, cwd=None):
    try:
        out = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=30
        )
        return out.stdout.strip(), out.returncode
    except Exception:
        return "", 1


def _repo_root():
    out, rc = _git(["rev-parse", "--show-toplevel"])
    return out if rc == 0 and out else os.getcwd()


def _git_dir():
    out, rc = _git(["rev-parse", "--git-dir"])
    return out if rc == 0 and out else ".git"


def _head_sha():
    out, rc = _git(["rev-parse", "HEAD"])
    return out if rc == 0 and out else ""


def _has_unpushed_commits():
    for ref in ("@{u}", "origin/main", "origin/master", "origin/HEAD"):
        out, rc = _git(["log", f"{ref}..HEAD", "--oneline"])
        if rc == 0:
            return bool(out.strip())
    return True  # unknown -> let the reviewer decide


def _is_advisory(repo_root):
    if os.environ.get("OCR_ADVISORY", "").strip().lower() in ("1", "true", "yes"):
        return True
    for name in (".ocr/config.json", ".ocr/config"):
        p = Path(repo_root) / name
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8")
            if name.endswith(".json"):
                if json.loads(txt).get("blocking") is False:
                    return True
            elif "blocking" in txt and "false" in txt.lower():
                return True
        except Exception:
            continue
    return False


def _marker_path(git_dir, head_sha):
    return Path(git_dir) / f"scr-push-reviewed-{head_sha}"


def _marker_fresh(path):
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) < MARKER_TTL
    except Exception:
        return False


def _extract_json(text):
    """Pull the review JSON object out of claude's stdout. Returns dict or None."""
    if not text:
        return None
    text = text.strip()
    # 1) whole thing
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2) fenced ```json ... ``` block (last one)
    import re

    blocks = re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    for b in reversed(blocks):
        try:
            return json.loads(b)
        except Exception:
            continue
    # 3) widest brace span
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return None


def _find_claude():
    """Locate the claude CLI so the gate works from any shell, not just inside
    Claude Code. Order: PATH; then OCR_CLAUDE_BIN / CLAUDE_CODE_EXECPATH; then,
    on Windows, the Desktop App's bundled claude.exe (a versioned path that is
    not on PATH) — newest version wins."""
    found = shutil.which("claude")
    if found:
        return found
    for env in ("OCR_CLAUDE_BIN", "CLAUDE_CODE_EXECPATH"):
        exe = os.environ.get(env, "")
        if exe and os.path.isfile(exe):
            return exe
    import glob

    roots = [p for p in (os.environ.get("LOCALAPPDATA"), os.path.join(os.path.expanduser("~"), "AppData", "Local")) if p]
    cands = []
    for root in roots:
        cands += glob.glob(os.path.join(root, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "claude.exe"))
    if cands:
        cands.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        return cands[0]
    return None


def _hook_timeout_budget():
    """Return hooks/hooks.json's PreToolUse 'timeout' (int seconds), or None if
    it can't be read/parsed. Never raises -- this is advisory message text, not
    gate logic, so a missing/malformed file must not crash the review."""
    try:
        path = Path(os.path.dirname(os.path.abspath(__file__))) / ".." / "hooks" / "hooks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"])
    except Exception:
        return None


def _bypass_hint(mode):
    """Emergency-bypass instruction text, mode-aware.

    OCR_FAIL_OPEN is read from this process's own os.environ (line ~330). In
    --mode hook this process inherits Claude Code's own launch environment, NOT
    the shell environment of the `git commit` Bash tool call that triggered the
    PreToolUse hook -- so an inline `OCR_FAIL_OPEN=1 git commit ...` prefix is a
    no-op in hook mode: the gate denies again and the printed bypass appears to
    not work, with no indication why.
    """
    if mode == "hook":
        return (
            "  Emergency one-shot bypass : export OCR_FAIL_OPEN=1 in the environment\n"
            "    Claude Code itself is launched from (a shell prefix on `git commit` will\n"
            "    NOT work in hook mode -- this process inherits Claude Code's env, not the\n"
            "    Bash tool call's)."
        )
    return "  Emergency one-shot bypass : OCR_FAIL_OPEN=1 git commit ..."


def _downgrade_hint(mode):
    """Permanent block->advisory downgrade instruction text, mode-aware.

    OCR_ADVISORY has the identical env-inheritance limitation as OCR_FAIL_OPEN
    (see _bypass_hint) -- an inline prefix on `git commit` never reaches this
    process in hook mode. But this message is `permissionDecisionReason`, which
    Claude Code surfaces back to the SAME agent whose commit just got blocked
    -- unlike a human at a terminal reading a git CLI error, that agent has a
    Write tool. A file-based alternative (.ocr/config.json {"blocking": false})
    is therefore not a neutral workaround here: it reads as a self-service,
    persistent, repo-wide kill switch the blocked agent could just write and
    commit. Keep the session-scoped env-var route as the primary hint (it
    still requires a human to re-launch Claude Code with the var set -- not
    something the agent can do to itself mid-session) and only mention the
    file route with an explicit human-only/persistent/repo-wide label.
    """
    if mode == "hook":
        return (
            '  Downgrade to advisory (warn-only) : export OCR_ADVISORY=1 in the environment\n'
            "    Claude Code itself is launched from (a shell prefix on `git commit` will NOT\n"
            "    work in hook mode -- this process inherits Claude Code's env, not the Bash\n"
            '    tool call\'s). A human can also add (or create) {"blocking": false} in\n'
            "    .ocr/config.json, but that is a PERSISTENT, REPO-WIDE change committed into\n"
            "    the repo -- a human decision, not something to self-apply from inside a\n"
            "    blocked session."
        )
    return "Downgrade to advisory (warn-only): OCR_ADVISORY=1 git commit ..."


def _run_review(repo_root, mode):
    """Return (result_dict, True) on success.

    Raises ReviewGateError on timeout, subprocess error, or unparseable output
    so that main() can fail the gate closed.  Only 'claude not found' still
    returns (None, False) to allow the commit — there is no gate without the
    tool.
    """
    claude = _find_claude()
    if not claude:
        _warn("`claude` CLI not found on PATH or CLAUDE_CODE_EXECPATH — skipping review (fail-open).")
        return None, False
    # OCR_CLAUDE_ARGS replaces the defaults wholesale (full escape hatch, also
    # discards the cost controls); OCR_CLAUDE_EXTRA_ARGS appends to them, which
    # is what callers usually want -- the git-hook adapter uses it to add
    # --dangerously-skip-permissions without losing the pinned model.
    override = os.environ.get("OCR_CLAUDE_ARGS")
    if override:
        args = shlex.split(override)
    else:
        args = list(DEFAULT_CLAUDE_ARGS)
        args += shlex.split(os.environ.get("OCR_CLAUDE_EXTRA_ARGS", ""))
    bypass = _bypass_hint(mode)
    try:
        proc = subprocess.run(
            [claude, "-p", PROMPT] + args,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        if mode == "hook":
            # gate-hook.sh execs this script with no argv/stdin parsing of OCR_TIMEOUT
            # (see gate-hook.sh) -- it only ever sees whatever environment Claude Code's
            # own PreToolUse hook launcher was started with, NOT the shell env of the
            # `git commit` Bash tool call. An inline `OCR_TIMEOUT=<n> git commit ...`
            # prefix therefore never reaches this process in hook mode.
            budget = _hook_timeout_budget()
            budget_str = f"currently {budget}s" if budget is not None else "see hooks/hooks.json"
            escalation = (
                f"  Give Claude more time : export OCR_TIMEOUT={TIMEOUT * 2} in the environment\n"
                f"    Claude Code itself is launched from (a shell prefix on `git commit` will\n"
                f"    NOT work in hook mode -- this process inherits Claude Code's env, not the\n"
                f"    Bash tool call's). Also raise hooks/hooks.json's PreToolUse 'timeout'\n"
                f"    ({budget_str}) to stay above the new OCR_TIMEOUT -- Claude Code kills\n"
                f"    this hook at that fixed harness deadline regardless of OCR_TIMEOUT, which\n"
                f"    silently reopens the fail-open path this gate exists to close."
            )
        else:
            escalation = f"  Give Claude more time : OCR_TIMEOUT={TIMEOUT * 2} git commit ..."
        raise ReviewGateError(
            f"review timed out after {TIMEOUT}s — blocking commit to preserve gate integrity.\n"
            f"{escalation}\n"
            f"{bypass}"
        )
    except Exception as exc:
        raise ReviewGateError(
            f"review process error ({exc}) — blocking commit to preserve gate integrity.\n"
            f"{bypass}"
        )
    result = _extract_json(proc.stdout)
    if result is None:
        raise ReviewGateError(
            "could not parse review output — blocking commit to preserve gate integrity.\n"
            f"  Claude stdout (first 400 chars): {proc.stdout[:400]!r}\n"
            f"{bypass}"
        )
    return result, True


def _format_reasons(result, limit=20):
    lines = []
    for f in result.get("findings", []) if isinstance(result, dict) else []:
        sev = str(f.get("severity", "?"))
        path = f.get("path", "?")
        s, e = f.get("start_line", "?"), f.get("end_line", "?")
        loc = f"{path}:{s}" if s == e else f"{path}:{s}-{e}"
        lines.append(f"  [{sev}] {loc} - {f.get('content','').strip()}")
    return "\n".join(lines[:limit])


def _emit_hook(decision, reason=""):
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    sys.stdout.write(json.dumps(out))
    sys.exit(0)  # hook itself always exits 0; the decision is in the payload


def _fail_closed(mode, msg):
    """Block the commit (or deny the hook) with a clear message.

    Called for ReviewGateError AND for unhandled exceptions anywhere in main()
    so that any internal crash fails closed rather than open.
    """
    if mode == "hook":
        _emit_hook("deny", msg)  # exits 0; decision is in payload
    else:
        _warn(msg)
        sys.exit(1)


def main(argv):
    # Mode is parsed INSIDE the safety net so an IndexError from a malformed
    # '--mode' flag (e.g. '--mode' with no value) is also caught and fails
    # closed rather than crashing without emitting a deny payload.
    mode = "git"  # safe default for the except clause below

    # Top-level safety net: any unhandled exception in main() fails closed.
    # Without this, a crash in compute_verdict(), _format_reasons(), or any
    # other helper exits the process with a non-zero code WITHOUT emitting a
    # deny payload — in hook mode Claude Code would treat that as a non-blocking
    # error and let the commit through, defeating the fail-closed policy.
    try:
        if "--mode" in argv:
            mode = argv[argv.index("--mode") + 1]
        _main_inner(argv, mode)
    except SystemExit:
        raise  # propagate intentional exits (allow/deny both use sys.exit)
    except Exception as exc:  # noqa: BLE001
        _fail_closed(mode, f"review-gate internal error ({type(exc).__name__}: {exc}) — blocking commit.")


def _main_inner(argv, mode):
    # Hook mode: consume the PreToolUse payload on stdin (and only gate pushes).
    if mode == "hook":
        try:
            payload = json.load(sys.stdin)
            cmd = (payload.get("tool_input") or {}).get("command", "")
            if "git push" not in cmd:
                _emit_hook("allow")
        except Exception:
            pass  # if we can't read it, fall through and review anyway

    repo_root = _repo_root()
    allow = (lambda: _emit_hook("allow")) if mode == "hook" else (lambda: sys.exit(0))

    if not _has_unpushed_commits():
        allow()

    git_dir = _git_dir()
    head_sha = _head_sha()
    marker = _marker_path(git_dir, head_sha) if head_sha else None

    # The other adapter already reviewed this exact staged tree and passed it.
    if marker and _marker_fresh(marker):
        allow()

    try:
        result, ran = _run_review(repo_root, mode)
    except ReviewGateError as exc:
        # Fail closed: block the commit unless OCR_FAIL_OPEN=1 is set.
        if os.environ.get("OCR_FAIL_OPEN", "").strip().lower() in ("1", "true", "yes"):
            _warn(
                f"OCR_FAIL_OPEN=1 set — bypassing fail-closed gate. Reason:\n  {exc}\n"
                "[!] This bypass should be used sparingly and intentionally."
            )
            allow()
            return  # unreachable; allow() calls sys.exit / _emit_hook
        _fail_closed(mode, str(exc))
        return  # unreachable

    if not ran:
        allow()  # fail-open: only reaches here when claude is not installed

    verdict = compute_verdict(result)
    advisory = _is_advisory(repo_root)
    reasons = _format_reasons(result)

    if verdict == "block" and not advisory:
        reason = "review-gate blocked this commit (high-severity issues):\n" + (
            reasons or "  (see review output)"
        ) + f"\n\nFix the issues above, then commit again.\n{_downgrade_hint(mode)}"
        _fail_closed(mode, reason)
        return  # unreachable

    # Passed (or advisory): record marker so the paired adapter can skip, print
    # any advisory findings, and allow.
    if marker:
        try:
            marker.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass
    if reasons:
        label = "advisory (blocking disabled)" if advisory else f"verdict: {verdict}"
        _warn(f"{label} — findings:\n{reasons}")
    allow()


if __name__ == "__main__":
    main(sys.argv)
