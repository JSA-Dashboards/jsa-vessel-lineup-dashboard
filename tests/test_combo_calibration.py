"""Combo boats: shares learned from USDA FGIS vessel inspections, never an assumed split.

    pytest tests/test_combo_calibration.py -v
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import combo_calibration as cc
from fact_model import store


def ship(vessel, grain_kmt, date="2026-03-12", port="MISSISSIPPI R."):
    return [{"date": pd.Timestamp(date), "vessel_key": cc.vessel_key(vessel), "grain": g.upper(), "commodity": c, "kmt": k, "port": port}
            for g, c, k in [(g, c, k) for (g, c), k in grain_kmt.items()]]


def combo_event(i, vessel, combo, kmt, date="2026-03-14", region="US Gulf"):
    return {"event_id": f"e{i}", "source": "US", "region": region, "port": "South Louisiana", "fgis_port": "MISSISSIPPI R.", "vessel": vessel,
            "commodity": "Mixed Cargo", "combo": combo, "kmt": kmt, "sail_date": pd.Timestamp(date), "vessel_w": 1.0}


def test_names_and_combo_types_normalise():
    assert cc.vessel_key("MV  Star-Opal") == cc.vessel_key("STAR OPAL") == "STAROPAL"
    assert cc.combo_key("SBM/CORN") == cc.combo_key("CORN/SBM") == ("Corn", "Soybean Meal")
    assert cc.combo_key("CORN") == () and cc.combo_key(None) == ()


def test_a_matched_combo_gives_its_real_split_not_an_even_one():
    ships = pd.DataFrame(ship("AAA", {("CORN", "Corn"): 40.0}) + ship("BBB", {("CORN", "Corn"): 30.0, ("WHEAT", "Wheat"): 31.0}))
    ev = pd.DataFrame([combo_event(1, "AAA", "CORN/SBM", 60.0), combo_event(2, "BBB", "CORN/WHT", 60.0)])
    obs = cc.observed_shares(cc.match_events(ev, ships))
    a = obs[obs["event_id"] == "e1"].set_index("commodity")["share"]
    assert a["Corn"] == pytest.approx(40 / 60) and a["Soybean Meal"] == pytest.approx(20 / 60)     # meal = what FGIS does not inspect
    b = obs[obs["event_id"] == "e2"].set_index("commodity")["share"]
    assert b["Corn"] == pytest.approx(30 / 60) and b["Wheat"] == pytest.approx(31 / 60)


def test_matches_that_do_not_add_up_are_dropped():
    # grain-only combo whose inspected tonnage is far from the reported total (two-port loading) must not be used
    ships = pd.DataFrame(ship("CCC", {("CORN", "Corn"): 90.0}))
    ev = pd.DataFrame([combo_event(1, "CCC", "CORN/WHT", 60.0), combo_event(2, "NOSHIP", "CORN/SBM", 50.0)])
    m = cc.match_events(ev, ships)
    assert m.set_index("event_id").loc["e1", "matched"] and not m.set_index("event_id").loc["e2", "matched"]
    assert cc.observed_shares(m).empty


def _history(n=12, corn_share=0.7):
    ships, events = [], []
    for i in range(n):
        v = f"V{i}"
        ships += ship(v, {("CORN", "Corn"): 60 * corn_share})
        events.append(combo_event(i, v, "CORN/SBM", 60.0))
    return pd.DataFrame(ships), pd.DataFrame(events)


def test_learned_average_per_type_and_weights_fallback():
    ships, ev = _history()
    obs = cc.observed_shares(cc.match_events(ev, ships))
    w = cc.fit_weights(obs)
    assert w["Corn"] == pytest.approx(1.0) and 0.2 < w["Soybean Meal"] < 0.6          # meal weighs ~3/7 of corn here
    tab = cc.learn_shares(obs, w)
    us = tab[(tab["scope"] == "US") & (tab["key"] == "Corn / Soybean Meal")].set_index("commodity")["share"]
    assert us["Corn"] == pytest.approx(0.7) and us["Soybean Meal"] == pytest.approx(0.3)
    sh, method = cc.shares_for("CORN/SBM", "US Gulf", tab, w)
    assert method == "learned" and sh["Corn"] == pytest.approx(0.7)
    sh, method = cc.shares_for("SBM/WHT", "US Gulf", tab, w)                           # unseen type: fitted weights, not 50/50
    assert method == "model" and sum(sh.values()) == pytest.approx(1.0)
    assert sh["Wheat"] > sh["Soybean Meal"] and sh["Soybean Meal"] != pytest.approx(0.5)      # measured prior weights, not 50/50


def test_allocation_conserves_totals_and_prefers_the_vessels_own_split():
    ships, ev = _history()
    obs = cc.observed_shares(cc.match_events(ev, ships))
    w = cc.fit_weights(obs)
    tab, act = cc.learn_shares(obs, w), cc.complete_shares(obs, w)
    act = act[act["event_id"] != "e0"]                                                   # e0 has no measured split
    mix = pd.concat([ev, pd.DataFrame([combo_event(99, "NEW", "CORN/SBM", 40.0), {**combo_event(98, "X", "CORN", 10.0), "commodity": "Corn"}])])
    out = cc.allocate(mix, tab, w, act, value_cols=("kmt",), count_cols=("vessel_w",), id_col="event_id")
    assert out["kmt"].sum() == pytest.approx(mix["kmt"].sum()) and out["vessel_w"].sum() == pytest.approx(len(mix))
    assert set(out.loc[out["event_id"] == "e1", "split_method"]) == {"actual"}
    assert set(out.loc[out["event_id"] == "e0", "split_method"]) == {"learned"}          # no actual -> the type's average
    assert out.loc[(out["event_id"] == "e98"), "commodity"].tolist() == ["Corn"]         # single-commodity rows are untouched
    assert "Mixed Cargo" not in set(out["commodity"])


def test_an_even_split_is_the_worst_predictor():
    ships, ev = _history(n=30, corn_share=0.7)
    obs = cc.observed_shares(cc.match_events(ev, ships))
    res = cc.evaluate(obs, cc.fit_weights(obs))
    assert res["equal"][0] > res["learned"][0] and res["equal"][0] > res["weights"][0]


def test_refresh_saves_the_tables_the_dashboard_reads(tmp_path):
    ships, ev = _history()
    db = str(tmp_path / "f.db")
    store.save_events(ev.assign(mapped=True, unmapped_reason="", elevator="X", fgis_port="MISSISSIPPI R.", destination="CHINA"), db)
    meta = cc.refresh(db, ships=ships)
    assert meta["usable"] == 12 and meta["types_learned"] == 1
    table, weights, actual, m = cc.load_calibration(db)
    assert len(table) and "Corn" in weights and actual["event_id"].nunique() == 12 and m["matched"] == 12
    empty_table, empty_w, empty_a, empty_m = cc.load_calibration(str(tmp_path / "none.db"))
    assert empty_table.empty and empty_w["Corn"] == 1.0 and not empty_m                    # app still runs before the first refresh
