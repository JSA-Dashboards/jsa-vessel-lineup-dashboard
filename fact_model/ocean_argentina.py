"""Argentina history from Ocean's daily "Arg Consolidated Line-up" PDF (jrolando@ocean.com.ar, Jun 2024 - Jul 2026).

One layout throughout (9 pages in 2024-25, 6 in 2026):
  page 1  EXPORT DAILY SUMMARY          totals only (port x product, monthly sailed totals) - not read, except the date
  p 2-4   LINE-UP                       Port | Berth | Vessels | Charterers | Status | ETA | ETB | ETC | WT | Destination | Product | MT
                                        a "Line-up pre product" table above it gives each product's total, used to validate the parse
  p 5-9   VESSELS SAILED <MONTH>-<YEAR> same columns, Status SLD; the month-to-date list (it restarts each month);
                                        a "TTL LDD MONTH-TO DATE" table above it gives the sailed totals by product
A vessel that loads several cargoes/destinations has one row per cargo; the extra rows leave the vessel cell empty.
Sail date = ETC (estimated completion of loading), the report's closest date to sailing - the same convention as
the Brazil (APS) events. Nabsa, which replaced this report in Oct 2026, gives actual sailing dates.

The 2026 PDFs list only San Lorenzo's sailed vessels although their month-to-date totals cover every port, so a
sailed list is loaded only when it adds up to those totals (sailed_complete); the line-up always ties out.

Cells are read in the PDF's content-stream order (it is cell by cell; sorting by x mixes overflowing text) and
split wherever the x position jumps; each cell is placed in a column by its first character.
"""

import re
from datetime import datetime

import numpy as np
import pandas as pd

from . import argentina as ar
from . import events as ev
from . import lineup as lu
from . import store
from .dimensions import add_geography, default_config

COLS = ["port", "berth", "vessel", "charterer", "status", "eta", "etb", "etc", "wt", "destination", "product", "mt"]
DEFAULT_STARTS = [24, 94, 164, 234, 304, 346, 378, 410, 441, 461, 495, 549]      # 2024 layout; the header row overrides it
DATE_RE = r"\d{2}-[A-Za-z]{3}-\d{2}"
ROW_COLS = ["section", "port", "berth", "vessel", "charterer", "status", "eta", "etb", "etc", "destination", "product", "mt"]

# Ocean product codes -> commodity groups (same groups as Nabsa); anything else is Other / Other Oils
PRODUCT_GROUP = {"MZ": "Corn", "MZ FLINT": "Corn", "SBS": "Soybeans", "SBM": "Soybean Meal", "WHT": "Wheat",
                 "SORGHUM": "Sorghum", "SBO": "Soybean Oil", "RSBO": "Soybean Oil", "NEUTRAL OIL": "Soybean Oil",
                 "SFO": "Other Oils", "SFO HO": "Other Oils", "SME": "Other Oils", "OLEIN": "Other Oils", "LCT": "Other Oils",
                 "ACID OIL": "Other Oils", "CORN OIL": "Other Oils", "SOY ACID OIL": "Other Oils"}


def product_group(code):
    c = re.sub(r"\s+", " ", str(code or "").upper()).strip()
    return PRODUCT_GROUP.get(c, "Other")


def _num(s):
    s = re.sub(r"[\s,]", "", str(s or ""))
    return float(s) if re.fullmatch(r"\d+(\.\d+)?", s) else np.nan


def _cells(chars, starts):
    """Split one line's characters (content-stream order) into cells; return {column index: text}."""
    cells, cur, prev = [], [], None
    for c in chars:
        if prev is not None and (c["x0"] < prev["x1"] - 0.6 or c["x0"] - prev["x1"] > 1.4):
            cells.append(cur)
            cur = []
        cur.append(c)
        prev = c
    if cur:
        cells.append(cur)
    out = {}
    for cell in cells:
        text = re.sub(r"\s+", " ", "".join(c["text"] for c in cell)).strip()
        if not text:
            continue
        x = cell[0]["x0"]
        k = max([i for i, s in enumerate(starts) if s <= x + 2.5] or [0])
        out[k] = (out[k] + " " + text) if k in out else text
    return out


def _lines(page):
    rows = {}
    for c in page.chars:
        rows.setdefault(round(c["top"] / 2.2), []).append(c)       # stream order is kept inside each line
    return [rows[k] for k in sorted(rows)]


def _header_starts(lines):
    for chars in lines:
        words = "".join(c["text"] for c in chars)
        if words.startswith("Port") and "Charterers" in words and words.rstrip().endswith("MT"):
            xs, cur, prev = [], [], None
            for c in chars:
                if prev is None or c["x0"] - prev["x1"] > 1.4 or c["x0"] < prev["x1"] - 0.6:
                    if cur:
                        xs.append(cur)
                    cur = []
                cur.append(c)
                prev = c
            xs.append(cur)
            starts = [cell[0]["x0"] for cell in xs]
            if len(starts) == len(COLS):
                return starts
    return None


def report_date(text):
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    return pd.Timestamp(datetime.strptime(m.group(1), "%B %d, %Y")) if m else None


def _split_tail(cells):
    """Destination, product and MT from the cells of the right-hand columns (product and MT often arrive as one cell)."""
    dest = cells.get(9, "")
    rest = " ".join(cells[k] for k in sorted(cells) if k >= 10)
    m = re.match(r"^(.*?)\s*(\d[\d,]*)$", rest)
    product, mt = (m.group(1).strip(), _num(m.group(2))) if m else (rest.strip(), np.nan)
    return dest, product, mt


def _product_table(text, start_prefix):
    """{code: MT} from the small product table that starts at the line beginning `start_prefix` and ends at the data header."""
    out, on = {}, False
    for ln in text.split("\n"):
        if ln.startswith(start_prefix):
            on = True
        elif on and ln.startswith("Port Berth"):
            break
        elif on:
            m = re.fullmatch(r"([A-Z][A-Z ]*?)\s+([\d ,]+)", ln.strip())
            if m and m.group(1) != "TOTAL":
                v = _num(m.group(2))
                if not np.isnan(v):
                    out[m.group(1).strip()] = v
    return out


def parse_pdf(path):
    """-> dict(report_date, lineup, sailed, expected, expected_sailed): lineup / sailed are row frames (one per cargo line);
    `expected` / `expected_sailed` are the report's own product totals ({code: MT}) for validation."""
    import pdfplumber
    rows, expected, expected_sailed, rd = [], {}, {}, None
    section, starts, ctx = None, None, {}
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            head = text.split("\n")[0].upper()
            if pno == 0:
                rd = report_date(text)
                continue
            lines = _lines(page)
            hs = _header_starts(lines)
            if "LINE-UP" in head and "SUMMARY" not in head:
                section, ctx = "lineup", {}
            elif "VESSELS SAILED" in head:
                section, ctx = "sailed", {}
            if hs:
                starts = hs
            if section == "lineup" and hs:
                expected.update(_product_table(text, "Line-up pre product"))
            elif section == "sailed" and hs:
                expected_sailed.update(_product_table(text, "TTL LDD"))
            st = starts or DEFAULT_STARTS
            past_header = hs is None                                   # continuation pages carry data from their first line
            for chars in lines:
                cells = _cells(chars, st)
                joined = " ".join(cells[k] for k in sorted(cells))
                if not past_header:
                    past_header = joined.startswith("Port Berth") and "Charterers" in joined
                    continue
                if section is None or not cells:
                    continue
                if cells.get(1, "").startswith("Total") and 0 in cells:   # 'BAHIA BLANCA Total ADM AGRO (BB) ...' page-top total row
                    ctx["port"] = cells[0]
                    continue
                if joined.startswith("Total"):
                    continue
                right = " ".join(cells[k] for k in sorted(cells) if 4 <= k <= 8)
                dates = re.findall(DATE_RE, right)
                if len(dates) >= 3 and 2 in cells:                      # a new vessel line
                    if 0 in cells:
                        ctx["port"] = cells[0]
                    if 1 in cells:
                        ctx["berth"] = cells[1]
                    status = right.split(dates[0])[0].strip()
                    ctx.update(vessel=cells[2], charterer=cells.get(3, ""), status=status, eta=dates[0], etb=dates[1], etc=dates[2])
                    ctx["destination"] = cells.get(9, "")
                elif "vessel" in ctx and (9 in cells or 10 in cells) and len(dates) == 0 and 2 not in cells:   # another cargo
                    if 9 in cells:
                        ctx["destination"] = cells[9]
                else:
                    continue
                dest, product, mt = _split_tail(cells)
                if not product:
                    continue
                rows.append({"section": section, "port": ctx.get("port", ""), "berth": ctx.get("berth", ""),
                             "vessel": ctx["vessel"], "charterer": ctx.get("charterer", ""), "status": ctx.get("status", ""),
                             "eta": ctx.get("eta"), "etb": ctx.get("etb"), "etc": ctx.get("etc"),
                             "destination": dest or ctx.get("destination", ""), "product": product, "mt": mt})
    df = pd.DataFrame(rows, columns=ROW_COLS)
    for c in ("eta", "etb", "etc"):
        df[c] = pd.to_datetime(df[c], format="%d-%b-%y", errors="coerce")
    return {"report_date": rd, "lineup": df[df["section"] == "lineup"].reset_index(drop=True),
            "sailed": df[df["section"] == "sailed"].reset_index(drop=True), "expected": expected,
            "expected_sailed": expected_sailed}


MAIN_PRODUCTS = ("MZ", "SBM", "SBS", "SBO", "WHT", "SORGHUM")


def validate(parsed, rel_tol=0.03, abs_tol=5000):
    """Compare parsed line-up tonnage by product with the report's own 'Line-up pre product' table, for the six products
    the dashboard uses (a stray missed row elsewhere does not matter). A missed row is typically 1-2% of a product.
    -> (ok, {product: (reported, parsed)} for products that disagree)"""
    got = parsed["lineup"].groupby(parsed["lineup"]["product"].str.upper())["mt"].sum()
    bad = {}
    for code, v in parsed["expected"].items():
        if code not in MAIN_PRODUCTS:
            continue
        g = float(got.get(code, 0.0))
        if abs(g - v) > max(abs_tol, rel_tol * v):
            bad[code] = (v, g)
    return (not bad), bad


def sailed_complete(parsed, rel_tol=0.03):
    """True when the parsed sailed list adds up to the report's own month-to-date totals for the main products. A partial
    list (the 2026 PDFs list only San Lorenzo) must not be loaded as if it were the whole month.
    -> (complete, {product: (reported, parsed)} for products that fall short)"""
    exp = {k: v for k, v in parsed.get("expected_sailed", {}).items() if k in ("MZ", "SBM", "SBS", "SBO", "WHT", "SORGHUM")}
    if not exp:
        return False, {}
    got = parsed["sailed"].groupby(parsed["sailed"]["product"].str.upper())["mt"].sum()
    short = {k: (v, float(got.get(k, 0.0))) for k, v in exp.items()
             if v > 20000 and abs(float(got.get(k, 0.0)) - v) > rel_tol * v}
    return (not short), short


# ── facts ────────────────────────────────────────────────────────────────────

def _fix_berth(s):
    s = re.sub(r"\bNORTE\b", "NORTH", str(s or ""))
    return re.sub(r"\bSUR\b", "SOUTH", s)


def _vessel_frame(df):
    d = df.copy()
    d["port_raw"] = d["port"]
    d["berth"] = d["berth"].map(_fix_berth)
    d["kmt"] = d["mt"] / 1000.0
    d["commodity_raw"] = d["product"]
    d["commodity_group"] = d["product"].map(product_group)
    return d


def lineup_snapshot(parsed, cfg=None):
    cfg = cfg or default_config()
    d = _vessel_frame(parsed["lineup"])
    if d.empty:
        return pd.DataFrame(columns=lu.FACT_COLS)
    q = add_geography(d, "Argentina", cfg)
    q["source"] = "Argentina"
    q["report_date"] = pd.Timestamp(parsed["report_date"])
    q["commodity"] = q["commodity_group"]
    q["combo"] = ""
    q["vessel"] = q["vessel"].fillna("?")
    q["_w"] = ar._vessel_weights(q, ["port", "vessel"])
    snap = lu._aggregate(q)
    qq = q.assign(destination=q["destination"].astype("string").str.strip().str.upper().replace({"": "UNKNOWN"}).fillna("UNKNOWN"))
    w = qq.groupby(lu.DIMS, dropna=False)["_w"].sum().reset_index().rename(columns={"_w": "_vw"})
    w["report_date"] = pd.to_datetime(w["report_date"])
    snap = snap.merge(w, on=lu.DIMS, how="left")
    snap["vessels"] = snap["_vw"]
    return snap[lu.FACT_COLS]


def sail_events(parsed, cfg=None):
    cfg = cfg or default_config()
    d = _vessel_frame(parsed["sailed"])
    d = d[d["etc"].notna()]
    if d.empty:
        return pd.DataFrame(columns=ev.EVENT_COLS + ["vessel_w"])
    d["sail_date"] = d["etc"]
    s = add_geography(d, "Argentina", cfg)
    s["commodity"] = s["commodity_group"]
    s["combo"] = ""
    s["_d"] = s["sail_date"].dt.strftime("%Y-%m-%d")
    s["event_id"] = "AO" + pd.Series(ev._event_ids(s, ["vessel", "_d", "commodity_raw", "destination"]), index=s.index).str[:18]
    s["kmt"] = s["kmt"].where(s["kmt"] > 0)
    s["vessel_w"] = ar._vessel_weights(s, ["vessel", "_d"])
    out = ev._finish(s, "Argentina", cfg)
    out["vessel_w"] = s["vessel_w"].values
    return out


def ingest_parsed(parsed, facts_db=None, cfg=None):
    """Bank one parsed report. The line-up snapshot is loaded only if it ties to the report's own product totals; the
    sailed events only if the sailed list is complete (see sailed_complete)."""
    ok, bad = validate(parsed)
    done, short = sailed_complete(parsed)
    out = {"report_date": str(pd.Timestamp(parsed["report_date"]).date()), "lineup_ok": ok, "lineup_mismatch": bad,
           "sailed_complete": done, "sailed_short": short, "queued_vessels": 0.0, "sail_events": 0}
    if ok and len(parsed["lineup"]):
        snap = lineup_snapshot(parsed, cfg)
        store.save_lineup_snapshot(snap, facts_db)
        out["queued_vessels"] = round(float(snap["vessels"].sum()), 1)
    if done:
        out["sail_events"] = store.save_events(sail_events(parsed, cfg), facts_db)
    return out


def ingest_pdf(path, facts_db=None, cfg=None):
    return ingest_parsed(parse_pdf(path), facts_db, cfg)
