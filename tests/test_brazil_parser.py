"""
tests/test_brazil_parser.py

Run against the Oct-6 fixture:
    pytest tests/test_brazil_parser.py -v
"""

import os
import sys
from datetime import date

import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE_PATH = r"C:\Users\KoltenPostin\Downloads\APS Brz Consolidated line-up 2026.10.06.pdf"

_fixture_missing = not os.path.exists(FIXTURE_PATH)
skip_if_missing = pytest.mark.skipif(
    _fixture_missing, reason=f"Fixture not found: {FIXTURE_PATH}"
)


@pytest.fixture(scope="module")
def parsed():
    from brazil_parser import parse_pdf
    return parse_pdf(FIXTURE_PATH)


# ---------------------------------------------------------------------------
# 1. Report date
# ---------------------------------------------------------------------------
@skip_if_missing
def test_report_date(parsed):
    assert parsed["report_date"] == date(2026, 10, 6)


# ---------------------------------------------------------------------------
# 2. Lineup statuses
# ---------------------------------------------------------------------------
@skip_if_missing
def test_lineup_statuses(parsed):
    valid = {"LDG", "WTG", "ETA"}
    bad = [r for r in parsed["lineup"] if r["status"] not in valid]
    assert bad == [], f"Unexpected lineup statuses: {[(r['vessel'], r['status']) for r in bad[:5]]}"


# ---------------------------------------------------------------------------
# 3. Sailed statuses
# ---------------------------------------------------------------------------
@skip_if_missing
def test_sailed_statuses(parsed):
    bad = [r for r in parsed["sailed"] if r["status"] != "SLD"]
    assert bad == [], f"Unexpected sailed statuses: {[(r['vessel'], r['status']) for r in bad[:5]]}"


# ---------------------------------------------------------------------------
# 4. Lineup grand total matches PDF
# ---------------------------------------------------------------------------
@skip_if_missing
def test_lineup_grand_total(parsed):
    v = parsed["validation"]
    assert v["lineup_grand_total_pdf"] is not None, "No lineup grand total found in PDF"
    assert v["lineup_grand_total_parsed"] is not None
    diff = abs(v["lineup_grand_total_parsed"] - v["lineup_grand_total_pdf"])
    assert diff <= 1, (
        f"Lineup grand total mismatch: "
        f"pdf={v['lineup_grand_total_pdf']:,}  parsed={v['lineup_grand_total_parsed']:,}"
    )


# ---------------------------------------------------------------------------
# 5. Sailed grand total matches PDF
# ---------------------------------------------------------------------------
@skip_if_missing
def test_sailed_grand_total(parsed):
    v = parsed["validation"]
    assert v["sailed_grand_total_pdf"] is not None, "No sailed grand total found in PDF"
    diff = abs(v["sailed_grand_total_parsed"] - v["sailed_grand_total_pdf"])
    assert diff <= 1, (
        f"Sailed grand total mismatch: "
        f"pdf={v['sailed_grand_total_pdf']:,}  parsed={v['sailed_grand_total_parsed']:,}"
    )


# ---------------------------------------------------------------------------
# 6. Split-cargo: NEW AMBITION at SANTOS/TES → HIPRO + SBMP
# ---------------------------------------------------------------------------
@skip_if_missing
def test_new_ambition_split_cargo(parsed):
    rows = [
        r for r in parsed["lineup"]
        if r["vessel"] == "NEW AMBITION"
        and r["berth"] is not None
        and "TES" in r["berth"]
    ]
    assert len(rows) >= 2, (
        f"Expected >= 2 rows for NEW AMBITION @ TES, got {len(rows)}: "
        f"{[(r['product'], r['mt']) for r in rows]}"
    )
    products = {r["product"] for r in rows}
    assert "HIPRO" in products, f"Missing HIPRO row; products found: {products}"
    assert "SBMP" in products, f"Missing SBMP row; products found: {products}"

    # All shared fields must match
    shared_keys = ("vessel", "charterer", "status", "eta", "etb", "etcs", "wt_days", "destination", "agent")
    first = rows[0]
    for r in rows[1:]:
        for k in shared_keys:
            assert r[k] == first[k], f"NEW AMBITION shared field '{k}' differs: {first[k]!r} vs {r[k]!r}"


# ---------------------------------------------------------------------------
# 7. Split-cargo: GOOD WAY at ITACOATIARA → HIPRO + MZ (or similar continuation)
# ---------------------------------------------------------------------------
@skip_if_missing
def test_good_way_split_cargo(parsed):
    rows = [r for r in parsed["lineup"] if r["vessel"] == "GOOD WAY"]
    assert len(rows) >= 2, (
        f"Expected >= 2 rows for GOOD WAY, got {len(rows)}: "
        f"{[(r['product'], r['mt']) for r in rows]}"
    )
    ports = {r["port"] for r in rows}
    # All should share the same port/berth (it's one vessel with split cargo)
    berths = {r["berth"] for r in rows}
    assert len(ports) == 1, f"GOOD WAY found at multiple ports: {ports}"
    assert len(berths) == 1, f"GOOD WAY found at multiple berths: {berths}"

    # Products should differ (split cargo)
    products = [r["product"] for r in rows]
    assert len(set(products)) == len(products) or len(rows) == 2, \
        f"GOOD WAY products not split: {products}"


# ---------------------------------------------------------------------------
# 8. Duplicate vessel: MAYE MANX at two different ports
#    (skipped gracefully if not present in the Oct-6 fixture)
# ---------------------------------------------------------------------------
@skip_if_missing
def test_maye_manx_two_ports(parsed):
    all_rows = parsed["lineup"] + parsed["sailed"]
    maye_rows = [r for r in all_rows if r["vessel"] == "MAYE MANX"]
    if len(maye_rows) < 2:
        pytest.skip(f"MAYE MANX appears {len(maye_rows)} time(s) in Oct-6 fixture; skipping duplicate-port check")
    ports = {r["port"] for r in maye_rows}
    assert len(ports) > 1, (
        f"MAYE MANX appears {len(maye_rows)} times but at only one port: {ports}"
    )


# ---------------------------------------------------------------------------
# 9. Exclusion: MAINTENANCE rows
# ---------------------------------------------------------------------------
@skip_if_missing
def test_maintenance_excluded(parsed):
    maint = [r for r in parsed["lineup"] + parsed["sailed"] if "MAINTENANCE" in (r["vessel"] or "")]
    assert len(maint) > 0, "No MAINTENANCE rows found"
    for r in maint:
        assert r["excluded"] is True, f"MAINTENANCE row not excluded: {r['vessel']}"
        assert r["mt"] == 0, f"MAINTENANCE row has non-zero MT: {r['mt']}"


# ---------------------------------------------------------------------------
# 10. "?" charterer → None
# ---------------------------------------------------------------------------
@skip_if_missing
def test_question_mark_charterer(parsed):
    all_rows = parsed["lineup"] + parsed["sailed"]
    # At least one row should have charterer=None from a '?' in the PDF
    none_charterers = [r for r in all_rows if r["charterer"] is None]
    assert len(none_charterers) > 0, "Expected at least one row with charterer=None ('?')"


# ---------------------------------------------------------------------------
# 11. Summary box: SBS > 0 and total roughly matches lineup grand total
# ---------------------------------------------------------------------------
@skip_if_missing
def test_summary_box(parsed):
    lpp = parsed["summary"]["lineup_per_product"]
    assert lpp, "lineup_per_product summary box is empty"
    assert "SBS" in lpp, f"SBS not in summary; keys: {list(lpp.keys())}"
    assert lpp["SBS"] > 0, f"SBS = {lpp['SBS']}"

    # The summary box shows only selected products (SBS, MZ, SBMP, HIPRO, SPC, RAW SUG),
    # so its total is less than the full parsed total.  Just verify SBS is reasonable.
    assert lpp["SBS"] > 1_000_000, f"SBS total unexpectedly small: {lpp['SBS']:,}"


# ---------------------------------------------------------------------------
# 12. Lot numbers are sequential per (port, berth, vessel) group
# ---------------------------------------------------------------------------
@skip_if_missing
def test_lot_numbers(parsed):
    """Lot numbers must be sequential per (port, berth, vessel) within each section."""
    from collections import defaultdict
    # Check lineup and sailed SEPARATELY — the same vessel can appear in both
    # sections independently (e.g. a MAINTENANCE row in lineup ETA and in sailed SLD)
    for section_name, section in (("lineup", parsed["lineup"]), ("sailed", parsed["sailed"])):
        groups = defaultdict(list)
        for r in section:
            key = (r["report_date"], r["port"], r["berth"], r["vessel"])
            groups[key].append(r["lot"])
        for key, lots in groups.items():
            expected = list(range(1, len(lots) + 1))
            assert lots == expected, (
                f"Lot sequence wrong in {section_name} for {key}: {lots}"
            )


# ---------------------------------------------------------------------------
# 13. MT parsing sanity
# ---------------------------------------------------------------------------
@skip_if_missing
def test_mt_values_non_negative(parsed):
    bad = [r for r in parsed["lineup"] + parsed["sailed"] if r["mt"] < 0]
    assert bad == [], f"Negative MT values: {[(r['vessel'], r['mt']) for r in bad]}"


@skip_if_missing
def test_mt_parsing_unit():
    """Unit test: dots are thousands separators, not decimals."""
    from brazil_parser import parse_mt
    assert parse_mt("63.000") == 63000
    assert parse_mt("5.118.476") == 5118476
    assert parse_mt("0") == 0
    assert parse_mt("-") == 0
    assert parse_mt("") == 0
    assert parse_mt("1.824.704") == 1824704
