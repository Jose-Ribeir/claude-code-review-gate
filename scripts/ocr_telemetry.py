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
#
# 0.9.6 adds time metrics (where a run's minutes went): per model call (turns,
# API time, cost, tool use, orchestrator vs reviewer-agent wall time), per run
# phase and per chunk. Those keys, and the always-on rotating log lines written
# next to them (metric_line / rotating_append), carry COUNTS, BYTES AND TIMINGS
# ONLY -- never a path or any code. metric_line enforces that by construction.
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
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


# --- time metrics (0.9.6) ----------------------------------------------------

# The reviewer's orchestrator hands the diff to a sub-agent through the Agent
# tool ("Task" in older CLIs); its wall time is the reviewer-side share of a call.
_AGENT_TOOLS = ("Agent", "Task")

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:+-]{1,40}$")


def _event_ts(event):
    """Epoch seconds of a stream-json event's `timestamp`, or None."""
    raw = event.get("timestamp")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw) / (1000.0 if raw > 1e11 else 1.0)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _union_seconds(intervals):
    """Total length of the union of (start, end) intervals (parallel agents overlap)."""
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def stream_stats(text, started_at=None):
    """Time metrics out of a reviewer's stream-json stdout (never raises).

    Counts, bytes and timings only. Keys, when known: turns, api_s, cost_usd,
    duration_s (from the result event); events; first_event_s (the first event's
    timestamp minus `started_at`, the epoch the process started; else the result
    event's own first-frame time); tools ({name: [calls, input_bytes]});
    agent_calls, agent_input_bytes, agent_wall_s (tool_use -> tool_result of the
    Agent tool, overlapping calls counted once); orchestrator_s (the call's own
    duration minus agent_wall_s). A reply that is not stream-json gives {}.
    """
    out = {}
    try:
        tools, agent_open, agent_spans = {}, {}, []
        first_ts, n_events = None, 0
        result = None
        for line in (text or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or not ev.get("type"):
                continue
            n_events += 1
            ts = _event_ts(ev)
            if ts is not None and first_ts is None:
                first_ts = ts
            kind = ev.get("type")
            if kind == "result":
                result = ev
                continue
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if kind == "assistant" and block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    size = len(json.dumps(block.get("input"), ensure_ascii=False,
                                          default=str).encode("utf-8"))
                    entry = tools.setdefault(name, [0, 0])
                    entry[0] += 1
                    entry[1] += size
                    if name in _AGENT_TOOLS and ts is not None and block.get("id"):
                        agent_open[block["id"]] = ts
                elif kind == "user" and block.get("type") == "tool_result":
                    began = agent_open.pop(block.get("tool_use_id"), None)
                    if began is not None and ts is not None and ts >= began:
                        agent_spans.append((began, ts))
        if not n_events:
            return {}
        out["events"] = n_events
        if first_ts is not None and started_at:
            out["first_event_s"] = round(max(0.0, first_ts - started_at), 2)
        if tools:
            out["tools"] = tools
            agent = [v for k, v in tools.items() if k in _AGENT_TOOLS]
            if agent:
                out["agent_calls"] = sum(v[0] for v in agent)
                out["agent_input_bytes"] = sum(v[1] for v in agent)
        agent_wall = _union_seconds(agent_spans)
        if agent_spans:
            out["agent_wall_s"] = round(agent_wall, 2)
        if isinstance(result, dict):
            if isinstance(result.get("num_turns"), (int, float)):
                out["turns"] = int(result["num_turns"])
            if isinstance(result.get("duration_api_ms"), (int, float)):
                out["api_s"] = round(result["duration_api_ms"] / 1000.0, 2)
            if isinstance(result.get("total_cost_usd"), (int, float)):
                out["cost_usd"] = round(float(result["total_cost_usd"]), 4)
            ttft = result.get("first_content_frame_ms", result.get("ttft_ms"))
            if isinstance(ttft, (int, float)) and "first_event_s" not in out:
                out["first_event_s"] = round(ttft / 1000.0, 2)
            if isinstance(result.get("duration_ms"), (int, float)):
                out["duration_s"] = round(result["duration_ms"] / 1000.0, 2)
                if agent_spans:
                    out["orchestrator_s"] = round(max(0.0, out["duration_s"] - agent_wall), 2)
    except Exception:
        return {}
    return out


def metric_line(event, **fields):
    """One log line `ts=<UTC> event=<name> k=v ...` that cannot carry paths or code.

    A value is written only when it is a number/bool, or a short token of
    [A-Za-z0-9_.:+-] (tool names, outcomes, run ids); anything else -- a path, a
    sentence, a code fragment -- becomes "?". A dict is flattened as
    `name=key:count,key:count` under the same rule.
    """
    def safe(v):
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v)
        return s if _SAFE_VALUE.match(s) else "?"

    parts = [f"ts={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}", f"event={safe(event)}"]
    for key, val in fields.items():
        if val is None:
            continue
        if isinstance(val, dict):
            val = ",".join(f"{safe(k)}:{safe(v[0] if isinstance(v, (list, tuple)) else v)}"
                           for k, v in val.items())
            if not val:
                continue
            parts.append(f"{safe(key)}={val}")
        else:
            parts.append(f"{safe(key)}={safe(val)}")
    return " ".join(parts)


def rotating_append(path, line, max_bytes=1024 * 1024, keep=3):
    """Append `line` to `path`; past `max_bytes` shift path -> path.1 -> ... path.<keep>.

    Best-effort: a rotation another process (or an open handle on Windows)
    refuses is skipped and the line is still appended. Returns True if written.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size >= max_bytes:
                for i in range(keep, 0, -1):
                    src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
                    if src.exists():
                        os.replace(str(src), str(path.with_name(f"{path.name}.{i}")))
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:
        return False


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


def _avg(vals):
    return sum(vals) / len(vals) if vals else None


def _p90(vals):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(0.9 * len(vals)))]


def _fmt(v, spec=".1f"):
    return "-" if v is None else format(v, spec)


def _time_metrics(calls, records, add):
    """The 0.9.6 additions to report(): where the time went. Old records, which
    lack these keys, simply contribute nothing."""
    stat_kinds = {k: cs for k, cs in calls.items() if any("turns" in c or "api_s" in c for c in cs)}
    if stat_kinds:
        add("\nModel call time (n with stats, avg s, p90 s, turns, cost $, agent-input KB,"
            " first event s):")
        for kind, cs in sorted(stat_kinds.items()):
            secs = [c.get("seconds") or 0 for c in cs]
            turns = [c["turns"] for c in cs if "turns" in c]
            cost = [c["cost_usd"] for c in cs if "cost_usd" in c]
            kb = [c["agent_input_bytes"] / 1024.0 for c in cs if "agent_input_bytes" in c]
            ttfe = [c["first_event_s"] for c in cs if "first_event_s" in c]
            add(f"  {kind:<10} {len(turns):>5}  {_fmt(_avg(secs)):>7}  {_fmt(_p90(secs)):>7}"
                f"  {_fmt(_avg(turns)):>6}  {_fmt(_avg(cost), '.3f'):>7}  {_fmt(_avg(kb)):>8}"
                f"  {_fmt(_avg(ttfe)):>6}")
    orch = sum(c["orchestrator_s"] for cs in calls.values() for c in cs if "orchestrator_s" in c)
    agent = sum(c["agent_wall_s"] for cs in calls.values() for c in cs if "orchestrator_s" in c)
    if orch + agent > 0:
        add(f"  orchestrator vs reviewer agent: {orch:.0f}s ({_pct(orch, orch + agent)}) vs "
            f"{agent:.0f}s ({_pct(agent, orch + agent)})"
            "  (orchestrator = the call's time outside its Agent tool calls)")

    phases = defaultdict(list)
    for r in records:
        for ph in r.get("phases") or []:
            if isinstance(ph, dict) and ph.get("name"):
                phases[ph["name"]].append(ph.get("seconds") or 0)
    if phases:
        totals = [r.get("seconds") or 0 for r in records if r.get("phases")]
        grand = sum(totals)
        add("\nRun phases (runs, avg s, p90 s, share of run time):")
        for name, vals in sorted(phases.items(), key=lambda kv: -sum(kv[1])):
            add(f"  {name:<10} {len(vals):>5}  {_fmt(_avg(vals)):>7}  {_fmt(_p90(vals)):>7}"
                f"  {_pct(sum(vals), grand):>5}")

    chunks = [c for r in records for c in (r.get("chunks") or []) if isinstance(c, dict)]
    if chunks:
        by_outcome = Counter(c.get("outcome") or "?" for c in chunks)
        secs = [c.get("seconds") or 0 for c in chunks]
        add(f"\nChunks: {len(chunks)}  avg {_fmt(_avg(secs))}s  p90 {_fmt(_p90(secs))}s"
            f"  avg files {_fmt(_avg([c.get('files') or 0 for c in chunks]))}"
            f"  avg lines {_fmt(_avg([c.get('lines') or 0 for c in chunks]), '.0f')}  "
            + "  ".join(f"{k}={v}" for k, v in by_outcome.most_common()))


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
    _time_metrics(calls, records, add)

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
