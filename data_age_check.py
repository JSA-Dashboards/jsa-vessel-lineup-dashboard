"""Heartbeat for the vessel line-up data: is what the dashboard shows current, and is the droplet publishing it?

The pollers log their own failures but nobody reads poller.log. On 10-27 the droplet's clone was left mid-rebase, every
push failed, the data on the droplet stayed fresh and the dashboard (which reads GitHub) sat on 10-27 for two weeks.
This checks the three things that would have caught it the next morning:

  1. the PUBLISHED facts.db (origin/master, what the app fetches) has a line-up no more than MAX_LAG weekdays old per source
  2. the droplet's own facts.db is not ahead of the published one (ingested but not pushed)
  3. the clone is healthy: on a branch, not mid-rebase, nothing committed-but-unpushed for UNPUSHED_MINUTES

Exit 0 = fine, 2 = something to look at (printed). Run daily by cron-alert (deploy/data-age.cron), which emails on non-zero.

    python data_age_check.py            # on the droplet
    python data_age_check.py --no-fetch # skip `git fetch` (tests)
"""

import argparse
import datetime as dt
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
MAX_LAG = {"US": 3, "Brazil": 3, "Argentina": 4}     # weekdays behind (counting today) before it is an alert; Nabsa skips days
UNPUSHED_MINUTES = 45                                # a push normally lands seconds after its commit


def weekdays_behind(newest, today):
    """Weekdays after `newest` up to and including `today` (0 = newest is today or in the future)."""
    n, d = 0, newest
    while d < today:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def newest_by_source(db_path):
    """{source: newest line-up report_date} from a facts.db."""
    c = sqlite3.connect(str(db_path))
    try:
        rows = c.execute("SELECT source, MAX(report_date) FROM lineup_fact GROUP BY source").fetchall()
    finally:
        c.close()
    return {s: dt.date.fromisoformat(str(d)[:10]) for s, d in rows if d}


def evaluate(local, published, today, max_lag=None):
    """Problems (list of strings) from the newest dates per source on the droplet and on GitHub."""
    out = []
    for src, limit in (max_lag or MAX_LAG).items():
        pub, loc = published.get(src), local.get(src)
        if pub is None:
            out.append(f"{src}: no line-up at all on GitHub")
            continue
        if loc and weekdays_behind(pub, loc) >= 1:
            out.append(f"{src}: the droplet has {loc} but GitHub only {pub} - ingested but not published")
        lag = weekdays_behind(pub, today)
        if lag >= limit and not (loc and weekdays_behind(pub, loc) >= 1):
            out.append(f"{src}: newest line-up on GitHub is {pub}, {lag} weekdays old (limit {limit}) - nothing new has arrived")
    return out


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def repo_problems(repo, fetch=True, now=None):
    """Clone health: branch checked out, no rebase/merge in progress, nothing stuck unpushed."""
    repo = Path(repo)
    out = []
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head == "HEAD":
        out.append("the clone is on a detached HEAD (a failed rebase?) - pushes will fail")
    git_dir = repo / ".git"
    for name, what in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge")):
        if (git_dir / name).exists():
            out.append(f"a {what} is in progress in the clone - pushes will fail")
            break
    if fetch:
        f = _git(repo, "fetch", "-q")
        if f.returncode != 0:
            out.append("git fetch failed: " + (f.stderr.strip() or f.stdout.strip())[:200])
    stamps = _git(repo, "log", "origin/master..HEAD", "--format=%ct").stdout.split()
    if stamps:
        now = now or dt.datetime.now().timestamp()
        age = (now - min(int(s) for s in stamps)) / 60
        if age >= UNPUSHED_MINUTES:
            out.append(f"{len(stamps)} commit(s) not pushed, the oldest {age:.0f} minutes old")
    return out


def published_newest(repo):
    """newest_by_source() of origin/master's facts.db, i.e. what the dashboard fetches."""
    blob = subprocess.run(["git", "-C", str(repo), "show", "origin/master:facts.db"], capture_output=True)
    if blob.returncode != 0:
        raise RuntimeError("cannot read origin/master:facts.db: " + blob.stderr.decode(errors="replace")[:200])
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as t:
        t.write(blob.stdout)
    try:
        return newest_by_source(t.name)
    finally:
        Path(t.name).unlink(missing_ok=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)
    problems = repo_problems(REPO, fetch=not a.no_fetch)
    try:
        local, published = newest_by_source(REPO / "facts.db"), published_newest(REPO)
    except Exception as e:                      # unreadable data is itself a finding
        problems.append(f"could not read the data: {e}")
    else:
        today = dt.date.today()
        problems += evaluate(local, published, today)
        print("newest line-up  " + "   ".join(f"{s}: droplet {local.get(s)} / GitHub {published.get(s)}" for s in MAX_LAG))
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
