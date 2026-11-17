"""SPEC-15.3 end to end: parallel command-check scheduling, isolated clone.

A command check declaring `parallel: true` runs concurrently with the review
session spawn, against its own throwaway clone of the tip -- never the
review tree, so reviewer greps and reads can never observe executor-written
probe files. The gate joins it before verdict assembly; both subsets merge
through the one extra_findings/extra_warnings path (sequential findings
first, then parallel ones -- the documented order), so compute_verdict
stays the sole verdict authority and every downstream pass is untouched.

These tests drive the real hook with the stub reviewer, asserting on
externally visible events (marker files the checks and the reviewer touch)
and on the run's state/telemetry -- never on internals. The overlap proof
is an event-join, not a wall-clock duration: the stub reviewer touches a
file right before answering (STUB_TOUCH), and the parallel check's own
start-marker must be OLDER than that touch (mtime ordering brackets the
two runs jointly).
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
import ocr_telemetry as _ot  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_pc", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)
rg_store = review_gate.rg_store

_APP_V1 = "def verdict():\n    return 'model-says-pass'\n"
_APP_V2 = "def verdict():\n    return trust_model()\n\n\ndef trust_model():\n    return True\n"


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp_path, files):
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    _write(work, files)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _write(work, files):
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _commit(work, files):
    _write(work, files)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "c"], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _write_checks_config(work, checks):
    """The per-repo checks override, uncommitted (rg_config reads the file)."""
    p = work / ".ocr" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"checks": checks}), encoding="utf-8")


def _env(tmp_path, trace, **kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    env.update({"CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
                "OCR_STATE_DIR": os.environ["OCR_STATE_DIR"],
                "OCR_REVIEWER_CMD": f'"{sys.executable}" "{_STUB}"',
                "STUB_TRACE": str(tmp_path / trace), "OCR_INLINE_BUDGET": "30"})
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _push(work, env):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": "git push origin main"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _state(work, tip, timeout=90):
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while True:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed") or time.monotonic() > deadline:
            return st
        time.sleep(0.2)


def _run_records(work):
    common = review_gate._git_common_dir(str(work))
    return [r for r in _ot.load(common, days=1)]


def _finding_contents(work):
    """Every telemetry finding content of the repo's runs, in record order."""
    out = []
    for r in _run_records(work):
        for f in (r.get("findings") or []):
            out.append(str(f.get("content") or ""))
    return out


def _run_warnings(work):
    out = []
    for r in _run_records(work):
        for w in (r.get("warnings") or []):
            out.append(str(w.get("message") or w))
    return out


def _check_records(work):
    """The command_checks telemetry entries of every run, flattened."""
    return [e for r in _run_records(work) for e in (r.get("command_checks") or [])]


def _repo_with_checks(tmp_path, checks, bodies):
    """A repo whose tip carries the given executable check scripts (committed
    -- the clones are checkouts of the tip), with the declarations (mode /
    parallel / timeout) in the uncommitted .ocr/config.json override that
    rg_config reads from the working tree."""
    work = _repo(tmp_path, {"app.py": _APP_V1})
    for rel, body in bodies.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "add checks"], cwd=work)
    _write_checks_config(work, checks)
    return work


def _finding_body(content_cmd):
    """A check body answering one finding whose content is the given shell
    command's stdout (severity high, path app.py)."""
    return ("#!/bin/sh\n"
            "cat >/dev/null\n"
            "content=\"$(%s)\"\n"
            "printf '{\"findings\": [{\"path\": \"app.py\", \"start_line\": 1, "
            "\"end_line\": 1, \"severity\": \"high\", \"confidence\": 0.95, "
            "\"category\": \"custom\", \"content\": \"%s\"}]}' \"$content\"\n"
            % (content_cmd, "%s"))


def _push_and_state(work, tmp_path, trace, **env_kw):
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, trace, **env_kw))
    return decision, reason, _state(work, tip)


def test_parallel_check_overlaps_the_review_session(tmp_path):
    """SPEC-15.3: a parallel check runs CONCURRENTLY with the review. Event
    -join, not wall-clock: the stub reviewer (sleeping 2 s) touches a marker
    right before answering; the parallel check touches its own start-marker
    first, then waits for the reviewer's. Overlap is proven when the check's
    marker is OLDER than the reviewer's -- i.e. the executor was alive while
    the review session was still running. Under today's sequential semantics
    the check would start only after the review, and its marker would be the
    newer one."""
    events = tmp_path / "events"
    events.mkdir()
    started, answered = events / "parallel-started", events / "reviewer-answered"
    body = (
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "touch %s\n"
        "i=0\n"
        "while [ ! -f %s ] && [ \"$i\" -lt 600 ]; do\n"
        "  sleep 0.1; i=$((i+1))\n"
        "done\n"
        "if [ -f %s ] && [ %s -ot %s ]; then\n"
        "  content=bracketed-parallel-executor-ran-during-the-review\n"
        "else\n"
        "  content=NOT-bracketed-parallel-check-started-only-after-the-review\n"
        "fi\n"
        "printf '{\"findings\": [{\"path\": \"app.py\", \"start_line\": 1, "
        "\"end_line\": 1, \"severity\": \"high\", \"confidence\": 0.95, "
        "\"category\": \"custom\", \"content\": \"%%s\"}]}' \"$content\"\n"
        % (shlex.quote(str(started)), shlex.quote(str(answered)),
           shlex.quote(str(answered)), shlex.quote(str(started)),
           shlex.quote(str(answered))))
    work = _repo_with_checks(
        tmp_path,
        {"race-probe": {"command": "scripts/raceprobe.sh", "globs": ["**/*.py"],
                        "parallel": True, "mode": "warn"}},
        {"scripts/raceprobe.sh": body})
    decision, reason, st = _push_and_state(
        work, tmp_path, "t.trace", STUB_SLEEP="2", STUB_TOUCH=str(answered))
    assert decision == "pass", reason
    # A warn-mode finding lands a `warn` verdict, never a block -- the review
    # survived the concurrent check either way.
    assert st.get("state") == "done" and st.get("verdict") in ("pass", "warn"), st
    contents = _finding_contents(work)
    assert any("bracketed-parallel-executor-ran-during-the-review" in c
               for c in contents), contents
    assert not any("NOT-bracketed" in c for c in contents), contents
    assert answered.exists(), "the reviewer stub actually ran"


def test_parallel_check_writes_probes_only_in_its_own_clone(tmp_path):
    """Isolation: the parallel executor's cwd is a DEDICATED throwaway clone
    of the tip, never the review tree. The check writes a probe file and
    reports its own cwd (it verifies the probe exists while it runs); a
    sequential check -- which keeps today's semantics, cwd the review clone
    -- reports that the probe never appeared there. After the run the
    parallel clone is gone (removed with the run's other temporaries)."""
    par = ("#!/bin/sh\n"
           "cat >/dev/null\n"
           "echo probe > probe.txt\n"
           "if [ -f probe.txt ]; then p=probe-ok; else p=probe-MISSING; fi\n"
           "content=\"parpwd $PWD $p\"\n"
           "printf '{\"findings\": [{\"path\": \"app.py\", \"start_line\": 1, "
           "\"end_line\": 1, \"severity\": \"high\", \"confidence\": 0.95, "
           "\"category\": \"custom\", \"content\": \"%s\"}]}' \"$content\"\n")
    seq = ("#!/bin/sh\n"
           "cat >/dev/null\n"
           "if [ -f probe.txt ]; then p=yes; else p=no; fi\n"
           "content=\"seqpwd $PWD probe-in-review-root $p\"\n"
           "printf '{\"findings\": [{\"path\": \"app.py\", \"start_line\": 1, "
           "\"end_line\": 1, \"severity\": \"high\", \"confidence\": 0.95, "
           "\"category\": \"custom\", \"content\": \"%s\"}]}' \"$content\"\n")
    work = _repo_with_checks(
        tmp_path,
        {"race-probe": {"command": "scripts/raceprobe.sh", "globs": ["**/*.py"],
                        "parallel": True, "mode": "warn"},
         "seq-probe": {"command": "scripts/seqprobe.sh", "globs": ["**/*.py"],
                       "mode": "warn"}},
        {"scripts/raceprobe.sh": par, "scripts/seqprobe.sh": seq})
    decision, reason, st = _push_and_state(work, tmp_path, "t.trace")
    assert decision == "pass", reason
    contents = _finding_contents(work)
    par_c = next(c for c in contents if c.startswith("parpwd "))
    seq_c = next(c for c in contents if c.startswith("seqpwd "))
    assert "probe-ok" in par_c, par_c            # the clone is writable, live during the run
    assert "probe-in-review-root no" in seq_c, seq_c   # never in the review tree
    par_pwd, seq_pwd = par_c.split()[1], seq_c.split()[1]
    clones = str(rg_store.clones_dir())
    assert par_pwd.startswith(clones) and seq_pwd.startswith(clones), (par_c, seq_c)
    assert par_pwd != seq_pwd, "the parallel clone is a second, separate clone"
    assert par_pwd != str(work) and seq_pwd != str(work), (par_c, seq_c)
    assert not Path(par_pwd).exists(), "the parallel clone is removed after the run"


def test_parallel_findings_join_before_verdict_in_stream_order(tmp_path):
    """Join-before-verdict: the parallel subset's findings merge with the
    sequential subset's through the same extra_findings path -- sequential
    findings first, then parallel ones (the documented order) -- and ride the
    same verdict machinery: both block, and command_checks telemetry carries
    entries for both subsets."""
    par = _finding_body("echo parallel-finding-race")
    seq = _finding_body("echo sequential-finding-dsl")
    work = _repo_with_checks(
        tmp_path,
        {"race-probe": {"command": "scripts/raceprobe.sh", "globs": ["**/*.py"],
                        "parallel": True},
         "dsl-check": {"command": "scripts/seqprobe.sh", "globs": ["**/*.py"]}},
        {"scripts/raceprobe.sh": par, "scripts/seqprobe.sh": seq})
    decision, reason, st = _push_and_state(work, tmp_path, "t.trace")
    assert decision == "deny", reason
    assert st.get("verdict") == "block", st
    assert "sequential-finding-dsl" in (st.get("reasons") or ""), st
    assert "parallel-finding-race" in (st.get("reasons") or ""), st
    names = {e.get("name") for e in _check_records(work)}
    assert {"race-probe", "dsl-check"} <= names, names
    # Documented merge order: sequential findings precede parallel ones.
    contents = _finding_contents(work)
    assert ("sequential-finding-dsl" in contents
            and "parallel-finding-race" in contents), contents
    assert contents.index("sequential-finding-dsl") < contents.index(
        "parallel-finding-race"), contents


def test_parallel_check_timeout_is_honored(tmp_path):
    """The check's own `timeout` replaces the 60 s default on the parallel
    path: a 2 s sleep under a 1 s budget degrades to a warning naming the
    budget, with telemetry; the review itself survives."""
    work = _repo_with_checks(
        tmp_path,
        {"slow-race": {"command": "scripts/raceprobe.sh", "globs": ["**/*.py"],
                       "parallel": True, "timeout": 1}},
        {"scripts/raceprobe.sh": "#!/bin/sh\ncat >/dev/null\nsleep 2\n"})
    decision, reason, st = _push_and_state(work, tmp_path, "t.trace")
    assert decision == "pass", reason
    assert st.get("state") == "done" and st.get("verdict") == "pass", st
    errors = [str(e.get("error") or "") for e in _check_records(work)]
    assert any("timed out after 1s" in e for e in errors), errors
    assert any("timed out after 1s" in w for w in _run_warnings(work))


def test_parallel_check_nonzero_exit_degrades_to_warning_and_telemetry(tmp_path):
    """Executor error mirrors today's sequential degrade: findings discarded,
    warning plus telemetry, the gate never crashes."""
    work = _repo_with_checks(
        tmp_path,
        {"boom-race": {"command": "scripts/raceprobe.sh", "globs": ["**/*.py"],
                       "parallel": True}},
        {"scripts/raceprobe.sh": "#!/bin/sh\ncat >/dev/null\nexit 3\n"})
    decision, reason, st = _push_and_state(work, tmp_path, "t.trace")
    assert decision == "pass", reason
    assert st.get("state") == "done" and st.get("verdict") == "pass", st
    errors = [str(e.get("error") or "") for e in _check_records(work)]
    assert any("exit code 3" in e for e in errors), errors
    assert any("exit code 3" in w for w in _run_warnings(work))


def test_sequential_check_keeps_running_against_the_review_clone(tmp_path):
    """Regression: a check WITHOUT `parallel` keeps today's exact semantics
    -- post-review, cwd the review clone (never the user's working tree).
    The rest of the sequential surface is the existing test_docs_gate.py
    command-check suite, which this change must leave green."""
    seq = _finding_body("echo seqpwd $PWD")
    work = _repo_with_checks(
        tmp_path,
        {"dsl-check": {"command": "scripts/seqprobe.sh", "globs": ["**/*.py"],
                       "mode": "warn"}},
        {"scripts/seqprobe.sh": seq})
    decision, reason, st = _push_and_state(work, tmp_path, "t.trace")
    assert decision == "pass", reason
    contents = _finding_contents(work)
    seq_c = next((c for c in contents if c.startswith("seqpwd ")), "")
    pwd = seq_c.split()[1]
    assert pwd != str(work), "the check runs in the clone, never the live tree"
    assert pwd.startswith(str(rg_store.clones_dir())), seq_c


def test_parallel_clone_creation_failure_skips_checks_never_review_root(
        monkeypatch, tmp_path):
    """Clone-creation failure degrades to a warning noting the parallel check
    was SKIPPED -- it never falls back to running against review_root
    (isolation is the point: a probe written there would contaminate the
    reviewer's evidence). Unit seam (the test_waivers.py in-process
    monkeypatch pattern): the clone factory fails, the worker never
    spawns, and the check's marker never appears."""
    executed = tmp_path / "executed-marker"
    body = ("#!/bin/sh\n"
            "cat >/dev/null\n"
            "touch %s\n"
            "printf '{\"findings\": []}'\n"
            % shlex.quote(str(executed)))
    work = _repo_with_checks(
        tmp_path,
        {"race-probe": {"command": "scripts/raceprobe.sh", "globs": ["**/*.py"],
                        "parallel": True}},
        {"scripts/raceprobe.sh": body})
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    monkeypatch.setattr(review_gate.rg_store, "make_review_clone",
                        lambda *a, **k: "")
    review_gate._TELE.pop("command_checks", None)
    runner = review_gate._ParallelCommandChecks(
        str(work), str(tmp_path / "state.json"), "r1", None, tip).start()
    assert runner.thread is None and runner.clone == ""
    findings, warnings = runner.join()
    assert findings == []
    assert any("parallel command checks skipped" in str(w.get("message") or "")
               and "race-probe" in str(w.get("message") or "")
               for w in warnings), warnings
    tele = review_gate._TELE.get("command_checks") or []
    assert tele and tele[0].get("name") == "race-probe", tele
    assert "skipped" in str(tele[0].get("error") or ""), tele
    assert not executed.exists(), "the check never ran anywhere"
    runner.cleanup()   # no clone, no thread: a safe no-op
