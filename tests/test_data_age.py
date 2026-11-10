"""Heartbeat on data age: the 10-27..11-10 incident (droplet current, GitHub stuck) must raise an alert.

    pytest tests/test_data_age.py -v
"""

import datetime as dt
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_age_check as dac

D = dt.date


def test_weekdays_behind_skips_weekends():
    assert dac.weekdays_behind(D(2026, 11, 10), D(2026, 11, 10)) == 0
    assert dac.weekdays_behind(D(2026, 11, 9), D(2026, 11, 10)) == 1
    assert dac.weekdays_behind(D(2026, 11, 6), D(2026, 11, 9)) == 1          # Fri -> Mon: the weekend does not count
    assert dac.weekdays_behind(D(2026, 10, 27), D(2026, 11, 10)) == 10
    assert dac.weekdays_behind(D(2026, 11, 10), D(2026, 11, 6)) == 0         # newest in the future


def fresh(**kw):
    return {"US": D(2026, 11, 10), "Brazil": D(2026, 11, 9), "Argentina": D(2026, 11, 6), **kw}


def test_a_normal_morning_raises_nothing():
    today = D(2026, 11, 10)
    assert dac.evaluate(fresh(), fresh(), today) == []


def test_the_incident_droplet_current_github_stuck_is_caught():
    stuck = {"US": D(2026, 10, 27), "Brazil": D(2026, 10, 27), "Argentina": D(2026, 10, 27)}
    out = dac.evaluate(fresh(), stuck, D(2026, 11, 3))                      # a week after the push stopped
    assert any("ingested but not published" in p and p.startswith("US") for p in out)
    assert not any("nothing new has arrived" in p for p in out)             # it is a publish problem, not a feed problem


def test_a_feed_that_stopped_is_caught_when_both_sides_are_equally_old():
    old = {"US": D(2026, 11, 4), "Brazil": D(2026, 11, 9), "Argentina": D(2026, 11, 6)}
    out = dac.evaluate(old, old, D(2026, 11, 10))
    assert out == ["US: newest line-up on GitHub is 2026-11-04, 4 weekdays old (limit 3) - nothing new has arrived"]


def test_argentina_gets_more_slack_for_skipped_days():
    arg = {"Argentina": D(2026, 11, 5)}
    assert dac.evaluate(arg, arg, D(2026, 11, 10), max_lag={"Argentina": 4}) == []      # 3 weekdays: Nabsa skipped Monday
    assert dac.evaluate(arg, arg, D(2026, 11, 11), max_lag={"Argentina": 4}) != []


def test_a_source_missing_from_github_is_reported():
    assert dac.evaluate(fresh(), {"US": D(2026, 11, 10)}, D(2026, 11, 10)) == [
        "Brazil: no line-up at all on GitHub", "Argentina: no line-up at all on GitHub"]


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def clone(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(remote))
    d = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(remote), str(d))
    _git(d, "config", "user.name", "t")
    _git(d, "config", "user.email", "t@t")
    (d / "f").write_text("1")
    _git(d, "add", "f")
    _git(d, "commit", "-q", "-m", "one")
    _git(d, "push", "-q", "origin", "master")
    return d


def test_a_healthy_clone_has_no_problems(clone):
    assert dac.repo_problems(clone) == []


def test_a_detached_head_is_reported(clone):
    _git(clone, "checkout", "-q", "--detach")
    assert any("detached HEAD" in p for p in dac.repo_problems(clone))


def test_a_commit_stuck_unpushed_is_reported_only_after_a_while(clone):
    (clone / "f").write_text("2")
    _git(clone, "commit", "-q", "-am", "two")
    assert dac.repo_problems(clone) == []                                   # just committed: the push is about to land
    later = dt.datetime.now().timestamp() + 3600
    assert any("not pushed" in p for p in dac.repo_problems(clone, now=later))


def test_an_in_progress_rebase_is_reported(clone):
    (clone / ".git" / "rebase-merge").mkdir()
    assert any("rebase is in progress" in p for p in dac.repo_problems(clone))


def test_newest_by_source_reads_facts_db(tmp_path):
    p = tmp_path / "f.db"
    c = sqlite3.connect(p)
    c.execute("create table lineup_fact (source text, report_date text)")
    c.executemany("insert into lineup_fact values (?,?)", [("US", "2026-11-09"), ("US", "2026-11-10"), ("Brazil", "2026-11-06")])
    c.commit()
    c.close()
    assert dac.newest_by_source(p) == {"US": D(2026, 11, 10), "Brazil": D(2026, 11, 6)}
