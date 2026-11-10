"""Weekly vessel line-up email (Wednesdays 4 PM CT, from the droplet).

Top: a link to the vessel line-up portal. Then the line-up for PNW, USG, TXG, Brazil and Argentina: the change vs last week / month /
year, and by commodity. Then, for PNW, USG, Brazil and Argentina (every port and terminal), corn and soybeans each: the line-up over
time with where the line-up is headed. The numbers come from the dashboard's own data prep (`overview._prepare`: combo boats split from
USDA's measured shares, undisclosed cargoes filled, data gaps skipped), so the email and the app agree.

    python weekly_email.py --preview OUTDIR          # write email.html + the chart PNGs, send nothing
    python weekly_email.py --to kpostin@jpsi.com     # a test copy to one address (does not count as the week's send)
    python weekly_email.py --send                    # the live send: To VESSEL_EMAIL_TO (default Kolten), BCC VESSEL_EMAIL_BCC; once per ISO week

Sent through Microsoft Graph as the basis-tracker@ mailbox (Mail.Send), credentials read from /opt/basis-tracker/.env like the cron
alerts do (VESSEL_EMAIL_ENV overrides the path). Needs streamlit + plotly (overview imports them) and matplotlib (the charts).
"""

import argparse
import base64
import datetime as dt
import io
import json
import os
import sys
from html import escape
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates          # noqa: E402
import matplotlib.pyplot as plt            # noqa: E402
import pandas as pd                        # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import data_age_check as dac               # noqa: E402
import overview as ov                      # noqa: E402
from fact_model import changes as ch       # noqa: E402

PORTAL_URL = os.environ.get("VESSEL_PORTAL_URL", "https://jsa-vessel-lineup-dashboard.streamlit.app")
DEFAULT_TO = "kpostin@jpsi.com"
STATE = HERE / "logs" / ".weekly_email_state.json"
ENV_FILE = Path(os.environ.get("VESSEL_EMAIL_ENV", "/opt/basis-tracker/.env"))
GRAPH = "https://graph.microsoft.com/v1.0"

SCOPES = dict(ov.SCOPES)
TABLE_AREAS = ["PNW", "USG", "TXG", "Brazil", "Argentina"]               # the line-up tables, in this order
CHART_AREAS = ["PNW", "USG", "Brazil", "Argentina"]                       # corn and soybean charts for each
CHART_COMMS = ["Corn", "Soybeans"]
HEADING = {"PNW": "Pacific Northwest (PNW)", "USG": "US Gulf (USG)", "TXG": "Texas Gulf (TXG)", "Brazil": "Brazil", "Argentina": "Argentina"}
INK, MUTED, BORDER, ACCENT, BAND = ov.INK, ov.MUTED, ov.BORDER, "#0693e3", "#eef3f0"
FONT = "Segoe UI, Arial, Helvetica, sans-serif"


# ── data ─────────────────────────────────────────────────────────────────────

def load(facts_db):
    """Everything the email needs from the dashboard's own prep: vessels and volume views, same options as the app's defaults."""
    mtime = os.path.getmtime(facts_db)
    v = ov._prepare(facts_db, mtime, "vessels", True, True)
    k = ov._prepare(facts_db, mtime, "kmt", True, True)
    return {"lineup": v[0], "detail_v": v[2], "detail_k": k[2], "meta": v[3], "gaps": v[4], "left_out": v[5]}


def _roll(detail, sel):
    d = ov._apply(detail, sel)
    return None if d.empty else ch.rollup(d.assign(_t="t"), ["_t"]).iloc[0]


def asof_line(meta):
    bits = [f"{ov.SRC_LABEL.get(s, s)} {pd.Timestamp(r['as_of']):%b %d}" for s, r in meta.groupby("source").agg(as_of=("as_of", "max")).iterrows()]
    return "Line-up as of " + " · ".join(bits)


def stale_sources(meta, today):
    """Sources whose newest line-up is 3+ weekdays old: the email says so rather than showing old numbers as current."""
    out = []
    for s, r in meta.groupby("source").agg(as_of=("as_of", "max")).iterrows():
        if dac.weekdays_behind(pd.Timestamp(r["as_of"]).date(), today) >= 3:
            out.append(f"{ov.SRC_LABEL.get(s, s)} (as of {pd.Timestamp(r['as_of']):%b %d})")
    return out


# ── charts ───────────────────────────────────────────────────────────────────

def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#c9d6ce")
    ax.tick_params(colors=MUTED, labelsize=8.5, length=3)
    ax.grid(axis="y", color="#e3ebe6", lw=0.8)
    ax.set_axisbelow(True)


def series(data, area, commodity):
    """Vessels per snapshot date for one area and commodity over the last year. Every snapshot date the area was reported on is a point
    (0 where the commodity was absent), and data-gap dates are skipped, so a quiet commodity is not drawn as a straight line."""
    sel = {k: v for k, v in SCOPES[area].items()}
    region = ov._apply(data["lineup"], sel)
    g = data["gaps"].assign(_gap=1)
    region = region.merge(g, on=["source", "report_date"], how="left")
    region = region[region["_gap"].isna()]
    if region.empty:
        return pd.Series(dtype=float)
    dates = pd.DatetimeIndex(sorted(region["report_date"].unique()))
    dates = dates[dates >= dates.max() - pd.DateOffset(months=12)]
    s = region[region["commodity"] == commodity].groupby("report_date")["vessels"].sum()
    return s.reindex(dates).fillna(0.0)


def destinations(data, area, commodity, top=10):
    """(Series of vessels by destination for the current line-up, share of vessels with no destination reported)."""
    ln = ov._apply(data["lineup"], {**SCOPES[area], "commodity": [commodity]})
    cur = ch.current_rows(ln, data["meta"])
    known = cur[cur["destination"].astype(str).str.lower() != "unknown"]
    unk = cur.loc[cur["destination"].astype(str).str.lower() == "unknown", "vessels"].sum() / max(cur["vessels"].sum(), 1e-9)
    return known.groupby("destination")["vessels"].sum().nlargest(top), float(unk)


def chart_png(data, area, commodity):
    color = ov.COMM_COLORS[commodity]
    s, (dest, unk) = series(data, area, commodity), destinations(data, area, commodity)
    fig = plt.figure(figsize=(11.2, 3.6), dpi=120, facecolor="white")
    gs = fig.add_gridspec(1, 2, width_ratios=[2.15, 1], wspace=0.34, left=0.055, right=0.985, top=0.80, bottom=0.14)
    a1, a2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    fig.text(0.012, 0.955, f"{HEADING[area]} · {commodity}", fontsize=13, fontweight="bold", color=INK, va="top")
    a1.set_title("Line-up over time (vessels)", loc="left", fontsize=9.5, color=MUTED, pad=8)
    _style(a1)
    if len(s):
        a1.fill_between(s.index, s.values, color=color, alpha=0.42, lw=0)
        a1.plot(s.index, s.values, color=color, lw=1.8)
        a1.set_ylim(0, max(s.max() * 1.12, 1))
        loc = mdates.AutoDateLocator(minticks=3, maxticks=7)
        a1.xaxis.set_major_locator(loc)
        a1.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
        a1.margins(x=0.01)
        a1.annotate(ov._num(s.iloc[-1]), (s.index[-1], s.iloc[-1]), xytext=(-4, 6), textcoords="offset points", ha="right",
                    fontsize=9, fontweight="bold", color=INK)
    else:
        a1.text(0.5, 0.5, "No line-up history", transform=a1.transAxes, ha="center", color=MUTED)
    a2.set_title("Where the line-up is headed (vessels)", loc="left", fontsize=9.5, color=MUTED, pad=8)
    _style(a2)
    a2.grid(axis="y", visible=False)
    a2.grid(axis="x", color="#e3ebe6", lw=0.8)
    if len(dest):
        n = len(dest)
        a2.barh(range(n), dest.values, color=color, height=0.68)
        a2.set_yticks(range(n), dest.index)
        for y, v in enumerate(dest.values):
            a2.text(v, y, " " + ov._num(v), va="center", fontsize=8.5, color=INK)
        a2.set_xlim(0, dest.max() * 1.18)
        a2.set_ylim(max(n, 5) - 0.5, -0.5)                  # biggest on top, and a lone destination is not stretched to fill the panel
        a2.tick_params(axis="y", labelsize=8.5, length=0)
        if unk > 0.05:
            fig.text(0.985, 0.02, f"{unk:.0%} of the vessels have no destination reported (not shown)", fontsize=7.5, color=MUTED, ha="right")
    else:
        a2.text(0.5, 0.5, "No destinations reported", transform=a2.transAxes, ha="center", color=MUTED)
        a2.set_xticks([])
        a2.set_yticks([])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ── html ─────────────────────────────────────────────────────────────────────

def _td(text, align="right", bold=False, color=None, bg=None, extra=""):
    st = f"padding:7px 8px;border-bottom:1px solid {BORDER};font-size:13px;text-align:{align};white-space:nowrap;" + ("font-weight:700;" if bold else "")
    st += f"color:{color or INK};" + (f"background:{bg};" if bg else "") + extra
    return f'<td style="{st}">{text}</td>'


def _th(text, align="right"):
    return (f'<th style="padding:7px 8px;background:{BAND};border-bottom:2px solid {ACCENT};font-size:11px;letter-spacing:.04em;'
            f'text-transform:uppercase;color:{MUTED};text-align:{align};font-weight:700;">{escape(text)}</th>')


def _tint(text):
    return ov.GREEN if text.startswith("+") else ov.RED if text.startswith("−") else MUTED if text in ("n/a", "0", "0%") else INK


def _n(v, measure="vessels"):
    """A count: vessels keep one decimal when fractional (combo splits), volume is whole kMT."""
    return ov._num(v) if measure == "vessels" else ("n/a" if pd.isna(v) else f"{v:,.0f}")


def _d(v, measure="vessels"):
    if measure == "vessels" or pd.isna(v):
        return ov._delta(v)
    return "0" if abs(v) < 0.5 else ("+" if v > 0 else "−") + f"{abs(v):,.0f}"


def _chg(d, p, unit_pct=True, measure="vessels"):
    dd = _d(d, measure)
    txt = dd if p is None or pd.isna(p) or not unit_pct else f"{dd} ({ov._pct(p)})"
    return f'<span style="color:{_tint(dd)};font-weight:600;">{escape(txt)}</span>'


def trend_table(detail, measure):
    unit = "vessels" if measure == "vessels" else "kMT"
    rows = []
    for name in TABLE_AREAS:
        r = _roll(detail, SCOPES[name])
        if r is None:
            rows.append("<tr>" + _td(name, "left", True) + _td("—") * 4 + "</tr>")
            continue
        rows.append("<tr>" + _td(name, "left", True) + _td(f"<b>{_n(r['now'], measure)}</b>")
                    + "".join(_td(_chg(r[f"d_{k}"], r[f"p_{k}"], measure=measure)) for k in ("wk", "mo", "yr")) + "</tr>")
    head = "<tr>" + _th("Area", "left") + _th(f"Line-up now ({unit})") + _th("vs last week") + _th("vs last month") + _th("vs last year") + "</tr>"
    return f'<table role="presentation" cellspacing="0" cellpadding="0" width="100%" style="border-collapse:collapse;">{head}{"".join(rows)}</table>'


def commodity_table(detail, measure):
    unit = "vessels" if measure == "vessels" else "kMT"
    cols = {n: ov._by_commodity(ov._apply(detail, SCOPES[n]), "now") for n in TABLE_AREAS}
    m = pd.DataFrame(cols)[TABLE_AREAS]
    m = m[(m.sum(axis=1) > 0)]
    head = "<tr>" + _th(f"Commodity ({unit})", "left") + "".join(_th(n) for n in TABLE_AREAS) + "</tr>"
    rows = []
    for c, r in m.iterrows():
        rows.append("<tr>" + _td(f'<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:{ov.COMM_COLORS.get(c, INK)};'
                                 f'margin-right:7px;"></span>{escape(c)}', "left")
                    + "".join(_td("" if v == 0 else _n(v, measure)) for v in r.values) + "</tr>")
    tot = m.sum()
    rows.append("<tr>" + _td("Total", "left", True, bg=BAND) + "".join(_td(_n(v, measure), bold=True, bg=BAND) for v in tot.values) + "</tr>")
    return f'<table role="presentation" cellspacing="0" cellpadding="0" width="100%" style="border-collapse:collapse;">{head}{"".join(rows)}</table>'


def stat_line(data, area, commodity):
    """'12 vessels · 435 kMT · vs LW +2 · LM −3 · LY +5' for one area and commodity."""
    sel = {**SCOPES[area], "commodity": [commodity]}
    v, k = _roll(data["detail_v"], sel), _roll(data["detail_k"], sel)
    if v is None:
        return f'<span style="color:{MUTED};">Nothing in the line-up now.</span>'
    bits = [f"<b>{ov._num(v['now'])}</b> vessels"] + ([f"<b>{_n(k['now'], 'kmt')}</b> kMT"] if k is not None else [])
    chg = " · ".join(f"{lbl} {_chg(v[f'd_{key}'], None)}" for lbl, key in (("vs last week", "wk"), ("last month", "mo"), ("last year", "yr")))
    return f'<span style="color:{INK};">{" · ".join(bits)}</span> <span style="color:{MUTED};">&nbsp;|&nbsp; {chg}</span>'


def h2(text, sub=""):
    s = f'<div style="font-size:12px;color:{MUTED};margin-top:2px;">{sub}</div>' if sub else ""
    return (f'<div style="margin:26px 0 8px;"><div style="font-size:16px;font-weight:700;color:{INK};border-left:4px solid {ACCENT};'
            f'padding-left:10px;">{escape(text)}</div>{s}</div>')


def build(facts_db, today=None):
    """(subject, html, {cid: png bytes}) for the email."""
    today = today or dt.date.today()
    data = load(facts_db)
    images, blocks = {}, []
    stale = stale_sources(data["meta"], today)
    notes = [n for n in data["meta"]["note"].unique() if n]
    banner = ""
    if stale:
        banner = (f'<div style="background:#fff4e5;border:1px solid #f0c48a;color:#7a4a00;padding:9px 12px;font-size:13px;margin:0 0 14px;">'
                  f'Heads-up: no fresh line-up for {escape(", ".join(stale))}.</div>')
    blocks.append(h2("Line-up change", "Vessels in the line-up now, and the change vs last week, last month and last year."))
    blocks.append(trend_table(data["detail_v"], "vessels"))
    blocks.append('<div style="height:14px;"></div>' + trend_table(data["detail_k"], "kmt"))
    blocks.append(h2("Line-up by commodity", "Vessels, then volume (kMT)."))
    blocks.append(commodity_table(data["detail_v"], "vessels"))
    blocks.append('<div style="height:14px;"></div>' + commodity_table(data["detail_k"], "kmt"))
    for area in CHART_AREAS:
        blocks.append(h2(HEADING[area], "Every port and terminal. Line-up over time (last 12 months) and where the line-up is headed."))
        for c in CHART_COMMS:
            cid = f"{area.lower()}_{c.lower()}"
            images[cid] = chart_png(data, area, c)
            blocks.append(f'<div style="margin:12px 0 4px;font-size:13px;">{stat_line(data, area, c)}</div>'
                          f'<img src="cid:{cid}" alt="{escape(HEADING[area])} {c}: line-up over time and destinations" width="680" '
                          f'style="display:block;width:100%;max-width:680px;height:auto;border:1px solid {BORDER};">')
    foot = ("Combo (multi-commodity) boats are split using USDA-measured shares, and undisclosed cargoes are filled from USDA vessel "
            "inspections, the same as the portal. Week / month / year changes compare each area's newest line-up with its snapshot "
            "7 days, 1 month and 1 year earlier.")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;padding:0;background:#f4f6f5;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6f5;"><tr><td align="center" style="padding:18px 8px;">
<table role="presentation" width="700" cellspacing="0" cellpadding="0" style="width:100%;max-width:700px;background:#ffffff;border:1px solid {BORDER};font-family:{FONT};">
<tr><td style="background:{INK};padding:18px 24px;"><div style="color:#ffffff;font-size:20px;font-weight:700;">Vessel Line-up</div>
<div style="color:#b7c6bd;font-size:13px;margin-top:3px;">Weekly update · {today:%A, %B} {today.day}, {today.year}</div></td></tr>
<tr><td style="padding:18px 24px 6px;">
<a href="{escape(PORTAL_URL)}" style="display:inline-block;background:{ACCENT};color:#ffffff;text-decoration:none;font-weight:700;font-size:14px;padding:10px 18px;border-radius:4px;">Open the Vessel Line-up portal &rarr;</a>
<div style="font-size:12px;color:{MUTED};margin:12px 0 14px;">{escape(asof_line(data['meta']))}{(' · ' + escape('; '.join(notes))) if notes else ''}</div>
{banner}
{"".join(blocks)}
<div style="font-size:11px;color:{MUTED};margin:28px 0 6px;line-height:1.5;border-top:1px solid {BORDER};padding-top:12px;">{escape(foot)}<br>
Source: daily port line-up reports (US, Brazil, Argentina) · John Stewart &amp; Associates · jpsi.com</div>
</td></tr></table></td></tr></table></body></html>"""
    return f"JSA Vessel Line-up · {today:%a %b} {today.day}", html, images


# ── send ─────────────────────────────────────────────────────────────────────

def _env(path):
    out = {}
    if Path(path).is_file():
        for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip().lstrip("﻿")
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.removeprefix("export ").partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def send_via_graph(subject, html, images, to, bcc=()):
    import msal
    import requests
    cfg = {**_env(ENV_FILE), **{k: v for k, v in os.environ.items() if k.startswith("GRAPH_")}}
    for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_SENDER"):
        if not cfg.get(k):
            raise RuntimeError(f"{k} missing (looked in {ENV_FILE})")
    app = msal.ConfidentialClientApplication(cfg["GRAPH_CLIENT_ID"], authority="https://login.microsoftonline.com/" + cfg["GRAPH_TENANT_ID"],
                                             client_credential=cfg["GRAPH_CLIENT_SECRET"])
    tok = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in tok:
        raise RuntimeError("Graph token failed: " + str(tok.get("error_description", tok))[:300])
    rcpt = lambda addrs: [{"emailAddress": {"address": a}} for a in addrs]
    msg = {"subject": subject, "body": {"contentType": "HTML", "content": html}, "toRecipients": rcpt(to), "bccRecipients": rcpt(bcc),
           "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment", "name": f"{cid}.png", "contentType": "image/png",
                            "contentBytes": base64.b64encode(png).decode(), "contentId": cid, "isInline": True} for cid, png in images.items()]}
    r = requests.post(f"{GRAPH}/users/{cfg['GRAPH_SENDER']}/sendMail", headers={"Authorization": "Bearer " + tok["access_token"]},
                      json={"message": msg, "saveToSentItems": True}, timeout=60)
    if r.status_code != 202:
        raise RuntimeError(f"Graph sendMail {r.status_code}: {r.text[:300]}")
    return cfg["GRAPH_SENDER"]


def _split(s):
    return [a.strip() for a in (s or "").replace(";", ",").split(",") if a.strip()]


def week_key(today):
    y, w, _ = today.isocalendar()
    return f"{y}-W{w:02d}"


def already_sent(today):
    try:
        return json.loads(STATE.read_text()).get("week") == week_key(today)
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", metavar="DIR", help="write email.html and the chart PNGs here; send nothing")
    ap.add_argument("--to", help="send a test copy to this address only (no BCC, does not count as the week's send)")
    ap.add_argument("--send", action="store_true", help="the live send (once per ISO week)")
    ap.add_argument("--force", action="store_true", help="with --send: send again even if this week's has gone out")
    ap.add_argument("--db", default=str(HERE / "facts.db"))
    a = ap.parse_args(argv)
    if not (a.preview or a.to or a.send):
        ap.error("give --preview DIR, --to ADDRESS or --send")
    today = dt.date.today()
    if a.send and not a.force and already_sent(today):
        print(f"already sent for {week_key(today)}; nothing to do (--force to resend)")
        return 0
    subject, html, images = build(a.db, today)
    if a.preview:
        out = Path(a.preview)
        out.mkdir(parents=True, exist_ok=True)
        page = html
        for cid, png in images.items():
            (out / f"{cid}.png").write_bytes(png)
            page = page.replace(f'src="cid:{cid}"', f'src="{cid}.png"')                       # the PNGs sit beside the page
        (out / "email.html").write_text(page, encoding="utf-8")
        print(f"preview written to {out} ({len(images)} charts): {subject}")
        return 0
    if a.to:
        sender = send_via_graph(subject, html, images, _split(a.to))
        print(f"test copy sent via graph as {sender} to {a.to}: {subject}")
        return 0
    to = _split(os.environ.get("VESSEL_EMAIL_TO")) or [DEFAULT_TO]
    bcc = _split(os.environ.get("VESSEL_EMAIL_BCC"))
    sender = send_via_graph(subject, html, images, to, bcc)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"week": week_key(today), "sent": dt.datetime.now().isoformat(timespec="seconds"), "to": to, "bcc": bcc}))
    print(f"sent via graph as {sender} to {', '.join(to)}" + (f" (bcc {', '.join(bcc)})" if bcc else "") + f": {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
