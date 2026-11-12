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
    # order: portal link, then the charts (PNW first, Argentina last), then the tables, then the footnote
    order = [html.index(we.PORTAL_URL), html.index("cid:pnw_corn"), html.index("cid:usg_corn"), html.index("cid:brazil_corn"),
             html.index("cid:argentina_soybeans"), html.index("Line-up change"), html.index("Line-up by commodity"),
             html.index("Source: daily port line-up reports")]
    assert order == sorted(order)
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
    monkeypatch.setattr(we, "build", lambda db, today, signature=None: ("s", "<html>", {}))
    monkeypatch.setattr(we, "load_signature", lambda folder=None: ("<sig>", {}))
    monkeypatch.setattr(we, "send_via_graph", lambda subject, html, images, to, bcc=(): calls.append((to, list(bcc))) or "basis-tracker@jpsi.com")
    monkeypatch.delenv("VESSEL_EMAIL_TO", raising=False)
    monkeypatch.setenv("VESSEL_EMAIL_BCC", "jsagroup@jpsi.com,CIF@jpsi.com")
    assert we.main(["--to", "kpostin@jpsi.com"]) == 0 and not (tmp_path / "state.json").exists()      # a test copy leaves no mark
    assert we.main(["--send"]) == 0
    assert calls[-1] == (["kpostin@jpsi.com"], ["jsagroup@jpsi.com", "CIF@jpsi.com"])          # Kolten on To, the list on BCC
    assert json.loads((tmp_path / "state.json").read_text())["week"] == we.week_key(dt.date.today())
    n = len(calls)
    assert we.main(["--send"]) == 0 and len(calls) == n                                               # the cron's second run does nothing
    assert we.main(["--send", "--force"]) == 0 and len(calls) == n + 1


def test_a_preview_writes_the_page_and_the_pngs_and_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(we, "build", lambda db, today, signature=None: ("s", '<img src="cid:x_y">', {"x_y": PNG}))
    monkeypatch.setattr(we, "SIGNATURE_DIR", tmp_path / "no_signature_here")                  # a preview does not need it
    monkeypatch.setattr(we, "send_via_graph", lambda *a, **k: pytest.fail("must not send"))
    assert we.main(["--preview", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "x_y.png").read_bytes() == PNG
    assert 'src="x_y.png"' in (tmp_path / "out" / "email.html").read_text(encoding="utf-8")


def _sig(tmp_path, html='<p>Kolten A. Postin</p><p>John Stewart &amp; Associates</p><img src="cid:sig_1">'):
    d = tmp_path / "sig"
    d.mkdir()
    (d / "signature.html").write_text(html, encoding="utf-8")
    (d / "sig_1.png").write_bytes(PNG)
    return d


def test_the_signature_is_loaded_with_its_pictures_and_goes_last(tmp_path, built):
    html, images = we.load_signature(_sig(tmp_path))
    assert images == {"sig_1": PNG}
    subject, page, imgs = we.build(FACTS, dt.date(2026, 11, 11), (html, images))
    assert "sig_1" in imgs and page.index("Kolten A. Postin") > page.index("Source: daily port line-up reports")
    assert 'src="cid:sig_1"' in page


def test_no_signature_no_send_but_a_preview_is_fine(monkeypatch, tmp_path):
    monkeypatch.setattr(we, "SIGNATURE_DIR", tmp_path / "nothing")
    monkeypatch.setattr(we, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(we, "build", lambda db, today, signature=None: ("s", "<html>", {}))
    monkeypatch.setattr(we, "send_via_graph", lambda *a, **k: pytest.fail("a send without the signature must not go out"))
    with pytest.raises(RuntimeError, match="signature is missing"):
        we.main(["--send"])
    with pytest.raises(RuntimeError, match="signature is missing"):
        we.main(["--to", "kpostin@jpsi.com"])
    assert we.main(["--preview", str(tmp_path / "out")]) == 0
    assert not (tmp_path / "state.json").exists()


def test_something_that_is_not_the_signature_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="does not look like the signature"):
        we.load_signature(_sig(tmp_path, '<p>hello</p>'))


def test_replies_go_to_kolten_not_the_basis_tracker_mailbox(monkeypatch, tmp_path):
    sent = _graph(monkeypatch, tmp_path)
    we.send_via_graph("s", "h", {}, ["kpostin@jpsi.com"], ["jsagroup@jpsi.com", "CIF@jpsi.com"])
    m = sent["body"]["message"]
    assert [r["emailAddress"]["address"] for r in m["replyTo"]] == ["kpostin@jpsi.com"]
    assert [r["emailAddress"]["address"] for r in m["bccRecipients"]] == ["jsagroup@jpsi.com", "CIF@jpsi.com"]


def test_a_transient_graph_error_is_retried_but_a_real_one_is_not(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("GRAPH_TENANT_ID=t\nGRAPH_CLIENT_ID=c\nGRAPH_CLIENT_SECRET=s\nGRAPH_SENDER=basis-tracker@jpsi.com\n")
    monkeypatch.setattr(we, "ENV_FILE", env)
    monkeypatch.setattr(we, "RETRY_WAIT", 0)
    monkeypatch.setitem(sys.modules, "msal", FakeMsal)
    calls = []

    def post_factory(codes):
        def post(url, headers, json, timeout):
            calls.append(1)
            return types.SimpleNamespace(status_code=codes[min(len(calls) - 1, len(codes) - 1)], text="boom")
        return types.SimpleNamespace(post=post)
    monkeypatch.setitem(sys.modules, "requests", post_factory([503, 503, 202]))
    assert we.send_via_graph("s", "h", {}, ["a@b.c"]) == "basis-tracker@jpsi.com" and len(calls) == 3          # two 503s, then through
    calls.clear()
    monkeypatch.setitem(sys.modules, "requests", post_factory([403]))
    with pytest.raises(RuntimeError, match="403"):
        we.send_via_graph("s", "h", {}, ["a@b.c"])
    assert len(calls) == 1                                                                                      # a 403 is not retried
    calls.clear()
    monkeypatch.setitem(sys.modules, "requests", post_factory([503]))
    with pytest.raises(RuntimeError, match="503"):
        we.send_via_graph("s", "h", {}, ["a@b.c"])
    assert len(calls) == we.RETRIES                                                                             # gives up after RETRIES
