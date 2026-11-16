# Review-gate configuration reference

User-level configuration for the review gate. Precedence, highest first:

1. environment variable,
2. `~/.config/review-gate/config.json` (override the path with `OCR_CONFIG_FILE`),
3. built-in default.

Invalid values at any level warn and fall through to the next level; a
missing or malformed config file is not an error. Decision record: ADR
0006.

## File shape

```json
{
  "review": {
    "depth": "full",
    "timeoutSeconds": 0,
    "failOpen": false
  }
}
```

## Keys

| Key | Values | Default | Env override | Notes |
|---|---|---|---|---|
| `review.depth` | `full`, `structural`, `large-diff-scoped` | `full` | `OCR_REVIEW_DEPTH` | `full` is the operator default: always full-depth review. `structural` reviews per-file skeletons and hotspots; `large-diff-scoped` scopes very large diffs and names the files not deeply read. |
| `review.timeoutSeconds` | integer ≥ 0 | `0` (uncapped) | `OCR_REVIEW_TIMEOUT_SECONDS` | Wall-clock cap for the reviewer call. On expiry the gate fails closed; the escape routes below apply. Values above the hook harness's own timeout (`hooks/hooks.json`, **900s**) trigger a loud gate warning: under the PreToolUse adapters the host kills the hook at 900s, and a killed hook is a silent fail-open. The value is warned about, never clamped. |
| `review.failOpen` | boolean | `false` | `OCR_FAIL_OPEN` (`1`/`true`/`yes`) | When true, gate-internal failures allow the push after recording an audited bypass. Prefer the one-shot env usage; a persistent file setting silently weakens every push. |

## Related environment surface (pre-existing)

| Variable | Meaning |
|---|---|
| `OCR_CONFIG_FILE` | Redirects the config file location (tests, sandboxed runs). |
| `OCR_STATE_DIR` | Redirects the external state store (`~/.local/state/review-gate` default). |
| `OCR_FAIL_OPEN` | One-shot fail-open for a single invocation; every use is recorded in `bypasses.jsonl` and announced. |

`git push --no-verify` skips git-hook enforcement entirely for that
push. It never invokes the hook at all, so it is **unrecordable by
design**: nothing lands in `bypasses.jsonl`, and the forge's own audit
trail is the only evidence. The recorded bypass is the
`OCR_FAIL_OPEN` / `review.failOpen` path. All bypass records live in
the state store with timestamp, mechanism, remote, ref, and tip.

## Installer flags

`install-git-hook.sh` installs the global pre-push hook **by default**
(chaining any pre-existing global `core.hooksPath` — see ADR 0006(a)).
Pass `--no-git-hook` for the legacy opt-in behaviour, `--chain-into
<repo>` to also chain a repo-local hook set, and the documented
uninstall to restore the backed-up previous hooksPath.
