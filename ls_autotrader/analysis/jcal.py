"""체결강도(J) 보정: 10/12·10/13 실측(t1310) ↔ 1분봉 추정치 비교, 2개월 전체에 쓸 추정 규칙을 고른다."""
from __future__ import annotations

import os
import sys
from math import erf

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from engine import DATA  # noqa: E402
from refine_m1 import read_m1  # noqa: E402

DAYGB = {20261013: "0", 20261012: "1"}


def true_strength(e: pd.DataFrame, t1310_csv=DATA / "t1310_out.csv") -> pd.Series:
    """편입 순간의 실제 체결강도 (분 경계 사이는 매수·매도 누적량을 선형 보간)."""
    t = pd.read_csv(t1310_csv, dtype={"code": str, "daygb": str, "qtime": str, "chetime": str})
    t = t.drop_duplicates(["code", "daygb", "chetime"])
    t["min"] = t["chetime"].str[:2].astype(int) * 60 + t["chetime"].str[2:4].astype(int)
    g = {k: v.sort_values("min") for k, v in t.groupby(["code", "daygb"])}
    out = pd.Series(np.nan, index=e.index)
    for idx, r in e.iterrows():
        dg = DAYGB.get(int(r["date"]))
        if dg is None or (r["code"], dg) not in g:
            continue
        s = g[(r["code"], dg)]
        m = float(r["est_min"])
        lo = s[s["min"] <= m]
        hi = s[s["min"] >= m]
        if lo.empty and hi.empty:
            continue
        if lo.empty:
            a = b = hi.iloc[0]
            w = 0.0
        elif hi.empty:
            a = b = lo.iloc[-1]
            w = 0.0
        else:
            a, b = lo.iloc[-1], hi.iloc[0]
            w = 0.0 if b["min"] == a["min"] else (m - a["min"]) / (b["min"] - a["min"])
        ms = a["msvolume"] + w * (b["msvolume"] - a["msvolume"])
        md = a["mdvolume"] + w * (b["mdvolume"] - a["mdvolume"])
        out[idx] = 100 * ms / md if md > 0 else (np.inf if ms > 0 else np.nan)
    return out


def proxies_m1(code: str, day: int, prev_close: float, est_min: float) -> dict:
    """1분봉으로 만든 편입 순간 체결강도 추정치들."""
    m1 = read_m1(code, day)
    if m1 is None:
        return {}
    t = m1["time"].to_numpy()
    mins = (t // 10000) * 60 + (t // 100) % 100  # 분봉 끝 시각
    c = m1["close"].to_numpy(float)
    o = m1["open"].to_numpy(float)
    h = m1["high"].to_numpy(float)
    l = m1["low"].to_numpy(float)
    v = m1["volume"].to_numpy(float).copy()
    if len(v) and mins[0] == 541:
        # 첫 분봉 거래량에는 시가 단일가 체결이 섞여 있다(체결강도 집계 제외) → 첫 분 거래량의 절반만 반영
        v[0] *= 0.5
    full = mins <= np.floor(est_min)  # 편입 시각 이전에 끝난 분
    part = (mins > np.floor(est_min)) & (mins - 1 < est_min)
    wgt = np.where(full, 1.0, 0.0)
    if part.any():
        k = np.argmax(part)
        wgt[k] = est_min - (mins[k] - 1)
    vv = v * wgt
    dc = np.diff(np.r_[prev_close, c])
    tick = np.where(dc > 0, 1.0, np.where(dc < 0, 0.0, 0.5))
    rng = h - l
    clv = np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1), 0.5)
    body = np.where(rng > 0, 0.5 + 0.5 * (c - o) / np.where(rng > 0, rng, 1), 0.5)
    sd = np.std(dc[1:]) if len(dc) > 2 else 1.0
    bvc = 0.5 * (1 + np.vectorize(erf)(np.clip(dc / max(sd, 1e-9), -6, 6) / np.sqrt(2)))
    res = {}
    for nm, frac in (("tick", tick), ("clv", clv), ("body", body), ("bvc", bvc),
                     ("tick_clv", np.where(dc == 0, clv, tick))):
        b = (frac * vv).sum()
        s = ((1 - frac) * vv).sum()
        res[f"px_{nm}"] = 100 * b / s if s > 0 else (np.inf if b > 0 else np.nan)
    return res
