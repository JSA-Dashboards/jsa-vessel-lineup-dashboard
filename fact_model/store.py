"""SQLite persistence for the two fact tables (facts.db next to app.py).

lineup_fact   append-only snapshots. Re-loading the same (report_date, source) replaces
              just that snapshot; every other date is kept.
sail_events   vessel sail events (the executed source today); upserted by event_id.
executed_raw  append-only reported cumulative figures (for a future cumulative source).
executed_fact materialised view: executed_raw + derived MTD/MYTD, rebuilt from executed_raw
              (derivation depends on neighbouring snapshots and on editable MY config).
"""

import os
import sqlite3

import pandas as pd

from .executed import KEY, OUT_COLS, derive_executed
from .lineup import FACT_COLS

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "facts.db")


def _conn(db_path):
    return sqlite3.connect(db_path or DB_PATH)


def _iso(df):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
    return d


def _replace_snapshots(df, table, db_path):
    if df.empty:
        return 0
    d = _iso(df)
    with _conn(db_path) as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone()
        if exists:
            for rd, src in d[["report_date", "source"]].drop_duplicates().itertuples(index=False):
                c.execute(f"DELETE FROM {table} WHERE report_date=? AND source=?", (rd, src))
        d.to_sql(table, c, if_exists="append", index=False)
    return len(d)


def save_lineup_snapshot(df, db_path=None):
    return _replace_snapshots(df[FACT_COLS], "lineup_fact", db_path)


def save_executed_raw(raw, db_path=None):
    return _replace_snapshots(raw[["report_date", *KEY, "cum_raw_kmt"]], "executed_raw", db_path)


def _read(table, db_path, date_cols):
    with _conn(db_path) as c:
        try:
            df = pd.read_sql_query(f"SELECT * FROM {table}", c)
        except Exception:
            return pd.DataFrame()
    for col in date_cols:
        if col in df:
            df[col] = pd.to_datetime(df[col])
    if "mapped" in df:
        df["mapped"] = df["mapped"].astype(bool)
    for col in ("unmapped_reason", "fgis_port"):
        if col in df:
            df[col] = df[col].fillna("")
    return df


def save_events(events, db_path=None):
    """Upsert by event_id: re-loading never duplicates, and events that later drop out of a
    source file (the workbook is overwritten each load) stay in the history."""
    if events.empty:
        return 0
    d = _iso(events)
    with _conn(db_path) as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sail_events'").fetchone()
        if exists:
            c.executemany("DELETE FROM sail_events WHERE event_id=?", [(i,) for i in d["event_id"]])
        d.to_sql("sail_events", c, if_exists="append", index=False)
    return len(d)


def load_events(db_path=None):
    return _read("sail_events", db_path, ["sail_date"])


def load_lineup(db_path=None):
    return _read("lineup_fact", db_path, ["report_date", "month"])


def load_executed_raw(db_path=None):
    return _read("executed_raw", db_path, ["report_date"])


def rebuild_executed(db_path=None, cfg=None):
    raw = load_executed_raw(db_path)
    fact = derive_executed(raw, cfg) if not raw.empty else pd.DataFrame(columns=OUT_COLS)
    d = _iso(fact)
    with _conn(db_path) as c:
        d.to_sql("executed_fact", c, if_exists="replace", index=False)
    return fact


def load_executed(db_path=None):
    return _read("executed_fact", db_path,
                 ["report_date", "month", "mtd_baseline_date", "mytd_baseline_date"])
