"""Reading a PreToolUse hook's stdout in tests.

The gate never answers `permissionDecision: "allow"`: an allow from a hook
auto-approves the tool call and skips the user's own permission prompt. A call
it lets through is a pass-through -- empty stdout (or, when it has something to
tell the user, a bare `systemMessage`) and no permissionDecision at all.
"""
import json


def parse_pretooluse(stdout):
    """Return (decision, reason) for a PreToolUse hook's stdout.

    decision is "deny" (or "ask") when the hook blocked, and "pass" when it
    stayed out of the permission decision. Asserts that "allow" is never
    emitted. For a pass, reason is the `systemMessage` ("" when there is none).
    """
    text = (stdout or "").strip()
    if not text:
        return "pass", ""
    obj = json.loads(text)
    hso = obj.get("hookSpecificOutput") or {}
    decision = hso.get("permissionDecision")
    assert decision != "allow", f"hook must never emit permissionDecision allow: {text}"
    if decision is None:
        return "pass", obj.get("systemMessage", "")
    return decision, hso.get("permissionDecisionReason", "")
