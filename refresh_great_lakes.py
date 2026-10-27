#!/usr/bin/env python3
"""Reload the Great Lakes loadings (USDA FGIS ship inspections at Toledo, Duluth-Superior, Chicago, Seaway) into facts.db.

    python refresh_great_lakes.py [--facts-db facts.db] [--start-year 2015]

USDA adds a week of data on Thursdays; events are upserted by id, so re-running is safe.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fact_model import greatlakes  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-db", default=os.path.join(HERE, "facts.db"))
    ap.add_argument("--start-year", type=int, default=2015)
    a = ap.parse_args()
    print("great lakes:", greatlakes.refresh(a.facts_db, a.start_year))
