#!/usr/bin/env python3
"""Acceptance simulation harness for review-gate (docs-gate 07, spec seam 2).

Runs the REAL gate -- the installed pre-push hook, the detached supervisor,
the real `kimchi` worker with its pinned models; never the stub worker --
against a scratch copy of a target repository, driving real `git push`es at a
local bare remote (a fake origin). No remote anywhere: the scratch's remotes
are rewritten to local paths under the scratch root and the harness asserts
every effective push URL stays on this machine. The original repository is
never mutated: it is cloned read-only, and its refs/status are snapshotted
before and after the run and compared.

The gate is asynchronous singleflight: the first push for a tip spawns the
supervisor and is denied with "is still running"; a re-run of the same push
joins the finished review's verdict. drive_push() implements that two-step:
it re-attempts the identical push until the gate stops answering
"is still running", then classifies the final answer as pass (push landed) or
block (gate denial).

Parameterized: --repo (target git repository), --scenarios (a Python module
exposing SCENARIOS and optionally SEED_FILES), --gate-scripts (the review-gate
plugin's scripts/ directory, defaulting to this repo's own). Future gate
features reuse it by supplying another scenario module.

Scenario modules contain data only: each scenario is a name, a list of
commits (write/delete/replace file operations), an expected outcome
("block"/"pass") and substrings the denial/approval transcript must (and must
not) contain. Assertions are on external behavior -- was the push blocked,
and what did the pusher read -- never on gate internals.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# The gate's async denial (review-gate.py, _still_running_message): the push
# was NOT executed; the identical push re-run joins the finished review.
STILL_RUNNING_RE = re.compile(r"is still running", re.IGNORECASE)
# The gate's block banner (_fail_closed in the verdict branch).
BLOCK_BANNER = "review-gate blocked this commit"
# Git's own trailer when the pre-push hook fails.
GIT_PUSH_FAILED = "failed to push some refs"
# Every answer the gate prints -- async denial, block banner, error-state
# deny -- mentions itself. A failed push whose output has NO such marker was
# never seen by the gate (a git-level/transport failure): classifying that as
# a "block" is how a harness accident poisons every scenario verdict after it.
GATE_MARKER_RE = re.compile(r"review-gate", re.IGNORECASE)

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from rg_final_review import run_owned_process  # noqa: E402


class SimError(RuntimeError):
    """Infrastructure failure of the harness itself (never a scenario verdict)."""


@dataclass
class CommitSpec:
    """One fabricated commit: full-file writes, deletions, and exact string
    replacements (old must occur exactly once in the file at that point)."""
    message: str
    write: dict = field(default_factory=dict)    # path -> full text
    delete: list = field(default_factory=list)   # paths
    replace: list = field(default_factory=list)  # [(path, old, new)]


@dataclass
class Scenario:
    name: str
    description: str
    commits: list                    # [CommitSpec] (single-push form)
    expect: str                      # "block" | "pass"
    expect_message: list = field(default_factory=list)   # required substrings (ci)
    forbid_message: list = field(default_factory=list)   # forbidden substrings (ci)
    steps: list = field(default_factory=list)  # [PushStep] (multi-push form)


@dataclass
class PushStep:
    """One push inside a multi-push scenario (SPEC-15 acceptance: waiver
    scenarios need a block push, an operator command, then another push).
    Commits apply on top of the previous step's tip; `commands` run between
    the commits and the push, in the work clone with the push environment --
    gate operator commands (waive/unwaive/--list). argv tokens may use the
    placeholders "{python}" (this interpreter), "{gate}" (the gate script
    under test), "{finding:<path>}" (the display id of the last recorded
    verdict's finding on <path>, read from the append-only findings log --
    the same id a pusher reads from --history), and "{findings:<path>}"
    (the PLURAL: EVERY accepted display id on <path> in the latest recorded
    verdict, one argv token each, in recorded order -- a waiver scenario
    pre-waives all the expected findings on a path, not just the first)."""
    label: str
    commits: list = field(default_factory=list)            # [CommitSpec]
    commands: list = field(default_factory=list)            # [argv tokens]
    expect: str = "block"
    expect_message: list = field(default_factory=list)
    forbid_message: list = field(default_factory=list)


@dataclass
class Scratch:
    root: Path        # scratch root; EVERYTHING the run touches lives under here
    work: Path        # the working clone of the target repository
    origin: Path      # the fake remote: a local bare repository
    branch: str       # the pushed branch (the clone's initial branch)
    hooks: Path       # gate hooks dir materialized by the repo's own installer
    gitconfig: Path   # GIT_CONFIG_GLOBAL redirect (the real one is never touched)


@dataclass
class ScenarioResult:
    name: str
    expect: str
    outcome: str                     # "block" | "pass" | "error"
    ok: bool
    pushes: int
    elapsed_s: float
    transcript: str
    failures: list = field(default_factory=list)


def _git(args, cwd, env=None, check=True):
    proc = subprocess.run(["git"] + args, cwd=str(cwd), env=env,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SimError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


# --------------------------------------------------------------------------
# Original-repository integrity
# --------------------------------------------------------------------------

def repo_snapshot(repo):
    """{p head, refs, status} -- compared before and after the run."""
    repo = str(repo)
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    refs = {}
    out = _git(["for-each-ref", "--format=%(refname) %(objectname)"], repo).stdout
    for line in out.splitlines():
        line = line.strip()
        if line:
            name, _, sha = line.partition(" ")
            refs[name] = sha
    status = _git(["status", "--porcelain=v1"], repo).stdout
    return {"head": head, "refs": refs, "status": status}


def assert_repo_unchanged(before, after):
    diffs = []
    if before["head"] != after["head"]:
        diffs.append(f"HEAD moved: {before['head']} -> {after['head']}")
    if before["refs"] != after["refs"]:
        diffs.append("refs changed")
    if before["status"] != after["status"]:
        diffs.append("working-tree status changed")
    if diffs:
        raise SimError("the target repository was mutated: " + "; ".join(diffs))


# --------------------------------------------------------------------------
# Scratch copy + remote isolation
# --------------------------------------------------------------------------

def _is_local_path_url(url):
    """True when a git URL is a plain local path (no scheme, no scp syntax)."""
    if re.match(r"^[a-zA-Z]:[\\/]", url):    # a Windows drive path, not scp syntax
        return True
    return not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url) and ":" not in url.split("/", 1)[0]


def assert_remote_isolation(scratch, env=None):
    """Prove the scratch's git state cannot leave the machine.

    Every configured remote URL and pushurl -- evaluated with url.*.insteadOf
    rewrites applied (git ls-remote --get-url) -- must be a local path under
    the scratch root. Anything else is a hard harness failure.
    """
    work, root = str(scratch.work), scratch.root.resolve()
    remotes = _git(["remote"], work).stdout.split()
    if not remotes:
        raise SimError("scratch has no remotes at all; expected the fake origin")
    for name in remotes:
        for kind in ("url", "pushurl"):
            proc = _git(["remote", "get-url"] + (["--push"] if kind == "pushurl" else []) + [name],
                        work, env=env, check=False)
            for url in [u.strip() for u in proc.stdout.splitlines() if u.strip()]:
                if not _is_local_path_url(url):
                    raise SimError(f"remote {name!r} {kind} is not a local path: {url!r}")
                if not Path(url).resolve().is_relative_to(root):
                    raise SimError(f"remote {name!r} {kind} escapes the scratch root: {url!r}")


def make_scratch(target, root, branch=None, seed_files=None, seed_executables=None):
    """Clone `target` into root/work, seed an optional baseline commit, rewrite
    every remote to a local bare fake origin, and push the baseline with
    --no-verify (before the gate hook is installed). Paths listed in
    `seed_executables` are chmod +x before the baseline commit so a seeded
    command-check executor survives the clone with its exec bit (the gate's
    command-check contract requires a repo-relative EXECUTABLE)."""
    target, root = Path(target).resolve(), Path(root).resolve()
    if not (target / ".git").exists():
        raise SimError(f"not a git repository: {target}")
    root.mkdir(parents=True, exist_ok=True)
    work, origin = root / "work", root / "origin.git"
    _git(["clone", "--no-hardlinks", "--quiet", str(target), str(work)], root)
    if seed_files:
        for rel, text in seed_files.items():
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        for rel in seed_executables or []:
            p = work / rel
            if not p.is_file():
                raise SimError(f"seeded executable missing from SEED_FILES: {rel}")
            os.chmod(str(p), 0o755)
        _git(["add", "-A"], work)
        _git(["-c", "user.email=sim@harness.local", "-c", "user.name=sim-harness",
              "commit", "-q", "-m", "sim-harness seed: baseline decision record"], work)
    for name in _git(["remote"], work).stdout.split():
        _git(["remote", "remove", name], work)
    _git(["init", "--bare", "-q", str(origin)], root)
    head_branch = branch or _git(["rev-parse", "--abbrev-ref", "HEAD"], work).stdout.strip()
    _git(["remote", "add", "origin", str(origin)], work)
    for k, v in (("user.email", "sim@harness.local"), ("user.name", "sim-harness"),
                 ("commit.gpgsign", "false")):
        _git(["config", k, v], work)
    scratch = Scratch(root=root, work=work, origin=origin, branch=head_branch,
                      hooks=root / "hooks", gitconfig=root / "gitconfig")
    assert_remote_isolation(scratch)
    _git(["push", "--no-verify", "-q", "-u", "origin", f"HEAD:{head_branch}"], work)
    return scratch


def install_gate_hook(scratch, gate_scripts):
    """Install the gate using the repo's OWN installer, with every global side
    effect redirected into the scratch: SCR_HOOKS_DIR receives the hook copy
    and GIT_CONFIG_GLOBAL receives the core.hooksPath write, so the installer's
    `git config --global` can never touch the developer's real gitconfig."""
    installer = Path(gate_scripts) / "install-git-hook.sh"
    if not installer.is_file():
        raise SimError(f"installer not found: {installer}")
    before = _git(["config", "--global", "--get", "core.hooksPath"], scratch.root,
                  check=False).stdout.strip()
    env = dict(os.environ)
    env["SCR_HOOKS_DIR"] = str(scratch.hooks)
    env["GIT_CONFIG_GLOBAL"] = str(scratch.gitconfig)
    proc = subprocess.run(["bash", str(installer)], env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SimError(f"install-git-hook.sh failed: {proc.stderr.strip() or proc.stdout.strip()}")
    hook = scratch.hooks / "pre-push"
    if not hook.is_file():
        raise SimError(f"installer did not materialize {hook}")
    after = _git(["config", "--global", "--get", "core.hooksPath"], scratch.root,
                 check=False).stdout.strip()
    if before != after:
        raise SimError("the installer mutated the REAL global gitconfig "
                       f"({before!r} -> {after!r}) despite the GIT_CONFIG_GLOBAL redirect")
    assert_remote_isolation(scratch)
    return hook


def push_env(scratch, gate_scripts, inline_budget=120, review_timeout=2700):
    """Environment for scenario pushes: the scratch's redirected global config
    (so core.hooksPath applies), SCR_GATE_DIR at the plugin under test, a
    scratch-private state store, the REAL worker (no OCR_STUB_WORKER /
    OCR_REVIEWER_CMD -- `kimchi` must be on PATH), and an inline budget low
    enough that the async two-step (spawn, then join on re-push) is exercised
    deliberately."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    env.update({
        "GIT_CONFIG_GLOBAL": str(scratch.gitconfig),
        "SCR_GATE_DIR": str(gate_scripts),
        "OCR_STATE_DIR": str(scratch.root / "rg-state"),
        "CLAUDE_PLUGIN_DATA": str(scratch.root / "gate-data"),
        "OCR_INLINE_BUDGET_GIT": str(inline_budget),
        "OCR_TIMEOUT": str(review_timeout),
    })
    return env


# --------------------------------------------------------------------------
# Driving pushes
# --------------------------------------------------------------------------

def _origin_intact(scratch):
    """The fake remote must survive the whole run: it is the fence-post of
    remote isolation, and nothing the harness or the gate legitimately runs
    writes outside it -- least of all deletes it."""
    return (scratch.origin / "HEAD").is_file()


def _origin_vanished_message(scratch):
    """Diagnose the mid-scenario disappearance of the fake origin.

    Observed 2026-11-11: while a real review ran detached, origin.git was
    deleted under the scratch root and every later push died with git's
    'does not appear to be a git repository' -- misclassified as gate
    'block's across the remaining scenarios. This is a HARNESS-level failure,
    so it raises and names the vanished remote instead of faking a verdict."""
    try:
        children = sorted(p.name for p in scratch.root.iterdir())
    except OSError:
        children = []
    return (f"the fake origin {scratch.origin} VANISHED mid-scenario "
            "(something outside the harness deleted the bare remote); "
            f"scratch root now contains: {children or '(unreadable)'}. "
            "This is a harness error, not a gate verdict.")


def _assert_origin_intact(scratch):
    if not _origin_intact(scratch):
        raise SimError(_origin_vanished_message(scratch))


def _transport_failure_message(scratch, out):
    """A failed push the gate never answered: diagnose it as the harness
    error it is, never as a scenario 'block'."""
    origin = ("still exists" if _origin_intact(scratch)
              else "VANISHED mid-scenario")
    return ("git-level push failure with no gate output -- a transport error "
            "is not a gate verdict and is never classified as a 'block':\n"
            f"  the fake origin {scratch.origin} {origin}\n"
            f"  push said:\n{out.strip()[-1500:]}")


def drive_push(scratch, env, timeout_s=5400, poll_s=10):
    """Push HEAD to the fake origin, driving the gate's async two-step.

    Returns (outcome, transcript, attempts): "pass" when the push landed,
    "block" when the gate denied with anything that is not the still-running
    async answer. Raises SimError when the gate never settles, when a failed
    push has no gate answer at all (a git-level transport failure is a
    harness error, not a gate verdict), or when the fake origin itself
    vanishes mid-scenario.

    The default settle deadline must span a REAL review end to end: the
    detached supervisor first regenerates the docs summary inline when it is
    stale (measured ~470 s on kimchi-studio, docs-gate 07) and only then
    reviews (measured ~630 s for a one-file push); minutes, not seconds."""
    deadline = time.monotonic() + timeout_s
    attempts = []
    while True:
        proc = subprocess.run(["git", "push", "origin", f"HEAD:{scratch.branch}"],
                              cwd=str(scratch.work), env=env, capture_output=True, text=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        attempts.append({"rc": proc.returncode, "output": out})
        if proc.returncode == 0:
            return "pass", "\n".join(a["output"] for a in attempts), attempts
        if STILL_RUNNING_RE.search(out):
            if time.monotonic() > deadline:
                raise SimError(f"gate still running after {timeout_s}s -- never reached a verdict")
            time.sleep(poll_s)
            _assert_origin_intact(scratch)
            continue
        if not GATE_MARKER_RE.search(out):
            raise SimError(_transport_failure_message(scratch, out))
        return "block", "\n".join(a["output"] for a in attempts), attempts


def apply_commit(work, spec):
    for rel, text in (spec.write or {}).items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    for rel in spec.delete or []:
        p = work / rel
        if not p.exists():
            raise SimError(f"delete target missing: {rel}")
        p.unlink()
    for rel, old, new in spec.replace or []:
        p = work / rel
        text = p.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SimError(f"replace anchor occurs {text.count(old)}x (need exactly 1) in {rel}")
        p.write_text(text.replace(old, new), encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-q", "-m", spec.message], work)


def reset_to_origin(scratch):
    """Back to the fake origin's tip, keeping gitignored local caches (.ocr/)
    so a once-generated docs summary survives into the next scenario."""
    _git(["reset", "--hard", "-q", f"origin/{scratch.branch}"], scratch.work)
    _git(["clean", "-fdq"], scratch.work)


def _display_id(f):
    """The id the gate shows, waives, and unwaives by (review-gate.py
    _waiver_display_id over _finding_id): the reviewer-assigned id verbatim,
    else the stable sha256(path, existing_code, content) truncated to 12
    characters -- the fallback command checks (no reviewer id) get."""
    sid = f.get("id")
    if sid:
        return str(sid)
    raw = "\x00".join(str(v) for v in (
        f.get("path") or "", f.get("existing_code") or "",
        f.get("content") or ""))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]


def _read_findings_log(scratch):
    """The append-only findings log's entries, oldest first; [] when the
    log does not exist yet (nothing has been recorded to waive from)."""
    log = scratch.work / ".git" / "review-gate-findings.jsonl"
    try:
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
    entries = []
    for ln in lines:
        try:
            entries.append(json.loads(ln))
        except ValueError:
            continue
    return entries


def _last_finding_id_for_path(scratch, path):
    """The display id accepted by the gate for the finding on `path`
    (reviewer id verbatim, else the stable sha fallback)."""
    for entry in reversed(_read_findings_log(scratch)):
        for f in entry.get("findings") or []:
            if isinstance(f, dict) and f.get("path") == path:
                return _display_id(f)
    raise SimError(f"no recorded finding on {path!r} in the findings log")


def _finding_ids_for_path(scratch, path):
    """EVERY accepted display id on `path` in the latest recorded verdict,
    in recorded order (the singular picks only the first). A blocking push
    can record SEVERAL findings on one path -- a reviewer correctness
    finding, an existing-test reproduction, an ephemeral probe of any
    shape -- and a waiver scenario must pre-waive them all or the ones
    left out keep blocking. Still surgical per content anchor: a finding
    that appears differently on the next push gets a different stable id,
    misses the waived ledger entry, and blocks."""
    for entry in reversed(_read_findings_log(scratch)):
        ids = [_display_id(f) for f in entry.get("findings") or []
               if isinstance(f, dict) and f.get("path") == path]
        if ids:
            return ids
    raise SimError(f"no recorded finding on {path!r} in the findings log")


def _run_gate_command(scratch, env, argv, gate_scripts):
    """One operator command between pushes: placeholders resolved, run in
    the work clone with the push env (the ledger and history the gate reads
    are the same ones the pushes use). Returns (rc, output)."""
    resolved = []
    for tok in argv:
        if tok == "{python}":
            resolved.append(sys.executable)
        elif tok == "{gate}":
            resolved.append(os.path.join(str(gate_scripts), "review-gate.py"))
        elif tok.startswith("{findings:") and tok.endswith("}"):
            # The PLURAL: every accepted id on that path, one token each.
            resolved.extend(
                _finding_ids_for_path(scratch, tok[len("{findings:"):-1]))
        elif tok.startswith("{finding:") and tok.endswith("}"):
            resolved.append(_last_finding_id_for_path(scratch, tok[len("{finding:"):-1]))
        else:
            resolved.append(tok)
    proc = subprocess.run(resolved, cwd=str(scratch.work), env=env,
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_scenario(scratch, env, scenario, timeout_s=5400, gate_scripts=None):
    t0 = time.monotonic()
    failures, pushes, transcripts = [], 0, []
    reset_to_origin(scratch)
    if scenario.steps:
        steps = scenario.steps
    else:
        steps = [PushStep(label=scenario.name, commits=scenario.commits,
                          expect=scenario.expect,
                          expect_message=scenario.expect_message,
                          forbid_message=scenario.forbid_message)]
    outcome = "error"
    for step in steps:
        step_text = []   # this step's commands + push transcript (assertions
        for spec in step.commits or []:   # see both: an operator command's
            apply_commit(scratch.work, spec)   # output is part of what the
        for argv in step.commands or []:  # pusher reads, e.g. a waiver's
            rc, out = _run_gate_command(scratch, env, argv, gate_scripts)  # loud
            step_text.append(f"$ {' '.join(argv)}\n{out}")  # listing or a
            transcripts.append(step_text[-1])  # recorded warning read back
            if rc != 0:
                failures.append(f"command step {step.label!r} exited {rc}: "
                                f"{argv}")
        outcome, transcript, attempts = drive_push(scratch, env, timeout_s=timeout_s)
        pushes += len(attempts)
        step_text.append(transcript)
        transcripts.append(transcript)
        if outcome != step.expect:
            failures.append(f"step {step.label!r}: expected {step.expect!r}, "
                            f"got {outcome!r}")
        if outcome == "block" and BLOCK_BANNER not in transcript:
            failures.append(f"step {step.label!r}: denial lacks the gate's "
                            f"block banner {BLOCK_BANNER!r}")
        if outcome == "pass" and BLOCK_BANNER.lower() in transcript.lower():
            failures.append(f"step {step.label!r}: approval transcript contains "
                            "the block banner")
        low = "\n".join(step_text).lower()
        for sub in step.expect_message:
            if sub.lower() not in low:
                failures.append(f"step {step.label!r}: message missing required "
                                f"text: {sub!r}")
        for sub in step.forbid_message:
            if sub.lower() in low:
                failures.append(f"step {step.label!r}: message contains forbidden "
                                f"text: {sub!r}")
    transcript = "\n".join(transcripts)
    return ScenarioResult(name=scenario.name, expect=scenario.expect, outcome=outcome,
                          ok=not failures, pushes=pushes,
                          elapsed_s=round(time.monotonic() - t0, 1),
                          transcript=transcript, failures=failures)


# --------------------------------------------------------------------------
# SPEC-16 real-model final-review simulations
# --------------------------------------------------------------------------

_REAL_WORKER_OVERRIDES = (
    "OCR_REVIEWER_CMD", "OCR_STUB_WORKER", "RG_FINAL_WORKER",
    "RG_FINAL_DYNAMIC_EXECUTOR", "OCR_TEST_SELECTOR_CMD",
)
_PROVIDER_SECRET_PARTS = ("GITLAB", "GITHUB", "GLAB_", "GH_")


def real_model_environment(environ=None, *, artifact_dir):
    """Return an inference-capable, recorded-provider simulation environment.

    No reviewer/selector test double can leak into a real-model run. Provider
    credentials from the caller are discarded; the local recorded GitLab
    transport receives only a non-secret marker so its read audit remains
    explicit. Model authentication is retained for the selected gate backend.
    """
    source = os.environ if environ is None else environ
    env = dict(source)
    for key in list(env):
        upper = key.upper()
        if upper.startswith(("STUB_", "GIT_TRACE")) or any(
            part in upper for part in _PROVIDER_SECRET_PARTS
        ):
            env.pop(key, None)
    for key in _REAL_WORKER_OVERRIDES:
        env.pop(key, None)
    for key in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_COUNT"):
        env.pop(key, None)
    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    env["OCR_STATE_DIR"] = str(root / "state")
    if env.get("RG_TEST_PROVIDER_SCRIPT"):
        env["GLAB_TOKEN"] = "recorded-read-only-provider-fixture"
    return env


def selected_inference_backend(environ=None, *, repo_root=None):
    """Resolve the gate's real backend and fail exactly when it is unavailable."""
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    import ocr_profile

    env = os.environ if environ is None else environ
    try:
        selection = ocr_profile.select_backend(env=env, repo_root=repo_root)
    except ocr_profile.BackendError as exc:
        raise SimError(f"selected inference backend is unavailable: {exc}") from exc
    executable = shutil.which(selection.name, path=env.get("PATH"))
    if not executable:
        raise SimError(
            f"selected inference backend {selection.name!r} is not on PATH "
            f"({selection.reason}); real-model simulation was not executed"
        )
    return {
        "name": selection.name,
        "reason": selection.reason,
        "executable": executable,
        "notes": list(selection.notes),
    }


def persist_simulation_artifact(artifact_dir, name, payload):
    """Persist one human/machine inspectable artifact and return its identity."""
    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    path.write_text(serialized + "\n", encoding="utf-8")
    digest = hashlib.sha256((serialized + "\n").encode("utf-8")).hexdigest()
    return {
        "evidence_id": "sha256:" + digest,
        "artifact": str(path),
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def _recorded_provider_writes(script_path):
    """Audit recorded provider CLI calls; any mutation is a hard failure."""
    if not script_path:
        return {"calls": 0, "provider_writes": False, "audit": "no provider fixture"}
    script = json.loads(Path(script_path).read_text(encoding="utf-8"))
    calls_path = Path(script["calls"])
    calls = json.loads(calls_path.read_text(encoding="utf-8")) if calls_path.exists() else []
    mutations = []
    for call in calls:
        argv = [str(part) for part in call.get("argv") or []]
        lowered = [part.lower() for part in argv]
        mutating_flags = {
            "-x", "--method", "-f", "--raw-field", "-f", "--field", "--input",
        }
        method_write = any(
            part in {"post", "put", "patch", "delete"}
            for index, part in enumerate(lowered)
            if index and lowered[index - 1] in {"-x", "--method"}
        )
        if mutating_flags.intersection(lowered) or method_write:
            mutations.append(argv)
    return {
        "calls": len(calls),
        "provider_writes": bool(mutations),
        "mutating_argv": mutations,
        "audit": str(calls_path),
    }


def _simulation_cleanup(artifact_dir, result=None):
    clone_root = Path(artifact_dir).resolve() / "state" / "clones"
    registered = sorted(str(path) for path in clone_root.glob("*-final-*")) \
        if clone_root.exists() else []
    repository_paths = []
    for unit in (result or {}).get("units") or []:
        value = unit.get("repository_path")
        if value:
            repository_paths.append(str(value))
    return {
        "registered_scratch_clones": registered,
        "materialized_repositories_remaining": [path for path in repository_paths if Path(path).exists()],
        "clean": not registered and not any(Path(path).exists() for path in repository_paths),
    }


def run_final_review_simulation(reference, *, repo_root, artifact_dir, environ=None,
                                timeout=7200, runner=None):
    """Invoke the public final-review CLI with the selected real backend.

    Provider reads may come from ``RG_TEST_PROVIDER_SCRIPT``; every recorded
    call is audited for writes. The source snapshot, CLI result, inference
    diagnostics, and cleanup proof are persisted before a verdict is returned.
    """
    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = real_model_environment(environ, artifact_dir=root)
    backend = selected_inference_backend(env, repo_root=repo_root)
    if backend["name"] == "kimchi":
        env["KIMCHI_BIN"] = backend["executable"]
    before = repo_snapshot(repo_root)
    gate = Path(_SCRIPTS) / "review-gate.py"
    command = [sys.executable, str(gate), "final-review", str(reference)]
    process = None
    error = ""
    stdout = stderr = ""
    returncode = None
    process_cleanup = None
    try:
        if runner is None:
            # Bounded independent lens waves fit inside the existing two-hour
            # outer allowance; no model depth or individual coverage is removed.
            process = run_owned_process(
                command, cwd=str(repo_root), env=env, timeout=float(timeout),
            )
        else:
            process = runner(
                command, cwd=str(repo_root), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=float(timeout), check=False,
            )
    except subprocess.TimeoutExpired as exc:
        error = f"real-model final-review invocation unavailable: TimeoutExpired: {exc}"
        stdout = exc.output or ""
        stderr = exc.stderr or ""
        returncode = getattr(exc, "returncode", None)
        process_cleanup = getattr(exc, "cleanup", None)
    except (OSError, subprocess.SubprocessError) as exc:
        error = f"real-model final-review invocation unavailable: {type(exc).__name__}: {exc}"
    if process is not None:
        stdout = process.stdout or ""
        stderr = process.stderr or ""
        returncode = process.returncode
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    try:
        # Partial output from a killed deadline is evidence, never a completed
        # operator result (and must not replace the exact timeout diagnosis).
        result = json.loads(stdout) if process is not None and stdout.strip() else None
    except ValueError:
        result = None
        error = "real-model final-review returned non-JSON operator output"
    provider = _recorded_provider_writes(env.get("RG_TEST_PROVIDER_SCRIPT"))
    after = repo_snapshot(repo_root)
    source_unchanged = before == after
    if not source_unchanged:
        error = "real-model simulation mutated the source repository"
    if provider["provider_writes"]:
        error = "recorded provider audit contains a forbidden provider mutation"
    cleanup = _simulation_cleanup(root, result)
    if process_cleanup is not None:
        cleanup["process"] = process_cleanup
        cleanup["clean"] = bool(
            cleanup["clean"] and process_cleanup.get("status") == "complete"
        )
    cleanup_verified = process_cleanup is None or bool(
        process_cleanup.get("direct_child_reaped")
        and process_cleanup.get("tree_termination_verified")
        and process_cleanup.get("status") == "complete"
    )
    if process is None and "TimeoutExpired" in error and cleanup_verified:
        # Only registered clones inside this simulation's private state root
        # are eligible for harness cleanup; never touch the caller's store. An
        # unverified Windows descendant tree keeps its directories as evidence.
        removed = []
        clone_root = (root / "state" / "clones").resolve()
        for registered in cleanup["registered_scratch_clones"]:
            path = Path(registered).resolve()
            if path.is_relative_to(clone_root) and "-final-" in path.name:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(str(path))
        cleanup = _simulation_cleanup(root, result)
        if process_cleanup is not None:
            cleanup["process"] = process_cleanup
        cleanup["registered_clones_removed_after_timeout"] = removed
    elif process is None and "TimeoutExpired" in error and process_cleanup is not None:
        cleanup["registered_clones_removed_after_timeout"] = []
    report = {
        "schema": "spec16-real-model-simulation/v1",
        "kind": "final-review",
        "evidence_type": "real-model-simulation",
        "backend": backend,
        "provider": provider,
        "command": ["review-gate.py", "final-review", str(reference)],
        "returncode": returncode,
        "result": result,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
        "source_unchanged": source_unchanged,
        "cleanup": cleanup,
        "state": (result or {}).get("state", "incomplete"),
    }
    report["artifact"] = persist_simulation_artifact(root, "final-review-simulation.json", report)
    return report


def run_dynamic_simulation(repository, target_sha, head_sha, *, artifact_dir,
                           environ=None, timeout=600, dynamic_runner=None):
    """Run the reference dynamic selector/executor against a real pinned Git unit."""
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    import rg_final_dynamic

    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = real_model_environment(environ, artifact_dir=root)
    backend = selected_inference_backend(env, repo_root=repository)
    if backend["name"] != "kimchi":
        raise SimError(
            "the reference dynamic selector requires the kimchi launch shape; "
            f"selected gate backend is {backend['name']!r}; dynamic simulation was not executed"
        )
    env["KIMCHI_BIN"] = backend["executable"]
    before = repo_snapshot(repository)
    execute = rg_final_dynamic.run_dynamic_checks if dynamic_runner is None else dynamic_runner
    # materialize_review reads the process environment (its public API has no
    # environ argument), so the mapping passed to the executor alone is not a
    # sufficient scratch-store sandbox. Temporarily bind the actual process
    # too, restoring the caller even when inference or execution raises.
    previous_state_dir = os.environ.get("OCR_STATE_DIR")
    os.environ["OCR_STATE_DIR"] = env["OCR_STATE_DIR"]
    try:
        result = execute(
            repository, target_sha, head_sha, environ=env, timeout=float(timeout),
            unit_key="individual:1",
        )
    finally:
        if previous_state_dir is None:
            os.environ.pop("OCR_STATE_DIR", None)
        else:
            os.environ["OCR_STATE_DIR"] = previous_state_dir
    after = repo_snapshot(repository)
    cleanup = _simulation_cleanup(root)
    probe_root = Path(repository) / rg_final_dynamic.PROBE_ROOT
    cleanup["source_probe_root_remaining"] = probe_root.exists()
    cleanup["clean"] = cleanup["clean"] and not probe_root.exists()
    report = {
        "schema": "spec16-real-model-simulation/v1",
        "kind": "dynamic",
        "evidence_type": "real-model-simulation",
        "backend": backend,
        "provider": {"calls": 0, "provider_writes": False, "audit": "no provider transport"},
        "result": result,
        "state": (result or {}).get("state", "degraded"),
        "source_unchanged": before == after,
        "cleanup": cleanup,
        "error": "" if before == after else "real-model dynamic simulation mutated the source repository",
    }
    report["artifact"] = persist_simulation_artifact(root, "dynamic-simulation.json", report)
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_scenario_module(path):
    # Scenario modules import the harness itself (Scenario/CommitSpec).
    d = os.path.dirname(os.path.abspath(path))
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("sim_scenarios", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    scenarios = getattr(mod, "SCENARIOS", None)
    if not scenarios:
        raise SimError(f"{path} defines no SCENARIOS")
    return mod, scenarios


def _result_dict(r):
    return {"name": r.name, "expect": r.expect, "outcome": r.outcome, "ok": r.ok,
            "pushes": r.pushes, "elapsed_s": r.elapsed_s, "failures": r.failures,
            "transcript": r.transcript}


def main(argv=None):
    ap = argparse.ArgumentParser(description="review-gate acceptance simulation harness")
    ap.add_argument("--repo", required=True, help="target git repository (cloned, never mutated)")
    ap.add_argument("--scenarios", required=True, help="Python module: SCENARIOS (+ SEED_FILES)")
    ap.add_argument("--workdir", default="", help="scratch root (default: fresh sim-scratch-<ts>)")
    ap.add_argument("--gate-scripts", default=_SCRIPTS, help="plugin scripts/ under test")
    ap.add_argument("--branch", default="", help="branch to exercise (default: clone's HEAD)")
    ap.add_argument("--timeout", type=int, default=5400,
                    help="per-push settle deadline (s); must span a real review "
                         "including an inline docs-summary regeneration (measured "
                         "~1100s combined on kimchi-studio, docs-gate 07)")
    ap.add_argument("--inline-budget", type=int, default=120, help="OCR_INLINE_BUDGET_GIT (s)")
    ap.add_argument("--only", default="", help="comma-separated scenario names to run "
                    "(default: all in the module)")
    args = ap.parse_args(argv)
    t_start = time.monotonic()

    target = Path(args.repo).resolve()
    mod_path = Path(args.scenarios).resolve()
    mod, scenarios = load_scenario_module(str(mod_path))
    seed_files = getattr(mod, "SEED_FILES", None)
    seed_executables = getattr(mod, "SEED_EXECUTABLES", None)
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        missing = wanted - {s.name for s in scenarios}
        if missing:
            raise SimError(f"--only names unknown scenario(s): {sorted(missing)}")
        scenarios = [s for s in scenarios if s.name in wanted]
    root = Path(args.workdir).resolve() if args.workdir else (
        Path.cwd() / f"sim-scratch-{time.strftime('%Y%m%d-%H%M%S')}")

    report = {"target": str(target), "scenarios": str(mod_path), "root": str(root),
              "seeded": sorted((seed_files or {}).keys()), "results": []}
    print(f"[sim] target: {target}")
    print(f"[sim] scratch: {root}")
    if seed_files:
        print(f"[sim] seeded into the scratch baseline: {', '.join(sorted(seed_files))}")

    before = repo_snapshot(target)
    scratch = make_scratch(target, root, branch=args.branch or None,
                           seed_files=seed_files,
                           seed_executables=seed_executables)
    hook = install_gate_hook(scratch, args.gate_scripts)
    print(f"[sim] gate hook installed via the repo installer: {hook}")
    env = push_env(scratch, args.gate_scripts, inline_budget=args.inline_budget)

    killed = None
    for scenario in scenarios:
        print(f"[sim] --- {scenario.name}: {scenario.description}")
        print(f"[sim]     expect: {scenario.expect}")
        try:
            r = run_scenario(scratch, env, scenario, timeout_s=args.timeout,
                             gate_scripts=args.gate_scripts)
        except SimError as exc:
            killed = str(exc)
            r = ScenarioResult(name=scenario.name, expect=scenario.expect, outcome="error",
                               ok=False, pushes=0, elapsed_s=0, transcript="", failures=[killed])
        report["results"].append(_result_dict(r))
        verdict = "OK  " if r.ok else "FAIL"
        print(f"[sim]     [{verdict}] outcome={r.outcome} pushes={r.pushes} "
              f"elapsed={r.elapsed_s}s")
        for line in r.failures:
            print(f"[sim]     failure: {line}")
        if killed:
            break

    # Ticket checkbox: the target's real summary cache is regenerated inline
    # during the run (docs-gate 05's path on a real project).
    artifact = scratch.work / ".ocr" / "docs-summary.md"
    report["docs_summary_cache"] = {
        "exists": artifact.is_file(),
        "bytes": artifact.stat().st_size if artifact.is_file() else 0,
        "freshness_marker": artifact.read_text(encoding="utf-8").splitlines()[0][:120]
        if artifact.is_file() else "",
    }
    print(f"[sim] docs summary cache regenerated inline: "
          f"{report['docs_summary_cache']['exists']} "
          f"({report['docs_summary_cache']['bytes']} bytes)")

    try:
        assert_repo_unchanged(before, repo_snapshot(target))
        report["target_untouched"] = True
    except SimError as exc:
        report["target_untouched"] = False
        report["target_mutation"] = str(exc)
    print(f"[sim] original repository untouched: {report['target_untouched']}")

    out_path = root / "results.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    all_ok = (not killed and report["target_untouched"]
              and all(r["ok"] for r in report["results"]))
    print(f"[sim] report written to {out_path}")
    print(f"[sim] ==== {'ALL SCENARIOS PASS' if all_ok else 'FAILURES RECORDED'} "
          f"({time.monotonic() - t_start:.0f}s total) ====")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
