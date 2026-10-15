"""체결강도(J) 조건까지 넣은 편입 재현 (1분봉 + 보정된 체결강도 추정치).

차트 조건(A~D 또는 E~H)이 참인 동안 체결강도 추정치가 기준 이상이 되는 첫 분에 편입(N/R)한다.
- 봉 i 안: 처음 p* 를 넘은 분부터, 그 봉에서 고가가 p* 를 넘는 분까지 차트 조건 참
- 봉 i 가 p* 위에서 끝나면(완성 돌파) 다음 봉 i+1 전체가 E~H 로 참
- 체결강도 추정: 1분봉 틱룰(분 종가 등락으로 그 분 거래량을 매수/매도로 나눔) 누적 비율 × 보정계수
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from engine import DATA, read_m5, tick_ceil, to_stock_bars  # noqa: E402
from bt import TR_ACT, TR_MODES, TR_SL, TR_TRAIL, build_path, exits_grid, exits_trail  # noqa: E402
from refine_m1 import minute_of, read_m1  # noqa: E402


def cum_strength(m1: pd.DataFrame, prev_close: float, method: str = "tick") -> tuple[np.ndarray, np.ndarray]:
    """분봉 끝 시각 기준 누적 매수/매도 추정량."""
    c = m1["close"].to_numpy(float)
    o = m1["open"].to_numpy(float)
    h = m1["high"].to_numpy(float)
    l = m1["low"].to_numpy(float)
    v = m1["volume"].to_numpy(float).copy()
    if len(v):
        v[0] *= 0.5  # 첫 분 거래량 중 시가 단일가 체결분(집계 제외) 대략 제거
    dc = np.diff(np.r_[prev_close, c])
    if method == "tick":
        f = np.where(dc > 0, 1.0, np.where(dc < 0, 0.0, 0.5))
    elif method == "tick_clv":
        rng = h - l
        clv = np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1), 0.5)
        f = np.where(dc > 0, 1.0, np.where(dc < 0, 0.0, clv))
    else:  # body
        rng = h - l
        f = np.where(rng > 0, 0.5 + 0.5 * (c - o) / np.where(rng > 0, rng, 1), 0.5)
    return np.cumsum(f * v), np.cumsum((1 - f) * v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(DATA / "events.pkl"))
    ap.add_argument("--out", default=str(DATA / "events_j.pkl"))
    ap.add_argument("--method", default="tick")
    ap.add_argument("--thr", type=float, default=100.0, help="추정 체결강도 기준 (보정 후 값)")
    ap.add_argument("--scale", type=float, default=1.0, help="추정치 보정 배율")
    ap.add_argument("--min-volume", type=float, default=50_000)
    a = ap.parse_args()
    with open(a.events, "rb") as f:
        o = pickle.load(f)
    e = o["events"].copy()
    n = len(e)
    price, key, reason = o["price"].copy(), o["key"].copy(), o["reason"].copy()
    tshape = (len(TR_ACT), len(TR_TRAIL), len(TR_SL), len(TR_MODES))
    tprice = np.zeros((n,) + tshape, dtype=np.float32)
    tkey = np.zeros((n,) + tshape, dtype=np.int64)
    treason = np.zeros((n,) + tshape, dtype=np.int8)
    e["j_status"] = ""
    e["j_ok"] = False
    for code, grp in e.groupby("code", sort=False):
        df = read_m5(code, regular_only=True)
        sb = to_stock_bars(code, df)
        for idx, r in grp.iterrows():
            if not (540 <= r["est_min"] < 915):
                e.at[idx, "j_status"] = "outside_window"
                continue
            day = int(r["date"])
            m1 = read_m1(code, day)
            if m1 is None:
                e.at[idx, "j_status"] = "no_m1"
                continue
            i = int(r["i"])
            pstar = float(r["pstar"])
            mins = minute_of(m1["time"].to_numpy())
            bar_end = minute_of(np.array([int(r["bar_time"])]))[0]
            bar_start = bar_end - 5
            in_bar = (mins > bar_start) & (mins <= bar_end)
            hit = in_bar & (m1["high"].to_numpy(float) > pstar)
            if not hit.any():
                e.at[idx, "j_status"] = "no_cross_in_m1"
                continue
            k0 = int(np.argmax(hit))
            last_in_bar = int(np.nonzero(hit)[0][-1])
            window = list(range(k0, last_in_bar + 1))
            if sb.c[i] > pstar and i + 1 < len(sb.c) and sb.date[i + 1] == day:
                nb_end = minute_of(np.array([int(sb.time[i + 1])]))[0]
                window += list(np.nonzero((mins > bar_end) & (mins <= nb_end))[0])
            cb, cs = cum_strength(m1, float(r["prev_close"]), a.method)
            cv = np.cumsum(m1["volume"].to_numpy(float))
            entry_k = None
            for k in window:
                # k 분 안에서 판단: 그 분이 시작될 때까지의 누적 (k==k0 은 돌파 순간까지 일부 포함)
                b_ = cb[k - 1] if k > 0 else 0.0
                s_ = cs[k - 1] if k > 0 else 0.0
                if k == k0:
                    # 돌파 분: 직전 분까지 누적 + 돌파 직전까지의 거래(시간 비율만큼)를 매수로 본다.
                    # (그 분의 종가·이후 거래는 쓰지 않는다 - 미래 정보 방지)
                    row0 = m1.iloc[k]
                    rng0 = float(row0["high"] - row0["open"])
                    fr = 0.0 if row0["open"] > pstar or rng0 <= 0 else \
                        float(np.clip((max(float(row0["open"]), float(tick_ceil(pstar))) - row0["open"]) / rng0, 0, 1))
                    vk = float(m1["volume"].iloc[k]) * (0.5 if k == 0 else 1.0)
                    b_ = (cb[k - 1] if k > 0 else 0.0) + fr * vk
                    s_ = cs[k - 1] if k > 0 else 0.0
                st = a.scale * 100 * b_ / s_ if s_ > 0 else (np.inf if b_ > 0 else 0.0)
                cv_prev = cv[k - 1] if k > 0 else 0.0
                vol_ok = (cv_prev + (fr * float(m1["volume"].iloc[k]) if k == k0 else 0.0)) >= a.min_volume
                if vol_ok and a.thr <= st <= 1000 * a.scale:
                    entry_k = k
                    st_at_entry = st
                    break
            if entry_k is None:
                e.at[idx, "j_status"] = "J_never"
                continue
            row = m1.iloc[entry_k]
            if entry_k == k0:
                entry = max(float(row["open"]), float(tick_ceil(pstar)))
                entry = min(entry, float(row["high"]))
                tmin = mins[entry_k] - 1
                if row["open"] <= pstar and row["high"] > row["open"]:
                    tmin += float(np.clip((entry - row["open"]) / (row["high"] - row["open"]), 0, 1))
            else:
                entry = float(row["open"])
                tmin = float(mins[entry_k] - 1)
            path = build_path(sb, i, entry, m1=m1, trig_minute=int(row["time"]))
            price[idx], key[idx], reason[idx] = exits_grid(path, entry)
            tprice[idx], tkey[idx], treason[idx] = exits_trail(path, entry)
            day0 = path.dayn == 0
            eod = np.nonzero(day0)[0][-1]
            e.at[idx, "entry"] = entry
            e.at[idx, "est_min"] = tmin
            e.at[idx, "ret_entry"] = entry / r["prev_close"] - 1
            e.at[idx, "run_open"] = entry / r["day_open"] - 1
            e.at[idx, "eod"] = path.c[eod] / entry - 1
            e.at[idx, "mfe0"] = path.h[day0].max() / entry - 1
            e.at[idx, "mae0"] = path.l[day0].min() / entry - 1
            for nm, mm in (("f5m", 5), ("f15m", 15), ("f30m", 30), ("f60m", 60)):
                e.at[idx, nm] = path.c[min(mm, eod)] / entry - 1
            e.at[idx, "j_delay"] = entry_k - k0
            e.at[idx, "trig_time"] = int(row["time"])
            avg20 = sb.v[max(0, i - 20):i].mean() if i > 0 else np.nan
            vv = m1["volume"].to_numpy(float)
            part = vv[(mins > bar_start) & (mins <= mins[entry_k])].sum() if entry_k <= last_in_bar else np.nan
            e.at[idx, "burst"] = part / avg20 if avg20 and avg20 > 0 else np.nan
            e.at[idx, "strength_est"] = st_at_entry
            e.at[idx, "j_ok"] = True
            e.at[idx, "j_status"] = "ok"
    o["events"], o["price"], o["key"], o["reason"] = e, price, key, reason
    o["tprice"], o["tkey"], o["treason"] = tprice, tkey, treason
    o["tr_grid"] = {"act": TR_ACT, "trail": TR_TRAIL, "sl": TR_SL, "modes": TR_MODES}
    o["j_params"] = {"method": a.method, "thr": a.thr, "scale": a.scale}
    with open(a.out, "wb") as f:
        pickle.dump(o, f)
    print(e["j_status"].value_counts().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
