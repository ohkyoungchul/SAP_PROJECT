"""전 종목 조건 편입 이벤트 + 특성 + 청산 격자 계산 → events.pkl

    analysis\\.venv-ana\\Scripts\\python analysis\\run_events.py [--after-hours]
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
import time as _time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from engine import (DATA, detect_events, excluded_codes, read_daily, read_m5, tick_ceil, to_stock_bars, universe,
                    upper_limit)
from bt import HOLD_MODES, SL_GRID, TP_GRID, bar_key, build_path, daily_features, exits_grid, strength_proxy

DATE_FROM, DATE_TO = 20260812, 20261013
MARKETS = {"kq": "229200", "ks": "069500"}


def minute_of(hhmmss: np.ndarray) -> np.ndarray:
    hh = hhmmss // 10000
    mm = (hhmmss // 100) % 100
    return hh * 60 + mm


def market_lookup(code: str, regular_only: bool):
    df = read_m5(code, regular_only=regular_only)
    if df is None:
        print(f"시장 비교용 {code} 데이터 없음 - 시장 특성 생략")
        return pd.Series(dtype=float)
    sb = to_stock_bars(code, df)
    keys = bar_key(sb.date, sb.time)
    # 날짜별 전일 종가
    last_close = pd.Series(sb.c).groupby(sb.date).last()
    prev_close = last_close.shift(1)
    pc = prev_close.reindex(sb.date).to_numpy()
    ret = sb.c / pc - 1
    return pd.Series(ret, index=keys)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regular-only", action="store_true",
                    help="15:30 이후 봉을 빼고 지표 계산 (가설 H1). 기본은 저장된 봉 전부 (서버와 같음, t1859 로 확인)")
    ap.add_argument("--out", default=str(DATA / "events.pkl"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--from", dest="date_from", type=int, default=DATE_FROM)
    ap.add_argument("--to", dest="date_to", type=int, default=DATE_TO)
    a = ap.parse_args()
    regular = a.regular_only
    uni = universe().set_index("code")
    excl = excluded_codes()
    mk = {k: market_lookup(v, True) for k, v in MARKETS.items()}
    files = sorted(f.split(".")[0] for f in os.listdir(DATA / "m5") if f.endswith(".csv.gz"))
    codes = [c for c in files if c not in MARKETS.values()]
    if a.limit:
        codes = codes[:a.limit]
    t0 = _time.monotonic()
    ev_rows, P, K, R = [], [], [], []
    stats = {"stocks": 0, "excluded": 0, "no_data": 0}
    for n, code in enumerate(codes, 1):
        if code in excl:
            stats["excluded"] += 1
            continue
        df = read_m5(code, regular_only=regular)
        d = read_daily(code)
        if df is None or d is None or len(df) < 100:
            stats["no_data"] += 1
            continue
        stats["stocks"] += 1
        sb_all = to_stock_bars(code, df)
        ev = detect_events(sb_all, a.date_from, a.date_to)
        if ev.empty:
            continue
        stats["events_all"] = stats.get("events_all", 0) + len(ev)
        ev = ev[ev["time"] <= 153000]  # 시간외 편입은 자동매매가 매수하지 않음
        if ev.empty:
            continue
        # 매매·청산 경로는 정규장 봉만 (자동매매는 09:00~15:30 에만 주문)
        reg = df[df["time"] <= 153000].reset_index(drop=True)
        sb = to_stock_bars(code, reg)
        kmap = pd.Series(np.arange(len(reg)), index=bar_key(sb.date, sb.time))
        ev = ev.copy()
        ev["i_all"] = ev["i"]
        ev["i"] = kmap.reindex(bar_key(ev["date"].to_numpy(), ev["time"].to_numpy())).to_numpy()
        ev = ev[ev["i"].notna()]
        ev["i"] = ev["i"].astype(int)
        sp = strength_proxy(sb)
        dfeat_cache = {}
        name = uni["name"].get(code, "")
        market = uni["market"].get(code, "")
        for r in ev.itertuples(index=False):
            i = int(r.i)
            d0 = int(r.date)
            if d0 not in dfeat_cache:
                dfeat_cache[d0] = daily_features(d, d0)
            df0 = dfeat_cache[d0]
            pc = df0.get("prev_close", np.nan)
            if not np.isfinite(pc) or pc <= 0:
                continue
            E0 = float(r.entry)
            ul = float(upper_limit(pc))
            if E0 > ul:  # 상한가 위 임계가 → 불가능
                continue
            o, h = float(r.bar_open), float(r.bar_high)
            frac = 0.0 if r.open_above else float(np.clip((E0 - o) / (h - o), 0, 1)) if h > o else 0.0
            t_end = minute_of(np.array([int(r.time)]))[0]
            t_start = t_end - 5 if int(r.time) != 153000 else t_end
            est_min = t_start + frac * (t_end - t_start)
            ds = sb.day_start[i]
            day_open = sb.o[ds]
            prev_cum = sb.cumvol[i - 1] if i - 1 >= ds else 0.0
            prev_cumval = sb.cumval[i - 1] if i - 1 >= ds else 0.0
            cumvol_e = prev_cum + frac * sb.v[i]
            cumval_e = prev_cumval + frac * sb.val[i]
            lo = max(0, i - 20)
            avgbar = sb.v[lo:i].mean() if i > lo else np.nan
            kprev = bar_key(sb.date[i - 1:i], sb.time[i - 1:i])[0]
            path = build_path(sb, i, E0)
            pr, ky, rs = exits_grid(path, E0)
            # 당일 경로 통계
            day0 = path.dayn == 0
            mfe0 = path.h[day0].max() / E0 - 1
            mae0 = path.l[day0].min() / E0 - 1
            eod_idx = np.nonzero(day0)[0][-1]
            nxt = np.nonzero(path.dayn == 1)[0]

            def fwd(nb):
                j = min(nb, len(path.c) - 1)
                return path.c[j] / E0 - 1 if path.dayn[j] == 0 else path.c[eod_idx] / E0 - 1

            ev_rows.append({
                "code": code, "name": name, "market": market, "date": d0, "bar_time": int(r.time), "i": i,
                "est_min": est_min, "frac": frac, "open_above": bool(r.open_above), "first_of_day": bool(r.first_of_day),
                "pstar": float(r.pstar), "entry": E0, "prev_close": pc, "day_open": day_open,
                "ret_entry": E0 / pc - 1, "gap": day_open / pc - 1, "run_open": E0 / day_open - 1,
                "dist_ul": ul / E0 - 1, "cumvol": cumvol_e, "cumval": cumval_e,
                "vol_ratio": cumvol_e / df0.get("avgvol20", np.nan) if df0.get("avgvol20") else np.nan,
                "vol_prev_ratio": cumvol_e / df0["prev_vol"] if df0.get("prev_vol") else np.nan,
                "prev_val": df0.get("prev_val", np.nan), "prev_vol": df0.get("prev_vol", np.nan),
                "avgvol20": df0.get("avgvol20", np.nan),
                "barvol_ratio": sb.v[i] / avgbar if avgbar and avgbar > 0 else np.nan,
                "prevbar_ratio": sb.v[i - 1] / avgbar if avgbar and avgbar > 0 else np.nan,
                "pc_ma5": pc / df0.get("ma5", np.nan) - 1, "pc_ma20": pc / df0.get("ma20", np.nan) - 1,
                "pc_ma60": pc / df0.get("ma60", np.nan) - 1, "ret1d": df0.get("ret1d", np.nan),
                "ret5d": df0.get("ret5d", np.nan), "ret20d": df0.get("ret20d", np.nan),
                "mcap": df0.get("mcap", np.nan), "avgval20": df0.get("avgval20", np.nan),
                "e_hi60": E0 / df0.get("hi60", np.nan) - 1,
                "strength_proxy": sp[i], "chdegree_eod": df0.get("chdegree_eod", np.nan),
                "mkt_kq": mk["kq"].get(kprev, np.nan), "mkt_ks": mk["ks"].get(kprev, np.nan),
                "f1": fwd(1), "f3": fwd(3), "f6": fwd(6), "f12": fwd(12), "eod": path.c[eod_idx] / E0 - 1,
                "next_open": path.o[nxt[0]] / E0 - 1 if len(nxt) else np.nan,
                "next_close": path.c[nxt[-1]] / E0 - 1 if len(nxt) else np.nan,
                "mfe0": mfe0, "mae0": mae0,
            })
            P.append(pr.astype(np.float32))
            K.append(ky)
            R.append(rs)
        if n % 200 == 0:
            print(f"{n}/{len(codes)} 종목, 이벤트 {len(ev_rows)}, {(_time.monotonic() - t0):.0f}s", flush=True)
    evdf = pd.DataFrame(ev_rows)
    out = {"events": evdf, "price": np.stack(P) if P else None, "key": np.stack(K) if K else None,
           "reason": np.stack(R) if R else None, "tp_grid": TP_GRID, "sl_grid": SL_GRID, "hold_modes": HOLD_MODES,
           "stats": stats, "regular_only": regular, "date_from": a.date_from, "date_to": a.date_to}
    with open(a.out, "wb") as f:
        pickle.dump(out, f)
    print("완료:", stats, "이벤트", len(evdf), f"{(_time.monotonic() - t0):.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
