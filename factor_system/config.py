"""factor_system 全局配置。

按任务书第十二部分做诚实的技术栈适配：
- 任务书建议 PostgreSQL/Airflow/K8s；本机 8GB 单机，采用 SQLite 注册中心 + launchd 调度 + parquet 数据面板。
- 数据源：backtest_project 已建的 PIT 面板（universe_daily.parquet / pit_fundamentals.parquet / stock_meta.parquet），
  全部为本地真实数据，2020-10 ~ 2025-12，含退市股与 ST 标记，杜绝幸存者偏差。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FACTOR_ROOT = PROJECT_ROOT / "factor_system"
RESEARCH_DATA = PROJECT_ROOT / "research" / "backtest_project" / "data"
STATE_DIR = FACTOR_ROOT / "state"
REGISTRY_DIR = FACTOR_ROOT / "registry"
CACHE_DIR = FACTOR_ROOT / "cache"
SOURCE_CATALOG = FACTOR_ROOT / "source_catalog.json"

# ── 数据窗口（严格时间分离：发现/验证/历史确认/前向观察）──
IS_START = "2021-01-04"
IS_END = "2024-06-28"
OOS_START = "2024-07-01"
OOS_END = "2025-12-31"
# 24h 搜索只能看发现段；L2 只能看验证段。历史确认段只由
# backtest 消费，不得反馈给 GP/方向/权重。旧 IS_*/OOS_* 常量保留兼容。
DISCOVERY_START = IS_START
DISCOVERY_END = "2023-06-30"
VALIDATION_START = "2023-07-03"
VALIDATION_END = IS_END
HISTORICAL_TEST_START = OOS_START
HISTORICAL_TEST_END = OOS_END
# 旧策略报告已实际读取到 2026-07-29，2026 年初不再是未触碰样本。
# 全局盲测从本次严格冻结后的下一自然日起算；每个新因子还必须等待自身 seal 后数据。
FORWARD_SHADOW_START = "2026-08-20"
FORWARD_SHADOW_MIN_DAYS = 120

# 2024-07~2025-12 已被旧系统反复查看，不能再宣称为“未触碰 OOS”。
# 新 epoch 把它降级为 historical confirmation；真正 production 需要
# FORWARD_SHADOW_START 之后的新数据。
RESEARCH_EPOCH = "strict-source-first-shsz-main-gem-20260820-v5"
L0_CONTRACT_VERSION = "eligible-cross-section-v2"
MIN_BARS = 400          # 个股最少有效交易日
MAX_DATES = 1800

# ── 检验阈值（第六部分 L0-L3）──
IC_THRESHOLD = 0.03     # L1: 样本内 |IC| 均值门槛
IC_IR_THRESHOLD = 0.5   # L1: IC_IR = mean(IC)/std(IC)
MIN_IC_DATES = 120      # L1: 最少 IC 观测日
MIN_QUANTILE_SPREAD = 0.06  # L1: 多空分层年化利差门槛
OOS_MAX_IC_DEGRADE = 0.50   # L2: 样本外 |IC| 相对样本内衰减上限（诚实声明：受样本长度影响，0.5 而非任务书 0.3）
FDR_Q = 0.05            # L3: Benjamini-Hochberg FDR 目标 q

# ── 挖掘规模（8GB 单机，串行）──
MAX_CANDIDATES_PER_RUN = 400   # 单批候选上限（任务书 5000，按算力诚实下调）
MAX_TEMPLATE_PARAM_COMBOS = 12 # 单模板参数网格上限
GEN_POPULATION = 56            # P-C 扩量 40→56（固定种子可复现）
GEN_GENERATIONS = 6            # P-C 扩量 4→6
GEN_DEPTH = 3
GEN_SEED = 20260809

# ── 回测（第八部分）──
# 费用只保留 slippage 覆盖项；佣金/印花税/过户费全部走 quantqc.costs 单一真相源（同花顺口径）。
COSTS = {
    "slippage": 0.001,
    # 日线执行容量：交易日开盘只能使用此前 20 个交易日成交额；单名成交
    # 不超过 ADV 的 5%。平方根冲击系数是透明压力参数，不声称由分钟成交校准。
    "adv_window": 20,
    "max_adv_participation": 0.05,
    "impact_coefficient_bps": 25.0,
    # 2026-08-14 C3.1 换手缓冲带（名次数）：组合成员名次在前 max_pos+buffer 的在仓股
    # 保留，新进股仅前 max_pos。净收益=毛利−换手×成本，缓冲带是最有效的降换手手段
    # （Web 研究：换手半衰期平滑约把多因子换手降到 ~68%、保留 ~85% IR；低换手低 IC
    # 常跑赢高换手高 IC）。已在仓股容忍轻微名次滑落，成本拖累下降多于信号衰减损失。
    # 0=关闭（原精确 top-N 行为）；开启后所有科学闸门/回测自动生效。
    "rebal_buffer": 3,
}
HOLD_DAYS = 10          # 默认持仓周期
QUANTILE = 5            # 多头取前 1/5
STOP_FREEZE_DAYS = 20   # 连续 N 日 IC 转负 → 冻结（生命周期）

# ── M2.3 24h 常驻 daemon ──
# 完全相同的数据、搜索空间和人工假设不得无限重跑；连续零产出
# 后只保持心跳并等待真实新输入。原子/grammar 改动时显式升版这两个标识。
DAEMON_SLEEP_SHORT = 60          # 常驻调度轮间 pacing（秒）
DAEMON_MEM_LIMIT_GB = 4.0        # RSS 内存守卫上限（2026-08-14 延迟整改：5.0→4.0，保护 8GB 机上的 5200+Chrome）
DAEMON_ZERO_INFO_ROUNDS = 3      # 连续 N 轮零候选且失败分布不变后等待新输入
DAEMON_ATOM_VERSION = "factor-atoms-v1"
DAEMON_GRAMMAR_VERSION = "factor-grammar-v1"
DAEMON_NOVELTY_CORR = 0.8        # 新颖性：与滚动库最近因子 |IC 相关| > 阈值 → 重复拒
DAEMON_GP_BUDGET = 20            # 每轮 GP 探索候选数（8GB 单机小预算）
DAEMON_NOVELTY_WINDOW = 40       # 滚动新颖性库保留的因子 IC 数
TEMPLATE_GRID_INTERVAL_DAYS = 7  # 模板网格全量批间隔（天）
DAEMON_REVALIDATE_HOURS = 6      # 旧状态/API 兼容字段；新版以 panel data key 变化触发一次重验，
                                 # 静态数据不再每 6h 重复跑同一科学闸门。
