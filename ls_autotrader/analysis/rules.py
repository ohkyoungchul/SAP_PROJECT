"""경로(매수 후 가격 흐름)를 저장해 두고, 여러 청산 규칙을 조합해 빠르게 평가한다.

규칙 항목 (None 이면 사용 안 함)
  tp        익절 수익률              sl        손절 수익률(양수)
  be        본전 손절 시작 수익률 (고점이 이만큼 오르면 손절선을 매수가로)
  tr_act / tr  추적손절 시작 수익률 / 고점 대비 하락폭
  tstop     (분, 최소수익률): 매수 후 그 분이 지났는데 수익률이 최소수익률 미만이면 그 시점 종가에 매도
  eod       "D0_1515" (당일 15:15 종가 청산) / "D0_1520" / "D1_1515" / "H"(보유)
"""
from __future__ import annotations

import os
import pickle
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from bt import BIG, R_FORCE, R_OPEN, R_SL, R_TP, Path, build_path, force_index  # noqa: E402
from engine import DATA, read_m5, to_stock_bars  # noqa: E402
from refine_m1 import read_m1  # noqa: E402


@dataclass(frozen=True)
class Rule:
    tp: float | None = None
    sl: float | None = None
    be: float | None = None
    tr_act: float | None = None
    tr: float | None = None
    tstop: tuple | None = None
    eod: str = "D0_1515"

    def label(self) -> str:
        parts = []
        if self.tp is not None:
            parts.append(f"TP{self.tp * 100:g}")
        if self.sl is not None:
            parts.append(f"SL{self.sl * 100:g}")
        if self.be is not None:
            parts.append(f"BE{self.be * 100:g}")
        if self.tr is not None:
            parts.append(f"TR{self.tr_act * 100:g}/{self.tr * 100:g}")
        if self.tstop is not None:
            parts.append(f"T{self.tstop[0]}m<{self.tstop[1] * 100:g}")
        parts.append(self.eod)
        return " ".join(parts)


def minutes_since(p: Path, k0: int = 0) -> np.ndarray:
    hh = (p.key % 10000) // 100
    mm = p.key % 100
    day = p.key // 10000
    t = hh * 60 + mm
    # 다른 날은 큰 값 (시간 손절은 매수한 날 안에서만)
    return np.where(day == day[0], t - t[0], 10_000)


def apply_rule(p: Path, entry: float, r: Rule):
    n = len(p.o)
    INF = np.inf
    peaks = np.maximum.accumulate(np.r_[entry, p.h[:-1]])  # 직전 봉까지 고점
    stop = np.full(n, -INF)
    if r.sl is not None:
        stop = np.maximum(stop, entry * (1 - r.sl))
    if r.be is not None:
        stop = np.where(peaks >= entry * (1 + r.be), np.maximum(stop, entry), stop)
    if r.tr is not None:
        stop = np.where(peaks >= entry * (1 + r.tr_act), np.maximum(stop, peaks * (1 - r.tr)), stop)
    hit_s = p.l <= stop
    js = int(np.argmax(hit_s)) if hit_s.any() else BIG
    jt = BIG
    if r.tp is not None:
        hit_t = p.h >= entry * (1 + r.tp)
        jt = int(np.argmax(hit_t)) if hit_t.any() else BIG
    jts = BIG
    if r.tstop is not None:
        ms = minutes_since(p)
        cand = np.nonzero(ms >= r.tstop[0])[0]
        if len(cand) and p.dayn[cand[0]] == 0:
            k = cand[0]
            if p.c[k] / entry - 1 < r.tstop[1]:
                jts = k
    jf = force_index(p, r.eod) if r.eod != "H" else BIG
    j = min(js, jt, jts, jf)
    if j >= BIG:
        return float(p.c[-1]), int(p.key[-1]), R_OPEN
    k = int(p.key[j])
    if j == js and j == jt:  # 같은 봉에서 손절선·익절선 모두 닿음
        tpx = entry * (1 + r.tp)
        lv = stop[j]
        if p.o[j] >= tpx:
            return float(p.o[j]), k, R_TP
        if p.o[j] <= lv:
            return float(p.o[j]), k, (R_SL if p.o[j] < entry else R_TP)
        if p.c[j] < p.o[j]:  # 음봉: 고가가 먼저
            return float(tpx), k, R_TP
        return float(lv), k, (R_SL if lv < entry else R_TP)  # 양봉: 저가가 먼저
    if j == js:
        lv = stop[j]
        px = float(p.o[j]) if p.o[j] <= lv else float(lv)
        return px, k, (R_SL if px < entry else R_TP)
    if j == jt:
        return float(max(p.o[j], entry * (1 + r.tp))), k, R_TP
    return float(p.c[j]), k, R_FORCE


def build_paths(events: pd.DataFrame, use_m1: bool = True, horizon_days: int = 5, ok_col: str = "m1_status") -> list:
    """events 의 index 는 0..n-1 이어야 한다. ok_col 이 'ok' 인 이벤트만 1분봉 경로를 쓴다."""
    paths = [None] * len(events)
    for code, grp in events.groupby("code", sort=False):
        sb = to_stock_bars(code, read_m5(code, regular_only=True))
        for idx, r in grp.iterrows():
            m1 = read_m1(code, int(r["date"])) if use_m1 and r.get(ok_col, "") == "ok" else None
            trig = None
            if m1 is not None:
                tt = r.get("trig_time", np.nan)
                if pd.notna(tt):
                    trig = int(tt)
                else:
                    t_end = int(np.floor(float(r["est_min"]))) + 1  # 편입이 일어난 분봉의 끝 시각(분)
                    trig = (t_end // 60) * 10000 + (t_end % 60) * 100
                if not (m1["time"] == trig).any():
                    m1, trig = None, None
            p = build_path(sb, int(r["i"]), float(r["entry"]), m1=m1, trig_minute=trig, horizon_days=horizon_days)
            paths[idx] = Path(p.key, p.o.astype(np.float32), p.h.astype(np.float32), p.l.astype(np.float32),
                              p.c.astype(np.float32), p.dayn.astype(np.int16), p.time)
    return paths


def evaluate(paths: list, entries: np.ndarray, r: Rule, idx: np.ndarray | None = None):
    n = len(paths)
    sel = range(n) if idx is None else idx
    price = np.full(n, np.nan)
    key = np.zeros(n, dtype=np.int64)
    reason = np.zeros(n, dtype=np.int8)
    for i in sel:
        p = paths[i]
        if p is None:
            continue
        price[i], key[i], reason[i] = apply_rule(p, float(entries[i]), r)
    return price, key, reason
