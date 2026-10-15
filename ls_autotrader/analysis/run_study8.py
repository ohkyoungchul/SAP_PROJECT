import sys, itertools
sys.path.insert(0, '.')
from study import *
pd.set_option('display.width', 250)
o, paths = load_set("events_m1", "m1_status")
e = o["events"]
m0 = base_mask(o) & (e["m1_status"] == "ok").to_numpy()
hA = (e["half"] == "A").to_numpy()
E = e["entry"].to_numpy(); tod = e["tod"].to_numpy(); pm = e["pc_ma20"].fillna(0).to_numpy(); hi = e["e_hi60"].fillna(0).to_numpy()
d = m0 & (tod <= 3) & (E <= 4000) & (pm <= 0.09) & (hi <= -0.16)
print("== 가격 하한 효과 (TP8 SL3 당일, 신호당 %)")
r = Rule(tp=0.08, sl=0.03, eod="D0_1515")
px, k, rs = evaluate(paths, E, r, idx=np.nonzero(d)[0]); nr = net_return(E, px, Cost())
for lo in (0, 500, 1000, 1500, 2000):
    mm = d & (E >= lo)
    print(f"  {lo}원 이상: n={int(mm.sum())} {nr[mm&hA].mean()*100:.2f}/{nr[mm&~hA].mean()*100:.2f}")
print("== 매수 마감 시각 효과 (분)")
for t in (1, 2, 3, 5, 10):
    mm = m0 & (tod <= t) & (E <= 4000) & (pm <= 0.09) & (hi <= -0.16)
    idx = np.nonzero(mm)[0]; px2, _, _ = evaluate(paths, E, r, idx=idx); n2 = net_return(E, px2, Cost())
    print(f"  09:00~09:{t:02d}: n={int(mm.sum())} {n2[mm&hA].mean()*100:.2f}/{n2[mm&~hA].mean()*100:.2f}")
print("== 익절·손절 격자 (후보 d, 신호당 % 전반/후반) - 보유(현재 봇) vs 당일 15:15 청산")
rows = []
for eod in ("H", "D0_1515"):
    for tp, sl in itertools.product([0.05, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15], [0.02, 0.025, 0.03, 0.04, 0.05]):
        rr = Rule(tp=tp, sl=sl, eod=eod)
        px, k, rs = evaluate(paths, E, rr, idx=np.nonzero(d)[0]); n3 = net_return(E, px, Cost())
        a, b = n3[d & hA].mean() * 100, n3[d & ~hA].mean() * 100
        rows.append({"eod": eod, "TP": tp * 100, "SL": sl * 100, "A": round(a, 2), "B": round(b, 2), "min": round(min(a, b), 2)})
g = pd.DataFrame(rows)
for eod in ("H", "D0_1515"):
    print(eod); print(g[g.eod == eod].pivot(index="SL", columns="TP", values="min").to_string())
g.to_csv(DATA / "study8_grid_d.csv", index=False)
