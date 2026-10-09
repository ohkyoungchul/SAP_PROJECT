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

## 2. 설치 (Windows 명령 프롬프트)

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
