from __future__ import annotations

from datetime import datetime

import pytest

from autotrader.models import ConditionSignal, Holding, OrderResult, Quote
from autotrader.trader import AutoTrader, StateStore, TradeLog


class FakeBroker:
    """시장가 주문이 다음 잔고 조회 때 체결된 것으로 보이는 가짜 브로커."""

    def __init__(self) -> None:
        self.prices: dict[str, int] = {}
        self.holdings: dict[str, Holding] = {}
        self.orders: list[tuple[str, str, int]] = []
        self._fills: list[tuple[str, str, int]] = []
        self._ord_no = 1000
        self.fail_buy = False
        self.fill_immediately = True

    def get_quotes(self, codes):
        return {c: Quote(c, f"종목{c}", self.prices[c], self.prices[c], self.prices[c] - 5)
                for c in codes if c in self.prices}

    def get_holdings(self):
        if self.fill_immediately:
            self.apply_fills()
        return list(self.holdings.values())

    def apply_fills(self):
        for side, code, qty in self._fills:
            h = self.holdings.get(code)
            price = self.prices[code]
            if side == "BUY":
                old_qty = h.qty if h else 0
                old_avg = h.avg_price if h else 0
                new_qty = old_qty + qty
                avg = (old_qty * old_avg + qty * price) / new_qty
                self.holdings[code] = Holding(code, f"종목{code}", new_qty, new_qty, avg, price)
            else:
                left = h.qty - qty
                if left <= 0:
                    del self.holdings[code]
                else:
                    self.holdings[code] = Holding(code, h.name, left, left, h.avg_price, price)
        self._fills.clear()

    def _order(self, side, code, qty):
        self._ord_no += 1
        self.orders.append((side, code, qty))
        self._fills.append((side, code, qty))
        return OrderResult(self._ord_no, "00040", "ok")

    def buy_market(self, code, qty):
        if self.fail_buy:
            raise RuntimeError("주문가능금액 부족")
        return self._order("BUY", code, qty)

    def sell_market(self, code, qty):
        return self._order("SELL", code, qty)


class Clock:
    """벽시계(t)와 단조시계(m)를 따로 둔다. 기본은 함께 움직인다."""

    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.m = 500.0
        self.dt = datetime(2026, 10, 8, 10, 0, 0)  # 목요일 장중

    def __call__(self) -> float:
        return self.t

    def mono(self) -> float:
        return self.m

    def now(self) -> datetime:
        return self.dt

    def advance(self, sec: float) -> None:
        from datetime import timedelta
        self.t += sec
        self.m += sec
        self.dt += timedelta(seconds=sec)


def make_trader(broker, clock, settings, tmp_path, store=None, trade_log=None):
    return AutoTrader(broker, settings, store=store or StateStore(tmp_path / "state.json"),
                      trade_log=trade_log or TradeLog(tmp_path / "trades.csv"), clock=clock,
                      mono=clock.mono, now=clock.now)


@pytest.fixture
def env(settings, tmp_path):
    def _make(s=None):
        broker = FakeBroker()
        clock = Clock()
        trader = make_trader(broker, clock, s or settings, tmp_path)
        return broker, clock, trader
    return _make


def sig(code, flag="N", price=0):
    return ConditionSignal(code=code, name=f"종목{code}", job_flag=flag, price=price)


def run(trader, clock, seconds=5.0, step=0.5):
    n = int(seconds / step)
    for _ in range(n):
        trader.step()
        clock.advance(step)


def test_buy_on_signal_uses_amount(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 3_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    assert broker.orders == [("BUY", "000001", 33)]
    assert "000001" in trader.holdings
    assert "000001" in trader.managed


def test_exit_flag_does_not_buy(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 3_000
    trader.on_signal(sig("000001", flag="O"))
    run(trader, clock)
    assert broker.orders == []


def test_take_profit_and_stop_loss(env):
    broker, clock, trader = env()
    broker.prices.update({"000001": 10_000, "000002": 10_000})
    trader.on_signal(sig("000001"))
    trader.on_signal(sig("000002"))
    run(trader, clock)
    assert [o[0] for o in broker.orders] == ["BUY", "BUY"]

    broker.prices["000001"] = 10_999  # +9.99% → 아직
    broker.prices["000002"] = 9_701   # -2.99% → 아직
    run(trader, clock)
    assert len(broker.orders) == 2

    broker.prices["000001"] = 11_000  # +10% → 익절
    broker.prices["000002"] = 9_700   # -3% → 손절
    run(trader, clock, seconds=12)  # 잔고에서 연속 3회 안 보여야 매도 완료로 확정
    assert ("SELL", "000001", 10) in broker.orders
    assert ("SELL", "000002", 10) in broker.orders
    assert trader.holdings == {}
    assert trader.managed == set()


def test_no_duplicate_sell_while_pending(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    broker.fill_immediately = False  # 매도 체결이 늦어지는 상황
    broker.prices["000001"] = 9_000
    run(trader, clock, seconds=10)
    sells = [o for o in broker.orders if o[0] == "SELL"]
    assert sells == [("SELL", "000001", 10)]


def test_sell_retried_if_still_held_after_timeout(env, make_settings):
    broker, clock, trader = env(make_settings(sell_retry_sec=5.0))
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    broker.fill_immediately = False
    broker._fills.clear()
    broker.prices["000001"] = 9_000
    run(trader, clock, seconds=3)
    broker._fills.clear()  # 매도가 체결되지 않았다고 가정
    run(trader, clock, seconds=10)
    sells = [o for o in broker.orders if o[0] == "SELL"]
    assert len(sells) == 2


def test_max_positions(env, make_settings):
    broker, clock, trader = env(make_settings(max_positions=2))
    for code in ("000001", "000002", "000003"):
        broker.prices[code] = 5_000
        trader.on_signal(sig(code))
    run(trader, clock)
    assert [o[1] for o in broker.orders] == ["000001", "000002"]


def test_max_positions_counts_pending_orders(env, make_settings):
    broker, clock, trader = env(make_settings(max_positions=2))
    broker.fill_immediately = False  # 체결이 잔고에 아직 안 보임
    trader.step()
    for code in ("000001", "000002", "000003"):
        broker.prices[code] = 5_000
        trader.on_signal(sig(code))
    run(trader, clock)
    assert [o[1] for o in broker.orders] == ["000001", "000002"]


def test_max_positions_counts_existing_holdings(env, make_settings):
    broker, clock, trader = env(make_settings(max_positions=1))
    broker.holdings["999999"] = Holding("999999", "기존보유", 5, 5, 1000.0, 1000)
    broker.prices["000001"] = 5_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    assert broker.orders == []


def test_existing_holdings_not_sold(env):
    broker, clock, trader = env()
    broker.holdings["999999"] = Holding("999999", "기존보유", 5, 5, 1000.0, 1000)
    broker.prices["999999"] = 500  # -50% 이지만 봇이 산 종목이 아님
    run(trader, clock)
    assert broker.orders == []


def test_no_rebuy_same_day(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    broker.prices["000001"] = 9_000
    run(trader, clock)
    trader.on_signal(sig("000001", flag="R"))
    run(trader, clock)
    assert [o[0] for o in broker.orders] == ["BUY", "SELL"]


def test_rebuy_same_day_allowed(env, make_settings):
    broker, clock, trader = env(make_settings(rebuy_same_day=True))
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    broker.prices["000001"] = 9_000
    run(trader, clock, seconds=12)
    broker.prices["000001"] = 9_500
    trader.on_signal(sig("000001", flag="R"))
    run(trader, clock)
    assert [o[0] for o in broker.orders] == ["BUY", "SELL", "BUY"]


def test_price_above_amount_skipped(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 150_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    assert broker.orders == []


def test_outside_buy_window(env):
    broker, clock, trader = env()
    clock.dt = datetime(2026, 10, 8, 15, 20)
    broker.prices["000001"] = 1_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    assert broker.orders == []


def test_exits_still_work_outside_buy_window(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    clock.dt = datetime(2026, 10, 8, 15, 20)
    broker.prices["000001"] = 9_000
    run(trader, clock)
    assert broker.orders[-1] == ("SELL", "000001", 10)


def test_dry_run_sends_no_orders(env, make_settings):
    broker, clock, trader = env(make_settings(dry_run=True))
    broker.prices["000001"] = 1_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    assert broker.orders == []


def test_failed_buy_does_not_block_future_signal(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 1_000
    broker.fail_buy = True
    trader.on_signal(sig("000001"))
    run(trader, clock)
    broker.fail_buy = False
    trader.on_signal(sig("000001", flag="R"))
    run(trader, clock)
    assert broker.orders == [("BUY", "000001", 100)]


def test_state_survives_restart(env, settings, tmp_path):
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)

    # 재시작: 같은 상태 파일, 같은 계좌
    trader2 = make_trader(broker, clock, settings, tmp_path)
    assert "000001" in trader2.managed
    trader2.on_signal(sig("000001", flag="R"))  # 당일 재매수 금지 유지
    broker.prices["000001"] = 9_000
    run(trader2, clock)
    assert [o[0] for o in broker.orders] == ["BUY", "SELL"]


def test_late_fill_still_managed(env, make_settings):
    """체결이 늦어도(VI 등) 주문 중으로 유지하고, 뒤늦게 체결되면 익절/손절 대상이 된다."""
    broker, clock, trader = env(make_settings(buy_fill_timeout_sec=5.0))
    broker.fill_immediately = False
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock, seconds=10)  # 경고 시간 경과
    assert "000001" in trader.pending_buys and trader.pending_buys["000001"].delay_warned
    broker.apply_fills()  # 뒤늦게 체결
    broker.fill_immediately = True
    broker.prices["000001"] = 9_000
    run(trader, clock)
    assert broker.orders[-1] == ("SELL", "000001", 10)


def test_trade_log_written(env, tmp_path):
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    text = (tmp_path / "trades.csv").read_text(encoding="utf-8-sig")
    assert "BUY" in text and "000001" in text


class AmbiguousError(RuntimeError):
    ambiguous = True


def test_ambiguous_buy_is_tracked_not_repeated(env):
    """주문 응답이 타임아웃이어도 실제로 체결됐을 수 있다 → 재매수 금지 + 체결되면 관리."""
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    orig = broker.buy_market

    def flaky_buy(code, qty):
        orig(code, qty)  # 서버에는 접수됨
        raise AmbiguousError("read timeout")

    broker.buy_market = flaky_buy
    trader.on_signal(sig("000001"))
    trader.on_signal(sig("000001", flag="R"))
    run(trader, clock)
    assert broker.orders == [("BUY", "000001", 10)]
    assert "000001" in trader.managed and "000001" in trader.holdings
    broker.prices["000001"] = 9_000
    run(trader, clock)
    assert broker.orders[-1] == ("SELL", "000001", 10)


def test_rejected_sell_is_throttled(env, make_settings):
    broker, clock, trader = env(make_settings(sell_retry_sec=10.0))
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    calls = []

    def reject(code, qty):
        calls.append(code)
        raise RuntimeError("매도가능수량 부족")

    broker.sell_market = reject
    broker.prices["000001"] = 9_000
    run(trader, clock, seconds=5)
    assert len(calls) == 1  # 1초마다 재주문하지 않음
    run(trader, clock, seconds=10)
    assert len(calls) == 2


def test_quote_failure_falls_back_to_balance_price(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)

    def boom(codes):
        raise RuntimeError("t8407 unavailable")

    broker.get_quotes = boom
    broker.prices["000001"] = 9_000  # 잔고(t0424)의 현재가에 반영됨
    h = broker.holdings["000001"]
    broker.holdings["000001"] = Holding(h.code, h.name, h.qty, h.sellable_qty, h.avg_price, 9_000)
    run(trader, clock)
    assert broker.orders[-1] == ("SELL", "000001", 10)


def test_buy_uses_signal_price_when_quote_fails(env):
    broker, clock, trader = env()

    def boom(codes):
        raise RuntimeError("t8407 unavailable")

    broker.get_quotes = boom
    broker.prices["000001"] = 2_000
    trader.on_signal(sig("000001", price=2_000))
    run(trader, clock)
    assert broker.orders == [("BUY", "000001", 50)]


def test_invalid_signal_code_ignored(env):
    broker, clock, trader = env()
    trader.on_signal(sig("\x007720"))
    run(trader, clock)
    assert broker.orders == []


# ---------------------------------------------------------------- 리뷰 지적 회귀 테스트
def test_one_empty_snapshot_does_not_drop_managed(env):
    """TL-1: 잔고 조회가 한 번 비어 와도 관리 종목/매도대기를 버리지 않는다."""
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    real = broker.get_holdings
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return [] if calls["n"] == 1 else real()

    broker.get_holdings = flaky
    run(trader, clock, seconds=8)
    assert "000001" in trader.managed
    broker.prices["000001"] = 9_000
    run(trader, clock)
    assert broker.orders[-1] == ("SELL", "000001", 10)


def test_state_of_other_mode_is_ignored(settings, tmp_path):
    """TL-2: 모의투자 상태 파일을 실전 계좌가 읽지 않는다."""
    paper = StateStore(tmp_path / "s.json", identity={"mode": "paper", "account": "a"})
    paper.save({"managed": ["005930"]})
    real = StateStore(tmp_path / "s.json", identity={"mode": "real", "account": "b"})
    assert real.load() == {}
    assert paper.load()["managed"] == ["005930"]


def test_slow_fill_keeps_slot_and_blocks_duplicate(env, make_settings):
    """TL-3/MS-3: VI 등으로 체결이 늦어도 슬롯을 계속 차지하고 같은 종목을 다시 사지 않는다."""
    broker, clock, trader = env(make_settings(max_positions=2, rebuy_same_day=True,
                                              buy_fill_timeout_sec=5.0))
    broker.fill_immediately = False
    for code in ("000001", "000002"):
        broker.prices[code] = 5_000
    trader.on_signal(sig("000001"))
    run(trader, clock, seconds=70)  # 경고 시간 훨씬 경과
    trader.on_signal(sig("000001", flag="R"))
    trader.on_signal(sig("000002"))
    trader.on_signal(sig("000003"))
    broker.prices["000003"] = 5_000
    run(trader, clock)
    assert [o[1] for o in broker.orders if o[0] == "BUY"] == ["000001", "000002"]


def test_no_rebuy_after_overnight_stop_loss(env, settings, tmp_path):
    """TL-4: 전날 산 종목을 오늘 손절했으면 오늘은 다시 사지 않는다."""
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    clock.advance(24 * 3600)  # 다음 날
    broker.prices["000001"] = 9_000
    run(trader, clock, seconds=12)
    trader.on_signal(sig("000001", flag="R"))
    run(trader, clock)
    assert [o[0] for o in broker.orders] == ["BUY", "SELL"]


def test_partial_fill_remainder_sold(env):
    """TL-5: 매도 주문 이후 남은 매수 수량이 체결되면 바로 다시 매도한다."""
    broker, clock, trader = env()
    broker.fill_immediately = False
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    trader.step()
    broker._fills.clear()
    broker.holdings["000001"] = Holding("000001", "종목000001", 3, 3, 10_000.0, 10_000)  # 3/10 체결
    broker.prices["000001"] = 9_000
    run(trader, clock, seconds=3)
    assert broker.orders[-1] == ("SELL", "000001", 3)
    broker._fills.clear()
    broker.holdings["000001"] = Holding("000001", "종목000001", 10, 7, 10_000.0, 9_000)  # 나머지 체결
    run(trader, clock, seconds=3)
    assert broker.orders[-1] == ("SELL", "000001", 7)
    assert "000001" in trader.managed


def test_locked_trade_log_does_not_lose_position(env, tmp_path):
    """TL-6/CR-2: trades.csv 가 엑셀로 잠겨도 결과 불명 매수가 관리 대상에 남는다."""
    broker, clock, trader = env()
    trader._trades.path = tmp_path / "locked_dir"  # 디렉터리라 파일 열기 실패
    trader._trades.path.mkdir()
    broker.prices["000001"] = 10_000
    orig = broker.buy_market

    def flaky_buy(code, qty):
        orig(code, qty)
        raise AmbiguousError("timeout")

    broker.buy_market = flaky_buy
    trader.on_signal(sig("000001"))
    run(trader, clock)
    assert "000001" in trader.managed
    broker.prices["000001"] = 9_000
    run(trader, clock)
    assert broker.orders[-1] == ("SELL", "000001", 10)
    assert trader._trades._pending  # 못 쓴 기록은 보관


def test_write_ahead_before_order(env, settings, tmp_path):
    """TL-7: 주문 요청 중 강제 종료돼도 상태 파일에 주문 중으로 남아 있다."""
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000

    def interrupted(code, qty):
        raise KeyboardInterrupt

    broker.buy_market = interrupted
    trader.on_signal(sig("000001"))
    with pytest.raises(KeyboardInterrupt):
        run(trader, clock)
    t2 = make_trader(FakeBroker(), clock, settings, tmp_path)
    assert "000001" in t2.managed and "000001" in t2.pending_buys


def test_rejected_buy_is_rolled_back(env):
    broker, clock, trader = env()
    broker.prices["000001"] = 1_000
    broker.fail_buy = True
    trader.on_signal(sig("000001"))
    run(trader, clock)
    assert not trader.pending_buys and "000001" not in trader.managed
    assert "000001" not in trader.bought_today


def test_dry_run_does_not_persist_buys(env, make_settings, settings, tmp_path):
    """TL-8/MS-8: DRY_RUN 가상 매수가 실매매 전환 후 매수를 막지 않는다."""
    broker, clock, trader = env(make_settings(dry_run=True))
    broker.prices["000001"] = 1_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    live = make_trader(broker, clock, settings, tmp_path)
    live.on_signal(sig("000001"))
    run(live, clock)
    assert broker.orders == [("BUY", "000001", 100)]


def test_wall_clock_step_back_does_not_stall_exits(env):
    """TL-9/CR-9: PC 시계가 뒤로 가도 손절 감시는 계속된다."""
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    clock.t -= 600  # 벽시계만 10분 뒤로
    broker.prices["000001"] = 9_000
    run(trader, clock, seconds=2)
    assert broker.orders[-1] == ("SELL", "000001", 10)


def test_state_save_failure_does_not_kill_step(env):
    """TL-10/CR-8: 날짜 변경 시 저장이 실패해도 step 이 예외로 끝나지 않는다."""
    broker, clock, trader = env()

    def boom(state):
        raise PermissionError("locked")

    trader._store.save = boom
    clock.advance(24 * 3600)
    trader.step()  # 예외 없이 통과해야 함


def test_corrupt_state_refuses_to_start(settings, tmp_path):
    """TL-11: 상태 파일이 깨졌으면 조용히 비우지 않고 시작을 거부한다."""
    from autotrader.trader import StateError
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(StateError):
        make_trader(FakeBroker(), Clock(), settings, tmp_path)


def test_backup_used_when_state_missing(settings, tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save({"managed": ["000001"]})
    store.save({"managed": ["000001", "000002"]})
    (tmp_path / "state.json").unlink()
    assert store.load()["managed"] == ["000001"]


def test_no_exit_orders_outside_session(env):
    """TL-12: 장 마감 후에는 시장가 매도를 보내지 않는다."""
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    clock.dt = datetime(2026, 10, 8, 16, 0)
    broker.prices["000001"] = 9_000
    run(trader, clock, seconds=60)
    assert [o[0] for o in broker.orders] == ["BUY"]
    clock.dt = datetime(2026, 10, 9, 9, 0, 5)  # 다음 날 장 시작
    run(trader, clock)
    assert broker.orders[-1] == ("SELL", "000001", 10)


def test_exits_checked_before_signal_backlog(env):
    """CR-3: 신호가 많이 쌓여도 손절 확인이 먼저다."""
    broker, clock, trader = env()
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock)
    broker.prices["000001"] = 9_000
    quote_calls = []
    orig = broker.get_quotes

    def slow_quotes(codes):
        quote_calls.append(list(codes))
        return orig(codes)

    broker.get_quotes = slow_quotes
    for i in range(30):
        code = f"1{i:05d}"
        broker.prices[code] = 1_000
        trader.on_signal(sig(code))
    trader.step()
    assert quote_calls[0] == ["000001"]
    assert ("SELL", "000001", 10) in broker.orders
    assert len(quote_calls) <= 1 + 3
