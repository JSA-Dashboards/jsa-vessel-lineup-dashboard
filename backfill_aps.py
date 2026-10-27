#!/usr/bin/env python3
"""Extend the US Gulf + PNW line-up history from Agri Port Services' daily workbook (see fact_model/aps_lineup.py).

    python backfill_aps.py <folder of APS .xls> [--db facts.db] [--dry-run]

Loads only dates Southport does not cover, scaled to Southport's basis (ratios measured on the overlap), and records them in
the lineup_provenance table. Safe to re-run.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fact_model import aps_lineup  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--db", default=os.path.join(HERE, "facts.db"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(aps_lineup.backfill(a.folder, a.db, dry_run=a.dry_run))
