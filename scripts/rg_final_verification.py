"""Declared required verification for immutable final-review commits.

Only repository-declared blocking command checks are suites. The runner never
invents a test command from a manifest or toolchain filename. Discovery reads
the pinned commit, not the caller's mutable working tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
import tempfile

_ABSENT_WARNING = "no required verification suite is declared by repository checks or CI"
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class VerificationError(RuntimeError):
    """The pinned repository declaration cannot be inspected safely."""


def _git(repository: Path, *args: str, env=None) -> str:
    if env is None:
        # Discovery also runs without caller credentials, tracing, or global
        # Git config; suite execution supplies its own temporary HOME below.
        env = _safe_environment(Path(tempfile.gettempdir()))
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repository), capture_output=True,
            text=True, check=False, timeout=30, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode:
        raise VerificationError(
            f"git {args[0]} failed with exit code {proc.returncode}: "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def _committed_json(repository: Path, commit: str, name: str):
    try:
        raw = _git(repository, "show", f"{commit}:{name}")
    except VerificationError as exc:
        if "exists on disk, but not in" in str(exc) or "does not exist in" in str(exc):
            return None
        raise
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise VerificationError(f"{name} is not valid JSON at {commit}: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{name} must contain a JSON object at {commit}")
    return value


def _required_check_declarations(repository: Path, commit: str) -> list[dict]:
    config = _committed_json(repository, commit, ".ocr/config.json")
    if config is None:
        return []
    checks = config.get("checks", {})
    if not isinstance(checks, dict):
        raise VerificationError(".ocr/config.json checks must be an object")
    suites = []
    for name, declaration in checks.items():
        if not isinstance(declaration, dict) or declaration.get("required") is not True:
            continue
        if declaration.get("enabled") is False:
            continue
        command = declaration.get("command")
        if not isinstance(command, str) or not command.strip():
            raise VerificationError(f"required check {name!r} has no executable command")
        suites.append({
            "name": str(name),
            "command": command,
            "source": ".ocr/config.json",
            "timeout": declaration.get("timeout", 60),
        })
    return suites


def _yaml_scalar(raw: str) -> str:
    value = raw.strip()
    if " #" in value and not value.startswith(("'", '"')):
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _suite(source: str, name: str, command: str) -> dict:
    return {"name": name, "command": command, "source": source, "timeout": 60}


def _unassessable(source: str, name: str, reason: str) -> dict:
    return {
        **_suite(source, name, None),
        "state": "unassessable",
        "error": f"required declaration {name!r} is unassessable: {reason}",
    }


def _github_declarations(source: str, raw: str) -> list[dict]:
    """Extract literal required step commands; no Actions runner emulation.

    Diagnostic-only and explicitly non-blocking steps are not required suites.
    Actions, reusable workflows, matrices, and unresolved expressions become
    explicit unassessable declarations rather than guessed passes or failures.
    """
    lines = raw.splitlines()
    try:
        jobs_at = next(i for i, line in enumerate(lines) if line == "jobs:")
    except StopIteration:
        return []
    end = len(lines)
    for i in range(jobs_at + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            end = i
            break
    headings = [(i, re.match(r"^  ([^\s:][^:]*):\s*$", lines[i]).group(1))
                for i in range(jobs_at + 1, end)
                if re.match(r"^  ([^\s:][^:]*):\s*$", lines[i])]
    declarations = []
    for position, (start, job) in enumerate(headings):
        job_end = headings[position + 1][0] if position + 1 < len(headings) else end
        block = lines[start + 1:job_end]
        if any(re.match(r"^    continue-on-error:\s*true\s*$", line) for line in block):
            continue
        reusable = next((match for line in block
                         if (match := re.match(r"^    uses:\s*(.*?)\s*$", line))), None)
        if reusable is not None:
            declarations.append(_unassessable(
                source, f"{Path(source).stem}:{job}",
                f"reusable workflow {_yaml_scalar(reusable.group(1))!r} "
                "cannot be expanded locally"))
            continue
        if any(re.match(r"^      matrix:\s*", line) for line in block):
            declarations.append(_unassessable(
                source, f"{Path(source).stem}:{job}",
                "matrix expansion cannot be faithfully reproduced locally"))
            continue
        try:
            steps_at = next(i for i in range(start + 1, job_end)
                            if re.match(r"^    steps:\s*$", lines[i]))
        except StopIteration:
            continue
        step_starts = [i for i in range(steps_at + 1, job_end)
                       if re.match(r"^      -\s+", lines[i])]
        for step_number, step_at in enumerate(step_starts, 1):
            step_end = (step_starts[step_number]
                        if step_number < len(step_starts) else job_end)
            step = lines[step_at:step_end]
            if any(re.search(r"\bcontinue-on-error:\s*true\s*$", line)
                   or re.search(r"\bif:\s*(?:failure|always|cancelled)\s*\(", line)
                   for line in step):
                continue
            name = f"{Path(source).stem}:{job}:step-{step_number}"
            action = next((re.match(r"^\s*(?:-\s*)?uses:\s*(.*?)\s*$", line)
                           for line in step
                           if re.match(r"^\s*(?:-\s*)?uses:\s*(.*?)\s*$", line)), None)
            if action is not None:
                declarations.append(_unassessable(
                    source, name,
                    f"action invocation {_yaml_scalar(action.group(1))!r} cannot be executed locally"))
                continue
            for offset, line in enumerate(step):
                match = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*?)\s*$", line)
                if not match:
                    continue
                command = _yaml_scalar(match.group(2))
                if command in ("|", ">", "|-", ">-"):
                    run_indent = len(match.group(1))
                    body = []
                    for following in step[offset + 1:]:
                        if following.strip() and len(following) - len(following.lstrip()) <= run_indent:
                            break
                        body.append(following.strip())
                    command = (" " if command.startswith(">") else "\n").join(body)
                if command:
                    if "${{" in command:
                        declarations.append(_unassessable(
                            source, name,
                            "GitHub expression syntax cannot be resolved locally"))
                    else:
                        declarations.append(_suite(source, name, command))
                break
    return declarations


def _gitlab_declarations(source: str, raw: str) -> list[dict]:
    """Extract literal job commands, preserving unsupported CI as evidence."""
    lines = raw.splitlines()
    include_at = next((i for i, line in enumerate(lines)
                       if re.match(r"^include:\s*", line)), None)
    if include_at is not None:
        include_end = next((i for i in range(include_at + 1, len(lines))
                            if lines[i] and not lines[i][0].isspace()), len(lines))
        include = " ".join([lines[include_at].split(":", 1)[1].strip()]
                           + [line.strip() for line in lines[include_at + 1:include_end]])
        if include.strip() not in ("", "[]", "{}", "null", "Null", "NULL", "~"):
            return [_unassessable(
                source, "gitlab:include",
                f"include {_yaml_scalar(include)!r} cannot be expanded locally")]
    reserved = {"default", "include", "stages", "variables", "workflow"}
    headings = [(i, match.group(1)) for i, line in enumerate(lines)
                if (match := re.match(r"^([A-Za-z0-9_.-]+):\s*$", line))
                and match.group(1) not in reserved]
    declarations = []
    for position, (start, job) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        block = lines[start + 1:end]
        if job.startswith("."):
            continue
        if any(re.match(r"^  allow_failure:\s*true\s*$", line) for line in block):
            continue
        extends = next((match for line in block
                        if (match := re.match(r"^  extends:\s*(.*?)\s*$", line))), None)
        if extends is not None:
            declarations.append(_unassessable(
                source, f"gitlab:{job}",
                f"extends {_yaml_scalar(extends.group(1))!r} cannot be expanded locally"))
            continue
        if any(re.match(r"^    matrix:\s*", line) for line in block):
            declarations.append(_unassessable(
                source, f"gitlab:{job}",
                "matrix expansion cannot be faithfully reproduced locally"))
            continue
        for key in ("before_script", "script"):
            for offset, line in enumerate(block):
                if not re.match(rf"^  {key}:\s*$", line):
                    continue
                number = 0
                for command_line in block[offset + 1:]:
                    if command_line.strip() and len(command_line) - len(command_line.lstrip()) <= 2:
                        break
                    match = re.match(r"^\s+-\s*(.*?)\s*$", command_line)
                    if match:
                        command = _yaml_scalar(match.group(1))
                        if command and command not in ("|", ">", "|-", ">-"):
                            number += 1
                            declarations.append(_suite(
                                source, f"gitlab:{job}:{key}-{number}", command))
    return declarations


def _ci_declarations(repository: Path, commit: str) -> list[dict]:
    names = _git(repository, "ls-tree", "-r", "--name-only", commit).splitlines()
    declarations = []
    for name in sorted(names):
        if (name.startswith(".github/workflows/")
                and name.endswith((".yml", ".yaml"))) or name == ".gitlab-ci.yml":
            raw = _git(repository, "show", f"{commit}:{name}")
            if name == ".gitlab-ci.yml":
                declarations.extend(_gitlab_declarations(name, raw))
            else:
                declarations.extend(_github_declarations(name, raw))
    return declarations


def discover_required_verification(repository, commit: str) -> list[dict]:
    """Return required suites declared by the immutable repository commit."""
    repository = Path(repository)
    if not _FULL_SHA.fullmatch(commit):
        raise VerificationError("commit must be a full 40-character immutable SHA")
    actual = _git(repository, "rev-parse", f"{commit}^{{commit}}").strip()
    if actual.lower() != commit.lower():
        raise VerificationError("commit does not resolve to the requested immutable SHA")
    return (_required_check_declarations(repository, commit)
            + _ci_declarations(repository, commit))


def _safe_environment(home: Path) -> dict[str, str]:
    """An explicit credential-free environment, never a scrubbed os.environ copy."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": os.devnull,
        "SSH_ASKPASS": os.devnull,
        "CI": "1",
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "COMSPEC",
                 "PATHEXT", "TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _fingerprint(commit: str, declaration: dict, attempts: list[dict]) -> str:
    evidence = {
        "commit": commit,
        "name": declaration["name"],
        "command": declaration["command"],
        "source": declaration["source"],
        "attempts": attempts,
    }
    encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _run_suite(repository: Path, commit: str, declaration: dict) -> dict:
    """Execute one declaration in its own pinned clone and always clean up."""
    if declaration.get("state") == "unassessable":
        return {
            **declaration,
            "kind": "required",
            "blocking": False,
            "output": "",
            "attempts": [],
            "evidence_fingerprint": _fingerprint(commit, declaration, []),
        }
    outputs = []
    attempts = []
    state = "incomplete"
    blocking = False
    error = ""
    with tempfile.TemporaryDirectory(prefix="rg-final-required-") as temporary:
        root = Path(temporary)
        clone = root / "tree"
        home = root / "home"
        home.mkdir()
        env = _safe_environment(home)
        try:
            _git(repository, "clone", "--no-hardlinks", "--no-checkout", "--quiet",
                 str(repository.resolve()), str(clone), env=env)
            _git(clone, "checkout", "--detach", "--quiet", commit, env=env)
            for number in (1, 2):
                if number == 2:
                    # A failed command may modify its checkout. Every rerun starts
                    # from exactly the same immutable tree as the initial run.
                    _git(clone, "reset", "--hard", "--quiet", commit, env=env)
                    _git(clone, "clean", "-ffdx", "--quiet", env=env)
                proc = subprocess.run(
                    declaration["command"], cwd=clone, env=env, shell=True,
                    capture_output=True, text=True, check=False,
                    timeout=declaration["timeout"],
                )
                outputs.extend((proc.stdout, proc.stderr))
                attempts.append({
                    "number": number,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                })
                if proc.returncode == 0:
                    state = "pass" if number == 1 else "incomplete"
                    if number == 2:
                        error = (f"required suite {declaration['name']!r} failed initially "
                                 "but the failure was not reproduced on its one rerun")
                    break
                if proc.returncode in (126, 127):
                    error = (f"required suite {declaration['name']!r} is not executable "
                             f"(exit code {proc.returncode})")
                    break
                if number == 2:
                    state = "fail"
                    blocking = True
        except (OSError, subprocess.SubprocessError, VerificationError) as exc:
            error = f"required suite {declaration['name']!r} could not execute: {exc}"
    return {
        **declaration,
        "kind": "required",
        "state": state,
        "blocking": blocking,
        "output": "".join(outputs),
        "attempts": attempts,
        "error": error,
        "evidence_fingerprint": _fingerprint(commit, declaration, attempts),
    }


def run_required_verification(repository, commit: str) -> dict:
    """Discover and run required verification, returning store-ready evidence."""
    repository = Path(repository)
    commit = commit.lower()
    declarations = discover_required_verification(repository, commit)
    if not declarations:
        return {
            "status": "absent",
            "blocking": False,
            "commit": commit,
            "suites": [],
            "findings": [],
            "warnings": [_ABSENT_WARNING],
        }
    suites = [_run_suite(repository, commit, declaration) for declaration in declarations]
    blocking = any(suite["blocking"] for suite in suites)
    findings = [{
        "category": "required-verification",
        "severity": "high",
        "confidence": 1.0,
        "name": suite["name"],
        "content": (f"required verification {suite['name']!r} failed on its initial run "
                    "and one rerun"),
        "evidence_fingerprint": suite["evidence_fingerprint"],
    } for suite in suites if suite["blocking"]]
    status = "block" if blocking else (
        "pass" if all(suite["state"] == "pass" for suite in suites) else "incomplete"
    )
    return {
        "status": status,
        "blocking": blocking,
        "commit": commit,
        "suites": suites,
        "findings": findings,
        "warnings": [suite["error"] for suite in suites if suite["error"]],
    }
