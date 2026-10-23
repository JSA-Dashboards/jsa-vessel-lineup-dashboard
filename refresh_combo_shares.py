#!/usr/bin/env python3
"""Rebuild and vet the combo-boat calibration (see fact_model/combo_calibration.py) in facts.db.

    python refresh_combo_shares.py [--facts-db facts.db]

Fetches USDA FGIS ship inspections (by vessel), matches them to the combo sail events, learns how each combo type splits,
re-runs the walk-forward check of the monthly totals against USDA, and saves everything the dashboard reads. Exit code 2 when
the health check says 'fail' (so the droplet's cron-alert emails): the learned split stopped beating the placeholder, too few
vessels matched, or splitting combos makes the monthly totals fit USDA worse than leaving them out.
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
    print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in meta.items()})
    print(f"COMBO CHECK: {meta['status'].upper()}" + (f" - {meta['reasons']}" if meta["reasons"] else ""))
    sys.exit(2 if meta["status"] == "fail" else 0)
