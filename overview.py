"""Summary (snapshot + explorer) and one page per country (United States, Brazil, Argentina), read from facts.db.

Scope: corn, soybeans, wheat, sorghum, soybean meal and soybean oil only. Combo (multi-commodity) boats and every
other cargo are left out for now. Everything switches between VESSELS and VOLUME (kMT). Anything that is not yet
sailed - ETA, loading, filed - is simply "line-up".

Every view answers the same two questions the same way:
  LINE-UP   what is queued now and how that has changed vs a week / month / year ago
  SAILED    last 7 days, month-to-date and year-to-date vs the prior periods, monthly shipments, a sailing
            timeline, and where the cargoes are going

Called from app.py: page_summary(facts_db) and page_country(country, facts_db).
"""

import os

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from fact_model import changes as ch
from fact_model import dimensions as dim
from fact_model import store

# ── look ─────────────────────────────────────────────────────────────────────
INK, MUTED, BORDER, SURFACE = "#32373c", "#5f7267", "#d7e2dc", "#ffffff"
GREEN, RED = "#16a34a", "#dc2626"

COMMODITIES = ["Corn", "Soybeans", "Wheat", "Sorghum", "Soybean Meal", "Soybean Oil"]
COMM_COLORS = {"Corn": "#e0a526", "Soybeans": "#3e8e5a", "Wheat": "#c98b3c", "Sorghum": "#b5543c",
               "Soybean Meal": "#1f5f46", "Soybean Oil": "#2f7fa8", "Total": INK}
SRC_LABEL = {"US": "United States", "Brazil": "Brazil", "Argentina": "Argentina"}
COUNTRIES = {v: k for k, v in SRC_LABEL.items()}               # label -> source
AREA_SHORT = {"US Gulf": "USG", "Texas Gulf": "TXG", "PNW": "PNW"}
MEASURES = {"Vessels": "vessels", "Volume (kMT)": "kmt"}
UNK = {"UNKNOWN", "C/P", "?", "", "RVT", "NOT AVAILABLE", "NAN", "NONE", "WHT", "CORN", "SBM", "YSB", "SORGHUM", "RICE"}   # codes the reports sometimes put in the destination column
GROUPS = {"Commodity": "commodity", "Country": "country", "Area": "area", "Port": "port", "Elevator / terminal": "elevator"}
PREV_COLS = ch.PREV


def _layout(fig, height=340, title=None):
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=36 if title else 10, b=8), paper_bgcolor=SURFACE,
                      plot_bgcolor=SURFACE, font=dict(color=MUTED, family="Arial", size=12),
                      title=dict(text=title, x=0, font=dict(size=13, color=INK)) if title else None,
                      legend=dict(orientation="h", y=-0.18, font=dict(color=INK)))
    fig.update_xaxes(gridcolor="#eef3f0", linecolor=BORDER)
    fig.update_yaxes(gridcolor="#eef3f0", linecolor=BORDER)
    return fig


# ── data ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, max_entries=6)
def _load(facts_db, mtime=None):
    return store.load_lineup(facts_db), store.load_events(facts_db)


def _place(df):
    """Port name; a terminal we cannot place is shown by its own name (flagged), never lumped as UNMAPPED."""
    el = df["elevator"].astype(str).str.strip()
    nameless = el.str.upper().isin(["UNMAPPED", "NONE", "NAN", ""])
    label = np.where(nameless, "No terminal named", el + " (unmapped)")
    return np.where(df["port"] == dim.UNMAPPED, label, df["port"])


def _tidy(df):
    df = df.copy()
    df["destination"] = df["destination"].astype(str).str.strip().str.title().replace({k.title(): "Unknown" for k in UNK})
    df["port"] = _place(df)
    df["elevator"] = df["elevator"].fillna("").astype(str).replace({"UNMAPPED": "No terminal named", "": "No terminal named"})
    df["country"] = df["source"].map(SRC_LABEL)
    df["area"] = df["region"].map(lambda r: AREA_SHORT.get(r, r))
    return df


@st.cache_data(show_spinner=False, max_entries=4)
def _prepare(facts_db, mtime, measure):
    """Line-up, events and the week/month/year detail for the six commodities. The change baselines are worked out
    on the WHOLE line-up first (so a region whose newest file came through empty is recognised as a data gap) and
    only then cut down to the six commodities. Returns (lineup, events, detail, meta, gaps, left_out)."""
    lineup, events = _load(facts_db, mtime)
    events = events.copy()
    events["vessel_w"] = events["vessel_w"].fillna(1.0) if "vessel_w" in events else 1.0
    lineup, events = _tidy(lineup), _tidy(events)
    detail, meta = ch.lineup_detail(lineup, measure)
    detail = _tidy(detail.assign(destination=""))
    gaps = pd.concat([pd.DataFrame({"source": src, "report_date": sorted(ch.gap_dates(g))})
                      for src, g in lineup.groupby("source")], ignore_index=True)
    gaps["report_date"] = pd.to_datetime(gaps["report_date"])
    combos = detail[detail["commodity"] == "Mixed Cargo"].groupby("source")["now"].sum()
    left_out = {SRC_LABEL[s]: v for s, v in combos.items() if v > 0 and s in SRC_LABEL}
    keep = lambda d: d[d["commodity"].isin(COMMODITIES)]
    return keep(lineup), keep(events), keep(detail), meta, gaps, left_out


def _region_order():
    cfg = dim.default_config()
    order = cfg.regions.sort_values("sort_order", key=lambda s: s.astype(int))["region"].tolist()
    return order + [dim.UNMAPPED]


def _comm_sort(values):
    rank = {c: i for i, c in enumerate(COMMODITIES)}
    return sorted(values, key=lambda c: (rank.get(c, 99), c))


# ── formatting ───────────────────────────────────────────────────────────────

def _num(v):
    if v is None or pd.isna(v):
        return "n/a"
    return f"{v:,.0f}" if abs(v - round(v)) < 0.05 else f"{v:,.1f}"


def _delta(v):
    if v is None or pd.isna(v):
        return "n/a"
    if abs(v) < 0.05:
        return "0"
    s = f"{abs(v):,.0f}" if abs(v - round(v)) < 0.05 else f"{abs(v):,.1f}"
    return ("+" if v > 0 else "−") + s


def _pct(v):
    if v is None or pd.isna(v):
        return "n/a"
    return "0%" if abs(v) < 0.5 else f"{v:+,.0f}%".replace("-", "−")


def _tint(v):
    if not isinstance(v, str) or v in ("n/a", "0", "0%", ""):
        return f"color:{MUTED}"
    return f"color:{GREEN};font-weight:600" if v[0] == "+" else f"color:{RED};font-weight:600"


def _styled(df, delta_cols, label_col=None):
    sty = df.style.map(_tint, subset=delta_cols) if delta_cols else df.style
    if label_col is not None:
        sty = sty.apply(lambda r: ["font-weight:700;background:#eef3f0" if r[label_col] == "Total" else "" for _ in r], axis=1)
    return sty


def _unit(measure):
    return "vessels" if measure == "vessels" else "kMT"


def _measure_toggle(key):
    label = st.segmented_control("Show", list(MEASURES), default="Vessels", key=f"{key}_measure", selection_mode="single")
    return MEASURES[label or "Vessels"]


def _asof_caption(meta, sources=None, left_out=None):
    m = meta if sources is None else meta[meta["source"].isin(sources)]
    bits = [f"{SRC_LABEL.get(src, src)} {pd.Timestamp(r['as_of']):%b %d}" for src, r in m.groupby("source").agg(as_of=("as_of", "max")).iterrows()]
    notes = [n for n in m["note"].unique() if n]
    extra = ""
    if left_out:
        shown = {k: v for k, v in left_out.items() if sources is None or COUNTRIES[k] in sources}
        if shown:
            extra = ("  \nCombo (multi-commodity) boats are left out for now: " +
                     ", ".join(f"{_num(v)} in the {k} line-up" for k, v in shown.items()) + ".")
    st.caption("Line-up as of: " + " · ".join(bits) + ("  \n" + " | ".join(notes) if notes else "") + extra)


# ── filters on any frame that carries source / region / port / elevator / commodity ──

def _apply(df, sel):
    m = pd.Series(True, index=df.index)
    for col, vals in sel.items():
        if vals is not None and col in df:
            m &= df[col].isin(vals)
    return df[m]


# ── line-up tables ───────────────────────────────────────────────────────────

def change_table(detail, by, label):
    """Rows = `by` values (+ Total), columns = Now and the week / month / year change (absolute and %)."""
    g = ch.rollup(detail, [by])
    g = g[(g["now"] > 0) | (g[PREV_COLS].fillna(0).sum(axis=1) > 0)]
    if by == "commodity":
        g = g.assign(_o=g[by].map({c: i for i, c in enumerate(COMMODITIES)}).fillna(99)).sort_values("_o").drop(columns="_o")
    else:
        g = g.sort_values("now", ascending=False)
    t = ch.rollup(detail.assign(_t="Total"), ["_t"]).rename(columns={"_t": by})
    g = pd.concat([g, t], ignore_index=True)
    out = pd.DataFrame({label: g[by], "Now": g["now"].map(_num)})
    for k, nm in (("wk", "Week"), ("mo", "Month"), ("yr", "Year")):
        out[f"Δ {nm}"] = g[f"d_{k}"].map(_delta)
        out[f"% {nm}"] = g[f"p_{k}"].map(_pct)
    return out


def show_change_table(detail, by, label):
    if detail.empty:
        st.info("Nothing in the line-up for this selection.")
        return
    t = change_table(detail, by, label)
    d_cols = [c for c in t.columns if c.startswith(("Δ", "%"))]
    st.dataframe(_styled(t, d_cols, label), hide_index=True, height=min(38 * (len(t) + 1), 560))


def port_table(detail):
    """Port rows x commodity columns (now), with total and week / month / year change."""
    now = detail.groupby(["region", "port", "commodity"])["now"].sum().unstack(fill_value=0.0)
    now = now[_comm_sort(now.columns)]
    tot = ch.rollup(detail, ["region", "port"]).set_index(["region", "port"])
    out = now.copy()
    out.insert(0, "Total", tot["now"])
    for i, (k, nm) in enumerate((("wk", "Δ Week"), ("mo", "Δ Month"), ("yr", "Δ Year"))):
        out.insert(1 + i, nm, tot[f"d_{k}"])
    order = {r: i for i, r in enumerate(_region_order())}
    out = out.reset_index()
    out = out.assign(_r=out["region"].map(order).fillna(99)).sort_values(["_r", "Total"], ascending=[True, False]).drop(columns="_r")
    disp = out.copy()
    for c in ["Total"] + list(now.columns):
        disp[c] = disp[c].map(lambda v: "" if (pd.isna(v) or v == 0) else _num(v))
    for c in ("Δ Week", "Δ Month", "Δ Year"):
        disp[c] = disp[c].map(_delta)
    disp["region"] = disp["region"].map(lambda r: AREA_SHORT.get(r, r))
    return disp.rename(columns={"region": "Area", "port": "Port"})


def show_port_table(detail):
    if detail.empty:
        st.info("Nothing in the line-up for this selection.")
        return
    t = port_table(detail)
    st.dataframe(_styled(t, ["Δ Week", "Δ Month", "Δ Year"]), hide_index=True, height=min(38 * (len(t) + 1), 640))


# ── charts ───────────────────────────────────────────────────────────────────

def _trend(lineup, gaps, measure, key):
    rng = st.segmented_control("Range", ["3M", "6M", "1Y", "All"], default="1Y", key=f"{key}_trange")
    d = lineup.merge(gaps.assign(_gap=1), on=["source", "report_date"], how="left")
    d = d[d["_gap"].isna()]
    if rng and rng != "All" and not d.empty:
        d = d[d["report_date"] >= d["report_date"].max() - pd.DateOffset(months={"3M": 3, "6M": 6, "1Y": 12}[rng])]
    g = d.groupby(["report_date", "commodity"])[measure].sum().reset_index()
    if g.empty:
        st.info("No line-up history for this selection.")
        return
    fig = px.area(g, x="report_date", y=measure, color="commodity", color_discrete_map=COMM_COLORS,
                  category_orders={"commodity": _comm_sort(g["commodity"].unique())}, labels={measure: _unit(measure), "report_date": ""})
    st.plotly_chart(_layout(fig, 320, f"Line-up over time ({_unit(measure)})"), key=f"{key}_trend")
    if g["report_date"].nunique() < 3:
        st.caption("Only a few snapshots are banked for this selection so far; the trend fills in as new line-ups arrive.")


def _destinations(df, col, measure, title, key, top=15):
    d = df[df["destination"].astype(str).str.lower() != "unknown"]
    if d.empty:
        st.info("No destinations are reported for this selection.")
        return
    tot = d.groupby("destination")[col].sum().nlargest(top)
    g = d[d["destination"].isin(tot.index)].groupby(["destination", "commodity"])[col].sum().reset_index()
    unk = df.loc[df["destination"].astype(str).str.lower() == "unknown", col].sum() / max(df[col].sum(), 1e-9)
    fig = px.bar(g, y="destination", x=col, color="commodity", orientation="h", color_discrete_map=COMM_COLORS,
                 category_orders={"destination": list(tot.index), "commodity": _comm_sort(g["commodity"].unique())},
                 labels={col: _unit(measure), "destination": ""})
    fig.update_yaxes(autorange="reversed")
    st.plotly_chart(_layout(fig, 60 + 26 * len(tot), title), key=key)
    if unk > 0.05:
        st.caption(f"{unk:.0%} of the {_unit(measure)} has no destination reported and is left out of this chart.")


# ── LINE-UP view ─────────────────────────────────────────────────────────────

def lineup_view(sel, lineup, detail, meta, gaps, left_out, measure, key, group_by="Commodity", port_matrix=False):
    d = _apply(detail, sel)
    if d.empty:
        st.info("Nothing in the line-up for this selection.")
        return
    tot = ch.rollup(d.assign(_t="t"), ["_t"]).iloc[0]
    k = st.columns(4)
    k[0].metric(f"Line-up now ({_unit(measure)})", _num(tot["now"]))
    k[1].metric("vs last week", _delta(tot["d_wk"]), _pct(tot["p_wk"]), delta_color="off")
    k[2].metric("vs last month", _delta(tot["d_mo"]), _pct(tot["p_mo"]), delta_color="off")
    k[3].metric("vs last year", _delta(tot["d_yr"]), _pct(tot["p_yr"]), delta_color="off")
    srcs = sorted(d["source"].unique())
    _asof_caption(meta, srcs, left_out)
    new = [s for s in srcs if meta.loc[meta["source"] == s, "wk_date"].isna().all()]
    if new:
        st.caption(f"{', '.join(SRC_LABEL[s] for s in new)}: daily line-up began Oct 22, 2026, so its week / month / year "
                   "comparisons fill in as it accumulates (it is in the totals, not in the changes).")
    show_change_table(d, GROUPS[group_by], group_by)
    ln = _apply(lineup, sel)
    _trend(ln, gaps, measure, key)
    if port_matrix:
        st.markdown("**By port**")
        show_port_table(d)
    st.markdown("**Where the line-up is headed**")
    cur = ch.current_rows(ln, meta)
    _destinations(cur, measure, measure, f"Line-up destinations ({_unit(measure)})", f"{key}_ldest")


# ── SAILED view ──────────────────────────────────────────────────────────────

def _sailed_table(w, label, by):
    if w.empty:
        return pd.DataFrame(columns=[label])
    if by == "commodity":
        w = w.assign(_o=w[by].map({c: i for i, c in enumerate(COMMODITIES)}).fillna(99)).sort_values("_o").drop(columns="_o")
    else:
        w = w.sort_values("mtd", ascending=False)
    tot = w[ch.WINDOWS + ch.DELTAS].sum(min_count=1)
    t = pd.concat([w, pd.DataFrame([{by: "Total", **tot.to_dict()}])], ignore_index=True)

    def pc(d, prev):
        return d / prev.where(prev > 0) * 100          # percent change over the periods that have a comparison
    return pd.DataFrame({
        label: t[by], "Last 7d": t["l7"].map(_num), "Δ vs prior 7d": t["d_wk"].map(_delta), "% 7d": pc(t["d_wk"], t["l7_prev"]).map(_pct),
        "Month-to-date": t["mtd"].map(_num), "Δ vs last month": t["d_mo"].map(_delta), "% mo": pc(t["d_mo"], t["mtd_pm"]).map(_pct),
        "Δ vs last year": t["d_yr"].map(_delta), "% yr": pc(t["d_yr"], t["mtd_ly"]).map(_pct),
        "Year-to-date": t["ytd"].map(_num), "Δ YTD vs last year": t["d_ytd"].map(_delta), "% YTD": pc(t["d_ytd"], t["ytd_ly"]).map(_pct)})


def _timeline(ev, key):
    win = st.segmented_control("Timeline window", ["14 days", "30 days", "60 days", "90 days", "6 months", "1 year"],
                               default="30 days", key=f"{key}_twin")
    days = {"14 days": 14, "30 days": 30, "60 days": 60, "90 days": 90, "6 months": 183, "1 year": 365}[win or "30 days"]
    end = ev["sail_date"].max()
    r = ev[ev["sail_date"] > end - pd.Timedelta(days=days)].copy()
    if r.empty:
        st.info("No sailings in this window.")
        return r, days
    r["size"] = r["kmt"].fillna(r["kmt"].median() if r["kmt"].notna().any() else 20).clip(lower=5)
    order = r.groupby("port").size().sort_values(ascending=False).index[:28].tolist()
    r = r[r["port"].isin(order)]
    fig = px.scatter(r, x="sail_date", y="port", size="size", color="commodity", color_discrete_map=COMM_COLORS,
                     category_orders={"port": order[::-1], "commodity": _comm_sort(r["commodity"].unique())}, size_max=18,
                     hover_data={"vessel": True, "destination": True, "kmt": ":,.1f", "size": False, "sail_date": "|%b %d, %Y"},
                     labels={"sail_date": "", "port": "", "kmt": "kMT"})
    st.plotly_chart(_layout(fig, 140 + 24 * len(order), f"Sailings, last {win or '30 days'} (bubble = kMT)"), key=f"{key}_timeline")
    return r, days


def sailed_view(sel, events, measure, months, key, group_by="Commodity"):
    ev = _apply(events, sel).copy()
    if ev.empty:
        st.info("No sailings banked for this selection.")
        return
    ev["sail_date"] = pd.to_datetime(ev["sail_date"])
    asof = ev["sail_date"].max()
    srcs = sorted(ev["source"].unique())
    st.caption(f"Sailings through {asof:%b %d, %Y}. Week = last 7 days vs the 7 before; month and year compare month-to-date with the "
               "same days of last month and of last year.")
    by = GROUPS[group_by]
    t = _sailed_table(ch.sailed_windows(ev, [by], measure), group_by, by)
    st.dataframe(_styled(t, [c for c in t.columns if c.startswith(("Δ", "%"))], group_by), hide_index=True,
                 height=min(38 * (len(t) + 1), 560))
    if "Argentina" in srcs:
        st.caption("Argentina's sailed history starts Oct 1, 2026 (the first Nabsa report), so its last-month and last-year "
                   "comparisons are n/a for now.")

    st.markdown("**Monthly shipments**")
    ev["month"] = ev["sail_date"].dt.to_period("M").dt.to_timestamp()
    ev["_m"] = ev["vessel_w"] if measure == "vessels" else ev["kmt"].fillna(0.0)
    cut = pd.Timestamp(asof).normalize().replace(day=1) - pd.DateOffset(months=months - 1)
    mm = ev[ev["month"] >= cut].groupby(["month", "commodity"])["_m"].sum().reset_index()
    if not mm.empty:
        fig = px.bar(mm, x="month", y="_m", color="commodity", color_discrete_map=COMM_COLORS, barmode="stack",
                     category_orders={"commodity": _comm_sort(mm["commodity"].unique())}, labels={"_m": _unit(measure), "month": ""})
        fig.update_xaxes(tickformat="%b %Y")
        fig.update_traces(marker_line_width=0)
        st.plotly_chart(_layout(fig, 340, f"Shipped {_unit(measure)} by month and commodity"), key=f"{key}_monthly")
        piv = mm.pivot_table(index="commodity", columns="month", values="_m", aggfunc="sum", fill_value=0.0)
        piv = piv.loc[_comm_sort(piv.index)]
        piv.columns = [c.strftime("%b %Y") for c in piv.columns]
        piv["Total"] = piv.sum(axis=1)
        piv.loc["Total"] = piv.sum()
        st.dataframe(piv.map(lambda v: "" if v == 0 else _num(v)), height=min(38 * (len(piv) + 1), 360))
    st.markdown("**Sailing timeline**")
    recent, days = _timeline(ev, key)
    st.markdown("**Where recent sailings went**")
    if not recent.empty:
        r = recent.assign(_v=recent["vessel_w"] if measure == "vessels" else recent["kmt"].fillna(0.0))
        _destinations(r, "_v", measure, f"Destinations of sailings, last {days} days ({_unit(measure)})", f"{key}_sdest")


# ── PAGE: summary ────────────────────────────────────────────────────────────

def _snapshot_matrices(detail, events, measure):
    """Commodity rows x (United States, Brazil, Argentina, Total): line-up now and sailed month-to-date."""
    unit = _unit(measure)
    now = detail.groupby(["commodity", "source"])["now"].sum().unstack(fill_value=0.0)
    w = ch.sailed_windows(events, ["source", "commodity"], measure)
    mtd = w.pivot_table(index="commodity", columns="source", values="mtd", aggfunc="sum", fill_value=0.0) if not w.empty else pd.DataFrame()

    def render(m):
        m = m.reindex(index=COMMODITIES, columns=list(SRC_LABEL), fill_value=0.0).fillna(0.0)
        m["Total"] = m.sum(axis=1)
        m.loc["Total"] = m.sum()
        m = m.rename(columns=SRC_LABEL)
        out = m.map(lambda v: "" if v == 0 else _num(v))
        out.insert(0, "Commodity", out.index)
        return out
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Line-up now ({unit})**")
        t = render(now)
        st.dataframe(_styled(t, [], "Commodity"), hide_index=True, height=38 * (len(t) + 1))
    with right:
        st.markdown(f"**Sailed month-to-date ({unit})**")
        t = render(mtd)
        st.dataframe(_styled(t, [], "Commodity"), hide_index=True, height=38 * (len(t) + 1))


def _explorer(lineup, events, detail, meta, gaps, left_out, measure):
    st.markdown("### Explore")
    st.caption("Pick what you want to look at: country, port, elevator or terminal, commodity, and line-up or sailed.")
    c1, c2 = st.columns([3, 2])
    with c1:
        countries = st.pills("Country", list(COUNTRIES), selection_mode="multi", default=list(COUNTRIES), key="ex_country") or list(COUNTRIES)
    with c2:
        view = st.segmented_control("View", ["Line-up", "Sailed"], default="Line-up", key="ex_view") or "Line-up"
    srcs = [COUNTRIES[c] for c in countries]
    base = pd.concat([detail[["source", "port", "elevator"]], events[["source", "port", "elevator"]]]).drop_duplicates()
    base = base[base["source"].isin(srcs)]
    # options depend on the selections above them, so those selections are part of each widget's key
    ports = sorted(base["port"].unique())
    c3, c4, c5 = st.columns(3)
    with c3:
        port = st.multiselect("Port", ports, key=f"ex_port_{'-'.join(srcs)}", placeholder="All ports")
    elevs = sorted(base[base["port"].isin(port)]["elevator"].unique() if port else base["elevator"].unique())
    with c4:
        elev = st.multiselect("Elevator / terminal", elevs, key=f"ex_elev_{'-'.join(srcs)}_{'-'.join(port)}", placeholder="All elevators")
    with c5:
        comm = st.multiselect("Commodity", COMMODITIES, key="ex_comm", placeholder="All six commodities")
    sel = {"source": srcs, "port": port or None, "elevator": elev or None, "commodity": comm or COMMODITIES}
    group_by = st.segmented_control("Group the table by", list(GROUPS), default="Commodity", key="ex_group") or "Commodity"
    if view == "Line-up":
        lineup_view(sel, lineup, detail, meta, gaps, left_out, measure, "ex", group_by)
    else:
        months = st.slider("Months of shipment history", 3, 36, 12, key="ex_months")
        sailed_view(sel, events, measure, months, "ex", group_by)


def page_summary(facts_db):
    mtime = os.path.getmtime(facts_db)
    measure = _measure_toggle("sum")
    lineup, events, detail, meta, gaps, left_out = _prepare(facts_db, mtime, measure)
    unit = _unit(measure)

    st.caption(f"Line-up now, in {unit}; change vs last week below each figure.")
    cols = st.columns(4)
    for col, (name, srcs) in zip(cols, (("Total line-up", list(SRC_LABEL)), *[(v, [k]) for k, v in SRC_LABEL.items()])):
        d = detail[detail["source"].isin(srcs)]
        if d.empty:
            col.metric(name, "—")
            continue
        r = ch.rollup(d.assign(_t="t"), ["_t"]).iloc[0]
        col.metric(name, _num(r["now"]), None if pd.isna(r["d_wk"]) else f"{_delta(r['d_wk'])} vs last week",
                   delta_color="off", help=f"Month: {_delta(r['d_mo'])} · Year: {_delta(r['d_yr'])}")
        col.caption(f"Mo {_delta(r['d_mo'])} · Yr {_delta(r['d_yr'])}")
    _asof_caption(meta, None, left_out)

    st.markdown("##### Snapshot by commodity")
    _snapshot_matrices(detail, events, measure)

    st.markdown("##### Week, month and year change")
    for name, srcs in (("All three countries", list(SRC_LABEL)), *[(v, [k]) for k, v in SRC_LABEL.items()]):
        sel = {"source": srcs}
        d = _apply(detail, sel)
        if d.empty:
            continue
        with st.expander(name, expanded=name == "All three countries"):
            st.markdown("**Line-up**")
            show_change_table(d, "commodity", "Commodity")
            if len(srcs) == 1:
                st.markdown("**Line-up by port**")
                show_port_table(d)
            ev = _apply(events, sel)
            if not ev.empty:
                st.markdown("**Sailed**")
                t = _sailed_table(ch.sailed_windows(ev, ["commodity"], measure), "Commodity", "commodity")
                st.dataframe(_styled(t, [c for c in t.columns if c.startswith(("Δ", "%"))], "Commodity"), hide_index=True,
                             height=min(38 * (len(t) + 1), 400))

    st.markdown("---")
    _explorer(lineup, events, detail, meta, gaps, left_out, measure)


# ── PAGE: country ────────────────────────────────────────────────────────────

def page_country(country, facts_db):
    source = COUNTRIES[country]
    key = f"c_{source}"
    mtime = os.path.getmtime(facts_db)
    c_show, c_area, c_comm = st.columns([1.4, 2.2, 3.4])          # every filter in one row at the top
    with c_show:
        measure = _measure_toggle(key)
    lineup, events, detail, meta, gaps, left_out = _prepare(facts_db, mtime, measure)
    d_src = detail[detail["source"] == source]
    if d_src.empty:
        st.info(f"No {country} line-up banked yet.")
        return
    regions = [r for r in _region_order() if r in set(d_src["region"])]
    with c_area:
        if len(regions) > 1:
            area = st.pills("Area", regions, selection_mode="multi", default=regions, key=f"{key}_area",
                            format_func=lambda r: AREA_SHORT.get(r, r)) or regions
        else:
            st.caption("Area")
            st.markdown(f"**{AREA_SHORT.get(regions[0], regions[0])}**")
            area = regions
    with c_comm:
        comms = st.multiselect("Commodity", COMMODITIES, default=COMMODITIES, key=f"{key}_comm") or COMMODITIES
    sel = {"source": [source], "region": area, "commodity": comms}
    st.markdown("#### Line-up")
    lineup_view(sel, lineup, detail, meta, gaps, left_out, measure, key, port_matrix=True)
    st.markdown("---")
    st.markdown("#### Sailed")
    months = st.slider("Months of shipment history", 3, 36, 12, key=f"{key}_months")
    sailed_view(sel, events, measure, months, key)
