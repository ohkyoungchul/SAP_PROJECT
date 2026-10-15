"""분석 도구 모음 (대화형으로 불러 쓰는 함수들)."""
from __future__ import annotations

import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from sim import Cost, event_returns, load, net_return, pick, portfolio  # noqa: E402

HALF_SPLIT = 20260914  # 전반 A: 8/12~9/11, 후반 B: 9/14~10/13


def prep(path: str) -> dict:
    o = load(path)
    e = o["events"]
    e["half"] = np.where(e["date"] < HALF_SPLIT, "A", "B")
    e["month"] = (e["date"] // 100).astype(int)
    e["tod"] = e["est_min"] - 540  # 09:00 부터 분
    e["window"] = (e["est_min"] >= 540) & (e["est_min"] < 915)
    if "strength_m1" not in e:
        e["strength_m1"] = np.nan
    o["events"] = e
    o["tp"] = list(np.round(o["tp_grid"], 4))
    o["sl"] = list(np.round(o["sl_grid"], 4))
    o["modes"] = list(o["hold_modes"])
    return o


def j_masks(e: pd.DataFrame, proxy_thr: float) -> dict:
    s = e["strength_m1"].fillna(e["strength_proxy"])
    return {"none": np.ones(len(e), bool),
            "eod": e["chdegree_eod"].between(100, 1000).to_numpy(),
            "proxy": (s >= proxy_thr).to_numpy()}


def ix(o, tp, sl, mode):
    return o["tp"].index(round(tp, 4)), o["sl"].index(round(sl, 4)), o["modes"].index(mode)


def run(o, mask, tp=None, sl=None, mode=None, exits=None, cost=Cost(), **kw):
    if exits is None:
        exits = pick(o, *ix(o, tp, sl, mode))
    return portfolio(o, mask=mask, exits=exits, cost=cost, **kw)


def halves(o, mask, **kw) -> dict:
    e = o["events"]
    out = {}
    for h, hm in (("all", np.ones(len(e), bool)), ("A", (e["half"] == "A").to_numpy()),
                  ("B", (e["half"] == "B").to_numpy())):
        out[h] = run(o, mask & hm, **kw)
    return out


def grid(o, mask, modes=("D0_1515", "D0_1530", "D1_1515", "H"), cost=Cost(), **kw) -> pd.DataFrame:
    e = o["events"]
    hA = (e["half"] == "A").to_numpy()
    rows = []
    entry = e["entry"].to_numpy()
    for m in modes:
        mi = o["modes"].index(m)
        for ti, tp in enumerate(o["tp"]):
            for si, sl in enumerate(o["sl"]):
                ex = pick(o, ti, si, mi)
                nr = net_return(entry, ex[0], cost)
                row = {"mode": m, "tp": tp, "sl": sl}
                for h, hm in (("A", hA), ("B", ~hA)):
                    r = portfolio(o, mask=mask & hm, exits=ex, cost=cost, **kw)
                    row[f"pnl_{h}"] = r["pnl"]
                    row[f"n_{h}"] = r["n"]
                    row[f"win_{h}"] = r.get("win", np.nan)
                    row[f"ev_{h}"] = float(np.nanmean(nr[mask & hm & e["window"].to_numpy()]))
                row["pnl"] = row["pnl_A"] + row["pnl_B"]
                rows.append(row)
    return pd.DataFrame(rows)


def buckets(e: pd.DataFrame, ret: np.ndarray, feat: str, mask: np.ndarray, q=5) -> pd.DataFrame:
    x = e.loc[mask, feat]
    r = pd.Series(ret[mask], index=x.index)
    try:
        b = pd.qcut(x, q, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    df = pd.DataFrame({"b": b, "r": r, "half": e.loc[mask, "half"]})
    t = df.groupby(["b", "half"], observed=True)["r"].agg(["mean", "count"]).unstack("half")
    t.columns = [f"{a}_{h}" for a, h in t.columns]
    t["mean_all"] = df.groupby("b", observed=True)["r"].mean()
    t["win_all"] = df.groupby("b", observed=True)["r"].apply(lambda s: (s > 0).mean())
    return t
