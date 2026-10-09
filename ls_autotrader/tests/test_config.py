from __future__ import annotations

import os

import pytest

from autotrader.config import REAL_CONFIRM_PHRASE, ConfigError, Settings, load_env_file

BASE = {"LS_APPKEY": "paperkey", "LS_APPSECRET": "s", "LS_USER_ID": "tester",
        "CONDITION_NAME": "떡상이"}
KEYS = ["LS_APPKEY", "LS_APPSECRET", "LS_USER_ID", "CONDITION_NAME", "CONDITION_INDEX",
        "TRADING_MODE", "DRY_RUN", "LS_DATA_APPKEY", "LS_DATA_APPSECRET", "LS_WS_URL",
        "SKIP_ENV_CHECK", "REAL_TRADING_CONFIRM", "BUY_JOB_FLAGS", "BUY_FILL_TIMEOUT_SEC",
        "SELL_RETRY_SEC", "ORDER_MBR_NO"]


@pytest.fixture
def env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)

    def _set(**kw):
        for k, v in {**BASE, **kw}.items():
            monkeypatch.setenv(k, v)
        return Settings.from_env()
    return _set


def test_defaults_are_safe(env):
    s = env()
    assert s.dry_run is True  # 명시적으로 false 로 해야 주문
    assert s.trading_mode == "paper"
    assert s.ws_url == "wss://openapi.ls-sec.co.kr:29443/websocket"
    assert s.order_mbr_no == "KRX"


def test_data_key_switches_ws_to_real(env):
    s = env(LS_DATA_APPKEY="realkey", LS_DATA_APPSECRET="x")
    assert s.ws_url == "wss://openapi.ls-sec.co.kr:9443/websocket"


def test_ws_port_mismatch_rejected_with_data_key(env):
    with pytest.raises(ConfigError):
        env(LS_DATA_APPKEY="realkey", LS_DATA_APPSECRET="x",
            LS_WS_URL="wss://openapi.ls-sec.co.kr:29443/websocket")


def test_ws_port_mismatch_rejected_paper(env):
    with pytest.raises(ConfigError):
        env(LS_WS_URL="wss://openapi.ls-sec.co.kr:9443/websocket")
    assert env(LS_WS_URL="wss://openapi.ls-sec.co.kr:29443/websocket").ws_url.endswith("29443/websocket")


def test_data_key_must_differ(env):
    with pytest.raises(ConfigError):
        env(LS_DATA_APPKEY="paperkey", LS_DATA_APPSECRET="s")


def test_real_requires_confirm(env):
    with pytest.raises(ConfigError):
        env(TRADING_MODE="real")
    assert env(TRADING_MODE="real", REAL_TRADING_CONFIRM=REAL_CONFIRM_PHRASE).is_real


def test_skip_env_check_requires_confirm(env):
    """MS-1: 키 확인을 끄려면 실전 확인 문구가 필요하다."""
    with pytest.raises(ConfigError):
        env(SKIP_ENV_CHECK="true")
    assert env(SKIP_ENV_CHECK="true", REAL_TRADING_CONFIRM=REAL_CONFIRM_PHRASE).skip_env_check


@pytest.mark.parametrize("flags", ["N,R,O", "NR", "N R", "O"])
def test_bad_job_flags_rejected(env, flags):
    with pytest.raises(ConfigError):
        env(BUY_JOB_FLAGS=flags)


def test_job_flags_ok(env):
    assert env(BUY_JOB_FLAGS="n, r").buy_job_flags == frozenset({"N", "R"})


@pytest.mark.parametrize("name", ["BUY_FILL_TIMEOUT_SEC", "SELL_RETRY_SEC"])
def test_timeouts_must_be_positive(env, name):
    with pytest.raises(ConfigError):
        env(**{name: "0"})


def test_env_file_with_bom(tmp_path, monkeypatch):
    """CR-10/MS-5: 메모장 BOM 이 첫 키를 망가뜨리지 않는다."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("CONDITION_NAME", raising=False)
    p = tmp_path / ".env"
    p.write_bytes("﻿DRY_RUN=false\nCONDITION_NAME=\"떡상이\"\n# c\n".encode("utf-8"))
    load_env_file(p)
    assert os.environ["DRY_RUN"] == "false"
    assert os.environ["CONDITION_NAME"] == "떡상이"


def test_env_file_cp949_gives_config_error(tmp_path):
    p = tmp_path / ".env"
    p.write_bytes("CONDITION_NAME=떡상이\n".encode("cp949"))
    with pytest.raises(ConfigError):
        load_env_file(p)


def test_env_duplicate_conflicting_keys_rejected(tmp_path, monkeypatch):
    """V2-CFG-1: 같은 키가 다른 값으로 두 번 있으면 조용히 첫 값을 쓰지 않는다."""
    monkeypatch.delenv("TRADING_MODE", raising=False)
    p = tmp_path / ".env"
    p.write_text("TRADING_MODE=real\nDRY_RUN=true\nTRADING_MODE=paper\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_env_file(p)


def test_env_empty_template_line_filled_by_later_value(tmp_path, monkeypatch):
    monkeypatch.delenv("LS_APPKEY", raising=False)
    p = tmp_path / ".env"
    p.write_text("LS_APPKEY=\n# ...\nLS_APPKEY=abc\n", encoding="utf-8")
    load_env_file(p)
    assert os.environ["LS_APPKEY"] == "abc"
