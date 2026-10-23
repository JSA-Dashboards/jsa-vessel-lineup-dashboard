#!/usr/bin/env python3
"""Load Ocean's Argentina line-up PDF history (see fact_model/ocean_argentina.py) into facts.db.

    python backfill_argentina.py <folder of PDFs> [--db facts.db] [--workers 3] [--dry-run]

PDFs are parsed in parallel (about 4 s each), then banked one by one under the same lock the pollers use. Each report's
line-up is loaded only if it ties to the report's own product totals; its sailed list only if it is complete. Re-running
is safe: snapshots are replaced per date and sail events are upserted by id.
"""

import argparse
import contextlib
import glob
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


@contextlib.contextmanager
def facts_lock():
    try:
        import fcntl
    except ImportError:                                    # Windows: no pollers run here
        yield
        return
    path = os.path.join(HERE, "logs", ".facts.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _parse(path):
    from fact_model import ocean_argentina as oa
    try:
        return path, oa.parse_pdf(path), None
    except Exception as e:                                 # one unreadable PDF must not stop the run
        return path, None, repr(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--db", default=os.path.join(HERE, "facts.db"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    from fact_model import ocean_argentina as oa, store
    files = sorted(glob.glob(os.path.join(a.folder, "*.pdf")))
    print(f"{len(files)} PDFs in {a.folder}", flush=True)
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        parsed = list(ex.map(_parse, files))
    results, errors = [], []
    with facts_lock():
        for path, p, err in parsed:
            name = os.path.basename(path)
            if err or p is None or p["report_date"] is None:
                errors.append((name, err or "no report date"))
                continue
            if a.dry_run:
                ok, bad = oa.validate(p)
                done, short = oa.sailed_complete(p)
                results.append({"file": name, "report_date": str(p["report_date"].date()), "lineup_ok": ok, "lineup_mismatch": bad,
                                "sailed_complete": done, "queued_vessels": 0, "sail_events": 0})
                continue
            r = oa.ingest_parsed(p, a.db)
            results.append({"file": name, **r})
    df = pd.DataFrame(results)
    if df.empty:
        print("nothing loaded", errors)
        return
    print(f"\nreports parsed: {len(df)} | line-up loaded: {int(df['lineup_ok'].sum())} | sailed complete: {int(df['sailed_complete'].sum())} "
          f"| sail events: {int(df['sail_events'].sum())} | unreadable: {len(errors)}")
    bad = df[~df["lineup_ok"]]
    if len(bad):
        print("\nLINE-UP NOT LOADED (does not tie to the report's totals):")
        for _, r in bad.iterrows():
            print(f"  {r['report_date']}  {r['file']}  {r['lineup_mismatch']}")
    for n, e in errors:
        print("UNREADABLE:", n, e)
    if not a.dry_run:
        l, e = store.load_lineup(a.db), store.load_events(a.db)
        ar_l, ar_e = l[l["source"] == "Argentina"], e[e["source"] == "Argentina"]
        print(f"\nfacts.db Argentina: {ar_l['report_date'].nunique()} line-up dates "
              f"{ar_l['report_date'].min():%Y-%m-%d}..{ar_l['report_date'].max():%Y-%m-%d}, {len(ar_e)} sail events "
              f"{ar_e['sail_date'].min():%Y-%m-%d}..{ar_e['sail_date'].max():%Y-%m-%d}")
        um = ar_l[~ar_l["mapped"]].groupby(["port", "elevator"])["kmt"].sum()
        if len(um):
            print("UNMAPPED ports/terminals:", um.to_dict())


if __name__ == "__main__":
    main()
