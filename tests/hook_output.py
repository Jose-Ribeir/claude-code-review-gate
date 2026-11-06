"""Reading a Kimchi bash hook's stdout in tests.

Kimchi port: the hook protocol is {"decision":"block","reason":"..."} on
stdout; a hook that prints nothing is a pass-through. The gate never answers
"allow" (a hook may never grant permission); what it lets through is silent.
"""
import json


def parse_pretooluse(stdout):
    """Return (decision, reason) for a hook's stdout.

    decision is "deny" when the hook blocked, and "pass" when it stayed out of
    the permission decision. Asserts that "allow" is never emitted.
    """
    text = (stdout or "").strip()
    if not text:
        return "pass", ""
    obj = json.loads(text)
    decision = obj.get("decision")
    assert decision != "allow", f"hook must never emit decision allow: {text}"
    if decision is None:
        return "pass", ""
    return "deny" if decision == "block" else decision, obj.get("reason", "")
