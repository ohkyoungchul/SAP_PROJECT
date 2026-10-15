"""매수 직전 추가 필터 (전략 파일 + 장 시작 전에 계산해 둔 일봉 특성).

서버 조건검색(HTS 검색식)이 보낸 편입 신호 중, 전략 파일(strategy.json)의 조건을 모두 만족하는
종목만 산다. HTS 검색식을 고치지 않고도 매매 로직을 바꿀 수 있게 하기 위한 것이다.

- 전략 파일: 주간 재최적화(analysis/weekly_update.py)가 기준을 통과했을 때 자동으로 바꾼다.
  손으로 고쳐도 된다. 파일이 없으면 추가 필터 없이 서버 검색식 신호를 그대로 쓴다.
- 일봉 특성 파일: 매일 저녁 `python -m autotrader.prep_daily` 가 만든다 (전일까지의 일봉으로 계산).
- 날짜가 바뀌면 두 파일을 다시 읽는다 (봇을 끄지 않고 여러 날 돌려도 새 전략이 적용된다).

전략 파일 예:
{
  "name": "auto-20261017",
  "enabled": true,
  "trade": {"take_profit_pct": 8, "stop_loss_pct": 3, "buy_start": "09:00", "buy_end": "09:05"},
  "filters": [
    {"feature": "price", "min": 1000, "max": 4000},
    {"feature": "disparity20", "max": 109},
    {"feature": "from_high60_pct", "max": -16}
  ]
}
"""
from __future__ import annotations

import csv
import dataclasses
import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

log = logging.getLogger(__name__)

# 필터에 쓸 수 있는 특성 (단위는 전략 파일 값과 같다)
FEATURES = {
    "price": "현재가(원) - 매수 기준가(매도1호가)",
    "ret_entry_pct": "전일 종가 대비 현재가 등락률(%)",
    "disparity20": "20일 이격도(%) = 전일 종가 ÷ 전일까지 20일 이동평균 × 100",
    "from_high60_pct": "직전 60거래일 최고가 대비 현재가 등락률(%)",
    "ret1d_pct": "전일 등락률(%)",
    "ret5d_pct": "5거래일 등락률(%) (전일 종가 기준)",
    "ret20d_pct": "20거래일 등락률(%) (전일 종가 기준)",
}
DAILY_COLUMNS = ["code", "base_date", "prev_close", "ma20", "high60", "ret1d_pct", "ret5d_pct", "ret20d_pct"]
TRADE_KEYS = {"take_profit_pct", "stop_loss_pct", "buy_start", "buy_end"}
MAX_FEATURE_AGE_DAYS = 7  # 일봉 특성이 이보다 오래되면 쓰지 않는다 (필터가 있으면 매수 안 함)


class StrategyError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    feature: str
    min: float | None = None
    max: float | None = None

    def text(self) -> str:
        lo = "" if self.min is None else f"{self.min:g} ≤ "
        hi = "" if self.max is None else f" ≤ {self.max:g}"
        return f"{lo}{self.feature}{hi}"


@dataclass(frozen=True)
class Strategy:
    name: str
    enabled: bool
    rules: tuple[Rule, ...]
    trade: dict
    source: str = ""

    def describe(self) -> str:
        if not self.enabled:
            return f"{self.name} (꺼짐: 추가 필터 없음)"
        rules = ", ".join(r.text() for r in self.rules) or "필터 없음"
        return f"{self.name} [{rules}]"


def _num(v, where: str) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise StrategyError(f"{where} 값은 숫자여야 합니다: {v!r}")
    return float(v)


def _hhmm(v, where: str) -> str:
    try:
        hh, mm = str(v).split(":")
        t = time(int(hh), int(mm))
    except (ValueError, TypeError) as e:
        raise StrategyError(f"{where} 는 HH:MM 형식이어야 합니다: {v!r}") from e
    if not time(9, 0) <= t <= time(15, 20):
        raise StrategyError(f"{where} 는 09:00~15:20 사이여야 합니다: {v!r}")
    return t.strftime("%H:%M")


def parse_strategy(data: dict, source: str = "") -> Strategy:
    if not isinstance(data, dict):
        raise StrategyError("전략 파일 최상위는 객체({ })여야 합니다.")
    rules = []
    for i, r in enumerate(data.get("filters") or []):
        where = f"filters[{i}]"
        if not isinstance(r, dict) or r.get("feature") not in FEATURES:
            raise StrategyError(f"{where}.feature 는 {', '.join(FEATURES)} 중 하나여야 합니다: {r!r}")
        unknown = set(r) - {"feature", "min", "max", "note"}
        if unknown:
            raise StrategyError(f"{where} 에 모르는 항목이 있습니다: {', '.join(sorted(unknown))}")
        lo, hi = _num(r.get("min"), f"{where}.min"), _num(r.get("max"), f"{where}.max")
        if lo is None and hi is None:
            raise StrategyError(f"{where} 에 min 이나 max 중 하나는 있어야 합니다.")
        if lo is not None and hi is not None and lo > hi:
            raise StrategyError(f"{where} 의 min({lo:g}) 이 max({hi:g}) 보다 큽니다.")
        rules.append(Rule(r["feature"], lo, hi))
    trade_in = data.get("trade") or {}
    if not isinstance(trade_in, dict):
        raise StrategyError("trade 는 객체여야 합니다.")
    unknown = set(trade_in) - TRADE_KEYS
    if unknown:
        raise StrategyError(f"trade 에 모르는 항목이 있습니다: {', '.join(sorted(unknown))}")
    trade = {}
    if "take_profit_pct" in trade_in:
        v = _num(trade_in["take_profit_pct"], "trade.take_profit_pct")
        if not 0.5 <= v <= 50:
            raise StrategyError(f"trade.take_profit_pct 는 0.5~50 사이여야 합니다: {v:g}")
        trade["take_profit_pct"] = v
    if "stop_loss_pct" in trade_in:
        v = _num(trade_in["stop_loss_pct"], "trade.stop_loss_pct")
        if not 0.5 <= v <= 30:
            raise StrategyError(f"trade.stop_loss_pct 는 0.5~30 사이여야 합니다: {v:g}")
        trade["stop_loss_pct"] = v
    for k in ("buy_start", "buy_end"):
        if k in trade_in:
            trade[k] = _hhmm(trade_in[k], f"trade.{k}")
    if "buy_start" in trade and "buy_end" in trade and trade["buy_start"] >= trade["buy_end"]:
        raise StrategyError("trade.buy_start 는 buy_end 보다 빨라야 합니다.")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise StrategyError("enabled 는 true/false 여야 합니다.")
    return Strategy(name=str(data.get("name") or "이름 없음"), enabled=enabled, rules=tuple(rules),
                    trade=trade, source=source)


def load_strategy(path: Path) -> Strategy | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise StrategyError(f"전략 파일을 읽을 수 없습니다: {path} ({e})") from e
    return parse_strategy(data, source=str(path))


@dataclass(frozen=True)
class DailyFeature:
    base_date: str
    prev_close: float
    ma20: float
    high60: float
    ret1d_pct: float
    ret5d_pct: float
    ret20d_pct: float


def load_daily_features(path: Path) -> dict[str, DailyFeature]:
    out: dict[str, DailyFeature] = {}
    if not path.is_file():
        return out
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out[r["code"]] = DailyFeature(
                    base_date=r["base_date"], prev_close=float(r["prev_close"]),
                    ma20=float(r["ma20"]) if r["ma20"] else math.nan,
                    high60=float(r["high60"]) if r["high60"] else math.nan,
                    ret1d_pct=float(r["ret1d_pct"]) if r["ret1d_pct"] else math.nan,
                    ret5d_pct=float(r["ret5d_pct"]) if r["ret5d_pct"] else math.nan,
                    ret20d_pct=float(r["ret20d_pct"]) if r["ret20d_pct"] else math.nan)
            except (KeyError, ValueError):
                continue
    return out


def feature_values(f: DailyFeature | None, price: float) -> dict[str, float]:
    v = {"price": float(price)}
    if f is None:
        return v
    v["ret_entry_pct"] = (price / f.prev_close - 1) * 100 if f.prev_close > 0 else math.nan
    v["disparity20"] = f.prev_close / f.ma20 * 100 if f.ma20 and f.ma20 > 0 else math.nan
    v["from_high60_pct"] = (price / f.high60 - 1) * 100 if f.high60 and f.high60 > 0 else math.nan
    v["ret1d_pct"], v["ret5d_pct"], v["ret20d_pct"] = f.ret1d_pct, f.ret5d_pct, f.ret20d_pct
    return v


class EntryFilter:
    def __init__(self, strategy_path: Path, features_path: Path, today: date | None = None) -> None:
        self.strategy_path = strategy_path
        self.features_path = features_path
        self.strategy: Strategy | None = None
        self.features: dict[str, DailyFeature] = {}
        self.features_date = ""
        self._loaded_for: date | None = None
        self._features_ok = False
        self.reload(today or date.today(), strict=True)

    # ------------------------------------------------------------- load
    def reload(self, today: date, strict: bool = False) -> bool:
        """두 파일을 다시 읽는다. 전략이 바뀌었으면 True.

        strict=True(시작할 때)면 잘못된 전략 파일에서 예외를 낸다. 실행 중 재적재에서는
        기존 전략을 그대로 쓰고 오류만 남긴다.
        """
        old = self.strategy
        try:
            self.strategy = load_strategy(self.strategy_path)
        except StrategyError as e:
            if strict:
                raise
            log.error("전략 파일 오류로 기존 전략을 계속 씁니다: %s", e)
        self.features = load_daily_features(self.features_path)
        dates = sorted({f.base_date for f in self.features.values()})
        self.features_date = dates[-1] if dates else ""
        self._loaded_for = today
        self._features_ok = self._check_features_age(today)
        return self.strategy != old

    def _check_features_age(self, today: date) -> bool:
        if not self.needs_daily:
            return True
        if not self.features_date:
            log.error("일봉 특성 파일(%s)이 없어 전략 필터를 통과하는 종목이 없습니다. "
                      "python -m autotrader.prep_daily 를 실행하세요.", self.features_path)
            return False
        base = datetime.strptime(self.features_date, "%Y%m%d").date()
        if base >= today:
            log.error("일봉 특성 기준일(%s)이 오늘(%s) 이후입니다. 장중에 만든 파일일 수 있어 쓰지 않습니다.",
                      self.features_date, today)
            return False
        age = (today - base).days
        if age > MAX_FEATURE_AGE_DAYS:
            log.error("일봉 특성 기준일(%s)이 %d일 전이라 쓰지 않습니다. 저녁 준비 작업이 돌았는지 확인하세요.",
                      self.features_date, age)
            return False
        if age > 4:
            log.warning("일봉 특성 기준일이 %s 입니다 (%d일 전). 연휴가 아니라면 저녁 준비 작업을 확인하세요.",
                        self.features_date, age)
        return True

    @property
    def active(self) -> bool:
        return self.strategy is not None and self.strategy.enabled

    @property
    def needs_daily(self) -> bool:
        return self.active and any(r.feature != "price" for r in self.strategy.rules)

    def trade_overrides(self) -> dict:
        return dict(self.strategy.trade) if self.strategy is not None and self.strategy.enabled else {}

    def describe(self) -> str:
        if self.strategy is None:
            return f"전략 파일 없음({self.strategy_path}) → 추가 필터 없이 서버 검색식 신호를 그대로 씀"
        feat = f", 일봉 특성 {len(self.features)}종목 (기준일 {self.features_date or '없음'})" \
            if self.needs_daily else ""
        return self.strategy.describe() + feat

    # ------------------------------------------------------------ check
    def check(self, code: str, price: float, today: date | None = None) -> tuple[bool, str]:
        today = today or date.today()
        if self._loaded_for != today:
            self.reload(today)
        if not self.active:
            return True, ""
        if price <= 0:
            return False, "가격 정보 없음"
        f = self.features.get(code)
        if self.needs_daily:
            if not self._features_ok:
                return False, "일봉 특성 파일을 쓸 수 없음"
            if f is None:
                return False, "일봉 특성 없음(신규 상장 등)"
        vals = feature_values(f, price)
        for r in self.strategy.rules:
            v = vals.get(r.feature, math.nan)
            if v is None or not math.isfinite(v):
                return False, f"{r.feature} 계산 불가"
            if r.min is not None and v < r.min:
                return False, f"{r.feature}={v:.4g} < {r.min:g}"
            if r.max is not None and v > r.max:
                return False, f"{r.feature}={v:.4g} > {r.max:g}"
        return True, ""


def apply_trade_overrides(settings, overrides: dict):
    """전략 파일의 trade 값으로 Settings 를 바꾼 새 객체 (검증 포함)."""
    if not overrides:
        return settings
    kw = {}
    for k, v in overrides.items():
        if k in ("buy_start", "buy_end"):
            hh, mm = v.split(":")
            kw[k] = time(int(hh), int(mm))
        else:
            kw[k] = float(v)
    new = dataclasses.replace(settings, **kw)
    new.validate()
    return new
