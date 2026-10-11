from __future__ import annotations

import math

from backtest import fetch_data
from backtest.simulate import (
    Bar, Params, bb_upper, chart_condition, check_exit, env_upper, find_signals, full_condition,
    run, to_bars,
)


def naive_bb(values, n, k=2.0):
    out = []
    for i in range(len(values)):
        if i < n - 1:
            out.append(None)
            continue
        w = values[i - n + 1:i + 1]
        m = sum(w) / n
        out.append(m + k * math.sqrt(sum((x - m) ** 2 for x in w) / n))
    return out


def test_bb_upper_matches_naive():
    vals = [1000 + (i * 37) % 91 - 45 + i * 0.5 for i in range(200)]
    for n in (45, 55):
        fast, slow = bb_upper(vals, n), naive_bb(vals, n)
        for a, b in zip(fast, slow):
            assert (a is None and b is None) or abs(a - b) < 1e-6


def test_env_upper():
    vals = [100.0] * 13
    assert abs(env_upper(vals, 13)[-1] - 102.5) < 1e-9
    assert env_upper(vals, 13)[-2] is None


def test_to_bars_start_vs_end_label():
    rows = [{"date": "20260901", "time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    assert to_bars(rows)[0].end == "0905"
    rows[0]["time"] = "090500"
    assert to_bars(rows)[0].end == "0905"


def flat_then_jump(n_flat=60, base=1000, jump=1100, date="20260901"):
    bars = []
    t = 9 * 60
    for i in range(n_flat):
        bars.append(Bar(date, f"{t // 60:02d}{t % 60:02d}", base, base, base, base + (i % 2), 10_000))
        t += 5
    bars.append(Bar(date, f"{t // 60:02d}{t % 60:02d}", base, jump, base, jump, 10_000))
    t += 5
    bars.append(Bar(date, f"{t // 60:02d}{t % 60:02d}", jump, jump, jump, jump + 5, 10_000))
    return bars


def test_chart_condition_cross_and_one_bar_later():
    bars = flat_then_jump()
    cond = chart_condition(bars)
    i = len(bars) - 2
    assert cond[i] is True  # A~D 동시 상향 돌파
    assert cond[i + 1] is True  # E~H (1봉전 기준)
    assert not any(cond[:i])
    assert find_signals(bars, cond) == [i]


def test_full_condition_volume_and_strength():
    bars = flat_then_jump()
    p = Params()
    assert full_condition(bars, {"20260901": 120.0}, p)[-2] is True
    assert full_condition(bars, {"20260901": 90.0}, p)[-2] is False
    assert full_condition(bars, {}, p)[-2] is False
    p.use_strength = False
    assert full_condition(bars, {}, p)[-2] is True
    p.min_volume = 10_000_000
    assert full_condition(bars, {}, p)[-2] is False


def test_check_exit_rules():
    from backtest.simulate import Position
    p = Params()
    pos = Position("000010", "x", 10, 1000.0, "20260901", "1000")
    assert check_exit(pos, Bar("d", "t", 1000, 1050, 990, 1000, 1), p) is None
    assert check_exit(pos, Bar("d", "t", 1000, 1100, 990, 1000, 1), p) == (1100.0, "TAKE_PROFIT", False)
    assert check_exit(pos, Bar("d", "t", 1000, 1000, 970, 1000, 1), p) == (970.0, "STOP_LOSS", False)
    assert check_exit(pos, Bar("d", "t", 900, 950, 890, 900, 1), p) == (900.0, "STOP_LOSS", False)
    assert check_exit(pos, Bar("d", "t", 1000, 1200, 900, 1000, 1), p) == (970.0, "STOP_LOSS", True)


def test_run_slots_rebuy_and_month_end():
    p = Params(max_positions=1, date_from="20260901", date_to="20260930")
    a = [Bar("20260901", "0905", 1000, 1000, 1000, 1000, 1), Bar("20260901", "0910", 1000, 1100, 1000, 1050, 1),
         Bar("20260901", "0915", 1050, 1050, 1050, 1050, 1)]
    b = [Bar("20260901", "0905", 500, 500, 500, 500, 1), Bar("20260901", "0910", 500, 500, 500, 500, 1),
         Bar("20260901", "0915", 500, 500, 500, 500, 1), Bar("20260930", "1530", 510, 510, 510, 510, 1)]
    data = {"000010": ("A", a, [0, 2]), "000020": ("B", b, [0, 2])}
    res = run(data, p)
    # 09:05 A 매수 (B 는 슬롯 없음), 09:10 A 익절, 09:15 A 는 당일 재매수 금지, B 매수
    assert [(t.code, t.buy_time) for t in res.trades] == [("000010", "0905"), ("000020", "0915")]
    assert res.trades[0].reason == "TAKE_PROFIT" and res.trades[0].sell_price == 1100.0
    assert res.trades[1].reason == "MONTH_END_OPEN" and res.trades[1].sell_price == 510.0
    assert res.skipped == {"최대 보유 종목 수": 1, "당일 이미 거래": 1}
    t = res.trades[0]
    assert t.qty == 100 and t.gross == 10_000
    assert abs(t.cost - (100_000 * 0.00015 + 110_000 * (0.00015 + 0.002))) < 1e-6


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, tr, body, tr_cont="N", tr_cont_key=""):
        self.calls.append((tr, body, tr_cont))
        blk = body["t8412InBlock"]
        if not blk["cts_date"]:
            rows = [{"date": "20260930", "time": "153000", "open": 1, "high": 1, "low": 1, "close": 2,
                     "jdiff_vol": 5}]
            return {"t8412OutBlock": {"cts_date": "20260825", "cts_time": "093000"},
                    "t8412OutBlock1": rows}, {"tr_cont_key": "k1"}
        rows = [{"date": "20260822", "time": "153000", "open": 1, "high": 1, "low": 1, "close": 1, "jdiff_vol": 1},
                {"date": "20260825", "time": "093000", "open": 1, "high": 1, "low": 1, "close": 1, "jdiff_vol": 3}]
        return {"t8412OutBlock": {"cts_date": "20260822", "cts_time": "150000"},
                "t8412OutBlock1": rows}, {"tr_cont_key": "k2"}


def test_fetch_bars_pages_and_filters_range():
    c = FakeClient()
    bars = fetch_data.fetch_bars(c, "000010", "20260825", "20260930")
    assert [(b["date"], b["time"]) for b in bars] == [("20260825", "093000"), ("20260930", "153000")]
    assert len(c.calls) == 2 and c.calls[1][2] == "Y"
    assert c.calls[1][1]["t8412InBlock"]["cts_date"] == "20260825"
