"""
OBAF — Optimal Bottleneck Alpha Framework
Master Orchestrator

실행 순서:
  0. 설정 로드 & 환경 변수 주입
  1. 유니버스 수집 (S&P500 + NASDAQ100)
  2. 데이터 파이프라인 (가격 + SEC 재무)
  3. 대체 데이터 빌드 (china_dep / OFAC / 규제)
  4. 매크로 데이터 수집
  5. 다크풀 프록시 + 기술 지표 계산
  6. Phase 0 → Phase 1 → Phase 2 → Phase 3 순차 필터링
  7. Excel 보고서 생성 + Telegram 발송
"""

import gc
import logging
import os
import sys
import yaml

# 로깅 설정 (GitHub Actions 및 로컬 모두 호환)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | [%(levelname)-8s] | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("OBAF_Master")

# 파일 핸들러 (logs/ 디렉토리)
os.makedirs("logs", exist_ok=True)
from logging.handlers import RotatingFileHandler
file_handler = RotatingFileHandler(
    "logs/obaf.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s | [%(levelname)-8s] | %(name)s | %(message)s")
)
logging.getLogger().addHandler(file_handler)

# OBAF 모듈 임포트
from src.universe        import UniverseManager
from src.data_pipeline   import QuantDataPipeline
from src.alt_data        import AltDataEngine
from src.macro_data      import MacroDataFetcher
from src.dark_pool_proxy import DarkPoolProxyEngine
from src.phase0_veto_gate     import VetoGateFilter
from src.phase1_bneck_strat   import BNeckStratEngine
from src.phase2_valuation     import ValuationSurvivalEngine
from src.phase3_tech_execution import TechExecutionEngine
from src.reporter        import ExcelReporter, TelegramDispatcher


# =============================================================================
# 설정 로더 — 환경 변수 치환
# =============================================================================

def _resolve_env(value):
    """${VAR_NAME} 형식을 os.environ에서 치환"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        resolved = os.environ.get(var_name, "")
        if not resolved:
            logger.warning(f"환경 변수 미설정: {var_name}")
        return resolved
    return value


def _recursive_resolve(obj):
    if isinstance(obj, dict):
        return {k: _recursive_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_recursive_resolve(i) for i in obj]
    return _resolve_env(obj)


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return _recursive_resolve(raw)


# =============================================================================
# 파이프라인 통계 추적
# =============================================================================

class PipelineStats:
    def __init__(self):
        self.data = {}

    def set(self, key: str, val: int):
        self.data[key] = val

    def get(self, key: str, default: int = 0) -> int:
        return self.data.get(key, default)


# =============================================================================
# OBAF 마스터 파이프라인
# =============================================================================

class MasterOBAFPipeline:

    def __init__(self, config: dict):
        self.cfg   = config
        self.stats = PipelineStats()

        # 설정 값 추출
        sec_ua   = config.get("sec",      {}).get("user_agent", "OBAF admin@obaf.io")
        fred_key = config.get("fred",     {}).get("api_key",    "")
        tg_token = config.get("telegram", {}).get("bot_token",  "")
        tg_chat  = config.get("telegram", {}).get("chat_id",    "")
        out_dir  = config.get("output",   {}).get("excel_dir",  "reports")
        dp_cfg   = config.get("dark_pool", {})
        pipe_cfg = config.get("pipeline",  {})

        # 모듈 초기화
        self.universe     = UniverseManager(sec_ua)
        self.pipeline     = QuantDataPipeline(sec_ua, pipe_cfg)
        self.alt_engine   = AltDataEngine("config/blacklist.json", sec_ua)
        self.macro        = MacroDataFetcher(fred_key)
        self.dpi_engine   = DarkPoolProxyEngine(dp_cfg)
        self.phase0       = VetoGateFilter(config)
        self.phase1       = BNeckStratEngine("config/sector_weights.yaml")
        self.phase2       = ValuationSurvivalEngine(config)
        self.phase3       = TechExecutionEngine(config)
        self.excel        = ExcelReporter(out_dir)
        self.telegram     = TelegramDispatcher(tg_token, tg_chat)

    # ------------------------------------------------------------------
    # 메인 실행
    # ------------------------------------------------------------------
    def execute_daily_batch(self) -> dict:
        logger.info("=" * 60)
        logger.info("  OBAF MASTER PIPELINE — 일배치 시작")
        logger.info("=" * 60)

        # ── Step 1: 유니버스 수집 ─────────────────────────────────
        logger.info("[Step 1] 유니버스 수집 (S&P500 + NASDAQ100)")
        target_assets, _cik_map = self.universe.get_full_universe()
        if not target_assets:
            logger.critical("유니버스 수집 실패 → 파이프라인 중단")
            return {}
        logger.info(f"유니버스 확정: {len(target_assets)}개")

        # ── Step 2: 데이터 파이프라인 ────────────────────────────
        logger.info("[Step 2] 데이터 파이프라인 (가격 + SEC 재무)")
        raw_db = self.pipeline.run_pipeline(target_assets)
        if not raw_db:
            logger.critical("데이터 파이프라인 실패 → 중단")
            return {}
        logger.info(f"데이터 수집 완료: {len(raw_db)}개")

        # ── Step 3: 대체 데이터 빌드 ─────────────────────────────
        logger.info("[Step 3] 대체 데이터 빌드 (china_dep / OFAC / 규제)")
        sec_db = {tk: data.get("raw_sec") for tk, data in raw_db.items()}
        self.alt_engine.load_ofac_list()
        alt_db = self.alt_engine.build_alt_database(target_assets, sec_db)

        # ── Step 4: 매크로 데이터 ────────────────────────────────
        logger.info("[Step 4] 매크로 데이터 수집")
        macro_data = self.macro.fetch_all()
        dash_score = macro_data.get("DashScore", 50.0)
        logger.info(
            f"매크로 — US10Y:{macro_data['US10Y']}% | "
            f"Oil:${macro_data['CrudeOil']} | "
            f"Geo:{macro_data['GeoIndex']} | "
            f"DashScore:{dash_score:.1f}"
        )

        # ── Step 5: 다크풀 프록시 + 기술 지표 ───────────────────
        logger.info("[Step 5] 다크풀 프록시 + 기술 지표 계산")
        price_db = {tk: data["prices"] for tk, data in raw_db.items()}
        tech_db  = self.dpi_engine.score_all(price_db)

        # ── Step 6-A: Phase 0 — VETO GATE ────────────────────────
        logger.info("[Step 6-A] Phase 0 VETO GATE")
        self.stats.set("phase0_in", len(raw_db))
        survivors_0 = self.phase0.run_veto_gate(raw_db, alt_db)
        self.stats.set("phase0_out", len(survivors_0))
        gc.collect()

        # ── Step 6-B: Phase 1 — B_NECK_STRAT ─────────────────────
        logger.info("[Step 6-B] Phase 1 B_NECK_STRAT")
        self.stats.set("phase1_in", len(survivors_0))
        survivors_1 = self.phase1.run_alpha_selection(survivors_0, alt_db)
        self.stats.set("phase1_out", len(survivors_1))
        gc.collect()

        # ── Step 6-C: Phase 2 — VALUATION & SURVIVAL ─────────────
        logger.info("[Step 6-C] Phase 2 VALUATION & SURVIVAL")
        self.stats.set("phase2_in", len(survivors_1))
        survivors_2 = self.phase2.run_valuation_survival(
            survivors_1, macro_data, dash_score
        )
        self.stats.set("phase2_out", len(survivors_2))
        gc.collect()

        # ── Step 6-D: Phase 3 — TECH EXECUTION ────────────────────
        logger.info("[Step 6-D] Phase 3 TECH EXECUTION & ANTI-FOMO")
        self.stats.set("phase3_in", len(survivors_2))
        execution_orders = self.phase3.run(survivors_2, tech_db)
        self.stats.set("phase3_out", len(execution_orders))
        gc.collect()

        # ── Step 7: 보고서 생성 + Telegram 발송 ───────────────────
        logger.info("[Step 7] 보고서 생성 + Telegram 발송")
        excel_path = self.excel.generate(
            execution_orders, macro_data, dash_score, self.stats.data
        )
        self.telegram.dispatch_summary(
            execution_orders, macro_data, dash_score,
            self.stats.data, excel_path
        )

        # ── 최종 요약 ─────────────────────────────────────────────
        logger.info("=" * 60)
        logger.info("  OBAF MASTER PIPELINE — 완료")
        logger.info(
            f"  유니버스:{len(target_assets)} → "
            f"P0:{self.stats.get('phase0_out')} → "
            f"P1:{self.stats.get('phase1_out')} → "
            f"P2:{self.stats.get('phase2_out')} → "
            f"P3(주문):{self.stats.get('phase3_out')}"
        )
        buy_cnt  = sum(1 for o in execution_orders.values() if o["action"] == "BUY_TARGET")
        sell_cnt = sum(1 for o in execution_orders.values() if o["action"] in ("SELL_100","SELL_50"))
        logger.info(f"  최종 BUY:{buy_cnt}개 | SELL:{sell_cnt}개")
        logger.info("=" * 60)

        return execution_orders


# =============================================================================
# 진입점
# =============================================================================

if __name__ == "__main__":
    try:
        cfg = load_config("config/settings.yaml")
        pipeline = MasterOBAFPipeline(cfg)
        orders   = pipeline.execute_daily_batch()

        # 콘솔 최종 출력
        print("\n[최종 매매 지시서]")
        for ticker, order in orders.items():
            print(f"  {ticker:8s} | {order['action']:12s} | {order['reasoning'][:80]}")

    except KeyboardInterrupt:
        logger.info("사용자 중단 (KeyboardInterrupt)")
    except Exception as e:
        logger.critical(f"OBAF 마스터 파이프라인 치명적 오류: {e}", exc_info=True)
        sys.exit(1)
    finally:
        gc.collect()
