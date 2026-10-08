"""
brazil_db.py  –  SQLite persistence for Brazil vessel lineup data.

DB file: brazil_lineup.db  (same directory as this script)
"""

import sqlite3
import os
import pandas as pd
from datetime import date

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brazil_lineup.db")

# ---- Schema ----------------------------------------------------------------

_VESSEL_COLS = """
    id          INTEGER PRIMARY KEY,
    report_date TEXT NOT NULL,
    port        TEXT,
    berth       TEXT,
    vessel      TEXT,
    charterer   TEXT,
    status      TEXT,
    eta         TEXT,
    etb         TEXT,
    etcs        TEXT,
    wt_days     INTEGER,
    destination TEXT,
    agent       TEXT,
    product     TEXT,
    mt          INTEGER,
    lot         INTEGER,
    core_product INTEGER,
    excluded    INTEGER
"""

_CREATE_LINEUP = f"CREATE TABLE IF NOT EXISTS lineup ({_VESSEL_COLS})"
_CREATE_SAILED = f"CREATE TABLE IF NOT EXISTS sailed ({_VESSEL_COLS})"
_CREATE_SUMMARY = """
    CREATE TABLE IF NOT EXISTS summary (
        id           INTEGER PRIMARY KEY,
        report_date  TEXT NOT NULL,
        product      TEXT NOT NULL,
        lineup_mt    INTEGER,
        sailed_mtd_mt INTEGER,
        UNIQUE(report_date, product)
    )
"""

# ---- Helpers ---------------------------------------------------------------

def _get_conn(db_path=None):
    return sqlite3.connect(db_path or _DB_PATH)


def _row_to_tuple(row, table):
    """Convert a parsed row dict to INSERT tuple (minus id)."""
    def _d(v):
        if isinstance(v, date):
            return v.isoformat()
        return v
    return (
        _d(row["report_date"]),
        row["port"],
        row["berth"],
        row["vessel"],
        row["charterer"],
        row["status"],
        _d(row["eta"]),
        _d(row["etb"]),
        _d(row["etcs"]),
        row["wt_days"],
        row["destination"],
        row["agent"],
        row["product"],
        row["mt"],
        row["lot"],
        1 if row["core_product"] else 0,
        1 if row["excluded"] else 0,
    )


_INSERT_VESSEL = """
    INSERT INTO {table} (
        report_date, port, berth, vessel, charterer, status,
        eta, etb, etcs, wt_days, destination, agent,
        product, mt, lot, core_product, excluded
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

# ---- Public API ------------------------------------------------------------

def init_db(db_path=None):
    """Create tables if they don't exist."""
    with _get_conn(db_path) as conn:
        conn.execute(_CREATE_LINEUP)
        conn.execute(_CREATE_SAILED)
        conn.execute(_CREATE_SUMMARY)
        conn.commit()


def date_loaded(report_date: str, db_path=None) -> bool:
    """Return True if any row for report_date exists in lineup."""
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "SELECT 1 FROM lineup WHERE report_date=? LIMIT 1",
            (report_date,),
        )
        return cur.fetchone() is not None


def upsert_report(parsed: dict, db_path=None):
    """
    Idempotent: delete existing rows for that report_date, then insert all.
    Runs in a single transaction.
    """
    rd = parsed["report_date"]
    if isinstance(rd, date):
        rd_str = rd.isoformat()
    else:
        rd_str = str(rd)

    lineup_rows = parsed["lineup"]
    sailed_rows = parsed["sailed"]
    summary = parsed["summary"]

    with _get_conn(db_path) as conn:
        # Delete existing
        conn.execute("DELETE FROM lineup WHERE report_date=?", (rd_str,))
        conn.execute("DELETE FROM sailed WHERE report_date=?", (rd_str,))
        conn.execute("DELETE FROM summary WHERE report_date=?", (rd_str,))

        # Insert lineup
        sql_lineup = _INSERT_VESSEL.format(table="lineup")
        for row in lineup_rows:
            conn.execute(sql_lineup, _row_to_tuple(row, "lineup"))

        # Insert sailed
        sql_sailed = _INSERT_VESSEL.format(table="sailed")
        for row in sailed_rows:
            conn.execute(sql_sailed, _row_to_tuple(row, "sailed"))

        # Insert summary
        lineup_pp = summary.get("lineup_per_product", {})
        sailed_mtd = summary.get("sailed_mtd", {})
        all_products = set(lineup_pp) | set(sailed_mtd)
        for prod in all_products:
            conn.execute(
                """INSERT OR REPLACE INTO summary
                   (report_date, product, lineup_mt, sailed_mtd_mt)
                   VALUES (?,?,?,?)""",
                (
                    rd_str,
                    prod,
                    lineup_pp.get(prod),
                    sailed_mtd.get(prod),
                ),
            )

        conn.commit()


def get_lineup(report_date=None, db_path=None) -> pd.DataFrame:
    """Return lineup rows. If report_date is None, return latest date."""
    with _get_conn(db_path) as conn:
        if report_date is None:
            cur = conn.execute("SELECT MAX(report_date) FROM lineup")
            row = cur.fetchone()
            if row is None or row[0] is None:
                return pd.DataFrame()
            report_date = row[0]
        df = pd.read_sql_query(
            "SELECT * FROM lineup WHERE report_date=? ORDER BY id",
            conn,
            params=(report_date,),
        )
    return df


def get_sailed(report_date=None, db_path=None) -> pd.DataFrame:
    """Return sailed rows. If report_date is None, return latest date."""
    with _get_conn(db_path) as conn:
        if report_date is None:
            cur = conn.execute("SELECT MAX(report_date) FROM sailed")
            row = cur.fetchone()
            if row is None or row[0] is None:
                return pd.DataFrame()
            report_date = row[0]
        df = pd.read_sql_query(
            "SELECT * FROM sailed WHERE report_date=? ORDER BY id",
            conn,
            params=(report_date,),
        )
    return df


def get_all_lineup(db_path=None) -> pd.DataFrame:
    """Return all lineup rows across all dates."""
    with _get_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM lineup ORDER BY report_date DESC, id",
            conn,
        )
    return df


def get_all_sailed(db_path=None) -> pd.DataFrame:
    """Return all sailed rows across all dates."""
    with _get_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM sailed ORDER BY report_date DESC, id",
            conn,
        )
    return df


def get_report_dates(db_path=None) -> list:
    """Return distinct report dates in the lineup table, newest first."""
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "SELECT DISTINCT report_date FROM lineup ORDER BY report_date DESC"
        )
        return [row[0] for row in cur.fetchall()]


def get_summary(db_path=None) -> pd.DataFrame:
    """Return all summary rows."""
    with _get_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM summary ORDER BY report_date DESC, product",
            conn,
        )
    return df
