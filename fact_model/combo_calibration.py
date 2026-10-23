"""Combo (multi-commodity) boats: learn what they actually carry instead of assuming a split.

The line-up reports list a combo vessel only as "CORN/SBM" with one total tonnage. USDA FGIS publishes ship inspections
by VESSEL NAME and grain (corn, soybeans, wheat, sorghum), so for a combo that has sailed we can see how much of each
grain it really loaded; whatever is left of the reported total is the non-grain part (meal, DDGS, ...).

  match_events      combo sail events -> FGIS grain tonnage by vessel name + date window
  observed_shares   keep the matches that add up (grain tonnage ~ the reported total) -> one share per commodity per vessel
  learn_shares      average observed shares per (region, combo type) over recent history, with p10-p90 and n
  fit_weights       a per-commodity weight (share = w_c / sum of w over the commodities present), fitted on the observed
                    vessels; the fallback for a combo type too rare to average (never an equal split by default)
  shares_for        the split to use for one combo: this vessel's ACTUAL split if matched, else the learned average for
                    its type, else the fitted-weight model

Measured on 800+ matched US vessels (2023-26): CORN/SBM averages 69% corn, CORN/YSB 67%, CORN/DDGS 79%, SBM/YSB 78% meal;
individual vessels vary widely (CORN/SBM corn share runs 46-88%), so single-vessel splits are estimates and totals are firmer.
"""

import re

import numpy as np
import pandas as pd

from . import combos as cb

GRAIN = {"CORN": "Corn", "SOYBEANS": "Soybeans", "WHEAT": "Wheat", "SORGHUM": "Sorghum"}
SOCRATA_URL = "https://agtransport.usda.gov/resource/sruw-w49i.json"
MIN_N = 8                         # vessels needed before a combo type's own average is trusted


def vessel_key(name):
    s = re.sub(r"^(M/?V|MV|M\.V\.)\s+", "", str(name).upper().strip())
    return re.sub(r"[^A-Z0-9]", "", s)


def combo_key(combo):
    """Sorted commodity groups of a combo string, e.g. 'SBM/CORN' -> ('Corn', 'Soybean Meal'); () when not splittable."""
    return tuple(sorted(cb.combo_shares(combo)))


def key_label(key):
    return " / ".join(key)


def fetch_fgis_ships(start_year, timeout=120):
    """FGIS ship inspections by vessel: DataFrame[date, vessel_key, grain, kmt, port]."""
    import requests
    rows, off = [], 0
    while True:
        r = requests.get(SOCRATA_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, params={
            "$select": "date,carrier_name,grain,port,mt", "$where": f"type_carrier_text='SHIP' and year>={int(start_year)}",
            "$limit": "50000", "$offset": str(off), "$order": "date"})
        r.raise_for_status()
        j = r.json()
        rows += j
        if len(j) < 50000:
            break
        off += 50000
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["date", "vessel_key", "grain", "kmt", "port"])
    df["date"] = pd.to_datetime(df["date"])
    df["kmt"] = df["mt"].astype(float) / 1000.0
    df["vessel_key"] = df["carrier_name"].map(vessel_key)
    df["commodity"] = df["grain"].map(GRAIN)
    return df[["date", "vessel_key", "grain", "commodity", "kmt", "port"]]


def match_events(events, ships, before=35, after=10):
    """Combo sail events (commodity 'Mixed Cargo', reported kmt) with the grain tonnage FGIS inspected for the same vessel
    in [sail_date - before, sail_date + after] days. -> DataFrame[event_id, region, key, kmt, <grain>..., matched]."""
    c = events[(events["commodity"] == "Mixed Cargo") & events["kmt"].notna() & events["vessel"].notna()].copy()
    c["vk"] = c["vessel"].map(vessel_key)
    c["key"] = c["combo"].map(combo_key)
    c = c[c["key"].map(len) >= 2]
    by_vessel = {k: g for k, g in ships.groupby("vessel_key")}
    out = []
    for r in c.itertuples():
        g = by_vessel.get(r.vk)
        row = {"event_id": r.event_id, "region": r.region, "key": r.key, "kmt": r.kmt, "sail_date": pd.Timestamp(r.sail_date), "matched": False}
        if g is not None:
            lo, hi = pd.Timestamp(r.sail_date) - pd.Timedelta(days=before), pd.Timestamp(r.sail_date) + pd.Timedelta(days=after)
            m = g[(g["date"] >= lo) & (g["date"] <= hi)]
            if r.fgis_port and (m["port"] == r.fgis_port).any():
                m = m[m["port"] == r.fgis_port]
            if len(m):
                row["matched"] = True
                row.update(m.groupby("commodity")["kmt"].sum().to_dict())
        out.append(row)
    return pd.DataFrame(out)


def observed_shares(matched, tol=0.15):
    """Per-vessel observed split, only where the match is trustworthy: for a combo of grains only, FGIS must account for the
    reported total (+-tol); with a non-grain part (meal, DDGS) the grains must be between 5% and 110% of it. A vessel that
    loaded at two ports or visited twice breaks the arithmetic and is dropped. Grain shares are exact; a single non-grain
    commodity gets the remainder; with two or more non-grain commodities only their pooled share is known (left unassigned).
    -> long DataFrame[event_id, region, key, commodity, share, kmt]"""
    rows = []
    for r in matched[matched["matched"]].itertuples(index=False):
        d = r._asdict()
        comps = list(d["key"])
        grains = [c for c in comps if c in GRAIN.values()]
        non = [c for c in comps if c not in GRAIN.values()]
        if not grains:
            continue
        tot = sum(float(d.get(c) or 0.0) for c in grains if not pd.isna(d.get(c)))
        k = d["kmt"]
        ok = abs(tot / k - 1) <= tol if not non else (0.05 * k <= tot <= 1.1 * k)
        if not ok:
            continue
        for c in grains:
            v = d.get(c)
            rows.append({"event_id": d["event_id"], "region": d["region"], "key": d["key"], "commodity": c,
                         "share": (0.0 if pd.isna(v) else float(v)) / k, "kmt": k, "sail_date": d["sail_date"]})
        if len(non) == 1:
            rows.append({"event_id": d["event_id"], "region": d["region"], "key": d["key"], "commodity": non[0],
                         "share": max(0.0, 1 - tot / k), "kmt": k, "sail_date": d["sail_date"]})
    return pd.DataFrame(rows, columns=["event_id", "region", "key", "commodity", "share", "kmt", "sail_date"])


def fit_weights(obs, iters=3000, lr=0.05):
    """Per-commodity weights w_c with share_c = w_c / sum(w over the combo's commodities), fitted by least squares on the
    observed shares. Normalised so that corn = 1. Used for combo types too rare to average."""
    ev = obs.groupby("event_id")
    comms = sorted(obs["commodity"].unique())
    if not comms:
        return {}
    idx = {c: i for i, c in enumerate(comms)}
    groups = [(np.array([idx[c] for c in g["commodity"]]), g["share"].to_numpy()) for _, g in ev if len(g) >= 2]
    theta = np.zeros(len(comms))
    for _ in range(iters):
        grad = np.zeros_like(theta)
        for ii, s in groups:
            w = np.exp(theta[ii])
            p = w / w.sum()
            # d/dtheta_i of sum (p - s)^2 = 2 * sum_j (p_j - s_j) * p_j * (delta_ij - p_i)
            e = (p - s) * p
            grad[ii] += 2 * (e - p * e.sum())
        theta -= lr * grad / max(len(groups), 1) * 50
    w = np.exp(theta - theta[idx["Corn"]]) if "Corn" in idx else np.exp(theta)
    return {c: float(w[idx[c]]) for c in comms}


# Weights measured on 850+ matched US combo vessels (2023-26), corn = 1. They stand in for any commodity the current fit has
# not seen, so an unseen combo type is never split evenly by default.
PRIOR_WEIGHTS = {"Corn": 1.0, "Wheat": 0.63, "Soybean Meal": 0.50, "Soybeans": 0.46, "Sorghum": 0.35, "Rice": 0.33,
                 "Dist. Grains": 0.27, "Other": 0.60}


def _weights_split(comms, weights):
    w = np.array([max(weights.get(c, PRIOR_WEIGHTS.get(c, np.nan)), 0.0) for c in comms], dtype=float)
    if np.isnan(w).any():
        fill = np.nanmedian(w) if (~np.isnan(w)).any() else 1.0
        w = np.where(np.isnan(w), fill, w)
    return dict(zip(comms, w / w.sum()))


def _fill_remainder(shares, comps, weights):
    """`shares` covers some of `comps`; the commodities with no observation split what is left in proportion to their weights."""
    missing = [c for c in comps if c not in shares]
    left = max(0.0, 1.0 - sum(shares.values()))
    out = dict(shares)
    if missing:
        split = _weights_split(missing, weights)
        for c in missing:
            out[c] = left * split[c]
    tot = sum(out.values())
    return {c: v / tot for c, v in out.items()} if tot > 0 else _weights_split(comps, weights)


def complete_shares(obs, weights):
    """Observed per-vessel shares completed so each vessel's shares cover every commodity in its combo and sum to 1 (a combo
    with two or more non-grain commodities only has their pooled share observed). -> same columns as `obs`."""
    rows = []
    for eid, g in obs.groupby("event_id"):
        key = g["key"].iloc[0]
        sh = _fill_remainder(dict(zip(g["commodity"], g["share"])), list(key), weights)
        for c, v in sh.items():
            rows.append({"event_id": eid, "region": g["region"].iloc[0], "key": key, "commodity": c, "share": v,
                         "kmt": g["kmt"].iloc[0], "sail_date": g["sail_date"].iloc[0]})
    return pd.DataFrame(rows, columns=["event_id", "region", "key", "commodity", "share", "kmt", "sail_date"])


def learn_shares(obs, weights, since=None, min_n=MIN_N):
    """Average observed share per (scope, combo type, commodity), over vessels sailed since `since`. scope = a region or 'US'
    (all regions pooled). Only types seen on at least `min_n` vessels are kept; a commodity never observed on its type gets the
    remainder in proportion to its fitted weight. Columns: scope, key, commodity, share, p10, p90, n."""
    o = obs if since is None else obs[obs["sail_date"] >= pd.Timestamp(since)]
    rows = []
    for scope, g in [("US", o)] + [(r, x) for r, x in o.groupby("region")]:
        for key, k in g.groupby("key"):
            n = k["event_id"].nunique()
            if n < min_n:
                continue
            means = k.groupby("commodity")["share"].mean().to_dict()
            full = _fill_remainder(means, list(key), weights)
            for c in key:
                x = k[k["commodity"] == c]["share"]
                rows.append({"scope": scope, "key": key_label(key), "commodity": c, "share": float(full[c]),
                             "p10": float(x.quantile(0.10)) if len(x) else np.nan,
                             "p90": float(x.quantile(0.90)) if len(x) else np.nan, "n": int(n)})
    return pd.DataFrame(rows, columns=["scope", "key", "commodity", "share", "p10", "p90", "n"])


def shares_for(combo, region, table, weights):
    """-> ({commodity: share}, method): 'learned' (average of this combo type in this region, else all US) or 'model' (fitted
    weights). The split of a vessel that has already been matched to FGIS comes from `actual_shares` instead."""
    key = combo_key(combo)
    if len(key) < 2:
        return {}, "none"
    label = key_label(key)
    for scope in (region, "US"):
        t = table[(table["scope"] == scope) & (table["key"] == label)] if len(table) else table
        if len(t):
            return dict(zip(t["commodity"], t["share"])), "learned"
    return _weights_split(list(key), weights), "model"


def allocate(df, table, weights, actual=None, value_cols=("kmt",), count_cols=(), id_col=None, combo_col="combo",
             group_col="commodity", region_col="region"):
    """Replace each combo row (commodity 'Mixed Cargo' with a combo string) by one row per commodity, with value_cols and
    count_cols multiplied by that commodity's share, so totals are conserved. A vessel in `actual` (long DataFrame of
    event_id, commodity, share) uses its own measured split; the rest use shares_for(). Adds `split_method`
    ('actual' / 'learned' / 'model') on split rows; rows that cannot be split stay as Mixed Cargo."""
    d = df.copy()
    d["split_method"] = ""
    is_c = (d[group_col] == cb.COMBO_GROUP) & d[combo_col].notna() & (d[combo_col].astype(str) != "")
    if not is_c.any():
        return d
    c = d[is_c].copy()
    pieces = [d[~is_c]]
    if actual is not None and id_col is not None and len(actual):
        a = c[c[id_col].isin(set(actual["event_id"]))]
        if len(a):
            j = a.merge(actual[["event_id", "commodity", "share"]].rename(columns={"commodity": "_com", "share": "_share"}),
                        left_on=id_col, right_on="event_id", how="inner", suffixes=("", "_a"))
            j["_method"] = "actual"
            pieces.append(_scale(j, value_cols, count_cols, group_col))
            c = c[~c[id_col].isin(set(actual["event_id"]))]
    if len(c):
        recs = []
        for combo, region in c[[combo_col, region_col]].drop_duplicates().itertuples(index=False):
            sh, method = shares_for(combo, region, table, weights)
            recs += [(combo, region, com, s, method) for com, s in sh.items()]
        sh = pd.DataFrame(recs, columns=[combo_col, region_col, "_com", "_share", "_method"])
        j = c.merge(sh, on=[combo_col, region_col], how="left")
        pieces.append(_scale(j[j["_com"].notna()], value_cols, count_cols, group_col))
        pieces.append(j[j["_com"].isna()].drop(columns=["_com", "_share", "_method"]))   # no usable split: stays Mixed Cargo
    return pd.concat(pieces, ignore_index=True)


def _scale(j, value_cols, count_cols, group_col):
    out = j.copy()
    for col in list(value_cols) + list(count_cols):
        out[col] = out[col] * out["_share"]
    out[group_col] = out["_com"]
    out["split_method"] = out["_method"]
    return out.drop(columns=[x for x in ("_com", "_share", "_method", "event_id_a") if x in out.columns])


def evaluate(obs, weights):
    """Mean absolute error of the predicted share per commodity per vessel, for four ways of splitting a combo, on the
    vessels where every commodity's share was observed (learned averages are leave-one-out). -> {strategy: (MAE, n)}"""
    res = {"learned": [], "weights": [], "rule": [], "equal": []}
    code = {"Corn": "CORN", "Soybeans": "YSB", "Wheat": "WHT", "Sorghum": "SORG", "Soybean Meal": "SBM", "Dist. Grains": "DDGS"}
    for eid, g in obs.groupby("event_id"):
        key = g["key"].iloc[0]
        if len(g) != len(key):
            continue
        truth = dict(zip(g["commodity"], g["share"]))
        mae = lambda p: float(np.mean([abs(p.get(c, 0) - truth[c]) for c in truth]))
        others = obs[(obs["event_id"] != eid) & (obs["key"] == key)]
        if others["event_id"].nunique() >= MIN_N:
            m = others.groupby("commodity")["share"].mean()
            res["learned"].append(mae((m / m.sum()).to_dict()))
        res["weights"].append(mae(_weights_split(list(key), weights)))
        res["rule"].append(mae(cb.combo_shares("/".join(code.get(c, "XYZ") for c in key))))
        res["equal"].append(mae({c: 1 / len(key) for c in key}))
    return {k: (float(np.mean(v)) if v else np.nan, len(v)) for k, v in res.items()}


# ── ongoing vetting: does the split we use keep beating the placeholder assumptions? ─────────────────────────────────────

STRATEGIES = ["excluded", "equal", "rule", "learned", "learned+actual"]
STRATEGY_LABEL = {"excluded": "Combos left out", "equal": "Even split", "rule": "Corn/wheat double (old rule)",
                  "learned": "Learned averages (walk-forward)", "learned+actual": "Learned + each boat's measured split"}


def fgis_monthly(ships):
    """FGIS ship inspections by month x FGIS port region x commodity. -> DataFrame[month, fgis_port, commodity, fgis_kmt]"""
    s = ships[ships["commodity"].notna()].copy()
    s["month"] = pd.to_datetime(s["date"]).dt.to_period("M").dt.to_timestamp()
    return s.groupby(["month", "port", "commodity"], as_index=False)["kmt"].sum().rename(columns={"port": "fgis_port", "kmt": "fgis_kmt"})


def backtest_monthly(events, ships, obs, weights, months=12, learn_months=24, min_n=MIN_N):
    """Walk-forward check of the totals. For each of the last `months` completed months and each FGIS port region, build the
    month's grain tonnage (corn, soybeans, wheat, sorghum) from single-commodity sailings plus the combo boats split five ways
    (see STRATEGIES), and compare with what FGIS inspected. The 'learned' strategies only use boats that sailed BEFORE the month,
    so each month is a genuine out-of-sample test. (Fitted weights are fitted once on all data and only matter for rare types.)
    Inspections lead loading, so no strategy matches FGIS exactly; what counts is whether the combo split closes the gap.
    -> long DataFrame[month, fgis_port, commodity, strategy, pred_kmt, fgis_kmt]"""
    e = events[(events["source"] == "US") & events["kmt"].notna() & (events["fgis_port"].fillna("") != "")].copy()
    e["month"] = pd.to_datetime(e["sail_date"]).dt.to_period("M").dt.to_timestamp()
    f = fgis_monthly(ships)
    this_month = pd.Timestamp.now().normalize().replace(day=1)
    test_months = sorted(m for m in e["month"].unique() if m < this_month)[-months:]
    grains = list(GRAIN.values())
    act = complete_shares(obs, weights)
    act_by = {k: dict(zip(g["commodity"], g["share"])) for k, g in act.groupby("event_id")}
    rows = []
    for m in test_months:
        table = learn_shares(obs[obs["sail_date"] < m], weights, since=m - pd.DateOffset(months=learn_months), min_n=min_n)
        em = e[e["month"] == m]
        for port, g in em.groupby("fgis_port"):
            pred = {s: {c: 0.0 for c in grains} for s in STRATEGIES}
            for r in g.itertuples():
                key = combo_key(r.combo) if r.commodity == cb.COMBO_GROUP else ()
                if r.commodity in grains:
                    for s in STRATEGIES:
                        pred[s][r.commodity] += r.kmt
                elif len(key) >= 2:
                    rule = cb.combo_shares(r.combo)
                    learned, _ = shares_for(r.combo, r.region, table, weights)
                    mixed = dict(act_by[r.event_id]) if r.event_id in act_by else learned
                    for c in grains:
                        pred["equal"][c] += r.kmt / len(key) if c in key else 0.0
                        pred["rule"][c] += r.kmt * rule.get(c, 0.0)
                        pred["learned"][c] += r.kmt * learned.get(c, 0.0)
                        pred["learned+actual"][c] += r.kmt * mixed.get(c, 0.0)
            truth = f[(f["month"] == m) & (f["fgis_port"] == port)].set_index("commodity")["fgis_kmt"]
            for s in STRATEGIES:
                for c in grains:
                    rows.append({"month": m, "fgis_port": port, "commodity": c, "strategy": s, "pred_kmt": pred[s][c],
                                 "fgis_kmt": float(truth.get(c, 0.0))})
    return pd.DataFrame(rows, columns=["month", "fgis_port", "commodity", "strategy", "pred_kmt", "fgis_kmt"])


def backtest_summary(bt, port=None):
    """Weighted absolute % error of the monthly grain tonnage per strategy (sum |predicted - FGIS| / sum FGIS), plus the net bias
    (predicted / FGIS - 1). `port` limits it to one FGIS region (e.g. 'MISSISSIPPI R.' for the US Gulf)."""
    b = bt if port is None else bt[bt["fgis_port"] == port]
    out = []
    for s, g in b.groupby("strategy"):
        tot = g["fgis_kmt"].sum()
        out.append({"strategy": s, "wape": float((g["pred_kmt"] - g["fgis_kmt"]).abs().sum() / tot) if tot else np.nan,
                    "bias": float(g["pred_kmt"].sum() / tot - 1) if tot else np.nan})
    return pd.DataFrame(out).set_index("strategy").reindex(STRATEGIES) if out else pd.DataFrame(columns=["wape", "bias"])


def drift(previous, table, min_n=30, threshold=0.10):
    """Combo types whose learned share moved more than `threshold` since the last stored refresh.
    previous: DataFrame[date, scope, key, commodity, share, n] (combo_shares_history) -> list of (key, commodity, before, now)."""
    if previous is None or previous.empty or table.empty:
        return []
    last = previous[previous["date"] == previous["date"].max()]
    before = {(r.key, r.commodity): r.share for r in last.itertuples() if r.scope == "US"}
    out = []
    for r in table[(table["scope"] == "US") & (table["n"] >= min_n)].itertuples():
        b = before.get((r.key, r.commodity))
        if b is not None and abs(r.share - b) > threshold:
            out.append((r.key, r.commodity, float(b), float(r.share)))
    return out


def health(meta, summary, moved=()):
    """Status of the combo math: ('ok' | 'warn' | 'fail', [reasons]). 'fail' = something broke, or the learned split is no
    better than the placeholder (act on it); 'warn' = worth a look (a learned share jumped, or a benchmark did better)."""
    rank = {"ok": 0, "warn": 1, "fail": 2}
    status, reasons = "ok", []

    def flag(level, why):
        nonlocal status
        reasons.append(why)
        if rank[level] > rank[status]:
            status = level

    if meta.get("combo_events", 0) and meta["matched"] / meta["combo_events"] < 0.5:
        flag("fail", f"only {meta['matched'] / meta['combo_events']:.0%} of combo sailings matched to USDA by vessel name (normally ~80%)")
    if meta.get("usable", 0) < 200:
        flag("fail", f"only {meta.get('usable', 0)} vessels usable for learning (normally 800+)")
    if not pd.isna(meta.get("mae_learned", np.nan)) and meta["mae_learned"] > meta["mae_equal"] - 0.02:
        flag("fail", "the learned split is no longer clearly better than an even split per boat")
    if len(summary) and "learned" in summary.index and not pd.isna(summary.loc["learned", "wape"]):
        if summary.loc["learned", "wape"] > summary.loc["excluded", "wape"]:
            flag("fail", "adding the split combo tonnage makes the monthly totals fit USDA worse than leaving combos out")
        elif summary.loc["learned", "wape"] > summary.loc["equal", "wape"]:
            flag("warn", "the learned split fits monthly USDA totals worse than an even split this period")
    for key, com, b, n in moved:
        flag("warn", f"{key}: {com} share moved from {b:.0%} to {n:.0%} since the last refresh")
    return status, reasons


def refresh(facts_db=None, start_year=2023, learn_months=24, ships=None, backtest_months=12):
    """Rebuild the combo calibration from the sail events in facts.db and FGIS ship inspections, vet it, and save to facts.db:
    combo_shares (learned average split per combo type), combo_weights (fitted per-commodity weights), combo_actual (measured
    split of each matched vessel), combo_backtest (monthly walk-forward check against USDA totals), combo_history (one row per
    refresh: accuracy of every strategy + health status) and combo_shares_history (the learned shares per refresh, so drift is
    visible). -> a summary dict (also stored as combo_meta) whose 'status' is ok / warn / fail."""
    from . import store
    events = store.load_events(facts_db)
    events = events[events["source"] == "US"]
    ships = fetch_fgis_ships(start_year) if ships is None else ships
    matched = match_events(events, ships)
    obs = observed_shares(matched)
    weights = fit_weights(obs)
    since = (pd.Timestamp.now().normalize() - pd.DateOffset(months=learn_months)) if learn_months else None
    table = learn_shares(obs, weights, since=since)
    actual = complete_shares(obs, weights)
    ev = evaluate(obs, weights)
    bt = backtest_monthly(events, ships, obs, weights, months=backtest_months)
    summ_all, summ_usg = backtest_summary(bt), backtest_summary(bt, "MISSISSIPPI R.")
    prev = store.load_table("combo_shares_history", facts_db)
    status, reasons = health({"combo_events": int(len(matched)), "matched": int(matched["matched"].sum()), "usable": int(obs["event_id"].nunique()),
                              "mae_learned": ev["learned"][0], "mae_equal": ev["equal"][0]}, summ_all, drift(prev, table))
    today = str(pd.Timestamp.now().date())
    meta = {"refreshed": today, "combo_events": int(len(matched)), "matched": int(matched["matched"].sum()),
            "usable": int(obs["event_id"].nunique()), "types_learned": int(table[table["scope"] == "US"]["key"].nunique()),
            "mae_learned": ev["learned"][0], "mae_weights": ev["weights"][0], "mae_rule": ev["rule"][0], "mae_equal": ev["equal"][0],
            "status": status, "reasons": " | ".join(reasons),
            **{f"wape_{s}": summ_all.loc[s, "wape"] for s in STRATEGIES}, **{f"wape_usg_{s}": summ_usg.loc[s, "wape"] for s in STRATEGIES}}
    store.save_table(table, "combo_shares", facts_db)
    store.save_table(pd.DataFrame({"commodity": list(weights), "weight": list(weights.values())}), "combo_weights", facts_db)
    store.save_table(actual[["event_id", "commodity", "share"]], "combo_actual", facts_db)
    store.save_table(bt, "combo_backtest", facts_db)
    store.save_table(pd.DataFrame([meta]), "combo_meta", facts_db)
    hist = store.load_table("combo_history", facts_db)
    hist = pd.concat([hist[hist["refreshed"].astype(str) != today] if len(hist) else hist, pd.DataFrame([meta])], ignore_index=True)
    store.save_table(hist, "combo_history", facts_db)
    snap = table[table["scope"] == "US"].assign(date=today)[["date", "scope", "key", "commodity", "share", "n"]]
    sh = pd.concat([prev[prev["date"].astype(str) != today] if len(prev) else prev, snap], ignore_index=True)
    store.save_table(sh, "combo_shares_history", facts_db)
    return meta


def load_calibration(facts_db=None):
    """-> (shares table, weights dict, actual DataFrame, meta dict); empty pieces when the tables are not there yet."""
    from . import store
    table = store.load_table("combo_shares", facts_db)
    if table.empty:
        table = pd.DataFrame(columns=["scope", "key", "commodity", "share", "p10", "p90", "n"])
    w = store.load_table("combo_weights", facts_db)
    weights = dict(zip(w["commodity"], w["weight"])) if len(w) else dict(PRIOR_WEIGHTS)
    actual = store.load_table("combo_actual", facts_db)
    m = store.load_table("combo_meta", facts_db)
    return table, weights, actual, (m.iloc[0].to_dict() if len(m) else {})


def load_vetting(facts_db=None):
    """-> (backtest DataFrame, history DataFrame, shares-history DataFrame) for the dashboard's vetting panel."""
    from . import store
    return (store.load_table("combo_backtest", facts_db, ["month"]), store.load_table("combo_history", facts_db),
            store.load_table("combo_shares_history", facts_db, ["date"]))
