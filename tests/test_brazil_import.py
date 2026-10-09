"""Brazil importer guards: only the consolidated line-up, and only a parse that reconciles.

    pytest tests/test_brazil_import.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brazil_lineup_import as imp
import brazil_parser as bp


@pytest.mark.parametrize("name,ok", [
    ("APS Brz Consolidated line-up 2026.10.08.pdf", True),
    ("APS Brz Consolidated line up 2025.09.11.pdf", True),
    ("APS_Brz_Consolidated_line_up_2024.11.01.pdf", True),
    ("APS Brz Chinese Report 2026.10.08.pdf", False),
    ("APS Brz Chinese report  2025.03.04.pdf", False),
    ("APS Br Port Performance Monthly report 8.2025.pdf", False),
    ("APS Br Agri Products Monthly report 8.2025.pdf", False),
    ("MISS RIVER 2026-2027.pdf", False),
])
def test_only_the_consolidated_lineup_matches(name, ok):
    assert imp._attachment_name_matches(name) is ok


def parsed(pdf_l, got_l, pdf_s=None, got_s=0, rows=1, rd="2026-10-08"):
    return {"report_date": rd, "lineup": [object()] * rows,
            "validation": {"lineup_grand_total_pdf": pdf_l, "lineup_grand_total_parsed": got_l,
                           "sailed_grand_total_pdf": pdf_s, "sailed_grand_total_parsed": got_s}}


def test_reconciling_parse_is_accepted_and_rounding_is_tolerated():
    assert bp.validation_error(parsed(15_000_000, 15_000_000, 3_000_000, 3_000_000)) is None
    assert bp.validation_error(parsed(15_000_000, 14_999_995)) is None            # a few MT of rounding in the PDF


def test_a_missed_vessel_is_an_error_strictly_but_loadable_under_one_percent():
    p = parsed(15_000_000, 14_940_000)                                            # one 60,000 MT vessel short
    assert "lineup total" in bp.validation_error(p)
    assert bp.validation_error(p, rel_tol=0.01) is None                           # 0.4%: warn and load
    assert bp.validation_error(parsed(15_000_000, 12_000_000), rel_tol=0.01)      # 20% off: never loaded


def test_a_non_lineup_report_is_rejected():
    assert bp.validation_error(parsed(None, 0, rows=0)) == "no line-up rows"
    assert "no line-up grand total" in bp.validation_error(parsed(None, 500_000))
    assert bp.validation_error(parsed(1, 1, rd=None)) == "no report date"


def test_excluded_maintenance_tonnage_counts_toward_the_total():
    p = parsed(15_077_000, 15_000_000)
    p["validation"]["lineup_excluded_pdf_mt"] = 77_000
    assert bp.validation_error(p) is None
