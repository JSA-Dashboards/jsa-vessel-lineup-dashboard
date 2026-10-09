"""Executed-from-sail-events tests (additive math) + FGIS reconciliation.

    pytest tests/test_events.py -v
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import events as ev
from fact_model import executed as ex
from fact_model import fgis, store
from fact_model import lineup as lu


def us_vessels(rows):
    base = {"sheet": "USG", "elevator_raw": "CHS", "vessel": "V", "status_norm": "Sailed", "kmt": 50.0,
            "commodity_raw": "CORN", "destination": "JAPAN", "sail_date": pd.NaT}
    return pd.DataFrame([{**base, **r} for r in rows])


def mk_events(rows):
    base = {"source": "US", "region": "US Gulf", "port": "Plaquemines", "fgis_port": "MISSISSIPPI R.",
            "elevator": "CHS", "commodity": "Corn", "combo": "", "destination": "JAPAN", "vessel": "V", "kmt": 50.0,
            "mapped": True, "unmapped_reason": ""}
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["event_id"] = [f"e{i}" for i in range(len(df))]
    df["sail_date"] = pd.to_datetime(df["sail_date"])
    return df[ev.EVENT_COLS]


# ── windows ──────────────────────────────────────────────────────────────────

def test_mtd_mytd_ytd_windows_per_commodity_marketing_year():
    e = mk_events([
        {"sail_date": "2026-10-02", "kmt": 10.0},                                   # corn: MTD, MYTD, YTD
        {"sail_date": "2026-09-15", "kmt": 20.0},                                   # corn: MYTD, YTD
        {"sail_date": "2026-08-31", "kmt": 40.0},                                   # corn: before MY start -> YTD only
        {"sail_date": "2026-06-10", "kmt": 5.0, "commodity": "Wheat"},              # wheat MY starts Jun 1 -> MYTD
        {"sail_date": "2026-05-30", "kmt": 7.0, "commodity": "Wheat"},              # before wheat MY
        {"sail_date": "2026-10-20", "kmt": 99.0},                                   # after as_of: excluded
    ])
    out = ev.events_executed(e, "2026-10-07", by=["commodity"]).set_index("commodity")
    assert out.loc["Corn", ["mtd_kmt", "mytd_kmt", "ytd_kmt"]].tolist() == [10.0, 30.0, 70.0]
    assert out.loc["Wheat", ["mtd_kmt", "mytd_kmt", "ytd_kmt"]].tolist() == [0.0, 5.0, 12.0]


def test_per_source_as_of_dates():
    e = mk_events([{"sail_date": "2026-09-25", "kmt": 10.0}, {"sail_date": "2026-10-05", "kmt": 4.0, "source": "Brazil"},
                   {"sail_date": "2026-10-05", "kmt": 8.0, "source": "US"}])
    out = ev.events_executed(e, {"US": "2026-09-30", "Brazil": "2026-10-07"}, by=["source"]).set_index("source")
    assert out.loc["US", "mtd_kmt"] == 10.0          # US as-of is Sep 30: the Oct 5 sailing is not yet known
    assert out.loc["Brazil", "mtd_kmt"] == 4.0


def test_missing_tonnage_is_nan_not_zero_and_shows_in_coverage():
    e = mk_events([{"sail_date": "2026-10-01", "kmt": 50.0}, {"sail_date": "2026-10-02", "kmt": np.nan},
                   {"sail_date": "2026-10-03", "kmt": np.nan}])
    out = ev.events_executed(e, "2026-10-07", by=["commodity"]).iloc[0]
    assert out["mtd_kmt"] == 50.0 and out["vessels_mtd"] == 3 and out["no_tonnage_mytd"] == 2
    assert out["coverage_mytd"] == pytest.approx(1 / 3)


def test_event_math_agrees_with_cumulative_math_at_month_end():
    # Same shipments reached two ways must give the same MTD/MYTD when the baseline is month-end.
    days = pd.date_range("2026-01-01", "2026-12-31")
    e = mk_events([{"sail_date": d, "kmt": float((d.dayofyear % 5) + 1)} for d in days if d.dayofyear % 3 == 0])
    daily = e.groupby("sail_date")["kmt"].sum().reindex(days, fill_value=0.0)
    cum = daily.cumsum()
    snaps = [d for d in days if d.is_month_end]
    key = {"source": "US", "region": "US Gulf", "port": "Plaquemines", "elevator": "CHS", "commodity": "Corn"}
    raw = pd.DataFrame([{**key, "report_date": d, "cum_raw_kmt": cum[d]} for d in snaps])
    cumulative = ex.derive_executed(raw).set_index("report_date")
    for d in snaps[8:]:                                    # Sep onward: MYTD has a baseline
        by_events = ev.events_executed(e, d, by=["commodity"]).iloc[0]
        assert cumulative.loc[d, "mtd_kmt"] == pytest.approx(by_events["mtd_kmt"])
        assert cumulative.loc[d, "mytd_kmt"] == pytest.approx(by_events["mytd_kmt"])


def test_monthly_sums_to_ytd():
    e = mk_events([{"sail_date": "2026-01-15", "kmt": 3.0}, {"sail_date": "2026-01-20", "kmt": 4.0},
                   {"sail_date": "2026-03-02", "kmt": 5.0}])
    m = ev.events_monthly(e, by=["commodity"])
    assert m.set_index("month")["kmt"].to_dict() == {pd.Timestamp("2026-01-01"): 7.0, pd.Timestamp("2026-03-01"): 5.0}
    assert m["kmt"].sum() == ev.events_executed(e, "2026-12-31", by=["commodity"])["ytd_kmt"].iloc[0]


# ── US event construction ────────────────────────────────────────────────────

def test_us_events_drop_junk_dates_keep_duplicates_and_flag_unmapped():
    v = us_vessels([
        {"sail_date": pd.Timestamp("2026-09-10"), "vessel": "A"},
        {"sail_date": pd.Timestamp("2026-09-10"), "vessel": "A"},                    # genuine repeat line: kept, distinct id
        {"sail_date": pd.Timestamp("1900-02-16"), "vessel": "JUNK"},                 # workbook date glitch
        {"sail_date": pd.Timestamp("2026-09-11"), "vessel": "B", "elevator_raw": "MGMT"},   # unmapped
        {"sail_date": pd.Timestamp("2026-09-12"), "vessel": "C", "kmt": np.nan},     # RVT
    ])
    e = ev.us_sail_events(v)
    assert len(e) == 4 and e["event_id"].is_unique
    assert "JUNK" not in set(e["vessel"])
    assert not e.loc[e["vessel"] == "B", "mapped"].iloc[0]
    assert e["kmt"].isna().sum() == 1
    assert set(ev.us_sail_events(v)["event_id"]) == set(e["event_id"])               # stable across runs


def test_us_event_identity_survives_southport_revisions():
    # Same sailing seen in two files: elevator pairing, commodity code and tonnage all changed.
    a = us_vessels([{"sheet": "TXG", "elevator_raw": "TEMCO HOUSTON DOCK 1 / ANDERSONS HOUSTON", "vessel": "AFRICAN FINFOOT",
                     "sail_date": pd.Timestamp("2026-02-23"), "kmt": np.nan, "commodity_raw": "RVT"}])
    b = us_vessels([{"sheet": "TXG", "elevator_raw": "TEMCO HOUSTON DOCK 1 / HANSEN MUELLER", "vessel": "AFRICAN FINFOOT",
                     "sail_date": pd.Timestamp("2026-02-23"), "kmt": 33.0, "commodity_raw": "WHT"}])
    ea, eb = ev.us_sail_events(a), ev.us_sail_events(b)
    assert ea["event_id"].iloc[0] == eb["event_id"].iloc[0]
    import tempfile
    db = os.path.join(tempfile.mkdtemp(), "f.db")
    store.save_events(ea, db); store.save_events(eb, db)                    # chronological: later file wins
    got = store.load_events(db)
    assert len(got) == 1 and got["kmt"].iloc[0] == 33.0 and got["commodity"].iloc[0] == "Wheat"


def sailed_rows(report_date, rows):
    base = {"report_date": report_date, "port": "SANTOS", "berth": "TEG", "vessel": "X", "product": "MZ",
            "mt": 60000, "etcs": "2026-10-01", "excluded": 0, "status": "SLD", "destination": "CHINA"}
    out = pd.DataFrame([{**base, **r} for r in rows])
    out["id"] = range(len(out))
    return out


def test_brazil_events_dedupe_across_snapshots_and_latest_wins():
    s1 = sailed_rows("2026-10-06", [{"vessel": "A", "mt": 60000}, {"vessel": "B", "mt": 30000}])
    s2 = sailed_rows("2026-10-07", [{"vessel": "A", "mt": 62000},                      # revised in the later snapshot
                                    {"vessel": "B", "mt": 30000}, {"vessel": "C", "mt": 20000, "etcs": "2026-10-07"}])
    e = ev.brazil_sail_events(pd.concat([s1, s2], ignore_index=True))
    assert len(e) == 3
    assert e.set_index("vessel")["kmt"].to_dict() == {"A": 62.0, "B": 30.0, "C": 20.0}
    assert e["sail_date"].max() == pd.Timestamp("2026-10-07")


def test_brazil_split_cargo_lines_are_separate_events_and_excluded_rows_ignored():
    s = sailed_rows("2026-10-07", [{"vessel": "A", "product": "SBS", "mt": 40000},
                                   {"vessel": "A", "product": "SBMP", "mt": 20000},
                                   {"vessel": "M", "product": "MAINTENANCE", "mt": 0, "excluded": 1}])
    e = ev.brazil_sail_events(s)
    assert len(e) == 2 and e["kmt"].sum() == pytest.approx(60.0) and e["vessel"].nunique() == 1


def test_event_store_upsert_is_idempotent_and_keeps_history(tmp_path):
    db = str(tmp_path / "f.db")
    a = mk_events([{"sail_date": "2026-09-01", "kmt": 10.0}, {"sail_date": "2026-09-02", "kmt": 20.0}])
    store.save_events(a, db)
    store.save_events(a, db)                                                            # re-run
    later = a.iloc[[1]].assign(kmt=25.0)                                                # revised; first event absent from new file
    store.save_events(later, db)
    got = store.load_events(db).set_index("event_id")["kmt"].to_dict()
    assert got == {"e0": 10.0, "e1": 25.0}


# ── FGIS reconciliation ──────────────────────────────────────────────────────

def test_fgis_reconciliation_diff_ratio_and_unmatched_commodities():
    e = mk_events([
        {"sail_date": "2026-09-05", "kmt": 100.0},
        {"sail_date": "2026-09-20", "kmt": 50.0},
        {"sail_date": "2026-09-21", "kmt": np.nan},
        {"sail_date": "2026-09-22", "kmt": 30.0, "commodity": "Mixed Cargo"},
        {"sail_date": "2026-09-23", "kmt": 99.0, "source": "Brazil", "fgis_port": ""},   # not part of FGIS
    ])
    f = pd.DataFrame({"month": [pd.Timestamp("2026-09-01")], "fgis_port": ["MISSISSIPPI R."],
                      "commodity": ["Corn"], "fgis_kmt": [200.0]})
    r = fgis.reconcile_monthly(e, f).set_index("commodity")
    assert r.loc["Corn", "ours_kmt"] == 150.0 and r.loc["Corn", "diff_kmt"] == -50.0
    assert r.loc["Corn", "ratio"] == pytest.approx(0.75) and r.loc["Corn", "ours_no_tonnage"] == 1
    assert np.isnan(r.loc["Mixed Cargo", "fgis_kmt"]) and r.loc["Mixed Cargo", "ours_kmt"] == 30.0
    assert len(r) == 2                                                                  # Brazil row excluded


def test_every_us_port_has_an_fgis_region():
    from fact_model import dimensions as dim
    cfg = dim.default_config()
    us_ports = cfg.port_region[cfg.port_region["country"] == "US"]["port"]
    assert all(p in cfg.fgis_by_port for p in us_ports)
