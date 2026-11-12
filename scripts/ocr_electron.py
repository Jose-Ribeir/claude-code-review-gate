#!/usr/bin/env python3
#
# Deterministic Electron checker (v1, regex-only) for review-gate.
#
# Electron analysis Item C (.kimchi/docs/electron-ts-support-analysis.md):
# given the push range A..B, build an IPC-channel index over the whole repo
# at B and emit deterministic, no-model findings the gate injects as
# `impact`/`known_defects` hints for the reviewer:
#
#   1. orphan renderer channels — ipcRenderer.send/invoke/sendSync("ch")
#      with no ipcMain.on/handle/once/handleOnce("ch") anywhere, including
#      the case where this very push removed the handler.
#   2. ipcMain handlers with no sender validation in the callback body
#      (heuristic: no senderFrame/event.sender/validateSender-style token
#      between the registration's braces).
#   3. webPreferences anti-patterns newly added in the diff
#      (nodeIntegration: true, contextIsolation: false, webSecurity: false,
#      nodeIntegrationIn*: true, sandbox: false).
#   4. stale export references (rename-broken callers) — an exported symbol
#      removed by the push, not re-exported anywhere at B, still referenced
#      by another file: a type-level break the reviewer's LSP confirms.
#
# A test-file allowlist (tests/, fixtures/, __tests__/, __mocks__/,
# *.test.*, *.spec.*, node_modules/) keeps the checker from indexing or
# flagging test code, so test doubles of ipcMain/ipcRenderer cannot self-FP.
#
# Safety: everything read comes from the untrusted branch under review. git
# is always invoked with a list argv (never through a shell) and GIT_TRACE*
# is scrubbed from its environment; every read is capped; any failure
# degrades to an empty result plus a warning instead of raising.
#
# CLI:
#   python3 scripts/ocr_electron.py --range <A>..<B> [repo]
# Prints one JSON object on stdout:
#   {"channels": {...}, "findings": [{id,severity,file,line,kind,detail}],
#    "known_defects": [...], "changed_files": [...], "warnings": [...]}
# Exit code: 0 whether or not there are findings (the gate decides), 1 on
# bad usage / unresolvable range.
#
import argparse
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple

# --- limits: the branch under review is untrusted, cap everything ---------
MAX_FILE_CHARS = 1_000_000
MAX_FILES = 5_000
MAX_HANDLER_BODY_CHARS = 20_000
MAX_FINDINGS = 200
MAX_CHANNELS = 2_000
GIT_TIMEOUT = 60

JS_TS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# --- test-file allowlist (plan-validation report fix #8: no self-FPs) -----
_TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|__mocks__|fixtures?)(/|$)")
_TEST_NAME_RE = re.compile(r"\.(test|spec)\.[a-z]+$")
_VENDOR_DIR_RE = re.compile(r"(^|/)node_modules(/|$)")


def is_skipped(path: str) -> bool:
    """True for test/fixture paths the checker must neither index nor flag."""
    p = path.replace("\\", "/")
    return bool(
        _TEST_DIR_RE.search(p)
        or _TEST_NAME_RE.search(p)
        or _VENDOR_DIR_RE.search(p)
    )


# --- channel registrations / renderer calls (string-literal channels only)
MAIN_API_RE = re.compile(
    r"ipcMain\s*\.\s*(on|once|handle|handleOnce)\s*\(\s*(['\"])([^'\"\n]+)\2"
)
RENDER_API_RE = re.compile(
    r"ipcRenderer\s*\.\s*(send|sendSync|invoke)\s*\(\s*(['\"])([^'\"\n]+)\2"
)

# --- webPreferences anti-patterns: (rule, regex, severity, message) --------
WEBPREF_RULES = [
    ("nodeIntegration", re.compile(r"nodeIntegration\s*:\s*true"), "high",
     "nodeIntegration: true gives the renderer full Node access"),
    ("contextIsolation", re.compile(r"contextIsolation\s*:\s*false"), "high",
     "contextIsolation: false disables the preload isolation boundary"),
    ("webSecurity", re.compile(r"webSecurity\s*:\s*false"), "high",
     "webSecurity: false disables the same-origin policy"),
    ("nodeIntegrationInWorker", re.compile(r"nodeIntegrationInWorker\s*:\s*true"), "high",
     "nodeIntegrationInWorker: true exposes Node in worker contexts"),
    ("nodeIntegrationInSubFrames", re.compile(r"nodeIntegrationInSubFrames\s*:\s*true"), "high",
     "nodeIntegrationInSubFrames: true exposes Node in iframes"),
    ("sandbox", re.compile(r"\bsandbox\s*:\s*false"), "medium",
     "sandbox: false widens the renderer process surface"),
]

# --- sender-validation heuristic tokens (electron.md checklist #17) --------
VALIDATION_TOKENS = (
    "senderFrame",      # event.senderFrame (canonical Electron check)
    "event.sender",     # event.sender / event.sender.id
    "sender.frame",
    "event.frame",
    "senderId",
    "validateSender",
    "checkSender",
    "isTrustedSender",
    "trustedSender",
)

# --- exported declarations (rename-broken caller heuristic) ----------------
EXPORT_DECL_RE = re.compile(
    r"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function\s*\*?|const|let|var|class|type|interface|enum)"
    r"\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# ---------------------------------------------------------------------------
# git plumbing (never raises; GIT_TRACE* scrubbed per conftest findings)
# ---------------------------------------------------------------------------

def _run_git(repo: str, args: List[str]) -> Tuple[str, int]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    try:
        result = subprocess.run(
            ["git", "-C", repo] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            env=env,
            # Called from the detached review supervisor, which has no
            # console: without this each git call opens its own window.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout = result.stdout
        if stdout.endswith("\n"):
            stdout = stdout[:-1]
        return stdout, result.returncode
    except Exception:
        return "", 1


def _read_at(repo: str, sha: str, path: str) -> Optional[str]:
    out, rc = _run_git(repo, ["show", "%s:%s" % (sha, path)])
    if rc != 0:
        return None
    return out[:MAX_FILE_CHARS]


def _list_files(repo: str, sha: str) -> List[str]:
    out, rc = _run_git(repo, ["ls-tree", "-r", "--name-only", sha])
    if rc != 0:
        return []
    return [f for f in out.split("\n") if f][:MAX_FILES]


def _diff_names(repo: str, a: str, b: str) -> List[str]:
    out, rc = _run_git(repo, ["diff", "--name-only", "-z", a, b])
    if rc != 0:
        return []
    return [p for p in out.split("\0") if p]


def _diff_map(repo: str, a: str, b: str) -> Tuple[Dict[str, Set[int]], Dict[str, Set[int]], Dict[str, str]]:
    """Added/deleted line numbers per file for the range, via git diff -U0.

    Added line numbers are relative to the file at B and keyed by its
    B-side path; deleted ones are relative to the file at A and keyed by
    its A-side path. Whole-file deletions ('+++ /dev/null') and renames
    therefore keep their hunks instead of dropping out, so checks 3b and
    4 can still read the file at A. The third return value maps the
    B-side path to the A-side path for files both renamed and edited.
    """
    out, rc = _run_git(repo, ["diff", "-U0", "--no-prefix", "--no-color", a, b])
    added: Dict[str, Set[int]] = {}
    deleted: Dict[str, Set[int]] = {}
    renames: Dict[str, str] = {}
    if rc != 0:
        return added, deleted, renames
    old_cur: Optional[str] = None
    new_cur: Optional[str] = None
    in_headers = False  # '--- '/'+++ ' are headers only before the first hunk;
    # content lines can legitimately start with '--- '/'+++ ' too
    for line in out.split("\n"):
        if line.startswith("diff --git"):
            old_cur = new_cur = None
            in_headers = True
        elif in_headers and line.startswith("--- "):
            name = line[4:].split("\t")[0].strip()
            old_cur = None if name == "/dev/null" else name
        elif in_headers and line.startswith("+++ "):
            name = line[4:].split("\t")[0].strip()
            new_cur = None if name == "/dev/null" else name
            in_headers = False
            if new_cur is not None:
                added.setdefault(new_cur, set())
                deleted.setdefault(old_cur or new_cur, set())
                if old_cur is not None and old_cur != new_cur:
                    renames[new_cur] = old_cur
            elif old_cur is not None:
                # Whole-file deletion: keep the A-side path addressable so
                # check 3b can see the removed registrations.
                deleted.setdefault(old_cur, set())
        elif line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                old_start, old_len = int(m.group(1)), int(m.group(2) or "1")
                new_start, new_len = int(m.group(3)), int(m.group(4) or "1")
                if old_len and old_cur is not None:
                    deleted.setdefault(old_cur, set()).update(
                        range(old_start, old_start + old_len))
                if new_len and new_cur is not None:
                    added.setdefault(new_cur, set()).update(
                        range(new_start, new_start + new_len))
    return added, deleted, renames


# ---------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------

def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _ext(path: str) -> str:
    _, ext = os.path.splitext(path)
    return ext.lower()


def _handler_body(text: str, start: int) -> Optional[str]:
    """The callback body for an ipcMain registration starting after the
    channel literal — None when the registration has no inline callback:
    the registration call's closing paren (or a ';') is reached before
    any brace. That matches a bare reference (`ipcMain.on("ch", handler)`)
    in ANY semicolon style — in ASI code there is no `;`, so the scan
    must not roll into the next statement's block — or when the braces
    never balance (v1 counts braces naively; strings and comments
    containing braces can perturb it, at worst widening the body).
    """
    i = start
    limit = min(len(text), start + 400)
    depth = 1  # scanning begins inside the ipcMain.on( ... ) call
    while i < limit:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return None  # the call closed before any brace: bare reference
        elif c == "{":
            depth = 0
            j = i
            end = min(len(text), i + MAX_HANDLER_BODY_CHARS)
            while j < end:
                d = text[j]
                if d == "{":
                    depth += 1
                elif d == "}":
                    depth -= 1
                    if depth == 0:
                        return text[i:j + 1]
                j += 1
            return None
        if c == ";":
            return None
        i += 1
    return None


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def analyze(repo: str, range_str: str) -> Dict:
    warnings: List[str] = []
    result: Dict = {
        "repo": os.path.abspath(repo),
        "range": range_str,
        "a": "",
        "b": "",
        "changed_files": [],
        "channels": {},
        "findings": [],
        "known_defects": [],
        "warnings": warnings,
    }

    # --- resolve the range -------------------------------------------------
    if ".." not in range_str:
        return {"error": "bad range (want A..B): %r" % range_str}
    a_ref, _, b_ref = range_str.partition("..")
    if not a_ref or not b_ref or b_ref.startswith("."):
        return {"error": "bad range (want A..B, not A...B): %r" % range_str}
    a, rc = _run_git(repo, ["rev-parse", "--verify", a_ref + "^{commit}"])
    b, rc2 = _run_git(repo, ["rev-parse", "--verify", b_ref + "^{commit}"])
    if rc != 0 or rc2 != 0:
        return {"error": "cannot resolve range %r in %s" % (range_str, repo)}
    result["a"], result["b"] = a, b

    changed_files = sorted(_diff_names(repo, a, b))
    result["changed_files"] = changed_files
    added, deleted, renames = _diff_map(repo, a, b)
    if not changed_files:
        warnings.append("empty diff for range %s" % range_str)

    # --- read every non-test JS/TS file at B and index the channels --------
    files = [f for f in _list_files(repo, b) if not is_skipped(f)]
    code_files = [f for f in files if _ext(f) in JS_TS_EXTS]
    contents: Dict[str, str] = {}
    for f in code_files:
        text = _read_at(repo, b, f)
        if text is not None:
            contents[f] = text

    channels: Dict[str, Dict[str, List[Dict]]] = {}
    for f, text in contents.items():
        for m in MAIN_API_RE.finditer(text):
            _channel(channels, m.group(3))["main"].append(
                {"file": f, "line": _line_of(text, m.start()),
                 "api": "ipcMain." + m.group(1)})
        for m in RENDER_API_RE.finditer(text):
            _channel(channels, m.group(3))["renderer"].append(
                {"file": f, "line": _line_of(text, m.start()),
                 "api": "ipcRenderer." + m.group(1)})

    findings: List[Dict] = []
    seen_ids: Set[str] = set()

    def add(kind: str, severity: str, file: str, line: int, detail: str) -> None:
        fid = "%s@%s:%d" % (kind, file, line)
        if fid in seen_ids:
            return
        seen_ids.add(fid)
        findings.append({"id": fid, "severity": severity, "file": file,
                         "line": line, "kind": kind, "detail": detail})

    changed_code = [f for f in changed_files
                    if f in contents and not is_skipped(f)
                    and _ext(f) in JS_TS_EXTS]

    # --- 1. webPreferences anti-patterns newly added in the diff -----------
    for f in changed_code:
        text = contents[f]
        for _rule, rx, severity, msg in WEBPREF_RULES:
            for m in rx.finditer(text):
                line = _line_of(text, m.start())
                if line in added.get(f, set()):
                    add("webpref-misuse", severity, f, line,
                        "%s (webPreferences.%s)" % (msg, _rule))

    # --- 2. ipcMain handlers without sender validation ---------------------
    for f in changed_code:
        text = contents[f]
        for m in MAIN_API_RE.finditer(text):
            line = _line_of(text, m.start())
            if line not in added.get(f, set()):
                continue  # pre-existing registration, not part of this push
            body = _handler_body(text, m.end())
            if body is None:
                continue  # bare callback reference: body not visible here
            if any(tok in body for tok in VALIDATION_TOKENS):
                continue
            add("missing-sender-validation", "medium", f, line,
                'ipcMain.%s("%s") callback never checks the sender '
                "(no senderFrame/event.sender/validateSender-style check "
                "in the handler body)" % (m.group(1), m.group(3)))

    # --- 3. orphan renderer channels ---------------------------------------
    # 3a. renderer call added by this push with no handler anywhere.
    for f in changed_code:
        text = contents[f]
        for m in RENDER_API_RE.finditer(text):
            line = _line_of(text, m.start())
            if line not in added.get(f, set()):
                continue
            channel = m.group(3)
            if channels.get(channel, {}).get("main"):
                continue
            add("orphan-channel", "high", f, line,
                'ipcRenderer.%s("%s") has no ipcMain.on/handle registration '
                "anywhere in the repo" % (m.group(1), channel))

    # 3b. handler removed by this push while the renderer still calls it.
    for f, dels in deleted.items():
        if not dels or is_skipped(f) or _ext(f) not in JS_TS_EXTS:
            continue
        old = _read_at(repo, a, f)
        if old is None:
            continue
        for m in MAIN_API_RE.finditer(old):
            if _line_of(old, m.start()) not in dels:
                continue
            channel = m.group(3)
            if channels.get(channel, {}).get("main"):
                continue  # another handler for the channel survived
            for entry in channels.get(channel, {}).get("renderer", []):
                add("orphan-channel", "high", entry["file"], entry["line"],
                    'channel "%s" lost its ipcMain handler in this push '
                    "(removed from %s) but the renderer still calls it"
                    % (channel, f))

    # --- 4. stale export references (rename-broken callers) ----------------
    repo_exports: Set[str] = set()
    for text in contents.values():
        for m in EXPORT_DECL_RE.finditer(text):
            repo_exports.add(m.group(1))

    for f in changed_code:
        old_path = renames.get(f, f)  # A-side path (renames included)
        old = _read_at(repo, a, old_path)
        if old is None:
            continue
        removed_names: Set[str] = set()
        for m in EXPORT_DECL_RE.finditer(old):
            if _line_of(old, m.start()) in deleted.get(old_path, set()):
                removed_names.add(m.group(1))
        added_names: Set[str] = set()
        for m in EXPORT_DECL_RE.finditer(contents[f]):
            if _line_of(contents[f], m.start()) in added.get(f, set()):
                added_names.add(m.group(1))
        for name in sorted(removed_names - added_names):
            if name in repo_exports:
                continue  # still exported somewhere (moved, not removed)
            rx = re.compile(
                r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")
            for other, text in contents.items():
                if other == f:
                    continue
                m = rx.search(text)
                if m:
                    add("stale-export-reference", "medium", other,
                        _line_of(text, m.start()),
                        'export "%s" was removed/renamed in %s by this push,'
                        " but this file still references it (type-level"
                        " break; LSP/tsc confirms)" % (name, f))

    # --- assemble ----------------------------------------------------------
    findings.sort(key=lambda x: (x["file"], x["line"], x["kind"]))
    if len(findings) > MAX_FINDINGS:
        warnings.append("findings capped at %d" % MAX_FINDINGS)
        findings = findings[:MAX_FINDINGS]
    result["findings"] = findings
    result["known_defects"] = [
        "%s [%s] %s" % (f["id"], f["severity"], f["detail"]) for f in findings
    ]

    channels_out: Dict[str, Dict] = {}
    for name in sorted(channels)[:MAX_CHANNELS]:
        entry = channels[name]
        channels_out[name] = {
            "main": sorted(entry["main"], key=lambda e: (e["file"], e["line"])),
            "renderer": sorted(entry["renderer"], key=lambda e: (e["file"], e["line"])),
        }
    result["channels"] = channels_out
    return result


def _channel(channels: Dict[str, Dict[str, List[Dict]]], name: str) -> Dict[str, List[Dict]]:
    if name not in channels:
        channels[name] = {"main": [], "renderer": []}
    return channels[name]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic Electron checker (v1, regex-only) for review-gate.")
    parser.add_argument("--range", dest="range_", required=True,
                        metavar="A..B",
                        help="commit range under review (e.g. origin/main..HEAD)")
    parser.add_argument("repo", nargs="?", default=".",
                        help="path to the git repository (worktree or bare)")
    args = parser.parse_args(argv)

    result = analyze(args.repo, args.range_)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
