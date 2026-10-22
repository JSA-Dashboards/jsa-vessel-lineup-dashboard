"""Nabsa (Argentina/Uruguay) line-up + sailed workbooks -> facts.

    pytest tests/test_argentina.py -v
"""

import datetime as dt
import os
import sys

import pandas as pd
import pytest
from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_model import argentina as ar
from fact_model import dimensions as dim
from fact_model import store

HDR = ["Port", "Terminal", "Vessel", "ETA", "ETB", "ETF", "Ops", "Tons", "Commodity", "Destination", "Origin", "Charterer"]


def make_lineup(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "DailyVesselLineUp"
    ws.append(["Line Up:", dt.datetime(2026, 10, 22), "Circ.Nbr.:", 197])
    ws.append([])
    ws.append(["Notes: REC (RECALADA PILOT STATION)"])
    ws.append([])
    ws.append(HDR)
    for r in rows:
        ws.append(r)
    wb.save(path)


def make_sailed(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["AGENCIA MARITIMA NABSA"])
    ws.append(["SAILED VESSELS  - 2026 (CUMULATIVE)"])
    ws.append(["Port", "Terminal", "Vessel", "Status", "Date", "Tons", "Cargo", "Origin", "Destination", "Coordinator", "Charterer"])
    for r in rows:
        ws.append(r)
    wb.save(path)


LINEUP = [
    ["SAN LORENZO", "RENOVA NORTH", "NIL", None, None, None, None, None],
    ["SAN LORENZO", "RENOVA NORTH", "ASTARTE", "ETA REC 26/10", None, None, "load", 17600, "SOYBEANMEAL", "GERMANY", "ARGENTINA", "BUNGE"],
    ["SAN LORENZO", "RENOVA NORTH", "ASTARTE", "ETA REC 26/10", None, None, "load", 8800, "SOYA HULL PELLETS", "GERMANY", "ARGENTINA", "BUNGE"],
    ["SAN LORENZO", "RENOVA NORTH", "TIGER EAST", "ETA 22/10", None, None, "load", 46000, "CORN", "KOREA", "ARGENTINA", "BUNGE"],
    ["SAN LORENZO", "COFCO", "NAVI LYRA", "ETA REC 24/10", None, None, "discharge", 3500, "TSP", "ARGENTINA", "MOROCCO", "COFCO"],
    ["BAHIA BLANCA", "TERMINAL", "BB ONE", "ETA 23/10", None, None, "load", 30000, "WHEAT", "BRAZIL", "ARGENTINA", "COFCO"],
    ["MONTEVIDEO(R.O.U)", "TMT", "UY ONE", "ETA 23/10", None, None, "load", 20000, "WOOD PULP", "CHINA", "URUGUAY", "UPM"],
    ["LIMA", "DELTA DOCK LIMA", "LIMA ONE", "ETA 23/10", None, None, "load", 25000, "CORN", "PERU", "ARGENTINA", "COFCO"],
]
SAILED = [
    ["SAN LORENZO", "RENOVA NORTH", "AURIGA STAR", "SAILED", dt.datetime(2026, 10, 1), 11201.63, "CORN", "ARGENTINA", "SAUDI ARABIA", "BUNGE", "BUNGE"],
    ["SAN LORENZO", "RENOVA NORTH", "AURIGA STAR", "SAILED", dt.datetime(2026, 10, 1), 36675.76, "SOYBEANMEAL", "ARGENTINA", "SAUDI ARABIA", "BUNGE", "BUNGE"],
    ["ROSARIO", "TERMINAL 6", "UREA ONE", "SAILED", dt.datetime(2026, 10, 2), 20000, "GRANULAR UREA", "NIGERIA", "ARGENTINA", "X", "X"],
    ["BAHIA BLANCA", "TERMINAL", "BB ONE", "SAILED", dt.datetime(2026, 10, 3), 30000, "WHEAT", "ARGENTINA", "BRAZIL", "COFCO", "COFCO"],
]


@pytest.fixture
def files(tmp_path):
    a, b = str(tmp_path / "lineup20261022.xlsx"), str(tmp_path / "sailed20261022.xlsx")
    make_lineup(a, LINEUP)
    make_sailed(b, SAILED)
    return a, b


def test_commodity_groups():
    g = lambda c: dim.commodity_group("Argentina", c)
    assert [g(c) for c in ("CORN", "SOYBEANMEAL", "SOYA BEAN", "SOYBEAN OIL", "WHEAT", "SORGHUM")] == \
        ["Corn", "Soybean Meal", "Soybeans", "Soybean Oil", "Wheat", "Sorghum"]
    assert g("SUN FLOWER OIL") == "Other Oils" and g("BIODIESEL") == "Other Oils"
    assert g("WOOD PULP") == "Non-Ag" and g("CRUDE OIL") == "Non-Ag" and g("GRANULAR UREA") == "Non-Ag"
    assert g("SOYA HULL PELLETS") == "Other" and g("BARLEY") == "Other"


def test_lineup_keeps_only_loading_vessels_and_reads_the_date(files):
    v = ar.read_lineup(files[0])
    assert v.attrs["report_date"] == pd.Timestamp("2026-10-22")
    assert sorted(v["vessel"]) == ["ASTARTE", "ASTARTE", "BB ONE", "LIMA ONE", "TIGER EAST", "UY ONE"]    # no NIL, no discharge
    assert v.loc[v["vessel"] == "TIGER EAST", "kmt"].iloc[0] == pytest.approx(46.0)


def test_snapshot_region_totals_equal_vessels_and_split_multi_cargo_by_tonnage(files):
    v = ar.read_lineup(files[0])
    s = ar.argentina_lineup_snapshot(v, "2026-10-22")
    assert s["vessels"].sum() == pytest.approx(5.0)                      # ASTARTE counts once, not twice
    assert s["mapped"].all()
    up = s[(s["region"] == "Argentina Up-River")]
    assert set(up["port"]) == {"San Lorenzo", "Lima"}                    # Lima is Buenos Aires province, not Peru
    a = up[up["port"] == "San Lorenzo"].set_index("commodity")["vessels"]
    assert a["Soybean Meal"] == pytest.approx(17.6 / 26.4) and a["Other"] == pytest.approx(8.8 / 26.4)
    assert s[s["region"] == "Uruguay"]["commodity"].tolist() == ["Non-Ag"]
    assert (s["source"] == "Argentina").all()


def test_sailed_keeps_exports_only_and_splits_the_vessel_count(files):
    e = ar.argentina_sail_events(ar.read_sailed(files[1]))
    assert sorted(e["vessel"]) == ["AURIGA STAR", "AURIGA STAR", "BB ONE"]            # the urea import is out
    assert e["event_id"].is_unique
    w = e.groupby("vessel")["vessel_w"].sum()
    assert w["AURIGA STAR"] == pytest.approx(1.0) and w["BB ONE"] == pytest.approx(1.0)


def test_ingest_is_idempotent_and_banks_both_tables(files, tmp_path):
    db = str(tmp_path / "facts.db")
    r1 = ar.ingest_argentina_files(files[0], files[1], facts_db=db)
    r2 = ar.ingest_argentina_files(files[0], files[1], facts_db=db)
    assert r1["report_date"] == "2026-10-22" and r1["queued_vessels"] == pytest.approx(5.0)
    assert len(store.load_lineup(db)) == len(ar.argentina_lineup_snapshot(ar.read_lineup(files[0]), "2026-10-22"))
    assert len(store.load_events(db)) == 3 == r2["sail_events"]
