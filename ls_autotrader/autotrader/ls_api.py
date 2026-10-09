"""LS증권 OPEN API REST 클라이언트.

TR 경로, 요청/응답 블록 이름, 초당 호출 한도는 LS증권 OPEN API 포털의 TR 명세를 따른다.
- REST 주소는 실전/모의투자 모두 https://openapi.ls-sec.co.kr:8080 이고,
  어느 서버로 붙는지는 App Key 로 결정된다.
- 응답 성공 코드는 TR마다 다르다 (조회 00000, 매수 00040, 매도 00039 …).
  그래서 rsp_cd 가 아니라 기대하는 OutBlock / 주문번호로 성공을 판단한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .models import Condition, Holding, OrderResult, Quote

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# TR 코드 → (URL 경로, 개인 초당 호출 한도)
TR_SPEC: dict[str, tuple[str, float]] = {
    "t1866": ("/stock/item-search", 1),  # 서버저장조건 리스트조회
    "t1859": ("/stock/item-search", 1),  # 서버저장조건 조건검색
    "t1860": ("/stock/item-search", 1),  # 서버저장조건 실시간검색 (등록/해제)
    "t8407": ("/stock/market-data", 5),  # API용 주식멀티현재가조회
    "t0424": ("/stock/accno", 2),  # 주식잔고2
    "CSPAQ12200": ("/stock/accno", 1),  # 현물계좌예수금 주문가능금액 총평가 조회
    "CSPAT00601": ("/stock/order", 10),  # 현물주문
}
ORDER_TRS = {"CSPAT00601"}

TOKEN_PATH = "/oauth2/token"
TOKEN_MIN_REISSUE_SEC = 10  # 토큰 재발급 최소 간격 (재발급하면 기존 토큰이 무효화될 수 있음)
GLOBAL_TPS = 15  # 전체 TR 합산 상한 (기본 한도 20TPS 보다 여유 있게)
MULTI_QUOTE_MAX = 50  # t8407 한 번에 조회할 종목 수
QUERY_TIMEOUT_SEC = 5.0  # 조회 TR 응답 대기 (주문 TR 은 생성자 timeout 사용)
RATE_LIMIT_CODE = "IGW00201"  # 호출 거래건수를 초과하였습니다.
HOLDINGS_OK_CODES = {"00000", "00200"}  # 00200: 조회내역이 없습니다
PAPER_UNSUPPORTED_CODE = "01900"  # 모의투자에서는 해당업무가 제공되지 않습니다.
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


class LsApiError(RuntimeError):
    def __init__(self, tr_cd: str, message: str, *, http_status: int | None = None,
                 rsp_cd: str = "", rsp_msg: str = "", ambiguous: bool = False) -> None:
        super().__init__(f"[{tr_cd}] {message}")
        self.tr_cd = tr_cd
        self.http_status = http_status
        self.rsp_cd = rsp_cd
        self.rsp_msg = rsp_msg
        # 주문이 서버에 접수됐는지 알 수 없는 오류 (타임아웃, 응답 본문 없는 5xx 등)
        self.ambiguous = ambiguous

    @property
    def paper_unsupported(self) -> bool:
        return self.rsp_cd == PAPER_UNSUPPORTED_CODE or "모의투자에서는" in self.rsp_msg


def clean_str(value: Any) -> str:
    """응답 문자열의 NUL 패딩(\\u0000)과 공백을 제거한다."""
    if value is None:
        return ""
    return str(value).split("\x00", 1)[0].strip()


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


def valid_code(code: str) -> bool:
    return bool(CODE_RE.match(code))


def next_token_expiry(issued: datetime, expires_in: int) -> datetime:
    """토큰 만료 시각: (발급 + expires_in) 과 '발급 후 처음 오는 07:00(KST)' 중 이른 쪽.

    LS 안내: 접근토큰 유효기간은 신청일로부터 익일 07시까지. 응답의 expires_in(86400)만
    믿으면 07:00 이후에 만료된 토큰을 쓰게 된다.
    """
    issued_kst = issued.astimezone(KST)
    seven = issued_kst.replace(hour=7, minute=0, second=0, microsecond=0)
    if seven <= issued_kst:
        seven += timedelta(days=1)
    by_ttl = issued_kst + timedelta(seconds=max(expires_in, 60))
    return min(seven, by_ttl)


class RateLimiter:
    """TR별 최소 호출 간격과 전체 합산 간격을 지킨다 (여러 스레드에서 호출해도 안전)."""

    def __init__(self, safety: float = 1.2, global_tps: float = GLOBAL_TPS,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._safety = safety
        self._global_interval = 1.0 / global_tps
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._last_any = float("-inf")
        self._lock = threading.Lock()

    def wait(self, key: str, per_sec: float) -> None:
        interval = self._safety / per_sec
        with self._lock:
            now = self._clock()
            due = max(self._last.get(key, float("-inf")) + interval,
                      self._last_any + self._global_interval)
            if now < due:
                self._sleep(due - now)
                now = self._clock()
            self._last[key] = now
            self._last_any = now


class LsRestClient:
    """App Key 하나에 대한 REST 클라이언트 (토큰 발급/캐시, 호출 한도, 오류 처리)."""

    def __init__(self, appkey: str, appsecret: str, rest_base: str, *, label: str,
                 token_cache_dir: Path | None = None, mac_address: str = "",
                 session: requests.Session | None = None, limiter: RateLimiter | None = None,
                 timeout: float = 10.0, now: Callable[[], datetime] = lambda: datetime.now(KST),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.label = label
        self._appkey = appkey
        self._appsecret = appsecret
        self._base = rest_base.rstrip("/")
        self._mac = mac_address
        self._http = session or requests.Session()
        self._limiter = limiter or RateLimiter()
        self._timeout = timeout
        self._now = now
        self._sleep = sleep
        self._token = ""
        self._refresh_at: datetime | None = None
        self._last_issue_mono = float("-inf")
        self._token_lock = threading.Lock()
        self.token_version = 0  # 토큰이 바뀔 때마다 증가 (WebSocket 재접속 판단용)
        self._cache_file: Path | None = None
        if token_cache_dir is not None:
            digest = hashlib.sha256(appkey.encode()).hexdigest()[:16]
            self._cache_file = token_cache_dir / f"token_{digest}.json"
            self._load_cached_token()

    # ---------------------------------------------------------------- token
    @property
    def token(self) -> str:
        with self._token_lock:
            if not self._token or self._refresh_at is None or self._now() >= self._refresh_at:
                self._issue_token()
            return self._token

    def invalidate_token(self) -> None:
        with self._token_lock:
            self._refresh_at = None  # 다음 사용 때 재발급 (같은 토큰이 오면 버전은 그대로)

    def _load_cached_token(self) -> None:
        assert self._cache_file is not None
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            refresh_at = datetime.fromisoformat(data["refresh_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if data.get("access_token") and self._now() < refresh_at:
            self._token = data["access_token"]
            self._refresh_at = refresh_at
            self.token_version += 1
            log.info("[%s] 저장된 접근토큰 사용 (갱신 예정 %s)", self.label,
                     refresh_at.astimezone(KST).strftime("%m-%d %H:%M"))

    def _issue_token(self) -> None:
        wait = self._last_issue_mono + TOKEN_MIN_REISSUE_SEC - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_issue_mono = time.monotonic()
        try:
            resp = self._http.post(
                self._base + TOKEN_PATH,
                headers={"content-type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "appkey": self._appkey,
                      "appsecretkey": self._appsecret, "scope": "oob"},
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            raise LsApiError("token", f"[{self.label}] 접근토큰 발급 요청 실패: {e}") from e
        body = _json(resp)
        token = body.get("access_token")
        if resp.status_code != 200 or not token:
            raise LsApiError("token", f"[{self.label}] 접근토큰 발급 실패: HTTP {resp.status_code} "
                             f"{clean_str(body.get('rsp_cd'))} {clean_str(body.get('rsp_msg')) or resp.text[:200]}",
                             http_status=resp.status_code, rsp_cd=clean_str(body.get("rsp_cd")),
                             rsp_msg=clean_str(body.get("rsp_msg")))
        issued = self._now()
        expires_in = to_int(body.get("expires_in") or body.get("expire_in")) or 86400
        expiry = next_token_expiry(issued, expires_in)
        if expiry - issued <= timedelta(minutes=10):
            # 07:00 직전 발급: 새로 받아도 07:00 에 만료되므로 07:00 이 지난 뒤 한 번만 갱신
            self._refresh_at = expiry + timedelta(seconds=5)
        else:
            self._refresh_at = expiry - timedelta(minutes=5)
        if token != self._token:
            self.token_version += 1
        self._token = token
        log.info("[%s] 접근토큰 발급 완료 (만료 예상 %s)", self.label,
                 expiry.astimezone(KST).strftime("%m-%d %H:%M"))
        if self._cache_file is not None:
            try:
                self._cache_file.parent.mkdir(parents=True, exist_ok=True)
                self._cache_file.write_text(json.dumps({
                    "access_token": token, "refresh_at": self._refresh_at.isoformat()}),
                    encoding="utf-8")
            except OSError:
                log.warning("[%s] 토큰 캐시 파일 저장 실패", self.label)

    # ----------------------------------------------------------------- call
    def call(self, tr_cd: str, body: dict, tr_cont: str = "N",
             tr_cont_key: str = "") -> tuple[dict, dict]:
        """TR 1건 호출. (응답 JSON, 응답 헤더) 를 돌려준다.

        - 토큰 오류: 토큰을 다시 받아 1회 재시도 (요청이 처리되지 않은 경우라 주문도 안전)
        - 호출 한도 초과(IGW00201): 조회 TR만 1초 쉬고 최대 2회 재시도
        - 주문 TR의 타임아웃/본문 없는 5xx: 접수 여부를 모르므로 재시도하지 않고 ambiguous 로 알린다
        """
        path, per_sec = TR_SPEC[tr_cd]
        is_order = tr_cd in ORDER_TRS
        token_retry_left, rate_retry_left = 1, (0 if is_order else 2)
        while True:
            self._limiter.wait(tr_cd, per_sec)
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token}",
                "tr_cd": tr_cd,
                "tr_cont": tr_cont,
                "tr_cont_key": tr_cont_key,
            }
            if self._mac:
                headers["mac_address"] = self._mac
            try:
                resp = self._http.post(self._base + path, headers=headers, json=body,
                                       timeout=self._timeout if is_order else
                                       min(self._timeout, QUERY_TIMEOUT_SEC))
            except requests.RequestException as e:
                raise LsApiError(tr_cd, f"[{self.label}] 요청 실패: {e}", ambiguous=is_order) from e
            data = _json(resp)
            rsp_cd = clean_str(data.get("rsp_cd"))
            rsp_msg = clean_str(data.get("rsp_msg"))

            if _is_token_error(resp.status_code, rsp_cd, rsp_msg) and token_retry_left:
                token_retry_left -= 1
                log.warning("[%s] %s 토큰 오류(%s %s) → 토큰 재발급 후 재시도",
                            self.label, tr_cd, rsp_cd, rsp_msg)
                self.invalidate_token()
                continue
            if rsp_cd == RATE_LIMIT_CODE or resp.status_code == 429:
                if rate_retry_left:
                    rate_retry_left -= 1
                    log.warning("[%s] %s 호출 한도 초과 → 1초 후 재시도", self.label, tr_cd)
                    self._sleep(1.0)
                    continue
                raise LsApiError(tr_cd, f"[{self.label}] 호출 한도 초과 {rsp_cd} {rsp_msg}",
                                 http_status=resp.status_code, rsp_cd=rsp_cd, rsp_msg=rsp_msg)
            if rsp_cd == PAPER_UNSUPPORTED_CODE or "모의투자에서는" in rsp_msg:
                raise LsApiError(tr_cd, f"[{self.label}] {rsp_cd} {rsp_msg}",
                                 http_status=resp.status_code, rsp_cd=rsp_cd, rsp_msg=rsp_msg)
            if resp.status_code == 200:
                return data, dict(resp.headers)
            # 게이트웨이 오류(IGW…)는 요청이 거부된 것. 본문이 없는 5xx 는 처리 여부를 알 수 없다.
            ambiguous = is_order and resp.status_code >= 500 and not rsp_cd.startswith("IGW")
            raise LsApiError(tr_cd, f"[{self.label}] HTTP {resp.status_code} {rsp_cd} "
                             f"{rsp_msg or resp.text[:300]}", http_status=resp.status_code,
                             rsp_cd=rsp_cd, rsp_msg=rsp_msg, ambiguous=ambiguous)


def _json(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_token_error(status: int, rsp_cd: str, rsp_msg: str) -> bool:
    if status in (401, 403):
        return True
    if rsp_cd in ("IGW00121", "IGW00122", "IGW00123"):
        return True
    text = rsp_msg.lower()
    return "token" in text or "토큰" in text


def _check_holdings_page(data: dict) -> None:
    """t0424 응답이 정상인지 확인한다. 오류 응답을 '보유 종목 없음'으로 읽으면 안 된다."""
    rsp_cd = clean_str(data.get("rsp_cd"))
    ok = rsp_cd in HOLDINGS_OK_CODES or (rsp_cd == "" and isinstance(data.get("t0424OutBlock"), dict))
    rows = data.get("t0424OutBlock1")
    if not ok or (rows is not None and not isinstance(rows, list)):
        raise LsApiError("t0424", f"잔고 조회 응답 이상: {rsp_cd} {clean_str(data.get('rsp_msg'))}",
                         rsp_cd=rsp_cd, rsp_msg=clean_str(data.get("rsp_msg")))


def _header(headers: dict, name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name:
            return clean_str(v)
    return ""


class MarketData:
    """조건검색과 시세 조회 (주문 기능 없음).

    모의투자 키로 조건검색이 안 되면 실전 키로 만든 클라이언트를 넣어 조회 전용으로 쓴다.
    """

    def __init__(self, client: LsRestClient, user_id: str) -> None:
        self.client = client
        self._user_id = user_id

    def list_conditions(self) -> list[Condition]:
        result: dict[str, Condition] = {}
        cont, cont_key = "0", ""
        tr_cont, tr_cont_key = "N", ""
        seen_keys: set[str] = set()
        for _ in range(50):
            data, headers = self.client.call("t1866", {"t1866InBlock": {
                "user_id": self._user_id, "gb": "0", "group_name": "",
                "cont": cont, "cont_key": cont_key,
            }}, tr_cont=tr_cont, tr_cont_key=tr_cont_key)
            for row in data.get("t1866OutBlock1") or []:
                # query_index 는 공백 패딩이 있을 수 있어 NUL 만 제거하고 그대로 쓴다
                qi = str(row.get("query_index") or "").split("\x00", 1)[0]
                if qi and qi not in result:
                    result[qi] = Condition(query_index=qi,
                                           group_name=clean_str(row.get("group_name")),
                                           query_name=clean_str(row.get("query_name")))
            out = data.get("t1866OutBlock") or {}
            cont = clean_str(out.get("cont"))
            cont_key = clean_str(out.get("contkey") or out.get("cont_key"))
            if cont != "1" or not cont_key or cont_key in seen_keys:
                break
            seen_keys.add(cont_key)
            tr_cont, tr_cont_key = "Y", _header(headers, "tr_cont_key")
        return list(result.values())

    def search_condition_once(self, query_index: str) -> list[tuple[str, str, int]]:
        """현재 조건을 만족하는 종목 (코드, 종목명, 현재가)."""
        data, _ = self.client.call("t1859", {"t1859InBlock": {"query_index": query_index}})
        rows = []
        for row in data.get("t1859OutBlock1") or []:
            code = clean_str(row.get("shcode"))
            if valid_code(code):
                rows.append((code, clean_str(row.get("hname")), to_int(row.get("price"))))
        return rows

    def register_realtime_condition(self, query_index: str) -> str:
        data, _ = self.client.call("t1860", {"t1860InBlock": {
            "sSysUserFlag": "U", "sFlag": "E", "sAlertNum": "", "query_index": query_index,
        }})
        out = data.get("t1860OutBlock") or {}
        alert_num = clean_str(out.get("sAlertNum"))
        if not alert_num:
            raise LsApiError("t1860", f"실시간 조건검색 등록 실패: {clean_str(out.get('Msg'))} "
                             f"{clean_str(data.get('rsp_cd'))} {clean_str(data.get('rsp_msg'))}",
                             rsp_cd=clean_str(data.get("rsp_cd")),
                             rsp_msg=clean_str(data.get("rsp_msg")))
        return alert_num

    def unregister_realtime_condition(self, query_index: str, alert_num: str) -> None:
        self.client.call("t1860", {"t1860InBlock": {
            "sSysUserFlag": "U", "sFlag": "D", "sAlertNum": alert_num, "query_index": query_index,
        }})

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        unique = list(dict.fromkeys(codes))
        for i in range(0, len(unique), MULTI_QUOTE_MAX):
            chunk = unique[i:i + MULTI_QUOTE_MAX]
            data, _ = self.client.call("t8407", {"t8407InBlock": {
                "nrec": len(chunk), "shcode": "".join(chunk),
            }})
            for row in data.get("t8407OutBlock1") or []:
                code = clean_str(row.get("shcode"))
                quotes[code] = Quote(code=code, name=clean_str(row.get("hname")),
                                     price=to_int(row.get("price")),
                                     ask=to_int(row.get("offerho")), bid=to_int(row.get("bidho")))
        return quotes


class Account:
    """주문과 잔고 (매매 계좌의 App Key 로 만든 클라이언트)."""

    def __init__(self, client: LsRestClient, mbr_no: str = "KRX") -> None:
        self.client = client
        self._mbr_no = mbr_no

    def account_summary(self) -> tuple[int, str]:
        """(현금주문가능금액, 응답메시지). 모의투자 키면 메시지에 '모의투자' 가 들어온다."""
        data, _ = self.client.call("CSPAQ12200", {"CSPAQ12200InBlock1": {"BalCreTp": "1"}})
        out2 = data.get("CSPAQ12200OutBlock2")
        if not isinstance(out2, dict):
            raise LsApiError("CSPAQ12200", f"계좌 조회 실패: {clean_str(data.get('rsp_cd'))} "
                             f"{clean_str(data.get('rsp_msg'))}")
        return to_int(out2.get("MnyOrdAbleAmt")), clean_str(data.get("rsp_msg"))

    def get_holdings(self) -> list[Holding]:
        holdings: list[Holding] = []
        cts, tr_cont, tr_cont_key = "", "N", ""
        for _ in range(50):
            data, headers = self.client.call(
                "t0424",
                # prcgb 1: 평균단가, chegb 2: 체결기준(당일 체결 즉시 반영), dangb 0: 정규장, charge 0: 제비용 미포함
                {"t0424InBlock": {"prcgb": "1", "chegb": "2", "dangb": "0", "charge": "0",
                                  "cts_expcode": cts}},
                tr_cont=tr_cont, tr_cont_key=tr_cont_key,
            )
            _check_holdings_page(data)
            for row in data.get("t0424OutBlock1") or []:
                code = clean_str(row.get("expcode"))
                if code.startswith("A") and len(code) == 7:
                    code = code[1:]
                qty = to_int(row.get("janqty"))
                if not code or qty <= 0:
                    continue
                holdings.append(Holding(
                    code=code, name=clean_str(row.get("hname")), qty=qty,
                    sellable_qty=to_int(row.get("mdposqt")), avg_price=to_float(row.get("pamt")),
                    last_price=to_int(row.get("price")),
                ))
            cts = clean_str((data.get("t0424OutBlock") or {}).get("cts_expcode"))
            if _header(headers, "tr_cont") != "Y" or not cts:
                break
            tr_cont, tr_cont_key = "Y", _header(headers, "tr_cont_key")
        return holdings

    def buy_market(self, code: str, qty: int) -> OrderResult:
        return self._order(code, qty, side="2")

    def sell_market(self, code: str, qty: int) -> OrderResult:
        return self._order(code, qty, side="1")

    def _order(self, code: str, qty: int, side: str) -> OrderResult:
        if not valid_code(code):
            raise ValueError(f"종목코드 형식 오류: {code!r}")
        if qty <= 0:
            raise ValueError("주문 수량은 1 이상이어야 합니다.")
        block = {
            "IsuNo": "A" + code,  # 모의투자는 A+종목코드 필수 (실전도 허용)
            "OrdQty": int(qty),
            "OrdPrc": 0,  # 시장가: 숫자 0 (문자열 "0" 은 오류)
            "BnsTpCode": side,  # 1: 매도, 2: 매수
            "OrdprcPtnCode": "03",  # 03: 시장가
            "MgntrnCode": "000",  # 000: 보통
            "LoanDt": "",
            "OrdCndiTpCode": "0",  # 0: 없음
            "MbrNo": self._mbr_no,  # KRX / NXT (그 외 값은 KRX 로 처리)
        }
        data, _ = self.client.call("CSPAT00601", {"CSPAT00601InBlock1": block})
        out2 = data.get("CSPAT00601OutBlock2") or {}
        ord_no = to_int(out2.get("OrdNo"))
        rsp_cd = clean_str(data.get("rsp_cd"))
        rsp_msg = clean_str(data.get("rsp_msg"))
        if ord_no <= 0:
            raise LsApiError("CSPAT00601", f"주문 거부: {rsp_cd} {rsp_msg}", rsp_cd=rsp_cd,
                             rsp_msg=rsp_msg)
        return OrderResult(ord_no=ord_no, rsp_cd=rsp_cd, rsp_msg=rsp_msg)


class LsBroker:
    """AutoTrader 가 쓰는 인터페이스: 시세는 MarketData, 잔고/주문은 Account."""

    def __init__(self, market: MarketData, account: Account) -> None:
        self.market = market
        self.account = account

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        return self.market.get_quotes(codes)

    def get_holdings(self) -> list[Holding]:
        return self.account.get_holdings()

    def buy_market(self, code: str, qty: int) -> OrderResult:
        return self.account.buy_market(code, qty)

    def sell_market(self, code: str, qty: int) -> OrderResult:
        return self.account.sell_market(code, qty)
