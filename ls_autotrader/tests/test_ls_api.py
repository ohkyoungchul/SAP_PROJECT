from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import requests

from autotrader.ls_api import (KST, Account, LsApiError, LsRestClient, MarketData, RateLimiter,
                               clean_str, next_token_expiry, to_int, valid_code)


class FakeResp:
    def __init__(self, status: int, body: dict | None, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body, ensure_ascii=False) if body is not None else "<html>err</html>"

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, list] = {}
        self.token_count = 0

    def queue(self, tr_cd: str, *resps) -> None:
        self.responses.setdefault(tr_cd, []).extend(resps)

    def post(self, url, headers=None, data=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "data": data, "json": json})
        if url.endswith("/oauth2/token"):
            self.token_count += 1
            return FakeResp(200, {"access_token": f"tok{self.token_count}", "expires_in": 86400,
                                  "token_type": "Bearer", "scope": "oob"})
        r = self.responses[headers["tr_cd"]].pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class NoWaitLimiter(RateLimiter):
    def wait(self, key, per_sec):
        pass


BASE = "https://example.invalid:8080"


def make_client(session, tmp_path=None, now=None):
    return LsRestClient("key", "secret", BASE, label="test", session=session,
                        limiter=NoWaitLimiter(), token_cache_dir=tmp_path,
                        now=now or (lambda: datetime(2026, 10, 8, 10, 0, tzinfo=KST)),
                        sleep=lambda s: None)


@pytest.fixture
def api():
    session = FakeSession()
    client = make_client(session)
    return session, client, MarketData(client, "tester"), Account(client)


# ---------------------------------------------------------------- helpers
def test_clean_helpers():
    assert clean_str("abc\x00\x00 ") == "abc"
    assert clean_str("NH투자증권\x00?") == "NH투자증권"
    assert to_int("000.22") == 0
    assert to_int("4535") == 4535
    assert to_int("") == 0
    assert to_int(None) == 0
    assert valid_code("005930") and valid_code("0009K0")
    assert not valid_code("A005930") and not valid_code("\x007720") and not valid_code("")


def test_token_expiry_is_next_0700_kst():
    issued = datetime(2026, 10, 8, 12, 53, tzinfo=KST)
    assert next_token_expiry(issued, 86400) == datetime(2026, 10, 9, 7, 0, tzinfo=KST)
    early = datetime(2026, 10, 8, 6, 30, tzinfo=KST)
    assert next_token_expiry(early, 86400) == datetime(2026, 10, 8, 7, 0, tzinfo=KST)
    assert next_token_expiry(issued, 3600) == issued + timedelta(hours=1)


# ------------------------------------------------------------------ token
def test_token_request_format(api):
    session, client, _, _ = api
    assert client.token == "tok1"
    call = session.calls[0]
    assert call["url"] == BASE + "/oauth2/token"
    assert call["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert call["data"] == {"grant_type": "client_credentials", "appkey": "key",
                            "appsecretkey": "secret", "scope": "oob"}
    _ = client.token
    assert session.token_count == 1  # 캐시됨


def test_token_refreshed_after_0700():
    session = FakeSession()
    clock = {"now": datetime(2026, 10, 8, 10, 0, tzinfo=KST)}
    client = make_client(session, now=lambda: clock["now"])
    assert client.token == "tok1"
    clock["now"] = datetime(2026, 10, 9, 6, 50, tzinfo=KST)
    assert client.token == "tok1"
    clock["now"] = datetime(2026, 10, 9, 6, 56, tzinfo=KST)  # 07:00 5분 전부터 갱신
    assert client.token == "tok2"


def test_token_file_cache_reused_across_restart(tmp_path):
    s1 = FakeSession()
    assert make_client(s1, tmp_path).token == "tok1"
    s2 = FakeSession()
    c2 = make_client(s2, tmp_path)
    assert c2.token == "tok1"
    assert s2.token_count == 0  # 재시작해도 재발급하지 않음


def test_token_error_retries_once(api):
    session, _, market, _ = api
    session.queue("t8407",
                  FakeResp(500, {"rsp_cd": "IGW00121", "rsp_msg": "유효하지 않은 token 입니다."}),
                  FakeResp(200, {"t8407OutBlock1": [{"shcode": "005930", "price": 70000,
                                                     "offerho": 70100, "bidho": 70000,
                                                     "hname": "삼성전자"}]}))
    q = market.get_quotes(["005930"])
    assert q["005930"].price == 70000
    assert session.token_count == 2


# ------------------------------------------------------------- error codes
def test_rate_limit_retried_for_queries(api):
    session, _, market, _ = api
    session.queue("t8407",
                  FakeResp(500, {"rsp_cd": "IGW00201", "rsp_msg": "호출 거래건수를 초과하였습니다."}),
                  FakeResp(200, {"t8407OutBlock1": []}))
    assert market.get_quotes(["005930"]) == {}


def test_rate_limit_not_retried_for_orders(api):
    session, _, _, account = api
    session.queue("CSPAT00601",
                  FakeResp(500, {"rsp_cd": "IGW00201", "rsp_msg": "호출 거래건수를 초과하였습니다."}))
    with pytest.raises(LsApiError) as e:
        account.buy_market("005930", 1)
    assert not e.value.ambiguous
    assert len([c for c in session.calls if c["headers"].get("tr_cd") == "CSPAT00601"]) == 1


def test_paper_unsupported_detected_even_with_http_200(api):
    session, _, market, _ = api
    session.queue("t1866", FakeResp(200, {"rsp_cd": "01900",
                                          "rsp_msg": "모의투자에서는 해당업무가 제공되지 않습니다."}))
    with pytest.raises(LsApiError) as e:
        market.list_conditions()
    assert e.value.paper_unsupported


def test_http_error_raises(api):
    session, _, market, _ = api
    session.queue("t8407", FakeResp(500, {"rsp_cd": "IGW00000", "rsp_msg": "오류"}))
    with pytest.raises(LsApiError):
        market.get_quotes(["005930"])


# ------------------------------------------------------------------ orders
def test_order_request_format(api):
    session, _, _, account = api
    session.queue("CSPAT00601", FakeResp(200, {
        "rsp_cd": "00040", "rsp_msg": "매수 주문이 완료되었습니다.",
        "CSPAT00601OutBlock2": {"OrdNo": 32004}}))
    res = account.buy_market("272210", 3)
    assert res.ord_no == 32004
    call = session.calls[-1]
    assert call["url"] == BASE + "/stock/order"
    h = call["headers"]
    assert h["tr_cd"] == "CSPAT00601" and h["authorization"] == "Bearer tok1"
    assert h["tr_cont"] == "N" and h["content-type"].startswith("application/json")
    block = call["json"]["CSPAT00601InBlock1"]
    assert block == {"IsuNo": "A272210", "OrdQty": 3, "OrdPrc": 0, "BnsTpCode": "2",
                     "OrdprcPtnCode": "03", "MgntrnCode": "000", "LoanDt": "",
                     "OrdCndiTpCode": "0", "MbrNo": "KRX"}
    assert isinstance(block["OrdPrc"], int) and isinstance(block["OrdQty"], int)


def test_sell_side_code(api):
    session, _, _, account = api
    session.queue("CSPAT00601", FakeResp(200, {"rsp_cd": "00039",
                                               "CSPAT00601OutBlock2": {"OrdNo": 5}}))
    account.sell_market("005930", 1)
    assert session.calls[-1]["json"]["CSPAT00601InBlock1"]["BnsTpCode"] == "1"


def test_order_rejection_is_not_ambiguous(api):
    session, _, _, account = api
    session.queue("CSPAT00601", FakeResp(200, {"rsp_cd": "02714", "rsp_msg": "주문가능금액 부족"}))
    with pytest.raises(LsApiError) as e:
        account.buy_market("005930", 1)
    assert "주문가능금액" in str(e.value)
    assert not e.value.ambiguous


def test_order_timeout_is_ambiguous_and_not_retried(api):
    session, _, _, account = api
    session.queue("CSPAT00601", requests.Timeout("read timeout"))
    with pytest.raises(LsApiError) as e:
        account.buy_market("005930", 1)
    assert e.value.ambiguous


def test_order_5xx_without_body_is_ambiguous(api):
    session, _, _, account = api
    session.queue("CSPAT00601", FakeResp(502, None))
    with pytest.raises(LsApiError) as e:
        account.sell_market("005930", 1)
    assert e.value.ambiguous


def test_invalid_code_rejected_before_sending(api):
    session, _, _, account = api
    with pytest.raises(ValueError):
        account.buy_market("A005930", 1)
    assert session.calls == []


# ------------------------------------------------------------- queries
def test_multi_quote_request(api):
    session, _, market, _ = api
    session.queue("t8407", FakeResp(200, {"t8407OutBlock1": [
        {"shcode": "078020", "price": 4530, "offerho": 4540, "bidho": 4530, "hname": "A"},
        {"shcode": "000660", "price": 108700, "offerho": 108800, "bidho": 108700, "hname": "B"},
    ]}))
    q = market.get_quotes(["078020", "000660", "078020"])
    body = session.calls[-1]["json"]["t8407InBlock"]
    assert body == {"nrec": 2, "shcode": "078020000660"}
    assert q["000660"].ask == 108800


def test_holdings_parsing_and_paging(api):
    session, _, _, account = api
    session.queue(
        "t0424",
        FakeResp(200, {
            "t0424OutBlock": {"cts_expcode": "000660"},
            "t0424OutBlock1": [
                {"expcode": "005930", "hname": "삼성전자", "janqty": 2, "mdposqt": 2,
                 "pamt": 60000, "price": 75300},
                {"expcode": "000001", "hname": "zero", "janqty": 0, "mdposqt": 0,
                 "pamt": 0, "price": 0},
            ]}, headers={"tr_cont": "Y", "tr_cont_key": "KEY123"}),
        FakeResp(200, {
            "t0424OutBlock": {"cts_expcode": ""},
            "t0424OutBlock1": [{"expcode": "000660", "hname": "SK하이닉스", "janqty": 1,
                                "mdposqt": 0, "pamt": "100000.5", "price": 101000}]},
            headers={"tr_cont": "N"}),
    )
    hs = account.get_holdings()
    assert [(h.code, h.qty, h.sellable_qty, h.avg_price, h.last_price) for h in hs] == [
        ("005930", 2, 2, 60000.0, 75300), ("000660", 1, 0, 100000.5, 101000)]
    first, second = [c for c in session.calls if c["headers"].get("tr_cd") == "t0424"]
    assert first["json"]["t0424InBlock"] == {"prcgb": "1", "chegb": "2", "dangb": "0",
                                             "charge": "0", "cts_expcode": ""}
    assert second["headers"]["tr_cont"] == "Y" and second["headers"]["tr_cont_key"] == "KEY123"
    assert second["json"]["t0424InBlock"]["cts_expcode"] == "000660"


def test_condition_list_paging_and_register(api):
    session, _, market, _ = api
    session.queue(
        "t1866",
        FakeResp(200, {"t1866OutBlock": {"result_count": 1, "cont": "1", "contkey": "CK"},
                       "t1866OutBlock1": [{"query_index": "tester  0000", "group_name": "나의전략",
                                           "query_name": "떡상이\x00\x00"}]}),
        FakeResp(200, {"t1866OutBlock": {"result_count": 1, "cont": "", "contkey": ""},
                       "t1866OutBlock1": [{"query_index": "tester  0001", "group_name": "나의전략",
                                           "query_name": "기타"}]}),
    )
    conds = market.list_conditions()
    assert [c.query_name for c in conds] == ["떡상이", "기타"]
    assert conds[0].query_index == "tester  0000"  # 공백 패딩 유지
    calls = [c for c in session.calls if c["headers"].get("tr_cd") == "t1866"]
    assert calls[0]["json"]["t1866InBlock"] == {"user_id": "tester", "gb": "0", "group_name": "",
                                                "cont": "0", "cont_key": ""}
    assert calls[1]["json"]["t1866InBlock"]["cont"] == "1"
    assert calls[1]["json"]["t1866InBlock"]["cont_key"] == "CK"

    session.queue("t1860", FakeResp(200, {"t1860OutBlock": {
        "sSysUserFlag": "U", "sFlag": "E", "sResultFlag": "S", "sAlertNum": "1722490200A",
        "Msg": "정상처리 되었습니다."}}))
    assert market.register_realtime_condition("tester  0000") == "1722490200A"
    body = session.calls[-1]["json"]["t1860InBlock"]
    assert body == {"sSysUserFlag": "U", "sFlag": "E", "sAlertNum": "", "query_index": "tester  0000"}

    session.queue("t1860", FakeResp(200, {"t1860OutBlock": {"sFlag": "D"}}))
    market.unregister_realtime_condition("tester  0000", "1722490200A")
    body = session.calls[-1]["json"]["t1860InBlock"]
    assert body["sFlag"] == "D" and body["sAlertNum"] == "1722490200A"


def test_register_without_alert_num_fails(api):
    session, _, market, _ = api
    session.queue("t1860", FakeResp(200, {"t1860OutBlock": {"sAlertNum": "", "Msg": "실패"}}))
    with pytest.raises(LsApiError):
        market.register_realtime_condition("tester0000")


def test_search_once_filters_bad_codes(api):
    session, _, market, _ = api
    session.queue("t1859", FakeResp(200, {"t1859OutBlock": {"result_count": 2},
                                          "t1859OutBlock1": [
                                              {"shcode": "005930", "hname": "삼성전자", "price": 70000},
                                              {"shcode": "\x007720", "hname": "?", "price": 1}]}))
    assert market.search_condition_once("q") == [("005930", "삼성전자", 70000)]
    assert session.calls[-1]["headers"]["tr_cd"] == "t1859"


def test_rate_limiter_spacing():
    t = {"now": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        t["now"] += s

    lim = RateLimiter(safety=1.0, global_tps=100, clock=lambda: t["now"], sleep=sleep)
    lim.wait("t0424", 2)
    lim.wait("t0424", 2)
    assert slept == [0.5]
    lim.wait("t8407", 5)
    assert slept[-1] == pytest.approx(0.01)


# ---------------------------------------------------------------- 리뷰 지적 회귀 테스트
def test_t0424_error_body_raises_instead_of_empty(api):
    """CONF-1: HTTP 200 오류 응답을 '보유 없음'으로 읽지 않는다."""
    session, _, _, account = api
    session.queue("t0424", FakeResp(200, {"rsp_cd": "02001", "rsp_msg": "조회중 오류"}))
    with pytest.raises(LsApiError):
        account.get_holdings()
    session.queue("t0424", FakeResp(200, None))  # JSON 아님
    with pytest.raises(LsApiError):
        account.get_holdings()


def test_t0424_empty_account_ok(api):
    session, _, _, account = api
    session.queue("t0424", FakeResp(200, {"rsp_cd": "00000", "rsp_msg": "조회완료"}))
    assert account.get_holdings() == []
    session.queue("t0424", FakeResp(200, {"rsp_cd": "00200", "rsp_msg": "조회내역이 없습니다."}))
    assert account.get_holdings() == []


def test_token_issued_just_before_0700_refreshes_once_after():
    """CONF-5/CR-6: 07:00 직전에는 재발급을 반복하지 않는다."""
    session = FakeSession()
    clock = {"now": datetime(2026, 10, 9, 6, 56, tzinfo=KST)}
    client = make_client(session, now=lambda: clock["now"])
    assert client.token == "tok1"
    for minute, sec in ((57, 0), (58, 30), (59, 50), (0, 2)):
        clock["now"] = datetime(2026, 10, 9, 6 if minute else 7, minute, sec, tzinfo=KST)
        _ = client.token
    assert session.token_count == 1
    clock["now"] = datetime(2026, 10, 9, 7, 1, 50, tzinfo=KST)
    assert client.token == "tok1"  # PC 시계가 빠를 수 있어 07:02 까지 기다림
    clock["now"] = datetime(2026, 10, 9, 7, 2, 1, tzinfo=KST)
    assert client.token == "tok2"
    assert session.token_count == 2


def test_stale_token_after_0700_is_retried_soon():
    """N3: PC 시계가 빨라 07:00 직후에 어제 토큰이 다시 오면 1분 뒤 다시 받는다."""
    class Stale(FakeSession):
        def post(self, url, **kw):
            r = super().post(url, **kw)
            if url.endswith("/oauth2/token") and self.token_count <= 2:
                r._body["access_token"] = "OLD"
            return r

    session = Stale()
    clock = {"now": datetime(2026, 10, 9, 6, 56, tzinfo=KST)}
    client = make_client(session, now=lambda: clock["now"])
    assert client.token == "OLD"
    v = client.token_version
    clock["now"] = datetime(2026, 10, 9, 7, 2, 1, tzinfo=KST)
    assert client.token == "OLD"  # 서버는 아직 07:00 전이라 같은 토큰
    clock["now"] = datetime(2026, 10, 9, 7, 3, 2, tzinfo=KST)
    assert client.token == "tok3"  # 1분 뒤 새 토큰
    assert client.token_version == v + 1


def test_same_token_does_not_bump_version():
    class SameToken(FakeSession):
        def post(self, url, **kw):
            r = super().post(url, **kw)
            if url.endswith("/oauth2/token"):
                r._body["access_token"] = "SAME"
            return r

    session = SameToken()
    client = make_client(session)
    _ = client.token
    v = client.token_version
    client.invalidate_token()
    assert client.token == "SAME"
    assert client.token_version == v


def test_t1866_stuck_paging_stops_and_dedupes(api):
    """CONF-6: 같은 연속키가 반복되면 멈추고 중복을 제거한다."""
    session, _, market, _ = api
    page = {"t1866OutBlock": {"cont": "1", "contkey": "SAME"},
            "t1866OutBlock1": [{"query_index": "tester  0003", "group_name": "g", "query_name": "떡상이"}]}
    session.queue("t1866", *[FakeResp(200, page, headers={"tr_cont": "Y", "tr_cont_key": "HK"})
                             for _ in range(5)])
    conds = market.list_conditions()
    assert [c.query_name for c in conds] == ["떡상이"]
    calls = [c for c in session.calls if c["headers"].get("tr_cd") == "t1866"]
    assert len(calls) == 2
    assert calls[1]["headers"]["tr_cont"] == "Y" and calls[1]["headers"]["tr_cont_key"] == "HK"


def test_query_uses_short_timeout_order_uses_long(settings):
    seen = []

    class S(FakeSession):
        def post(self, url, headers=None, data=None, json=None, timeout=None):
            if headers and "tr_cd" in headers:
                seen.append((headers["tr_cd"], timeout))
            return super().post(url, headers=headers, data=data, json=json, timeout=timeout)

    session = S()
    client = make_client(session)
    session.queue("t8407", FakeResp(200, {"t8407OutBlock1": []}))
    session.queue("CSPAT00601", FakeResp(200, {"CSPAT00601OutBlock2": {"OrdNo": 1}}))
    MarketData(client, "u").get_quotes(["005930"])
    Account(client).buy_market("005930", 1)
    assert seen == [("t8407", 5.0), ("CSPAT00601", 10.0)]


def test_account_summary(api):
    session, _, _, account = api
    session.queue("CSPAQ12200", FakeResp(200, {"rsp_cd": "00136", "rsp_msg": "모의투자 조회가 완료되었습니다.",
                                               "CSPAQ12200OutBlock2": {"MnyOrdAbleAmt": 5_000_000}}))
    assert account.account_summary() == (5_000_000, "모의투자 조회가 완료되었습니다.")
