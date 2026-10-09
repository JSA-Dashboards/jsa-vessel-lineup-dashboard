import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import numpy as np
from datetime import datetime
import os
import io

# ── JSA Brand Colors ─────────────────────────────────────────────────────────
JSA_GREEN    = "#5e7164"
JSA_GREEN_LT = "#8db89a"

DM_BG       = "#0d1210"
DM_SURFACE  = "#141c18"
DM_SURFACE2 = "#1a2620"
DM_BORDER   = "#253328"
DM_TEXT     = "#e8ede9"
DM_MUTED    = "#7a9485"

COL_POS  = "#8db89a"
COL_NEG  = "#e07070"
COL_AMB  = "#c4b456"
COL_BLUE = "#6fa8c4"
COL_PURP = "#9b89c4"
COL_ORG  = "#c4896a"
COL_TEAL = "#6ac4b8"

JSA_LOGO = "https://www.jpsi.com/wp-content/themes/gate39media/img/logo-white.png"

DATA_FILENAME = "Vessel Lineup - US.xlsx"

# Search order:
#   1. Same folder as app.py  (repo copy — works on Streamlit Cloud + locally)
#   2. Original OneDrive source path  (local fallback if repo copy is stale)
#   3. File uploader widget  (last resort)
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_FILE_PATH = os.path.join(_APP_DIR, DATA_FILENAME)
ONEDRIVE_FILE_PATH = (
    r"C:\Users\KoltenPostin\John Stewart and Associates"
    r"\JSA - Documents\Research Analyst\Misc\Boat Lineup\Vessel Lineup - US.xlsx"
)

# Commodity color map
COMM_COLORS = {
    "Corn":          COL_AMB,
    "Wheat":         "#e8c96a",
    "Soybeans/Meal": COL_POS,
    "Sorghum":       COL_ORG,
    "Dist. Grains":  COL_PURP,
    "Rice":          "#c46a8d",
    "Mixed Cargo":   COL_BLUE,   # combo loads (e.g. CORN/SBM, CORN/WHT/YSB)
    "Other":         "#5a6660",
}

REGION_COLORS = {
    "USG": COL_BLUE,
    "PNW": COL_POS,
    "TXG": COL_AMB,
}

BRZ_PROD_COLORS = {
    "SBS":        COL_POS,
    "MZ":         COL_AMB,
    "SBMP":       JSA_GREEN,
    "HIPRO":      COL_TEAL,
    "RAW SUG":    COL_ORG,
    "DDGS":       COL_PURP,
    "SPC":        COL_BLUE,
    "MILL WHEAT": "#e8c96a",
    "Other":      "#5a6660",
}

_BRZ_DB_PATH = os.path.join(_APP_DIR, "brazil_lineup.db")

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="JSA Vessel Lineup",
    page_icon="🚢",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Hide the Streamlit Community Cloud viewer badge (the profile avatar that links
# to the creator's other apps) for a clean, client-facing footer.
# The Streamlit Cloud badge (creator avatar + logo) is drawn by Cloud's outer page,
# outside this iframe, so CSS here can't reach it; add the rule to the parent
# document instead (same-origin). No-op when run locally.
_BADGE_JS = """<script>(function(){try{var w=window;while(w.parent&&w.parent!==w){try{void w.parent.document;w=w.parent;}catch(e){break;}}var d=w.document;if(d.getElementById('jsa-hide-cloud-badge'))return;var s=d.createElement('style');s.id='jsa-hide-cloud-badge';s.textContent="[class*='_profileContainer_'],[class*='_viewerBadge_']{display:none !important;}";d.head.appendChild(s);}catch(e){}})();</script>"""
try:
    st.html(_BADGE_JS, unsafe_allow_javascript=True)
except TypeError:  # older Streamlit without st.html JS support
    import streamlit.components.v1 as _stc
    _stc.html(_BADGE_JS, height=0)

st.markdown(f"""
<style>
/* ── Base ── */
.stApp {{ background-color: {DM_BG}; color: {DM_TEXT}; }}
.block-container {{ padding: 1.2rem 2rem 2rem 2rem; }}
h1, h2, h3, h4 {{ color: {DM_TEXT}; }}

/* ── Sidebar ── */
[data-testid="stSidebar"] {{
    background-color: {DM_SURFACE};
    border-right: 1px solid {DM_BORDER};
}}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] .stMarkdown {{ color: {DM_MUTED}; }}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {{
    background: {DM_SURFACE};
    border-radius: 8px;
    padding: 4px 6px;
    gap: 4px;
    border: 1px solid {DM_BORDER};
}}
.stTabs [data-baseweb="tab"] {{
    background: transparent;
    color: {DM_MUTED};
    border-radius: 6px;
    font-weight: 500;
}}
.stTabs [aria-selected="true"] {{
    background: {JSA_GREEN} !important;
    color: {DM_TEXT} !important;
}}

/* ── KPI cards ── */
.kpi-wrap {{
    background: {DM_SURFACE};
    border: 1px solid {DM_BORDER};
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 6px;
}}
.kpi-label {{
    font-size: 10px;
    font-weight: 600;
    color: {DM_MUTED};
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 4px;
}}
.kpi-value {{
    font-size: 26px;
    font-weight: 700;
    color: {DM_TEXT};
    line-height: 1.1;
    margin: 0;
}}
.kpi-sub {{
    font-size: 11px;
    color: {DM_MUTED};
    margin-top: 2px;
}}

/* ── Section headers ── */
.sec-hdr {{
    font-size: 11px;
    font-weight: 700;
    color: {DM_MUTED};
    text-transform: uppercase;
    letter-spacing: 0.09em;
    border-bottom: 1px solid {DM_BORDER};
    padding-bottom: 6px;
    margin: 22px 0 14px 0;
}}

/* ── Dataframe ── */
[data-testid="stDataFrame"] {{ border-radius: 6px; overflow: hidden; }}
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def kpi(col, label, value, sub="", accent=JSA_GREEN):
    col.markdown(
        f'<div class="kpi-wrap" style="border-left:3px solid {accent};">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'<div class="kpi-sub">{sub}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


BASE_LAYOUT = dict(
    plot_bgcolor=DM_SURFACE2,
    paper_bgcolor=DM_SURFACE,
    font=dict(color=DM_MUTED, family="Arial"),
    xaxis=dict(gridcolor=DM_BORDER, linecolor=DM_BORDER, tickfont=dict(color=DM_MUTED)),
    yaxis=dict(gridcolor=DM_BORDER, linecolor=DM_BORDER, tickfont=dict(color=DM_MUTED)),
    legend=dict(bgcolor=DM_SURFACE2, bordercolor=DM_BORDER, borderwidth=1,
                font=dict(color=DM_TEXT)),
    margin=dict(l=10, r=10, t=36, b=10),
    title_font=dict(size=12, color=DM_MUTED),
    title_x=0,
)


def sec(label):
    st.markdown(f'<div class="sec-hdr">{label}</div>', unsafe_allow_html=True)


# ── Commodity normalisation ───────────────────────────────────────────────────

def _comm_us(c):
    if pd.isna(c):
        return "Other"
    c = str(c).strip().upper()
    # Combo loads (e.g. CORN/SBM, CORN/WHT/YSB) — vessel carries multiple
    # commodities so we don't attribute it to any single one.
    if "/" in c:
        return "Mixed Cargo"
    if c == "CORN" or c.startswith("CORN "):
        return "Corn"
    if any(x in c for x in ("SBM", "YSB", "SOY", "CANOLA")):
        return "Soybeans/Meal"
    if "WHT" in c or "WHEAT" in c:
        return "Wheat"
    if "SORGHUM" in c:
        return "Sorghum"
    if any(x in c for x in ("DDGS", "CGM", "CGFP", "GDDG")):
        return "Dist. Grains"
    if "RICE" in c:
        return "Rice"
    return "Other"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reader(file_source):
    """Return a pandas-compatible file source (path str or fresh BytesIO)."""
    if isinstance(file_source, bytes):
        return io.BytesIO(file_source)
    return file_source   # str path — pandas opens directly


# ── Data loading & processing ─────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def load_data(file_source, _mtime=None):
    """Load data from a file path (str) or file-like object (uploaded bytes).
    _mtime is passed for local files so the cache auto-busts when the file changes.
    """
    US_COLS = ["ELEVATOR", "VESSEL", "ATA", "STATUS", "MT",
               "COMMODITY", "DESTINATION", "SAIL DATE"]

    frames = {}
    for region in ("USG", "PNW", "TXG"):
        df = pd.read_excel(file_source, sheet_name=region,
                           usecols=list(range(8)), header=0)
        df.columns = US_COLS
        df = df.dropna(how="all")
        df["REGION"] = region

        # MT  — keep RVT flag, convert to numeric kMT
        df["IS_RVT"] = df["MT"].astype(str).str.strip().str.upper() == "RVT"
        df["MT_kMT"] = pd.to_numeric(df["MT"], errors="coerce")

        # Status normalisation
        def _stat(s):
            if pd.isna(s):
                return "Unknown"
            s = str(s).strip().upper()
            if s.startswith("SAIL"):
                return "Sailed"
            if s in ("LOADING", "PART-LDD", "IN PORT"):
                return "Loading/In Port"
            if s == "FILED":
                return "Filed"
            if s.startswith("ETA") or s.startswith("L/R"):
                return "ETA"
            return "Other"

        df["STATUS_NORM"] = df["STATUS"].apply(_stat)
        df["SAILED"] = df["STATUS_NORM"] == "Sailed"
        df["COMM_GRP"] = df["COMMODITY"].apply(_comm_us)

        df["ATA"] = pd.to_datetime(df["ATA"], errors="coerce")
        df["SAIL_DT"] = pd.to_datetime(df["SAIL DATE"], errors="coerce")
        df["SAIL_MONTH"] = df["SAIL_DT"].dt.to_period("M").dt.to_timestamp()

        frames[region] = df

    return frames


@st.cache_data(show_spinner=False)
def load_trends_snapshot(file_source, _mtime=None):
    """
    Parse the Trends sheet for commodity-level MT, weekly change, and monthly change
    for USG, PNW, and TXG.  Returns:
      {region: DataFrame[COMMODITY, MT, Weekly_MT, Monthly_MT]}
      and the latest snapshot date.
    """
    df = pd.read_excel(_reader(file_source), sheet_name="Trends", header=None)

    # Row 0 has dates in cols 1-N then 'Weekly', 'Monthly', etc.
    header_row = df.iloc[0]
    weekly_col = monthly_col = latest_col = None
    latest_date = None
    for ci, val in enumerate(header_row):
        ci = int(ci)
        if isinstance(val, str):
            v = val.strip()
            if v == "Weekly":
                weekly_col = ci
            elif v == "Monthly":
                monthly_col = ci
        elif hasattr(val, "year") and hasattr(val, "month"):  # datetime or Timestamp
            val_ts = pd.Timestamp(val)
            if latest_date is None or val_ts > latest_date:
                latest_date = val_ts
                latest_col = ci

    # Region sections are identified by scanning for region-header rows.
    # A region-header row has col[0] == region name AND col[latest_col] is NaN or a Timestamp.
    # The "Commodity" row follows, then commodity data rows, then a total row where
    # col[0] == region name AND col[latest_col] is numeric.
    TARGETS = {"USG", "PNW", "TXG"}

    def _float(v):
        if pd.isna(v):
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(v)
        except Exception:
            return 0.0

    regions = {}
    i = 0
    while i < len(df):
        cell = str(df.iloc[i, 0]).strip() if pd.notna(df.iloc[i, 0]) else ""
        if cell in TARGETS:
            region = cell
            latest_val = df.iloc[i, latest_col] if latest_col else None
            # Check if this is the header row (latest_col has a date or NaN, not a number)
            if not isinstance(latest_val, (int, float)):
                rows = []
                i += 1  # advance past region header
                # Skip "Commodity" label row
                if i < len(df) and str(df.iloc[i, 0]).strip() == "Commodity":
                    i += 1
                # Read commodity rows until we hit the total row (col[0] == region)
                while i < len(df):
                    label = str(df.iloc[i, 0]).strip() if pd.notna(df.iloc[i, 0]) else ""
                    if label == region:
                        # This is the total row — skip it (we'll sum ourselves)
                        i += 1
                        break
                    if label and label != "nan":
                        rows.append({
                            "COMMODITY": label.upper(),
                            "MT": _float(df.iloc[i, latest_col]) if latest_col else 0.0,
                            "Weekly_MT": _float(df.iloc[i, weekly_col]) if weekly_col else 0.0,
                            "Monthly_MT": _float(df.iloc[i, monthly_col]) if monthly_col else 0.0,
                        })
                    i += 1
                if rows:
                    regions[region] = pd.DataFrame(rows)
                continue
        i += 1

    return regions, latest_date


@st.cache_data(show_spinner=False)
def load_brazil_data(_db_mtime=None):
    """Return (lineup_df, sailed_df, summary_df) or (None, None, None)."""
    try:
        import brazil_db
    except ImportError:
        return None, None, None
    if not os.path.exists(_BRZ_DB_PATH):
        return None, None, None
    try:
        lineup  = brazil_db.get_all_lineup(db_path=_BRZ_DB_PATH)
        sailed  = brazil_db.get_all_sailed(db_path=_BRZ_DB_PATH)
        summary = brazil_db.get_summary(db_path=_BRZ_DB_PATH)
    except Exception:
        return None, None, None
    for df in (lineup, sailed):
        if df is not None and not df.empty:
            df["report_date"] = pd.to_datetime(df["report_date"]).dt.date
            df["prod_grp"] = df["product"].apply(
                lambda p: str(p).strip().upper() if pd.notna(p) and str(p).strip().upper() in BRZ_PROD_COLORS else "Other"
            )
    return lineup, sailed, summary


def _cutoff(months):
    return pd.Timestamp.now().normalize() - pd.DateOffset(months=months)


# ── Shared chart builders ─────────────────────────────────────────────────────

def bar_comm(df, title):
    """Vessel count bar by commodity group."""
    g = (df.groupby("COMM_GRP").size()
           .reset_index(name="Vessels")
           .sort_values("Vessels", ascending=False))
    fig = px.bar(g, x="COMM_GRP", y="Vessels",
                 color="COMM_GRP", color_discrete_map=COMM_COLORS,
                 title=title, labels={"COMM_GRP": "Commodity"})
    fig.update_layout(**BASE_LAYOUT, showlegend=False)
    fig.update_traces(marker_line_width=0)
    return fig


def bar_monthly_stacked(df, title, color_col="COMM_GRP"):
    """Stacked bar: month × commodity or region."""
    cmap = COMM_COLORS if color_col == "COMM_GRP" else REGION_COLORS
    lbl = "Commodity" if color_col == "COMM_GRP" else "Region"
    g = (df.groupby(["SAIL_MONTH", color_col]).size()
           .reset_index(name="Vessels")
           .sort_values("SAIL_MONTH"))
    fig = px.bar(g, x="SAIL_MONTH", y="Vessels",
                 color=color_col, color_discrete_map=cmap,
                 barmode="stack", title=title,
                 labels={"SAIL_MONTH": "Month", color_col: lbl})
    fig.update_layout(**BASE_LAYOUT)
    fig.update_xaxes(tickformat="%b %Y")
    fig.update_traces(marker_line_width=0)
    return fig


def pivot_comm_month(df):
    """Commodity × Month pivot table for shipped vessels."""
    p = (df.groupby(["COMM_GRP", "SAIL_MONTH"]).size()
           .unstack(fill_value=0))
    p.columns = [c.strftime("%b %Y") if hasattr(c, "strftime") else str(c)
                 for c in p.columns]
    p["Total"] = p.sum(axis=1)
    return p.sort_values("Total", ascending=False)


# ── Per-region page (US) ──────────────────────────────────────────────────────

def page_us(df, region_label, n_months):
    lined = df[~df["SAILED"]].copy()
    sailed = df[df["SAILED"] & (df["SAIL_MONTH"] >= _cutoff(n_months))].copy()

    # ── KPI row ──────────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)

    lu_mt = lined["MT_kMT"].sum()
    s_mt = sailed["MT_kMT"].sum()

    top_lu = (lined["COMM_GRP"].value_counts().index[0]
              if len(lined) > 0 else "—")
    top_lu_n = (lined["COMM_GRP"].value_counts().iloc[0]
                if len(lined) > 0 else 0)
    top_s = (sailed["COMM_GRP"].value_counts().index[0]
             if len(sailed) > 0 else "—")
    top_s_n = (sailed["COMM_GRP"].value_counts().iloc[0]
               if len(sailed) > 0 else 0)

    kpi(c1, "Vessels Lined Up", f"{len(lined):,}",
        f"{lu_mt:,.0f} kMT known" if lu_mt > 0 else "MT not disclosed",
        COL_BLUE)
    kpi(c2, "Top Commodity (Lined Up)", top_lu,
        f"{top_lu_n} vessels", COL_AMB)
    kpi(c3, f"Vessels Sailed — last {n_months}mo", f"{len(sailed):,}",
        f"{s_mt:,.0f} kMT", COL_POS)
    kpi(c4, "Top Commodity (Sailed)", top_s,
        f"{top_s_n} vessels", COL_PURP)

    st.markdown("---")

    # ── Current Lineup ────────────────────────────────────────────────────────
    sec("🟡  Current Lineup — Vessels Waiting to Ship")

    if len(lined) == 0:
        st.info("No vessels currently in the lineup.")
    else:
        ch, tb = st.columns([1.5, 1])
        with ch:
            st.plotly_chart(bar_comm(lined, "Vessel Count by Commodity"),
                            use_container_width=True)
        with tb:
            st.markdown("<br>", unsafe_allow_html=True)
            piv = (lined.groupby(["COMM_GRP", "STATUS_NORM"])
                        .size().unstack(fill_value=0))
            piv["Total"] = piv.sum(axis=1)
            st.dataframe(piv.sort_values("Total", ascending=False),
                         use_container_width=True)

        with st.expander("📋 Full Lineup Detail"):
            cols = [c for c in
                    ["ELEVATOR", "VESSEL", "ATA", "STATUS", "COMMODITY",
                     "COMM_GRP", "MT", "DESTINATION"]
                    if c in lined.columns]
            st.dataframe(
                lined[cols].sort_values("ATA", na_position="last"),
                use_container_width=True, height=320,
            )

    st.markdown("---")

    # ── Shipped by Month ──────────────────────────────────────────────────────
    sec("✅  Shipped — Departed Vessels by Month")

    if len(sailed) == 0:
        st.info("No sailed vessel data for this period.")
        return

    st.plotly_chart(
        bar_monthly_stacked(sailed, "Shipped Vessels by Month & Commodity"),
        use_container_width=True,
    )
    st.dataframe(pivot_comm_month(sailed), use_container_width=True)

    # Volume bar
    mv = (sailed.groupby("SAIL_MONTH")["MT_kMT"].sum().reset_index())
    fig_v = px.bar(mv.sort_values("SAIL_MONTH"), x="SAIL_MONTH", y="MT_kMT",
                   title="Shipped Volume by Month (kMT — excludes RVT)",
                   labels={"SAIL_MONTH": "Month", "MT_kMT": "kMT"},
                   color_discrete_sequence=[JSA_GREEN_LT])
    fig_v.update_layout(**BASE_LAYOUT)
    fig_v.update_xaxes(tickformat="%b %Y")
    fig_v.update_traces(marker_line_width=0)
    st.plotly_chart(fig_v, use_container_width=True)


# ── Snapshot table helpers ───────────────────────────────────────────────────

def _fmt_change(val):
    """Format a numeric change with leading + for positives."""
    if pd.isna(val) or val == 0:
        return "0"
    return f"+{val:,.0f}" if val > 0 else f"{val:,.0f}"



def _current_table(lined_up_df, trends_df):
    """
    Build the 'current lineup' table: vessel count from live tabs,
    MT from Trends latest.  Returns a display-ready DataFrame.
    """
    # Vessel counts from live lineup
    vc = (
        lined_up_df.groupby("COMMODITY")
        .size()
        .reset_index(name="Vessels")
    )
    vc["COMMODITY"] = vc["COMMODITY"].str.upper().str.strip()

    # MT from Trends (most recent snapshot)
    if trends_df is not None and not trends_df.empty:
        mt = trends_df[["COMMODITY", "MT"]].copy()
    else:
        # Fall back to live MT
        mt = (
            lined_up_df.groupby("COMMODITY")["MT_kMT"]
            .sum()
            .reset_index()
            .rename(columns={"COMMODITY": "COMMODITY", "MT_kMT": "MT"})
        )
        mt["COMMODITY"] = mt["COMMODITY"].str.upper().str.strip()

    merged = vc.merge(mt, on="COMMODITY", how="left").fillna({"MT": 0})
    merged["MT"] = merged["MT"].round(0).astype(int)

    # Total row
    total = pd.DataFrame(
        [{"COMMODITY": "Grand Total",
          "Vessels": merged["Vessels"].sum(),
          "MT": merged["MT"].sum()}]
    )
    out = pd.concat([merged, total], ignore_index=True)
    return out.set_index("COMMODITY")


def _change_table(trends_df, col):
    """Build a weekly or monthly MT-change table from the Trends data."""
    if trends_df is None or trends_df.empty:
        return pd.DataFrame()
    t = trends_df[["COMMODITY", col]].copy()
    t = t.rename(columns={col: "MT Δ (kMT)"})
    total = pd.DataFrame(
        [{"COMMODITY": "Total", "MT Δ (kMT)": t["MT Δ (kMT)"].sum()}]
    )
    t = pd.concat([t, total], ignore_index=True).set_index("COMMODITY")
    return t


def render_snapshot_section(frames, trends_regions, latest_date):
    """Render the 3×3 commodity snapshot grid on the Summary page."""
    date_str = latest_date.strftime("%m/%d/%Y") if latest_date else "latest"
    sec(f"📋  Commodity Snapshot — Lineup as of {date_str}")

    REGION_LABELS = {
        "USG": "🇺🇸 US Gulf",
        "PNW": "🌲 Pacific Northwest",
        "TXG": "⭐ Texas Gulf",
    }

    for region in ("USG", "PNW", "TXG"):
        lined = frames.get(region)
        trends = trends_regions.get(region) if trends_regions else None
        label = REGION_LABELS[region]

        st.markdown(
            f"<div style='font-size:14px; font-weight:700; color:{DM_TEXT}; "
            f"margin:14px 0 6px 0;'>{label}</div>",
            unsafe_allow_html=True,
        )

        col_cur, col_wk, col_mo = st.columns(3)

        # ── Current Lineup ────────────────────────────────────────────────────
        with col_cur:
            st.markdown(
                f"<div style='font-size:11px; font-weight:600; color:{DM_MUTED}; "
                f"text-transform:uppercase; letter-spacing:.06em; margin-bottom:4px;'>"
                f"Current Lineup</div>",
                unsafe_allow_html=True,
            )
            if lined is not None and len(lined[~lined["SAILED"]]) > 0:
                cur_df = _current_table(lined[~lined["SAILED"]], trends)
                st.dataframe(cur_df, use_container_width=True)
            else:
                st.info("No vessels lined up.")

        # ── Weekly MT Change ──────────────────────────────────────────────────
        with col_wk:
            st.markdown(
                f"<div style='font-size:11px; font-weight:600; color:{DM_MUTED}; "
                f"text-transform:uppercase; letter-spacing:.06em; margin-bottom:4px;'>"
                f"Weekly MT Change</div>",
                unsafe_allow_html=True,
            )
            wk = _change_table(trends, "Weekly_MT")
            if not wk.empty:
                wk_fmt = wk.copy()
                wk_fmt["MT Δ (kMT)"] = wk_fmt["MT Δ (kMT)"].apply(
                    lambda v: _fmt_change(v) if isinstance(v, (int, float)) else v
                )
                st.dataframe(wk_fmt, use_container_width=True)
            else:
                st.info("No Trends data.")

        # ── Monthly MT Change ─────────────────────────────────────────────────
        with col_mo:
            st.markdown(
                f"<div style='font-size:11px; font-weight:600; color:{DM_MUTED}; "
                f"text-transform:uppercase; letter-spacing:.06em; margin-bottom:4px;'>"
                f"Monthly MT Change</div>",
                unsafe_allow_html=True,
            )
            mo = _change_table(trends, "Monthly_MT")
            if not mo.empty:
                mo_fmt = mo.copy()
                mo_fmt["MT Δ (kMT)"] = mo_fmt["MT Δ (kMT)"].apply(
                    lambda v: _fmt_change(v) if isinstance(v, (int, float)) else v
                )
                st.dataframe(mo_fmt, use_container_width=True)
            else:
                st.info("No Trends data.")

    st.markdown("---")


# ── Summary page ──────────────────────────────────────────────────────────────

def page_summary(frames, n_months, trends_regions=None, latest_date=None):
    cut = _cutoff(n_months)

    rows = []
    for r, df in frames.items():
        lu = df[~df["SAILED"]]
        s = df[df["SAILED"] & (df["SAIL_MONTH"] >= cut)]
        rows.append(dict(Region=r,
                         Lined_Up=len(lu), LU_MT=lu["MT_kMT"].sum(),
                         Sailed=len(s), S_MT=s["MT_kMT"].sum()))
    smry = pd.DataFrame(rows)

    # ── KPI row ──────────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    kpi(c1, "Total Vessels Lined Up",
        f"{smry['Lined_Up'].sum():,}",
        "all regions combined", COL_BLUE)
    kpi(c2, "Total Vessels Sailed",
        f"{smry['Sailed'].sum():,}",
        f"last {n_months} months", COL_POS)
    kpi(c3, "Volume Lined Up",
        f"{smry['LU_MT'].sum():,.0f} kMT",
        "known MT (excl. RVT)", COL_AMB)
    kpi(c4, "Volume Sailed",
        f"{smry['S_MT'].sum():,.0f} kMT",
        f"last {n_months} months", COL_PURP)

    st.markdown("---")

    # ── Commodity snapshot tables ─────────────────────────────────────────────
    if trends_regions:
        render_snapshot_section(frames, trends_regions, latest_date)

    # ── Lineup by region ──────────────────────────────────────────────────────
    sec("🟡  Current Lineup — All Regions")

    lu_all = pd.concat(
        [df[~df["SAILED"]] for df in frames.values()], ignore_index=True
    )

    ch1, ch2 = st.columns(2)
    with ch1:
        g = (lu_all.groupby(["REGION", "COMM_GRP"]).size()
                   .reset_index(name="Vessels"))
        fig = px.bar(g, x="REGION", y="Vessels",
                     color="COMM_GRP", color_discrete_map=COMM_COLORS,
                     barmode="stack", title="Lined-Up Vessels by Region & Commodity",
                     category_orders={"REGION": ["USG", "PNW", "TXG"]},
                     labels={"REGION": "Region", "COMM_GRP": "Commodity"})
        fig.update_layout(**BASE_LAYOUT)
        fig.update_traces(marker_line_width=0)
        st.plotly_chart(fig, use_container_width=True)

    with ch2:
        g2 = (lu_all.groupby(["COMM_GRP", "REGION"]).size()
                    .reset_index(name="Vessels"))
        fig2 = px.bar(g2, x="COMM_GRP", y="Vessels",
                      color="REGION", color_discrete_map=REGION_COLORS,
                      barmode="stack", title="Lined-Up Vessels by Commodity & Region",
                      labels={"COMM_GRP": "Commodity", "REGION": "Region"})
        fig2.update_layout(**BASE_LAYOUT)
        fig2.update_traces(marker_line_width=0)
        st.plotly_chart(fig2, use_container_width=True)

    # Summary table
    st.dataframe(
        smry.rename(columns=dict(
            Lined_Up="Lined Up", LU_MT="Lined Up kMT",
            Sailed=f"Sailed ({n_months}mo)", S_MT="Sailed kMT"
        )).set_index("Region"),
        use_container_width=True,
    )

    st.markdown("---")

    # ── Shipped all regions ───────────────────────────────────────────────────
    sec("✅  Shipped — All Regions by Month")

    s_all = pd.concat(
        [df[df["SAILED"] & (df["SAIL_MONTH"] >= cut)] for df in frames.values()],
        ignore_index=True,
    )

    if len(s_all) == 0:
        st.info("No sailed vessel data for the selected period.")
        return

    ch3, ch4 = st.columns(2)
    with ch3:
        st.plotly_chart(
            bar_monthly_stacked(s_all,
                                "Shipped Vessels by Month & Region",
                                color_col="REGION"),
            use_container_width=True,
        )
    with ch4:
        st.plotly_chart(
            bar_monthly_stacked(s_all,
                                "Shipped Vessels by Month & Commodity"),
            use_container_width=True,
        )

    # Region × Month table
    reg_month = (s_all.groupby(["REGION", "SAIL_MONTH"]).size()
                      .unstack(fill_value=0))
    reg_month.columns = [c.strftime("%b %Y") if hasattr(c, "strftime") else str(c)
                         for c in reg_month.columns]
    reg_month["Total"] = reg_month.sum(axis=1)
    st.dataframe(reg_month, use_container_width=True)


# ── Brazil tab ───────────────────────────────────────────────────────────────

@st.fragment
def page_brazil(frames_us=None):
    """Brazil vessel lineup tab (all 7 views)."""
    import tempfile

    brz_mtime = os.path.getmtime(_BRZ_DB_PATH) if os.path.exists(_BRZ_DB_PATH) else None
    lineup_all, sailed_all, summary_df = load_brazil_data(_db_mtime=brz_mtime)

    # ── PDF uploader fallback ─────────────────────────────────────────────────
    if lineup_all is None or lineup_all.empty:
        st.info(
            "No Brazil lineup data loaded yet. "
            "Upload an APS Brazil consolidated line-up PDF to populate the dashboard.",
        )
        pdf_up = st.file_uploader("Upload APS Brazil PDF", type=["pdf"])
        if pdf_up:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_up.read())
                tmp_path = tmp.name
            try:
                from brazil_parser import parse_pdf
                import brazil_db
                brazil_db.init_db(_BRZ_DB_PATH)
                parsed = parse_pdf(tmp_path)
                brazil_db.upsert_report(parsed, _BRZ_DB_PATH)
                st.cache_data.clear()
                st.success(
                    f"Loaded {len(parsed['lineup'])} lineup rows, "
                    f"{len(parsed['sailed'])} sailed rows for {parsed['report_date']}."
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Parse failed: {exc}")
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        return

    import brazil_db
    from datetime import date as _date
    dates = brazil_db.get_report_dates(_BRZ_DB_PATH)  # strings "YYYY-MM-DD"
    latest_date = _date.fromisoformat(dates[0]) if dates else None
    if latest_date is None:
        st.info("No report dates found.")
        return
    # Pre-convert older dates too (for day-over-day)
    date_objs = [_date.fromisoformat(d) for d in dates]

    # ── Working frames ────────────────────────────────────────────────────────
    def _active(df, rd):
        # rd is datetime.date; report_date column is also datetime.date (from load_brazil_data)
        return df[(df["report_date"] == rd) & (~df["excluded"].astype(bool))].copy()

    cur  = _active(lineup_all, latest_date)
    scur = _active(sailed_all, latest_date)
    hist = lineup_all[~lineup_all["excluded"].astype(bool)].copy()

    # ── KPI row ───────────────────────────────────────────────────────────────
    total_mt   = cur["mt"].sum()
    n_vessels  = cur["vessel"].nunique()
    n_ports    = cur["port"].nunique()
    sbs_mt     = cur[cur["product"] == "SBS"]["mt"].sum()
    china_sbs  = cur[
        (cur["product"] == "SBS") &
        cur["destination"].str.upper().str.contains(r"CHINA|CHN", na=False, regex=True)
    ]["mt"].sum()
    china_sbs_pct = 100 * china_sbs / sbs_mt if sbs_mt > 0 else 0
    mtd_sailed = scur["mt"].sum()

    c1, c2, c3, c4 = st.columns(4)
    kpi(c1, "Total Lineup MT",    f"{total_mt/1e6:.2f}M",     f"as of {latest_date}", COL_BLUE)
    kpi(c2, "Vessels / Ports",    f"{n_vessels} / {n_ports}", f"{len(dates)} daily reports", COL_AMB)
    kpi(c3, "China SBS %",        f"{china_sbs_pct:.0f}%",    f"{sbs_mt/1e6:.2f}M MT SBS", COL_POS)
    kpi(c4, "MTD Sailed",         f"{mtd_sailed/1e6:.2f}M",   "month-to-date", DM_MUTED)

    st.markdown("---")

    # ── 7 sub-tabs ────────────────────────────────────────────────────────────
    s1, s2, s3, s4, s5, s6, s7 = st.tabs([
        "📍 By Port",
        "📈 History",
        "⏳ Congestion",
        "🌍 Destinations",
        "↕ Day-Over-Day",
        "🔍 Vessel Lookup",
        "🇺🇸 vs Brazil",
    ])

    # ── S1: MT by port × product ──────────────────────────────────────────────
    with s1:
        sec(f"MT by Port & Product — {latest_date}")
        g = (
            cur.groupby(["port", "prod_grp"])["mt"]
            .sum().reset_index()
            .sort_values("mt", ascending=False)
        )
        g["mt_MMT"] = g["mt"] / 1e6
        fig = px.bar(
            g, x="port", y="mt_MMT", color="prod_grp",
            color_discrete_map=BRZ_PROD_COLORS,
            barmode="stack",
            title="Lineup MT by Port & Product",
            labels={"port": "Port", "mt_MMT": "MT (millions)", "prod_grp": "Product"},
        )
        fig.update_layout(**BASE_LAYOUT)
        fig.update_traces(marker_line_width=0)
        st.plotly_chart(fig, use_container_width=True)

        piv = (
            cur.groupby(["port", "prod_grp"])["mt"]
            .sum().unstack(fill_value=0)
        )
        piv["Total"] = piv.sum(axis=1)
        piv = piv.sort_values("Total", ascending=False)
        fmt_cols = {c: "{:,.0f}" for c in piv.columns}
        st.dataframe(piv.style.format(fmt_cols), use_container_width=True)

    # ── S2: History ───────────────────────────────────────────────────────────
    with s2:
        sec("Lineup History — Daily MT by Product")
        ghist = (
            hist.groupby(["report_date", "prod_grp"])["mt"]
            .sum().reset_index()
        )
        ghist["mt_MMT"] = ghist["mt"] / 1e6
        ghist["report_date"] = pd.to_datetime(ghist["report_date"])
        fig2 = px.bar(
            ghist, x="report_date", y="mt_MMT", color="prod_grp",
            color_discrete_map=BRZ_PROD_COLORS,
            barmode="stack",
            title="Daily Lineup MT by Product",
            labels={"report_date": "Date", "mt_MMT": "MT (millions)", "prod_grp": "Product"},
        )
        fig2.update_layout(**BASE_LAYOUT)
        fig2.update_traces(marker_line_width=0)
        fig2.update_xaxes(tickformat="%b %d")
        st.plotly_chart(fig2, use_container_width=True)

        if sailed_all is not None and not sailed_all.empty:
            sec("MTD Sailed History")
            shist = (
                sailed_all[~sailed_all["excluded"].astype(bool)]
                .groupby(["report_date", "prod_grp"])["mt"]
                .sum().reset_index()
            )
            shist["mt_MMT"] = shist["mt"] / 1e6
            shist["report_date"] = pd.to_datetime(shist["report_date"])
            fig2s = px.bar(
                shist, x="report_date", y="mt_MMT", color="prod_grp",
                color_discrete_map=BRZ_PROD_COLORS,
                barmode="stack",
                title="MTD Sailed MT by Product",
                labels={"report_date": "Date", "mt_MMT": "MT (millions)", "prod_grp": "Product"},
            )
            fig2s.update_layout(**BASE_LAYOUT)
            fig2s.update_traces(marker_line_width=0)
            fig2s.update_xaxes(tickformat="%b %d")
            st.plotly_chart(fig2s, use_container_width=True)

    # ── S3: Congestion ────────────────────────────────────────────────────────
    with s3:
        sec(f"Congestion — Wait Times by Port — {latest_date}")
        wt = cur[cur["wt_days"].notna() & (cur["wt_days"] > 0)].copy()
        if wt.empty:
            st.info("No wait-time data for this report.")
        else:
            gw = (
                wt.groupby("port")["wt_days"]
                .agg(Avg_WT="mean", Max_WT="max", Vessels="count")
                .reset_index()
                .sort_values("Avg_WT", ascending=False)
            )
            fig3 = px.bar(
                gw, x="port", y="Avg_WT", color="Vessels",
                color_continuous_scale=[[0, JSA_GREEN], [1, COL_NEG]],
                title="Average Wait Days by Port",
                labels={"port": "Port", "Avg_WT": "Avg WT (days)", "Vessels": "# Vessels"},
                text=gw["Avg_WT"].round(1),
            )
            fig3.update_layout(**BASE_LAYOUT, coloraxis_showscale=False)
            fig3.update_traces(marker_line_width=0, textposition="outside")
            st.plotly_chart(fig3, use_container_width=True)
            st.dataframe(
                gw.rename(columns={"port": "Port"}).set_index("Port")
                .style.format({"Avg_WT": "{:.1f}", "Max_WT": "{:.0f}"}),
                use_container_width=True,
            )

    # ── S4: Destinations ──────────────────────────────────────────────────────
    with s4:
        sec(f"Destination Mix — {latest_date}")
        gdest = (
            cur.groupby("destination")["mt"]
            .sum().reset_index()
            .sort_values("mt", ascending=False)
            .head(15)
        )
        gdest["mt_MMT"] = gdest["mt"] / 1e6
        gdest["destination"] = gdest["destination"].fillna("Unknown")
        fig4 = px.bar(
            gdest, x="destination", y="mt_MMT",
            title="Top Destinations by MT",
            labels={"destination": "Destination", "mt_MMT": "MT (millions)"},
            color_discrete_sequence=[JSA_GREEN_LT],
        )
        fig4.update_layout(**BASE_LAYOUT)
        fig4.update_traces(marker_line_width=0)
        st.plotly_chart(fig4, use_container_width=True)

        # SBS destination breakdown
        sbs_dest = (
            cur[cur["product"] == "SBS"]
            .groupby("destination")["mt"]
            .sum().reset_index()
            .sort_values("mt", ascending=False)
            .head(10)
        )
        if not sbs_dest.empty:
            sec("SBS Destination Breakdown")
            sbs_dest["mt_MMT"] = sbs_dest["mt"] / 1e6
            sbs_dest["destination"] = sbs_dest["destination"].fillna("Unknown")
            fig4s = px.pie(
                sbs_dest, names="destination", values="mt_MMT",
                title="SBS MT by Destination",
                color_discrete_sequence=px.colors.sequential.Greens_r,
            )
            fig4s.update_layout(**BASE_LAYOUT)
            st.plotly_chart(fig4s, use_container_width=True)

    # ── S5: Day-over-Day ─────────────────────────────────────────────────────
    with s5:
        if len(date_objs) < 2:
            st.info("Need at least 2 report dates for day-over-day comparison.")
        else:
            prev_date = date_objs[1]
            sec(f"Day-Over-Day: {prev_date} → {latest_date}")

            prev  = _active(lineup_all, prev_date)
            cur5  = cur

            # By port × product
            def _port_prod_mt(df):
                return (
                    df.groupby(["port", "prod_grp"])["mt"]
                    .sum().reset_index().rename(columns={"mt": "MT"})
                )

            pp_cur  = _port_prod_mt(cur5)
            pp_prev = _port_prod_mt(prev)
            merged = pp_cur.merge(
                pp_prev, on=["port", "prod_grp"], how="outer",
                suffixes=("_new", "_old"),
            ).fillna(0)
            merged["delta"] = merged["MT_new"] - merged["MT_old"]
            merged["delta_MMT"] = merged["delta"] / 1e6

            fig5 = px.bar(
                merged[merged["delta"] != 0].sort_values("delta", ascending=False),
                x="prod_grp", y="delta_MMT", color="port",
                barmode="group",
                title="MT Change by Product & Port",
                labels={"prod_grp": "Product", "delta_MMT": "MT Change (millions)", "port": "Port"},
            )
            fig5.update_layout(**BASE_LAYOUT)
            fig5.update_traces(marker_line_width=0)
            st.plotly_chart(fig5, use_container_width=True)

            # New / departed vessels
            vessels_new  = set(cur5["vessel"]) - set(prev["vessel"])
            vessels_gone = set(prev["vessel"]) - set(cur5["vessel"])

            c5a, c5b = st.columns(2)
            with c5a:
                sec(f"New Vessels ({len(vessels_new)})")
                if vessels_new:
                    new_df = cur5[cur5["vessel"].isin(vessels_new)][
                        ["vessel", "port", "product", "mt", "status"]
                    ].drop_duplicates("vessel")
                    st.dataframe(new_df, use_container_width=True, hide_index=True)
                else:
                    st.caption("None")
            with c5b:
                sec(f"Departed Vessels ({len(vessels_gone)})")
                if vessels_gone:
                    gone_df = prev[prev["vessel"].isin(vessels_gone)][
                        ["vessel", "port", "product", "mt", "status"]
                    ].drop_duplicates("vessel")
                    st.dataframe(gone_df, use_container_width=True, hide_index=True)
                else:
                    st.caption("None")

    # ── S6: Vessel Lookup ─────────────────────────────────────────────────────
    with s6:
        sec("Vessel Status Path Across Reports")
        query = st.text_input("Search vessel name (partial match, case-insensitive)")
        if query:
            mask = lineup_all["vessel"].str.upper().str.contains(query.upper(), na=False)
            smask = sailed_all["vessel"].str.upper().str.contains(query.upper(), na=False) if sailed_all is not None else pd.Series([], dtype=bool)
            hits = pd.concat([
                lineup_all[mask],
                sailed_all[smask] if sailed_all is not None else pd.DataFrame(),
            ], ignore_index=True)
            if hits.empty:
                st.info("No matching vessels found.")
            else:
                vessels = sorted(hits["vessel"].unique())
                sel = st.selectbox("Select vessel", vessels)
                vdf = hits[hits["vessel"] == sel].sort_values("report_date")[
                    ["report_date", "port", "berth", "status", "product", "mt", "wt_days", "destination"]
                ]
                st.dataframe(vdf, use_container_width=True, hide_index=True)

    # ── S7: Brazil vs US ──────────────────────────────────────────────────────
    with s7:
        sec("Brazil vs US Lineup Comparison")

        brz_total = total_mt
        us_total_kmt = 0.0
        if frames_us:
            for df in frames_us.values():
                lined = df[~df["SAILED"]]
                us_total_kmt += lined["MT_kMT"].sum()
        us_total_mt = us_total_kmt * 1000

        c7a, c7b = st.columns(2)
        kpi(c7a, "Brazil Lineup MT", f"{brz_total/1e6:.2f}M MT",
            f"{latest_date}", COL_AMB)
        kpi(c7b, "US Lineup MT (known)",
            f"{us_total_mt/1e6:.2f}M MT" if us_total_mt > 0 else "N/A",
            "USG + PNW + TXG (excl. RVT)", COL_BLUE)

        cmp = pd.DataFrame([
            {"Region": "Brazil",  "MT_M": brz_total / 1e6},
            {"Region": "US Gulf + PNW + TXG", "MT_M": us_total_mt / 1e6},
        ])
        fig7 = px.bar(
            cmp, x="Region", y="MT_M",
            color="Region",
            color_discrete_sequence=[COL_AMB, COL_BLUE],
            title="Lineup MT Comparison",
            labels={"Region": "", "MT_M": "MT (millions)"},
        )
        fig7.update_layout(**BASE_LAYOUT, showlegend=False)
        fig7.update_traces(marker_line_width=0)
        st.plotly_chart(fig7, use_container_width=True)

        # Brazil product breakdown
        sec("Brazil Lineup by Product")
        gp = cur.groupby("prod_grp")["mt"].sum().reset_index().sort_values("mt", ascending=False)
        gp["mt_MMT"] = gp["mt"] / 1e6
        fig7b = px.bar(
            gp, x="prod_grp", y="mt_MMT",
            color="prod_grp", color_discrete_map=BRZ_PROD_COLORS,
            title="Brazil Lineup by Product",
            labels={"prod_grp": "Product", "mt_MMT": "MT (millions)"},
        )
        fig7b.update_layout(**BASE_LAYOUT, showlegend=False)
        fig7b.update_traces(marker_line_width=0)
        st.plotly_chart(fig7b, use_container_width=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        try:
            st.image(JSA_LOGO, width=150)
        except Exception:
            st.markdown("## JPSI")

        st.markdown("## 🚢 Vessel Lineup")
        st.markdown("---")

        n_months = st.slider(
            "Shipped History Window",
            min_value=3, max_value=24, value=12, step=1,
            help="Number of months of sailed history to display",
        )
        st.markdown("---")

        # ── File source priority ──────────────────────────────────────────────
        # 1. Repo copy (same folder as app.py) — works on Streamlit Cloud + local
        # 2. Original OneDrive source path — local fallback
        # 3. File uploader — last resort
        file_mtime = None

        if os.path.exists(REPO_FILE_PATH):
            file_source = REPO_FILE_PATH
            try:
                file_mtime = os.path.getmtime(REPO_FILE_PATH)
                mod_str = datetime.fromtimestamp(file_mtime).strftime("%b %d · %I:%M %p")
            except Exception:
                mod_str = "Unknown"
            st.caption(f"📂 {DATA_FILENAME}")
            st.caption(f"File saved: {mod_str}")
            local_available = True

        elif os.path.exists(ONEDRIVE_FILE_PATH):
            file_source = ONEDRIVE_FILE_PATH
            try:
                file_mtime = os.path.getmtime(ONEDRIVE_FILE_PATH)
                mod_str = datetime.fromtimestamp(file_mtime).strftime("%b %d · %I:%M %p")
            except Exception:
                mod_str = "Unknown"
            st.caption(f"📂 OneDrive source")
            st.caption(f"File saved: {mod_str}")
            local_available = True

        else:
            local_available = False
            st.markdown("**Upload Data File**")
            uploaded = st.file_uploader(
                DATA_FILENAME,
                type=["xlsx"],
                help="Upload the Vessel Lineup Excel file to load the dashboard",
            )
            if uploaded is None:
                st.warning("Upload the Vessel Lineup Excel file to continue.")
                st.stop()
            file_source = io.BytesIO(uploaded.read())

        st.markdown("---")

        if st.button("🔄 Reload Data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        st.markdown("---")
        st.caption(f"Loaded: {datetime.now().strftime('%b %d · %I:%M %p')}")

    # ── Load ──────────────────────────────────────────────────────────────────
    with st.spinner("Loading vessel data..."):
        frames = load_data(file_source, _mtime=file_mtime)
        trends_regions, latest_date = load_trends_snapshot(file_source, _mtime=file_mtime)

    # ── Stale-data banner (local only) ────────────────────────────────────────
    # If the file on disk is newer than what's in cache, prompt a reload.
    if local_available and file_mtime is not None:
        cached_mtime = st.session_state.get("cached_mtime")
        if cached_mtime is not None and file_mtime > cached_mtime:
            st.warning(
                "⚠️ The source file has been updated since last load. "
                "Click **Reload Data** in the sidebar to refresh.",
                icon="🔄",
            )
        st.session_state["cached_mtime"] = file_mtime

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown(
        "<h1 style='margin-bottom:2px;'>🚢 Vessel Lineup Dashboard</h1>"
        f"<p style='color:{DM_MUTED}; margin-top:0; margin-bottom:18px;'>"
        "US Gulf &nbsp;·&nbsp; Pacific Northwest &nbsp;·&nbsp; Texas Gulf"
        " &nbsp;·&nbsp; Brazil</p>",
        unsafe_allow_html=True,
    )

    tabs = st.tabs(["📊 Summary", "🇺🇸 USG", "🌲 PNW", "⭐ TXG", "🇧🇷 Brazil", "🧭 Regions"])

    with tabs[0]:
        page_summary(frames, n_months, trends_regions=trends_regions, latest_date=latest_date)

    with tabs[1]:
        st.markdown("<h2>US Gulf &nbsp;(USG)</h2>", unsafe_allow_html=True)
        page_us(frames["USG"], "USG", n_months)

    with tabs[2]:
        st.markdown("<h2>Pacific Northwest &nbsp;(PNW)</h2>", unsafe_allow_html=True)
        page_us(frames["PNW"], "PNW", n_months)

    with tabs[3]:
        st.markdown("<h2>Texas Gulf &nbsp;(TXG)</h2>", unsafe_allow_html=True)
        page_us(frames["TXG"], "TXG", n_months)

    with tabs[4]:
        st.markdown("<h2>Brazil</h2>", unsafe_allow_html=True)
        page_brazil(frames_us=frames)

    with tabs[5]:
        st.markdown("<h2>Regions &nbsp;— line-up, trend &amp; execution</h2>", unsafe_allow_html=True)
        import region_views
        region_views.page_regions(dict(
            kpi=kpi, sec=sec, layout=BASE_LAYOUT,
            comm_colors={**COMM_COLORS, "Soybeans": COMM_COLORS["Soybeans/Meal"], "Soybean Meal": JSA_GREEN, "Sugar": COL_ORG},
            blue=COL_BLUE, amb=COL_AMB, pos=COL_POS, neg=COL_NEG, purp=COL_PURP, green_lt=JSA_GREEN_LT,
        ))

    # ── Disclaimer footer ─────────────────────────────────────────────────────
    current_year = datetime.now().year
    st.markdown("---")
    st.markdown(
        f"""<div style="font-size:10px; color:{DM_MUTED}; line-height:1.6; padding:8px 0 16px 0;">
        Trading commodity futures, options on futures, cash commodities, and over-the-counter
        derivative products involves substantial risk of loss and may not be suitable for all
        investors. This communication is provided for informational purposes only and does not
        constitute investment advice, a recommendation, or an offer or solicitation to buy or
        sell any futures, options, cash commodities, or derivative products. John Stewart &amp;
        Associates, Inc. does not accept orders to buy or sell any financial instruments via
        email. The information contained herein has been obtained from sources believed to be
        reliable; however, its accuracy and completeness are not guaranteed. Any opinions
        expressed are solely those of the author, are subject to change without notice, and
        should not be relied upon as a basis for investment decisions. Past performance is not
        indicative of future results. This message may contain confidential or proprietary
        information intended solely for the use of the designated recipient.
        &copy; John Stewart &amp; Associates, Inc. {current_year}
        </div>""",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
