"""LS증권 OPEN API REST 클라이언트.

TR 경로, 요청/응답 블록 이름, 초당 호출 한도는 LS증권 OPEN API 포털의
TR 명세(https://openapi.ls-sec.co.kr/apiservice)를 기준으로 했다.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import requests

from .config import Settings
from .models import Condition, Holding, OrderResult, Quote

log = logging.getLogger(__name__)

# TR 코드 → (URL 경로, 초당 호출 한도)
TR_SPEC: dict[str, tuple[str, float]] = {
    "t1866": ("/stock/item-search", 1),  # 서버저장조건 리스트조회
    "t1859": ("/stock/item-search", 1),  # 서버저장조건 조건검색
    "t1860": ("/stock/item-search", 1),  # 서버저장조건 실시간검색 (등록/해제)
    "t8407": ("/stock/market-data", 5),  # API용 주식멀티현재가조회
    "t1102": ("/stock/market-data", 10),  # 주식현재가(시세)조회
    "t0424": ("/stock/accno", 2),  # 주식잔고2
    "CSPAQ12200": ("/stock/accno", 1),  # 현물계좌예수금 주문가능금액 총평가 조회
    "CSPAT00601": ("/stock/order", 10),  # 현물주문
    "CSPAT00801": ("/stock/order", 3),  # 현물취소주문
}

TOKEN_PATH = "/oauth2/token"
TOKEN_REFRESH_MARGIN_SEC = 600
MULTI_QUOTE_MAX = 50  # t8407 한 번에 조회할 종목 수


class LsApiError(RuntimeError):
    def __init__(self, tr_cd: str, message: str, *, http_status: int | None = None,
                 rsp_cd: str = "", rsp_msg: str = "") -> None:
        super().__init__(f"[{tr_cd}] {message}")
        self.tr_cd = tr_cd
        self.http_status = http_status
        self.rsp_cd = rsp_cd
        self.rsp_msg = rsp_msg


def clean_str(value: Any) -> str:
    """응답 문자열의 NUL 패딩(\\u0000)과 공백을 제거한다."""
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def to_int(value: Any) -> int:
    s = clean_str(value).replace(",", "")
    if s in ("", "-", "+"):
        return 0
    return int(float(s))


def to_float(value: Any) -> float:
    s = clean_str(value).replace(",", "")
    if s in ("", "-", "+"):
        return 0.0
    return float(s)


class RateLimiter:
    """TR별 최소 호출 간격을 지킨다 (여러 스레드에서 호출해도 안전)."""

    def __init__(self, safety: float = 1.2, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._safety = safety
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str, per_sec: float) -> None:
        interval = self._safety / per_sec
        with self._lock:
            now = self._clock()
            last = self._last.get(key)
            if last is not None and now - last < interval:
                self._sleep(interval - (now - last))
                now = self._clock()
            self._last[key] = now


class LsRestClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None,
                 limiter: RateLimiter | None = None, timeout: float = 10.0) -> None:
        self._s = settings
        self._http = session or requests.Session()
        self._limiter = limiter or RateLimiter()
        self._timeout = timeout
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    # ---------------------------------------------------------------- token
    @property
    def token(self) -> str:
        with self._token_lock:
            if not self._token or time.time() >= self._token_expires_at - TOKEN_REFRESH_MARGIN_SEC:
                self._issue_token()
            return self._token

    def invalidate_token(self) -> None:
        with self._token_lock:
            self._token = ""
            self._token_expires_at = 0.0

    def _issue_token(self) -> None:
        resp = self._http.post(
            self._s.rest_base + TOKEN_PATH,
            headers={"content-type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "appkey": self._s.appkey,
                "appsecretkey": self._s.appsecret,
                "scope": "oob",
            },
            timeout=self._timeout,
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if resp.status_code != 200 or not token:
            raise LsApiError("token", f"접근토큰 발급 실패: HTTP {resp.status_code} {resp.text[:300]}",
                             http_status=resp.status_code)
        expires_in = to_int(body.get("expires_in")) or 3600
        self._token = token
        self._token_expires_at = time.time() + expires_in
        log.info("접근토큰 발급 완료 (유효 %d초)", expires_in)

    # ----------------------------------------------------------------- call
    def call(self, tr_cd: str, body: dict, tr_cont: str = "N",
             tr_cont_key: str = "") -> tuple[dict, dict]:
        """TR 1건 호출. (응답 JSON, 응답 헤더) 를 돌려준다."""
        path, per_sec = TR_SPEC[tr_cd]
        for attempt in (1, 2):
            self._limiter.wait(tr_cd, per_sec)
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token}",
                "tr_cd": tr_cd,
                "tr_cont": tr_cont,
                "tr_cont_key": tr_cont_key,
            }
            if self._s.mac_address:
                headers["mac_address"] = self._s.mac_address
            resp = self._http.post(self._s.rest_base + path, headers=headers, json=body,
                                   timeout=self._timeout)
            try:
                data = resp.json()
            except ValueError:
                data = {}
            rsp_cd = clean_str(data.get("rsp_cd")) if isinstance(data, dict) else ""
            rsp_msg = clean_str(data.get("rsp_msg")) if isinstance(data, dict) else ""
            if resp.status_code == 200:
                return data, dict(resp.headers)
            if attempt == 1 and _is_token_error(resp.status_code, rsp_cd, rsp_msg):
                log.warning("[%s] 토큰 오류로 재발급 후 재시도: %s %s", tr_cd, rsp_cd, rsp_msg)
                self.invalidate_token()
                continue
            raise LsApiError(tr_cd, f"HTTP {resp.status_code} {rsp_cd} {rsp_msg or resp.text[:300]}",
                             http_status=resp.status_code, rsp_cd=rsp_cd, rsp_msg=rsp_msg)
        raise AssertionError("unreachable")


def _is_token_error(status: int, rsp_cd: str, rsp_msg: str) -> bool:
    if status == 401:
        return True
    text = f"{rsp_cd} {rsp_msg}".lower()
    return "token" in text or "토큰" in text


class LsBroker:
    """자동매매에 필요한 기능만 감싼 고수준 API."""

    def __init__(self, client: LsRestClient, settings: Settings) -> None:
        self._c = client
        self._s = settings

    # ------------------------------------------------------- 조건검색
    def list_conditions(self) -> list[Condition]:
        result: list[Condition] = []
        cont, cont_key = "", ""
        for _ in range(20):
            data, _ = self._c.call("t1866", {"t1866InBlock": {
                "user_id": self._s.user_id, "gb": "0", "group_name": "",
                "cont": cont, "cont_key": cont_key,
            }})
            for row in data.get("t1866OutBlock1") or []:
                result.append(Condition(
                    query_index=clean_str(row.get("query_index")),
                    group_name=clean_str(row.get("group_name")),
                    query_name=clean_str(row.get("query_name")),
                ))
            out = data.get("t1866OutBlock") or {}
            cont = clean_str(out.get("cont"))
            cont_key = clean_str(out.get("contkey"))
            if cont != "1" or not cont_key:
                break
        return result

    def search_condition_once(self, query_index: str) -> list[str]:
        data, _ = self._c.call("t1859", {"t1859InBlock": {"query_index": query_index}})
        codes = []
        for key, value in data.items():
            if key.startswith("t1859OutBlock") and isinstance(value, list):
                for row in value:
                    code = clean_str(row.get("shcode"))
                    if code:
                        codes.append(code)
        return codes

    def register_realtime_condition(self, query_index: str) -> str:
        data, _ = self._c.call("t1860", {"t1860InBlock": {
            "sSysUserFlag": "U", "sFlag": "E", "sAlertNum": "", "query_index": query_index,
        }})
        out = data.get("t1860OutBlock") or {}
        alert_num = clean_str(out.get("sAlertNum"))
        if not alert_num:
            raise LsApiError("t1860", f"실시간 조건검색 등록 실패: {out.get('Msg') or data}")
        return alert_num

    def unregister_realtime_condition(self, query_index: str, alert_num: str) -> None:
        self._c.call("t1860", {"t1860InBlock": {
            "sSysUserFlag": "U", "sFlag": "D", "sAlertNum": alert_num, "query_index": query_index,
        }})

    # ----------------------------------------------------------- 시세
    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        unique = list(dict.fromkeys(codes))
        for i in range(0, len(unique), MULTI_QUOTE_MAX):
            chunk = unique[i:i + MULTI_QUOTE_MAX]
            data, _ = self._c.call("t8407", {"t8407InBlock": {
                "nrec": len(chunk), "shcode": "".join(chunk),
            }})
            for row in data.get("t8407OutBlock1") or []:
                code = clean_str(row.get("shcode"))
                quotes[code] = Quote(
                    code=code,
                    name=clean_str(row.get("hname")),
                    price=to_int(row.get("price")),
                    ask=to_int(row.get("offerho")),
                    bid=to_int(row.get("bidho")),
                )
        return quotes

    # ----------------------------------------------------------- 계좌
    def get_holdings(self) -> list[Holding]:
        holdings: list[Holding] = []
        cts = ""
        for _ in range(50):
            data, headers = self._c.call(
                "t0424",
                {"t0424InBlock": {"prcgb": "", "chegb": "", "dangb": "", "charge": "",
                                  "cts_expcode": cts}},
                tr_cont="Y" if cts else "N",
            )
            for row in data.get("t0424OutBlock1") or []:
                code = clean_str(row.get("expcode"))
                qty = to_int(row.get("janqty"))
                if not code or qty <= 0:
                    continue
                holdings.append(Holding(
                    code=code,
                    name=clean_str(row.get("hname")),
                    qty=qty,
                    sellable_qty=to_int(row.get("mdposqt")),
                    avg_price=to_float(row.get("pamt")),
                    last_price=to_int(row.get("price")),
                ))
            cts = clean_str((data.get("t0424OutBlock") or {}).get("cts_expcode"))
            if not cts or _header(headers, "tr_cont") != "Y":
                break
        return holdings

    def get_orderable_cash(self) -> int:
        data, _ = self._c.call("CSPAQ12200", {"CSPAQ12200InBlock1": {"BalCreTp": "0"}})
        return to_int((data.get("CSPAQ12200OutBlock2") or {}).get("MnyOrdAbleAmt"))

    # ----------------------------------------------------------- 주문
    def buy_market(self, code: str, qty: int) -> OrderResult:
        return self._order(code, qty, side="2")

    def sell_market(self, code: str, qty: int) -> OrderResult:
        return self._order(code, qty, side="1")

    def _order(self, code: str, qty: int, side: str) -> OrderResult:
        if qty <= 0:
            raise ValueError("주문 수량은 1 이상이어야 합니다.")
        block = {
            "IsuNo": "A" + code,
            "OrdQty": qty,
            "OrdPrc": 0,
            "BnsTpCode": side,  # 1: 매도, 2: 매수
            "OrdprcPtnCode": "03",  # 03: 시장가
            "MgntrnCode": "000",  # 000: 보통
            "LoanDt": "",
            "OrdCndiTpCode": "0",  # 0: 없음
        }
        if self._s.order_mbr_no:
            block["MbrNo"] = self._s.order_mbr_no
        data, _ = self._c.call("CSPAT00601", {"CSPAT00601InBlock1": block})
        out2 = data.get("CSPAT00601OutBlock2") or {}
        ord_no = to_int(out2.get("OrdNo"))
        rsp_cd = clean_str(data.get("rsp_cd"))
        rsp_msg = clean_str(data.get("rsp_msg"))
        if ord_no <= 0:
            raise LsApiError("CSPAT00601", f"주문 실패: {rsp_cd} {rsp_msg}", rsp_cd=rsp_cd,
                             rsp_msg=rsp_msg)
        return OrderResult(ord_no=ord_no, rsp_cd=rsp_cd, rsp_msg=rsp_msg)


def _header(headers: dict, name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name:
            return clean_str(v)
    return ""
