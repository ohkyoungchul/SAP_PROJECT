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
    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.dt = datetime(2026, 10, 8, 10, 0, 0)  # 목요일 장중

    def __call__(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self.dt

    def advance(self, sec: float) -> None:
        from datetime import timedelta
        self.t += sec
        self.dt += timedelta(seconds=sec)


@pytest.fixture
def env(settings, tmp_path):
    def _make(s=None):
        broker = FakeBroker()
        clock = Clock()
        trader = AutoTrader(broker, s or settings, store=StateStore(tmp_path / "state.json"),
                            trade_log=TradeLog(tmp_path / "trades.csv"), clock=clock,
                            now=clock.now)
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
    run(trader, clock)
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
    run(trader, clock)
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
    trader2 = AutoTrader(broker, settings, store=StateStore(tmp_path / "state.json"),
                         trade_log=TradeLog(tmp_path / "trades.csv"), clock=clock, now=clock.now)
    assert "000001" in trader2.managed
    trader2.on_signal(sig("000001", flag="R"))  # 당일 재매수 금지 유지
    broker.prices["000001"] = 9_000
    run(trader2, clock)
    assert [o[0] for o in broker.orders] == ["BUY", "SELL"]


def test_late_fill_still_managed(env, make_settings):
    broker, clock, trader = env(make_settings(buy_fill_timeout_sec=5.0))
    broker.fill_immediately = False
    broker.prices["000001"] = 10_000
    trader.on_signal(sig("000001"))
    run(trader, clock, seconds=10)  # 타임아웃 경과
    assert "000001" not in trader.pending_buys
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
