"""Incremental ingestion used by the droplet jobs.

    pytest tests/test_ingest.py -v
"""

import os
import sys
from datetime import date

import pandas as pd
import pytest
from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brazil_db
from fact_model import ingest, store

SORT_HDR = ["ELEVATOR", "VESSEL", "ATA", "STATUS", "MT", "COMMODITY", "DESTINATION"]
SAIL_HDR = SORT_HDR + ["SAIL DATE"]


def southport_workbook(path, sail_kmt="35K"):
    wb = Workbook()
    wb.remove(wb.active)
    for sheet, rows in {
        "MISS RIVER SORT": [["CHS", "KEN UN", None, "ETA 10/09", "35K", "CORN", "GUATEMALA"]],
        "MISS RIVER SAIL": [["ADM DESTREHAN", "BIG SHIP", None, "SAILED", sail_kmt, "YSB", "CHINA", pd.Timestamp("2026-10-05")]],
        "TEXAS SORT": [], "TEXAS SAIL": [], "PNW SORT": [], "PNW SAIL": [],
    }.items():
        ws = wb.create_sheet(sheet)
        ws.append(SAIL_HDR if sheet.endswith("SAIL") else SORT_HDR)
        for r in rows:
            ws.append(r)
    wb.save(path)


def test_southport_report_date_from_the_file_name_then_received_date():
    f = ingest.southport_report_date
    assert f("SOUTHPORT LINEUP 10.05.2026.xlsx") == date(2026, 10, 5)
    assert f("SOUTHPORT LINEUP 1.9.26.xlsx") == date(2026, 1, 9)
    assert f("something.xlsx", "2026-10-06T21:06:00Z") == date(2026, 10, 6)
    with pytest.raises(ValueError):
        f("something.xlsx")


def test_ingest_southport_banks_a_snapshot_and_events_and_is_idempotent(tmp_path):
    x, db = str(tmp_path / "s.xlsx"), str(tmp_path / "f.db")
    southport_workbook(x)
    r1 = ingest.ingest_southport_file(x, name="SOUTHPORT LINEUP 10.06.2026.xlsx", facts_db=db)
    assert r1["report_date"] == "2026-10-06" and r1["queued_vessels"] == 1 and r1["sail_events"] == 1
    ingest.ingest_southport_file(x, name="SOUTHPORT LINEUP 10.06.2026.xlsx", facts_db=db)      # same file again
    assert len(store.load_lineup(db)) == 1 and len(store.load_events(db)) == 1
    # a later file banks a second snapshot; the earlier snapshot is kept, and a revised tonnage replaces the event
    southport_workbook(x, sail_kmt="40K")
    ingest.ingest_southport_file(x, name="SOUTHPORT LINEUP 10.07.2026.xlsx", facts_db=db)
    lineup, events = store.load_lineup(db), store.load_events(db)
    assert sorted(lineup["report_date"].dt.strftime("%Y-%m-%d").unique()) == ["2026-10-06", "2026-10-07"]
    assert len(events) == 1 and events["kmt"].iloc[0] == 40.0


def parsed_report(rd, rows):
    base = {"report_date": rd, "berth": "TEG", "charterer": "X", "status": "LDG", "eta": date(2026, 10, 1), "etb": date(2026, 10, 2),
            "etcs": date(2026, 10, 4), "wt_days": 1, "destination": "CHINA", "agent": "A", "lot": 1, "core_product": True, "excluded": False}
    lineup = [{**base, "port": "SANTOS", "vessel": "ALPHA", "product": "MZ", "mt": 60000, "pdf_mt": 60000}]
    sailed = [{**base, "port": "SANTOS", "vessel": v, "product": p, "status": "SLD", "mt": m, "pdf_mt": m} for v, p, m in rows]
    return {"report_date": rd, "lineup": lineup, "sailed": sailed,
            "summary": {"lineup_per_product": {"MZ": 60000}, "sailed_mtd": {}}, "validation": {}}


def test_refresh_brazil_derives_snapshots_and_events_and_is_idempotent(tmp_path):
    bdb, db = str(tmp_path / "b.db"), str(tmp_path / "f.db")
    brazil_db.init_db(bdb)
    brazil_db.upsert_report(parsed_report(date(2026, 10, 6), [("BETA", "SBS", 64000)]), bdb)
    brazil_db.upsert_report(parsed_report(date(2026, 10, 7), [("BETA", "SBS", 64000), ("GAMMA", "MZ", 30000)]), bdb)
    r = ingest.refresh_brazil(bdb, db)
    assert r["latest"] == "2026-10-07" and r["snapshots"] == 2
    ev1 = store.load_events(db)
    assert len(ev1) == 2 and sorted(ev1["commodity"]) == ["Corn", "Soybeans"]          # BETA seen in two snapshots, counted once
    ingest.refresh_brazil(bdb, db)
    assert len(store.load_events(db)) == 2 and len(store.load_lineup(db)) == 2
    brazil_db.upsert_report(parsed_report(date(2026, 10, 8), [("BETA", "SBS", 64000), ("GAMMA", "MZ", 30000), ("DELTA", "MZ", 20000)]), bdb)
    ingest.refresh_brazil(bdb, db)
    assert len(store.load_events(db)) == 3 and store.load_lineup(db)["report_date"].nunique() == 3
