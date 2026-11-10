"""The weekly vessel line-up email: content, the Graph payload, and the once-a-week guard.

    pytest tests/test_weekly_email.py -v
"""

import datetime as dt
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weekly_email as we

FACTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "facts.db")
PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def built():
    if not os.path.exists(FACTS):
        pytest.skip("no facts.db")
    return we.build(FACTS, dt.date(2026, 11, 11))


def test_the_email_has_the_portal_link_first_and_every_section(built):
    subject, html, images = built
    assert subject == "JSA Vessel Line-up · Wed Nov 11"
    assert html.index(we.PORTAL_URL) < html.index("Line-up change") < html.index("Line-up by commodity")
    for name in ("Pacific Northwest (PNW)", "US Gulf (USG)", "Brazil", "Argentina"):
        assert f">{name}<" in html
    for area in ("PNW", "USG", "TXG", "Brazil", "Argentina"):                      # the trend table lists every area
        assert f">{area}</td>" in html
    assert "vs last week" in html and "vs last month" in html and "vs last year" in html


def test_corn_and_soybean_charts_for_each_of_the_four_areas(built):
    _, html, images = built
    assert sorted(images) == sorted(f"{a}_{c}" for a in ("pnw", "usg", "brazil", "argentina") for c in ("corn", "soybeans"))
    assert all(png.startswith(PNG) and len(png) > 5000 for png in images.values())
    assert all(f'src="cid:{cid}"' in html for cid in images)                         # every image is referenced, nothing is orphaned
    assert "txg" not in " ".join(images)                                              # Texas Gulf is in the tables only


def test_numbers_match_the_dashboard(built):
    # the dashboard's USG line-up must be what the email's USG row says
    import overview as ov
    data = we.load(FACTS)
    r = we._roll(data["detail_v"], we.SCOPES["USG"])
    assert f"<b>{ov._num(r['now'])}</b>" in built[1]


def test_volume_is_whole_numbers_and_vessels_keep_their_decimal():
    assert we._n(1234.6, "kmt") == "1,235" and we._n(12.2, "vessels") == "12.2" and we._n(float("nan"), "kmt") == "n/a"
    assert we._d(-834.6, "kmt") == "−835" and we._d(0.2, "kmt") == "0" and we._d(5.5, "vessels") == "+5.5"


def test_stale_data_is_called_out():
    import pandas as pd
    meta = pd.DataFrame([{"source": "US", "as_of": pd.Timestamp("2026-11-04")}, {"source": "Brazil", "as_of": pd.Timestamp("2026-11-10")}])
    assert we.stale_sources(meta, dt.date(2026, 11, 11)) == ["United States (as of Nov 04)"]


class FakeMsal:
    class ConfidentialClientApplication:
        def __init__(self, client_id, authority, client_credential):
            self.client_id = client_id

        def acquire_token_for_client(self, scopes):
            return {"access_token": "tok"}


def _graph(monkeypatch, tmp_path, status=202):
    env = tmp_path / ".env"
    env.write_text("GRAPH_TENANT_ID=t\nGRAPH_CLIENT_ID=c\nGRAPH_CLIENT_SECRET=s\nGRAPH_SENDER=basis-tracker@jpsi.com\n")
    monkeypatch.setattr(we, "ENV_FILE", env)
    sent = {}
    monkeypatch.setitem(sys.modules, "msal", FakeMsal)

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, body=json)
        return types.SimpleNamespace(status_code=status, text="")
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    return sent


def test_graph_payload_inlines_the_charts_and_puts_the_list_on_bcc(monkeypatch, tmp_path):
    sent = _graph(monkeypatch, tmp_path)
    who = we.send_via_graph("subj", "<img src='cid:pnw_corn'>", {"pnw_corn": PNG + b"x"}, ["kpostin@jpsi.com"], ["list@jpsi.com"])
    m = sent["body"]["message"]
    assert who == "basis-tracker@jpsi.com" and sent["url"].endswith("/users/basis-tracker@jpsi.com/sendMail")
    assert [r["emailAddress"]["address"] for r in m["toRecipients"]] == ["kpostin@jpsi.com"]
    assert [r["emailAddress"]["address"] for r in m["bccRecipients"]] == ["list@jpsi.com"]
    att = m["attachments"][0]
    assert att["contentId"] == "pnw_corn" and att["isInline"] and att["contentType"] == "image/png"


def test_a_failed_send_raises(monkeypatch, tmp_path):
    _graph(monkeypatch, tmp_path, status=403)
    with pytest.raises(RuntimeError, match="403"):
        we.send_via_graph("s", "h", {}, ["a@b.c"])


def test_the_live_send_goes_out_once_per_week_and_a_test_copy_does_not_count(monkeypatch, tmp_path):
    monkeypatch.setattr(we, "STATE", tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(we, "build", lambda db, today: ("s", "<html>", {}))
    monkeypatch.setattr(we, "send_via_graph", lambda subject, html, images, to, bcc=(): calls.append((to, list(bcc))) or "basis-tracker@jpsi.com")
    monkeypatch.delenv("VESSEL_EMAIL_TO", raising=False)
    monkeypatch.setenv("VESSEL_EMAIL_BCC", "a@jpsi.com; b@jpsi.com")
    assert we.main(["--to", "kpostin@jpsi.com"]) == 0 and not (tmp_path / "state.json").exists()      # a test copy leaves no mark
    assert we.main(["--send"]) == 0
    assert calls[-1] == (["kpostin@jpsi.com"], ["a@jpsi.com", "b@jpsi.com"])
    assert json.loads((tmp_path / "state.json").read_text())["week"] == we.week_key(dt.date.today())
    n = len(calls)
    assert we.main(["--send"]) == 0 and len(calls) == n                                               # the cron's second run does nothing
    assert we.main(["--send", "--force"]) == 0 and len(calls) == n + 1


def test_a_preview_writes_the_page_and_the_pngs_and_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(we, "build", lambda db, today: ("s", '<img src="cid:x_y">', {"x_y": PNG}))
    monkeypatch.setattr(we, "send_via_graph", lambda *a, **k: pytest.fail("must not send"))
    assert we.main(["--preview", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "x_y.png").read_bytes() == PNG
    assert 'src="x_y.png"' in (tmp_path / "out" / "email.html").read_text(encoding="utf-8")
