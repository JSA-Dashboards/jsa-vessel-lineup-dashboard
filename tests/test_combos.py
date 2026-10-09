"""Combo-boat allocation: corn (else wheat) counts double, everything else once; totals conserved.

    pytest tests/test_combos.py -v
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import combos as cb


def test_the_rule_two_three_four_commodities():
    two = cb.combo_shares("CORN/SBM")
    assert two == pytest.approx({"Corn": 2 / 3, "Soybean Meal": 1 / 3})
    three = cb.combo_shares("CORN/SBM/WHT")
    assert three == pytest.approx({"Corn": 1 / 2, "Soybean Meal": 1 / 4, "Wheat": 1 / 4})
    four = cb.combo_shares("CORN/SBM/WHT/YSB")
    assert four["Corn"] == pytest.approx(2 / 5) and four["Wheat"] == pytest.approx(1 / 5) and sum(four.values()) == pytest.approx(1)


def test_order_in_the_string_does_not_matter_and_codes_listed_twice_count_once():
    assert cb.combo_shares("SBM/CORN") == pytest.approx(cb.combo_shares("CORN/SBM"))
    assert cb.combo_shares("CORN/CORN/SBM") == pytest.approx(cb.combo_shares("CORN/SBM"))


def test_wheat_is_the_heavy_one_when_there_is_no_corn():
    assert cb.combo_shares("SBM/WHT") == pytest.approx({"Wheat": 2 / 3, "Soybean Meal": 1 / 3})
    assert cb.combo_shares("WHT/SBM/YSB") == pytest.approx({"Wheat": 1 / 2, "Soybean Meal": 1 / 4, "Soybeans": 1 / 4})
    # corn still wins when both are there: wheat is then an ordinary "other"
    assert cb.combo_shares("CORN/WHT") == pytest.approx({"Corn": 2 / 3, "Wheat": 1 / 3})
    assert cb.combo_shares("WHT/CORN/SBM") == pytest.approx({"Corn": 1 / 2, "Wheat": 1 / 4, "Soybean Meal": 1 / 4})


def test_neither_corn_nor_wheat_splits_equally_and_distillers_codes_pool_into_one_group():
    assert cb.combo_shares("SBM/YSB") == pytest.approx({"Soybean Meal": 0.5, "Soybeans": 0.5})
    assert cb.combo_shares("SBM/YSB/RICE") == pytest.approx({"Soybean Meal": 1 / 3, "Soybeans": 1 / 3, "Rice": 1 / 3})
    # DDGS and GDDG are two listed commodities (weight 1 each) that both belong to Dist. Grains
    s = cb.combo_shares("CORN/DDGS/GDDG")
    assert s == pytest.approx({"Corn": 1 / 2, "Dist. Grains": 1 / 2})


def test_unknown_codes_become_other_and_non_combos_return_nothing():
    assert cb.combo_shares("CORN/XYZ") == pytest.approx({"Corn": 2 / 3, "Other": 1 / 3})
    assert cb.combo_shares("CORN") == {} and cb.combo_shares("") == {} and cb.combo_shares(None) == {} and cb.combo_shares(float("nan")) == {}


def frame():
    return pd.DataFrame({
        "port": ["South Louisiana"] * 4, "commodity": ["Mixed Cargo", "Mixed Cargo", "Corn", "Mixed Cargo"],
        "combo": ["CORN/SBM", "CORN/SBM/WHT", "", ""], "kmt": [60.0, 40.0, 100.0, 25.0], "vessels": [1.0, 1.0, 1.0, 1.0]})


def test_allocation_conserves_tonnage_and_vessel_counts_and_marks_split_rows():
    f = frame()
    out = cb.allocate_combos(f, value_cols=("kmt",), count_cols=("vessels",))
    assert out["kmt"].sum() == pytest.approx(f["kmt"].sum()) and out["vessels"].sum() == pytest.approx(f["vessels"].sum())
    by = out.groupby("commodity")["kmt"].sum()
    assert by["Corn"] == pytest.approx(100 + 60 * 2 / 3 + 40 * 1 / 2)
    assert by["Soybean Meal"] == pytest.approx(60 / 3 + 40 / 4) and by["Wheat"] == pytest.approx(40 / 4)
    assert out.loc[out["from_combo"], "commodity"].isin(["Corn", "Soybean Meal", "Wheat"]).all()


def test_a_combo_without_a_string_stays_mixed_cargo():
    out = cb.allocate_combos(frame(), value_cols=("kmt",))
    assert out.loc[out["commodity"] == "Mixed Cargo", "kmt"].sum() == 25.0          # the row with no string is untouched


def test_combo_mix_uses_trailing_tonnage_weighted_shares_per_port():
    e = pd.DataFrame({
        "source": "US", "region": "US Gulf", "port": ["A", "A", "B", "A"], "commodity": ["Mixed Cargo"] * 4,
        "combo": ["CORN/SBM", "CORN/SBM", "SBM/WHT", "CORN/WHT"], "kmt": [30.0, 30.0, 50.0, 10.0],
        "sail_date": pd.to_datetime(["2026-08-01", "2026-09-01", "2026-09-02", "2024-01-01"])})            # last row is older than 12 months
    m = cb.combo_mix(e).set_index(["port", "commodity"])["share"]
    assert m[("A", "Corn")] == pytest.approx(2 / 3) and m[("A", "Soybean Meal")] == pytest.approx(1 / 3)
    assert m[("B", "Wheat")] == pytest.approx(2 / 3) and m[("B", "Soybean Meal")] == pytest.approx(1 / 3)       # SBM/WHT: wheat is heavy
    assert m.groupby(level="port").sum().tolist() == pytest.approx([1.0, 1.0])


# ── plumbing: the raw combo string survives into events and snapshots ────────

def _vessels(rows):
    base = {"sheet": "USG", "elevator_raw": "CHS", "vessel": "V", "status_norm": "Sailed", "kmt": 60.0,
            "commodity_raw": "CORN/SBM", "destination": "CHINA", "sail_date": pd.Timestamp("2026-09-10")}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_events_and_snapshots_keep_the_combo_string_only_for_combos():
    from fact_model import events as ev, lineup as lu
    e = ev.us_sail_events(_vessels([{"vessel": "A"}, {"vessel": "B", "commodity_raw": "CORN"}]))
    assert dict(zip(e["vessel"], e["combo"])) == {"A": "CORN/SBM", "B": ""} and dict(zip(e["vessel"], e["commodity"])) == {"A": "Mixed Cargo", "B": "Corn"}
    q = _vessels([{"vessel": "A", "status_norm": "ETA", "sail_date": pd.NaT}, {"vessel": "B", "commodity_raw": "WHT", "status_norm": "ETA", "sail_date": pd.NaT}])
    s = lu.us_lineup_snapshot(q, "2026-10-07")
    assert dict(zip(s["commodity"], s["combo"])) == {"Mixed Cargo": "CORN/SBM", "Wheat": ""}


def test_execution_math_counts_split_vessels_pro_rata_and_conserves_tonnage():
    from fact_model import events as ev
    e = ev.us_sail_events(_vessels([{"vessel": "A"}, {"vessel": "B", "commodity_raw": "CORN"}]))
    e["vessel_w"] = 1.0
    a = cb.allocate_combos(e, value_cols=("kmt",), count_cols=("vessel_w",))
    x = ev.events_executed(a, "2026-09-30", by=["commodity"]).set_index("commodity")
    assert x["mtd_kmt"].sum() == pytest.approx(120.0) and x["vessels_mtd"].sum() == pytest.approx(2.0)
    assert x.loc["Corn", "mtd_kmt"] == pytest.approx(60 + 40) and x.loc["Soybean Meal", "mtd_kmt"] == pytest.approx(20)
    assert x.loc["Corn", "vessels_mtd"] == pytest.approx(1 + 2 / 3)
