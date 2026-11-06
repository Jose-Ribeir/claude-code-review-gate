"""The hook adapters must never grant permission (Kimchi port).

A hook answering {"decision":"allow"} auto-approves the tool call and bypasses
the user's own permission prompts. The Kimchi bash hook fires on every bash
call, so an allow on its non-push paths approved arbitrary commands. Every
non-blocking outcome is a pass-through (exit 0, no output) and only
{"decision":"block"} is ever emitted.

Ported from test_hook_passthrough.py (claude-code-review-gate): the Claude
adapters are replaced by hooks/sh/gate-hook.sh, and the parser-inversion cases
(D14) are new.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from hook_output import parse_pretooluse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_SH = os.path.join(_ROOT, "hooks", "sh", "gate-hook.sh")

_BASH = shutil.which("bash")


def _payload(cmd, cwd=None):
    return json.dumps({"tool_name": "bash", "input": {"command": cmd}, "cwd": cwd or _ROOT})


def _env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    env.update(extra)
    return env


def _run(argv, cmd, **env):
    return subprocess.run(argv, input=_payload(cmd), capture_output=True, text=True,
                          env=_env(**env), timeout=120)


def _assert_passes(proc):
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout
    assert "permissionDecision" not in proc.stdout


def test_no_script_can_emit_an_allow_decision():
    pat = re.compile(r'''"decision"["']?\s*:\s*["']allow"''')
    for name in ("gate-hook.sh", "review-gate.py"):
        text = open(os.path.join(_ROOT, "hooks", "sh", name) if name.endswith(".sh")
                    else os.path.join(_SCRIPTS, name), encoding="utf-8").read()
        hits = [ln for ln in text.splitlines() if pat.search(ln)]
        assert not hits, (name, hits)


def test_the_parser_helper_rejects_an_allow():
    allow = json.dumps({"decision": "allow", "reason": "x"})
    with pytest.raises(AssertionError):
        parse_pretooluse(allow)
    assert parse_pretooluse("")[0] == "pass"


# --- review-gate.py (hook mode, driven through the Kimchi payload shape) ------

def test_core_passes_through_a_command_that_is_not_a_push():
    _assert_passes(_run([sys.executable, _GATE, "--mode", "hook"], "ls -la"))
    _assert_passes(_run([sys.executable, _GATE, "--mode", "hook"], "echo git push"))


def test_core_guard_passes_through_a_safe_command_and_still_denies_a_write():
    argv = [sys.executable, _GATE, "--mode", "hook"]
    _assert_passes(_run(argv, "git diff HEAD~1", OCR_IN_REVIEW="1"))
    proc = _run(argv, "git diff --output=x.txt HEAD~1", OCR_IN_REVIEW="1")
    assert parse_pretooluse(proc.stdout)[0] == "deny"


def test_core_accepts_the_kimchi_input_payload_shape():
    # Kimchi's own payload puts the command at input.command; the gate must
    # read it (the bash adapter re-wraps, but the core accepts both).
    env = _env(OCR_IN_REVIEW="1")
    payload = json.dumps({"tool_name": "bash", "input": {"command": "git diff HEAD~1"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, env=env, timeout=60)
    _assert_passes(proc)


# --- hooks/sh/gate-hook.sh (the Kimchi UX layer) ------------------------------

needs_bash = pytest.mark.skipif(not _BASH, reason="bash not available")


@needs_bash
def test_sh_adapter_passes_through_a_non_push():
    _assert_passes(_run([_BASH, _SH], "rm -rf /tmp/whatever"))
    _assert_passes(_run([_BASH, _SH], "git status"))
    _assert_passes(_run([_BASH, _SH], "echo git push"))


@needs_bash
def test_sh_adapter_passes_through_inside_the_review_and_still_vets_writes():
    _assert_passes(_run([_BASH, _SH], "git diff HEAD~1", OCR_IN_REVIEW="1"))
    proc = _run([_BASH, _SH], "git diff --output=x.txt HEAD~1", OCR_IN_REVIEW="1")
    assert parse_pretooluse(proc.stdout)[0] == "deny"


@needs_bash
def test_sh_adapter_passes_through_a_command_embedding_a_quoted_push_string():
    # The regression the port is built around: a NON-push command whose
    # argument contains the literal quoted string "git push origin main" must
    # NOT block (Kimchi fires this hook again on the real inner segment).
    _assert_passes(_run([_BASH, _SH], 'echo "git push origin main"'))
    _assert_passes(_run([_BASH, _SH], 'git log --grep="git push origin main"'))
    # Data in a heredoc body is never code either.
    _assert_passes(_run([_BASH, _SH], "cat <<EOF\ngit push origin main\nEOF"))


@needs_bash
@pytest.mark.parametrize("cmd", [
    "git push --all",
    "git push --mirror",
    "git push --tags",
    "git push",
    "git push origin",
    "git -c k=v push",
    "VAR=x git push",
    "git push origin $BRANCH",
])
def test_sh_adapter_blocks_push_shapes_it_cannot_parse(cmd):
    proc = _run([_BASH, _SH], cmd)
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", cmd
    assert "could not parse push command" in reason, reason
    assert "explicit refspec" in reason


@needs_bash
def test_sh_adapter_blocks_two_pushes_in_one_command():
    proc = _run([_BASH, _SH], "git push origin main; git push origin main")
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny"
    assert "more than one `git push`" in reason


@needs_bash
def test_sh_adapter_routes_a_parsed_push_to_the_gate(tmp_path):
    # A gated repo (our pre-push installed): a parsed push reaches
    # review-gate.py --mode hook, which denies (no verdict yet) with the
    # Kimchi envelope.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text("# review-gate\n")
    proc = subprocess.run(
        [_BASH, _SH], input=_payload("git push origin main", cwd=str(repo)),
        capture_output=True, text=True, env=_env(), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny"
    assert "review-gate" in reason


@needs_bash
def test_sh_adapter_is_inert_outside_gated_repositories(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    # A repo WITHOUT our pre-push: a push command passes through untouched.
    _assert_passes(_run([_BASH, _SH], "git push origin main", cwd=str(repo)))
    # Neither does a non-repo cwd.
    _assert_passes(_run([_BASH, _SH], "git push origin main", cwd=str(tmp_path / "nope")))


@needs_bash
def test_sh_adapter_fails_closed_when_the_gate_is_missing_and_fail_open_passes(tmp_path):
    # A GATED repo (our pre-push installed) but a broken gate dir: deny.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text("# review-gate\n")
    broken = tmp_path / "gate-hook.sh"
    shutil.copy(_SH, broken)  # no review-gate.py resolvable: a broken install

    def run_broken(fail_open=False):
        env = _env(OCR_GATE_DIR=str(tmp_path))
        if fail_open:
            env["OCR_FAIL_OPEN"] = "1"
        return subprocess.run(
            [_BASH, str(broken)], input=_payload("git push origin main", cwd=str(repo)),
            capture_output=True, text=True, env=env, timeout=60)

    proc = run_broken()
    assert parse_pretooluse(proc.stdout)[0] == "deny", (proc.stdout, proc.stderr)
    # OCR_FAIL_OPEN lets the push go; it must not APPROVE it.
    proc = run_broken(fail_open=True)
    assert proc.returncode == 0 and parse_pretooluse(proc.stdout)[0] == "pass"


# --- A1/A2 spoof shapes: a gated repo must still DENY them --------------------
# Proven live during the port: these shapes used to fail OPEN. Each is driven
# through the same gated-repo setup as the routing test above.

def _gated_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text("# review-gate\n")
    return repo


@needs_bash
@pytest.mark.parametrize("cmd", [
    'git pu""sh origin main',              # tokenizer must not flush on quote-close
    "git pu''sh origin main",
    'git "pu"sh origin main',
    'bash -c "git push origin main"',     # recursion into the interpreter arg
    "sh -c 'git push origin main'",
    "env OCR_IN_REVIEW=1 git push origin main",   # env(1) prefix: still a push
    "env -u X OCR_IN_REVIEW=1 git push origin main",
])
def test_sh_adapter_denies_quote_splice_and_env_preflex_spoof_shapes(tmp_path, cmd):
    repo = _gated_repo(tmp_path)
    proc = subprocess.run(
        [_BASH, _SH], input=_payload(cmd, cwd=str(repo)),
        capture_output=True, text=True, env=_env(), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (cmd, proc.stdout)


@needs_bash
def test_sh_adapter_blocks_send_pack_as_plumbing(tmp_path):
    proc = _run([_BASH, _SH], "git send-pack ../remote.git main")
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny"
    assert "send-pack" in reason


def test_core_does_not_self_disable_on_a_spoofed_in_review_env(tmp_path):
    # OCR_IN_REVIEW in the PROCESS environment (what a spoofing shell gives
    # the pre-push hook) must not disable the push gate in hook mode: the
    # push payload is still denied; only non-push calls route to the guard.
    repo = _gated_repo(tmp_path)
    argv = [sys.executable, _GATE, "--mode", "hook"]
    payload = json.dumps({"tool_name": "bash", "input": {"command": "git push origin main"},
                          "cwd": str(repo)})
    proc = subprocess.run(argv, input=payload, capture_output=True, text=True,
                          env=_env(OCR_IN_REVIEW="1"), timeout=60)
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", proc.stdout
    # And the guard routing for non-push commands is preserved.
    _assert_passes(_run(argv, "git diff HEAD~1", OCR_IN_REVIEW="1"))
