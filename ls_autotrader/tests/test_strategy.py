from datetime import datetime, time

from autotrader.strategy import STOP_LOSS, TAKE_PROFIT, calc_buy_qty, exit_reason, in_buy_window


def test_calc_buy_qty():
    assert calc_buy_qty(100_000, 3_000) == 33
    assert calc_buy_qty(100_000, 100_000) == 1
    assert calc_buy_qty(100_000, 100_001) == 0
    assert calc_buy_qty(100_000, 0) == 0


def test_take_profit_boundary():
    # 평균단가 10,000 → +10% = 11,000 에서 익절
    assert exit_reason(10_000, 11_000, 10, 3) == TAKE_PROFIT
    assert exit_reason(10_000, 10_990, 10, 3) is None


def test_stop_loss_boundary():
    # 평균단가 10,000 → -3% = 9,700 에서 손절
    assert exit_reason(10_000, 9_700, 10, 3) == STOP_LOSS
    assert exit_reason(10_000, 9_710, 10, 3) is None


def test_float_avg_price_boundary_is_exact():
    # 1.1 같은 값의 부동소수 오차로 경계가 어긋나지 않아야 한다
    assert exit_reason(3_330.0, 3_663, 10, 3) == TAKE_PROFIT
    assert exit_reason(3_330.0, 3_230.1, 10, 3) == STOP_LOSS


def test_invalid_prices_never_trigger():
    assert exit_reason(0, 9_000, 10, 3) is None
    assert exit_reason(10_000, 0, 10, 3) is None


def test_buy_window():
    start, end = time(9, 0), time(15, 15)
    assert in_buy_window(datetime(2026, 10, 8, 9, 0), start, end)  # 목요일
    assert not in_buy_window(datetime(2026, 10, 8, 8, 59), start, end)
    assert not in_buy_window(datetime(2026, 10, 8, 15, 15), start, end)
    assert not in_buy_window(datetime(2026, 10, 10, 10, 0), start, end)  # 토요일
