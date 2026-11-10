"""t08 accept fixture, end to end: dismiss a finding, re-raise the same
defect class on the next push, and the gate demotes it to low severity with
a "previously dismissed" note instead of blocking — while a plain review run
never writes to .ocr/memory.jsonl itself.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
import ocr_memory as om  # noqa: E402
from hook_output import parse_pretooluse  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate_mg", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

SIG = "if user.is_service_account:  # quota check skipped for service accounts"
CONTENT = "quota check is missing for service accounts"
FINDING_SPEC = {"severity": "high", "content": CONTENT, "existing_code": SIG}


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp_path, files):
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    for name, content in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _env(tmp_path, **kw):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OCR_", "STUB_"))}
    env.update({"CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
                "OCR_REVIEWER_CMD": f'"{sys.executable}" "{_STUB}"',
                "STUB_TRACE": str(tmp_path / "stub.trace"),
                "OCR_INLINE_BUDGET": "30",
                "STUB_FINDINGS_FOR": json.dumps({"app/x.py": FINDING_SPEC})})
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _push(work, env):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": "git push -f origin main"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env, timeout=90)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _dismiss(work, fid):
    return subprocess.run([sys.executable, _GATE, "--dismiss", fid],
                          capture_output=True, text=True, cwd=str(work), timeout=30)


def _last_recorded_finding(work):
    git_dir = review_gate._git_dir(str(work))
    entries = review_gate._read_history(git_dir, 1)
    assert entries, "no recorded review"
    findings = entries[-1].get("findings") or []
    assert findings, "recorded review has no findings"
    return findings[0]


CODE_V1 = SIG + "\n    handle(user)\n"
CODE_V2 = SIG + "\n    handle(user)  # touched again\n"


def test_dismissed_finding_is_demoted_on_next_push(tmp_path):
    work = _repo(tmp_path, {"app/x.py": "def handle(user):\n    pass\n"})
    _git(["commit", "-q", "--allow-empty", "-m", "base2"], cwd=work)
    env = _env(tmp_path)

    # 1. The defect class blocks the push...
    (work / "app/x.py").write_text("def handle(user):\n    " + CODE_V1, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "add quota path"], cwd=work)
    decision, reason = _push(work, env)
    assert decision == "deny", reason

    # 2. ...the user dismisses that recorded finding by id...
    finding = _last_recorded_finding(work)
    assert finding["severity"] == "high"
    fid = finding.get("id") or review_gate._finding_id(finding)
    proc = _dismiss(work, fid)
    assert proc.returncode == 0, proc.stderr
    store = Path(om.store_path(str(work)))
    before = store.read_bytes()
    assert before.count(b"\n") == 1

    # 3. ...and the same defect class re-raised on the next push is demoted:
    # the push passes, the recorded finding is low with the prefix, and the
    # plain review run left the store untouched.
    (work / "app/x.py").write_text("def handle(user):\n    " + CODE_V2, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "touch quota path again"], cwd=work)
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    demoted = _last_recorded_finding(work)
    assert demoted["severity"] == "low"
    assert demoted["content"].startswith("previously dismissed")
    assert CONTENT in demoted["content"]
    assert store.read_bytes() == before, "a plain review run must never write the store"


def test_undismissed_high_finding_still_blocks(tmp_path):
    """Control: without a dismissal the same push blocks, so the pass above is
    the memory and not a broken gate."""
    work = _repo(tmp_path, {"app/x.py": "def handle(user):\n    pass\n"})
    (work / "app/x.py").write_text("def handle(user):\n    " + CODE_V1, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "add quota path"], cwd=work)
    decision, reason = _push(work, _env(tmp_path))
    assert decision == "deny", reason
    assert not Path(om.store_path(str(work))).exists()


def test_review_without_store_creates_nothing(tmp_path):
    work = _repo(tmp_path, {"app/x.py": "def handle(user):\n    pass\n"})
    (work / "app/x.py").write_text("def handle(user):\n    " + CODE_V1 + "# v2\n",
                                   encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "change"], cwd=work)
    _push(work, _env(tmp_path))
    assert not (work / ".ocr").exists()
