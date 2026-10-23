"""Ocean (Argentina) PDF parser: helpers on synthetic data, plus a check against real PDFs when they are available.

    pytest tests/test_ocean_argentina.py -v

The vendor PDFs are not in the (public) repo; set OCEAN_PDF_DIR to a folder holding a few to run the real-file test.
"""

import glob
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import dimensions as dim
from fact_model import ocean_argentina as oa


def test_product_groups():
    g = oa.product_group
    assert [g(c) for c in ("MZ", "MZ FLINT", "SBS", "SBM", "WHT", "SORGHUM", "SBO", "NEUTRAL OIL")] == \
        ["Corn", "Corn", "Soybeans", "Soybean Meal", "Wheat", "Sorghum", "Soybean Oil", "Soybean Oil"]
    assert g("SFO") == "Other Oils" and g("SHP") == "Other" and g("BARLEY") == "Other" and g(None) == "Other"


def test_split_tail_product_and_tonnage_may_arrive_as_one_cell():
    assert oa._split_tail({9: "SOUTH KOREA", 10: "SBO 11,000"}) == ("SOUTH KOREA", "SBO", 11000.0)
    assert oa._split_tail({10: "MZ FLINT 24,000"}) == ("", "MZ FLINT", 24000.0)
    d, p, m = oa._split_tail({9: "VENEZUELA", 10: "SBS", 11: "0"})
    assert (d, p, m) == ("VENEZUELA", "SBS", 0.0)
    assert np.isnan(oa._split_tail({9: "PERU", 10: "SBM"})[2])


def test_report_date_and_berth_wording():
    assert oa.report_date("ARGENTINA PORTS\nJune 27, 2024\nNote: ...") == pd.Timestamp("2024-06-27")
    assert oa._fix_berth("RENOVA NORTE") == "RENOVA NORTH" and oa._fix_berth("COFCO PGSM SUR") == "COFCO PGSM SOUTH"


def _parsed(lineup_rows, expected, sailed_rows=(), expected_sailed=None):
    cols = ["section", "port", "berth", "vessel", "charterer", "status", "eta", "etb", "etc", "destination", "product", "mt"]
    mk = lambda sec, rows: pd.DataFrame([[sec, "SAN LORENZO", "RENOVA NORTH", v, "X", "ETA", pd.NaT, pd.NaT, pd.Timestamp(e),
                                          d, p, m] for v, e, d, p, m in rows], columns=cols)
    return {"report_date": pd.Timestamp("2026-06-19"), "lineup": mk("lineup", lineup_rows), "sailed": mk("sailed", sailed_rows),
            "expected": expected, "expected_sailed": expected_sailed or {}}


def test_validation_ties_the_parse_to_the_reports_own_totals():
    rows = [("A", "2026-06-20", "CHINA", "MZ", 30000.0), ("B", "2026-06-21", "INDIA", "SBM", 40000.0)]
    assert oa.validate(_parsed(rows, {"MZ": 30000.0, "SBM": 40500.0}))[0]                    # within 1%
    ok, bad = oa.validate(_parsed(rows, {"MZ": 30000.0, "SBM": 90000.0}))
    assert not ok and bad["SBM"] == (90000.0, 40000.0)


def test_a_partial_sailed_list_is_not_loaded_as_the_whole_month():
    sailed = [("A", "2026-06-02", "CHINA", "MZ", 60000.0)]
    assert oa.sailed_complete(_parsed([], {}, sailed, {"MZ": 61000.0}))[0]
    done, short = oa.sailed_complete(_parsed([], {}, sailed, {"MZ": 1770117.0}))
    assert not done and short["MZ"] == (1770117.0, 60000.0)
    assert not oa.sailed_complete(_parsed([], {}, sailed, {}))[0]                              # no totals table -> do not trust


def test_snapshot_and_events_from_parsed_rows(tmp_path):
    lineup = [("A", "2026-06-20", "CHINA", "MZ", 30000.0), ("A", "2026-06-20", "CHINA", "SBM", 10000.0),
              ("B", "2026-06-21", "INDIA", "SBO", 20000.0)]
    sailed = [("C", "2026-06-02", "PERU", "MZ", 45000.0)]
    p = _parsed(lineup, {"MZ": 30000.0, "SBM": 10000.0, "SBO": 20000.0}, sailed, {"MZ": 45000.0})
    snap = oa.lineup_snapshot(p)
    assert snap["vessels"].sum() == pytest.approx(2.0) and snap["mapped"].all() and (snap["region"] == "Argentina Up-River").all()
    assert set(snap["commodity"]) == {"Corn", "Soybean Meal", "Soybean Oil"}
    e = oa.sail_events(p)
    assert len(e) == 1 and e["sail_date"].iloc[0] == pd.Timestamp("2026-06-02") and e["event_id"].iloc[0].startswith("AO")
    db = str(tmp_path / "f.db")
    r = oa.ingest_parsed(p, db)
    assert r["lineup_ok"] and r["sailed_complete"] and r["sail_events"] == 1 and r["queued_vessels"] == pytest.approx(2.0)


def test_ports_seen_in_the_ocean_reports_are_all_mapped():
    cfg = dim.default_config()
    for name in ("SAN LORENZO", "ROSARIO", "RAMALLO", "ZARATE", "NECOCHEA", "BAHIA BLANCA", "CONCEPCION DEL URUGUAY",
                 "VILLA CONSTITUCION", "SAN PEDRO", "CAMPANA", "SAN NICOLAS", "DEL GUAZU", "LIMA"):
        assert dim.resolve_port(name, cfg)[2], name


@pytest.mark.skipif(not os.environ.get("OCEAN_PDF_DIR"), reason="set OCEAN_PDF_DIR to a folder with Ocean line-up PDFs")
def test_real_pdfs_tie_out():
    files = sorted(glob.glob(os.path.join(os.environ["OCEAN_PDF_DIR"], "*.pdf")))
    assert files
    for f in files[:6]:
        p = oa.parse_pdf(f)
        assert len(p["lineup"]) > 50
        assert oa.validate(p)[0], (f, oa.validate(p)[1])
