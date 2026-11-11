# Pointer artifact registry & resolution (review-gate v2, t09)

`prose_pointers` demands name artifacts (reports, runbooks) that pointer
utterances in the code reference — e.g. `REPORT = "out/weekly-2026-11.md"`.
The collectors (`scripts/ocr_collectors.py`) ask the resolver in
`scripts/ocr_forge.py` where each target comes from. This document is the
contract for the registry file and for what happens when the forge is
unreachable.

## Registry: `.ocr/artifacts.json`

A project-maintained, **committed** JSON object at the repo root:

```json
{
  "weekly-2026-11.md": {
    "produced_by": "app/reports.py",
    "path": "out/weekly-2026-11.md",
    "mr": "!42"
  }
}
```

- **Keys** name the artifact. Matching against a pointer target is loose, the
  same way the collectors match pointer utterances: basenames and stems
  compare either way, so `weekly-2026-11`, `weekly-2026-11.md` and
  `out/weekly-2026-11.md` all name the same entry.
- **`produced_by`** (string) — who makes the artifact: a script, a job, a
  team. Free text, surfaced verbatim to the reviewer.
- **`path`** (string) — where the artifact lands, repo-relative or a URI.
- **`mr`** (optional string) — the merge request that introduced the artifact
  (`"!42"` on GitLab, `"#42"` on GitHub).

A missing registry file is normal (tier skipped silently). A malformed or
non-object registry degrades with a warning on the resolution dict — never an
error — and resolution continues to the lower tiers.

## Resolution tiers

For each pointer target the resolver tries, in order:

1. **Registry** — the `.ocr/artifacts.json` entry above.
2. **Repo grep** — `git grep` for the target's basename over the working tree,
   capped at 10 hits. Hits that are the target's *own utterance sites*
   (per the event index) are excluded: a pointer spoken somewhere but
   produced nowhere is exactly the dangling-pointer shape the reviewer is
   asked to catch, and grepping the utterance back would mask it.
3. **Sibling branch tips** — the same grep against local branch refs (current
   branch excluded), capped at 8 branches, for artifacts produced on a
   sibling MR.
4. **Forge call** — a live search through the forge CLI, autodetected from
   `git remote get-url origin`: `github.com` remotes use `gh` (the GitHub
   CLI), `gitlab.com` and self-hosted GitLab remotes use `glab`.

The first tier that matches wins; a `resolved` resolution always names the
tier (`via`) and carries its evidence (`produced_by`/`path`/`mr`,
`hits`, `branches`, or forge `hits`).

## Statuses and degrade semantics

Every resolution is one of:

| status        | meaning                                                                 |
|---------------|-------------------------------------------------------------------------|
| `resolved`    | a tier located the artifact; `via` names which tier.                    |
| `unresolved`  | every tier ran to completion and nothing matched — the reviewer's medium-finding case ("resolved by no tier, in no registry"). The bundle carries it verbatim; the Python side never fabricates a finding. |
| `unavailable` | coverage was local-only: the forge tier could not execute (no origin remote, CLI not installed, auth failure, offline, or timeout). A `warning` on the dict says why, and `searched` names the tiers that did run. |

Forge-call degradation is deliberate and bounded:

- **Hard 3-second timeout** per call (subprocess-level; the spec accepts this
  stall risk in the blocking lane and caps it).
- Missing CLI, no remote, non-zero exit (auth/offline) and the timeout all
  degrade identically: tiers 1–3 still ran, the result is `unavailable` with
  a human-readable warning, and nothing raises.
- **Negative caching**: forge misses and forge failures are cached per
  (CLI, target-name) — in-process for the duration of one collectors run and
  persisted to `.ocr/index/forge_negcache.json` (untracked, at most 200
  entries, 24h TTL). A degraded forge therefore costs at most one bounded
  call per push, not one per pointer.
