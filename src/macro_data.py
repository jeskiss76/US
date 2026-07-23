"""
OBAF — Macro Data Fetcher
미국채 10Y / WTI 원유 / 지정학 리스크 지수 / Risk-On 대시보드 스코어 수집
1차: yfinance  |  2차 백업: FRED API
"""

import logging
import requests
import yfinance as yf

logger = logging.getLogger("MacroDataFetcher")

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


class MacroDataFetcher:
    """
    매크로 데이터 수집기
    US10Y  : ^TNX (yfinance) → DGS10 (FRED)
    CrudeOil: CL=F (yfinance) → DCOILWTICO (FRED)
    GeoIndex: ^VIX 기반 프록시
    DashScore: SPY/QQQ vs SMA200 + VIX + US10Y 복합 지수
    """

    def __init__(self, fred_api_key: str = None):
        self.fred_api_key = fred_api_key

    # ------------------------------------------------------------------
    # 내부 헬퍼 — FRED API 조회
    # ------------------------------------------------------------------
    def _fred_latest(self, series_id: str) -> float | None:
        if not self.fred_api_key:
            return None
        try:
            params = {
                "series_id":  series_id,
                "api_key":    self.fred_api_key,
                "file_type":  "json",
                "limit":      5,
                "sort_order": "desc"
            }
            resp = requests.get(FRED_BASE, params=params, timeout=10)
            resp.raise_for_status()
            for obs in resp.json().get("observations", []):
                if obs["value"] != ".":
                    return float(obs["value"])
        except Exception as e:
            logger.warning(f"FRED [{series_id}] 조회 실패: {e}")
        return None

    # ------------------------------------------------------------------
    # 1. 미국채 10Y 금리
    # ------------------------------------------------------------------
    def fetch_us10y(self) -> float:
        try:
            hist = yf.Ticker("^TNX").history(period="5d")
            if not hist.empty:
                val = round(hist["Close"].iloc[-1], 4)
                logger.info(f"US10Y (yfinance): {val}%")
                return val
        except Exception as e:
            logger.warning(f"yfinance ^TNX 실패: {e}")

        val = self._fred_latest("DGS10")
        if val is not None:
            logger.info(f"US10Y (FRED): {val}%")
            return val

        logger.error("US10Y 수집 실패 → 기본값 4.0% 사용")
        return 4.0

    # ------------------------------------------------------------------
    # 2. WTI 원유 가격
    # ------------------------------------------------------------------
    def fetch_crude_oil(self) -> float:
        try:
            hist = yf.Ticker("CL=F").history(period="5d")
            if not hist.empty:
                val = round(hist["Close"].iloc[-1], 2)
                logger.info(f"CrudeOil (yfinance): ${val}")
                return val
        except Exception as e:
            logger.warning(f"yfinance CL=F 실패: {e}")

        val = self._fred_latest("DCOILWTICO")
        if val is not None:
            logger.info(f"CrudeOil (FRED): ${val}")
            return val

        logger.error("CrudeOil 수집 실패 → 기본값 80.0$ 사용")
        return 80.0

    # ------------------------------------------------------------------
    # 3. 지정학 리스크 지수 (VIX 기반 프록시)
    # VIX 20 → GeoIndex≈40 / VIX 30 → GeoIndex≈60 / VIX 40 → GeoIndex≈80
    # ------------------------------------------------------------------
    def fetch_geo_index(self) -> float:
        try:
            hist = yf.Ticker("^VIX").history(period="5d")
            if not hist.empty:
                vix = hist["Close"].iloc[-1]
                geo = round(min(vix * 2.0, 100.0), 2)
                logger.info(f"GeoIndex (VIX proxy): VIX={vix:.2f} → GeoIndex={geo}")
                return geo
        except Exception as e:
            logger.warning(f"VIX 조회 실패: {e}")
        return 50.0

    # ------------------------------------------------------------------
    # 4. Risk-On 대시보드 스코어 (PRE_AUDIT 기준)
    # SPY > SMA200: +30 | QQQ > SMA200: +20 | US10Y < 4.5: +20 | VIX < 20: +30
    # ------------------------------------------------------------------
    def fetch_dash_score(self) -> float:
        score = 0.0
        try:
            for etf, pts in [("SPY", 30.0), ("QQQ", 20.0)]:
                hist = yf.Ticker(etf).history(period="1y")
                if not hist.empty and len(hist) >= 200:
                    price  = hist["Close"].iloc[-1]
                    sma200 = hist["Close"].tail(200).mean()
                    if price > sma200:
                        score += pts
                        logger.info(f"{etf} > SMA200 ({price:.2f} > {sma200:.2f}) → +{pts}점")

            us10y = self.fetch_us10y()
            if us10y < 4.5:
                score += 20.0
                logger.info(f"US10Y {us10y}% < 4.5% → +20점")

            vix_hist = yf.Ticker("^VIX").history(period="5d")
            if not vix_hist.empty:
                vix = vix_hist["Close"].iloc[-1]
                if vix < 20.0:
                    score += 30.0
                    logger.info(f"VIX {vix:.2f} < 20 → +30점")
                elif vix < 25.0:
                    score += 15.0
                    logger.info(f"VIX {vix:.2f} 20~25 → +15점")

        except Exception as e:
            logger.error(f"DashScore 계산 중 오류: {e}")
            return 50.0

        logger.info(f"Risk-On DashScore 최종: {score:.1f}")
        return score

    # ------------------------------------------------------------------
    # 5. 전체 일괄 수집
    # ------------------------------------------------------------------
    def fetch_all(self) -> dict:
        """모든 매크로 데이터 일괄 수집"""
        us10y   = self.fetch_us10y()
        oil     = self.fetch_crude_oil()
        geo     = self.fetch_geo_index()
        dash    = self.fetch_dash_score()
        return {
            "US10Y":      us10y,
            "CrudeOil":   oil,
            "GeoIndex":   geo,
            "DashScore":  dash
        }
