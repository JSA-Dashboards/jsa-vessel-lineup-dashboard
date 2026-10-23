"""Undisclosed ('RVT') sailings filled from USDA FGIS vessel inspections.

    pytest tests/test_fgis_fill.py -v
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import combo_calibration as cc
from fact_model import fgis_fill as ff


def ship(vessel, rows, date="2026-03-12", port="MISSISSIPPI R.", dest="CHINA"):
    return [{"date": pd.Timestamp(date), "vessel_key": cc.vessel_key(vessel), "grain": c.upper(), "commodity": c, "kmt": k, "port": port,
             "destination": dest} for c, k in rows.items()]


def ev(i, vessel, commodity="Other", kmt=np.nan, date="2026-03-14", combo="", port="MISSISSIPPI R."):
    return {"event_id": f"e{i}", "source": "US", "region": "US Gulf", "port": "South Louisiana", "fgis_port": port, "vessel": vessel,
            "commodity": commodity, "combo": combo, "kmt": kmt, "sail_date": pd.Timestamp(date), "vessel_w": 1.0, "destination": "UNKNOWN"}


def test_an_undisclosed_sailing_gets_its_commodity_tonnage_and_destination_from_usda():
    ships = pd.DataFrame(ship("STAR OPAL", {"Corn": 40.0, "Soybeans": 20.0}, dest="JAPAN"))
    events = pd.DataFrame([ev(1, "STAR OPAL"), ev(2, "OTHER SHIP", commodity="Corn", kmt=30.0)])
    f = ff.fill_undisclosed(events, ships)
    assert set(f["event_id"]) == {"e1"} and f.set_index("commodity")["kmt"].to_dict() == {"Corn": 40.0, "Soybeans": 20.0}
    assert f["share"].sum() == pytest.approx(1.0) and set(f["destination"]) == {"Japan"}
    out = ff.apply_fill(events, f)
    assert out["vessel_w"].sum() == pytest.approx(2.0) and out["kmt"].sum() == pytest.approx(90.0)        # vessel count conserved
    assert set(out.loc[out["fill_method"] == "usda", "commodity"]) == {"Corn", "Soybeans"}
    assert out.loc[out["event_id"] == "e2", "fill_method"].iloc[0] == ""                                 # disclosed boats untouched


def test_two_undisclosed_sailings_in_one_usda_visit_share_its_tonnage():
    ships = pd.DataFrame(ship("DOUBLE", {"Corn": 60.0}))
    events = pd.DataFrame([ev(1, "DOUBLE", date="2026-03-13"), ev(2, "DOUBLE", date="2026-03-15")])
    f = ff.fill_undisclosed(events, ships)
    assert f["kmt"].tolist() == [30.0, 30.0]


def test_tonnage_already_disclosed_on_the_same_visit_comes_off_the_top():
    ships = pd.DataFrame(ship("SPLITLOAD", {"Soybeans": 60.0}))
    events = pd.DataFrame([ev(1, "SPLITLOAD", commodity="Soybeans", kmt=20.0, date="2026-03-13"), ev(2, "SPLITLOAD", date="2026-03-14")])
    f = ff.fill_undisclosed(events, ships)
    assert f["kmt"].iloc[0] == pytest.approx(40.0)


def test_no_usda_record_or_a_different_visit_leaves_the_sailing_undisclosed():
    ships = pd.DataFrame(ship("OTHERVISIT", {"Corn": 50.0}, date="2025-11-01"))                 # months earlier
    events = pd.DataFrame([ev(1, "OTHERVISIT"), ev(2, "NOSHIP")])
    assert ff.fill_undisclosed(events, ships).empty
    assert ff.apply_fill(events, ff.fill_undisclosed(events, ships)).shape[0] == 2


def test_backtest_scores_the_fill_on_boats_whose_cargo_is_known():
    ships, events = [], []
    for i in range(20):
        ships += ship(f"V{i}", {"Corn": 50.0 + i})
        events.append(ev(i, f"V{i}", commodity="Corn", kmt=50.0 + i))
    r = ff.backtest_fill(pd.DataFrame(events), pd.DataFrame(ships))
    assert r["n"] == 20 and r["commodity_match"] == 1.0 and r["tonnage_total_ratio"] == pytest.approx(1.0)


def test_health_fails_when_the_fill_stops_being_trustworthy():
    base = {"combo_events": 1000, "matched": 800, "usable": 700, "mae_learned": 0.15, "mae_equal": 0.23, "fill_n": 500,
            "fill_commodity_match": 0.97, "fill_total_ratio": 0.99, "undisclosed": 1000, "filled": 850}
    summ = pd.DataFrame({"wape": [0.40, 0.30, 0.29, 0.28, 0.28]}, index=cc.STRATEGIES)
    assert cc.health(base, summ) == ("ok", [])
    assert cc.health({**base, "fill_commodity_match": 0.80}, summ)[0] == "fail"
    assert cc.health({**base, "fill_total_ratio": 1.30}, summ)[0] == "fail"
    assert cc.health({**base, "filled": 300}, summ)[0] == "warn"
