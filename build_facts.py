"""Load the current US workbook and Brazil DB into facts.db (idempotent).

    python build_facts.py                         # US snapshot dated from the workbook's mtime
    python build_facts.py --us-report-date 2026-10-02

Line-up snapshots are appended per (report_date, source); sail events are upserted. Run it
after each source update. Every banked snapshot is kept.
"""

import argparse
import os
import sqlite3
from datetime import datetime

import pandas as pd

from fact_model import dimensions as dim
from fact_model import events as ev
from fact_model import lineup as lu
from fact_model import store

HERE = os.path.dirname(os.path.abspath(__file__))
US_XLSX = os.path.join(HERE, "Vessel Lineup - US.xlsx")
BRZ_DB = os.path.join(HERE, "brazil_lineup.db")


def build(us_report_date=None, db_path=None, us_xlsx=US_XLSX, brz_db=BRZ_DB):
    cfg = dim.default_config()
    out = {}
    if os.path.exists(us_xlsx):
        rd = us_report_date or datetime.fromtimestamp(os.path.getmtime(us_xlsx)).strftime("%Y-%m-%d")
        v = lu.read_us_vessels(us_xlsx)
        snap = lu.us_lineup_snapshot(v, rd, cfg)
        out["us_snapshot"] = (rd, store.save_lineup_snapshot(snap, db_path))
        out["us_events"] = store.save_events(ev.us_sail_events(v, cfg), db_path)
    if os.path.exists(brz_db):
        with sqlite3.connect(brz_db) as c:
            lineup = pd.read_sql_query("SELECT * FROM lineup", c)
            sailed = pd.read_sql_query("SELECT * FROM sailed", c)
        if not lineup.empty:
            snaps = lu.brazil_lineup_snapshot(lineup, cfg)
            out["brazil_snapshots"] = (sorted(snaps["report_date"].dt.strftime("%Y-%m-%d").unique()),
                                       store.save_lineup_snapshot(snaps, db_path))
        if not sailed.empty:
            out["brazil_events"] = store.save_events(ev.brazil_sail_events(sailed, cfg), db_path)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--us-report-date", help="YYYY-MM-DD for the US snapshot (default: workbook mtime)")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    for k, v in build(a.us_report_date, a.db).items():
        print(f"{k}: {v}")
    lineup_all = store.load_lineup(a.db)
    bad = dim.unmapped_report(lineup_all[lineup_all["report_date"] == lineup_all.groupby("source")["report_date"].transform("max")])
    print(f"unmapped in latest snapshots: {len(bad)} group(s)")
    if len(bad):
        print(bad.to_string(index=False))
