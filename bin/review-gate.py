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
# Design rule: FAIL OPEN. If the reviewer can't run (claude missing, timeout,
# unparseable output), allow the commit with a warning. A review tool must never
# become a hard outage on `git commit`.
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

PROMPT = "/review-gate:review --staged --json"
DEFAULT_CLAUDE_ARGS = ["--allowedTools", "Bash Read Grep Glob Task"]
TIMEOUT = int(os.environ.get("OCR_TIMEOUT", "240"))
MARKER_TTL = 3600  # seconds


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


def _staged_tree_hash():
    out, rc = _git(["write-tree"])
    return out if rc == 0 and out else ""


def _has_staged_source():
    out, rc = _git(["diff", "--staged", "--name-only"])
    if rc != 0:
        return True  # unknown -> let the reviewer decide
    return bool(out.strip())


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


def _marker_path(git_dir, tree_hash):
    return Path(git_dir) / f"scr-reviewed-{tree_hash}"


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


def _run_review(repo_root):
    """Return (result_dict_or_None, ran_ok_bool)."""
    claude = _find_claude()
    if not claude:
        _warn("`claude` CLI not found on PATH or CLAUDE_CODE_EXECPATH — skipping review (fail-open).")
        return None, False
    extra = os.environ.get("OCR_CLAUDE_ARGS")
    args = shlex.split(extra) if extra else DEFAULT_CLAUDE_ARGS
    try:
        proc = subprocess.run(
            [claude, "-p", PROMPT] + args,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _warn(f"review timed out after {TIMEOUT}s — allowing commit (fail-open).")
        return None, False
    except Exception as exc:
        _warn(f"could not run review ({exc}) — allowing commit (fail-open).")
        return None, False
    result = _extract_json(proc.stdout)
    if result is None:
        _warn("could not parse review output — allowing commit (fail-open).")
    return result, result is not None


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


def main(argv):
    mode = "git"
    if "--mode" in argv:
        mode = argv[argv.index("--mode") + 1]

    # Hook mode: consume the PreToolUse payload on stdin (and only gate commits).
    if mode == "hook":
        try:
            payload = json.load(sys.stdin)
            cmd = (payload.get("tool_input") or {}).get("command", "")
            if "git commit" not in cmd:
                _emit_hook("allow")
        except Exception:
            pass  # if we can't read it, fall through and review anyway

    repo_root = _repo_root()
    allow = (lambda: _emit_hook("allow")) if mode == "hook" else (lambda: sys.exit(0))

    if not _has_staged_source():
        allow()

    git_dir = _git_dir()
    tree_hash = _staged_tree_hash()
    marker = _marker_path(git_dir, tree_hash) if tree_hash else None

    # The other adapter already reviewed this exact staged tree and passed it.
    if marker and _marker_fresh(marker):
        allow()

    result, ran = _run_review(repo_root)
    if not ran:
        allow()  # fail-open

    verdict = compute_verdict(result)
    advisory = _is_advisory(repo_root)
    reasons = _format_reasons(result)

    if verdict == "block" and not advisory:
        reason = "review-gate blocked this commit (high-severity issues):\n" + (
            reasons or "  (see review output)"
        ) + "\n\nFix the issues, or bypass with: git commit --no-verify"
        if mode == "hook":
            _emit_hook("deny", reason)
        else:
            _warn(reason)
            sys.exit(1)

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
