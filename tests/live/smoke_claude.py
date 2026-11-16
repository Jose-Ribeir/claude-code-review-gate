"""Smoke test: drive the real `claude` backend through the gate's own worker path."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

GATE = sys.argv[1]
os.environ["OCR_BACKEND"] = "claude"
for k in list(os.environ):
    if k in ("OCR_REVIEWER_CMD", "OCR_STUB_WORKER_CMD", "OCR_CLAUDE_ARGS"):
        del os.environ[k]
os.environ.pop("OCR_IN_REVIEW", None)

repo = tempfile.mkdtemp(prefix="crg-smoke-")


def git(*a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


git("init", "-q", "-b", "main")
git("config", "user.email", "t@example.com")
git("config", "user.name", "t")
git("config", "commit.gpgsign", "false")
open(os.path.join(repo, "calc.py"), "w").write("def avg(xs):\n    return sum(xs) / len(xs)\n")
git("add", "-A")
git("commit", "-qm", "base")
open(os.path.join(repo, "calc.py"), "w").write(
    "def avg(xs):\n    return sum(xs) / len(xs)\n\n\n"
    "def first(xs):\n    return xs[1]\n")
git("add", "-A")
git("commit", "-qm", "add first()")
tip = git("rev-parse", "HEAD")
print("scratch repo:", repo)

spec = importlib.util.spec_from_file_location("rg", GATE)
rg = importlib.util.module_from_spec(spec)
sys.path.insert(0, os.path.dirname(GATE))
spec.loader.exec_module(rg)
os.chdir(repo)
try:
    out = rg._run_review_once(repo, "hook", os.path.join(repo, ".git"), tip, "HEAD~1..HEAD", timeout=420)
    print("RESULT:", json.dumps(out[0], indent=2)[:3000])
    print("selection:", getattr(rg._backend_local, "selection", None))
except Exception as exc:  # noqa: BLE001
    print("RAISED:", type(exc).__name__, str(exc)[:3000])
    print("selection:", getattr(rg._backend_local, "selection", None))
