"""The pollers scan a date window (not 'newest N'), so a file buried deep in a busy Inbox is still found.

    pytest tests/test_pollers.py -v
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault("fcntl", types.ModuleType("fcntl"))        # the poller is Linux-only (droplet)

import southport_poller as sp


def msg(i, subject, sender="payton.minder@gfh.ag", received="2026-10-13T13:44:00Z", att=True):
    return {"id": f"m{i}", "subject": subject, "receivedDateTime": received, "hasAttachments": att,
            "from": {"emailAddress": {"name": sender, "address": sender}}}


@pytest.fixture
def fake_graph(monkeypatch):
    inbox = ([msg(i, f"noise {i}", sender="x@y.com") for i in range(150)]                # 150 newer, unrelated messages
             + [msg(200, "FW: SOUTHPORT GRAIN REPORT - SOUTHPORT LINEUP 10.13.2026")]
             + [msg(201, "SOUTHPORT LINEUP 10.09.2026", received="2026-10-09T13:00:00Z")]
             + [msg(202, "Southport (no attachment)", att=False)])
    pages = [{"value": inbox[:100], "@odata.nextLink": "next"}, {"value": inbox[100:]}]
    calls = iter(pages)
    monkeypatch.setattr(sp, "_page", lambda token, url, params=None: next(calls))
    stats = {"m201"}                                                         # a monthly STATS workbook, not a line-up
    monkeypatch.setattr(sp, "graph_get", lambda token, path, **kw: {"value": (
        [{"id": "s1", "name": "PNW STATS SEPT 2026.xlsx"}] if any(m in path for m in stats) else
        [{"id": "a1", "name": "ignore.xlsx"}, {"id": "a2", "name": "SOUTHPORT LINEUP 10.13.2026.xlsx"}])})


def test_finds_a_southport_file_deep_in_the_inbox_oldest_first(fake_graph):
    out = sp.find_new_southport_emails("t", "u", set())
    assert [e[0] for e in out] == ["m200"]                           # the STATS-only message is not a line-up
    assert out[0][3:] == ("a2", "SOUTHPORT LINEUP 10.13.2026.xlsx")


def test_skips_already_processed_messages(fake_graph):
    assert sp.find_new_southport_emails("t", "u", {"m200"}) == []


# ── a failed push must not leave the repo mid-rebase ─────────────────────────

def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def conflicting_clones(tmp_path, monkeypatch):
    """The droplet's clone and a second clone that pushed a different facts.db first (a binary conflict)."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(remote))
    clones = {}
    for name in ("droplet", "other"):
        d = tmp_path / name
        _git(tmp_path, "clone", "-q", str(remote), str(d))
        _git(d, "config", "user.name", "t")
        _git(d, "config", "user.email", "t@t")
        clones[name] = d
    (clones["droplet"] / "facts.db").write_bytes(b"base")
    _git(clones["droplet"], "add", "facts.db")
    _git(clones["droplet"], "commit", "-q", "-m", "base")
    _git(clones["droplet"], "push", "-q", "origin", "master")
    _git(clones["other"], "pull", "-q")
    (clones["other"] / "facts.db").write_bytes(b"other side")
    _git(clones["other"], "commit", "-q", "-am", "other")
    _git(clones["other"], "push", "-q")
    (clones["droplet"] / "facts.db").write_bytes(b"droplet side")
    (clones["droplet"] / "Vessel Lineup - US.xlsx").write_bytes(b"x")
    monkeypatch.setattr(sp, "REPO_DIR", clones["droplet"])
    monkeypatch.setattr(sp, "FACTS_DB", clones["droplet"] / "facts.db")
    return clones["droplet"]


def test_a_conflicting_push_aborts_the_rebase_instead_of_leaving_a_detached_head(conflicting_clones):
    d = conflicting_clones
    with pytest.raises(RuntimeError, match="pull --rebase failed"):
        sp.git_push("test")
    assert _git(d, "rev-parse", "--abbrev-ref", "HEAD").strip() == "master"            # not "HEAD" (detached)
    assert not (d / ".git" / "rebase-merge").exists() and not (d / ".git" / "rebase-apply").exists()
