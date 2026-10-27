"""History from Agri Port Services' daily "Grain Elevator Line-ups" workbook (Mississippi River + Columbia River + Puget Sound).

APS emails this unofficial line-up (sjenkins@agriportservices.com); it is the lineage Southport's cleaner workbook comes from, and it
goes back to Sept 2023, before our Southport history starts (Jan 2025). Layout, per sheet (elevator blocks, not a table):

  Miss River      Elevator | Vessel Name | ETA | ETB | ETC | Status | Cargo | [Tons] | Destination     cargo like "YC SBM", tons "50K"
  Columbia River  same, cargo like "66K WHEAT" / "62K YC/WHEAT"                                         (anchorage table to the right)
  Puget Sound     Elevator | Vessel Name | ETA | Status | ETB | ETD | Cargo | Destination             cargo like "65,000 MT CORN"
The elevator is named only on the first vessel of its block. Vessels whose cargo is 'REVERTING' have no tonnage/commodity (Southport's
RVT). Output rows are shaped like lineup.read_southport_vessels(): sheet USG (Miss River) / PNW (Columbia + Puget), so the existing
us_lineup_snapshot() turns them into facts.

Only the line-up is read: these workbooks carry no sailed list before 2025, and Vancouver / Prince Rupert / Norfolk are empty templates.
"""

import re
import warnings

import numpy as np
import pandas as pd

from . import lineup as lu

SHEETS = {"Miss River": "USG", "Columbia River": "PNW", "Puget Sound": "PNW"}
NOT_VESSELS = {"IDLE", "IDLE-MAINT.", "IDLE - MAINT.", "NONE", "NONE LOADING", "TBN", "TBC", "REVERTING", "N/A", "NO VESSEL", "LINE UP CURRENT"}
TOKEN = {"YC": "CORN", "CORN": "CORN", "YSB": "YSB", "SBS": "YSB", "SOYBEANS": "YSB", "SOYBEAN": "YSB", "SOY": "YSB", "SBM": "SBM",
         "MEAL": "SBM", "WHT": "WHT", "WHEAT": "WHT", "SRW": "WHT", "HRW": "WHT", "HRS": "WHT", "NSW": "WHT", "SWW": "WHT", "DNS": "WHT",
         "SORGHUM": "SORGHUM", "SORG": "SORGHUM", "SOR": "SORGHUM", "MILO": "SORGHUM", "DDG": "DDGS", "DDGS": "DDGS", "GDDG": "GDDG",
         "GDDGS": "GDDG", "CGM": "CGM", "CGFP": "CGFP", "CGF": "CGFP", "BY-PROD": "BY-PROD", "RICE": "RICE", "SHP": "SHP", "LGRR": "LGRR"}
# APS names its elevators differently from Southport; map them (by APS sheet) onto the names in elevator_port.csv
ELEV_ALIAS = {
    "Miss River": {"CHS MYRTLE GROVE": "CHS", "LDC": "LDC PORT ALLEN", "ADM-BUOYS": "ADM WOOD BUOYS", "ADM BUOYS": "ADM WOOD BUOYS",
                   "ZENNOH": "ZEN-NOH", "FILED": None},
    "Columbia River": {"TEMCO": "TEMCO KALAMA", "KEC (KALAMA)": "KALAMA EXPORT", "KEC": "KALAMA EXPORT", "EGT LONGVIEW": "EGT",
                       "CET": "COLUMBIA EXPORT", "UGC": "UNITED GRAIN", "LINE UP CURRENT": None},
    "Puget Sound": {"LOUIS DRYFUS": "LDC SEATTLE", "AGP-GRAYS HARBOR": "GRAYS HARBOR", "TEMCO": "TEMCO TACOMA"},
}
IGNORE = {"MT", "K", "OF", "AND", "PLUS", "APPX", "APPROX", "EST", "TBD", "TBC"}


def parse_cargo(cargo, tons=None):
    """-> (kmt or NaN, commodity string like 'CORN/SBM', rvt flag). Tonnage may sit in the cargo cell ('38K WHT/BY-PROD',
    '65,000 MT CORN') or in its own Tons cell ('50K')."""
    text = "" if cargo is None or (isinstance(cargo, float) and np.isnan(cargo)) else str(cargo).upper().strip()
    if "REVERT" in text or text in ("RVT", "TBD", "TBC") and not tons:
        return np.nan, "RVT", True
    kmt = np.nan
    for src in (tons, text):
        if src is None or (isinstance(src, float) and np.isnan(src)):
            continue
        m = re.match(r"\s*([\d][\d,]*\.?\d*)\s*(K|MT|KMT)?\b", str(src).upper())
        if m:
            v = float(m.group(1).replace(",", ""))
            unit = m.group(2) or ""
            kmt = v / 1000.0 if (unit == "MT" or (unit == "" and v >= 1000)) else v          # '65,000 MT' -> 65; '50K' -> 50
            break
    body = re.sub(r"^\s*[\d][\d,]*\.?\d*\s*(K|MT|KMT)?\b", "", text)
    toks = [TOKEN.get(t, t) for t in re.split(r"[\s/,&+()]+", body) if t and t not in IGNORE]
    toks = [t for t in toks if re.fullmatch(r"[A-Z][A-Z\-]*", t)]
    seen = list(dict.fromkeys(toks))
    return kmt, "/".join(seen) if seen else "RVT", False


def _headers(row):
    cols = {}
    for j, v in enumerate(row):
        s = str(v).strip().upper() if str(v) not in ("nan", "NaT") else ""
        if s in ("VESSEL NAME", "VESSEL"):
            cols["vessel"] = j
        elif s == "STATUS":
            cols["status"] = j
        elif s == "CARGO":
            cols["cargo"] = j
        elif s == "TONS":
            cols["tons"] = j
        elif s == "DESTINATION":
            cols["dest"] = j
        elif s.startswith("ETA"):
            cols.setdefault("eta", j)
    return cols


def parse_sheet(df, region_sheet, src_sheet=None):
    """One APS sheet -> vessel rows [sheet, elevator_raw, vessel, status_norm, kmt, commodity_raw, destination, sail_date]."""
    alias = ELEV_ALIAS.get(src_sheet, {})
    out, cols, elevator = [], None, ""
    for _, r in df.iterrows():
        vals = list(r)
        h = _headers(vals)
        if "vessel" in h and "cargo" in h:
            cols, elevator = h, ""                           # a header row (Puget repeats it per block)
            continue
        if cols is None or all(str(v) in ("nan", "NaT", "") for v in vals):
            continue
        first = vals[0]
        if isinstance(first, str) and first.strip():
            elevator = first.strip()
            elevator = alias[elevator.upper()] if elevator.upper() in alias else elevator
            elevator = elevator or ""
        v = vals[cols["vessel"]] if cols["vessel"] < len(vals) else None
        if not isinstance(v, str) or not v.strip():
            continue
        name = re.sub(r"\s+", " ", v.strip())
        if name.upper() in NOT_VESSELS or name.upper().startswith(("IDLE", "NONE")):
            continue
        cargo = vals[cols["cargo"]] if "cargo" in cols else None
        tons = vals[cols["tons"]] if "tons" in cols and cols["tons"] < len(vals) else None
        dest = vals[cols["dest"]] if "dest" in cols and cols["dest"] < len(vals) else None
        status = vals[cols["status"]] if "status" in cols and cols["status"] < len(vals) else None
        kmt, comm, rvt = parse_cargo(cargo, tons)
        if str(dest).upper().strip() in ("REVERTING", "NAN", "NONE"):
            dest = "RVT"
        out.append({"sheet": region_sheet, "elevator_raw": elevator or None, "vessel": name,
                    "status_norm": "Loading/In Port" if str(status).upper().strip() in ("LOADING", "IN PORT", "PART-LDD") else "ETA",
                    "kmt": kmt if not rvt else np.nan, "commodity_raw": comm, "destination": None if str(dest) == "nan" else dest,
                    "sail_date": pd.NaT})
    return pd.DataFrame(out, columns=["sheet", "elevator_raw", "vessel", "status_norm", "kmt", "commodity_raw", "destination", "sail_date"])


def report_date_of(df, fallback):
    """The workbook's own date: the first date in the 5-day weather table / 'Date:' cell; else the file's date."""
    for v in df.head(14).stack().tolist():
        if isinstance(v, (pd.Timestamp, np.datetime64)) or hasattr(v, "year"):
            try:
                t = pd.Timestamp(v)
                if t.year >= 2020:
                    return t.normalize()
            except Exception:
                pass
    return pd.Timestamp(fallback)


def read_workbook(path, fallback_date):
    """-> (report_date, vessel-level DataFrame for USG and PNW). Sheets that are missing or unreadable are skipped."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        x = pd.ExcelFile(path)
        frames, rd = [], None
        for sheet, region in SHEETS.items():
            if sheet not in x.sheet_names:
                continue
            df = x.parse(sheet, header=None)
            if rd is None:
                rd = report_date_of(df, fallback_date)
            frames.append(parse_sheet(df, region, sheet))
    v = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["sheet", "elevator_raw", "vessel", "status_norm", "kmt", "commodity_raw", "destination", "sail_date"])
    return (rd if rd is not None else pd.Timestamp(fallback_date)), v


def snapshot(path, fallback_date, cfg=None):
    """APS workbook -> (report_date, lineup_fact rows for USG + PNW)."""
    rd, v = read_workbook(path, fallback_date)
    return rd, lu.us_lineup_snapshot(v, rd, cfg)
