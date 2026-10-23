"""Pure helpers behind the Summary / country pages (no Streamlit session needed).

    pytest tests/test_overview.py -v
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import overview as ov
from fact_model import changes as ch


def lineup(date, rows, source="US", region="US Gulf"):
    return pd.DataFrame([{"report_date": pd.Timestamp(date), "source": source, "region": region, "port": p, "elevator": e,
                          "commodity": c, "vessels": v, "kmt": v * 50.0} for p, e, c, v in rows])


def test_scope_is_the_six_commodities_in_the_requested_order():
    assert ov.COMMODITIES == ["Corn", "Soybeans", "Wheat", "Sorghum", "Soybean Meal", "Soybean Oil"]


def test_change_table_has_a_total_row_and_drops_all_zero_rows():
    l = pd.concat([lineup("2026-10-21", [("A", "E1", "Corn", 10), ("A", "E2", "Soybeans", 5)]),
                   lineup("2026-10-14", [("A", "E1", "Corn", 8), ("A", "E2", "Wheat", 0)])])
    d, _ = ch.lineup_detail(l, "vessels")
    t = ov.change_table(d, "commodity", "Commodity")
    assert t["Commodity"].tolist() == ["Corn", "Soybeans", "Total"]            # the all-zero Wheat row is dropped
    assert t.loc[t["Commodity"] == "Total", "Now"].iloc[0] == "15"
    assert t.loc[t["Commodity"] == "Corn", "Δ Week"].iloc[0] == "+2" and t.loc[t["Commodity"] == "Soybeans", "Δ Week"].iloc[0] == "+5"


def test_elevator_is_a_grouping_level_and_filters_apply_to_any_frame():
    l = pd.concat([lineup("2026-10-21", [("A", "E1", "Corn", 10), ("A", "E2", "Corn", 4), ("B", "E3", "Wheat", 3)]),
                   lineup("2026-10-14", [("A", "E1", "Corn", 8)])])
    d, _ = ch.lineup_detail(l, "vessels")
    only = ov._apply(d, {"port": ["A"], "elevator": None, "commodity": ov.COMMODITIES})
    g = ch.rollup(only, ["elevator"]).set_index("elevator")
    assert g.loc["E1", "now"] == 10 and g.loc["E2", "now"] == 4 and "E3" not in g.index


def test_number_formats():
    assert ov._num(12) == "12" and ov._num(12.4) == "12.4" and ov._num(float("nan")) == "n/a"
    assert ov._delta(3) == "+3" and ov._delta(-2) == "−2" and ov._delta(0) == "0" and ov._delta(None) == "n/a"
    assert ov._pct(12.4) == "+12%" and ov._pct(-30) == "−30%"
