# LS증권 조건검색 자동매매 (모의투자)

LS증권 HTS에서 만든 **서버저장 조건식**의 실시간 편입 신호를 받아 매수합니다. 매수한 종목은 **익절 / 손절** 기준에 닿으면 시장가로 전량 매도합니다.
LS증권 OPEN API(REST + WebSocket)를 사용하며, Windows PC에서 Python으로 실행합니다.

## 동작 규칙

| 항목 | 기본값 | 설정 |
|---|---|---|
| 매수 신호 | 조건검색 실시간 `N`(진입), `R`(재진입). `O`(이탈)에서는 매수하지 않음 | `BUY_JOB_FLAGS` |
| 1회 주문금액 | 100,000원. 수량 = 금액 ÷ 매도1호가(내림). 1주 가격이 금액보다 크면 매수 안 함 | `BUY_AMOUNT` |
| 최대 보유 종목 | 10개. 계좌 전체 보유 종목 + 아직 다 체결되지 않은 매수 주문(장 마감까지)을 합쳐서 셈 | `MAX_POSITIONS` |
| 익절 | 현재가 ≥ 평균단가 × 1.10 → 시장가 전량 매도 | `TAKE_PROFIT_PCT` |
| 손절 | 현재가 ≤ 평균단가 × 0.97 → 시장가 전량 매도 | `STOP_LOSS_PCT` |
| 신규 매수 시간 | 평일 09:00 ~ 15:15 | `BUY_START`, `BUY_END` |
| 익절/손절 주문 시간 | 평일 09:00 ~ 15:30. 장 시간 외에는 시장가 주문이 거부되므로 다음 장 시작 때 판단. 공휴일·거래정지로 거부되면 재시도 간격을 최대 5분까지 늘림 | 고정 |
| 당일 재매수 | 안 함. 그날 사거나 판 종목은 다시 편입돼도 그날은 사지 않음 | `REBUY_SAME_DAY` |

- 익절·손절 수익률은 **평균단가 기준 단순 수익률**입니다. 수수료와 세금은 빼지 않습니다.
- **봇이 산 종목만** 자동으로 매도합니다. 실행 전부터 갖고 있던 종목은 건드리지 않지만, 보유 종목 수에는 들어갑니다.
  - 봇이 보유 중인 종목을 직접 더 사면, 봇은 그 종목의 **전체 수량**을 자기 것으로 보고 익절/손절합니다. 봇이 들고 있는 종목은 직접 매매하지 마세요.
  - 봇이 산 종목을 직접 전량 매도했다가 다시 사면 수량이나 평균단가가 달라집니다. 이 경우 봇은 해당 종목을 사용자 보유로 보고 관리에서 뺍니다.
- 주문은 모두 **시장가**입니다. 급등주는 체결가가 신호 시점 가격과 다를 수 있습니다.
- 체결 확인은 잔고(t0424)를 2초마다 조회해서 합니다. 현재가 감시는 t8407로 1초마다 합니다.
- 잔고 조회에서 종목이 한 번 안 보였다고 바로 '팔림'으로 판단하지 않습니다. 연속 3회 확인합니다.
- 프로그램을 끄면 익절/손절 감시도 멈춥니다. 다시 켜면 상태 파일을 읽어 봇이 산 종목을 이어서 감시합니다.
- 같은 데이터 폴더로 프로그램을 두 개 동시에 실행할 수 없습니다.

## 1. 사전 준비

1. **LS증권 OPEN API 모의투자 키 발급**
   - LS증권 OPEN API 포털(API 투자전략센터)에서 신청합니다. 모의투자 App Key / Secret Key는 실전 키와 **별도로** 발급받아야 합니다.
   - 포털 안내에 따르면 OPEN API를 신청하려면 xingAPI 사용등록이 먼저 되어 있어야 합니다.
2. **조건식을 서버에 저장**
   - HTS 종목검색 화면에서 조건식을 만든 뒤 **API보내기**(또는 전략관리 → 서버저장)를 누릅니다.
   - 저장이 됐는지는 아래 3단계의 `--list-conditions`로 확인할 수 있습니다.
3. **Python 3.10 이상**
   - python.org에서 설치합니다. 설치 화면에서 "Add python.exe to PATH"를 체크하세요.
4. **Git**
   - git-scm.com에서 설치합니다.
   - Git 없이 받으려면 GitHub 저장소에서 `claude/quirky-tesla-le47nl` 브랜치를 고른 뒤 Code → Download ZIP으로 받아 압축을 풀면 됩니다.

## 2. 설치

### 자동 설치 (권장): `C:\workspace\Stock_Trading`

**명령 프롬프트**(시작 → `cmd`)에 아래 두 줄을 차례로 붙여 넣습니다. Git은 필요 없습니다.

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; New-Item -ItemType Directory -Force C:\workspace | Out-Null; iwr -UseBasicParsing https://raw.githubusercontent.com/ohkyoungchul/SAP_PROJECT/claude/quirky-tesla-le47nl/ls_autotrader/install.ps1 -OutFile C:\workspace\install_stock_trading.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File C:\workspace\install_stock_trading.ps1
```

설치 스크립트가 하는 일
1. Python 3.10 이상이 있는지 확인합니다. 없으면 winget으로 설치할지 묻습니다.
2. 최신 코드를 받아 `C:\workspace\Stock_Trading`에 복사합니다.
3. 가상환경과 패키지를 설치합니다.
4. `.env`를 만들어 메모장으로 엽니다.
5. 실행 파일 3개를 만듭니다.

| 파일 | 용도 |
|---|---|
| `run.bat` | 자동매매 실행 |
| `list_conditions.bat` | 서버저장 조건식 목록 확인 |
| `update.bat` | 최신 코드로 업데이트. `.env`, `data` 폴더는 그대로 둠 |

다른 폴더에 설치하려면 두 번째 줄 끝에 `-Target D:\원하는\폴더`를 붙입니다.

### 수동 설치 (Windows 명령 프롬프트)

```bat
git clone -b claude/quirky-tesla-le47nl https://github.com/ohkyoungchul/SAP_PROJECT.git
cd SAP_PROJECT\ls_autotrader
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

**Git 없이 ZIP으로 받은 경우**
- `git clone` 줄은 건너뜁니다.
- 압축을 푼 폴더 안에서 `README.md`와 `requirements.txt`가 있는 `ls_autotrader` 폴더로 이동합니다.
  - 예: `cd %USERPROFILE%\Downloads\SAP_PROJECT-claude-quirky-tesla-le47nl\ls_autotrader`
- 그다음 `py -m venv .venv`부터 실행합니다.

`.env` 파일 작성 요령
- **이미 있는 줄에 값을 채웁니다.** 아래 내용을 파일 끝에 덧붙이지 마세요.
  - 같은 키가 서로 다른 값으로 두 번 있으면 실행을 멈추고 알려줍니다.
- 메모장으로 저장할 때 인코딩은 UTF-8로 하세요.

최소한 채워야 하는 값:

```
LS_APPKEY=모의투자 App Key
LS_APPSECRET=모의투자 Secret Key
LS_USER_ID=HTS 로그인 ID
CONDITION_NAME=떡상이
```

## 3. 실행

자동 설치를 했다면 아래 배치 파일을 더블클릭하면 됩니다.
1. `list_conditions.bat`: 조건식 목록 확인
2. `run.bat`: 자동매매 실행

수동 설치를 했다면 아래 명령을 씁니다.

```bat
:: (1) 서버에 저장된 조건식 목록 확인
.venv\Scripts\python -m autotrader --list-conditions

:: (2) DRY_RUN=true (기본) 로 장중 실행 → 조건검색 신호와 매수 판단만 로그에 찍힘
::     (익절/손절은 실제 보유 종목이 있어야 하므로 DRY_RUN=false 모의투자에서 확인)
.venv\Scripts\python -m autotrader

:: (3) 로그가 정상이면 .env 의 DRY_RUN=false 로 바꿔 모의투자 주문 시작
```

- **종료**: `Ctrl+C`를 누르면 진행 중이던 주문 처리를 마친 뒤 종료합니다.
- **PC 절전**: 장중에는 PC가 절전 모드로 들어가지 않게 하세요.
- **콘솔 창 클릭**: 클릭해도 멈추지 않도록 프로그램이 콘솔의 '빠른 편집 모드'를 끕니다.
- **`DRY_RUN`**: 설정을 빼먹으면 기본값 `true`가 적용돼 주문이 나가지 않습니다. 주문을 내려면 `DRY_RUN=false`를 직접 넣어야 합니다.

### "모의투자에서는 해당업무가 제공되지 않습니다" 오류가 나는 경우

서버저장 조건식의 실시간검색(t1866 / t1860 / AFR)은 **실전 서버에서만 된다**는 커뮤니티 자료가 있습니다(LS 공식 확인은 없음). 이 오류가 나면 실전 App Key를 **조회 전용**으로 추가하세요.

```
LS_DATA_APPKEY=실전 App Key
LS_DATA_APPSECRET=실전 Secret Key
```

- 이 키는 **조건검색과 현재가 조회에만** 쓰입니다. 코드상 이 키로 주문하는 경로는 없습니다.
- 주문과 잔고 조회는 계속 모의투자 키(`LS_APPKEY`)로 나갑니다.
- 실전 키를 쓰려면 실전 OPEN API 사용 신청이 별도로 필요합니다.

### 안전장치

- `TRADING_MODE=paper`인데 `LS_APPKEY`가 실전 키로 보이면 시작하지 않습니다.
  - 판단 기준: 계좌조회(CSPAQ12200) 응답 메시지에 '모의투자'라는 글자가 있는지입니다.
  - 이 확인을 끄는 `SKIP_ENV_CHECK=true`는 `REAL_TRADING_CONFIRM`도 함께 넣어야만 동작합니다.
- 상태 파일은 모드(모의/실전)와 App Key별로 따로 저장됩니다. 모의투자 때 산 종목 정보로 실전 계좌의 종목을 파는 일은 없습니다.
- 다른 App Key나 이전 버전(`data\state.json`)의 상태 파일에 봇이 산 종목이 남아 있으면 실행하지 않고 알려줍니다.
  - App Key 재발급 등으로 **같은 계좌**라면 아래 명령으로 가져옵니다.
    ```bat
    .venv\Scripts\python -m autotrader --adopt-state data\파일이름.json
    ```
  - 다른 계좌라면 그 파일을 다른 폴더로 옮기세요.
- 실거래(`TRADING_MODE=real`)는 `REAL_TRADING_CONFIRM=I_UNDERSTAND_REAL_MONEY`까지 넣어야만 실행됩니다.
- 주문 응답이 타임아웃 등으로 불분명할 때가 있습니다. 이때는 재주문하지 않습니다. 주문이 들어간 것으로 보고 잔고로 확인한 뒤 감시합니다.
- 매수 주문을 보내기 전에 '주문 중' 상태를 먼저 파일에 기록합니다. 주문 도중 프로그램이 꺼져도 체결된 종목이 감시 대상에서 빠지지 않습니다.

## 4. 생성되는 파일 (`data\`)

| 파일 | 내용 |
|---|---|
| `logs\autotrader_YYYYMMDD.log` | 전체 실행 로그 |
| `trades.csv` | 매수/매도 주문 기록. 엑셀로 열어 두면 기록이 메모리에 보관됐다가, 파일을 닫은 뒤 다음 주문 때 함께 기록됨 |
| `state_<모드>_<App Key 지문>.json` (+ `.bak`) | 봇이 산 종목, 당일 매수/매도 이력, 미체결 주문. 깨져 있으면 실행을 멈추고 알림 |
| `tokens\` | 접근토큰 캐시 (다음 날 07:00 만료). 재시작해도 토큰을 다시 발급받지 않기 위한 파일 |
| `realtime.json` | 실시간 조건검색 등록 정보. 비정상 종료 뒤 다음 실행 때 등록을 해제하는 데 씀 |
| `strategy\strategy.json` | 매수 직전 추가 필터와 익절/손절/매수시간 (아래 4-1). 주간 재최적화가 자동으로 바꿈 |
| `strategy\daily_features.csv` | 전 종목 일봉 특성 (20일 이격도, 60일 최고가 등). 매일 저녁 준비 작업이 만듦 |
| `strategy\latest_report.md`, `strategy\history\` | 주간 재최적화 보고서와 이전 전략 파일 |
| `research\` | 재최적화용 원본 데이터 (일봉·5분봉·1분봉) |

## 4-1. 전략 파일과 주기적 로직 갱신

HTS 검색식(서버저장 조건식)은 그대로 두고, **봇이 매수 직전에 전략 파일의 조건을 한 번 더 확인**합니다.
매매 로직을 바꿀 때 HTS를 고칠 필요가 없습니다.

```
서버 검색식 편입 신호 → 매수 시간·보유·재매수 확인 → 전략 필터(strategy.json) → 매수
```

- `data\strategy\strategy.json`
  - `filters`: 쓸 수 있는 특성은 `price`(현재가), `ret_entry_pct`(전일 대비 등락률), `disparity20`(전일 종가 ÷ 20일선 × 100),
    `from_high60_pct`(직전 60거래일 최고가 대비 %), `ret1d_pct`, `ret5d_pct`, `ret20d_pct`. 각 항목에 `min`/`max`.
  - `trade`: `take_profit_pct`, `stop_loss_pct`, `buy_start`, `buy_end` — 있으면 `.env` 값보다 우선합니다.
  - 파일이 없거나 `"enabled": false` 면 추가 필터 없이 서버 검색식 신호를 그대로 씁니다.
  - 봇은 시작할 때와 날짜가 바뀔 때 파일을 다시 읽습니다. 파일이 잘못되면 시작하지 않고(실행 중이면 이전 전략 유지) 로그에 남깁니다.
- 일봉 특성 파일이 없거나 7일 넘게 오래됐으면, 일봉 특성이 필요한 필터는 통과하지 않습니다(매수 안 함).
- 확인: `check_strategy.bat` (적용될 설정, 필터 통과 종목 수, 최근 재최적화 보고서)
- 새 PC에서 주간 재최적화를 쓰려면 분석용 환경을 한 번 만듭니다 (numpy·pandas 포함, 봇의 `.venv` 와 별도):

  ```bat
  py -m venv analysis\.venv-ana
  analysis\.venv-ana\Scripts\python -m pip install -r analysis\requirements.txt
  ```

  일봉 특성은 `prep_daily.bat` 가 만들고, 첫 주간 재최적화는 종목별 5분봉 120일치를 새로 받으므로 오래 걸립니다.

| 예약 작업 (작업 스케줄러 `\StockTrading\`) | 시각 | 하는 일 |
|---|---|---|
| `Bot` → `run_scheduled.bat` | 평일 08:50 | 자동매매 실행 (이미 실행 중이면 새로 띄우지 않음) |
| `PrepDaily` → `prep_daily.bat` | 평일 18:10 | 전 종목 일봉 갱신 → 다음 거래일 특성 파일 (약 45분) |
| `WeeklyUpdate` → `weekly_update.bat` | 토요일 09:00 | 5분봉·1분봉 갱신 → 최근 40거래일로 후보 7,500개 평가 → 기준 통과 시 `strategy.json` 자동 교체 (약 1~2시간) |

주간 재최적화의 교체 기준 (`analysis\optimize.py`)
- 후보: 매수 마감(09:03~09:30) × 최고가 × 20일 이격도 상한 × 60일 최고가 대비 상한 × 익절(6~10%) × 손절(2~4%)
- 봇 규칙(1회 금액·최대 보유·당일 재매수 없음)으로 구간 전반·후반을 따로 시뮬레이션, 보수적 비용(매수 미끄러짐 0.6%)
- 모두 만족해야 통과: 전반·후반 모두 플러스 / 반기별 거래 20건 이상 / 미끄러짐 1%에서도 건당 플러스 / 최대낙폭 1.5×1회 금액 이내
- 통과 후보 중 이웃 격자까지 평균한 점수가 가장 높은 것을 고르고, 현재 전략이 기준 미달이거나 새 후보가 15% 이상 좋을 때만 교체
- 체결강도 조건(HTS 검색식의 J)은 과거 장중 값을 구할 수 없어 시뮬레이션에 없습니다.

## 5. 확인된 사항 / 확인되지 않은 사항

LS 공식 포털(openapi.ls-sec.co.kr)은 개발 환경에서 접속할 수 없었습니다. 아래 사항은 공식 TR 명세의 제3자 크롤링 2건, 공개 코드, 검색 결과에 인용된 공식 문구로 교차확인한 것입니다.

**확인됨**
- REST 주소는 `https://openapi.ls-sec.co.kr:8080`이며 실전·모의 공통입니다. 어느 서버로 붙을지는 App Key가 결정합니다.
- WebSocket 주소는 실전 `:9443/websocket`, 모의 `:29443/websocket`입니다.
- 토큰 요청은 `appsecretkey`, `scope=oob`, form 형식입니다. 유효기간은 다음 날 07:00까지입니다.
- 주문 CSPAT00601 코드값:
  - 종목번호는 `A`+종목코드
  - 매매구분 1 = 매도, 2 = 매수
  - 호가유형 03 = 시장가, 주문가격은 숫자 0
  - 성공 시 응답코드는 매수 00040, 매도 00039
- 조건검색 t1860은 등록 `E`, 해제 `D`로 요청합니다. 응답의 `sAlertNum`을 실시간 TR `AFR`의 `tr_key`로 씁니다.
- 초당 호출 한도: t1866/t1859/t1860 1회, t8407 5회, t0424 2회, CSPAT00601 10회.

**확인되지 않음 (실제 실행으로 확인 필요)**
- 모의투자 키로 조건검색(t1866/t1860/AFR)이 되는지 (안 된다는 커뮤니티 자료 2건)
- AFR의 `gsJobFlag` 값 의미
  - 공식 문서에는 '종목상태'라고만 되어 있습니다.
  - N/R/O = 진입/재진입/이탈은 xingAPI t1857과 같다고 보고 적용했습니다.
- 5분봉 지표 조건이 실시간검색에서 봉 단위로 평가되는 방식
- 등록 직후 AFR이 현재 만족 종목을 한꺼번에 보내는지 여부
  - 그래서 기본값으로는 시작 시점에 이미 만족하는 종목을 사지 않습니다(`BUY_INITIAL_MATCHES=false`).
- 모의투자 키로 t8407(현재가)을 쓸 수 있는지
  - 실패하면 잔고 조회(t0424)의 현재가로 대신 판단합니다.
- LS 게이트웨이의 WebSocket ping 응답 여부
  - pong을 보내지 않는다는 실측 보고가 있습니다.
  - 그래서 30초마다 ping을 보내 연결을 유지하되 pong은 기다리지 않습니다.

## 개발자용

```bat
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

테스트에는 가짜 LS 서버(REST + WebSocket)를 띄워 `main()` 전체 흐름을 검증하는 시나리오도 들어 있습니다.
