# ADR 0005 — Claude backend: per-profile tier models, provider-guard scope, helper allowlist and Jira routing

## Status

Accepted (2026-11-16). Covers the config-surface and decision changes the
post-unification hardening introduced for the `claude` profile, following the
house rule ADR 0002/0003/0004 established: environment switches and
operator-visible behaviour carry decisions.

- (a) `OCR_CLAUDE_SPOTTER_MODEL`, `OCR_CLAUDE_TIER1_MODEL`,
  `OCR_CLAUDE_TIER2_MODEL` (and `OCR_CLAUDE_MODEL`) — per-profile tier models;
- (b) the kimchi provider guard stands down under the `claude` backend;
- (c) the claude review worker may run exactly one helper command, the
  conventions resolver, published as `manifest.helper_cmds`;
- (d) the Jira side-fetch rides the selected backend.

## Context and decisions

### a) Per-profile tier models
The tier models were kimchi ids (at the time kimi-k3 and deepseek-v4-flash;
the kimchi defaults are now kimi-k3.1-pro and deepseek-v4.1-flash-1017, see
`docs/config-reference.md`). Under `claude`
they are meaningless. Each profile now resolves its own tier models: kimchi
keeps the existing defaults and `OCR_*_MODEL` switches; claude defaults to the
`haiku` / `sonnet` / `opus` aliases for spotter / tier1 / tier2 and honours
`OCR_CLAUDE_<ROLE>_MODEL`. The shared `OCR_*_MODEL` variables are honoured
under claude only when they name an Anthropic model, so a kimchi-targeted
override never leaks across. The four new variables are in the review
fingerprint, so changing one invalidates cached verdicts.

### b) Provider guard scope
The guard exists to protect the kimchi account from Anthropic/OpenAI models.
Under `claude` those models are the point, so the guard returns no violations
there. It is unchanged under kimchi. `skills/review/SKILL.md` states the
carve-out.

### c) Helper allowlist
Claude's sandbox is its argv. The conventions resolver is the only helper the
review skill needs; one `Bash(<cmd> *)` rule for exactly that command is added
to the review worker's allowlist (never to the docs-summary worker) and the
command is published as `manifest.helper_cmds` so the skill uses it verbatim.
The demands validator needs JSON on stdin, which the Bash tool's safety filter
refuses, so it stays unavailable under claude and degrades with a warning.

Kimchi note (ADR 0010): the kimchi profile also publishes `manifest.helper_cmds`,
for exactly three scripts (conventions, demands, collectors) admitted by the
deny extension by exact path and fixed argument grammar; the demands validator's
heredoc stdin is accepted only for those commands. The kimchi sandbox is
otherwise unchanged (`python -c` and every other script stay denied).

### d) Jira routing
The side-fetch follows the selected backend: claude runs
`claude -p --strict-mcp-config` against the Atlassian/Jira connector in
`~/.claude.json` (`OCR_JIRA_MCP_CONFIG` overrides); kimchi is unchanged. Every
failure remains a silent miss note; no token is stored in gate config.

## Consequences

Telemetry records carry the `backend` field so the two profiles can be told
apart. Future config-surface changes to either profile require updating this
ADR or a successor.
