"""포트폴리오 시뮬레이션 (자동매매 규칙: 1회 10만원, 최대 10종목, 당일 재매수 없음)."""
from __future__ import annotations

import pickle
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from bt import R_FORCE, R_OPEN, R_SL, R_SL_AMB, R_TP, R_TP_AMB


@dataclass
class Cost:
    slip_b: float = 0.003  # 매수 시장가 체결 미끄러짐 (신호가 대비)
    slip_s: float = 0.002  # 매도 시장가 체결 미끄러짐
    fee: float = 0.00015  # 매수·매도 각각
    tax: float = 0.0020  # 매도 증권거래세


def load(path) -> dict:
    with open(path, "rb") as f:
        o = pickle.load(f)
    e = o["events"]
    e["hhmm"] = (e["est_min"] // 60 * 100 + e["est_min"] % 60).astype(int)
    e["tkey"] = e["date"].astype(np.int64) * 10000 + e["hhmm"]
    o["events"] = e
    return o


def net_return(entry, exit_price, cost: Cost):
    buy = entry * (1 + cost.slip_b)
    sell = exit_price * (1 - cost.slip_s)
    return (sell * (1 - cost.fee - cost.tax) - buy * (1 + cost.fee)) / buy


def event_returns(o: dict, exits, cost: Cost = Cost()) -> np.ndarray:
    return net_return(o["events"]["entry"].to_numpy(), exits[0], cost)


def pick(o: dict, *idx, trail: bool = False):
    """격자에서 한 조합의 (청산가, 청산시각키, 사유) 1차원 배열."""
    p, k, r = ("tprice", "tkey", "treason") if trail else ("price", "key", "reason")
    sl = (slice(None),) + tuple(idx)
    return o[p][sl].astype(float), o[k][sl], o[r][sl]


def portfolio(o: dict, ti: int = 0, si: int = 0, mi: int = 0, mask: np.ndarray | None = None, cost: Cost = Cost(),
              buy_amount: int = 100_000, max_pos: int = 10, buy_start: int = 540, buy_end: int = 915,
              rebuy: bool = False, keep_trades: bool = False, exits=None) -> dict:
    e = o["events"]
    if exits is None:
        exits = pick(o, ti, si, mi)
    price, key, reason = exits
    order = o.get("_order")
    if order is None or len(order) != len(e):
        order = np.lexsort((e["code"].to_numpy(), e["est_min"].to_numpy(), e["date"].to_numpy()))
        o["_order"] = order
    if mask is not None:
        order = order[mask[order]]
    held: dict[str, tuple] = {}
    traded = defaultdict(set)
    trades = []
    skipped = defaultdict(int)
    codes = e["code"].to_numpy()
    dates = e["date"].to_numpy()
    tkeys = e["tkey"].to_numpy()
    est = e["est_min"].to_numpy()
    entries = e["entry"].to_numpy()

    def realize(code, pos):
        idx, qty, buy_fill, xkey = pos
        sell_fill = float(price[idx]) * (1 - cost.slip_s)
        pnl = qty * (sell_fill - buy_fill) - cost.fee * qty * (buy_fill + sell_fill) - cost.tax * qty * sell_fill
        trades.append((idx, code, int(dates[idx]), int(xkey // 10000), qty, buy_fill, sell_fill, pnl,
                       int(reason[idx]), int(tkeys[idx]), int(xkey)))
        traded[int(xkey // 10000)].add(code)

    for idx in order:
        if mask is not None and not mask[idx]:
            continue
        tk = tkeys[idx]
        for c in [c for c, p in held.items() if p[3] <= tk]:
            realize(c, held.pop(c))
        code = codes[idx]
        if not (buy_start <= est[idx] < buy_end):
            skipped["time"] += 1
            continue
        if code in held:
            skipped["held"] += 1
            continue
        if not rebuy and code in traded[int(dates[idx])]:
            skipped["traded_today"] += 1
            continue
        if len(held) >= max_pos:
            skipped["max_pos"] += 1
            continue
        buy_fill = entries[idx] * (1 + cost.slip_b)
        qty = int(buy_amount // buy_fill)
        if qty <= 0:
            skipped["price"] += 1
            continue
        held[code] = (idx, qty, buy_fill, int(key[idx]))
        traded[int(dates[idx])].add(code)
    for c in list(held):
        realize(c, held.pop(c))
    t = pd.DataFrame(trades, columns=["idx", "code", "buy_date", "sell_date", "qty", "buy", "sell", "pnl", "reason",
                                      "tkey", "xkey"])
    res = summarize(t, buy_amount)
    res["skipped"] = dict(skipped)
    if keep_trades:
        res["trades"] = t
    return res


def summarize(t: pd.DataFrame, buy_amount: int = 100_000) -> dict:
    if t.empty:
        return {"n": 0, "pnl": 0.0}
    ret = t["pnl"] / (t["qty"] * t["buy"])
    daily = t.groupby("sell_date")["pnl"].sum().sort_index()
    eq = daily.cumsum()
    mdd = float((eq - eq.cummax()).min()) if len(eq) else 0.0
    wins = t["pnl"] > 0
    gp, gl = t.loc[wins, "pnl"].sum(), -t.loc[~wins, "pnl"].sum()
    hold_days = (pd.to_datetime(t["sell_date"].astype(str)) - pd.to_datetime(t["buy_date"].astype(str))).dt.days
    r = t["reason"]
    return {
        "n": len(t), "pnl": float(t["pnl"].sum()), "avg_ret": float(ret.mean()), "med_ret": float(ret.median()),
        "win": float(wins.mean()), "pf": float(gp / gl) if gl > 0 else np.inf, "mdd": mdd,
        "tp": int(r.isin([R_TP, R_TP_AMB]).sum()), "sl": int(r.isin([R_SL, R_SL_AMB]).sum()),
        "force": int((r == R_FORCE).sum()), "open": int((r == R_OPEN).sum()),
        "overnight": float((hold_days > 0).mean()), "avg_hold_days": float(hold_days.mean()),
        "days_traded": int(t["buy_date"].nunique()),
    }
