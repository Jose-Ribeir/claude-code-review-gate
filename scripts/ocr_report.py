#!/usr/bin/env python3
"""ocr_report — everything the gate recorded, in one analyzable summary.

The gate is fail-closed and records EVERY review it runs (nothing is pruned
unless ocr_janitor is invoked manually). Three record layers:

  1. The verdict store   $OCR_STATE_DIR/verdicts/*.json — one record per
                         (remote, tip): verdict, blocked, covered commits,
                         timestamp. The durable, machine-readable layer.
  2. Per-repo findings   <repo>/.git/review-gate-findings.jsonl — append-only,
                         one entry per completed review with the FULL findings
                         (severity, confidence, path, line, content) plus the
                         raw-output archive name for deep dives.
  3. Raw streams         <repo>/.git/review-gate-history/<ts>-<tip>.json — the
                         complete --mode json event stream of each review.

This tool aggregates layer 1 (all repos) and, with --repo, joins layer 2 for
finding-level analysis. Output: a markdown table + aggregate counts. --json
emits machine-readable records instead (one per review, joined where
possible) for downstream analysis scripts.

Usage:
  python3 scripts/ocr_report.py                    # all verdicts, summary
  python3 scripts/ocr_report.py --repo /path/to/repo   # + that repo's findings
  python3 scripts/ocr_report.py --json | jq .      # machine-readable
"""
import argparse
import glob
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import rg_store  # noqa: E402


def _store_root():
    return rg_store.store_root()


def _verdict_records():
    """Every verdict in the store, newest first. Unparseable records are
    counted (never silently skipped) and reported by the caller."""
    out = []
    skipped = []
    for f in glob.glob(str(_store_root() / "verdicts" / "*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            skipped.append(f)
            continue
        if isinstance(d, dict) and d.get("tip"):
            out.append(d)
    out.sort(key=lambda d: d.get("ts") or 0, reverse=True)
    return out, skipped


def _repo_reviews(repo):
    """(findings entries, history archives) for one repo's git dir."""
    import subprocess
    try:
        git_dir = Path(subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True).stdout.strip())
    except Exception:
        git_dir = Path(repo) / ".git"
        if not git_dir.is_dir():
            return [], []
    findings = []
    path = git_dir / "review-gate-findings.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                findings.append(json.loads(line))
            except Exception:
                continue
    history = sorted(glob.glob(str(git_dir / "review-gate-history" / "*.json")))
    return findings, history


def _shorten(sha, n=7):
    return str(sha or "")[:n]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Report every recorded gate review")
    ap.add_argument("--repo", help="join this repo's finding-level records")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable records instead of markdown")
    args = ap.parse_args(argv)

    verdicts, skipped = _verdict_records()
    findings_by_tip, history_count, findings_total = {}, 0, 0
    if args.repo:
        reviews, history = _repo_reviews(args.repo)
        history_count = len(history)
        for r in reviews:
            t = str(r.get("tip") or r.get("head") or "")
            findings_by_tip.setdefault(t, []).append(r)
        findings_total = len(reviews)

    if args.json:
        if skipped:
            print(f"WARNING: {len(skipped)} unparseable verdict record(s) skipped — investigate the store",
                  file=sys.stderr)
        for d in verdicts:
            t = d["tip"]
            rec = {
                "ts": d.get("ts"),
                "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(d.get("ts") or 0)),
                "remote": d.get("remote"),
                "tip": d["tip"],
                "base": d.get("base"),
                "verdict": d.get("verdict"),
                "blocked": bool(d.get("blocked")),
                "commits": len(d.get("commits") or []),
                "capped": bool(d.get("capped")),
                "note": d.get("note") or "",
            }
            if args.repo and t in findings_by_tip:
                rec["findings"] = [
                    {k: f.get(k) for k in
                     ("severity", "confidence", "path", "start_line", "end_line",
                      "content", "category")}
                    for f in (findings_by_tip[t][-1].get("findings") or [])
                ]
            print(json.dumps(rec, ensure_ascii=False))
        return 0

    # markdown summary
    print(f"# Gate review record — {len(verdicts)} reviews recorded\n")
    if skipped:
        print(f"WARNING: {len(skipped)} unparseable verdict record(s) skipped — investigate the store")
    counts = {}
    for d in verdicts:
        counts[d.get("verdict") or "?"] = counts.get(d.get("verdict") or "?", 0) + 1
    print("verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    blocked = sum(1 for d in verdicts if d.get("blocked"))
    print(f"blocked pushes: {blocked}\n")
    if args.repo:
        print(f"repo {args.repo}: {findings_total} finding-level reviews, "
              f"{history_count} raw-stream archives\n")
    print("| when (UTC) | repo | tip | verdict | blocked | commits | note |")
    print("|---|---|---|---|---|---|---|")
    for d in verdicts:
        repo = str(d.get("remote") or "?")
        repo = repo.rsplit("/", 1)[-1] if "/" in repo else repo
        iso = time.strftime("%m-%d %H:%M", time.gmtime(d.get("ts") or 0))
        print(f"| {iso} | {repo} | {_shorten(d['tip'])} | {d.get('verdict')} | "
              f"{'yes' if d.get('blocked') else 'no'} | "
              f"{len(d.get('commits') or [])} | {str(d.get('note') or '')[:40]} |")
    print()
    if args.repo:
        # finding-level aggregate for the named repo
        sev = {}
        for r in findings_by_tip.values():
            for f in (r[-1].get("findings") or []):
                s = str(f.get("severity") or "?").lower()
                sev[s] = sev.get(s, 0) + 1
        print(f"finding severities ({args.repo}): "
              + ", ".join(f"{k}={v}" for k, v in sorted(sev.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
