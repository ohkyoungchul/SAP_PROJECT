"""실행 설정.

모든 값은 환경변수(또는 .env 파일)에서 읽는다. App Key / Secret 같은 비밀값을
코드나 저장소에 넣지 않기 위해서다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlparse

REAL_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY"


class ConfigError(ValueError):
    pass


def load_env_file(path: Path) -> None:
    """KEY=VALUE 형식의 .env 파일을 읽어 os.environ에 넣는다.

    이미 설정된 환경변수는 덮어쓰지 않는다. 따옴표로 감싼 값은 따옴표를 벗긴다.
    """
    if not path.is_file():
        return
    try:
        # utf-8-sig: 메모장이 붙이는 BOM 제거 (없으면 첫 줄 키 이름이 깨진다)
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path} 를 UTF-8 로 저장해 주세요 (메모장: 다른 이름으로 저장 → 인코딩 UTF-8).") from e
    values: dict[str, tuple[str, int]] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        prev = values.get(key)
        if prev is not None and prev[0] and value and prev[0] != value:
            raise ConfigError(f"{path} 에 {key} 가 {prev[1]}번째 줄과 {lineno}번째 줄에 서로 다른 값으로 "
                              "두 번 있습니다. 하나만 남겨 주세요.")
        if prev is None or not prev[0]:  # 빈 값은 뒤에 나온 실제 값으로 채운다
            values[key] = (value, lineno)
    for key, (value, _) in values.items():
        os.environ.setdefault(key, value)


def _get(name: str, default: str | None = None) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        if default is None:
            raise ConfigError(f"환경변수 {name} 가 설정되지 않았습니다.")
        return default
    return value.strip()


def _get_bool(name: str, default: bool) -> bool:
    value = _get(name, "true" if default else "false").lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"{name} 는 true/false 여야 합니다: {value!r}")


def _get_int(name: str, default: int) -> int:
    value = _get(name, str(default))
    try:
        return int(value.replace(",", "").replace("_", ""))
    except ValueError as e:
        raise ConfigError(f"{name} 는 정수여야 합니다: {value!r}") from e


def _get_float(name: str, default: float) -> float:
    value = _get(name, str(default))
    try:
        return float(value)
    except ValueError as e:
        raise ConfigError(f"{name} 는 숫자여야 합니다: {value!r}") from e


def _get_time(name: str, default: str) -> time:
    value = _get(name, default)
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except ValueError as e:
        raise ConfigError(f"{name} 는 HH:MM 형식이어야 합니다: {value!r}") from e


@dataclass(frozen=True)
class Settings:
    appkey: str
    appsecret: str
    user_id: str
    condition_name: str
    condition_index: str
    trading_mode: str  # "paper" | "real"
    rest_base: str
    ws_url: str
    dry_run: bool

    buy_amount: int
    max_positions: int
    take_profit_pct: float
    stop_loss_pct: float
    buy_start: time
    buy_end: time
    rebuy_same_day: bool
    buy_job_flags: frozenset[str]
    buy_initial_matches: bool

    price_poll_sec: float
    balance_poll_sec: float
    buy_fill_timeout_sec: float
    sell_retry_sec: float

    data_dir: Path
    order_mbr_no: str = "KRX"
    mac_address: str = ""
    # 조건검색·시세 조회 전용 키 (모의투자 키로 조건검색이 안 될 때 실전 키를 넣는다)
    data_appkey: str = ""
    data_appsecret: str = ""
    skip_env_check: bool = False

    @property
    def is_real(self) -> bool:
        return self.trading_mode == "real"

    @property
    def uses_data_key(self) -> bool:
        return bool(self.data_appkey)

    @property
    def condition_server_is_real(self) -> bool:
        """조건검색(t1860/AFR)을 실전 서버 토큰으로 하는지 여부 → WebSocket 포트 결정."""
        return self.is_real or self.uses_data_key

    @classmethod
    def from_env(cls) -> "Settings":
        mode = _get("TRADING_MODE", "paper").lower()
        if mode not in ("paper", "real"):
            raise ConfigError("TRADING_MODE 는 paper 또는 real 이어야 합니다.")
        if mode == "real" and _get("REAL_TRADING_CONFIRM", "") != REAL_CONFIRM_PHRASE:
            raise ConfigError(
                "실거래(TRADING_MODE=real)는 REAL_TRADING_CONFIRM="
                f"{REAL_CONFIRM_PHRASE} 를 함께 설정해야만 실행됩니다."
            )

        data_appkey = _get("LS_DATA_APPKEY", "")
        data_appsecret = _get("LS_DATA_APPSECRET", "")
        if bool(data_appkey) != bool(data_appsecret):
            raise ConfigError("LS_DATA_APPKEY 와 LS_DATA_APPSECRET 은 함께 설정해야 합니다.")
        if mode == "real" and data_appkey:
            raise ConfigError("실거래 모드에서는 LS_DATA_APPKEY 를 쓰지 않습니다 (LS_APPKEY 하나로 충분).")
        # 실시간 조건검색(AFR)은 조건검색에 쓰는 키와 같은 서버의 WebSocket 에 붙어야 한다.
        # 실전 9443, 모의투자 29443 (경로는 /websocket)
        default_ws = (
            "wss://openapi.ls-sec.co.kr:9443/websocket"
            if mode == "real" or data_appkey
            else "wss://openapi.ls-sec.co.kr:29443/websocket"
        )

        condition_name = _get("CONDITION_NAME", "")
        condition_index = _get("CONDITION_INDEX", "")
        if not condition_name and not condition_index:
            raise ConfigError("CONDITION_NAME 또는 CONDITION_INDEX 중 하나는 설정해야 합니다.")

        flags = frozenset(
            f.strip().upper() for f in _get("BUY_JOB_FLAGS", "N,R").split(",") if f.strip()
        )
        appkey = _get("LS_APPKEY")
        if data_appkey and data_appkey == appkey:
            raise ConfigError("LS_DATA_APPKEY 는 LS_APPKEY 와 다른 (실전) 키여야 합니다.")
        skip_env_check = _get_bool("SKIP_ENV_CHECK", False)
        if skip_env_check and _get("REAL_TRADING_CONFIRM", "") != REAL_CONFIRM_PHRASE:
            raise ConfigError(
                "SKIP_ENV_CHECK=true 는 모의투자/실전 키 확인을 끄는 설정이라, 실수로 실전 키를 쓰면 "
                "실제 돈으로 주문됩니다. 정말 필요하면 REAL_TRADING_CONFIRM="
                f"{REAL_CONFIRM_PHRASE} 도 함께 넣어야 합니다."
            )
        ws_url = _get("LS_WS_URL", default_ws)
        parsed = urlparse(ws_url)
        if parsed.hostname == "openapi.ls-sec.co.kr":
            want = 9443 if (mode == "real" or data_appkey) else 29443
            if parsed.port != want:
                raise ConfigError(
                    f"LS_WS_URL={ws_url} 의 포트가 맞지 않습니다. 조건검색에 쓰는 키가 "
                    f"{'실전' if want == 9443 else '모의투자'} 키이므로 {want} 이어야 합니다 "
                    "(다른 서버의 토큰으로 접속하면 오류 없이 데이터만 오지 않습니다).")

        s = cls(
            appkey=appkey,
            appsecret=_get("LS_APPSECRET"),
            user_id=_get("LS_USER_ID"),
            condition_name=condition_name,
            condition_index=condition_index,
            trading_mode=mode,
            rest_base=_get("LS_REST_BASE", "https://openapi.ls-sec.co.kr:8080").rstrip("/"),
            ws_url=ws_url,
            dry_run=_get_bool("DRY_RUN", True),
            buy_amount=_get_int("BUY_AMOUNT", 100_000),
            max_positions=_get_int("MAX_POSITIONS", 10),
            take_profit_pct=_get_float("TAKE_PROFIT_PCT", 10.0),
            stop_loss_pct=_get_float("STOP_LOSS_PCT", 3.0),
            buy_start=_get_time("BUY_START", "09:00"),
            buy_end=_get_time("BUY_END", "15:15"),
            rebuy_same_day=_get_bool("REBUY_SAME_DAY", False),
            buy_job_flags=flags,
            buy_initial_matches=_get_bool("BUY_INITIAL_MATCHES", False),
            price_poll_sec=_get_float("PRICE_POLL_SEC", 1.0),
            balance_poll_sec=_get_float("BALANCE_POLL_SEC", 2.0),
            buy_fill_timeout_sec=_get_float("BUY_FILL_TIMEOUT_SEC", 60.0),
            sell_retry_sec=_get_float("SELL_RETRY_SEC", 20.0),
            data_dir=Path(_get("DATA_DIR", "data")),
            order_mbr_no=_get("ORDER_MBR_NO", "KRX").upper(),
            mac_address=_get("LS_MAC_ADDRESS", ""),
            data_appkey=data_appkey,
            data_appsecret=data_appsecret,
            skip_env_check=skip_env_check,
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.buy_amount <= 0:
            raise ConfigError("BUY_AMOUNT 는 0보다 커야 합니다.")
        if self.max_positions <= 0:
            raise ConfigError("MAX_POSITIONS 는 0보다 커야 합니다.")
        if self.take_profit_pct <= 0:
            raise ConfigError("TAKE_PROFIT_PCT 는 0보다 커야 합니다.")
        if not 0 < self.stop_loss_pct < 100:
            raise ConfigError("STOP_LOSS_PCT 는 0~100 사이의 양수여야 합니다. (예: 3 → -3%)")
        if self.buy_start >= self.buy_end:
            raise ConfigError("BUY_START 는 BUY_END 보다 빨라야 합니다.")
        if not self.buy_job_flags or not self.buy_job_flags <= {"N", "R"}:
            raise ConfigError("BUY_JOB_FLAGS 는 N(진입), R(재진입) 중에서 쉼표로 구분해 지정합니다. 예: N,R "
                              f"(현재: {','.join(sorted(self.buy_job_flags)) or '비어 있음'})")
        if self.buy_fill_timeout_sec <= 0 or self.sell_retry_sec <= 0:
            raise ConfigError("BUY_FILL_TIMEOUT_SEC, SELL_RETRY_SEC 는 0보다 커야 합니다.")
        if self.price_poll_sec < 0.5 or self.balance_poll_sec < 1.0:
            raise ConfigError("PRICE_POLL_SEC 는 0.5초 이상, BALANCE_POLL_SEC 는 1초 이상이어야 합니다 "
                              "(LS 초당 호출 한도: t8407 5회, t0424 2회).")
        if self.order_mbr_no not in ("KRX", "NXT"):
            raise ConfigError("ORDER_MBR_NO 는 KRX 또는 NXT 여야 합니다.")
