"""Deterministic tests for the scoring engine math.

Uses a literal config (not config/scoring.json) so test expectations stay
stable when the live config is tuned.
"""
import pytest

from engine import scoring as S

CFG = {
    "side": "call",
    "top_n": 15,
    "weights": {
        "delta_fit": 0.20,
        "extrinsic": 0.15,
        "spread": 0.15,
        "open_interest": 0.10,
        "volume": 0.10,
        "iv_rank": 0.10,
        "dte_fit": 0.10,
        "trend_alignment": 0.10,
    },
    "delta_band": {"min": 0.60, "max": 0.80, "falloff": 0.15},
    "dte_band": {"min": 25, "max": 60, "falloff_days": 15},
    "extrinsic": {"best_max_pct": 0.10, "worst_pct": 0.40},
    "spread": {"best_max_pct": 0.02, "worst_pct": 0.10},
    "open_interest": {"floor": 100, "full_credit": 2000},
    "volume": {"floor": 10, "full_credit": 1000},
    "iv_rank": {"prefer_low": True, "min_history_days": 10},
    "trend_alignment": {"trend_weight": 0.6, "regime_weight": 0.4},
    "filters": {
        "min_open_interest": 50,
        "min_volume": 0,
        "max_spread_pct": 0.15,
        "min_mid": 0.20,
        "require_greeks": True,
    },
}


def make_contract(**overrides):
    base = {
        "occ_symbol": "TEST260117C00090000",
        "underlying": "TEST",
        "strike": 90.0,
        "dte": 40,
        "bid": 10.4,
        "ask": 10.6,
        "mid": 10.5,
        "delta": 0.70,
        "open_interest": 2000,
        "volume": 1000,
        "iv": 0.25,
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------- delta band

def test_delta_inside_band_scores_one():
    assert S.score_delta_fit(0.70, CFG) == 1.0
    assert S.score_delta_fit(0.60, CFG) == 1.0
    assert S.score_delta_fit(0.80, CFG) == 1.0


def test_delta_below_band_linear_falloff():
    # 0.50 is 0.10 below the band; falloff 0.15 -> 1 - 0.10/0.15
    assert S.score_delta_fit(0.50, CFG) == pytest.approx(1.0 - 0.10 / 0.15)


def test_delta_far_outside_band_scores_zero():
    # exactly at the falloff edge (float-tolerant), and clearly beyond it
    assert S.score_delta_fit(0.95, CFG) == pytest.approx(0.0, abs=1e-9)
    assert S.score_delta_fit(0.99, CFG) == 0.0
    assert S.score_delta_fit(0.30, CFG) == 0.0


def test_delta_uses_absolute_value():
    assert S.score_delta_fit(-0.70, CFG) == 1.0


def test_delta_missing_scores_zero():
    assert S.score_delta_fit(None, CFG) == 0.0


# ----------------------------------------------------------- extrinsic

def test_extrinsic_pct_call():
    # spot 100, strike 90, mid 12 -> intrinsic 10, extrinsic 2/12
    assert S.extrinsic_pct(12.0, 90.0, 100.0, "call") == pytest.approx(2.0 / 12.0)


def test_extrinsic_pct_otm_is_all_extrinsic():
    assert S.extrinsic_pct(1.5, 110.0, 100.0, "call") == 1.0


def test_score_extrinsic_endpoints_and_midpoint():
    assert S.score_extrinsic(0.05, CFG) == 1.0           # below best_max
    assert S.score_extrinsic(0.40, CFG) == 0.0           # at worst
    assert S.score_extrinsic(0.25, CFG) == pytest.approx(0.5)


# -------------------------------------------------------------- spread

def test_spread_pct():
    c = make_contract(bid=4.9, ask=5.1, mid=5.0)
    assert S.spread_pct(c) == pytest.approx(0.04)


def test_score_spread_linear():
    assert S.score_spread(0.01, CFG) == 1.0
    assert S.score_spread(0.10, CFG) == 0.0
    assert S.score_spread(0.06, CFG) == pytest.approx(0.5)


# ------------------------------------------------- open interest / volume

def test_open_interest_scaling():
    assert S.score_open_interest(100, CFG) == 0.0
    assert S.score_open_interest(1050, CFG) == pytest.approx(0.5)
    assert S.score_open_interest(2000, CFG) == 1.0
    assert S.score_open_interest(50000, CFG) == 1.0


def test_volume_scaling():
    assert S.score_volume(10, CFG) == 0.0
    assert S.score_volume(505, CFG) == pytest.approx(0.5)
    assert S.score_volume(1000, CFG) == 1.0


# -------------------------------------------------------------- iv rank

def test_iv_rank_none_is_neutral():
    assert S.score_iv_rank(None, CFG) == 0.5


def test_iv_rank_prefer_low():
    assert S.score_iv_rank(0.2, CFG) == pytest.approx(0.8)
    assert S.score_iv_rank(1.0, CFG) == 0.0


def test_iv_rank_prefer_high():
    cfg = {**CFG, "iv_rank": {"prefer_low": False, "min_history_days": 10}}
    assert S.score_iv_rank(0.2, cfg) == pytest.approx(0.2)


# ------------------------------------------------------------------ dte

def test_dte_inside_band():
    assert S.score_dte_fit(40, CFG) == 1.0


def test_dte_falloff_below_and_above():
    assert S.score_dte_fit(20, CFG) == pytest.approx(1.0 - 5.0 / 15.0)
    assert S.score_dte_fit(70, CFG) == pytest.approx(1.0 - 10.0 / 15.0)
    assert S.score_dte_fit(80, CFG) == 0.0


# ------------------------------------------------------ trend alignment

def test_trend_alignment_blend():
    # 0.6 * 1.0 + 0.4 * 0.5 = 0.8
    assert S.score_trend_alignment(1.0, 0.5, CFG) == pytest.approx(0.8)


def test_trend_alignment_missing_inputs_neutral():
    assert S.score_trend_alignment(None, None, CFG) == pytest.approx(0.5)


# -------------------------------------------------------------- filters

def test_filters_reject_low_open_interest():
    ok, reason = S.passes_filters(make_contract(open_interest=10), CFG)
    assert not ok and reason == "open interest below minimum"


def test_filters_reject_wide_spread():
    ok, reason = S.passes_filters(make_contract(bid=1.0, ask=1.4, mid=1.2), CFG)
    assert not ok and reason == "spread too wide"


def test_filters_reject_tiny_mid():
    ok, reason = S.passes_filters(make_contract(bid=0.05, ask=0.15, mid=0.10), CFG)
    assert not ok and reason == "no quote or mid below min_mid"


def test_filters_reject_missing_greeks():
    ok, reason = S.passes_filters(make_contract(delta=None), CFG)
    assert not ok and reason == "missing greeks"


def test_filters_accept_good_contract():
    ok, reason = S.passes_filters(make_contract(), CFG)
    assert ok and reason is None


# ------------------------------------------------------- composite score

PERFECT_CTX = {"spot": 100.0, "trend01": 1.0, "regime01": 1.0, "iv_rank": 0.0}


def test_perfect_contract_scores_100():
    # Every component pinned at 1.0:
    # delta 0.70 in band; mid 10.5 on strike 90 spot 100 -> extrinsic 0.5/10.5
    # = 4.8% <= 10%; spread 0.2/10.5 = 1.9% <= 2%; OI/volume at full credit;
    # iv_rank percentile 0 with prefer_low; dte 40 in band; trend/regime 1.0.
    result = S.compute_contract_score(make_contract(), PERFECT_CTX, CFG)
    assert result["total"] == 100.0
    assert all(c["score"] == 1.0 for c in result["components"])


def test_composite_with_one_weak_component():
    # delta 0.50 scores 1/3; everything else stays 1.0.
    # total = 100 * (0.20 * 1/3 + 0.80) = 86.7 after rounding
    result = S.compute_contract_score(make_contract(delta=0.50), PERFECT_CTX, CFG)
    assert result["total"] == pytest.approx(86.7)
    by_name = {c["name"]: c for c in result["components"]}
    assert by_name["delta_fit"]["score"] == pytest.approx(1.0 / 3.0, abs=1e-4)
    assert by_name["delta_fit"]["contribution"] == pytest.approx(0.20 / 3.0, abs=1e-4)


def test_breakdown_contributions_sum_to_total():
    result = S.compute_contract_score(
        make_contract(delta=0.55, open_interest=400, volume=120, dte=22),
        {"spot": 100.0, "trend01": 0.75, "regime01": 0.6, "iv_rank": 0.3},
        CFG,
    )
    summed = sum(c["contribution"] for c in result["components"]) * 100.0
    assert result["total"] == pytest.approx(summed, abs=0.06)
    # every component must expose raw, score, weight, contribution
    for c in result["components"]:
        assert {"name", "raw", "score", "weight", "contribution", "note"} <= set(c)


def test_weights_are_normalized():
    cfg = {**CFG, "weights": {**CFG["weights"]}}
    for name in cfg["weights"]:
        cfg["weights"][name] = CFG["weights"][name] * 3.0  # same ratios, larger sum
    a = S.compute_contract_score(make_contract(), PERFECT_CTX, CFG)
    b = S.compute_contract_score(make_contract(), PERFECT_CTX, cfg)
    assert a["total"] == b["total"]


def test_better_contract_ranks_higher():
    good = S.compute_contract_score(make_contract(), PERFECT_CTX, CFG)
    worse = S.compute_contract_score(
        make_contract(delta=0.45, open_interest=150, volume=30), PERFECT_CTX, CFG
    )
    assert good["total"] > worse["total"]
