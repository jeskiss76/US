"""
OBAF — Dark Pool Proxy Engine + Technical Indicator Engine
[Q3-1] OHLCV 기반 다크풀 유입 프록시 (외부 API 불필요)
- 달러볼륨 Z-score (20일 롤링)
- 블록 트레이드 비율 (볼륨 2x 초과 + 가격 레인지 협소)
- 방향성 압력 지수 (Directional Pressure Index)

기술적 지표 (Phase 3 입력값 생성)
- SMA200 + 기울기
- RSI-14 (Wilder's Smoothing)
- KDJ(9,3,3)
- Thesis Flip / OI 프록시
"""

import gc
import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("DarkPoolProxy")


# =============================================================================
# 기술적 지표 계산 함수 모음
# =============================================================================

def calc_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Smoothing RSI"""
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def calc_kdj(high: pd.Series, low: pd.Series, close: pd.Series,
             n: int = 9, m1: int = 3, m2: int = 3
             ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    KDJ (Stochastic Oscillator 변형)
    K = EMA(RSV, 1/m1)  D = EMA(K, 1/m2)  J = 3K - 2D
    """
    ll  = low.rolling(n, min_periods=1).min()
    hh  = high.rolling(n, min_periods=1).max()
    rsv = ((close - ll) / (hh - ll + 1e-10) * 100).fillna(50.0)
    k   = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d   = k.ewm(alpha=1 / m2, adjust=False).mean()
    j   = 3 * k - 2 * d
    return k, d, j


def calc_macd(series: pd.Series,
              fast: int = 12, slow: int = 26, signal: int = 9
              ) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast   = series.ewm(span=fast, adjust=False).mean()
    ema_slow   = series.ewm(span=slow, adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


# =============================================================================
# DPI 프록시 엔진
# =============================================================================

class DarkPoolProxyEngine:
    """
    OHLCV 기반 다크풀 유입 프록시 엔진
    최종 출력: dpi_inflow (bool), dpi_score (0~1)
    """

    def __init__(self, config: dict):
        self.vol_zscore_threshold  = config.get("vol_zscore_threshold",  1.5)
        self.block_vol_multiple    = config.get("block_vol_multiple",    2.0)
        self.block_range_threshold = config.get("block_range_threshold", 0.005)
        self.dir_pressure_threshold = config.get("dir_pressure_threshold", 0.6)
        self.lookback              = config.get("lookback_days",          20)

    def _dollar_vol_zscore(self, df: pd.DataFrame) -> float:
        """달러볼륨 Z-score: (현재 달러볼륨 - 20일 평균) / 20일 표준편차"""
        if len(df) < self.lookback + 1:
            return 0.0
        dollar_vol = df["Close"] * df["Volume"]
        recent     = dollar_vol.iloc[-self.lookback:]
        mean_dv    = recent.mean()
        std_dv     = recent.std()
        if std_dv < 1e-10:
            return 0.0
        return float((dollar_vol.iloc[-1] - mean_dv) / std_dv)

    def _block_trade_ratio(self, df: pd.DataFrame) -> float:
        """
        블록 트레이드 비율:
        지난 lookback일 중 (볼륨 > 평균*배수 AND 레인지 < 임계) 일수 비율
        """
        if len(df) < self.lookback:
            return 0.0
        sub = df.iloc[-self.lookback:].copy()
        avg_vol   = sub["Volume"].mean()
        price_rng = (sub["High"] - sub["Low"]) / (sub["Close"] + 1e-10)
        block_days = (
            (sub["Volume"] > avg_vol * self.block_vol_multiple) &
            (price_rng < self.block_range_threshold)
        ).sum()
        return float(block_days / self.lookback)

    def _directional_pressure(self, df: pd.DataFrame, window: int = 5) -> float:
        """
        방향성 압력 지수: 볼륨 가중 (종가-시가)/(고가-저가) 5일 평균
        양수 → 매수 압력, 음수 → 매도 압력
        """
        if len(df) < window:
            return 0.0
        sub = df.iloc[-window:].copy()
        hl  = (sub["High"] - sub["Low"]).replace(0, np.nan)
        dp  = ((sub["Close"] - sub["Open"]) / hl).fillna(0) * sub["Volume"]
        return float(dp.sum() / sub["Volume"].sum()) if sub["Volume"].sum() > 0 else 0.0

    def score_ticker(self, ticker: str, df: pd.DataFrame) -> dict:
        """
        단일 종목 DPI + 기술적 지표 전체 계산

        Returns
        -------
        dict with keys:
            price, sma200, sma200_slope_up, rsi, kdj_j, kdj_golden_cross,
            dpi_inflow, dpi_score, oi_down, thesis_flip,
            vol_zscore, block_ratio, dir_pressure
        """
        result = {
            "price": 0.0, "sma200": 0.0, "sma200_slope_up": False,
            "rsi": 50.0, "kdj_j": 50.0, "kdj_golden_cross": False,
            "dpi_inflow": False, "dpi_score": 0.0,
            "oi_down": False, "thesis_flip": False,
            "vol_zscore": 0.0, "block_ratio": 0.0, "dir_pressure": 0.0
        }

        if df is None or df.empty or len(df) < 30:
            logger.warning(f"[{ticker}] 데이터 부족 → 기본값 반환")
            return result

        try:
            close  = df["Close"]
            high   = df["High"]
            low    = df["Low"]
            volume = df["Volume"]

            # ── 가격 & SMA200 ────────────────────────────────────────
            price = float(close.iloc[-1])
            sma200_series = calc_sma(close, 200)
            sma200 = float(sma200_series.iloc[-1]) if not sma200_series.isna().iloc[-1] else price
            sma200_prev10 = (
                float(sma200_series.iloc[-11])
                if len(sma200_series.dropna()) >= 11
                else sma200
            )
            sma200_slope_up = sma200 > sma200_prev10

            # ── RSI ──────────────────────────────────────────────────
            rsi = float(calc_rsi(close).iloc[-1])

            # ── KDJ ──────────────────────────────────────────────────
            k_ser, d_ser, j_ser = calc_kdj(high, low, close)
            kdj_j    = float(j_ser.iloc[-1])
            k_curr   = float(k_ser.iloc[-1])
            d_curr   = float(d_ser.iloc[-1])
            k_prev   = float(k_ser.iloc[-2]) if len(k_ser) >= 2 else k_curr
            d_prev   = float(d_ser.iloc[-2]) if len(d_ser) >= 2 else d_curr
            kdj_cross = (k_prev <= d_prev) and (k_curr > d_curr)  # Golden Cross

            # ── DPI 프록시 ───────────────────────────────────────────
            vol_z   = self._dollar_vol_zscore(df)
            blk_r   = self._block_trade_ratio(df)
            dir_p   = self._directional_pressure(df)

            # DPI 종합 스코어 (0~1): 각 신호 0~1 정규화 후 가중 평균
            z_norm  = min(max((vol_z  - (-3)) / 6, 0), 1)
            b_norm  = min(blk_r * 5, 1)
            d_norm  = min(max((dir_p + 1) / 2, 0), 1)
            dpi_score  = round(z_norm * 0.5 + b_norm * 0.3 + d_norm * 0.2, 4)
            dpi_inflow = (
                vol_z >= self.vol_zscore_threshold and
                dir_p >= self.dir_pressure_threshold
            )

            # ── OI 프록시 ────────────────────────────────────────────
            # 볼륨 5일 평균 < 20일 평균 AND 가격 > SMA50 → 분배 신호
            sma50   = calc_sma(close, 50)
            vol5    = volume.iloc[-5:].mean()  if len(volume) >= 5  else 0
            vol20   = volume.iloc[-20:].mean() if len(volume) >= 20 else 0
            sma50_v = float(sma50.iloc[-1])    if not sma50.isna().iloc[-1] else price
            oi_down = bool(vol5 < vol20 * 0.85 and price > sma50_v)

            # ── Thesis Flip ───────────────────────────────────────────
            # SMA200 5% 이탈 + RSI < 40 + 고볼륨 (기관 분배)
            thesis_flip = bool(price < sma200 * 0.95 and rsi < 40 and vol_z > 1.5)

            result.update({
                "price":            round(price, 4),
                "sma200":           round(sma200, 4),
                "sma200_slope_up":  sma200_slope_up,
                "rsi":              round(rsi, 2),
                "kdj_j":            round(kdj_j, 2),
                "kdj_golden_cross": kdj_cross,
                "dpi_inflow":       dpi_inflow,
                "dpi_score":        dpi_score,
                "oi_down":          oi_down,
                "thesis_flip":      thesis_flip,
                "vol_zscore":       round(vol_z, 4),
                "block_ratio":      round(blk_r, 4),
                "dir_pressure":     round(dir_p, 4),
            })

        except Exception as e:
            logger.error(f"[{ticker}] DPI/기술적 지표 계산 오류: {e}")
        finally:
            gc.collect()

        return result

    # ------------------------------------------------------------------
    # 전체 유니버스 일괄 처리
    # ------------------------------------------------------------------
    def score_all(self, price_db: dict) -> dict:
        """
        Parameters
        ----------
        price_db : dict  {ticker: DataFrame(OHLCV)}

        Returns
        -------
        tech_db  : dict  {ticker: tech_data_dict}
        """
        logger.info(f"=== DarkPoolProxy: {len(price_db)}개 종목 기술 분석 시작 ===")
        tech_db = {}
        for ticker, df in price_db.items():
            tech_db[ticker] = self.score_ticker(ticker, df)
        logger.info("=== DarkPoolProxy: 기술 분석 완료 ===")
        return tech_db
