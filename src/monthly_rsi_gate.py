"""
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

[검증용] 월봉 RSI(장기 관점)와 Phase 3 CORE ENTRY의 일봉 RSI(단기 관점, 기본
40~50) 는 서로 다른 시계열에서 계산되는 별개의 지표이므로 상충하지 않는다.
다만 실제로 두 조건을 함께 만족하는 종목이 얼마나 되는지 감을 잡을 수 있도록,
이 게이트를 통과한(월봉 RSI<30) 종목에 한해 일봉 RSI-14도 함께 계산해
검증용 Excel에 같이 표기한다.
"""

import gc
import logging
import random
import time

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger("Phase-1_MonthlyRSIGate")


def calc_monthly_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Smoothing RSI (src/dark_pool_proxy.py calc_rsi 와 동일 로직)"""
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


class MonthlyRSIGateFilter:
    """
    Phase -1: 월봉 RSI 사전 필터 게이트
    - 월봉 종가 기준 RSI-14 계산
    - RSI >= max_monthly_rsi (기본 30) → 즉시 제외
    - RSI < max_monthly_rsi → 통과 (다음 단계로 진행)
    - (검증용) 통과 종목은 일봉 RSI-14도 함께 계산해 Phase 3 CORE ENTRY
      RSI 구간(기본 40~50)과 비교할 수 있게 한다.
    """

    def __init__(self, config: dict):
        p  = config.get("thresholds", {}).get("phase_minus1", {})
        p3 = config.get("thresholds", {}).get("phase3", {})

        self.max_monthly_rsi = p.get("max_monthly_rsi", 30)
        self.rsi_period      = p.get("rsi_period", 14)
        self.lookback_period = p.get("lookback_period", "5y")
        self.max_retries     = p.get("max_retries", 3)
        self.initial_backoff = p.get("initial_backoff", 2)

        # 일봉 RSI 교차 검증용 (Phase 3 CORE ENTRY 구간과 동일 기준 재사용)
        self.daily_rsi_lookback = p.get("daily_rsi_lookback_period", "1y")
        self.core_entry_min_rsi = p3.get("core_entry_min_rsi", 40)
        self.core_entry_max_rsi = p3.get("core_entry_max_rsi", 50)

    # ------------------------------------------------------------------
    # 월봉/일봉 종가 수집 공용 (지수 백오프 재시도)
    # ------------------------------------------------------------------
    def _fetch_close(self, ticker: str, period: str, interval: str) -> pd.Series | None:
        retries = 0
        backoff = self.initial_backoff
        last_exc = None
        while retries < self.max_retries:
            try:
                df = yf.Ticker(ticker).history(
                    period=period,
                    interval=interval,
                    auto_adjust=True,
                )
                if df is None or df.empty:
                    raise ValueError(f"{ticker}: {interval} 데이터 없음")
                return df["Close"]
            except Exception as e:
                last_exc = e
                retries += 1
                if retries >= self.max_retries:
                    break
                sleep_time = backoff + random.uniform(0.0, 1.0)
                logger.debug(
                    f"[{ticker}] {interval} 재시도 {retries}/{self.max_retries} "
                    f"[{str(e)[:60]}] {sleep_time:.1f}초 대기"
                )
                time.sleep(sleep_time)
                backoff *= 2
        logger.warning(f"[{ticker}] {interval} 데이터 수집 최종 실패: {str(last_exc)[:80]}")
        return None

    def _fetch_monthly_close(self, ticker: str) -> pd.Series | None:
        return self._fetch_close(ticker, self.lookback_period, "1mo")

    def _fetch_daily_close(self, ticker: str) -> pd.Series | None:
        return self._fetch_close(ticker, self.daily_rsi_lookback, "1d")

    # ------------------------------------------------------------------
    # (검증용) 일봉 RSI 교차 계산 — 월봉 게이트 통과 종목만 대상으로 실행
    # ------------------------------------------------------------------
    def _cross_check_daily_rsi(self, ticker: str) -> dict:
        """
        Returns
        -------
        dict: {"daily_rsi": float|None, "in_core_entry_band": bool|None}
        """
        close = self._fetch_daily_close(ticker)
        if close is None or len(close.dropna()) < self.rsi_period + 1:
            return {"daily_rsi": None, "in_core_entry_band": None}

        daily_rsi = float(calc_monthly_rsi(close, self.rsi_period).iloc[-1])
        in_band = self.core_entry_min_rsi <= daily_rsi <= self.core_entry_max_rsi
        return {"daily_rsi": round(daily_rsi, 2), "in_core_entry_band": in_band}

    # ------------------------------------------------------------------
    # 개별 종목 판정
    # ------------------------------------------------------------------
    def check_ticker(self, ticker: str) -> dict:
        """
        Returns
        -------
        dict: {
            "ticker", "monthly_rsi" (float|None), "pass" (bool), "reason" (str),
            "daily_rsi" (float|None), "in_core_entry_band" (bool|None)
            — daily_rsi/in_core_entry_band 는 월봉 게이트 통과 종목만 계산됨
              (탈락 종목은 후속 계산 불필요 → None/"N/A")
        }
        """
        close = self._fetch_monthly_close(ticker)

        if close is None or len(close.dropna()) < self.rsi_period + 1:
            reason = "[Phase -1: MONTHLY_RSI] 월봉 데이터 부족 → 보수적 제외"
            logger.warning(f"[{ticker}] EXCLUDE: {reason}")
            return {
                "ticker": ticker, "monthly_rsi": None, "pass": False, "reason": reason,
                "daily_rsi": None, "in_core_entry_band": None,
            }

        rsi_val = float(calc_monthly_rsi(close, self.rsi_period).iloc[-1])

        if rsi_val >= self.max_monthly_rsi:
            reason = (
                f"[Phase -1: MONTHLY_RSI] 월봉 RSI {rsi_val:.2f} "
                f">= 기준 {self.max_monthly_rsi} → 제외"
            )
            logger.info(f"[{ticker}] EXCLUDE: {reason}")
            return {
                "ticker": ticker, "monthly_rsi": round(rsi_val, 2), "pass": False, "reason": reason,
                "daily_rsi": None, "in_core_entry_band": None,
            }

        reason = (
            f"[Phase -1: MONTHLY_RSI] 월봉 RSI {rsi_val:.2f} "
            f"< 기준 {self.max_monthly_rsi} → 통과"
        )
        logger.info(f"[{ticker}] PASS: {reason}")

        # ── 통과 종목만 일봉 RSI 교차 검증 (Phase 3 CORE ENTRY 구간 비교) ──
        cross = self._cross_check_daily_rsi(ticker)

        return {
            "ticker": ticker, "monthly_rsi": round(rsi_val, 2), "pass": True, "reason": reason,
            **cross,
        }

    # ------------------------------------------------------------------
    # 전체 유니버스 일괄 실행
    # ------------------------------------------------------------------
    def run_gate(self, target_assets: list[dict]) -> tuple[list[dict], dict]:
        """
        Parameters
        ----------
        target_assets : list[dict]   universe.get_full_universe() 결과

        Returns
        -------
        survivors : list[dict]   월봉 RSI < 기준 통과 종목만 (target_assets 서브셋)
        details   : dict {ticker: {monthly_rsi, pass, reason, daily_rsi,
                    in_core_entry_band, company, gics_sector}}
                    (통과/탈락 전 종목 포함 — 검증용 Excel 생성에 사용)
        """
        logger.info(
            f"=== Phase -1: 월봉 RSI 사전 필터 게이트 시작 "
            f"(대상 {len(target_assets)}개, 기준: RSI<{self.max_monthly_rsi}) ==="
        )
        survivors = []
        details = {}

        for idx, asset in enumerate(target_assets, 1):
            ticker = asset.get("ticker", "")
            if not ticker:
                continue
            if idx % 50 == 1:
                logger.info(f"[{idx:03d}/{len(target_assets)}] 월봉 RSI 검사 중 ...")
            try:
                result = self.check_ticker(ticker)
                details[ticker] = {
                    **result,
                    "company":     asset.get("company", "Unknown"),
                    "gics_sector": asset.get("gics_sector", "Unknown"),
                }
                if result["pass"]:
                    survivors.append(asset)
            except Exception as e:
                reason = f"[Phase -1: MONTHLY_RSI] 예외 발생: {str(e)[:80]}"
                logger.error(f"[{ticker}] Phase -1 처리 중 예외: {str(e)}")
                details[ticker] = {
                    "ticker": ticker, "monthly_rsi": None, "pass": False,
                    "reason": reason, "daily_rsi": None, "in_core_entry_band": None,
                    "company": asset.get("company", "Unknown"),
                    "gics_sector": asset.get("gics_sector", "Unknown"),
                }
            finally:
                gc.collect()

        logger.info(
            f"=== Phase -1 완료 — 통과: {len(survivors)} / {len(target_assets)} "
            f"(월봉 RSI < {self.max_monthly_rsi}) ==="
        )
        return survivors, details

