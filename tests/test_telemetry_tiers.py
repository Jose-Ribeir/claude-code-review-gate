"""Per-tier token/cost telemetry records (review-gate v2, t12 Phase 1).

Spec: "Cost is measured (existing telemetry), not capped" (rollout decision:
no hard cap -- measure first) and user story 25 ("As an operator, I want
per-tier token and cost telemetry"). The gate's tiers -- spotter, tier-1
review, tier-2 deep pass, and the deterministic collectors -- each emit
records shaped on the module's existing run-record pattern (one JSON line per
event in the local jsonl log), carrying tier, model, exact tokens when known,
a token estimate when not, and duration.

Covered here:
* ``tier_record`` builds/normalises the per-tier field set and rejects tiers
  outside the four v2 names (tier is a contract, I/O is fail-soft);
* ``estimate_tokens`` gives the chars/4 estimate used when a tier (or the
  CLI) cannot report exact token counts;
* ``record_tier_call`` appends a ``kind="tier_call"`` jsonl line, honours
  OCR_TELEMETRY=0, and never raises;
* ``report()`` keeps tier records out of the run/verdict counters and adds a
  per-tier cost section, so a tier-heavy run history still sums runs right.
"""
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_telemetry as ot  # noqa: E402


@pytest.fixture
def common_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_TELEMETRY", "1")
    return str(tmp_path)


# ---------------------------------------------------------------------------
# tier_record: field shape and normalisation
# ---------------------------------------------------------------------------

class TestTierRecord:
    def test_minimal_record_has_all_fields(self):
        rec = ot.tier_record(ot.TIER_SPOTTER, model="deepseek-v4-flash")
        assert rec["tier"] == "spotter"
        assert rec["model"] == "deepseek-v4-flash"
        for field in ("tokens_in", "tokens_out", "token_estimate",
                      "cost_usd", "seconds"):
            assert field in rec
            assert rec[field] is None

    def test_exact_tokens_when_known(self):
        """Spotter/reviewer CLIs reporting usage carry exact counts."""
        rec = ot.tier_record(ot.TIER_REVIEW, model="kimi-k2.5",
                             tokens_in=8123, tokens_out=412,
                             cost_usd=0.0421, seconds=61.27)
        assert rec["tokens_in"] == 8123
        assert rec["tokens_out"] == 412
        assert rec["token_estimate"] is None
        assert rec["cost_usd"] == 0.0421
        assert rec["seconds"] == 61.27

    def test_estimate_when_exact_counts_unknown(self):
        """Exact counts unavailable (e.g. collectors measuring evidence
        bytes) -> token_estimate instead of tokens_in/out."""
        rec = ot.tier_record(ot.TIER_COLLECTORS, token_estimate=512, seconds=0.4)
        assert rec["tokens_in"] is None and rec["tokens_out"] is None
        assert rec["token_estimate"] == 512

    def test_rounding(self):
        rec = ot.tier_record(ot.TIER_DEEP, cost_usd=0.123456, seconds=1.98765,
                             tokens_in=100.0, tokens_out=50.9)
        assert rec["cost_usd"] == 0.1235
        assert rec["seconds"] == 1.99
        assert rec["tokens_in"] == 100
        assert rec["tokens_out"] == 50

    def test_non_numeric_counts_become_none(self):
        rec = ot.tier_record(ot.TIER_SPOTTER, tokens_in="many", seconds="slow")
        assert rec["tokens_in"] is None
        assert rec["seconds"] is None

    def test_all_four_tiers_accepted(self):
        for tier in ("spotter", "tier1", "tier2", "collectors"):
            assert ot.tier_record(tier)["tier"] == tier

    def test_unknown_tier_rejected(self):
        """The tier name is a code-level contract (not untrusted input), so a
        typo fails loudly here instead of polluting the operator's report."""
        with pytest.raises(ValueError):
            ot.tier_record("sommelier")
        with pytest.raises(ValueError):
            ot.tier_record("")

    def test_record_is_json_serializable(self):
        rec = ot.tier_record(ot.TIER_REVIEW, model="m", tokens_in=1,
                             cost_usd=0.1, seconds=0.1)
        json.dumps(rec)


# ---------------------------------------------------------------------------
# estimate_tokens: the chars/4 fallback when exact counts are unavailable
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_string_input(self):
        assert ot.estimate_tokens("a" * 400) == 100
        assert ot.estimate_tokens("a" * 401) == 100  # chars // 4

    def test_char_count_input(self):
        assert ot.estimate_tokens(800) == 200

    def test_empty_and_none(self):
        assert ot.estimate_tokens("") == 0
        assert ot.estimate_tokens(0) == 0
        assert ot.estimate_tokens(None) is None


# ---------------------------------------------------------------------------
# record_tier_call: jsonl emitter, fail-soft, honours OCR_TELEMETRY
# ---------------------------------------------------------------------------

class TestRecordTierCall:
    def test_appends_one_record_to_the_run_log(self, common_dir):
        rec = ot.record_tier_call(common_dir, ot.TIER_SPOTTER,
                                  model="deepseek-v4-flash",
                                  tokens_in=1000, tokens_out=40,
                                  cost_usd=0.002, seconds=1.2)
        assert rec is not None
        assert rec["kind"] == "tier_call"
        assert rec["tier"] == "spotter"
        assert isinstance(rec["ts"], (int, float))
        records = ot.load(common_dir, days=1)
        assert len(records) == 1
        written = records[0]
        assert written["kind"] == "tier_call"
        assert written["model"] == "deepseek-v4-flash"
        assert written["tokens_in"] == 1000
        assert written["tokens_out"] == 40
        assert written["cost_usd"] == 0.002
        assert written["seconds"] == 1.2

    def test_distinct_records_per_tier(self, common_dir):
        """The four tiers produce distinct, separately discoverable records."""
        ot.record_tier_call(common_dir, "spotter", model="fast", tokens_in=10)
        ot.record_tier_call(common_dir, "tier1", model="strong", tokens_in=100)
        ot.record_tier_call(common_dir, "tier2", model="strong", tokens_in=1000)
        ot.record_tier_call(common_dir, "collectors", token_estimate=50, seconds=0.3)
        tiers = [r["tier"] for r in ot.load(common_dir, days=1)
                 if r.get("kind") == "tier_call"]
        assert tiers == ["spotter", "tier1", "tier2", "collectors"]

    def test_tier_records_coexist_with_run_records(self, common_dir):
        import time
        ot.append(common_dir, {"ts": time.time(), "verdict": "pass", "seconds": 9})
        ot.record_tier_call(common_dir, "spotter")
        records = ot.load(common_dir, days=30)
        kinds = [r.get("kind") for r in records]
        assert kinds == [None, "tier_call"]
        assert records[0]["verdict"] == "pass"

    def test_disabled_writes_nothing(self, common_dir, monkeypatch):
        monkeypatch.setenv("OCR_TELEMETRY", "0")
        assert ot.record_tier_call(common_dir, "spotter", tokens_in=1) is None
        assert ot.load(common_dir, days=1) == []

    def test_unwritable_dir_returns_none_not_raises(self, monkeypatch):
        monkeypatch.setenv("OCR_TELEMETRY", "1")
        rec = ot.record_tier_call("/nonexistent-\x00dir/common", "spotter")
        assert rec is None

    def test_accepts_tier_record_kwargs(self, common_dir):
        """emit(tier_record(...)) ergonomics: the emitter takes the builder's
        fields directly."""
        fields = ot.tier_record(ot.TIER_DEEP, model="kimi-k2.5", tokens_in=5)
        rec = ot.record_tier_call(common_dir, fields.pop("tier"), **fields)
        assert rec["tier"] == "tier2" and rec["tokens_in"] == 5


# ---------------------------------------------------------------------------
# report(): tier records must not pollute run stats; per-tier section
# ---------------------------------------------------------------------------

class TestReport:
    def _run_rec(self, verdict="pass", seconds=10):
        return {"ts": 1, "verdict": verdict, "seconds": seconds}

    def test_tier_records_are_not_counted_as_runs(self):
        records = [self._run_rec(),
                   dict(ot.tier_record("spotter", tokens_in=1), kind="tier_call", ts=2),
                   dict(ot.tier_record("tier1", tokens_in=1), kind="tier_call", ts=3)]
        out = ot.report(records)
        assert out.splitlines()[0] == "Runs: 1  pass=1"

    def test_per_tier_cost_section(self):
        records = [
            self._run_rec(),
            dict(ot.tier_record("spotter", model="fast", tokens_in=1000,
                                tokens_out=40, cost_usd=0.002, seconds=1.0),
                 kind="tier_call", ts=2),
            dict(ot.tier_record("tier1", model="strong", tokens_in=8000,
                                tokens_out=400, cost_usd=0.04, seconds=60.0),
                 kind="tier_call", ts=3),
            dict(ot.tier_record("tier1", model="strong", tokens_in=2000,
                                tokens_out=100, cost_usd=0.01, seconds=20.0),
                 kind="tier_call", ts=4),
            dict(ot.tier_record("collectors", token_estimate=500, seconds=0.3),
                 kind="tier_call", ts=5),
        ]
        out = ot.report(records)
        assert "Per-tier" in out
        section = out.split("Per-tier", 1)[1]
        assert "spotter" in section and "tier1" in section
        assert "tier2" in section and "collectors" in section

    def test_per_tier_sums_and_averages(self):
        records = [
            dict(ot.tier_record("tier1", tokens_in=8000, tokens_out=400,
                                cost_usd=0.04, seconds=60.0),
                 kind="tier_call", ts=2),
            dict(ot.tier_record("tier1", tokens_in=2000, tokens_out=100,
                                cost_usd=0.01, seconds=20.0),
                 kind="tier_call", ts=3),
            dict(ot.tier_record("collectors", token_estimate=500, seconds=0.3),
                 kind="tier_call", ts=4),
        ]
        out = ot.report(records)
        tier1_line = next(l for l in out.splitlines() if l.strip().startswith("tier1"))
        # 2 calls; total 10000 in / 500 out; avg 40 s per call; total cost $0.05
        assert "10000" in tier1_line
        assert "500" in tier1_line
        assert "40" in tier1_line
        assert "0.050" in tier1_line or "0.05" in tier1_line
        coll_line = next(l for l in out.splitlines()
                         if l.strip().startswith("collectors"))
        # collectors: exact tokens unknown -> the estimate column carries it
        assert "500" in coll_line

    def test_no_tier_records_no_section(self):
        out = ot.report([self._run_rec()])
        assert "Per-tier" not in out
        assert out.splitlines()[0] == "Runs: 1  pass=1"
