"""Month-end forecast of shipped tonnage, PORT x COMMODITY, from vessel sail events.

Regions are never forecast directly: every projection is made per port and summed up, so
per-port congestion differences survive (rollup_to_region).

Two views plus a blend, all for the month that contains the source's data-through date:
  Pace         MTD shipped / elapsed days x days in month. Elapsed days run to the source's actual
               data-through date (see data_through), not to today's calendar date.
  Seasonality  mean full-month total of the same calendar month in prior marketing years, scaled by
               how the current marketing year is running against those same years.
  Blend        w x Seasonality + (1 - w) x Pace, never below what has already shipped. Pace-only where
               no prior year exists. In 'elapsed' mode (default) the seasonal weight falls through the
               month, w = w0 x (1 - elapsed/days)^power: Pace is near-useless on day 2 and the best
               single method by day 20 (see backtest_methods). 'fixed' mode uses w0 throughout.

Range: low / high are the configured quantiles of actual / projected from backtesting the same
method on earlier months at the same day-of-month (port x commodity, pooled by commodity when a
port has too few months). Region ranges are sums of port ranges (assumes ports move together,
so they are conservative).

This is the base projection. Adjustment layers (line-up-fed, elevator share, freight spread)
slot in afterwards via adjustments.py without touching this module.
"""

import os

import numpy as np
import pandas as pd

from . import combos
from .dimensions import CONFIG_DIR, default_config, my_start

KEY = ["source", "region", "port", "commodity"]
OUT_COLS = ["source", "region", "port", "commodity", "forecast_month", "data_through", "days_in_month",
            "days_elapsed", "days_remaining", "mtd_kmt", "pace_kmt", "seasonal_base_kmt", "seasonal_scale",
            "seasonal_years_used", "seasonal_kmt", "weight_mode", "weight_seasonal", "blend_kmt", "low_kmt", "high_kmt",
            "range_basis", "range_n", "vessels_mtd", "tonnage_coverage", "quality"]
SUM_COLS = ["mtd_kmt", "pace_kmt", "seasonal_kmt", "blend_kmt", "low_kmt", "high_kmt", "vessels_mtd"]


def load_forecast_config(path=None):
    df = pd.read_csv(path or os.path.join(CONFIG_DIR, "forecast.csv"), dtype=str, keep_default_na=False)
    return dict(zip(df["key"], df["value"]))


def _f(fcfg, key):
    return float(fcfg[key])


def blend_weight(elapsed, dim, mode, w0, power):
    """Seasonal weight in the blend. 'fixed' -> w0. 'elapsed' -> w0 x (1 - elapsed/dim)^power, which
    is w0 at the start of the month and 0 once the month is complete."""
    if mode == "fixed":
        return float(w0)
    if mode == "elapsed":
        return float(w0) * max(0.0, 1.0 - elapsed / dim) ** float(power)
    raise ValueError(f"unknown weight mode {mode!r}")


def _spec(fcfg, weight=None, mode=None, power=None):
    """(mode, w0, power) from the config, overridden by explicit arguments."""
    return (mode or fcfg.get("weight_mode", "elapsed"),
            float(weight if weight is not None else _f(fcfg, "blend_weight_seasonality")),
            float(power if power is not None else _f(fcfg, "weight_decay_power")))


def data_through(events, snapshots=None, fcfg=None):
    """Per source: the last day the data actually covers. 'last_event' = latest sail date;
    'last_snapshot' = latest line-up report date (falls back to last_event if none)."""
    fcfg = fcfg or load_forecast_config()
    out = {}
    for src, g in events.groupby("source"):
        rule = fcfg.get(f"data_through_{src}", "last_event")
        if rule == "last_snapshot" and snapshots is not None and (snapshots["source"] == src).any():
            out[src] = pd.Timestamp(snapshots.loc[snapshots["source"] == src, "report_date"].max()).normalize()
        else:
            out[src] = pd.Timestamp(g["sail_date"].max()).normalize()
    return out


class _Series:
    """Daily tonnage for one key with O(1) window sums. Day 0 is the first day of the source's
    history window, always a month start so month boundaries align with the array."""

    def __init__(self, start, kmt, vessels, no_tonnage):
        self.start = start
        self.n = len(kmt)
        self.c_kmt = np.concatenate([[0.0], np.cumsum(kmt)])
        self.c_ves = np.concatenate([[0], np.cumsum(vessels)])
        self.c_nt = np.concatenate([[0], np.cumsum(no_tonnage)])

    def _pos(self, d):
        return int(min(max((pd.Timestamp(d) - self.start).days, 0), self.n))

    def window(self, lo, hi_excl, c=None):
        c = self.c_kmt if c is None else c
        return float(c[self._pos(hi_excl)] - c[self._pos(lo)])

    def month_total(self, ms):
        return self.window(ms, ms + pd.offsets.MonthBegin(1))


def _build_series(events, through, fcfg):
    """{(key tuple): _Series}, zero-filled over each source's history window to its data-through date."""
    e = events[events["sail_date"].notna()].copy()
    e["d"] = pd.to_datetime(e["sail_date"]).dt.normalize()
    out = {}
    for src, T in through.items():
        start = pd.Timestamp(fcfg.get(f"history_start_{src}", "1900-01-01")).to_period("M").to_timestamp()
        s = e[(e["source"] == src) & (e["d"] >= start) & (e["d"] <= T)]
        idx = pd.date_range(start, T, freq="D")
        for key, g in s.groupby(KEY[1:], dropna=False):
            by_d = g.groupby("d")
            kmt = by_d["kmt"].sum().reindex(idx, fill_value=0.0).to_numpy()
            ves = by_d.size().reindex(idx, fill_value=0).to_numpy()
            nt = by_d["kmt"].apply(lambda x: int(x.isna().sum())).reindex(idx, fill_value=0).to_numpy()
            out[(src, *key)] = _Series(start, kmt, ves, nt)
    return out


def _seasonal(ks, m_start, commodity, fcfg, cfg):
    """(base, scale, years_used, flags) or None when no prior year is available."""
    Y = int(_f(fcfg, "seasonal_years"))
    prior = [(k, m_start - pd.DateOffset(years=k)) for k in range(1, Y + 1)]
    prior = [(k, pm) for k, pm in prior if pm >= ks.start]
    if not prior:
        return None
    base = float(np.mean([ks.month_total(pm) for _, pm in prior]))
    mys = my_start(m_start, commodity, cfg)
    mys = pd.Timestamp(mys.year, mys.month, 1)
    scale, flags = 1.0, []
    if mys < m_start:
        num = ks.window(mys, m_start)
        dens = [ks.window(mys - pd.DateOffset(years=k), pm) for k, pm in prior
                if mys - pd.DateOffset(years=k) >= ks.start]
        den = float(np.mean(dens)) if dens else 0.0
        if den > 0:
            scale = float(np.clip(num / den, _f(fcfg, "scale_min"), _f(fcfg, "scale_max")))
        else:
            flags.append("unscaled_seasonal")
    else:
        flags.append("unscaled_seasonal")        # first month of the marketing year: no progress to scale by
    return base, scale, len(prior), flags


def _project(ks, m_start, elapsed, commodity, spec, fcfg, cfg):
    dim = m_start.days_in_month
    elapsed = int(min(elapsed, dim))
    mtd = ks.window(m_start, m_start + pd.Timedelta(days=elapsed))
    pace = mtd / elapsed * dim
    w = blend_weight(elapsed, dim, *spec)
    seas = _seasonal(ks, m_start, commodity, fcfg, cfg)
    if seas is None:
        blend, seasonal_kmt, flags, w = pace, np.nan, ["pace_only"], 0.0
    else:
        base, scale, _, flags = seas
        seasonal_kmt = base * scale
        blend = w * seasonal_kmt + (1 - w) * pace
        flags = list(flags)
    if blend < mtd:
        blend = mtd
        flags.append("floored_at_mtd")
    return {"mtd": mtd, "pace": pace, "blend": blend, "seasonal": seasonal_kmt,
            "seas": seas, "flags": flags, "dim": dim, "elapsed": elapsed, "w": w}


def _backtest_ratios(ks, m_start, elapsed, commodity, spec, fcfg, cfg):
    """actual / projected for every earlier month of this key, projected at the same day-of-month
    using only information that existed then (no look-ahead)."""
    ratios = []
    p = ks.start
    while p < m_start:
        pr = _project(ks, p, elapsed, commodity, spec, fcfg, cfg)
        actual = ks.month_total(p)
        if pr["blend"] > 0 and actual >= 0:
            ratios.append(actual / pr["blend"])
        p = p + pd.offsets.MonthBegin(1)
    return ratios


def run_forecast(events, snapshots=None, weight=None, mode=None, power=None, cfg=None, fcfg=None):
    """Port x commodity month-end projection for the month holding each source's data-through date."""
    cfg = cfg or default_config()
    fcfg = fcfg or load_forecast_config()
    spec = _spec(fcfg, weight, mode, power)
    through = data_through(events, snapshots, fcfg)
    series = _build_series(events, through, fcfg)
    lo_q, hi_q = _f(fcfg, "range_low_q"), _f(fcfg, "range_high_q")
    min_bt = int(_f(fcfg, "min_backtest_months"))

    staged, pooled = [], {}
    for key, ks in series.items():
        src = key[0]
        T = through[src]
        m_start = T.to_period("M").to_timestamp()
        elapsed = (T - m_start).days + 1
        commodity = key[3]
        pr = _project(ks, m_start, elapsed, commodity, spec, fcfg, cfg)
        ratios = _backtest_ratios(ks, m_start, pr["elapsed"], commodity, spec, fcfg, cfg)
        pooled.setdefault((src, commodity), []).extend(ratios)
        staged.append((key, ks, T, m_start, pr, ratios))

    rows = []
    for key, ks, T, m_start, pr, ratios in staged:
        src, region, port, commodity = key
        flags = list(pr["flags"])
        remaining = pr["dim"] - pr["elapsed"]
        if remaining == 0:
            flags.append("complete_month")
        if pr["elapsed"] < _f(fcfg, "min_elapsed_days") and remaining > 0:
            flags.append("early_month")
        basis, rr = "none", []
        if len(ratios) >= min_bt:
            basis, rr = "port", ratios
        elif len(pooled[(src, commodity)]) >= 2 * min_bt:
            basis, rr = "pooled", pooled[(src, commodity)]
        blend = pr["blend"]
        if remaining == 0:
            low = high = pr["mtd"]
        elif rr:
            ql, qh = np.quantile(rr, [lo_q, hi_q])
            low = max(blend * ql, pr["mtd"])
            high = max(blend * qh, low)
        else:
            low = high = np.nan
        ves = ks.window(m_start, m_start + pd.Timedelta(days=pr["elapsed"]), ks.c_ves)
        nt = ks.window(m_start, m_start + pd.Timedelta(days=pr["elapsed"]), ks.c_nt)
        seas = pr["seas"]
        rows.append({
            "source": src, "region": region, "port": port, "commodity": commodity,
            "forecast_month": m_start, "data_through": T, "days_in_month": pr["dim"],
            "days_elapsed": pr["elapsed"], "days_remaining": remaining,
            "mtd_kmt": pr["mtd"], "pace_kmt": pr["pace"],
            "seasonal_base_kmt": seas[0] if seas else np.nan, "seasonal_scale": seas[1] if seas else np.nan,
            "seasonal_years_used": seas[2] if seas else 0, "seasonal_kmt": pr["seasonal"],
            "weight_mode": spec[0], "weight_seasonal": pr["w"], "blend_kmt": blend, "low_kmt": low, "high_kmt": high,
            "range_basis": basis, "range_n": len(rr), "vessels_mtd": int(ves),
            "tonnage_coverage": (1 - nt / ves) if ves else np.nan, "quality": ";".join(dict.fromkeys(flags)),
        })
    out = pd.DataFrame(rows, columns=OUT_COLS)
    return out.sort_values(["source", "region", "port", "commodity"]).reset_index(drop=True)


def rollup_to_region(fc):
    """Region projection = SUM of its port projections (never forecast directly). Seasonal is
    summed over ports that have one; ports_with_seasonal says how many did."""
    g = fc.groupby(["source", "region", "commodity", "forecast_month", "data_through"], dropna=False)
    out = g[SUM_COLS].sum(min_count=1).reset_index()
    out["ports"] = g.size().to_numpy()
    out["ports_with_seasonal"] = g["seasonal_kmt"].count().to_numpy()
    out["days_elapsed"] = g["days_elapsed"].first().to_numpy()
    out["days_remaining"] = g["days_remaining"].first().to_numpy()
    return out


def export_table(fc):
    """The clean port x commodity table meant to feed the major-exporters outlook."""
    t = fc[["source", "region", "port", "commodity", "forecast_month", "data_through", "blend_kmt", "low_kmt",
            "high_kmt", "mtd_kmt", "pace_kmt", "seasonal_kmt", "weight_mode", "weight_seasonal", "days_elapsed",
            "days_remaining", "tonnage_coverage", "range_basis", "quality"]].copy()
    t = t.rename(columns={"blend_kmt": "projection_kmt"})
    for c in ("projection_kmt", "low_kmt", "high_kmt", "mtd_kmt", "pace_kmt", "seasonal_kmt"):
        t[c] = t[c].round(1)
    t["tonnage_coverage"] = t["tonnage_coverage"].round(3)
    return t


def backtest_methods(events, snapshots=None, elapsed=15, specs=None, min_actual_kmt=50.0, cfg=None, fcfg=None):
    """How well each blend would have done: for every earlier month, project at the same
    day-of-month per port, sum to region x commodity, compare with what shipped. `specs` is a list of
    (mode, w0, power); default = fixed 0 / 0.25 / 0.5 / 0.75 / 1 plus the configured elapsed schedule.
    Fixed 0 is Pace only, fixed 1 is Seasonality (Pace where no prior year existed). Returns the median
    absolute % error and bias (projected / actual - 1) per source x commodity, and the months used."""
    cfg = cfg or default_config()
    fcfg = fcfg or load_forecast_config()
    if specs is None:
        specs = [("fixed", w, 1.0) for w in (0.0, 0.25, 0.5, 0.75, 1.0)] + [_spec(fcfg, mode="elapsed")]
    labels = {sp: (f"fixed {sp[1]:g}" if sp[0] == "fixed" else f"elapsed {sp[1]:g}^{sp[2]:g}") for sp in specs}
    through = data_through(events, snapshots, fcfg)
    series = _build_series(events, through, fcfg)
    rec = {}
    for key, ks in series.items():
        src, region, port, commodity = key
        m_end = through[src].to_period("M").to_timestamp()
        p = ks.start + pd.DateOffset(years=1)          # first month with a prior year behind it
        while p < m_end:
            actual = ks.month_total(p)
            for i, sp in enumerate(specs):
                pr = _project(ks, p, elapsed, commodity, sp, fcfg, cfg)
                d = rec.setdefault((src, region, commodity, p, sp), [0.0, 0.0])
                d[0] += pr["blend"]
                d[1] += actual if i == 0 else 0.0
            p = p + pd.offsets.MonthBegin(1)
    rows = [{"source": k[0], "region": k[1], "commodity": k[2], "month": k[3], "spec": labels[k[4]],
             "projected": v[0], "actual_acc": v[1]} for k, v in rec.items()]
    df = pd.DataFrame(rows)
    first = labels[specs[0]]
    actual = {(r.source, r.region, r.commodity, r.month): r.actual_acc for r in df[df["spec"] == first].itertuples()}
    df["actual"] = [actual[(r.source, r.region, r.commodity, r.month)] for r in df.itertuples()]
    df = df[df["actual"] >= min_actual_kmt]
    df["ape"] = (df["projected"] - df["actual"]).abs() / df["actual"]
    df["bias"] = df["projected"] / df["actual"] - 1
    out = (df.groupby(["source", "commodity", "spec"])
             .agg(months=("ape", "size"), median_abs_pct_error=("ape", "median"), bias=("bias", "median"))
             .reset_index())
    out["spec"] = pd.Categorical(out["spec"], categories=list(labels.values()), ordered=True)
    return out.sort_values(["source", "commodity", "spec"]).reset_index(drop=True)


ALLOC_COLS = ["mtd_kmt", "pace_kmt", "seasonal_kmt", "blend_kmt", "low_kmt", "high_kmt", "vessels_mtd"]


def allocate_combo_projection(fc, events, trailing_months=12):
    """Split each port's projected Mixed Cargo tonnage across commodities and add it to that port's
    single-commodity rows. The combo series is forecast on its own first (combo boats are lumpy but steady
    in total), then split using that port's trailing-12-month combo mix under the corn-counts-double rule,
    falling back to the region's mix. Totals are conserved: every column sums to the same value before and
    after. A port with no combo history keeps its Mixed Cargo row."""
    mix_port = combos.combo_mix(events, by=("source", "region", "port"), trailing_months=trailing_months)
    mix_reg = combos.combo_mix(events, by=("source", "region"), trailing_months=trailing_months)
    is_mixed = fc["commodity"] == combos.COMBO_GROUP
    rest, mixed = fc[~is_mixed], fc[is_mixed]
    pieces, kept = [], []
    for _, r in mixed.iterrows():
        m = mix_port[(mix_port["source"] == r["source"]) & (mix_port["region"] == r["region"]) & (mix_port["port"] == r["port"])]
        if m.empty:
            m = mix_reg[(mix_reg["source"] == r["source"]) & (mix_reg["region"] == r["region"])]
        if m.empty:
            kept.append(r)
            continue
        for _, s_ in m.iterrows():
            p = r.copy()
            p["commodity"] = s_["commodity"]
            for c in ALLOC_COLS:
                p[c] = p[c] * s_["share"] if pd.notna(p[c]) else p[c]
            p["quality"] = (str(p["quality"]) + ";incl_combo_share").strip(";")
            pieces.append(p)
    parts = [rest] + ([pd.DataFrame(pieces)] if pieces else []) + ([pd.DataFrame(kept)] if kept else [])
    allr = pd.concat(parts, ignore_index=True)
    agg = {c: "sum" for c in ALLOC_COLS}
    agg.update({c: "first" for c in allr.columns if c not in ALLOC_COLS + KEY})
    agg["quality"] = lambda q: ";".join(dict.fromkeys(x for v in q for x in str(v).split(";") if x))
    out = allr.groupby(KEY, dropna=False, as_index=False).agg({**agg})
    for c in ("low_kmt", "high_kmt", "seasonal_kmt"):               # sum() turns all-NaN into 0; keep 'no range' as NaN
        none = allr.groupby(KEY, dropna=False)[c].apply(lambda x: x.isna().all()).to_numpy()
        out.loc[none, c] = np.nan
    return out[OUT_COLS].sort_values(["source", "region", "port", "commodity"]).reset_index(drop=True)
