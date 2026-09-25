#!/usr/bin/env python3
#
# Local run log for review-gate: one JSON line per review run, appended under
# <git-common-dir>/review-gate-telemetry/<UTC date>.jsonl.
#
# Why this exists: 0.9.0 adds deterministic impact analysis next to the
# reviewer's own cross-file step, and whether the Python side can replace the
# model's for a language is a question only real pushes can answer. Every run
# therefore records what each side found, how the planner classified files,
# what the resolver did, and how long each model call took -- and
# `review-gate.py --telemetry-report` summarises it.
#
# Local only: the log lives inside .git (never committed, never sent anywhere)
# and holds file paths, line numbers and finding text from the reviewed code.
# OCR_TELEMETRY=0 turns it off. Writing is best-effort and never affects a
# review's outcome.
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

TELEMETRY_DIR = "review-gate-telemetry"
_ROTATE_BYTES = 50 * 1024 * 1024
_SCHEMA = 1


def enabled():
    return os.environ.get("OCR_TELEMETRY", "1").strip().lower() not in ("0", "false", "no")


def telemetry_dir(common_dir):
    return Path(common_dir) / TELEMETRY_DIR


def append(common_dir, record):
    """Append one run record. Returns the file written, or None."""
    if not enabled() or not common_dir:
        return None
    try:
        d = telemetry_dir(common_dir)
        d.mkdir(parents=True, exist_ok=True)
        ts = record.get("ts") or time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        path, n = d / f"{day}.jsonl", 1
        while path.exists() and path.stat().st_size >= _ROTATE_BYTES:
            path, n = d / f"{day}.{n}.jsonl", n + 1
        line = json.dumps(dict(record, schema=_SCHEMA), ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path
    except Exception:
        return None


def load(common_dir, days=7):
    """Records from the last `days` days, oldest first. Unreadable lines are skipped."""
    d = telemetry_dir(common_dir)
    if not d.is_dir():
        return []
    cutoff = time.time() - days * 86400
    out = []
    for f in sorted(d.glob("*.jsonl")):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and (rec.get("ts") or 0) >= cutoff:
                        out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts") or 0)
    return out


def _lang(path):
    ext = os.path.splitext(path or "")[1].lower()
    return ext.lstrip(".") or "?"


def _pct(a, b):
    return f"{(100.0 * a / b):.0f}%" if b else "-"


def _site_keys(sites):
    return {(s.get("path"), s.get("line")) for s in sites or [] if isinstance(s, dict)}


def report(records):
    """Plain-text summary of `records` (see load)."""
    if not records:
        return "No review-gate telemetry recorded in this period."
    lines = []
    add = lines.append

    verdicts = Counter(r.get("verdict") or r.get("state") or "?" for r in records)
    add(f"Runs: {len(records)}  " + "  ".join(f"{k}={v}" for k, v in verdicts.most_common()))

    # Model calls.
    calls = defaultdict(list)
    for r in records:
        for c in r.get("calls") or []:
            calls[c.get("kind") or "?"].append(c)
    if calls:
        add("\nModel calls (count, avg s, failures):")
        for kind, cs in sorted(calls.items()):
            secs = [c.get("seconds") or 0 for c in cs]
            fails = sum(1 for c in cs if c.get("outcome") != "ok")
            add(f"  {kind:<10} {len(cs):>5}  {sum(secs) / len(secs):>7.1f}  {fails}")

    # Planner.
    modes, reasons = Counter(), Counter()
    for r in records:
        for p in r.get("plan") or []:
            modes[p.get("mode")] += 1
            if p.get("miss_reason") not in (None, "none"):
                reasons[p.get("miss_reason")] += 1
    if modes:
        add("\nFiles by mode: " + "  ".join(f"{k}={v}" for k, v in modes.most_common()))
        if reasons:
            add("  full because: " + "  ".join(f"{k}={v}" for k, v in reasons.most_common()))

    # Impact: Python bundle vs the model's own cross-file step.
    per_lang = defaultdict(Counter)
    truncated_runs = 0
    for r in records:
        imp = r.get("impact") or {}
        if imp.get("truncated"):
            truncated_runs += 1
        py_sites = imp.get("sites") or []
        model = r.get("model_cross_file") or {}
        model_sites = []
        for sym in model.get("symbols") or []:
            if not isinstance(sym, dict):
                continue
            for ref in sym.get("external_refs") or []:
                if isinstance(ref, dict):
                    model_sites.append(dict(ref, lang=_lang(sym.get("defined_in"))))
        for s in py_sites:
            per_lang[_lang(s.get("defined_in") or s.get("path"))]["python_sites"] += 1
        py_keys = _site_keys(py_sites)
        for s in model_sites:
            c = per_lang[s["lang"]]
            c["model_sites"] += 1
            if (s.get("path"), s.get("line")) in py_keys:
                c["both"] += 1
            else:
                c["model_only"] += 1
        for sym in imp.get("symbols") or []:
            per_lang[_lang(sym.get("defined_in"))]["python_symbols"] += 1
        for name in imp.get("unsupported") or []:
            per_lang[_lang(name)]["unsupported_files"] += 1
    if per_lang:
        add("\nImpact analysis by language (call sites):")
        add("  lang    py_syms py_sites model_sites  both model_only  py_covers_model unsupported")
        for lang, c in sorted(per_lang.items()):
            add(f"  {lang:<7} {c['python_symbols']:>7} {c['python_sites']:>8} {c['model_sites']:>11}"
                f" {c['both']:>5} {c['model_only']:>10}  {_pct(c['both'], c['model_sites']):>15}"
                f" {c['unsupported_files']:>11}")
        add(f"  runs with a capped bundle: {truncated_runs}")
        add("  (py_covers_model = model-found sites Python also found; model_only are Python misses)")

    verdict_counts = Counter()
    for r in records:
        for v in (r.get("site_verdicts") or {}).values():
            verdict_counts[v if isinstance(v, str) else "?"] += 1
    if verdict_counts:
        add("\nReviewer verdicts on call sites: "
            + "  ".join(f"{k}={v}" for k, v in verdict_counts.most_common()))

    # Siblings.
    sib = Counter()
    for r in records:
        s = r.get("siblings") or {}
        sib["to_reviewer"] += len(s.get("to_reviewer") or [])
        sib["noted_now"] += len(s.get("noted") or [])
        for f in r.get("findings") or []:
            if f.get("provenance") == "sibling":
                sib["confirmed"] += 1
    if any(sib.values()):
        add(f"\nSame-bug search: sent to reviewer={sib['to_reviewer']}  "
            f"noted for next push={sib['noted_now']}  confirmed findings={sib['confirmed']}")

    # Resolver.
    res = Counter()
    for r in records:
        rv = r.get("resolver") or {}
        if not rv:
            continue
        res["runs"] += 1
        res["priors"] += rv.get("sent") or 0
        res["rechecks"] += rv.get("recheck") or 0
        res["warnings"] += len(rv.get("warnings") or [])
        for v in (rv.get("judged") or {}).values():
            res[v] += 1
    if res:
        add(f"\nResolver: runs={res['runs']} priors={res['priors']} resolved={res['resolved']} "
            f"still_present={res['still_present']} unverified={res['unverified']} "
            f"rechecks={res['rechecks']} warnings={res['warnings']}")

    prov = Counter()
    for r in records:
        for f in r.get("findings") or []:
            prov[(f.get("provenance") or "new", (f.get("severity") or "?").lower())] += 1
    if prov:
        add("\nFindings by provenance/severity: "
            + "  ".join(f"{p}/{s}={n}" for (p, s), n in sorted(prov.items())))

    fps = []
    for r in records:
        if r.get("fp") and (not fps or fps[-1][1] != r["fp"]):
            fps.append((r.get("ts"), r["fp"], r.get("fp_parts") or {}))
    if len(fps) > 1:
        add("\nReview criteria changed:")
        for (_, _, before), (ts, fp, after) in zip(fps, fps[1:]):
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts or 0))
            add(f"  {when} -> {fp[:12]}: {', '.join(changed) or '?'}")

    return "\n".join(lines)
