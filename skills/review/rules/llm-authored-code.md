# LLM-authored code review rules

Apply these to ALL files when the change set was authored by an LLM (Claude,
GPT, etc.). These rules target the specific, recurring failure modes of LLM
code generation — they are distinct from the generic correctness rubric.

## Incomplete refactors — Correctness / high
LLMs rename, remove, or replace a symbol in the file they are editing but miss
other files that reference the old symbol. This is the #1 failure mode.

**What to check:**
- If a function, class, variable, or constant is renamed or removed, Grep the
  full repo for the old name. Any hit in another file that was NOT also changed
  is an incomplete refactor.
- If a mechanism is removed from one file (an early-return guard, a rate limit,
  a deduplication check), Grep for equivalent logic in other files — the
  mechanism may need updating there too.
- Pay special attention to import statements: a removed symbol must be removed
  from every importer.

```
Commit rule (CLAUDE.md): "Any commit that reverts, exempts, or removes a
mechanism must state the grep count before and after the change."
```

## Mock-patching wrong namespace — Test Coverage / high
LLMs patch the module where a class or function is **defined**, not where it is
**imported and used**. The mock has no effect; the test exercises the real code
path and produces a misleading green result.

```python
# WRONG — patches definition, not usage site
@patch('app.utils.requests.get')        # utils is where requests is imported
def test_fetch(mock_get): ...

# CORRECT — patch where the module under test imports it
@patch('app.service.requests.get')      # service is the module under test
def test_fetch(mock_get): ...
```

**What to check:** When a test uses `@patch` or `unittest.mock.patch`, verify
that the patch target string matches the import path in the **module being
tested**, not the module where the symbol originated.

## Tests patched-to-green — Test Coverage / high
LLMs edit test assertions to match the actual (potentially wrong) output rather
than fixing the underlying code. A test that was previously failing is now
"passing" because its assertion was weakened or changed to reflect the bug.

**Signs to look for:**
- An assertion value changed from a specific expected value to a broader match
  (`assert result == 42` → `assert result is not None`)
- A previously strict comparison replaced with `assertIn`, `assertTrue`, or a
  similar looser check
- A test's expected value changed to match new behaviour without any explanation
  in the commit message of why the new behaviour is correct

## Early-return silently skipping behaviour — Correctness / high
LLMs add an early-return guard (`if not condition: return`) that looks defensive
but silently skips all the logic below it in cases where the logic should run.

**What to check:** For every new early-return added in the diff, verify that
the cases it skips are genuinely no-ops. If the return is inside a loop or a
function that callers expect to always perform work, the skip is a correctness
defect even if it looks like a guard clause.

## Hallucinated or signature-changed API calls — Correctness / high
LLMs call functions with the wrong number of arguments, in the wrong order, or
with keyword arguments that do not exist. This is especially common after a
refactor: the LLM updates the call site with a new signature it inferred rather
than the one actually defined.

**What to check:** For any function call in the diff where the function was
also changed in the same diff (or recently), verify the call site matches the
current definition signature exactly — parameter count, names, and types.

## Silent error swallowing — Correctness / medium
LLMs frequently add `except Exception: pass` or `except Exception: return None`
to "handle" errors. This turns hard failures into silent wrong-value returns,
making failures invisible to the caller and to monitoring.

**What to check:** Every new `except` clause that does not re-raise, log, or
return a meaningful error indicator is suspect. At minimum it should log the
exception at WARNING or ERROR level.

## Missing error propagation — Correctness / medium
A function that can fail returns `None` on error (or a boolean `False`) instead
of raising or returning an error type. The caller cannot distinguish "not found"
from "failed with exception".

## Commit cross-contamination (multi-agent repos) — Correctness / high
In repos where multiple LLM agents work concurrently, `git add -A` or `git add .`
stages files modified by other agents. The commit then contains unrelated changes
under an unrelated message.

**What to check in diffs:** If the diff contains changes to files that are not
mentioned in the commit message or the stated task, flag them. Legitimate commits
use explicit file paths: `git commit -m "..." -- path/to/file`.

## Commit message without file:line references — Maintainability / low
Per project rules (CLAUDE.md), every behavioural claim in a commit message must
include a `file:line` reference. A commit message like "Fixed the credit-deduction
skip" with no file reference cannot be traced; it may describe a change that is
incomplete or in the wrong file.

This is a low-severity finding (commit messages are not runtime behaviour) but
flag it when the commit is in the change set being reviewed.
