"""SPEC-15.5 end to end: the reference test-check executor (examples/test-check).

The executor is the command check a gated repo wires up: matched changed
paths on stdin, {"findings", "warnings"} on stdout, selector-driven (one
cheap model call per push, stubbed here through OCR_TEST_SELECTOR_CMD -- the
OCR_REVIEWER_CMD stub pattern, so no test ever calls a real model). These
tests drive the REAL executable against scratch fixture repos and assert the
externally visible contract:

* a fabricated regression in changed behavior, selected as an EXISTING repo
  test, fails twice and becomes a finding quoting the verbatim output
  (evidence_source existing_test) with no fail-soft warnings;
* a race-detector probe (Go where the toolchain exists, a simulated probe
  otherwise) becomes an ephemeral_test finding paired with the canonical
  no-permanent-coverage warning;
* a flake (fails once, passes the re-run) warns instead of blocking;
* a coverage gap (nothing selected, nothing probed) is the canonical warning;
* an unbuildable probe degrades to a warning, never a crash or a finding;
* nothing outside the executor's own scratch dir is ever written;
* a malformed selector reply costs exactly ONE attempt and one warning;
* the gate's own runner accepts the executor's answer (contract proof,
  including the coverage-warning pairing rule).
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
EXECUTOR = os.path.join(_ROOT, "examples", "test-check", "test_check")
POLICY = os.path.join(_ROOT, "scripts", "rg_final_env.py")
PROBE_ROOT = ".test-check-probes"

sys.path.insert(0, os.path.join(_ROOT, "scripts"))
_spec = importlib.util.spec_from_file_location(
    "review_gate_tc", os.path.join(_ROOT, "scripts", "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

_PY = sys.executable
_IMPORTER = (
    "import os, sys\n"
    "sys.path.insert(0, os.path.dirname(os.path.dirname("
    "os.path.abspath(__file__))))\n"
    "from dsl import verdict\n"
)
_DSL_OK = "def verdict():\n    return 'ok'\n"
_DSL_TEST = (_IMPORTER
             + "assert verdict() == 'ok', 'regression: verdict broken'\n")
_DSL_BROKEN = "def verdict():\n    return None\n"

_STUB_BODY = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "here = os.path.dirname(os.path.abspath(__file__))\n"
    "try:\n"
    "    manifest = json.load(sys.stdin)\n"
    "    assert isinstance(manifest, list) and manifest\n"
    "    assert all(isinstance(e, dict) and 'path' in e for e in manifest)\n"
    "except Exception:\n"
    "    pass\n"
    "with open(os.path.join(here, 'calls'), 'a') as fh:\n"
    "    fh.write('call\\n')\n"
    "with open(os.path.join(here, 'selection.json'), encoding='utf-8') as fh:\n"
    "    sys.stdout.write(fh.read())\n"
)


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                   text=True, check=True)


def _repo(tmp_path, commits):
    """A git repo whose commits are the given {path: content} dicts."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _git(["init", "-q", "-b", "main", str(work)], cwd=tmp_path)
    for key, value in (("user.email", "t@t.com"), ("user.name", "t"),
                       ("commit.gpgsign", "false")):
        _git(["config", key, value], cwd=work)
    for files in commits:
        for rel, body in files.items():
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        _git(["add", "."], cwd=work)
        _git(["commit", "-q", "-m", "c"], cwd=work)
    return work


def _stub_selector(tmp_path, selection):
    """(OCR_TEST_SELECTOR_CMD value, call-count file): a selector stub that
    consumes the manifest from stdin and answers with the given selection
    (a dict) or raw text (malformed-output cases)."""
    sel_dir = tmp_path / "selector"
    sel_dir.mkdir(exist_ok=True)
    stub = sel_dir / "stub.py"
    stub.write_text(_STUB_BODY, encoding="utf-8")
    sel_file = sel_dir / "selection.json"
    sel_file.write_text(selection if isinstance(selection, str)
                        else json.dumps(selection), encoding="utf-8")
    count = sel_dir / "calls"
    return '"%s" "%s"' % (_PY, stub), count


def _env(**kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "FLAKE_", "KIMCHI_"))}
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _run_executor(repo, matched, env, timeout=120):
    return subprocess.run([EXECUTOR], input=json.dumps(matched), text=True,
                          capture_output=True, cwd=str(repo), env=env,
                          timeout=timeout)


def _answer(proc):
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert isinstance(data, dict), data
    assert isinstance(data.get("findings"), list), data
    assert isinstance(data.get("warnings"), list), data
    return data


def test_existing_test_regression_blocks_with_verbatim_output(tmp_path):
    """The core story: a selected EXISTING repo test that fails the push's
    behavior fails, is re-run, fails again, and becomes a blocking finding
    quoting the failing output verbatim -- with the fail-soft channels
    silent (no warnings at all)."""
    work = _repo(tmp_path, [
        {"dsl.py": _DSL_OK, "tests/test_dsl.py": _DSL_TEST},
        {"dsl.py": _DSL_BROKEN},
    ])
    cmd, _count = _stub_selector(
        tmp_path, {"existing_tests": ["tests/test_dsl.py"], "probes": []})
    proc = _run_executor(work, ["dsl.py"], _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    assert data["warnings"] == []
    (finding,) = data["findings"]
    assert finding["evidence_source"] == "existing_test"
    assert finding["path"] == "tests/test_dsl.py"
    assert finding["severity"] == "high"
    assert finding["confidence"] == 0.9
    assert "regression: verdict broken" in finding["content"], finding
    assert finding["content"].startswith("reproduced failure of existing test")


def test_race_probe_finding_and_coverage_warning(tmp_path):
    """A selector-authored probe for uncovered changed behavior: with the Go
    toolchain present, a real data race under `go test -race` becomes an
    ephemeral_test finding quoting the race detector's verbatim output,
    paired with the canonical no-permanent-coverage warning; without Go, a
    simulated probe proves the same contract (run_cmd executed the source
    file the executor wrote, via the {probe_file} placeholder)."""
    if shutil.which("go"):
        language, run_cmd = "go", "go test -race -count=1 ."
        marker = "WARNING: DATA RACE"
        source = (
            "package probe\n"
            "\n"
            "import (\n"
            '\t"sync"\n'
            '\t"testing"\n'
            ")\n"
            "\n"
            "func TestUnsynchronizedCounter(t *testing.T) {\n"
            "\tvar n int\n"
            "\tvar wg sync.WaitGroup\n"
            "\tfor i := 0; i < 4; i++ {\n"
            "\t\twg.Add(1)\n"
            "\t\tgo func() {\n"
            "\t\t\tdefer wg.Done()\n"
            "\t\t\tn++\n"
            "\t\t}()\n"
            "\t}\n"
            "\twg.Wait()\n"
            "\t_ = n\n"
            "}\n"
        )
        work = _repo(tmp_path, [
            {"go.mod": "module example.com/racy\n\ngo 1.16\n",
             "main.go": "package main\n\nfunc Counter() int { return 0 }\n"}])
    else:
        language = "python"
        run_cmd = '"%s" {probe_file}' % _PY
        marker = "simulated-race-detected"
        source = ('print("simulated-race-detected: unsynchronized counter")\n'
                  "import sys\n"
                  "sys.exit(3)\n")
        work = _repo(tmp_path, [{"main.py": "x = 1\n"}])
    cmd, _count = _stub_selector(tmp_path, {
        "existing_tests": [],
        "probes": [{"name": "race", "language": language,
                    "source": source, "run_cmd": run_cmd,
                    "path": "main.go" if language == "go" else "main.py"}]})
    proc = _run_executor(
        work, ["main.go" if language == "go" else "main.py"],
        _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    (finding,) = data["findings"]
    assert finding["evidence_source"] == "ephemeral_test"
    assert marker in finding["content"], finding["content"]
    covered = [w for w in data["warnings"]
               if str(w.get("message", "")).startswith(
                   "no permanent test coverage for")]
    assert covered, data["warnings"]
    assert "ephemeral probe was the only evidence" in covered[0]["message"]
    assert review_gate._COVERAGE_WARNING_RE.search(covered[0]["message"])
    assert not (work / PROBE_ROOT).exists(), "probe scratch dir was cleaned"


def test_flaky_test_demotes_to_warning(tmp_path):
    """Reproduce-then-block: a fixture that fails the first invocation and
    passes the second (state file) is a flake -- a WARNING naming the test,
    never a finding."""
    state = tmp_path / "flake-state"
    flaky = (
        "import os, sys\n"
        "state = os.environ['FLAKE_STATE']\n"
        "if not os.path.exists(state):\n"
        "    open(state, 'w').close()\n"
        "    print('flake: first invocation fails')\n"
        "    sys.exit(1)\n"
        "\n"
        "def test_flake_stable():\n"
        "    assert True\n"
    )
    work = _repo(tmp_path, [{"flake.py": "VALUE = 1\n",
                             "tests/test_flake.py": flaky}])
    cmd, _count = _stub_selector(
        tmp_path, {"existing_tests": ["tests/test_flake.py"], "probes": []})
    proc = _run_executor(work, ["flake.py"], _env(
        OCR_TEST_SELECTOR_CMD=cmd, FLAKE_STATE=state))
    data = _answer(proc)
    assert data["findings"] == []
    assert state.exists(), "the fixture test actually ran"
    (warning,) = data["warnings"]
    assert "flak" in warning["message"], warning
    assert "tests/test_flake.py" in warning["message"], warning


def test_coverage_gap_is_the_canonical_warning(tmp_path):
    """Nothing selected and nothing probed: the canonical no-permanent-
    coverage warning (matching the gate's _COVERAGE_WARNING_RE pairing
    pattern), no findings, no other noise."""
    work = _repo(tmp_path, [{"dsl.py": _DSL_OK}])
    cmd, _count = _stub_selector(
        tmp_path, {"existing_tests": [], "probes": []})
    proc = _run_executor(work, ["dsl.py"], _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    assert data["findings"] == []
    (warning,) = data["warnings"]
    assert warning["message"].startswith(
        "no permanent test coverage for dsl.py"), warning
    assert review_gate._COVERAGE_WARNING_RE.search(warning["message"])
    assert "no existing tests, no probes" in warning["message"]


def test_unbuildable_probe_degrades_to_warning(tmp_path):
    """A probe whose source does not compile never became a running test:
    warning, no crash, no findings -- the selector's limits can never block
    a push."""
    work = _repo(tmp_path, [{"app.py": "x = 1\n"}])
    cmd, _count = _stub_selector(tmp_path, {
        "existing_tests": [],
        "probes": [{"name": "broken", "language": "python",
                    "source": "def broken(:\n    pass\n",
                    "run_cmd": '"%s" {probe_file}' % _PY,
                    "path": "app.py"}]})
    proc = _run_executor(work, ["app.py"], _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    assert data["findings"] == []
    assert any("failed to build or run" in str(w.get("message", ""))
               and "broken" in str(w.get("message", ""))
               for w in data["warnings"]), data["warnings"]
    assert not (work / PROBE_ROOT).exists()


def test_existing_tests_selected_discards_probes_deterministically(tmp_path):
    """SPEC-15 deterministic coverage rule: existing tests OR probes, never
    both. The selector returns BOTH an existing test and a probe that would
    fail loudly (a blocking finding if it ran); the executor runs the
    existing test, never materializes or runs the probe, leaves no probe
    scratch, and reports the probes as unauthored. Without this rule every
    re-push of a known defect could mint a semantically new ephemeral
    finding (fresh output, fresh anchor) that no waiver ever matches, so
    waiver acceptance could never stabilize."""
    work = _repo(tmp_path, [
        {"dsl.py": _DSL_OK, "tests/test_dsl.py": _DSL_TEST},
        {"dsl.py": _DSL_BROKEN},
    ])
    loud = "selector-probe-would-have-blocked"
    cmd, _count = _stub_selector(tmp_path, {
        "existing_tests": ["tests/test_dsl.py"],
        "probes": [{"name": "loud", "language": "python",
                    "source": 'import sys\nprint("%s")\nsys.exit(1)\n' % loud,
                    "run_cmd": '"%s" {probe_file}' % _PY,
                    "path": "dsl.py"}]})
    proc = _run_executor(work, ["dsl.py"], _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    assert data["warnings"] == [], data["warnings"]
    (finding,) = data["findings"]
    assert finding["evidence_source"] == "existing_test"
    assert finding["path"] == "tests/test_dsl.py"
    assert "regression: verdict broken" in finding["content"], finding
    assert loud not in finding["content"], "the probe must never have run"
    assert data["telemetry"]["tests_selected"] == 1
    assert data["telemetry"]["probes_authored"] == 0
    assert not (work / PROBE_ROOT).exists(), "no probe scratch left"


def test_nothing_written_outside_the_probe_scratch(tmp_path):
    """Isolation, asserted: a probe-only run (an existing test in the same
    selection would deterministically DROP the probe -- the coverage rule
    above) leaves the clone's file tree byte-identical and adds nothing
    anywhere else in the fixture area -- probes live only inside the
    executor's scratch dir, which is removed afterwards. The probe is a
    shell one so no bytecode or cache side effects exist even without the
    executor's own hygiene."""
    passing = ("import os, sys\n"
               "sys.path.insert(0, os.path.dirname(os.path.dirname("
               "os.path.abspath(__file__))))\n"
               "import app\n"
               "assert app.x == 1\n"
               "\n"
               "def test_app_value():\n"
               "    assert app.x == 1\n")
    work = _repo(tmp_path, [{"app.py": "x = 1\n",
                             "tests/test_app.py": passing}])
    cmd, _count = _stub_selector(tmp_path, {
        "existing_tests": [],
        "probes": [{"name": "smoke", "language": "shell",
                    "source": "echo probe-ran\n",
                    "run_cmd": "sh {probe_file}", "path": "app.py"}]})

    def snapshot():
        out = {}
        for root, _dirs, files in os.walk(work):
            if ".git" in root.split(os.sep):
                continue
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), work)
                with open(os.path.join(root, name), "rb") as fh:
                    out[rel] = fh.read()
        return out

    def outside_names():
        # Scope: the fixture area MINUS the clone (checked separately by
        # snapshot()) and minus the selector/ stub directory, which the test
        # rig itself writes (calls/ log, selection.json) — this assertion
        # guards against the EXECUTOR writing outside its clone, not against
        # the stub's own bookkeeping.
        selector_dir = os.path.join(str(tmp_path), "selector")
        names = []
        for root, dirs, files in os.walk(tmp_path):
            if root == str(tmp_path):
                dirs[:] = [d for d in dirs
                          if os.path.join(root, d) not in
                          (str(work), selector_dir)]
            for name in files:
                names.append(os.path.relpath(os.path.join(root, name),
                                             tmp_path))
            for name in dirs:
                names.append(os.path.join(
                    os.path.relpath(root, tmp_path), name))
        return sorted(names)

    before, outside_before = snapshot(), outside_names()
    proc = _run_executor(work, ["app.py"], _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    assert data["findings"] == []
    assert any("ephemeral probe was the only evidence" in str(w.get("message"))
               for w in data["warnings"]), data["warnings"]
    assert snapshot() == before, "the clone's tree was modified"
    assert outside_names() == outside_before, "something appeared outside"
    assert not (work / PROBE_ROOT).exists()


def test_malformed_selector_output_one_warning_no_retry(tmp_path):
    """A plain-text selector reply: exactly ONE attempt (the stub counts its
    invocations), ONE warning, no findings, exit 0."""
    work = _repo(tmp_path, [{"app.py": "x = 1\n"}])
    cmd, count = _stub_selector(tmp_path, "this is plain text, not JSON")
    proc = _run_executor(work, ["app.py"], _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    assert data["findings"] == []
    assert data["warnings"] == [
        {"file": None, "message": "selector failed; no tests selected"}]
    assert count.read_text(encoding="utf-8") == "call\n", \
        "the selector is invoked exactly once"


def test_severity_and_confidence_env_knobs(tmp_path):
    """The finding's rank comes from the environment (default high): a repo
    can demote the whole check's findings without touching the executor."""
    work = _repo(tmp_path, [
        {"dsl.py": _DSL_OK, "tests/test_dsl.py": _DSL_TEST},
        {"dsl.py": _DSL_BROKEN},
    ])
    cmd, _count = _stub_selector(
        tmp_path, {"existing_tests": ["tests/test_dsl.py"], "probes": []})
    proc = _run_executor(work, ["dsl.py"], _env(
        OCR_TEST_SELECTOR_CMD=cmd, OCR_TEST_SEVERITY="medium",
        OCR_TEST_CONFIDENCE="0.5"))
    data = _answer(proc)
    (finding,) = data["findings"]
    assert finding["severity"] == "medium"
    assert finding["confidence"] == 0.5


def test_contract_through_the_gate_runner(tmp_path, monkeypatch):
    """The gate's own command-check runner accepts the executor end to end:
    copied into a repo as scripts/rg_test_check and run through
    _run_one_command_check, its findings parse (evidence_source survives),
    and the coverage-warning pairing holds -- an ephemeral_test finding
    WITH the canonical warning draws no gate-side note."""
    work = _repo(tmp_path, [
        {"dsl.py": _DSL_OK, "tests/test_dsl.py": _DSL_TEST},
        {"dsl.py": _DSL_BROKEN},
    ])
    exe = work / "scripts" / "rg_test_check"
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXECUTOR, exe)
    shutil.copyfile(POLICY, exe.parent / "rg_final_env.py")
    exe.chmod(0o755)
    cmd, _count = _stub_selector(
        tmp_path, {"existing_tests": ["tests/test_dsl.py"], "probes": []})
    monkeypatch.setenv("OCR_TEST_SELECTOR_CMD", cmd)
    findings, warnings, note, tele = review_gate._run_one_command_check(
        str(work), "scripts/rg_test_check", ["dsl.py"], timeout=120)
    assert note is None, note
    assert warnings == [], warnings
    (finding,) = findings
    assert finding["evidence_source"] == "existing_test"
    assert "regression: verdict broken" in finding["content"]

    # The ephemeral half: a failing probe finding PLUS the canonical
    # coverage warning -- the gate pairs them, so no gate-side note appears.
    cmd2, _count2 = _stub_selector(tmp_path, {
        "existing_tests": [],
        "probes": [{"name": "sim", "language": "python",
                    "source": 'import sys\nprint("sim-failure")\n'
                              "sys.exit(1)\n",
                    "run_cmd": '"%s" {probe_file}' % _PY,
                    "path": "dsl.py"}]})
    monkeypatch.setenv("OCR_TEST_SELECTOR_CMD", cmd2)
    findings, warnings, note, tele = review_gate._run_one_command_check(
        str(work), "scripts/rg_test_check", ["dsl.py"], timeout=120)
    assert note is None, note
    (finding,) = findings
    assert finding["evidence_source"] == "ephemeral_test"
    assert "sim-failure" in finding["content"]
    assert any(review_gate._COVERAGE_WARNING_RE.search(str(w.get("message")))
               for w in warnings), warnings
    assert not any("no-permanent-coverage warning" in str(w.get("message"))
                   for w in warnings), warnings
    assert not (work / PROBE_ROOT).exists()


# ---------------------------------------------------------------------------
# W9: the per-check budget reaches the executor; telemetry counts are real
# ---------------------------------------------------------------------------

_spec_tc = importlib.util.spec_from_file_location(
    "test_check_ref", EXECUTOR,
    loader=importlib.machinery.SourceFileLoader("test_check_ref", EXECUTOR))
test_check = importlib.util.module_from_spec(_spec_tc)
_spec_tc.loader.exec_module(test_check)


def test_executor_clamps_internal_budgets_under_the_gate_budget(monkeypatch):
    """OCR_CHECK_TIMEOUT_S (the gate's resolved per-check budget) clamps the
    executor's own selector/run budgets so every phase finishes BEFORE the
    gate's kill; without it the knobs and defaults stand unchanged."""
    for var in ("OCR_CHECK_TIMEOUT_S", "OCR_TEST_SELECTOR_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    assert test_check._selector_timeout() == test_check.SELECTOR_TIMEOUT_S
    assert test_check._run_timeout() == test_check.RUN_TIMEOUT_S
    monkeypatch.setenv("OCR_CHECK_TIMEOUT_S", "30")
    # grace = max(1, min(5, 5% of 30)) = 1.5 -> each phase at most 28.5 s.
    assert test_check._selector_timeout() == 28.5
    assert test_check._run_timeout() == 28.5
    monkeypatch.setenv("OCR_TEST_SELECTOR_TIMEOUT", "3")
    assert test_check._selector_timeout() == 3.0   # clamped, never raised
    monkeypatch.setenv("OCR_CHECK_TIMEOUT_S", "600")
    assert test_check._run_timeout() == test_check.RUN_TIMEOUT_S  # knob below
    monkeypatch.setenv("OCR_TEST_SELECTOR_TIMEOUT", "700")
    assert test_check._selector_timeout() == 595.0      # grace = min(5, 30) = 5
    monkeypatch.setenv("OCR_CHECK_TIMEOUT_S", "garbage")
    # An unparseable budget is no budget at all: the knobs stand (the
    # selector knob still wins over the module default).
    assert test_check._selector_timeout() == 700.0
    monkeypatch.delenv("OCR_TEST_SELECTOR_TIMEOUT", raising=False)
    assert test_check._selector_timeout() == test_check.SELECTOR_TIMEOUT_S
    assert test_check._run_timeout() == test_check.RUN_TIMEOUT_S


def test_executor_emits_actual_telemetry_counts(tmp_path):
    """The telemetry object carries ACTUAL counts (SPEC-15.4): one selector
    attempt, the selected tests, authored probes, subject runs (a failure
    plus its re-run), and the classified reproduce result."""
    work = _repo(tmp_path, [
        {"dsl.py": _DSL_OK, "tests/test_dsl.py": _DSL_TEST},
        {"dsl.py": _DSL_BROKEN},
    ])
    cmd, _count = _stub_selector(
        tmp_path, {"existing_tests": ["tests/test_dsl.py"], "probes": []})
    proc = _run_executor(work, ["dsl.py"], _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    assert data["telemetry"] == {
        "selector_attempts": 1, "tests_selected": 1, "probes_authored": 0,
        "runs": 2, "reproduce_results": {"fail": 1, "pass": 0, "flake": 0,
                                         "build": 0, "error": 0}}


def test_executor_telemetry_counts_probes_and_build_errors(tmp_path):
    """An unbuildable probe still counts as authored, run once, and
    classified as a build error -- never a finding."""
    work = _repo(tmp_path, [{"app.py": "x = 1\n"}])
    cmd, _count = _stub_selector(tmp_path, {
        "existing_tests": [],
        "probes": [{"name": "broken", "language": "python",
                    "source": "def broken(:\n    pass\n",
                    "run_cmd": '"%s" {probe_file}' % _PY,
                    "path": "app.py"}]})
    proc = _run_executor(work, ["app.py"], _env(OCR_TEST_SELECTOR_CMD=cmd))
    data = _answer(proc)
    assert data["findings"] == []
    assert data["telemetry"]["probes_authored"] == 1
    assert data["telemetry"]["runs"] == 1
    assert data["telemetry"]["reproduce_results"]["build"] == 1


def test_gate_merges_executor_telemetry_into_command_checks(
        tmp_path, monkeypatch):
    """The gate-side half of the contract: the executor's telemetry object
    reaches _TELE merged into THAT check's command_checks entry (direct
    _run_one_command_check path through _command_check_findings)."""
    import rg_config

    work = _repo(tmp_path, [
        {"dsl.py": _DSL_OK, "tests/test_dsl.py": _DSL_TEST},
        {"dsl.py": _DSL_BROKEN},
    ])
    exe = work / "scripts" / "rg_test_check"
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXECUTOR, exe)
    shutil.copyfile(POLICY, exe.parent / "rg_final_env.py")
    exe.chmod(0o755)
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(work),
                         capture_output=True, text=True, check=True).stdout.strip()
    cmd, _count = _stub_selector(
        tmp_path, {"existing_tests": ["tests/test_dsl.py"], "probes": []})
    monkeypatch.setenv("OCR_TEST_SELECTOR_CMD", cmd)
    monkeypatch.setattr(rg_config, "load_settings", lambda repo_root=None: {
        "checks": {"test-check": {"command": "scripts/rg_test_check",
                                  "globs": ["**/*.py"], "timeout": 120}}})
    review_gate._TELE.pop("command_checks", None)
    findings, warnings = review_gate._command_check_findings(
        str(work), str(work), None, tip,
        entries=[{"path": "dsl.py", "status": "A"}])
    assert findings, "the executor's finding reached the stream"
    tele = review_gate._TELE["command_checks"][-1]
    assert tele["name"] == "test-check", tele
    assert tele["tests_selected"] == 1, tele
    assert tele["selector_attempts"] == 1, tele
    assert tele["runs"] == 2, tele
    assert tele["reproduce_results"]["fail"] == 1, tele
    # The declared per-check budget was exported to the executor, and the
    # executor's clamp honored it (the run finished well inside it).
    assert tele["seconds"] < 120, tele


def test_executor_subject_environment_excludes_selector_and_publication_secrets(
        monkeypatch):
    """Model auth stops at the selector; untrusted tests get no write secrets.

    Non-secret fixture state remains usable, so this boundary does not alter
    the existing reproduce/flake tests' behavior.
    """
    for key, value in {
        "PUBLICATION_SECRET": "do-not-publish",
        "GITLAB_TOKEN": "do-not-write",
        "OPENAI_API_KEY": "selector-only",
        "KIMCHI_BIN": "selector-only-binary",
        "OCR_TEST_SELECTOR_CMD": "selector-only-command",
        "FLAKE_STATE": "/tmp/non-secret-fixture-state",
    }.items():
        monkeypatch.setenv(key, value)

    argv, _cwd, env = test_check._runner_for("tests/test_example.py")

    assert argv
    assert env["FLAKE_STATE"] == "/tmp/non-secret-fixture-state"
    assert env["RG_FINAL_CREDENTIAL_FREE"] == "1"
    for key in ("PUBLICATION_SECRET", "GITLAB_TOKEN", "OPENAI_API_KEY",
                "KIMCHI_BIN", "OCR_TEST_SELECTOR_CMD"):
        assert key not in env


def test_executor_subject_timeout_is_a_warning_without_a_second_run(
        tmp_path, monkeypatch):
    """A timed-out subject is missing evidence, not a reproduced defect."""
    monkeypatch.setattr(test_check, "_run_timeout", lambda: 0.05)
    counts = {"runs": 0,
              "reproduce_results": {"fail": 0, "pass": 0, "flake": 0,
                                    "build": 0, "error": 0}}
    argv = [sys.executable, "-c", "import time; time.sleep(2)"]

    outcome, output = test_check._execute_subject(
        argv, str(tmp_path), "slow test", counts=counts)
    findings, warnings = [], []
    test_check._handle(outcome, output, "existing test", "tests/slow.py",
                       "tests/slow.py", "existing_test", findings, warnings)

    assert outcome == "timeout"
    assert counts["runs"] == 1, "a timeout is not retried as a failure"
    assert counts["timeouts"] == 1
    assert counts["reproduce_results"]["error"] == 1
    assert findings == []
    (warning,) = warnings
    assert "timed out" in warning["message"]
    assert "tests/slow.py" in warning["message"]


# ---------------------------------------------------------------------------
# SPEC-16: deterministic real-race acceptance and opt-in selector diagnostics
# ---------------------------------------------------------------------------


def _run_spec16_dynamic_with_selector(tmp_path, monkeypatch, probe_source,
                                      run_id="race-evidence", run_cmd=None):
    """Run the real acceptance fixture/executor with only inference doubled."""
    import rg_final_dynamic
    import sim_scenarios_spec16 as scenarios

    cmd, _count = _stub_selector(tmp_path, {
        "existing_tests": [],
        "probes": [{
            "name": "direct-race-oracle", "language": "python",
            "source": probe_source,
            "run_cmd": run_cmd or '"%s" {probe_file}' % _PY,
            "path": "dsl.py",
        }],
    })
    real_simulation = scenarios.sim.run_dynamic_simulation
    monkeypatch.setattr(scenarios.sim, "selected_inference_backend",
                        lambda *args, **kwargs: {
                            "name": "kimchi", "reason": "mocked inference",
                            "executable": "/bin/kimchi", "notes": [],
                        })

    def simulated(repository, target_sha, head_sha, **kwargs):
        supplied_runner = kwargs.get("dynamic_runner")

        def runner(*args, **run_kwargs):
            environ = dict(run_kwargs.get("environ") or {})
            environ["OCR_TEST_SELECTOR_CMD"] = cmd
            run_kwargs["environ"] = environ
            execute = supplied_runner or rg_final_dynamic.run_dynamic_checks
            return execute(*args, **run_kwargs)

        kwargs["dynamic_runner"] = runner
        return real_simulation(repository, target_sha, head_sha, **kwargs)

    monkeypatch.setattr(scenarios.sim, "run_dynamic_simulation", simulated)
    call = {
        "scenario": {
            "scenario_id": "spec16-09-ephemeral-probe-attribution-cleanup",
            "dispatch_key": "ephemeral_probe_attribution_cleanup",
        },
        "acceptance_run_id": run_id,
        "context": {"test_tmp_path": str(tmp_path)},
    }
    result = scenarios._real_model_dynamic(call)
    artifact = result["evidence"]["reproduced_failure"]["simulation_artifact"]
    persisted = json.loads(open(artifact["artifact"], encoding="utf-8").read())
    return result, persisted


def test_correct_direct_probe_deterministically_reproduces_planted_race(
        tmp_path, monkeypatch):
    """Two barrier-rendezvoused workers perform 40 logical increments.

    The independent oracle therefore requires 40 on every fresh invocation;
    a deterministic lost-update interleaving observes 20, and the executor
    must execute that real assertion failure twice before blocking.
    """
    probe = (
        "import os, sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(\n"
        "    os.path.abspath(__file__)))))\n"
        "from dsl import verdict\n"
        "expected = 2 * 20\n"
        "observations = [verdict() for _ in range(6)]\n"
        "assert observations == [expected] * len(observations), (\n"
        "    f'lost updates: expected {expected} per independent run, '\n"
        "    f'observed {observations}')\n"
    )
    result, persisted = _run_spec16_dynamic_with_selector(
        tmp_path, monkeypatch, probe)

    assert result["state"] == "findings", result
    finding = result["evidence"]["reproduced_failure"]["findings"][0]
    assert finding["evidence_source"] == "ephemeral_test"
    assert "observed [20, 20, 20, 20, 20, 20]" in finding["content"]
    assert "expected 40" in finding["content"]
    assert persisted["result"]["telemetry"]["runs"] == 2
    assert persisted["result"]["telemetry"]["reproduce_results"] == {
        "fail": 1, "pass": 0, "flake": 0, "build": 0, "error": 0,
    }
    assert persisted["cleanup"]["clean"] is True
    diagnostics = persisted["result"]["selector_diagnostics"]
    assert diagnostics["schema"] == "test-check-selector-diagnostics/v1"
    assert diagnostics["selector"]["valid"] is True
    assert "direct-race-oracle" in diagnostics["selector"]["raw_response"]
    assert '"existing_tests"' in diagnostics["selector"]["json"]
    (probe_record,) = diagnostics["probes"]
    assert "from dsl import verdict" in probe_record["source_excerpt"]
    assert "expected = 2 * 20" in probe_record["source_excerpt"]
    assert probe_record["source_sha256"]
    assert probe_record["run_command"].endswith("probe_test.py")
    assert _PY not in probe_record["run_command"]
    assert probe_record["outcome"] == "finding"
    assert [run["returncode"] for run in probe_record["runs"]] == [1, 1]
    assert all("observed [20, 20, 20, 20, 20, 20]" in run["output"]
               for run in probe_record["runs"])
    assert diagnostics["cleanup"] == {"probe_root_removed": True}
    assert str(tmp_path) not in json.dumps(diagnostics)
    artifact = result["evidence"]["reproduced_failure"]["simulation_artifact"]
    artifact_path = os.path.realpath(artifact["artifact"])
    assert os.stat(artifact_path).st_mode & 0o777 == 0o600
    assert not os.path.exists(os.path.join(
        os.path.dirname(artifact_path), "selector-diagnostics.json"))


def test_diagnostics_redact_complete_credentials_before_simulation_persistence(
        tmp_path, monkeypatch):
    probe = (
        "import os, sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(\n"
        "    os.path.abspath(__file__)))))\n"
        "from dsl import verdict\n"
        "# Authorization: Basic dXNlcjpwYXNz\n"
        "# proxy_authorization=Bearer bearer.token.value\n"
        '# {\"access_token\":\"token with spaces\",\n'
        '#  \"client_secret\":\"client secret with spaces\"}\n'
        "# CLAUDE_TChoices=valid-diagnostic /Users/alice/private/app.py\n"
        "retry = os.path.exists('credential-retry.flag')\n"
        "if retry:\n"
        "    print('Authorization: Basic dXNlcjpwYXNz')\n"
        "    print('proxy_authorization=Bearer bearer.token.value')\n"
        '    print(\'{"access_token":"token with spaces"}\')\n'
        '    print(\'{"client_secret":"client secret with spaces"}\')\n'
        "    print('CLAUDE_TChoices=valid-diagnostic /Users/alice/private/app.py',\n"
        "          file=sys.stderr)\n"
        "else:\n"
        "    open('credential-retry.flag', 'w').close()\n"
        "    print('safe initial dynamic failure')\n"
        "expected = 2 * 20\n"
        "observations = [verdict() for _ in range(6)]\n"
        "assert observations == [expected] * len(observations), (\n"
        "    f'lost updates: expected {expected}, observed {observations}')\n"
    )
    run_cmd = (
        "proxy_authorization='Bearer bearer.token.value' "
        "access_token='token with spaces' "
        "client_secret='client secret with spaces' "
        'CLAUDE_TChoices=valid-diagnostic "%s" {probe_file}' % _PY
    )

    result, persisted = _run_spec16_dynamic_with_selector(
        tmp_path, monkeypatch, probe, run_id="credential-redaction", run_cmd=run_cmd)

    assert result["state"] == "findings", result
    diagnostics = persisted["result"]["selector_diagnostics"]
    assert diagnostics["selector"]["valid"] is True
    (record,) = diagnostics["probes"]
    assert [run["returncode"] for run in record["runs"]] == [1, 1]
    assert "safe initial dynamic failure" in record["runs"][0]["output"]
    scrubbed_fields = (
        diagnostics["selector"]["raw_response"], diagnostics["selector"]["json"],
        record["source_excerpt"], record["run_command"], record["runs"][1]["output"],
    )
    for text in scrubbed_fields:
        assert "[redacted]" in text
    assert "CLAUDE_TChoices=valid-diagnostic" in record["run_command"]
    assert "CLAUDE_TChoices=valid-diagnostic" in record["runs"][1]["output"]

    serialized = json.dumps(persisted)
    for secret in (
            "dXNlcjpwYXNz", "bearer.token.value", "token with spaces",
            "client secret with spaces"):
        assert secret not in serialized
    assert "/Users/alice" not in serialized
    assert "Basic [redacted]" not in serialized
    assert "Bearer [redacted]" not in serialized
    assert str(tmp_path) not in json.dumps(diagnostics)
    artifact = result["evidence"]["reproduced_failure"]["simulation_artifact"]
    assert not os.path.exists(os.path.join(
        os.path.dirname(artifact["artifact"]), "selector-diagnostics.json"))


def test_repeated_print_only_probe_cannot_satisfy_race_scenario(
        tmp_path, monkeypatch):
    """Observing the defective result without an oracle is not test evidence."""
    probe = (
        "import os, sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(\n"
        "    os.path.abspath(__file__)))))\n"
        "from dsl import verdict\n"
        "print('bounded observations:', [verdict() for _ in range(6)])\n"
    )
    result, persisted = _run_spec16_dynamic_with_selector(
        tmp_path, monkeypatch, probe, run_id="smoke-is-not-evidence")

    assert result["state"] == "error"
    assert "did not reproduce" in result["error"]
    assert persisted["result"]["findings"] == []
    assert persisted["result"]["telemetry"]["runs"] == 1
    assert persisted["result"]["telemetry"]["reproduce_results"]["pass"] == 1
    (probe_record,) = persisted["result"]["selector_diagnostics"]["probes"]
    assert probe_record["outcome"] == "pass"
    assert len(probe_record["runs"]) == 1
    assert "bounded observations: [20, 20, 20, 20, 20, 20]" in \
        probe_record["runs"][0]["output"]
    assert persisted["cleanup"]["clean"] is True


def test_selector_prompt_requires_behavioral_oracles_and_bounded_race_evidence():
    prompt = " ".join(test_check._SELECTOR_PROMPT.split())
    for requirement in (
            "directly call the changed public API",
            "independently derived expected value or invariant",
            "never compare an API result with itself",
            "Import-only, print-only, and exit-without-assertion probes are invalid",
            "multiple independent invocations",
            "shared-state and interleaving invariants",
            "bounded observation/counter output",
            "Do not invent concurrency or races outside the changed behavior"):
        assert requirement in prompt


def test_selector_diagnostics_are_opt_in_bounded_sanitized_and_clean(tmp_path):
    """Only the acceptance harness's explicit path enables diagnostics."""
    work = _repo(tmp_path, [{"app.py": "x = 1\n"}])
    secret = "diagnostic-model-secret"
    malformed = ("not-json " + secret + " /home/private-runner/source/app.py "
                 + "x" * 12000)
    cmd, _count = _stub_selector(tmp_path, malformed)
    proc = _run_executor(work, ["app.py"], _env(
        OCR_TEST_SELECTOR_CMD=cmd, OPENAI_API_KEY=secret))
    assert _answer(proc)["warnings"] == [
        {"file": None, "message": "selector failed; no tests selected"}]
    assert not list(work.rglob("*diagnostic*")), "default runs persist no logs"

    capture = tmp_path / "selector-diagnostics.json"
    proc = _run_executor(work, ["app.py"], _env(
        OCR_TEST_SELECTOR_CMD=cmd, OPENAI_API_KEY=secret,
        OCR_TEST_DIAGNOSTICS_FILE=str(capture)))
    assert _answer(proc)["warnings"] == [
        {"file": None, "message": "selector failed; no tests selected"}]
    diagnostics = json.loads(capture.read_text(encoding="utf-8"))
    raw = diagnostics["selector"]["raw_response"]
    assert diagnostics["selector"]["valid"] is False
    assert diagnostics["selector"]["json"] is None
    assert 0 < len(raw) <= 6000
    assert diagnostics["probes"] == []
    assert diagnostics["cleanup"] == {"probe_root_removed": True}
    serialized = json.dumps(diagnostics)
    assert secret not in serialized
    assert "/home/private-runner" not in serialized
    assert str(tmp_path) not in serialized
    assert capture.stat().st_mode & 0o777 == 0o600
    assert not (work / PROBE_ROOT).exists()
