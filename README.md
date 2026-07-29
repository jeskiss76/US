# OBAF — Optimal Bottleneck Alpha Framework

S&P500 + NASDAQ100 전체 유니버스 대상 4단계 병목 필터링 퀀트 시스템

---

## 아키텍처

```
유니버스 (~600종목)
      │
      ▼
OBAF — Phase -1: MONTHLY RSI GATE (월봉 RSI 사전 필터 / 1차 관문)
다른 모든 필터링(Phase 0~3)에 앞서 실행되는 최초 관문.
월봉(월 단위) 종가 기준 RSI-14 를 계산하여, RSI가 기준값(기본 30) 이상인
종목은 이후 단계로 진행하지 못하도록 전부 제외한다.

- 월봉 RSI 안정화를 위해 별도로 장기간(기본 5년) 월봉 데이터를 수집한다.
  (본 파이프라인의 일봉 1년치 데이터를 리샘플링하면 월봉 바가 12개 내외에
   불과해 Wilder's Smoothing RSI-14가 충분히 수렴하지 않기 때문)
- 데이터 부족/수집 실패 종목은 안전하게 "제외" 처리한다 (보수적 접근).
- 이 게이트는 무거운 SEC/펀더멘털 수집 이전에 실행되어, 탈락 종목에 대한
  불필요한 후속 데이터 수집을 막아 파이프라인 비용/시간을 절감한다.
      │
      ▼
[Phase 0] VETO GATE
  - T(-3) DEBT  : Cash/Debt_12M ≥ 1.2x
  - T(-3) REFORM: 중국 공급망 의존도 < 30%
  - T(-3) SANCTION: Special Measure 6 / CLARITY Act 2026 / OFAC
      │
      ▼
[Phase 1] B_NECK_STRAT
  - 경로 A: 원본 병목 섹터 (Uranium/Copper/Grid Utilities 등) → 1.5x 프리미엄
  - 경로 B: GICS 11개 섹터 품질 게이트 (Gross Margin / Revenue Growth)
  - L(2) GRID: 전력망 초정밀 규격 (1GW+ 하드스탑)
      │
      ▼
[Phase 2] VALUATION & SURVIVAL
  - PRE_AUDIT : Risk-On DashScore ≥ 40 (전체 차단 게이트)
  - T(-2) LIQ : 매크로 헤어컷 (US10Y / WTI / 지정학)
  - T(0) SURV : 병목자산(ICR>2.5x, OCF>0) / 일반자산(ICR>5.0x, D/E<0.40)
      │
      ▼
[Phase 3] TECH EXECUTION & ANTI-FOMO
  - THESIS_FLIP : 내러티브 전환 → 100% 청산
  - WHALE_EXIT  : 기관 이탈 감지 → 50% IED 청산
  - ANTI-FOMO   : Price<SMA200 OR RSI>70 OR 이격>40% 차단
  - CORE ENTRY  : SMA200↑ + KDJ골든크로스(J<20) + DPI유입 + RSI[40,50]
      │
      ▼
Excel 보고서 + Telegram 알림
```

---

## 파일 구조

```
obaf_system/
├── main.py                          # 마스터 오케스트레이터
├── requirements.txt
├── config/
│   ├── settings.yaml                # 시스템 설정 (환경변수 주입)
│   ├── sector_weights.yaml          # GICS 섹터 가중치 / 품질 게이트
│   └── blacklist.json               # 정적 블랙리스트 (SM6 / CLARITY Act)
├── src/
│   ├── universe.py                  # S&P500 + NASDAQ100 유니버스 관리
│   ├── data_pipeline.py             # yfinance + SEC EDGAR 파이프라인
│   ├── alt_data.py                  # 대체 데이터 엔진
│   ├── macro_data.py                # 매크로 데이터 (yfinance + FRED)
│   ├── dark_pool_proxy.py           # DPI 프록시 + 기술적 지표 엔진
│   ├── phase0_veto_gate.py          # Phase 0
│   ├── phase1_bneck_strat.py        # Phase 1
│   ├── phase2_valuation.py          # Phase 2
│   ├── phase3_tech_execution.py     # Phase 3
│   └── reporter.py                  # Excel + Telegram
├── reports/                         # Excel 보고서 출력
├── logs/                            # 실행 로그
└── .github/workflows/
    └── obaf_daily.yml               # GitHub Actions 스케줄러
```

---

## 설치 및 실행

### 로컬 실행

```bash
git clone https://github.com/YOUR_USERNAME/obaf_system.git
cd obaf_system
pip install -r requirements.txt

# 환경변수 설정
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
export FRED_API_KEY="your_fred_key"
export SEC_USER_AGENT="YourName your@email.com"

python main.py
```

### GitHub Actions 설정

`Settings → Secrets and variables → Actions → New repository secret`

| Secret 이름 | 설명 |
|-------------|------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot 토큰 |
| `TELEGRAM_CHAT_ID` | Telegram 채팅 ID |
| `FRED_API_KEY` | FRED API 키 |
| `SEC_USER_AGENT` | SEC EDGAR 헤더 (예: `MyFund admin@myfund.com`) |

---

## 실행 스케줄 (KST)

| 시간 | UTC | 목적 |
|------|-----|------|
| 08:00 | 23:00 (전일) | 프리마켓 전 스크리닝 |
| 17:00 | 08:00 | 미국 프리마켓 점검 |
| 21:00 | 12:00 | 정규장 개장 전 최종 점검 |

---

## 데이터 소스

| 데이터 | 소스 | 비용 |
|--------|------|------|
| 가격 OHLCV | yfinance (Yahoo Finance) | 무료 |
| 재무 공시 | SEC EDGAR XBRL | 무료 |
| 매크로 (US10Y, WTI) | yfinance → FRED API 백업 | 무료 |
| 다크풀 프록시 | OHLCV 기반 자체 계산 | 무료 |
| OFAC 제재 | 미국 재무부 공식 CSV | 무료 |
| 유니버스 | Wikipedia HTML 파싱 | 무료 |
