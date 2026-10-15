"""주간 재최적화 전체 흐름 (토요일 예약 작업이 실행). 조회 TR만 쓰고 주문은 하지 않는다.

    analysis\\.venv-ana\\Scripts\\python analysis\\weekly_update.py [--no-apply] [--skip-fetch]

1) 일봉·특성 갱신 (금요일 저녁 준비 작업이 이미 했으면 건너뜀)
2) 5분봉 증분 갱신 (전 종목, 약 40~50분)
3) 최근 40거래일 조건 편입 이벤트 계산
4) 장 초반(09:30 전) 이벤트의 1분봉 추가 수집
5) 1분봉 정밀 보정
6) 전략 후보 평가 → 기준 통과 시 data/strategy/strategy.json 교체 (optimize.py)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import logging
import os
import pickle
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANA = ROOT / "analysis"
RESEARCH = ROOT / "data" / "research"
STRATEGY_DIR = ROOT / "data" / "strategy"
PY = sys.executable  # analysis\.venv-ana
WINDOW_DAYS = 40
EARLY_MIN = 30  # 1분봉을 받을 이벤트: 09:30 전

log = logging.getLogger("weekly")


def run(cmd: list[str], what: str) -> None:
    log.info("▶ %s", what)
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    t0 = time.monotonic()
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in (p.stdout or "").splitlines()[-15:]:
        log.info("   %s", line)
    if p.returncode != 0:
        for line in (p.stderr or "").splitlines()[-20:]:
            log.error("   %s", line)
        raise RuntimeError(f"{what} 실패 (종료코드 {p.returncode})")
    log.info("  %s 완료 (%.0f분)", what, (time.monotonic() - t0) / 60)


def trading_days() -> list[str]:
    with gzip.open(RESEARCH / "m5" / "069500.csv.gz", "rt", encoding="utf-8") as f:
        return sorted({r["date"] for r in csv.DictReader(f)})


def features_fresh() -> bool:
    p = STRATEGY_DIR / "daily_features.csv"
    if not p.is_file():
        return False
    age_h = (time.time() - p.stat().st_mtime) / 3600
    return age_h < 30 and datetime.fromtimestamp(p.stat().st_mtime).hour >= 16


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-apply", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true", help="데이터 수집 없이 계산만 (시험용)")
    ap.add_argument("--force", action="store_true", help="평일 장중에도 실행")
    a = ap.parse_args(argv)
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    (STRATEGY_DIR / "history").mkdir(exist_ok=True)
    logfile = STRATEGY_DIR / "history" / f"weekly_{datetime.now():%Y%m%d_%H%M}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.FileHandler(logfile, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
    now = datetime.now()
    if not a.force and now.weekday() < 5 and "0830" <= now.strftime("%H%M") < "1545":
        log.error("평일 장중에는 실행하지 않습니다 (자동매매와 같은 키를 씀). --force 로 강제 실행 가능")
        return 2
    bot_py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    try:
        if not a.skip_fetch:
            if features_fresh():
                log.info("일봉·특성은 최근 준비 작업 결과를 사용 (갱신 생략)")
            else:
                run([bot_py, "-m", "autotrader.prep_daily"], "일봉·특성 갱신")
            run([PY, str(ANA / "fetch_incremental.py")], "5분봉 증분 갱신")
        days = trading_days()
        window = days[-WINDOW_DAYS:]
        log.info("평가 구간 %s ~ %s (%d거래일)", window[0], window[-1], len(window))
        run([PY, str(ANA / "run_events.py"), "--from", window[0], "--to", window[-1],
             "--out", str(RESEARCH / "events.pkl")], "조건 편입 이벤트 계산")
        with open(RESEARCH / "events.pkl", "rb") as f:
            ev = pickle.load(f)["events"]
        early = ev[(ev["est_min"] >= 540) & (ev["est_min"] < 540 + EARLY_MIN)]
        pairs = early[["code", "date"]].drop_duplicates()
        need = [(c, str(d)) for c, d in pairs.itertuples(index=False)
                if not (RESEARCH / "m1" / f"{c}_{d}.csv.gz").exists()]
        log.info("장 초반 이벤트 %d건, 1분봉 필요 %d개 (이미 있음 %d)", len(early), len(need), len(pairs) - len(need))
        if need and not a.skip_fetch:
            pf = RESEARCH / "m1_pairs_weekly.csv"
            with pf.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["code", "date"])
                w.writerows(need)
            run([PY, str(ANA / "fetch_1m.py"), "--pairs", str(pf), "--out", str(RESEARCH / "m1"),
                 "--deadline", "0830"], "1분봉 수집")
        run([PY, str(ANA / "refine_m1.py"), "--events", str(RESEARCH / "events.pkl")], "1분봉 정밀 보정")
        for p in RESEARCH.glob("paths_*.pkl"):
            p.unlink()  # 이벤트가 바뀌었으므로 경로 캐시 삭제
        cmd = [PY, str(ANA / "optimize.py"), "--events", str(RESEARCH / "events_m1.pkl")]
        if a.no_apply:
            cmd.append("--no-apply")
        run(cmd, "전략 후보 평가·결정")
        # 정리: 구간보다 오래된 1분봉 삭제
        cut = window[0]
        old = [p for p in (RESEARCH / "m1").glob("*.csv.gz") if p.stem.split("_")[-1].split(".")[0] < cut]
        for p in old:
            p.unlink()
        log.info("오래된 1분봉 %d개 삭제", len(old))
    except Exception as e:  # noqa: BLE001
        log.exception("주간 재최적화 실패: %s (전략 파일은 바뀌지 않았습니다)", e)
        return 1
    log.info("주간 재최적화 끝. 보고서: %s", STRATEGY_DIR / "latest_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
