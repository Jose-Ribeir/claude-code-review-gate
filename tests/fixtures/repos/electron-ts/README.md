# electron-ts — seeded-bug Electron fixture repo

Small Electron + TypeScript fixture used by the C5 acceptance runs. The
seeded review-relevant bugs live in the git history (one commit per bug):

- `feat: settings window` — `nodeIntegration: true` plus
  `contextIsolation: false` in a BrowserWindow's webPreferences.
- `feat: renderer diagnostics panel` — orphan IPC channel: the renderer
  `ipcRenderer.send("channel-x", ...)` has no matching
  `ipcMain.on("channel-x")` anywhere in the repo.
- `feat: diagnostics window handler` — `ipcMain.handle("diag:open", ...)`
  without any sender validation (no `event.senderFrame` check), in
  contrast to the validated `app:open` handler from the base commit.
- `refactor: rename formatBytes to formatSize` — the rename in
  `src/main/util.ts` leaves `src/renderer/panel.ts` (and `status.ts`)
  importing the old name: a type-level break LSP/tsc can see.

`tsc --noEmit` passes at the clean base (first two commits) and fails at
the tip (the stale `formatBytes` imports are the seeded break). No
`node_modules` is vendored; the `electron` module only resolves when a
typescript install is visible (see the port spec's LSP notes).

The git history (6 commits on `main`) lives in the sibling bare repo
`../electron-ts.git`. This directory is just the worktree snapshot at the
tip — it deliberately carries no `.git`.

    git clone tests/fixtures/repos/electron-ts.git my-et
    cd my-et && git log --oneline

Per-bug review ranges (oldest commit = 1): HEAD~5..HEAD~4 clean,
HEAD~4..HEAD~3 nodeIntegration, HEAD~3..HEAD~2 orphan channel,
HEAD~2..HEAD~1 missing sender validation, HEAD~1..HEAD rename-broken
caller.
