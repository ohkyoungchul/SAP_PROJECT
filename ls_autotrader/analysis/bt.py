"""이벤트(조건 편입) → 청산 결과(익절/손절/시간청산 격자) → 포트폴리오(최대 10종목) 시뮬레이션."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from engine import (DATA, StockBars, detect_events, excluded_codes, read_daily, read_m5, tick_ceil, tick_floor,
                    to_stock_bars, universe, upper_limit, BB_SPECS, ENV_SPECS)

TP_GRID = np.array([2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 999.0]) / 100  # 999% = 익절 없음
SL_GRID = np.array([1, 1.5, 2, 2.5, 3, 4, 5, 7, 99.0]) / 100  # 99% = 손절 없음
HOLD_MODES = ["D0_1515", "D0_1530", "D1_1515", "D2_1515", "H"]
R_TP, R_SL, R_FORCE, R_OPEN, R_TP_AMB, R_SL_AMB = 1, 2, 3, 4, 5, 6
BIG = 1 << 30
FIRST_LOW_MODE = "real"  # "real": 분 시가 매수면 실제 저가 / "pessimistic": 항상 그 분 저가


def bar_key(date: np.ndarray, time: np.ndarray) -> np.ndarray:
    return date.astype(np.int64) * 10000 + (time.astype(np.int64) // 100)


@dataclass
class Path:
    key: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    dayn: np.ndarray  # 0 = 매수한 날
    time: np.ndarray  # HHMMSS (봉 끝)


def build_path(sb: StockBars, i: int, entry: float, m1: pd.DataFrame | None = None,
               trig_minute: int | None = None, horizon_days: int = 25) -> Path:
    """매수 직후부터의 가격 경로. 매수한 날은 1분봉(있으면), 이후는 5분봉."""
    d0 = sb.date[i]
    dates = sb.date
    # 이후 날짜들
    j0 = i + 1
    later = np.arange(j0, len(dates))
    if len(later):
        ud = np.unique(dates[later])
        keep_days = ud[ud > d0][:horizon_days]
        later = later[(dates[later] == d0) | np.isin(dates[later], keep_days)]
    if m1 is not None and trig_minute is not None:
        mm = m1[m1["date"] == d0]
        mm = mm[mm["time"] >= trig_minute]
        if len(mm):
            first = mm.iloc[0]
            o = np.r_[entry, mm["open"].to_numpy(float)[1:]]
            h = np.r_[max(entry, first["high"]), mm["high"].to_numpy(float)[1:]]
            # 그 분의 시가에 샀으면 그 분 전체가 매수 후 → 실제 저가. 분 중간에 샀으면 저가가 매수 전일 수 있음
            if FIRST_LOW_MODE == "pessimistic" or float(first["open"]) >= entry:
                low0 = min(entry, float(first["low"]))
            else:
                low0 = min(entry, float(first["close"]))
            l = np.r_[low0, mm["low"].to_numpy(float)[1:]]
            c = mm["close"].to_numpy(float)
            t = mm["time"].to_numpy()
            day_rest = later[dates[later] == d0]
            nxt = later[dates[later] != d0]
            key = np.r_[bar_key(np.full(len(mm), d0), t), bar_key(dates[nxt], sb.time[nxt])]
            dayn_next = np.searchsorted(np.unique(dates[nxt]), dates[nxt]) + 1 if len(nxt) else np.array([], int)
            return Path(key, np.r_[o, sb.o[nxt]], np.r_[h, sb.h[nxt]], np.r_[l, sb.l[nxt]], np.r_[c, sb.c[nxt]],
                        np.r_[np.zeros(len(mm), int), dayn_next], np.r_[t, sb.time[nxt]])
    # 5분봉만: 매수 봉의 남은 부분을 합성 봉으로
    o = np.r_[entry, sb.o[later]]
    h = np.r_[max(entry, sb.h[i]), sb.h[later]]
    l = np.r_[min(entry, sb.c[i]), sb.l[later]]
    c = np.r_[sb.c[i], sb.c[later]]
    dd = np.r_[d0, dates[later]]
    tt = np.r_[sb.time[i], sb.time[later]]
    ud = np.unique(dd)
    return Path(bar_key(dd, tt), o, h, l, c, np.searchsorted(ud, dd), tt)


def exits_grid(p: Path, entry: float, tp_grid=TP_GRID, sl_grid=SL_GRID, hold_modes=HOLD_MODES):
    """격자 전체의 (청산가, 청산시각키, 사유). 모양 (TP, SL, 모드)."""
    n = len(p.o)
    tp = entry * (1 + tp_grid)
    sl = entry * (1 - sl_grid)
    hit_tp = p.h[None, :] >= tp[:, None]
    hit_sl = p.l[None, :] <= sl[:, None]
    ftp = np.where(hit_tp.any(1), hit_tp.argmax(1), BIG)
    fsl = np.where(hit_sl.any(1), hit_sl.argmax(1), BIG)
    force = []
    for m in hold_modes:
        if m == "H":
            force.append(BIG)
            continue
        dn = int(m[1])
        hhmm = int(m.split("_")[1]) * 100
        cand = np.nonzero((p.dayn == dn) & (p.time >= hhmm))[0]
        if len(cand) == 0:  # 해당 날 데이터 없음(기간 끝) → 그 이전 마지막 봉
            cand2 = np.nonzero(p.dayn <= dn)[0]
            force.append(cand2[-1] if len(cand2) and p.dayn.max() > dn else BIG)
        else:
            force.append(cand[0])
    force = np.array(force)
    T, S, M = len(tp_grid), len(sl_grid), len(hold_modes)
    jt = ftp[:, None, None]
    js = fsl[None, :, None]
    jf = force[None, None, :]
    jx = np.minimum(np.minimum(jt, js), jf)
    price = np.empty((T, S, M))
    key = np.empty((T, S, M), dtype=np.int64)
    reason = np.empty((T, S, M), dtype=np.int8)
    tpb = np.broadcast_to(tp[:, None, None], (T, S, M))
    slb = np.broadcast_to(sl[None, :, None], (T, S, M))
    jtb = np.broadcast_to(jt, (T, S, M))
    jsb = np.broadcast_to(js, (T, S, M))
    jfb = np.broadcast_to(jf, (T, S, M))
    tpsl_first = np.minimum(jtb, jsb) <= jfb
    none = jx >= BIG
    jj = np.where(none, n - 1, np.minimum(jx, n - 1))
    O, C = p.o[jj], p.c[jj]
    # 기본: 강제청산 (그 봉 종가)
    price[:] = C
    reason[:] = R_FORCE
    # 익절/손절
    both = tpsl_first & (jtb == jsb) & ~none
    only_tp = tpsl_first & (jtb < jsb) & ~none
    only_sl = tpsl_first & (jsb < jtb) & ~none
    price = np.where(only_tp, np.where(O >= tpb, O, tpb), price)
    reason = np.where(only_tp, R_TP, reason)
    price = np.where(only_sl, np.where(O <= slb, O, slb), price)
    reason = np.where(only_sl, R_SL, reason)
    # 같은 봉에서 둘 다: 시가로 판정, 아니면 양봉=저가 먼저(손절), 음봉=고가 먼저(익절)
    gap_tp = both & (O >= tpb)
    gap_sl = both & (O <= slb)
    amb = both & ~gap_tp & ~gap_sl
    bull = C >= O
    price = np.where(gap_tp, O, price)
    reason = np.where(gap_tp, R_TP, reason)
    price = np.where(gap_sl, O, price)
    reason = np.where(gap_sl, R_SL, reason)
    price = np.where(amb & bull, slb, price)
    reason = np.where(amb & bull, R_SL_AMB, reason)
    price = np.where(amb & ~bull, tpb, price)
    reason = np.where(amb & ~bull, R_TP_AMB, reason)
    # 끝까지 미청산: 마지막 종가로 평가
    price = np.where(none, p.c[-1], price)
    reason = np.where(none, R_OPEN, reason)
    key[:] = p.key[jj]
    return price, key, reason


TR_ACT = np.array([1, 2, 3, 5]) / 100  # 추적손절 시작 수익률
TR_TRAIL = np.array([1, 1.5, 2, 3]) / 100  # 고점 대비 하락폭
TR_SL = np.array([1.5, 2, 3]) / 100  # 고정 손절
TR_MODES = ["D0_1515", "H"]


def force_index(p: Path, mode: str) -> int:
    if mode == "H":
        return BIG
    dn = int(mode[1])
    hhmm = int(mode.split("_")[1]) * 100
    cand = np.nonzero((p.dayn == dn) & (p.time >= hhmm))[0]
    if len(cand):
        return int(cand[0])
    cand2 = np.nonzero(p.dayn <= dn)[0]
    return int(cand2[-1]) if len(cand2) and p.dayn.max() > dn else BIG


def exits_trail(p: Path, entry: float, acts=TR_ACT, trails=TR_TRAIL, sls=TR_SL, modes=TR_MODES):
    """추적손절 격자: 고점(직전 봉까지)이 entry*(1+act) 이상이면 고점*(1-trail) 아래로 내려올 때 매도."""
    n = len(p.o)
    peaks = np.maximum.accumulate(np.r_[entry, p.h[:-1]])
    A, B, S = len(acts), len(trails), len(sls)
    active = peaks[None, :] >= entry * (1 + acts)[:, None]  # (A, n)
    lvl_tr = np.where(active[:, None, :], peaks[None, None, :] * (1 - trails)[None, :, None], -np.inf)  # (A,B,n)
    lvl = np.maximum(lvl_tr[:, :, None, :], (entry * (1 - sls))[None, None, :, None])  # (A,B,S,n)
    hit = p.l[None, None, None, :] <= lvl
    first = np.where(hit.any(-1), hit.argmax(-1), BIG)  # (A,B,S)
    M = len(modes)
    price = np.empty((A, B, S, M))
    key = np.empty((A, B, S, M), dtype=np.int64)
    reason = np.empty((A, B, S, M), dtype=np.int8)
    for m, mode in enumerate(modes):
        fi = force_index(p, mode)
        j = np.minimum(first, fi)
        none = j >= BIG
        jj = np.where(none, n - 1, np.minimum(j, n - 1))
        by_stop = (first <= fi) & ~none
        lv = np.take_along_axis(lvl, jj[..., None], axis=-1)[..., 0]
        o = p.o[jj]
        stop_px = np.where(o <= lv, o, lv)
        price[..., m] = np.where(by_stop, stop_px, np.where(none, p.c[-1], p.c[jj]))
        reason[..., m] = np.where(by_stop, np.where(stop_px >= entry, R_TP, R_SL),
                                  np.where(none, R_OPEN, R_FORCE))
        key[..., m] = p.key[jj]
    return price, key, reason


# ---------------------------------------------------------------- 특성
def daily_features(d: pd.DataFrame, d0: int) -> dict:
    prev = d[d["date"] < d0]
    cur = d[d["date"] == d0]
    out = {"prev_close": np.nan}
    if len(prev) == 0:
        return out
    pc = prev["close"].to_numpy(float)
    out["prev_close"] = pc[-1]
    out["ma5"] = pc[-5:].mean() if len(pc) >= 5 else np.nan
    out["ma20"] = pc[-20:].mean() if len(pc) >= 20 else np.nan
    out["ma60"] = pc[-60:].mean() if len(pc) >= 60 else np.nan
    out["ret1d"] = pc[-1] / pc[-2] - 1 if len(pc) >= 2 else np.nan
    out["ret5d"] = pc[-1] / pc[-6] - 1 if len(pc) >= 6 else np.nan
    out["ret20d"] = pc[-1] / pc[-21] - 1 if len(pc) >= 21 else np.nan
    pv = prev["volume"].to_numpy(float)
    out["avgvol20"] = pv[-20:].mean() if len(pv) >= 5 else np.nan
    out["prev_vol"] = pv[-1]
    out["prev_val"] = float(prev["value"].iloc[-1])
    pval = prev["value"].to_numpy(float)
    out["avgval20"] = pval[-20:].mean() if len(pval) >= 5 else np.nan
    out["mcap"] = float(prev["marketcap"].iloc[-1])
    out["hi60"] = prev["high"].to_numpy(float)[-60:].max()
    if len(cur):
        out["chdegree_eod"] = float(cur["chdegree"].iloc[0])
        out["day_close"] = float(cur["close"].iloc[0])
        out["day_high"] = float(cur["high"].iloc[0])
    return out


def strength_proxy(sb: StockBars) -> np.ndarray:
    """봉 단위 틱룰 체결강도 근사 (당일 누적, 해당 봉 직전까지)."""
    dc = np.r_[0.0, np.diff(sb.c)]
    b = np.where(dc > 0, 1.0, np.where(dc < 0, 0.0, 0.5)) * sb.v
    s = sb.v - b
    newday = np.r_[True, sb.date[1:] != sb.date[:-1]]
    g = np.cumsum(newday)
    cb = pd.Series(b).groupby(g).cumsum().to_numpy() - b
    cs = pd.Series(s).groupby(g).cumsum().to_numpy() - s
    return 100 * cb / np.maximum(cs, 1)
