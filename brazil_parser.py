"""
brazil_parser.py  –  parse APS Brazil consolidated line-up PDFs.

Usage (CLI test):
    python brazil_parser.py "path/to/APS Brz Consolidated line-up 2026.10.06.pdf"
"""

import pdfplumber
from datetime import datetime, date
from collections import defaultdict
import sys


# ---------------------------------------------------------------------------
# Column definitions (name, nominal x0)
# ---------------------------------------------------------------------------
LINEUP_COLS = [
    ("port",        19.8),
    ("berth",       81.7),
    ("vessel",      130.5),
    ("charterer",   195.7),
    ("status",      251.1),
    ("eta",         286.7),
    ("etb",         310.5),
    ("etcs",        339.8),
    ("wt",          374.6),
    ("destination", 388.1),
    ("agent",       450.1),
    ("product",     499.1),
    ("mt",          556.5),
]

SAILED_COLS = [
    ("port",        19.8),
    ("berth",       80.5),
    ("vessel",      142.7),
    ("charterer",   206.6),
    ("status",      260.9),
    ("eta",         289.7),
    ("etb",         317.6),
    ("etcs",        346.8),
    ("wt",          381.4),
    ("destination", 394.8),
    ("agent",       455.5),
    ("product",     494.9),
    ("mt",          550.9),
]

# Header-row x0 of each column in the layout the nominal positions above were measured on.
# Other report vintages shift the columns by a few points; the shift seen in the header row
# is applied to the nominal data positions.
_HEADER_NAMES = {"Port": "port", "Berth": "berth", "Vessels": "vessel", "Charterers": "charterer",
                 "Status": "status", "ETA": "eta", "ETB": "etb", "ETC/S": "etcs", "WT": "wt",
                 "Destination": "destination", "Agents": "agent", "Product": "product", "MT": "mt"}
LINEUP_HDR_REF = {"port": 19.8, "berth": 81.7, "vessel": 130.6, "charterer": 195.8, "status": 251.2,
                  "eta": 286.7, "etb": 316.1, "etcs": 342.9, "wt": 371.9, "destination": 388.3,
                  "agent": 450.2, "product": 499.1, "mt": 563.4}
SAILED_HDR_REF = {"port": 19.8, "berth": 80.5, "vessel": 142.8, "charterer": 206.7, "status": 261.1,
                  "eta": 295.9, "etb": 324.3, "etcs": 350.1, "wt": 378.7, "destination": 394.9,
                  "agent": 455.6, "product": 494.9, "mt": 557.7}

CORE_PRODUCTS = {"SBS", "MZ", "SBMP", "HIPRO", "SPC", "RAW SUG", "DDGS"}

VALID_STATUSES = {"LDG", "WTG", "ETA", "SLD"}

_NULL_VALS = {"", "?", "-", "- "}

_MONTH_NAMES = {
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
}


# ---------------------------------------------------------------------------
# Column bucket helpers
# ---------------------------------------------------------------------------

def build_col_boundaries(col_defs):
    """Return list of (col_name, lo_x, hi_x) covering the full page width."""
    xs = [c[1] for c in col_defs]
    result = []
    for i, (name, x) in enumerate(col_defs):
        lo = 0 if i == 0 else (xs[i - 1] + x) / 2.0
        hi = 9999 if i == len(col_defs) - 1 else (x + xs[i + 1]) / 2.0
        result.append((name, lo, hi))
    return result


def adapt_cols(col_defs, header_words, hdr_ref):
    """Shift the nominal column positions by how far this page's header row has moved from
    the reference layout. Falls back to the nominal positions if the header is incomplete."""
    found = {_HEADER_NAMES[w["text"]]: w["x0"] for w in header_words if w["text"] in _HEADER_NAMES}
    if set(found) != set(hdr_ref):
        return col_defs
    shifted = [(name, x + (found[name] - hdr_ref[name])) for name, x in col_defs]
    xs = [x for _, x in shifted]
    return shifted if xs == sorted(xs) else col_defs


def _page_section(words):
    """'lineup' / 'sailed' from the page footer (every page carries 'Line-up' or 'Sailed' at
    the bottom), falling back to the 'VESSELS SAILED' / 'LINE-UP' title. None if neither."""
    foot = {w["text"] for w in words if w["top"] > 780}
    if "Sailed" in foot:
        return "sailed"
    if "Line-up" in foot:
        return "lineup"
    title = {w["text"].upper() for w in words if 90 < w["top"] < 135}
    if "SAILED" in title:
        return "sailed"
    if "LINE-UP" in title:
        return "lineup"
    return None


def _header_words(rows):
    for top, row_words in rows:
        texts = {w["text"] for w in row_words}
        if "Port" in texts and ("Berth" in texts or "Vessels" in texts):
            return row_words
    return None


def assign_col(x0, col_boundaries):
    for name, lo, hi in col_boundaries:
        if lo <= x0 < hi:
            return name
    return None


def get_col_text(row_words, col_boundaries, col_name, sep=" "):
    tokens = [
        w["text"]
        for w in row_words
        if assign_col(w["x0"], col_boundaries) == col_name
    ]
    return sep.join(tokens)


# ---------------------------------------------------------------------------
# Row grouping
# ---------------------------------------------------------------------------

def group_rows_by_top(words, y_tol=3.5):
    """Cluster words into rows; return [(top, [words]), …] sorted by top."""
    if not words:
        return []
    buckets = {}  # representative_top -> list of words
    for w in words:
        t = w["top"]
        matched = None
        for rep in buckets:
            if abs(rep - t) <= y_tol:
                matched = rep
                break
        if matched is None:
            buckets[t] = []
            matched = t
        buckets[matched].append(w)
    return [
        (top, sorted(ws, key=lambda w: w["x0"]))
        for top, ws in sorted(buckets.items())
    ]


# ---------------------------------------------------------------------------
# Value parsers
# ---------------------------------------------------------------------------

def parse_mt(s):
    """'63.000' → 63000; '5.118.476' → 5118476. Strips ALL dots."""
    s = (s or "").strip()
    if s in _NULL_VALS:
        return 0
    cleaned = s.replace(".", "").replace(",", "")
    if not cleaned or cleaned == "-":
        return 0
    try:
        return int(cleaned)
    except ValueError:
        try:
            return int(float(cleaned))
        except Exception:
            return 0


def parse_date(s):
    """'07-Oct-26' → date(2026, 10, 7). Returns None for null-ish values."""
    s = (s or "").strip()
    if s in _NULL_VALS:
        return None
    try:
        return datetime.strptime(s, "%d-%b-%y").date()
    except ValueError:
        return None


def null_str(s):
    """Return None if value is empty/dash/question, else strip."""
    s = (s or "").strip()
    return None if s in _NULL_VALS else s


def parse_wt(s):
    s = (s or "").strip()
    if not s:
        return None
    digits = s.lstrip("-")
    if digits.isdigit():
        return int(s) if s.lstrip("-") else None
    return None


def extract_status(status_text):
    """
    Return a clean status code from the raw status-column text.

    The PDF sometimes merges a charterer name and the status into one token
    (e.g. 'INTERGRAINLDG' → 'LDG').  Try known codes at the end first.
    Returns the original string if no valid code is found (caller may use
    date-based inference as a fallback).
    """
    s = (status_text or "").strip()
    if s in VALID_STATUSES:
        return s
    # Scan space-delimited tokens
    for token in s.split():
        if token in VALID_STATUSES:
            return token
    # Check if the string ends with a known code
    for code in sorted(VALID_STATUSES, key=len, reverse=True):
        if s.upper().endswith(code):
            return code
    # Try last 3 chars with case normalisation
    if len(s) >= 3 and s[-3:].upper() in VALID_STATUSES:
        return s[-3:].upper()
    return s  # fallback – caller will use date inference


def infer_status_from_dates(eta, etb, report_date):
    """
    Fallback status inference used when the status column text is garbled.
    Logic:  etb <= today → LDG;  eta <= today → WTG;  eta > today → ETA.
    """
    today = report_date or date.today()
    if etb is not None and etb <= today:
        return "LDG"
    if eta is not None and eta <= today:
        return "WTG"
    if eta is not None:
        return "ETA"
    return None


# ---------------------------------------------------------------------------
# Report date extraction
# ---------------------------------------------------------------------------

def parse_report_date(words):
    """
    Find 'October 06, 2026' near the top-right of a page (x>500, top<135).
    Returns date or None.
    """
    for w in words:
        if w["x0"] > 500 and w["top"] < 135 and w["text"] in _MONTH_NAMES:
            same_row = sorted(
                [w2 for w2 in words if abs(w2["top"] - w["top"]) < 3 and w2["x0"] > 490],
                key=lambda w2: w2["x0"],
            )
            date_str = " ".join(t["text"] for t in same_row).replace(",", "").strip()
            for fmt in ("%B %d %Y", "%B %d, %Y"):
                try:
                    return datetime.strptime(date_str, fmt).date()
                except ValueError:
                    pass
    return None


# ---------------------------------------------------------------------------
# Summary box extraction
# ---------------------------------------------------------------------------

def _parse_summary_rows(rows, header_top, label_x_max=80, mt_x_min=80):
    """Generic: extract product→MT dict from a product-summary box above the header."""
    result = {}
    in_section = False
    for top, row_words in rows:
        if top >= header_top:
            break
        texts = [w["text"] for w in row_words]
        row_text = " ".join(texts)
        # Start markers
        if not in_section:
            if "Line-up" in row_text or "TTL" in row_text or "MONTH" in row_text:
                in_section = True
            continue
        if not texts:
            continue
        prod_toks = [w["text"] for w in row_words if w["x0"] < label_x_max]
        mt_toks = [w["text"] for w in row_words if w["x0"] >= mt_x_min]
        prod = " ".join(prod_toks).strip().upper()
        mt_str = "".join(mt_toks).strip()
        if prod in ("TOTAL", ""):
            if prod == "TOTAL":
                break
            continue
        if prod and mt_str:
            result[prod] = parse_mt(mt_str)
    return result


def extract_lineup_summary(rows, header_top):
    return _parse_summary_rows(rows, header_top, label_x_max=80, mt_x_min=80)


def extract_sailed_summary(rows, header_top):
    return _parse_summary_rows(rows, header_top, label_x_max=80, mt_x_min=80)


# ---------------------------------------------------------------------------
# Page parser
# ---------------------------------------------------------------------------

def _find_header_top(rows):
    """Return top y of the header row ('Port' + 'Berth'/'Vessels'), or None."""
    for top, row_words in rows:
        texts = {w["text"] for w in row_words}
        if "Port" in texts and ("Berth" in texts or "Vessels" in texts):
            return top
    return None


def _is_total_row(texts_set):
    return "Total" in texts_set or "TOTAL" in texts_set


def parse_page_rows(rows, col_boundaries, report_date, always_data=False):
    """
    Parse vessel/continuation rows from a pre-grouped rows list.

    Returns:
        records        – list of row dicts
        port_totals    – {port: int}  (non-excluded sum per port from PDF totals)
        grand_total_pdf – int or None
    """
    records = []
    port_totals = {}
    grand_total_pdf = None

    current_port = None
    current_berth = None
    prev_record = None

    in_data = always_data

    for top, row_words in rows:
        texts = [w["text"] for w in row_words]
        texts_set = set(texts)

        # Detect and activate on header row
        if "Port" in texts_set and ("Berth" in texts_set or "Vessels" in texts_set):
            in_data = True
            continue

        if not in_data:
            # Skip page-header content (title, note, email address)
            # For always_data pages, only skip very top rows
            if always_data and top < 50:
                continue
            elif not always_data:
                continue

        # Skip rows with no useful content
        if not row_words:
            continue

        # ---- Assign columns ----
        port_text  = get_col_text(row_words, col_boundaries, "port")
        berth_text = get_col_text(row_words, col_boundaries, "berth")
        vessel_text = get_col_text(row_words, col_boundaries, "vessel")
        charterer_text = get_col_text(row_words, col_boundaries, "charterer")
        status_text = get_col_text(row_words, col_boundaries, "status")
        eta_text = get_col_text(row_words, col_boundaries, "eta")
        etb_text = get_col_text(row_words, col_boundaries, "etb")
        etcs_text = get_col_text(row_words, col_boundaries, "etcs")
        wt_text = get_col_text(row_words, col_boundaries, "wt")
        dest_text = get_col_text(row_words, col_boundaries, "destination")
        agent_text = get_col_text(row_words, col_boundaries, "agent")
        product_text = get_col_text(row_words, col_boundaries, "product")
        # MT: concatenate without space to handle split numbers like "2" + ".890.252"
        mt_text = get_col_text(row_words, col_boundaries, "mt", sep="")

        # A cargo row is any row with a readable date and a tonnage. When its status cell is
        # unreadable (older reports run the charterer and status together, sometimes interleaved
        # as FOUNDATIOENTA), take a status code from the end of the charterer, else infer it
        # from the dates, so the tonnage is not lost.
        cargo_dates = any(parse_date(t) is not None for t in (eta_text, etb_text, etcs_text))
        if (not null_str(status_text) and cargo_dates and parse_mt(mt_text) > 0
                and not _is_total_row(texts_set)):
            ch, code = charterer_text.strip(), None
            for c in sorted(VALID_STATUSES, key=len, reverse=True):
                if ch.upper().endswith(c) and len(ch) > len(c):
                    code, charterer_text = c, ch[: -len(c)].rstrip(" /-")
                    break
            status_text = code or infer_status_from_dates(
                parse_date(eta_text), parse_date(etb_text), report_date) or ""

        # ---- Total rows ----
        if _is_total_row(texts_set):
            mt_val = parse_mt(mt_text or "".join(
                w["text"] for w in row_words if w["x0"] > 400
            ))
            if "Grand" in texts_set or "Geral" in texts_set:
                grand_total_pdf = mt_val
            else:
                # Port total: port column has a name
                pt = null_str(port_text)
                if pt and current_port:
                    port_totals[current_port] = mt_val
            continue

        # Berth-only "Total" row (e.g., "Total" at berth x with MT)
        bt = berth_text.strip()
        if bt == "Total":
            continue

        # ---- Port and berth forwarding ----
        pt = null_str(port_text)
        if pt:
            current_port = pt

        bt2 = null_str(berth_text)
        if bt2:
            current_berth = bt2

        # ---- Row classification ----
        has_vessel = bool(null_str(vessel_text))
        has_status = bool(null_str(status_text))
        has_product = bool(null_str(product_text))

        # A second cargo line can carry status/dates/product/MT but no vessel name. It belongs to
        # the vessel above when the dates match; otherwise it is kept as UNNAMED so its tonnage
        # still reaches the totals.
        if (not has_vessel and has_status and prev_record is not None
                and parse_mt(mt_text) > 0 and cargo_dates):
            same = (parse_date(eta_text) == prev_record["eta"] and parse_date(etcs_text) == prev_record["etcs"])
            vessel_text = prev_record["vessel"] if same else "UNNAMED"
            has_vessel = True

        # VESSEL ROW
        if has_vessel and has_status:
            is_maintenance = "MAINTENANCE" in vessel_text.upper()

            charterer_val = null_str(charterer_text)
            dest_val = null_str(dest_text)
            agent_val = null_str(agent_text)
            product_norm = null_str(product_text)
            if product_norm:
                product_norm = product_norm.upper()

            pdf_mt = parse_mt(mt_text)
            mt_val = 0 if is_maintenance else pdf_mt
            excluded = is_maintenance or (product_norm == "ORANGE JUICE" and mt_val == 0)

            clean_status = extract_status(status_text)
            eta_d = parse_date(eta_text)
            etb_d = parse_date(etb_text)
            etcs_d = parse_date(etcs_text)
            if clean_status not in VALID_STATUSES:
                inferred = infer_status_from_dates(eta_d, etb_d, report_date)
                if inferred:
                    clean_status = inferred

            record = {
                "report_date": report_date,
                "port": current_port,
                "berth": current_berth,
                "vessel": vessel_text.strip(),
                "charterer": charterer_val,
                "status": clean_status,
                "eta": eta_d,
                "etb": etb_d,
                "etcs": etcs_d,
                "wt_days": parse_wt(wt_text),
                "destination": dest_val,
                "agent": agent_val,
                "product": product_norm,
                "mt": mt_val,
                "pdf_mt": pdf_mt,          # tonnage as printed, even where we exclude the row
                "lot": None,
                "core_product": product_norm in CORE_PRODUCTS if product_norm else False,
                "excluded": excluded,
            }
            records.append(record)
            prev_record = record

        # CONTINUATION ROW (split cargo)
        elif (
            has_product
            and not has_vessel
            and not has_status
            and not null_str(berth_text)
            and not null_str(port_text)
            and prev_record is not None
        ):
            product_norm = null_str(product_text)
            if product_norm:
                product_norm = product_norm.upper()
            mt_val = parse_mt(mt_text)
            excluded = product_norm == "ORANGE JUICE" and mt_val == 0

            record = {
                "report_date": prev_record["report_date"],
                "port": prev_record["port"],
                "berth": prev_record["berth"],
                "vessel": prev_record["vessel"],
                "charterer": prev_record["charterer"],
                "status": prev_record["status"],
                "eta": prev_record["eta"],
                "etb": prev_record["etb"],
                "etcs": prev_record["etcs"],
                "wt_days": prev_record["wt_days"],
                "destination": prev_record["destination"],
                "agent": prev_record["agent"],
                "product": product_norm,
                "mt": mt_val,
                "pdf_mt": mt_val,
                "lot": None,
                "core_product": product_norm in CORE_PRODUCTS if product_norm else False,
                "excluded": excluded,
            }
            records.append(record)

    return records, port_totals, grand_total_pdf


# ---------------------------------------------------------------------------
# Top-level parse
# ---------------------------------------------------------------------------

def parse_pdf(pdf_path) -> dict:
    """
    Parse an APS Brazil consolidated line-up PDF.

    Returns dict with keys:
        report_date, lineup, sailed, summary, validation
    """
    report_date = None
    all_lineup = []
    all_sailed = []
    lineup_summary = {}
    sailed_summary = {}
    lineup_port_totals = {}
    sailed_port_totals = {}
    lineup_grand_total_pdf = None
    sailed_grand_total_pdf = None

    with pdfplumber.open(pdf_path) as pdf:
        # ---- Report date (scan first 2 pages) ----
        for pg in pdf.pages[:2]:
            words = pg.extract_words(x_tolerance=3, y_tolerance=3)
            rd = parse_report_date(words)
            if rd:
                report_date = rd
                break

        # ---- Process pages ----
        # The section (line-up vs sailed) comes from each page's title, not its position:
        # older reports start the sailed section on page 6, current ones on page 7.
        section = "lineup"
        cols = {"lineup": LINEUP_COLS, "sailed": SAILED_COLS}
        hdr_ref = {"lineup": LINEUP_HDR_REF, "sailed": SAILED_HDR_REF}
        seen_header = {"lineup": False, "sailed": False}
        for page_idx, page in enumerate(pdf.pages):
            if page_idx == 0:      # page 1 = summary only; no vessel rows
                continue

            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            section = _page_section(words) or section
            is_sailed = section == "sailed"

            rows = group_rows_by_top(words)
            header_top = _find_header_top(rows)
            always_data = header_top is None  # continuation pages have no header

            if header_top is not None:
                cols[section] = adapt_cols(
                    LINEUP_COLS if not is_sailed else SAILED_COLS, _header_words(rows), hdr_ref[section])
            col_boundaries = build_col_boundaries(cols[section])

            # Summary boxes (first headed page of each section)
            if header_top is not None and not seen_header[section]:
                if is_sailed:
                    sailed_summary = extract_sailed_summary(rows, header_top)
                else:
                    lineup_summary = extract_lineup_summary(rows, header_top)
            if header_top is not None:
                seen_header[section] = True

            records, port_totals, grand_total = parse_page_rows(
                rows, col_boundaries, report_date, always_data=always_data
            )

            if is_sailed:
                all_sailed.extend(records)
                sailed_port_totals.update(port_totals)
                if grand_total is not None:      # older reports print one total per sailed block
                    sailed_grand_total_pdf = (sailed_grand_total_pdf or 0) + grand_total
            else:
                all_lineup.extend(records)
                lineup_port_totals.update(port_totals)
                if grand_total is not None:
                    lineup_grand_total_pdf = (lineup_grand_total_pdf or 0) + grand_total

    # ---- Assign lot numbers across all pages ----
    lot_counters = defaultdict(int)
    for rec in all_lineup:
        key = (rec["report_date"], rec["port"], rec["berth"], rec["vessel"])
        lot_counters[key] += 1
        rec["lot"] = lot_counters[key]

    lot_counters = defaultdict(int)
    for rec in all_sailed:
        key = (rec["report_date"], rec["port"], rec["berth"], rec["vessel"])
        lot_counters[key] += 1
        rec["lot"] = lot_counters[key]

    # ---- Validation ----
    lineup_non_excl = [r for r in all_lineup if not r["excluded"]]
    sailed_non_excl = [r for r in all_sailed if not r["excluded"]]
    lineup_parsed_total = sum(r["mt"] for r in lineup_non_excl)
    sailed_parsed_total = sum(r["mt"] for r in sailed_non_excl)
    # Older reports print a nominal tonnage on MAINTENANCE rows and count it in their totals.
    lineup_excluded_pdf = sum(r["pdf_mt"] for r in all_lineup if r["excluded"])
    sailed_excluded_pdf = sum(r["pdf_mt"] for r in all_sailed if r["excluded"])

    import logging
    logger = logging.getLogger(__name__)

    port_mismatches = []
    for port, pdf_total in lineup_port_totals.items():
        parsed = sum(r["pdf_mt"] for r in all_lineup if r["port"] == port)
        diff = parsed - pdf_total
        if abs(diff) > 1:
            msg = f"Port mismatch – {port}: pdf={pdf_total}, parsed={parsed}, diff={diff}"
            logger.warning(msg)
            port_mismatches.append({
                "port": port,
                "pdf": pdf_total,
                "parsed": parsed,
                "diff": diff,
            })

    return {
        "report_date": report_date,
        "lineup": all_lineup,
        "sailed": all_sailed,
        "summary": {
            "lineup_per_product": lineup_summary,
            "sailed_mtd": sailed_summary,
        },
        "validation": {
            "lineup_grand_total_pdf": lineup_grand_total_pdf,
            "lineup_grand_total_parsed": lineup_parsed_total,
            "sailed_grand_total_pdf": sailed_grand_total_pdf,
            "sailed_grand_total_parsed": sailed_parsed_total,
            "lineup_excluded_pdf_mt": lineup_excluded_pdf,
            "sailed_excluded_pdf_mt": sailed_excluded_pdf,
            "port_mismatches": port_mismatches,
        },
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python brazil_parser.py <pdf_path>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    print(f"Parsing: {pdf_path}")
    parsed = parse_pdf(pdf_path)

    rd = parsed["report_date"]
    lineup = parsed["lineup"]
    sailed = parsed["sailed"]
    v = parsed["validation"]
    s = parsed["summary"]

    print(f"\nReport date  : {rd}")
    print(f"Lineup rows  : {len(lineup)}  (incl. {sum(1 for r in lineup if r['excluded'])} excluded)")
    print(f"Sailed rows  : {len(sailed)}  (incl. {sum(1 for r in sailed if r['excluded'])} excluded)")
    print(f"\nLineup grand total  — PDF: {v['lineup_grand_total_pdf']:>12,}   parsed: {v['lineup_grand_total_parsed']:>12,}")
    print(f"Sailed grand total  — PDF: {v['sailed_grand_total_pdf']:>12,}   parsed: {v['sailed_grand_total_parsed']:>12,}")

    if v["port_mismatches"]:
        print("\nPort mismatches:")
        for m in v["port_mismatches"]:
            print(f"  {m['port']}: pdf={m['pdf']:,}  parsed={m['parsed']:,}  diff={m['diff']:+,}")
    else:
        print("\nAll port subtotals match (within 1 MT).")

    print("\nLineup per product (summary box):")
    for prod, mt in s["lineup_per_product"].items():
        print(f"  {prod:<12} {mt:>12,}")

    print("\nSailed MTD (summary box):")
    for prod, mt in s["sailed_mtd"].items():
        print(f"  {prod:<12} {mt:>12,}")

    # Quick split-cargo check
    from collections import Counter
    vessel_counts = Counter((r["port"], r["berth"], r["vessel"]) for r in lineup)
    multi = {k: v for k, v in vessel_counts.items() if v > 1}
    if multi:
        print(f"\nVessels with multiple cargo rows (split cargo): {len(multi)}")
        for (port, berth, vessel), cnt in list(multi.items())[:5]:
            print(f"  {vessel} @ {port}/{berth}: {cnt} rows")
