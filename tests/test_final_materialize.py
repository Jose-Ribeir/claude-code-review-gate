"""SPEC-16 immutable final-review materialization, through its public seam."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import sys
from pathlib import Path
import threading

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import rg_store  # noqa: E402
import rg_final_materialize as materialization  # noqa: E402
from rg_final_materialize import MaterializationError, materialize_review  # noqa: E402


def _git(cwd, *args, input_bytes=None):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), input=input_bytes, capture_output=True,
        check=True, timeout=30,
    ).stdout


def _text(cwd, *args):
    return _git(cwd, *args).decode("utf-8").strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _text(repo, "rev-parse", "HEAD")


def _repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Final Review Test"),
                       ("user.email", "final@example.test"),
                       ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "ADR.md").write_text("unchanged decision\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "target.txt").write_text("target only\n", encoding="utf-8")
    target = _commit(repo, "target")
    _git(repo, "checkout", "-q", "-b", "feature", base)
    (repo / "README.md").write_text("feature one\n", encoding="utf-8")
    _commit(repo, "feature one")
    (repo / "feature.txt").write_text("feature two\n", encoding="utf-8")
    head = _commit(repo, "feature two")
    _git(repo, "checkout", "-q", "main")
    return repo, base, target, head


def _credential_helper(tmp_path):
    marker = tmp_path / "credential-helper-invoked"
    helper = tmp_path / "malicious-credential-helper"
    helper.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('credentials recovered', encoding='utf-8')\n"
        "print('username=attacker')\n"
        "print('password=recovered-secret')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    return helper, marker


@contextmanager
def _authentication_required_server():
    class RejectAuthentication(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="materialization-test"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RejectAuthentication)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/repository.git"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _isolate_global_git_config(monkeypatch, tmp_path, helper=None):
    config = tmp_path / "global.gitconfig"
    value = f"[credential]\n\thelper = {helper}\n" if helper else ""
    config.write_text(value, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


def test_git_invocation_has_explicit_credential_and_prompt_policy(tmp_path, monkeypatch):
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(materialization.subprocess, "run", record)
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-cross")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross-either")
    materialization._git(tmp_path, "fetch", "origin", "0" * 40)

    command, kwargs = calls[0]
    assert command[:7] == [
        "git", "-c", "credential.helper=", "-c", "core.askPass=" + os.devnull,
        "-c", "credential.interactive=false",
    ]
    assert command[7:] == ["fetch", "origin", "0" * 40]
    assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["env"]["GIT_ASKPASS"] == os.devnull
    assert kwargs["env"]["SSH_ASKPASS"] == os.devnull
    assert kwargs["env"]["GIT_SSH_COMMAND"] == "ssh -oBatchMode=yes"
    assert kwargs["env"]["SSH_ASKPASS_REQUIRE"] == "never"
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in kwargs["env"]


def test_clone_cannot_recover_credentials_from_global_environment(
        tmp_path, monkeypatch):
    helper, marker = _credential_helper(tmp_path)
    _isolate_global_git_config(monkeypatch, tmp_path, helper)
    with _authentication_required_server() as url:
        with pytest.raises(MaterializationError, match="clone"):
            with materialize_review(url, "0" * 40, "1" * 40):
                pass

    assert not marker.exists(), "an inherited global credential helper was executed"


@pytest.mark.parametrize("checkout", ["repository", "submodule"])
def test_local_checkout_config_cannot_restore_credential_helpers(
        tmp_path, monkeypatch, checkout):
    helper, marker = _credential_helper(tmp_path)
    _isolate_global_git_config(monkeypatch, tmp_path)
    repo = _leaf_repository(tmp_path / checkout, "content.txt", "content\n")
    if checkout == "submodule":
        parent = _leaf_repository(tmp_path / "parent", "parent.txt", "parent\n")
        _git(parent, "-c", "protocol.file.allow=always", "submodule", "add",
             str(repo), "vendor/child")
        repo = parent / "vendor/child"
    _git(repo, "config", "credential.helper", str(helper))
    with _authentication_required_server() as url:
        _git(repo, "config", f"credential.{url}.helper", str(helper))
        with pytest.raises(MaterializationError, match="fetch"):
            materialization._git(repo, "fetch", "--no-tags", url)

    assert not marker.exists(), f"{checkout}-local credential helper was executed"


def test_materializes_only_pinned_tree_and_complete_merge_base_diff(tmp_path):
    repo, base, target, head = _repository(tmp_path)
    (repo / "local-only.txt").write_text("uncommitted secret\n", encoding="utf-8")
    (repo / "README.md").write_text("local edit\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "local.js").write_text("local dependency\n", encoding="utf-8")

    with materialize_review(repo, target, head) as review:
        assert review.path.is_dir()
        assert review.target_sha == target
        assert review.head_sha == head
        assert review.merge_base_sha == base
        assert (review.path / "README.md").read_text(encoding="utf-8") == "feature one\n"
        assert (review.path / "docs" / "ADR.md").read_text(encoding="utf-8") == \
            "unchanged decision\n"
        assert not (review.path / "local-only.txt").exists()
        assert not (review.path / "node_modules").exists()
        assert "feature two" in review.diff
        assert "feature one" in review.diff
        assert "target only" not in review.diff
        assert _text(review.path, "status", "--porcelain") == ""
        materialized_path = review.path

    assert not materialized_path.exists()
    assert (repo / "local-only.txt").read_text(encoding="utf-8") == "uncommitted secret\n"
    assert (repo / "README.md").read_text(encoding="utf-8") == "local edit\n"


def test_materializes_target_from_a_separate_repository_and_cleans_failures(tmp_path):
    upstream, base, target, _head = _repository(tmp_path / "upstream")
    source = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", str(upstream), str(source))
    _git(source, "config", "user.name", "Fork Test")
    _git(source, "config", "user.email", "fork@example.test")
    _git(source, "checkout", "-q", "-b", "fork-feature", base)
    (source / "fork.txt").write_text("fork change\n", encoding="utf-8")
    head = _commit(source, "fork change")
    # Make the target commit unavailable from the source clone, as it is for a
    # fork whose upstream moved after the fork was synchronized.
    (upstream / "new-target.txt").write_text("new target\n", encoding="utf-8")
    new_target = _commit(upstream, "new target")

    with materialize_review(
            source, new_target, head, target_repository=upstream) as review:
        assert review.merge_base_sha == base
        assert "fork change" in review.diff
        assert "target only" not in review.diff

    consumed = tmp_path / "consumer-failure"
    with pytest.raises(RuntimeError, match="sentinel consumer failure"):
        with materialize_review(source, target, head) as review:
            consumed = review.path
            raise RuntimeError("sentinel consumer failure")
    assert not consumed.exists()

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git(unrelated, "init", "-q")
    for key, value in (("user.name", "Other"), ("user.email", "o@example.test")):
        _git(unrelated, "config", key, value)
    (unrelated / "other").write_text("unrelated\n", encoding="utf-8")
    other_head = _commit(unrelated, "unrelated root")
    with pytest.raises(MaterializationError, match="merge-base"):
        with materialize_review(source, other_head, head):
            pass
    assert not list(Path(rg_store.clones_dir()).glob("*-final-*"))


def _leaf_repository(path, name, content):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for key, value in (("user.name", "Submodule Test"),
                       ("user.email", "submodule@example.test"),
                       ("commit.gpgsign", "false")):
        _git(path, "config", key, value)
    (path / name).write_text(content, encoding="utf-8")
    _commit(path, "initial content")
    return path


def test_recursive_submodules_are_complete_or_fail_with_cleanup(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    repo, _base, target, _head = _repository(tmp_path / "parent")
    nested = _leaf_repository(tmp_path / "nested", "nested.txt", "nested content\n")
    child = _leaf_repository(tmp_path / "child", "child.txt", "child content\n")
    _git(child, "submodule", "add", str(nested), "deps/nested")
    _commit(child, "add nested submodule")
    _git(repo, "submodule", "add", str(child), "vendor/child")
    head = _commit(repo, "add recursive submodules")

    with materialize_review(repo, target, head) as review:
        assert (review.path / "vendor/child/child.txt").read_text(
            encoding="utf-8") == "child content\n"
        assert (review.path / "vendor/child/deps/nested/nested.txt").read_text(
            encoding="utf-8") == "nested content\n"
        assert review.submodules == ("vendor/child", "vendor/child/deps/nested")
        assert _text(review.path, "status", "--porcelain",
                     "--ignore-submodules=none") == ""

    # Destroying a required upstream makes the same public operation fail
    # explicitly; a partial review tree must never escape as usable evidence.
    import shutil
    shutil.rmtree(child)
    with pytest.raises(MaterializationError, match="submodule"):
        with materialize_review(repo, target, head):
            pass
    assert not list(Path(rg_store.clones_dir()).glob("*-final-*"))


def test_required_lfs_content_is_materialized_or_an_explicit_error(
        tmp_path, monkeypatch):
    repo, _base, target, _head = _repository(tmp_path)
    (repo / ".gitattributes").write_text(
        "assets/*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
    (repo / "assets").mkdir()
    pointer = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:" + "a" * 64 + "\nsize 19\n")
    (repo / "assets/data.bin").write_text(pointer, encoding="ascii")
    head = _commit(repo, "add LFS asset")

    with materialize_review(repo, target, head) as review:
        assert review.lfs_files == ("assets/data.bin",)
        assert review.lfs_complete is False
        assert (review.path / "assets/data.bin").read_text(
            encoding="ascii") == pointer

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "git-lfs"
    fake.write_text("#!/bin/sh\necho synthetic LFS transport failure >&2\nexit 7\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    with pytest.raises(MaterializationError, match="synthetic LFS transport failure"):
        with materialize_review(repo, target, head, require_lfs=True):
            pass
    assert not list(Path(rg_store.clones_dir()).glob("*-final-*"))

    fake.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "for path in Path.cwd().rglob('*'):\n"
        "    if path.is_file() and '.git' not in path.parts:\n"
        "        data = path.read_bytes()\n"
        "        if data.startswith(b'version https://git-lfs.github.com/spec/v1\\n'):\n"
        "            path.write_bytes(b'actual LFS content\\n')\n",
        encoding="utf-8")
    fake.chmod(0o755)
    with materialize_review(repo, target, head, require_lfs=True) as review:
        assert review.lfs_files == ("assets/data.bin",)
        assert review.lfs_complete is True
        assert (review.path / "assets/data.bin").read_bytes() == b"actual LFS content\n"
