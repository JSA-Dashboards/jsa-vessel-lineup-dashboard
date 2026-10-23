#!/usr/bin/env python3
"""Rebuild the combo-boat calibration (see fact_model/combo_calibration.py) in facts.db.

    python refresh_combo_shares.py [--facts-db facts.db]

Fetches USDA FGIS ship inspections (by vessel), matches them to the combo sail events, learns how each combo type splits, and
saves the tables the dashboard reads. Safe to re-run; FGIS adds a week of data on Thursdays.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fact_model import combo_calibration as cc  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-db", default=os.path.join(HERE, "facts.db"))
    a = ap.parse_args()
    meta = cc.refresh(a.facts_db)
    print(meta)
