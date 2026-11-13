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
    # Kimchi port (plan D12): gate state and parked reports live in the
    # EXTERNAL store under OCR_STATE_DIR -- the OCR_* filter above would strip
    # it, sending every gate subprocess this test spawns into the developer's
    # REAL ~/.local/state/review-gate (refreshing the gate-dir pointer, parking
    # pending- reports, leaking verdicts). Keep the conftest's private store.
    env["OCR_STATE_DIR"] = os.environ["OCR_STATE_DIR"]
    env.update(extra)
    return env


def _run(argv, cmd, _cwd=None, **env):
    return subprocess.run(argv, input=_payload(cmd, cwd=_cwd), capture_output=True, text=True,
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
def test_sh_adapter_strips_ALL_heredoc_bodies_opened_on_one_line():
    # Parity with _strip_heredocs (scripts/review-gate.py, wave-2 finding):
    # `cmd <<A <<B` opens TWO bodies on one line; the old bash port matched
    # only the FIRST opener, leaving the second body ('cd /evil', a push
    # shape) in the scanned text to be misread as code. Bodies are data.
    cmd = "cat <<A <<B\ncd /evil\ngit push --all\nA\ncd /evil\ngit push --all\nB\necho done"
    _assert_passes(_run([_BASH, _SH], cmd))
    # The single-opener behavior the loop replaced still holds.
    _assert_passes(_run([_BASH, _SH], "cat <<EOF\ncd /evil\ngit push --all\nEOF"))


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
    _assert_passes(_run([_BASH, _SH], "git push origin main", _cwd=str(repo)))
    # Neither does a non-repo cwd.
    _assert_passes(_run([_BASH, _SH], "git push origin main", _cwd=str(tmp_path / "nope")))


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


# --- SPEC-04: the repo guard must resolve hooks the way git does -------------

# husky 9.1.7's `h` script, verbatim (node_modules/husky): the shim it writes
# is `#!/usr/bin/env sh\n. "$(dirname "$0")/h"` and h runs the user hook at
# "$(dirname "$(dirname "$0")")/$(basename "$0")".
_HUSKY_H = '''#!/usr/bin/env sh
[ "$HUSKY" = "2" ] && set -x
n=$(basename "$0")
s=$(dirname "$(dirname "$0")")/$n

[ ! -f "$s" ] && exit 0

if [ -f "$HOME/.huskyrc" ]; then
	echo "husky - '~/.huskyrc' is DEPRECATED, please move your code to ~/.config/husky/init.sh"
fi
i="${XDG_CONFIG_HOME:-$HOME/.config}/husky/init.sh"
[ -f "$i" ] && . "$i"

[ "${HUSKY-}" = "0" ] && exit 0

export PATH="node_modules/.bin:$PATH"
sh -e "$s" "$@"
c=$?

[ $c != 0 ] && echo "husky - $n script failed (code $c)"
[ $c = 127 ] && echo "husky - command not found in PATH=$PATH"
exit $c
'''


def _mkcommitted_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=str(repo), check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=str(repo), check=True)
    return repo


def _add_worktree(repo, tmp_path, name="wt"):
    wt = tmp_path / name
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "wt-branch"],
                   cwd=str(repo), check=True)
    return wt


def _husky_gated_repo(tmp_path, name="repo"):
    """A repo gated through the husky 9 shim chain, exactly as husky lays it
    out: core.hooksPath=.husky/_, .husky/_/pre-push sources ./h, h runs
    .husky/pre-push (which is where the gate was installed)."""
    repo = _mkcommitted_repo(tmp_path, name)
    shim = repo / ".husky" / "_"
    shim.mkdir(parents=True)
    (shim / "pre-push").write_text('#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n')
    (shim / "h").write_text(_HUSKY_H)
    (repo / ".husky" / "pre-push").write_text("# review-gate\n")
    subprocess.run(["git", "config", "core.hooksPath", ".husky/_"],
                   cwd=str(repo), check=True)
    # The husky files must be COMMITTED before any linked worktree is cut:
    # a worktree checks out the committed tree, so uncommitted .husky content
    # would be missing there and the chain could not resolve (SPEC-04).
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "husky chain"], cwd=str(repo), check=True)
    return repo


def _push_from(cwd):
    return subprocess.run(
        [_BASH, _SH], input=_payload("git push origin main", cwd=str(cwd)),
        capture_output=True, text=True, env=_env(), timeout=60,
    )


@needs_bash
def test_sh_adapter_gates_a_push_behind_a_husky_shim_chain(tmp_path):
    # The gate lives in .husky/pre-push, reachable only THROUGH the husky
    # shim: the entry hook (.husky/_/pre-push) mentions nothing, so the guard
    # must follow the chain before declaring the repo inert.
    repo = _husky_gated_repo(tmp_path)
    proc = _push_from(repo)
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (proc.stdout, proc.stderr)
    assert "review-gate" in reason


@needs_bash
def test_sh_adapter_gates_a_push_from_a_linked_worktree_of_a_gated_repo(tmp_path):
    # THE regression: a linked worktree's gitdir is .git/worktrees/<name>, so
    # the old <gitdir>/hooks/pre-push check never saw the gate installed in
    # the COMMON .git/hooks and the hook stayed inert on every push from a
    # worktree. git's own --git-path hooks resolution finds the common dir.
    repo = _gated_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c",
                    "user.name=t", "commit", "-q", "-m", "base"],
                   cwd=str(repo), check=True)
    wt = _add_worktree(repo, tmp_path)
    proc = _push_from(wt)
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (proc.stdout, proc.stderr)
    assert "review-gate" in reason


@needs_bash
def test_sh_adapter_gates_a_push_from_a_linked_worktree_of_a_husky_repo(tmp_path):
    # core.hooksPath is repo-local config, so worktrees share the husky
    # chain; the guard must stay armed there too.
    repo = _husky_gated_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path)
    proc = _push_from(wt)
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (proc.stdout, proc.stderr)
    assert "review-gate" in reason


def _installer_gated_husky_repo(tmp_path, name="repo"):
    """The exact 2026-11-10 repository shape, laid out the way the INSTALLER
    leaves a husky repo: the husky shim that was at .husky/_/pre-push is
    preserved as .husky/_/pre-push.user and the installer-written gate
    (hooks/git/pre-push, SCR_DIR stamped) now occupies .husky/_/pre-push,
    chaining into pre-push.user -- which is the husky shim that sources ./h
    and runs .husky/pre-push. The gate is therefore reachable only as
    gate -> husky shim -> h -> user hook, from core.hooksPath=.husky/_."""
    repo = _mkcommitted_repo(tmp_path, name)
    shim = repo / ".husky" / "_"
    shim.mkdir(parents=True)
    template = (os.path.join(os.path.dirname(_SCRIPTS), "hooks", "git", "pre-push"))
    stamped = open(template, encoding="utf-8").read()
    stamped = stamped.replace("__SCR_DIR__", _SCRIPTS).replace("__SCR_VERSION__", "test")
    (shim / "pre-push").write_text(stamped)
    os.chmod(str(shim / "pre-push"), 0o755)
    (shim / "pre-push.user").write_text('#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n')
    os.chmod(str(shim / "pre-push.user"), 0o755)
    (shim / "h").write_text(_HUSKY_H)
    # The user's own hook behind the whole chain: some foreign tool, never
    # the gate (the installer did not touch it).
    (repo / ".husky" / "pre-push").write_text("#!/bin/sh\nnpx lint-staged\n")
    subprocess.run(["git", "config", "core.hooksPath", ".husky/_"],
                   cwd=str(repo), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "installer chain"], cwd=str(repo), check=True)
    return repo


@needs_bash
def test_sh_adapter_gates_a_push_from_a_linked_worktree_of_the_2026_11_10_installer_shape(tmp_path):
    # THE blind spot, locked shut: a linked worktree of a husky repo where
    # the gate was installed BY THE INSTALLER -- .husky/_/pre-push is the
    # gate's own pre-push (stamped with SCR_DIR), the husky shim it displaced
    # lives on as pre-push.user and chains back through h to .husky/pre-push.
    # The desktop bash hook is fed a payload whose cwd is the linked worktree
    # (its gitdir is .git/worktrees/<name>, so the old <gitdir>/hooks check
    # went inert here). The guard must resolve hooks the way git does, see a
    # gated repository, and hand the push to review-gate.py -- which starts a
    # review and answers with the review-start block.
    repo = _installer_gated_husky_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path)
    proc = _push_from(wt)
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (proc.stdout, proc.stderr)
    assert "review-gate" in reason
    # The review-start shape (the decision a first push gets): the gate names
    # the hook-mode retry contract instead of waving the push through.
    assert "re-run this exact `git push` command" in reason, reason
    assert "was NOT executed" in reason, reason


@needs_bash
def test_sh_adapter_runs_the_stub_reviewer_from_a_linked_worktree_of_the_installer_shape(tmp_path):
    # Same shape, but with the reviewer seam pointed at the stub: the gate
    # review-gate.py the guard exec'd must actually run a review (the stub
    # traces the call) and the decision must stay a block.
    repo = _installer_gated_husky_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path)
    trace = tmp_path / "stub.trace"
    env = {
        "OCR_REVIEWER_CMD": f'"{sys.executable}" "'
                            f'{os.path.join(_ROOT, "tests", "stub_reviewer.py")}"',
        "STUB_TRACE": str(trace),
        "STUB_VERDICT": "block",
        "OCR_INLINE_BUDGET": "30",
    }
    argv = [_BASH, _SH]
    proc = subprocess.run(argv, input=_payload("git push origin main", cwd=str(wt)),
                          capture_output=True, text=True, env=_env(**env), timeout=120)
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (proc.stdout, proc.stderr)
    assert "review-gate" in reason
    assert trace.is_file() and trace.read_text().strip(), \
        "the gate exec'd from the worktree must run the reviewer"


@needs_bash
def test_sh_adapter_stays_inert_for_a_push_from_a_linked_worktree_of_an_un_gated_repo(tmp_path):
    # The mirror case: a linked worktree of a repo that never installed the
    # gate (no hooks at all) must stay a silent pass-through -- chain
    # following must arm the guard only where a gate actually exists.
    repo = _mkcommitted_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path)
    _assert_passes(_push_from(wt))


@needs_bash
def test_sh_adapter_gates_when_the_entry_hook_chains_to_the_gate(tmp_path):
    # Chaining shape the installer itself produces (and the spec calls out):
    # the entry pre-push execs a sibling script, and THAT script is the gate.
    repo = _mkcommitted_repo(tmp_path)
    hooks = repo / ".githooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text('#!/bin/sh\nexec "$(dirname "$0")/pre-push.user"\n')
    (hooks / "pre-push.user").write_text("#!/bin/sh\n# review-gate\n")
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"],
                   cwd=str(repo), check=True)
    proc = _push_from(repo)
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (proc.stdout, proc.stderr)
    assert "review-gate" in reason


@needs_bash
def test_sh_adapter_gates_a_push_via_an_absolute_hooks_path(tmp_path):
    # --git-path hooks must hand back the core.hooksPath dir verbatim.
    repo = _mkcommitted_repo(tmp_path)
    hooks = tmp_path / "abs-hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("# review-gate\n")
    subprocess.run(["git", "config", "core.hooksPath", str(hooks)],
                   cwd=str(repo), check=True)
    proc = _push_from(repo)
    assert proc.returncode == 0, proc.stderr
    decision, _ = parse_pretooluse(proc.stdout)
    assert decision == "deny", proc.stdout


@needs_bash
def test_sh_adapter_stays_inert_when_the_hook_chain_lacks_the_gate(tmp_path):
    # Chain-following must not over-gate: a foreign hook manager (chained,
    # like the installer's own shim) whose chain never mentions review-gate
    # leaves the bash hook inert.
    repo = _mkcommitted_repo(tmp_path)
    hooks = repo / ".githooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text('#!/bin/sh\nexec "$(dirname "$0")/pre-push.user"\n')
    (hooks / "pre-push.user").write_text("#!/bin/sh\nexit 0\n")
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"],
                   cwd=str(repo), check=True)
    _assert_passes(_push_from(repo))
    # Same for a husky-shaped chain whose user hook is some other tool.
    repo2 = _husky_gated_repo(tmp_path, name="repo2")
    (repo2 / ".husky" / "pre-push").write_text("#!/bin/sh\nnpx lint-staged\n")
    _assert_passes(_push_from(repo2))


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


# --- review-gate findings: comment and eval handling in the tokenizer ---------

@needs_bash
def test_sh_adapter_classifies_a_push_on_the_line_after_a_comment(tmp_path):
    # '#'-comment handling used to BREAK OUT of the tokenizer instead of
    # skipping to the newline, truncating a multi-line call at the '#', so
    # `git status # note\ngit push origin main' never classified (or gated)
    # the push on the following line. Shell comments end at the newline.
    repo = _gated_repo(tmp_path)
    proc = subprocess.run(
        [_BASH, _SH], input=_payload("git status # note\ngit push origin main", cwd=str(repo)),
        capture_output=True, text=True, env=_env(), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (proc.stdout, proc.stderr)
    assert "review-gate" in reason
    # Same for the push-shaped short path (blocks before any repo lookup).
    proc = _run([_BASH, _SH], "git status # note\ngit push --all")
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny"
    assert "could not parse push command" in reason


@needs_bash
def test_sh_adapter_never_classifies_the_comment_text_itself():
    # The mirror guarantee: text after '#' (to the end of the line) is data.
    _assert_passes(_run([_BASH, _SH], "git status # git push origin main"))
    _assert_passes(_run([_BASH, _SH], "git status # note\ngit status # git push origin main"))


@needs_bash
def test_sh_adapter_routes_a_quoted_eval_push_to_the_gate(tmp_path):
    # The old `case " $_INTERPRETERS " in ... eval)' arm was UNREACHABLE (the
    # case subject is the interpreter LIST, never the word `eval'), so even
    # the quoted single-argument form slipped past the recursion. Now a gated
    # repo must deny it -- review-gate.py's own strict parser positively
    # parses this quoted hand-off.
    repo = _gated_repo(tmp_path)
    proc = subprocess.run(
        [_BASH, _SH], input=_payload("eval 'git push origin main'", cwd=str(repo)),
        capture_output=True, text=True, env=_env(), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny", (proc.stdout, proc.stderr)
    assert "review-gate" in reason


@needs_bash
def test_sh_adapter_sees_the_push_shape_through_an_unquoted_eval():
    # The unquoted multi-word eval form: eval joins ALL its arguments with
    # spaces, so the recursion must re-classify `git push --all' as a whole.
    # (Routing end-to-end here can't be asserted -- review-gate.py's strict
    # parser owns the quoted comparison -- so the shape and count tests below
    # carry the unquoted-form regression.)
    proc = _run([_BASH, _SH], "eval git push --all")
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny"
    assert "could not parse push command" in reason


@needs_bash
def test_sh_adapter_counts_each_eval_push_exactly_once(tmp_path):
    # Two eval pushes => the multi-push block. A single eval push must count
    # EXACTLY once: in an un-gated repo it passes through, whereas a double
    # count (re-scanning the joined words a second time) would block there.
    proc = _run([_BASH, _SH], "eval git push origin main; eval 'git push origin main'")
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision == "deny"
    assert "more than one `git push`" in reason
    repo = _mkcommitted_repo(tmp_path)   # never installed the gate: the hook is inert
    _assert_passes(_run([_BASH, _SH], "eval git push origin main", _cwd=str(repo)))


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
