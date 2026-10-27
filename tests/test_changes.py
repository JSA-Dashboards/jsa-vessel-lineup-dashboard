"""Week / month / year changes for the line-up and for sailings.

    pytest tests/test_changes.py -v
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import changes as ch


def snap(date, rows, source="US", region="US Gulf"):
    return pd.DataFrame([{"report_date": pd.Timestamp(date), "source": source, "region": region, "port": p, "commodity": c,
                          "vessels": v, "kmt": v * 50.0} for p, c, v in rows])


def test_detail_compares_with_snapshots_a_week_month_and_year_back():
    l = pd.concat([snap("2026-10-21", [("A", "Corn", 10), ("A", "Soybeans", 5)]),
                   snap("2026-10-14", [("A", "Corn", 8), ("A", "Soybeans", 5)]),
                   snap("2026-09-21", [("A", "Corn", 4)]),
                   snap("2025-10-21", [("A", "Corn", 12), ("A", "Soybeans", 1)])])
    d, m = ch.lineup_detail(l, "vessels")
    r = ch.rollup(d, ["commodity"]).set_index("commodity")
    assert r.loc["Corn", ["now", "d_wk", "d_mo", "d_yr"]].tolist() == [10, 2, 6, -2]
    assert r.loc["Soybeans", "d_mo"] == 5                       # absent a month ago but a baseline exists -> 0, not n/a
    assert m.iloc[0]["wk_date"] == pd.Timestamp("2026-10-14") and m.iloc[0]["yr_date"] == pd.Timestamp("2025-10-21")


def test_no_close_snapshot_means_nan_not_zero():
    l = pd.concat([snap("2026-10-21", [("A", "Corn", 10)]), snap("2026-10-14", [("A", "Corn", 8)])])
    r = ch.rollup(ch.lineup_detail(l, "vessels")[0], ["commodity"]).iloc[0]
    assert r["d_wk"] == 2 and np.isnan(r["d_mo"]) and np.isnan(r["d_yr"])


def test_an_empty_snapshot_of_a_busy_region_is_a_gap_not_a_zero():
    busy = [("A", "Corn", 12)]
    l = pd.concat([snap("2026-10-02", busy), snap("2026-10-09", busy), snap("2026-10-13", busy),
                   snap("2026-10-14", busy), snap("2026-10-21", [])])        # newest file had an empty sheet
    # keep the date alive with another region, as in the real Southport files
    l = pd.concat([l, snap("2026-10-21", [("P", "Wheat", 3)], region="PNW")])
    d, m = ch.lineup_detail(l, "vessels")
    usg = m[m["region"] == "US Gulf"].iloc[0]
    assert usg["as_of"] == pd.Timestamp("2026-10-14") and "no US Gulf line-up" in usg["note"]
    assert ch.rollup(d[d["region"] == "US Gulf"], ["region"]).iloc[0]["now"] == 12
    assert pd.Timestamp("2026-10-21") in ch.gap_dates(l)


def test_a_stray_region_seen_on_a_few_dates_does_not_turn_every_other_date_into_a_data_gap():
    # Brazil, Oct 2026: 3 dates carried a 'UNMAPPED' region (rows whose port heading was lost at a PDF page break), 10+ vessels
    # each time. That made UNMAPPED look like a region that "normally holds vessels", so the other 448 dates were dropped as gaps.
    days = pd.date_range("2026-06-01", periods=60)
    main = pd.concat([snap(d, [("P", "Corn", 40)], source="Brazil", region="Brazil North Arc") for d in days])
    stray = pd.concat([snap(d, [("nan", "Corn", 12)], source="Brazil", region="UNMAPPED") for d in days[[5, 20, 40]]])
    l = pd.concat([main, stray])
    assert ch.gap_dates(l) == set()
    d, m = ch.lineup_detail(l, "vessels")
    assert m.set_index("region").loc["UNMAPPED", "note"] == ""        # it is not 'stuck' at its last sighting either


def test_a_source_without_history_drops_out_of_the_change_instead_of_counting_as_growth():
    us = pd.concat([snap("2026-10-22", [("A", "Corn", 10)]), snap("2026-10-15", [("A", "Corn", 8)])])
    ar = snap("2026-10-22", [("S", "Corn", 40)], source="Argentina", region="Argentina Up-River")
    d, _ = ch.lineup_detail(pd.concat([us, ar]), "vessels")
    r = ch.rollup(d, ["commodity"]).iloc[0]
    assert r["now"] == 50 and r["d_wk"] == 2                 # Argentina's 40 is in "now" but not in the +change
    assert ch.rollup(d[d["source"] == "Argentina"], ["commodity"]).iloc[0]["d_wk"] != ch.rollup(d[d["source"] == "Argentina"], ["commodity"]).iloc[0]["d_wk"]  # NaN


def ev(date, source="US", commodity="Corn", kmt=50.0, w=1.0, port="A"):
    return {"sail_date": pd.Timestamp(date), "source": source, "region": "R", "port": port, "commodity": commodity,
            "kmt": kmt, "vessel_w": w}


def test_sailed_windows_week_month_year_and_ytd():
    e = pd.DataFrame([ev("2026-10-21"), ev("2026-10-20"), ev("2026-10-12"),            # l7: 21,20 ; prior 7: 12
                      ev("2026-10-05"), ev("2026-09-03"), ev("2026-09-30"),             # MTD 4 ; last month same days: 03 -> 1
                      ev("2025-10-02"), ev("2025-10-20"), ev("2025-01-15"), ev("2025-03-01"), ev("2024-12-01")])
    w = ch.sailed_windows(e, ["commodity"], "vessels").iloc[0]
    assert (w["l7"], w["l7_prev"]) == (2, 1)
    assert w["mtd"] == 4 and w["mtd_pm"] == 1 and w["mtd_ly"] == 2                     # last year: Oct 2 and Oct 20 are both within days 1-21
    assert w["d_wk"] == 1 and w["d_mo"] == 3


def test_sailed_volume_measure_and_no_history_windows_are_nan():
    e = pd.DataFrame([ev("2026-10-21", source="Argentina", kmt=40.0), ev("2026-10-01", source="Argentina", kmt=10.0)])
    w = ch.sailed_windows(e, ["commodity"], "kmt").iloc[0]
    assert w["mtd"] == 50 and np.isnan(w["mtd_pm"]) and np.isnan(w["mtd_ly"]) and np.isnan(w["d_mo"])


def test_sailed_vessel_measure_uses_the_split_weight():
    e = pd.DataFrame([ev("2026-10-21", w=0.25), ev("2026-10-21", w=0.75)])
    assert ch.sailed_windows(e, ["commodity"], "vessels").iloc[0]["l7"] == pytest.approx(1.0)
