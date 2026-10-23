"""Fill UNDISCLOSED sailings from USDA FGIS vessel inspections.

Southport lists a sailed vessel's tonnage, commodity and destination as "RVT" (revert) when the shipper does not disclose them:
28% of sailed rows overall, and on the Mississippi that is ~29% of what USDA inspects (the whole gap between our sailings and
USDA's inspected tonnage). Those boats were also undisclosed while waiting in the line-up (only ~7% had a figure earlier), so the
only source for what they loaded is USDA, which reports every inspected vessel by name with grain, tonnage and destination.

  fill_undisclosed   match each undisclosed sailing to its vessel's USDA inspections and read off commodity, tonnage, destination
  apply_fill         swap the undisclosed sailings for their filled rows (vessel counts conserved)
  backtest_fill      hide boats whose cargo IS known, fill them from USDA, and score the fill

USDA covers grain only (corn, soybeans, wheat, sorghum): meal, DDGS and other cargo on an undisclosed boat stay unknown. USDA also
lags: a sailing is filled once its vessel's inspections are published (usually within a week), so the newest sailings stay
undisclosed until then. If a vessel has two undisclosed sailings in one USDA visit, the visit's tonnage is shared equally.
"""

import numpy as np
import pandas as pd

from .combo_calibration import GRAIN, vessel_key

FILL_COLS = ["event_id", "commodity", "kmt", "destination", "share"]
WINDOW = (35, 10)           # USDA inspection dates considered: [sail date - 35 days, sail date + 10 days]


def undisclosed_mask(events):
    """Sailings whose tonnage is unknown and whose commodity fell into 'Other' (the RVT rows)."""
    return (events["source"] == "US") & events["kmt"].isna() & (events["commodity"] == "Other") & events["vessel"].notna()


def _visits(ships, gap=25):
    s = ships[ships["commodity"].notna()].sort_values(["vessel_key", "port", "date"]).copy()
    s["visit"] = (s.groupby(["vessel_key", "port"])["date"].diff().dt.days.fillna(999) > gap).cumsum()
    return s


def fill_undisclosed(events, ships, window=WINDOW):
    """-> DataFrame[event_id, commodity, kmt, destination, share]: one row per (undisclosed sailing, grain). `share` is the grain's
    share of the filled tonnage, used to split the sailing's vessel count."""
    und = events[undisclosed_mask(events)].copy()
    if und.empty or ships.empty:
        return pd.DataFrame(columns=FILL_COLS)
    und["vk"] = und["vessel"].map(vessel_key)
    sv = _visits(ships)
    by_vessel = {k: g for k, g in sv.groupby("vessel_key")}
    disclosed = events[(events["source"] == "US") & events["kmt"].notna() & events["vessel"].notna()].copy()
    disclosed["vk"] = disclosed["vessel"].map(vessel_key)
    disc_by = {k: g for k, g in disclosed.groupby("vk")}
    assigned = {}                                    # visit id -> list of undisclosed event rows
    for r in und.itertuples():
        g = by_vessel.get(r.vk)
        if g is None:
            continue
        sd = pd.Timestamp(r.sail_date)
        cand = g[(g["date"] >= sd - pd.Timedelta(days=window[0])) & (g["date"] <= sd + pd.Timedelta(days=window[1]))]
        if r.fgis_port and (cand["port"] == r.fgis_port).any():
            cand = cand[cand["port"] == r.fgis_port]
        if cand.empty:
            continue
        # the visit whose inspections are closest to the sail date
        vid = cand.assign(d=(cand["date"] - sd).abs()).sort_values("d")["visit"].iloc[0]
        assigned.setdefault(vid, []).append(r)
    rows = []
    for vid, evs in assigned.items():
        v = sv[sv["visit"] == vid]
        first, last = v["date"].min(), v["date"].max()
        by_c = v.groupby("commodity")["kmt"].sum()
        total = by_c.sum()
        # tonnage of this vessel already disclosed on other sailings in the same visit comes off the top
        d = disc_by.get(evs[0].vk)
        used = 0.0
        if d is not None:
            dd = d[(pd.to_datetime(d["sail_date"]) >= first - pd.Timedelta(days=12)) & (pd.to_datetime(d["sail_date"]) <= last + pd.Timedelta(days=30))]
            used = float(dd["kmt"].sum())
        remaining = by_c * max(0.0, 1.0 - used / total) if total > 0 else by_c
        remaining = remaining[remaining > 0.05]
        if remaining.empty:
            continue
        dest = (v.assign(dest=v["destination"].astype(str).str.title()).groupby("dest")["kmt"].sum().idxmax()
                if "destination" in v and v["destination"].notna().any() else "Unknown")
        n = len(evs)
        tot_rem = float(remaining.sum())
        for e in evs:
            for c, k in remaining.items():
                rows.append({"event_id": e.event_id, "commodity": c, "kmt": float(k) / n, "destination": dest, "share": float(k) / tot_rem})
    return pd.DataFrame(rows, columns=FILL_COLS)


def apply_fill(events, fill):
    """Replace each filled sailing by one row per grain (commodity, tonnage, destination from USDA; vessel_w split by `share`).
    Adds `fill_method` = 'usda' on those rows. Unmatched undisclosed sailings are left as they are."""
    e = events.copy()
    e["fill_method"] = ""
    if fill is None or fill.empty:
        return e
    ids = set(fill["event_id"])
    hit = e[e["event_id"].isin(ids)]
    j = hit.merge(fill.rename(columns={"commodity": "_c", "kmt": "_k", "destination": "_d", "share": "_s"}), on="event_id", how="inner")
    j["commodity"], j["kmt"], j["destination"] = j["_c"], j["_k"], j["_d"]
    if "vessel_w" in j:
        j["vessel_w"] = j["vessel_w"].fillna(1.0) * j["_s"]
    j["event_id"] = j["event_id"] + "|" + j["_c"]
    j["combo"] = ""
    j["fill_method"] = "usda"
    return pd.concat([e[~e["event_id"].isin(ids)], j.drop(columns=["_c", "_k", "_d", "_s"])], ignore_index=True)


def backtest_fill(events, ships, since=None, sample_cap=None):
    """Hide the cargo of boats whose cargo IS known (single-commodity, with tonnage), fill them from USDA, and compare.
    -> dict: n (boats scored), commodity_match (share where the filled main commodity is the disclosed one), tonnage_median_abs_err
    and tonnage_total_ratio (filled / disclosed, summed)."""
    e = events[(events["source"] == "US") & events["kmt"].notna() & events["commodity"].isin(GRAIN.values()) & events["vessel"].notna()].copy()
    if since is not None:
        e = e[pd.to_datetime(e["sail_date"]) >= pd.Timestamp(since)]
    if sample_cap and len(e) > sample_cap:
        e = e.sample(sample_cap, random_state=1)
    truth = e[["event_id", "commodity", "kmt"]].rename(columns={"commodity": "true_c", "kmt": "true_k"})
    masked = e.assign(kmt=np.nan, commodity="Other")
    # the other sailings of these vessels are still 'disclosed' for the subtraction step only in the real use; here every
    # test boat is hidden, so pass the masked set alone
    f = fill_undisclosed(masked, ships)
    if f.empty:
        return {"n": 0, "commodity_match": np.nan, "tonnage_median_abs_err": np.nan, "tonnage_total_ratio": np.nan}
    by_event = f.groupby("event_id").agg(k=("kmt", "sum"))
    main = f.sort_values("kmt", ascending=False).drop_duplicates("event_id").set_index("event_id")["commodity"]
    t = truth.set_index("event_id").join(by_event, how="inner").join(main.rename("fill_c"))
    return {"n": int(len(t)), "commodity_match": float((t["true_c"] == t["fill_c"]).mean()),
            "tonnage_median_abs_err": float(((t["k"] - t["true_k"]).abs() / t["true_k"]).median()),
            "tonnage_total_ratio": float(t["k"].sum() / t["true_k"].sum())}
