"""
OBAF — Phase 2: Valuation & Survival Engine v2.1
[PRE_AUDIT]  Risk-On DashScore 검증
[T(-2) LIQ]  매크로 헤어컷
[T(0) SURV]  생존 지표 (S&P500/NDX100 전체 유니버스 기준 임계값 조정)

임계값 변경 사유:
  원본 일반자산 기준(ICR>5.0x, D/E<0.40)은 특수 인프라 채권 기준으로
  S&P500 전체 적용 시 전종목 탈락. GICS 섹터별 합리적 기준으로 조정.
"""

import gc
import logging

logger = logging.getLogger("Phase2_Valuation")

# GICS 섹터별 생존 임계값 (ICR, D/E)
# 섹터 특성을 반영한 차등 기준
SECTOR_SURVIVAL_GATES = {
    "Utilities":               {"min_icr": 1.5,  "max_de": 3.0},
    "Energy":                  {"min_icr": 2.0,  "max_de": 1.5},
    "Real Estate":             {"min_icr": 1.5,  "max_de": 3.0},
    "Financials":              {"min_icr": 1.0,  "max_de": 5.0},
    "Financial Services":      {"min_icr": 1.0,  "max_de": 5.0},
    "Industrials":             {"min_icr": 2.5,  "max_de": 2.0},
    "Health Care":             {"min_icr": 3.0,  "max_de": 1.5},
    "Healthcare":              {"min_icr": 3.0,  "max_de": 1.5},
    "Consumer Discretionary":  {"min_icr": 2.0,  "max_de": 2.0},
    "Consumer Cyclical":       {"min_icr": 2.0,  "max_de": 2.0},
    "Consumer Staples":        {"min_icr": 3.0,  "max_de": 1.5},
    "Consumer Defensive":      {"min_icr": 3.0,  "max_de": 1.5},
    "Information Technology":  {"min_icr": 5.0,  "max_de": 1.0},
    "Technology":              {"min_icr": 5.0,  "max_de": 1.0},
    "Communication Services":  {"min_icr": 3.0,  "max_de": 1.5},
    "Materials":               {"min_icr": 3.0,  "max_de": 1.5},
    "Basic Materials":         {"min_icr": 3.0,  "max_de": 1.5},
    "Unknown":                 {"min_icr": 2.0,  "max_de": 2.0},
}
DEFAULT_GATE = {"min_icr": 2.0, "max_de": 2.0}

# 병목 섹터 생존 기준 (원본 보존)
BOTTLENECK_SECTORS = {
    "Uranium", "Copper", "Transformers",
    "Grid Utilities", "Midstream Natural Gas", "Greenland REE"
}


class ValuationSurvivalEngine:

    def __init__(self, config: dict):
        p2 = config.get("thresholds", {}).get("phase2", {})
        self.MAX_US10Y         = p2.get("max_us10y",          4.5)
        self.OIL_WARN          = p2.get("oil_warn",           95.0)
        self.OIL_CRIT          = p2.get("oil_crit",          110.0)
        self.MAX_GEO_INDEX     = p2.get("max_geo_index",      75.0)
        self.MIN_RISK_ON_SCORE = p2.get("min_risk_on_score",  40.0)

    # ------------------------------------------------------------------
    # [T(-2) LIQ] 매크로 헤어컷
    # ------------------------------------------------------------------
    def calculate_valuation_haircut(self, macro_data: dict) -> tuple[bool, float, str]:
        us10y     = macro_data.get("US10Y",    0.0)
        oil       = macro_data.get("CrudeOil", 0.0)
        geo_index = macro_data.get("GeoIndex", 0.0)

        strict_mode        = False
        haircut_multiplier = 1.0
        parts              = []

        if us10y > self.MAX_US10Y or oil > self.OIL_WARN or geo_index > self.MAX_GEO_INDEX:
            strict_mode = True
            parts.append(f"STRICT_MODE ON (US10Y:{us10y}%, Oil:${oil}, Geo:{geo_index})")

        if oil > self.OIL_CRIT:
            haircut_multiplier = 0.5
            parts.append("Oil > $110 → 0.5x Haircut")
        elif oil > self.OIL_WARN:
            haircut_multiplier = 0.7
            parts.append("Oil $95~$110 → 0.7x Haircut")

        return strict_mode, haircut_multiplier, " | ".join(parts) or "매크로 정상"

    # ------------------------------------------------------------------
    # [T(0) SURV] 생존 지표 검증
    # ------------------------------------------------------------------
    def check_survival_metrics(
        self, ticker: str, asset_info: dict, financial_data: dict
    ) -> tuple[bool, str]:

        b_sector  = asset_info.get("bottleneck_sector", "Unknown")
        gics      = asset_info.get("gics_sector", "Unknown")

        icr      = financial_data.get("icr",            0.0)
        de_ratio = financial_data.get("debt_to_equity", 1.0)
        ocf      = financial_data.get("ocf",           -1.0)

        # ── 병목 자산 (원본 기준 유지) ────────────────────────────
        if b_sector in BOTTLENECK_SECTORS:
            if icr <= 2.5 or ocf <= 0:
                reason = (
                    f"[Phase2: SURV] 병목 자산 생존 미달 "
                    f"(ICR={icr:.2f}x, OCF={ocf:,.0f})"
                )
                return False, reason
            return True, f"[Phase2: SURV] 병목 자산 통과 (ICR={icr:.2f}x)"

        # ── 일반 자산 — GICS 섹터별 차등 기준 ───────────────────
        gate = SECTOR_SURVIVAL_GATES.get(gics) or DEFAULT_GATE
        min_icr = gate["min_icr"]
        max_de  = gate["max_de"]

        fails = []
        if icr < min_icr:
            fails.append(f"ICR {icr:.2f}x < {min_icr}x")
        if de_ratio > max_de:
            fails.append(f"D/E {de_ratio:.2f} > {max_de}")

        if fails:
            reason = (
                f"[Phase2: SURV] {gics} 생존 미달 "
                f"({' | '.join(fails)})"
            )
            logger.info(f"[{ticker}] {reason}")
            return False, reason

        return (
            True,
            f"[Phase2: SURV] {gics} 통과 "
            f"(ICR={icr:.2f}x≥{min_icr}x, D/E={de_ratio:.2f}≤{max_de})"
        )

    # ------------------------------------------------------------------
    # 메인 실행
    # ------------------------------------------------------------------
    def run_valuation_survival(
        self, alpha_targets: dict, macro_data: dict, dash_score: float
    ) -> dict:
        logger.info("=== Phase 2: 밸류에이션 및 생존 검증 시작 ===")

        if dash_score < self.MIN_RISK_ON_SCORE:
            logger.critical(
                f"PRE_AUDIT FAIL: Risk-On Score({dash_score:.1f}) < "
                f"{self.MIN_RISK_ON_SCORE} → 전체 매수 중단"
            )
            return {}

        strict_mode, haircut, macro_reason = self.calculate_valuation_haircut(macro_data)
        logger.info(f"매크로: Strict={strict_mode}, Haircut={haircut}x | {macro_reason}")

        survivors = {}
        pass_cnt = fail_cnt = 0

        for ticker, asset_info in alpha_targets.items():
            try:
                data_block   = asset_info.get("data", {})
                fundamentals = data_block.get("fundamentals", {})

                surv_pass, surv_reason = self.check_survival_metrics(
                    ticker, asset_info, fundamentals
                )

                if not surv_pass:
                    fail_cnt += 1
                    continue

                asset_info["phase_2_logs"]    = [macro_reason, surv_reason]
                asset_info["applied_haircut"] = haircut
                asset_info["strict_mode"]     = strict_mode
                survivors[ticker] = asset_info
                pass_cnt += 1
                logger.debug(f"[{ticker}] Phase 2 PASS ✓")

            except Exception as e:
                logger.error(f"[{ticker}] Phase 2 오류: {e}")
                continue
            finally:
                gc.collect()

        logger.info(
            f"=== Phase 2 완료 — 통과: {pass_cnt} / {len(alpha_targets)} "
            f"(탈락: {fail_cnt}) ==="
        )
        return survivors
