"""Extension points for the forecast: isolated adjustment layers on top of the base projection.

Contract: a layer is a function  layer(projection_df, **context) -> projection_df  that may ADD
columns and may change `adjusted_kmt`, but never edits the base columns (blend_kmt, pace_kmt,
seasonal_kmt, low_kmt, high_kmt) that forecast.run_forecast produced. apply_adjustments runs the
layers in order and records which ran. With no layers, adjusted_kmt == blend_kmt.

Nothing here is wired into the forecast tab or the exporter outlook. Status:

1. lineup_fed_adjustment   STUB. Intended to become the primary view.
2. disaggregate_to_elevator  Hook (small, working, unused by the engine).
3. reallocate_by_spread    STUB, interface only. See the warning on that function.
"""

import numpy as np
import pandas as pd

KEY = ["source", "region", "port", "commodity"]
BASE_COLS = ["blend_kmt", "pace_kmt", "seasonal_kmt", "low_kmt", "high_kmt"]


def apply_adjustments(base, layers=(), **context):
    """Run adjustment layers over a base projection. Base columns are checked unchanged after each layer."""
    out = base.copy()
    out["adjusted_kmt"] = out["blend_kmt"]
    out["adjustments_applied"] = ""
    for layer in layers:
        before = out[BASE_COLS].copy()
        out = layer(out, **context)
        if not out[BASE_COLS].equals(before):
            raise ValueError(f"{layer.__name__} modified base projection columns; layers may only change adjusted_kmt")
        out["adjustments_applied"] = (out["adjustments_applied"] + ";" + layer.__name__).str.strip(";")
    return out


# ── 1. Line-up-fed model ─────────────────────────────────────────────────────

MIN_SNAPSHOTS_FOR_LINEUP_MODEL = 60     # about three months of daily snapshots per source (judgement; revisit with data)


def lineup_model_readiness(lineup_fact, min_snapshots=MIN_SNAPSHOTS_FOR_LINEUP_MODEL):
    """Per source: how many line-up snapshots are banked vs what the model needs. The model must
    learn a queue -> sailed relationship (how much queued tonnage actually ships, and how fast), which
    needs enough snapshots spanning different queue sizes and seasons. Until then it must not adjust."""
    g = lineup_fact.groupby("source")["report_date"].agg(snapshots="nunique", first="min", last="max").reset_index()
    g["needed"] = min_snapshots
    g["ready"] = g["snapshots"] >= g["needed"]
    return g


def lineup_fed_adjustment(projection, lineup=None, **context):
    """STUB. Will take the base projection plus the current line-up and return an adjusted
    projection, using queued tonnage (esp. loading / waiting vessels at the port) as a leading
    indicator of executions in the days that remain.

    Planned shape: for each port x commodity, adjusted_kmt = mtd + remaining-days estimate, where the
    remaining estimate blends the base projection's remainder with queued tonnage x an empirical
    queue-to-sailed conversion (by status and by days-to-ETB) learned from banked snapshots matched to
    later sail events. Must be backtested against the base projection before it is allowed to move a
    number; lineup_model_readiness says whether enough snapshots exist.

    Today it changes nothing: adjusted_kmt is left as the base blend."""
    return projection


# ── 2. Elevator market share ─────────────────────────────────────────────────

def disaggregate_to_elevator(port_values, shares, value_cols=("projection_kmt",), by=("source", "port", "commodity")):
    """Split port-level values (projected OR actual) to elevator level with a separately loaded
    share table. It only multiplies; it never feeds back into port-level forecast math.

    port_values  one row per (source, port, commodity) with the value columns.
    shares       columns source, port, elevator, commodity, share. Loaded independently of the
                 forecast. Join key = (source, port, commodity) -> elevator. Shares must sum to 1
                 within each key, otherwise this raises rather than quietly losing or inventing tonnage.
    Returns one row per elevator with value x share, so elevator totals add back to the port value."""
    by = list(by)
    sums = shares.groupby(by)["share"].sum()
    bad = sums[(sums - 1.0).abs() > 1e-6]
    if len(bad):
        raise ValueError(f"shares do not sum to 1 for {len(bad)} key(s), e.g. {bad.index[0]} -> {bad.iloc[0]:.4f}")
    m = shares.merge(port_values, on=by, how="inner")
    for c in value_cols:
        m[c] = m[c] * m["share"]
    return m.drop(columns="share")


# ── 3. Freight spreads ───────────────────────────────────────────────────────

def spread_volume_backtest(monthly_region_volumes, spreads, **context):
    """STUB. Diagnostic only: historical freight spread (e.g. Gulf vs PNW) against the later shift in
    regional share of volume, to see whether the relationship exists and how stable it is.
    Must exist and be reviewed before any forward adjustment is allowed."""
    raise NotImplementedError("freight-spread backtest not built")


def reallocate_by_spread(projection, spreads=None, **context):
    """STUB, interface only: would move projected tonnage between regions in response to freight
    spread changes, with total tonnage conserved.

    WARNING: this layer asserts a CAUSAL link (spread moves volume). Spreads and volumes are both driven by
    crop size, export demand and logistics, so a correlation in history does not justify adjusting a
    forward number. It must ship first as spread_volume_backtest (a diagnostic of historical spread vs
    volume shifts) and only adjust forecasts after that backtest shows a stable, out-of-sample effect."""
    raise NotImplementedError("freight-spread reallocation is not built; run spread_volume_backtest first")
