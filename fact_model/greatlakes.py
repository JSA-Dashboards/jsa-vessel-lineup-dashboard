"""Great Lakes loadings from USDA FGIS ship inspections (Toledo, Duluth-Superior, Chicago, Seaway).

There is no public line-up for the lakes, so this is the after-the-fact record: every vessel USDA inspected loading grain at a lake
port, with grain, tonnage and destination (agtransport.usda.gov, same dataset as the vessel fill; history from 1983, loaded from
2015). One event per vessel x week x grain. USDA's `date` is the week ending Thursday, so that is the event's date.
Oceangoing ships dominate (Duluth: wheat to Italy; Toledo: corn/soy to Europe and North Africa) with some lakers carrying
Toledo grain to Canada. Sailed history only: there is nothing to put in a line-up. Events carry no fgis_port so the
combo/fill calibration, which reconciles against USDA, leaves them alone.
"""

import hashlib

import pandas as pd

from . import argentina as ar
from . import events as ev
from . import store
from .dimensions import add_geography, default_config

SOCRATA_URL = "https://agtransport.usda.gov/resource/sruw-w49i.json"
GRAIN = {"CORN": "Corn", "SOYBEANS": "Soybeans", "WHEAT": "Wheat", "SORGHUM": "Sorghum"}
PORT_NAME = {"TOLEDO": "Toledo", "DULUTH-SUP": "Duluth-Superior", "CHICAGO": "Chicago", "SEAWAY": "Seaway",
             "LAKE ERIE": "Lake Erie", "LAKE ONTARIO": "Lake Ontario", "LAKE SUPERIOR": "Lake Superior", "PORT HURON": "Port Huron"}


def fetch_lake_ships(start_year=2015, timeout=120):
    """USDA ship inspections at lake ports. -> DataFrame[date, vessel, grain, commodity, destination, port, kmt]"""
    import requests
    rows, off = [], 0
    ports = ",".join(f"'{p}'" for p in PORT_NAME)
    while True:
        r = requests.get(SOCRATA_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, params={
            "$select": "date,carrier_name,grain,destination,port,mt",
            "$where": f"year>={int(start_year)} and type_carrier_text='SHIP' and port in({ports})",
            "$limit": "50000", "$offset": str(off), "$order": "date"})
        r.raise_for_status()
        j = r.json()
        rows += j
        if len(j) < 50000:
            break
        off += 50000
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["date", "vessel", "grain", "commodity", "destination", "port", "kmt"])
    df["date"] = pd.to_datetime(df["date"])
    df["kmt"] = df["mt"].astype(float) / 1000.0
    df["vessel"] = df["carrier_name"].astype(str).str.strip()
    df["commodity"] = df["grain"].map(GRAIN).fillna("Other")
    return df[["date", "vessel", "grain", "commodity", "destination", "port", "kmt"]]


def lake_events(ships, cfg=None):
    """FGIS lake ship rows -> sail events (see events.EVENT_COLS + vessel_w), region 'Great Lakes'."""
    cfg = cfg or default_config()
    if ships.empty:
        return pd.DataFrame(columns=ev.EVENT_COLS + ["vessel_w"])
    s = ships.copy()
    s["port_raw"] = s["port"].map(PORT_NAME).fillna(s["port"].astype(str).str.title())
    s["berth"] = s["port_raw"]                         # no terminal is named; the port stands in
    s = add_geography(s, "Argentina", cfg)             # port-named geography (resolve_port); not Argentina-specific
    s["source"] = "US"
    s["sail_date"] = s["date"]
    s["_d"] = s["sail_date"].dt.strftime("%Y-%m-%d")
    s["combo"] = ""
    s["destination"] = s["destination"].astype(str).str.strip().str.upper().replace({"": "UNKNOWN", "NAN": "UNKNOWN"})
    # one event per vessel x week x grain x destination x port; identical repeats get an ordinal
    base = s["vessel"].astype(str) + "|" + s["_d"] + "|" + s["commodity"] + "|" + s["destination"] + "|" + s["port"].astype(str)
    ordinal = base.groupby(base).cumcount().astype(str)
    s["event_id"] = ["GL" + hashlib.sha1((b + "|" + o).encode()).hexdigest()[:18] for b, o in zip(base, ordinal)]
    s["vessel_w"] = ar._vessel_weights(s, ["vessel", "_d", "port"])
    s["fgis_port"] = ""                                # keep these out of the reconciliation against USDA (they ARE USDA)
    out = s[ev.EVENT_COLS].copy()
    out["vessel_w"] = s["vessel_w"].values
    return out.reset_index(drop=True)


def refresh(facts_db=None, start_year=2015, ships=None):
    """Re-read the lake inspections and upsert them into sail_events. -> {'events', 'first', 'last'}"""
    ships = fetch_lake_ships(start_year) if ships is None else ships
    e = lake_events(ships)
    n = store.save_events(e, facts_db)
    return {"events": int(n), "first": str(e["sail_date"].min().date()) if n else None, "last": str(e["sail_date"].max().date()) if n else None}
