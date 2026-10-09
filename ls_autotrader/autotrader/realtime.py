"""LS증권 OPEN API WebSocket 클라이언트 (실시간 조건검색 AFR 수신용).

별도 스레드에서 접속을 유지하고, 끊기면 다시 접속해 등록했던 실시간 TR을 다시 등록한다.
수신 메시지는 콜백으로 넘기며, 매매 판단은 메인 스레드에서 한다.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import websocket  # websocket-client

log = logging.getLogger(__name__)

TR_TYPE_REGISTER = "3"
TR_TYPE_UNREGISTER = "4"


class RealtimeClient:
    def __init__(self, url: str, token_provider: Callable[[], str],
                 on_data: Callable[[str, str, dict], None],
                 on_reconnect: Callable[[], None] | None = None) -> None:
        self._url = url
        self._token = token_provider
        self._on_data = on_data
        self._on_reconnect = on_reconnect
        self._subs: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._connect_count = 0

    # ------------------------------------------------------------ public
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ls-websocket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def subscribe(self, tr_cd: str, tr_key: str) -> None:
        with self._lock:
            if (tr_cd, tr_key) not in self._subs:
                self._subs.append((tr_cd, tr_key))
        if self._connected.is_set():
            self._send(TR_TYPE_REGISTER, tr_cd, tr_key)

    def unsubscribe(self, tr_cd: str, tr_key: str) -> None:
        with self._lock:
            if (tr_cd, tr_key) in self._subs:
                self._subs.remove((tr_cd, tr_key))
        if self._connected.is_set():
            self._send(TR_TYPE_UNREGISTER, tr_cd, tr_key)

    def replace_subscription(self, tr_cd: str, old_key: str, new_key: str) -> None:
        with self._lock:
            if (tr_cd, old_key) in self._subs:
                self._subs.remove((tr_cd, old_key))
        self.subscribe(tr_cd, new_key)

    # ---------------------------------------------------------- internals
    def _send(self, tr_type: str, tr_cd: str, tr_key: str) -> None:
        msg = {"header": {"token": self._token(), "tr_type": tr_type},
               "body": {"tr_cd": tr_cd, "tr_key": tr_key}}
        ws = self._ws
        if ws is None:
            return
        try:
            ws.send(json.dumps(msg))
            log.info("실시간 %s: %s %s", "등록" if tr_type == TR_TYPE_REGISTER else "해제",
                     tr_cd, tr_key)
        except Exception as e:  # noqa: BLE001
            log.warning("실시간 요청 전송 실패 (%s %s): %s", tr_cd, tr_key, e)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self._url,
                on_open=self._handle_open,
                on_message=self._handle_message,
                on_error=lambda _ws, e: log.warning("WebSocket 오류: %s", e),
                on_close=lambda _ws, code, msg: log.warning("WebSocket 종료: %s %s", code, msg),
            )
            started = time.monotonic()
            try:
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:  # noqa: BLE001
                log.warning("WebSocket 실행 오류: %s", e)
            self._connected.clear()
            if self._stop.is_set():
                break
            if time.monotonic() - started > 60:
                backoff = 1.0
            log.info("WebSocket %.0f초 후 재접속", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _handle_open(self, _ws) -> None:
        self._connect_count += 1
        log.info("WebSocket 접속: %s", self._url)
        self._connected.set()
        if self._connect_count > 1 and self._on_reconnect is not None:
            try:
                self._on_reconnect()
            except Exception:  # noqa: BLE001
                log.exception("재접속 처리 중 오류")
        with self._lock:
            subs = list(self._subs)
        for tr_cd, tr_key in subs:
            self._send(TR_TYPE_REGISTER, tr_cd, tr_key)

    def _handle_message(self, _ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            log.warning("WebSocket 비정상 메시지: %r", raw[:200])
            return
        header = msg.get("header") or {}
        body = msg.get("body")
        tr_cd = str(header.get("tr_cd") or "")
        tr_key = str(header.get("tr_key") or "")
        if not body:
            # 등록/해제 응답 등 데이터가 없는 메시지
            rsp_cd = header.get("rsp_cd")
            rsp_msg = header.get("rsp_msg")
            if rsp_cd or rsp_msg:
                log.info("실시간 응답 %s %s: %s %s", tr_cd, tr_key, rsp_cd, rsp_msg)
            return
        try:
            self._on_data(tr_cd, tr_key, body)
        except Exception:  # noqa: BLE001
            log.exception("실시간 데이터 처리 오류: %s", raw[:300])
