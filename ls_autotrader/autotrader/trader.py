"""자동매매 엔진.

- 조건검색 실시간 신호(편입) → 시장가 매수 (1회 주문금액, 최대 보유 종목 수 제한)
- 보유 종목 현재가 감시 → 평균단가 대비 +익절% / −손절% 도달 시 시장가 전량 매도
- 체결 여부는 잔고(t0424)를 주기적으로 조회해서 확인한다.

모든 매매 판단은 step() 을 호출하는 한 스레드에서만 한다.
WebSocket 스레드는 on_signal() 로 신호를 큐에 넣기만 한다.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import queue
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .config import Settings
from .ls_api import valid_code
from .models import ConditionSignal, Holding, OrderResult, Quote
from .strategy import STOP_LOSS, TAKE_PROFIT, calc_buy_qty, exit_reason, in_buy_window

log = logging.getLogger(__name__)

REASON_TEXT = {TAKE_PROFIT: "익절", STOP_LOSS: "손절"}


class Broker(Protocol):
    def get_quotes(self, codes: list[str]) -> dict[str, Quote]: ...
    def get_holdings(self) -> list[Holding]: ...
    def buy_market(self, code: str, qty: int) -> OrderResult: ...
    def sell_market(self, code: str, qty: int) -> OrderResult: ...


@dataclass
class PendingBuy:
    code: str
    name: str
    qty: int
    ord_no: int
    ordered_at: float


@dataclass
class PendingSell:
    code: str
    name: str
    qty: int
    ord_no: int
    reason: str
    ordered_at: float


class StateStore:
    """재시작해도 '봇이 산 종목'과 '오늘 이미 매수한 종목'을 잊지 않도록 파일에 저장한다."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("상태 파일을 읽지 못했습니다: %s", self.path)
            return {}

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class TradeLog:
    FIELDS = ["time", "mode", "side", "code", "name", "qty", "ref_price", "avg_price",
              "reason", "ord_no", "result"]

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, **row: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.is_file()
        with self.path.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in self.FIELDS})


class AutoTrader:
    def __init__(self, broker: Broker, settings: Settings, *, store: StateStore,
                 trade_log: TradeLog, clock: Callable[[], float] = time.time,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self._b = broker
        self._s = settings
        self._store = store
        self._trades = trade_log
        self._clock = clock
        self._now = now
        self._signals: "queue.Queue[ConditionSignal]" = queue.Queue()

        self.holdings: dict[str, Holding] = {}
        self.managed: set[str] = set()
        self.bought_today: set[str] = set()
        self.pending_buys: dict[str, PendingBuy] = {}
        self.pending_sells: dict[str, PendingSell] = {}
        self._state_date = ""
        self._holdings_loaded = False
        self._next_reconcile = 0.0
        self._next_exit_check = 0.0
        self._load_state()

    # ------------------------------------------------------------ state
    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _load_state(self) -> None:
        st = self._store.load()
        self.managed = set(st.get("managed", []))
        self.pending_buys = {k: PendingBuy(**v) for k, v in st.get("pending_buys", {}).items()}
        self.pending_sells = {k: PendingSell(**v) for k, v in st.get("pending_sells", {}).items()}
        self._state_date = st.get("date", "")
        if self._state_date == self._today():
            self.bought_today = set(st.get("bought_today", []))
        self._state_date = self._today()
        if self.managed:
            log.info("이전 실행에서 관리하던 종목: %s", ", ".join(sorted(self.managed)))

    def _save_state(self) -> None:
        self._store.save({
            "date": self._state_date,
            "managed": sorted(self.managed),
            "bought_today": sorted(self.bought_today),
            "pending_buys": {k: asdict(v) for k, v in self.pending_buys.items()},
            "pending_sells": {k: asdict(v) for k, v in self.pending_sells.items()},
        })

    def _roll_date(self) -> None:
        today = self._today()
        if today != self._state_date:
            log.info("날짜 변경 (%s → %s): 당일 매수 이력 초기화", self._state_date, today)
            self._state_date = today
            self.bought_today.clear()
            self._save_state()

    # ------------------------------------------------------------ input
    def on_signal(self, signal: ConditionSignal) -> None:
        """WebSocket 스레드에서 호출된다. 큐에 넣기만 한다."""
        self._signals.put(signal)

    # ------------------------------------------------------------- loop
    def step(self) -> None:
        self._roll_date()
        now = self._clock()
        if now >= self._next_reconcile or not self._holdings_loaded:
            self._guard(self.reconcile)
        self._guard(self._process_signals)
        if now >= self._next_exit_check:
            self._next_exit_check = now + self._s.price_poll_sec
            self._guard(self.check_exits)

    @staticmethod
    def _guard(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - 네트워크 오류 등으로 루프가 죽지 않게
            log.exception("%s 처리 중 오류", getattr(fn, "__name__", fn))

    # -------------------------------------------------------- reconcile
    def reconcile(self) -> None:
        """잔고를 조회해 미체결 매수/매도 상태와 관리 종목을 갱신한다."""
        self._next_reconcile = self._clock() + self._s.balance_poll_sec
        holdings = {h.code: h for h in self._b.get_holdings()}
        self.holdings = holdings
        self._holdings_loaded = True
        now = self._clock()
        changed = False

        for code, pb in list(self.pending_buys.items()):
            h = holdings.get(code)
            if h is not None:
                log.info("매수 체결 확인: %s %s %d주 평균단가 %.0f", code, h.name, h.qty, h.avg_price)
                del self.pending_buys[code]
                changed = True
            elif now - pb.ordered_at > self._s.buy_fill_timeout_sec:
                log.warning("매수 주문 %s(%s) 이 %.0f초 동안 잔고에 나타나지 않아 대기 목록에서 제외합니다. "
                            "(늦게 체결되면 당일 중에는 계속 관리합니다)",
                            code, pb.name, self._s.buy_fill_timeout_sec)
                del self.pending_buys[code]
                changed = True

        for code, ps in list(self.pending_sells.items()):
            h = holdings.get(code)
            if h is None:
                log.info("매도 완료 확인: %s %s (%s)", code, ps.name, REASON_TEXT.get(ps.reason, ps.reason))
                del self.pending_sells[code]
                self.managed.discard(code)
                changed = True
            elif now - ps.ordered_at > self._s.sell_retry_sec:
                log.warning("매도 주문 후 %.0f초가 지나도 잔고가 남아 있습니다: %s %d주 → 다시 판단합니다.",
                            self._s.sell_retry_sec, code, h.qty)
                del self.pending_sells[code]
                changed = True

        for code in list(self.managed):
            if code in holdings or code in self.pending_buys or code in self.pending_sells:
                continue
            if code in self.bought_today:
                continue  # 늦은 체결 대비: 당일 매수 종목은 당일 동안 관리 대상으로 유지
            log.info("관리 종목 %s 이(가) 잔고에 없어 관리 대상에서 제외합니다.", code)
            self.managed.discard(code)
            changed = True

        if changed:
            self._save_state()

    # ------------------------------------------------------------ buys
    def _slots_used(self) -> int:
        return len(set(self.holdings) | set(self.pending_buys))

    def _process_signals(self) -> None:
        while True:
            try:
                sig = self._signals.get_nowait()
            except queue.Empty:
                return
            self._handle_signal(sig)

    def _handle_signal(self, sig: ConditionSignal) -> None:
        code = sig.code
        if not valid_code(code):
            log.warning("종목코드 형식이 올바르지 않은 신호 무시: %r", code)
            return
        if sig.job_flag not in self._s.buy_job_flags:
            log.debug("매수 대상 아님(구분 %s): %s %s", sig.job_flag, code, sig.name)
            return
        tag = f"{code} {sig.name}".strip()
        if not in_buy_window(self._now(), self._s.buy_start, self._s.buy_end):
            log.info("매수 시간 외 신호 무시: %s", tag)
            return
        if code in self.holdings or code in self.pending_buys:
            log.info("이미 보유/주문 중인 종목이라 건너뜀: %s", tag)
            return
        if code in self.bought_today and not self._s.rebuy_same_day:
            log.info("오늘 이미 매수했던 종목이라 건너뜀: %s", tag)
            return
        if not self._holdings_loaded:
            log.warning("잔고를 아직 확인하지 못해 매수를 보류합니다: %s", tag)
            return
        used = self._slots_used()
        if used >= self._s.max_positions:
            log.info("최대 보유 종목 수(%d) 도달로 매수하지 않음: %s", self._s.max_positions, tag)
            return

        ref_price = 0
        try:
            quote = self._b.get_quotes([code]).get(code)
            if quote is not None:
                ref_price = quote.ask if quote.ask > 0 else quote.price
        except Exception as e:  # noqa: BLE001
            log.warning("현재가 조회 실패, 신호 가격으로 대신 계산: %s (%s)", tag, e)
        if ref_price <= 0:
            ref_price = sig.price
        qty = calc_buy_qty(self._s.buy_amount, ref_price)
        if qty <= 0:
            log.info("주가(%d원)가 1회 주문금액(%d원)보다 커서 매수하지 않음: %s",
                     ref_price, self._s.buy_amount, tag)
            return

        if self._s.dry_run:
            log.info("[DRY_RUN] 매수 신호: %s %d주 (기준가 %d원) — 실제 주문 안 함", tag, qty, ref_price)
            self.bought_today.add(code)
            self._trades.write(time=self._now().isoformat(timespec="seconds"), mode="DRY_RUN",
                               side="BUY", code=code, name=sig.name, qty=qty, ref_price=ref_price,
                               reason=f"조건편입({sig.job_flag})", result="not_sent")
            self._save_state()
            return

        try:
            res = self._b.buy_market(code, qty)
        except Exception as e:  # noqa: BLE001
            ambiguous = bool(getattr(e, "ambiguous", False))
            self._trades.write(time=self._now().isoformat(timespec="seconds"),
                               mode=self._s.trading_mode, side="BUY", code=code, name=sig.name,
                               qty=qty, ref_price=ref_price, reason=f"조건편입({sig.job_flag})",
                               result=f"{'unknown' if ambiguous else 'error'}: {e}")
            if not ambiguous:
                log.error("매수 주문 거부/실패: %s %d주 — %s", tag, qty, e)
                return
            # 접수됐는지 알 수 없다 → 같은 종목을 다시 사지 않도록 주문된 것으로 보고 잔고로 확인한다
            log.error("매수 주문 결과 불명(접수됐을 수 있음): %s %d주 — %s → 잔고로 확인합니다", tag, qty, e)
            res = OrderResult(ord_no=0, rsp_cd="", rsp_msg="unknown")

        if res.ord_no:
            log.info("매수 주문 접수: %s %d주 시장가 (기준가 %d원, 주문번호 %d) [%d/%d]",
                     tag, qty, ref_price, res.ord_no, used + 1, self._s.max_positions)
        self.pending_buys[code] = PendingBuy(code=code, name=sig.name, qty=qty,
                                             ord_no=res.ord_no, ordered_at=self._clock())
        self.managed.add(code)
        self.bought_today.add(code)
        self._save_state()
        if res.ord_no:
            self._trades.write(time=self._now().isoformat(timespec="seconds"),
                               mode=self._s.trading_mode, side="BUY", code=code, name=sig.name,
                               qty=qty, ref_price=ref_price, reason=f"조건편입({sig.job_flag})",
                               ord_no=res.ord_no, result=f"{res.rsp_cd} {res.rsp_msg}")
        self._next_reconcile = min(self._next_reconcile, self._clock() + 1.0)

    # ----------------------------------------------------------- exits
    def check_exits(self) -> None:
        targets = [c for c in sorted(self.managed)
                   if c in self.holdings and c not in self.pending_sells
                   and self.holdings[c].sellable_qty > 0]
        if not targets:
            return
        try:
            quotes = self._b.get_quotes(targets)
        except Exception as e:  # noqa: BLE001
            log.warning("현재가 조회 실패, 잔고의 현재가로 판단합니다: %s", e)
            quotes = {}
        for code in targets:
            h = self.holdings[code]
            q = quotes.get(code)
            price = q.price if q is not None and q.price > 0 else h.last_price
            if price <= 0:
                continue
            reason = exit_reason(h.avg_price, price, self._s.take_profit_pct, self._s.stop_loss_pct)
            if reason is None:
                continue
            self._sell(h, price, reason)

    def _sell(self, h: Holding, price: int, reason: str) -> None:
        rate = (price / h.avg_price - 1) * 100 if h.avg_price > 0 else 0.0
        tag = f"{h.code} {h.name}".strip()
        label = REASON_TEXT[reason]
        if self._s.dry_run:
            log.info("[DRY_RUN] %s 신호: %s %d주 현재가 %d원 (평균단가 %.0f원, %+.2f%%) — 실제 주문 안 함",
                     label, tag, h.sellable_qty, price, h.avg_price, rate)
            return
        try:
            res = self._b.sell_market(h.code, h.sellable_qty)
        except Exception as e:  # noqa: BLE001
            ambiguous = bool(getattr(e, "ambiguous", False))
            log.error("%s 매도 주문 %s: %s %d주 — %s (%.0f초 뒤 잔고를 보고 다시 판단)", label,
                      "결과 불명" if ambiguous else "실패", tag, h.sellable_qty, e,
                      self._s.sell_retry_sec)
            self._trades.write(time=self._now().isoformat(timespec="seconds"),
                               mode=self._s.trading_mode, side="SELL", code=h.code, name=h.name,
                               qty=h.sellable_qty, ref_price=price, avg_price=h.avg_price,
                               reason=label, result=f"{'unknown' if ambiguous else 'error'}: {e}")
            # 바로 재주문하지 않도록 대기 상태로 둔다 (중복 매도 방지 / 거부 반복 방지)
            self.pending_sells[h.code] = PendingSell(code=h.code, name=h.name, qty=h.sellable_qty,
                                                     ord_no=0, reason=reason,
                                                     ordered_at=self._clock())
            self._save_state()
            return
        log.info("%s 매도 주문 접수: %s %d주 시장가 (현재가 %d원, 평균단가 %.0f원, %+.2f%%, 주문번호 %d)",
                 label, tag, h.sellable_qty, price, h.avg_price, rate, res.ord_no)
        self.pending_sells[h.code] = PendingSell(code=h.code, name=h.name, qty=h.sellable_qty,
                                                 ord_no=res.ord_no, reason=reason,
                                                 ordered_at=self._clock())
        self._save_state()
        self._trades.write(time=self._now().isoformat(timespec="seconds"),
                           mode=self._s.trading_mode, side="SELL", code=h.code, name=h.name,
                           qty=h.sellable_qty, ref_price=price, avg_price=h.avg_price,
                           reason=f"{label} {rate:+.2f}%", ord_no=res.ord_no,
                           result=f"{res.rsp_cd} {res.rsp_msg}")
        self._next_reconcile = min(self._next_reconcile, self._clock() + 1.0)
