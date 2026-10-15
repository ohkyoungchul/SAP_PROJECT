"""10/12·10/13 편입 이벤트로 체결강도(J) 추정 규칙 보정.

1) events.pkl 에서 두 날짜 이벤트만 뽑아 1분봉으로 편입 시각 정밀화 (refine_m1 재사용)
2) t1310 실측 체결강도(편입 순간) 붙이기
3) 1분봉 추정치·전일 체결강도 등으로 J(100~1000%) 판별력 비교
"""
from __future__ import annotations

import os
import pickle
import subprocess
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from engine import DATA, read_daily  # noqa: E402
from jcal import proxies_m1, true_strength  # noqa: E402

PY = sys.executable


def main() -> int:
    src = DATA / "events.pkl"
    sub = DATA / "events_jcal.pkl"
    with open(src, "rb") as f:
        o = pickle.load(f)
    e = o["events"]
    m = (e["date"].isin([20261012, 20261013]) & (e["est_min"] >= 540) & (e["est_min"] < 915)).to_numpy()
    keep = np.nonzero(m)[0]
    o2 = dict(o)
    o2["events"] = e.iloc[keep].reset_index(drop=True)
    for k in ("price", "key", "reason"):
        o2[k] = o[k][keep]
    with open(sub, "wb") as f:
        pickle.dump(o2, f)
    subprocess.run([PY, os.path.join(os.path.dirname(__file__), "refine_m1.py"), "--events", str(sub)], check=True)
    with open(str(sub).replace(".pkl", "_m1.pkl"), "rb") as f:
        o3 = pickle.load(f)
    ev = o3["events"]
    ev["ch_true"] = true_strength(ev)
    rows = []
    for idx, r in ev.iterrows():
        px = proxies_m1(r["code"], int(r["date"]), float(r["prev_close"]), float(r["est_min"]))
        d = read_daily(r["code"])
        prev = d[d["date"] < r["date"]]
        px["chd_prev1"] = float(prev["chdegree"].iloc[-1]) if len(prev) else np.nan
        px["chd_avg5"] = float(prev["chdegree"].iloc[-5:].mean()) if len(prev) else np.nan
        px["chd_med20"] = float(prev["chdegree"].iloc[-20:].median()) if len(prev) else np.nan
        rows.append(px)
    ev = pd.concat([ev, pd.DataFrame(rows, index=ev.index)], axis=1)
    ev.to_pickle(DATA / "jcal_events.pkl")
    v = ev[ev["m1_status"].eq("ok") & ev["ch_true"].notna()].copy()
    v["J"] = v["ch_true"].between(100, 1000)
    print(f"표본 {len(v)} (전체 {len(ev)}), 실제 J 통과율 {v['J'].mean():.3f}")
    print(v.groupby("date")["J"].agg(["mean", "count"]).round(3).to_string())
    for c in [c for c in v.columns if c.startswith("px_")] + ["chd_prev1", "chd_avg5", "chd_med20", "strength_m1",
                                                               "chdegree_eod"]:
        if c not in v:
            continue
        x = v[c].replace([np.inf, -np.inf], 1e4)
        sp = x.rank().corr(v["ch_true"].rank())
        best = max(((np.mean((x >= t) == v["J"]), t) for t in np.nanquantile(x, np.linspace(0.02, 0.98, 97))),
                   default=(np.nan, np.nan))
        print(f"{c:>14}: spearman={sp:.3f} best_acc={best[0]:.3f} @>={best[1]:.1f}")
    print("\nJ 통과/탈락별 결과 (gross, %):")
    print((v.groupby("J")[["f5m", "f15m", "f30m", "eod", "mfe0", "mae0"]].mean() * 100).round(2).assign(
        n=v.groupby("J").size()).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
