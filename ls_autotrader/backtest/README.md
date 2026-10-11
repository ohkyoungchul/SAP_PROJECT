# 9월 백테스트 (조건식 '떡상이(2023.04.03)' + 자동매매 규칙)

주문 TR은 쓰지 않고 조회 TR(t8436, t1404, t1405, t8412, t1305)만 호출한다.
자동매매와 같은 키를 쓰므로 자동매매가 돌지 않는 시간(15:40 이후 ~ 다음 날 08:50 전)에 실행한다.

## 1. 데이터 받기 (PC)

```bat
cd C:\workspace\ls_autotrader
:: 시험: 두 종목만 받아서 9월 5분봉이 오는지 확인 (로그의 "첫 봉" 날짜가 20260825 근처여야 함)
.venv\Scripts\python -m backtest.fetch_data --codes 005930,000660 --out data\backtest_test
:: 전체 (우선주·ETF·ETN·스팩 제외 약 2,400종목, 종목당 약 7초)
.venv\Scripts\python -m backtest.fetch_data
```

끊기면 같은 명령을 다시 실행하면 받은 종목은 건너뛴다.
모의투자 키로 차트 조회가 안 되면 `.env`에 `LS_DATA_APPKEY`/`LS_DATA_APPSECRET`(실전 키, 조회 전용)을 넣는다.

## 2. 시뮬레이션

```bat
.venv\Scripts\python -m backtest.simulate
.venv\Scripts\python -m backtest.simulate --no-strength
```

결과: `data\backtest\result\summary.txt`, `trades.csv` (`--no-strength` 는 `result_no_strength`).
판단 기준과 근사 내용은 `simulate.py` 맨 위 설명에 있다.
