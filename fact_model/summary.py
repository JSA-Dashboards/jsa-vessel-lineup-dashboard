"""Plain-text line-up summary: vessels queued per region and commodity, with the change vs last
week (LW) and last month (LM) in parentheses, e.g.

    PNW Total 20 (+1 LW, +3 LM), YSB 10 (-2 LW, +4 LM), YC 6 (-1 LW, -1 LM), SBM 4 (Unch LW, +1 LM)

Counts are vessels in the line-up (not tonnage). LW / LM compare against the banked snapshot nearest
7 days / 1 calendar month earlier, within a tolerance because snapshots are not daily (Southport
arrives roughly every other weekday). If no snapshot is close enough the change shows n/a.

A region whose newest snapshot is EMPTY after normally holding vessels (e.g. a Southport file whose
Mississippi River sheet came through blank) is reported from its last good snapshot and marked, never
as a false zero.
"""

import numpy as np
import pandas as pd

SHORT = {"Corn": "YC", "Soybeans": "YSB", "Soybean Meal": "SBM", "Wheat": "WHT", "Sorghum": "SORG",
         "Dist. Grains": "DDGS", "Rice": "RICE", "Sugar": "SUGAR", "Mixed Cargo": "COMBO", "Other": "OTHER"}
REGION_LABEL = {"PNW": "PNW", "US Gulf": "USG", "Texas Gulf": "TXG", "Brazil North Arc": "BZL North Arc",
                "Brazil South/Southeast": "BZL South/SE", "UNMAPPED": "UNMAPPED"}
REGION_ORDER = ["PNW", "US Gulf", "Texas Gulf", "Brazil North Arc", "Brazil South/Southeast", "UNMAPPED"]
MIN_NORMAL_VESSELS = 10          # a region that usually holds at least this many and suddenly holds 0 is a data gap


def _baseline(dates, target, tol_days):
    """Banked snapshot date nearest `target` within tol_days, else None."""
    if not len(dates):
        return None
    d = min(dates, key=lambda x: abs((x - target).days))
    return d if abs((d - target).days) <= tol_days else None


def lineup_summary(lineup, lw_days=7, lm_months=1, tol_days=3):
    """One row per source x region x commodity (plus a 'Total' row per region): vessels now and the
    change vs last week / last month. Columns: source, region, commodity, now, d_lw, d_lm, as_of,
    lw_date, lm_date, gap_note ('' unless the newest snapshot was empty for this region)."""
    rows = []
    for src, g in lineup.groupby("source"):
        g = g.assign(report_date=pd.to_datetime(g["report_date"]))
        dates = [pd.Timestamp(d) for d in sorted(g["report_date"].unique())]
        newest = dates[-1]
        counts = g.groupby(["report_date", "region", "commodity"])["vessels"].sum()
        totals = g.groupby(["report_date", "region"])["vessels"].sum()

        def tot(d, region):
            return int(totals.get((d, region), 0))

        for region in g["region"].unique():
            good = [d for d in dates if tot(d, region) > 0]
            as_of, note = newest, ""
            if tot(newest, region) == 0 and good:
                typical = float(np.median([tot(d, region) for d in good[-5:]]))
                if typical >= MIN_NORMAL_VESSELS:
                    as_of = good[-1]
                    note = f"newest snapshot ({newest:%b %d}) had no {REGION_LABEL.get(region, region)} line-up"
            lw = _baseline(dates, as_of - pd.Timedelta(days=lw_days), tol_days)
            lm = _baseline(dates, as_of - pd.DateOffset(months=lm_months), tol_days)
            lw = None if lw == as_of else lw
            lm = None if lm == as_of else lm

            def at(d, com):
                if d is None:
                    return None
                return tot(d, region) if com is None else int(counts.get((d, region, com), 0))

            for com in [None] + sorted(g.loc[g["region"] == region, "commodity"].unique()):
                now, a, b = at(as_of, com), at(lw, com), at(lm, com)
                rows.append({"source": src, "region": region, "commodity": "Total" if com is None else com, "now": now,
                             "d_lw": None if a is None else now - a, "d_lm": None if b is None else now - b,
                             "as_of": as_of, "lw_date": lw, "lm_date": lm, "gap_note": note})
    return pd.DataFrame(rows)


def _delta(d):
    if d is None or pd.isna(d):
        return "n/a"
    d = int(d)
    return "Unch" if d == 0 else f"{d:+d}"


def _change(r):
    return f"({_delta(r['d_lw'])} LW, {_delta(r['d_lm'])} LM)"


def summary_lines(summary, regions=None):
    """[(source, region, as_of, line, note)] with line like
    'PNW Total 20 (+1 LW, +3 LM), YSB 10 (-2 LW, +4 LM), ...'. Commodities are listed largest first, and only
    when there is something to say (vessels now, or a change). A region reported from an earlier snapshot is
    starred and the note says why."""
    out = []
    for src in summary["source"].unique():
        s_src = summary[summary["source"] == src]
        for region in [r for r in (regions or REGION_ORDER) if r in set(s_src["region"])]:
            s = s_src[s_src["region"] == region]
            tot = s[s["commodity"] == "Total"].iloc[0]
            label = REGION_LABEL.get(region, region) + ("*" if tot["gap_note"] else "")
            parts = [f"{label} Total {int(tot['now'])} {_change(tot)}"]
            for _, r in s[s["commodity"] != "Total"].sort_values(["now", "commodity"], ascending=[False, True]).iterrows():
                if r["now"] == 0 and not (r["d_lw"] or r["d_lm"]):
                    continue
                parts.append(f"{SHORT.get(r['commodity'], r['commodity'])} {int(r['now'])} {_change(r)}")
            note = f"*as of {tot['as_of']:%b %d}: {tot['gap_note']}" if tot["gap_note"] else ""
            out.append((src, region, tot["as_of"], ", ".join(parts), note))
    return out
