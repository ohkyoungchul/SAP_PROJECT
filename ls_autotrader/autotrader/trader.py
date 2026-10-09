"""자동매매 엔진.

- 조건검색 실시간 신호(편입) → 시장가 매수 (1회 주문금액, 최대 보유 종목 수 제한)
- 보유 종목 현재가 감시 → 평균단가 대비 +익절% / −손절% 도달 시 시장가 전량 매도
- 체결 여부는 잔고(t0424)를 주기적으로 조회해서 확인한다.

안전 원칙
- 매수 주문 전에 '주문함' 상태를 먼저 파일에 기록한다 (주문 중 강제종료돼도 관리 대상에서 빠지지 않게).
- 잔고 조회 1회에서 종목이 안 보였다고 바로 '팔림/없음' 으로 판단하지 않는다 (연속 3회 확인).
- 접수 여부를 알 수 없는 주문은 접수된 것으로 보고 재주문하지 않는다.

모든 매매 판단은 step() 을 호출하는 한 스레드에서만 한다.
WebSocket 스레드는 on_signal() 로 신호를 큐에 넣기만 한다.
"""
from __future__ import annotations

import csv
import dataclasses
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
from .strategy import (PRUNE_END, SELL_START, STOP_LOSS, TAKE_PROFIT, calc_buy_qty, exit_reason,
                       in_buy_window, in_sell_window)

log = logging.getLogger(__name__)

REASON_TEXT = {TAKE_PROFIT: "익절", STOP_LOSS: "손절"}
MISS_CONFIRM = 3  # 잔고에서 연속 몇 번 안 보여야 '없음'으로 확정할지
MAX_SIGNALS_PER_STEP = 3  # 한 번의 step 에서 처리할 신호 수 (손절 감시가 밀리지 않게)
SIGNAL_TIME_BUDGET_SEC = 1.0  # 한 번의 step 에서 신호 처리에 쓸 최대 시간
SELL_REJECT_BACKOFF_MAX_SEC = 300.0  # 매도 거부(거래정지·휴장 등)가 반복될 때 재시도 간격 상한


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
    ordered_at: float  # epoch 초 (파일 저장용)
    delay_warned: bool = False


@dataclass
class PendingSell:
    code: str
    name: str
    qty: int
    ord_no: int
    reason: str
    ordered_at: float
    held_qty: int = 0  # 매도 주문 시점의 잔고수량


def _from_dict(cls, d: dict):
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


class StateError(RuntimeError):
    pass


class StateStore:
    """봇이 산 종목, 당일 매수/매도 이력, 미확정 주문을 파일에 저장한다.

    파일은 (모드, 계좌) 별로 따로 쓰고, 다른 모드/계좌의 상태는 절대 읽지 않는다.
    """

    def __init__(self, path: Path, identity: dict | None = None) -> None:
        self.path = path
        self.backup = path.with_name(path.name + ".bak")
        self.identity = identity or {}

    def load(self) -> dict:
        for p in (self.path, self.backup):
            if not p.is_file():
                continue
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(st, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError) as e:
                raise StateError(
                    f"상태 파일을 읽을 수 없습니다: {p} ({e})\n"
                    f"  봇이 산 종목 정보가 들어 있는 파일입니다. 그대로 시작하면 그 종목들의 익절/손절이 "
                    f"멈추므로 실행을 중단합니다. '{self.backup.name}' 이 있으면 그 파일을 "
                    f"'{self.path.name}' 으로 복사하거나, 보유 종목을 직접 정리한 뒤 파일을 삭제하세요.") from e
            if self.identity and st.get("identity") not in (None, self.identity):
                log.warning("상태 파일의 모드/계좌(%s)가 현재 설정(%s)과 달라 무시합니다.",
                            st.get("identity"), self.identity)
                return {}
            if p is self.backup:
                log.warning("상태 파일이 없어 백업(%s)을 사용합니다.", p.name)
            return st
        return {}

    def save(self, state: dict) -> None:
        state = {**state, "identity": self.identity}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(3):
            try:
                if self.path.exists():
                    os.replace(self.path, self.backup)
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1)  # 백신/동기화 프로그램이 잠깐 잡고 있는 경우


class TradeLog:
    """주문 기록 CSV. 엑셀로 열어 둬서 파일이 잠겨도 예외를 내지 않고 다음에 다시 쓴다."""

    FIELDS = ["time", "mode", "side", "code", "name", "qty", "ref_price", "avg_price",
              "reason", "ord_no", "result"]

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pending: list[dict] = []

    def write(self, **row: object) -> None:
        self._pending.append({k: row.get(k, "") for k in self.FIELDS})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.is_file()
            with self.path.open("a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=self.FIELDS)
                if new:
                    w.writeheader()
                w.writerows(self._pending)
            self._pending.clear()
        except OSError as e:
            log.warning("주문 기록(%s)을 쓰지 못했습니다 (엑셀로 열려 있나요?): %s — %d건 보관 후 다음에 기록",
                        self.path.name, e, len(self._pending))


class AutoTrader:
    def __init__(self, broker: Broker, settings: Settings, *, store: StateStore,
                 trade_log: TradeLog, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self._b = broker
        self._s = settings
        self._store = store
        self._trades = trade_log
        self._clock = clock  # 벽시계 (파일에 남기는 주문 시각)
        self._mono = mono  # 단조 시계 (주기 스케줄링: PC 시계 보정에 영향받지 않음)
        self._now = now
        self._signals: "queue.Queue[ConditionSignal]" = queue.Queue()

        self.holdings: dict[str, Holding] = {}
        self.managed: set[str] = set()
        self.bought_today: set[str] = set()
        self.sold_today: set[str] = set()
        self.pending_buys: dict[str, PendingBuy] = {}
        self.pending_sells: dict[str, PendingSell] = {}
        self._dry_bought: set[str] = set()  # DRY_RUN 가상 매수 (파일에 저장하지 않음)
        self._miss: dict[str, int] = {}  # 관리 종목이 잔고에서 연속으로 안 보인 횟수
        self._recent: dict[str, int] = {}  # 최근 잔고에 있던 모든 종목 (한 번 빠져도 '보유'로 간주)
        self._order_mono: dict[tuple[str, str], float] = {}  # (BUY|SELL, 종목) → 주문 시각(단조시계)
        self._sell_rejects: dict[str, int] = {}
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
        self.pending_buys = {k: _from_dict(PendingBuy, v) for k, v in st.get("pending_buys", {}).items()}
        self.pending_sells = {k: _from_dict(PendingSell, v)
                              for k, v in st.get("pending_sells", {}).items()}
        self._state_date = st.get("date", "")
        if self._state_date == self._today():
            self.bought_today = set(st.get("bought_today", []))
            self.sold_today = set(st.get("sold_today", []))
        elif self._state_date:
            self.pending_buys.clear()  # 지난 날짜의 미체결 주문은 장 마감으로 소멸
        self._state_date = self._today()
        if self.managed:
            log.info("이전 실행에서 관리하던 종목: %s", ", ".join(sorted(self.managed)))

    def _save_state(self) -> bool:
        try:
            self._store.save({
                "date": self._state_date,
                "managed": sorted(self.managed),
                "bought_today": sorted(self.bought_today),
                "sold_today": sorted(self.sold_today),
                "pending_buys": {k: asdict(v) for k, v in self.pending_buys.items()},
                "pending_sells": {k: asdict(v) for k, v in self.pending_sells.items()},
            })
            return True
        except OSError as e:
            log.error("상태 파일 저장 실패: %s", e)
            return False

    def _roll_date(self) -> None:
        today = self._today()
        if today != self._state_date:
            log.info("날짜 변경 (%s → %s): 당일 매수/매도 이력과 지난 미체결 주문 정리",
                     self._state_date, today)
            self._state_date = today
            self.bought_today.clear()
            self.sold_today.clear()
            self._dry_bought.clear()
            self.pending_buys.clear()
            self._sell_rejects.clear()
            self._save_state()

    # ------------------------------------------------------------ input
    def on_signal(self, signal: ConditionSignal) -> None:
        """WebSocket 스레드에서 호출된다. 큐에 넣기만 한다."""
        self._signals.put(signal)

    # ------------------------------------------------------------- loop
    def step(self) -> None:
        self._guard(self._roll_date)
        now = self._mono()
        if now >= self._next_reconcile or not self._holdings_loaded:
            self._guard(self.reconcile)
        if now >= self._next_exit_check:
            self._next_exit_check = now + self._s.price_poll_sec
            self._guard(self.check_exits)
        self._guard(self._process_signals)

    @staticmethod
    def _guard(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - 네트워크 오류 등으로 루프가 죽지 않게
            log.exception("%s 처리 중 오류", getattr(fn, "__name__", fn))

    def _ts(self) -> str:
        return self._now().isoformat(timespec="seconds")

    # -------------------------------------------------------- reconcile
    def reconcile(self) -> None:
        """잔고를 조회해 미체결 매수/매도 상태와 관리 종목을 갱신한다."""
        self._next_reconcile = self._mono() + self._s.balance_poll_sec
        holdings = {h.code: h for h in self._b.get_holdings()}  # 조회 실패 시 예외 → 상태 유지
        self.holdings = holdings
        self._holdings_loaded = True
        now = self._clock()
        changed = False

        for code in set(self._recent) | set(holdings):
            n = 0 if code in holdings else self._recent.get(code, 0) + 1
            if n >= MISS_CONFIRM:
                self._recent.pop(code, None)
            else:
                self._recent[code] = n
        tracked = self.managed | set(self.pending_buys) | set(self.pending_sells)
        for code in tracked:
            self._miss[code] = 0 if code in holdings else self._miss.get(code, 0) + 1
        for code in list(self._miss):
            if code not in tracked:
                del self._miss[code]
        # '없어졌다'는 판단은 봇이 팔 수 있었던 시간(평일 장중 + 마감 동시호가 반영 여유)에만 한다.
        # 밤사이 점검 시간의 빈 응답 때문에 관리 종목을 잃지 않기 위해서다.
        may_prune = in_buy_window(self._now(), SELL_START, PRUNE_END)
        gone = ({c for c in tracked if self._miss.get(c, 0) >= MISS_CONFIRM}
                if may_prune else set())

        for code, pb in list(self.pending_buys.items()):
            h = holdings.get(code)
            if h is not None and h.qty >= pb.qty:
                log.info("매수 체결 확인: %s %s %d주 평균단가 %.0f", code, h.name, h.qty, h.avg_price)
                del self.pending_buys[code]
                changed = True
            elif not pb.delay_warned and self._elapsed("BUY", code, pb.ordered_at, now) > \
                    self._s.buy_fill_timeout_sec:
                pb.delay_warned = True
                log.warning("매수 주문 %s(%s)이 %.0f초 넘게 다 체결되지 않았습니다 (현재 %d/%d주). "
                            "VI 등으로 지연될 수 있어 장 마감까지 주문 중으로 보고 종목 수에 포함합니다.",
                            code, pb.name, self._s.buy_fill_timeout_sec,
                            h.qty if h else 0, pb.qty)

        for code, ps in list(self.pending_sells.items()):
            h = holdings.get(code)
            if h is None:
                if code in gone:
                    log.info("매도 완료 확인: %s %s (%s)", code, ps.name,
                             REASON_TEXT.get(ps.reason, ps.reason))
                    del self.pending_sells[code]
                    self._sell_rejects.pop(code, None)
                    changed = True
            elif h.qty > ps.held_qty:
                log.info("매도 주문 이후 추가로 체결된 수량이 있어 다시 판단합니다: %s %d주", code, h.qty)
                del self.pending_sells[code]
                changed = True
            elif self._elapsed("SELL", code, ps.ordered_at, now) > self._sell_retry_delay(code):
                log.warning("매도 주문 후 %.0f초가 지나도 잔고가 남아 있습니다: %s %d주 → 다시 판단합니다.",
                            self._sell_retry_delay(code), code, h.qty)
                del self.pending_sells[code]
                changed = True

        for code in list(self.managed):
            if code in holdings or code in self.pending_buys or code in self.pending_sells:
                continue
            if code in gone:
                log.info("관리 종목 %s 이(가) 잔고에 없어 관리 대상에서 제외합니다.", code)
                self.managed.discard(code)
                changed = True

        if changed:
            self._save_state()

    # ------------------------------------------------------------ buys
    def _held_codes(self) -> set[str]:
        """보유 중으로 볼 종목: 최신 잔고 + 최근 잔고에 있었던 종목 (일시적 빈 응답 대비)."""
        return set(self.holdings) | set(self._recent)

    def _slots_used(self) -> int:
        return len(self._held_codes() | set(self.pending_buys))

    def _elapsed(self, kind: str, code: str, ordered_at: float, wall_now: float) -> float:
        """주문 후 경과 시간. 이번 실행에서 낸 주문은 단조시계로 잰다 (PC 시계 보정 영향 없음)."""
        started = self._order_mono.get((kind, code))
        if started is not None:
            return self._mono() - started
        return wall_now - ordered_at

    def _sell_retry_delay(self, code: str) -> float:
        """매도 재판단 간격: 기본 SELL_RETRY_SEC, 거부가 반복되면 2배씩 늘려 최대 5분."""
        n = self._sell_rejects.get(code, 0)
        delay = self._s.sell_retry_sec * (2 ** max(n - 1, 0))
        return min(delay, max(SELL_REJECT_BACKOFF_MAX_SEC, self._s.sell_retry_sec))

    def _process_signals(self) -> None:
        deadline = self._mono() + SIGNAL_TIME_BUDGET_SEC
        for _ in range(MAX_SIGNALS_PER_STEP):
            if self._mono() > deadline:
                return
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
        tag = f"{code} {sig.name}".strip()
        if sig.job_flag not in self._s.buy_job_flags:
            log.info("매수 대상 신호 아님(구분 %s): %s", sig.job_flag, tag)
            return
        if not in_buy_window(self._now(), self._s.buy_start, self._s.buy_end):
            log.info("매수 시간 외 신호 무시: %s", tag)
            return
        if (code in self._held_codes() or code in self.pending_buys or code in self.pending_sells
                or code in self.managed):
            log.info("이미 보유/주문 중인 종목이라 건너뜀: %s", tag)
            return
        if not self._s.rebuy_same_day and (code in self.bought_today or code in self.sold_today):
            log.info("오늘 이미 매수/매도했던 종목이라 건너뜀: %s", tag)
            return
        if self._s.dry_run and code in self._dry_bought:
            log.info("[DRY_RUN] 이미 매수 신호를 기록한 종목: %s", tag)
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
        reason = f"조건편입({sig.job_flag})"

        if self._s.dry_run:
            log.info("[DRY_RUN] 매수 신호: %s %d주 (기준가 %d원) - 실제 주문 안 함", tag, qty, ref_price)
            self._dry_bought.add(code)
            self._trades.write(time=self._ts(), mode="DRY_RUN", side="BUY", code=code, name=sig.name,
                               qty=qty, ref_price=ref_price, reason=reason, result="not_sent")
            return

        # 주문 전에 먼저 기록 (주문 도중 종료돼도 체결분이 관리 대상에 남도록)
        had_bought_today = code in self.bought_today
        had_managed = code in self.managed
        pb = PendingBuy(code=code, name=sig.name, qty=qty, ord_no=0, ordered_at=self._clock())
        self.pending_buys[code] = pb
        self._order_mono[("BUY", code)] = self._mono()
        self.managed.add(code)
        self.bought_today.add(code)
        if not self._save_state():
            log.error("상태를 저장하지 못해 매수 주문을 보내지 않습니다: %s", tag)
            self._rollback_buy(code, had_bought_today, had_managed)
            return

        try:
            res = self._b.buy_market(code, qty)
        except Exception as e:  # noqa: BLE001
            ambiguous = bool(getattr(e, "ambiguous", False))
            if ambiguous:
                log.error("매수 주문 결과 불명(접수됐을 수 있음): %s %d주 - %s → 주문 중으로 보고 잔고로 확인합니다",
                          tag, qty, e)
            else:
                log.error("매수 주문 거부/실패: %s %d주 - %s", tag, qty, e)
                self._rollback_buy(code, had_bought_today, had_managed)
                self._save_state()
            self._trades.write(time=self._ts(), mode=self._s.trading_mode, side="BUY", code=code,
                               name=sig.name, qty=qty, ref_price=ref_price, reason=reason,
                               result=f"{'unknown' if ambiguous else 'error'}: {e}")
            return

        pb.ord_no = res.ord_no
        self._save_state()
        log.info("매수 주문 접수: %s %d주 시장가 (기준가 %d원, 주문번호 %d) [%d/%d]",
                 tag, qty, ref_price, res.ord_no, used + 1, self._s.max_positions)
        self._trades.write(time=self._ts(), mode=self._s.trading_mode, side="BUY", code=code,
                           name=sig.name, qty=qty, ref_price=ref_price, reason=reason,
                           ord_no=res.ord_no, result=f"{res.rsp_cd} {res.rsp_msg}")
        self._next_reconcile = min(self._next_reconcile, self._mono() + 1.0)

    def _rollback_buy(self, code: str, had_bought_today: bool, had_managed: bool) -> None:
        self.pending_buys.pop(code, None)
        self._order_mono.pop(("BUY", code), None)
        if not had_bought_today:
            self.bought_today.discard(code)
        if not had_managed and code not in self.holdings:
            self.managed.discard(code)

    # ----------------------------------------------------------- exits
    def check_exits(self) -> None:
        if not in_sell_window(self._now()):
            return  # 장 시간 외에는 시장가 주문이 거부되므로 보내지 않는다
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
            self._guard(lambda h=h, price=price, reason=reason: self._sell(h, price, reason))

    def _sell(self, h: Holding, price: int, reason: str) -> None:
        rate = (price / h.avg_price - 1) * 100 if h.avg_price > 0 else 0.0
        tag = f"{h.code} {h.name}".strip()
        label = REASON_TEXT[reason]
        if self._s.dry_run:
            log.info("[DRY_RUN] %s 신호: %s %d주 현재가 %d원 (평균단가 %.0f원, %+.2f%%) - 실제 주문 안 함",
                     label, tag, h.sellable_qty, price, h.avg_price, rate)
            return
        try:
            res = self._b.sell_market(h.code, h.sellable_qty)
        except Exception as e:  # noqa: BLE001
            ambiguous = bool(getattr(e, "ambiguous", False))
            if not ambiguous:  # 거래정지·휴장 등으로 계속 거부되면 재시도 간격을 늘린다
                self._sell_rejects[h.code] = self._sell_rejects.get(h.code, 0) + 1
            # 바로 재주문하지 않도록 대기 상태로 둔다 (중복 매도 방지 / 거부 반복 방지)
            self.pending_sells[h.code] = PendingSell(code=h.code, name=h.name, qty=h.sellable_qty,
                                                     ord_no=0, reason=reason,
                                                     ordered_at=self._clock(), held_qty=h.qty)
            self._order_mono[("SELL", h.code)] = self._mono()
            self.sold_today.add(h.code)
            self._save_state()
            log.error("%s 매도 주문 %s: %s %d주 - %s (%.0f초 뒤 잔고를 보고 다시 판단)", label,
                      "결과 불명" if ambiguous else "실패", tag, h.sellable_qty, e,
                      self._sell_retry_delay(h.code))
            self._trades.write(time=self._ts(), mode=self._s.trading_mode, side="SELL", code=h.code,
                               name=h.name, qty=h.sellable_qty, ref_price=price,
                               avg_price=h.avg_price, reason=label,
                               result=f"{'unknown' if ambiguous else 'error'}: {e}")
            return
        self.pending_sells[h.code] = PendingSell(code=h.code, name=h.name, qty=h.sellable_qty,
                                                 ord_no=res.ord_no, reason=reason,
                                                 ordered_at=self._clock(), held_qty=h.qty)
        self._order_mono[("SELL", h.code)] = self._mono()
        self._sell_rejects.pop(h.code, None)
        self.sold_today.add(h.code)
        self._save_state()
        log.info("%s 매도 주문 접수: %s %d주 시장가 (현재가 %d원, 평균단가 %.0f원, %+.2f%%, 주문번호 %d)",
                 label, tag, h.sellable_qty, price, h.avg_price, rate, res.ord_no)
        self._trades.write(time=self._ts(), mode=self._s.trading_mode, side="SELL", code=h.code,
                           name=h.name, qty=h.sellable_qty, ref_price=price, avg_price=h.avg_price,
                           reason=f"{label} {rate:+.2f}%", ord_no=res.ord_no,
                           result=f"{res.rsp_cd} {res.rsp_msg}")
        self._next_reconcile = min(self._next_reconcile, self._mono() + 1.0)
