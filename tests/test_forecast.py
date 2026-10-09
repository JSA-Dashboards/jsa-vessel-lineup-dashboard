"""Forecast engine tests: pace, seasonality, blend, range, region rollup, extension points.

    pytest tests/test_forecast.py -v
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import adjustments as adj
from fact_model import forecast as fc

FCFG = {**fc.load_forecast_config(), "history_start_US": "2024-01-01", "history_start_Brazil": "2024-01-01"}


def ev(rows, source="US", region="US Gulf", port="South Louisiana", commodity="Corn"):
    """rows: [(date, kmt)] -> events frame with the columns the engine reads."""
    return pd.DataFrame([{"source": source, "region": region, "port": port, "commodity": commodity,
                          "sail_date": pd.Timestamp(d), "kmt": k} for d, k in rows])


def run(events, snaps=None, w=None, mode="fixed", power=None):
    return fc.run_forecast(events, snaps, weight=w, mode=mode, power=power, fcfg=FCFG)


# ── Pace uses the actual elapsed report days ─────────────────────────────────

def test_pace_elapsed_days_run_to_the_last_sail_date_for_us():
    out = run(ev([("2026-01-03", 10.0), ("2026-01-08", 20.0)])).iloc[0]
    assert out["data_through"] == pd.Timestamp("2026-01-08")
    assert out["days_elapsed"] == 8 and out["days_remaining"] == 23 and out["days_in_month"] == 31
    assert out["mtd_kmt"] == 30.0
    assert out["pace_kmt"] == pytest.approx(30.0 / 8 * 31)


def test_pace_elapsed_days_run_to_the_latest_report_for_brazil_even_if_last_sailing_was_earlier():
    e = ev([("2026-01-03", 10.0), ("2026-01-09", 20.0)], source="Brazil", region="Brazil South/Southeast",
           port="Santos", commodity="Corn")
    snaps = pd.DataFrame({"source": ["Brazil", "Brazil"], "report_date": pd.to_datetime(["2026-01-09", "2026-01-12"])})
    out = run(e, snaps).iloc[0]
    assert out["data_through"] == pd.Timestamp("2026-01-12") and out["days_elapsed"] == 12
    assert out["pace_kmt"] == pytest.approx(30.0 / 12 * 31)          # not 9 days, not today's calendar day


def test_pace_does_not_depend_on_todays_date(monkeypatch):
    e = ev([("2026-02-04", 40.0)])
    a = run(e).iloc[0]["pace_kmt"]
    monkeypatch.setattr(pd.Timestamp, "now", classmethod(lambda cls, *a, **k: pd.Timestamp("2031-07-19")))
    assert run(e).iloc[0]["pace_kmt"] == a == pytest.approx(40.0 / 4 * 28)


def test_missing_days_are_zero_filled_and_events_after_data_through_are_ignored():
    snaps = pd.DataFrame({"source": ["Brazil"], "report_date": pd.to_datetime(["2026-03-10"])})
    e = ev([("2026-03-02", 10.0), ("2026-03-04", 5.0), ("2026-03-25", 999.0)], source="Brazil",
           region="Brazil South/Southeast", port="Santos")
    out = run(e, snaps).iloc[0]
    assert out["days_elapsed"] == 10 and out["mtd_kmt"] == 15.0             # days 1, 3, 5-10 had no sailings: still elapsed
    assert out["pace_kmt"] == pytest.approx(15.0 / 10 * 31)


# ── Seasonality ──────────────────────────────────────────────────────────────

SEAS = [("2024-09-10", 20.0), ("2024-10-10", 100.0), ("2025-09-10", 40.0), ("2025-10-10", 200.0),
        ("2026-09-10", 60.0), ("2026-10-05", 10.0)]


def test_seasonality_is_prior_year_mean_scaled_by_current_marketing_year_progress():
    out = run(ev(SEAS), w=0.5).iloc[0]
    assert out["seasonal_base_kmt"] == pytest.approx(150.0)                  # mean of Oct 2024 (100) and Oct 2025 (200)
    assert out["seasonal_years_used"] == 2
    assert out["seasonal_scale"] == pytest.approx(2.0)                      # Sep-26 60 vs mean(Sep-25 40, Sep-24 20) = 30
    assert out["seasonal_kmt"] == pytest.approx(300.0)
    assert out["pace_kmt"] == pytest.approx(10.0 / 5 * 31)                  # 62
    assert out["blend_kmt"] == pytest.approx(0.5 * 300 + 0.5 * 62)          # 181 (fixed 0.5)
    assert out["weight_seasonal"] == 0.5


def test_fixed_blend_weight_endpoints():
    pace_only, seas_only = run(ev(SEAS), w=0.0).iloc[0], run(ev(SEAS), w=1.0).iloc[0]
    assert pace_only["blend_kmt"] == pytest.approx(pace_only["pace_kmt"])
    assert seas_only["blend_kmt"] == pytest.approx(seas_only["seasonal_kmt"])
    assert run(ev(SEAS), w=0.5).iloc[0]["blend_kmt"] == pytest.approx(181.0)


def test_configured_defaults_are_the_elapsed_weighted_blend():
    cfg = fc.load_forecast_config()
    assert cfg["weight_mode"] == "elapsed" and float(cfg["blend_weight_seasonality"]) == 0.75 and float(cfg["weight_decay_power"]) == 2.0
    out = fc.run_forecast(ev(SEAS), fcfg=FCFG).iloc[0]                       # no explicit weight or mode: the config decides
    w = 0.75 * (1 - 5 / 31) ** 2
    assert out["weight_mode"] == "elapsed" and out["weight_seasonal"] == pytest.approx(w)
    assert out["blend_kmt"] == pytest.approx(w * 300.0 + (1 - w) * 62.0)


def test_blend_weight_function():
    assert fc.blend_weight(10, 30, "fixed", 0.6, 2.0) == 0.6
    assert fc.blend_weight(0, 30, "elapsed", 0.75, 2.0) == pytest.approx(0.75)           # start of month: full w0
    assert fc.blend_weight(15, 30, "elapsed", 0.8, 1.0) == pytest.approx(0.4)
    assert fc.blend_weight(30, 30, "elapsed", 0.75, 2.0) == 0.0                         # complete month: Pace only
    ws = [fc.blend_weight(e, 31, "elapsed", 0.75, 2.0) for e in range(1, 32)]
    assert all(a > b for a, b in zip(ws, ws[1:]))                                       # strictly falling through the month
    with pytest.raises(ValueError):
        fc.blend_weight(5, 31, "bogus", 0.5, 1.0)


def test_elapsed_mode_leans_on_seasonality_early_and_pace_late():
    early = [("2024-10-10", 100.0), ("2025-10-10", 100.0), ("2026-10-01", 1.0)]          # 1 day in, almost nothing shipped
    late = [("2024-10-10", 100.0), ("2025-10-10", 100.0), ("2026-10-25", 1.0)]           # 25 days in, almost nothing shipped
    e_row = run(ev(early), w=0.75, mode="elapsed").iloc[0]
    l_row = run(ev(late), w=0.75, mode="elapsed").iloc[0]
    assert e_row["weight_seasonal"] > 0.6 > l_row["weight_seasonal"] and l_row["weight_seasonal"] < 0.1
    assert e_row["blend_kmt"] > e_row["pace_kmt"]                                        # seasonal (100) pulls the tiny pace up
    assert abs(l_row["blend_kmt"] - l_row["pace_kmt"]) < 0.05 * l_row["seasonal_kmt"]    # late: far nearer Pace than Seasonality
    assert abs(e_row["blend_kmt"] - e_row["seasonal_kmt"]) < abs(e_row["blend_kmt"] - e_row["pace_kmt"])   # early: nearer Seasonality


def test_pace_only_rows_report_zero_seasonal_weight():
    out = run(ev([("2024-03-05", 10.0)]), w=0.75, mode="elapsed").iloc[0]
    assert out["weight_seasonal"] == 0.0 and "pace_only" in out["quality"]


def test_scale_is_clamped_and_first_month_of_marketing_year_is_unscaled():
    wild = [("2024-09-10", 1.0), ("2024-10-10", 100.0), ("2025-09-10", 1.0), ("2025-10-10", 100.0),
            ("2026-09-10", 500.0), ("2026-10-05", 10.0)]
    assert run(ev(wild)).iloc[0]["seasonal_scale"] == pytest.approx(4.0)     # 500 / 1 clamped to scale_max
    first = [("2024-09-10", 100.0), ("2025-09-10", 200.0), ("2026-09-05", 10.0)]    # forecast month = Sep = MY start
    out = run(ev(first)).iloc[0]
    assert out["seasonal_scale"] == 1.0 and "unscaled_seasonal" in out["quality"]


def test_no_prior_year_falls_back_to_pace_only():
    out = run(ev([("2024-03-05", 10.0)])).iloc[0]
    assert np.isnan(out["seasonal_kmt"]) and out["seasonal_years_used"] == 0
    assert out["blend_kmt"] == pytest.approx(out["pace_kmt"]) and "pace_only" in out["quality"]


def test_projection_never_falls_below_what_already_shipped():
    late = [("2024-10-10", 1.0), ("2025-10-10", 1.0), ("2026-10-30", 100.0)]    # 30 of 31 days elapsed, tiny seasonal
    out = run(ev(late), w=0.5).iloc[0]
    assert out["blend_kmt"] == out["mtd_kmt"] == 100.0 and "floored_at_mtd" in out["quality"]


def test_complete_month_is_the_actual_with_no_range():
    out = run(ev([("2026-01-03", 10.0), ("2026-01-31", 20.0)])).iloc[0]
    assert out["days_remaining"] == 0 and "complete_month" in out["quality"]
    assert out["blend_kmt"] == out["low_kmt"] == out["high_kmt"] == 30.0


# ── Range from backtest ──────────────────────────────────────────────────────

def steady(port, months=36, per_month=100.0):
    rows = []
    for m in pd.date_range("2024-01-01", periods=months, freq="MS"):
        rows += [(m + pd.Timedelta(days=0), per_month / 2), (m + pd.Timedelta(days=19), per_month / 2)]
    return ev(rows, port=port)


def test_range_comes_from_backtest_at_port_level_then_pools_by_commodity_when_sparse():
    rich = steady("South Louisiana")                                        # 36 months of history
    sparse = ev([("2026-08-03", 40.0), ("2026-09-03", 40.0)], port="Baton Rouge")
    out = run(pd.concat([rich, sparse])).set_index("port")
    r = out.loc["South Louisiana"]
    assert r["range_basis"] == "port" and r["range_n"] >= 6
    assert r["low_kmt"] <= r["blend_kmt"] <= r["high_kmt"] or r["low_kmt"] >= r["mtd_kmt"]
    assert r["low_kmt"] >= r["mtd_kmt"]
    s = out.loc["Baton Rouge"]
    assert s["range_basis"] == "pooled" and s["range_n"] > 12                 # too few months of its own


def test_range_is_none_when_there_is_not_enough_history_anywhere():
    out = run(ev([("2026-09-03", 40.0)])).iloc[0]
    assert out["range_basis"] == "none" and np.isnan(out["low_kmt"]) and np.isnan(out["high_kmt"])


def test_backtest_has_no_look_ahead():
    base = steady("South Louisiana", months=30)
    through = {"US": pd.Timestamp("2026-06-10")}
    a = fc._build_series(base, through, FCFG)[("US", "US Gulf", "South Louisiana", "Corn")]
    changed = pd.concat([base, ev([("2026-06-05", 5000.0)])])                    # extra shipment inside the CURRENT month
    b = fc._build_series(changed, through, FCFG)[("US", "US Gulf", "South Louisiana", "Corn")]
    m = pd.Timestamp("2026-06-01")
    from fact_model.dimensions import default_config
    spec = ("elapsed", 0.75, 2.0)
    ra = fc._backtest_ratios(a, m, 10, "Corn", spec, FCFG, default_config())
    rb = fc._backtest_ratios(b, m, 10, "Corn", spec, FCFG, default_config())
    assert ra == rb and len(ra) > 6


# ── Port projections sum to the region ───────────────────────────────────────

def test_sum_of_port_projections_equals_the_region_projection():
    e = pd.concat([steady("South Louisiana"), steady("New Orleans", per_month=60.0), steady("Plaquemines", per_month=30.0),
                   ev([("2026-09-04", 12.0)], port="Baton Rouge")])
    ports = run(e)
    region = fc.rollup_to_region(ports).set_index(["region", "commodity"]).loc[("US Gulf", "Corn")]
    sel = ports[(ports["region"] == "US Gulf") & (ports["commodity"] == "Corn")]
    for col in ("blend_kmt", "pace_kmt", "mtd_kmt", "low_kmt", "high_kmt"):
        assert region[col] == pytest.approx(sel[col].sum()), col
    assert region["ports"] == 4 and len(sel) == 4


def test_regions_are_never_forecast_directly():
    # one commodity, two ports with very different seasonal shapes: the region number must be the
    # sum of the ports' own projections, not a projection of the pooled region series
    a = ev([("2024-10-10", 100.0), ("2025-10-10", 100.0), ("2026-10-05", 50.0)], port="Portland", region="PNW")
    b = ev([("2024-10-10", 10.0), ("2025-10-10", 10.0), ("2026-10-05", 0.5)], port="Tacoma", region="PNW")
    ports = run(pd.concat([a, b]), w=0.5)
    reg = fc.rollup_to_region(ports).iloc[0]
    assert reg["blend_kmt"] == pytest.approx(ports["blend_kmt"].sum())


def test_export_table_is_clean():
    t = fc.export_table(run(ev(SEAS), w=0.5))
    assert list(t.columns[:8]) == ["source", "region", "port", "commodity", "forecast_month", "data_through",
                                   "projection_kmt", "low_kmt"]
    assert t["projection_kmt"].iloc[0] == 181.0 and "weight_mode" in t.columns


# ── Extension points ─────────────────────────────────────────────────────────

def test_with_no_layers_adjusted_equals_base():
    base = run(ev(SEAS))
    out = adj.apply_adjustments(base)
    assert (out["adjusted_kmt"] == out["blend_kmt"]).all() and (out["adjustments_applied"] == "").all()


def test_stub_layers_change_nothing_and_a_layer_cannot_edit_base_columns():
    base = run(ev(SEAS))
    out = adj.apply_adjustments(base, layers=[adj.lineup_fed_adjustment])
    assert (out["adjusted_kmt"] == out["blend_kmt"]).all() and out["adjustments_applied"].iloc[0] == "lineup_fed_adjustment"

    def bad_layer(df, **_):
        df = df.copy(); df["blend_kmt"] = df["blend_kmt"] * 2
        return df
    with pytest.raises(ValueError, match="base projection columns"):
        adj.apply_adjustments(base, layers=[bad_layer])


def test_lineup_model_readiness_reports_banked_snapshots():
    lf = pd.DataFrame({"source": ["US"] * 3 + ["Brazil"] * 70,
                       "report_date": list(pd.date_range("2026-01-01", periods=3)) + list(pd.date_range("2026-01-01", periods=70))})
    r = adj.lineup_model_readiness(lf).set_index("source")
    assert not r.loc["US", "ready"] and r.loc["Brazil", "ready"] and r.loc["US", "snapshots"] == 3


def test_elevator_disaggregation_preserves_port_totals_and_validates_shares():
    port = pd.DataFrame({"source": ["US"], "port": ["Houston"], "commodity": ["Corn"], "projection_kmt": [100.0]})
    shares = pd.DataFrame({"source": "US", "port": "Houston", "commodity": "Corn",
                           "elevator": ["TEMCO HOUSTON DOCK 1", "ANDERSONS HOUSTON"], "share": [0.7, 0.3]})
    out = adj.disaggregate_to_elevator(port, shares)
    assert out["projection_kmt"].sum() == pytest.approx(100.0) and set(out["elevator"]) == set(shares["elevator"])
    with pytest.raises(ValueError, match="do not sum to 1"):
        adj.disaggregate_to_elevator(port, shares.assign(share=[0.7, 0.2]))


def test_forecast_engine_is_isolated_from_the_adjustment_layers():
    import ast, inspect
    imported = {n.module or "" for n in ast.walk(ast.parse(inspect.getsource(fc))) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(ast.parse(inspect.getsource(fc))) if isinstance(n, ast.Import) for a in n.names}
    assert not any("adjustments" in m for m in imported)                       # base math cannot depend on a layer
    port = pd.DataFrame({"source": ["US"], "port": ["Houston"], "commodity": ["Corn"], "projection_kmt": [100.0]})
    before = port.copy()
    shares = pd.DataFrame({"source": "US", "port": "Houston", "commodity": "Corn", "elevator": ["A", "B"], "share": [0.5, 0.5]})
    adj.disaggregate_to_elevator(port, shares)
    assert port.equals(before)                                                  # port-level numbers untouched


def test_freight_spread_layer_is_an_interface_only():
    with pytest.raises(NotImplementedError, match="not built"):
        adj.reallocate_by_spread(pd.DataFrame())
    with pytest.raises(NotImplementedError):
        adj.spread_volume_backtest(None, None)
    assert "CAUSAL" in adj.reallocate_by_spread.__doc__ and "backtest" in adj.reallocate_by_spread.__doc__


# ── combo allocation of the projection ───────────────────────────────────────

def test_projection_allocation_conserves_totals_and_uses_the_ports_trailing_combo_mix():
    rows = []
    for m in pd.date_range("2024-01-01", periods=33, freq="MS"):                       # steady single-commodity corn and a combo series
        rows += [(m + pd.Timedelta(days=2), 50.0), (m + pd.Timedelta(days=21), 50.0)]
    corn = ev(rows)
    combo = ev(rows, commodity="Mixed Cargo").assign(combo="CORN/SBM")
    events = pd.concat([corn, combo], ignore_index=True)
    fc_ = run(events, w=0.5)
    out = fc.allocate_combo_projection(fc_, events)
    for col in ("mtd_kmt", "pace_kmt", "blend_kmt", "vessels_mtd"):
        assert out[col].sum() == pytest.approx(fc_[col].sum()), col
    assert set(out["commodity"]) == {"Corn", "Soybean Meal"}                            # Mixed Cargo is gone, split two ways
    mixed = fc_[fc_["commodity"] == "Mixed Cargo"].iloc[0]
    corn_row = out[out["commodity"] == "Corn"].iloc[0]
    base_corn = fc_[fc_["commodity"] == "Corn"].iloc[0]
    assert corn_row["blend_kmt"] == pytest.approx(base_corn["blend_kmt"] + mixed["blend_kmt"] * 2 / 3)
    assert out[out["commodity"] == "Soybean Meal"].iloc[0]["blend_kmt"] == pytest.approx(mixed["blend_kmt"] / 3)
    assert "incl_combo_share" in out[out["commodity"] == "Soybean Meal"].iloc[0]["quality"]


def test_a_port_with_no_combo_history_keeps_its_mixed_cargo_row():
    events = ev([("2026-09-03", 40.0)], commodity="Mixed Cargo").assign(combo="")        # no usable combo string anywhere
    fc_ = run(events)
    out = fc.allocate_combo_projection(fc_, events)
    assert list(out["commodity"]) == ["Mixed Cargo"] and out["blend_kmt"].sum() == pytest.approx(fc_["blend_kmt"].sum())
