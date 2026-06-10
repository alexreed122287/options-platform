"""Deterministic tests for Public.com response normalization (pure functions)."""
import datetime as dt

from data.public_client import compact_occ, map_order_status, norm_chain_item

TODAY = dt.date(2026, 6, 10)


def test_compact_occ_strips_osi_padding():
    assert compact_occ("AAPL  260717C00210000") == "AAPL260717C00210000"
    assert compact_occ("spy260717c00600000") == "SPY260717C00600000"
    assert compact_occ(None) == ""


def test_map_order_status():
    assert map_order_status("NEW") == "new"
    assert map_order_status("FILLED") == "filled"
    assert map_order_status("PARTIALLY_FILLED") == "partially_filled"
    assert map_order_status("CANCELLED") == "canceled"
    assert map_order_status("QUEUED_CANCELLED") == "canceled"
    assert map_order_status("REJECTED") == "rejected"
    assert map_order_status("SOMETHING_NEW") == "something_new"
    assert map_order_status(None) is None


def _chain_item(**overrides):
    item = {
        "instrument": {"symbol": "NVDA  260717C00170000", "type": "OPTION"},
        "outcome": "SUCCESS",
        "last": "14.10",
        "bid": "14.05",
        "ask": "14.45",
        "volume": 1893,
        "openInterest": 4521,
        "optionDetails": {
            "greeks": {
                "delta": "0.72",
                "gamma": "0.013",
                "theta": "-0.085",
                "vega": "0.21",
                "impliedVolatility": "0.41",
            },
            "strikePrice": "170",
            "midPrice": "14.25",
        },
    }
    item.update(overrides)
    return item


def test_norm_chain_item_full():
    norm = norm_chain_item(_chain_item(), "NVDA", "call", TODAY)
    assert norm["occ_symbol"] == "NVDA260717C00170000"
    assert norm["underlying"] == "NVDA"
    assert norm["strike"] == 170.0
    assert norm["expiration"] == "2026-07-17"
    assert norm["dte"] == 37
    assert norm["bid"] == 14.05
    assert norm["ask"] == 14.45
    assert norm["mid"] == 14.25  # Public-provided midPrice wins
    assert norm["volume"] == 1893
    assert norm["open_interest"] == 4521
    assert norm["delta"] == 0.72
    assert norm["iv"] == 0.41


def test_norm_chain_item_mid_computed_when_missing():
    item = _chain_item()
    item["optionDetails"]["midPrice"] = ""
    norm = norm_chain_item(item, "NVDA", "call", TODAY)
    assert norm["mid"] == round((14.05 + 14.45) / 2, 4)


def test_norm_chain_item_no_quotes_means_no_mid():
    item = _chain_item(bid="", ask="")
    item["optionDetails"]["midPrice"] = ""
    norm = norm_chain_item(item, "NVDA", "call", TODAY)
    assert norm["mid"] is None
    assert norm["bid"] == 0.0 and norm["ask"] == 0.0


def test_norm_chain_item_strike_falls_back_to_occ():
    item = _chain_item()
    item["optionDetails"]["strikePrice"] = None
    norm = norm_chain_item(item, "NVDA", "call", TODAY)
    assert norm["strike"] == 170.0  # parsed from 00170000


def test_norm_chain_item_rejects_unparseable_symbol():
    item = _chain_item()
    item["instrument"]["symbol"] = "NOT_AN_OCC"
    assert norm_chain_item(item, "NVDA", "call", TODAY) is None
