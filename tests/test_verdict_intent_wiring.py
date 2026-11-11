"""t25: the verdict record carries the intent context the review used.

t15 gave rg_store.record_verdict an optional `intent` extras dict; no
production caller passed it. This suite pins the wiring added in
_supervise_run:

  - the day-0 single-context intent view is computed ONCE at collection
    time (_verdict_intent_base) -- no recompute and no fetch at verdict
    time -- and lands in the record: sources, the intent text (capped at
    _INTENT_RECORD_TEXT_CAP), counts and fetch notes;
  - chunked runs add per-chunk counts + text digests (_intent_chunk_stat),
    capped at _INTENT_RECORD_MAX_CHUNKS, while the record keeps the
    single-context view;
  - intent recording is fail-open: a builder bug degrades to "no intent
    extra", never a failed finalize and never a lost verdict record;
  - the ticket side is exercised with the fetch boundary MOCKED
    (rg_ticket_fetch.fetch_tickets): no network in tests, and the mock's
    call count proves verdict time does not re-fetch.

Style follows test_async_gate.py: in-process _supervise runs against the
stub worker (kimi_stub via OCR_STUB_WORKER=1) for the single-context and
fail-open cases, a hook-level subprocess for the chunked case.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402

from conftest import scrub_git_trace  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate_intent_wiring", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


_SUBJECT = "fix: retry on timeout"
_BODY = "The request path now retries transient timeouts with backoff."


import pytest  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """One pushed base commit on main behind a bare origin."""
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _commit(work, files, msg=_SUBJECT, body=_BODY):
    for name, text in files.items():
        (work / name).write_text(text)
    _git(["add", "."], cwd=work)
    args = ["commit", "-q", "-m", msg] + (["-m", body] if body else [])
    _git(args, cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _run_supervisor(repo, tip):
    """One in-process supervisor run for tip's push range."""
    common = review_gate._git_common_dir(str(repo))
    state_path = review_gate._state_path(common, tip)
    base = _git(["rev-parse", "origin/main"], cwd=repo)
    review_gate._write_state(state_path, {
        "state": "claimed", "run_id": "t25run", "claimed_ts": time.time(),
        "tip": tip, "branch": "main", "base": base,
        "range": f"{base}..{tip}", "repo_root": str(repo),
        "git_dir": review_gate._git_dir(str(repo)), "mode": "hook",
        "attempts": 0, "remote": "origin",
        "remote_url": rg_store.remote_url(str(repo), "origin"),
        "protocol_version": review_gate.PROTOCOL_VERSION,
    })
    review_gate._supervise(str(state_path), "t25run")
    return review_gate._read_state(state_path)


def _verdict_record(work, tip, remote="origin"):
    url = rg_store.remote_url(str(work), remote)
    return rg_store.load_verdict(url, tip)


# ---------------------------------------------------------------------------
# Unit: the record builders (pure, over already-collected data)
# ---------------------------------------------------------------------------

_COMMITS = [
    {"sha": "a" * 40, "subject": _SUBJECT, "body": _BODY, "files": ["retry.py"]},
    {"sha": "b" * 40, "subject": "chore: bump deps", "body": "", "files": ["deps.txt"]},
]

_TICKET_CTX = {
    "tickets": [{"key": "PROJ-123", "tracker": "github", "source": "branch",
                 "role": "target", "title": "Retry transient timeouts",
                 "body": "Requests must retry with backoff.", "truncated": False}],
    "listed_only": ["PROJ-999"],
    "notes": ["PROJ-999: left unfetched (cap 3)", "github fetch ok"],
}


def test_verdict_intent_base_none_when_no_intent_context():
    assert review_gate._verdict_intent_base(None, None, "b..t") is None
    assert review_gate._verdict_intent_base([], {"tickets": []}, "b..t") is None


def test_verdict_intent_base_commit_messages_only():
    base = review_gate._verdict_intent_base(_COMMITS, None, "b..t")
    assert base["sources"] == ["commit_messages"]
    assert _SUBJECT in base["text"] and _BODY in base["text"]
    assert "chore: bump deps" in base["text"]
    assert base["counts"]["messages"] == 2
    assert base["counts"]["tickets"] == 0
    assert base["range"] == "b..t"
    assert base["notes"] == []


def test_verdict_intent_base_ticket_and_messages_from_collected_data():
    """The fetch boundary is already behind us: the base builder never
    fetches, it only formats the collected ticket_context."""
    base = review_gate._verdict_intent_base(_COMMITS, _TICKET_CTX, "origin/main..tip")
    assert base["sources"] == ["ticket", "commit_messages"]
    # Ticket text precedes commit messages (precedence order in the record).
    assert base["text"].index("PROJ-123") < base["text"].index(_SUBJECT)
    assert "Retry transient timeouts" in base["text"]
    assert base["counts"] == {"tickets": 1, "listed_only": 1,
                              "messages": 2, "subject_only": 0}
    assert base["notes"] == ["PROJ-999: left unfetched (cap 3)",
                             "github fetch ok"]


def test_verdict_intent_base_ticket_only_still_records():
    base = review_gate._verdict_intent_base(None, _TICKET_CTX, "b..t")
    assert base["sources"] == ["ticket"]
    assert "PROJ-123" in base["text"]


def test_verdict_intent_base_text_is_capped_at_the_named_constant():
    fat = [{"sha": "c" * 40, "subject": _SUBJECT,
            "body": "x" * (review_gate._INTENT_RECORD_TEXT_CAP * 3),
            "files": ["retry.py"]}]
    base = review_gate._verdict_intent_base(fat, None, "b..t")
    cap = review_gate._INTENT_RECORD_TEXT_CAP
    assert len(base["text"]) <= cap + 60  # cap + the visible marker line
    assert f"capped at {cap} chars" in base["text"]


def test_intent_chunk_stat_counts_and_stable_digest():
    intent = review_gate._intent_extra(_COMMITS, ["retry.py"], "b..t")
    stat = review_gate._intent_chunk_stat(0, intent)
    assert stat == review_gate._intent_chunk_stat(0, intent)  # deterministic
    assert stat["chunk"] == 0
    assert stat["messages"] == 1          # only retry.py's commit is in scope
    assert stat["subject_only"] == 1      # the other commit demoted, not dropped
    assert len(stat["digest"]) == 12
    # A different chunk's text digests differently.
    other = review_gate._intent_extra(_COMMITS, ["deps.txt"], "b..t")
    assert review_gate._intent_chunk_stat(1, other)["digest"] != stat["digest"]


def test_verdict_intent_extras_combines_and_caps():
    assert review_gate._verdict_intent_extras(None, None) is None
    assert review_gate._verdict_intent_extras(None, []) is None
    base = review_gate._verdict_intent_base(_COMMITS, None, "b..t")
    stats = [{"chunk": k, "messages": 1, "subject_only": 0,
              "trimmed": False, "digest": f"{k:012d}"}
             for k in range(review_gate._INTENT_RECORD_MAX_CHUNKS + 10)]
    extra = review_gate._verdict_intent_extras(base, stats)
    assert extra["sources"] == ["commit_messages"]
    assert len(extra["chunks"]) == review_gate._INTENT_RECORD_MAX_CHUNKS
    assert "chunks" not in base  # the stash dict is never mutated
    # Chunk stats alone (no single-context view) still produce a record.
    only = review_gate._verdict_intent_extras(None, stats[:1])
    assert only == {"chunks": stats[:1]}


# ---------------------------------------------------------------------------
# Wiring: the finalize path records what the review used
# ---------------------------------------------------------------------------

def test_single_context_run_records_the_intent_used(repo, gate_env):
    tip = _commit(repo, {"retry.py": "def fetch(u):\n    return get(u)\n"})
    st = _run_supervisor(repo, tip)
    assert st["state"] == "done" and st["verdict"] == "pass", st.get("reason")
    rec = _verdict_record(repo, tip)
    assert rec is not None
    intent = rec["intent"]
    assert intent["sources"] == ["commit_messages"]
    assert _SUBJECT in intent["text"] and _BODY in intent["text"]
    assert intent["counts"]["messages"] == 1
    assert intent["range"].endswith(f"..{tip}")
    # Single-context: no per-chunk digest section at all.
    assert "chunks" not in intent
    # The core record is untouched by the extra (protocol_version 1).
    assert rec["verdict"] == "pass" and rec["protocol_version"] == 1


def test_ticket_context_lands_in_the_record_without_a_verdict_time_refetch(
        repo, gate_env, monkeypatch):
    """intent.tickets on; discovery + fetch mocked at the module boundary.
    The mock's call count proves the verdict path never re-fetches."""
    settings = {"intent": {"tickets": True, "messages": True}, "jira": {}}
    monkeypatch.setattr(review_gate.rg_config, "load_settings",
                        lambda repo_root=None: dict(settings, notes=[]))
    monkeypatch.setattr(
        review_gate.rg_tickets, "discover_ticket_keys",
        lambda settings, branch=None, commit_messages=None: {
            "selected": [{"key": "PROJ-123", "source": "branch", "role": "target"}],
            "extras": ["PROJ-999"], "notes": ["PROJ-999: cap reached"]})
    calls = {"n": 0}

    def _fake_fetch(settings, discovered, repo_root=None, **_):
        calls["n"] += 1
        return {"tickets": [{"key": "PROJ-123", "tracker": "github",
                             "source": "branch", "role": "target",
                             "title": "Retry transient timeouts",
                             "body": "Requests must retry with backoff.",
                             "truncated": False}],
                "notes": ["github fetch ok"]}

    monkeypatch.setattr(review_gate.rg_ticket_fetch, "fetch_tickets", _fake_fetch)

    tip = _commit(repo, {"retry.py": "def fetch(u):\n    return get(u)\n"})
    st = _run_supervisor(repo, tip)
    assert st["state"] == "done", st.get("reason")
    rec = _verdict_record(repo, tip)
    intent = rec["intent"]
    assert intent["sources"] == ["ticket", "commit_messages"]
    assert intent["text"].index("PROJ-123") < intent["text"].index(_SUBJECT)
    assert intent["counts"]["tickets"] == 1
    assert intent["counts"]["listed_only"] == 1
    assert "github fetch ok" in intent["notes"]
    assert "PROJ-999: cap reached" in intent["notes"]
    assert calls["n"] == 1, "fetch ran only at collection, never at verdict time"


def test_intent_builder_bug_never_breaks_finalize(repo, gate_env, monkeypatch):
    """Fail-open: a crash building the intent extra must cost only the
    extra -- the verdict record itself is still written."""
    def _boom(*a, **k):
        raise RuntimeError("simulated intent builder bug")

    monkeypatch.setattr(review_gate, "_verdict_intent_base", _boom)
    tip = _commit(repo, {"retry.py": "def fetch(u):\n    return get(u)\n"})
    st = _run_supervisor(repo, tip)
    assert st["state"] == "done" and st["verdict"] == "pass", st.get("reason")
    rec = _verdict_record(repo, tip)
    assert rec is not None and rec["verdict"] == "pass"
    assert "intent" not in rec


def _push_hook(repo, tmp_path, **env_over):
    env = scrub_git_trace(dict(os.environ))
    for k in ("OCR_IN_REVIEW", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_FORCE_REVIEW",
              "STUB_SLEEP", "STUB_VERDICT"):
        env.pop(k, None)
    env["OCR_STUB_WORKER"] = "1"
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    env.update({k: str(v) for k, v in env_over.items()})
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(repo),
                          "tool_input": {"command": "git push origin main"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(repo), env=env,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc


def _wait_done(repo, tip, timeout=90):
    common = review_gate._git_common_dir(str(repo))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed"):
            return st
        time.sleep(0.2)
    raise AssertionError(f"never finished: {review_gate._read_state(path)}")


def test_chunked_run_records_single_context_view_plus_chunk_digests(repo, tmp_path):
    # Four files, threshold 3 and one file per chunk -> a 4-chunk run.
    files = {f"f{i}.py": f"def f{i}():\n    return {i}\n" for i in range(4)}
    tip = _commit(repo, files)
    _push_hook(repo, tmp_path,
               OCR_CHUNK_THRESHOLD=3, OCR_CHUNK_FILES=1, OCR_CHUNK_LINES=99999,
               OCR_RUN_BUDGET=9999)
    st = _wait_done(repo, tip)
    assert st["state"] == "done", st.get("reason")
    rec = _verdict_record(repo, tip)
    intent = rec["intent"]
    # The record keeps the single-context (day-0) view ...
    assert intent["sources"] == ["commit_messages"]
    assert _SUBJECT in intent["text"]
    assert intent["counts"]["messages"] == 1
    # ... plus one compact counts+digest entry per chunk, nothing bigger.
    chunks = intent["chunks"]
    assert sorted(c["chunk"] for c in chunks) == [0, 1, 2, 3]
    for c in chunks:
        assert set(c) == {"chunk", "messages", "subject_only", "trimmed", "digest"}
        assert c["messages"] == 1 and len(c["digest"]) == 12
    assert len(chunks) <= review_gate._INTENT_RECORD_MAX_CHUNKS
