# Native review operations and gated rollout

This runbook describes the implemented native transport and its current
pre-certification gate. The versioned review contract, acceptance registry,
and durable store remain canonical; this document does not certify real-model
execution. **#21 real native-session acceptance is still required. No real
model acceptance was run for this documentation ticket.**

ADR 0009 records the rollout decision and historical-evidence constraints.

## 1. Transport selection: legacy by default

The two opt-ins are independent and affect different commands.

| Surface | Default before #21 acceptance | Explicit native opt-in | Invalid selection |
| --- | --- | --- | --- |
| `review-gate.py final-review` | Unset/blank `RG_FINAL_REVIEW_TRANSPORT`, or `legacy`: existing final-review worker. | `RG_FINAL_REVIEW_TRANSPORT=native` | Any other value is rejected before standalone or multi-reference dispatch. |
| Deep pre-push review | Unset `OCR_DEEP_TRANSPORT`, or `legacy`: existing deep reviewer. | `OCR_DEEP_TRANSPORT=native` | Unknown values, including an explicitly blank value, fail closed. |

Values are trimmed and case-folded. `NATIVE` is therefore an intentional
opt-in; a misspelling is not a fallback request. Enabling one variable does
not enable the other. Stub and test-double transports in the test suite set
their native opt-ins explicitly and are not production certification.

For a local final-review experiment that cannot mutate provider state:

```bash
export RG_FINAL_REVIEW_TRANSPORT=native
export FINAL_REVIEW_STORE="$HOME/.final-review/final-review.sqlite3"
python3 scripts/review-gate.py final-review --no-publish '!123'
```

For multiple references, retain explicit base-first order:

```bash
python3 scripts/review-gate.py final-review --no-publish '!17' '!18' '!19'
```

For an opted-in deep pre-push review, export the variable in the environment
that starts Git and the detached supervisor:

```bash
export OCR_DEEP_TRANSPORT=native
git push origin HEAD:main
```

Normal deny-first-push/retry-join behavior still applies. A native opt-in is
not an instruction to run nested agents. The production adapter launches
`kimchi --mode rpc --no-session` and sends a correlated `prompt` command over
strict JSONL. It waits for the final assistant message and `agent_settled`;
an accepted prompt response or `agent_end` does not prove completion.
Cancellation sends a correlated `abort` before bounded process-group cleanup.
Native session IDs contain the explicit run/task identity, not a bare PID.

### No silent fallback

Before dispatch, the native adapter checks `kimchi --help` for the direct
session and restriction flags it needs. This help-only probe makes no inference
call. A missing executable, unsupported flags, launch error, malformed result,
or native deadline is a visible failure/incomplete outcome. It never selects a
legacy worker, an Agent call, or a nested harness as a substitute.

## 2. Stack shapes and top-level task scheduling

### Final review

| Invocation | Required units | Native lens tasks at full eligibility |
| --- | --- | --- |
| One isolated reference (standalone) | `individual:1`, `combined` | 14: seven mandatory lenses per unit |
| N explicitly ordered references | `individual:1` ... `individual:N`, `combined`, `prospective` | `7 × (N + 2)` |

Individual work checks each item against its declared target. Combined work
checks cross-item contracts and integration behavior. Prospective work checks
integration against the current root target. No scheduler may replace one of
these scopes with another or omit a mandatory lens.

A run-scoped scheduler receives tasks and dependencies up front where their
immutable trees are available. Eligible individual, combined, and prospective
lens tasks can overlap; there is no artificial requirement to finish every
individual callback before launching combined inference. Trusted dependency
and critical-stop mapping controls eligibility and later finalization.
Aggregation and tests consume results in the established unit/lens order, not
callback order.

`RG_FINAL_LENS_WORKERS` selects the inference cap for standalone/stack lens
work. It defaults to 4, treats blank/non-numeric values as 4, and clamps other
values to 1..4. `RG_FINAL_NATIVE_MODEL` defaults to `kimi-k3.1-pro` and is
recorded in every task identity.

The scheduler has separate resource classes with starting limits of **4
inference** and **2 execution** tasks. `RG_FINAL_EXECUTION_WORKERS` sets the
execution limit. These are initial safeguards, not benchmark-derived optima.
They bound one native run's scheduler, not independent review runs.
Required/dynamic execution starts after its unit's mandatory lenses reach
terminal outcomes. Ordinary lens failures preserve independent test evidence;
trusted STOP dependencies mark affected execution ineligible. Individual
execution does not wait for combined evaluation, and execution subprocesses
belong to the same run resource owner as inference workers. Execution results
persist when their task callbacks complete, without becoming model artifacts.

### Legacy deep review

Opted-in native deep review has two forms:

- A direct review without a decomposition paths file launches one `deep` lens
  task and preserves the existing legacy result path.
- A supplied paths file launches deterministic bounded subsystem manifests,
  each owning at most four changed paths, plus exactly one `global` manifest.
  Paths are sorted, grouped by their first path component (`<root>` for root
  files), and partitioned without reordering-dependent aggregation.

Every changed path belongs to exactly one subsystem task. `other_changed` and
`native_deep_task.context_files` retain all other changed paths as context for
call-chain checks, not as permission to omit them from global assessment.
Subsystem duties are `changed-file-contracts`, `changed-call-chains`, and one
`file:<path>` duty per owned path. The global task owns
`cross-file-call-chains`, `cross-file-api-contracts`, and `global-invariants`.

Each manifest records its task ID, kind, duties, context files, and
`manifest_sha256`. A valid worker result includes exactly matching
`native_deep_evidence`: `task_id`, `input_sha256`, `manifest_sha256`,
`reviewed_paths`, and `completed_duties`. The supervisor rejects omitted,
repeated, or misidentified evidence and verifies that file-task coverage is
exactly the parent changed-path set. Existing legacy merge, memory, waiver,
falsification, and verdict processing then handles validated results; task
completion order does not choose the verdict.

## 3. Callback, artifact, and lifecycle contracts

### Task identity

A `native-review-task/v1` record contains the transport and an immutable
identity with:

- `run_id`, `task_id`, `session_name`, `unit_id`, `stage`, and `lens`;
- the immutable `revision_tuple` and SHA-256 `input_sha256`;
- `backend`, `model`, and `deadline_at_unix`.

For final review, task IDs have the form `<unit-id>:<lens>`. Files replace
characters outside `[A-Za-z0-9_.-]` with `-`; the durable identity retains the
original task ID.

### Completion and commit

The host delivers one terminal callback containing the exact native session
ID, return code, captured stdout/stderr, and start/finish timestamps. Protocol,
provider, process, and launch failures use the same once-only callback path.
The executor accepts a `final-review-worker-result/v1` JSON payload but does
not publish it. The supervisor validates the payload and STOP request, then
atomically writes a `native-review-artifact/v1` envelope with task identity,
transport, native session ID, worker payload, and timing.

An output file alone is not accepted evidence. The scheduler calls
`commit_native_artifact`; the supervisor atomically commits the artifact file,
an idempotent callback/message receipt, and the completion event as
`native-review-artifact-commit/v1`. File or database failure leaves no success
file or committed success event, and failed publication produces structured
unsuccessful evidence. Run cancellation, finalization, and same-store
supersession fence late commits; their output remains diagnostic only. Lens aggregation only
consumes this validated path. Duplicate delivery has no second state effect;
conflicting reuse of an event/message identity is an error. Cancellation fences
the exact run/session handle so late output cannot replace active evidence.
Temporary `.tmp-*` files are not valid artifacts.

Final-review files live below the selected store directory:

```text
<store-parent>/native/<run-id>/requests/<safe-task-id>.json
<store-parent>/native/<run-id>/artifacts/<safe-task-id>.json
<store-parent>/final-review.sqlite3                 # unless another name was selected
<store-parent>/reports/<run-id>-<safe-unit-key>.json
<store-parent>/reports/<run-id>-<safe-unit-key>.md
```

The default database is `~/.final-review/final-review.sqlite3`; an explicit
`FinalStore(path)` or view `--store PATH` wins over `FINAL_REVIEW_STORE`.
Native request/result artifacts do not move when viewing another database;
they are run-local supporting files, while the selected SQLite records are the
portable evidence authority.

Legacy deep decomposition writes beside that run's asynchronous state:

```text
<async-dir>/native-<run-id>-deep-tasks.json  # legacy-deep-native-journal/v1
<async-dir>/manifest-<task-id>.json
<async-dir>/native-<task-id>.json            # native-review-artifact/v1
```

The journal deliberately does not invent final-review run/unit rows. Direct
single-task deep review uses `<async-dir>/native-<run-id>-deep.json`, a
journal-backed supervisor commit, and the existing supervisor log/result
state.

After interruption, a later partitioned attempt validates each accepted
journal entry against its artifact file, immutable identity, and coverage
before reusing it. It executes only unfinished tasks with a fresh runtime
deadline and attempt-scoped event identities. Conflicting files, uncommitted
artifacts, stale identities, and omitted or repeated coverage fail closed;
they never authorize replacement inference or rewrite accepted evidence.

### Lifecycle and views

`native-review-event/v1` supports `queued`, `started`, `completed`, `failed`,
`warning`, `timeout`, `cancelled`, `cleanup`, and `finalization`. Events retain
identity, transport, native session, occurrence/start/finish/duration fields,
and bounded details. Not every run reaches every state. Accepted artifacts are
incremental evidence, not independent finalization authority.

Final-review progress is written to stderr; the final CLI result is one JSON
object on stdout. Inspect a run without launching a model or contacting a
provider:

```bash
python3 scripts/review-gate.py final-report show RUN_ID --store /path/store.sqlite3
python3 scripts/review-gate.py final-report show RUN_ID --store /path/store.sqlite3 --json
python3 scripts/review-gate.py final-report export RUN_ID \
  --store /path/store.sqlite3 --format markdown --output -
```

JSON reports include `native_tasks` with events, committed artifacts, and
artifact callback receipts. Markdown adds a **Native task lifecycle** section
only when those records exist. A legacy or pre-native run therefore keeps its
prior report meaning rather than gaining a claim that native work ran.

CLI final-review exit codes remain 0 for `pass`, 1 for `findings`, 2 for
`incomplete` or usage/orchestration failure, and 3 for `stale`. Publication
transport failure does not change the review state or exit code into a green
result.

## 4. Prerequisites and failure classification

Install prerequisites before review. The engine does not implicitly install
global packages, select a package manager, or provision a toolchain.

- **Supervisor:** Python 3 and Git on `PATH`.
- **Native inference:** a `kimchi` executable with `--mode rpc`,
  `--no-session`, `--no-approve`, `--no-auto-update`, `--model`,
  `--mcp-config`, `--allow-tool`, and `--deny-tool` support, plus the operator's
  model authentication.
- **Provider graph views/review:** authenticated `gh` for GitHub or `glab` for
  GitLab, as selected by the reference. `--no-publish` disables status writes,
  not the provider metadata reads needed to discover and pin the graph.
- **Required suites:** every command executable and language module must
  already exist in the pinned checkout or on the credential-free execution
  `PATH`. For `python -m <module>`, that module must be importable by the
  selected interpreter.

For reconstructed GitHub workflows, supported checkout materialization and
`actions/setup-python` declarations require a literal `major.minor` Python
version. The runner resolves `pythonX.Y` on `PATH`, verifies its actual version,
and supplies matching `python`/`python3` links inside that suite's isolated
environment. It does not emulate arbitrary actions or expressions. Supported
finite literal matrices remain separate declarations; unsupported forms are
explicitly unassessable rather than silently omitted.

| Observation | Evidence/classification | Operator meaning |
| --- | --- | --- |
| Missing/mismatched Python, missing command, unavailable `python -m` module, Git/CLI prerequisite failure | Required suite `state=environment`, `failure_type=environment`, `blocking=true`; overall required evidence is incomplete. No source finding is invented. | Fix the environment as directed by `missing_tools[].remediation`, then rerun. This is not a test pass or source-caused test failure. Exit 126/127 retains attempts where available and follows the same environment classification. |
| Required command fails normally on its initial run and its one clean rerun | Suite `state=fail`, blocking required-verification finding, overall review `findings` | Reproduced repository/test failure; inspect both attempts and evidence fingerprint. |
| Initial required-command failure does not reproduce on the clean rerun | Suite `state=incomplete`, no fabricated pass/finding | Treat as inconclusive evidence; investigate nondeterminism or environment and rerun separately. |
| Required suite timeout/discovery/execution error | Incomplete required evidence, not a reproduced source failure | Correct the timeout/declaration/environment and rerun; it cannot publish green. |
| A model lens returns a validated actionable defect | Ordinary finding through existing falsification/memory/verdict rules | This is a model finding, not a missing-tool environment error or a test execution result. |

A required-suite declaration in the pinned `.ocr/config.json` needs literal
JSON `"required": true`, must not be disabled, and needs a non-empty command.
No declaration is recorded `absent` with a warning, never as a passing suite.

## 5. Timeout budgets and cleanup margins

| Setting | Default/bounds | Scope and operator guidance |
| --- | --- | --- |
| `RG_FINAL_REVIEW_TIMEOUT` | 1800 seconds; finite positive required | Native standalone/stack task-execution phase, after provider discovery and required tree/context preparation. Expiry stops queued work, cancels exact owned native handles, performs bounded cleanup, preserves accepted evidence, and yields controlled incomplete; it does not retroactively bound earlier provider discovery. |
| `RG_FINAL_REVIEW_EXTERNAL_WATCHDOG_TIMEOUT` | Unset; finite positive when supplied | Declares a last-resort external kill. The implementation reserves a fixed 30-second cleanup margin and shortens the controlled deadline to `min(run timeout, external timeout - 30)`. Set the external watchdog later than the intended whole-run budget **plus at least 30 seconds**. Values <=30 leave no safe controlled window and fail before dispatch. |
| `RG_FINAL_WORKER_TIMEOUT` | 900 seconds | Per-worker runtime deadline for native inference and required/dynamic execution. RPC workers receive `abort` before bounded cleanup; execution workers cancel their owned subprocess groups. Worker deadline events are `timeout`, not ordinary operator cancellation. |
| `OCR_DEEP_TIMEOUT` | 600 seconds, minimum 60; invalid numeric falls back to 600 | Legacy or opted-in native deep-review whole deadline. Native decomposition applies this deadline across its task set, including cleanup coordination. |
| `OCR_TIMEOUT` | 1800 seconds | Ordinary non-deep legacy reviewer wall clock. |
| `OCR_CHUNK_TIMEOUT` | 1200 seconds, minimum 60 | Existing bounded legacy chunk lane, not a hidden retry budget. |
| Required declaration `timeout` | 60 seconds; clamped to 1..3600 | Per required-suite attempt. A runner API may separately provide a default/override, which is recorded in `timeout_policy`; there is no global package provisioning or automatic timeout escalation. |

External watchdogs remain backstops. They should not be the normal way a native
run ends. Native cancellation uses owned handles/process groups and bounded
pipe drain/reap; it never matches unrelated processes by name. Sanitized
stdout/stderr tails are bounded diagnostics, not substitutes for missing
artifacts.

## 6. Deterministic aggregation, migration, and publication

Native callbacks can arrive in any order. Accepted native artifacts feed the
existing lens outcomes in required-lens order, and findings/warnings pass the
existing falsification and memory pipeline. `ocr_verdict.compute_verdict` is
still the only finding-verdict authority. There is **no DeepSeek stage, no
model join, and no nested review session**.

Final store schema v4 adds four tables: `native_tasks`,
`native_task_events`, `native_artifacts`, and `native_artifact_messages`.
Migration is additive and opening a view does not create review/acceptance
results. Existing run, unit, lens, test, finding, contract, acceptance, trust,
and timestamps stay unchanged. For legacy/pre-native reports,
`native_tasks == []`; this means “no native task records,” not “all required
work completed.” In particular, a historical interrupted/incomplete run stays
incomplete, and missing old test rows are not reinterpreted as executed tests
or as proof that tests never started.

Provider publication is requested by default by `final-review`; use
`--no-publish` for pre-certification experiments. `--publish` merely requests
trusted publication; it does not override the guards. A provider green needs
all required individual/combined/prospective scopes, mandatory lenses/tests,
memory and freshness, final `pass`, same-store continuity, a non-stale and
non-superseded current run, and trusted orchestration. Independent,
incomplete, stale, findings, local-only, and superseded runs cannot publish
green. Native workers and test executors receive no provider-write tokens and
never mutate reviewed branches. Permission/transport errors preserve
local-only unenforced evidence.

## 7. Rollback and explicit legacy transition

Transport selection applies to a new run. To stop opting in:

```bash
unset RG_FINAL_REVIEW_TRANSPORT OCR_DEEP_TRANSPORT
# Equivalent explicit selection for either lane:
export RG_FINAL_REVIEW_TRANSPORT=legacy
export OCR_DEEP_TRANSPORT=legacy
```

Start a new final review or retry the intended pre-push after an in-flight
native run has completed controlled cancellation/cleanup. Changing a shell
variable does not rewrite an already-running supervisor's environment. Do not
delete old SQLite rows or native journals to perform rollback.

The legacy run creates no native final-review task section and uses the
established worker path. Prior native tasks, callbacks, artifacts, findings,
and incomplete states remain historical evidence under their original run and
revision identities. If native capability is fixed later, opt in again with a
new run; there is no automatic transport migration or background fallback.
Only a separately reviewed #21 acceptance change may promote native to either
default.
