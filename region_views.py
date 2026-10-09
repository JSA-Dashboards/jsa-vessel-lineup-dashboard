"""Region-grouped views: Line-up, Trend, Execution, Forecast (reads facts.db built by build_facts.py).

Pivots are always region -> port -> elevator. Unmapped ports/elevators are shown, never dropped.
Called from app.py as page_regions(ui); `ui` carries the app's kpi/sec helpers, chart layout and
colours so this module does not import app.py.
"""

import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from fact_model import dimensions as dim
from fact_model import adjustments as adj
from fact_model import events as ev
from fact_model import fgis, store
from fact_model import forecast as fcst
from fact_model import lineup as lu
from fact_model import summary as sm

LEVELS = {"Region": ["region"], "Region → Port": ["region", "port"],
          "Region → Port → Elevator": ["region", "port", "elevator"]}


@st.cache_data(show_spinner=False)
def _load(facts_db, mtime=None):
    return store.load_lineup(facts_db), store.load_events(facts_db)


@st.cache_data(show_spinner=False)
def _forecast(weight, mode, _events, _lineup, mtime=None):
    return fcst.run_forecast(_events, _lineup, weight=weight, mode=mode)


@st.cache_data(show_spinner=False)
def _backtest(elapsed, _events, _lineup, mtime=None):
    return fcst.backtest_methods(_events, _lineup, elapsed=elapsed)


@st.cache_data(ttl=3600, show_spinner=False)
def _load_fgis(start_year):
    return fgis.fetch_fgis_monthly(start_year)


def _region_order():
    cfg = dim.default_config()
    order = cfg.regions.sort_values("sort_order", key=lambda s: s.astype(int))["region"].tolist()
    return order + [dim.UNMAPPED]


def _sort_regions(df):
    order = {r: i for i, r in enumerate(_region_order())}
    keys = ["_r"] + [c for c in ("port", "elevator", "commodity") if c in df.columns]
    return df.assign(_r=df["region"].map(order).fillna(99)).sort_values(keys).drop(columns="_r")


def _unmapped_panel(df, value_col, noun):
    bad = dim.unmapped_report(df, value_col)
    if bad.empty:
        return
    st.warning(f"{len(bad)} {noun} could not be mapped to a region and are shown as UNMAPPED "
               f"({bad[value_col].sum():,.0f} kMT). Assign them in fact_model/config/elevator_port.csv "
               f"(US) or port_region.csv (ports).", icon="⚠️")
    with st.expander("Unmapped detail"):
        st.dataframe(bad, use_container_width=True, hide_index=True)


def _pct(x):
    return "—" if pd.isna(x) else f"{x:.0%}"


# ── Line-up ──────────────────────────────────────────────────────────────────

@st.fragment
def view_lineup(lineup, ui):
    if lineup.empty:
        st.info("No line-up snapshots banked yet. Run build_facts.py.")
        return
    dates = [d.strftime("%Y-%m-%d") for d in sorted(lineup["report_date"].unique(), reverse=True)]
    c1, c2, c3 = st.columns([1, 1.4, 1.6])
    pick = c1.selectbox("Snapshot date", dates, key="rl_snap")
    snap = lu.asof_snapshot(lineup, pick)
    asof = snap.groupby("source")["report_date"].max()
    c1.caption(" · ".join(f"{s} as of {d:%b %d}" for s, d in asof.items()))
    regions = [r for r in _region_order() if r in set(snap["region"])]
    reg_sel = c2.multiselect("Regions", regions, default=regions, key="rl_reg")
    comms = sorted(snap["commodity"].unique())
    com_sel = c3.multiselect("Commodities", comms, default=comms, key="rl_com")
    s = snap[snap["region"].isin(reg_sel) & snap["commodity"].isin(com_sel)]

    ui["sec"]("📝  Line-up summary — vessels queued, change vs last week (LW) / last month (LM)")
    lines = sm.summary_lines(sm.lineup_summary(lineup))
    show_bz = st.checkbox("Include Brazil", value=False, key="rl_sum_bz")
    text = []
    for src, region, as_of, line, note in lines:
        if src == "Brazil" and not show_bz:
            continue
        if region == "UNMAPPED" and not show_bz and src == "US":
            continue
        text.append(line)
        if note:
            text.append("    " + note)
    st.code(chr(10).join(text) if text else "No line-up snapshots.", language=None)
    st.caption("Vessel counts, not tonnage. LW / LM compare with the banked snapshot nearest 7 days / 1 month before the "
               "region's latest one; n/a means none was close enough. COMBO = multi-commodity vessels (counted once, as COMBO).")

    k1, k2, k3, k4 = st.columns(4)
    ui["kpi"](k1, "Queued tonnage", f"{s['kmt'].sum():,.0f} kMT", "latest snapshot per source", ui["blue"])
    ui["kpi"](k2, "Vessels queued", f"{int(s['vessels'].sum()):,}", f"{s['port'].nunique()} ports", ui["amb"])
    disclosed = 1 - s["vessels_no_tonnage"].sum() / s["vessels"].sum() if s["vessels"].sum() else float("nan")
    ui["kpi"](k3, "Tonnage disclosed", _pct(disclosed), "vessels with a reported MT (US shows RVT)", ui["pos"])
    ui["kpi"](k4, "Unmapped", f"{s.loc[~s['mapped'], 'kmt'].sum():,.0f} kMT", "region not assigned yet", ui["neg"])
    _unmapped_panel(snap, "kmt", "elevator/port group(s)")

    ui["sec"]("🟡  Queued tonnage — region × commodity")
    piv = s.pivot_table(index="region", columns="commodity", values="kmt", aggfunc="sum", fill_value=0.0)
    piv = piv.reindex([r for r in _region_order() if r in piv.index])
    piv["Total"] = piv.sum(axis=1)
    cc, ct = st.columns([1.4, 1])
    with cc:
        g = s.groupby(["region", "commodity"], as_index=False)["kmt"].sum()
        fig = px.bar(g, x="region", y="kmt", color="commodity", color_discrete_map=ui["comm_colors"],
                     barmode="stack", labels={"kmt": "kMT", "region": ""}, title="Queued tonnage by region (kMT)",
                     category_orders={"region": [r for r in _region_order() if r in set(g["region"])]})
        fig.update_layout(**ui["layout"])
        fig.update_traces(marker_line_width=0)
        st.plotly_chart(fig, use_container_width=True)
    with ct:
        st.dataframe(piv.round(0), use_container_width=True)

    ui["sec"]("🎯  Destination")
    d = (s.groupby("destination", as_index=False).agg(kmt=("kmt", "sum"), vessels=("vessels", "sum"))
          .sort_values("kmt", ascending=False))
    topn = st.slider("Top destinations", 5, 30, 12, key="rl_topn")
    d = d.head(topn)
    dc, dt = st.columns([1.4, 1])
    with dc:
        fig = px.bar(d.sort_values("kmt"), x="kmt", y="destination", orientation="h",
                     labels={"kmt": "kMT", "destination": ""}, title="Top destinations (kMT)", color_discrete_sequence=[ui["green_lt"]])
        fig.update_layout(**ui["layout"])
        fig.update_traces(marker_line_width=0)
        st.plotly_chart(fig, use_container_width=True)
    with dt:
        st.dataframe(d.round(0), use_container_width=True, hide_index=True)

    ui["sec"]("🔎  Drill-down — region → port → elevator")
    r_opts = [r for r in _region_order() if r in set(s["region"])]
    if not r_opts:
        st.info("Nothing in the current filter.")
        return
    dr, dp = st.columns(2)
    reg = dr.selectbox("Region", r_opts, key="rl_dr_reg")
    in_reg = s[s["region"] == reg]
    ports = ["All ports"] + sorted(in_reg["port"].unique())
    port = dp.selectbox("Port", ports, key=f"rl_dr_port_{reg}")
    if port == "All ports":
        t = in_reg.groupby("port", as_index=False).agg(kmt=("kmt", "sum"), vessels=("vessels", "sum"),
                                                       no_tonnage=("vessels_no_tonnage", "sum"))
    else:
        t = (in_reg[in_reg["port"] == port].groupby(["elevator", "commodity"], as_index=False)
             .agg(kmt=("kmt", "sum"), vessels=("vessels", "sum"), no_tonnage=("vessels_no_tonnage", "sum")))
    st.dataframe(t.sort_values("kmt", ascending=False).round(0), use_container_width=True, hide_index=True)


# ── Trend ────────────────────────────────────────────────────────────────────

@st.fragment
def view_trend(lineup, ui):
    if lineup.empty:
        st.info("No line-up snapshots banked yet.")
        return
    ledger = (lineup.groupby("source")["report_date"]
              .agg(snapshots="nunique", first="min", last="max").reset_index())
    st.caption("Snapshots banked — every one is kept and plotted on its real date; no daily spacing is assumed. "
               "Sources are never added together because their snapshot dates differ.")
    st.dataframe(ledger, use_container_width=False, hide_index=True)

    c1, c2, c3 = st.columns([1, 1.4, 1.6])
    by = c1.radio("Split by", ["Commodity", "Region"], key="rt_by", horizontal=True)
    regions = [r for r in _region_order() if r in set(lineup["region"])]
    reg_sel = c2.multiselect("Regions", regions, default=regions, key="rt_reg")
    comms = sorted(lineup["commodity"].unique())
    com_sel = c3.multiselect("Commodities", comms, default=comms, key="rt_com")
    f = lineup[lineup["region"].isin(reg_sel) & lineup["commodity"].isin(com_sel)]
    col = "commodity" if by == "Commodity" else "region"
    t = lu.lineup_trend(f, by=["source", col])
    ui["sec"]("📈  Queued tonnage by snapshot date")
    fig = px.line(t, x="report_date", y="kmt", color=col, line_dash="source", markers=True,
                  color_discrete_map=ui["comm_colors"] if col == "commodity" else None,
                  labels={"kmt": "kMT", "report_date": "Snapshot"}, title="Queued tonnage (kMT)")
    fig.update_layout(**ui["layout"])
    fig.update_xaxes(tickformat="%b %d")
    st.plotly_chart(fig, use_container_width=True)

    ui["sec"]("↕️  Queue growth / retraction vs previous snapshot")
    rows = []
    for src, g in f.groupby("source"):
        ds = sorted(g["report_date"].unique())
        if len(ds) < 2:
            rows.append({"source": src, "note": f"only {len(ds)} snapshot banked - change shows once a second arrives"})
            continue
        cur, prev = ds[-1], ds[-2]
        a = g[g["report_date"] == cur].groupby(col)["kmt"].sum()
        b = g[g["report_date"] == prev].groupby(col)["kmt"].sum()
        x = pd.concat([b.rename("previous"), a.rename("latest")], axis=1).fillna(0.0)
        x["change"] = x["latest"] - x["previous"]
        x["pct"] = x["change"] / x["previous"].where(x["previous"] > 0)
        x = x.reset_index().assign(source=src, latest_date=pd.Timestamp(cur).date(), previous_date=pd.Timestamp(prev).date())
        rows.extend(x.to_dict("records"))
    out = pd.DataFrame(rows)
    st.dataframe(out, use_container_width=True, hide_index=True)


# ── Execution ────────────────────────────────────────────────────────────────

@st.fragment
def view_execution(events, ui):
    if events.empty:
        st.info("No sail events banked yet. Run build_facts.py.")
        return
    through = events.groupby("source")["sail_date"].max()
    st.caption("Shipped tonnage is built from vessel sail events (US: SAIL DATE; Brazil: ETCS of vessels the APS "
               "report lists as sailed). Figures are as of each source's last sail date: "
               + " · ".join(f"{s} through {d:%b %d, %Y}" for s, d in through.items()))
    c1, c2, c3 = st.columns([1.3, 1.2, 1.6])
    level = c1.radio("Group by", list(LEVELS), key="rx_level")
    srcs = sorted(events["source"].unique())
    src_sel = c2.multiselect("Source", srcs, default=srcs, key="rx_src")
    comms = sorted(events["commodity"].unique())
    com_sel = c3.multiselect("Commodities", comms, default=comms, key="rx_com")
    e = events[events["source"].isin(src_sel) & events["commodity"].isin(com_sel)]
    split = st.checkbox("Split by commodity", value=False, key="rx_split")
    cols = LEVELS[level] + (["commodity"] if split else [])
    asof = {s: d for s, d in through.items()}
    x = ev.events_executed(e, asof, by=cols)
    _unmapped_panel(e, "kmt", "elevator/port group(s) with sailings")

    allx = ev.events_executed(e, asof, by=["source"]).set_index("source")
    for src in sorted(src_sel):
        if src not in allx.index:
            continue
        r, first, last = allx.loc[src], events.loc[events["source"] == src, "sail_date"].min(), through[src]
        st.markdown(f"**{src}** — month-to-date {last:%b 1}–{last:%b %d}; sail history starts {first:%b %d, %Y}"
                    + (" (so MYTD/YTD only cover that span)" if first > pd.Timestamp(last.year, 1, 1) else ""))
        k1, k2, k3, k4 = st.columns(4)
        ui["kpi"](k1, f"{src} shipped MTD", f"{r['mtd_kmt']:,.0f} kMT", f"{int(r['vessels_mtd'])} vessels", ui["pos"])
        ui["kpi"](k2, f"{src} shipped MYTD", f"{r['mytd_kmt']:,.0f} kMT", f"{int(r['vessels_mytd'])} vessels (each commodity's marketing year)", ui["blue"])
        ui["kpi"](k3, f"{src} shipped YTD", f"{r['ytd_kmt']:,.0f} kMT", "calendar year", ui["purp"])
        cov = 1 - r["no_tonnage_mytd"] / r["vessels_mytd"] if r["vessels_mytd"] else float("nan")
        ui["kpi"](k4, f"{src} tonnage reported", _pct(cov), "of MYTD vessels (RVT = none; totals understate)", ui["amb"])

    ui["sec"](f"✅  Shipped — {level}")
    show = x.rename(columns={"mtd_kmt": "MTD kMT", "mytd_kmt": "MYTD kMT", "ytd_kmt": "YTD kMT",
                             "vessels_mytd": "Vessels MYTD", "coverage_mytd": "Tonnage reported"})
    show = _sort_regions(show)[cols + ["MTD kMT", "MYTD kMT", "YTD kMT", "Vessels MYTD", "Tonnage reported"]]
    st.dataframe(show.style.format({"MTD kMT": "{:,.0f}", "MYTD kMT": "{:,.0f}", "YTD kMT": "{:,.0f}",
                                    "Tonnage reported": "{:.0%}"}, na_rep="—"),
                 use_container_width=True, hide_index=True)

    ui["sec"]("📅  Shipped by month")
    m1, m2 = st.columns(2)
    months = m1.selectbox("Window (months)", [6, 12, 24, 36], index=1, key="rx_months")
    grp = m2.selectbox("Stack by", ["Region", "Commodity", "Port"], key="rx_grp")
    gcol = {"Region": "region", "Commodity": "commodity", "Port": "port"}[grp]
    mm = ev.events_monthly(e, by=["source", gcol])
    cut = (pd.Timestamp(through.max()).to_period("M").to_timestamp() - pd.DateOffset(months=months - 1))
    mm = mm[mm["month"] >= cut]
    mm = mm.groupby(["month", gcol], as_index=False).agg(kmt=("kmt", "sum"), vessels=("vessels", "sum"), no_tonnage=("no_tonnage", "sum"))
    fig = px.bar(mm, x="month", y="kmt", color=gcol, barmode="stack", labels={"kmt": "kMT", "month": ""}, title=f"Shipped by month, stacked by {grp.lower()} (kMT)",
                 color_discrete_map=ui["comm_colors"] if gcol == "commodity" else None)
    fig.update_layout(**ui["layout"])
    fig.update_xaxes(tickformat="%b %Y")
    fig.update_traces(marker_line_width=0)
    st.plotly_chart(fig, use_container_width=True)
    pv = mm.pivot_table(index=gcol, columns="month", values="kmt", aggfunc="sum", fill_value=0.0)
    pv.columns = [c.strftime("%b %Y") for c in pv.columns]
    pv["Total"] = pv.sum(axis=1)
    st.dataframe(pv.round(0), use_container_width=True)
    st.caption("The latest month is partial. Months include only vessels with a reported tonnage; "
               "the no-tonnage count is in the reconciliation below.")

    _view_reconciliation(events, ui)


def _view_reconciliation(events, ui):
    ui["sec"]("⚖️  Reconciliation vs USDA FGIS export inspections (US, vessel/ship only)")
    us = events[events["source"] == "US"]
    if us.empty:
        st.info("No US sail events to reconcile.")
        return
    start_year = int(us["sail_date"].max().year) - 1
    try:
        f = _load_fgis(start_year)
    except Exception as exc:
        st.info(f"FGIS data is unavailable right now ({type(exc).__name__}). The reconciliation will show when it is reachable.")
        return
    rec = fgis.reconcile_monthly(us, f)
    rec = rec[rec["month"] >= pd.Timestamp(us["sail_date"].max()).to_period("M").to_timestamp() - pd.DateOffset(months=5)]
    c1, c2 = st.columns(2)
    com = c1.selectbox("Commodity", ["All matched"] + sorted(set(rec.loc[rec["fgis_kmt"].notna(), "commodity"])), key="rx_rec_com")
    r = rec if com == "All matched" else rec[rec["commodity"] == com]
    r = r[r["fgis_kmt"].notna()]
    g = r.groupby("month", as_index=False).agg(ours=("ours_kmt", "sum"), fgis=("fgis_kmt", "sum"), no_tonnage=("ours_no_tonnage", "sum"))
    g["diff"] = g["ours"] - g["fgis"]
    g["ratio"] = g["ours"] / g["fgis"].where(g["fgis"] > 0)
    fig = go.Figure()
    fig.add_bar(x=g["month"], y=g["fgis"], name="FGIS inspected", marker_color=ui["blue"])
    fig.add_bar(x=g["month"], y=g["ours"], name="Our sail events", marker_color=ui["green_lt"])
    fig.update_layout(**ui["layout"], barmode="group", title="FGIS inspected vs our sail events (kMT)")
    fig.update_xaxes(tickformat="%b %Y")
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(g.rename(columns={"ours": "Ours kMT", "fgis": "FGIS kMT", "diff": "Ours − FGIS", "ratio": "Ours / FGIS",
                                   "no_tonnage": "Our vessels w/o MT"}).style.format(
        {"Ours kMT": "{:,.0f}", "FGIS kMT": "{:,.0f}", "Ours − FGIS": "{:+,.0f}", "Ours / FGIS": "{:.0%}"}, na_rep="—"),
        use_container_width=True, hide_index=True)
    with st.expander("By FGIS port region"):
        st.dataframe(r.drop(columns=["ours_vessels"]).round(1), use_container_width=True, hide_index=True)
    st.caption("Expect gaps: FGIS counts inspections (before/during loading, by certificate date) while we count "
               "sailings; our totals exclude vessels with no reported tonnage; Soybean Meal, Distillers Grains, Rice, "
               "Mixed Cargo and Other have no FGIS counterpart here (FGIS rows are corn, soybeans, wheat, sorghum).")



# ── Forecast ─────────────────────────────────────────────────────────────────

@st.fragment
def view_forecast(events, lineup, ui, mtime):
    if events.empty:
        st.info("No sail events banked yet.")
        return
    fcfg = fcst.load_forecast_config()
    c0, c1, c2, c3 = st.columns([1.1, 1.3, 1.1, 1.6])
    modes = {"Elapsed-weighted": "elapsed", "Fixed weight": "fixed"}
    default_mode = "Elapsed-weighted" if fcfg.get("weight_mode", "elapsed") == "elapsed" else "Fixed weight"
    mode_label = c0.radio("Blend weighting", list(modes), index=list(modes).index(default_mode), key="rf_mode")
    mode = modes[mode_label]
    power = float(fcfg["weight_decay_power"])
    w = c1.slider("Seasonality weight at start of month (w0)" if mode == "elapsed" else "Seasonality weight (fixed)",
                  0.0, 1.0, float(fcfg["blend_weight_seasonality"]), 0.05, key=f"rf_w_{mode}",
                  help=(f"Elapsed mode: w = w0 × (1 − elapsed/days)^{power:g}, so Pace takes over as the month fills in. "
                        "0 = Pace only. Defaults come from fact_model/config/forecast.csv." if mode == "elapsed" else
                        "Constant weight on Seasonality all month. 0 = Pace only, 1 = Seasonality only."))
    srcs = sorted(events["source"].unique())
    src_sel = c2.multiselect("Source", srcs, default=srcs, key="rf_src")
    fc = _forecast(round(w, 4), mode, events, lineup, mtime=mtime)
    comms = sorted(fc["commodity"].unique())
    default_c = [c for c in ("Corn", "Soybeans", "Soybean Meal", "Wheat") if c in comms]
    com_sel = c3.multiselect("Commodities", comms, default=default_c, key="rf_com")
    f = fc[fc["source"].isin(src_sel) & fc["commodity"].isin(com_sel)]
    if f.empty:
        st.info("Nothing in the current filter.")
        return

    meta = f.groupby("source").agg(month=("forecast_month", "first"), through=("data_through", "first"),
                                   elapsed=("days_elapsed", "first"), remaining=("days_remaining", "first"),
                                   dim=("days_in_month", "first"))
    st.caption("Month-end projection for the month holding each source's data-through date, per port, summed to region. "
               + " · ".join(f"{s}: {r.month:%b %Y}, data through {r.through:%b %d} ({int(r.elapsed)} of {int(r.dim)} days elapsed)"
                            for s, r in meta.iterrows()))
    eff = f.groupby("source")["weight_seasonal"].max()
    st.caption("Seasonality weight in use right now: " + " · ".join(f"{s} {eff[s]:.0%}" for s in eff.index)
               + (f" (w0 {w:.0%} falling to 0% by month-end)" if mode == "elapsed" else " (fixed)"))
    early = meta[meta["elapsed"] < float(fcfg["min_elapsed_days"])]
    if len(early):
        st.warning("Early in the month for " + ", ".join(early.index) + ": Pace extrapolates only a few days and is noisy, "
                   "so the blend leans on Seasonality until more of the month has shipped.", icon="⚠️")

    reg = fcst.rollup_to_region(f)
    ui["sec"]("🎯  Region projection (sum of port projections)")
    cc, ct = st.columns([1.5, 1])
    with cc:
        pick = st.selectbox("Chart commodity", sorted(reg["commodity"].unique()), key="rf_chart_com")
        r = reg[reg["commodity"] == pick].copy()
        r["label"] = r["source"] + " · " + r["region"]
        fig = go.Figure()
        fig.add_bar(x=r["label"], y=r["mtd_kmt"], name="Shipped MTD", marker_color=ui["pos"])
        fig.add_scatter(x=r["label"], y=r["pace_kmt"], mode="markers", name="Pace",
                        marker=dict(symbol="diamond", size=10, color=ui["blue"]))
        fig.add_scatter(x=r["label"], y=r["seasonal_kmt"], mode="markers", name="Seasonality",
                        marker=dict(symbol="square", size=9, color=ui["purp"]))
        fig.add_scatter(x=r["label"], y=r["blend_kmt"], mode="markers", name="Blend",
                        error_y=dict(type="data", symmetric=False, array=(r["high_kmt"] - r["blend_kmt"]).fillna(0),
                                     arrayminus=(r["blend_kmt"] - r["low_kmt"]).fillna(0), color=ui["amb"]),
                        marker=dict(size=12, color=ui["amb"]))
        fig.update_layout(**ui["layout"], title=f"{pick}: month-end projection with low-high range (kMT)")
        st.plotly_chart(fig, use_container_width=True)
    with ct:
        show = reg[["source", "region", "commodity", "mtd_kmt", "pace_kmt", "seasonal_kmt", "blend_kmt", "low_kmt",
                    "high_kmt", "ports"]].copy()
        st.dataframe(_sort_regions(show).round(0), use_container_width=True, hide_index=True)
    st.caption("Region low/high are sums of port ranges, so they are conservative (they assume ports move together). "
               "Seasonality is summed over ports that have a prior year.")

    ui["sec"]("📋  Port × commodity projection (exportable)")
    tbl = fcst.export_table(f)
    st.dataframe(tbl.drop(columns=["data_through"]), use_container_width=True, hide_index=True)
    st.download_button("⬇️ Download port × commodity projection (CSV)", tbl.to_csv(index=False).encode("utf-8"),
                       file_name=f"port_commodity_projection_{meta['month'].min():%Y_%m}.csv", mime="text/csv")
    with st.expander("Inputs behind each row"):
        st.dataframe(f[["source", "region", "port", "commodity", "days_elapsed", "days_remaining", "mtd_kmt", "pace_kmt",
                        "seasonal_base_kmt", "seasonal_scale", "seasonal_years_used", "seasonal_kmt", "weight_seasonal",
                        "range_basis", "range_n", "vessels_mtd", "tonnage_coverage", "quality"]].round(2),
                     use_container_width=True, hide_index=True)

    ui["sec"]("🧪  Backtest — which blend weight would have worked")
    day = st.select_slider("Project at day-of-month", options=[2, 5, 10, 15, 20, 25], value=10, key="rf_bt_day")
    bt = _backtest(day, events, lineup, mtime=mtime)
    bt = bt[bt["commodity"].isin(com_sel) & bt["source"].isin(src_sel)]
    if bt.empty:
        st.info("Not enough history to backtest the current selection.")
    else:
        piv = bt.pivot_table(index=["source", "commodity"], columns="spec", values="median_abs_pct_error", observed=True)
        piv.columns = [str(c) for c in piv.columns]
        months = bt.groupby(["source", "commodity"])["months"].first().rename("months")
        st.dataframe((pd.concat([months, piv * 100], axis=1)).round(0), use_container_width=True)
        st.caption("Median absolute % error of region × commodity month-end totals, projecting each past month at the same "
                   "day-of-month with only the information available then. 'fixed 0' is Pace only, 'fixed 1' Seasonality; "
                   "'elapsed' is the configured schedule (w0^power).")

    with st.expander("How this is calculated, and extension points"):
        st.markdown(
            "- **Pace** = shipped so far ÷ elapsed days × days in month. Elapsed days run to each source's data-through "
            "date (US: last sail date; Brazil: latest APS report), not to today.\n"
            "- **Seasonality** = mean full-month total of the same calendar month in up to 3 prior marketing years, scaled by "
            "how this marketing year is running against those years (clamped 0.25-4x; unscaled in a marketing year's first month).\n"
            "- **Blend** = w × Seasonality + (1 - w) × Pace, never below what already shipped; Pace only where no prior year exists. "
            "In elapsed mode w = w0 × (1 − elapsed/days)^power, falling to 0 by month-end; w0 and the power were chosen from the backtest.\n"
            "- **Range** = 10th-90th percentile of backtested actual ÷ projected at the same day-of-month (port, else pooled by commodity).\n"
            "- Tonnage is only what the reports disclose, so US Gulf and Texas projections understate (see Tonnage reported on the Execution tab)."
        )
        st.markdown("**Extension points (not wired in):**")
        st.markdown("1. *Line-up-fed adjustment* — stub. Needs enough banked snapshots to learn a queue-to-sailed relationship:")
        if not lineup.empty:
            st.dataframe(adj.lineup_model_readiness(lineup), use_container_width=True, hide_index=True)
        st.markdown("2. *Elevator market share* — hook only (`disaggregate_to_elevator`); no share dataset loaded, never alters port math.\n\n"
                    "3. *Freight spreads* — interface only. It asserts causation, so it must first ship as a backtest of historical "
                    "spread vs volume shifts before it may adjust any forward number.")

# ── Entry point ──────────────────────────────────────────────────────────────

def page_regions(ui, facts_db=None):
    facts_db = facts_db or store.DB_PATH
    if not os.path.exists(facts_db):
        st.info("facts.db has not been built yet. Run `python build_facts.py` in the app folder.")
        return
    mtime = os.path.getmtime(facts_db)
    lineup, events = _load(facts_db, mtime=mtime)
    if lineup.empty and events.empty:
        st.info("facts.db is empty. Run `python build_facts.py`.")
        return
    t1, t2, t3, t4 = st.tabs(["Line-up", "Trend", "Execution", "Forecast"])
    with t1:
        view_lineup(lineup, ui)
    with t2:
        view_trend(lineup, ui)
    with t3:
        view_execution(events, ui)
    with t4:
        view_forecast(events, lineup, ui, mtime)
