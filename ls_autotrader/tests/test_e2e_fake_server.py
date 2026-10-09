"""가짜 LS 서버(REST + WebSocket)를 띄워 main() 전체 흐름을 검증한다.

조건검색 등록 → AFR 신호 수신 → 시장가 매수 → 잔고 반영 → 익절 매도 → 종료 시 등록 해제.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

websockets = pytest.importorskip("websockets")
from websockets.asyncio.server import serve  # noqa: E402

from autotrader import main as main_mod  # noqa: E402
from autotrader import trader as trader_mod  # noqa: E402


class FakeLs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.prices = {"005930": 10_000}
        self.holdings: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.t1860: list[dict] = []
        self.ws_msgs: list[dict] = []
        self.ws_conns: list = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.tokens_issued = 0

    # ------------------------------------------------------------- REST
    def handle(self, path: str, headers, body: dict) -> tuple[int, dict]:
        if path == "/oauth2/token":
            self.tokens_issued += 1
            return 200, {"access_token": "TESTTOKEN", "token_type": "Bearer", "expires_in": 86400}
        if headers.get("authorization") != "Bearer TESTTOKEN":
            return 500, {"rsp_cd": "IGW00121", "rsp_msg": "유효하지 않은 token 입니다."}
        tr = headers.get("tr_cd")
        with self.lock:
            if tr == "t1866":
                return 200, {"t1866OutBlock": {"result_count": 1, "cont": "", "contkey": ""},
                             "t1866OutBlock1": [{"query_index": "tester  0003", "group_name": "나의전략",
                                                 "query_name": "떡상이"}], "rsp_cd": "00000"}
            if tr == "t1860":
                blk = body["t1860InBlock"]
                self.t1860.append(blk)
                alert = "1722490200A" if blk["sFlag"] == "E" else blk["sAlertNum"]
                return 200, {"t1860OutBlock": {"sFlag": blk["sFlag"], "sAlertNum": alert,
                                               "Msg": "정상처리 되었습니다."}, "rsp_cd": "00000"}
            if tr == "t1859":
                return 200, {"t1859OutBlock": {"result_count": 0}, "rsp_cd": "00000"}
            if tr == "t8407":
                codes = body["t8407InBlock"]["shcode"]
                rows = []
                for i in range(0, len(codes), 6):
                    c = codes[i:i + 6]
                    p = self.prices.get(c, 0)
                    rows.append({"shcode": c, "hname": "삼성전자", "price": p, "offerho": p,
                                 "bidho": p - 10})
                return 200, {"t8407OutBlock1": rows, "rsp_cd": "00000"}
            if tr == "CSPAQ12200":
                return 200, {"CSPAQ12200OutBlock2": {"MnyOrdAbleAmt": 10_000_000},
                             "rsp_cd": "00136", "rsp_msg": "모의투자 조회가 완료되었습니다."}
            if tr == "t0424":
                rows = [{"expcode": c, "hname": "삼성전자", "janqty": h["qty"], "mdposqt": h["qty"],
                         "pamt": h["avg"], "price": self.prices[c]} for c, h in self.holdings.items()]
                return 200, {"t0424OutBlock": {"cts_expcode": ""}, "t0424OutBlock1": rows,
                             "rsp_cd": "00000"}
            if tr == "CSPAT00601":
                blk = body["CSPAT00601InBlock1"]
                self.orders.append(blk)
                code = blk["IsuNo"][1:]
                qty = blk["OrdQty"]
                if blk["BnsTpCode"] == "2":
                    self.holdings[code] = {"qty": qty, "avg": self.prices[code]}
                    rsp = ("00040", "매수 주문이 완료되었습니다.")
                else:
                    self.holdings.pop(code, None)
                    rsp = ("00039", "매도 주문이 완료되었습니다.")
                return 200, {"CSPAT00601OutBlock2": {"OrdNo": len(self.orders)},
                             "rsp_cd": rsp[0], "rsp_msg": rsp[1]}
        return 404, {"rsp_cd": "99999", "rsp_msg": f"unknown {path} {tr}"}

    # -------------------------------------------------------- websocket
    async def ws_handler(self, conn) -> None:
        self.ws_conns.append(conn)
        async for raw in conn:
            msg = json.loads(raw)
            self.ws_msgs.append(msg)
            await conn.send(json.dumps({"header": {
                "tr_cd": msg["body"]["tr_cd"], "tr_key": msg["body"]["tr_key"],
                "tr_type": msg["header"]["tr_type"], "rsp_cd": "00000", "rsp_msg": "정상처리되었습니다"},
                "body": None}))

    def push_afr(self, code: str, flag: str, price: int) -> None:
        msg = json.dumps({"header": {"tr_cd": "AFR", "tr_key": "1722490200A"},
                          "body": {"gsCode": code, "gshname": "삼성전자", "gsJobFlag": flag,
                                   "gsPrice": str(price), "gsSign": "2", "gsChange": "100",
                                   "gsChgRate": "1.0", "gsVolume": "1000"}})
        for conn in list(self.ws_conns):
            asyncio.run_coroutine_threadsafe(conn.send(msg), self.loop).result(5)


def start_http(fake: FakeLs) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n).decode()
            body = json.loads(raw) if raw.startswith("{") else {}
            status, out = fake.handle(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            data = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("tr_cont", "N")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_ws(fake: FakeLs) -> int:
    ready = threading.Event()
    port = {}

    def run():
        loop = asyncio.new_event_loop()
        fake.loop = loop
        asyncio.set_event_loop(loop)

        async def go():
            async with serve(fake.ws_handler, "127.0.0.1", 0) as server:
                port["p"] = server.sockets[0].getsockname()[1]
                ready.set()
                await asyncio.Future()

        loop.run_until_complete(go())

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(5)
    return port["p"]


def wait_until(cond, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_full_flow(tmp_path, monkeypatch):
    fake = FakeLs()
    http = start_http(fake)
    ws_port = start_ws(fake)
    env = {
        "LS_APPKEY": "k", "LS_APPSECRET": "s", "LS_USER_ID": "tester",
        "CONDITION_NAME": "떡상이", "TRADING_MODE": "paper", "DRY_RUN": "false",
        "LS_REST_BASE": f"http://127.0.0.1:{http.server_address[1]}",
        "LS_WS_URL": f"ws://127.0.0.1:{ws_port}/websocket",
        "DATA_DIR": str(tmp_path), "PRICE_POLL_SEC": "0.5", "BALANCE_POLL_SEC": "1",
    }
    for k in list(os.environ):
        if k.startswith("LS_") or k in ("CONDITION_NAME", "CONDITION_INDEX", "TRADING_MODE"):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(trader_mod, "in_buy_window", lambda *a: True)  # 시험 시각과 무관하게
    monkeypatch.setattr(trader_mod, "in_sell_window", lambda *a: True)

    stop = threading.Event()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "rc", main_mod.main(["--env", str(tmp_path / "none.env")], stop_event=stop)), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: any(m["body"]["tr_cd"] == "AFR" for m in fake.ws_msgs)), \
            "AFR 등록 메시지가 오지 않음"
        sub = next(m for m in fake.ws_msgs if m["body"]["tr_cd"] == "AFR")
        assert sub == {"header": {"token": "TESTTOKEN", "tr_type": "3"},
                       "body": {"tr_cd": "AFR", "tr_key": "1722490200A"}}
        assert fake.t1860[0] == {"sSysUserFlag": "U", "sFlag": "E", "sAlertNum": "",
                                 "query_index": "tester  0003"}

        # 이탈 신호는 무시, 편입 신호에 매수
        fake.push_afr("005930", "O", 10_000)
        time.sleep(0.5)
        assert fake.orders == []
        fake.push_afr("005930", "N", 10_000)
        assert wait_until(lambda: len(fake.orders) == 1)
        assert fake.orders[0]["IsuNo"] == "A005930" and fake.orders[0]["OrdQty"] == 10
        assert fake.orders[0]["BnsTpCode"] == "2"

        # +10% → 익절
        with fake.lock:
            fake.prices["005930"] = 11_000
        assert wait_until(lambda: len(fake.orders) == 2)
        assert fake.orders[1]["BnsTpCode"] == "1" and fake.orders[1]["OrdQty"] == 10
        trades = (tmp_path / "trades.csv").read_text(encoding="utf-8-sig")
        assert "익절" in trades
    finally:
        stop.set()
        t.join(10)
        http.shutdown()
    assert result.get("rc") == 0
    # 종료 시 AFR 해제(tr_type 4) + t1860 해제(D)
    assert any(m["body"]["tr_cd"] == "AFR" and m["header"]["tr_type"] == "4" for m in fake.ws_msgs)
    assert fake.t1860[-1]["sFlag"] == "D" and fake.t1860[-1]["sAlertNum"] == "1722490200A"
    assert not (tmp_path / "realtime.json").exists()
    assert fake.tokens_issued == 1


def test_paper_key_guard_blocks_real_key(tmp_path, monkeypatch):
    """TRADING_MODE=paper 인데 실전 키로 보이면 주문 전에 멈춘다."""
    fake = FakeLs()
    orig = fake.handle

    def handle(path, headers, body):
        status, out = orig(path, headers, body)
        if headers.get("tr_cd") == "CSPAQ12200":
            out["rsp_msg"] = "조회가 완료되었습니다."
        return status, out

    fake.handle = handle
    http = start_http(fake)
    for k, v in {"LS_APPKEY": "k", "LS_APPSECRET": "s", "LS_USER_ID": "tester",
                 "CONDITION_NAME": "떡상이", "TRADING_MODE": "paper",
                 "LS_REST_BASE": f"http://127.0.0.1:{http.server_address[1]}",
                 "LS_WS_URL": "ws://127.0.0.1:9/websocket", "DATA_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    try:
        assert main_mod.main(["--env", str(tmp_path / "none.env")]) == 1
    finally:
        http.shutdown()
    assert fake.orders == []


def test_websocket_reconnect_resubscribes(tmp_path, monkeypatch):
    """서버가 연결을 끊으면 재접속 후 t1860 재등록 + AFR 재구독."""
    fake = FakeLs()
    http = start_http(fake)
    ws_port = start_ws(fake)
    for k, v in {"LS_APPKEY": "k", "LS_APPSECRET": "s", "LS_USER_ID": "tester",
                 "CONDITION_NAME": "떡상이", "TRADING_MODE": "paper",
                 "LS_REST_BASE": f"http://127.0.0.1:{http.server_address[1]}",
                 "LS_WS_URL": f"ws://127.0.0.1:{ws_port}/websocket",
                 "DATA_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    stop = threading.Event()
    t = threading.Thread(target=lambda: main_mod.main(["--env", str(tmp_path / "none.env")],
                                                      stop_event=stop), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: len(fake.ws_conns) == 1 and fake.ws_msgs)
        conn = fake.ws_conns[0]
        asyncio.run_coroutine_threadsafe(conn.close(), fake.loop)  # 서버가 연결을 끊음
        assert wait_until(lambda: len(fake.ws_conns) == 2, timeout=15), "재접속 안 됨"
        assert wait_until(lambda: sum(1 for b in fake.t1860 if b["sFlag"] == "E") >= 2, timeout=15)
        n_afr = sum(1 for m in fake.ws_msgs
                    if m["body"]["tr_cd"] == "AFR" and m["header"]["tr_type"] == "3")
        assert n_afr >= 2
    finally:
        stop.set()
        t.join(10)
        http.shutdown()


def _base_env(monkeypatch, tmp_path, http, ws_port=9, **extra):
    for k in list(os.environ):
        if k.startswith("LS_") or k in ("CONDITION_NAME", "CONDITION_INDEX", "TRADING_MODE",
                                        "SKIP_ENV_CHECK", "REAL_TRADING_CONFIRM", "DRY_RUN"):
            monkeypatch.delenv(k, raising=False)
    env = {"LS_APPKEY": "k", "LS_APPSECRET": "s", "LS_USER_ID": "tester",
           "CONDITION_NAME": "떡상이", "TRADING_MODE": "paper",
           "LS_REST_BASE": f"http://127.0.0.1:{http.server_address[1]}",
           "LS_WS_URL": f"ws://127.0.0.1:{ws_port}/websocket", "DATA_DIR": str(tmp_path), **extra}
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_afr_rejected_exits_with_help(tmp_path, monkeypatch, caplog):
    """CONF-2: AFR 구독이 거부되면 조용히 돌지 않고 안내 후 종료 + 등록 해제."""
    fake = FakeLs()

    async def reject(conn):
        fake.ws_conns.append(conn)
        async for raw in conn:
            msg = json.loads(raw)
            fake.ws_msgs.append(msg)
            await conn.send(json.dumps({"header": {
                "tr_cd": msg["body"]["tr_cd"], "tr_key": msg["body"]["tr_key"],
                "rsp_cd": "01900", "rsp_msg": "모의투자에서는 해당업무가 제공되지 않습니다."},
                "body": None}))

    fake.ws_handler = reject
    http = start_http(fake)
    ws_port = start_ws(fake)
    _base_env(monkeypatch, tmp_path, http, ws_port)
    try:
        rc = main_mod.main(["--env", str(tmp_path / "none.env")])
    finally:
        http.shutdown()
    assert rc == 1
    assert fake.t1860[-1]["sFlag"] == "D"
    assert fake.orders == []


def test_second_instance_refused(tmp_path, monkeypatch):
    """CR-12: 같은 데이터 폴더로 두 번 실행되지 않는다."""
    fake = FakeLs()
    http = start_http(fake)
    _base_env(monkeypatch, tmp_path, http)
    lock = main_mod.InstanceLock(tmp_path / "autotrader.lock")
    assert lock.acquire()
    try:
        assert main_mod.main(["--env", str(tmp_path / "none.env")]) == 2
    finally:
        lock.release()
        http.shutdown()


def test_skip_env_check_requires_confirm_phrase(tmp_path, monkeypatch):
    """MS-1: SKIP_ENV_CHECK 만으로는 키 확인을 우회할 수 없다."""
    fake = FakeLs()
    http = start_http(fake)
    _base_env(monkeypatch, tmp_path, http, SKIP_ENV_CHECK="true")
    try:
        assert main_mod.main(["--env", str(tmp_path / "none.env")]) == 2  # 설정 오류
    finally:
        http.shutdown()
    assert fake.orders == []


def test_state_file_is_per_mode_and_account(tmp_path, monkeypatch):
    """TL-2/MS-2: 상태 파일 이름에 모드와 계좌 지문이 들어간다."""
    fp = main_mod.account_fingerprint("k")
    assert len(fp) == 12 and fp != main_mod.account_fingerprint("k2")


def test_legacy_state_blocks_start_until_adopted(tmp_path, monkeypatch):
    """V2-TR-4/F3: 이전 버전 state.json 에 봇 종목이 있으면 조용히 버리지 않는다."""
    fake = FakeLs()
    http = start_http(fake)
    _base_env(monkeypatch, tmp_path, http)
    (tmp_path / "state.json").write_text(json.dumps(
        {"date": "2026-10-08", "managed": ["005930"], "pending_buys": {}, "pending_sells": {}}),
        encoding="utf-8")
    try:
        assert main_mod.main(["--env", str(tmp_path / "none.env")]) == 1
        assert main_mod.main(["--env", str(tmp_path / "none.env"),
                              "--adopt-state", str(tmp_path / "state.json")]) == 0
    finally:
        http.shutdown()
    assert (tmp_path / "state.json.adopted").exists()
    fp = main_mod.account_fingerprint("k")
    st = json.loads((tmp_path / f"state_paper_{fp}.json").read_text(encoding="utf-8"))
    assert st["managed"] == ["005930"] and st["identity"] == {"mode": "paper", "account": fp}


def test_runtime_afr_rejection_keeps_retrying(tmp_path, monkeypatch):
    """N1/F1: 재등록한 구독이 또 거부돼도 재시도를 멈추지 않는다."""
    fake = FakeLs()
    counter = {"n": 0}
    reject = set()
    orig = fake.handle

    def handle(path, headers, body):
        if headers.get("tr_cd") == "t1860" and body["t1860InBlock"]["sFlag"] == "E":
            with fake.lock:
                fake.t1860.append(body["t1860InBlock"])
                counter["n"] += 1
                return 200, {"t1860OutBlock": {"sAlertNum": f"A{counter['n']}"}, "rsp_cd": "00000"}
        return orig(path, headers, body)

    async def ws_handler(conn):
        fake.ws_conns.append(conn)
        async for raw in conn:
            msg = json.loads(raw)
            fake.ws_msgs.append(msg)
            key = msg["body"]["tr_key"]
            bad = msg["header"]["tr_type"] == "3" and key in reject
            await conn.send(json.dumps({"header": {
                "tr_cd": "AFR", "tr_key": key, "tr_type": msg["header"]["tr_type"],
                "rsp_cd": "99999" if bad else "00000", "rsp_msg": "거부" if bad else "정상"},
                "body": None}))

    fake.handle = handle
    fake.ws_handler = ws_handler
    http = start_http(fake)
    ws_port = start_ws(fake)
    _base_env(monkeypatch, tmp_path, http, ws_port)
    monkeypatch.setattr(main_mod, "REREGISTER_RETRY_SEC", 1.0)
    stop = threading.Event()
    t = threading.Thread(target=lambda: main_mod.main(["--env", str(tmp_path / "none.env")],
                                                      stop_event=stop), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: counter["n"] == 1 and len(fake.ws_msgs) >= 1)
        reject.update({"A1", "A2"})  # 현재 키와 다음 키 모두 거부
        asyncio.run_coroutine_threadsafe(fake.ws_conns[0].close(), fake.loop)
        assert wait_until(lambda: counter["n"] >= 3, timeout=25), f"재시도 멈춤 (E 호출 {counter['n']}회)"
        assert wait_until(lambda: any(m["body"]["tr_key"] == "A3" and m["header"]["tr_type"] == "3"
                                      for m in fake.ws_msgs), timeout=10)
    finally:
        stop.set()
        t.join(10)
        http.shutdown()


def test_adopt_refuses_paper_or_legacy_state_in_real_mode(tmp_path, monkeypatch):
    """R3-2/LC-2: 실거래 모드에서는 모의투자/구버전 상태를 가져오지 않는다."""
    from autotrader.trader import StateStore
    real_store = StateStore(tmp_path / "state_real_x.json", identity={"mode": "real", "account": "x"})
    legacy = tmp_path / "state.json"
    legacy.write_text(json.dumps({"managed": ["005930"]}), encoding="utf-8")
    assert main_mod.adopt_state(legacy, real_store) == 1
    paper = tmp_path / "state_paper_y.json"
    paper.write_text(json.dumps({"managed": ["005930"], "identity": {"mode": "paper", "account": "y"}}),
                     encoding="utf-8")
    assert main_mod.adopt_state(paper, real_store) == 1
    assert not real_store.path.exists()
    paper_store = StateStore(tmp_path / "state_paper_z.json", identity={"mode": "paper", "account": "z"})
    assert main_mod.adopt_state(paper, paper_store) == 0
    assert json.loads(paper_store.path.read_text(encoding="utf-8"))["managed"] == ["005930"]
