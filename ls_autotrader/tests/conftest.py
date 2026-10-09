from __future__ import annotations

import sys
from dataclasses import replace
from datetime import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autotrader.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        appkey="key",
        appsecret="secret",
        user_id="tester",
        condition_name="떡상이",
        condition_index="",
        trading_mode="paper",
        rest_base="https://example.invalid:8080",
        ws_url="wss://example.invalid:29443/websocket",
        dry_run=False,
        buy_amount=100_000,
        max_positions=10,
        take_profit_pct=10.0,
        stop_loss_pct=3.0,
        buy_start=time(9, 0),
        buy_end=time(15, 15),
        rebuy_same_day=False,
        buy_job_flags=frozenset({"N", "R"}),
        buy_initial_matches=False,
        price_poll_sec=1.0,
        balance_poll_sec=3.0,
        buy_fill_timeout_sec=60.0,
        sell_retry_sec=20.0,
        data_dir=tmp_path,
    )


@pytest.fixture
def make_settings(settings: Settings):
    def _make(**kw) -> Settings:
        return replace(settings, **kw)
    return _make
