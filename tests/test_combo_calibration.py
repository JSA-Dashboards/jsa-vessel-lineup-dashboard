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


# ── ongoing vetting ──────────────────────────────────────────────────────────

def _month_history(months=("2026-01-10", "2026-02-10", "2026-03-10", "2026-04-10"), per=10, corn=0.7):
    """Each month: `per` single-corn sailings, `per` CORN/SBM combos (corn share `corn`) - and FGIS inspected exactly that corn."""
    ships, events, i = [], [], 0
    for d in months:
        for _ in range(per):
            events.append({**combo_event(i, f"S{i}", "", 50.0, d), "commodity": "Corn", "combo": ""})
            ships += ship(f"S{i}", {("CORN", "Corn"): 50.0}, d)
            i += 1
        for _ in range(per):
            events.append(combo_event(i, f"C{i}", "CORN/SBM", 60.0, d))
            ships += ship(f"C{i}", {("CORN", "Corn"): 60.0 * corn}, d)
            i += 1
    return pd.DataFrame(ships), pd.DataFrame(events)


def test_walk_forward_backtest_ranks_the_strategies_against_usda(monkeypatch):
    ships, ev = _month_history()
    obs = cc.observed_shares(cc.match_events(ev, ships))
    w = cc.fit_weights(obs)
    monkeypatch.setattr(pd.Timestamp, "now", classmethod(lambda cls, tz=None: pd.Timestamp("2026-06-15")))
    bt = cc.backtest_monthly(ev, ships, obs, w, months=3, min_n=5)
    assert set(bt["strategy"]) == set(cc.STRATEGIES) and bt["month"].nunique() == 3
    s = cc.backtest_summary(bt)
    # the walk-forward learned split (70% corn) lands on USDA; leaving combos out misses their corn; an even split overshoots less than excluding
    assert s.loc["learned", "wape"] < 0.05 < s.loc["excluded", "wape"]
    assert s.loc["learned", "wape"] < s.loc["equal", "wape"] < s.loc["excluded", "wape"]
    assert s.loc["excluded", "bias"] < 0 and abs(s.loc["learned", "bias"]) < 0.05


def test_health_flags_a_split_that_stopped_beating_the_placeholder():
    ok_meta = {"combo_events": 1000, "matched": 800, "usable": 700, "mae_learned": 0.15, "mae_equal": 0.23}
    good = pd.DataFrame({"wape": [0.40, 0.30, 0.29, 0.28, 0.28]}, index=cc.STRATEGIES)
    assert cc.health(ok_meta, good) == ("ok", [])
    worse_than_even = {**ok_meta, "mae_learned": 0.22}
    status, why = cc.health(worse_than_even, good)
    assert status == "fail" and "even split" in why[0]
    status, why = cc.health({**ok_meta, "matched": 300}, good)
    assert status == "fail" and "matched" in why[0]
    hurts = good.copy(); hurts.loc["learned", "wape"] = 0.55
    assert cc.health(ok_meta, hurts)[0] == "fail"
    assert cc.health(ok_meta, good, moved=[("Corn / Soybean Meal", "Corn", 0.70, 0.55)])[0] == "warn"


def test_drift_reports_learned_shares_that_jumped_since_the_last_refresh():
    prev = pd.DataFrame([{"date": "2026-10-01", "scope": "US", "key": "Corn / Soybean Meal", "commodity": c, "share": s, "n": 200}
                         for c, s in (("Corn", 0.70), ("Soybean Meal", 0.30))])
    now = pd.DataFrame([{"scope": "US", "key": "Corn / Soybean Meal", "commodity": c, "share": s, "p10": 0, "p90": 1, "n": 200}
                        for c, s in (("Corn", 0.55), ("Soybean Meal", 0.45))])
    moved = cc.drift(prev, now)
    assert {(k, c) for k, c, b, n in moved} == {("Corn / Soybean Meal", "Corn"), ("Corn / Soybean Meal", "Soybean Meal")}
    assert cc.drift(prev, now.assign(share=now["share"].replace({0.55: 0.68, 0.45: 0.32}))) == []
    assert cc.drift(None, now) == []


def test_refresh_stores_the_check_and_its_history(tmp_path, monkeypatch):
    ships, ev = _month_history()
    db = str(tmp_path / "f.db")
    store.save_events(ev.assign(mapped=True, unmapped_reason="", elevator="X", destination="CHINA"), db)
    monkeypatch.setattr(pd.Timestamp, "now", classmethod(lambda cls, tz=None: pd.Timestamp("2026-06-15")))
    meta = cc.refresh(db, ships=ships, backtest_months=3)
    assert meta["status"] in ("ok", "warn", "fail") and meta["wape_learned"] < meta["wape_excluded"]
    cc.refresh(db, ships=ships, backtest_months=3)                                        # same day twice -> one history row
    bt, hist, shist = cc.load_vetting(db)
    assert len(bt) and len(hist) == 1 and len(shist) > 0
