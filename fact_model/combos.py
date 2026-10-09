"""Combo (multi-commodity) boats: split their tonnage across the commodities they carry.

The reports list a combo vessel only as "CORN/SBM" or "CORN/SBM/WHT" with one total tonnage. Rule
(Kolten's): corn counts double, every other listed commodity counts once. So

    2 commodities incl. corn:  corn 2/3, other 1/3
    3 commodities incl. corn:  corn 1/2, each other 1/4
    n commodities incl. corn:  corn 2/(n+1), each other 1/(n+1)

A combo with no corn splits equally. This is an allocation of a known total, not a measurement: the
Execution and Forecast views let you switch it off, and combo vessels are always counted once in vessel
counts (the COMBO line in the summary).
"""

import numpy as np
import pandas as pd

CORN_WEIGHT = 2.0

# Report codes -> commodity group (same groups as dimensions.commodity_group).
TOKEN_GROUP = {
    "CORN": "Corn", "YC": "Corn",
    "SBM": "Soybean Meal", "MEAL": "Soybean Meal",
    "YSB": "Soybeans", "SOY": "Soybeans", "SBS": "Soybeans",
    "WHT": "Wheat", "WHEAT": "Wheat",
    "SORGHUM": "Sorghum", "SORG": "Sorghum",
    "RICE": "Rice",
    "DDGS": "Dist. Grains", "GDDG": "Dist. Grains", "GDDGS": "Dist. Grains", "DDG": "Dist. Grains", "GDDS": "Dist. Grains",
    "CGM": "Dist. Grains", "CGF": "Dist. Grains", "CGFP": "Dist. Grains", "CGP": "Dist. Grains",
}
COMBO_GROUP = "Mixed Cargo"


def combo_shares(raw, corn_weight=CORN_WEIGHT):
    """{commodity group: share} for a combo string like 'CORN/SBM/WHT'; shares sum to 1. Unknown codes
    count as 'Other'. Returns {} for an empty / single-token string (nothing to split)."""
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return {}
    tokens = [t.strip().upper() for t in str(raw).split("/") if t.strip()]
    tokens = list(dict.fromkeys(tokens))                      # a code listed twice counts once
    if len(tokens) < 2:
        return {}
    weights = {}
    for t in tokens:
        g = TOKEN_GROUP.get(t, "Other")
        weights[g] = weights.get(g, 0.0) + (corn_weight if TOKEN_GROUP.get(t) == "Corn" else 1.0)
    total = sum(weights.values())
    return {g: w / total for g, w in weights.items()}


def allocate_combos(df, combo_col="combo", value_cols=("kmt",), count_cols=(), group_col="commodity", corn_weight=CORN_WEIGHT):
    """Replace each combo row (group_col == 'Mixed Cargo' with a splittable combo string) by one row per
    commodity, with value_cols (tonnage) and count_cols (e.g. vessel counts, pro rata) multiplied by the
    share. Totals are conserved exactly. Rows that cannot be split (no combo string) stay as Mixed Cargo.
    Adds `from_combo` = True on the split rows."""
    d = df.copy()
    d["from_combo"] = False
    is_combo = (d[group_col] == COMBO_GROUP) & d[combo_col].notna() & (d[combo_col].astype(str) != "")
    if not is_combo.any():
        return d
    keep = d[~is_combo]
    pieces = []
    for combo, g in d[is_combo].groupby(combo_col):
        sh = combo_shares(combo, corn_weight)
        if not sh:
            pieces.append(g)
            continue
        for com, s in sh.items():
            p = g.copy()
            p[group_col] = com
            for c in list(value_cols) + list(count_cols):
                p[c] = p[c] * s
            p["from_combo"] = True
            pieces.append(p)
    return pd.concat([keep] + pieces, ignore_index=True)


def combo_mix(events, by=("source", "region", "port"), trailing_months=12, corn_weight=CORN_WEIGHT):
    """Typical commodity mix of combo tonnage per `by` (trailing months of sail events, tonnage-weighted,
    allocated by the rule). Used to split a port's projected Mixed Cargo tonnage."""
    e = events[(events["commodity"] == COMBO_GROUP) & events["kmt"].notna() & (events["combo"].astype(str) != "")].copy()
    if e.empty:
        return pd.DataFrame(columns=list(by) + ["commodity", "share"])
    e = e[pd.to_datetime(e["sail_date"]) > pd.to_datetime(e["sail_date"]).max() - pd.DateOffset(months=trailing_months)]
    a = allocate_combos(e, value_cols=("kmt",), corn_weight=corn_weight)
    a = a[a["from_combo"]]
    g = a.groupby(list(by) + ["commodity"], as_index=False)["kmt"].sum()
    g["share"] = g["kmt"] / g.groupby(list(by))["kmt"].transform("sum")
    return g[list(by) + ["commodity", "share"]]
