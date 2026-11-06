// review-gate-deny.ts -- read-only sandbox for the review-gate worker (port D13).
//
// Loaded into the headless reviewer via `kimchi -p ... -e <this file>`.
// Why an extension and not CLI flags: `--deny-tool`/`--allow-tool` and
// KIMCHI_PERMISSIONS are NOT enforced in headless -p runs on this build
// (verified, port spec section 6) -- sessions report permission_mode=yolo.
// A tool_call subscriber is enforced regardless of mode.
//
// Scope of THIS version (C2): hard-deny the mutating tools while the worker
// marker OCR_IN_REVIEW=1 is in the environment. C3 hardens it: a bash
// allowlist (git read verbs + grep/find/ls/cat/...), mcp__* denial, the
// startup canary (the worker's first instructed action is a deliberate write
// that must fail -- the only detector for a silently-unloaded -e extension),
// and a bun-test suite including an inertness test (env unset => no change).
//
// Verified live on this machine: with this file loaded and OCR_IN_REVIEW=1,
// the write tool is refused with the reason below.

export default function denyExtension(pi: any) {
  pi.on("tool_call", (event: any) => {
    if (process.env.OCR_IN_REVIEW !== "1") return undefined
    if (["write", "edit", "web_fetch", "web_search"].includes(event.toolName)) {
      return { block: true, reason: "review worker is read-only (tool " + event.toolName + " blocked)" }
    }
    return undefined
  })
}
