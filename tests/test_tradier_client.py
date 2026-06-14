"""Deterministic tests for Tradier response normalization (pure functions)."""
import datetime as dt

from data.tradier_client import listify, map_order_status, norm_chain_item, occ_underlying

TODAY = dt.date(2026, 6, 10)


def test_listify_handles_single_and_array_and_none():
    assert listify(None) == []
    assert listify({"a": 1}) == [{"a": 1}]
    assert listify([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]


def test_map_order_status():
    assert map_order_status("open") == "new"
    assert map_order_status("filled") == "filled"
    assert map_order_status("partially_filled") == "partially_filled"
    assert map_order_status("canceled") == "canceled"
    assert map_order_status("rejected") == "rejected"
    assert map_order_status("error") == "rejected"
    assert map_order_status(None) is None


def test_occ_underlying():
    assert occ_underlying("NVDA260717C00170000") == "NVDA"
    assert occ_underlying("SPY260717C00600000") == "SPY"
    assert occ_underlying("not-an-occ") is None


def _chain_item(**overrides):
    item = {
        "symbol": "NVDA260717C00170000",
        "description": "NVDA Jul 17 2026 $170 Call",
        "last": 14.10,
        "bid": 14.05,
        "ask": 14.45,
        "volume": 1893,
        "open_interest": 4521,
        "strike": 170.0,
        "expiration_date": "2026-07-17",
        "option_type": "call",
        "greeks": {
            "delta": 0.72, "gamma": 0.013, "theta": -0.085, "vega": 0.21,
            "rho": 0.05, "mid_iv": 0.41, "smv_vol": 0.40,
        },
    }
    item.update(overrides)
    return item


def test_norm_chain_item_full():
    norm = norm_chain_item(_chain_item(), "NVDA", TODAY)
    assert norm["occ_symbol"] == "NVDA260717C00170000"
    assert norm["underlying"] == "NVDA"
    assert norm["type"] == "call"
    assert norm["strike"] == 170.0
    assert norm["expiration"] == "2026-07-17"
    assert norm["dte"] == 37
    assert norm["bid"] == 14.05
    assert norm["ask"] == 14.45
    assert norm["mid"] == round((14.05 + 14.45) / 2, 4)
    assert norm["volume"] == 1893
    assert norm["open_interest"] == 4521
    assert norm["delta"] == 0.72
    assert norm["iv"] == 0.41  # mid_iv


def test_norm_chain_item_iv_falls_back_to_smv():
    item = _chain_item()
    item["greeks"].pop("mid_iv")
    norm = norm_chain_item(item, "NVDA", TODAY)
    assert norm["iv"] == 0.40


def test_norm_chain_item_no_quotes_means_no_mid():
    norm = norm_chain_item(_chain_item(bid=0, ask=0), "NVDA", TODAY)
    assert norm["mid"] is None
    assert norm["bid"] == 0.0


def test_norm_chain_item_missing_greeks():
    item = _chain_item()
    del item["greeks"]
    norm = norm_chain_item(item, "NVDA", TODAY)
    assert norm["delta"] is None
    assert norm["iv"] is None
    # still produces a usable contract row
    assert norm["strike"] == 170.0 and norm["mid"] is not None


def test_norm_chain_item_put_type():
    norm = norm_chain_item(_chain_item(option_type="put"), "NVDA", TODAY)
    assert norm["type"] == "put"
