"""Incremental updates to facts.db, called by the droplet jobs right after a new source file arrives.

Southport: each email's workbook is a full snapshot -> one line-up snapshot (kept) + sail events (upserted).
Brazil:    re-derive snapshots and events from the most recent weeks of brazil_lineup.db.
Both are idempotent: running them twice for the same input changes nothing.
"""

import os
import re
import sqlite3
from datetime import date, datetime

import pandas as pd

from . import dimensions as dim
from . import events as ev
from . import lineup as lu
from . import store


def southport_report_date(name, received=None):
    """Report date from 'SOUTHPORT LINEUP 10.05.2026.xlsx' (m.d.yy or m.d.yyyy); else the email's received date."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", name or "")
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        try:
            return date(y, mo, d)
        except ValueError:
            pass
    if received:
        return datetime.fromisoformat(str(received).replace("Z", "+00:00")).date()
    raise ValueError(f"cannot work out a report date from {name!r} and no received date given")


def ingest_southport_file(path, report_date=None, facts_db=None, name=None, received=None, cfg=None):
    cfg = cfg or dim.default_config()
    rd = report_date or southport_report_date(name or os.path.basename(path), received)
    v = lu.read_southport_vessels(path)
    snap = lu.us_lineup_snapshot(v, rd, cfg)
    store.save_lineup_snapshot(snap, facts_db)
    n = store.save_events(ev.us_sail_events(v, cfg), facts_db)
    return {"report_date": str(rd), "queued_kmt": round(float(snap["kmt"].sum()), 1),
            "queued_vessels": int(snap["vessels"].sum()), "sail_events": n}


def refresh_brazil(brazil_db, facts_db=None, window_days=40, cfg=None):
    """Re-derive Brazil snapshots and sail events from the last `window_days` of brazil_db. The sailed
    list resets monthly, so 40 days always covers the current month plus the tail of the previous one;
    older events are already banked and are upserted by id, never duplicated."""
    cfg = cfg or dim.default_config()
    with sqlite3.connect(brazil_db) as c:
        latest = c.execute("SELECT MAX(report_date) FROM lineup").fetchone()[0]
        if latest is None:
            return {"latest": None, "snapshots": 0, "sail_events": 0}
        cutoff = (pd.Timestamp(latest) - pd.Timedelta(days=window_days)).strftime("%Y-%m-%d")
        lineup = pd.read_sql_query("SELECT * FROM lineup WHERE report_date >= ?", c, params=(cutoff,))
        sailed = pd.read_sql_query("SELECT * FROM sailed WHERE report_date >= ?", c, params=(cutoff,))
    snaps = lu.brazil_lineup_snapshot(lineup, cfg)
    store.save_lineup_snapshot(snaps, facts_db)
    n = store.save_events(ev.brazil_sail_events(sailed, cfg), facts_db) if not sailed.empty else 0
    return {"latest": latest, "snapshots": int(snaps["report_date"].nunique()), "sail_events": n}
