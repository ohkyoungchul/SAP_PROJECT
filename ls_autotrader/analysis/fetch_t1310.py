"""10/12·10/13 편입 시각 전후의 실제 체결강도(t1310 분 데이터) 수집 - 조회 전용.

입력 CSV: code,daygb,time  (daygb 0=당일(10/13), 1=전일(10/12), time=HHMMSS 조회 끝 시각)
출력 CSV: 조회 끝 시각부터 거슬러 올라간 20분치 분 데이터 (chetime, chdegree, 매수/매도 체결량 누적)
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from autotrader import ls_api
from autotrader.config import Settings, load_env_file
from autotrader.ls_api import LsApiError, LsRestClient, clean_str, to_float, to_int

ls_api.TR_SPEC.update({"t1310": ("/stock/market-data", 1.2)})
FIELDS = ["code", "daygb", "qtime", "chetime", "chdegree", "msvolume", "mdvolume", "volume", "price"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    load_env_file(Path(".env"))
    s = Settings.from_env()
    c = LsRestClient(s.appkey, s.appsecret, s.rest_base, label="모의투자", token_cache_dir=s.data_dir / "tokens")
    rows = list(csv.DictReader(open(a.inp, encoding="utf-8")))
    outp = Path(a.out)
    done = set()
    if outp.exists():
        done = {(r["code"], r["daygb"], r["qtime"]) for r in csv.DictReader(open(outp, encoding="utf-8"))}
    new = not outp.exists()
    f = open(outp, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=FIELDS)
    if new:
        w.writeheader()
    n = 0
    for r in rows:
        k = (r["code"], r["daygb"], r["time"])
        if k in done:
            continue
        try:
            d, _ = c.call("t1310", {"t1310InBlock": {"daygb": r["daygb"], "timegb": "0", "shcode": r["code"],
                                                    "endtime": r["time"], "cts_time": "", "exchgubun": "K"}})
        except LsApiError as e:
            print("실패", k, e, file=sys.stderr)
            continue
        for x in d.get("t1310OutBlock1") or []:
            w.writerow({"code": r["code"], "daygb": r["daygb"], "qtime": r["time"],
                        "chetime": clean_str(x.get("chetime")), "chdegree": to_float(x.get("chdegree")),
                        "msvolume": to_int(x.get("msvolume")), "mdvolume": to_int(x.get("mdvolume")),
                        "volume": to_int(x.get("volume")), "price": to_int(x.get("price"))})
        f.flush()
        done.add(k)
        n += 1
    f.close()
    print("완료", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
