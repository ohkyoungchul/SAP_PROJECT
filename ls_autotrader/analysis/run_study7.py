import sys
sys.path.insert(0, '.')
import bt
from study import *
pd.set_option('display.width', 250)
o, paths = load_set("events_m1", "m1_status")
e = o["events"]
m0 = base_mask(o) & (e["m1_status"] == "ok").to_numpy()
hA = (e["half"] == "A").to_numpy()
E = e["entry"].to_numpy()
tod = e["tod"].to_numpy(); pm = e["pc_ma20"].fillna(0).to_numpy(); hi = e["e_hi60"].fillna(0).to_numpy()
C = {"c": m0 & (tod <= 3) & (E <= 4000) & (pm <= 0.09), "d": m0 & (tod <= 3) & (E <= 4000) & (pm <= 0.09) & (hi <= -0.16),
     "e": m0 & (tod <= 3) & (pm <= 0.09) & (hi <= -0.16)}
R = {"TP8 SL3 당일": Rule(tp=0.08, sl=0.03, eod="D0_1515"), "TP10 SL3 보유(현재)": Rule(tp=0.10, sl=0.03, eod="H"),
     "SL3 추적4/3 당일": Rule(sl=0.03, tr_act=0.04, tr=0.03, eod="D0_1515")}
print("== 1) 매수 미끄러짐 민감도 (익절·손절선은 실제 체결가 기준), 신호당 순수익률 % 전반/후반")
for cn, m in C.items():
    idx = np.nonzero(m)[0]
    line = f"{cn}: "
    for rn, r in R.items():
        parts = []
        for sb in (0.003, 0.006, 0.01, 0.015):
            fill = E * (1 + sb)
            px, k, rs = evaluate(paths, fill, r, idx=idx)
            nr = net_return(fill, px, Cost(slip_b=0.0))
            parts.append(f"{sb*100:.1f}%:{nr[m&hA].mean()*100:.2f}/{nr[m&~hA].mean()*100:.2f}")
        line += f"\n   [{rn}] " + "  ".join(parts)
    print(line)
print("\n== 2) 첫 1분 저가 비관 가정 (분 중간 매수여도 그 분 저가 반영)")
bt.FIRST_LOW_MODE = "pessimistic"
from rules import build_paths
for cn in ("d", "e"):
    m = C[cn]; idx = np.nonzero(m)[0]
    sub = e.iloc[idx].reset_index(drop=True)
    pp = build_paths(sub, ok_col="m1_status")
    for rn, r in R.items():
        px, k, rs = evaluate(pp, sub["entry"].to_numpy(), r)
        nr = net_return(sub["entry"].to_numpy(), px, Cost())
        h = (sub["half"] == "A").to_numpy()
        print(f"{cn} [{rn}] {nr[h].mean()*100:.2f}/{nr[~h].mean()*100:.2f}")
bt.FIRST_LOW_MODE = "real"
print("\n== 3) 날짜·종목 쏠림 (TP8 SL3 당일, 후보 d·e)")
for cn in ("d", "e"):
    m = C[cn]; idx = np.nonzero(m)[0]
    px, k, rs = evaluate(paths, E, R["TP8 SL3 당일"], idx=idx)
    nr = pd.Series(net_return(E, px, Cost())[m], index=e.index[m])
    d = e.loc[m, "date"]
    daily = nr.groupby(d).sum()
    top = nr.groupby(e.loc[m, "code"]).sum().sort_values(ascending=False)
    print(f"{cn}: 건수 {len(nr)}, 수익일 {int((daily>0).sum())}/{len(daily)}, 상위5일 합 {daily.nlargest(5).sum()*100:.0f}%p / 전체 {nr.sum()*100:.0f}%p, "
          f"상위5종목 합 {top.head(5).sum()*100:.0f}%p, 상위5종목 빼면 평균 {nr[~e.loc[m,'code'].isin(top.head(5).index)].mean()*100:.2f}%, "
          f"승률 {(nr>0).mean()*100:.0f}%, 중앙값 {nr.median()*100:.2f}%")
    print("   청산 사유:", pd.Series(rs[idx]).map({1:'익절',2:'손절',3:'시간청산',4:'미청산'}).value_counts().to_dict())
