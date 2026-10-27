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


# ── backfill: put APS history on the Southport basis and load the dates Southport does not cover ──────────────────────────

REGIONS = ("US Gulf", "PNW")
PROVENANCE = "lineup_provenance"


def overlap_scales(aps_snaps, facts, max_lag_days=1):
    """Ratio APS / Southport per region on the days both exist (APS within `max_lag_days`), from vessel counts and tonnage:
    -> {region: (vessels_ratio, kmt_ratio, n_dates)}. APS lists fewer PNW boats than Southport (~0.84 vessels, ~0.92 kMT) and about
    the same on the Gulf (~1.03 / ~1.05), stable across quarters; these ratios are divided out of the backfilled history."""
    f = facts[(facts["source"] == "US") & facts["region"].isin(REGIONS)]
    fd = sorted(f["report_date"].dt.normalize().unique())
    sums = {r: [0.0, 0.0, 0.0, 0.0, 0] for r in REGIONS}
    for d in fd:
        near = [r for r in aps_snaps if abs((r - d).days) <= max_lag_days]
        if not near:
            continue
        snap = aps_snaps[min(near, key=lambda x: abs((x - d).days))]
        for reg in REGIONS:
            a, b = snap[snap["region"] == reg], f[(f["report_date"] == d) & (f["region"] == reg)]
            if b["vessels"].sum() > 0:
                s = sums[reg]
                s[0] += a["vessels"].sum(); s[1] += b["vessels"].sum(); s[2] += a["kmt"].sum(); s[3] += b["kmt"].sum(); s[4] += 1
    return {r: (s[0] / s[1], s[2] / s[3], int(s[4])) for r, s in sums.items() if s[4] and s[1] and s[3]}


def scale_snapshot(snap, scales):
    """Divide each region's vessels and kMT by its APS/Southport ratio, so APS history reads on the Southport basis."""
    out = snap.copy()
    for col in ("vessels", "kmt", "vessels_no_tonnage"):
        if col in out:
            out[col] = out[col].astype(float)
    for reg, (rv, rk, _) in scales.items():
        m = out["region"] == reg
        out.loc[m, "vessels"] = out.loc[m, "vessels"] / rv
        out.loc[m, "kmt"] = out.loc[m, "kmt"] / rk
        if "vessels_no_tonnage" in out:
            out.loc[m, "vessels_no_tonnage"] = out.loc[m, "vessels_no_tonnage"] / rv
    return out


def backfill(folder, facts_db=None, since="2023-09-01", skip_within_days=2, cfg=None, dry_run=False):
    """Load APS line-up history into facts.db for every date Southport does not cover (no Southport snapshot within
    `skip_within_days`), scaled to the Southport basis, and record which dates came from APS in the `lineup_provenance` table.
    Idempotent. -> summary dict."""
    import glob
    import os
    from . import store
    facts = store.load_lineup(facts_db)
    prov = store.load_table(PROVENANCE, facts_db, ["report_date"])
    aps_loaded = set(pd.to_datetime(prov["report_date"]).dt.normalize()) if len(prov) else set()
    us = facts[facts["source"] == "US"]                          # coverage = US snapshots only (Brazil/Argentina dates are irrelevant)
    southport = sorted(set(us["report_date"].dt.normalize().unique()) - aps_loaded)
    snaps, errors = {}, 0
    for f in sorted(glob.glob(os.path.join(folder, "*.xls*"))):
        fd = pd.Timestamp(os.path.basename(f)[:10])
        if fd < pd.Timestamp(since):
            continue
        try:
            rd, snap = snapshot(f, fd, cfg)
        except Exception:
            errors += 1
            continue
        if len(snap):
            snaps[rd] = snap
    scales = overlap_scales(snaps, us[us["report_date"].dt.normalize().isin(southport)])
    to_load = [d for d in sorted(snaps) if d not in aps_loaded and not any(abs((d - s).days) <= skip_within_days for s in southport)]
    summary = {"aps_dates": len(snaps), "to_load": len(to_load), "already_loaded": len(aps_loaded), "errors": errors,
               "scales": {r: (round(v[0], 3), round(v[1], 3), v[2]) for r, v in scales.items()},
               "first": str(to_load[0].date()) if to_load else None, "last": str(to_load[-1].date()) if to_load else None}
    if dry_run or not to_load:
        return summary
    if set(scales) != set(REGIONS):
        raise RuntimeError(f"cannot scale APS to Southport: overlap found for {sorted(scales)} only")
    rows = []
    for d in to_load:
        s = scale_snapshot(snaps[d], scales)
        store.save_lineup_snapshot(s, facts_db)
        rows.append({"report_date": d, "source": "US", "provider": "APS", "regions_covered": ",".join(REGIONS),
                     "scale_vessels_pnw": scales["PNW"][0], "scale_kmt_pnw": scales["PNW"][1],
                     "scale_vessels_usg": scales["US Gulf"][0], "scale_kmt_usg": scales["US Gulf"][1], "overlap_dates": scales["PNW"][2]})
    new = pd.DataFrame(rows)
    store.save_table(pd.concat([prov, new], ignore_index=True) if len(prov) else new, PROVENANCE, facts_db)
    return summary
