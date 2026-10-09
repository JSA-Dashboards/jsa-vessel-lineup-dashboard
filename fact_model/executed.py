"""Executed (shipped) facts: CUMULATIVE math only.

The reported shipped figure is a calendar-year-to-date running total. It is never
summed across snapshots. MTD / MYTD come from differencing against a baseline
snapshot. Line-up (snapshot) math lives in lineup.py and must not be used here.

Semantics
- cum_raw_kmt is YTD: it restarts at 0 on Jan 1. To difference across a year
  boundary we build an absolute running total A = cum_ytd + (last cum_ytd of every
  earlier calendar year in the series).
- Baseline for a boundary date B = the LAST snapshot strictly before B.
  MTD  boundary = first day of the snapshot's month.
  MYTD boundary = marketing-year start for that commodity (config).
  Snapshots may be unevenly spaced; the baseline gap (days) is reported and a
  quality flag is raised when it is stale. If no baseline exists the value is NaN
  (never guessed), except at a Jan 1 boundary where the cumulative is 0 by definition.
- MTD is exact only when the baseline snapshot is month-end (gap 0). Otherwise the days
  between the baseline and month-end roll into the NEXT month; nothing is lost or double
  counted, so monthly totals still reconcile exactly to MYTD.
"""

import numpy as np
import pandas as pd

from .dimensions import default_config, marketing_year_label, my_start

KEY = ["source", "region", "port", "elevator", "commodity"]
STALE_DAYS = 7
OUT_COLS = ["report_date", *KEY, "month", "marketing_year", "cum_raw_kmt", "mtd_kmt", "mytd_kmt",
            "mtd_baseline_date", "mytd_baseline_date", "mtd_baseline_gap_days",
            "mytd_baseline_gap_days", "quality"]


def _derive_series(g, cfg, cum_col):
    g = g.sort_values("report_date")
    commodity = g["commodity"].iloc[0]
    dates = g["report_date"].to_numpy("datetime64[ns]")
    cum = g[cum_col].to_numpy(float)
    years = g["report_date"].dt.year.to_numpy()
    first_year = int(years[0])

    year_end = {}
    for y, c in zip(years, cum):
        year_end[int(y)] = c
    carry = np.array([sum(year_end[yy] for yy in range(first_year, int(y)) if yy in year_end)
                      for y in years])
    year_gap = np.array([any(yy not in year_end for yy in range(first_year, int(y))) for y in years])
    A = cum + carry
    revision = np.concatenate([[False], np.diff(A) < 0])

    def baseline(boundary):
        i = int(np.searchsorted(dates, np.datetime64(boundary), side="left")) - 1
        if i >= 0:
            gap = int((boundary - pd.Timedelta(days=1) - pd.Timestamp(dates[i])).days)
            return A[i], pd.Timestamp(dates[i]), gap
        if boundary.month == 1 and boundary.day == 1 and boundary.year == first_year:
            return 0.0, pd.NaT, 0
        return np.nan, pd.NaT, np.nan

    rows = []
    for idx, d in enumerate(g["report_date"]):
        mb = pd.Timestamp(d.year, d.month, 1)
        yb = my_start(d, commodity, cfg)
        m_val, m_dt, m_gap = baseline(mb)
        y_val, y_dt, y_gap = baseline(yb)
        flags = []
        if np.isnan(m_val):
            flags.append("no_mtd_baseline")
        elif m_gap > STALE_DAYS:
            flags.append("stale_mtd_baseline")
        if np.isnan(y_val):
            flags.append("no_mytd_baseline")
        elif y_gap > STALE_DAYS:
            flags.append("stale_mytd_baseline")
        if revision[idx]:
            flags.append("revision")
        if year_gap[idx]:
            flags.append("year_gap")
        rows.append({
            "report_date": d, "month": mb,
            "marketing_year": marketing_year_label(d, commodity, cfg),
            "cum_raw_kmt": cum[idx],
            "mtd_kmt": A[idx] - m_val, "mytd_kmt": A[idx] - y_val,
            "mtd_baseline_date": m_dt, "mytd_baseline_date": y_dt,
            "mtd_baseline_gap_days": m_gap, "mytd_baseline_gap_days": y_gap,
            "quality": ";".join(flags),
        })
    out = pd.DataFrame(rows)
    for k in KEY:
        out[k] = g[k].iloc[0]
    return out[OUT_COLS]


def derive_executed(raw, cfg=None, cum_col="cum_raw_kmt"):
    """raw: one row per (KEY, report_date) with the reported YTD cumulative in cum_col.
    Returns the executed fact (raw cumulative + MTD + MYTD per row)."""
    cfg = cfg or default_config()
    raw = raw.copy()
    raw["report_date"] = pd.to_datetime(raw["report_date"]).dt.normalize()
    if raw.duplicated(KEY + ["report_date"], keep=False).any():
        raise ValueError("Two cumulative figures for the same key and report_date; "
                         "summing them would be wrong - resolve at the source.")
    parts = [_derive_series(g, cfg, cum_col) for _, g in raw.groupby(KEY, dropna=False, sort=False)]
    if not parts:
        return pd.DataFrame(columns=OUT_COLS)
    return pd.concat(parts, ignore_index=True).sort_values(KEY + ["report_date"]).reset_index(drop=True)


def executed_asof(df, as_of=None):
    """Latest snapshot per key at or before as_of. This is how cumulative data is read."""
    d = df if as_of is None else df[df["report_date"] <= pd.Timestamp(as_of)]
    return d.sort_values("report_date").groupby(KEY, dropna=False, sort=False).tail(1)


def rollup_executed(df, by, as_of=None, cols=("cum_raw_kmt", "mtd_kmt", "mytd_kmt")):
    """Sum ACROSS keys (ports/elevators/commodities) of each key's latest snapshot.
    Never sums across report_dates."""
    latest = executed_asof(df, as_of)
    return latest.groupby(by, dropna=False)[list(cols)].sum(min_count=1).reset_index()


def monthly_executed(df):
    """Shipped per month per key = MTD on the key's last snapshot in that month.
    last_report_date shows how far into the month the data actually reaches."""
    d = df.copy()
    d["_m"] = d["report_date"].dt.to_period("M").dt.to_timestamp()
    last = d.sort_values("report_date").groupby(KEY + ["_m"], dropna=False, sort=False).tail(1)
    return (last.rename(columns={"_m": "month_start", "report_date": "last_report_date"})
                [KEY + ["month_start", "last_report_date", "mtd_kmt", "quality"]]
                .reset_index(drop=True))
