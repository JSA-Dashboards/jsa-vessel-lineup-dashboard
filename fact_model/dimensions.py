"""Shared dimensions: region / port / elevator mapping, commodity group, marketing year.

Everything editable lives in fact_model/config/*.csv. Rows that cannot be mapped are
KEPT with region == UNMAPPED and mapped == False; nothing is ever dropped here.
"""

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

import pandas as pd

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
UNMAPPED = "UNMAPPED"


def norm_key(s):
    """Upper-case ASCII alphanumerics only. Drops U+FFFD, which the Brazil PDF parser
    leaves in place of accented characters (S?O FRANCISCO -> SOFRANCISCO)."""
    if s is None or (isinstance(s, float) and s != s):
        return ""
    s = unicodedata.normalize("NFKD", str(s)).replace("�", "")
    s = s.encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]", "", s)


@dataclass
class Config:
    regions: pd.DataFrame
    port_region: pd.DataFrame
    elevator_port: pd.DataFrame
    marketing_year: pd.DataFrame
    _port_lookup: dict
    _elev_lookup: dict
    _my_lookup: dict
    fgis_by_port: dict = None


def load_config(config_dir=None):
    d = config_dir or CONFIG_DIR
    rd = lambda n: pd.read_csv(os.path.join(d, n), dtype=str, keep_default_na=False)
    regions, port_region = rd("regions.csv"), rd("port_region.csv")
    elevator_port, my = rd("elevator_port.csv"), rd("marketing_year.csv")

    bad = set(port_region["region"]) - set(regions["region"])
    if bad:
        raise ValueError(f"port_region.csv uses regions not in regions.csv: {sorted(bad)}")
    bad = set(elevator_port["port"]) - set(port_region["port"])
    if bad:
        raise ValueError(f"elevator_port.csv uses ports not in port_region.csv: {sorted(bad)}")

    port_lookup = {}
    for r in port_region.itertuples():
        keys = [r.port] + [a for a in r.aliases.split("|") if a.strip()]
        for k in keys:
            port_lookup[norm_key(k)] = (r.port, r.region)
    port_region_by_name = dict(zip(port_region["port"], port_region["region"]))

    elev_lookup = {}
    for r in elevator_port.itertuples():
        elev_lookup[(r.source, norm_key(r.raw_name))] = (
            r.elevator, r.port, port_region_by_name[r.port], r.confidence)

    my_lookup = {r.commodity: (int(r.start_month), int(r.start_day)) for r in my.itertuples()}
    cfg = Config(regions, port_region, elevator_port, my, port_lookup, elev_lookup, my_lookup)
    cfg.fgis_by_port = {r.port: r.fgis_port for r in port_region.itertuples() if r.fgis_port}
    return cfg


_DEFAULT = None


def default_config():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load_config()
    return _DEFAULT


# ── Geography ────────────────────────────────────────────────────────────────

def resolve_port(raw_port, cfg=None):
    """Brazil-style: the report names the port. -> (port, region, mapped)."""
    cfg = cfg or default_config()
    hit = cfg._port_lookup.get(norm_key(raw_port))
    if hit:
        return hit[0], hit[1], True
    return (str(raw_port).strip() if str(raw_port).strip() else UNMAPPED), UNMAPPED, False


# The sheet a US row comes from already says which region it is in. An elevator we cannot place
# is therefore UNMAPPED as a PORT (flagged, never dropped) but still counts in its region.
SHEET_REGION = {"USG": "US Gulf", "PNW": "PNW", "TXG": "Texas Gulf"}


def resolve_us_elevator(sheet, raw_elevator, cfg=None):
    """US sheets name only the elevator. TXG cells are 'dock / company'; the dock
    (first token) is the facility. -> (elevator, port, region, mapped, reason)."""
    cfg = cfg or default_config()
    region = SHEET_REGION.get(sheet, UNMAPPED)
    raw = "" if pd.isna(raw_elevator) else str(raw_elevator).strip()
    token = raw.split(" / ")[0].strip() if sheet == "TXG" else raw
    if not token or token.lower() == "nan":
        return UNMAPPED, UNMAPPED, region, False, "no terminal named in report"
    hit = cfg._elev_lookup.get((sheet, norm_key(token)))
    if hit:
        return hit[0], hit[1], hit[2], True, ""
    return token, UNMAPPED, region, False, f"elevator not in elevator_port.csv ({sheet})"


def add_geography(df, source, cfg=None):
    """Add elevator / port / region / mapped / unmapped_reason to a vessel-level frame.

    source == 'US'     needs columns: sheet, elevator_raw
    source == 'Brazil' needs columns: port_raw, berth
    """
    cfg = cfg or default_config()
    df = df.copy()
    if source == "US":
        pairs = df[["sheet", "elevator_raw"]].drop_duplicates()
        res = {(s, e): resolve_us_elevator(s, e, cfg) for s, e in pairs.itertuples(index=False)}
        out = [res[(s, e)] for s, e in zip(df["sheet"], df["elevator_raw"])]
        df["elevator"] = [o[0] for o in out]
        df["port"] = [o[1] for o in out]
        df["region"] = [o[2] for o in out]
        df["mapped"] = [o[3] for o in out]
        df["unmapped_reason"] = [o[4] for o in out]
    elif source == "Brazil":
        res = {p: resolve_port(p, cfg) for p in df["port_raw"].drop_duplicates()}
        df["port"] = [res[p][0] for p in df["port_raw"]]
        df["region"] = [res[p][1] for p in df["port_raw"]]
        df["mapped"] = [res[p][2] for p in df["port_raw"]]
        df["unmapped_reason"] = [("" if res[p][2] else "port not in port_region.csv") for p in df["port_raw"]]
        df["elevator"] = df["berth"].fillna("").astype(str).str.strip().replace("", UNMAPPED)
    else:
        raise ValueError(f"unknown source {source!r}")
    return df


def unmapped_report(df, value_col="kmt"):
    """Distinct unmapped (source, sheet/port, elevator) with row counts and tonnage."""
    bad = df[~df["mapped"]]
    if bad.empty:
        return pd.DataFrame(columns=["source", "elevator", "port", "reason", "rows", value_col])
    g = (bad.groupby(["source", "elevator", "port", "unmapped_reason"], dropna=False)
            .agg(rows=("mapped", "size"), **{value_col: (value_col, "sum")})
            .reset_index().rename(columns={"unmapped_reason": "reason"})
            .sort_values(value_col, ascending=False))
    return g


# ── Commodity ────────────────────────────────────────────────────────────────
# Groups follow the existing app (_comm_us) except soybeans and soybean meal are separate:
# USDA gives them different marketing years (Sep 1 vs Oct 1) and FGIS counts beans only.
# Canola is Other (no USDA year on the FAS table).

def commodity_group(source, raw):
    if raw is None or (isinstance(raw, float) and raw != raw):
        return "Other"
    c = str(raw).strip().upper()
    if source == "Brazil":
        if any(t in c for t in ("SBMP", "HIPRO", "SPC")):
            return "Soybean Meal"
        if "SBS" in c:
            return "Soybeans"
        if c == "MZ" or c.endswith(" MZ"):
            return "Corn"
        if "SUG" in c:
            return "Sugar"
        if "DDGS" in c:
            return "Dist. Grains"
        if "WHEAT" in c:
            return "Wheat"
        return "Other"
    if "/" in c:
        return "Mixed Cargo"
    if c == "CORN" or c.startswith("CORN "):
        return "Corn"
    if any(x in c for x in ("SBM", "MEAL")):
        return "Soybean Meal"
    if any(x in c for x in ("YSB", "SOY")):
        return "Soybeans"
    if "WHT" in c or "WHEAT" in c:
        return "Wheat"
    if "SORGHUM" in c:
        return "Sorghum"
    if any(x in c for x in ("DDGS", "CGM", "CGFP", "GDDG")):
        return "Dist. Grains"
    if "RICE" in c:
        return "Rice"
    return "Other"


# ── Marketing year ───────────────────────────────────────────────────────────

def my_start(d, commodity, cfg=None):
    """Start date of the marketing year that contains d."""
    cfg = cfg or default_config()
    m, day = cfg._my_lookup.get(commodity, cfg._my_lookup["Other"])
    d = pd.Timestamp(d)
    start = pd.Timestamp(date(d.year, m, day))
    return start if d >= start else pd.Timestamp(date(d.year - 1, m, day))


def marketing_year_label(d, commodity, cfg=None):
    s = my_start(d, commodity, cfg)
    cfg = cfg or default_config()
    m, _ = cfg._my_lookup.get(commodity, cfg._my_lookup["Other"])
    return str(s.year) if m == 1 else f"{s.year}/{str(s.year + 1)[-2:]}"
