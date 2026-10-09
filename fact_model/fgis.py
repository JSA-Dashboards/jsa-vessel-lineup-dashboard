"""Monthly reconciliation of vessel sail events against USDA FGIS export inspections.

FGIS (agtransport.usda.gov Socrata, no key) reports inspected tonnage by FGIS port region
and month. Inspections happen before/while loading, so a month rarely matches sailings
exactly; the reconciliation shows the gap and ratio, it does not force agreement.
"""

import numpy as np
import pandas as pd

SOCRATA_URL = "https://agtransport.usda.gov/resource/sruw-w49i.json"
GRAIN_TO_COMMODITY = {"CORN": "Corn", "SOYBEANS": "Soybeans", "WHEAT": "Wheat", "SORGHUM": "Sorghum"}
CARRIER = "SHIP"


def fetch_fgis_monthly(start_year, timeout=60):
    """-> DataFrame[month, fgis_port, commodity, fgis_kmt] for ship (vessel) inspections."""
    import requests
    grains = ",".join(f"'{g}'" for g in GRAIN_TO_COMMODITY)
    params = {
        "$select": "year,month,port,grain,sum(mt) as tot",
        "$where": f"year>={int(start_year)} and type_carrier_text='{CARRIER}' and grain in({grains})",
        "$group": "year,month,port,grain",
        "$limit": "50000",
    }
    r = requests.get(SOCRATA_URL, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    if df.empty:
        return pd.DataFrame(columns=["month", "fgis_port", "commodity", "fgis_kmt"])
    df["month"] = pd.to_datetime(dict(year=df["year"].astype(int), month=df["month"].astype(int), day=1))
    df["fgis_kmt"] = df["tot"].astype(float) / 1000.0
    df["commodity"] = df["grain"].map(GRAIN_TO_COMMODITY)
    df = df.rename(columns={"port": "fgis_port"})
    return df.groupby(["month", "fgis_port", "commodity"], as_index=False)["fgis_kmt"].sum()


def reconcile_monthly(events, fgis):
    """Our US sail events vs FGIS by month x FGIS port region x commodity.
    ours_no_tonnage = vessels we counted that have no reported tonnage (so ours is understated).
    Mixed Cargo / Other have no FGIS counterpart and are returned with fgis_kmt NaN."""
    e = events[(events["source"] == "US") & (events["fgis_port"] != "")].copy()
    e["month"] = pd.to_datetime(e["sail_date"]).dt.to_period("M").dt.to_timestamp()
    ours = (e.assign(_nt=e["kmt"].isna().astype(int), kmt=e["kmt"].fillna(0.0), _v=1)
              .groupby(["month", "fgis_port", "commodity"], as_index=False)
              .agg(ours_kmt=("kmt", "sum"), ours_vessels=("_v", "sum"), ours_no_tonnage=("_nt", "sum")))
    out = ours.merge(fgis, on=["month", "fgis_port", "commodity"], how="outer")
    for c in ("ours_kmt", "ours_vessels", "ours_no_tonnage"):
        out[c] = out[c].fillna(0)
    out["diff_kmt"] = out["ours_kmt"] - out["fgis_kmt"]
    out["ratio"] = np.where(out["fgis_kmt"] > 0, out["ours_kmt"] / out["fgis_kmt"].where(out["fgis_kmt"] > 0), np.nan)
    return out.sort_values(["month", "fgis_port", "commodity"]).reset_index(drop=True)
