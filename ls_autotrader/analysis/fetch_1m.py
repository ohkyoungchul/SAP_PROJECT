"""조건 편입이 있었던 (종목, 날짜)의 1분봉 수집 (조회 TR만, 주문 없음).

    set PYTHONPATH=.
    .venv\\Scripts\\python analysis\\fetch_1m.py --pairs data\\research\\m1_pairs.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_2m import write_csv  # noqa: E402  (TR_SPEC 갱신 포함)
from autotrader import ls_api  # noqa: E402
from autotrader.config import Settings, load_env_file  # noqa: E402
from autotrader.ls_api import KST, LsApiError, LsRestClient, clean_str, to_int  # noqa: E402

log = logging.getLogger("fetch1m")
FIELDS = ["date", "time", "open", "high", "low", "close", "volume"]


def fetch_day(c: LsRestClient, tr: str, code: str, day: str) -> list[dict]:
    bars: dict[str, dict] = {}
    cts_date = cts_time = ""
    tr_cont, key = "N", ""
    for _ in range(10):
        blk = {"shcode": code, "ncnt": 1, "qrycnt": 500, "nday": "0", "sdate": day, "stime": "", "edate": day,
               "etime": "", "cts_date": cts_date, "cts_time": cts_time, "comp_yn": "N"}
        if tr == "t8452":
            blk["exchgubun"] = "K"
        data, headers = c.call(tr, {f"{tr}InBlock": blk}, tr_cont=tr_cont, tr_cont_key=key)
        page = data.get(f"{tr}OutBlock1") or []
        for r in page:
            d, t = clean_str(r.get("date")), clean_str(r.get("time"))
            if d == day and t <= "153000":
                bars[t] = {"date": d, "time": t, "open": to_int(r.get("open")), "high": to_int(r.get("high")),
                           "low": to_int(r.get("low")), "close": to_int(r.get("close")),
                           "volume": to_int(r.get("jdiff_vol"))}
        ob = data.get(f"{tr}OutBlock") or {}
        nd, nt = clean_str(ob.get("cts_date")), clean_str(ob.get("cts_time"))
        if not page or not nd or nd < day or (nd, nt) == (cts_date, cts_time) \
                or ls_api._header(headers, "tr_cont") != "Y":
            break
        cts_date, cts_time = nd, nt
        tr_cont, key = "Y", ls_api._header(headers, "tr_cont_key")
    return [bars[k] for k in sorted(bars)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--out", default="data/research/m1")
    ap.add_argument("--deadline", default="0835")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(threadName)s %(message)s",
                        handlers=[logging.FileHandler(out.parent / "fetch_1m.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_env_file(Path(".env"))
    s = Settings.from_env()
    c = LsRestClient(s.appkey, s.appsecret, s.rest_base, label="모의투자", token_cache_dir=s.data_dir / "tokens")
    with open(a.pairs, encoding="utf-8") as f:
        pairs = [(r["code"], r["date"]) for r in csv.DictReader(f)]
    todo = [(cd, d) for cd, d in pairs if not (out / f"{cd}_{d}.csv.gz").exists()]
    log.info("1분봉 대상 %d (남은 것 %d)", len(pairs), len(todo))
    q: queue.Queue = queue.Queue()
    for x in todo:
        q.put(x)
    lock = threading.Lock()
    done = [0]
    t0 = time.monotonic()

    def past_deadline() -> bool:
        now = datetime.now(KST)
        return now.hour < 12 and now.strftime("%H%M") >= a.deadline

    def worker(tr: str) -> None:
        while not past_deadline():
            try:
                cd, d = q.get_nowait()
            except queue.Empty:
                return
            try:
                write_csv(out / f"{cd}_{d}.csv.gz", FIELDS, fetch_day(c, tr, cd, d), gz=True)
            except LsApiError as e:
                log.warning("%s %s 실패: %s", cd, d, e)
            with lock:
                done[0] += 1
                if done[0] % 200 == 0 or done[0] == len(todo):
                    el = time.monotonic() - t0
                    log.info("1분봉 %d/%d, 남은 예상 %.0f분", done[0], len(todo),
                             el / done[0] * (len(todo) - done[0]) / 60)

    ts = [threading.Thread(target=worker, args=(tr,), name=tr) for tr in ("t8412", "t8452")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    log.info("끝: %d/%d", done[0], len(todo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
