import sys, itertools
sys.path.insert(0, '.')
from study import *
pd.set_option('display.width', 250)
RULES = [Rule(tp=0.08, sl=0.03, eod="D0_1515"), Rule(tp=0.05, sl=0.02, eod="D0_1515"), Rule(tp=0.10, sl=0.03, eod="H"),
         Rule(tp=0.15, sl=0.04, eod="D0_1515"), Rule(tp=0.08, sl=0.04, eod="D0_1515")]
out = []
for name, ok_col, jcol in (("events_m1", "m1_status", None),):
    o, paths = load_set(name, ok_col)
    e = o["events"]
    cf = pd.read_pickle(DATA / f"clean_feats_{name}.pkl")
    for c in cf: e[c] = cf[c].astype(float)
    m0 = base_mask(o, jcol)
    if jcol is None: m0 &= (e["m1_status"] == "ok").to_numpy()
    idx = np.nonzero(m0)[0]
    refs = {}
    for rule in RULES:
        px, k, rs = evaluate(paths, e["entry"].to_numpy(), rule, idx=idx)
        refs[rule.label()] = net_return(e["entry"].to_numpy(), px, Cost())
    hA = (e["half"] == "A").to_numpy()
    r20 = e["ret20d"].fillna(0).to_numpy(); pm = e["pc_ma20"].fillna(0).to_numpy(); hi = e["e_hi60"].fillna(0).to_numpy()
    tod = e["tod"].to_numpy(); pr = e["entry"].to_numpy(); cv = e["cumval"].fillna(0).to_numpy()
    for a, b, c, d, f, g in itertools.product([9, 0.17, 0.06], [9, 0.18, 0.09], [9, -0.16, -0.26], [999, 60, 30, 3],
                                              [1e9, 7000, 4000], [1e12, 3500]):
        m = m0 & (r20 <= a) & (pm <= b) & (hi <= c) & (tod <= d) & (pr <= f) & (cv <= g)
        nA, nB = int((m & hA).sum()), int((m & ~hA).sum())
        if nA < 60 or nB < 60: continue
        row = {"set": name, "ret20d<=": a, "pc_ma20<=": b, "e_hi60<=": c, "tod<=": d, "price<=": f, "cumval<=": g,
               "nA": nA, "nB": nB}
        for rl, ref in refs.items():
            row[f"{rl}|A"] = ref[m & hA].mean() * 100
            row[f"{rl}|B"] = ref[m & ~hA].mean() * 100
        out.append(row)
t = pd.DataFrame(out)
rl = [r.label() for r in RULES]
for x in rl:
    t[f"{x}|min"] = t[[f"{x}|A", f"{x}|B"]].min(axis=1)
t["best_min"] = t[[f"{x}|min" for x in rl]].max(axis=1)
t["best_rule"] = t[[f"{x}|min" for x in rl]].idxmax(axis=1).str.replace("|min", "")
t.to_csv(DATA / "study5_combos.csv", index=False)
cols = ["set", "ret20d<=", "pc_ma20<=", "e_hi60<=", "tod<=", "price<=", "cumval<=", "nA", "nB", "best_rule", "best_min"]
for s in ("events_m1", "events_j"):
    print("=====", s, " 조합 수", (t.set == s).sum(), " 두 기간 모두 플러스:", int(((t.set == s) & (t.best_min > 0)).sum()))
    print(t[t.set == s].sort_values("best_min", ascending=False)[cols].head(15).round(2).to_string(index=False))
