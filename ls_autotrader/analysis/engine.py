"""떡상이(2023.04.03) 조건 + 자동매매 규칙 정밀 백테스트 엔진.

실시간 조건검색 재현 방식
- A~D(0봉전 돌파)는 봉이 끝나기 전에도 성립한다. 현재 봉의 상한선은 '지금 가격'에 따라 같이 움직이므로
  "가격 p 가 상한선(p)을 넘는 최소 가격" p* 를 지표마다 닫힌 식으로 구한다.
    Envelope(n,e): p* = (1+e)(n-1)·m0 / (n-1-e)
    BB(n,k)      : p* = m0 + k·sqrt(n·v0 / (n-1-k²))
  (m0, v0 = 직전 n-1개 종가의 평균·모분산). p* 는 직전 봉들로만 정해지므로 봉 안에서 고정이다.
- 봉 i 에서 네 지표 모두 '직전 봉 종가 ≤ 직전 상한선'이고 고가 > max(p*) 이면 그 봉 안에서 조건 편입.
  편입가 = max(시가, p* 를 호가단위로 올림). 1분봉이 있으면 처음 p* 를 넘은 분을 찾아 시각·가격을 정한다.
- E~H(1봉전)는 직전 봉이 완성 돌파였으면 참 → 이미 편입된 상태를 유지하는 역할이라 새 편입 신호는 A~D 에서만 생긴다.
"""
from __future__ import annotations

import csv
import gzip
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data" / "research"

BB_SPECS = ((45, 2.0), (55, 2.0))
ENV_SPECS = ((13, 2.5), (22, 2.5))


# ---------------------------------------------------------------- 호가단위 / 가격제한
def tick_size(p: np.ndarray | float):
    p = np.asarray(p, dtype=float)
    return np.select([p < 2000, p < 5000, p < 20000, p < 50000, p < 200000, p < 500000],
                     [1, 5, 10, 50, 100, 500], 1000).astype(float)


def tick_ceil(p):
    """p 보다 큰 첫 호가 (p 가 정확히 호가면 그 다음 호가: '돌파'는 초과가 필요)."""
    p = np.asarray(p, dtype=float)
    t = tick_size(p)
    q = np.floor(p / t + 1e-9) * t
    return q + t


def tick_floor(p):
    p = np.asarray(p, dtype=float)
    t = tick_size(p)
    return np.floor(p / t + 1e-9) * t


def upper_limit(prev_close):
    return tick_floor(np.asarray(prev_close, dtype=float) * 1.3)


# ---------------------------------------------------------------- 로딩
def read_m5(code: str, regular_only: bool = True) -> pd.DataFrame | None:
    p = DATA / "m5" / f"{code}.csv.gz"
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype={"date": np.int64, "time": np.int64})
    if df.empty:
        return None
    if regular_only:
        df = df[df["time"] <= 153000]
    df = df[(df["open"] > 0) & (df["close"] > 0)].reset_index(drop=True)
    return df


def read_daily(code: str) -> pd.DataFrame | None:
    p = DATA / "daily" / f"{code}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype={"date": np.int64})
    return df if not df.empty else None


def universe() -> pd.DataFrame:
    u = pd.read_csv(DATA / "universe.csv", dtype={"code": str})
    return u


def excluded_codes(flags=("t1404:1", "t1404:4", "t1405:1", "t1405:2", "t1405:3", "t1405:5")) -> set[str]:
    p = DATA / "flags.csv"
    if not p.exists():
        return set()
    f = pd.read_csv(p, dtype={"code": str})
    keys = {tuple(x.split(":")) for x in flags}
    f["key"] = list(zip(f["tr"], f["jongchk"].astype(str)))
    return set(f.loc[f["key"].isin(keys), "code"])


# ---------------------------------------------------------------- 지표 임계가
def thresholds(close: np.ndarray, bb_specs=BB_SPECS, env_specs=ENV_SPECS) -> np.ndarray:
    """각 봉 i 에 대해 지표별 p*_j(i) (직전 n-1 종가로 계산). 모양 (지표수, 봉수), 계산 불가는 nan."""
    s = pd.Series(close, dtype=float)
    out = []
    for n, k in bb_specs:
        prev = s.shift(1)
        m0 = prev.rolling(n - 1).mean()
        v0 = prev.rolling(n - 1).var(ddof=0).clip(lower=0)
        out.append((m0 + k * np.sqrt(n * v0 / (n - 1 - k * k))).to_numpy())
    for n, e in env_specs:
        e = e / 100
        m0 = s.shift(1).rolling(n - 1).mean()
        out.append(((1 + e) * (n - 1) * m0 / (n - 1 - e)).to_numpy())
    return np.vstack(out)


@dataclass
class StockBars:
    code: str
    date: np.ndarray
    time: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    val: np.ndarray
    day_start: np.ndarray  # 각 봉이 속한 날의 첫 봉 인덱스
    cumvol: np.ndarray  # 당일 누적 거래량 (해당 봉 포함)
    cumval: np.ndarray  # 당일 누적 거래대금(백만원 단위 원자료 합)


def to_stock_bars(code: str, df: pd.DataFrame) -> StockBars:
    date = df["date"].to_numpy()
    newday = np.r_[True, date[1:] != date[:-1]]
    idx = np.arange(len(df))
    day_start = np.maximum.accumulate(np.where(newday, idx, 0))
    v = df["volume"].to_numpy(dtype=float)
    val = df["value"].to_numpy(dtype=float)
    g = np.cumsum(newday)
    cumvol = pd.Series(v).groupby(g).cumsum().to_numpy()
    cumval = pd.Series(val).groupby(g).cumsum().to_numpy()
    return StockBars(code, date, df["time"].to_numpy(), df["open"].to_numpy(dtype=float),
                     df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float),
                     df["close"].to_numpy(dtype=float), v, val, day_start, cumvol, cumval)


def detect_events(sb: StockBars, date_from: int, date_to: int, min_volume: float = 50_000,
                  bb_specs=BB_SPECS, env_specs=ENV_SPECS) -> pd.DataFrame:
    """조건 편입(N/R) 이벤트. 봉 단위 근사 (봉 안의 첫 편입만)."""
    c, h, o = sb.c, sb.h, sb.o
    P = thresholds(c, bb_specs, env_specs)
    valid = ~np.isnan(P).any(axis=0)
    Pmax = np.where(valid, np.nanmax(np.where(np.isnan(P), -np.inf, P), axis=0), np.nan)
    above = c[None, :] > P  # 완성 봉 종가가 각 상한선 위
    above_prev = np.zeros_like(above)
    above_prev[:, 1:] = above[:, :-1]
    valid_prev = np.r_[False, valid[:-1]]
    elig = valid & valid_prev & ~above_prev.any(axis=0)
    cross_done = elig & (c > Pmax)  # 봉 완성 시 A~D 성립
    eh = np.r_[False, cross_done[:-1]]  # E~H: 직전 봉이 완성 돌파
    end_state = cross_done | eh
    end_prev = np.r_[True, end_state[:-1]]
    trig = elig & (h > Pmax) & ~end_prev
    # 거래량 조건 I: 당일 누적 거래량 (봉 포함, 근사)
    trig &= sb.cumvol >= min_volume
    trig &= (sb.date >= date_from) & (sb.date <= date_to)
    idx = np.nonzero(trig)[0]
    if len(idx) == 0:
        return pd.DataFrame()
    pstar = Pmax[idx]
    entry_raw = np.maximum(o[idx], tick_ceil(pstar))
    entry_raw = np.minimum(entry_raw, h[idx])
    ev = pd.DataFrame({
        "code": sb.code, "i": idx, "date": sb.date[idx], "time": sb.time[idx],
        "pstar": pstar, "entry": entry_raw, "bar_open": o[idx], "bar_high": h[idx], "bar_low": sb.l[idx],
        "bar_close": c[idx], "open_above": o[idx] > pstar, "cumvol": sb.cumvol[idx], "cumval": sb.cumval[idx],
        "first_of_day": False,
    })
    first = ~pd.Series(ev["date"].to_numpy()).duplicated().to_numpy()
    ev["first_of_day"] = first
    return ev
