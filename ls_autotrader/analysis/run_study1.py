import sys, time, itertools
sys.path.insert(0, '.')
from study import *

TPS = [0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15, None]
SLS = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04]
for name, ok_col, jcol in (("events_m1", "m1_status", None), ("events_j", "j_status", "j_ok")):
    t0 = time.time()
    o, paths = load_set(name, ok_col)
    e = o["events"]
    m = base_mask(o, jcol)
    if jcol is None:
        m &= (e["m1_status"] == "ok").to_numpy()
    print(f"===== {name}: events {int(m.sum())} (paths {time.time()-t0:.0f}s)", flush=True)
    rows = [run_rule(o, paths, Rule(tp=0.10, sl=0.03, eod="H"), m)]
    for eod in ("D0_1515", "H"):
        for tp, sl in itertools.product(TPS, SLS):
            rows.append(run_rule(o, paths, Rule(tp=tp, sl=sl, eod=eod), m))
    t = table(rows)
    t.to_csv(DATA / f"study1_{name}.csv", index=False)
    print("기준(현재 설정):"); print(fmt(t.head(1)))
    for eod in ("D0_1515", "H"):
        s = t[t.rule.str.endswith(eod)].sort_values("pnl", ascending=False)
        print(f"--- {eod} 상위 8 (P&L 합)"); print(fmt(s.head(8)))
    print(f"({time.time()-t0:.0f}s)", flush=True)
