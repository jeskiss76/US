"""
OBAF — Phase 3: Technical Execution & Anti-FOMO Engine
[THESIS_FLIP]     내러티브 전환 감지 → 100% 청산
[WHALE_EXIT]      기관 이탈 감지 → 50% IED 청산
[ANTI-FOMO VETO]  고점 추격 차단 (Price<SMA200 OR RSI>70 OR DIV>0.4)
[CORE ENTRY]      최적 진입 타점 (SMA200↑ + KDJ골든크로스 + DPI유입 + RSI[40,50])
[WAIT/HOLD]       타점 미달 관망
"""

import gc
import logging

logger = logging.getLogger("Phase3_TechExecution")

# 매매 액션 상수
ACTION_BUY_TARGET = "BUY_TARGET"
ACTION_SELL_100   = "SELL_100"
ACTION_SELL_50    = "SELL_50"
ACTION_NO_ENTRY   = "NO_ENTRY"
ACTION_HOLD       = "HOLD"


class TechExecutionEngine:
    """
    Phase 3: 기술적 진입·청산 판단 엔진 (원본 OBAF 로직 100% 보존)
    DPI 프록시 결과(DarkPoolProxyEngine 출력)를 입력으로 사용
    """

    def __init__(self, config: dict):
        p3 = config.get("thresholds", {}).get("phase3", {})
        self.ANTI_FOMO_MAX_RSI       = p3.get("anti_fomo_max_rsi",        70)
        self.ANTI_FOMO_MAX_DIV       = p3.get("anti_fomo_max_sma200_div", 0.40)
        self.CORE_ENTRY_MIN_RSI      = p3.get("core_entry_min_rsi",       40)
        self.CORE_ENTRY_MAX_RSI      = p3.get("core_entry_max_rsi",       50)
        self.CORE_ENTRY_MAX_KDJ_J    = p3.get("core_entry_max_kdj_j",     20)

    # ------------------------------------------------------------------
    # 단일 종목 판단 로직
    # ------------------------------------------------------------------
    def _evaluate_ticker(
        self, ticker: str, asset_info: dict, tech_data: dict
    ) -> dict:
        """
        tech_data 키 목록 (DarkPoolProxyEngine.score_ticker 출력과 동일):
          price, sma200, sma200_slope_up, rsi, kdj_j, kdj_golden_cross,
          dpi_inflow, dpi_score, oi_down, thesis_flip,
          vol_zscore, block_ratio, dir_pressure
        """
        td = tech_data.get(ticker, {})

        price         = td.get("price",            0.0)
        sma200        = td.get("sma200",            1.0) or 1.0
        sma200_up     = td.get("sma200_slope_up",  False)
        rsi           = td.get("rsi",              50.0)
        kdj_j         = td.get("kdj_j",            50.0)
        kdj_cross     = td.get("kdj_golden_cross", False)
        dpi_inflow    = td.get("dpi_inflow",       False)
        dpi_score     = td.get("dpi_score",         0.0)
        oi_down       = td.get("oi_down",          False)
        thesis_flip   = td.get("thesis_flip",      False)
        vol_z         = td.get("vol_zscore",        0.0)
        block_r       = td.get("block_ratio",       0.0)
        dir_p         = td.get("dir_pressure",      0.0)

        # SMA200 이격률
        div_sma200 = (price - sma200) / sma200 if sma200 else 0.0

        # 섹터 가중치 (Phase 1 부여)
        sector_weight  = asset_info.get("sector_weight",   1.0)
        haircut        = asset_info.get("applied_haircut", 1.0)
        gics_sector    = asset_info.get("gics_sector", "Unknown")
        b_sector       = asset_info.get("bottleneck_sector", "Unknown")
        company        = asset_info.get("data", {}).get("company", ticker)

        # ── 1. 청산 로직 (최우선순위) ─────────────────────────────
        if thesis_flip:
            reason = (
                f"[THESIS_FLIP] Narrative Shift 감지 "
                f"(Price<SMA200×0.95 + RSI<40 + VolZ>1.5) → 100% LIQUIDATION"
            )
            logger.critical(f"[{ticker}] {reason}")
            return self._build_order(
                ticker, company, gics_sector, b_sector,
                ACTION_SELL_100, reason, price, sma200, rsi,
                kdj_j, dpi_score, div_sma200, sector_weight, haircut
            )

        if price > sma200 and (not dpi_inflow or oi_down):
            reason = (
                f"[WHALE_EXIT] 가격 SMA200 상방이나 "
                f"DPI 미유입({not dpi_inflow}) or OI 감소({oi_down}) "
                f"→ IED_50_EXIT 실행"
            )
            logger.warning(f"[{ticker}] {reason}")
            return self._build_order(
                ticker, company, gics_sector, b_sector,
                ACTION_SELL_50, reason, price, sma200, rsi,
                kdj_j, dpi_score, div_sma200, sector_weight, haircut
            )

        # ── 2. 안티-포모 차단 (진입 전 검열) ─────────────────────
        fomo_triggers = []
        if price < sma200:
            fomo_triggers.append(f"Price({price:.2f}) < SMA200({sma200:.2f})")
        if rsi > self.ANTI_FOMO_MAX_RSI:
            fomo_triggers.append(f"RSI({rsi:.1f}) > {self.ANTI_FOMO_MAX_RSI}")
        if div_sma200 > self.ANTI_FOMO_MAX_DIV:
            fomo_triggers.append(f"SMA200 이격({div_sma200:.1%}) > {self.ANTI_FOMO_MAX_DIV:.0%}")

        if fomo_triggers:
            reason = f"[ANTI-FOMO VETO] {' | '.join(fomo_triggers)}"
            logger.info(f"[{ticker}] {reason}")
            return self._build_order(
                ticker, company, gics_sector, b_sector,
                ACTION_NO_ENTRY, reason, price, sma200, rsi,
                kdj_j, dpi_score, div_sma200, sector_weight, haircut
            )

        # ── 3. 코어 진입 로직 ─────────────────────────────────────
        entry_conditions = (
            sma200_up and
            (price > sma200) and
            kdj_cross and
            (kdj_j < self.CORE_ENTRY_MAX_KDJ_J) and
            dpi_inflow and
            (self.CORE_ENTRY_MIN_RSI <= rsi <= self.CORE_ENTRY_MAX_RSI)
        )

        if entry_conditions:
            reason = (
                f"[CORE ENTRY] SMA200↑ + KDJ골든크로스(J={kdj_j:.1f}<{self.CORE_ENTRY_MAX_KDJ_J}) "
                f"+ DPI유입(score={dpi_score:.3f}) "
                f"+ RSI[{self.CORE_ENTRY_MIN_RSI},{self.CORE_ENTRY_MAX_RSI}]({rsi:.1f})"
            )
            logger.info(f"[{ticker}] 🚀 {reason} → EXECUTE BUY")
            return self._build_order(
                ticker, company, gics_sector, b_sector,
                ACTION_BUY_TARGET, reason, price, sma200, rsi,
                kdj_j, dpi_score, div_sma200, sector_weight, haircut
            )

        # ── 4. 관망 ───────────────────────────────────────────────
        cond_summary = (
            f"SMA200↑:{sma200_up} | KDJ크로스:{kdj_cross} | "
            f"J<{self.CORE_ENTRY_MAX_KDJ_J}:{kdj_j < self.CORE_ENTRY_MAX_KDJ_J} | "
            f"DPI:{dpi_inflow} | RSI[40,50]:{self.CORE_ENTRY_MIN_RSI <= rsi <= self.CORE_ENTRY_MAX_RSI}"
        )
        reason = f"[WAIT] Phase 3 타점 미달 — {cond_summary}"
        logger.info(f"[{ticker}] {reason}")
        return self._build_order(
            ticker, company, gics_sector, b_sector,
            ACTION_HOLD, reason, price, sma200, rsi,
            kdj_j, dpi_score, div_sma200, sector_weight, haircut
        )

    # ------------------------------------------------------------------
    # 주문 딕셔너리 빌더
    # ------------------------------------------------------------------
    def _build_order(
        self, ticker: str, company: str,
        gics_sector: str, bottleneck_sector: str,
        action: str, reasoning: str,
        price: float, sma200: float, rsi: float,
        kdj_j: float, dpi_score: float, div_sma200: float,
        sector_weight: float, haircut: float
    ) -> dict:
        return {
            "ticker":             ticker,
            "company":            company,
            "gics_sector":        gics_sector,
            "bottleneck_sector":  bottleneck_sector,
            "action":             action,
            "reasoning":          reasoning,
            "price":              round(price, 4),
            "sma200":             round(sma200, 4),
            "div_sma200":         round(div_sma200, 4),
            "rsi":                round(rsi, 2),
            "kdj_j":              round(kdj_j, 2),
            "dpi_score":          round(dpi_score, 4),
            "sector_weight":      sector_weight,
            "applied_haircut":    haircut,
            "effective_weight":   round(sector_weight * haircut, 4),
            "_asset_data":        {},
        }

    # ------------------------------------------------------------------
    # 메인 실행 엔진
    # ------------------------------------------------------------------
    def run(
        self,
        final_targets: dict,
        tech_data: dict
    ) -> dict:
        """
        Parameters
        ----------
        final_targets : Phase 2 통과 종목 dict
        tech_data     : {ticker: DarkPoolProxyEngine.score_ticker 결과}

        Returns
        -------
        execution_orders : {ticker: order_dict}
        """
        logger.info("=== Phase 3: TECH EXECUTION & ANTI-FOMO 시작 ===")
        execution_orders = {}

        for ticker, asset_info in final_targets.items():
            try:
                order = self._evaluate_ticker(ticker, asset_info, tech_data)
                order['_asset_data'] = asset_info.get('data', {})
                execution_orders[ticker] = order
            except Exception as e:
                logger.error(f"[{ticker}] Phase 3 오류: {str(e)}")
            finally:
                gc.collect()

        # 액션별 집계 로그
        buy_count  = sum(1 for o in execution_orders.values() if o["action"] == ACTION_BUY_TARGET)
        sell_count = sum(1 for o in execution_orders.values() if o["action"] in (ACTION_SELL_100, ACTION_SELL_50))
        hold_count = sum(1 for o in execution_orders.values() if o["action"] == ACTION_HOLD)
        fomo_count = sum(1 for o in execution_orders.values() if o["action"] == ACTION_NO_ENTRY)

        logger.info(
            f"=== Phase 3 완료 — "
            f"BUY:{buy_count} | SELL:{sell_count} | HOLD:{hold_count} | NO_ENTRY:{fomo_count} ==="
        )
        return execution_orders
