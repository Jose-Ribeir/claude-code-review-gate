# py-app — seeded-bug Python fixture repo

Small Python fixture (8 files) used by the C5 acceptance runs. The seeded
review-relevant bugs live in the git history (one commit per bug):

- `perf: faster user lookup (raw SQL)` — SQL injection: `get_user()`
  interpolates the caller-controlled `user_id` straight into
  `conn.execute()`; `list_users()` in the same file is the parameterized
  contrast.
- `feat: admin command handler` — missing sender validation: the exposed
  `on_command()` dispatcher never checks `sender` against an allowlist, so
  any connected client can invoke admin commands.

`clean-variant` (branched off the clean base) re-implements both features
safely (parameterized SQL + a sender allowlist) and is used as the clean
push for false-positive testing; `v0.1.0` tags the clean base.

The git history (4 commits on `main`, plus the `clean-variant` branch and
the `v0.1.0` tag) lives in the sibling bare repo `../py-app.git`. This
directory is just the worktree snapshot at the tip of `main` — it
deliberately carries no `.git`.

To use it, clone the bare repo:

    git clone tests/fixtures/repos/py-app.git my-py-app
    cd my-py-app && git log --oneline --all

The buggy push range under review is `v0.1.0..main` (both seeded bugs);
the clean push range is `v0.1.0..clean-variant`.
