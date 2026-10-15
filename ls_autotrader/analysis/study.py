"""최종 검증용 도구: 이벤트 세트 + 가격 경로 캐시 + 규칙/필터 평가."""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from analyze import prep  # noqa: E402
from engine import DATA  # noqa: E402
from rules import Rule, build_paths, evaluate  # noqa: E402
from sim import Cost, net_return, portfolio  # noqa: E402


def load_set(name: str, ok_col: str):
    """name: events_m1 / events_j"""
    o = prep(str(DATA / f"{name}.pkl"))
    cache = DATA / f"paths_{name}.pkl"
    if cache.exists():
        with open(cache, "rb") as f:
            paths = pickle.load(f)
    else:
        paths = build_paths(o["events"], ok_col=ok_col)
        with open(cache, "wb") as f:
            pickle.dump(paths, f)
    return o, paths


def base_mask(o, j_col: str | None = None) -> np.ndarray:
    e = o["events"]
    m = e["window"].to_numpy() & (e["entry"] <= 99_700).to_numpy()
    if j_col:
        m &= e[j_col].fillna(False).astype(bool).to_numpy()
    return m


def run_rule(o, paths, rule: Rule, mask: np.ndarray, cost: Cost = Cost(), max_pos: int = 10,
             buy_end: int = 915, keep_trades: bool = False) -> dict:
    e = o["events"]
    idx = np.nonzero(mask)[0]
    px, key, rs = evaluate(paths, e["entry"].to_numpy(), rule, idx=idx)
    nr = net_return(e["entry"].to_numpy(), px, cost)
    hA = (e["half"] == "A").to_numpy()
    out = {"rule": rule.label(), "n_ev": int(mask.sum())}
    for h, hm in (("A", hA), ("B", ~hA)):
        mm = mask & hm
        out[f"ev_{h}"] = float(np.nanmean(nr[mm])) if mm.any() else np.nan
        r = portfolio(o, mask=mm, exits=(px, key, rs), cost=cost, max_pos=max_pos, buy_end=buy_end,
                      keep_trades=keep_trades)
        out[f"pnl_{h}"] = r["pnl"]
        out[f"n_{h}"] = r["n"]
        out[f"win_{h}"] = r.get("win", np.nan)
        out[f"mdd_{h}"] = r.get("mdd", np.nan)
        out[f"avgret_{h}"] = r.get("avg_ret", np.nan)
        if keep_trades:
            out[f"trades_{h}"] = r.get("trades")
    out["pnl"] = out["pnl_A"] + out["pnl_B"]
    out["ev"] = np.nanmean([out["ev_A"], out["ev_B"]])
    return out


def table(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    cols = ["rule", "n_ev", "ev_A", "ev_B", "pnl_A", "pnl_B", "pnl", "n_A", "n_B", "win_A", "win_B", "mdd_A",
            "mdd_B"]
    return df[[c for c in cols if c in df]]


def fmt(df: pd.DataFrame) -> str:
    d = df.copy()
    for c in d:
        if c.startswith(("ev_", "win_", "avgret_")):
            d[c] = (d[c] * 100).round(2)
        elif c.startswith(("pnl", "mdd")):
            d[c] = d[c].round(0).astype("Int64")
    return d.to_string(index=False)
