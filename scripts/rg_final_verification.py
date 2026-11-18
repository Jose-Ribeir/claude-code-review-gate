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


def _gitlab_block_scalar(
        block: list[str], offset: int, header: str, parent_indent: int = 2
) -> tuple[str | None, int, str]:
    body = []
    end = len(block)
    for index in range(offset + 1, len(block)):
        line = block[index]
        if line.strip() and len(line) - len(line.lstrip()) <= parent_indent:
            end = index
            break
        body.append(line)
    content_indent = next((len(line) - len(line.lstrip()) for line in body if line.strip()), 0)
    if content_indent <= parent_indent:
        return "", end, ""
    if any(line.strip() and len(line) - len(line.lstrip()) < content_indent
           for line in body):
        return None, end, "block scalar indentation is inconsistent"
    lines = [line[content_indent:] if line.strip() else "" for line in body]
    if header.startswith(">"):
        meaningful = list(lines)
        while meaningful and not meaningful[-1]:
            meaningful.pop()
        if any(not line for line in meaningful) or any(
                line[:1].isspace() for line in meaningful):
            return None, end, ("only a single-paragraph, uniformly indented folded "
                               "block scalar can be reproduced faithfully")
        lines = meaningful
    command = ("\n" if header.startswith("|") else " ").join(lines)
    return command, end, ""


def _gitlab_literal(raw: str) -> str:
    value = raw.strip()
    if value.startswith(("[", "{", "!", "&", "*")):
        raise ValueError("flow collections, YAML tags, anchors, and aliases are unsupported")
    scalar = _yaml_scalar(value)
    if value.startswith(("|", ">")) and scalar not in ("|", ">", "|-", ">-"):
        raise ValueError(f"block scalar header {value!r} is unsupported")
    return scalar


def _gitlab_script_declarations(
        source: str, job: str, key: str, block: list[str], offset: int, raw: str) -> list[dict]:
    content_end = len(block)
    for index in range(offset + 1, len(block)):
        line = block[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent < 2 or (indent == 2 and not re.match(r"^  -(?:\s|$)", line)):
            content_end = index
            break
    section = "\n".join(block[offset:content_end]).strip()
    name = f"gitlab:{job}:{key}"

    def unsupported(reason: str) -> list[dict]:
        return [_unassessable(
            source, name, f"{reason}; declared content {section!r}")]

    try:
        inline = _gitlab_literal(raw) if raw.strip() else ""
    except ValueError as exc:
        return unsupported(str(exc))
    value = raw.strip()
    if inline in ("|", ">", "|-", ">-") and not value.startswith(("'", '"')):
        command, _, error = _gitlab_block_scalar(block, offset, inline)
        if error:
            return unsupported(error)
        if not command or not command.strip():
            return unsupported("the block scalar declares no executable command")
        return [_suite(source, f"{name}-1", command)]
    if value:
        if not inline.strip():
            return unsupported("the scalar declares no executable command")
        return [_suite(source, f"{name}-1", inline)]

    suites = []
    command_at = offset + 1
    item_indent = None
    while command_at < content_end:
        command_line = block[command_at]
        if not command_line.strip() or command_line.lstrip().startswith("#"):
            command_at += 1
            continue
        match = re.match(r"^(\s+)-(?:\s+(.*?))?\s*$", command_line)
        if match is None or not match.group(2):
            return unsupported("the value is not a supported nonempty command sequence")
        current_indent = len(match.group(1))
        if item_indent is None:
            item_indent = current_indent
        if current_indent != item_indent:
            return unsupported("command sequence indentation is inconsistent")
        item_raw = match.group(2)
        try:
            command = _gitlab_literal(item_raw)
        except ValueError as exc:
            return unsupported(str(exc))
        if command in ("|", ">", "|-", ">-") and not item_raw.strip().startswith(("'", '"')):
            command, command_at, error = _gitlab_block_scalar(
                block, command_at, command, item_indent)
            if error:
                return unsupported(error)
        else:
            command_at += 1
        if not command or not command.strip():
            return unsupported("a command sequence item declares no executable command")
        number = len(suites) + 1
        suites.append(_suite(source, f"{name}-{number}", command))
    if not suites:
        return unsupported("no executable command sequence is declared")
    return suites


def _gitlab_top_level_entry(line: str) -> tuple[str, str]:
    """Decode one literal mapping key without constructing arbitrary YAML objects."""
    if line.startswith(("'", '"')):
        quote = line[0]
        cursor = 1
        while cursor < len(line):
            char = line[cursor]
            if quote == '"' and char == "\\":
                cursor += 2
                continue
            if char == quote:
                if quote == "'" and line[cursor + 1:cursor + 2] == "'":
                    cursor += 2
                    continue
                break
            cursor += 1
        else:
            raise ValueError("quoted mapping key is unterminated")
        raw_key = line[:cursor + 1]
        remainder = line[cursor + 1:]
        separator = len(remainder) - len(remainder.lstrip())
        if remainder[separator:separator + 1] != ":":
            raise ValueError("quoted mapping key is not followed by a mapping colon")
        value = remainder[separator + 1:]
        if value and not value[0].isspace():
            raise ValueError("mapping colon must be followed by whitespace")
        if quote == "'":
            key = raw_key[1:-1].replace("''", "'")
        else:
            try:
                key = json.loads(raw_key)
            except ValueError as exc:
                raise ValueError(
                    f"double-quoted mapping key is unsupported: {exc}") from exc
    else:
        match = re.search(r":(?=\s|$)", line)
        if match is None:
            raise ValueError("top-level line is not a supported mapping entry")
        key = line[:match.start()].strip()
        value = line[match.end():]
        if key.startswith(("[", "{", "!", "&", "*", "|", ">", "?")):
            raise ValueError("tagged, anchored, aliased, and flow mapping keys are unsupported")
        if re.search(r"\s#", key):
            raise ValueError("mapping key contains a comment before its colon")
    value = value.strip()
    if not key:
        raise ValueError("mapping key is empty")
    return key, value


def _gitlab_declarations(source: str, raw: str) -> list[dict]:
    """Extract literal job commands, preserving unsupported CI as evidence."""
    lines = raw.splitlines()
    headings = []
    for index, line in enumerate(lines):
        if (not line or line[0].isspace() or line.startswith("#")
                or re.match(r"^(?:---|\.\.\.)(?:\s+#.*)?$", line)
                or re.match(r"^%(?:YAML|TAG)\s", line)):
            continue
        try:
            job, value = _gitlab_top_level_entry(line)
            headings.append((index, job, value, ""))
        except ValueError as exc:
            headings.append((index, None, "", str(exc)))

    for position, (start, job, value, error) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        if job != "include" or error:
            continue
        initial = "" if value.startswith("#") else value
        include = " ".join([initial] + [
            line.strip() for line in lines[start + 1:end]
            if line.strip() and not line.lstrip().startswith("#")
        ]).strip()
        if include not in ("", "[]", "{}", "null", "Null", "NULL", "~"):
            return [_unassessable(
                source, "gitlab:include",
                f"include {_yaml_scalar(include)!r} cannot be expanded locally")]

    # Root-only keywords and deprecated global job defaults are settings, not
    # inline jobs, regardless of whether their values look like shell commands.
    reserved = {
        "after_script", "before_script", "cache", "default", "image", "include",
        "services", "spec", "stages", "types", "variables", "workflow",
    }
    declarations = []
    for position, (start, job, value, error) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        section = "\n".join(lines[start:end]).strip()
        if job is None:
            declarations.append(_unassessable(
                source, f"gitlab:line-{start + 1}",
                f"{error}; declared content {section!r}"))
            continue
        if job in reserved or job.startswith("."):
            continue
        block = lines[start + 1:end]
        if value and not value.startswith("#"):
            declarations.append(_unassessable(
                source, f"gitlab:{job}",
                f"inline job mappings are unsupported; declared content {section!r}"))
            continue
        if any((match := re.match(r"^  allow_failure:\s*(.*?)\s*$", line))
               and _yaml_scalar(match.group(1)).lower() == "true" for line in block):
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
                declaration_match = re.match(rf"^  {key}:\s*(.*?)\s*$", line)
                if declaration_match is not None:
                    declarations.extend(_gitlab_script_declarations(
                        source, job, key, block, offset, declaration_match.group(1)))
    if not headings and any(
            line.strip() and not line.lstrip().startswith(("#", "---", "..."))
            for line in lines):
        declarations.append(_unassessable(
            source, "gitlab:document",
            f"no supported top-level mapping entry; declared content {raw.strip()!r}"))
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
