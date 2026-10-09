"""Step-1 tests: region mapping, snapshot vs cumulative math, MTD/MYTD differencing.

    pytest tests/test_fact_model.py -v
"""

import os
import shutil
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import dimensions as dim
from fact_model import executed as ex
from fact_model import lineup as lu

KEY = {"source": "US", "region": "Texas Gulf", "port": "Houston",
       "elevator": "TEMCO HOUSTON DOCK 1", "commodity": "Corn"}


# ── helpers ──────────────────────────────────────────────────────────────────

def truth_daily(start, end):
    """Deterministic shipped-that-day series, so any window sum is known exactly."""
    days = pd.date_range(start, end, freq="D")
    return pd.Series([(d.dayofyear % 7) + 1.0 for d in days], index=days)


def ytd_snapshots(truth, snap_dates, key=KEY):
    """What the report would show: calendar-year-to-date cumulative on each snapshot date."""
    rows = []
    for d in pd.to_datetime(snap_dates):
        cum = truth[(truth.index >= pd.Timestamp(d.year, 1, 1)) & (truth.index <= d)].sum()
        rows.append({"report_date": d, **key, "cum_raw_kmt": cum})
    return pd.DataFrame(rows)


def window(truth, a, b):
    return truth[(truth.index >= pd.Timestamp(a)) & (truth.index <= pd.Timestamp(b))].sum()


@pytest.fixture(scope="module")
def truth():
    return truth_daily("2026-01-01", "2027-03-31")


@pytest.fixture(scope="module")
def month_ends(truth):
    return list(pd.date_range("2026-01-31", "2027-02-28", freq="ME"))


# ── MTD / MYTD by differencing ───────────────────────────────────────────────

def test_mtd_at_month_end_equals_true_month_total(truth, month_ends):
    out = ex.derive_executed(ytd_snapshots(truth, month_ends)).set_index("report_date")
    for me in month_ends:
        assert out.loc[me, "mtd_kmt"] == pytest.approx(window(truth, me.replace(day=1), me)), me


def test_mytd_at_my_end_and_across_year_boundary(truth, month_ends):
    out = ex.derive_executed(ytd_snapshots(truth, month_ends)).set_index("report_date")
    # Corn MY 2026/27 starts Sep 1. Dec 31 is inside the calendar year; Jan 31 2027 crosses it.
    assert out.loc["2026-12-31", "mytd_kmt"] == pytest.approx(window(truth, "2026-09-01", "2026-12-31"))
    assert out.loc["2027-01-31", "mytd_kmt"] == pytest.approx(window(truth, "2026-09-01", "2027-01-31"))
    assert out.loc["2027-02-28", "mytd_kmt"] == pytest.approx(window(truth, "2026-09-01", "2027-02-28"))
    # MY-end of the PRIOR year (Aug 31 2026): full 2025/26 MY not derivable (series starts Jan 2026) -> NaN, not a guess
    assert np.isnan(out.loc["2026-08-31", "mytd_kmt"])
    assert "no_mytd_baseline" in out.loc["2026-08-31", "quality"]


def test_monthly_totals_sum_to_mytd(truth, month_ends):
    out = ex.derive_executed(ytd_snapshots(truth, month_ends))
    m = ex.monthly_executed(out).set_index("month_start")["mtd_kmt"]
    my = out.set_index("report_date").loc["2027-02-28", "mytd_kmt"]
    assert m.loc["2026-09-01":"2027-02-01"].sum() == pytest.approx(my)


def test_mid_month_snapshot_uses_last_month_end_baseline(truth, month_ends):
    snaps = month_ends + [pd.Timestamp("2026-10-19")]
    out = ex.derive_executed(ytd_snapshots(truth, snaps)).set_index("report_date")
    assert out.loc["2026-10-19", "mtd_kmt"] == pytest.approx(window(truth, "2026-10-01", "2026-10-19"))
    assert out.loc["2026-10-19", "mytd_kmt"] == pytest.approx(window(truth, "2026-09-01", "2026-10-19"))
    assert out.loc["2026-10-19", "quality"] == ""


# ── cumulative is never summed ───────────────────────────────────────────────

def test_rollup_reads_latest_snapshot_never_sums_across_snapshots(truth):
    snaps = ["2026-10-05", "2026-10-12", "2026-10-19"]
    raw = pd.concat([ytd_snapshots(truth, snaps),
                     ytd_snapshots(truth, snaps, {**KEY, "port": "Galveston", "elevator": "ADM GALVESTON"})])
    out = ex.derive_executed(raw)
    tot = ex.rollup_executed(out, by=["commodity"])
    expect = window(truth, "2026-01-01", "2026-10-19") * 2          # two ports, latest snapshot each
    wrong = sum(ytd_snapshots(truth, snaps)["cum_raw_kmt"]) * 2      # what summing across snapshots would give
    assert tot["cum_raw_kmt"].iloc[0] == pytest.approx(expect)
    assert tot["cum_raw_kmt"].iloc[0] != pytest.approx(wrong)


def test_rollup_asof_picks_each_keys_own_latest_snapshot(truth):
    a = ytd_snapshots(truth, ["2026-10-05", "2026-10-19"])
    b = ytd_snapshots(truth, ["2026-10-05"], {**KEY, "port": "Galveston", "elevator": "ADM GALVESTON"})
    out = ex.derive_executed(pd.concat([a, b]))
    tot = ex.rollup_executed(out, by=["commodity"])["cum_raw_kmt"].iloc[0]
    assert tot == pytest.approx(window(truth, "2026-01-01", "2026-10-19") + window(truth, "2026-01-01", "2026-10-05"))


def test_duplicate_cumulative_snapshot_is_rejected(truth):
    raw = ytd_snapshots(truth, ["2026-10-05", "2026-10-05"])
    with pytest.raises(ValueError, match="summing"):
        ex.derive_executed(raw)


# ── uneven spacing / missing days ────────────────────────────────────────────

def test_uneven_spacing_flags_stale_baseline_and_does_not_crash(truth):
    snaps = ["2026-02-05", "2026-02-20", "2026-03-05", "2026-03-20", "2026-04-05"]   # no month-ends
    out = ex.derive_executed(ytd_snapshots(truth, snaps)).set_index("report_date")
    row = out.loc["2026-03-20"]
    assert row["mtd_baseline_date"] == pd.Timestamp("2026-02-20")
    assert row["mtd_baseline_gap_days"] == 8
    assert "stale_mtd_baseline" in row["quality"]
    # the value is exactly cum difference (it includes Feb 21-28, which is why it is flagged)
    assert row["mtd_kmt"] == pytest.approx(window(truth, "2026-02-21", "2026-03-20"))


def test_uneven_schedule_shifts_gap_days_into_next_month_but_reconciles_exactly(truth):
    # Mon/Thu snapshots: the last prior-month snapshot is rarely month-end, so a month's MTD
    # includes/excludes a few gap days. Nothing is lost or double counted: monthly totals still
    # add up to MYTD exactly, and MTD is exact whenever the baseline IS month-end.
    snaps = [d for d in pd.date_range("2026-08-03", "2026-12-31") if d.dayofweek in (0, 3)]
    out = ex.derive_executed(ytd_snapshots(truth, snaps))
    monthly = ex.monthly_executed(out).set_index("month_start")["mtd_kmt"]
    last = out.sort_values("report_date").iloc[-1]
    assert monthly.loc["2026-09-01":"2026-12-01"].sum() == pytest.approx(last["mytd_kmt"])
    exact = out[out["mtd_baseline_gap_days"] == 0]
    for r in exact.itertuples():
        assert r.mtd_kmt == pytest.approx(window(truth, r.report_date.replace(day=1), r.report_date))


def test_first_snapshot_mid_month_has_no_mtd_baseline_but_january_does(truth):
    mar = ex.derive_executed(ytd_snapshots(truth, ["2026-03-15"])).iloc[0]
    assert np.isnan(mar["mtd_kmt"]) and "no_mtd_baseline" in mar["quality"]
    jan = ex.derive_executed(ytd_snapshots(truth, ["2026-01-20"])).iloc[0]
    assert jan["mtd_kmt"] == pytest.approx(window(truth, "2026-01-01", "2026-01-20"))   # cumulative is 0 on Jan 1
    assert "no_mtd_baseline" not in jan["quality"]


def test_revision_is_flagged_not_hidden(truth):
    raw = ytd_snapshots(truth, ["2026-04-30", "2026-05-31"])
    raw.loc[1, "cum_raw_kmt"] = raw.loc[0, "cum_raw_kmt"] - 5      # report restated downward
    out = ex.derive_executed(raw).set_index("report_date")
    assert "revision" in out.loc["2026-05-31", "quality"]


def test_lineup_trend_uses_real_snapshot_dates_no_resampling():
    dates = pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-06", "2026-10-13"])   # uneven, gaps
    fact = pd.DataFrame({"report_date": dates, "source": "US", "commodity": "Corn",
                         "region": "US Gulf", "kmt": [100.0, 120.0, 90.0, 150.0], "vessels": [2, 3, 2, 4]})
    t = lu.lineup_trend(fact, by=["commodity"])
    assert list(t["report_date"]) == list(dates)
    assert list(t["kmt"]) == [100.0, 120.0, 90.0, 150.0]          # snapshots as-is, never accumulated


def test_asof_snapshot_returns_one_snapshot_per_source():
    fact = pd.DataFrame({
        "report_date": pd.to_datetime(["2026-10-01", "2026-10-06", "2026-10-01", "2026-10-07"]),
        "source": ["US", "US", "Brazil", "Brazil"], "kmt": [10.0, 20.0, 1.0, 2.0]})
    cur = lu.asof_snapshot(fact)
    assert sorted(cur["kmt"]) == [2.0, 20.0]
    old = lu.asof_snapshot(fact, "2026-10-03")
    assert sorted(old["kmt"]) == [1.0, 10.0]


def test_blank_placeholder_rows_are_not_counted_as_queued_vessels():
    v = pd.DataFrame({"sheet": ["TXG", "TXG", "TXG"],
                      "elevator_raw": ["ANDERSONS HOUSTON", "ANDERSONS HOUSTON", "TEMCO HOUSTON DOCK 1"],
                      "vessel": [None, "REAL SHIP", None], "status_norm": ["Unknown", "ETA", "Unknown"],
                      "kmt": [np.nan, 55.0, np.nan], "commodity_raw": [None, "CORN", None],
                      "destination": [None, "JAPAN", None], "sail_date": pd.NaT})
    snap = lu.us_lineup_snapshot(v, "2026-10-07")
    assert snap["vessels"].sum() == 1 and snap["kmt"].sum() == 55.0


# ── unmapped ports are flagged, not dropped ──────────────────────────────────

def test_unmapped_us_elevators_are_kept_flagged_and_stay_in_their_sheet_region():
    v = pd.DataFrame({"sheet": ["USG", "USG", "PNW", "TXG"],
                      "elevator_raw": ["CHS", "MGMT", "COLUMBIA EXPORT", None],
                      "vessel": list("abcd"), "status_norm": "ETA", "kmt": [10.0, 20.0, 30.0, 40.0],
                      "commodity_raw": "CORN", "destination": "X", "sail_date": pd.NaT})
    snap = lu.us_lineup_snapshot(v, "2026-10-07")
    assert snap["kmt"].sum() == 100.0 and snap["vessels"].sum() == 4            # nothing dropped
    bad = snap[~snap["mapped"]]
    assert set(bad["elevator"]) == {"MGMT", "COLUMBIA EXPORT", dim.UNMAPPED}
    assert (bad["port"] == dim.UNMAPPED).all()                                  # the PORT is unknown ...
    assert dict(zip(bad["elevator"], bad["region"])) == {"MGMT": "US Gulf", "COLUMBIA EXPORT": "PNW", dim.UNMAPPED: "Texas Gulf"}   # ... the region is not
    rep = dim.unmapped_report(snap)
    assert rep["kmt"].sum() == 90.0
    assert snap[snap["mapped"]]["port"].tolist() == ["Plaquemines"]
    by_region = snap.groupby("region")["vessels"].sum().to_dict()
    assert by_region == {"US Gulf": 2, "PNW": 1, "Texas Gulf": 1}               # region totals include the unmapped ones


def test_unmapped_brazil_port_is_kept_and_flagged():
    df = pd.DataFrame({"report_date": "2026-10-07", "port": ["SANTOS", "NEWPORT"], "berth": ["TEG", "X"],
                       "vessel": ["a", "b"], "product": "MZ", "mt": [60000, 30000], "destination": "?",
                       "excluded": 0})
    snap = lu.brazil_lineup_snapshot(df)
    assert snap["kmt"].sum() == pytest.approx(90.0)
    new = snap[snap["port"] == "NEWPORT"].iloc[0]
    assert not new["mapped"] and new["region"] == dim.UNMAPPED
    assert snap[snap["port"] == "Santos"].iloc[0]["region"] == "Brazil South/Southeast"


@pytest.mark.parametrize("raw,port,region", [
    ("IMBITUBA", "Imbituba", "Brazil South/Southeast"),
    ("PRANAGUA", "Paranagua", "Brazil South/Southeast"),                       # source typo
    ("S�O FRANCISCO", "Sao Francisco", "Brazil South/Southeast"),   # parser drops the accented char
    ("S�O SEBASTI�O", "Sao Sebastiao", "Brazil South/Southeast"),
    ("TUBAR�O", "Tubarao", "Brazil South/Southeast"),
    ("SÃO FRANCISCO", "Sao Francisco", "Brazil South/Southeast"),        # proper accents also work
    ("itaqui", "Itaqui", "Brazil North Arc"),
    ("SANTAREM", "Santarem", "Brazil North Arc"),
])
def test_brazil_port_aliases(raw, port, region):
    p, r, ok = dim.resolve_port(raw)
    assert (p, r, ok) == (port, region, True)


def test_txg_dock_is_the_facility_and_missing_dock_is_flagged():
    e, p, r, ok, _ = dim.resolve_us_elevator("TXG", "TEMCO HOUSTON DOCK 1 / ADM CORPUS CHRISTI")
    assert (e, p, r, ok) == ("TEMCO HOUSTON DOCK 1", "Houston", "Texas Gulf", True)
    e, p, r, ok, why = dim.resolve_us_elevator("TXG", "nan / ADM CORPUS CHRISTI")
    assert not ok and p == dim.UNMAPPED and r == "Texas Gulf" and "no terminal" in why


def test_every_region_in_spec_exists_and_every_mapped_port_has_a_valid_region():
    cfg = dim.default_config()
    assert set(cfg.regions["region"]) == {
        "PNW", "US Gulf", "Texas Gulf", "Great Lakes", "Atlantic", "Interior / river elevators",
        "Brazil North Arc", "Brazil South/Southeast"}
    assert set(cfg.port_region["region"]) <= set(cfg.regions["region"])


def test_config_typo_in_region_fails_loudly(tmp_path):
    for f in os.listdir(dim.CONFIG_DIR):
        shutil.copy(os.path.join(dim.CONFIG_DIR, f), tmp_path / f)
    pr = (tmp_path / "port_region.csv").read_text().replace("Houston,Texas Gulf", "Houston,Texas Gulff")
    (tmp_path / "port_region.csv").write_text(pr)
    with pytest.raises(ValueError, match="not in regions.csv"):
        dim.load_config(str(tmp_path))


# ── marketing year / commodity ───────────────────────────────────────────────

@pytest.mark.parametrize("d,c,label", [
    ("2026-10-07", "Corn", "2026/27"), ("2026-08-31", "Corn", "2025/26"), ("2026-09-01", "Soybeans", "2026/27"),
    ("2026-06-01", "Wheat", "2026/27"), ("2026-05-31", "Wheat", "2025/26"),
    ("2026-09-30", "Soybeans", "2026/27"), ("2026-09-30", "Soybean Meal", "2025/26"),      # meal year starts Oct 1
    ("2026-10-01", "Soybean Meal", "2026/27"), ("2026-07-31", "Rice", "2025/26"), ("2026-08-01", "Rice", "2026/27"),
    ("2026-10-07", "Other", "2026")])
def test_marketing_year_labels(d, c, label):
    assert dim.marketing_year_label(d, c) == label


def test_marketing_years_are_the_usda_fas_ones():
    cfg = dim.default_config()
    got = {c: cfg._my_lookup[c] for c in ("Corn", "Soybeans", "Soybean Meal", "Wheat", "Sorghum", "Rice")}
    assert got == {"Corn": (9, 1), "Soybeans": (9, 1), "Soybean Meal": (10, 1), "Wheat": (6, 1),
                   "Sorghum": (9, 1), "Rice": (8, 1)}
    assert set(cfg.marketing_year["commodity"]) >= {"Mixed Cargo", "Other", "Dist. Grains", "Sugar"}


def test_commodity_groups_cover_us_and_brazil_products():
    assert dim.commodity_group("US", "CORN/SBM/WHT") == "Mixed Cargo"
    assert dim.commodity_group("US", "YSB") == "Soybeans"
    assert dim.commodity_group("US", "SBM") == "Soybean Meal"
    assert dim.commodity_group("US", "CANOLA") == "Other"
    for p, g in [("SBS", "Soybeans"), ("SONS SBS", "Soybeans"), ("MZ", "Corn"), ("SBMP", "Soybean Meal"),
                 ("HIPRO", "Soybean Meal"), ("SPC", "Soybean Meal"), ("RAW SUG", "Sugar"), ("DDGS", "Dist. Grains"), ("MILL WHEAT", "Wheat")]:
        assert dim.commodity_group("Brazil", p) == g


# ── store keeps every snapshot ───────────────────────────────────────────────

def test_store_appends_snapshots_and_replaces_only_the_same_date(tmp_path):
    from fact_model import store
    db = str(tmp_path / "f.db")
    def snap(day, kmt):
        v = pd.DataFrame({"sheet": ["USG"], "elevator_raw": ["CHS"], "vessel": ["a"], "status_norm": "ETA",
                          "kmt": [kmt], "commodity_raw": "CORN", "destination": "X", "sail_date": pd.NaT})
        return lu.us_lineup_snapshot(v, day)
    store.save_lineup_snapshot(snap("2026-10-01", 10.0), db)
    store.save_lineup_snapshot(snap("2026-10-03", 20.0), db)
    store.save_lineup_snapshot(snap("2026-10-03", 25.0), db)       # re-run of the same snapshot
    got = store.load_lineup(db).sort_values("report_date")
    assert list(got["kmt"]) == [10.0, 25.0]
