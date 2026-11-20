// Driver for tests/test_kimchi_helper_allowlist.py: loads the real deny
// extension (extensions/review-gate-deny.ts) as `kimchi -e` would, runs every
// bash command read from stdin (a JSON list) through its tool_call
// subscriber with OCR_IN_REVIEW=1, and prints [{blocked, reason}, ...] as JSON.
import ext from "../extensions/review-gate-deny.ts"

process.env.OCR_IN_REVIEW = "1"
const subs = new Map()
ext({ on: (name, fn) => subs.set(name, fn) })
const handler = subs.get("tool_call")

let raw = ""
process.stdin.setEncoding("utf8")
for await (const chunk of process.stdin) raw += chunk
const out = JSON.parse(raw).map((command) => {
  const r = handler({ toolName: "bash", input: { command } })
  return { blocked: !!(r && r.block), reason: r && r.reason ? r.reason : "" }
})
process.stdout.write(JSON.stringify(out))
