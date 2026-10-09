from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Condition:
    query_index: str
    group_name: str
    query_name: str


@dataclass(frozen=True)
class ConditionSignal:
    """조건검색 실시간(AFR) 이벤트 1건."""

    code: str
    name: str
    job_flag: str  # 편입/재편입/이탈 구분 (gsJobFlag)
    price: int


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: int
    ask: int  # 매도1호가
    bid: int  # 매수1호가


@dataclass(frozen=True)
class Holding:
    code: str
    name: str
    qty: int  # 잔고수량
    sellable_qty: int  # 매도가능수량
    avg_price: float  # 평균단가
    last_price: int  # 현재가


@dataclass(frozen=True)
class OrderResult:
    ord_no: int
    rsp_cd: str
    rsp_msg: str
