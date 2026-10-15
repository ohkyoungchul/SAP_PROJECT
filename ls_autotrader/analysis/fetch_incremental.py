"""5분봉 증분 갱신 (주간 재최적화용, 조회 TR만 / 주문 없음).

종목별 파일의 마지막 날짜부터 오늘까지만 받아 붙이고, 오래된 봉은 잘라낸다.
t8412 와 t8452 두 TR 을 스레드 2개로 나눠 쓴다 (TR별 초당 1건 한도).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import logging
import queue
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_2m import M5_FIELDS, MARKET_PROXIES, fetch_flags, fetch_m5, write_csv  # noqa: E402  (TR_SPEC 갱신 포함)
from autotrader.config import Settings, load_env_file  # noqa: E402
from autotrader.ls_api import LsApiError, LsRestClient  # noqa: E402
from autotrader.prep_daily import common_stocks  # noqa: E402

log = logging.getLogger("fetch_inc")


def read_m5(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--research-dir", default="data/research")
    ap.add_argument("--keep-days", type=int, default=75, help="남길 거래일 수 (지표 계산 여유 포함)")
    ap.add_argument("--first-start", default="", help="파일이 없는 종목의 시작일 (기본: 오늘-120일)")
    a = ap.parse_args(argv)
    research = Path(a.research_dir)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(threadName)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_env_file(Path(".env"))
    s = Settings.from_env()
    import autotrader.ls_api as ls_api
    ls_api.QUERY_TIMEOUT_SEC = 30.0
    c = LsRestClient(s.appkey, s.appsecret, s.rest_base, label="모의투자", token_cache_dir=s.data_dir / "tokens",
                     timeout=30.0)
    uni = common_stocks(c, research)
    try:
        fetch_flags(c, research)
    except LsApiError as e:
        log.warning("관리/경고 목록 갱신 실패 (이전 목록 사용): %s", e)
    codes = [u["code"] for u in uni] + MARKET_PROXIES
    today = datetime.now().strftime("%Y%m%d")
    first_start = a.first_start or (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
    m5_dir = research / "m5"
    q: queue.Queue = queue.Queue()
    for code in codes:
        q.put(code)
    lock = threading.Lock()
    stats = {"done": 0, "fail": 0, "new": 0}
    t0 = time.monotonic()

    def worker(tr: str) -> None:
        while True:
            try:
                code = q.get_nowait()
            except queue.Empty:
                return
            p = m5_dir / f"{code}.csv.gz"
            old = read_m5(p)
            start = old[-1]["date"] if old else first_start  # 마지막 날은 다시 받아 덮어씀
            try:
                new = fetch_m5(c, tr, code, start, today)
            except LsApiError as e:
                with lock:
                    stats["fail"] += 1
                log.warning("%s 5분봉 갱신 실패: %s", code, e)
                continue
            merged = {(r["date"], r["time"]): r for r in old if r["date"] < start}
            for r in new:
                merged[(str(r["date"]), str(r["time"]))] = r
            rows = [merged[k] for k in sorted(merged)]
            days = sorted({r["date"] for r in rows})
            if len(days) > a.keep_days:
                cut = days[-a.keep_days]
                rows = [r for r in rows if str(r["date"]) >= cut]
            write_csv(p, M5_FIELDS, rows, gz=True)
            with lock:
                stats["done"] += 1
                stats["new"] += int(not old)
                if stats["done"] % 300 == 0:
                    el = time.monotonic() - t0
                    log.info("5분봉 %d/%d (실패 %d), 남은 예상 %.0f분", stats["done"], len(codes), stats["fail"],
                             el / stats["done"] * (len(codes) - stats["done"]) / 60)

    ts = [threading.Thread(target=worker, args=(tr,), name=tr) for tr in ("t8412", "t8452")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    log.info("5분봉 증분 갱신 끝: %s", stats)
    return 0 if stats["fail"] < len(codes) * 0.05 else 1


if __name__ == "__main__":
    sys.exit(main())
