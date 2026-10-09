"""Line-up summary line: counts, LW / LM changes, format, and the empty-latest-snapshot guard.

    pytest tests/test_summary.py -v
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import summary as sm


def lf(rows):
    """rows: (date, source, region, commodity, vessels)"""
    return pd.DataFrame(rows, columns=["report_date", "source", "region", "commodity", "vessels"])


def pnw_history():
    # latest Oct 7; last week Sep 30; last month Sep 7 (all dated snapshots exist)
    d = lambda s: pd.Timestamp(s)
    r = []
    for date, ysb, yc, sbm in [("2026-09-07", 6, 7, 3), ("2026-09-30", 12, 7, 4), ("2026-10-07", 10, 6, 4)]:
        r += [(d(date), "US", "PNW", "Soybeans", ysb), (d(date), "US", "PNW", "Corn", yc), (d(date), "US", "PNW", "Soybean Meal", sbm)]
    return lf(r)


def test_matches_the_requested_format_exactly():
    line = sm.summary_lines(sm.lineup_summary(pnw_history()))[0][3]
    assert line == "PNW Total 20 (-3 LW, +4 LM), YSB 10 (-2 LW, +4 LM), YC 6 (-1 LW, -1 LM), SBM 4 (Unch LW, +1 LM)"


def test_unchanged_and_signs():
    s = sm.lineup_summary(pnw_history()).set_index("commodity")
    assert s.loc["Soybean Meal", "d_lw"] == 0 and sm._delta(0) == "Unch" and sm._delta(4) == "+4" and sm._delta(-2) == "-2"


def test_baseline_is_the_nearest_banked_snapshot_within_tolerance_else_na():
    # snapshots only every ~2 days: LW target Sep 30 -> nearest Oct 1 (1 day off) is fine; nothing near Sep 7 -> n/a
    r = [(pd.Timestamp(d), "US", "PNW", "Corn", n) for d, n in [("2026-10-01", 5), ("2026-10-03", 6), ("2026-10-05", 7), ("2026-10-07", 9)]]
    s = sm.lineup_summary(lf(r))
    t = s[s["commodity"] == "Total"].iloc[0]
    assert t["lw_date"] == pd.Timestamp("2026-10-01") and t["d_lw"] == 4
    assert t["lm_date"] is None and pd.isna(t["d_lm"])
    assert "n/a LM" in sm.summary_lines(s)[0][3]


def test_a_commodity_that_disappeared_is_still_reported_with_its_change():
    r = [(pd.Timestamp("2026-09-30"), "US", "PNW", "Wheat", 3), (pd.Timestamp("2026-09-30"), "US", "PNW", "Corn", 5),
         (pd.Timestamp("2026-10-07"), "US", "PNW", "Corn", 6)]
    line = sm.summary_lines(sm.lineup_summary(lf(r)))[0][3]
    assert "WHT 0 (-3 LW" in line and line.startswith("PNW Total 6 (-2 LW")


def test_regions_and_sources_are_kept_apart():
    r = [(pd.Timestamp("2026-10-07"), "US", "UNMAPPED", "Corn", 2), (pd.Timestamp("2026-10-07"), "Brazil", "UNMAPPED", "Wheat", 5),
         (pd.Timestamp("2026-10-07"), "Brazil", "Brazil North Arc", "Corn", 40)]
    lines = {(src, reg): line for src, reg, _, line, _ in sm.summary_lines(sm.lineup_summary(lf(r)))}
    assert lines[("US", "UNMAPPED")].startswith("UNMAPPED Total 2") and lines[("Brazil", "UNMAPPED")].startswith("UNMAPPED Total 5")
    assert lines[("Brazil", "Brazil North Arc")].startswith("BZL North Arc Total 40")


def test_an_empty_latest_snapshot_for_a_normally_busy_region_uses_the_last_good_one():
    d = pd.Timestamp
    r = []
    for date in ["2026-09-09", "2026-09-16", "2026-09-23", "2026-09-30"]:
        r += [(d(date), "US", "US Gulf", "Corn", 20), (d(date), "US", "US Gulf", "Soybeans", 35), (d(date), "US", "PNW", "Corn", 8)]
    r += [(d("2026-10-02"), "US", "PNW", "Corn", 9)]                    # Oct 2 file: PNW present, Mississippi sheet empty
    out = sm.summary_lines(sm.lineup_summary(lf(r)))
    usg = next(o for o in out if o[1] == "US Gulf")
    assert usg[2] == d("2026-09-30") and usg[3].startswith("USG* Total 55") and "had no USG line-up" in usg[4]
    pnw = next(o for o in out if o[1] == "PNW")
    assert pnw[2] == d("2026-10-02") and pnw[4] == "" and "*" not in pnw[3]


def test_a_genuinely_small_region_going_to_zero_is_reported_as_zero():
    d = pd.Timestamp
    r = [(d("2026-09-30"), "US", "Texas Gulf", "Corn", 3), (d("2026-10-02"), "US", "PNW", "Corn", 9)]
    txg = next(o for o in sm.summary_lines(sm.lineup_summary(lf(r))) if o[1] == "Texas Gulf")
    assert txg[3].startswith("TXG Total 0") and txg[4] == ""            # 3 vessels is below the 'normally busy' bar: a real zero
