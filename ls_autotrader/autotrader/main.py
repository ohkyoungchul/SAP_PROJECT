"""실행 진입점: python -m autotrader [--list-conditions] [--env 경로]"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import ConfigError, Settings, load_env_file
from .ls_api import LsApiError, LsBroker, LsRestClient, clean_str, to_int
from .models import Condition, ConditionSignal
from .realtime import RealtimeClient
from .trader import AutoTrader, StateStore, TradeLog

log = logging.getLogger("autotrader")

LOOP_SLEEP_SEC = 0.2


def setup_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = logging.FileHandler(log_dir / f"autotrader_{datetime.now():%Y%m%d}.log", encoding="utf-8")
    file.setFormatter(fmt)
    root.handlers[:] = [console, file]
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


def pick_condition(conditions: list[Condition], settings: Settings) -> Condition:
    if settings.condition_index:
        for c in conditions:
            if c.query_index == settings.condition_index:
                return c
        raise SystemExit(f"CONDITION_INDEX={settings.condition_index} 인 조건식을 찾지 못했습니다.")
    matches = [c for c in conditions if c.query_name == settings.condition_name]
    if not matches:
        raise SystemExit(f"이름이 '{settings.condition_name}' 인 서버저장 조건식을 찾지 못했습니다. "
                         "--list-conditions 로 목록을 확인하세요.")
    if len(matches) > 1:
        idx = ", ".join(c.query_index for c in matches)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotrader", description="LS증권 조건검색 자동매매")
    parser.add_argument("--env", default=".env", help=".env 파일 경로 (기본: .env)")
    parser.add_argument("--list-conditions", action="store_true",
                        help="서버에 저장된 조건식 목록만 출력하고 종료")
    args = parser.parse_args(argv)

    load_env_file(Path(args.env))
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2

    setup_logging(settings.data_dir)
    client = LsRestClient(settings)
    broker = LsBroker(client, settings)

    try:
        conditions = broker.list_conditions()
    except LsApiError as e:
        log.error("조건식 목록 조회 실패: %s", e)
        return 1

    if args.list_conditions:
        print(f"서버저장 조건식 {len(conditions)}개")
        for c in conditions:
            print(f"  {c.query_index}\t[{c.group_name}] {c.query_name}")
        return 0

    condition = pick_condition(conditions, settings)
    log.info("=" * 60)
    log.info("모드: %s%s", "실거래" if settings.is_real else "모의투자",
             " (DRY_RUN: 주문 전송 안 함)" if settings.dry_run else "")
    log.info("조건식: [%s] %s (%s)", condition.group_name, condition.query_name, condition.query_index)
    log.info("1회 주문금액 %s원 / 최대 보유 %d종목 / 익절 +%s%% / 손절 -%s%% / 신규매수 %s~%s",
             f"{settings.buy_amount:,}", settings.max_positions, settings.take_profit_pct,
             settings.stop_loss_pct, settings.buy_start.strftime("%H:%M"),
             settings.buy_end.strftime("%H:%M"))
    log.info("=" * 60)

    store = StateStore(settings.data_dir / "state.json")
    trades = TradeLog(settings.data_dir / "trades.csv")
    trader = AutoTrader(broker, settings, store=store, trade_log=trades)
    trader.reconcile()

    reregister = threading.Event()

    def on_data(tr_cd: str, _tr_key: str, body: dict) -> None:
        if tr_cd != "AFR":
            return
        sig = parse_afr(body)
        if sig is None:
            return
        log.info("조건검색 신호: %s %s 구분=%s 가격=%s", sig.code, sig.name, sig.job_flag, sig.price)
        trader.on_signal(sig)

    ws = RealtimeClient(settings.ws_url, lambda: client.token, on_data,
                        on_reconnect=reregister.set)
    ws.start()
    if not ws.wait_connected(20):
        log.error("WebSocket 접속 실패: %s", settings.ws_url)
        ws.stop()
        return 1

    alert_num = broker.register_realtime_condition(condition.query_index)
    log.info("실시간 조건검색 등록 완료 (실시간키 %s)", alert_num)
    ws.subscribe("AFR", alert_num)

    if settings.buy_initial_matches:
        for code in broker.search_condition_once(condition.query_index):
            trader.on_signal(ConditionSignal(code=code, name="", job_flag="N", price=0))

    log.info("자동매매 시작. 종료하려면 Ctrl+C")
    try:
        while True:
            if reregister.is_set():
                reregister.clear()
                try:
                    new_num = broker.register_realtime_condition(condition.query_index)
                    ws.replace_subscription("AFR", alert_num, new_num)
                    log.info("재접속 후 실시간 조건검색 재등록 (실시간키 %s → %s)", alert_num, new_num)
                    alert_num = new_num
                except LsApiError as e:
                    log.error("실시간 조건검색 재등록 실패: %s", e)
                    reregister.set()
            trader.step()
            time.sleep(LOOP_SLEEP_SEC)
    except KeyboardInterrupt:
        log.info("종료 요청을 받았습니다.")
    finally:
        try:
            broker.unregister_realtime_condition(condition.query_index, alert_num)
        except Exception as e:  # noqa: BLE001
            log.warning("실시간 조건검색 해제 실패: %s", e)
        ws.stop()
        log.info("자동매매 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
