"""Direct-process integration for the bounded legacy deep lane."""
import json
from pathlib import Path

from test_deep_gate import _env, _push, _repo, _commit, _review_calls, _state
import fake_backend
from test_deep_gate import review_gate


def test_large_kimchi_deep_review_uses_bounded_direct_native_tasks(tmp_path):
    work = _repo(tmp_path, {"a.txt": "one\n"})
    tip = _commit(work, {
        f"auth/file-{index:02d}.py": f"value = {index}\n"
        for index in range(10)
    })
    env = _env(tmp_path)
    env.pop("OCR_REVIEWER_CMD")
    env.update({
        "OCR_BACKEND": "kimchi",
        "KIMCHI_BIN": fake_backend.install(tmp_path / "bin", "kimchi"),
        "OCR_SKIP_CANARY": "1",
        "FAKE_TRACE": str(tmp_path / "fake.trace"),
    })

    decision, reason = _push(work, env)
    assert decision == "pass", reason
    state = _state(work, tip)
    assert state["state"] == "done"

    calls = fake_backend.read_trace(tmp_path / "fake.trace")
    native_calls = [call for call in calls if "-p" in call["argv"]]
    assert len(native_calls) == 4
    task_manifests = [call["manifest"] for call in _review_calls(tmp_path)]
    file_manifests = [manifest for manifest in task_manifests if manifest["paths"]]
    global_manifests = [manifest for manifest in task_manifests if not manifest["paths"]]
    assert len(file_manifests) == 3
    assert max(len(manifest["paths"]) for manifest in file_manifests) == 4
    assert len(global_manifests) == 1
    assert set(global_manifests[0]["native_deep_task"]["duties"]) >= {
        "cross-file-call-chains", "global-invariants",
    }

    common = review_gate._git_common_dir(str(work))
    journal_path = review_gate._async_dir(common) / f"native-{state['run_id']}-deep-tasks.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert len(journal["native_tasks"]) == 4
    assert all(len(task["artifacts"]) == 1 for task in journal["native_tasks"])

    state_path = review_gate._state_path(common, tip)
    log = Path(str(state_path)[:-5] + ".supervisor.log").read_text(encoding="utf-8")
    assert log.count("native-session-started") == 4
    assert log.count("native-session-completed") == 4
    assert "stream-progress" in log
