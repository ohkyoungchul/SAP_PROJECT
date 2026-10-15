import sys
sys.path.insert(0, '.')
from study import *
from analyze import buckets
pd.set_option('display.width', 250)
FEATS = ["tod", "run_open", "ret_entry", "gap", "ret1d", "ret5d", "ret20d", "pc_ma5", "pc_ma20", "pc_ma60", "e_hi60",
         "cumval", "vol_prev_ratio", "mcap", "price", "burst_frac", "str_frac", "mkt_kq"]
res = []
for name, ok_col, jcol in (("events_m1", "m1_status", None), ("events_j", "j_status", "j_ok")):
    o, paths = load_set(name, ok_col)
    e = o["events"]
    cf = pd.read_pickle(DATA / f"clean_feats_{name}.pkl")
    for c in cf: e[c] = cf[c].astype(float)
    e["price"] = e["entry"]
    m = base_mask(o, jcol)
    if jcol is None: m &= (e["m1_status"] == "ok").to_numpy()
    idx = np.nonzero(m)[0]
    refs = {}
    for rule in (Rule(tp=0.08, sl=0.03, eod="D0_1515"), Rule(tp=0.10, sl=0.03, eod="H")):
        px, k, rs = evaluate(paths, e["entry"].to_numpy(), rule, idx=idx)
        refs[rule.label()] = net_return(e["entry"].to_numpy(), px, Cost())
    for f in FEATS:
        for rl, ref in refs.items():
            b = buckets(e, ref, f, m, q=5)
            if b.empty: continue
            for bi, (lab, row) in enumerate(b.iterrows()):
                res.append({"set": name, "rule": rl, "feat": f, "q": bi + 1, "range": str(lab),
                            "A": row["mean_A"] * 100, "B": row["mean_B"] * 100, "all": row["mean_all"] * 100})
r = pd.DataFrame(res)
r.to_csv(DATA / "study4_buckets.csv", index=False)
# 일관성: 두 세트·두 규칙 모두에서 최상/최하 구간
piv = r.pivot_table(index=["feat", "q"], columns=["set", "rule"], values="all").round(2)
print(piv.to_string())
