import sys
sys.path.insert(0, '.')
from study import *
from analyze import buckets
pd.set_option('display.width', 250)
o, paths = load_set("events_j", "j_status")
e = o["events"]
m = base_mask(o, "j_ok")
idx = np.nonzero(m)[0]
px, key, rs = evaluate(paths, e["entry"].to_numpy(), Rule(tp=0.08, sl=0.03, eod="D0_1515"), idx=idx)
ref = net_return(e["entry"].to_numpy(), px, Cost())
eodg = e["eod"].to_numpy()
e["cumval_eok"] = e["cumval"] / 100
e["price"] = e["entry"]
feats = ["tod", "ret_entry", "gap", "run_open", "cumval_eok", "vol_prev_ratio", "burst", "pc_ma5", "pc_ma20", "pc_ma60",
         "ret1d", "ret5d", "ret20d", "mcap", "e_hi60", "mkt_kq", "price", "strength_est", "j_delay"]
for f in feats:
    if f not in e: continue
    b = buckets(e, ref, f, m, q=5)
    b2 = buckets(e, eodg, f, m, q=5)
    if b.empty: continue
    out = pd.DataFrame({"n": b["count_A"] + b["count_B"], "netA": b["mean_A"]*100, "netB": b["mean_B"]*100,
                        "net": b["mean_all"]*100, "eodGross": b2["mean_all"]*100}).round(2)
    print("==", f); print(out.to_string())
