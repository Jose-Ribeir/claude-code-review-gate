#!/bin/sh
# C4 acceptance probe: headless /skill:review on the clean-ts fixture.
# Run from anywhere; creates its scratch under /tmp and leaves it for inspection.
set -e
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SCRATCH=${SCRATCH:-/tmp/rg-c4-test}
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"
git clone -q "$REPO_ROOT/tests/fixtures/repos/clean-ts.git" "$SCRATCH/proj"
mkdir -p "$SCRATCH/proj/.kimchi/skills"
ln -s "$REPO_ROOT/skills/review" "$SCRATCH/proj/.kimchi/skills/review"
cd "$SCRATCH/proj"
RANGE="$(git rev-parse HEAD~2)..$(git rev-parse HEAD)"
echo "range: $RANGE"
# --approve (-a) is REQUIRED: without it, project-local .kimchi/skills in an
# untrusted (scratch) directory are ignored and the skill silently does not
# load. See RESULTS.md, finding 1.
OCR_IN_REVIEW=1 kimchi -a -p "/skill:review --range $RANGE --json" \
  --mode json --no-session > "$SCRATCH/stream.jsonl" 2>"$SCRATCH/stderr.log"
echo "stream: $SCRATCH/stream.jsonl ($(wc -l < "$SCRATCH/stream.jsonl") events)"
python3 - "$SCRATCH/stream.jsonl" <<'PY'
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location('rg', 'scripts/review-gate.py')
rg = importlib.util.module_from_spec(spec); spec.loader.exec_module(rg)
v = rg._extract_from_stream_json(open(sys.argv[1]).read())
print("VERDICT:", "VALID (parseable by _extract_from_stream_json/_extract_json)" if v else "NONE")
if v: print(json.dumps({k: v[k] for k in v if k != 'findings'}))
PY
