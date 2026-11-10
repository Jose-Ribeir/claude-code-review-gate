<!--
  The supported-extension set and default exclusions are adapted from
  open-code-review (ocr): https://github.com/alibaba/open-code-review
  Licensed under the Apache License, Version 2.0. Modified for this project.
  See the repository NOTICE file for full attribution.
-->

# File selection: allowlist and exclusions

The orchestrator applies this list when choosing which changed (or scanned)
files to review. A file is reviewed only if its extension is allowed **and** it
does not match an exclusion. Binary files and deletions are always skipped.

## Allowed source extensions

```
.c .h .cc .cpp .cxx .hpp .hh .cs .go .rs .java .kt .kts .scala .swift
.py .pyi .rb .rake .gemspec .php .pl .pm .lua .r .jl .dart .groovy
.ex .exs .erl .hrl .ets .clj .cljs .vb .fs .m .mm
.js .jsx .mjs .cjs .ts .tsx .vue .svelte .astro
.sql .sh .bash .zsh .fish .ps1 .psm1
.html .htm .css .scss .sass .less
.tf .hcl .proto .graphql .gql
.ftl .ftlh .ftlx
.po .pot
```
Config/markup files (`.json .yaml .yml .toml .xml .md .gradle .properties`) are
reviewed only when a project rule explicitly opts them in, or in `--scan` mode.

## Two filters: review vs context search

The orchestrator applies two related admission filters:

- **Review filter** — decides which changed (or scanned) files are *reviewed*.
  Everything in "Default exclusions" below applies.
- **Context-search filter** — decides which repository files may be *searched*
  for callers, cross-file references, and sibling occurrences of a defect
  (impact analysis). Spec/test files are **not** review targets, but a caller
  living in a spec or test file is exactly where impact analysis must look;
  excluding them there stranded real call sites. The context filter therefore
  drops only the test-family exclusions (**Tests** below) and keeps everything
  else: the vendored/generated, VCS, lockfile, and generated-suffix exclusions
  plus the extension allowlist all still apply.

## Default exclusions (never auto-reviewed, tests still context-searchable)

- **Tests:** `**/*_test.go`, `**/*.test.{js,jsx,ts,tsx}`, `**/*.spec.{js,jsx,ts,tsx}`,
  `**/test_*.py`, `**/*_test.py`, `**/tests/**`, `**/__tests__/**`, `**/testdata/**`
- **Vendored / generated:** `**/vendor/**`, `**/node_modules/**`, `**/dist/**`,
  `**/build/**`, `**/out/**`, `**/target/**`, `**/.next/**`, `**/__generated__/**`,
  `**/*.min.js`, `**/*.pb.go`, `**/*.generated.*`
- **VCS / tooling:** `**/.git/**`, `**/.idea/**`, `**/.vscode/**`
- **Lockfiles:** `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `go.sum`,
  `Cargo.lock`, `poetry.lock`, `composer.lock`

A project rule may include otherwise-excluded paths (an explicit include wins
over the default exclusions); see `SKILL.md` for the resolution order.
