"""
OBAF — Alt Data Engine
- china_dependency : SEC 10-K 지리별 매출 자동 파싱 (EDGAR XBRL)
- OFAC SDN 자동 크롤링 (공식 consolidated.csv)
- CLARITY Act / Special Measure 6 : 정적 JSON 블랙리스트
- GICS 섹터 분류 (유니버스 메타데이터 활용)
"""

import gc
import json
import logging
import time

import requests
import yfinance as yf

logger = logging.getLogger("AltDataEngine")

OFAC_CSV_URL = (
    "https://www.treasury.gov/ofac/downloads/consolidated/consolidated.csv"
)

# 알려진 중국 매출 비중 고의존 종목 정적 보완 테이블 (SEC 파싱 실패 대비)
CHINA_DEP_STATIC: dict[str, float] = {
    "AAPL":  0.19,
    "QCOM":  0.62,
    "AVGO":  0.35,
    "AMAT":  0.28,
    "LRCX":  0.32,
    "KLAC":  0.25,
    "MU":    0.20,
    "INTC":  0.27,
    "NVDA":  0.17,
    "TXN":   0.21,
    "MRVL":  0.50,
    "ON":    0.30,
    "MPWR":  0.55,
    "SWKS":  0.64,
    "QRVO":  0.50,
    "ADI":   0.22,
    "NXPI":  0.35,
    "WOLF":  0.15,
    "STX":   0.30,
    "WDC":   0.30,
    "TSLA":  0.22,
    "GE":    0.10,
    "CAT":   0.08,
    "DE":    0.07,
    "NKE":   0.16,
    "SBUX":  0.15,
    "PG":    0.08,
    "KO":    0.05,
    "PEP":   0.04,
}


class AltDataEngine:
    """
    Phase 0/1에 필요한 대체 데이터(Alt Data) 일괄 구축
    """

    def __init__(self, blacklist_path: str, sec_user_agent: str):
        self.sec_headers = {"User-Agent": sec_user_agent}
        self.blacklist = self._load_blacklist(blacklist_path)
        self._ofac_names: set[str] = set()

    # ------------------------------------------------------------------
    # 초기화 헬퍼
    # ------------------------------------------------------------------
    def _load_blacklist(self, path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"blacklist.json 로드 실패: {e}")
            return {"special_measure_6": [], "clarity_act_noncompliant": [], "manual_veto": []}

    # ------------------------------------------------------------------
    # OFAC SDN 크롤링
    # ------------------------------------------------------------------
    def load_ofac_list(self):
        """OFAC consolidated.csv 다운로드 → 제재 대상 이름 집합 구축"""
        try:
            resp = requests.get(OFAC_CSV_URL, timeout=30)
            resp.raise_for_status()
            names = set()
            for line in resp.text.splitlines():
                parts = line.split(",")
                if len(parts) > 1:
                    names.add(parts[1].strip().upper())
            self._ofac_names = names
            logger.info(f"OFAC SDN 리스트 로드 완료: {len(names)}개 엔티티")
        except Exception as e:
            logger.warning(f"OFAC 다운로드 실패 (블랙리스트 정적 방어로 대체): {e}")

    # ------------------------------------------------------------------
    # china_dependency — SEC XBRL 지리 매출 파싱
    # ------------------------------------------------------------------
    def _get_most_recent_sec_value(
        self, facts: dict, concept: str, forms=("10-Q", "10-K")
    ) -> float | None:
        """SEC EDGAR XBRL facts JSON에서 지정 개념의 최신 값 추출"""
        try:
            units = (
                facts.get("facts", {})
                    .get("us-gaap", {})
                    .get(concept, {})
                    .get("units", {})
            )
            usd_vals = units.get("USD", [])
            filtered = [
                v for v in usd_vals
                if v.get("form") in forms and "end" in v
            ]
            if not filtered:
                return None
            filtered.sort(key=lambda x: x["end"], reverse=True)
            return float(filtered[0]["val"])
        except Exception:
            return None

    def _parse_china_dep_from_sec(self, sec_facts: dict) -> float | None:
        """
        SEC XBRL EntityReportingCurrencyISOCode 기반 지역별 매출 파싱
        다수 기업이 custom segment tag를 사용하므로 파싱 성공률은 ~40%
        실패 시 정적 테이블 또는 기본값으로 폴백
        """
        if not sec_facts:
            return None
        try:
            # 지역별 매출 커스텀 태그 후보 (광범위 탐색)
            segment_facts = sec_facts.get("facts", {})
            china_rev, total_rev = None, None

            for namespace in ("us-gaap", "dei"):
                ns_facts = segment_facts.get(namespace, {})
                for key, val_obj in ns_facts.items():
                    key_lower = key.lower()
                    if "china" in key_lower or "prc" in key_lower:
                        usd = val_obj.get("units", {}).get("USD", [])
                        if usd:
                            usd.sort(key=lambda x: x.get("end", ""), reverse=True)
                            china_rev = float(usd[0]["val"])
                    if key in ("RevenueFromContractWithCustomerExcludingAssessedTax",
                               "Revenues", "SalesRevenueNet"):
                        v = self._get_most_recent_sec_value(sec_facts, key)
                        if v and (total_rev is None or v > total_rev):
                            total_rev = v

            if china_rev and total_rev and total_rev > 0:
                ratio = round(china_rev / total_rev, 4)
                logger.debug(f"SEC 파싱 china_dep: {ratio:.2%}")
                return ratio
        except Exception as e:
            logger.debug(f"SEC china_dep 파싱 예외: {e}")
        return None

    def get_china_dep(self, ticker: str, sec_facts: dict | None) -> float:
        """
        china_dependency 결정 우선순위:
        1. SEC XBRL 파싱 성공
        2. 정적 고의존 테이블
        3. yfinance country 힌트 (미국 외 본사 → 0.05)
        4. 기본값 0.10
        """
        # 1. SEC 파싱
        if sec_facts:
            val = self._parse_china_dep_from_sec(sec_facts)
            if val is not None:
                return val

        # 2. 정적 테이블
        if ticker in CHINA_DEP_STATIC:
            return CHINA_DEP_STATIC[ticker]

        # 3. yfinance country
        try:
            info = yf.Ticker(ticker).info
            country = info.get("country", "United States")
            if country not in ("United States", "USA", "US"):
                return 0.05
        except Exception:
            pass

        return 0.10  # 기본 보수적 추정

    # ------------------------------------------------------------------
    # Phase 0 규제·제재 검증 데이터
    # ------------------------------------------------------------------
    def check_sanctions(self, ticker: str, company_name: str) -> tuple[bool, bool, bool]:
        """
        Returns
        -------
        sm6_exposure : bool
        clarity_noncompliant : bool
        ofac_hit : bool
        """
        bl = self.blacklist
        sm6      = ticker in bl.get("special_measure_6", [])
        clarity  = ticker in bl.get("clarity_act_noncompliant", [])
        manual   = ticker in bl.get("manual_veto", [])
        ofac_hit = company_name.upper() in self._ofac_names or manual
        return sm6, (not clarity), ofac_hit

    # ------------------------------------------------------------------
    # 메인 빌더 — 전체 유니버스 대체 데이터 구축
    # ------------------------------------------------------------------
    def build_alt_database(
        self,
        target_assets: list[dict],
        sec_db: dict
    ) -> dict:
        """
        Parameters
        ----------
        target_assets : list[dict]   UniverseManager에서 반환된 유니버스
        sec_db        : dict          {ticker: raw_sec_facts_dict}

        Returns
        -------
        alt_db : dict  {ticker: alt_data_dict}
        """
        logger.info("=== AltDataEngine: 대체 데이터 빌드 시작 ===")
        alt_db = {}

        for asset in target_assets:
            ticker       = asset.get("ticker", "")
            company      = asset.get("company", "")
            gics_sector  = asset.get("gics_sector", "Unknown")

            try:
                sec_facts = sec_db.get(ticker)

                # china_dep
                china_dep = self.get_china_dep(ticker, sec_facts)

                # 제재·규제
                sm6, clarity_ok, ofac_hit = self.check_sanctions(ticker, company)

                alt_db[ticker] = {
                    # 섹터 정보
                    "gics_sector":      gics_sector,
                    "bottleneck_sector": "Unknown",   # Phase 1에서 검증됨

                    # Phase 0 게이트
                    "china_dependency":             china_dep,
                    "special_measure_6_exposure":   sm6,
                    "clarity_act_2026_compliant":   clarity_ok,
                    "ofac_exposure":                ofac_hit,

                    # Phase 1 그리드 게이트 (에너지/유틸리티 종목만 의미 있음 — 기본값 False)
                    "interconnection_queue_approved":  False,
                    "power_capacity_gw":               0.0,
                    "has_hyperscaler_ppa_or_equity":   False,
                    "has_smr_integration":             False,
                    "has_microgrid":                   False,
                    "dod_doe_backed":                  False,
                }

            except Exception as e:
                logger.error(f"[{ticker}] AltData 구축 실패: {e}")
                alt_db[ticker] = {
                    "gics_sector": "Unknown",
                    "bottleneck_sector": "Unknown",
                    "china_dependency": 0.10,
                    "special_measure_6_exposure": False,
                    "clarity_act_2026_compliant": True,
                    "ofac_exposure": False,
                    "interconnection_queue_approved": False,
                    "power_capacity_gw": 0.0,
                    "has_hyperscaler_ppa_or_equity": False,
                    "has_smr_integration": False,
                    "has_microgrid": False,
                    "dod_doe_backed": False,
                }
            finally:
                gc.collect()

        logger.info(f"=== AltDataEngine 완료: {len(alt_db)}개 종목 처리 ===")
        return alt_db
