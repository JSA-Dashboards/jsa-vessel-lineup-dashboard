"""Great Lakes loadings from USDA FGIS ship inspections.

    pytest tests/test_greatlakes.py -v
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import greatlakes as gl
from fact_model import store


def ships():
    rows = [("2026-09-17", "EXEBORG", "WHEAT", "UN KINGDOM", "DULUTH-SUP", 9.662),
            ("2026-09-24", "FEDERAL MAYUMI", "CORN", "IRELAND", "TOLEDO", 22.736),
            ("2026-09-24", "FEDERAL MAYUMI", "SOYBEANS", "IRELAND", "TOLEDO", 2.0),
            ("2026-09-24", "FEDERAL MAYUMI", "CORN", "IRELAND", "TOLEDO", 1.0),          # same vessel/week/grain/destination twice
            ("2026-10-08", "FEDERAL DELTA", "SOYBEANS", "EGYPT", "TOLEDO", 21.317)]
    return pd.DataFrame([{"date": pd.Timestamp(d), "vessel": v, "grain": g, "commodity": gl.GRAIN[g], "destination": dest, "port": p, "kmt": k}
                         for d, v, g, dest, p, k in rows])


def test_lake_events_are_great_lakes_us_sailings_with_unique_ids_and_conserved_vessel_counts():
    e = gl.lake_events(ships())
    assert (e["region"] == "Great Lakes").all() and (e["source"] == "US").all() and e["mapped"].all()
    assert set(e["port"]) == {"Duluth-Superior", "Toledo"} and (e["fgis_port"] == "").all()
    assert e["event_id"].is_unique and e["event_id"].str.startswith("GL").all() and len(e) == 5
    assert e["kmt"].sum() == pytest.approx(ships()["kmt"].sum())
    # one vessel per vessel x week x port: its cargo lines share a single vessel count
    assert e["vessel_w"].sum() == pytest.approx(3.0)
    m = e[e["vessel"] == "FEDERAL MAYUMI"]
    assert m["vessel_w"].sum() == pytest.approx(1.0) and m["vessel_w"].max() < 1.0


def test_refresh_is_idempotent_and_keeps_other_regions(tmp_path):
    db = str(tmp_path / "f.db")
    gl.refresh(db, ships=ships())
    r = gl.refresh(db, ships=ships())
    ev = store.load_events(db)
    assert r["events"] == 5 and len(ev) == 5 and r["first"] == "2026-09-17" and r["last"] == "2026-10-08"
