<!--
  Portions of this file (the review checklist and severity definitions) are
  adapted from open-code-review (ocr): https://github.com/alibaba/open-code-review
  Licensed under the Apache License, Version 2.0.
  Modified for this project: re-expressed for native Claude Code execution and
  extended with explicit severity/confidence guidance. See the repository NOTICE
  file for full attribution.
-->

# Review checklist (default rubric)

Use this as the default `system_rule` when reviewing a file, unless a project or
user rule override applies (see the rule-resolution order in `SKILL.md`). Review
strictly through these lenses and ignore concerns that fall outside them.

#### Correctness
Is the logic correct? Are there missing boundary conditions?
Are exceptions handled properly?
Is it thread-safe in concurrent scenarios?

#### Security
Are there security vulnerabilities such as SQL injection or XSS?
Is sensitive information handled correctly?
Is permission validation complete?

#### Performance
Are there obvious performance issues (e.g., N+1 queries, unnecessary loops)?
Are resources properly released?

#### Maintainability
Is the code clear and easy to understand?
Do names accurately express intent?
Does it follow the project's existing code style and architecture patterns?

#### Test Coverage
Do critical logic paths have corresponding test cases?
Do test cases cover boundary conditions?

---

## Severity definitions

Assign every finding exactly one severity:

- **high** — may cause security vulnerabilities, data loss, data corruption,
  system crashes, or critical functional failures.
- **medium** — may affect performance or maintainability, or involves a
  plausible edge-case failure.
- **low** — code style, readability, or non-critical best-practice suggestions.

## Confidence

Assign every finding a `confidence` in `[0.0, 1.0]` reflecting how sure you are
the issue is real **after** your falsify pass:

- `>= 0.7` — you verified it against the actual code; the problem is concrete and
  will plausibly be hit in practice.
- `0.4 – 0.7` — likely real but you could not fully confirm it.
- `< 0.4` — speculative; prefer to drop rather than emit.

Only `high`-severity findings with `confidence >= 0.7` block a commit, so be
honest: do not inflate confidence, and do not mark something `high` unless it
truly fits the definition above.
