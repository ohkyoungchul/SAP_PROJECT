"""백테스트용 과거 데이터 수집 (PC에서 실행, 조회 TR만 사용 / 주문 없음).

사용법 (ls_autotrader 폴더에서):
    .venv\\Scripts\\python -m backtest.fetch_data --start 20260825 --end 20260930

받는 데이터 (data/backtest/ 아래):
- universe.csv      : t8436 전체 종목 (시장, ETF/ETN 구분, 스팩 구분)
- flags.csv         : t1404(관리 등) / t1405(투자경고·정지·정리매매 등) 현재 목록
- bars5/<코드>.csv  : t8412 5분봉 (date,time,open,high,low,close,volume)
- daily/<코드>.csv  : t1305 일봉 (date,close,volume,chdegree=체결강도)

이미 받은 종목 파일은 건너뛰므로 중간에 끊겨도 다시 실행하면 이어서 받는다.
자동매매(08:50~15:40)와 같은 키를 쓰므로 그 시간 밖에서 실행한다.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

from autotrader import ls_api
from autotrader.config import ConfigError, Settings, load_env_file
from autotrader.ls_api import LsApiError, LsRestClient, clean_str, to_float, to_int, valid_code

log = logging.getLogger("backtest.fetch")

# 이 스크립트에서만 쓰는 조회 TR (경로, 초당 한도) - LS OPEN API TR 명세 기준
EXTRA_TRS = {
    "t8412": ("/stock/chart", 1),  # 주식차트(N분)
    "t8436": ("/stock/etc", 2),  # 주식종목조회 API용
    "t1305": ("/stock/market-data", 1),  # 기간별주가
    "t1404": ("/stock/market-data", 1),  # 관리/불성실/투자유의조회
    "t1405": ("/stock/market-data", 1),  # 투자경고/매매정지/정리매매조회
}
QRYCNT = 500  # t8412 비압축 1회 최대 건수
MAX_PAGES = 40


def make_client(settings: Settings) -> LsRestClient:
    ls_api.TR_SPEC.update(EXTRA_TRS)
    token_dir = settings.data_dir / "tokens"
    if settings.uses_data_key:
        return LsRestClient(settings.data_appkey, settings.data_appsecret, settings.rest_base,
                            label="조회전용(실전)", token_cache_dir=token_dir,
                            mac_address=settings.mac_address)
    return LsRestClient(settings.appkey, settings.appsecret, settings.rest_base,
                        label="모의투자" if not settings.is_real else "실전",
                        token_cache_dir=token_dir, mac_address=settings.mac_address)


def fetch_universe(client: LsRestClient, out: Path) -> list[dict]:
    data, _ = client.call("t8436", {"t8436InBlock": {"gubun": "0"}})
    rows = []
    for r in data.get("t8436OutBlock") or []:
        code = clean_str(r.get("shcode"))
        if not valid_code(code):
            continue
        rows.append({"code": code, "name": clean_str(r.get("hname")),
                     "market": clean_str(r.get("gubun")),  # 1 코스피, 2 코스닥
                     "etfgubun": clean_str(r.get("etfgubun")),
                     "spac": clean_str(r.get("spac_gubun"))})
    write_csv(out, ["code", "name", "market", "etfgubun", "spac"], rows)
    return rows


def fetch_flags(client: LsRestClient, out: Path) -> None:
    """t1404/t1405 를 jongchk 1~6 으로 모두 받아 (tr, jongchk, code, name) 로 저장한다.

    jongchk 값의 의미는 simulate.py 의 EXCLUDE_FLAGS 주석 참고. 조회 시점 기준 목록이다.
    """
    rows = []
    for tr in ("t1404", "t1405"):
        for jc in range(1, 7):
            cts = " "
            for _ in range(50):
                try:
                    data, _ = client.call(tr, {f"{tr}InBlock": {"gubun": "0", "jongchk": str(jc),
                                                               "cts_shcode": cts}})
                except LsApiError as e:
                    log.warning("%s jongchk=%d 조회 실패: %s", tr, jc, e)
                    break
                for r in data.get(f"{tr}OutBlock1") or []:
                    code = clean_str(r.get("shcode"))
                    if valid_code(code):
                        rows.append({"tr": tr, "jongchk": jc, "code": code,
                                     "name": clean_str(r.get("hname")), "date": clean_str(r.get("date"))})
                nxt = clean_str((data.get(f"{tr}OutBlock") or {}).get("cts_shcode"))
                if not nxt or nxt == cts.strip():
                    break
                cts = nxt
    write_csv(out, ["tr", "jongchk", "code", "name", "date"], rows)
    log.info("투자유의/경고 목록 %d건 저장", len(rows))


def fetch_bars(client: LsRestClient, code: str, start: str, end: str) -> list[dict]:
    """t8412 5분봉을 start~end 범위로 받는다 (최신 → 과거 방향 연속조회)."""
    bars: dict[tuple[str, str], dict] = {}
    cts_date, cts_time = "", ""
    tr_cont, tr_cont_key = "N", ""
    for _ in range(MAX_PAGES):
        data, headers = client.call("t8412", {"t8412InBlock": {
            "shcode": code, "ncnt": 5, "qrycnt": QRYCNT, "nday": "0",
            "sdate": start, "stime": "", "edate": end, "etime": "",
            "cts_date": cts_date, "cts_time": cts_time, "comp_yn": "N"}},
            tr_cont=tr_cont, tr_cont_key=tr_cont_key)
        page = data.get("t8412OutBlock1") or []
        for r in page:
            d, t = clean_str(r.get("date")), clean_str(r.get("time"))
            if not d or d < start or d > end:
                continue
            bars[(d, t)] = {"date": d, "time": t, "open": to_int(r.get("open")),
                            "high": to_int(r.get("high")), "low": to_int(r.get("low")),
                            "close": to_int(r.get("close")), "volume": to_int(r.get("jdiff_vol"))}
        out = data.get("t8412OutBlock") or {}
        nd, nt = clean_str(out.get("cts_date")), clean_str(out.get("cts_time"))
        if not page or not nd or nd < start or (nd, nt) == (cts_date, cts_time):
            break
        cts_date, cts_time = nd, nt
        tr_cont, tr_cont_key = "Y", ls_api._header(headers, "tr_cont_key")
    return [bars[k] for k in sorted(bars)]


def fetch_daily(client: LsRestClient, code: str, start: str, end: str, cnt: int) -> list[dict]:
    data, _ = client.call("t1305", {"t1305InBlock": {"shcode": code, "dwmcode": 1, "date": "",
                                                     "idx": 0, "cnt": cnt}})
    rows = []
    for r in data.get("t1305OutBlock1") or []:
        d = clean_str(r.get("date"))
        if start <= d <= end:
            rows.append({"date": d, "close": to_int(r.get("close")), "volume": to_int(r.get("volume")),
                         "chdegree": to_float(r.get("chdegree"))})
    rows.sort(key=lambda x: x["date"])
    return rows


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="백테스트용 과거 데이터 수집 (조회 전용)")
    p.add_argument("--start", default="20260825", help="5분봉 시작일 (지표 계산용 여유 포함)")
    p.add_argument("--end", default="20260930")
    p.add_argument("--env", default=".env")
    p.add_argument("--out", default="data/backtest")
    p.add_argument("--codes", default="", help="쉼표로 구분한 종목코드만 받기 (시험용)")
    p.add_argument("--limit", type=int, default=0, help="앞에서부터 N종목만 받기 (시험용)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if Path(args.env).exists():
        load_env_file(Path(args.env))
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    client = make_client(settings)
    out = Path(args.out)

    universe = fetch_universe(client, out / "universe.csv")
    log.info("전체 종목 %d개", len(universe))
    fetch_flags(client, out / "flags.csv")

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        # 조건 N(우선주 제외), O(ETF/ETN/스팩 제외)에 걸리는 종목은 받지 않는다
        codes = [u["code"] for u in universe
                 if u["code"].endswith("0") and u["etfgubun"] in ("", "0") and u["spac"] != "Y"]
    if args.limit:
        codes = codes[:args.limit]

    today = date.today()
    start_d = datetime.strptime(args.start, "%Y%m%d").date()
    cnt = min(300, int((today - start_d).days * 5 / 7) + 10)

    todo = [c for c in codes if not (out / "bars5" / f"{c}.csv").exists()]
    log.info("수집 대상 %d종목 (남은 것 %d). 종목당 약 %d~%d초 걸립니다.",
             len(codes), len(todo), 3, 6)
    t0 = time.monotonic()
    for i, code in enumerate(todo, 1):
        try:
            daily = fetch_daily(client, code, args.start, args.end, cnt)
            bars = fetch_bars(client, code, args.start, args.end)
        except LsApiError as e:
            if e.paper_unsupported:
                print("모의투자 키로는 차트 조회가 안 됩니다. .env 에 LS_DATA_APPKEY(실전 키)를 넣어 주세요.",
                      file=sys.stderr)
                return 3
            log.warning("%s 수집 실패, 다음 실행 때 다시 받습니다: %s", code, e)
            continue
        write_csv(out / "daily" / f"{code}.csv", ["date", "close", "volume", "chdegree"], daily)
        write_csv(out / "bars5" / f"{code}.csv",
                  ["date", "time", "open", "high", "low", "close", "volume"], bars)
        if i == 1 or i % 50 == 0 or i == len(todo):
            first = bars[0]["date"] + " " + bars[0]["time"] if bars else "-"
            el = time.monotonic() - t0
            log.info("[%d/%d] %s 5분봉 %d개 (첫 봉 %s), 남은 예상 %.0f분",
                     i, len(todo), code, len(bars), first, el / i * (len(todo) - i) / 60)
    log.info("수집 완료: %s", out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
