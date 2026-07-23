"""
OBAF — Phase 1: B_NECK_STRAT Alpha Engine
[L(1)  PHYS & L(11) ARCTIC] 원본 병목 섹터 로직 완전 보존 + 1.5x 프리미엄
[L(2)  GRID & NEDC]          전력망 초정밀 게이트 (원본 보존)
[L(EX) GICS_EXPAND]          S&P500 + NASDAQ100 전체 GICS 섹터 품질 게이트 (신규)
"""

import gc
import logging
import yaml

logger = logging.getLogger("Phase1_BNeckStrat")


class BNeckStratEngine:
    """
    Phase 1: 섹터·품질 알파 엔진

    ① 원본 병목 섹터(Uranium/Copper/Grid Utilities 등) → 1.5x 프리미엄, 기존 게이트 적용
    ② GICS 11개 섹터 전체 → 섹터별 품질 기준(margin/revenue growth) 게이트 적용
    두 경로 모두 통과 기준은 독립적으로 설정 (프레임워크 무결성 보장)
    """

    def __init__(self, sector_config_path: str):
        with open(sector_config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self.BOTTLENECK_SECTORS    = cfg.get("bottleneck_premium_sectors", [])
        self.BOTTLENECK_MULTIPLIER = cfg.get("bottleneck_premium_multiplier", 1.5)
        self.GICS_WEIGHTS          = cfg.get("gics_sector_weights", {})
        self.GICS_QUALITY_GATES    = cfg.get("gics_quality_gates", {})

    # ------------------------------------------------------------------
    # [원본] L(1) PHYS & L(11) ARCTIC — 병목 섹터 검증
    # ------------------------------------------------------------------
    def check_l1_sector_match(
        self, ticker: str, alt_data: dict
    ) -> tuple[bool, str, float]:
        """
        원본 병목 섹터 검증
        Returns (pass, reason, premium_multiplier)
        """
        sector       = alt_data.get("bottleneck_sector", "Unknown")
        is_dod_backed = alt_data.get("dod_doe_backed", False)

        if sector not in self.BOTTLENECK_SECTORS:
            return False, f"[L1 PHYS] 병목 섹터 미해당 ({sector})", 1.0

        # 그린란드 REE — DoD/DoE 백업 필수
        if sector == "Greenland REE" and not is_dod_backed:
            reason = "[L11 ARCTIC] 그린란드 REE 타겟이나 DoD/DoE 백업 없음"
            logger.warning(f"[{ticker}] {reason}")
            return False, reason, 1.0

        return (
            True,
            f"[Phase 1: L1/L11] {sector} 병목 섹터 확인 (+{self.BOTTLENECK_MULTIPLIER}x 프리미엄)",
            self.BOTTLENECK_MULTIPLIER
        )

    # ------------------------------------------------------------------
    # [원본] L(2) GRID & NEDC — 전력망 초정밀 규격 게이트
    # ------------------------------------------------------------------
    def check_l2_grid_nedc(
        self, ticker: str, alt_data: dict
    ) -> tuple[bool, str]:
        """
        전력망/에너지 섹터 초정밀 검증 (원본 로직 100% 보존)
        """
        sector = alt_data.get("bottleneck_sector", "Unknown")

        if sector not in ("Grid Utilities", "Uranium", "Midstream Natural Gas"):
            return True, "[Phase 1: L2 GRID] 해당 없음 (비에너지 섹터)"

        # REQ_01: 상호연결 큐 승인 여부
        queue_approved = alt_data.get("interconnection_queue_approved", False)
        if not queue_approved:
            reason = "[Phase 1: L2 REQ_01] 상호연결 큐(Interconnection Queue) 미승인 → INSTANT VETO"
            logger.warning(f"[{ticker}] {reason}")
            return False, reason

        # REQ_02: 1GW 이상 자산 하드스탑
        power_capacity_gw = alt_data.get("power_capacity_gw", 0.0)
        if power_capacity_gw >= 1.0:
            has_ppa    = alt_data.get("has_hyperscaler_ppa_or_equity", False)
            has_smr    = alt_data.get("has_smr_integration",           False)
            has_micro  = alt_data.get("has_microgrid",                 False)
            if not (has_ppa or has_smr or has_micro):
                reason = (
                    "[Phase 1: L2 REQ_02] 1GW+ 타겟이나 "
                    "Hyperscaler PPA / SMR / 마이크로그리드 부재 → INSTANT VETO"
                )
                logger.warning(f"[{ticker}] {reason}")
                return False, reason

        return True, "[Phase 1: L2 GRID] 전력망 인프라 규격 통과"

    # ------------------------------------------------------------------
    # [신규] L(EX) GICS_EXPAND — GICS 전체 유니버스 품질 게이트
    # ------------------------------------------------------------------
    def check_gics_quality(
        self, ticker: str, fundamentals: dict, gics_sector: str
    ) -> tuple[bool, str, float]:
        """
        GICS 섹터별 최소 품질 기준 검증
        Returns (pass, reason, sector_weight)
        """
        gates  = self.GICS_QUALITY_GATES.get(gics_sector) or \
                 self.GICS_QUALITY_GATES.get("Unknown", {})
        weight = self.GICS_WEIGHTS.get(gics_sector) or \
                 self.GICS_WEIGHTS.get("Unknown", 0.8)

        min_gm  = gates.get("min_gross_margin",   0.15)
        min_rg  = gates.get("min_revenue_growth", -0.20)

        gm  = fundamentals.get("gross_margin",    0.0)
        rg  = fundamentals.get("revenue_growth",  0.0)

        if gm < min_gm:
            reason = (
                f"[Phase 1: GICS_QG] {gics_sector} 매출총이익률 "
                f"{gm:.1%} < 기준 {min_gm:.1%} → VETO"
            )
            logger.info(f"[{ticker}] {reason}")
            return False, reason, weight

        if rg < min_rg:
            reason = (
                f"[Phase 1: GICS_QG] {gics_sector} 매출성장률 "
                f"{rg:.1%} < 기준 {min_rg:.1%} → VETO"
            )
            logger.info(f"[{ticker}] {reason}")
            return False, reason, weight

        reason = (
            f"[Phase 1: GICS_QG] {gics_sector} 품질 게이트 통과 "
            f"(GM={gm:.1%}, RevG={rg:.1%}, 가중치={weight}x)"
        )
        return True, reason, weight

    # ------------------------------------------------------------------
    # 메인 실행 엔진
    # ------------------------------------------------------------------
    def run_alpha_selection(
        self,
        survivors_from_phase0: dict,
        alt_data_database: dict
    ) -> dict:
        """
        Parameters
        ----------
        survivors_from_phase0 : Phase 0 통과 종목 dict
        alt_data_database     : {ticker: alt_data_dict}

        Returns
        -------
        alpha_targets : {ticker: enriched_asset_info}
        """
        logger.info("=== Phase 1: B_NECK_STRAT 알파 엔진 스크리닝 시작 ===")
        alpha_targets = {}

        for ticker, asset_info in survivors_from_phase0.items():
            try:
                alt_data     = alt_data_database.get(ticker, {})
                gics_sector  = alt_data.get("gics_sector",    "Unknown")
                b_sector     = alt_data.get("bottleneck_sector", "Unknown")
                fundamentals = asset_info.get("data", {}).get("fundamentals", {})

                phase1_logs = []
                final_weight = 1.0
                passed = False

                # ── 경로 A: 원본 병목 섹터 로직 ─────────────────────
                if b_sector in self.BOTTLENECK_SECTORS:
                    l1_pass, l1_reason, l1_mult = self.check_l1_sector_match(
                        ticker, alt_data
                    )
                    phase1_logs.append(l1_reason)

                    if l1_pass:
                        l2_pass, l2_reason = self.check_l2_grid_nedc(
                            ticker, alt_data
                        )
                        phase1_logs.append(l2_reason)

                        if l2_pass:
                            final_weight = l1_mult
                            passed = True
                            logger.info(
                                f"[{ticker}] Phase 1 BOTTLENECK PASS ✓ "
                                f"(프리미엄 {l1_mult}x)"
                            )

                # ── 경로 B: GICS 전체 유니버스 품질 게이트 ───────────
                if not passed:
                    gics_pass, gics_reason, gics_w = self.check_gics_quality(
                        ticker, fundamentals, gics_sector
                    )
                    phase1_logs.append(gics_reason)

                    if gics_pass:
                        final_weight = gics_w
                        passed = True
                        logger.info(
                            f"[{ticker}] Phase 1 GICS PASS ✓ "
                            f"(섹터 가중치 {gics_w}x)"
                        )

                if not passed:
                    logger.info(f"[{ticker}] Phase 1 VETO — 모든 경로 탈락")
                    continue

                # 통과 정보 기록
                asset_info["phase_1_logs"]      = phase1_logs
                asset_info["sector_weight"]      = final_weight
                asset_info["gics_sector"]        = gics_sector
                asset_info["bottleneck_sector"]  = b_sector
                alpha_targets[ticker] = asset_info

            except Exception as e:
                logger.error(f"[{ticker}] Phase 1 처리 중 예외: {str(e)}")
                continue
            finally:
                gc.collect()

        logger.info(
            f"=== Phase 1 완료 — 알파 타겟: "
            f"{len(alpha_targets)} / {len(survivors_from_phase0)} ==="
        )
        return alpha_targets
