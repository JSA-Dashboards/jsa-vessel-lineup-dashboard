"""Week / month / year changes for the line-up and for sailings, at any grouping.

Line-up (snapshots): each region is read at its own newest usable snapshot ("now"), and compared with the
snapshot nearest 7 days, 1 month and 1 year earlier (within a tolerance, because snapshots are not daily).
A snapshot in which a region that normally holds vessels is EMPTY is a data gap (e.g. a Southport file with a
blank Mississippi sheet), not a real zero: it is never used as "now" or as a baseline. If no snapshot is close
enough the change is NaN and the view shows n/a.

Sailed (events, additive): last 7 days vs the 7 before; month-to-date vs the same days of last month and of
the same month last year; year-to-date vs last year's. A window that starts before the source's first event is
NaN (no history), never a false zero.
"""

import numpy as np
import pandas as pd

MIN_NORMAL_VESSELS = 10        # a region that usually holds at least this many and suddenly holds 0 is a data gap
TOL = {"wk": 3, "mo": 5, "yr": 10}
PREV = ["prev_wk", "prev_mo", "prev_yr"]


def _nearest(dates, target, tol_days):
    """The held snapshot date closest to `target` within tol_days, else None."""
    if not len(dates):
        return None
    d = min(dates, key=lambda x: abs((x - target).days))
    return d if abs((d - target).days) <= tol_days else None


def lineup_detail(lineup, measure="vessels"):
    """Finest-grain table: source, region, port, elevator, commodity with `now` and the value a week / month / year
    earlier (NaN where there is no usable snapshot). Also returns per-region metadata (as_of, baseline dates,
    note). Sum these columns up to any level with rollup()."""
    lineup = lineup.assign(report_date=pd.to_datetime(lineup["report_date"]))
    if "elevator" not in lineup:
        lineup = lineup.assign(elevator="")
    lineup["elevator"] = lineup["elevator"].fillna("").astype(str)
    parts, metas = [], []
    for src, g in lineup.groupby("source"):
        dates = [pd.Timestamp(d) for d in sorted(g["report_date"].unique())]
        newest = dates[-1]
        tot = g.groupby(["region", "report_date"])["vessels"].sum()
        agg = g.groupby(["report_date", "region", "port", "elevator", "commodity"])[measure].sum()
        for region in g["region"].unique():
            t = lambda d: float(tot.get((region, d), 0.0))
            good = [d for d in dates if t(d) > 0]
            typical = float(np.median([t(d) for d in good[-20:]])) if good else 0.0
            gap_prone = typical >= MIN_NORMAL_VESSELS
            usable = good if gap_prone else dates
            as_of, note = newest, ""
            if gap_prone and t(newest) == 0:
                as_of = good[-1]
                note = f"newest snapshot ({newest:%b %d}) had no {region} line-up; showing {as_of:%b %d}"
            wk = _nearest([d for d in usable if d != as_of], as_of - pd.Timedelta(days=7), TOL["wk"])
            mo = _nearest([d for d in usable if d != as_of], as_of - pd.DateOffset(months=1), TOL["mo"])
            yr = _nearest([d for d in usable if d != as_of], as_of - pd.DateOffset(years=1), TOL["yr"])
            want = {"now": as_of, "prev_wk": wk, "prev_mo": mo, "prev_yr": yr}
            sub = agg.xs(region, level="region")
            frames = []
            for col, d in want.items():
                if d is None:
                    continue
                s = sub[sub.index.get_level_values("report_date") == d].droplevel("report_date")
                frames.append(s.rename(col))
            if not frames:
                continue
            w = pd.concat(frames, axis=1)
            for col, d in want.items():                       # a held baseline date with no rows = 0; no baseline = NaN
                if col not in w:
                    w[col] = np.nan
                elif d is not None:
                    w[col] = w[col].fillna(0.0)
            w = w.reset_index()
            w.insert(0, "region", region)
            w.insert(0, "source", src)
            parts.append(w[["source", "region", "port", "elevator", "commodity", "now", *PREV]])
            metas.append({"source": src, "region": region, "as_of": as_of, "wk_date": wk, "mo_date": mo,
                          "yr_date": yr, "note": note, "newest": newest})
    detail = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["source", "region", "port", "elevator", "commodity", "now", *PREV])
    detail[["now", *PREV]] = detail[["now", *PREV]].astype(float)
    return detail, pd.DataFrame(metas)


def rollup(detail, by):
    """Sum a lineup_detail() table to the `by` columns and add absolute (d_*) and percent (p_*) changes.
    A region with no comparison snapshot (a source that started recently) drops out of that change - and out
    of its baseline - instead of being counted as growth from zero; the change is NaN only when NO contributing
    region has a baseline."""
    by = list(by)
    d = detail.copy()
    for k in ("wk", "mo", "yr"):
        d[f"d_{k}"] = d["now"] - d[f"prev_{k}"]
    cols = ["now", *PREV, "d_wk", "d_mo", "d_yr"]
    g = d.groupby(by, dropna=False)[cols].sum(min_count=1).reset_index()
    for k in ("wk", "mo", "yr"):
        g[f"p_{k}"] = (g[f"d_{k}"] / g[f"prev_{k}"].where(g[f"prev_{k}"] > 0)) * 100
    return g


def current_rows(lineup, meta):
    """Snapshot rows at each (source, region)'s `as_of` date - the current line-up, with destinations."""
    lineup = lineup.assign(report_date=pd.to_datetime(lineup["report_date"]))
    key = meta.set_index(["source", "region"])["as_of"]
    want = lineup.set_index(["source", "region"]).index.map(key)
    return lineup[(lineup["report_date"].values == pd.DatetimeIndex(want).values)]


# ── Sailed ───────────────────────────────────────────────────────────────────

WINDOWS = ["l7", "l7_prev", "mtd", "mtd_pm", "mtd_ly", "ytd", "ytd_ly"]
DELTAS = ["d_wk", "d_mo", "d_yr", "d_ytd"]


def _windows(asof):
    a = pd.Timestamp(asof).normalize()
    ms = a.replace(day=1)
    pm = ms - pd.DateOffset(months=1)
    ly = ms - pd.DateOffset(years=1)
    clip = lambda start: min(start + pd.Timedelta(days=a.day - 1), start + pd.offsets.MonthEnd(0))
    ys = a.replace(month=1, day=1)
    return {"l7": (a - pd.Timedelta(days=6), a), "l7_prev": (a - pd.Timedelta(days=13), a - pd.Timedelta(days=7)),
            "mtd": (ms, a), "mtd_pm": (pm, clip(pm)), "mtd_ly": (ly, clip(ly)),
            "ytd": (ys, a), "ytd_ly": (ys - pd.DateOffset(years=1), a - pd.DateOffset(years=1))}


def sailed_windows(events, by, measure="vessels", asof=None):
    """Sailed totals per `by` for each window in WINDOWS, plus changes. `measure` is 'vessels' (vessel_w, else 1
    per event) or 'kmt'. Each source is read through its own latest sail date (or `asof`)."""
    by = list(by)
    e = events.assign(sail_date=pd.to_datetime(events["sail_date"]))
    e["_v"] = e["vessel_w"] if "vessel_w" in e else 1.0
    e["_m"] = e["_v"] if measure == "vessels" else e["kmt"].fillna(0.0)
    outs = []
    for src, g in e.groupby("source"):
        a = pd.Timestamp(asof) if asof is not None else g["sail_date"].max()
        first = g["sail_date"].min()
        cols = {}
        for name, (lo, hi) in _windows(a).items():
            m = (g["sail_date"] >= lo) & (g["sail_date"] <= hi)
            cols[name] = g["_m"].where(m, 0.0)
        agg = pd.DataFrame({**{c: g[c] for c in by}, **cols}).groupby(by, dropna=False).sum().reset_index()
        for name, (lo, hi) in _windows(a).items():              # comparison window reaches before our first event: no history
            if name in ("l7", "mtd", "ytd"):
                continue
            if lo < (first if name == "l7_prev" else first.replace(day=1)):
                agg[name] = np.nan
        for dn, (x, y) in {"d_wk": ("l7", "l7_prev"), "d_mo": ("mtd", "mtd_pm"), "d_yr": ("mtd", "mtd_ly"), "d_ytd": ("ytd", "ytd_ly")}.items():
            agg[dn] = agg[x] - agg[y]
        outs.append(agg)
    if not outs:
        return pd.DataFrame(columns=by + WINDOWS)
    out = pd.concat(outs, ignore_index=True).groupby(by, dropna=False)[WINDOWS + DELTAS].sum(min_count=1).reset_index()
    return out


def sailed_asof(events):
    """Latest sail date per source."""
    e = events.assign(sail_date=pd.to_datetime(events["sail_date"]))
    return e.groupby("source")["sail_date"].max()


def gap_dates(lineup):
    """Snapshot dates on which a region that normally holds vessels came through EMPTY (a data gap, not a real
    zero). `lineup` is one source's rows; drop these dates from trend charts so they do not show false dips."""
    lineup = lineup.assign(report_date=pd.to_datetime(lineup["report_date"]))
    tot = lineup.groupby(["region", "report_date"])["vessels"].sum().unstack(0)
    bad = set()
    all_dates = lineup["report_date"].unique()
    for region in tot.columns:
        s = tot[region].reindex(all_dates).fillna(0.0)
        good = s[s > 0]
        if len(good) and float(good.iloc[-20:].median()) >= MIN_NORMAL_VESSELS:
            bad |= set(s[s == 0].index)
    return bad
