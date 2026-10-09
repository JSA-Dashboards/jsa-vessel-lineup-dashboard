"""Update facts.db after a source file arrives (called by the droplet jobs).

    python refresh_facts.py --brazil                         # re-derive from brazil_lineup.db
    python refresh_facts.py --southport "SOUTHPORT LINEUP 10.05.2026.xlsx" [--date 2026-10-05]
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fact_model import ingest  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--brazil", action="store_true")
    ap.add_argument("--southport", metavar="XLSX")
    ap.add_argument("--date", help="report date YYYY-MM-DD for --southport (default: from the file name)")
    ap.add_argument("--facts-db", default=None)
    ap.add_argument("--brazil-db", default=os.path.join(HERE, "brazil_lineup.db"))
    a = ap.parse_args()
    if not (a.brazil or a.southport):
        ap.error("give --brazil and/or --southport FILE")
    if a.brazil:
        print("brazil:", ingest.refresh_brazil(a.brazil_db, a.facts_db))
    if a.southport:
        print("southport:", ingest.ingest_southport_file(a.southport, report_date=a.date, facts_db=a.facts_db))
