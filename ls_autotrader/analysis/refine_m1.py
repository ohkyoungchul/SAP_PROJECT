"""1분봉으로 편입 시각·편입가·매수한 날의 청산 경로를 다시 계산한다.

    analysis\\.venv-ana\\Scripts\\python analysis\\refine_m1.py --events data\\analysis_2m\\events.pkl
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


def read_m1(code: str, day: int) -> pd.DataFrame | None:
    p = DATA / "m1" / f"{code}_{day}.csv.gz"
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype={"date": np.int64, "time": np.int64})
    df = df[(df["time"] <= 153000) & (df["open"] > 0)]
    return df if len(df) else None


def minute_of(hhmmss):
    return (hhmmss // 10000) * 60 + (hhmmss // 100) % 100


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(DATA / "events.pkl"))
    ap.add_argument("--out", default="")
    ap.add_argument("--min-volume", type=float, default=50_000)
    a = ap.parse_args()
    with open(a.events, "rb") as f:
        o = pickle.load(f)
    e = o["events"].copy()
    regular = o.get("regular_only", True)
    price, key, reason = o["price"].copy(), o["key"].copy(), o["reason"].copy()
    n = len(e)
    tshape = (len(TR_ACT), len(TR_TRAIL), len(TR_SL), len(TR_MODES))
    tprice = np.zeros((n,) + tshape, dtype=np.float32)
    tkey = np.zeros((n,) + tshape, dtype=np.int64)
    treason = np.zeros((n,) + tshape, dtype=np.int8)
    e["m1"] = False
    e["m1_status"] = ""
    for code, grp in e.groupby("code", sort=False):
        df = read_m5(code, regular_only=True)  # 청산 경로는 정규장 봉만 (이벤트의 i 도 정규장 기준)
        sb = to_stock_bars(code, df)
        for idx, r in grp.iterrows():
            m1 = read_m1(code, int(r["date"]))
            if m1 is None:
                e.at[idx, "m1_status"] = "no_m1"
            p5 = build_path(sb, int(r["i"]), float(r["entry"]))
            tp_, tk_, tr_ = exits_trail(p5, float(r["entry"]))
            tprice[idx], tkey[idx], treason[idx] = tp_, tk_, tr_
            if m1 is None:
                continue
            bar_end = int(r["bar_time"])
            end_min = minute_of(bar_end)
            start_min = end_min - 5 if bar_end != 153000 else minute_of(152000)
            mm = m1[(minute_of(m1["time"]) > start_min) & (minute_of(m1["time"]) <= end_min)]
            pstar = float(r["pstar"])
            hit = mm[mm["high"] > pstar]
            if hit.empty:
                e.at[idx, "m1_status"] = "no_cross_in_m1"
                continue
            first = hit.iloc[0]
            # 거래량 조건: 당일 누적(그 분 포함)
            cum = m1["volume"].cumsum()
            cum_at = float(cum[m1["time"] <= first["time"]].iloc[-1])
            if cum_at < a.min_volume:
                # 같은 5분봉 안에서 누적거래량 충족 후 첫 돌파 분
                ok = hit[[float(cum[m1["time"] <= t].iloc[-1]) >= a.min_volume for t in hit["time"]]]
                if ok.empty:
                    e.at[idx, "m1_status"] = "volume_not_met"
                    continue
                first = ok.iloc[0]
                cum_at = float(cum[m1["time"] <= first["time"]].iloc[-1])
            entry = max(float(first["open"]), float(tick_ceil(pstar)))
            entry = min(entry, float(first["high"]))
            tmin = minute_of(int(first["time"])) - 1  # 분봉 끝 시각 → 그 분의 시작
            if first["open"] <= pstar:
                rng = float(first["high"] - first["open"])
                tmin += float(np.clip((entry - first["open"]) / rng, 0, 1)) if rng > 0 else 0.0
            i = int(r["i"])
            path = build_path(sb, i, entry, m1=m1, trig_minute=int(first["time"]))
            pr, ky, rs = exits_grid(path, entry)
            price[idx], key[idx], reason[idx] = pr, ky, rs
            tp_, tk_, tr_ = exits_trail(path, entry)
            tprice[idx], tkey[idx], treason[idx] = tp_, tk_, tr_
            day0 = path.dayn == 0
            e.at[idx, "entry_5m"] = r["entry"]
            e.at[idx, "trig_time"] = int(first["time"])
            e.at[idx, "entry"] = entry
            e.at[idx, "est_min"] = tmin
            e.at[idx, "cumvol"] = cum_at
            if r.get("prev_vol", 0) and r["prev_vol"] > 0:
                e.at[idx, "vol_prev_ratio"] = cum_at / r["prev_vol"]
            if r.get("avgvol20", 0) and r["avgvol20"] > 0:
                e.at[idx, "vol_ratio"] = cum_at / r["avgvol20"]
            e.at[idx, "ret_entry"] = entry / r["prev_close"] - 1
            e.at[idx, "run_open"] = entry / r["day_open"] - 1
            e.at[idx, "mfe0"] = path.h[day0].max() / entry - 1
            e.at[idx, "mae0"] = path.l[day0].min() / entry - 1
            eod = np.nonzero(day0)[0][-1]
            e.at[idx, "eod"] = path.c[eod] / entry - 1
            for nm, mins in (("f5m", 5), ("f15m", 15), ("f30m", 30), ("f60m", 60)):
                j = min(mins, eod)
                e.at[idx, nm] = path.c[j] / entry - 1
            # 거래량 급증: 편입 분까지의 현재 5분봉 거래량 / 직전 20개 5분봉 평균
            mins_all = minute_of(m1["time"].to_numpy())
            part = m1["volume"].to_numpy(float)[(mins_all > start_min) & (mins_all <= minute_of(int(first["time"])))]
            avg20 = sb.v[max(0, i - 20):i].mean() if i > 0 else np.nan
            e.at[idx, "burst"] = part.sum() / avg20 if avg20 and avg20 > 0 else np.nan
            e.at[idx, "burst_prev"] = sb.v[i - 1] / avg20 if avg20 and avg20 > 0 else np.nan
            # 1분봉 틱룰 체결강도 근사: 편입 직전 분까지 / 하루 전체
            cl = m1["close"].to_numpy(float)
            vv = m1["volume"].to_numpy(float)
            dcl = np.diff(np.r_[float(r["prev_close"]), cl])
            buy = np.where(dcl > 0, vv, np.where(dcl < 0, 0.0, 0.5 * vv))
            before = (m1["time"] < first["time"]).to_numpy()
            sb_, ss_ = buy[before].sum(), (vv - buy)[before].sum()
            e.at[idx, "strength_m1"] = 100 * sb_ / ss_ if ss_ > 0 else np.nan
            e.at[idx, "strength_m1_eod"] = 100 * buy.sum() / max((vv - buy).sum(), 1)
            e.at[idx, "m1"] = True
            e.at[idx, "m1_status"] = "ok"
    o["events"], o["price"], o["key"], o["reason"] = e, price, key, reason
    o["tprice"], o["tkey"], o["treason"] = tprice, tkey, treason
    o["tr_grid"] = {"act": TR_ACT, "trail": TR_TRAIL, "sl": TR_SL, "modes": TR_MODES}
    out = a.out or a.events.replace(".pkl", "_m1.pkl")
    with open(out, "wb") as f:
        pickle.dump(o, f)
    print(e["m1_status"].value_counts().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
