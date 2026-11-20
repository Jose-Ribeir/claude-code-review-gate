# CI failure digest — merge-all-prs
Ledger: .delegate-kit/runs/merge-all-prs/ci/  (runs-6/9.json, failed-6/9.txt, full-6/9.txt)

## integration/push-gate (run 43923470682, Nov 16)
- Conclusion: failure. Failed jobs: test (3.12), test (3.9)
- test (3.12): 1 failed
  - test_cmdshim.py::test_unwrap_follows_an_npm_shim_to_the_native_exe
    - AssertionError: assert '.../tool.CMD' == '.../node_modules/pkg/bin/tool.exe'
    - Read: deterministic assertion (unwrap returned .CMD instead of exe)
- test (3.9): 4 failed
  - same cmdshim assertion
  - test_parallel_chunks.py::test_a_limit_stops_the_sibling_but_finished_chunks_keep_their_records
    - AssertionError: assert [0, 1, 2] == [0, 1, 2, 3] — race-like chunk ordering
  - test_sim_harness.py::test_drive_push_transport_failure_is_a_harness_error_not_a_block
  - test_sim_harness.py::test_run_scenario_propagates_transport_failures_as_harness_errors
    - both: TypeError: rmtree() got an unexpected keyword argument 'onexc' (conftest.py:97)
    - Read: deterministic — shutil.rmtree onexc is 3.12+; unsupported on Python 3.9

## spec-15/race-regression-checks (run 44358089609, Nov 18; prior 44157659606 success)
- Conclusion: failure. Failed jobs: test (3.9), test (3.12)
- Both jobs fail same test:
  - test_final_materialize.py::test_materializes_target_from_a_separate_repository_and_cleans_failures
    - subprocess.CalledProcessError: git commit -q -m 'fork change' exit 128
    - stderr: "Author identity unknown ... empty ident name ... not allowed"
    - Read: deterministic — git user.name/email unset in CI; NOT a race

## Cross-cutting
- No timeouts/"order"/"eventually" flake patterns anywhere; all deterministic.
- Fix targets: cmdshim unwrap (both branches), onexc rmtree for 3.9, git identity in test env (branch 9).