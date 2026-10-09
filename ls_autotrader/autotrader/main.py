"""실행 진입점: python -m autotrader [--list-conditions] [--env 경로]"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import ConfigError, Settings, load_env_file
from .ls_api import Account, LsApiError, LsBroker, LsRestClient, MarketData, clean_str, to_int
from .models import Condition, ConditionSignal
from .realtime import RealtimeClient
from .trader import AutoTrader, StateError, StateStore, TradeLog

log = logging.getLogger("autotrader")

LOOP_SLEEP_SEC = 0.2
REREGISTER_RETRY_SEC = 10.0
TOKEN_TOUCH_SEC = 30.0
AFR_ACK_WAIT_SEC = 5.0

PAPER_CONDITION_HELP = (
    "모의투자 App Key 로는 서버저장 조건검색(t1866/t1860/AFR)이 제공되지 않는 것으로 보입니다.\n"
    "  → .env 에 실전 App Key 를 조회 전용으로 넣어 주세요:\n"
    "       LS_DATA_APPKEY=실전 App Key\n"
    "       LS_DATA_APPSECRET=실전 Secret Key\n"
    "    이 키는 조건검색과 현재가 조회에만 쓰이고, 주문은 계속 모의투자 키(LS_APPKEY)로 나갑니다."
)

_log_listener: logging.handlers.QueueListener | None = None


def setup_logging(data_dir: Path) -> None:
    """콘솔 출력은 별도 스레드로 보낸다.

    Windows 콘솔에서 마우스로 글자를 선택하면(빠른 편집 모드) 콘솔 쓰기가 멈추는데,
    그때 매매 루프까지 멈추지 않게 하기 위해서다. 파일 로그는 바로 쓴다.
    """
    global _log_listener
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = logging.FileHandler(log_dir / f"autotrader_{datetime.now():%Y%m%d}.log", encoding="utf-8")
    file.setFormatter(fmt)
    q: "queue.Queue[logging.LogRecord]" = queue.Queue()
    if _log_listener is not None:
        _log_listener.stop()
    _log_listener = logging.handlers.QueueListener(q, console)
    _log_listener.start()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [logging.handlers.QueueHandler(q), file]
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


def disable_quick_edit() -> None:
    """Windows 콘솔 '빠른 편집 모드'를 끈다 (창 클릭으로 프로그램이 멈추는 것 방지)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x40, 0x80
            kernel32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
    except Exception:  # noqa: BLE001
        pass


class InstanceLock:
    """같은 DATA_DIR 로 두 개가 동시에 실행되지 않게 한다 (프로세스가 죽으면 OS 가 잠금을 푼다)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._f = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


def pick_condition(conditions: list[Condition], settings: Settings) -> Condition:
    if settings.condition_index:
        for c in conditions:
            if c.query_index.strip() == settings.condition_index.strip():
                return c
        raise SystemExit(f"CONDITION_INDEX={settings.condition_index} 인 조건식을 찾지 못했습니다. "
                         "--list-conditions 로 목록을 확인하세요.")
    matches = [c for c in conditions if c.query_name == settings.condition_name]
    if not matches:
        raise SystemExit(f"이름이 '{settings.condition_name}' 인 서버저장 조건식을 찾지 못했습니다. "
                         "--list-conditions 로 목록을 확인하세요.")
    if len(matches) > 1:
        idx = ", ".join(repr(c.query_index) for c in matches)
        raise SystemExit(f"이름이 같은 조건식이 여러 개입니다({idx}). CONDITION_INDEX 로 지정하세요.")
    return matches[0]


def parse_afr(body: dict) -> ConditionSignal | None:
    code = clean_str(body.get("gsCode"))
    if not code:
        return None
    return ConditionSignal(
        code=code,
        name=clean_str(body.get("gshname")),
        job_flag=clean_str(body.get("gsJobFlag")).upper(),
        price=to_int(body.get("gsPrice")),
    )


class RealtimeRegistry:
    """실시간 조건검색 등록 정보를 파일에 남겨, 비정상 종료 후 재시작 때 해제할 수 있게 한다."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[str, str] | None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d["query_index"], d["alert_num"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, query_index: str, alert_num: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"query_index": query_index, "alert_num": alert_num}),
                                 encoding="utf-8")
        except OSError as e:
            log.warning("실시간 등록 정보 저장 실패: %s", e)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def account_fingerprint(appkey: str) -> str:
    return hashlib.sha256(appkey.encode()).hexdigest()[:12]


def check_trading_environment(account: Account, settings: Settings) -> bool:
    """모의투자로 설정했는데 실전 키를 넣은 경우(또는 그 반대)를 막는다."""
    cash, msg = account.account_summary()
    is_paper_server = "모의투자" in msg
    log.info("매매 계좌 확인: 현금주문가능금액 %s원 (%s)", f"{cash:,}", msg)
    if not settings.is_real and not is_paper_server:
        if settings.skip_env_check:
            log.warning("SKIP_ENV_CHECK=true: 모의투자 키 확인 결과를 무시하고 진행합니다 (응답: %s)", msg)
            return True
        log.error("TRADING_MODE=paper 인데 LS_APPKEY 계좌 응답에 '모의투자' 표시가 없습니다 (응답: %s).\n"
                  "  LS_APPKEY / LS_APPSECRET 에 실전 키를 넣지 않았는지 확인하세요. 실전 키라면 "
                  "이대로 실행할 경우 실제 돈으로 주문됩니다.", msg)
        return False
    if settings.is_real and is_paper_server:
        log.error("TRADING_MODE=real 인데 LS_APPKEY 가 모의투자 키입니다 (응답: %s).", msg)
        return False
    return True


def main(argv: list[str] | None = None, stop_event: threading.Event | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotrader", description="LS증권 조건검색 자동매매")
    parser.add_argument("--env", default=".env", help=".env 파일 경로 (기본: .env)")
    parser.add_argument("--list-conditions", action="store_true",
                        help="서버에 저장된 조건식 목록만 출력하고 종료")
    args = parser.parse_args(argv)

    try:
        load_env_file(Path(args.env))
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2

    lock = InstanceLock(settings.data_dir / "autotrader.lock")
    if not args.list_conditions and not lock.acquire():
        print(f"이미 같은 데이터 폴더({settings.data_dir})로 실행 중인 자동매매가 있습니다.", file=sys.stderr)
        return 2
    try:
        return _run(args, settings, stop_event or threading.Event())
    finally:
        lock.release()


def _run(args: argparse.Namespace, settings: Settings, stop_event: threading.Event) -> int:
    setup_logging(settings.data_dir)
    disable_quick_edit()
    token_dir = settings.data_dir / "tokens"
    trade_label = "실전" if settings.is_real else "모의투자"
    trade_client = LsRestClient(settings.appkey, settings.appsecret, settings.rest_base,
                                label=trade_label, token_cache_dir=token_dir,
                                mac_address=settings.mac_address)
    if settings.uses_data_key:
        data_client = LsRestClient(settings.data_appkey, settings.data_appsecret, settings.rest_base,
                                   label="조회전용(실전)", token_cache_dir=token_dir,
                                   mac_address=settings.mac_address)
    else:
        data_client = trade_client
    market = MarketData(data_client, settings.user_id)
    account = Account(trade_client, settings.order_mbr_no)
    broker = LsBroker(market, account)

    # 1) 서버저장 조건식 확인
    try:
        conditions = market.list_conditions()
    except LsApiError as e:
        log.error("조건식 목록(t1866) 조회 실패: %s", e)
        if e.paper_unsupported:
            log.error(PAPER_CONDITION_HELP)
        return 1

    if args.list_conditions:
        print(f"서버저장 조건식 {len(conditions)}개 (LS_USER_ID={settings.user_id})")
        for c in conditions:
            print(f"  {c.query_index!r}\t[{c.group_name}] {c.query_name}")
        if not conditions:
            print("  → HTS 종목검색 화면에서 조건식을 'API보내기'(또는 전략관리 → 서버저장) 했는지, "
                  "LS_USER_ID 가 HTS 로그인 ID 인지 확인하세요.")
        return 0

    condition = pick_condition(conditions, settings)

    # 2) 매매 계좌 확인
    try:
        if not check_trading_environment(account, settings):
            return 1
    except LsApiError as e:
        log.error("매매 계좌 조회(CSPAQ12200) 실패: %s", e)
        return 1

    log.info("=" * 64)
    log.info("모드: %s%s", "실거래" if settings.is_real else "모의투자",
             " (DRY_RUN: 주문을 보내지 않음)" if settings.dry_run else "")
    if settings.uses_data_key:
        log.info("조건검색·시세: 조회전용 실전 키 / 주문·잔고: 모의투자 키")
    log.info("조건식: [%s] %s (%r)", condition.group_name, condition.query_name, condition.query_index)
    log.info("1회 주문금액 %s원 / 최대 보유 %d종목 / 익절 +%s%% / 손절 -%s%% / 신규매수 %s~%s",
             f"{settings.buy_amount:,}", settings.max_positions, settings.take_profit_pct,
             settings.stop_loss_pct, settings.buy_start.strftime("%H:%M"),
             settings.buy_end.strftime("%H:%M"))
    log.info("=" * 64)

    fp = account_fingerprint(settings.appkey)
    store = StateStore(settings.data_dir / f"state_{settings.trading_mode}_{fp}.json",
                       identity={"mode": settings.trading_mode, "account": fp})
    trades = TradeLog(settings.data_dir / "trades.csv")
    try:
        trader = AutoTrader(broker, settings, store=store, trade_log=trades)
    except StateError as e:
        log.error("%s", e)
        return 1
    try:
        trader.reconcile()
    except LsApiError as e:
        log.error("잔고(t0424) 조회 실패: %s", e)
        return 1

    # 3) 이전 실행이 비정상 종료돼 남아 있는 실시간 등록 해제
    registry = RealtimeRegistry(settings.data_dir / "realtime.json")
    stale = registry.load()
    if stale:
        try:
            market.unregister_realtime_condition(*stale)
            log.info("이전 실행의 실시간 조건검색 등록을 해제했습니다 (%s)", stale[1])
        except LsApiError as e:
            log.info("이전 실시간 등록 해제 생략: %s", e)
        registry.clear()

    # 4) WebSocket 접속 → 실시간 조건검색 등록
    reregister = threading.Event()
    acks: dict[tuple[str, str], tuple[str, str]] = {}
    ack_event = threading.Event()

    def on_data(tr_cd: str, _tr_key: str, body: dict) -> None:
        if tr_cd != "AFR":
            return
        sig = parse_afr(body)
        if sig is None:
            return
        log.info("조건검색 신호: %s %s 구분=%s 가격=%s", sig.code, sig.name, sig.job_flag, sig.price)
        trader.on_signal(sig)

    def on_ack(tr_cd: str, tr_key: str, rsp_cd: str, rsp_msg: str) -> None:
        if tr_cd != "AFR":
            return
        acks[(tr_cd, tr_key)] = (rsp_cd, rsp_msg)
        ack_event.set()
        if rsp_cd != "00000" and started.is_set():
            log.error("실시간 조건검색(AFR) 등록이 거부됐습니다: %s %s → 재등록 시도", rsp_cd, rsp_msg)
            reregister.set()

    started = threading.Event()
    ws = RealtimeClient(settings.ws_url, lambda: data_client.token, on_data,
                        on_reconnect=reregister.set, on_ack=on_ack)
    ws.start()
    if not ws.wait_connected(30):
        log.error("WebSocket 접속 실패: %s", settings.ws_url)
        ws.stop()
        return 1

    def cleanup_registration(alert: str) -> None:
        ws.unsubscribe("AFR", alert)
        try:
            market.unregister_realtime_condition(condition.query_index, alert)
            registry.clear()
        except Exception as e:  # noqa: BLE001
            log.warning("실시간 조건검색 해제 실패 (다음 실행 때 다시 해제합니다): %s", e)

    try:
        alert_num = market.register_realtime_condition(condition.query_index)
    except LsApiError as e:
        log.error("실시간 조건검색 등록(t1860) 실패: %s", e)
        if e.paper_unsupported:
            log.error(PAPER_CONDITION_HELP)
        ws.stop()
        return 1
    registry.save(condition.query_index, alert_num)
    ws.subscribe("AFR", alert_num)
    if ack_event.wait(AFR_ACK_WAIT_SEC):
        rsp_cd, rsp_msg = acks.get(("AFR", alert_num), next(iter(acks.values())))
        if rsp_cd != "00000":
            log.error("실시간 조건검색(AFR) 구독이 거부됐습니다: %s %s", rsp_cd, rsp_msg)
            if rsp_cd == "01900" or "모의투자" in rsp_msg:
                log.error(PAPER_CONDITION_HELP)
            cleanup_registration(alert_num)
            ws.stop()
            return 1
    else:
        log.warning("실시간 조건검색(AFR) 구독 응답을 %.0f초 안에 받지 못했습니다. 계속 진행합니다.",
                    AFR_ACK_WAIT_SEC)
    started.set()
    log.info("실시간 조건검색 등록 완료 (실시간키 %s)", alert_num)

    try:
        initial = market.search_condition_once(condition.query_index)
        log.info("현재 조건 만족 종목 %d개%s", len(initial),
                 "" if settings.buy_initial_matches else " (BUY_INITIAL_MATCHES=false: 매수 안 함)")
        if settings.buy_initial_matches:
            for code, name, price in initial:
                trader.on_signal(ConditionSignal(code=code, name=name, job_flag="N", price=price))
    except LsApiError as e:
        log.warning("현재 조건 만족 종목(t1859) 조회 실패: %s", e)

    # Ctrl+C 는 루프를 '단계 사이'에서 끝내도록 한다 (주문 요청 도중 끊기지 않게)
    prev_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGBREAK"):
            sig_no = getattr(signal, name, None)
            if sig_no is not None:
                prev_handlers[sig_no] = signal.signal(sig_no, lambda *_: stop_event.set())

    log.info("자동매매 시작. 종료하려면 Ctrl+C")
    token_version = data_client.token_version
    next_reregister = 0.0
    next_token_touch = time.monotonic() + TOKEN_TOUCH_SEC
    try:
        while not stop_event.is_set():
            try:
                now = time.monotonic()
                if now >= next_token_touch:
                    next_token_touch = now + TOKEN_TOUCH_SEC
                    for c in {id(trade_client): trade_client, id(data_client): data_client}.values():
                        try:
                            _ = c.token  # 만료 전 갱신 (거래가 없어도 07:00 전후 토큰 교체)
                        except LsApiError as e:
                            log.warning("접근토큰 갱신 실패: %s", e)
                if data_client.token_version != token_version:
                    token_version = data_client.token_version
                    log.info("접근토큰이 바뀌어 WebSocket 을 다시 연결합니다.")
                    ws.reconnect()  # 재접속되면 on_reconnect → 재등록
                if reregister.is_set() and now >= next_reregister:
                    try:
                        new_num = market.register_realtime_condition(condition.query_index)
                        ws.replace_subscription("AFR", alert_num, new_num)
                        if new_num != alert_num:
                            try:
                                market.unregister_realtime_condition(condition.query_index, alert_num)
                            except LsApiError:
                                pass
                        log.info("실시간 조건검색 재등록 (실시간키 %s → %s)", alert_num, new_num)
                        alert_num = new_num
                        registry.save(condition.query_index, alert_num)
                        reregister.clear()
                    except LsApiError as e:
                        log.error("실시간 조건검색 재등록 실패 (%.0f초 후 재시도): %s",
                                  REREGISTER_RETRY_SEC, e)
                        next_reregister = now + REREGISTER_RETRY_SEC
                trader.step()
            except Exception:  # noqa: BLE001 - 어떤 오류가 나도 감시는 계속
                log.exception("메인 루프 오류")
            stop_event.wait(LOOP_SLEEP_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("종료합니다...")
        for sig_no, handler in prev_handlers.items():
            signal.signal(sig_no, handler)
        cleanup_registration(alert_num)
        ws.stop()
        log.info("자동매매 종료. 보유 종목의 익절/손절 감시도 멈췄습니다.")
        if _log_listener is not None:
            _log_listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
