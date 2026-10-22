"""Summary + one page per country (United States, Brazil, Argentina), all read from facts.db.

Every page answers the same two questions in the same way:
  LINE-UP   what is queued now, by commodity and by port, and how that has changed vs a week / month / year ago
  SAILED    what has shipped (last 7 days, month-to-date, year-to-date) and how that compares, the monthly
            shipments, a sailing timeline, and where the cargoes are going
Everything switches between VESSELS and VOLUME (kMT). Line-up detail beyond that (ETA, loading, filed) is
deliberately left out.

Called from app.py: page_summary(facts_db) and page_country(country, facts_db).
"""

import os

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from fact_model import changes as ch
from fact_model import combos, store
from fact_model import dimensions as dim

# ── look ─────────────────────────────────────────────────────────────────────
INK, MUTED, BORDER, SURFACE, BG = "#32373c", "#5f7267", "#d7e2dc", "#ffffff", "#f6f8f7"
GREEN, RED, BLUE = "#16a34a", "#dc2626", "#0693e3"

COMM_ORDER = ["Corn", "Soybeans", "Soybean Meal", "Wheat", "Sorghum", "Soybean Oil", "Other Oils", "Sugar",
              "Dist. Grains", "Rice", "Mixed Cargo", "Other", "Non-Ag"]
COMM_COLORS = {"Corn": "#e0a526", "Soybeans": "#3e8e5a", "Soybean Meal": "#1f5f46", "Wheat": "#c98b3c",
               "Sorghum": "#b5543c", "Soybean Oil": "#2f7fa8", "Other Oils": "#86b6d2", "Sugar": "#d96c8a",
               "Dist. Grains": "#8a6fbf", "Rice": "#c05a9a", "Mixed Cargo": "#6b7f8f", "Other": "#9aa5a0",
               "Non-Ag": "#c5cbc8", "Total": INK}
HIDDEN_BY_DEFAULT = {"Non-Ag"}
PREV_COLS = ch.PREV

COUNTRIES = {"United States": "US", "Brazil": "Brazil", "Argentina": "Argentina"}
AREA_SHORT = {"US Gulf": "USG", "Texas Gulf": "TXG", "PNW": "PNW"}
MEASURES = {"Vessels": "vessels", "Volume (kMT)": "kmt"}
UNK = {"UNKNOWN", "C/P", "?", "", "RVT", "NOT AVAILABLE", "NAN", "NONE", "WHT", "CORN", "SBM", "YSB", "SORGHUM", "RICE"}   # codes the reports sometimes put in the destination column


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


@st.cache_data(show_spinner=False, max_entries=8)
def _prepare(facts_db, mtime, measure, alloc):
    """Line-up + events with combo boats handled, plus the week/month/year detail. Vessel counts keep a combo
    boat as ONE 'Mixed Cargo' vessel; volume can split its tonnage across the commodities it carries."""
    lineup, events = _load(facts_db, mtime)
    events = events.copy()
    events["vessel_w"] = events["vessel_w"].fillna(1.0) if "vessel_w" in events else 1.0
    for df in (lineup, events):
        df["destination"] = df["destination"].astype(str).str.strip().str.title().replace({k.title(): "Unknown" for k in UNK})
    if measure == "kmt" and alloc:
        lineup = combos.allocate_combos(lineup, value_cols=("kmt",))
        events = combos.allocate_combos(events, value_cols=("kmt",))
    lineup = lineup.assign(port=_place(lineup))
    events = events.assign(port=_place(events))
    detail, meta = ch.lineup_detail(lineup, measure)
    return lineup, events, detail, meta


def _place(df):
    """Port name; a terminal we cannot place is shown by its own name (flagged), never lumped as UNMAPPED."""
    el = df["elevator"].astype(str).str.strip()
    nameless = el.str.upper().isin(["UNMAPPED", "NONE", "NAN", ""])
    label = np.where(nameless, "No terminal named", el + " (unmapped)")
    return np.where(df["port"] == dim.UNMAPPED, label, df["port"])


def _region_order():
    cfg = dim.default_config()
    order = cfg.regions.sort_values("sort_order", key=lambda s: s.astype(int))["region"].tolist()
    return order + [dim.UNMAPPED]


def _comm_sort(values):
    rank = {c: i for i, c in enumerate(COMM_ORDER)}
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


def _styled(df, delta_cols, bold_total=True, label_col=None):
    sty = df.style.map(_tint, subset=delta_cols) if delta_cols else df.style
    if bold_total and label_col is not None:
        sty = sty.apply(lambda r: ["font-weight:700;background:#eef3f0" if r[label_col] == "Total" else "" for _ in r], axis=1)
    return sty


# ── controls ─────────────────────────────────────────────────────────────────

def _measure_toggle(key, wide=True):
    """Vessels / Volume switch (+ the combo-split option when showing volume). wide=False stacks them in a narrow column."""
    if wide:
        left, right = st.columns([2, 3])
    else:
        left = right = st.container()
    with left:
        label = st.segmented_control("Show", list(MEASURES), default="Vessels", key=f"{key}_measure", selection_mode="single")
    measure = MEASURES[label or "Vessels"]
    alloc = True
    if measure == "kmt":
        with right:
            alloc = st.checkbox("Split combo boats" if not wide else
                                "Split combo boats' tonnage across their commodities (corn, else wheat, counts double)",
                                value=True, key=f"{key}_alloc",
                                help="A combo boat's tonnage is divided across the commodities it carries: corn (else wheat) counts "
                                     "double, every other commodity once.")
    return measure, alloc


def _unit(measure):
    return "vessels" if measure == "vessels" else "kMT"


def _asof_caption(meta, extra=""):
    bits = [f"{src} {pd.Timestamp(m['as_of']):%b %d}" for src, m in meta.groupby("source").agg(as_of=("as_of", "max")).iterrows()]
    notes = [n for n in meta["note"].unique() if n]
    st.caption("Line-up as of: " + " · ".join(bits) + (" · " + extra if extra else "") +
               ("  \n" + " | ".join(notes) if notes else ""))


# ── tables ───────────────────────────────────────────────────────────────────

def change_table(detail, by, label, order=None):
    """Rows = `by` values (+ Total), columns = Now and the week / month / year change (absolute and %)."""
    if detail.empty:
        return pd.DataFrame(columns=[label])
    g = ch.rollup(detail, [by])
    g = g[(g["now"] > 0) | (g[PREV_COLS].fillna(0).sum(axis=1) > 0)]
    if order is not None:
        g = g.assign(_o=g[by].map({c: i for i, c in enumerate(order)}).fillna(99)).sort_values(["_o", "now"], ascending=[True, False]).drop(columns="_o")
    else:
        g = g.sort_values("now", ascending=False)
    t = ch.rollup(detail.assign(_t="Total"), ["_t"]).rename(columns={"_t": by})
    g = pd.concat([g, t], ignore_index=True)
    out = pd.DataFrame({label: g[by], "Now": g["now"].map(_num)})
    for k, nm in (("wk", "Week"), ("mo", "Month"), ("yr", "Year")):
        out[f"Δ {nm}"] = g[f"d_{k}"].map(_delta)
        out[f"% {nm}"] = g[f"p_{k}"].map(_pct)
    return out


def _show_change_table(detail, by, label, order=None, height=None):
    t = change_table(detail, by, label, order)
    if t.empty:
        st.info("Nothing in the line-up for this selection.")
        return
    d_cols = [c for c in t.columns if c.startswith(("Δ", "%"))]
    st.dataframe(_styled(t, d_cols, label_col=label), hide_index=True, height=height or min(38 * (len(t) + 1), 520))


def port_table(detail, measure):
    """Port rows x commodity columns (now), with total and week / month / year change."""
    if detail.empty:
        return pd.DataFrame()
    now = detail.groupby(["region", "port", "commodity"])["now"].sum().unstack(fill_value=0.0)
    now = now[_comm_sort(now.columns)]
    tot = ch.rollup(detail, ["region", "port"]).set_index(["region", "port"])
    out = now.copy()
    out.insert(0, "Total", tot["now"])
    for k, nm in (("wk", "Δ Week"), ("mo", "Δ Month"), ("yr", "Δ Year")):
        out.insert(1 + ["wk", "mo", "yr"].index(k), nm, tot[f"d_{k}"])
    order = {r: i for i, r in enumerate(_region_order())}
    out = out.reset_index()
    out = out.assign(_r=out["region"].map(order).fillna(99)).sort_values(["_r", "Total"], ascending=[True, False]).drop(columns="_r")
    disp = out.copy()
    for c in ["Total"] + _comm_sort([c for c in now.columns]):
        disp[c] = disp[c].map(lambda v: "" if (pd.isna(v) or v == 0) else _num(v))
    for c in ("Δ Week", "Δ Month", "Δ Year"):
        disp[c] = disp[c].map(_delta)
    disp["region"] = disp["region"].map(lambda r: AREA_SHORT.get(r, r))
    return disp.rename(columns={"region": "Area", "port": "Port"})


def _show_port_table(detail, measure):
    t = port_table(detail, measure)
    if t.empty:
        st.info("Nothing in the line-up for this selection.")
        return
    st.dataframe(_styled(t, ["Δ Week", "Δ Month", "Δ Year"], bold_total=False), hide_index=True, height=min(38 * (len(t) + 1), 640))


# ── PAGE: summary ────────────────────────────────────────────────────────────

SCOPES = [("World", lambda d: d), ("United States", lambda d: d[d["source"] == "US"]),
          ("US Gulf", lambda d: d[(d["source"] == "US") & (d["region"] == "US Gulf")]),
          ("PNW", lambda d: d[(d["source"] == "US") & (d["region"] == "PNW")]),
          ("Texas Gulf", lambda d: d[(d["source"] == "US") & (d["region"] == "Texas Gulf")]),
          ("Brazil", lambda d: d[d["source"] == "Brazil"]), ("Argentina", lambda d: d[d["source"] == "Argentina"])]


def page_summary(facts_db):
    mtime = os.path.getmtime(facts_db)
    measure, alloc = _measure_toggle("sum")
    lineup, events, detail, meta = _prepare(facts_db, mtime, measure, alloc)
    show_na = st.checkbox("Include non-agricultural cargoes (wood, fuel, fertiliser, ...)", value=False, key="sum_nonag")
    if not show_na:
        detail = detail[~detail["commodity"].isin(HIDDEN_BY_DEFAULT)]
    unit = _unit(measure)

    # headline cards
    cols = st.columns(len(SCOPES))
    for col, (name, sel) in zip(cols, SCOPES):
        d = sel(detail)
        if d.empty:
            col.metric(name, "—")
            continue
        r = ch.rollup(d.assign(_t="t"), ["_t"]).iloc[0]
        col.metric(name, _num(r["now"]), None if pd.isna(r["d_wk"]) else f"{_delta(r['d_wk'])} vs last week", delta_color="off",
                   help=f"{unit}. Month: {_delta(r['d_mo'])} · Year: {_delta(r['d_yr'])}")
        col.caption(f"Mo {_delta(r['d_mo'])} · Yr {_delta(r['d_yr'])}")
    _asof_caption(meta)
    if (meta["source"] == "Argentina").any() and meta.loc[meta["source"] == "Argentina", "wk_date"].isna().all():
        st.caption("Argentina's line-up starts Oct 22, 2026: it is in the totals but not yet in the week / month / year changes.")

    # commodity x scope, now
    st.markdown(f"##### Line-up by commodity ({unit})")
    cols_now = {}
    for name, sel in SCOPES:
        d = sel(detail)
        if not d.empty:
            cols_now[name] = d.groupby("commodity")["now"].sum()
    m = pd.DataFrame(cols_now).fillna(0.0)
    m = m.loc[_comm_sort(m.index)]
    m.loc["Total"] = m.sum()
    disp = m.copy()
    for c in disp.columns:
        disp[c] = disp[c].map(lambda v: "" if v == 0 else _num(v))
    disp.insert(0, "Commodity", disp.index)
    st.dataframe(_styled(disp, [], label_col="Commodity"), hide_index=True, height=min(38 * (len(disp) + 1), 560))

    # per country drop-downs: commodity changes, then by port
    st.markdown("##### Week, month and year change")
    st.caption("Open a country for its commodities and ports. Change = now minus the snapshot nearest 1 week / 1 month / 1 year earlier.")
    for name, sel in [SCOPES[0]] + [s for s in SCOPES if s[0] in ("United States", "Brazil", "Argentina")]:
        d = sel(detail)
        if d.empty:
            continue
        with st.expander(name, expanded=name in ("World", "United States")):
            _show_change_table(d, "commodity", "Commodity", order=COMM_ORDER)
            if name != "World":
                areas = d.groupby("region")["now"].sum()
                if len(areas) > 1:
                    st.markdown("**By area**")
                    a = d.assign(area=d["region"].map(lambda r: AREA_SHORT.get(r, r)))
                    _show_change_table(a, "area", "Area")
                st.markdown("**By port**")
                _show_port_table(d, measure)


# ── PAGE: country ────────────────────────────────────────────────────────────

def _filters(source, detail, key, col_area, col_comm):
    regions = [r for r in _region_order() if r in set(detail.loc[detail["source"] == source, "region"])]
    with col_area:
        if len(regions) > 1:
            area = st.pills("Area", regions, selection_mode="multi", default=regions, key=f"{key}_area",
                            format_func=lambda r: AREA_SHORT.get(r, r)) or regions
        else:
            st.caption("Area")
            st.markdown(f"**{AREA_SHORT.get(regions[0], regions[0]) if regions else ''}**")
            area = regions
    comms = _comm_sort(detail.loc[detail["source"] == source, "commodity"].unique())
    with col_comm:
        pick = st.multiselect("Commodity", comms, default=[c for c in comms if c not in HIDDEN_BY_DEFAULT], key=f"{key}_comm")
    return list(area), (pick or comms)


def _trend(lineup, source, area, comms, measure, key):
    rng = st.segmented_control("Range", ["3M", "6M", "1Y", "All"], default="1Y", key=f"{key}_trange")
    d = lineup[(lineup["source"] == source) & lineup["region"].isin(area) & lineup["commodity"].isin(comms)]
    bad = ch.gap_dates(lineup[lineup["source"] == source])
    d = d[~d["report_date"].isin(bad)]
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
        st.caption("Only a few snapshots are banked for this source so far; the trend fills in as new line-ups arrive.")


def _destinations(df, measure, title, key, top=15):
    d = df[df["destination"].astype(str).str.lower() != "unknown"]
    if d.empty:
        st.info("No destinations are reported for this selection.")
        return
    col = measure if measure in d else "now"
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


def _lineup_section(country, source, lineup, detail, meta, area, comms, measure, key):
    st.markdown("#### Line-up")
    d = detail[(detail["source"] == source) & detail["region"].isin(area) & detail["commodity"].isin(comms)]
    if d.empty:
        st.info("No line-up banked yet for this selection.")
        return
    tot = ch.rollup(d.assign(_t="t"), ["_t"]).iloc[0]
    k = st.columns(4)
    k[0].metric(f"Line-up now ({_unit(measure)})", _num(tot["now"]))
    k[1].metric("vs last week", _delta(tot["d_wk"]), _pct(tot["p_wk"]), delta_color="off")
    k[2].metric("vs last month", _delta(tot["d_mo"]), _pct(tot["p_mo"]), delta_color="off")
    k[3].metric("vs last year", _delta(tot["d_yr"]), _pct(tot["p_yr"]), delta_color="off")
    _asof_caption(meta[meta["source"] == source])
    if source == "Argentina" and tot[["prev_wk", "prev_mo", "prev_yr"]].isna().all():
        st.caption("Argentina's daily line-up began on Oct 22, 2026, so week, month and year comparisons fill in as it accumulates.")
    _show_change_table(d, "commodity", "Commodity", order=COMM_ORDER)
    _trend(lineup, source, area, comms, measure, key)
    st.markdown("**By port**")
    _show_port_table(d, measure)
    st.markdown("**Where the line-up is headed**")
    cur = ch.current_rows(lineup[(lineup["source"] == source)], meta)
    cur = cur[cur["region"].isin(area) & cur["commodity"].isin(comms)]
    _destinations(cur, measure, f"Line-up destinations ({_unit(measure)})", f"{key}_ldest")


def _sailed_table(w, label, by):
    if w.empty:
        return pd.DataFrame(columns=[label])
    w = w.sort_values("mtd", ascending=False) if by != "commodity" else w.assign(_o=w[by].map({c: i for i, c in enumerate(COMM_ORDER)}).fillna(99)).sort_values("_o").drop(columns="_o")
    tot = w[ch.WINDOWS + ch.DELTAS].sum(min_count=1)
    t = pd.concat([w, pd.DataFrame([{by: "Total", **tot.to_dict()}])], ignore_index=True)

    def pc(d, prev):
        return d / prev.where(prev > 0) * 100          # percent change over the periods that have a comparison
    return pd.DataFrame({
        label: t[by], "Last 7d": t["l7"].map(_num), "Δ vs prior 7d": t["d_wk"].map(_delta), "% 7d": pc(t["d_wk"], t["l7_prev"]).map(_pct),
        "Month-to-date": t["mtd"].map(_num), "Δ vs last month": t["d_mo"].map(_delta), "% mo": pc(t["d_mo"], t["mtd_pm"]).map(_pct),
        "Δ vs last year": t["d_yr"].map(_delta), "% yr": pc(t["d_yr"], t["mtd_ly"]).map(_pct),
        "Year-to-date": t["ytd"].map(_num), "Δ YTD vs last year": t["d_ytd"].map(_delta), "% YTD": pc(t["d_ytd"], t["ytd_ly"]).map(_pct)})


def _timeline(ev, measure, key):
    win = st.segmented_control("Timeline window", ["14 days", "30 days", "60 days", "90 days", "6 months", "1 year"],
                               default="30 days", key=f"{key}_twin")
    days = {"14 days": 14, "30 days": 30, "60 days": 60, "90 days": 90, "6 months": 183, "1 year": 365}[win or "30 days"]
    if ev.empty:
        return pd.DataFrame(), days
    end = ev["sail_date"].max()
    r = ev[ev["sail_date"] > end - pd.Timedelta(days=days)].copy()
    if r.empty:
        st.info("No sailings in this window.")
        return r, days
    r["size"] = r["kmt"].fillna(r["kmt"].median() if r["kmt"].notna().any() else 20).clip(lower=5)
    order = r.groupby("port")["kmt"].agg(["size"]).sort_values("size", ascending=False).index[:28].tolist()
    r = r[r["port"].isin(order)]
    fig = px.scatter(r, x="sail_date", y="port", size="size", color="commodity", color_discrete_map=COMM_COLORS,
                     category_orders={"port": order[::-1], "commodity": _comm_sort(r["commodity"].unique())}, size_max=18,
                     hover_data={"vessel": True, "destination": True, "kmt": ":,.1f", "size": False, "sail_date": "|%b %d, %Y"},
                     labels={"sail_date": "", "port": "", "kmt": "kMT"})
    st.plotly_chart(_layout(fig, 140 + 24 * len(order), f"Sailings, last {win or '30 days'} (bubble = kMT)"), key=f"{key}_timeline")
    return r, days


def _sailed_section(country, source, events, area, comms, measure, months, key):
    st.markdown("#### Sailed")
    ev = events[(events["source"] == source) & events["region"].isin(area) & events["commodity"].isin(comms)].copy()
    if ev.empty:
        st.info("No sailings banked for this selection.")
        return
    ev["sail_date"] = pd.to_datetime(ev["sail_date"])
    asof = ev["sail_date"].max()
    st.caption(f"Sailings through {asof:%b %d, %Y}. Week = last 7 days vs the 7 before; month and year compare month-to-date with the "
               "same days of last month and of last year.")
    w = ch.sailed_windows(ev, ["commodity"], measure)
    t = _sailed_table(w, "Commodity", "commodity")
    d_cols = [c for c in t.columns if c.startswith(("Δ", "%"))]
    st.dataframe(_styled(t, d_cols, label_col="Commodity"), hide_index=True, height=min(38 * (len(t) + 1), 520))
    if source == "Argentina":
        st.caption("Argentina's sailed history starts Oct 1, 2026 (the first Nabsa report), so last-month and last-year comparisons are n/a for now.")

    # monthly shipments: the stacked chart, now filterable and switchable
    st.markdown("**Monthly shipments**")
    ev["month"] = ev["sail_date"].dt.to_period("M").dt.to_timestamp()
    ev["_v"] = ev["vessel_w"]
    ev["_m"] = ev["_v"] if measure == "vessels" else ev["kmt"].fillna(0.0)
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
        st.dataframe(piv.map(lambda v: "" if v == 0 else _num(v)), height=min(38 * (len(piv) + 1), 520))
    st.markdown("**Sailing timeline**")
    recent, days = _timeline(ev, measure, key)
    st.markdown("**Where recent sailings went**")
    if not recent.empty:
        r = recent.assign(destination=recent["destination"])
        r[measure if measure == "kmt" else "vessels"] = r["kmt"].fillna(0.0) if measure == "kmt" else r["vessel_w"]
        _destinations(r, measure, f"Destinations of sailings, last {days} days ({_unit(measure)})", f"{key}_sdest")


def page_country(country, facts_db):
    source = COUNTRIES[country]
    key = f"c_{source}"
    mtime = os.path.getmtime(facts_db)
    c_show, c_area, c_comm = st.columns([1.4, 2.2, 3.4])          # every filter in one row at the top
    with c_show:
        measure, alloc = _measure_toggle(key, wide=False)
    lineup, events, detail, meta = _prepare(facts_db, mtime, measure, alloc)
    if not (detail["source"] == source).any():
        st.info(f"No {country} line-up banked yet.")
        return
    area, comms = _filters(source, detail, key, c_area, c_comm)
    _lineup_section(country, source, lineup, detail, meta, area, comms, measure, key)
    st.markdown("---")
    months = st.slider("Months of shipment history", 3, 36, 12, key=f"{key}_months")
    _sailed_section(country, source, events, area, comms, measure, months, key)
