"""Shared pytest setup (Kimchi port).

Every run must point the gate's EXTERNAL state store at a private temp dir:
the store lives at ~/.local/state/review-gate/ in production (D12 of the port
plan) and a test that runs the gate without the override would write into the
developer's real store. The plugin-data override is kept for the upstream
tests that still set it.
"""
import os

import pytest


def scrub_git_trace(env):
    """A copy of `env` with every GIT_TRACE* key removed.

    Kimchi Studio's git-attribution feature injects GIT_TRACE* env vars
    (GIT_TRACE_REFS, GIT_TRACE2_EVENT, ...) pointing at per-session temp
    dirs that are deleted when the session ends. Any git run inheriting
    them fails with exit 128 as soon as it tries to write its trace target
    (verified live: `git clone --no-hardlinks` in the supervisor dies this
    way). Every test subprocess that runs git must get a scrubbed env.
    """
    return {k: v for k, v in env.items() if not k.startswith("GIT_TRACE")}


def minimal_path(*front):
    """A PATH that holds git (and any `front` dirs) but no reviewer CLI.

    The tests that need "no kimchi/claude on PATH" used the literal
    "/usr/bin:/bin"; Windows has neither directory, and with them git itself
    disappears (the gate then fails earlier, for the wrong reason). There the
    base is the directory git.exe lives in."""
    import shutil
    import sys
    base = "/usr/bin:/bin"
    if sys.platform == "win32":
        git = shutil.which("git")
        base = os.path.dirname(git) if git else ""
    return os.pathsep.join([str(p) for p in front] + [base]) if base else \
        os.pathsep.join(str(p) for p in front)


def _git_sh():
    """Path of Git for Windows' own sh.exe (None when not found)."""
    import shutil
    for name in ("sh", "bash"):
        found = shutil.which(name)
        if found:
            return found
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(git))   # ...\\Git\\cmd|mingw64\\bin -> ...\\Git
        for rel in (("usr", "bin", "sh.exe"), ("..", "usr", "bin", "sh.exe")):
            cand = os.path.normpath(os.path.join(root, *rel))
            if os.path.isfile(cand):
                return cand
    return None


def write_fake_cli(bindir, name, sh_body):
    """An executable named `name` in `bindir` that runs the POSIX-sh `sh_body`.

    POSIX: the script itself (mode 0755). Windows has no shebangs and no
    executable bit: the body goes to `name.sh` and an inert `name.cmd` marker
    makes shutil.which(name) find it (PATHEXT). The marker is never executed:
    cmd.exe would split an argument such as `search?scope=blobs&search=x` at the
    `&`, and CreateProcess of a bare name finds only `.exe`; instead the
    `_fake_cli_shims_on_windows` fixture below makes ocr_forge run the `.sh`
    with Git for Windows' sh.exe. Returns the path made."""
    import sys
    os.makedirs(str(bindir), exist_ok=True)
    if sys.platform != "win32":
        path = os.path.join(str(bindir), name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(sh_body)
        os.chmod(path, 0o755)
        return path
    with open(os.path.join(str(bindir), name + ".sh"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(sh_body.replace("/bin/sleep", "sleep"))
    marker = os.path.join(str(bindir), name + ".cmd")
    with open(marker, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write("@exit /b 0\n")
    return marker


def force_rmtree(path):
    """shutil.rmtree that also removes git's read-only object files (Windows
    refuses to unlink a read-only file, where POSIX only checks the directory)."""
    import shutil
    import stat

    def _chmod_and_retry(func, target, exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(str(path), onexc=_chmod_and_retry)


def posix_path(path):
    """`path` with forward slashes: safe to embed in a sh script on Windows."""
    return str(path).replace("\\", "/")


@pytest.fixture(autouse=True)
def _fake_cli_shims_on_windows(monkeypatch):
    """Windows only: let ocr_forge "run" the fake gh/glab that write_fake_cli
    made. Its subprocess.run is wrapped (module attribute only; the real
    subprocess module is untouched) so a bare `gh` that PATH resolves to
    `gh.cmd` with a sibling `gh.sh` runs as `sh gh.sh args...`."""
    import shutil
    import subprocess
    import sys
    import types
    forge = sys.modules.get("ocr_forge")
    if sys.platform != "win32" or forge is None:
        return
    real = subprocess

    def run(argv, *args, **kw):
        if isinstance(argv, (list, tuple)) and argv and os.sep not in str(argv[0]):
            exe = shutil.which(str(argv[0]))
            if exe and exe.lower().endswith(".cmd") and os.path.isfile(exe[:-4] + ".sh"):
                argv = [_git_sh() or "sh", exe[:-4] + ".sh"] + list(argv[1:])
        return real.run(argv, *args, **kw)

    proxy = types.SimpleNamespace(**{k: getattr(real, k) for k in dir(real)
                                     if not k.startswith("__")})
    proxy.run = run
    monkeypatch.setattr(forge, "subprocess", proxy)


@pytest.fixture(autouse=True)
def _private_state_store(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path_factory.mktemp("rg-state")))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path_factory.mktemp("rg-data")))
    # The autouse half of the GIT_TRACE* scrub: the helpers below copy
    # os.environ, so scrubbing the live environment covers every subprocess
    # a test spawns; the helpers call scrub_git_trace() again explicitly.
    for k in [x for x in os.environ if x.startswith("GIT_TRACE")]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _kimchi_backend_unless_a_test_selects_one(monkeypatch):
    """Backend selection (scripts/ocr_profile.py) auto-detects from the
    developer's own harness: CLAUDECODE inside Claude Code, or whichever backend
    CLI happens to be the only one on PATH. Left alone, the same suite would
    review with claude on one machine and kimchi on another. Pin the historical
    default for every test; a test about selection removes or overrides this
    (test_backend_select.py, test_claude_profile.py)."""
    monkeypatch.setenv("OCR_BACKEND", "kimchi")
    monkeypatch.delenv("OCR_HARNESS", raising=False)
    # ...and the harness signals themselves, so a test about the environment
    # sets exactly the ones it means (the suite often runs inside Claude Code).
    for k in [x for x in os.environ if x == "CLAUDECODE" or x.startswith("CLAUDE_CODE_")]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    """Environment for an in-process supervisor run against the stub reviewer
    (tests/kimi_stub.py via OCR_STUB_WORKER=1): a private store, no inherited
    OCR_*/STUB_* settings. Returns the monkeypatch, so a test can also lower
    the gate module's limits."""
    import os
    for k in list(os.environ):
        if k.startswith(("OCR_", "STUB_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "gate-state"))
    monkeypatch.setenv("OCR_STUB_WORKER", "1")
    monkeypatch.setenv("STUB_TRACE", str(tmp_path / "stub.trace"))
    monkeypatch.setenv("OCR_INLINE_BUDGET", "3")
    return monkeypatch
