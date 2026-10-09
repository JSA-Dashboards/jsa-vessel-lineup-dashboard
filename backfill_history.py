"""Load archived Southport workbooks and APS Brazil PDFs (downloaded from the mailbox) into the databases.

    python backfill_history.py --archive history --brazil-db scratch/brazil.db --facts-db scratch/facts.db

Southport: every file is a full snapshot -> a line-up snapshot per report date (kept, never overwritten)
           + sail events (upserted by event_id, chronological so a later revision wins).
Brazil:    a PDF is accepted only if its report date is found AND the parsed line-up / sailed grand totals
           equal the totals printed in the PDF AND no port totals disagree. Rejected files are listed.
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fact_model import dimensions as dim
from fact_model import events as ev
from fact_model import ingest
from fact_model import lineup as lu
from fact_model import store


def _southport_date(name, received):
    return ingest.southport_report_date(name, received)


def load_southport(rows, facts_db, cfg):
    ok, bad = [], []
    rows = sorted(rows, key=lambda r: (_southport_date(r["name"], r["received"]), r["received"]))
    for r in rows:
        rd = _southport_date(r["name"], r["received"])
        try:
            v = lu.read_southport_vessels(r["file"])
            snap = lu.us_lineup_snapshot(v, rd, cfg)
            store.save_lineup_snapshot(snap, facts_db)
            n = store.save_events(ev.us_sail_events(v, cfg), facts_db)
            ok.append({"file": os.path.basename(r["file"]), "report_date": str(rd), "queued_kmt": round(snap["kmt"].sum()),
                       "vessels": int(snap["vessels"].sum()), "events": n})
        except Exception as exc:
            bad.append({"file": os.path.basename(r["file"]), "report_date": str(rd), "error": f"{type(exc).__name__}: {exc}"[:200]})
    return ok, bad


def _parse_brazil(path):
    import brazil_parser
    try:
        return path, brazil_parser.parse_pdf(path), None
    except Exception as exc:
        return path, None, f"{type(exc).__name__}: {exc}"[:200]


TOL_MT = 10     # the PDFs' own totals are off by a few MT of rounding; a missing vessel is tens of thousands


def _check(parsed):
    if parsed["report_date"] is None:
        return "no report date"
    v = parsed["validation"]
    lp = v["lineup_grand_total_parsed"] + v.get("lineup_excluded_pdf_mt", 0)
    sp = v["sailed_grand_total_parsed"] + v.get("sailed_excluded_pdf_mt", 0)
    if v["lineup_grand_total_pdf"] is None or abs(v["lineup_grand_total_pdf"] - lp) > TOL_MT:
        return f"lineup total {lp} != pdf {v['lineup_grand_total_pdf']}"
    if v["sailed_grand_total_pdf"] is not None and abs(v["sailed_grand_total_pdf"] - sp) > TOL_MT:
        return f"sailed total {sp} != pdf {v['sailed_grand_total_pdf']}"
    big = [m for m in v["port_mismatches"] if abs(m["diff"]) > TOL_MT]
    if big:
        return f"port mismatches: {[(m['port'], m['diff']) for m in big]}"
    return None


def load_brazil(rows, brazil_db, workers):
    import brazil_db as bdb
    bdb.init_db(brazil_db)
    ok, bad = [], []
    files = [r["file"] for r in rows]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for path, parsed, err in pool.map(_parse_brazil, files, chunksize=2):
            name = os.path.basename(path)
            if err or parsed is None:
                bad.append({"file": name, "error": err or "parse returned nothing"}); continue
            why = _check(parsed)
            if why:
                bad.append({"file": name, "report_date": str(parsed["report_date"]), "error": why}); continue
            bdb.upsert_report(parsed, brazil_db)
            ok.append({"file": name, "report_date": str(parsed["report_date"]),
                       "lineup_rows": len(parsed["lineup"]), "sailed_rows": len(parsed["sailed"])})
    return ok, bad


def facts_from_brazil(brazil_db, facts_db, cfg):
    import sqlite3
    with sqlite3.connect(brazil_db) as c:
        lineup = pd.read_sql_query("SELECT * FROM lineup", c)
        sailed = pd.read_sql_query("SELECT * FROM sailed", c)
    snaps = lu.brazil_lineup_snapshot(lineup, cfg)
    n1 = store.save_lineup_snapshot(snaps, facts_db)
    n2 = store.save_events(ev.brazil_sail_events(sailed, cfg), facts_db)
    return snaps["report_date"].nunique(), n2


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True, help="folder with manifest.json, brazil/, southport/")
    ap.add_argument("--brazil-db", required=True)
    ap.add_argument("--facts-db", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", choices=["brazil", "southport"])
    ap.add_argument("--limit", type=int, default=0, help="process only the first N files per kind (testing)")
    a = ap.parse_args()
    cfg = dim.default_config()
    man = [r for r in json.load(open(os.path.join(a.archive, "manifest.json"))) if not r["duplicate"]]
    for r in man:
        r["file"] = os.path.join(a.archive, r["kind"], os.path.basename(r["file"]))
    out = {}
    if a.only in (None, "southport"):
        rows = [r for r in man if r["kind"] == "southport"][: a.limit or None]
        ok, bad = load_southport(rows, a.facts_db, cfg)
        out["southport"] = {"loaded": len(ok), "failed": bad}
        pd.DataFrame(ok).to_csv(os.path.join(a.archive, "southport_loaded.csv"), index=False)
    if a.only in (None, "brazil"):
        rows = sorted([r for r in man if r["kind"] == "brazil"], key=lambda r: r["received"])[: a.limit or None]
        ok, bad = load_brazil(rows, a.brazil_db, a.workers)
        out["brazil"] = {"loaded": len(ok), "failed": bad}
        pd.DataFrame(ok).to_csv(os.path.join(a.archive, "brazil_loaded.csv"), index=False)
        if ok:
            out["brazil"]["snapshots"], out["brazil"]["events"] = facts_from_brazil(a.brazil_db, a.facts_db, cfg)
    json.dump(out, open(os.path.join(a.archive, "backfill_result.json"), "w"), indent=2, default=str)
    for k, v in out.items():
        print(k, "loaded:", v["loaded"], "failed:", len(v["failed"]))
        for f in v["failed"][:25]:
            print("   FAIL", f)
