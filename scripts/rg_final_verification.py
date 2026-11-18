"""Declared required verification for immutable final-review commits.

Only repository-declared blocking command checks are suites. The runner never
invents a test command from a manifest or toolchain filename. Discovery reads
the pinned commit, not the caller's mutable working tree.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import shlex
import shutil
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
            "timeout_source": "declaration" if "timeout" in declaration else "default",
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


def _suite(
        source: str, name: str, command: str, python_version=None, matrix=None) -> dict:
    suite = {
        "name": name, "command": command, "source": source,
        "timeout": 60, "timeout_source": "default",
    }
    if python_version is not None:
        suite["python_version"] = python_version
    if matrix:
        suite["matrix"] = dict(matrix)
    return suite


def _unassessable(
        source: str, name: str, reason: str, python_version=None, matrix=None) -> dict:
    return {
        **_suite(source, name, None, python_version=python_version, matrix=matrix),
        "state": "unassessable",
        "error": f"required declaration {name!r} is unassessable: {reason}",
    }


def _bounded_timeout(value) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise VerificationError("required suite timeout must be a positive finite number")
    return max(1, min(3600, value))


def _github_matrix_values(raw: str) -> list[str]:
    value = raw.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise ValueError("matrix values must be finite inline or block sequences")
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        parsed = [part.strip() for part in value[1:-1].split(",")]
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError("matrix sequences must contain only literal scalar values")
    values = [_yaml_scalar(item) for item in parsed]
    if any(not item or "${{" in item for item in values):
        raise ValueError("matrix values must be nonempty literal scalars, not expressions")
    return values


def _github_matrix_contexts(block: list[str]) -> list[dict]:
    matrix_at = next((i for i, line in enumerate(block)
                      if re.match(r"^      matrix:(?:\s|$)", line)), None)
    if matrix_at is None:
        return [{}]
    if not re.match(r"^      matrix:\s*$", block[matrix_at]):
        raise ValueError("only block-form literal matrix mappings are supported")
    matrix_end = next((i for i in range(matrix_at + 1, len(block))
                       if re.match(r"^    \S", block[i])), len(block))
    axes = []
    index = matrix_at + 1
    while index < matrix_end:
        line = block[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        match = re.match(r"^        ([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if match is None:
            raise ValueError("only literal matrix key/value mappings are supported")
        key, value = match.groups()
        if key in ("include", "exclude"):
            raise ValueError("matrix include/exclude rewriting is unsupported")
        if any(existing == key for existing, _ in axes):
            raise ValueError(f"matrix key {key!r} is declared more than once")
        if value:
            values = _github_matrix_values(value)
            index += 1
        else:
            index += 1
            values = []
            while index < matrix_end:
                item = re.match(r"^          -\s*(.*?)\s*$", block[index])
                if item is None:
                    break
                literal = _yaml_scalar(item.group(1))
                if not literal or "${{" in literal:
                    raise ValueError("matrix values must be nonempty literal scalars")
                values.append(literal)
                index += 1
            if not values:
                raise ValueError(f"matrix key {key!r} has no literal values")
        axes.append((key, values))
    if not axes:
        raise ValueError("matrix must declare at least one literal axis")
    combination_count = 1
    for _, values in axes:
        combination_count *= len(values)
        if combination_count > 64:
            raise ValueError("literal matrix exceeds the 64-combination safety limit")
    contexts = []

    def expand(position, current):
        if position == len(axes):
            contexts.append(dict(current))
            return
        key, values = axes[position]
        for value in values:
            current[key] = value
            expand(position + 1, current)
        current.pop(key, None)

    expand(0, {})
    return contexts


def _matrix_expression(value: str, matrix: dict) -> str:
    def replace(match):
        return matrix.get(match.group(1), match.group(0))

    return re.sub(r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}",
                  replace, value)


def _matrix_suite_name(base: str, matrix: dict) -> str:
    if not matrix:
        return base
    values = ",".join(f"{key}={value}" for key, value in matrix.items())
    return f"{base}[{values}]"


def _github_step_declarations(source, job, steps, matrix):
    declarations = []
    python_version = None
    for step_number, step in enumerate(steps, 1):
        if any(re.search(r"\bcontinue-on-error:\s*true\s*$", line)
               or re.search(r"\bif:\s*(?:failure|always|cancelled)\s*\(", line)
               for line in step):
            continue
        name = _matrix_suite_name(
            f"{Path(source).stem}:{job}:step-{step_number}", matrix)
        condition = next((re.match(r"^(?:      -\s+|        )if:\s*(.*?)\s*$", line)
                          for line in step
                          if re.match(r"^(?:      -\s+|        )if:\s*(.*?)\s*$", line)),
                         None)
        if condition is not None:
            declarations.append(_unassessable(
                source, name,
                "GitHub expression syntax in step conditions cannot be resolved locally: "
                f"{condition.group(1)!r}", python_version=python_version, matrix=matrix))
            continue
        action = next((re.match(r"^\s*(?:-\s*)?uses:\s*(.*?)\s*$", line)
                       for line in step
                       if re.match(r"^\s*(?:-\s*)?uses:\s*(.*?)\s*$", line)), None)
        if action is not None:
            action_name = _matrix_expression(_yaml_scalar(action.group(1)), matrix)
            if action_name.startswith("actions/checkout@"):
                # The pinned clone is already materialized; a second checkout
                # would be both redundant and less faithful to the commit.
                continue
            if action_name.startswith("actions/setup-python@"):
                declared = next((re.match(r"^\s+python-version:\s*(.*?)\s*$", line)
                                 for line in step
                                 if re.match(r"^\s+python-version:\s*(.*?)\s*$", line)), None)
                version = _matrix_expression(
                    _yaml_scalar(declared.group(1)), matrix) if declared else ""
                if not re.fullmatch(r"[0-9]+\.[0-9]+", version):
                    declarations.append(_unassessable(
                        source, name,
                        "setup-python requires one literal major.minor python-version",
                        matrix=matrix))
                else:
                    python_version = version
                continue
            declarations.append(_unassessable(
                source, name, f"action invocation {action_name!r} cannot be executed locally",
                python_version=python_version, matrix=matrix))
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
                    body.append(following)
                content_indent = min(
                    (len(line) - len(line.lstrip()) for line in body if line.strip()),
                    default=0,
                )
                body = [line[content_indent:] if line.strip() else "" for line in body]
                if command.startswith(">"):
                    while body and not body[-1]:
                        body.pop()
                    if any(not line or line[:1].isspace() for line in body):
                        declarations.append(_unassessable(
                            source, name,
                            "only a single-paragraph, uniformly indented folded "
                            "block scalar can be reproduced faithfully",
                            python_version=python_version, matrix=matrix))
                        break
                command = (" " if command.startswith(">") else "\n").join(body)
            command = _matrix_expression(command, matrix)
            if command:
                if "${{" in command:
                    declarations.append(_unassessable(
                        source, name, "GitHub expression syntax cannot be resolved locally",
                        python_version=python_version, matrix=matrix))
                else:
                    declarations.append(_suite(
                        source, name, command,
                        python_version=python_version, matrix=matrix))
            break
    return declarations


def _github_declarations(source: str, raw: str) -> list[dict]:
    """Extract literal required commands without emulating arbitrary Actions."""
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
        try:
            matrices = _github_matrix_contexts(block)
        except ValueError as exc:
            declarations.append(_unassessable(
                source, f"{Path(source).stem}:{job}", str(exc)))
            continue
        try:
            steps_at = next(i for i in range(start + 1, job_end)
                            if re.match(r"^    steps:\s*$", lines[i]))
        except StopIteration:
            continue
        step_starts = [i for i in range(steps_at + 1, job_end)
                       if re.match(r"^      -\s+", lines[i])]
        steps = [lines[step_at:(step_starts[number + 1]
                                if number + 1 < len(step_starts) else job_end)]
                 for number, step_at in enumerate(step_starts)]
        for matrix in matrices:
            declarations.extend(_github_step_declarations(
                source, job, steps, matrix))
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
        "timeout": declaration.get("timeout"),
        "matrix": declaration.get("matrix"),
        "python_version": declaration.get("python_version"),
        "attempts": attempts,
    }
    encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _environment_result(
        commit: str, declaration: dict, missing_tools: list[dict],
        attempts=None, output="") -> dict:
    attempts = list(attempts or [])
    remediations = [tool["remediation"] for tool in missing_tools]
    names = ", ".join(tool["name"] for tool in missing_tools)
    error = (f"required suite {declaration['name']!r} cannot execute because "
             f"required tooling is unavailable: {names}. " + " ".join(remediations))
    return {
        **declaration,
        "kind": "required",
        "state": "environment",
        "failure_type": "environment",
        "blocking": True,
        "output": output,
        "attempts": attempts,
        "error": error,
        "missing_tools": missing_tools,
        "evidence_fingerprint": _fingerprint(commit, declaration, attempts),
    }


def _missing_command_tool(command: str, checkout: Path, env: dict) -> list[dict]:
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return []
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words.pop(0)
    if not words:
        return []
    builtins = {
        ".", ":", "[", "break", "cd", "command", "continue", "eval", "exec",
        "exit", "export", "if", "printf", "readonly", "return", "set", "shift",
        "test", "times", "trap", "umask", "unset", "wait", "while",
    }
    tool = words[0]
    if tool in builtins or any(character in tool for character in "$*?~`"):
        return []
    if "/" in tool:
        executable = Path(tool) if Path(tool).is_absolute() else checkout / tool
        available = executable.is_file() and os.access(executable, os.X_OK)
    else:
        executable = shutil.which(tool, path=env["PATH"])
        available = executable is not None
    if available:
        is_python = re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(tool).name)
        module = words[2] if len(words) > 2 and words[1] == "-m" else ""
        if is_python and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
            probe = (
                "import importlib.util, sys; "
                "raise SystemExit(importlib.util.find_spec(sys.argv[1]) is None)"
            )
            try:
                available_module = subprocess.run(
                    [str(executable), "-c", probe, module], cwd=checkout, env=env,
                    capture_output=True, text=True, check=False, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                available_module = None
            if available_module is None or available_module.returncode:
                remediation = (
                    f"Install Python module {module!r} for the selected Python interpreter "
                    "before required verification, expose that environment on PATH, then rerun."
                )
                return [{
                    "name": f"python-module:{module}", "version": None,
                    "remediation": remediation,
                }]
        return []
    remediation = (f"Provide required executable {tool!r} in the pinned checkout "
                   "or on PATH, then rerun required verification.")
    return [{"name": tool, "version": None, "remediation": remediation}]


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
    python_version = declaration.get("python_version")
    if python_version is not None:
        lookup_env = _safe_environment(Path(tempfile.gettempdir()))
        executable = shutil.which(f"python{python_version}", path=lookup_env["PATH"])
        declaration = {**declaration, "python_executable": executable}
        if executable is None:
            remediation = (f"Install Python {python_version} and expose "
                           f"python{python_version} on PATH, then rerun required verification.")
            return _environment_result(commit, declaration, [{
                "name": f"python{python_version}",
                "version": python_version,
                "remediation": remediation,
            }])
        version_probe = "import sys; print('.'.join(map(str, sys.version_info[:2])))"
        try:
            inspected = subprocess.run(
                [executable, "-c", version_probe], cwd=tempfile.gettempdir(),
                env=lookup_env, capture_output=True, text=True, check=False, timeout=5,
            )
            detected = inspected.stdout.strip() if inspected.returncode == 0 else "unavailable"
        except (OSError, subprocess.SubprocessError):
            detected = "unavailable"
        if detected != python_version:
            remediation = (
                f"Install Python {python_version} and expose python{python_version} on PATH; "
                f"the selected interpreter reports {detected}. Rerun required verification "
                "after correcting the interpreter."
            )
            return _environment_result(commit, declaration, [{
                "name": f"python{python_version}", "version": python_version,
                "detected_version": detected, "remediation": remediation,
            }])
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
            if python_version is not None:
                runners = root / "python-bin"
                runners.mkdir()
                for runner_name in ("python", "python3"):
                    (runners / runner_name).symlink_to(declaration["python_executable"])
                env["PATH"] = str(runners) + os.pathsep + env["PATH"]
            _git(repository, "clone", "--no-hardlinks", "--no-checkout", "--quiet",
                 str(repository.resolve()), str(clone), env=env)
            _git(clone, "checkout", "--detach", "--quiet", commit, env=env)
            missing_tools = _missing_command_tool(declaration["command"], clone, env)
            if missing_tools:
                return _environment_result(commit, declaration, missing_tools)
            for number in (1, 2):
                if number == 2:
                    # A failed command may modify its checkout. Every rerun starts
                    # from exactly the same immutable tree as the initial run.
                    _git(clone, "reset", "--hard", "--quiet", commit, env=env)
                    _git(clone, "clean", "-ffdx", "--quiet", env=env)
                try:
                    proc = subprocess.run(
                        declaration["command"], cwd=clone, env=env, shell=True,
                        capture_output=True, text=True, check=False,
                        timeout=declaration["timeout"],
                    )
                except subprocess.TimeoutExpired as exc:
                    stdout = (exc.stdout.decode("utf-8", errors="replace")
                              if isinstance(exc.stdout, bytes) else exc.stdout or "")
                    stderr = (exc.stderr.decode("utf-8", errors="replace")
                              if isinstance(exc.stderr, bytes) else exc.stderr or "")
                    outputs.extend((stdout, stderr))
                    attempts.append({
                        "number": number, "returncode": None,
                        "stdout": stdout, "stderr": stderr,
                        "timeout": declaration["timeout"], "timed_out": True,
                    })
                    error = (f"required suite {declaration['name']!r} timed out after "
                             f"{declaration['timeout']} seconds")
                    break
                outputs.extend((proc.stdout, proc.stderr))
                attempts.append({
                    "number": number,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "timeout": declaration["timeout"],
                    "timed_out": False,
                })
                if proc.returncode == 0:
                    state = "pass" if number == 1 else "incomplete"
                    if number == 2:
                        error = (f"required suite {declaration['name']!r} failed initially "
                                 "but the failure was not reproduced on its one rerun")
                    break
                if proc.returncode in (126, 127):
                    missing = next((match.group(1) for line in proc.stderr.splitlines()
                                    if (match := re.search(
                                        r"([A-Za-z0-9_./+@-]+): (?:command not found|not found)",
                                        line))), "unresolved-executable")
                    remediation = (
                        f"Provide required executable {missing!r} in the pinned checkout "
                        "or on PATH, then rerun required verification; inspect the retained "
                        "command diagnostic for details."
                    )
                    return _environment_result(commit, declaration, [{
                        "name": missing, "version": None, "remediation": remediation,
                    }], attempts=attempts, output="".join(outputs))
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


def run_required_verification(
        repository, commit: str, *, default_timeout=60, timeout=None) -> dict:
    """Discover and run required verification, returning store-ready evidence."""
    repository = Path(repository)
    commit = commit.lower()
    default = _bounded_timeout(default_timeout)
    override = _bounded_timeout(timeout) if timeout is not None else None
    timeout_policy = {
        "default": default, "override": override, "minimum": 1, "maximum": 3600,
    }
    try:
        declarations = discover_required_verification(repository, commit)
    except VerificationError as exc:
        cause = exc.__cause__
        if not isinstance(cause, FileNotFoundError) or cause.filename != "git":
            raise
        remediation = "Install Git and expose git on PATH, then rerun required verification."
        suite = _environment_result(commit, {
            "name": "required-discovery", "command": None, "source": "repository",
            "timeout": default, "timeout_source": "default",
        }, [{"name": "git", "version": None, "remediation": remediation}])
        return {
            "status": "incomplete", "blocking": True, "commit": commit,
            "suites": [suite], "findings": [], "warnings": [suite["error"]],
            "timeout_policy": timeout_policy,
        }
    prepared = []
    for declaration in declarations:
        source = "override" if override is not None else declaration["timeout_source"]
        selected = (override if override is not None else declaration["timeout"]
                    if source == "declaration" else default)
        prepared.append({
            **declaration, "timeout": _bounded_timeout(selected), "timeout_source": source,
        })
    declarations = prepared
    if not declarations:
        return {
            "status": "absent",
            "blocking": False,
            "commit": commit,
            "suites": [],
            "findings": [],
            "warnings": [_ABSENT_WARNING],
            "timeout_policy": timeout_policy,
        }
    suites = [_run_suite(repository, commit, declaration) for declaration in declarations]
    blocking = any(suite["blocking"] for suite in suites)
    failed = any(suite["state"] == "fail" for suite in suites)
    findings = [{
        "category": "required-verification",
        "severity": "high",
        "confidence": 1.0,
        "name": suite["name"],
        "content": (f"required verification {suite['name']!r} failed on its initial run "
                    "and one rerun"),
        "evidence_fingerprint": suite["evidence_fingerprint"],
    } for suite in suites if suite["state"] == "fail"]
    status = "block" if failed else (
        "pass" if all(suite["state"] == "pass" for suite in suites) else "incomplete"
    )
    return {
        "status": status,
        "blocking": blocking,
        "commit": commit,
        "suites": suites,
        "findings": findings,
        "warnings": [suite["error"] for suite in suites if suite["error"]],
        "timeout_policy": timeout_policy,
    }
