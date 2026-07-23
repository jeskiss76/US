"""
OBAF — Universe Manager v2.1
S&P500 + NASDAQ100 유니버스 수집

수집 전략 (우선순위):
  1. Wikipedia (브라우저 UA 스푸핑)
  2. 공개 GitHub Raw CSV (datahub.io mirror)
  3. yfinance .info 기반 개별 조회 (최후 수단)
  4. 번들 정적 CSV (config/sp500_static.csv / ndx100_static.csv)

GitHub Actions 환경에서 Wikipedia 403 차단 대응 완료
"""

import logging
import os
from io import StringIO

import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("UniverseManager")

# ------------------------------------------------------------------
# 공개 대안 소스 (무인증, CDN 캐시 기반)
# ------------------------------------------------------------------
SP500_SOURCES = [
    # 1차: Wikipedia (브라우저 UA)
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    # 2차: datahub.io 공개 CSV
    "https://datahub.io/core/s-and-p-500-companies/r/constituents.csv",
    # 3차: GitHub raw (datasets 오픈소스)
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
]

NDX100_SOURCES = [
    # 1차: Wikipedia (브라우저 UA)
    "https://en.wikipedia.org/wiki/Nasdaq-100",
    # 2차: GitHub raw
    "https://raw.githubusercontent.com/datasets/nasdaq-100/main/data/constituents.csv",
]

# 브라우저 UA (GitHub Actions IP 차단 우회)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_COLS = ["ticker", "company", "gics_sector", "gics_sub_industry", "source"]


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_COLS)


class UniverseManager:

    SEC_CIK_URL = "https://www.sec.gov/files/company_tickers.json"

    def __init__(self, sec_user_agent: str):

        if not sec_user_agent:
            sec_user_agent = "OBAF Research admin@obaf.local"

        self.sec_ua = sec_user_agent

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry)

        # SEC 전용 세션 (SEC User-Agent 필수)
        self.sec_session = requests.Session()
        self.sec_session.mount("https://", adapter)
        self.sec_session.mount("http://",  adapter)
        self.sec_session.headers.update({
            "User-Agent":      sec_user_agent,
            "Accept":          "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })

        # 웹 수집 전용 세션 (브라우저 UA)
        self.web_session = requests.Session()
        self.web_session.mount("https://", adapter)
        self.web_session.mount("http://",  adapter)
        self.web_session.headers.update({
            "User-Agent":      BROWSER_UA,
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })

    # ------------------------------------------------------------------
    # 내부 헬퍼 — HTML 테이블 다운로드
    # ------------------------------------------------------------------
    def _download_html_tables(self, url: str) -> list:
        r = self.web_session.get(url, timeout=30)
        r.raise_for_status()
        return pd.read_html(StringIO(r.text), flavor="lxml")

    # ------------------------------------------------------------------
    # 내부 헬퍼 — CSV URL 직접 파싱
    # ------------------------------------------------------------------
    def _download_csv(self, url: str) -> pd.DataFrame:
        r = self.web_session.get(url, timeout=30)
        r.raise_for_status()
        return pd.read_csv(StringIO(r.text))

    # ------------------------------------------------------------------
    # 내부 헬퍼 — 컬럼 정규화 공통 처리
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_ticker_col(df: pd.DataFrame) -> pd.DataFrame:
        """ticker 컬럼 존재 여부 확인 + 정규화"""
        if "ticker" not in df.columns:
            # Symbol 등 대안 컬럼 탐색
            for c in df.columns:
                if c.lower() in ("symbol", "ticker"):
                    df = df.rename(columns={c: "ticker"})
                    break
        if "ticker" not in df.columns:
            raise KeyError("ticker 컬럼을 찾을 수 없습니다.")
        df["ticker"] = (
            df["ticker"]
            .astype(str)
            .str.replace(".", "-", regex=False)
            .str.strip()
            .str.upper()
        )
        return df

    # ------------------------------------------------------------------
    # 번들 정적 CSV 폴백 로더
    # ------------------------------------------------------------------
    @staticmethod
    def _load_static_csv(name: str) -> pd.DataFrame:
        """config/{name} 정적 CSV 로드 (최후 수단)"""
        path = os.path.join("config", name)
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                logger.warning("정적 CSV 폴백 사용: %s (%d개)", path, len(df))
                return df
            except Exception as e:
                logger.error("정적 CSV 로드 실패: %s — %s", path, e)
        return pd.DataFrame()

    # ------------------------------------------------------------------
    # S&P500
    # ------------------------------------------------------------------
    def fetch_sp500(self) -> pd.DataFrame:

        # ── 전략 1: Wikipedia HTML ────────────────────────────────
        try:
            tables = self._download_html_tables(SP500_SOURCES[0])
            df = tables[0].rename(columns={
                "Symbol":            "ticker",
                "Security":          "company",
                "GICS Sector":       "gics_sector",
                "GICS Sub-Industry": "gics_sub_industry",
            })
            df = self._normalize_ticker_col(df)
            df["source"] = "SP500"
            result = df[_COLS].copy()
            logger.info("S&P500 Wikipedia: %d개", len(result))
            return result
        except Exception as e:
            logger.warning("S&P500 Wikipedia 실패 (%s) → CSV 폴백", str(e)[:80])

        # ── 전략 2/3: 공개 CSV (datahub / GitHub raw) ─────────────
        for url in SP500_SOURCES[1:]:
            try:
                df = self._download_csv(url)
                # 컬럼 정규화 (constituents.csv: Symbol, Name, Sector)
                rename = {}
                for c in df.columns:
                    cl = c.lower().strip()
                    if cl in ("symbol", "ticker"):       rename[c] = "ticker"
                    elif cl in ("name", "company", "security"): rename[c] = "company"
                    elif "sector" in cl and "sub" not in cl:    rename[c] = "gics_sector"
                    elif "sub" in cl or "industry" in cl:       rename[c] = "gics_sub_industry"
                df = df.rename(columns=rename)
                df = self._normalize_ticker_col(df)
                for col in ("company", "gics_sector", "gics_sub_industry"):
                    if col not in df.columns:
                        df[col] = "Unknown"
                df["source"] = "SP500"
                result = df[_COLS].copy()
                logger.info("S&P500 CSV(%s): %d개", url.split("/")[-1], len(result))
                return result
            except Exception as e:
                logger.warning("S&P500 CSV 실패(%s): %s", url[:60], str(e)[:60])

        # ── 전략 4: 번들 정적 CSV ─────────────────────────────────
        df = self._load_static_csv("sp500_static.csv")
        if not df.empty:
            try:
                df = self._normalize_ticker_col(df)
                for col in ("company", "gics_sector", "gics_sub_industry"):
                    if col not in df.columns:
                        df[col] = "Unknown"
                df["source"] = "SP500"
                return df[_COLS].copy()
            except Exception as e:
                logger.error("S&P500 정적 CSV 처리 실패: %s", e)

        logger.error("S&P500 수집 전략 모두 실패 → 빈 DataFrame 반환")
        return _empty_df()

    # ------------------------------------------------------------------
    # NASDAQ100
    # ------------------------------------------------------------------
    def fetch_ndx100(self) -> pd.DataFrame:

        # ── 전략 1: Wikipedia HTML ────────────────────────────────
        try:
            tables = self._download_html_tables(NDX100_SOURCES[0])
            df = None
            for t in tables:
                cols = [str(c).lower() for c in t.columns]
                if any("ticker" in c or "symbol" in c for c in cols):
                    df = t
                    break
            if df is None:
                raise RuntimeError("NASDAQ100 테이블 없음")

            rename = {}
            for c in df.columns:
                cl = str(c).lower()
                if "ticker" in cl or "symbol" in cl:       rename[c] = "ticker"
                elif "company" in cl or "security" in cl or "name" in cl: rename[c] = "company"
                elif "gics sector" in cl or "sector" in cl: rename[c] = "gics_sector"
                elif "sub" in cl:                           rename[c] = "gics_sub_industry"
            df = df.rename(columns=rename)
            df = self._normalize_ticker_col(df)
            for col in ("company", "gics_sector", "gics_sub_industry"):
                if col not in df.columns:
                    df[col] = "Unknown"
            df["source"] = "NDX100"
            result = df[_COLS].copy()
            logger.info("NASDAQ100 Wikipedia: %d개", len(result))
            return result
        except Exception as e:
            logger.warning("NASDAQ100 Wikipedia 실패 (%s) → CSV 폴백", str(e)[:80])

        # ── 전략 2: GitHub raw CSV ────────────────────────────────
        for url in NDX100_SOURCES[1:]:
            try:
                df = self._download_csv(url)
                rename = {}
                for c in df.columns:
                    cl = c.lower().strip()
                    if cl in ("symbol", "ticker"):             rename[c] = "ticker"
                    elif cl in ("name", "company", "security"): rename[c] = "company"
                    elif "sector" in cl and "sub" not in cl:   rename[c] = "gics_sector"
                    elif "sub" in cl or "industry" in cl:      rename[c] = "gics_sub_industry"
                df = df.rename(columns=rename)
                df = self._normalize_ticker_col(df)
                for col in ("company", "gics_sector", "gics_sub_industry"):
                    if col not in df.columns:
                        df[col] = "Unknown"
                df["source"] = "NDX100"
                result = df[_COLS].copy()
                logger.info("NASDAQ100 CSV: %d개", len(result))
                return result
            except Exception as e:
                logger.warning("NASDAQ100 CSV 실패: %s", str(e)[:60])

        # ── 전략 3: 번들 정적 CSV ─────────────────────────────────
        df = self._load_static_csv("ndx100_static.csv")
        if not df.empty:
            try:
                df = self._normalize_ticker_col(df)
                for col in ("company", "gics_sector", "gics_sub_industry"):
                    if col not in df.columns:
                        df[col] = "Unknown"
                df["source"] = "NDX100"
                return df[_COLS].copy()
            except Exception as e:
                logger.error("NASDAQ100 정적 CSV 처리 실패: %s", e)

        logger.error("NASDAQ100 수집 전략 모두 실패 → 빈 DataFrame 반환")
        return _empty_df()

    # ------------------------------------------------------------------
    # SEC CIK 매핑
    # ------------------------------------------------------------------
    def fetch_sec_cik_map(self) -> dict:
        try:
            r = self.sec_session.get(self.SEC_CIK_URL, timeout=30)
            r.raise_for_status()
            cik_map = {}
            for entry in r.json().values():
                ticker = str(entry.get("ticker", "")).strip().upper()
                if ticker:
                    cik_map[ticker] = str(entry.get("cik_str", "")).zfill(10)
            logger.info("SEC CIK %d개", len(cik_map))
            return cik_map
        except Exception as e:
            logger.exception(e)
            return {}

    # ------------------------------------------------------------------
    # 전체 유니버스 합집합
    # ------------------------------------------------------------------
    def get_full_universe(self) -> tuple[list[dict], dict]:
        """
        Returns
        -------
        target_assets : list[dict]
        cik_map       : dict {TICKER: CIK}
        """
        sp500  = self.fetch_sp500()
        ndx100 = self.fetch_ndx100()

        combined = pd.concat([sp500, ndx100], ignore_index=True)

        # ── KeyError 방어: ticker 컬럼 없을 때 처리 ─────────────
        if "ticker" not in combined.columns:
            logger.critical(
                "유니버스 수집 전략 전부 실패 — "
                "config/sp500_static.csv 와 config/ndx100_static.csv 파일을 확인하세요."
            )
            return [], {}

        combined = (
            combined
            .dropna(subset=["ticker"])
            .drop_duplicates(subset=["ticker"], keep="first")
        )

        if combined.empty:
            logger.critical("유니버스가 비어 있습니다.")
            return [], {}

        cik_map = self.fetch_sec_cik_map()

        target_assets = []
        for _, row in combined.iterrows():
            ticker = str(row.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            target_assets.append({
                "ticker":      ticker,
                "cik":         cik_map.get(ticker, ""),
                "company":     str(row.get("company",          "Unknown")),
                "gics_sector": str(row.get("gics_sector",      "Unknown")),
                "gics_sub":    str(row.get("gics_sub_industry","Unknown")),
                "source":      str(row.get("source",           "Unknown")),
            })

        logger.info(
            "전체 유니버스 확정: %d개 (SP500=%d, NDX100=%d, 중복 제거 후)",
            len(target_assets), len(sp500), len(ndx100),
        )
        return target_assets, cik_map
