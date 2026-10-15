"""주간 재최적화: 최근 구간 데이터로 전략 후보를 평가하고, 기준을 통과하면 strategy.json 을 바꾼다.

    analysis\\.venv-ana\\Scripts\\python analysis\\optimize.py --events data\\research\\events_m1.pkl [--no-apply]

판단 방식
- 후보 = 매수 마감 × 최고가 × 이격도 상한 × 60일 고가 대비 상한 × 익절 × 손절 격자
- 각 후보를 봇 규칙(1회 금액, 최대 보유 종목, 당일 재매수 없음, 익절/손절 보유)으로 구간 전반·후반 따로 시뮬레이션
- 기준(모두 만족): 전반·후반 모두 플러스 / 반기별 거래 ≥ MIN_TRADES_HALF / 매수 미끄러짐 1% 가정에서도 건당 평균 플러스
                 / 최대낙폭 ≥ -MAX_DD_UNITS × 1회 주문금액
- 점수 = min(전반, 후반 손익), 이웃 격자 점수와 평균 내어 고립된 봉우리(과최적화)를 피한다
- 교체 조건: 기준 통과 후보가 있고, (현재 전략이 기준 미달이거나 새 후보 점수가 현재보다 SWITCH_MARGIN 이상 좋음)
- 그 외에는 현재 전략 유지. 매번 보고서를 남긴다.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from analyze import prep  # noqa: E402
from rules import Rule, build_paths, evaluate  # noqa: E402
from sim import Cost, net_return, portfolio  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GRID = {
    "buy_end_min": [3, 5, 10, 15, 30],
    "max_price": [3000, 4000, 5000, 7000, 10000],
    "max_disparity20": [105, 109, 115, 125, None],
    "max_from_high60": [-25, -16, -10, None],
    "tp": [6, 7, 8, 9, 10],
    "sl": [2, 3, 4],
}
MIN_PRICE = 1000
MIN_TRADES_HALF = 20
MAX_DD_UNITS = 1.5  # 최대낙폭 한도 = 1.5 × 1회 주문금액
STRESS_SLIP = 0.010
SCORE_COST = Cost(slip_b=0.006)  # 점수·기준은 보수적 비용(장 시작 직후 매수 미끄러짐 0.6%)으로 계산
SWITCH_MARGIN = 1.15  # 새 후보 점수가 현재의 1.15배 이상이어야 교체 (잦은 교체 방지)
SWITCH_MIN_GAIN = 10_000  # 그리고 차이가 1만원 이상 (1회 10만원 기준, 금액에 비례)


def event_features(e: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "price": e["entry"].to_numpy(float),
        "ret_entry_pct": e["ret_entry"].to_numpy(float) * 100,
        "disparity20": (1 + e["pc_ma20"].to_numpy(float)) * 100,
        "from_high60_pct": e["e_hi60"].to_numpy(float) * 100,
        "ret1d_pct": e["ret1d"].to_numpy(float) * 100,
        "ret5d_pct": e["ret5d"].to_numpy(float) * 100,
        "ret20d_pct": e["ret20d"].to_numpy(float) * 100,
    }


def hhmm_to_tod(s: str) -> float:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm) - 540


def strategy_mask(feats: dict, tod: np.ndarray, strat: dict) -> tuple[np.ndarray, float, float]:
    """전략 파일 내용 → (이벤트 마스크, 매수 시작 tod, 매수 마감 tod)."""
    tr = strat.get("trade", {})
    t0 = hhmm_to_tod(tr.get("buy_start", "09:00"))
    t1 = hhmm_to_tod(tr.get("buy_end", "15:15"))
    m = (tod >= t0) & (tod < t1)
    if strat.get("enabled", True):
        for r in strat.get("filters", []):
            v = feats[r["feature"]]
            ok = np.isfinite(v)
            if r.get("min") is not None:
                ok &= v >= r["min"]
            if r.get("max") is not None:
                ok &= v <= r["max"]
            m &= ok
    return m, t0, t1


class Evaluator:
    def __init__(self, o: dict, paths: list, base: np.ndarray, buy_amount: int, max_pos: int):
        self.o, self.paths, self.base = o, paths, base
        self.e = o["events"]
        self.E = self.e["entry"].to_numpy(float)
        self.hA = (self.e["half"] == "A").to_numpy()
        self.buy_amount, self.max_pos = buy_amount, max_pos
        self.idx = np.nonzero(base)[0]
        self._exits: dict = {}

    def exits(self, tp: float, sl: float):
        k = (tp, sl)
        if k not in self._exits:
            px, key, rs = evaluate(self.paths, self.E, Rule(tp=tp / 100, sl=sl / 100, eod="H"), idx=self.idx)
            stress = net_return(self.E, px, Cost(slip_b=STRESS_SLIP))
            self._exits[k] = (px, key, rs, stress)
        return self._exits[k]

    def run(self, mask: np.ndarray, tp: float, sl: float, buy_end_tod: float, buy_start_tod: float = 0) -> dict:
        mask = mask & self.base
        px, key, rs, stress = self.exits(tp, sl)
        out = {}
        for h, hm in (("A", self.hA), ("B", ~self.hA)):
            r = portfolio(self.o, mask=mask & hm, exits=(px, key, rs), cost=SCORE_COST, buy_amount=self.buy_amount,
                          max_pos=self.max_pos, buy_start=540 + buy_start_tod, buy_end=540 + buy_end_tod)
            out[f"pnl_{h}"] = r["pnl"]
            out[f"n_{h}"] = r["n"]
            out[f"mdd_{h}"] = r.get("mdd", 0.0)
            out[f"avg_{h}"] = r.get("avg_ret", np.nan)
            out[f"win_{h}"] = r.get("win", np.nan)
        m_all = mask & (self.e["tod"].to_numpy() >= buy_start_tod) & (self.e["tod"].to_numpy() < buy_end_tod)
        out["stress_avg"] = float(np.nanmean(stress[m_all])) if m_all.any() else np.nan
        out["n_events"] = int(m_all.sum())
        out["score"] = min(out["pnl_A"], out["pnl_B"])
        out["pnl"] = out["pnl_A"] + out["pnl_B"]
        out["mdd"] = min(out["mdd_A"], out["mdd_B"])
        return out


def guardrails(r: dict, buy_amount: int) -> dict[str, bool]:
    return {
        "전반 플러스": r["pnl_A"] > 0,
        "후반 플러스": r["pnl_B"] > 0,
        f"반기별 거래 {MIN_TRADES_HALF}건 이상": min(r["n_A"], r["n_B"]) >= MIN_TRADES_HALF,
        f"미끄러짐 {STRESS_SLIP * 100:.0f}%에서도 건당 플러스": np.isfinite(r["stress_avg"]) and r["stress_avg"] > 0,
        f"최대낙폭 {MAX_DD_UNITS:g}×주문금액 이내": r["mdd"] >= -MAX_DD_UNITS * buy_amount,
    }


def to_strategy(p: dict, name: str, evidence: dict) -> dict:
    filters = [{"feature": "price", "min": MIN_PRICE, "max": p["max_price"]}]
    if p["max_disparity20"] is not None:
        filters.append({"feature": "disparity20", "max": p["max_disparity20"]})
    if p["max_from_high60"] is not None:
        filters.append({"feature": "from_high60_pct", "max": p["max_from_high60"]})
    end = 540 + p["buy_end_min"]
    return {"name": name, "enabled": True, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": "주간 재최적화(analysis/optimize.py)가 기준 통과로 자동 적용",
            "trade": {"take_profit_pct": p["tp"], "stop_loss_pct": p["sl"], "buy_start": "09:00",
                      "buy_end": f"{end // 60:02d}:{end % 60:02d}"},
            "filters": filters, "evidence": evidence}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(ROOT / "data" / "research" / "events_m1.pkl"))
    ap.add_argument("--strategy", default=str(ROOT / "data" / "strategy" / "strategy.json"))
    ap.add_argument("--history", default=str(ROOT / "data" / "strategy" / "history"))
    ap.add_argument("--buy-amount", type=int, default=int(os.environ.get("BUY_AMOUNT", "100000")))
    ap.add_argument("--max-pos", type=int, default=int(os.environ.get("MAX_POSITIONS", "10")))
    ap.add_argument("--no-apply", action="store_true", help="평가·보고서만 만들고 전략 파일은 바꾸지 않음")
    a = ap.parse_args(argv)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    hist = Path(a.history) / stamp
    hist.mkdir(parents=True, exist_ok=True)

    o = prep(a.events)
    e = o["events"]
    days = sorted(e["date"].unique())
    split = days[len(days) // 2]
    e["half"] = np.where(e["date"] < split, "A", "B")
    max_end = max(GRID["buy_end_min"])
    base = ((e["m1_status"] == "ok").to_numpy() & (e["tod"].to_numpy() < max_end)
            & (e["entry"].to_numpy() <= a.buy_amount / 1.003) & (e["tod"].to_numpy() >= 0))
    sub_idx = np.nonzero(base)[0]
    print(f"구간 {days[0]}~{days[-1]} ({len(days)}거래일, 전반/후반 경계 {split}), 후보 이벤트 {len(sub_idx)}", flush=True)
    paths = [None] * len(e)
    sub = e.iloc[sub_idx].reset_index(drop=True)
    for k, pth in zip(sub_idx, build_paths(sub, ok_col="m1_status")):
        paths[k] = pth
    ev = Evaluator(o, paths, base, a.buy_amount, a.max_pos)
    feats = event_features(e)
    tod = e["tod"].to_numpy()

    # 1) 현재 전략
    cur = json.loads(Path(a.strategy).read_text(encoding="utf-8-sig")) if Path(a.strategy).is_file() else None
    cur_res = None
    if cur:
        m, t0, t1 = strategy_mask(feats, tod, cur)
        tr = cur.get("trade", {})
        cur_res = ev.run(m, float(tr.get("take_profit_pct", 8)), float(tr.get("stop_loss_pct", 3)), t1, t0)
        cur_res["guard"] = guardrails(cur_res, a.buy_amount)
        cur_res["pass"] = all(cur_res["guard"].values())

    # 2) 격자
    keys = list(GRID)
    rows = []
    for vals in itertools.product(*(GRID[k] for k in keys)):
        p = dict(zip(keys, vals))
        m = (feats["price"] >= MIN_PRICE) & (feats["price"] <= p["max_price"])
        if p["max_disparity20"] is not None:
            m &= feats["disparity20"] <= p["max_disparity20"]
        if p["max_from_high60"] is not None:
            m &= feats["from_high60_pct"] <= p["max_from_high60"]
        r = ev.run(m, p["tp"], p["sl"], p["buy_end_min"])
        g = guardrails(r, a.buy_amount)
        rows.append({**p, **r, "pass": all(g.values()), "fails": ", ".join(k for k, v in g.items() if not v)})
    t = pd.DataFrame(rows)
    # 이웃 평균 점수 (한 축만 한 칸 다른 격자)
    pos = {k: {v: i for i, v in enumerate(GRID[k])} for k in keys}
    score = {tuple(pos[k][r[k]] for k in keys): r["score"] for r in rows}
    smooth = []
    for r in rows:
        ix = [pos[k][r[k]] for k in keys]
        nb = []
        for d in range(len(keys)):
            for step in (-1, 1):
                j = list(ix)
                j[d] += step
                if tuple(j) in score:
                    nb.append(score[tuple(j)])
        smooth.append(0.5 * r["score"] + 0.5 * (np.mean(nb) if nb else r["score"]))
    t["smooth"] = smooth
    t.to_csv(hist / "grid.csv", index=False, encoding="utf-8-sig")
    passing = t[t["pass"]].sort_values("smooth", ascending=False)

    # 3) 결정
    unit = a.buy_amount / 100_000
    decision, chosen = "유지", None
    if len(passing):
        best = passing.iloc[0]
        cur_score = cur_res["score"] if cur_res else -np.inf
        cur_pass = bool(cur_res and cur_res["pass"])
        better = best["score"] >= max(cur_score * SWITCH_MARGIN, cur_score + SWITCH_MIN_GAIN * unit) \
            if cur_score > 0 else True
        if not cur_pass or better:
            chosen = best
            decision = "교체"
    reason = ("기준 통과 후보 없음" if not len(passing) else
              "현재 전략이 기준 미달" if chosen is not None and not (cur_res and cur_res["pass"]) else
              "새 후보가 충분히 더 좋음" if chosen is not None else "현재 전략이 기준 통과이고 새 후보와 차이가 작음")

    # 4) 보고서 / 적용
    def fmt(r) -> str:
        return (f"손익 {r['pnl']:+,.0f}원 (전반 {r['pnl_A']:+,.0f} / 후반 {r['pnl_B']:+,.0f}), 거래 {r['n_A']}+{r['n_B']}건, "
                f"건당 {np.nanmean([r['avg_A'], r['avg_B']]) * 100:+.2f}%, 최대낙폭 {r['mdd']:,.0f}원, "
                f"미끄러짐1% 건당 {r['stress_avg'] * 100:+.2f}%")
    lines = [f"# 주간 재최적화 보고서 ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"- 구간: {days[0]} ~ {days[-1]} ({len(days)}거래일), 전반/후반 경계 {split}",
             f"- 1회 주문금액 {a.buy_amount:,}원, 최대 {a.max_pos}종목, 비용(보수적): 매수 미끄러짐 {SCORE_COST.slip_b * 100:.1f}%·매도 "
             f"{SCORE_COST.slip_s * 100:.1f}%·수수료 {SCORE_COST.fee * 100:.3f}%×2·세금 {SCORE_COST.tax * 100:.2f}%",
             "- 체결강도(J) 조건은 과거 장중 값을 구할 수 없어 시뮬레이션에 없음 (실제 서버는 적용)", "",
             f"## 결정: **{decision}** ({reason})", ""]
    if cur_res:
        lines += [f"### 현재 전략: {cur.get('name')}", fmt(cur_res),
                  "기준: " + ", ".join(f"{k} {'O' if v else 'X'}" for k, v in cur_res["guard"].items()), ""]
    lines += [f"### 기준 통과 후보 {len(passing)}개 / 전체 {len(t)}개 — 상위 10", "",
              "| 매수마감 | 최고가 | 이격도≤ | 60일고가대비≤ | 익절 | 손절 | 손익 | 전반 | 후반 | 거래 | 점수(이웃평균) |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for _, r in passing.head(10).iterrows():
        lines.append(f"| 09:{r['buy_end_min']:02d} | {r['max_price']:,} | {r['max_disparity20'] if pd.notna(r['max_disparity20']) else '-'} "
                     f"| {r['max_from_high60'] if pd.notna(r['max_from_high60']) else '-'} | {r['tp']} | {r['sl']} "
                     f"| {r['pnl']:+,.0f} | {r['pnl_A']:+,.0f} | {r['pnl_B']:+,.0f} | {r['n_A'] + r['n_B']} | {r['smooth']:,.0f} |")
    if chosen is not None:
        def native(v):
            if v is None or (isinstance(v, (float, np.floating)) and np.isnan(v)):
                return None
            if isinstance(v, np.integer):
                return int(v)
            if isinstance(v, (float, np.floating)):
                return int(v) if float(v).is_integer() else float(v)
            return v
        p = {k: native(chosen[k]) for k in keys}
        evidence = {"window": f"{days[0]}-{days[-1]}", "trades": int(chosen["n_A"] + chosen["n_B"]),
                    "pnl": round(float(chosen["pnl"])), "pnl_first_half": round(float(chosen["pnl_A"])),
                    "pnl_second_half": round(float(chosen["pnl_B"])), "stress_avg_pct": round(float(chosen["stress_avg"]) * 100, 2),
                    "mdd": round(float(chosen["mdd"]))}
        new = to_strategy(p, f"auto-{datetime.now():%Y%m%d}", evidence)
        lines += ["", "### 새 전략", "```json", json.dumps(new, ensure_ascii=False, indent=2), "```"]
        (hist / "strategy_new.json").write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
        if a.no_apply:
            lines.append("(--no-apply: 전략 파일은 바꾸지 않음)")
        else:
            sp = Path(a.strategy)
            if sp.is_file():
                shutil.copy2(sp, hist / "strategy_prev.json")
            tmp = sp.with_name(sp.name + ".tmp")
            tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, sp)
            lines.append(f"→ {sp} 에 적용함 (이전 파일: history/{stamp}/strategy_prev.json). 봇은 다음 날짜 변경 때 읽음.")
    elif not len(passing) and cur_res and not cur_res["pass"]:
        lines += ["", "⚠ 현재 전략도 기준 미달이고 통과 후보도 없습니다. 기존 전략을 유지하지만 매매 중단·DRY_RUN 전환을 검토하세요."]
    report = "\n".join(lines) + "\n"
    (hist / "report.md").write_text(report, encoding="utf-8")
    (Path(a.strategy).parent / "latest_report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
