"""매매 판단 로직 (순수 함수). API 호출이 없어서 단위 테스트로 검증할 수 있다."""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

TAKE_PROFIT = "TAKE_PROFIT"
STOP_LOSS = "STOP_LOSS"


def calc_buy_qty(amount: int, price: int) -> int:
    """1회 주문 금액 안에서 살 수 있는 최대 수량 (단가가 금액보다 크면 0)."""
    if price <= 0 or amount <= 0:
        return 0
    return amount // price


def exit_reason(
    avg_price: float, last_price: float, take_profit_pct: float, stop_loss_pct: float
) -> str | None:
    """평균매입단가 대비 현재가로 익절/손절 여부를 판단한다.

    - 현재가 >= 평균단가 × (1 + 익절%)  → TAKE_PROFIT
    - 현재가 <= 평균단가 × (1 − 손절%)  → STOP_LOSS
    수수료·세금은 반영하지 않는다(평균단가 기준 단순 수익률).
    부동소수 오차로 경계값이 어긋나지 않도록 Decimal 로 비교한다.
    """
    if avg_price <= 0 or last_price <= 0:
        return None
    avg = Decimal(str(avg_price))
    last = Decimal(str(last_price))
    hundred = Decimal(100)
    if last * hundred >= avg * (hundred + Decimal(str(take_profit_pct))):
        return TAKE_PROFIT
    if last * hundred <= avg * (hundred - Decimal(str(stop_loss_pct))):
        return STOP_LOSS
    return None


def in_buy_window(now: datetime, start: time, end: time) -> bool:
    """평일 start <= 현재시각 < end 일 때만 신규 매수를 허용한다.

    공휴일은 판단하지 않는다(휴장일 주문은 증권사에서 거부된다).
    """
    if now.weekday() >= 5:
        return False
    t = now.time()
    return start <= t < end
