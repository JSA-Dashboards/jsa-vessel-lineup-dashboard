"""PNG / chart titles say what the table is and what it is filtered to.

    pytest tests/test_titles.py -v
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy_tools
import overview as ov

FRAME = pd.DataFrame([
    {"source": "US", "region": "US Gulf", "area": "USG", "port": "New Orleans", "elevator": "ADM", "commodity": "Corn"},
    {"source": "US", "region": "US Gulf", "area": "USG", "port": "Baton Rouge", "elevator": "CHS", "commodity": "Soybeans"},
    {"source": "US", "region": "PNW", "area": "PNW", "port": "Kalama", "elevator": "EGT", "commodity": "Wheat"},
    {"source": "Brazil", "region": "Brazil North Arc", "area": "Brazil North Arc", "port": "Itaqui", "elevator": "TEGRAM", "commodity": "Corn"},
    {"source": "Argentina", "region": "Argentina Up-River", "area": "Argentina Up-River", "port": "Rosario", "elevator": "X", "commodity": "Soybeans"},
])
ALL = {"source": ["US", "Brazil", "Argentina"], "area": ["USG", "PNW", "Brazil North Arc", "Argentina Up-River"], "commodity": ov.COMMODITIES}


def test_nothing_narrowed_reads_all_countries():
    assert ov.scope_label(ALL, FRAME) == "all countries"
    assert ov.scope_label({}, FRAME) == "all countries"


def test_a_region_a_port_and_a_commodity_show_in_the_title():
    assert ov.scope_label({"source": ["US"], "area": ["USG"], "commodity": ov.COMMODITIES}, FRAME) == "United States · US Gulf"
    sel = {"source": ["US"], "area": ["USG"], "port": ["New Orleans"], "commodity": ["Corn", "Wheat"]}
    assert ov.scope_label(sel, FRAME) == "United States · US Gulf · New Orleans · Corn, Wheat"


def test_choosing_every_value_of_a_dimension_is_not_a_filter():
    sel = {"source": ["US"], "area": ["USG", "PNW"], "commodity": ["Corn", "Soybeans", "Wheat"]}
    assert ov.scope_label(sel, FRAME) == "United States"                   # both US areas and all three US commodities


def test_a_country_page_selection_by_region():
    sel = {"source": ["Brazil"], "region": ["Brazil North Arc"], "commodity": ["Corn"]}
    assert ov.scope_label(sel, FRAME) == "Brazil"                          # the whole of Brazil's one area and commodity
    assert ov.scope_label({"source": ["US"], "region": ["PNW"]}, FRAME) == "United States · PNW"


def test_a_long_list_is_cut_short():
    frame = pd.DataFrame({"source": "US", "port": [f"P{i}" for i in range(8)], "commodity": "Corn"})
    assert ov.scope_label({"source": ["US"], "port": [f"P{i}" for i in range(5)]}, frame) == "P0, P1, P2, +2 more"        # the one source in this frame is not a narrowing


def test_a_page_that_prefixes_its_keys_still_gets_the_snapshot_titles():
    # River FOB's keys are rfv_snap_now / rfv_snap_mtd; they used to come out as "Rfv snap now"
    assert copy_tools._table_title("rfv_snap_now") == "Line-up now"
    assert copy_tools._table_title("rfv_snap_mtd") == "Sailed month-to-date"
    assert copy_tools._table_title("snap_now") == "Line-up now"


def test_unit_in_titles():
    assert ov._vol("vessels") == "vessels" and ov._vol("kmt") == "volume (kMT)"
