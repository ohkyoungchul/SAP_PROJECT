from __future__ import annotations

import json

import pytest

from autotrader.ls_api import LsApiError, LsBroker, LsRestClient, RateLimiter, clean_str, to_int


class FakeResp:
    def __init__(self, status: int, body: dict, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, list[FakeResp]] = {}
        self.token_count = 0

    def queue(self, tr_cd: str, *resps: FakeResp) -> None:
        self.responses.setdefault(tr_cd, []).extend(resps)

    def post(self, url, headers=None, data=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "data": data, "json": json})
        if url.endswith("/oauth2/token"):
            self.token_count += 1
            return FakeResp(200, {"access_token": f"tok{self.token_count}", "expires_in": 86400,
                                  "token_type": "Bearer", "scope": "oob"})
        return self.responses[headers["tr_cd"]].pop(0)


class NoWaitLimiter(RateLimiter):
    def wait(self, key, per_sec):
        pass


@pytest.fixture
def api(settings):
    session = FakeSession()
    client = LsRestClient(settings, session=session, limiter=NoWaitLimiter())
    return session, client, LsBroker(client, settings)


def test_clean_helpers():
    assert clean_str("abc\x00\x00 ") == "abc"
    assert to_int("000.22") == 0
    assert to_int("4535") == 4535
    assert to_int("") == 0
    assert to_int(None) == 0


def test_token_request_format(api, settings):
    session, client, _ = api
    assert client.token == "tok1"
    call = session.calls[0]
    assert call["url"] == settings.rest_base + "/oauth2/token"
    assert call["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert call["data"] == {"grant_type": "client_credentials", "appkey": "key",
                            "appsecretkey": "secret", "scope": "oob"}
    _ = client.token
    assert session.token_count == 1  # 캐시됨


def test_order_request_format(api, settings):
    session, _, broker = api
    session.queue("CSPAT00601", FakeResp(200, {
        "rsp_cd": "00040", "rsp_msg": "매수 주문이 완료되었습니다.",
        "CSPAT00601OutBlock2": {"OrdNo": 32004}}))
    res = broker.buy_market("272210", 3)
    assert res.ord_no == 32004
    call = session.calls[-1]
    assert call["url"] == settings.rest_base + "/stock/order"
    assert call["headers"]["tr_cd"] == "CSPAT00601"
    assert call["headers"]["authorization"] == "Bearer tok1"
    block = call["json"]["CSPAT00601InBlock1"]
    assert block["IsuNo"] == "A272210"
    assert block["OrdQty"] == 3
    assert block["BnsTpCode"] == "2"
    assert block["OrdprcPtnCode"] == "03"
    assert block["OrdPrc"] == 0


def test_sell_side_code(api):
    session, _, broker = api
    session.queue("CSPAT00601", FakeResp(200, {"rsp_cd": "00039",
                                               "CSPAT00601OutBlock2": {"OrdNo": 5}}))
    broker.sell_market("005930", 1)
    assert session.calls[-1]["json"]["CSPAT00601InBlock1"]["BnsTpCode"] == "1"


def test_order_without_order_number_raises(api):
    session, _, broker = api
    session.queue("CSPAT00601", FakeResp(200, {"rsp_cd": "02714", "rsp_msg": "주문가능금액 부족"}))
    with pytest.raises(LsApiError) as e:
        broker.buy_market("005930", 1)
    assert "주문가능금액" in str(e.value)


def test_http_error_raises(api):
    session, _, broker = api
    session.queue("t8407", FakeResp(500, {"rsp_cd": "IGW00000", "rsp_msg": "오류"}))
    with pytest.raises(LsApiError):
        broker.get_quotes(["005930"])


def test_token_error_retries_once(api):
    session, _, broker = api
    session.queue("t8407",
                  FakeResp(401, {"rsp_cd": "IGW00121", "rsp_msg": "token invalid"}),
                  FakeResp(200, {"t8407OutBlock1": [{"shcode": "005930", "price": 70000,
                                                     "offerho": 70100, "bidho": 70000,
                                                     "hname": "삼성전자"}]}))
    q = broker.get_quotes(["005930"])
    assert q["005930"].price == 70000
    assert session.token_count == 2


def test_multi_quote_request(api):
    session, _, broker = api
    session.queue("t8407", FakeResp(200, {"t8407OutBlock1": [
        {"shcode": "078020", "price": 4530, "offerho": 4540, "bidho": 4530, "hname": "A"},
        {"shcode": "000660", "price": 108700, "offerho": 108800, "bidho": 108700, "hname": "B"},
    ]}))
    q = broker.get_quotes(["078020", "000660", "078020"])
    body = session.calls[-1]["json"]["t8407InBlock"]
    assert body == {"nrec": 2, "shcode": "078020000660"}
    assert q["000660"].ask == 108800


def test_holdings_parsing(api):
    session, _, broker = api
    session.queue("t0424", FakeResp(200, {
        "t0424OutBlock": {"cts_expcode": ""},
        "t0424OutBlock1": [
            {"expcode": "005930", "hname": "삼성전자", "janqty": 2, "mdposqt": 2,
             "pamt": 60000, "price": 75300},
            {"expcode": "000660", "hname": "zero", "janqty": 0, "mdposqt": 0,
             "pamt": 0, "price": 0},
        ]}))
    hs = broker.get_holdings()
    assert len(hs) == 1
    h = hs[0]
    assert (h.code, h.qty, h.sellable_qty, h.avg_price, h.last_price) == ("005930", 2, 2, 60000.0, 75300)


def test_condition_list_and_register(api):
    session, _, broker = api
    session.queue("t1866", FakeResp(200, {
        "t1866OutBlock": {"result_count": 1, "cont": "", "contkey": ""},
        "t1866OutBlock1": [{"query_index": "tester0000", "group_name": "나의전략",
                            "query_name": "떡상이\x00\x00"}]}))
    conds = broker.list_conditions()
    assert conds[0].query_name == "떡상이"
    assert session.calls[-1]["json"]["t1866InBlock"]["user_id"] == "tester"

    session.queue("t1860", FakeResp(200, {"t1860OutBlock": {
        "sSysUserFlag": "U", "sFlag": "E", "sResultFlag": "S", "sAlertNum": "1722490200A",
        "Msg": "정상처리 되었습니다."}}))
    assert broker.register_realtime_condition("tester0000") == "1722490200A"
    body = session.calls[-1]["json"]["t1860InBlock"]
    assert body == {"sSysUserFlag": "U", "sFlag": "E", "sAlertNum": "", "query_index": "tester0000"}
