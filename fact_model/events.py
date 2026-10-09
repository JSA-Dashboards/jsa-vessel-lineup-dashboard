"""Executed (shipped) facts built from VESSEL SAIL EVENTS: ADDITIVE math only.

Each event is one cargo line on one vessel with a sail date. Unlike a cumulative report
figure, events are additive, so MTD / MYTD / YTD are plain windowed sums and are exact
(no baseline gap). Snapshot math lives in lineup.py; cumulative math in executed.py.

Sail date: US = the workbook's SAIL DATE. Brazil = ETCS of vessels the APS report lists as
sailed (SLD), i.e. estimated completion of loading, the report's closest date to sailing.
Tonnage is NaN (never 0) when the report gives none (US 'RVT'); callers see the coverage.
"""

import hashlib

import numpy as np
import pandas as pd

from .dimensions import add_geography, commodity_group, default_config, my_start, marketing_year_label

EVENT_COLS = ["event_id", "source", "sail_date", "region", "port", "fgis_port", "elevator",
              "commodity", "destination", "vessel", "kmt", "mapped", "unmapped_reason"]
KEY = ["source", "region", "port", "elevator", "commodity"]


def _event_ids(df, parts):
    cols = [df[p].map(lambda x: "" if pd.isna(x) else str(x)) for p in parts]
    base = cols[0].str.cat(cols[1:], sep="|")
    ordinal = base.groupby(base).cumcount().astype(str)
    return [hashlib.sha1((b + "|" + o).encode()).hexdigest()[:20] for b, o in zip(base, ordinal)]


def _finish(df, source, cfg):
    df["source"] = source
    df["fgis_port"] = df["port"].map(cfg.fgis_by_port).fillna("")
    df["destination"] = (df["destination"].astype("string").str.strip().str.upper()
                         .replace({"RVT": "UNKNOWN", "?": "UNKNOWN", "": "UNKNOWN"}).fillna("UNKNOWN"))
    return df[EVENT_COLS].reset_index(drop=True)


def us_sail_events(vessels, cfg=None):
    """vessels: lineup.read_us_vessels(). Rows with a plausible sail date (the workbook has
    1900-era junk dates) become events; a row with no tonnage is kept with kmt NaN."""
    cfg = cfg or default_config()
    s = vessels[vessels["sail_date"].notna() & (vessels["sail_date"] >= "2000-01-01")].copy()
    s = add_geography(s, "US", cfg)
    s["commodity"] = [commodity_group("US", c) for c in s["commodity_raw"]]
    # Identity = sheet + vessel + sail date. Southport revises the elevator pairing, commodity
    # code and tonnage between files, so those must not be part of the id or one sailing would be
    # counted once per revision. Rows with no vessel name fall back to the elevator.
    has_v = s["vessel"].notna()
    s["_who"] = s["vessel"].where(has_v, "E:" + s["elevator_raw"].fillna("").astype(str))
    s["event_id"] = _event_ids(s.assign(d=s["sail_date"].dt.strftime("%Y-%m-%d")), ["sheet", "_who", "d"])
    s["kmt"] = s["kmt"].where(s["kmt"] > 0)
    return _finish(s, "US", cfg)


def brazil_sail_events(sailed_df, cfg=None):
    """sailed_df: rows from brazil_db.get_all_sailed (every snapshot). The same vessel appears
    in many daily snapshots, so events are de-duplicated; the latest snapshot's values win."""
    cfg = cfg or default_config()
    s = sailed_df[(sailed_df["excluded"] == 0) & sailed_df["etcs"].notna()].copy()
    if s.empty:
        return pd.DataFrame(columns=EVENT_COLS)
    s["sail_date"] = pd.to_datetime(s["etcs"])
    s = s.sort_values(["report_date", "id"])
    parts = []
    for _, g in s.groupby("report_date", sort=True):          # ordinal is per snapshot so repeats don't collide
        g = g.copy()
        g["event_id"] = _event_ids(g, ["port", "berth", "vessel", "product", "etcs"])
        parts.append(g)
    s = pd.concat(parts).drop_duplicates("event_id", keep="last")
    s = s.rename(columns={"port": "port_raw"})
    s = add_geography(s, "Brazil", cfg)
    s["commodity"] = [commodity_group("Brazil", p) for p in s["product"]]
    s["kmt"] = pd.to_numeric(s["mt"], errors="coerce").where(lambda x: x > 0) / 1000.0
    return _finish(s, "Brazil", cfg)


def _as_of_series(events, as_of):
    if isinstance(as_of, dict):
        return events["source"].map({k: pd.Timestamp(v) for k, v in as_of.items()})
    return pd.Series(pd.Timestamp(as_of), index=events.index)


def events_executed(events, as_of, by=KEY, cfg=None):
    """MTD / MYTD / YTD tonnage as of `as_of` (a date, or {source: date}) per `by`.
    Windows: MTD = calendar month of as_of; MYTD = from that commodity's marketing-year
    start; YTD = calendar year. Also returns vessel counts and tonnage coverage so a reader
    can see how much of the shipped volume has no reported tonnage."""
    cfg = cfg or default_config()
    e = events.copy()
    e["sail_date"] = pd.to_datetime(e["sail_date"])
    e["_asof"] = _as_of_series(e, as_of)
    e = e[e["sail_date"] <= e["_asof"]]
    if e.empty:
        return pd.DataFrame(columns=list(by) + ["mtd_kmt", "mytd_kmt", "ytd_kmt", "vessels_mtd",
                                                "vessels_mytd", "no_tonnage_mytd", "coverage_mytd"])
    e["_mstart"] = e["_asof"].dt.to_period("M").dt.to_timestamp()
    e["_ystart"] = e["_asof"].dt.to_period("Y").dt.to_timestamp()
    my_cache = {(a, c): my_start(a, c, cfg) for a, c in e[["_asof", "commodity"]].drop_duplicates().itertuples(index=False)}
    e["_mystart"] = [my_cache[(a, c)] for a, c in zip(e["_asof"], e["commodity"])]
    e["_k"] = e["kmt"].fillna(0.0)
    e["_nt"] = e["kmt"].isna()
    in_m, in_my, in_y = (e["sail_date"] >= e["_mstart"]), (e["sail_date"] >= e["_mystart"]), (e["sail_date"] >= e["_ystart"])
    by = list(by)
    g = pd.DataFrame({
        **{c: e[c] for c in by},
        "mtd_kmt": e["_k"].where(in_m, 0.0), "mytd_kmt": e["_k"].where(in_my, 0.0),
        "ytd_kmt": e["_k"].where(in_y, 0.0),
        "vessels_mtd": in_m.astype(int), "vessels_mytd": in_my.astype(int),
        "no_tonnage_mytd": (e["_nt"] & in_my).astype(int),
    }).groupby(by, dropna=False).sum().reset_index()
    v = g["vessels_mytd"].where(g["vessels_mytd"] > 0)
    g["coverage_mytd"] = 1 - g["no_tonnage_mytd"] / v
    return g


def events_monthly(events, by=KEY):
    """Shipped tonnage per calendar month per `by`. Direct sums, no differencing."""
    e = events.copy()
    e["month"] = pd.to_datetime(e["sail_date"]).dt.to_period("M").dt.to_timestamp()
    e["_nt"] = e["kmt"].isna().astype(int)
    by = list(by)
    g = (e.assign(kmt=e["kmt"].fillna(0.0), vessels=1)
          .groupby(["month", *by], dropna=False)
          .agg(kmt=("kmt", "sum"), vessels=("vessels", "sum"), no_tonnage=("_nt", "sum")).reset_index())
    g["marketing_year"] = [marketing_year_label(m, c) for m, c in zip(g["month"], g["commodity"])] if "commodity" in g else ""
    return g
