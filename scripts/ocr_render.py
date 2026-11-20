"""Deterministic render steps that used to cost LLM turns in the review skill.

Two mechanical jobs moved here from the orchestrating session:

* the verdict *render*: a `--json` review session now emits its raw results
  (findings, warnings, the context summaries, a few status inputs) and the gate
  assembles the contract object around them. The formatter subagent that used
  to do this is gone. `compute_verdict` stays the only verdict authority; this
  module never decides a verdict.
* the hallucination check (skill step 3a): a finding whose `existing_code`
  quote appears nowhere in the cited file's diff or current content is
  downgraded (confidence -0.3) with a warning. The check is string matching,
  so it is deterministic and belongs in Python.

Pure functions only: no git, no I/O. The gate supplies the texts.
"""
import json
import re

# Confidence taken off a finding whose quote cannot be found (skill 3a).
QUOTE_DOWNGRADE = 0.3
QUOTE_WARNING = "existing_code not found in diff or file — confidence downgraded"

# A finding anchored somewhere other than the file's own diff or content (an
# impact site, a sibling defect, a dependency task, collected evidence, an
# intent claim, a documentation or comment finding) is checked against that
# anchor, not against the cited file -- the gate does not hold it here, so
# such a finding is left alone rather than downgraded on a wrong comparison.
_ANCHORED_KEYS = ("impact_site", "sibling_of", "dep_task", "evidence_source",
                  "docs_kind", "comment_kind", "stale_passage")


def derive_status(obj):
    """The contract `status` for a worker object that did not carry one.

    Inputs the session reports in `status_inputs` (`subagent_errored`,
    `files_reviewable`) plus the warnings list -- the same rules the formatter
    prompt spelled out: skipped when nothing was reviewable (unless a native
    deep task reports its evidence: a global-duty task has no paths and still
    ran), completed_with_errors when a subagent errored,
    completed_with_warnings when warnings exist, else success."""
    inputs = obj.get("status_inputs")
    inputs = inputs if isinstance(inputs, dict) else {}
    reviewable = inputs.get("files_reviewable")
    if (isinstance(reviewable, int) and not isinstance(reviewable, bool)
            and reviewable == 0 and not obj.get("native_deep_evidence")):
        return "skipped"
    if inputs.get("subagent_errored") is True:
        return "completed_with_errors"
    if obj.get("warnings"):
        return "completed_with_warnings"
    return "success"


def strip_trailing_commas(text):
    """`text` with commas that sit directly before a closing `}` or `]` removed.

    String-aware: a comma inside a JSON string is left alone. Models drift into
    trailing commas on long outputs; strict `json.loads` rejects them."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
            out.append(c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def loads_lenient(text):
    """json.loads, retried once with trailing commas stripped. Raises the
    original error when the repair does not help."""
    try:
        return json.loads(text)
    except ValueError as first:
        repaired = strip_trailing_commas(text)
        if repaired != text:
            try:
                return json.loads(repaired)
            except ValueError:
                pass
        raise first


# Keys under which a model wraps the verdict object it was asked to print.
_WRAPPER_KEYS = ("result", "review", "output", "verdict_object", "report",
                 "response", "data")


def unwrap_findings_object(obj, is_located):
    """The inner object of a wrapped or bare-array review answer, else None.

    Two improvisations the formatter used to absorb:
    * a bare JSON array of located finding objects (no envelope at all);
    * a single wrapper key (`{"result": {...findings...}}`).
    Only shapes that carry real finding data qualify: a bare list needs every
    entry to be a located finding dict, and a wrapper must hold a dict with a
    `findings` list. Anything else is None (and keeps failing closed)."""
    if isinstance(obj, list):
        if obj and all(is_located(x) for x in obj):
            return {"findings": list(obj)}
        return None
    if not isinstance(obj, dict) or isinstance(obj.get("findings"), list):
        return None
    for key in _WRAPPER_KEYS:
        inner = obj.get(key)
        if isinstance(inner, dict) and isinstance(inner.get("findings"), list):
            return inner
    return None


def _first_line(code):
    for line in str(code or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line
    return ""


def _norm(text):
    return " ".join(str(text or "").split())


def quote_check(findings, texts_for):
    """Skill step 3a in Python.

    `texts_for(path)` returns the list of texts to search for that path (the
    file's diff, its content at the reviewed tip) or None/[] when the gate
    cannot read any -- an unverifiable finding is never downgraded on missing
    data. Returns (new_findings, warnings); `new_findings` has the same
    length and order, with the confidence of each unverifiable finding
    lowered by QUOTE_DOWNGRADE (floor 0)."""
    cache = {}
    out, warnings = [], []
    for f in findings:
        if not isinstance(f, dict):
            out.append(f)
            continue
        code = _first_line(f.get("existing_code"))
        path = str(f.get("path") or "")
        if (not code or not path or f.get("type") == "intent_gap"
                or f.get("category") == "documentation"
                or any(f.get(k) for k in _ANCHORED_KEYS)
                or (f.get("provenance") not in (None, "", "new"))):
            out.append(f)
            continue
        if path not in cache:
            texts = texts_for(path) or []
            cache[path] = [_norm(t) for t in texts if t]
        haystacks = cache[path]
        if not haystacks or any(code in h for h in haystacks):
            out.append(f)
            continue
        g = dict(f)
        try:
            conf = float(g.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        g["confidence"] = round(max(0.0, conf - QUOTE_DOWNGRADE), 2)
        out.append(g)
        warnings.append({"file": path, "message": QUOTE_WARNING})
    return out, warnings


def reviewer_persona_body(agent_markdown):
    """The reviewer agent file's body as the skill's Appendix P.1 embeds it:
    the YAML frontmatter dropped and the tool names translated (`Read` ->
    `read`, `Grep` -> `grep`, plurals included)."""
    body = agent_markdown.split("---\n", 2)[2]
    body = re.sub(r"\bRead(s?)\b", lambda m: "read" + m.group(1), body)
    body = re.sub(r"\bGrep(s?)\b", lambda m: "grep" + m.group(1), body)
    return body.lstrip("\n")
