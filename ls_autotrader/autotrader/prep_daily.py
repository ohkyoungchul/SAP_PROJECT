"""매일 저녁 준비 작업: 전 종목 일봉 갱신 → 다음 거래일에 쓸 일봉 특성 파일 생성 (조회 TR만, 주문 없음).

    python -m autotrader.prep_daily            (Stock_Trading 폴더에서, 장 마감 후)

- 일봉 원본: data/research/daily/<코드>.csv  (t1305, 주간 재최적화도 이 파일을 쓴다)
- 특성 파일: data/strategy/daily_features.csv (entry_filter.py 가 읽는다)
- 장중(평일 08:30~15:40)에 실행하면 오늘 일봉(미완성)은 빼고 계산한다.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import ls_api
from .config import ConfigError, Settings, load_env_file
from .entry_filter import DAILY_COLUMNS
from .ls_api import LsApiError, LsRestClient, clean_str, to_float, to_int, valid_code

log = logging.getLogger("prep_daily")

ls_api.TR_SPEC.update({
    "t8436": ("/stock/etc", 2),
    "t1305": ("/stock/market-data", 1.2),  # 1.0초 간격
})
DAILY_FIELDS = ["date", "open", "high", "low", "close", "volume", "value", "chdegree", "diff", "marketcap"]
KEEP_ROWS = 260


def _filter_common(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["code"].endswith("0") and r["etfgubun"] in ("", "0") and r["spac"] != "Y"]


def common_stocks(c: LsRestClient, research_dir: Path) -> list[dict]:
    """t8436 전체 종목 → 보통주(우선주·ETF·ETN·스팩 제외). 조회가 계속 실패하면 저장된 목록을 쓴다."""
    data = None
    for attempt in range(3):
        try:
            data, _ = c.call("t8436", {"t8436InBlock": {"gubun": "0"}})
            break
        except LsApiError as e:
            log.warning("종목 목록(t8436) 조회 실패 %d/3: %s", attempt + 1, e)
            time.sleep(5)
    if data is None:
        saved = research_dir / "universe.csv"
        if not saved.is_file():
            raise LsApiError("t8436", "종목 목록을 받지 못했고 저장된 목록도 없습니다.")
        log.warning("저장된 종목 목록(%s)을 씁니다.", saved)
        with saved.open(encoding="utf-8") as f:
            return _filter_common(list(csv.DictReader(f)))
    rows = []
    for r in data.get("t8436OutBlock") or []:
        code = clean_str(r.get("shcode"))
        if not valid_code(code):
            continue
        rows.append({"code": code, "name": clean_str(r.get("hname")), "market": clean_str(r.get("gubun")),
                     "etfgubun": clean_str(r.get("etfgubun")), "spac": clean_str(r.get("spac_gubun")),
                     "prev_close": to_int(r.get("jnilclose"))})
    if rows:
        _write_csv(research_dir / "universe.csv", list(rows[0].keys()), rows)
    return _filter_common(rows)


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _read_daily(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        return {r["date"]: r for r in csv.DictReader(f)}


def fetch_daily(c: LsRestClient, code: str, cnt: int) -> list[dict]:
    data, _ = c.call("t1305", {"t1305InBlock": {"shcode": code, "dwmcode": 1, "date": "", "idx": 0, "cnt": cnt}})
    out = []
    for r in data.get("t1305OutBlock1") or []:
        d = clean_str(r.get("date"))
        if not d:
            continue
        out.append({"date": d, "open": to_int(r.get("open")), "high": to_int(r.get("high")),
                    "low": to_int(r.get("low")), "close": to_int(r.get("close")), "volume": to_int(r.get("volume")),
                    "value": to_int(r.get("value")), "chdegree": to_float(r.get("chdegree")),
                    "diff": to_float(r.get("diff")), "marketcap": to_int(r.get("marketcap"))})
    return out


def compute_features(code: str, rows: list[dict], before: str) -> dict | None:
    """before(YYYYMMDD) 이전 일봉으로 계산한 특성 한 줄."""
    rows = [r for r in rows if r["date"] < before]
    rows.sort(key=lambda r: r["date"])
    closes = [float(r["close"]) for r in rows if float(r["close"]) > 0]
    if len(closes) < 2:
        return None
    last = rows[-1]
    highs = [float(r["high"]) for r in rows[-60:]]

    def ret(n: int) -> str:
        return f"{(closes[-1] / closes[-1 - n] - 1) * 100:.4f}" if len(closes) > n else ""

    return {"code": code, "base_date": last["date"], "prev_close": f"{closes[-1]:.0f}",
            "ma20": f"{sum(closes[-20:]) / 20:.4f}" if len(closes) >= 20 else "",
            "high60": f"{max(highs):.0f}" if len(highs) >= 20 else "",
            "ret1d_pct": ret(1), "ret5d_pct": ret(5), "ret20d_pct": ret(20)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="일봉 갱신 + 일봉 특성 파일 생성 (조회 전용)")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--research-dir", default="data/research")
    ap.add_argument("--out", default="", help="특성 파일 경로 (기본: .env 의 DAILY_FEATURES_FILE)")
    ap.add_argument("--no-fetch", action="store_true", help="API 조회 없이 저장된 일봉으로 특성만 다시 계산")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_env_file(Path(a.env))
    try:
        s = Settings.from_env()
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    research = Path(a.research_dir)
    daily_dir = research / "daily"
    out = Path(a.out) if a.out else s.daily_features_file
    log_dir = s.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / f"prep_daily_{datetime.now():%Y%m%d}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logging.getLogger().addHandler(fh)

    now = datetime.now()
    in_session = now.weekday() < 5 and "0830" <= now.strftime("%H%M") < "1540"
    # 특성은 '다음 거래일'에 쓴다: 장 마감 후면 오늘까지, 장중이면 어제까지의 일봉으로 계산
    before = now.strftime("%Y%m%d") if in_session else "99999999"

    if a.no_fetch:
        codes = sorted(p.stem for p in daily_dir.glob("*.csv"))
    else:
        ls_api.QUERY_TIMEOUT_SEC = 30.0  # 종목 목록(t8436)은 응답이 커서 5초로는 모자랄 때가 있다
        c = LsRestClient(s.appkey, s.appsecret, s.rest_base, label="모의투자" if not s.is_real else "실전",
                         token_cache_dir=s.data_dir / "tokens", mac_address=s.mac_address, timeout=30.0)
        uni = common_stocks(c, research)
        codes = [u["code"] for u in uni]
        if a.limit:
            codes = codes[:a.limit]
        log.info("일봉 갱신 대상 %d종목 (약 %.0f분)", len(codes), len(codes) / 60)
        t0 = time.monotonic()
        fails = 0
        for i, code in enumerate(codes, 1):
            p = daily_dir / f"{code}.csv"
            old = _read_daily(p)
            try:
                new = fetch_daily(c, code, 10 if len(old) >= 70 else 150)
            except LsApiError as e:
                fails += 1
                log.warning("%s 일봉 조회 실패: %s", code, e)
                continue
            for r in new:
                old[r["date"]] = r
            rows = [old[k] for k in sorted(old)][-KEEP_ROWS:]
            _write_csv(p, DAILY_FIELDS, rows)
            if i % 300 == 0:
                log.info("일봉 %d/%d (실패 %d), %.0f분 경과", i, len(codes), fails, (time.monotonic() - t0) / 60)
        log.info("일봉 갱신 끝: %d종목, 실패 %d", len(codes), fails)

    feats = []
    for code in codes:
        f = compute_features(code, list(_read_daily(daily_dir / f"{code}.csv").values()), before)
        if f:
            feats.append(f)
    if not feats:
        log.error("특성을 계산한 종목이 없습니다. 특성 파일을 바꾸지 않습니다.")
        return 1
    base = max(f["base_date"] for f in feats)
    stale = sum(1 for f in feats if f["base_date"] != base)
    _write_csv(out, DAILY_COLUMNS, feats)
    log.info("특성 파일 저장: %s (%d종목, 기준일 %s, 기준일이 다른 종목 %d - 거래정지 등)", out, len(feats), base, stale)
    return 0


if __name__ == "__main__":
    sys.exit(main())
