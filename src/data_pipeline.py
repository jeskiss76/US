"""
OBAF — Quant Data Pipeline v2.1
yfinance 가격 데이터 + SEC EDGAR 재무 데이터 수집
ICR: yfinance financials(income statement) 기반 정확 계산
"""

import gc
import logging
import random
import time

import requests
import yfinance as yf
import pandas as pd
import numpy as np

logger = logging.getLogger("QuantDataPipeline")

SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


class QuantDataPipeline:

    def __init__(self, sec_user_agent: str, config: dict):
        self.sec_headers     = {"User-Agent": sec_user_agent}
        self.price_period    = config.get("price_period",     "1y")
        self.price_interval  = config.get("price_interval",   "1d")
        self.sec_delay       = config.get("sec_delay_seconds", 0.12)
        self.max_retries     = config.get("max_retries",       5)
        self.initial_backoff = config.get("initial_backoff",   2)

    # ------------------------------------------------------------------
    # 지수 백오프 재시도 래퍼
    # ------------------------------------------------------------------
    def _execute_with_retry(self, func, *args, **kwargs):
        retries = 0
        backoff = self.initial_backoff
        last_exc = None
        while retries < self.max_retries:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exc = e
                retries += 1
                if retries >= self.max_retries:
                    break
                sleep_time = backoff + random.uniform(0.0, 1.0)
                logger.debug(f"재시도 {retries}/{self.max_retries} [{str(e)[:60]}] {sleep_time:.1f}초 대기")
                time.sleep(sleep_time)
                backoff *= 2
        logger.warning(f"최대 재시도 초과: {str(last_exc)[:80]}")
        return None

    # ------------------------------------------------------------------
    # 1. 가격 데이터
    # ------------------------------------------------------------------
    def fetch_price_data(self, ticker: str) -> pd.DataFrame | None:
        def _fetch():
            stock = yf.Ticker(ticker)
            df = stock.history(
                period=self.price_period,
                interval=self.price_interval,
                auto_adjust=True
            )
            if df is None or df.empty:
                raise ValueError(f"{ticker}: 빈 데이터프레임")
            return df
        return self._execute_with_retry(_fetch)

    # ------------------------------------------------------------------
    # 2. SEC EDGAR 원천 재무 데이터
    # ------------------------------------------------------------------
    def fetch_sec_financials(self, cik: str) -> dict | None:
        if not cik:
            return None
        url = SEC_FACTS_URL.format(cik=cik.zfill(10))
        def _fetch():
            resp = requests.get(url, headers=self.sec_headers, timeout=15)
            if resp.status_code == 429:
                raise RuntimeError("SEC Rate Limit")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        data = self._execute_with_retry(_fetch)
        time.sleep(self.sec_delay)
        return data

    # ------------------------------------------------------------------
    # 3. ICR 정확 계산 (yfinance financials 기반)
    # ------------------------------------------------------------------
    def _calc_icr_from_financials(self, ticker: str) -> float | None:
        """
        ICR = EBIT / InterestExpense
        yfinance income_stmt (연간) 에서 직접 추출
        """
        try:
            tk   = yf.Ticker(ticker)
            stmt = tk.income_stmt  # 연간 손익계산서 DataFrame

            if stmt is None or stmt.empty:
                return None

            # 행 인덱스를 소문자로 정규화
            stmt.index = stmt.index.str.lower().str.strip()

            # EBIT 후보 행
            ebit_candidates = [
                "ebit", "operating income", "operating income or loss",
                "earnings before interest and taxes"
            ]
            # 이자비용 후보 행
            int_candidates = [
                "interest expense", "interest expense non operating",
                "total other income expense net",
                "net interest income"
            ]

            ebit_val = None
            for cand in ebit_candidates:
                if cand in stmt.index:
                    row = stmt.loc[cand].dropna()
                    if not row.empty:
                        ebit_val = float(row.iloc[0])
                        break

            int_val = None
            for cand in int_candidates:
                if cand in stmt.index:
                    row = stmt.loc[cand].dropna()
                    if not row.empty:
                        int_val = abs(float(row.iloc[0]))
                        break

            if ebit_val is not None and int_val and int_val > 0:
                icr = round(ebit_val / int_val, 4)
                logger.debug(f"[{ticker}] ICR={icr:.2f}x (EBIT={ebit_val:,.0f}/Int={int_val:,.0f})")
                return icr

        except Exception as e:
            logger.debug(f"[{ticker}] ICR financials 파싱 실패: {e}")
        return None

    # ------------------------------------------------------------------
    # 4. yfinance fundamentals
    # ------------------------------------------------------------------
    def fetch_fundamentals(self, ticker: str) -> dict:
        default = {
            "icr": 5.0, "debt_to_equity": 0.5, "ocf": 1.0,
            "current_ratio": 1.5, "gross_margin": 0.30,
            "revenue_growth": 0.05, "market_cap": 0.0,
            "pe_ratio": 20.0, "roe": 0.10,
        }

        def _fetch():
            info = yf.Ticker(ticker).info or {}

            # ── ICR 계산 (정확도 우선순위) ──────────────────────────
            icr = None

            # 방법 1: info에서 ebit + interestExpense
            ebit    = info.get("ebit")
            int_exp = info.get("interestExpense")
            if ebit and int_exp and abs(int_exp) > 0:
                icr = round(abs(ebit / int_exp), 4)

            # 방법 2: yfinance financials (income_stmt)
            if icr is None:
                icr = self._calc_icr_from_financials(ticker)

            # 방법 3: operatingCashflow / interestExpense
            if icr is None:
                ocf_raw = info.get("operatingCashflow", 0) or 0
                if int_exp and abs(int_exp) > 0:
                    icr = round(abs(ocf_raw / int_exp), 4)

            # 방법 4: 섹터별 합리적 기본값
            if icr is None or icr <= 0:
                icr = default["icr"]

            # D/E: yfinance debtToEquity는 이미 비율값 (단위 주의)
            # yfinance는 % 단위로 반환하는 경우 있음 → 100 초과시 /100 변환
            de_raw = info.get("debtToEquity", None)
            if de_raw is not None and de_raw > 0:
                de = de_raw / 100.0 if de_raw > 20 else de_raw
            else:
                de = default["debt_to_equity"]

            return {
                "icr":            round(icr, 4),
                "debt_to_equity": round(de, 4),
                "ocf":            info.get("operatingCashflow", default["ocf"]) or default["ocf"],
                "current_ratio":  info.get("currentRatio",      default["current_ratio"]) or default["current_ratio"],
                "gross_margin":   info.get("grossMargins",      default["gross_margin"])  or default["gross_margin"],
                "revenue_growth": info.get("revenueGrowth",     default["revenue_growth"]) or default["revenue_growth"],
                "market_cap":     info.get("marketCap",         0) or 0,
                "pe_ratio":       info.get("trailingPE",        default["pe_ratio"]) or default["pe_ratio"],
                "roe":            info.get("returnOnEquity",    default["roe"]) or default["roe"],
            }

        result = self._execute_with_retry(_fetch)
        return result if result else default

    # ------------------------------------------------------------------
    # 5. 단기 유동성 지표
    # ------------------------------------------------------------------
    def fetch_liquidity_metrics(self, ticker: str, sec_facts: dict | None) -> tuple[float, float]:
        cash, debt_12m = 0.0, 0.0

        # SEC XBRL
        if sec_facts:
            try:
                us_gaap = sec_facts.get("facts", {}).get("us-gaap", {})
                def _latest(concept):
                    entries = us_gaap.get(concept, {}).get("units", {}).get("USD", [])
                    annual  = [e for e in entries if e.get("form") in ("10-K", "10-Q")]
                    if not annual:
                        return None
                    annual.sort(key=lambda x: x.get("end", ""), reverse=True)
                    return float(annual[0]["val"])

                for c in ("CashAndCashEquivalentsAtCarryingValue",
                          "CashCashEquivalentsAndShortTermInvestments"):
                    v = _latest(c)
                    if v:
                        cash = v / 1e6; break

                for c in ("LongTermDebtCurrent", "ShortTermBorrowings",
                          "DebtCurrent", "NotesPayableCurrent"):
                    v = _latest(c)
                    if v:
                        debt_12m += v / 1e6
            except Exception:
                pass

        # yfinance 폴백
        if cash == 0.0 or debt_12m == 0.0:
            try:
                info = yf.Ticker(ticker).info or {}
                if cash == 0.0:
                    cash = round((info.get("totalCash") or
                                  info.get("cashAndShortTermInvestments") or 0) / 1e6, 4)
                if debt_12m == 0.0:
                    debt_12m = round((info.get("currentDebt") or
                                      info.get("shortTermDebt") or 0) / 1e6, 4)
            except Exception:
                pass

        return cash, debt_12m

    # ------------------------------------------------------------------
    # 6. 전체 파이프라인
    # ------------------------------------------------------------------
    def run_pipeline(self, target_assets: list[dict]) -> dict:
        logger.info(f"=== QuantDataPipeline 가동: {len(target_assets)}개 종목 ===")
        raw_database = {}

        for idx, asset in enumerate(target_assets, 1):
            ticker  = asset.get("ticker", "")
            cik     = asset.get("cik",    "")
            company = asset.get("company", "Unknown")
            gics    = asset.get("gics_sector", "Unknown")

            if not ticker:
                continue

            if idx % 50 == 1:
                logger.info(f"[{idx:03d}/{len(target_assets)}] 진행 중 ...")

            try:
                prices = self.fetch_price_data(ticker)
                if prices is None or prices.empty:
                    continue

                sec_data     = self.fetch_sec_financials(cik)
                fundamentals = self.fetch_fundamentals(ticker)
                cash, debt_12m = self.fetch_liquidity_metrics(ticker, sec_data)

                raw_database[ticker] = {
                    "prices":       prices,
                    "raw_sec":      sec_data,
                    "fundamentals": fundamentals,
                    "cash":         cash,
                    "debt_12m":     debt_12m,
                    "gics_sector":  gics,
                    "company":      company,
                }

            except Exception as e:
                logger.warning(f"[{ticker}] 파이프라인 오류: {str(e)[:80]}")
                continue
            finally:
                gc.collect()

        logger.info(f"=== 파이프라인 완료: {len(raw_database)}/{len(target_assets)}개 ===")
        return raw_database
