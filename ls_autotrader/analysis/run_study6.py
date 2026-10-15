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
tod = e["tod"].to_numpy(); pr = E; pm = e["pc_ma20"].fillna(0).to_numpy(); hi = e["e_hi60"].fillna(0).to_numpy()
r20 = e["ret20d"].fillna(0).to_numpy()
cands = {
    "a) 09:00~09:03": m0 & (tod <= 3),
    "b) a + 4,000원 이하": m0 & (tod <= 3) & (pr <= 4000),
    "c) b + 20일선 대비 +9% 이하": m0 & (tod <= 3) & (pr <= 4000) & (pm <= 0.09),
    "d) c + 60일 고점 대비 -16% 이하": m0 & (tod <= 3) & (pr <= 4000) & (pm <= 0.09) & (hi <= -0.16),
    "e) a + 20일선 +9% 이하 + 60일고점 -16% 이하": m0 & (tod <= 3) & (pm <= 0.09) & (hi <= -0.16),
    "참고) 09:03 이후 전부": m0 & (tod > 3),
}
rules = [Rule(tp=0.10, sl=0.03, eod="H"), Rule(tp=0.08, sl=0.03, eod="D0_1515"), Rule(tp=0.05, sl=0.02, eod="D0_1515"),
         Rule(tp=0.08, sl=0.04, eod="D0_1515"), Rule(tp=0.10, sl=0.03, eod="D0_1515"), Rule(tp=0.15, sl=0.04, eod="D0_1515"),
         Rule(tp=0.05, sl=0.03, eod="D0_1515"), Rule(sl=0.03, tr_act=0.04, tr=0.03, eod="D0_1515")]
print("== 후보별 신호당 순수익률(%) [전반/후반], 슬리피지 매수0.3%·매도0.2%")
rows = []
for cn, m in cands.items():
    idx = np.nonzero(m)[0]
    row = {"후보": cn, "nA": int((m & hA).sum()), "nB": int((m & ~hA).sum()), "일수": int(e.loc[m, "date"].nunique())}
    for r in rules:
        px, k, rs = evaluate(paths, E, r, idx=idx)
        nr = net_return(E, px, Cost())
        row[r.label()] = f"{nr[m & hA].mean()*100:5.2f}/{nr[m & ~hA].mean()*100:5.2f}"
    rows.append(row)
print(pd.DataFrame(rows).to_string(index=False))
