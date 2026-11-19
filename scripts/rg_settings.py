"""Review-agent settings layer: one schema-validated source for which model and
thinking level every review agent runs with, per backend, plus a small
`behavior` section for the non-model knobs.

Resolution order, per key, highest first:

    1. the existing OCR_* environment overrides (they keep working unchanged)
    2. the PROJECT file   <repo>/.ocr/settings.json
    3. the USER file      ~/.config/review-gate/settings.json
                          (OCR_SETTINGS_FILE relocates it; Path.home() honours
                          USERPROFILE on Windows)
    4. the built-in defaults in this module

With no settings file and no env override the result is exactly what the gate
did before this layer existed.

Every lookup returns a Resolved(value, source) so a caller (the gate, the
settings command, the doctor) can say WHERE a value came from. A source is
"env:<VAR>", "project:<path>", "user:<path>" or "default".

Strictness: a missing file is the normal state and is silently skipped, but an
UNREADABLE or INVALID file raises SettingsError naming the file and every
problem. There is deliberately no fallback to defaults: a typo in a pinned
model must be seen, not quietly replaced by a different model.

File shape (schemas/settings.schema.json is GENERATED from this module by
settings_schema(); a test keeps the two in step):

    {
      "agents": {
        "spotter": {
          "kimchi": {"model": "deepseek-v4-flash", "thinking": "high"},
          "claude": {"model": "haiku"}
        }
      },
      "behavior": {
        "inline_wait_seconds": {"hook": 600, "git": 300},
        "spotter_skip": {"enabled": true, "max_files": 3, "max_changed_lines": 60},
        "canary_cache": {"enabled": true, "ttl_days": 7},
        "docs_checker_parallel": {"enabled": true}
      }
    }

Environment switches this module reads (all pre-existing except the file
relocation): OCR_SETTINGS_FILE (user file path), OCR_SPOTTER_MODEL,
OCR_TIER1_MODEL, OCR_TIER2_MODEL, OCR_CLAUDE_SPOTTER_MODEL,
OCR_CLAUDE_TIER1_MODEL, OCR_CLAUDE_TIER2_MODEL, OCR_MODEL, OCR_CLAUDE_MODEL,
OCR_DOCS_SUMMARY_MODEL, OCR_CLAUDE_DOCS_SUMMARY_MODEL, OCR_INLINE_BUDGET,
OCR_INLINE_BUDGET_GIT. Decision record: ADR 0005 (tier models, provider
guard) and its successor for this layer.

This module reads and (through set_value) writes settings files; it never
launches anything.
"""
import json
import os
import re
from collections import namedtuple
from pathlib import Path

# The agents a model/thinking pair can be set for. The first three are the
# tiers the gate already passes to the skill through the manifest; docs-summary
# is the generation worker the gate launches for .ocr/docs-summary.md; the
# rest are the personas and sessions named in the review-speed work.
AGENTS = ("spotter", "code-reviewer", "deep-reviewer", "docs-checker",
          "code-filter", "code-resolver", "docs-summary", "orchestrator",
          "canary")
BACKENDS = ("claude", "kimchi")
THINKING_LEVELS = ("off", "low", "medium", "high", "max")

# The gate's role names for the three manifest tiers, and the agent each is.
TIER_AGENTS = {"spotter": "spotter", "tier1": "code-reviewer",
               "tier2": "deep-reviewer"}

# --- built-in defaults ----------------------------------------------------------
# These are today's pins (see skills/review/SKILL.md "Persona pins" and the
# gate's tier readers). The agents the gate does not itself launch (docs-checker,
# code-filter, code-resolver, canary) are resolved here so every consumer reads
# one table; they only take effect where a consumer reads them.
DEFAULT_MODELS = {
    ("spotter", "kimchi"): "deepseek-v4-flash",
    ("code-reviewer", "kimchi"): "kimi-k3",
    ("deep-reviewer", "kimchi"): "kimi-k3",
    ("docs-checker", "kimchi"): "kimchi-dev/glm-5.3",
    ("code-filter", "kimchi"): "kimchi-dev/deepseek-v4.1-flash-1017",
    ("code-resolver", "kimchi"): "kimchi-dev/glm-5.3",
    ("docs-summary", "kimchi"): "kimchi-dev/glm-5.3",
    ("orchestrator", "kimchi"): "kimchi-dev/kimi-k3",
    ("canary", "kimchi"): "kimchi-dev/kimi-k3",
    # claude: aliases, not dated ids, so the pin tracks the model family.
    ("spotter", "claude"): "haiku",
    ("code-reviewer", "claude"): "sonnet",
    ("deep-reviewer", "claude"): "opus",
    ("docs-checker", "claude"): "sonnet",
    ("code-filter", "claude"): "haiku",
    ("code-resolver", "claude"): "haiku",
    ("docs-summary", "claude"): "sonnet",
    ("orchestrator", "claude"): "sonnet",
    ("canary", "claude"): "haiku",
}

# None = the thinking level is not passed (the backend/persona default). The
# reviewers are the highest-stakes calls (kimi-k3 collapses below max).
DEFAULT_THINKING = {
    "spotter": "high", "code-reviewer": "max", "deep-reviewer": "max",
    "docs-checker": "high", "code-filter": "high", "code-resolver": "high",
    "docs-summary": None, "orchestrator": None, "canary": None,
}

# behavior key -> (default, kind, minimum). kind: "bool" or "int".
BEHAVIOR = {
    # Seconds the hook (claude) / git pre-push (git mode) waits inline for the
    # review before answering "still running". Defaults are today's
    # OCR_INLINE_BUDGET / OCR_INLINE_BUDGET_GIT values.
    "inline_wait_seconds.hook": (600, "int", 0),
    "inline_wait_seconds.git": (300, "int", 0),
    # Skip the spotter for a tiny change (at most max_files files AND at most
    # max_changed_lines changed lines).
    "spotter_skip.enabled": (True, "bool", None),
    "spotter_skip.max_files": (3, "int", 0),
    "spotter_skip.max_changed_lines": (60, "int", 0),
    # Remember a passed deny-extension canary for ttl_days days.
    "canary_cache.enabled": (True, "bool", None),
    "canary_cache.ttl_days": (7, "int", 0),
    # Run the docs-checker beside the reviewer instead of after it.
    "docs_checker_parallel.enabled": (True, "bool", None),
}
# The env var that already overrides a behavior key.
_BEHAVIOR_ENV = {
    "inline_wait_seconds.hook": "OCR_INLINE_BUDGET",
    "inline_wait_seconds.git": "OCR_INLINE_BUDGET_GIT",
}

# --- environment overrides ------------------------------------------------------
# (agent, backend) -> ordered [(VAR, rule)]. rule: "any" accepts any non-blank
# value; "anthropic" only an Anthropic model id (a kimchi id in a shared
# variable must never reach `claude --model`); "no-slash" only an id without a
# vendor prefix (the kimchi convention `kimchi-dev/<model>`). A blank variable
# is unset. These reproduce the gate's long-standing env rules exactly.
_ENV_CHAINS = {}
for _role_agent, _suffix in (("spotter", "SPOTTER"), ("code-reviewer", "TIER1"),
                             ("deep-reviewer", "TIER2")):
    _ENV_CHAINS[(_role_agent, "kimchi")] = [("OCR_%s_MODEL" % _suffix, "any")]
    _ENV_CHAINS[(_role_agent, "claude")] = [
        ("OCR_CLAUDE_%s_MODEL" % _suffix, "any"),
        ("OCR_%s_MODEL" % _suffix, "anthropic")]
_ENV_CHAINS[("orchestrator", "kimchi")] = [("OCR_MODEL", "any")]
_ENV_CHAINS[("orchestrator", "claude")] = [
    ("OCR_CLAUDE_MODEL", "any"), ("OCR_MODEL", "no-slash")]
_ENV_CHAINS[("docs-summary", "kimchi")] = [("OCR_DOCS_SUMMARY_MODEL", "any")]
_ENV_CHAINS[("docs-summary", "claude")] = [
    ("OCR_CLAUDE_DOCS_SUMMARY_MODEL", "any"),
    ("OCR_DOCS_SUMMARY_MODEL", "no-slash"),
    ("OCR_CLAUDE_MODEL", "any"), ("OCR_MODEL", "no-slash")]

# --- provider classification (the ADR 0005 guard's deny-list) ------------------
BLOCKED_PROVIDERS = ("anthropic", "openai")
# First-token families, lowercased. Anthropic ids are `claude-*` plus the bare
# `sonnet`/`haiku`/`opus` shorthands; OpenAI ids are `gpt-*`, `chatgpt-*`, and
# the reasoning series `o1`/`o3`/`o4`.
_PROVIDER_FIRST_TOKENS = (
    ("anthropic", ("claude", "sonnet", "haiku", "opus")),
    ("openai", ("gpt", "chatgpt", "o1", "o3", "o4")),
)


def model_provider(model):
    """Provider family for a model id, or "" when it is unrecognized. The guard
    is a deny-list for subscription-violating providers, so unknown providers
    pass.

    A vendor-prefixed id (openrouter-style `vendor/model`) is classified on the
    model part AFTER the prefix: `anthropic/claude-sonnet-4-5` is an anthropic
    model regardless of the prefix it was routed under. EVERY prefix is
    stripped: relay chains write `openrouter/anthropic/claude-...` and one
    split would leave the mid-chain vendor in place, defeating the deny-list.
    A bracketed suffix (`sonnet[1m]`) is not part of the family token."""
    ident = (model or "").strip().lower()
    while "/" in ident:
        ident = ident.split("/", 1)[1]
    first = re.split(r"[-\[]", ident, 1)[0]
    for provider, tokens in _PROVIDER_FIRST_TOKENS:
        if first in tokens:
            return provider
    return ""


# --- errors, results ------------------------------------------------------------

class SettingsError(Exception):
    """A settings file (or a value being set) is invalid. `problems` lists each
    one; str() is the whole human-readable message."""

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = list(problems or [])


Resolved = namedtuple("Resolved", "value source")

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]\-]*$")


def user_settings_path():
    """The user-level file: ~/.config/review-gate/settings.json unless
    OCR_SETTINGS_FILE relocates it. None only when no home directory can be
    determined (then there is simply no user file)."""
    override = os.environ.get("OCR_SETTINGS_FILE", "").strip()
    if override:
        return Path(override)
    try:
        return Path.home() / ".config" / "review-gate" / "settings.json"
    except (RuntimeError, KeyError):
        return None


def project_settings_path(repo_root):
    """The per-project file, beside the gate's other .ocr/ configuration."""
    return Path(repo_root) / ".ocr" / "settings.json"


# --- keys -----------------------------------------------------------------------

def _model_key(agent, backend):
    return "agents.%s.%s.model" % (agent, backend)


def _thinking_key(agent, backend):
    return "agents.%s.%s.thinking" % (agent, backend)


def all_keys():
    """Every settable key, in display order: per agent and backend a model then
    a thinking level, then the behavior knobs."""
    keys = []
    for agent in AGENTS:
        for backend in BACKENDS:
            keys += [_model_key(agent, backend), _thinking_key(agent, backend)]
    return keys + ["behavior." + k for k in BEHAVIOR]


def _split_key(key):
    """('agents', agent, backend, field) | ('behavior', dotted) or SettingsError."""
    parts = str(key).split(".")
    if parts[0] == "agents" and len(parts) == 4:
        _, agent, backend, field = parts
        if agent in AGENTS and backend in BACKENDS and field in ("model", "thinking"):
            return ("agents", agent, backend, field)
    if parts[0] == "behavior" and ".".join(parts[1:]) in BEHAVIOR:
        return ("behavior", ".".join(parts[1:]))
    raise SettingsError(
        "unknown settings key %r; valid keys look like "
        "agents.<agent>.<backend>.model|thinking or behavior.<knob>; run the "
        "settings command with no arguments to list them" % (key,),
        ["unknown key %r" % (key,)])


def default_model(agent, backend):
    return DEFAULT_MODELS[(agent, backend)]


# --- validation -----------------------------------------------------------------

def _check_model(value):
    if not isinstance(value, str) or not _MODEL_ID.match(value):
        return ("must be a model id string (letters, digits and . _ : / @ + [ ] -, "
                "starting with a letter or digit), got %r" % (value,))
    return None


def _check_thinking(value):
    if value not in THINKING_LEVELS or not isinstance(value, str):
        return "must be one of %s, got %r" % (", ".join(THINKING_LEVELS), value)
    return None


def _check_behavior(dotted, value):
    default, kind, minimum = BEHAVIOR[dotted]
    if kind == "bool":
        return None if isinstance(value, bool) else "must be true or false, got %r" % (value,)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return "must be an integer >= %d, got %r" % (minimum, value)
    return None


def validate_value(key, value):
    """Raise SettingsError when `value` is not valid for `key`."""
    parts = _split_key(key)
    if parts[0] == "agents":
        problem = (_check_model(value) if parts[3] == "model" else _check_thinking(value))
    else:
        problem = _check_behavior(parts[1], value)
    if problem:
        raise SettingsError("%s: %s" % (key, problem), ["%s: %s" % (key, problem)])


def _behavior_tree():
    tree = {}
    for dotted in BEHAVIOR:
        group, knob = dotted.split(".")
        tree.setdefault(group, {})[knob] = dotted
    return tree


def validate_document(doc):
    """[problem, ...] for a parsed settings document; empty when valid. Unknown
    keys are problems (a typo must not be silently ignored), except a leading
    "$schema" string, which editors use."""
    problems = []
    if not isinstance(doc, dict):
        return ["the file must hold a JSON object, got %s" % type(doc).__name__]
    for top, body in doc.items():
        if top == "$schema":
            if not isinstance(body, str):
                problems.append("$schema: must be a string")
        elif top == "agents":
            _validate_agents(body, problems)
        elif top == "behavior":
            _validate_behavior(body, problems)
        else:
            problems.append("unknown top-level key %r (expected agents, behavior)" % top)
    return problems


def _validate_agents(body, problems):
    if not isinstance(body, dict):
        problems.append("agents: must be an object keyed by agent name")
        return
    for agent, per_backend in body.items():
        if agent not in AGENTS:
            problems.append("agents.%s: unknown agent (known: %s)" % (agent, ", ".join(AGENTS)))
            continue
        if not isinstance(per_backend, dict):
            problems.append("agents.%s: must be an object keyed by backend" % agent)
            continue
        for backend, entry in per_backend.items():
            where = "agents.%s.%s" % (agent, backend)
            if backend not in BACKENDS:
                problems.append("%s: unknown backend (known: %s)" % (where, ", ".join(BACKENDS)))
                continue
            if not isinstance(entry, dict):
                problems.append("%s: must be an object with model and/or thinking" % where)
                continue
            for field, value in entry.items():
                if field == "model":
                    problem = _check_model(value)
                elif field == "thinking":
                    problem = _check_thinking(value)
                else:
                    problem = "unknown key (expected model, thinking)"
                    problems.append("%s.%s: %s" % (where, field, problem))
                    continue
                if problem:
                    problems.append("%s.%s: %s" % (where, field, problem))


def _validate_behavior(body, problems):
    if not isinstance(body, dict):
        problems.append("behavior: must be an object")
        return
    tree = _behavior_tree()
    for group, knobs in body.items():
        if group not in tree:
            problems.append("behavior.%s: unknown knob (known: %s)"
                            % (group, ", ".join(tree)))
            continue
        if not isinstance(knobs, dict):
            problems.append("behavior.%s: must be an object" % group)
            continue
        for knob, value in knobs.items():
            if knob not in tree[group]:
                problems.append("behavior.%s.%s: unknown key (known: %s)"
                                % (group, knob, ", ".join(tree[group])))
                continue
            problem = _check_behavior(tree[group][knob], value)
            if problem:
                problems.append("behavior.%s.%s: %s" % (group, knob, problem))


# --- reading files --------------------------------------------------------------

def _read_file(path, label):
    """The validated document at `path`, or {} when the file does not exist.
    Raises SettingsError for anything else wrong with it."""
    if path is None:
        return {}
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(
            "%s settings file %s cannot be read: %s" % (label, path, exc),
            ["unreadable: %s" % exc])
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise SettingsError(
            "%s settings file %s is not valid JSON: %s" % (label, path, exc),
            ["not valid JSON: %s" % exc])
    problems = validate_document(doc)
    if problems:
        raise SettingsError(
            "%s settings file %s is invalid:\n  - %s\n"
            "Fix or remove the file; the gate does not fall back to defaults "
            "for an invalid settings file." % (label, path, "\n  - ".join(problems)),
            problems)
    return doc


def _dig(doc, *path):
    for part in path:
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc


class Settings:
    """The loaded layers. Build with load(); every lookup returns Resolved."""

    def __init__(self, project, user, project_path, user_path, environ=None):
        self._layers = (("project", project, project_path),
                        ("user", user, user_path))
        self._environ = os.environ if environ is None else environ

    def _file_value(self, *path):
        for label, doc, where in self._layers:
            value = _dig(doc, *path)
            if value is not None:
                return Resolved(value, "%s:%s" % (label, where))
        return None

    def _env_model(self, agent, backend):
        for var, rule in _ENV_CHAINS.get((agent, backend), ()):
            value = (self._environ.get(var) or "").strip()
            if not value:
                continue
            if rule == "anthropic" and model_provider(value) != "anthropic":
                continue
            if rule == "no-slash" and "/" in value:
                continue
            return Resolved(value, "env:" + var)
        return None

    def model(self, agent, backend):
        """Resolved(model id, source) for one agent under one backend."""
        _split_key(_model_key(agent, backend))
        return (self._env_model(agent, backend)
                or self._file_value("agents", agent, backend, "model")
                or Resolved(DEFAULT_MODELS[(agent, backend)], "default"))

    def thinking(self, agent, backend):
        """Resolved(level | None, source); None = the level is not passed."""
        _split_key(_thinking_key(agent, backend))
        return (self._file_value("agents", agent, backend, "thinking")
                or Resolved(DEFAULT_THINKING[agent], "default"))

    def behavior(self, dotted):
        """Resolved(value, source) for a behavior knob such as
        "spotter_skip.max_files"."""
        if dotted not in BEHAVIOR:
            _split_key("behavior." + dotted)
        var = _BEHAVIOR_ENV.get(dotted)
        if var:
            raw = (self._environ.get(var) or "").strip()
            try:
                if raw:
                    return Resolved(int(raw), "env:" + var)
            except ValueError:
                pass            # an unusable env value is ignored, as it always was
        group, knob = dotted.split(".")
        return (self._file_value("behavior", group, knob)
                or Resolved(BEHAVIOR[dotted][0], "default"))

    def get(self, key):
        """Resolved for any key from all_keys()."""
        parts = _split_key(key)
        if parts[0] == "behavior":
            return self.behavior(parts[1])
        _, agent, backend, field = parts
        return self.model(agent, backend) if field == "model" else self.thinking(agent, backend)

    def entries(self):
        """[(key, Resolved)] for every key, in display order."""
        return [(key, self.get(key)) for key in all_keys()]

    def file_models(self):
        """[(key, model, source)] for every model pinned in a settings FILE,
        whatever wins precedence. The doctor validates these as written."""
        out = []
        for agent in AGENTS:
            for backend in BACKENDS:
                for label, doc, where in self._layers:
                    value = _dig(doc, "agents", agent, backend, "model")
                    if value is not None:
                        out.append((_model_key(agent, backend), value,
                                    "%s:%s" % (label, where)))
        return out

    def file_value_digest_input(self):
        """A stable text of every agent value that came from a settings file,
        for the review fingerprint (a changed pin must not reuse an old
        verdict). Empty when no file pins an agent."""
        pairs = []
        for key in all_keys():
            if key.startswith("behavior."):
                continue
            got = self.get(key)
            if got.source.startswith(("project:", "user:")):
                pairs.append("%s=%s" % (key, got.value))
        return "\n".join(pairs)

    def kimchi_guard_violations(self):
        """[(label, model, provider)] for every EFFECTIVE kimchi model from a
        blocked provider (ADR 0005). Env values count only for the three tier
        variables the guard has always covered (OCR_MODEL pins the harness
        session itself and stays outside it); a value from a settings file
        always counts. Defaults are open models, so they never trip."""
        tier_agents = set(TIER_AGENTS.values())
        out = []
        for agent in ("code-reviewer", "spotter", "deep-reviewer") + tuple(
                a for a in AGENTS if a not in tier_agents):
            got = self.model(agent, "kimchi")
            if got.source.startswith("env:"):
                if agent not in tier_agents:
                    continue
                label = got.source[len("env:"):]
            elif got.source == "default":
                continue
            else:
                label = "%s (%s)" % (_model_key(agent, "kimchi"), got.source)
            provider = model_provider(got.value)
            if provider in BLOCKED_PROVIDERS:
                out.append((label, got.value, provider))
        return out

    def file_guard_violations(self):
        """[(key, model, provider, source)] for every blocked-provider model
        written in the KIMCHI section of a settings file, effective or not."""
        out = []
        for key, model, source in self.file_models():
            if ".kimchi." not in key:
                continue
            provider = model_provider(model)
            if provider in BLOCKED_PROVIDERS:
                out.append((key, model, provider, source))
        return out


def load(repo_root=None, environ=None):
    """Load and validate the project and user files. Raises SettingsError for
    an unreadable or invalid file; missing files are fine. `repo_root` None
    skips the project file."""
    project_path = project_settings_path(repo_root) if repo_root else None
    user_path = user_settings_path()
    return Settings(_read_file(project_path, "project"),
                    _read_file(user_path, "user"),
                    project_path, user_path, environ)


def resolve(key, repo_root=None):
    """Resolved(value, source) for one key; shorthand for load().get(key)."""
    return load(repo_root).get(key)


# --- writing --------------------------------------------------------------------

def parse_cli_value(key, text):
    """The typed value for `text` typed on a command line: JSON for the bool
    and integer knobs, the bare string for models and thinking levels."""
    parts = _split_key(key)
    if parts[0] == "agents":
        return text
    kind = BEHAVIOR[parts[1]][1]
    word = text.strip().lower()
    if kind == "bool":
        if word in ("true", "on", "yes", "1"):
            return True
        if word in ("false", "off", "no", "0"):
            return False
        raise SettingsError("%s: must be true or false, got %r" % (key, text),
                            ["%s: must be true or false" % key])
    try:
        return int(word)
    except ValueError:
        raise SettingsError("%s: must be an integer, got %r" % (key, text),
                            ["%s: must be an integer" % key])


def set_value(key, value, scope="user", repo_root=None):
    """Write one validated value into the user or project file and return the
    file's path. The value is validated first; an existing file that is itself
    invalid is an error (it is never overwritten blindly)."""
    validate_value(key, value)
    if scope == "project":
        if not repo_root:
            raise SettingsError("a project setting needs a repository",
                                ["no repository for scope 'project'"])
        path = project_settings_path(repo_root)
    elif scope == "user":
        path = user_settings_path()
        if path is None:
            raise SettingsError("no home directory to hold the user settings file",
                                ["no home directory"])
    else:
        raise SettingsError("scope must be 'user' or 'project', got %r" % (scope,),
                            ["bad scope %r" % (scope,)])
    doc = _read_file(path, scope)
    parts = _split_key(key)
    steps = list(key.split("."))      # agents.<agent>.<backend>.<field> | behavior.<g>.<k>
    node = doc
    for part in steps[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise SettingsError("%s: cannot set a value under a non-object" % key,
                                ["%s is not an object" % part])
    node[steps[-1]] = value
    problems = validate_document(doc)
    if problems:
        raise SettingsError("refusing to write an invalid settings file:\n  - "
                            + "\n  - ".join(problems), problems)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))
    return path


# --- the JSON schema ------------------------------------------------------------

def settings_schema():
    """The JSON Schema (draft-07) of a settings file. schemas/settings.schema.json
    is this value; tests keep them identical."""
    entry = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "model": {"type": "string", "pattern": _MODEL_ID.pattern,
                      "description": "Model id the agent runs with under this backend."},
            "thinking": {"enum": list(THINKING_LEVELS),
                         "description": "Thinking level; omit to use the default."},
        },
    }
    backends = {"type": "object", "additionalProperties": False,
                "properties": {b: {"$ref": "#/definitions/agentEntry"} for b in BACKENDS}}
    behavior = {}
    for dotted, (default, kind, minimum) in BEHAVIOR.items():
        group, knob = dotted.split(".")
        spec = ({"type": "boolean"} if kind == "bool"
                else {"type": "integer", "minimum": minimum})
        spec["default"] = default
        behavior.setdefault(group, {"type": "object", "additionalProperties": False,
                                    "properties": {}})["properties"][knob] = spec
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://github.com/Jose-Ribeir/claude-code-review-gate/schemas/settings.schema.json",
        "title": "review-gate settings",
        "description": ("Per-agent model and thinking level for each backend, plus "
                        "behavior knobs. Validated strictly by scripts/rg_settings.py "
                        "(the single source; this schema is generated from it). "
                        "Precedence: OCR_* env > <repo>/.ocr/settings.json > "
                        "~/.config/review-gate/settings.json > built-in defaults."),
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "$schema": {"type": "string"},
            "agents": {"type": "object", "additionalProperties": False,
                       "properties": {a: {"$ref": "#/definitions/backends"} for a in AGENTS}},
            "behavior": {"type": "object", "additionalProperties": False,
                         "properties": behavior},
        },
        "definitions": {"agentEntry": entry, "backends": backends},
    }


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--schema"]:
        sys.stdout.write(json.dumps(settings_schema(), indent=2) + "\n")
    else:
        sys.stderr.write("usage: rg_settings.py --schema\n")
        sys.exit(2)
