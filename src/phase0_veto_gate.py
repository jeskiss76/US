"""
OBAF — Phase 0: VETO GATE
[T(-3) DEBT]   현금/단기부채 비율 검증
[T(-3) REFORM] 중국 공급망 의존도 검증
[T(-3) SANCTION] Special Measure 6 / OFAC / CLARITY Act 2026 검증
"""

import gc
import logging

logger = logging.getLogger("Phase0_VetoGate")


class VetoGateFilter:
    """
    Phase 0: 전 종목 대상 무조건 거부(Unconditional Veto) 게이트
    원본 OBAF 프레임워크 로직 100% 보존
    """

    def __init__(self, config: dict):
        p0 = config.get("thresholds", {}).get("phase0", {})
        self.MIN_CASH_DEBT_RATIO = p0.get("min_cash_debt_ratio", 1.2)
        self.MAX_CHINA_DEP       = p0.get("max_china_dep",       0.30)

    # ------------------------------------------------------------------
    # T(-3) DEBT — 단기 유동성 생존 검증
    # ------------------------------------------------------------------
    def check_debt_survival(
        self, ticker: str, cash: float, debt_12m: float
    ) -> tuple[bool, str]:
        """
        현금(Cash) / 12개월 내 만기 부채(Debt_12M) ≥ MIN_CASH_DEBT_RATIO
        부채가 0이면 무조건 통과
        """
        if debt_12m <= 0:
            reason = f"[Phase 0: DEBT] 부채=0 → 유동성 리스크 없음"
            logger.info(f"[{ticker}] PASS: {reason}")
            return True, reason

        ratio = cash / debt_12m
        reasoning = (
            f"[Phase 0: DEBT] Cash/Debt_12M = {ratio:.2f}x "
            f"(기준: {self.MIN_CASH_DEBT_RATIO}x)"
        )

        if ratio < self.MIN_CASH_DEBT_RATIO:
            logger.warning(f"[{ticker}] UNCONDITIONAL TERMINATE: {reasoning}")
            return False, reasoning

        logger.info(f"[{ticker}] PASS: {reasoning}")
        return True, reasoning

    # ------------------------------------------------------------------
    # T(-3) REFORM & SANCTION — 지정학·규제·제재 검증
    # ------------------------------------------------------------------
    def check_geopolitical_risk(
        self, ticker: str, alt_data: dict
    ) -> tuple[bool, str]:
        """
        1. 중국 공급망 의존도 30% 초과 → INSTANT TERMINATE
        2. Special Measure 6 노출 → IMMEDIATE VETO
        3. CLARITY Act 2026 미준수 → IMMEDIATE VETO
        4. OFAC 제재 노출 → IMMEDIATE VETO
        """
        china_dep     = alt_data.get("china_dependency",             0.0)
        sm6           = alt_data.get("special_measure_6_exposure",   False)
        clarity_ok    = alt_data.get("clarity_act_2026_compliant",   True)
        ofac_exposure = alt_data.get("ofac_exposure",                False)

        # 1. 중국 공급망
        if china_dep > self.MAX_CHINA_DEP:
            reason = (
                f"[Phase 0: REFORM] 중국 공급망 의존도 "
                f"{china_dep * 100:.1f}% > {self.MAX_CHINA_DEP * 100:.0f}%"
            )
            logger.warning(f"[{ticker}] INSTANT TERMINATE: {reason}")
            return False, reason

        # 2. Special Measure 6
        if sm6:
            reason = "[Phase 0: SANCTION] Special Measure 6 노출 적발"
            logger.warning(f"[{ticker}] IMMEDIATE VETO: {reason}")
            return False, reason

        # 3. CLARITY Act 2026
        if not clarity_ok:
            reason = "[Phase 0: CLARITY_GATE] 2026 CLARITY Act 미준수 자산"
            logger.warning(f"[{ticker}] IMMEDIATE VETO: {reason}")
            return False, reason

        # 4. OFAC 제재
        if ofac_exposure:
            reason = "[Phase 0: OFAC] OFAC SDN 리스트 노출 적발"
            logger.warning(f"[{ticker}] IMMEDIATE VETO: {reason}")
            return False, reason

        return True, "[Phase 0: REFORM/SANCTION] 지정학·규제·제재 검증 통과"

    # ------------------------------------------------------------------
    # 메인 실행 엔진
    # ------------------------------------------------------------------
    def run_veto_gate(
        self, raw_database: dict, alt_data_database: dict
    ) -> dict:
        """
        Parameters
        ----------
        raw_database      : {ticker: {cash, debt_12m, ...}}
        alt_data_database : {ticker: alt_data_dict}

        Returns
        -------
        survivors : {ticker: {data, phase_0_logs}}
        """
        logger.info("=== Phase 0: VETO_GATE 스크리닝 시작 ===")
        survivors = {}

        for ticker, data in raw_database.items():
            try:
                cash     = data.get("cash",     0.0)
                debt_12m = data.get("debt_12m", 0.0)
                alt_data = alt_data_database.get(ticker, {})

                # ── 1단계: 재무 건전성 ─────────────────────────────────
                debt_pass, debt_reason = self.check_debt_survival(
                    ticker, cash, debt_12m
                )
                if not debt_pass:
                    continue

                # ── 2단계: 지정학·규제·제재 ────────────────────────────
                geo_pass, geo_reason = self.check_geopolitical_risk(
                    ticker, alt_data
                )
                if not geo_pass:
                    continue

                # 모든 게이트 통과
                survivors[ticker] = {
                    "data":         data,
                    "phase_0_logs": [debt_reason, geo_reason]
                }
                logger.info(f"[{ticker}] Phase 0 PASS ✓")

            except Exception as e:
                logger.error(f"[{ticker}] Phase 0 처리 중 예외: {str(e)}")
                continue
            finally:
                gc.collect()

        logger.info(
            f"=== Phase 0 완료 — 통과: {len(survivors)} / {len(raw_database)} ==="
        )
        return survivors
