"""2개월 백테스트용 데이터 수집 (조회 TR만 사용, 주문 없음).

사용법 (Stock_Trading 폴더에서):
    set PYTHONPATH=.
    .venv\\Scripts\\python analysis\\fetch_2m.py --start 20260811 --end 20261013

- 5분봉은 t8412 와 t8452(거래소구분 K) 두 TR 을 스레드 2개로 나눠 받는다.
  (TR별 초당 1건 한도라 두 TR 을 번갈아 쓰면 초당 약 2건)
- 일봉(체결강도·거래대금 포함)은 t1305 로 따로 받는다.
- 이미 받은 파일은 건너뛰므로 끊겨도 다시 실행하면 이어서 받는다.
- --deadline 시각이 지나면 새 종목을 받지 않고 끝낸다 (자동매매 시작 전에 끝내기 위함).
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
from datetime import datetime
from pathlib import Path

from autotrader import ls_api
from autotrader.config import Settings, load_env_file
from autotrader.ls_api import KST, LsApiError, LsRestClient, clean_str, to_float, to_int, valid_code

log = logging.getLogger("fetch2m")

ls_api.TR_SPEC.update({
    "t8412": ("/stock/chart", 1.2),       # 주식차트(N분)      → 1.0초 간격
    "t8452": ("/stock/chart", 1.2),       # 주식차트(N분) 통합 → 1.0초 간격 (t8412 와 한도 별도)
    "t1305": ("/stock/market-data", 1.2),  # 기간별주가 (일봉, 체결강도)
    "t8436": ("/stock/etc", 2),
    "t1404": ("/stock/market-data", 1),
    "t1405": ("/stock/market-data", 1),
})
QRYCNT = 500
MARKET_PROXIES = ["069500", "229200"]  # KODEX 200, KODEX 코스닥150 (시장 흐름 비교용)
M5_FIELDS = ["date", "time", "open", "high", "low", "close", "volume", "value"]
D_FIELDS = ["date", "open", "high", "low", "close", "volume", "value", "chdegree", "diff", "marketcap"]


def write_csv(path: Path, fields: list[str], rows: list[dict], gz: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    opener = (lambda p: gzip.open(p, "wt", newline="", encoding="utf-8")) if gz else \
        (lambda p: open(p, "w", newline="", encoding="utf-8"))
    with opener(tmp) as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def fetch_universe(c: LsRestClient, out: Path) -> list[dict]:
    data, _ = c.call("t8436", {"t8436InBlock": {"gubun": "0"}})
    rows = []
    for r in data.get("t8436OutBlock") or []:
        code = clean_str(r.get("shcode"))
        if valid_code(code):
            rows.append({"code": code, "name": clean_str(r.get("hname")), "market": clean_str(r.get("gubun")),
                         "etfgubun": clean_str(r.get("etfgubun")), "spac": clean_str(r.get("spac_gubun")),
                         "prev_close": to_int(r.get("jnilclose"))})
    write_csv(out / "universe.csv", list(rows[0].keys()), rows)
    return rows


def fetch_flags(c: LsRestClient, out: Path) -> None:
    rows = []
    for tr in ("t1404", "t1405"):
        for jc in range(1, 7):
            cts = " "
            for _ in range(50):
                try:
                    data, _ = c.call(tr, {f"{tr}InBlock": {"gubun": "0", "jongchk": str(jc), "cts_shcode": cts}})
                except LsApiError as e:
                    log.warning("%s jongchk=%d 실패: %s", tr, jc, e)
                    break
                for r in data.get(f"{tr}OutBlock1") or []:
                    code = clean_str(r.get("shcode"))
                    if valid_code(code):
                        rows.append({"tr": tr, "jongchk": jc, "code": code, "name": clean_str(r.get("hname")),
                                     "date": clean_str(r.get("date"))})
                nxt = clean_str((data.get(f"{tr}OutBlock") or {}).get("cts_shcode"))
                if not nxt or nxt == cts.strip():
                    break
                cts = nxt
    write_csv(out / "flags.csv", ["tr", "jongchk", "code", "name", "date"], rows)
    log.info("관리/경고 목록 %d건", len(rows))


def fetch_m5(c: LsRestClient, tr: str, code: str, start: str, end: str) -> list[dict]:
    bars: dict[tuple[str, str], dict] = {}
    cts_date = cts_time = ""
    tr_cont, key = "N", ""
    for _ in range(60):
        blk = {"shcode": code, "ncnt": 5, "qrycnt": QRYCNT, "nday": "0", "sdate": start, "stime": "",
               "edate": end, "etime": "", "cts_date": cts_date, "cts_time": cts_time, "comp_yn": "N"}
        if tr == "t8452":
            blk["exchgubun"] = "K"
        data, headers = c.call(tr, {f"{tr}InBlock": blk}, tr_cont=tr_cont, tr_cont_key=key)
        page = data.get(f"{tr}OutBlock1") or []
        for r in page:
            d, t = clean_str(r.get("date")), clean_str(r.get("time"))
            if d and start <= d <= end:
                bars[(d, t)] = {"date": d, "time": t, "open": to_int(r.get("open")), "high": to_int(r.get("high")),
                                "low": to_int(r.get("low")), "close": to_int(r.get("close")),
                                "volume": to_int(r.get("jdiff_vol")), "value": to_int(r.get("value"))}
        ob = data.get(f"{tr}OutBlock") or {}
        nd, nt = clean_str(ob.get("cts_date")), clean_str(ob.get("cts_time"))
        if not page or not nd or nd < start or (nd, nt) == (cts_date, cts_time) \
                or ls_api._header(headers, "tr_cont") != "Y":
            break
        cts_date, cts_time = nd, nt
        tr_cont, key = "Y", ls_api._header(headers, "tr_cont_key")
    return [bars[k] for k in sorted(bars)]


def fetch_daily(c: LsRestClient, code: str, cnt: int) -> list[dict]:
    data, _ = c.call("t1305", {"t1305InBlock": {"shcode": code, "dwmcode": 1, "date": "", "idx": 0, "cnt": cnt}})
    rows = []
    for r in data.get("t1305OutBlock1") or []:
        rows.append({"date": clean_str(r.get("date")), "open": to_int(r.get("open")), "high": to_int(r.get("high")),
                     "low": to_int(r.get("low")), "close": to_int(r.get("close")), "volume": to_int(r.get("volume")),
                     "value": to_int(r.get("value")), "chdegree": to_float(r.get("chdegree")),
                     "diff": to_float(r.get("diff")), "marketcap": to_int(r.get("marketcap"))})
    rows.sort(key=lambda x: x["date"])
    return rows


class Progress:
    def __init__(self, total: int) -> None:
        self.total, self.done, self.failed = total, 0, []
        self.t0 = time.monotonic()
        self.lock = threading.Lock()

    def tick(self, code: str, n: int, ok: bool) -> None:
        with self.lock:
            self.done += 1
            if not ok:
                self.failed.append(code)
            if self.done % 50 == 0 or self.done == self.total:
                el = time.monotonic() - self.t0
                log.info("5분봉 %d/%d (실패 %d), 경과 %.0f분, 남은 예상 %.0f분", self.done, self.total,
                         len(self.failed), el / 60, el / self.done * (self.total - self.done) / 60)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260811")
    ap.add_argument("--end", default="20261013")
    ap.add_argument("--out", default="data/research")
    ap.add_argument("--deadline", default="0835", help="이 시각(HHMM, KST)이 지나면 새 종목을 받지 않음")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--codes", default="")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(threadName)s %(message)s",
                        handlers=[logging.FileHandler(out / "fetch.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_env_file(Path(".env"))
    s = Settings.from_env()
    c = LsRestClient(s.appkey, s.appsecret, s.rest_base, label="모의투자", token_cache_dir=s.data_dir / "tokens")

    def past_deadline() -> bool:
        now = datetime.now(KST)
        return now.hour < 12 and now.strftime("%H%M") >= a.deadline

    uni = fetch_universe(c, out)
    if not (out / "flags.csv").exists():
        fetch_flags(c, out)
    if a.codes:
        codes = [x.strip() for x in a.codes.split(",") if x.strip()]
    else:
        codes = [u["code"] for u in uni if u["code"].endswith("0") and u["etfgubun"] in ("", "0")
                 and u["spac"] != "Y"] + MARKET_PROXIES
    if a.limit:
        codes = codes[:a.limit]
    log.info("대상 %d종목, 기간 %s~%s", len(codes), a.start, a.end)

    # 일봉 (t1305) - 별도 스레드
    def daily_worker() -> None:
        n = 0
        for code in codes:
            if past_deadline():
                log.warning("마감 시각 도달 - 일봉 수집 중단")
                return
            p = out / "daily" / f"{code}.csv"
            if p.exists():
                continue
            try:
                write_csv(p, D_FIELDS, fetch_daily(c, code, 150))
            except LsApiError as e:
                log.warning("일봉 %s 실패: %s", code, e)
            n += 1
            if n % 200 == 0:
                log.info("일봉 %d종목 완료", n)
        log.info("일봉 수집 끝")

    todo = [x for x in codes if not (out / "m5" / f"{x}.csv.gz").exists()]
    q: queue.Queue[str] = queue.Queue()
    for x in todo:
        q.put(x)
    prog = Progress(len(todo))

    def m5_worker(tr: str) -> None:
        while not past_deadline():
            try:
                code = q.get_nowait()
            except queue.Empty:
                return
            try:
                bars = fetch_m5(c, tr, code, a.start, a.end)
                write_csv(out / "m5" / f"{code}.csv.gz", M5_FIELDS, bars, gz=True)
                prog.tick(code, len(bars), True)
            except LsApiError as e:
                log.warning("5분봉 %s 실패(%s): %s", code, tr, e)
                prog.tick(code, 0, False)
        log.warning("마감 시각 도달 또는 대기열 끝 (%s)", tr)

    threads = [threading.Thread(target=daily_worker, name="daily"),
               threading.Thread(target=m5_worker, args=("t8412",), name="t8412"),
               threading.Thread(target=m5_worker, args=("t8452",), name="t8452")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("끝: 5분봉 %d/%d, 실패 %s", prog.done, prog.total, ",".join(prog.failed[:50]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
