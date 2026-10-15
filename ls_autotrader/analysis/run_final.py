import sys, json
sys.path.insert(0, '.')
from study import *
pd.set_option('display.width', 250)
o, paths = load_set("events_m1", "m1_status")
e = o["events"]
m0 = base_mask(o) & (e["m1_status"] == "ok").to_numpy()
E = e["entry"].to_numpy(); tod = e["tod"].to_numpy(); pm = e["pc_ma20"].fillna(0).to_numpy(); hi = e["e_hi60"].fillna(0).to_numpy()
filt = m0 & (E >= 1000) & (E <= 4000) & (pm <= 0.09) & (hi <= -0.16)
configs = {
    "현재 설정 (필터 없음, 09:00~15:15, 익절10/손절3 보유)": (m0, 915, Rule(tp=0.10, sl=0.03, eod="H")),
    "필터 + 매수 09:00~09:05, 익절10/손절3 보유": (filt, 545, Rule(tp=0.10, sl=0.03, eod="H")),
    "필터 + 09:05까지, 익절8/손절3 보유 [추천]": (filt, 545, Rule(tp=0.08, sl=0.03, eod="H")),
    "필터 + 09:05까지, 익절7/손절3 보유": (filt, 545, Rule(tp=0.07, sl=0.03, eod="H")),
    "필터 + 09:05까지, 익절9/손절3 보유": (filt, 545, Rule(tp=0.09, sl=0.03, eod="H")),
    "필터 + 09:05까지, 익절8/손절4 보유": (filt, 545, Rule(tp=0.08, sl=0.04, eod="H")),
    "필터 + 09:05까지, 익절8/손절3 + 15:15 청산(코드 수정 필요)": (filt, 545, Rule(tp=0.08, sl=0.03, eod="D0_1515")),
}
rows, curves = [], {}
for name, (m, be, rule) in configs.items():
    idx = np.nonzero(m)[0]
    px, key, rs = evaluate(paths, E, rule, idx=idx)
    res = {}
    for h, hm in (("전체", np.ones(len(e), bool)), ("전반", (e["half"] == "A").to_numpy()), ("후반", (e["half"] == "B").to_numpy())):
        r = portfolio(o, mask=m & hm, exits=(px, key, rs), cost=Cost(), buy_end=be, keep_trades=(h == "전체"))
        res[h] = r
    t = res["전체"]["trades"]
    daily = t.groupby("sell_date")["pnl"].sum().sort_index()
    curves[name] = {str(k): float(v) for k, v in daily.cumsum().items()}
    rows.append({"설정": name, "거래": res["전체"]["n"], "손익": round(res["전체"]["pnl"]), "전반": round(res["전반"]["pnl"]),
                 "후반": round(res["후반"]["pnl"]), "건당%": round(res["전체"]["avg_ret"] * 100, 2),
                 "승률%": round(res["전체"]["win"] * 100, 1), "최대낙폭": round(res["전체"]["mdd"]),
                 "익절": res["전체"]["tp"], "손절": res["전체"]["sl"], "미청산": res["전체"]["open"],
                 "보유일평균": round(res["전체"]["avg_hold_days"], 2), "자리부족": res["전체"]["skipped"].get("max_pos", 0)})
df = pd.DataFrame(rows)
print(df.to_string(index=False))
df.to_csv(DATA / "final_configs.csv", index=False, encoding="utf-8-sig")
json.dump(curves, open(DATA / "final_curves.json", "w", encoding="utf-8"), ensure_ascii=False)
