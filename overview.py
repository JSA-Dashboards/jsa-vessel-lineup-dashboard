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

import base64
import functools
import os

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

import copy_tools
from fact_model import changes as ch
from fact_model import combo_calibration as cc
from fact_model import fgis_fill as ff
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


@functools.lru_cache(maxsize=1)
def _watermark():
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo-50yr.png")
        return "data:image/png;base64," + base64.b64encode(open(path, "rb").read()).decode()
    except OSError:
        return None


def _layout(fig, height=340, title=None, unified=True):
    """Same look as the CFTC COT dashboard: white panel, light grid, small legend top-left, faint JSA watermark."""
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=44 if title else 10, b=10), paper_bgcolor=SURFACE,
                      plot_bgcolor=SURFACE, font=dict(color=MUTED, family="Source Sans 3, Arial, sans-serif", size=12),
                      title=dict(text=title, x=0, y=0.97, font=dict(size=13, color=INK)) if title else None,
                      hovermode="x unified" if unified else "closest",
                      legend=dict(orientation="h", y=1.12 if not title else 1.0, x=0.45 if title else 0, font=dict(size=11, color=MUTED)))
    fig.update_xaxes(gridcolor=BORDER, linecolor=BORDER, tickfont=dict(color=MUTED, size=11), zeroline=False)
    fig.update_yaxes(gridcolor=BORDER, linecolor=BORDER, tickfont=dict(color=MUTED, size=11), zeroline=False)
    wm = _watermark()
    if wm:
        fig.add_layout_image(dict(source=wm, xref="paper", yref="paper", x=0.5, y=0.5, sizex=0.5, sizey=0.5,
                                  xanchor="center", yanchor="middle", sizing="contain", opacity=0.10, layer="below"))
    return fig


def tile(label, value, delta="", sub=""):
    """A CFTC-style tile: small caps label, big figure, neutral movement line, grey sub-line."""
    d = f'<div class="tile-delta-neu">{delta}</div>' if delta else ""
    u = f'<div class="tile-sub">{sub}</div>' if sub else ""
    return f'<div class="tile"><div class="tile-label">{label}</div><div class="tile-value">{value}</div>{d}{u}</div>'


def _arrow(v):
    return "–" if v is None or pd.isna(v) else ("▲ " if v > 0.05 else "▼ " if v < -0.05 else "") + _delta(v).lstrip("+−")


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
def _calibration(facts_db, mtime=None):
    return cc.load_calibration(facts_db)


@st.cache_data(show_spinner=False, max_entries=4)
def _fill_table(facts_db, mtime=None):
    t = store.load_table("events_fill", facts_db)
    return t if len(t) else pd.DataFrame(columns=ff.FILL_COLS)


@st.cache_data(show_spinner=False, max_entries=4)
def _vetting(facts_db, mtime=None):
    return cc.load_vetting(facts_db)


@st.cache_data(show_spinner=False, max_entries=6)
def _prepare(facts_db, mtime, measure, split=True, fill=True):
    """Line-up, events and the week/month/year detail for the six commodities. The change baselines are worked out
    on the WHOLE line-up first (so a region whose newest file came through empty is recognised as a data gap) and
    only then cut down to the six commodities. With `split`, combo boats are divided across the commodities they carry
    (their own USDA-measured split when known, else the learned average for their combo type; see combo_calibration) -
    tonnage and vessel counts both, totals conserved. Returns (lineup, events, detail, meta, gaps, left_out)."""
    lineup, events = _load(facts_db, mtime)
    events = events.copy()
    events["vessel_w"] = events["vessel_w"].fillna(1.0) if "vessel_w" in events else 1.0
    lineup, events = _tidy(lineup), _tidy(events)
    if fill:                                    # undisclosed 'RVT' sailings: cargo and tonnage read from USDA's vessel inspections
        events = ff.apply_fill(events, _fill_table(facts_db, mtime))
        events["destination"] = events["destination"].astype(str).str.strip().str.title().replace({k.title(): "Unknown" for k in UNK})
    if split:
        table, weights, actual, _ = _calibration(facts_db, mtime)
        lineup = cc.allocate(lineup, table, weights, None, value_cols=("kmt",), count_cols=("vessels",))
        events = cc.allocate(events, table, weights, actual, value_cols=("kmt",), count_cols=("vessel_w",), id_col="event_id")
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


def _split_toggle(key):
    """Two switches for the parts of the data the line-up reports do not give us directly. -> (split combos, fill undisclosed)."""
    split = st.checkbox("Split combo boats", value=True, key=f"{key}_split",
                        help="A combo boat (e.g. CORN/SBM) is listed with one total. Its tonnage and its vessel count are divided "
                             "across the commodities it carries using what USDA inspections show boats of that type really "
                             "loaded - never an even split. Off = combo boats are left out.")
    fill = st.checkbox("Fill undisclosed sailings", value=True, key=f"{key}_fill",
                       help="Southport lists some sailed boats as RVT (cargo not disclosed). USDA inspects every export vessel by "
                            "name, so their commodity, tonnage and destination are read from USDA. Off = they stay out of the "
                            "six commodities.")
    return split, fill


def combo_method_note(facts_db, mtime):
    """Expander explaining how combo boats are split, with the learned shares and how well each approach predicts."""
    table, weights, actual, meta = _calibration(facts_db, mtime)
    with st.expander("How combo boats are split"):
        if not meta:
            st.info("No combo calibration is stored yet; combo boats use the fallback weights until it runs.")
            return
        pct = lambda v: "n/a" if pd.isna(v) else f"{v:.0%}"
        st.markdown(
            f"A combo boat is listed once with one total tonnage (e.g. **CORN/SBM 60 kMT**). USDA inspects the grain on each vessel "
            f"by name, so for boats that have sailed we can see what they really loaded: **{meta.get('usable', 0):,.0f} of "
            f"{meta.get('combo_events', 0):,.0f}** US combo sailings since 2023 could be matched and checked "
            f"({meta.get('matched', 0):,.0f} matched by vessel name; the rest did not add up, e.g. two-port loadings).")
        st.markdown(
            "- **Sailed boats** use their own measured split when matched, otherwise the average for their combo type.\n"
            "- **Boats still in the line-up** use the average for their combo type (below), from the last 24 months of matched boats.\n"
            "- A combo type too rare to average uses a fitted per-commodity weight (corn 1.0, wheat ~0.6, soybeans/meal ~0.5, "
            "DDGS ~0.3) - **never an even split**.")
        st.markdown(
            "**Undisclosed sailings.** Many of Southport's sailed rows (about 28%) "
            "read RVT (cargo not disclosed). USDA reports every inspected vessel by name with grain, tonnage and destination, so "
            f"**{meta.get('filled', 0):,.0f} of {meta.get('undisclosed', 0):,.0f}** of them are filled from USDA "
            f"(~{meta.get('filled_kmt', 0):,.0f} kMT of grain). Grain only: meal or DDGS on those boats stays unknown, and the newest "
            "sailings are filled once USDA publishes the vessel.")
        c = st.columns(4)
        c[0].markdown(tile("Even split (50/50)", pct(meta.get("mae_equal")), "avg error / boat"), unsafe_allow_html=True)
        c[1].markdown(tile("Corn / wheat double", pct(meta.get("mae_rule")), "avg error / boat"), unsafe_allow_html=True)
        c[2].markdown(tile("Fitted weights", pct(meta.get("mae_weights")), "avg error / boat"), unsafe_allow_html=True)
        c[3].markdown(tile("Average of type", pct(meta.get("mae_learned")), "avg error / boat"), unsafe_allow_html=True)
        st.caption("Average error in a boat's commodity share, tested on boats whose split USDA shows. Individual boats vary a lot "
                   "(a CORN/SBM boat's corn share runs from about 45% to 90%), so one boat's split is an estimate; totals over "
                   "many boats are firmer.")
        t = table[table["scope"] == "US"].copy()
        if len(t):
            t["share"] = t["share"].map(pct)
            t["range"] = [("" if pd.isna(a) else f"{a:.0%} to {b:.0%}") for a, b in zip(t["p10"], t["p90"])]
            show = t.pivot_table(index=["key", "n"], columns="commodity", values="share", aggfunc="first").reset_index()
            show = show.rename(columns={"key": "Combo type", "n": "Boats"}).sort_values("Boats", ascending=False).fillna("")
            copy_tools.table(show, "combo_types", height=min(38 * (len(show) + 1), 520))
        _vetting_panel(facts_db, mtime, meta)


def _vetting_panel(facts_db, mtime, meta):
    """The ongoing check: refreshed with every calibration run, tested out-of-sample against USDA's monthly totals."""
    bt, hist, _ = _vetting(facts_db, mtime)
    if bt.empty:
        return
    st.markdown("##### Ongoing check against USDA")
    status, reasons = meta.get("status", "ok"), [r for r in str(meta.get("reasons", "")).split(" | ") if r]
    if status == "ok":
        st.success(f"Healthy as of {meta.get('refreshed', '')}: the learned split beats the even split per boat and fits USDA's monthly "
                   "totals better than leaving combos out.", icon=":material/check_circle:")
    else:
        (st.error if status == "fail" else st.warning)("Needs a look: " + "; ".join(reasons), icon=":material/warning:")
    if meta.get("fill_n"):
        f1 = st.columns(4)
        f1[0].markdown(tile("Undisclosed sailings filled", f"{meta['filled'] / max(meta['undisclosed'], 1):.0%}",
                            f"{meta['filled']:,.0f} of {meta['undisclosed']:,.0f}"), unsafe_allow_html=True)
        f1[1].markdown(tile("Fill names the right commodity", f"{meta['fill_commodity_match']:.0%}",
                            f"tested on {meta['fill_n']:,.0f} boats we know"), unsafe_allow_html=True)
        f1[2].markdown(tile("Fill tonnage vs known", f"{meta['fill_total_ratio']:.0%}",
                            f"median boat off by {meta['fill_tonnage_err']:.0%}"), unsafe_allow_html=True)
        f1[3].markdown(tile("Monthly error vs USDA", f"{meta['wape_learned']:.0%}",
                            f"was {meta['wape_learned_nofill']:.0%} before the fill", f"Mississippi {meta['wape_usg_learned']:.0%} (was {meta['wape_usg_learned_nofill']:.0%})"),
                       unsafe_allow_html=True)
    summ = cc.backtest_summary(bt)
    c = st.columns(len(cc.STRATEGIES))
    for col, s_ in zip(c, cc.STRATEGIES):
        w, b = summ.loc[s_, "wape"], summ.loc[s_, "bias"]
        col.markdown(tile(cc.STRATEGY_LABEL[s_], "n/a" if pd.isna(w) else f"{w:.0%}", "avg monthly error", f"net {b:+.0%} vs USDA"),
                     unsafe_allow_html=True)
    st.caption("Each of the last 12 months is rebuilt using only boats that sailed BEFORE it, then compared with what USDA inspected "
               "(corn, soybeans, wheat, sorghum; meal is not inspected). USDA inspects before loading, so no method matches exactly; "
               "what counts is whether splitting the combos closes the gap. The tiles above are with undisclosed sailings filled from USDA; "
               "the remaining shortfall (negative net) is vessels our line-up reports never listed (about 4% on the Mississippi, up to "
               "11% in Puget Sound) plus timing.")
    ports = sorted(bt["fgis_port"].unique(), key=lambda p: -bt[bt["fgis_port"] == p]["fgis_kmt"].sum())
    k1, k2 = st.columns(2)
    port = k1.selectbox("USDA port region", ports, key="vet_port")
    com = k2.selectbox("Commodity", cc.GRAIN.values(), key="vet_com")
    d = bt[(bt["fgis_port"] == port) & (bt["commodity"] == com)]
    lines = d[d["strategy"].isin(["excluded", "equal", "learned"])].assign(series=lambda x: x["strategy"].map(cc.STRATEGY_LABEL))
    base = d[d["strategy"] == "learned"].assign(series="USDA inspected", pred_kmt=lambda x: x["fgis_kmt"])
    fig = px.line(pd.concat([lines, base]), x="month", y="pred_kmt", color="series", markers=True,
                  color_discrete_map={"USDA inspected": INK, cc.STRATEGY_LABEL["learned"]: "#0693e3",
                                      cc.STRATEGY_LABEL["equal"]: "#c98b3c", cc.STRATEGY_LABEL["excluded"]: "#9aa5a0"},
                  labels={"pred_kmt": "kMT", "month": "", "series": ""})
    copy_tools.chart(_layout(fig, 300, f"{com}, {port.title()}: monthly tonnage by method vs USDA (kMT)"), "vet_chart")
    if len(hist) > 1:
        h = hist.tail(12)[["refreshed", "status", "usable", "mae_learned", "mae_equal", "wape_learned", "wape_equal"]].copy()
        for col in ("mae_learned", "mae_equal", "wape_learned", "wape_equal"):
            h[col] = h[col].map(lambda v: "n/a" if pd.isna(v) else f"{v:.0%}")
        h.columns = ["Refreshed", "Status", "Boats used", "Per-boat error (learned)", "Per-boat error (even)",
                     "Monthly error (learned)", "Monthly error (even)"]
        st.markdown("**History of the check**")
        copy_tools.table(h.iloc[::-1], "vet_history", height=min(38 * (len(h) + 1), 400))


def _asof_caption(meta, sources=None, left_out=None):
    m = meta if sources is None else meta[meta["source"].isin(sources)]
    bits = [f"{SRC_LABEL.get(src, src)} {pd.Timestamp(r['as_of']):%b %d}" for src, r in m.groupby("source").agg(as_of=("as_of", "max")).iterrows()]
    notes = [n for n in m["note"].unique() if n]
    extra = ""
    if left_out:
        shown = {k: v for k, v in left_out.items() if (sources is None or COUNTRIES[k] in sources) and v >= 0.5}
        if shown:
            extra = ("  \nCombo (multi-commodity) boats are left out: " +
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


def show_change_table(detail, by, label, key):
    if detail.empty:
        st.info("Nothing in the line-up for this selection.")
        return
    t = change_table(detail, by, label)
    d_cols = [c for c in t.columns if c.startswith(("Δ", "%"))]
    copy_tools.table(t, key, styler=_styled(t, d_cols, label), height=min(38 * (len(t) + 1), 560))


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


def show_port_table(detail, key):
    if detail.empty:
        st.info("Nothing in the line-up for this selection.")
        return
    t = port_table(detail)
    copy_tools.table(t, key, styler=_styled(t, ["Δ Week", "Δ Month", "Δ Year"]), height=min(38 * (len(t) + 1), 640))


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
    copy_tools.chart(_layout(fig, 320, f"Line-up over time ({_unit(measure)})"), f"{key}_trend")
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
    copy_tools.chart(_layout(fig, 60 + 26 * len(tot), title, unified=False), key)
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
    k[0].markdown(tile(f"Line-up now ({_unit(measure)})", _num(tot["now"])), unsafe_allow_html=True)
    for col, (lbl, chg, pc) in zip(k[1:], (("vs last week", tot["d_wk"], tot["p_wk"]), ("vs last month", tot["d_mo"], tot["p_mo"]),
                                            ("vs last year", tot["d_yr"], tot["p_yr"]))):
        col.markdown(tile(lbl, _delta(chg), _arrow(chg), _pct(pc)), unsafe_allow_html=True)
    srcs = sorted(d["source"].unique())
    _asof_caption(meta, srcs, left_out)
    new = [s for s in srcs if meta.loc[meta["source"] == s, "wk_date"].isna().all()]
    if new:
        st.caption(f"{', '.join(SRC_LABEL[s] for s in new)}: daily line-up began Oct 22, 2026, so its week / month / year "
                   "comparisons fill in as it accumulates (it is in the totals, not in the changes).")
    show_change_table(d, GROUPS[group_by], group_by, f"{key}_ct")
    ln = _apply(lineup, sel)
    _trend(ln, gaps, measure, key)
    if port_matrix:
        st.markdown("**By port**")
        show_port_table(d, f"{key}_ports")
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
    copy_tools.chart(_layout(fig, 140 + 24 * len(order), f"Sailings, last {win or '30 days'} (bubble = kMT)", unified=False), f"{key}_timeline")
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
    copy_tools.table(t, f"{key}_sailed", styler=_styled(t, [c for c in t.columns if c.startswith(("Δ", "%"))], group_by),
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
        copy_tools.chart(_layout(fig, 340, f"Shipped {_unit(measure)} by month and commodity"), f"{key}_monthly")
        piv = mm.pivot_table(index="commodity", columns="month", values="_m", aggfunc="sum", fill_value=0.0)
        piv = piv.loc[_comm_sort(piv.index)]
        piv.columns = [c.strftime("%b %Y") for c in piv.columns]
        piv["Total"] = piv.sum(axis=1)
        piv.loc["Total"] = piv.sum()
        copy_tools.table(piv.map(lambda v: "" if v == 0 else _num(v)), f"{key}_pivot", height=min(38 * (len(piv) + 1), 360),
                         hide_index=False)
    st.markdown("**Sailing timeline**")
    recent, days = _timeline(ev, key)
    st.markdown("**Where recent sailings went**")
    if not recent.empty:
        r = recent.assign(_v=recent["vessel_w"] if measure == "vessels" else recent["kmt"].fillna(0.0))
        _destinations(r, "_v", measure, f"Destinations of sailings, last {days} days ({_unit(measure)})", f"{key}_sdest")


# ── PAGE: summary ────────────────────────────────────────────────────────────
# The scopes the summary is organised around: regions first (PNW / USG / TXG for the US), then countries.
SCOPES = [("Total", {}),
          ("PNW", {"source": ["US"], "area": ["PNW"]}),
          ("USG", {"source": ["US"], "area": ["USG"]}),
          ("TXG", {"source": ["US"], "area": ["TXG"]}),
          ("United States", {"source": ["US"]}),
          ("Brazil", {"source": ["Brazil"]}),
          ("Argentina", {"source": ["Argentina"]})]


def _by_commodity(frame, value):
    return frame.groupby("commodity")[value].sum().reindex(COMMODITIES).fillna(0.0)


def _snapshot_matrices(detail, events, measure):
    """Commodity rows x scope columns (PNW, USG, TXG, United States, Brazil, Argentina, Total)."""
    unit = _unit(measure)
    order = [n for n, _ in SCOPES if n != "Total"] + ["Total"]
    now, mtd = {}, {}
    for name, sel in SCOPES:
        now[name] = _by_commodity(_apply(detail, sel), "now")
        ev = _apply(events, sel)
        w = ch.sailed_windows(ev, ["commodity"], measure) if not ev.empty else pd.DataFrame(columns=["commodity", "mtd"])
        mtd[name] = _by_commodity(w, "mtd") if not w.empty else pd.Series(0.0, index=COMMODITIES)

    def render(cols):
        m = pd.DataFrame(cols)[order]
        m.loc["Total"] = m.sum()
        out = m.map(lambda v: "" if v == 0 else _num(v))
        out.insert(0, "Commodity", out.index)
        return out
    for title, cols in ((f"Line-up now ({unit})", now), (f"Sailed month-to-date ({unit})", mtd)):
        st.markdown(f"**{title}**")
        t = render(cols)
        copy_tools.table(t, "snap_now" if title.startswith("Line-up") else "snap_mtd", styler=_styled(t, [], "Commodity"),
                         height=38 * (len(t) + 1))


def _area_table(detail, source):
    """Areas of one country as rows: line-up now and the week / month / year change."""
    d = detail[detail["source"] == source]
    if d["area"].nunique() < 2:
        return
    show_change_table(d, "area", "Area", f"sum_area_{source}")


def _explorer(lineup, events, detail, meta, gaps, left_out, measure):
    st.markdown("### Explore")
    st.caption("Pick what you want to look at: country, area, commodity, and line-up or sailed. Port and elevator are there if you need them.")
    c1, c2 = st.columns([3, 2])
    with c1:
        countries = st.pills("Country", list(COUNTRIES), selection_mode="multi", default=list(COUNTRIES), key="ex_country") or list(COUNTRIES)
    with c2:
        view = st.segmented_control("View", ["Line-up", "Sailed"], default="Line-up", key="ex_view") or "Line-up"
    srcs = [COUNTRIES[c] for c in countries]
    base = pd.concat([detail[["source", "region", "area", "port", "elevator"]], events[["source", "region", "area", "port", "elevator"]]]).drop_duplicates()
    base = base[base["source"].isin(srcs)]
    # options depend on the selections above them, so those selections are part of each widget's key
    rank = {r: i for i, r in enumerate(_region_order())}
    areas = sorted(base["area"].unique(), key=lambda a: rank.get(next(iter(base.loc[base["area"] == a, "region"])), 99))
    c3, c4 = st.columns([3, 2])
    with c3:
        area = st.pills("Area", areas, selection_mode="multi", default=areas, key=f"ex_area_{'-'.join(srcs)}") or areas
    with c4:
        comm = st.multiselect("Commodity", COMMODITIES, key="ex_comm", placeholder="All six commodities")
    ports = sorted(base[base["area"].isin(area)]["port"].unique())
    with st.expander("Narrow to a port or elevator (optional)"):
        k1, k2 = st.columns(2)
        with k1:
            port = st.multiselect("Port", ports, key=f"ex_port_{'-'.join(srcs)}_{'-'.join(area)}", placeholder="All ports")
        elevs = sorted(base[base["area"].isin(area) & (base["port"].isin(port) if port else True)]["elevator"].unique())
        with k2:
            elev = st.multiselect("Elevator / terminal", elevs, key=f"ex_elev_{'-'.join(srcs)}_{'-'.join(area)}_{'-'.join(port)}",
                                  placeholder="All elevators")
    sel = {"source": srcs, "area": area, "port": port or None, "elevator": elev or None, "commodity": comm or COMMODITIES}
    group_by = st.segmented_control("Group the table by", list(GROUPS), default="Area", key="ex_group") or "Area"
    if view == "Line-up":
        lineup_view(sel, lineup, detail, meta, gaps, left_out, measure, "ex", group_by)
    else:
        months = st.slider("Months of shipment history", 3, 36, 12, key="ex_months")
        sailed_view(sel, events, measure, months, "ex", group_by)


def page_summary(facts_db):
    mtime = os.path.getmtime(facts_db)
    t1, t2 = st.columns([3, 3], vertical_alignment="bottom")
    with t1:
        measure = _measure_toggle("sum")
    with t2:
        split, fill = _split_toggle("sum")
    lineup, events, detail, meta, gaps, left_out = _prepare(facts_db, mtime, measure, split, fill)
    if split or fill:
        combo_method_note(facts_db, mtime)
    unit = _unit(measure)

    st.caption(f"Line-up now, in {unit}. Under each figure: change vs last week (wk), month (Mo) and year (Yr).")
    cols = st.columns(len(SCOPES))
    for col, (name, sel) in zip(cols, SCOPES):
        d = _apply(detail, sel)
        label = "US" if name == "United States" else name
        if d.empty:
            col.markdown(tile(label, "—"), unsafe_allow_html=True)
            continue
        r = ch.rollup(d.assign(_t="t"), ["_t"]).iloc[0]
        col.markdown(tile(label, _num(r["now"]), f"{_arrow(r['d_wk'])} wk", f"Mo {_delta(r['d_mo'])} · Yr {_delta(r['d_yr'])}"),
                     unsafe_allow_html=True)
    _asof_caption(meta, None, left_out)

    st.markdown("##### Snapshot by commodity")
    _snapshot_matrices(detail, events, measure)

    st.markdown("##### Week, month and year change")
    st.caption("Open a region or country: commodities for the line-up and for what has sailed. Countries also show their areas.")
    for name, sel in SCOPES:
        d = _apply(detail, sel)
        if d.empty:
            continue
        with st.expander(name, expanded=name == "Total"):
            st.markdown("**Line-up**")
            show_change_table(d, "commodity", "Commodity", f"sum_ct_{name}")
            if name in ("United States", "Brazil", "Argentina"):
                st.markdown("**Line-up by area**")
                _area_table(detail, sel["source"][0])
            ev = _apply(events, sel)
            if not ev.empty:
                st.markdown("**Sailed**")
                t = _sailed_table(ch.sailed_windows(ev, ["commodity"], measure), "Commodity", "commodity")
                copy_tools.table(t, f"sum_sailed_{name}", styler=_styled(t, [c for c in t.columns if c.startswith(("Δ", "%"))], "Commodity"),
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
        split, fill = _split_toggle(key)
    lineup, events, detail, meta, gaps, left_out = _prepare(facts_db, mtime, measure, split, fill)
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
