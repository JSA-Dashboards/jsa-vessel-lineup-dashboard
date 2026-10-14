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
    monkeypatch.setattr(sp, "graph_get", lambda token, path, **kw: {"value": [
        {"id": "a1", "name": "ignore.xlsx"}, {"id": "a2", "name": "SOUTHPORT LINEUP 10.13.2026.xlsx"}]})


def test_finds_a_southport_file_deep_in_the_inbox_oldest_first(fake_graph):
    out = sp.find_new_southport_emails("t", "u", set())
    assert [e[0] for e in out] == ["m201", "m200"]                  # oldest first
    assert out[1][3:] == ("a2", "SOUTHPORT LINEUP 10.13.2026.xlsx")  # prefers the SOUTHPORT attachment


def test_skips_already_processed_messages(fake_graph):
    out = sp.find_new_southport_emails("t", "u", {"m200"})
    assert [e[0] for e in out] == ["m201"]
