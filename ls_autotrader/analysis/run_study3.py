import sys, itertools
sys.path.insert(0, '.')
from study import *
pd.set_option('display.width', 250)
o, paths = load_set("events_j", "j_status")
e = o["events"]
m0 = base_mask(o, "j_ok")
idx = np.nonzero(m0)[0]
px, key, rs = evaluate(paths, e["entry"].to_numpy(), Rule(tp=0.08, sl=0.03, eod="D0_1515"), idx=idx)
ref = net_return(e["entry"].to_numpy(), px, Cost())
hA = (e["half"] == "A").to_numpy()
B = e["burst"].fillna(0).to_numpy(); S = e["strength_est"].fillna(0).to_numpy()
RO = e["run_open"].to_numpy(); RE = e["ret_entry"].to_numpy(); CV = e["cumval"].fillna(0).to_numpy() / 100
rows = []
for b, s, ro, re_, cv in itertools.product([0, 5, 10, 15, 20, 30], [0, 150, 200, 250, 300], [9, 0.05, 0.03], [9, 0.08], [1e9, 30]):
    m = m0 & (B >= b) & (S >= s) & (RO <= ro) & (RE <= re_) & (CV <= cv)
    nA, nB = (m & hA).sum(), (m & ~hA).sum()
    if nA < 40 or nB < 40: continue
    rows.append({"burst>=": b, "str>=": s, "run_open<=": ro, "ret<=": re_, "cumval<=": cv,
                 "perday": round(m.sum() / 40, 1), "evA": ref[m & hA].mean() * 100, "evB": ref[m & ~hA].mean() * 100,
                 "winA": (ref[m & hA] > 0).mean() * 100, "winB": (ref[m & ~hA] > 0).mean() * 100})
t = pd.DataFrame(rows)
t["evmin"] = t[["evA", "evB"]].min(axis=1)
t.to_csv(DATA / "study3_filters.csv", index=False)
print(t.sort_values("evmin", ascending=False).head(30).round(2).to_string(index=False))
print()
print("단일 조건 효과:")
print(t[(t["run_open<="] == 9) & (t["ret<="] == 9) & (t["cumval<="] == 1e9)].sort_values(["burst>=", "str>="]).round(2).to_string(index=False))
