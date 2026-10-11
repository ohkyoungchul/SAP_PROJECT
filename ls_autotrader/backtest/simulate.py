"""조건식 '떡상이(2023.04.03)' + 자동매매 규칙 백테스트 (5분봉 근사).

사용법 (ls_autotrader 폴더에서, fetch_data.py 로 데이터를 받은 뒤):
    .venv\\Scripts\\python -m backtest.simulate --from 20260901 --to 20260930

조건식 (HTS 캡처 기준)
  ((A and B and C and D) or (E and F and G and H)) and I and J and M and N and O
  A~D: 5분봉 종가가 BB[45,2], BB[55,2], Envelope[13,2.5], Envelope[22,2.5] 상한선을 상향 돌파 (0봉전)
  E~H: 같은 네 조건을 1봉전 기준으로
  I  : 일간 0봉전 거래량 50,000주 이상 (당일 누적)
  J  : 일간 체결강도 100% ~ 1000%
  M  : 정리매매·관리·투자위험·투자경고·거래정지·환기 제외
  N  : 우선주 제외, O: ETF·ETN·스팩 제외

이 스크립트의 근사 (결과를 읽을 때 반드시 함께 볼 것)
  1. 조건은 5분봉이 끝난 시점의 값으로만 판단한다. 실제 서버는 봉이 만들어지는 중에도
     실시간으로 판단하므로, 봉 중간에 잠깐 돌파했다가 내려온 경우는 여기서 빠진다.
  2. 매수가는 신호가 난 5분봉의 종가로 본다 (실제는 그 순간의 매도1호가 시장가 체결).
  3. 체결강도(J)는 t1305 일봉의 그날 장마감 값으로 판단한다 (장중 값이 아님).
  4. 볼린저밴드 표준편차는 모집단 표준편차, 지표는 날짜를 넘어 이어진 5분봉으로 계산한다.
  5. M(관리·경고 등)은 데이터를 받은 날의 목록이다 (9월 당시 목록이 아님).
     상장폐지 등으로 지금 목록에 없는 종목은 데이터에 없다.
  6. 익절/손절은 이후 5분봉의 시가·고가·저가로 판단한다. 시가가 이미 기준을 넘으면 시가에,
     봉 안에서 닿으면 기준가에 판 것으로 본다. 한 봉 안에서 익절가와 손절가에 모두 닿으면
     어느 쪽이 먼저인지 알 수 없어 손절로 본다 (보수적).
  7. 같은 시각에 여러 종목 신호가 나면 종목코드 순으로 처리하고, 같은 봉에서는 매도를 먼저 처리한다.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# t1404 / t1405 의 jongchk 값 중 조건 M 에 해당한다고 보는 것.
# LS TR 명세의 jongchk 코드표 기준 (t1404: 1 관리, 4 투자환기 / t1405: 1 투자경고, 2 매매정지,
# 3 정리매매, 5 투자위험) - 코드표가 다르면 --exclude-flags 로 바꿔서 다시 돌린다.
EXCLUDE_FLAGS = "t1404:1,t1404:4,t1405:1,t1405:2,t1405:3,t1405:5"

BB_K = 2.0
ENV_PCT = 2.5
BB_PERIODS = (45, 55)
ENV_PERIODS = (13, 22)


@dataclass
class Bar:
    date: str
    end: str  # 봉이 끝나는 시각 HHMM
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass
class Params:
    date_from: str = "20260901"
    date_to: str = "20260930"
    buy_amount: int = 100_000
    max_positions: int = 10
    take_profit_pct: float = 10.0
    stop_loss_pct: float = 3.0
    buy_start: str = "0900"
    buy_end: str = "1515"
    rebuy_same_day: bool = False
    min_volume: int = 50_000
    strength_min: float = 100.0
    strength_max: float = 1000.0
    use_strength: bool = True
    fee_rate: float = 0.00015  # 매수·매도 각각 (가정)
    tax_rate: float = 0.0020  # 매도 시 증권거래세 등 (가정)


@dataclass
class Position:
    code: str
    name: str
    qty: int
    price: float
    date: str
    time: str


@dataclass
class Trade:
    code: str
    name: str
    buy_date: str
    buy_time: str
    buy_price: float
    qty: int
    sell_date: str = ""
    sell_time: str = ""
    sell_price: float = 0.0
    reason: str = ""
    gross: float = 0.0
    cost: float = 0.0
    net: float = 0.0


@dataclass
class Result:
    trades: list = field(default_factory=list)
    signals: int = 0
    skipped: dict = field(default_factory=lambda: defaultdict(int))
    ambiguous_bars: int = 0
    trading_days: list = field(default_factory=list)


# ------------------------------------------------------------------ 지표
def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = int(hhmm[:2]), int(hhmm[2:4])
    t = h * 60 + m + minutes
    return f"{t // 60:02d}{t % 60:02d}"


def to_bars(rows: list[dict]) -> list[Bar]:
    """t8412 의 time 이 봉 시작 시각인지 끝 시각인지 데이터로 판단해 끝 시각으로 맞춘다."""
    start_labeled = any(r["time"][:4] == "0900" for r in rows)
    out = []
    for r in rows:
        hhmm = r["time"][:4]
        out.append(Bar(r["date"], _add_minutes(hhmm, 5) if start_labeled else hhmm,
                       int(r["open"]), int(r["high"]), int(r["low"]), int(r["close"]), int(r["volume"])))
    return out


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def bb_upper(values: list[float], n: int, k: float = BB_K) -> list[float | None]:
    """볼린저밴드 상한 = 이동평균 + k × 표준편차(모집단). 합·제곱합을 이어서 계산한다."""
    out: list[float | None] = [None] * len(values)
    s = sq = 0.0
    for i, v in enumerate(values):
        s += v
        sq += v * v
        if i >= n:
            old = values[i - n]
            s -= old
            sq -= old * old
        if i >= n - 1:
            m = s / n
            out[i] = m + k * math.sqrt(max(sq / n - m * m, 0.0))
    return out


def env_upper(values: list[float], n: int, pct: float = ENV_PCT) -> list[float | None]:
    return [None if a is None else a * (1 + pct / 100) for a in sma(values, n)]


def cross_up(closes: list[float], upper: list[float | None]) -> list[bool]:
    out = [False] * len(closes)
    for i in range(1, len(closes)):
        if upper[i] is None or upper[i - 1] is None:
            continue
        out[i] = closes[i] > upper[i] and closes[i - 1] <= upper[i - 1]
    return out


def chart_condition(bars: list[Bar]) -> list[bool]:
    """(A and B and C and D) or (E and F and G and H)."""
    closes = [float(b.close) for b in bars]
    uppers = [bb_upper(closes, n) for n in BB_PERIODS] + [env_upper(closes, n) for n in ENV_PERIODS]
    crosses = [cross_up(closes, u) for u in uppers]
    abcd = [all(c[i] for c in crosses) for i in range(len(bars))]
    return [abcd[i] or (i > 0 and abcd[i - 1]) for i in range(len(bars))]


def full_condition(bars: list[Bar], strength: dict[str, float], p: Params) -> list[bool]:
    chart = chart_condition(bars)
    out = []
    cum, day = 0, ""
    for i, b in enumerate(bars):
        if b.date != day:
            cum, day = 0, b.date
        cum += b.volume
        ok = chart[i] and cum >= p.min_volume
        if ok and p.use_strength:
            s = strength.get(b.date)
            ok = s is not None and p.strength_min <= s <= p.strength_max
        out.append(ok)
    return out


def find_signals(bars: list[Bar], cond: list[bool]) -> list[int]:
    """조건이 거짓 → 참으로 바뀐 봉 (실시간 편입 N / 재편입 R 에 해당)."""
    return [i for i in range(len(bars)) if cond[i] and (i == 0 or not cond[i - 1])]


# ------------------------------------------------------------------ 매매
def check_exit(pos: Position, bar: Bar, p: Params) -> tuple[float, str, bool] | None:
    tp = pos.price * (1 + p.take_profit_pct / 100)
    sl = pos.price * (1 - p.stop_loss_pct / 100)
    if bar.open <= sl:
        return float(bar.open), "STOP_LOSS", False
    if bar.open >= tp:
        return float(bar.open), "TAKE_PROFIT", False
    hit_sl, hit_tp = bar.low <= sl, bar.high >= tp
    if hit_sl:
        return sl, "STOP_LOSS", hit_tp
    if hit_tp:
        return tp, "TAKE_PROFIT", False
    return None


def close_trade(t: Trade, price: float, date: str, time: str, reason: str, p: Params) -> None:
    t.sell_price, t.sell_date, t.sell_time, t.reason = price, date, time, reason
    buy_amt = t.buy_price * t.qty
    sell_amt = price * t.qty
    t.gross = sell_amt - buy_amt
    t.cost = buy_amt * p.fee_rate + sell_amt * (p.fee_rate + p.tax_rate)
    t.net = t.gross - t.cost


def run(data: dict[str, tuple[str, list[Bar], list[int]]], p: Params) -> Result:
    """data: 코드 → (종목명, 5분봉, 신호 봉 인덱스)."""
    res = Result()
    events: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    for code, (_, bars, _) in data.items():
        for i, b in enumerate(bars):
            if b.date <= p.date_to:
                events[(b.date, b.end)].append((code, i))
    signal_set = {(code, i) for code, (_, _, sigs) in data.items() for i in sigs}

    held: dict[str, tuple[Position, Trade]] = {}
    traded_today: set[str] = set()
    cur_day = ""
    last_close: dict[str, tuple[int, str, str]] = {}
    for key in sorted(events):
        day, end = key
        if day < p.date_from:
            continue
        if day != cur_day:
            cur_day = day
            traded_today = set()
        items = sorted(events[key])
        # 1) 보유 종목 매도 판단
        for code, i in items:
            b = data[code][1][i]
            last_close[code] = (b.close, b.date, b.end)
            if code in held:
                pos, tr = held[code]
                if (b.date, b.end) <= (pos.date, pos.time):
                    continue
                ex = check_exit(pos, b, p)
                if ex is None:
                    continue
                price, reason, amb = ex
                res.ambiguous_bars += int(amb)
                close_trade(tr, price, b.date, b.end, reason, p)
                del held[code]
                traded_today.add(code)
        # 2) 신호 → 매수
        for code, i in items:
            if (code, i) not in signal_set:
                continue
            b = data[code][1][i]
            res.signals += 1
            if not (p.buy_start <= b.end < p.buy_end):
                res.skipped["매수시간 외"] += 1
                continue
            if code in held:
                res.skipped["이미 보유"] += 1
                continue
            if not p.rebuy_same_day and code in traded_today:
                res.skipped["당일 이미 거래"] += 1
                continue
            if len(held) >= p.max_positions:
                res.skipped["최대 보유 종목 수"] += 1
                continue
            qty = p.buy_amount // b.close if b.close > 0 else 0
            if qty <= 0:
                res.skipped["주가가 1회 주문금액보다 큼"] += 1
                continue
            name = data[code][0]
            pos = Position(code, name, qty, float(b.close), b.date, b.end)
            tr = Trade(code, name, b.date, b.end, float(b.close), qty)
            held[code] = (pos, tr)
            traded_today.add(code)
            res.trades.append(tr)
    for code, (pos, tr) in held.items():
        price, d, t = last_close.get(code, (int(pos.price), pos.date, pos.time))
        close_trade(tr, float(price), d, t, "MONTH_END_OPEN", p)
    return res


# ------------------------------------------------------------------ 입출력
def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load(base: Path, p: Params, exclude_flags: set[tuple[str, int]]):
    universe = {r["code"]: r for r in read_csv(base / "universe.csv")}
    excluded: set[str] = set()
    flag_counts: dict[str, int] = defaultdict(int)
    if (base / "flags.csv").exists():
        for r in read_csv(base / "flags.csv"):
            flag_counts[f'{r["tr"]}:{r["jongchk"]}'] += 1
            if (r["tr"], int(r["jongchk"])) in exclude_flags:
                excluded.add(r["code"])
    data = {}
    stats = defaultdict(int)
    first_bar = None
    days: set[str] = set()
    for f in sorted((base / "bars5").glob("*.csv")):
        code = f.stem
        u = universe.get(code, {})
        if not code.endswith("0") or u.get("etfgubun", "0") not in ("", "0") or u.get("spac") == "Y":
            stats["우선주/ETF/ETN/스팩 제외"] += 1
            continue
        if code in excluded:
            stats["관리·경고 등 제외"] += 1
            continue
        rows = read_csv(f)
        if not rows:
            stats["5분봉 없음"] += 1
            continue
        bars = to_bars(rows)
        strength = {}
        dpath = base / "daily" / f"{code}.csv"
        if dpath.exists():
            strength = {r["date"]: float(r["chdegree"]) for r in read_csv(dpath)}
        cond = full_condition(bars, strength, p)
        sigs = [i for i in find_signals(bars, cond) if p.date_from <= bars[i].date <= p.date_to]
        stats["분석 종목"] += 1
        days.update(b.date for b in bars if p.date_from <= b.date <= p.date_to)
        if sigs:  # 신호가 없는 종목은 매매에 쓰이지 않으므로 메모리에 두지 않는다
            data[code] = (u.get("name", ""), bars, sigs)
        b0 = (bars[0].date, bars[0].end)
        first_bar = b0 if first_bar is None or b0 > first_bar else first_bar
    return data, stats, flag_counts, first_bar, sorted(days)


def report(res: Result, p: Params, stats, flag_counts, first_bar, out: Path) -> str:
    closed = [t for t in res.trades if t.reason != "MONTH_END_OPEN"]
    opened = [t for t in res.trades if t.reason == "MONTH_END_OPEN"]
    wins = [t for t in closed if t.net > 0]
    lines = []
    w = lines.append
    w(f"기간 {p.date_from}~{p.date_to}, 거래일 {len(res.trading_days)}일 ({', '.join(d[4:] for d in res.trading_days)})")
    w(f"설정: 1회 {p.buy_amount:,}원, 최대 {p.max_positions}종목, 익절 +{p.take_profit_pct}%, 손절 -{p.stop_loss_pct}%, "
      f"매수 {p.buy_start}~{p.buy_end}, 당일 재매수 {'허용' if p.rebuy_same_day else '안 함'}, "
      f"체결강도 조건 {'사용(장마감 값)' if p.use_strength else '미사용'}")
    w(f"비용(가정): 수수료 매수·매도 각 {p.fee_rate * 100:.3f}%, 매도세 {p.tax_rate * 100:.2f}%")
    w("종목: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    w("t1404/t1405 목록 건수(구분:jongchk): " + ", ".join(f"{k}={v}" for k, v in sorted(flag_counts.items())))
    if first_bar:
        w(f"모든 종목의 5분봉이 시작된 가장 늦은 시점: {first_bar[0]} {first_bar[1]}")
    w("")
    w(f"조건 편입 신호 {res.signals}건, 매수 {len(res.trades)}건")
    for k, v in sorted(res.skipped.items(), key=lambda x: -x[1]):
        w(f"  매수 안 함 - {k}: {v}건")
    w(f"청산 {len(closed)}건: 익절 {sum(t.reason == 'TAKE_PROFIT' for t in closed)}, "
      f"손절 {sum(t.reason == 'STOP_LOSS' for t in closed)}, 수익 거래 {len(wins)}건")
    w(f"한 봉 안에서 익절·손절가 모두 닿아 손절로 처리한 경우: {res.ambiguous_bars}건")
    g = sum(t.gross for t in closed)
    c = sum(t.cost for t in closed)
    w(f"실현손익: 세전 {g:,.0f}원, 비용 {c:,.0f}원, 순 {g - c:,.0f}원")
    go = sum(t.gross for t in opened)
    co = sum(t.cost for t in opened)
    w(f"월말 미청산 {len(opened)}종목 (9/30 종가 평가, 팔았다고 보고 비용 차감): 순 {go - co:,.0f}원")
    w(f"합계 순손익: {g - c + go - co:,.0f}원")
    daily = defaultdict(float)
    for t in closed:
        daily[t.sell_date] += t.net
    w("")
    w("청산일별 순손익: " + ", ".join(f"{d[4:]} {v:,.0f}" for d, v in sorted(daily.items())))

    out.mkdir(parents=True, exist_ok=True)
    with (out / "trades.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = list(Trade.__dataclass_fields__)
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for t in res.trades:
            wr.writerow({k: getattr(t, k) for k in fields})
    text = "\n".join(lines)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="떡상이 조건 + 자동매매 규칙 5분봉 백테스트")
    d = Params()
    ap.add_argument("--data", default="data/backtest")
    ap.add_argument("--from", dest="date_from", default=d.date_from)
    ap.add_argument("--to", dest="date_to", default=d.date_to)
    ap.add_argument("--buy-amount", type=int, default=d.buy_amount)
    ap.add_argument("--max-positions", type=int, default=d.max_positions)
    ap.add_argument("--take-profit", type=float, default=d.take_profit_pct)
    ap.add_argument("--stop-loss", type=float, default=d.stop_loss_pct)
    ap.add_argument("--fee", type=float, default=d.fee_rate * 100, help="수수료 %% (매수·매도 각각)")
    ap.add_argument("--tax", type=float, default=d.tax_rate * 100, help="매도세 %%")
    ap.add_argument("--no-strength", action="store_true", help="체결강도(J) 조건을 빼고 계산")
    ap.add_argument("--exclude-flags", default=EXCLUDE_FLAGS)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    p = Params(date_from=a.date_from, date_to=a.date_to, buy_amount=a.buy_amount,
               max_positions=a.max_positions, take_profit_pct=a.take_profit, stop_loss_pct=a.stop_loss,
               fee_rate=a.fee / 100, tax_rate=a.tax / 100, use_strength=not a.no_strength)
    flags = set()
    for item in a.exclude_flags.split(","):
        if item.strip():
            tr, jc = item.strip().split(":")
            flags.add((tr, int(jc)))
    base = Path(a.data)
    data, stats, flag_counts, first_bar, days = load(base, p, flags)
    res = run(data, p)
    res.trading_days = days
    out = Path(a.out) if a.out else base / ("result_no_strength" if a.no_strength else "result")
    print(report(res, p, stats, flag_counts, first_bar, out))
    print(f"\n거래 내역: {out / 'trades.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
