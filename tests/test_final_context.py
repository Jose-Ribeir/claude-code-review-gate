"""Complete-repository context acquisition for SPEC-16 final review.

All repository fixtures are real temporary Git repositories. Provider/ticket
traffic is replaced at the existing scripted CLI/fetcher seams; no test needs
a live tracker or review provider.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from rg_final_context import ContextCompletenessError, collect_review_context  # noqa: E402
from rg_final_materialize import materialize_review  # noqa: E402
from rg_ticket_fetch import fetch_final_intent  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=True,
        timeout=30,
    ).stdout.decode("utf-8").strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Context Test")
    _git(repo, "config", "user.email", "context@example.test")
    _git(repo, "config", "commit.gpgsign", "false")
    files = {
        "AGENTS.md": "Repository instructions\n",
        "docs/standards/review.md": "Review standard\n",
        "docs/adr/0001-storage.md": "Unchanged storage decision\n",
        "docs/GLOSSARY.md": "Unchanged domain language\n",
        "docs/domain/ordering.md": "Ordering invariant\n",
        "src/orders.py": "def total(lines):\n    return sum(lines)\n",
        "tests/test_orders.py": "def test_total():\n    assert True\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    target = _commit(repo, "base")
    (repo / "src/orders.py").write_text(
        "def total(lines):\n    return sum(line.amount for line in lines)\n",
        encoding="utf-8",
    )
    head = _commit(repo, "fix order totals (#45)")
    return repo, target, head


def test_collects_unchanged_repository_knowledge_without_serializing_sources(tmp_path):
    repo, target, head = _repository(tmp_path)
    with materialize_review(repo, target, head) as review:
        context = collect_review_context(
            review, description="Fix totals (#45)",
            intent_fetcher=lambda **_request: {"tickets": [], "warnings": []},
        )

    assert context["contract_version"] == 1
    assert context["review_mode"] == "complete-repository"
    assert context["head_sha"] == head
    assert context["merge_base_sha"] == target
    assert context["search_roots"] == [str(review.path)]
    assert "Search and read" in context["reviewer_instructions"]
    assert "Unchanged storage decision" not in str(context["documents"])

    documents = {doc["path"]: doc for doc in context["documents"]}
    for name in (
        "AGENTS.md", "docs/standards/review.md", "docs/adr/0001-storage.md",
        "docs/GLOSSARY.md", "docs/domain/ordering.md",
    ):
        assert name in documents
        assert documents[name]["unchanged"] is True
    assert documents["docs/adr/0001-storage.md"]["kind"] == "adr"
    assert documents["docs/GLOSSARY.md"]["kind"] == "glossary"
    assert documents["AGENTS.md"]["kind"] == "instructions"

    assert context["files"]["implementation"] == ["src/orders.py"]
    assert context["files"]["tests"] == ["tests/test_orders.py"]
    assert "AGENTS.md" in context["files"]["all"]
    # The patch scopes the change; unchanged implementation and tests remain
    # available as paths for reviewers to search/read on demand.
    assert "def total" not in str(context["files"])
    assert "src/orders.py" in context["diff"]
    assert context["completeness"]["complete"] is True


def test_intent_always_uses_description_and_attempts_linked_fetch(tmp_path):
    repo, target, head = _repository(tmp_path)
    calls = []

    def scripted_fetcher(**request):
        calls.append(request)
        return {
            "description": request["description"],
            "tickets": [{"key": "#45", "title": "Totals are wrong", "body": "Spec"}],
            "warnings": [],
        }

    with materialize_review(repo, target, head) as review:
        context = collect_review_context(
            review,
            description="Fix totals; see #45",
            intent_fetcher=scripted_fetcher,
            commit_messages=[{"subject": "fix order totals (#45)", "body": ""}],
        )

    assert len(calls) == 1
    assert calls[0]["description"] == "Fix totals; see #45"
    assert calls[0]["commit_messages"][0]["subject"] == "fix order totals (#45)"
    assert context["intent"]["description"]["text"] == "Fix totals; see #45"
    assert context["intent"]["tickets"][0]["key"] == "#45"
    assert context["intent"]["warnings"] == []


def test_intent_misses_are_visible_and_do_not_invent_requirements(tmp_path):
    repo, target, head = _repository(tmp_path)
    description_calls = []

    def no_description():
        description_calls.append(True)
        return None

    def no_tickets(**_request):
        return {"tickets": [], "warnings": ["#45: scripted tracker unavailable"]}

    with materialize_review(repo, target, head) as review:
        context = collect_review_context(
            review,
            description_fetcher=no_description,
            intent_fetcher=no_tickets,
        )

    assert description_calls == [True]
    assert context["intent"]["description"] is None
    assert context["intent"]["tickets"] == []
    assert any("description" in warning.lower()
               for warning in context["intent"]["warnings"])
    assert "#45: scripted tracker unavailable" in context["intent"]["warnings"]
    assert "requirements" not in context["intent"]


def test_unresolved_required_lfs_is_a_context_completeness_error(tmp_path):
    repo, target, _ = _repository(tmp_path)
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="ascii")
    pointer = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:" + "b" * 64 + "\nsize 12\n")
    (repo / "required.bin").write_text(pointer, encoding="ascii")
    head = _commit(repo, "add required model")
    with materialize_review(repo, target, head) as review:
        with pytest.raises(ContextCompletenessError, match="required.bin"):
            collect_review_context(review, description="Use the model")


def test_unavailable_binary_can_warn_only_with_explicit_irrelevance_evidence(tmp_path):
    repo, target, _ = _repository(tmp_path)
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="ascii")
    pointer = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:" + "c" * 64 + "\nsize 12\n")
    (repo / "mockup.bin").write_text(pointer, encoding="ascii")
    head = _commit(repo, "add unused design mockup")
    with materialize_review(repo, target, head) as review:
        with pytest.raises(ContextCompletenessError, match="irrelevance"):
            collect_review_context(
                review, description="docs only",
                binary_irrelevance={"mockup.bin": ""},
            )
        context = collect_review_context(
            review,
            description="Python algorithm change; binary design mockup is not executed",
            binary_irrelevance={
                "mockup.bin": "Unused documentation mockup; change and tests touch only Python",
            },
        )
    assert context["completeness"]["complete"] is True
    assert context["completeness"]["lfs_complete"] is False
    warning = context["warnings"][0]
    assert warning["code"] == "LFS_CONTENT_UNAVAILABLE"
    assert warning["path"] == "mockup.bin"
    assert "Unused documentation mockup" in warning["evidence"]


def test_final_intent_attempts_tickets_even_when_prepush_opt_in_is_off(tmp_path):
    settings = {"intent": {"tickets": False, "tracker_order": ["github"],
                           "ticket": None}}
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        return json.dumps({"title": "Wrong totals",
                           "body": "The specification requires decimal totals."}), None

    result = fetch_final_intent(
        description="Implement the totals specification in #45",
        commit_messages=[{"subject": "fix totals", "body": ""}],
        settings=settings, repo_root=tmp_path, runner=runner,
        cache_dir=tmp_path / "cache",
    )

    assert settings["intent"]["tickets"] is False
    assert calls == [["gh", "issue", "view", "45", "--json", "title,body"]]
    assert result["tickets"][0]["title"] == "Wrong totals"
    assert result["tickets"][0]["role"] == "direct"
    assert result["warnings"] == []


def test_provider_linked_intent_keeps_provenance_and_description_miss_visible(tmp_path):
    def runner(argv, cwd, timeout):
        return json.dumps({"title": "Spec issue", "body": "Required behavior"}), None

    linked = [{"key": "#45", "source": "description", "role": "direct"}]
    result = fetch_final_intent(
        linked_intent=linked,
        settings={"intent": {"tracker_order": ["github"]}},
        repo_root=tmp_path, runner=runner, cache_dir=tmp_path / "cache",
    )
    assert result["description"] is None
    assert result["references"] == linked
    assert result["tickets"][0]["source"] == "description"
    assert result["tickets"][0]["role"] == "direct"
    assert any("description" in warning for warning in result["warnings"])


def test_final_intent_miss_is_visible_without_fabricated_ticket(tmp_path):
    def runner(argv, cwd, timeout):
        return None, "scripted tracker offline"

    result = fetch_final_intent(
        description="See #45", settings={"intent": {"tracker_order": ["github"]}},
        repo_root=tmp_path, runner=runner, cache_dir=tmp_path / "cache",
    )
    assert result["tickets"] == []
    assert any("scripted tracker offline" in warning
               for warning in result["warnings"])
    assert "requirements" not in result


def test_materialization_can_warn_for_proven_irrelevant_lfs_only(
        tmp_path, monkeypatch):
    repo, target, _ = _repository(tmp_path)
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="ascii")
    pointer = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:" + "d" * 64 + "\nsize 12\n")
    (repo / "mockup.bin").write_text(pointer, encoding="ascii")
    head = _commit(repo, "add unused mockup")
    bindir = tmp_path / "lfs-bin"
    bindir.mkdir()
    fake = bindir / "git-lfs"
    fake.write_text("#!/bin/sh\necho scripted LFS outage >&2\nexit 7\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    evidence = {"mockup.bin": "Unreferenced image; only src/orders.py and tests are reviewed"}
    with materialize_review(
            repo, target, head, require_lfs=True,
            lfs_irrelevance=evidence) as review:
        assert review.lfs_complete is False
        assert review.warnings[0]["path"] == "mockup.bin"
        context = collect_review_context(
            review, description="Algorithm only",
            intent_fetcher=lambda **_request: {"tickets": [], "warnings": []},
        )
    assert context["completeness"]["complete"] is True
    assert context["completeness"]["lfs_complete"] is False
    assert context["warnings"][0]["path"] == "mockup.bin"
    assert "scripted LFS outage" in context["warnings"][0]["message"]


def test_irrelevant_lfs_evidence_cannot_hide_another_required_asset(
        tmp_path, monkeypatch):
    repo, target, _ = _repository(tmp_path)
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="ascii")
    pointer = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:" + "e" * 64 + "\nsize 12\n")
    (repo / "mockup.bin").write_text(pointer, encoding="ascii")
    (repo / "required.bin").write_text(pointer, encoding="ascii")
    head = _commit(repo, "add optional and required assets")
    bindir = tmp_path / "lfs-bin"
    bindir.mkdir()
    fake = bindir / "git-lfs"
    fake.write_text("#!/bin/sh\necho scripted LFS outage >&2\nexit 7\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    from rg_final_materialize import MaterializationError
    with pytest.raises(MaterializationError, match="required.bin|scripted LFS outage"):
        with materialize_review(
                repo, target, head, require_lfs=True,
                lfs_irrelevance={"mockup.bin": "Unreferenced design image"}):
            pass


def _submodule_repository(path, files):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Submodule Context Test")
    _git(path, "config", "user.email", "submodule-context@example.test")
    _git(path, "config", "commit.gpgsign", "false")
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _commit(path, "submodule context")
    return path


def test_context_inventory_and_documents_recurse_through_submodules(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    nested = _submodule_repository(tmp_path / "nested", {
        "docs/GLOSSARY.md": "Nested domain language\n",
        "nested.py": "VALUE = 1\n",
    })
    child = _submodule_repository(tmp_path / "child", {
        "docs/adr/0002-child.md": "Child decision\n",
        "child.py": "VALUE = 2\n",
        "tests/test_child.py": "def test_child():\n    assert True\n",
    })
    _git(child, "submodule", "add", str(nested), "deps/nested")
    _commit(child, "add nested context")
    repo, target, _ = _repository(tmp_path / "parent")
    _git(repo, "submodule", "add", str(child), "vendor/child")
    head = _commit(repo, "add child context")

    with materialize_review(repo, target, head) as review:
        context = collect_review_context(
            review, description="Add vendored implementation",
            intent_fetcher=lambda **_request: {"tickets": [], "warnings": []},
        )
        assert (review.path / "vendor/child/deps/nested/nested.py").is_file()

    assert context["completeness"]["submodules"] == [
        "vendor/child", "vendor/child/deps/nested"]
    assert "vendor/child/child.py" in context["files"]["implementation"]
    assert "vendor/child/tests/test_child.py" in context["files"]["tests"]
    assert "vendor/child/deps/nested/nested.py" in context["files"]["implementation"]
    documents = {document["path"]: document for document in context["documents"]}
    assert documents["vendor/child/docs/adr/0002-child.md"]["kind"] == "adr"
    assert documents["vendor/child/docs/adr/0002-child.md"]["unchanged"] is False
    assert documents["vendor/child/deps/nested/docs/GLOSSARY.md"]["kind"] == "glossary"


def test_changed_decision_remains_indexed_without_becoming_unchanged(tmp_path):
    repo, target, head = _repository(tmp_path)
    (repo / "docs/adr/0001-storage.md").write_text(
        "Reconsidered storage decision\n", encoding="utf-8")
    head = _commit(repo, "revise storage decision")
    with materialize_review(repo, target, head) as review:
        context = collect_review_context(
            review, description="Revise documented storage decision",
            intent_fetcher=lambda **_request: {"tickets": [], "warnings": []},
        )
    documents = {document["path"]: document for document in context["documents"]}
    assert documents["docs/adr/0001-storage.md"]["unchanged"] is False
    assert documents["docs/GLOSSARY.md"]["unchanged"] is True
    assert "dependency_edges" not in context


def test_materialization_rejects_empty_or_misdirected_irrelevance_evidence(tmp_path):
    from rg_final_materialize import MaterializationError

    repo, target, _ = _repository(tmp_path)
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="ascii")
    (repo / "mockup.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "f" * 64 + "\nsize 12\n", encoding="ascii")
    head = _commit(repo, "add LFS mockup")
    for evidence in ({"mockup.bin": "  "},
                     {"src/orders.py": "Not an LFS binary"}):
        with pytest.raises(MaterializationError, match="irrelevance evidence"):
            with materialize_review(
                    repo, target, head, require_lfs=True,
                    lfs_irrelevance=evidence):
                pass


def test_intent_fetcher_exception_is_a_warning_not_invented_intent(tmp_path):
    repo, target, head = _repository(tmp_path)

    def broken_fetcher(**_request):
        raise RuntimeError("scripted intent reader crashed")

    with materialize_review(repo, target, head) as review:
        context = collect_review_context(
            review, description="Provider description remains authoritative",
            intent_fetcher=broken_fetcher,
        )
    assert context["intent"]["description"]["text"] == \
        "Provider description remains authoritative"
    assert context["intent"]["tickets"] == []
    assert any("scripted intent reader crashed" in warning
               for warning in context["intent"]["warnings"])
    assert "requirements" not in context["intent"]
