"""진입 순간 이전 정보만 쓴 특성 (거래량 급증·체결강도) 계산 → clean_feats_<set>.pkl"""
import sys
sys.path.insert(0, '.')
import numpy as np, pandas as pd
from study import load_set
from engine import DATA, read_m5, to_stock_bars
from refine_m1 import read_m1, minute_of

def run(name, ok_col):
    o, _ = load_set(name, ok_col)
    e = o["events"]
    out = pd.DataFrame(index=e.index, columns=["burst_pre", "burst_frac", "str_pre", "str_frac", "v1_ratio"], dtype=float)
    for code, grp in e.groupby("code", sort=False):
        sb = to_stock_bars(code, read_m5(code, regular_only=True))
        for idx, r in grp.iterrows():
            if r.get(ok_col) != "ok" or pd.isna(r.get("trig_time")):
                continue
            m1 = read_m1(code, int(r["date"]))
            if m1 is None:
                continue
            mins = minute_of(m1["time"].to_numpy())
            v = m1["volume"].to_numpy(float).copy()
            c = m1["close"].to_numpy(float); o_ = m1["open"].to_numpy(float); h = m1["high"].to_numpy(float); l = m1["low"].to_numpy(float)
            k = int(np.nonzero(m1["time"].to_numpy() == int(r["trig_time"]))[0][0])
            frac = float(r["est_min"]) - (mins[k] - 1)
            frac = min(max(frac, 0.0), 1.0)
            i = int(r["i"])
            avg20 = sb.v[max(0, i - 20):i].mean() if i > 0 else np.nan
            bar_end = minute_of(np.array([int(r["bar_time"])]))[0]
            in_bar_before = (mins > bar_end - 5) & (mins < mins[k])
            pre = v[in_bar_before].sum()
            out.at[idx, "burst_pre"] = pre / avg20 if avg20 > 0 else np.nan
            out.at[idx, "burst_frac"] = (pre + frac * v[k]) / avg20 if avg20 > 0 else np.nan
            out.at[idx, "v1_ratio"] = (v[k - 1] if k > 0 else 0) / (avg20 / 5) if avg20 > 0 else np.nan
            vv = v.copy()
            if len(vv): vv[0] *= 0.5
            rng = h - l
            f = np.where(rng > 0, 0.5 + 0.5 * (c - o_) / np.where(rng > 0, rng, 1), 0.5)
            b = (f * vv)[:k].sum(); s = ((1 - f) * vv)[:k].sum()
            out.at[idx, "str_pre"] = 100 * b / s if s > 0 else np.nan
            out.at[idx, "str_frac"] = 100 * (b + frac * vv[k]) / s if s > 0 else np.nan
    out.to_pickle(DATA / f"clean_feats_{name}.pkl")
    print(name, out.notna().sum().to_dict())

# run("events_m1", "m1_status")
run("events_j", "j_status")
