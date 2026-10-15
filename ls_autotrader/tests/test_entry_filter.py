from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from autotrader.entry_filter import (EntryFilter, StrategyError, apply_trade_overrides, load_daily_features,
                                     parse_strategy)
from autotrader.prep_daily import compute_features
from autotrader.trader import AutoTrader, StateStore, TradeLog

from test_trader import Clock, FakeBroker, run, sig

STRATEGY = {
    "name": "t1",
    "trade": {"take_profit_pct": 8, "stop_loss_pct": 3, "buy_start": "09:00", "buy_end": "09:05"},
    "filters": [{"feature": "price", "min": 1000, "max": 4000},
                {"feature": "disparity20", "max": 109},
                {"feature": "from_high60_pct", "max": -16}],
}
FEAT_HEADER = "code,base_date,prev_close,ma20,high60,ret1d_pct,ret5d_pct,ret20d_pct\n"


def write(tmp_path, strategy=STRATEGY, features: str | None = None, base="20261007"):
    sp = tmp_path / "strategy.json"
    fp = tmp_path / "daily_features.csv"
    if strategy is not None:
        sp.write_text(json.dumps(strategy), encoding="utf-8")
    if features is None:
        # 000001: 이격도 100, 60일 고가 5000 → 3000원이면 -40% (통과)
        # 000002: 이격도 120 (탈락)   000003: 60일 고가 3200 → 3000원이면 -6.25% (탈락)
        features = (FEAT_HEADER + f"000001,{base},3000,3000,5000,0,0,0\n"
                    f"000002,{base},3000,2500,5000,0,0,0\n000003,{base},3000,3000,3200,0,0,0\n")
    fp.write_text(features, encoding="utf-8")
    return sp, fp


TODAY = date(2026, 10, 8)


def test_parse_rejects_bad_files():
    with pytest.raises(StrategyError):
        parse_strategy({"filters": [{"feature": "unknown", "max": 1}]})
    with pytest.raises(StrategyError):
        parse_strategy({"filters": [{"feature": "price", "min": 5, "max": 1}]})
    with pytest.raises(StrategyError):
        parse_strategy({"filters": [{"feature": "price"}]})
    with pytest.raises(StrategyError):
        parse_strategy({"trade": {"buy_end": "16:00"}})
    with pytest.raises(StrategyError):
        parse_strategy({"trade": {"take_profit_pct": 0}})
    with pytest.raises(StrategyError):
        parse_strategy({"trade": {"buy_start": "09:10", "buy_end": "09:05"}})
    with pytest.raises(StrategyError):
        parse_strategy({"trade": {"take_profit": 8}})


def test_check_pass_and_fail(tmp_path):
    sp, fp = write(tmp_path)
    f = EntryFilter(sp, fp, today=TODAY)
    assert f.check("000001", 3000, TODAY) == (True, "")
    ok, why = f.check("000002", 3000, TODAY)
    assert not ok and "disparity20" in why
    ok, why = f.check("000003", 3000, TODAY)
    assert not ok and "from_high60_pct" in why
    ok, why = f.check("000001", 4500, TODAY)
    assert not ok and "price" in why
    ok, why = f.check("999999", 3000, TODAY)  # 특성 없음 → 매수 안 함
    assert not ok and "일봉 특성 없음" in why


def test_no_strategy_file_means_no_filter(tmp_path):
    f = EntryFilter(tmp_path / "none.json", tmp_path / "none.csv", today=TODAY)
    assert not f.active
    assert f.check("123450", 999_999, TODAY) == (True, "")
    assert f.trade_overrides() == {}


def test_disabled_strategy_passes_everything(tmp_path):
    sp, fp = write(tmp_path, strategy={**STRATEGY, "enabled": False})
    f = EntryFilter(sp, fp, today=TODAY)
    assert f.check("000002", 99_999, TODAY) == (True, "")
    assert f.trade_overrides() == {}


def test_stale_or_future_features_fail_closed(tmp_path):
    sp, fp = write(tmp_path, base="20260920")  # 18일 전
    f = EntryFilter(sp, fp, today=TODAY)
    assert f.check("000001", 3000, TODAY)[0] is False
    sp, fp = write(tmp_path, base="20261008")  # 오늘 날짜 = 장중에 만든 파일
    f = EntryFilter(sp, fp, today=TODAY)
    assert f.check("000001", 3000, TODAY)[0] is False
    sp, fp = write(tmp_path, features=FEAT_HEADER)  # 비어 있음
    f = EntryFilter(sp, fp, today=TODAY)
    assert f.check("000001", 3000, TODAY)[0] is False


def test_price_only_rules_do_not_need_features(tmp_path):
    sp, fp = write(tmp_path, strategy={"filters": [{"feature": "price", "max": 4000}]}, features=FEAT_HEADER)
    f = EntryFilter(sp, fp, today=TODAY)
    assert f.check("777770", 3000, TODAY) == (True, "")


def test_trade_overrides(settings, tmp_path):
    sp, fp = write(tmp_path)
    f = EntryFilter(sp, fp, today=TODAY)
    s = apply_trade_overrides(settings, f.trade_overrides())
    assert (s.take_profit_pct, s.stop_loss_pct) == (8.0, 3.0)
    assert s.buy_end.strftime("%H:%M") == "09:05"
    assert settings.take_profit_pct == 10.0  # 원래 설정은 그대로


def make(settings, tmp_path, f, clock=None):
    broker = FakeBroker()
    clock = clock or Clock()
    clock.dt = datetime(2026, 10, 8, 9, 1, 0)
    trader = AutoTrader(broker, settings, store=StateStore(tmp_path / "state.json"),
                        trade_log=TradeLog(tmp_path / "trades.csv"), clock=clock, mono=clock.mono,
                        now=clock.now, entry_filter=f)
    return broker, clock, trader


def test_trader_buys_only_filtered_signals(settings, tmp_path):
    sp, fp = write(tmp_path)
    f = EntryFilter(sp, fp, today=TODAY)
    broker, clock, trader = make(settings, tmp_path, f)
    for c in ("000001", "000002", "000003"):
        broker.prices[c] = 3000
        trader.on_signal(sig(c))
    run(trader, clock, seconds=3)
    assert broker.orders == [("BUY", "000001", 33)]
    assert trader.settings.take_profit_pct == 8.0


def test_strategy_buy_window_applies(settings, tmp_path):
    sp, fp = write(tmp_path)
    f = EntryFilter(sp, fp, today=TODAY)
    broker, clock, trader = make(settings, tmp_path, f)
    clock.dt = datetime(2026, 10, 8, 9, 6, 0)  # 전략의 매수 마감 09:05 이후
    broker.prices["000001"] = 3000
    trader.on_signal(sig("000001"))
    run(trader, clock, seconds=3)
    assert broker.orders == []


def test_new_strategy_applied_after_date_change(settings, tmp_path):
    sp, fp = write(tmp_path)
    f = EntryFilter(sp, fp, today=TODAY)
    broker, clock, trader = make(settings, tmp_path, f)
    run(trader, clock, seconds=1)
    assert trader.settings.take_profit_pct == 8.0
    sp.write_text(json.dumps({**STRATEGY, "name": "t2", "trade": {**STRATEGY["trade"], "take_profit_pct": 6}}),
                  encoding="utf-8")
    write(tmp_path, strategy=None, base="20261008")
    clock.dt = datetime(2026, 10, 9, 8, 59, 0)
    run(trader, clock, seconds=1)
    assert trader.settings.take_profit_pct == 6.0
    assert f.strategy.name == "t2"


def test_broken_strategy_on_reload_keeps_previous(settings, tmp_path):
    sp, fp = write(tmp_path)
    f = EntryFilter(sp, fp, today=TODAY)
    broker, clock, trader = make(settings, tmp_path, f)
    sp.write_text("{not json", encoding="utf-8")
    write(tmp_path, strategy=None, base="20261008")
    clock.dt = datetime(2026, 10, 9, 8, 59, 0)
    run(trader, clock, seconds=1)
    assert f.strategy.name == "t1"
    assert trader.settings.take_profit_pct == 8.0


def test_broken_strategy_at_start_raises(tmp_path):
    (tmp_path / "s.json").write_text('{"filters": [{"feature": "x", "max": 1}]}', encoding="utf-8")
    with pytest.raises(StrategyError):
        EntryFilter(tmp_path / "s.json", tmp_path / "f.csv", today=TODAY)


def test_compute_features_matches_definition():
    rows = []
    d0 = date(2026, 6, 1)
    for i in range(70):
        d = (d0 + timedelta(days=i)).strftime("%Y%m%d")
        rows.append({"date": d, "close": str(1000 + i * 10), "high": str(1005 + i * 10)})
    f = compute_features("000001", rows, before="99999999")
    closes = [1000 + i * 10 for i in range(70)]
    assert f["base_date"] == rows[-1]["date"]
    assert float(f["prev_close"]) == closes[-1]
    assert float(f["ma20"]) == pytest.approx(sum(closes[-20:]) / 20)
    assert float(f["high60"]) == max(1005 + i * 10 for i in range(10, 70))
    assert float(f["ret20d_pct"]) == pytest.approx((closes[-1] / closes[-21] - 1) * 100, abs=1e-3)
    # 장중 실행: 오늘(마지막 행) 제외
    g = compute_features("000001", rows, before=rows[-1]["date"])
    assert g["base_date"] == rows[-2]["date"]


def test_feature_file_roundtrip(tmp_path):
    _, fp = write(tmp_path)
    feats = load_daily_features(fp)
    assert set(feats) == {"000001", "000002", "000003"}
    assert feats["000002"].ma20 == 2500
