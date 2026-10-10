from __future__ import annotations

import csv
import json
import os
from pathlib import Path

DEFAULT_MODEL = "deepseek-v4-pro"
EXTERNAL_PAID_API_DISABLED = True
PAID_API_DISABLED_MESSAGE = "外部付费 API 已按系统策略永久禁用；仅运行本地确定性研究。"
_KEYCHAIN_LOAD_ATTEMPTED = False
SYSTEM_PROMPT = """你是A股量化研究的独立红队审查员。
只审查用户提供的代码、规则和报告，不得假设不存在的文件或模块。
不得给出具体股票买卖推荐，不得承诺盈利，不得绕过历史研究门禁。
每个结论必须标注为：已确认、疑似、未发现、无法验证。
引用代码问题时必须给出实际函数名和代码片段；没有证据时明确说无法验证。
重点检查未来数据、T+1、交易成本、涨跌停、整数手、期末估值、幸存者偏差和过拟合。
"""

CODE_DATA_AUDIT_SYSTEM_PROMPT = """你是A股量化研究代码与数据泄漏审计员。
你只有只读权限，不得修改代码、报告、研究门禁或交易状态。
只依据提供的实际代码与报告定位证据，必须引用函数名、字段名或短代码片段。
重点审查未来函数、横截面/时间序列错位、样本选择、复权、幸存者偏差、标签泄漏、
多重检验、T日信号与T+1成交，以及缺失值造成的隐性筛选。
每个结论标注为：已确认、疑似、未发现、无法验证。不得给出个股建议或承诺收益。
本任务是推理和审查，不是训练或微调模型。
"""

STRATEGY_RED_TEAM_SYSTEM_PROMPT = """你是A股V4.4低成本候选策略的独立交易可实现性红队。
你只有只读权限，不得修改代码、报告、研究门禁或交易状态，也不得绕过前序门禁。
只使用提供的实际研究证据，检查100股整数手、T+1、停牌、涨跌停、佣金最低收费、
印花税、滑点、容量、期末估值、空仓和小资金约束。必须区分因子统计通过与策略通过。
每个结论标注为：已确认、疑似、未发现、无法验证。不得给出个股建议或承诺收益。
本任务是推理和审查，不是训练或微调模型。
"""

FACTOR_HYPOTHESIS_SYSTEM_PROMPT = """你是A股量化因子研究的预注册假设设计红队。
你只有只读权限，不得修改代码、报告、研究门禁或交易状态。
根据已失败和已通过的真实实验，提出少量、可证伪、经济机制清楚且不重复调参的实验。
每个实验只能改变一个主要变量，必须固定公式、数据时点、持有期、成本与失败条件；
不得宣称尚未运行的回测结果，不得给出个股建议或承诺收益。
本任务是推理和审查，不是训练或微调模型。
"""

COST_EXECUTION_SYSTEM_PROMPT = """你是A股小资金策略的交易成本与执行审计员。
你只有只读权限，不得修改代码、报告、门禁或交易状态。
重点复核100股整数手、最低佣金、印花税、过户费、滑点、涨跌停、停牌、T+1、
现金不足、容量、换手和期末估值。只依据提供的代码和报告逐项给出证据。
每个结论标注为：已确认、疑似、未发现、无法验证。不得给出个股建议或承诺收益。
本任务是推理和审查，不是训练或微调模型。
"""

FACTOR_ROBUSTNESS_SYSTEM_PROMPT = """你是A股因子经济机制与稳健性独立审查员。
你只有只读权限，不得修改代码、报告、门禁或交易状态。
检查因子经济机制、相关性冗余、市场状态依赖、样本外稳定性、多重检验和失败记录，
区分统计显著、经济显著与可交易。只能提出可证伪的复核建议，不得追逐最优回测。
每个结论标注为：已确认、疑似、未发现、无法验证。不得给出个股建议或承诺收益。
本任务是推理和审查，不是训练或微调模型。
"""

V4_3_GATE_SYSTEM_PROMPT = """你是A股量化研究门禁裁决与预注册审查员。
你只有只读权限，不得修改代码、报告、研究门禁或交易状态。
当冻结的V4.3候选不再满足V4.2统计门禁时，解释失败证据并设计最小下一步，
不得从失败结果中挑选表现最好的替代参数。最多提出3项一次只改变一个变量、
可证伪且能登记失败条件的新实验。不得宣称尚未运行的结果，不得给出个股建议。
本任务是推理和审查，不是训练或微调模型。
"""

CONTINUOUS_REVIEW_TASKS = (
    {
        "task_id": "code_data_leakage",
        "title": "代码与数据泄漏审计",
        "max_tokens": 10000,
        "report_filename": "DEEPSEEK_CODE_DATA_AUDIT_LATEST.md",
        "system_prompt": CODE_DATA_AUDIT_SYSTEM_PROMPT,
    },
    {
        "task_id": "v4_4_strategy_red_team",
        "title": "V4.4低成本策略可实现性红队",
        "max_tokens": 10000,
        "report_filename": "DEEPSEEK_V4_4_STRATEGY_RED_TEAM_LATEST.md",
        "system_prompt": STRATEGY_RED_TEAM_SYSTEM_PROMPT,
    },
    {
        "task_id": "v4_3_gate_adjudication",
        "title": "V4.3门禁失败裁决与再预注册",
        "max_tokens": 10000,
        "report_filename": "DEEPSEEK_V4_3_GATE_ADJUDICATION_LATEST.md",
        "system_prompt": V4_3_GATE_SYSTEM_PROMPT,
    },
    {
        "task_id": "next_factor_hypotheses",
        "title": "下一批因子预注册假设",
        "max_tokens": 8000,
        "report_filename": "DEEPSEEK_NEXT_FACTOR_HYPOTHESES_LATEST.md",
        "system_prompt": FACTOR_HYPOTHESIS_SYSTEM_PROMPT,
    },
    {
        "task_id": "cost_execution_audit",
        "title": "小资金成本与成交执行复核",
        "max_tokens": 8000,
        "report_filename": "DEEPSEEK_COST_EXECUTION_AUDIT_LATEST.md",
        "system_prompt": COST_EXECUTION_SYSTEM_PROMPT,
    },
    {
        "task_id": "factor_robustness_audit",
        "title": "因子经济机制与稳健性复核",
        "max_tokens": 8000,
        "report_filename": "DEEPSEEK_FACTOR_ROBUSTNESS_AUDIT_LATEST.md",
        "system_prompt": FACTOR_ROBUSTNESS_SYSTEM_PROMPT,
    },
)


class DeepSeekError(RuntimeError):
    pass


def _hydrate_key_from_keychain() -> None:
    """Never hydrate paid-API credentials into this process."""
    global _KEYCHAIN_LOAD_ATTEMPTED
    _KEYCHAIN_LOAD_ATTEMPTED = True
    os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ.pop("DEEPSEEK_KEY_VERIFIED", None)


def deepseek_configured() -> bool:
    _hydrate_key_from_keychain()
    return False


def deepseek_status() -> dict:
    deepseek_configured()
    return {
        "configured": False,
        "verified": False,
        "model": "disabled",
        "role": "本地确定性研究；不调用外部付费模型",
        "key_storage": "disabled_by_policy",
        "external_paid_api_disabled": True,
        "network_requests_allowed": False,
        "message": PAID_API_DISABLED_MESSAGE,
    }


def validate_api_key(api_key: str) -> tuple[bool, str | None]:
    del api_key
    return False, PAID_API_DISABLED_MESSAGE


def call_deepseek(
    user_prompt: str,
    *,
    timeout_seconds: int = 120,
    max_tokens: int = 8000,
    system_prompt: str = SYSTEM_PROMPT,
    http=None,
) -> dict:
    del user_prompt, timeout_seconds, max_tokens, system_prompt, http
    raise DeepSeekError(PAID_API_DISABLED_MESSAGE)


def stream_deepseek(
    user_prompt: str,
    *,
    timeout_seconds: int = 180,
    max_tokens: int = 8000,
    thinking_enabled: bool = True,
    http=None,
):
    del user_prompt, timeout_seconds, max_tokens, thinking_enabled, http
    raise DeepSeekError(PAID_API_DISABLED_MESSAGE)


def _compact_v3_report(project_root: Path) -> dict:
    report = json.loads(
        (project_root / "reports" / "v3_strategy_validation.json").read_text(
            encoding="utf-8"
        )
    )
    compact_report = {
        key: value for key, value in report.items()
        if key not in {"strategies"}
    }
    compact_report["strategies"] = [
        {
            key: value for key, value in strategy.items()
            if key not in {"trades", "equity_curve"}
        }
        for strategy in report["strategies"]
    ]
    return compact_report


def build_v3_audit_prompt(project_root: Path) -> str:
    preregistration = (
        project_root / "research" / "STRATEGY_PREREGISTRATION_V3.md"
    ).read_text(encoding="utf-8")
    source = (
        project_root / "research" / "v3_strategies.py"
    ).read_text(encoding="utf-8")
    compact_report = _compact_v3_report(project_root)
    return f"""请独立审查下列真实文件内容。不要引用未提供的路径。

【预注册规则】
{preregistration}

【实际执行代码】
{source}

【回测摘要，逐笔和权益曲线已省略以控制费用】
{json.dumps(compact_report, ensure_ascii=False, indent=2)}

请输出：
1. 可以从代码直接确认的问题；
2. 需要逐笔数据或第二数据源才能验证的问题；
3. T日信号/T+1成交、T+1卖出限制、费用、整数手和期末处理是否正确；
4. 三套策略是否达到预注册门禁；
5. 下一步最小化、不可调参的审计清单。
不得编造独立复算结果。
"""


def build_v4_hypothesis_prompt(project_root: Path) -> str:
    preregistration = (
        project_root / "research" / "STRATEGY_PREREGISTRATION_V3.md"
    ).read_text(encoding="utf-8")
    local_audit = (
        project_root / "reports" / "V3_INDEPENDENT_RED_TEAM.md"
    ).read_text(encoding="utf-8")
    v4_draft = (
        project_root / "research" / "V4_RESEARCH_HYPOTHESES.md"
    ).read_text(encoding="utf-8")
    compact_report = _compact_v3_report(project_root)
    return f"""你正在辅助设计 V4 研究假设，而不是推荐股票或优化回测数字。

【V3 预注册规则】
{preregistration}

【V3 回测摘要】
{json.dumps(compact_report, ensure_ascii=False, indent=2)}

【本地独立红队审查】
{local_audit}

【V4 假设草案】
{v4_draft}

请输出：
1. 逐条复核本地红队结论，标注“同意/不同意/无法验证”和证据；
2. 用通俗语言解释低波动、突破、动量、反转、流动性和市场状态因子；
3. 判断 V3 失败更像是因子无效、状态映射错误、成本侵蚀、数据问题还是样本问题；
4. 对 H1–H4 逐条红队，指出是否只有一个实验变量、是否可证伪、是否存在同一
   数据重复调参；
5. 给出修订后的最多 4 条 V4 假设，每条必须包含公式/固定规则、经济机制、
   主要指标、证伪条件和所需数据；
6. 给出先后顺序明确的最小研究计划。

不得声称 V4 已有效，不得给出个股买卖建议，不得承诺收益，不得编造复算。
"""


def _compact_report(path: Path, excluded_keys: set[str]) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: value for key, value in report.items()
        if key not in excluded_keys
    }


def _compact_v4_2_report(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    compact = {
        key: value for key, value in report.items()
        if key != "tests"
    }
    compact["tests"] = [
        {
            "hypothesis_id": item.get("hypothesis_id"),
            "factor_id": item.get("factor_id"),
            "holding_days": item.get("holding_days"),
            "state_condition": item.get("state_condition"),
            "validation": (item.get("splits") or {}).get("validation"),
            "test": (item.get("splits") or {}).get("test"),
            "low_volatility_filtered_validation": (
                item.get("low_volatility_filtered_splits") or {}
            ).get("validation"),
            "low_volatility_filtered_test": (
                item.get("low_volatility_filtered_splits") or {}
            ).get("test"),
            "positive_test_folds": item.get("positive_test_folds"),
            "one_sided_block_bootstrap_p_value": item.get(
                "one_sided_block_bootstrap_p_value"
            ),
            "holm_adjusted_p_value": item.get(
                "holm_adjusted_p_value"
            ),
            "gate_conditions": item.get("gate_conditions"),
            "failed_gates": item.get("failed_gates"),
            "statistical_gate_passed": item.get(
                "statistical_gate_passed"
            ),
            "state_comparison": item.get("state_comparison"),
        }
        for item in report.get("tests") or []
    ]
    return compact


def build_continuous_research_prompt(project_root: Path) -> str:
    download = _compact_report(
        project_root / "reports" / "v4_daily_download_manifest.json",
        {"batch_results", "files"},
    )
    audit = _compact_report(
        project_root / "reports" / "v4_daily_quality_audit.json",
        {"files"},
    )
    factor = _compact_report(
        project_root / "reports" / "v4_factor_pipeline_dry_run.json",
        {"per_symbol"},
    )
    mining_path = project_root / "reports" / "factor_mining_report.json"
    factor_mining = (
        _compact_report(mining_path, {"candidate_results"})
        if mining_path.exists()
        else {"status": "尚未运行"}
    )
    formal_path = (
        project_root / "reports" / "v4_formal_factor_research.json"
    )
    formal_research = (
        _compact_report(formal_path, {"factors"})
        if formal_path.exists()
        else {"status": "尚未运行"}
    )
    v4_1_path = (
        project_root / "reports" / "v4_1_exploratory_research.json"
    )
    v4_1_research = (
        _compact_report(v4_1_path, {"hypotheses"})
        if v4_1_path.exists()
        else {"status": "尚未运行"}
    )
    v4_2_path = (
        project_root / "reports" / "v4_2_microstructure_research.json"
    )
    v4_2_research = (
        _compact_v4_2_report(v4_2_path)
        if v4_2_path.exists()
        else {"status": "尚未运行"}
    )
    hypotheses = (
        project_root / "research" / "V4_RESEARCH_HYPOTHESES.md"
    ).read_text(encoding="utf-8")
    v4_2_hypotheses = (
        project_root / "research" / "V4_2_RESEARCH_HYPOTHESES.md"
    ).read_text(encoding="utf-8")
    open_source_audit = (
        project_root / "third_party" / "OPEN_SOURCE_AUDIT.md"
    ).read_text(encoding="utf-8")
    return f"""你正在参加一个持续运行的A股量化研究流水线。你只有只读审查权。
本地程序已经生成下列真实报告；报告之外的数值一律不得猜测。

【全量日线下载摘要】
{json.dumps(download, ensure_ascii=False, indent=2)}

【数据质量审计摘要】
{json.dumps(audit, ensure_ascii=False, indent=2)}

【因子计算干跑摘要】
{json.dumps(factor, ensure_ascii=False, indent=2)}

【候选因子独立测试与含成本回测摘要】
{json.dumps(factor_mining, ensure_ascii=False, indent=2)}

【全量V4多周期正式因子研究摘要】
{json.dumps(formal_research, ensure_ascii=False, indent=2)}

【V4.1风险过滤与滚动方向探索摘要】
{json.dumps(v4_1_research, ensure_ascii=False, indent=2)}

【V4.2微观结构因子研究摘要】
{json.dumps(v4_2_research, ensure_ascii=False, indent=2)}

【冻结的V4研究假设】
{hypotheses}

【冻结的V4.2研究假设】
{v4_2_hypotheses}

【开源项目许可证与接入边界】
{open_source_audit}

请输出一份可交给本地工程程序执行的研究红队报告：
1. 先判断数据门禁当前是否允许正式IC或策略回测，并引用报告字段；
2. 检查趋势、残差动量、低波动、量价确认、流动性、市场状态、隔夜收益、
   日内换手冲击和高离散反转是否存在未来数据、重复信息、幸存者偏差、
   多重检验错误或不可成交假设；
3. 只提出最多3个下一轮可证伪实验，每个只改变一个变量，写明固定公式、
   数据时间点、T+1成交规则、成本、主要指标和失败条件；
4. 指出哪些开源能力可以作为实现参考，哪些因许可证或市场不匹配必须隔离；
5. 列出仍需本地程序验证的事项。

不得修改代码，不得替本地程序宣称回测已运行，不得绕过门禁，不得给出个股
买卖建议，不得承诺提高胜率或盈利。
"""


def _optional_text(path: Path, *, max_chars: int = 50000) -> str:
    if not path.exists():
        return f"文件尚不存在：{path.name}"
    content = path.read_text(encoding="utf-8")
    if len(content) <= max_chars:
        return content
    return (
        content[:max_chars]
        + f"\n\n[为控制输入长度，{path.name}后续内容已截断]"
    )


def _optional_compact_report(
    path: Path,
    *,
    excluded_keys: set[str] | None = None,
) -> dict:
    if not path.exists():
        return {"status": "尚未生成", "path": str(path)}
    try:
        return _compact_report(path, excluded_keys or set())
    except (OSError, ValueError, TypeError):
        return {"status": "报告无法解析", "path": str(path)}


def _build_code_data_audit_prompt(project_root: Path) -> str:
    report_dir = project_root / "reports"
    research_dir = project_root / "research"
    audit = _optional_compact_report(
        report_dir / "v4_daily_quality_audit.json",
        excluded_keys={"files"},
    )
    factor_report = _optional_compact_report(
        report_dir / "v4_factor_pipeline_dry_run.json",
        excluded_keys={"per_symbol"},
    )
    v4_2_report = _optional_compact_report(
        report_dir / "v4_2_microstructure_research.json",
        excluded_keys={"tests"},
    )
    v5_report = _optional_compact_report(
        report_dir / "v5_limit_up_factor_research.json",
    )
    v6_report = _optional_compact_report(
        report_dir / "v6_non_limit_max_effect_research.json",
    )
    v25_report = _optional_compact_report(
        report_dir / "v25_structural_factor_research.json",
    )
    return f"""请对下面提供的实际代码和报告做一次独立代码/数据泄漏审计。

【v4_factor_pipeline.py】
{_optional_text(research_dir / 'v4_factor_pipeline.py')}

【v4_2_microstructure_research.py】
{_optional_text(research_dir / 'v4_2_microstructure_research.py')}

【v4_3_strategy_validation.py；若尚不存在必须明确说明无法审查】
{_optional_text(research_dir / 'v4_3_strategy_validation.py')}

【v4_4_factor_research.py】
{_optional_text(research_dir / 'v4_4_factor_research.py')}

【v4_4_low_cost_strategy.py】
{_optional_text(research_dir / 'v4_4_low_cost_strategy.py')}

【v5_limit_up_factor_research.py】
{_optional_text(research_dir / 'v5_limit_up_factor_research.py')}

【v6_non_limit_max_effect_research.py】
{_optional_text(research_dir / 'v6_non_limit_max_effect_research.py')}

【V25冻结协议与实现】
{_optional_text(research_dir / 'V25_STRUCTURAL_FACTORS_PREREGISTRATION.md')}

{_optional_text(research_dir / 'v25_structural_factor_research.py')}

【本地对上一轮DeepSeek指控的裁决；请复核证据，不得直接覆盖】
{_optional_text(report_dir / 'V4_DEEPSEEK_CODE_AUDIT_ADJUDICATION_2026_07_20.md')}

【数据质量摘要】
{json.dumps(audit, ensure_ascii=False, indent=2)}

【因子管线摘要】
{json.dumps(factor_report, ensure_ascii=False, indent=2)}

【V4.2结果元数据】
{json.dumps(v4_2_report, ensure_ascii=False, indent=2)}

【V5首次涨停研究结果】
{json.dumps(v5_report, ensure_ascii=False, indent=2)}

【V6非涨停型MAX效应结果】
{json.dumps(v6_report, ensure_ascii=False, indent=2)}

【V25结构性因子真实结果】
{json.dumps(v25_report, ensure_ascii=False, indent=2)}

请输出：
1. 按严重度列出可从代码直接确认的问题，并引用函数名或短代码片段；
2. 分别审查特征、标签、市场状态、训练/验证/测试切分是否使用未来信息；
3. 审查股票池、停牌/退市、复权和缺失值是否引入幸存者偏差或隐性筛选；
4. 审查bootstrap、Holm校正、低波动过滤是否与预注册一致；
5. 给出本地程序可执行的最小修复/复核清单，但不得声称你已运行代码。
"""


def _build_v4_4_strategy_red_team_prompt(project_root: Path) -> str:
    report_dir = project_root / "reports"
    research_dir = project_root / "research"
    v4_4_factor = _optional_compact_report(
        report_dir / "v4_4_factor_research.json",
    )
    raw_strategy = _optional_compact_report(
        report_dir / "v4_4_low_cost_strategy.json",
        excluded_keys={"results"},
    )
    strategy_path = report_dir / "v4_4_low_cost_strategy.json"
    if strategy_path.exists():
        try:
            full_strategy = json.loads(
                strategy_path.read_text(encoding="utf-8")
            )
            raw_strategy["observed_test_scenarios"] = {
                key: {
                    "metrics": value.get("metrics"),
                    "folds": value.get("folds"),
                    "yearly": value.get("yearly"),
                }
                for key, value in (
                    (
                        full_strategy.get("results") or {}
                    ).get("observed_test") or {}
                ).items()
            }
        except (OSError, ValueError, TypeError):
            raw_strategy["observed_test_scenarios"] = {
                "status": "无法解析"
            }
    return f"""请独立红队审查V4.4低成本候选策略。若代码或报告尚不存在，只能输出
预执行审计清单，不得虚构结果或把正IC等同于策略通过。

【V4.4三项预注册因子结果】
{json.dumps(v4_4_factor, ensure_ascii=False, indent=2)}

【V4.4低成本策略代码】
{_optional_text(research_dir / 'v4_4_low_cost_strategy.py')}

【V4.4策略精简报告】
{json.dumps(raw_strategy, ensure_ascii=False, indent=2)}

【冻结V4.4策略规则】
{_optional_text(research_dir / 'V4_4_LOW_COST_STRATEGY_PREREGISTRATION.md')}

请输出：
1. 明确当前证据属于因子统计、历史策略回测还是前向模拟中的哪一级；
2. 检查T日信号/T+1开盘、A股T+1卖出、100股整数手、单持仓与冷却期；
3. 检查最低佣金、卖出印花税、过户费、滑点与10/20BP压力测试；
4. 检查停牌、涨跌停无法成交、退市、现金不足、期末强平和允许空仓；
5. 给出策略门禁逐项通过/失败/无法验证，并列出进入模拟盘前的阻断项。
不得建议解除尚未通过的门禁。
"""


def _build_next_factor_hypotheses_prompt(project_root: Path) -> str:
    report_dir = project_root / "reports"
    research_dir = project_root / "research"
    formal = _optional_compact_report(
        report_dir / "v4_formal_factor_research.json",
        excluded_keys={"factors"},
    )
    v4_1 = _optional_compact_report(
        report_dir / "v4_1_exploratory_research.json",
        excluded_keys={"hypotheses"},
    )
    v4_2_path = report_dir / "v4_2_microstructure_research.json"
    if v4_2_path.exists():
        try:
            v4_2 = _compact_v4_2_report(v4_2_path)
        except (OSError, ValueError, TypeError):
            v4_2 = {"status": "报告无法解析"}
    else:
        v4_2 = {"status": "尚未生成"}
    v4_4_factor = _optional_compact_report(
        report_dir / "v4_4_factor_research.json",
    )
    v4_4_strategy = _optional_compact_report(
        report_dir / "v4_4_low_cost_strategy.json",
        excluded_keys={"results"},
    )
    v5 = _optional_compact_report(
        report_dir / "v5_limit_up_factor_research.json",
    )
    v6 = _optional_compact_report(
        report_dir / "v6_non_limit_max_effect_research.json",
    )
    v25 = _optional_compact_report(
        report_dir / "v25_structural_factor_research.json",
    )
    return f"""请基于真实失败/通过记录设计下一批预注册研究，而不是扩大搜索空间后挑最好结果。

【V4正式研究摘要】
{json.dumps(formal, ensure_ascii=False, indent=2)}

【V4.1探索摘要】
{json.dumps(v4_1, ensure_ascii=False, indent=2)}

【V4.2逐项精简结果】
{json.dumps(v4_2, ensure_ascii=False, indent=2)}

【V4.4三项因子结果】
{json.dumps(v4_4_factor, ensure_ascii=False, indent=2)}

【V4.4低成本策略摘要】
{json.dumps(v4_4_strategy, ensure_ascii=False, indent=2)}

【V5首次涨停溢出效应真实结果】
{json.dumps(v5, ensure_ascii=False, indent=2)}

【V6非涨停型彩票偏好MAX效应真实结果】
{json.dumps(v6, ensure_ascii=False, indent=2)}

【V25结构性因子真实结果；不得调参挽救失败项】
{json.dumps(v25, ensure_ascii=False, indent=2)}

【既有V4假设】
{_optional_text(research_dir / 'V4_RESEARCH_HYPOTHESES.md')}

【既有V4.2假设】
{_optional_text(research_dir / 'V4_2_RESEARCH_HYPOTHESES.md')}

【既有V4.4假设与策略预注册】
{_optional_text(research_dir / 'V4_4_RESEARCH_HYPOTHESES.md')}

{_optional_text(research_dir / 'V4_4_LOW_COST_STRATEGY_PREREGISTRATION.md')}

【V5冻结预注册；该实验已运行，不得改参数重跑】
{_optional_text(research_dir / 'V5_LIMIT_UP_SPILLOVER_PREREGISTRATION.md')}

【V6冻结预注册；该实验已运行且失败，不得改参数重跑】
{_optional_text(research_dir / 'V6_NON_LIMIT_MAX_EFFECT_PREREGISTRATION.md')}

【V25冻结预注册；已运行后不得改参数重跑】
{_optional_text(research_dir / 'V25_STRUCTURAL_FACTORS_PREREGISTRATION.md')}

请只提出最多3个互不重复的下一轮实验。每个必须包含：
- 唯一假设编号和经济机制；
- 完全固定的公式、方向、回看期、预测期与股票池；
- 每个字段在交易日何时可获得，明确禁止使用未来信息；
- 唯一主要变量，以及与既有V4/V4.1/V4.2实验的差异；
- 固定交易成本、多重检验族和失败条件；
- 为什么失败结果也有研究价值。
不得再次提出或换参数包装以下因子族：首次涨停溢出、整数关口、尾盘成交量
异动、短期反转、隔夜收益、负日内冲击、非涨停MAX彩票偏好。不得选择子年份、
板块、市场状态、持有期或阈值挽救V5/V6失败结果。
最后给出优先级，但不得声称这些实验能够提高胜率或盈利。
"""


def _build_continuous_discovery_evidence(project_root: Path) -> str:
    """Return a small, stable evidence packet for rapid discovery rounds.

    The full hypothesis prompt is deliberately comprehensive but is far too
    large to resend every few minutes.  Continuous work needs fixed outcomes
    and exclusions, not thousands of rows of already-failed factor detail.
    """
    report_dir = project_root / "reports"
    # Discovery only needs the immutable verdicts.  Sending registries,
    # per-split rows and event samples again caused a completed 26-factor
    # report to push every role beyond the prompt budget and encouraged three
    # roles to reason from different truncated tails.
    verdict_keys = {
        "version", "generated_at", "status", "historical_evidence_class",
        "data_gate_passed", "research_gate_passed",
        "statistical_gate_pass_count", "production_factor_gate_pass_count",
        "statistical_gate_passed", "historical_strategy_gate_passed",
        "event_data_gate_passed", "strategy_freeze_allowed",
        "strategy_backtest_started", "real_money_gate_passed",
        "completed_candidate_count", "total_candidate_count",
        "failed_gates", "production_blockers", "next_action", "warning",
        "execution_audit",
    }
    metric_keys = {
        "trades", "wins", "win_rate_pct", "annualized_return_pct",
        "total_return_pct", "max_drawdown_pct", "profit_factor",
        "final_equity", "net_profit", "start", "end",
    }

    def verdict(filename: str) -> dict:
        report = _optional_compact_report(report_dir / filename)
        if report.get("status") == "missing":
            return report
        result = {
            key: report.get(key) for key in verdict_keys if key in report
        }
        compact_summary = {}
        for name, item in (report.get("summary") or {}).items():
            if isinstance(item, dict):
                kept = {key: item.get(key) for key in metric_keys if key in item}
                if kept:
                    compact_summary[name] = kept
            elif name.endswith("wilson_95_ci_low_pct"):
                compact_summary[name] = item
        if compact_summary:
            result["summary"] = compact_summary
        return result

    compact_reports = {
        "V4正式因子": verdict("v4_formal_factor_research.json"),
        "V4.1探索": verdict("v4_1_exploratory_research.json"),
        "V4.2微观结构": verdict("v4_2_microstructure_research.json"),
        "V4.4因子": verdict("v4_4_factor_research.json"),
        "V4.4策略": verdict("v4_4_low_cost_strategy.json"),
        "V5事件": verdict("v5_limit_up_factor_research.json"),
        "V6 MAX": verdict("v6_non_limit_max_effect_research.json"),
        "V7公告源小样本": verdict("v7_pre_disclosure_source_audit.json"),
        "V13开源候选总表": verdict("v13_open_source_factor_research.json"),
        "V14融资余额加速度": verdict("v14_margin_balance_acceleration.json"),
        "V15融资标的调入事件库": verdict("v15_margin_eligibility_events.json"),
        "V15融资标的调入收益": verdict("v15_margin_eligibility_factor_research.json"),
        "V16首次股份回购方案事件库": verdict("v16_share_repurchase_events.json"),
        "V16首次股份回购方案收益": verdict("v16_share_repurchase_factor_research.json"),
        "V17跌停流动性恢复": verdict("v17_limit_down_liquidity_recovery.json"),
        "V18开源候选总表": verdict("v18_open_source_factor_research.json"),
        "V21股东增持": verdict("v21_shareholder_increase_pilot.json"),
        "V25结构性因子": verdict("v25_structural_factor_research.json"),
        "V31增持事件元模型": verdict("v31_event_meta_model.json"),
        "V44长周期质量动量": verdict("v44_long_horizon_quality_momentum.json"),
        "V45状态胜率模型": verdict("v45_regime_aware_win_probability.json"),
        "V46分散长周期": verdict("v46_diversified_long_horizon.json"),
        "V47公告时点基本面": verdict("v47_point_in_time_fundamental_quality.json"),
        "V48公告后漂移": verdict("v48_post_announcement_drift.json"),
        "V49现金盈利质量覆盖层": verdict("v49_cash_earnings_quality_overlay.json"),
        "V59季度滚动开盘缺口": verdict("v59_quarterly_walk_forward_opening_gap.json"),
        "V60次日开盘可执行纠偏": verdict("v60_next_open_executable_gap.json"),
        "V61季度滚动长周期": verdict("v61_quarterly_walk_forward_long_horizon.json"),
        "V62涨停延续": verdict("v62_limit_hit_continuation.json"),
    }
    v12_status = _optional_compact_report(
        report_dir / "v12_fundamental_builder_status.json"
    )
    v12_inventory = {
        key: v12_status.get(key)
        for key in (
            "status", "data_gate_passed", "fundamental_records",
            "symbols_with_records", "stat_periods", "temporal_audit",
            "field_coverage",
        )
        if key in v12_status
    }
    v50_status = _optional_compact_report(
        report_dir / "v50_roe_data_builder_status.json"
    )
    v50_inventory = {
        key: v50_status.get(key)
        for key in (
            "status", "planned_report_periods", "downloaded_report_periods",
            "records", "data_gate_passed", "factor_backtest_allowed",
            "block_reason",
        )
        if key in v50_status
    }
    csrc_audit = _optional_compact_report(
        report_dir / "v87_csrc_enforcement_data_epoch_audit.json"
    )
    csrc_manifest = _optional_compact_report(
        project_root / "research" / "data" / "v87_successor_intake"
        / "manifest.json"
    )
    csrc_epoch = {
        "data_epoch_id": csrc_manifest.get("data_epoch_id"),
        "created_at": csrc_manifest.get("created_at"),
        "purpose": csrc_manifest.get("purpose"),
        "candidate": csrc_manifest.get("candidate"),
        "source_count": len(csrc_manifest.get("data_sources") or []),
        "record_count": csrc_audit.get("record_count"),
        "category_totals": csrc_audit.get("actual_category_counts"),
        "coverage_start": csrc_audit.get("coverage_start"),
        "coverage_end": csrc_audit.get("coverage_end"),
        "publication_timestamp_field": "publishedTimeStr",
        "data_quality_gate_passed": (
            csrc_audit.get("data_quality_gate_passed") is True
        ),
        "quality_gates": csrc_audit.get("gates") or {},
        "source_urls": csrc_audit.get("source_urls") or {},
        "legal_sources": csrc_audit.get("legal_sources") or {},
    }
    v87_mapping_failure = _optional_compact_report(
        report_dir / "v87_csrc_enforcement_mapping_failure_diagnostic.json"
    )
    v88_mapping_audit = _optional_compact_report(
        report_dir / "v88_csrc_scoped_entity_mapping_audit.json"
    )
    mapping_status = {
        "v87_mapping_invalidated": (
            v87_mapping_failure.get("mapping_invalidated") is True
        ),
        "v87_confirmed_failure": v87_mapping_failure.get("confirmed_failure"),
        "v87_rows_mapped_to_citic_bank_count": (
            v87_mapping_failure.get("rows_mapped_to_citic_bank_count")
        ),
        "v88_mapping_epoch_id": v88_mapping_audit.get("mapping_epoch_id"),
        "v88_status": v88_mapping_audit.get("status"),
        "v88_mapping_data_ready": (
            v88_mapping_audit.get("mapping_data_ready") is True
        ),
        "v88_mapping_summary": v88_mapping_audit.get("mapping_summary") or {},
        "v88_manual_review": v88_mapping_audit.get("manual_review") or {},
        "v88_failed_gates": v88_mapping_audit.get("failed_gates") or [],
        "v88_factor_backtest_allowed": (
            v88_mapping_audit.get("factor_backtest_allowed") is True
        ),
    }
    sse_audit = _optional_compact_report(
        report_dir / "v89_sse_regulatory_measures_data_epoch_audit.json"
    )
    sse_summary = _optional_compact_report(
        project_root / "research" / "data" / "v89_sse_regulatory_measures"
        / "summary.json"
    )
    sse_regulatory_epoch = {
        "data_epoch_id": sse_audit.get("data_epoch_id"),
        "status": sse_audit.get("status"),
        "data_quality_gate_passed": (
            sse_audit.get("data_quality_gate_passed") is True
        ),
        "successor_discovery_allowed": (
            sse_audit.get("successor_discovery_allowed") is True
        ),
        "factor_backtest_allowed": (
            sse_audit.get("factor_backtest_allowed") is True
        ),
        "record_count": sse_audit.get("record_count"),
        "coverage_start": sse_audit.get("coverage_start"),
        "coverage_end": sse_audit.get("coverage_end"),
        "direct_single_a_share_records": sse_audit.get(
            "direct_single_a_share_records"
        ),
        "direct_multiple_a_share_records": sse_audit.get(
            "direct_multiple_a_share_records"
        ),
        "no_a_share_code_records": sse_audit.get("no_a_share_code_records"),
        "publication_timestamp_field": sse_audit.get(
            "publication_timestamp_field"
        ),
        "mapping_status_counts": sse_summary.get("mapping_status_counts") or {},
        "regulatory_type_counts": sse_summary.get("regulatory_type_counts") or {},
        "quality_gates": sse_audit.get("gates") or {},
        "official_source_url": sse_audit.get("official_source_url"),
        "official_legal_url": sse_audit.get("official_legal_url"),
        "price_return_ic_or_backtest_files_read": sse_summary.get(
            "price_return_ic_or_backtest_files_read"
        ),
    }
    v89_literature_register = _optional_text(
        project_root / "research" / "V89_REGULATORY_EVENT_LITERATURE_REGISTER.md",
        max_chars=12000,
    )
    try:
        from research.open_source_factor_registry_v18 import registry_payload

        v18_registry = registry_payload()
    except (ImportError, OSError, TypeError, ValueError) as exc:
        v18_registry = {
            "status": "registry_unavailable",
            "error": type(exc).__name__,
        }
    return f"""【连续研究上下文版本】
v89_sse_regulatory_measures_data_epoch_2026_08_10

【连续研究的固定证据摘要】
{json.dumps(compact_reports, ensure_ascii=False, indent=2)}

【当前已验收的新增点时数据】
{json.dumps(v12_inventory, ensure_ascii=False, indent=2)}

V12的物理可用字段必须以`field_coverage.usable_fields_at_least_95pct`为准；
列名存在但覆盖率为0的ROE、EPS、股本或现金流增长绝对不能当作可用数据。
交易日只能读取pub_date不晚于该交易日的记录。当前没有已验收的
资产负债表、分析师预测、逐笔订单、盘口、董监高交易明细或通用新闻文本；不得
假装这些字段存在。证监会行政处罚/市场禁入全文是下面单列的例外，但它还没有
股票代码、受罚主体类型或可交易时点映射，不能把全文存在等同于因子可直接回测。

【V87新验收的证监会执法数据纪元；仅允许候选发现】
{json.dumps(csrc_epoch, ensure_ascii=False, indent=2)}

本纪元只含证监会行政处罚与市场禁入决定的官方全文及官方发布时间。候选必须先
给出可审计的上市公司/股票身份映射、受罚对象分类、同日盘中发布的T+1处理，以及
公告已被价格吸收的反证方案。当前candidate=null，严禁声称已有IC、回测或收益；
未完成唯一候选共识和回测前预注册前，只能输出发现草案。

【执法决定主体映射状态】
{json.dumps(mapping_status, ensure_ascii=False, indent=2)}

V87全文映射已因罚款汇缴银行模板污染永久淘汰。V88只扫描标题、当事人区块和
立案前言，仍缺新的200条人工盲审；`v88_mapping_data_ready=false`期间，任何角色
都必须输出`点时数据：NEW_DATA_REQUIRED`，注册裁判必须REJECT，不能把970条
暂定可用记录当作已验收样本，更不能建议先看收益再补盲审。

【V89新验收的上交所监管措施直接代码数据纪元；仅允许候选发现】
{json.dumps(sse_regulatory_epoch, ensure_ascii=False, indent=2)}

V89只保存上交所官方监管措施索引元数据，不保存决定书正文；官方字段直接给出
证券代码、简称、监管类型、涉及对象、精确处理时间和决定书链接，不需要公司名
NLP映射。只有`direct_single_a_share`记录可作为候选事件，多个A股代码、B股或缺码
记录必须隔离。其法律快照只授权本机非商业研究浏览/下载，禁止出售或再分发内容。
对只依赖上述已审计索引字段的候选，`点时数据：READY`表示非收益输入已具备且可
冻结预注册；`factor_backtest_allowed=false`只表示价格读取尚未授权，不能据此把
已具备的事件输入误判为`NEW_DATA_REQUIRED`。若候选依赖决定书正文、处罚金额或
其他未保存字段，才必须输出`NEW_DATA_REQUIRED`。候选在四路形成唯一共识并完成
价格读取前预注册之前，仍不得读取收益。不得退回依赖V88盲审的CSRC候选来冒充
V89新纪元候选。

【V89已登记原始研究；机制依据不是盈利证据】
{v89_literature_register}

登记论文只证明监管事件后果值得被证伪，不证明V89口径、任何固定窗口或长仓排除
会盈利。角色必须点名采用的论文机制及V89适配偏差。事件股票未来收益可作为冻结的
辅助机制诊断；它既不能计入候选利润，也不能替代主要可执行检验。主要盈利门仍是
同一冻结长仓基线组合与事前排除/降权组合在10/20/30/50bp成本下的样本外差异。

【V50 ROE点时数据协议状态；不是已可用因子】
{json.dumps(v50_inventory, ensure_ascii=False, indent=2)}

V50只完成了冻结的数据采集协议；在`data_gate_passed=true`之前，不得把ROE
候选写成READY、不得运行ROE策略。通过数据门禁后也必须另写一次只改变一个
主要变量的因子预注册，不能根据结果选择ROE窗口、阈值或权重。

【V18冻结注册表与GitHub审计证据】
{json.dumps(v18_registry, ensure_ascii=False, indent=2)}

第四路审查必须逐项对照 source_formula 与 formula；implementation_note
中的字段替换属于已冻结偏差，不得静默当作源公式一致，也不得在看到结果后修正。
固定提交、文件SHA和许可证只证明来源可追溯，不证明该因子能盈利。

【不可重包装的失败机制族】
首次涨停溢出、整数关口、尾盘分钟量价（当前日线不支持）、短期反转、隔夜收益、
负日内冲击、非涨停 MAX 彩票偏好、融资余额加速度、融资标的调入、首次股份回购
方案公告、股东或董监高增持公告（V21/V31已经检验）、跌停后流动性恢复，
以及V25已经检验的下行尾部风险、成交额异常、
非涨停隔夜跳空事件；禁止在失败结果上换窗口、换阈值或选择子市场补救。V15测试期60.44%胜率不能覆盖验证期负收益；
V16测试期胜率58.966%、10BP后平均收益+1.61888%，但单侧
p=0.05618876且未满足用户60%胜率门槛，因此同样不得补救、分组或进入推荐。
V49现金盈利质量覆盖层的观察测试胜率49.057%、年化-9.109%，已经封存；
不得用现金流/利润率换权重、换阈值或与V44重新加权来补救。
V51 Alpha006、V52规模代理、V53季节性、V54 FIP、V55低MAX、V56分散缺口、
V57高置信缺口、V58预观察期重拟合、V61季度长周期及V62涨停延续均已按各自
冻结协议运行且未通过完整门禁；不得改参数、选子期或改名重跑。V59表面观察期
胜率62.447%、年化28.363%，但它在看到完整当日开盘横截面后仍按同一开盘价
成交，执行审计已经否决；唯一成交时点纠偏V60改为下一交易日开盘后，观察期
胜率43.103%、年化-15.339%。因此V59的高指标属于不可执行诊断，不是候选证据，
任何新研究都不得复用“观察开盘后按同一开盘成交”的假设。
V63点时估值审计通过4,802/4,802、0失败。V64/V65/V68分别为50.588%/8.798%、
50.769%/-7.258%、37.209%/-4.999%，已封存。V66为37笔62.162%/10.372%，
V67为27笔62.963%/13.321%，不得继续用观察期调仓位补救。V69日频104笔
55.769%/37.741%；V70市场状态50.000%/11.313%；V71滞后走前93笔
58.065%/16.682%；V73路径风险110笔59.091%/19.188%、回撤14.390%；V74
执行标签纠偏后110笔58.182%/17.228%，均按冻结定义失败。冻结V44已用同一源码、
面板和隔离输出精确复现108笔、胜率60.185%、年化64.221%、Wilson下界50.756%，
但回撤17.163%未过15%，且不是全局独立留出；前向仍0/30，真钱和自动交易锁定。

【研究边界】
4,802只真实A股日线已通过质量审计；V13的26个开源日线候选、V14、V15和V16均未
形成可解锁真钱门禁的证据。V18是独立冻结的开源公式批次；不得根据其结果改方向、
窗口或阈值。新机制优先选择能形成至少250个独立测试交易日的机制；若是稀疏事件，
必须在看收益前证明有足够独立事件日和可执行样本。任何新机制先通过点时数据和统计
门禁，才允许另行冻结策略；不产生交易信号。
"""


def _build_v89_discovery_evidence(project_root: Path) -> str:
    """Return only the evidence needed to preregister one V89 mechanism.

    The generic packet contains the verdicts of dozens of unrelated research
    generations.  Resending it on every V89 round adds no admissible evidence,
    costs tokens, and can trigger provider content-risk rejection.  This packet
    keeps the same fail-closed rules while binding the prompt to the audited
    event epoch actually under review.
    """
    report_dir = project_root / "reports"
    audit = _optional_compact_report(
        report_dir / "v89_sse_regulatory_measures_data_epoch_audit.json"
    )
    summary = _optional_compact_report(
        project_root / "research" / "data" / "v89_sse_regulatory_measures"
        / "summary.json"
    )
    epoch = {
        "data_epoch_id": audit.get("data_epoch_id"),
        "status": audit.get("status"),
        "data_quality_gate_passed": audit.get("data_quality_gate_passed") is True,
        "successor_discovery_allowed": audit.get("successor_discovery_allowed") is True,
        "factor_backtest_allowed": audit.get("factor_backtest_allowed") is True,
        "record_count": audit.get("record_count"),
        "coverage_start": audit.get("coverage_start"),
        "coverage_end": audit.get("coverage_end"),
        "publication_timestamp_field": audit.get("publication_timestamp_field"),
        "direct_single_a_share_records": audit.get("direct_single_a_share_records"),
        "direct_multiple_a_share_records": audit.get("direct_multiple_a_share_records"),
        "no_a_share_code_records": audit.get("no_a_share_code_records"),
        "mapping_status_counts": summary.get("mapping_status_counts") or {},
        "regulatory_type_counts": summary.get("regulatory_type_counts") or {},
        "quality_gates": audit.get("gates") or {},
        "price_return_ic_or_backtest_files_read": summary.get(
            "price_return_ic_or_backtest_files_read"
        ),
    }
    return f"""【V89唯一研究数据纪元】
{json.dumps(epoch, ensure_ascii=False, indent=2)}

V89只保存上交所官方监管措施索引元数据。只有`direct_single_a_share`记录可作为
候选事件，多代码、B股或缺码记录必须隔离。精确处理时间是点时边界；盘中或盘后
信息都不得在其公开前使用。`factor_backtest_allowed=false`表示价格读取尚未授权，
不能据此把已审计的事件输入误判为`NEW_DATA_REQUIRED`；候选四路共识和价格读取前
预注册完成前，仍严禁读取收益、IC或运行回测。

【机制依据不是盈利证据】
- He and Fang (2019), DOI 10.1016/j.pacfin.2019.101241：监管处罚可能影响定价效率，
  但不支持固定10日负收益或V89长仓排除利润。
- Xu and Xu (2020), DOI 10.1016/j.irle.2019.105877：行政处罚与异常收益关系可检验，
  但部分经济效果可能不显著，且CSRC处罚不等于交易所自律监管。
- Huang and Zhang (2023), DOI 10.1016/j.irle.2023.106152：公开谴责等声誉制裁可能
  有资本市场后果，但不能推导所有监管类型同质。
- Wu and Zhang (2014), DOI 10.1080/21697221.2014.891069：监管调查公告可做事件研究，
  但不支持固定窗口持续漂移或做空获利。

事件股票未来收益可作为冻结的
辅助机制诊断；它不能计入候选利润。若为负向信号，主要盈利检验只能是同一冻结
长仓基线与事前排除/降权组合在10/20/30/50bp成本下的滚动样本外差异，并同时对照
sh000300、sh000905、sh000852。监管类型合并规则、唯一基线、执行时点、窗口、
成本、多重检验和失败即封存条件必须在读取价格前固定。

【已封存且不得重包装的机制】
公告后漂移、融资标的调入、股份回购、股东或董监高增持、涨跌停延续/恢复、短期
反转、隔夜跳空、MAX彩票偏好、微观结构价格几何、质量动量及其换窗口/阈值/子类。
仅把“公告后漂移”改名为“监管公告后漂移”必须拒绝；论文引用不能替代独特经济
机制、长仓组合动作和V89适配偏差。

V89第1至7轮已经把同一失败族封存完整：凡以监管记录直接指向的证券为响应单元，
再对同一证券做纳入、排除或降权的规格，一律视为旧公告事件族，不得继续提出。
这包括但不限于按监管类型、严重度、首次/孤立、重复次数、密度、升级序列、回看期
或持有期切分。新规格必须更换分析单元和组合层面的经济机制，并有已登记研究能够
支持该机制；做不到时必须输出`点时数据：NEW_DATA_REQUIRED`，不能制造第8个变体。

【执行与安全边界】
仅允许长仓、最多5只、T日公开信息最早T+1成交、100股整数手、停牌和涨跌停不可
成交、允许空仓；不得连接券商、不得自动下单、不得承诺收益。四路没有形成同一
唯一候选时不登记、不回测。
"""


def _build_v90_discovery_evidence(project_root: Path) -> str:
    """Bind discovery to the audited PMI epoch and the sealed V90 failure."""
    report_dir = project_root / "reports"
    audit = _optional_compact_report(
        report_dir / "v90_nbs_pmi_data_epoch_audit.json"
    )
    v90 = _optional_compact_report(
        report_dir / "v90_nbs_pmi_regime_gate.json"
    )
    status = _optional_compact_report(
        report_dir / "v90_successor_research_status.json"
    )
    pmi_path = (
        project_root / "research" / "data" / "v90_nbs_pmi"
        / "manufacturing_pmi.csv"
    )
    fields = (
        "month", "pmi", "production", "new_orders",
        "raw_material_inventory", "employment", "supplier_delivery",
        "publication_at",
    )
    try:
        with pmi_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        pmi_csv = "\n".join([
            ",".join(fields),
            *(",".join(row[field] for field in fields) for row in rows),
        ])
    except (OSError, KeyError, csv.Error):
        pmi_csv = "PMI物理CSV不可读；本轮必须输出NEW_DATA_REQUIRED。"
    frozen_failure = {
        "strategy_id": v90.get("strategy_id"),
        "formula": v90.get("formula"),
        "historical_strategy_gate_passed": v90.get(
            "historical_strategy_gate_passed"
        ) is True,
        "failed_gates": v90.get("failed_gates") or [],
        "validation": ((v90.get("splits") or {}).get("validation") or {}).get(
            "cost_10bp"
        ),
        "test": ((v90.get("splits") or {}).get("test") or {}).get("cost_10bp"),
    }
    epoch = {
        "data_epoch_id": audit.get("data_epoch_id"),
        "data_epoch_ready": audit.get("data_epoch_ready") is True,
        "source": audit.get("source"),
        "source_attribution": audit.get("source_attribution"),
        "month_range": audit.get("month_range"),
        "actual_months": audit.get("actual_months"),
        "source_archives": audit.get("source_archives"),
        "quality_gates": audit.get("gates") or {},
        "discovery_round": status.get("discovery_round"),
        "round_limit": status.get("maximum_discovery_rounds_per_epoch"),
    }
    return f"""【V90国家统计局PMI数据纪元】
{json.dumps(epoch, ensure_ascii=False, indent=2)}

【已封存V90结果；不得追参或重包装】
{json.dumps(frozen_failure, ensure_ascii=False, indent=2)}

V90的`PMI>50且新订单>50则持有沪深300、否则现金`已按固定2022—2025样本、
公告后下一交易日开盘和10/20BP成本失败封存。不得改变50阈值、方向、样本、成本、
持有资产、执行时点或增加平滑窗口来补救，也不得把同一双扩张条件换名重提。

当前唯一新增输入是国家统计局月度、经季调的制造业PMI六项表和逐页真实发布时间。
48/48个月及116/116官方归档已做SHA-256验收。新机制必须有与V90不同的经济因果路径，
只使用下面已有字段；若这些字段无法支持真正不同的机制，必须输出
`点时数据：NEW_DATA_REQUIRED`并说明最小新增官方数据，不能拼接旧价格因子。

【已验收字段；只作机制设计，不是盈利证据】
{pmi_csv}

任何候选仍只允许研究规格。形成四路唯一共识、重复机制审查通过并在读取新增收益前
冻结公式、时点、成本、样本和失败条件后，才允许一次本地回测；不得输出个股、仓位、
订单或声称未来盈利。
"""


def _build_v4_3_gate_adjudication_prompt(project_root: Path) -> str:
    report_dir = project_root / "reports"
    research_dir = project_root / "research"
    v4_2_path = report_dir / "v4_2_microstructure_research.json"
    if v4_2_path.exists():
        try:
            v4_2 = _compact_v4_2_report(v4_2_path)
        except (OSError, ValueError, TypeError):
            v4_2 = {"status": "报告无法解析"}
    else:
        v4_2 = {"status": "尚未生成"}
    previous_v4_3 = _optional_compact_report(
        report_dir / "v4_3_strategy_validation.json",
        excluded_keys={"results"},
    )
    archive = _optional_compact_report(
        report_dir / "v4_3_historical_failure_archive.json",
    )
    v5 = _optional_compact_report(
        report_dir / "v5_limit_up_factor_research.json",
    )
    return f"""请复核为什么不能继续运行冻结的V4.3策略，并核对已经关闭的
V4.3历史失败档案与V5结果。V4.3不得恢复运行，也不得从失败候选中选择参数。

【V4.2逐项统计结果】
{json.dumps(v4_2, ensure_ascii=False, indent=2)}

【现有V4.3冻结规则】
{_optional_text(research_dir / 'V4_3_STRATEGY_PREREGISTRATION.md')}

【V4.3执行代码】
{_optional_text(research_dir / 'v4_3_strategy_validation.py', max_chars=45000)}

【此前V4.3结果；仅作历史记录，不得当作新样本外证据】
{json.dumps(previous_v4_3, ensure_ascii=False, indent=2)}

【V4.3历史失败档案】
{json.dumps(archive, ensure_ascii=False, indent=2)}

【V5首次涨停溢出效应结果】
{json.dumps(v5, ensure_ascii=False, indent=2)}

请输出：
1. 核对V4.3关闭是否符合既有统计门禁；
2. 核对V5失败是否足以阻止¥10,000策略回测；
3. 指出不得进行的事后分组、子样本挑选或阈值搜索；
4. 只提出下一轮研究必须满足的边界，不重复设计首次涨停、整数关口或尾盘
   成交量异动；
5. 列出本地程序必须验证的证据。
不得修改门禁，不得编造回测，不得承诺收益。
"""


def _build_cost_execution_audit_prompt(project_root: Path) -> str:
    report_dir = project_root / "reports"
    research_dir = project_root / "research"
    strategy = _optional_compact_report(
        report_dir / "v4_4_low_cost_strategy.json",
        excluded_keys={"results"},
    )
    return f"""请独立复核¥10,000小资金策略的成本和成交执行，不要重新设计策略。

【V4.4执行代码】
{_optional_text(research_dir / 'v4_4_low_cost_strategy.py', max_chars=40000)}

【V4.4成本与门禁摘要】
{json.dumps(strategy, ensure_ascii=False, indent=2)}

【冻结执行规则】
{_optional_text(research_dir / 'V4_4_LOW_COST_STRATEGY_PREREGISTRATION.md')}

请逐项核对整数手、最低佣金、印花税、过户费、滑点、T+1、停牌、涨跌停、
现金不足、期末估值、换手率和压力测试。给出代码证据、潜在偏差方向和最小本地
复核测试。不得修改代码，不得建议跳过策略或模拟盘门禁。
"""


def _build_factor_robustness_audit_prompt(project_root: Path) -> str:
    report_dir = project_root / "reports"
    summaries = {
        "v4_formal": _optional_compact_report(
            report_dir / "v4_formal_factor_research.json",
            excluded_keys={"factors"},
        ),
        "v4_1": _optional_compact_report(
            report_dir / "v4_1_exploratory_research.json",
            excluded_keys={"hypotheses"},
        ),
        "v4_2": _optional_compact_report(
            report_dir / "v4_2_microstructure_research.json",
            excluded_keys={"tests"},
        ),
        "v4_4": _optional_compact_report(
            report_dir / "v4_4_factor_research.json",
        ),
        "v5": _optional_compact_report(
            report_dir / "v5_limit_up_factor_research.json",
        ),
        "v6": _optional_compact_report(
            report_dir / "v6_non_limit_max_effect_research.json",
        ),
        "v13": _optional_compact_report(
            report_dir / "v13_open_source_factor_research.json",
            excluded_keys={"candidates"},
        ),
        "v14": _optional_compact_report(
            report_dir / "v14_margin_balance_acceleration.json",
        ),
        "v15": _optional_compact_report(
            report_dir / "v15_margin_eligibility_factor_research.json",
        ),
        "v17": _optional_compact_report(
            report_dir / "v17_limit_down_liquidity_recovery.json",
        ),
        "v18": _optional_compact_report(
            report_dir / "v18_open_source_factor_research.json",
            excluded_keys={"candidates"},
        ),
        "v25": _optional_compact_report(
            report_dir / "v25_structural_factor_research.json",
        ),
    }
    return f"""请对现有因子研究做经济机制和稳健性复核，不得新增无边界参数搜索。

【各轮真实研究摘要】
{json.dumps(summaries, ensure_ascii=False, indent=2)}

请输出：
1. 哪些因子可能重复表达同一风险暴露，依据是什么；
2. 哪些结果只具统计意义、尚无经济或可交易意义；
3. 牛市、熊市、震荡市中的方向稳定性及当前证据缺口；
4. 多重检验、滚动分段、置信区间和失败实验记录是否足以防止挑最好结果；
5. 最多3项只改变一个变量的稳健性复核，不得宣称未运行的结果。
"""


def _build_v120_discovery_evidence(project_root: Path) -> str:
    """Bind discovery to the audited PBoC epoch without exposing returns."""
    report_dir = project_root / "reports"
    audit = _optional_compact_report(
        report_dir / "v120_pbc_liquidity_data_epoch_audit.json"
    )
    status = _optional_compact_report(
        report_dir / "v120_successor_research_status.json"
    )
    v90 = _optional_compact_report(
        report_dir / "v90_successor_research_status.json"
    )
    v104 = _optional_compact_report(
        report_dir / "v104_prebacktest_admission_gate.json"
    )
    v121 = _optional_compact_report(
        report_dir / "v121_pbc_m2_acceleration_gate.json"
    )
    v142 = _optional_compact_report(
        report_dir / "v142_pbc_interbank_3pct_prebacktest_gate.json"
    )
    csv_path = (
        project_root / "research" / "data" / "v120_pbc_liquidity"
        / "monthly_financial_statistics.csv"
    )
    fields = (
        "month", "publication_at", "m2_yoy_pct", "m1_yoy_pct",
        "rmb_loan_yoy_pct", "interbank_lending_rate_pct",
        "pledged_repo_rate_pct",
    )
    try:
        with csv_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        liquidity_csv = "\n".join([
            ",".join(fields),
            *(",".join(row[field] for field in fields) for row in rows),
        ])
    except (OSError, KeyError, csv.Error):
        liquidity_csv = "人民银行物理CSV不可读；本轮必须输出NEW_DATA_REQUIRED。"
    epoch = {
        "data_epoch_id": audit.get("data_epoch_id"),
        "new_data_epoch_ready": audit.get("new_data_epoch_ready") is True,
        "month_range": audit.get("month_range"),
        "actual_months": audit.get("actual_months"),
        "listing_archives": audit.get("listing_archives"),
        "report_archives": audit.get("report_archives"),
        "quality_gates": audit.get("gates") or {},
        "discovery_round": status.get("discovery_round"),
        "round_limit": status.get("maximum_discovery_rounds_per_epoch"),
        "source": "People's Bank of China Financial Statistics Reports",
    }
    sealed_history = {
        "v90_rounds": v90.get("discovery_round"),
        "v90_cap_reached": v90.get("discovery_round_cap_reached") is True,
        "v90_actionable_candidate": v90.get("actionable_candidate") is True,
        "v104_status": v104.get("status"),
        "v104_failed_gates": v104.get("failed_gates") or [],
        "v104_return_data_read": v104.get("return_data_read") is True,
        "v121_candidate_id": v121.get("candidate_id"),
        "v121_status": v121.get("status"),
        "v121_failed_gates": v121.get("failed_gates") or [],
        "v121_test_10bp": (
            ((v121.get("results") or {}).get("test") or {}).get("cost_10bp")
        ),
        "v142_candidate_id": v142.get("candidate_id"),
        "v142_status": v142.get("status"),
        "v142_trigger_months": v142.get("trigger_months"),
        "v142_failed_gates": v142.get("failed_gates") or [],
    }
    return f"""【V120中国人民银行月度金融统计数据纪元】
{json.dumps(epoch, ensure_ascii=False, indent=2)}

【已封存历史；不得迁移或重包装】
{json.dumps(sealed_history, ensure_ascii=False, indent=2)}

V90国家统计局PMI纪元已经完成30轮且没有可执行候选；V104在读取收益前因信号过稀
和阈值无独立先验被淘汰。不得把任何PMI字段、阈值、窗口、方向或资产变体迁移到
本纪元，也不得用价格或收益挑选货币指标阈值。

若V121已有结果，其`M2同比月度加速度>0`机制及其阈值、方向、持有期、切分、资产
和组合变体均已永久封存，不得重提或补救。
若V142已有结果，其`同业拆借月均利率>3.0%`零触发机制及阈值、变量和组合变体也已
永久封存，不得把3.0%换成样本内可触发阈值后重提。

当前唯一新增输入是中国人民银行2018-01至2025-12的96/96个月度金融统计：M2同比、
M1同比、人民币贷款余额同比、同业拆借月加权平均利率、质押式回购月加权平均利率，
以及每期官方发布时间。18/18目录页、96/96月报页、96/96内容归档和CSV均已做
SHA-256验收。M1在2025年发生统计口径变化；任何使用M1的候选必须把结构断点写入
固定规格，禁止把新旧口径直接拼接后设阈值。

【已验收字段；只作机制设计，不是盈利证据】
{liquidity_csv}

只允许提出一个货币或信用传导机制。公式、方向、公告后最早执行时点、唯一阈值
先验、成本、样本切分和失败条件必须在读取价格收益前冻结。候选阈值必须有样本
分布之外的先验来源。若96个月仍不足以支持
可识别且不依赖样本分布调参的机制，必须输出`点时数据：NEW_DATA_REQUIRED`；不得
输出个股、仓位、订单、自动交易或声称未来盈利。
"""


def _build_v143_discovery_evidence(project_root: Path) -> str:
    """Bind discovery to audited CPI/PPI values without exposing returns."""
    report_dir = project_root / "reports"
    audit = _optional_compact_report(
        report_dir / "v143_nbs_cpi_ppi_data_epoch_audit.json"
    )
    status = _optional_compact_report(
        report_dir / "v143_successor_research_status.json"
    )
    v144 = _optional_compact_report(
        report_dir / "v144_nbs_ppi_cpi_gap_prebacktest_gate.json"
    )
    csv_path = (
        project_root / "research" / "data" / "v143_nbs_cpi_ppi"
        / "monthly_cpi_ppi.csv"
    )
    fields = (
        "month", "joint_available_at", "cpi_yoy_pct", "ppi_yoy_pct",
        "ppi_minus_cpi_yoy_pct",
    )
    try:
        with csv_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        values = "\n".join([
            ",".join(fields),
            *(",".join(row[field] for field in fields) for row in rows),
        ])
    except (OSError, KeyError, csv.Error):
        values = "V143物理CSV不可读；本轮必须输出NEW_DATA_REQUIRED。"
    epoch = {
        "data_epoch_id": audit.get("data_epoch_id"),
        "new_data_epoch_ready": audit.get("new_data_epoch_ready") is True,
        "coverage_start": audit.get("coverage_start"),
        "coverage_end": audit.get("coverage_end"),
        "actual_months": audit.get("actual_months"),
        "actual_releases": audit.get("actual_releases"),
        "search_index_fallback_releases": audit.get(
            "official_search_index_fallback_releases"
        ),
        "conservative_next_day_boundaries": audit.get(
            "conservative_next_day_boundaries"
        ),
        "quality_gates": audit.get("gates") or {},
        "discovery_round": status.get("discovery_round"),
        "round_limit": status.get("maximum_discovery_rounds_per_epoch"),
        "sealed_v144_candidate_id": v144.get("candidate_id"),
        "sealed_v144_failed_gates": v144.get("failed_gates") or [],
    }
    return f"""【V143国家统计局CPI/PPI数据纪元】
{json.dumps(epoch, ensure_ascii=False, indent=2)}

当前唯一新增输入是国家统计局2016-01至2025-12的120/120个月度CPI同比、PPI同比、
PPI减CPI同比差和点时可用边界。240/240发布及40份官网搜索响应均已做SHA-256；
只有发布日期而没有可信分钟值的记录一律延后到次日00:00可用，禁止用发布日期
00:01等索引时间制造盘前可得性。该CSV不含股票、指数、收益、IC或回测结果。

V90 PMI双扩张开关、V121 M2加速度、V142同业拆借3%、V144 PPI-CPI正缺口沪深300
风险关闭及其方向、阈值、窗口、资产和组合变体均已封存，不能迁移、反转或重包装。
V144虽有44个触发月，但只有2个独立正缺口阶段，而且没有直接支持沪深300择时方向
的外部来源，已在读取收益前淘汰。只允许提出一个不同且有独立经济依据的通胀传导
机制。阈值必须来自样本分布之外的数学或公开制度先验；不能观察这120个月的分位数、
均值或极值后设阈值。若仅凭CPI/PPI无法识别盈利机制，必须输出NEW_DATA_REQUIRED。

【已验收字段；只作机制设计，不是盈利证据】
{values}

公式、方向、联合可用边界后的最早执行时点、唯一阈值先验、成本、样本切分和失败
条件必须在读取价格收益前冻结。不得输出个股、仓位、订单、自动交易或承诺盈利。
"""


def _build_v145_discovery_evidence(project_root: Path) -> str:
    """Bind discovery to audited industrial profit data without returns."""
    report_dir = project_root / "reports"
    audit = _optional_compact_report(
        report_dir / "v145_nbs_industrial_profit_data_epoch_audit.json"
    )
    status = _optional_compact_report(
        report_dir / "v145_successor_research_status.json"
    )
    v146 = _optional_compact_report(
        report_dir / "v146_nbs_industrial_profit_sign_prebacktest_gate.json"
    )
    csv_path = (
        project_root / "research" / "data" / "v145_nbs_industrial_profit"
        / "industrial_profit_ytd.csv"
    )
    fields = (
        "period_end_month", "available_at", "industrial_profit_ytd_yoy_pct",
        "reporting_basis",
    )
    try:
        with csv_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        values = "\n".join([
            ",".join(fields),
            *(",".join(row[field] for field in fields) for row in rows),
        ])
    except (OSError, KeyError, csv.Error):
        values = "V145物理CSV不可读；本轮必须输出NEW_DATA_REQUIRED。"
    epoch = {
        "data_epoch_id": audit.get("data_epoch_id"),
        "new_data_epoch_ready": audit.get("new_data_epoch_ready") is True,
        "coverage_start": audit.get("coverage_start"),
        "coverage_end": audit.get("coverage_end"),
        "actual_observations": audit.get("actual_observations"),
        "search_archives": audit.get("search_archives"),
        "release_archives": audit.get("release_archives"),
        "official_interpretation_fallbacks": audit.get(
            "official_interpretation_fallbacks"
        ),
        "quality_gates": audit.get("gates") or {},
        "discovery_round": status.get("discovery_round"),
        "round_limit": status.get("maximum_discovery_rounds_per_epoch"),
        "sealed_v146_candidate_id": v146.get("candidate_id"),
        "sealed_v146_failed_gates": v146.get("failed_gates") or [],
    }
    return f"""【V145国家统计局规模以上工业企业利润数据纪元】
{json.dumps(epoch, ensure_ascii=False, indent=2)}

当前唯一新增输入是国家统计局2016—2025年每年1—2月至1—11月的累计利润同比和
全年利润同比，共110/110个点时观测。50份政府搜索响应、110份官网页面和CSV均已
做SHA-256验收；2016年1—2月只使用同日国家统计局工业司官方解读披露的4.8%，已
单独标记。只有发布日期的记录延后到次日00:00可用。该CSV不含股票、指数、收益、
IC或回测结果。

所有非12月数值都是年初至当期的累计同比，不是单月同比；禁止把连续累计值伪装成
独立月度事件。2021年“增长N倍”已按N×100%保存。V144 PPI-CPI正缺口沪深300风
险关闭及其方向、阈值、窗口和资产变体已封存，不能重包装、反转或改资产重试。
V146“工业利润同比为正时持有沪深300”也已在读取收益前淘汰：训练段只有一个独立
负利润阶段，全国规上工业企业总体不等于沪深300，且公司层面盈利溢价文献不能直接
证明宽基指数择时。其阈值、方向、资产和执行变体同样封存。

【已验收字段；只作机制设计，不是盈利证据】
{values}

只允许提出一个与工业实际盈利直接相关、具有样本外阈值先验的机制。公式、方向、
公告后的最早执行、资产映射、成本、样本切分和失败条件必须在读取收益前冻结。若
全国规上工业利润不能直接映射到可交易资产，必须输出NEW_DATA_REQUIRED；不得输
出个股、仓位、订单、自动交易或承诺盈利。
"""


def build_continuous_research_prompts(project_root: Path) -> list[dict]:
    """Build bounded, read-only review tasks for each full cycle."""
    prompt_builders = {
        "code_data_leakage": _build_code_data_audit_prompt,
        "v4_4_strategy_red_team": _build_v4_4_strategy_red_team_prompt,
        "v4_3_gate_adjudication": _build_v4_3_gate_adjudication_prompt,
        "next_factor_hypotheses": _build_next_factor_hypotheses_prompt,
        "cost_execution_audit": _build_cost_execution_audit_prompt,
        "factor_robustness_audit": _build_factor_robustness_audit_prompt,
    }
    return [
        {
            **task,
            "prompt": prompt_builders[task["task_id"]](project_root),
            "mode": "read_only_inference_not_training",
        }
        for task in CONTINUOUS_REVIEW_TASKS
    ]


def build_continuous_discovery_prompts(
    project_root: Path,
    *,
    round_number: int,
    data_epoch_id: str | None = None,
) -> list[dict]:
    """Build a new, versioned hypothesis round after stable audits finish.

    These tasks may propose research specifications, but they cannot edit code,
    run a backtest, change a gate, or unlock trading.
    """
    report_dir = project_root / "reports"
    epoch_key = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in (data_epoch_id or "legacy")
    )[:96]
    history_dir = report_dir / "deepseek_discovery_rounds" / epoch_key
    # Keep a compact mechanism-only history.  Taking the last two arbitrary
    # filenames often selected referee reports from one round and hid the
    # actual proposals, allowing the scout to rename the same mechanism.
    recent_paths = (
        sorted(
            history_dir.glob("round_*_mechanism_scout.md"),
            key=lambda path: path.name,
        )[-8:]
        if history_dir.exists()
        else []
    )
    recent_history = "\n\n".join(
        f"【{path.name}】\n{_optional_text(path, max_chars=900)}"
        for path in recent_paths
    ) or "尚无连续探索历史；本轮必须从已封存V4–V6失败族之外开始。"
    latest_assessment = _optional_compact_report(
        report_dir / "DEEPSEEK_DISCOVERY_ASSESSMENT_LATEST.json"
    )
    assessment_for_epoch = (
        latest_assessment
        if latest_assessment.get("data_epoch_id") == data_epoch_id else {}
    )
    local_admission = _optional_compact_report(
        report_dir / "v104_prebacktest_admission_gate.json"
    )
    local_v120_reports = [
        _optional_compact_report(path)
        for path in (
            report_dir / "v121_pbc_m2_acceleration_gate.json",
            report_dir / "v142_pbc_interbank_3pct_prebacktest_gate.json",
            report_dir / "v144_nbs_ppi_cpi_gap_prebacktest_gate.json",
            report_dir / "v146_nbs_industrial_profit_sign_prebacktest_gate.json",
        )
    ]
    v104_rejected = bool(
        assessment_for_epoch.get("candidate_ids")
        == [local_admission.get("candidate_id")]
        and local_admission.get("preregistration_admission_passed") is False
    )
    local_v120_rejection = next((
        report for report in local_v120_reports
        if assessment_for_epoch.get("candidate_ids") == [report.get("candidate_id")]
        and (
            report.get("preregistration_admission_passed") is False
            or (
                report.get("historical_strategy_gate_passed") is False
                and report.get("single_backtest_consumed") is True
            )
        )
    ), {})
    locally_rejected = v104_rejected or bool(local_v120_rejection)
    local_rejection = local_v120_rejection or local_admission
    assessment_feedback = {
        "data_epoch_id": assessment_for_epoch.get("data_epoch_id"),
        "discovery_round": assessment_for_epoch.get("discovery_round"),
        "candidate_ids": assessment_for_epoch.get("candidate_ids") or [],
        "actionable_candidate": (
            assessment_for_epoch.get("actionable_candidate") is True
            and not locally_rejected
        ),
        "reasons": (
            local_rejection.get("reasons")
            or local_rejection.get("failed_gates") or []
            if locally_rejected else assessment_for_epoch.get("reasons") or []
        ),
        "registry_verdict": (
            "REJECT_LOCAL_PREREGISTRATION"
            if local_v120_rejection.get("preregistration_admission_passed") is False else
            "REJECT_LOCAL_HISTORICAL_GATE" if local_v120_rejection else
            "REJECT_LOCAL_PREREGISTRATION"
            if locally_rejected else
            (assessment_for_epoch.get("open_source_audit") or {}).get(
                "registry_verdict"
            )
        ),
        "repackaging_review": (
            assessment_for_epoch.get("open_source_audit") or {}
        ).get("repackaging_review"),
    }
    is_v89 = (data_epoch_id or "").startswith("sse_regulatory_measures_")
    is_v90 = (data_epoch_id or "").startswith("nbs_monthly_pmi_")
    is_v120 = (data_epoch_id or "").startswith("pbc_financial_statistics_")
    is_v143 = (data_epoch_id or "").startswith("nbs_cpi_ppi_")
    is_v145 = (data_epoch_id or "").startswith("nbs_industrial_profit_")
    evidence = (
        _build_v89_discovery_evidence(project_root) if is_v89 else
        _build_v90_discovery_evidence(project_root) if is_v90 else
        _build_v120_discovery_evidence(project_root) if is_v120 else
        _build_v143_discovery_evidence(project_root) if is_v143 else
        _build_v145_discovery_evidence(project_root) if is_v145 else
        _build_continuous_discovery_evidence(project_root)
    )
    epoch_focus = (
        "本轮唯一新增数据必须是V89上交所监管措施直接代码纪元；不得继续提出"
        "依赖V88人工盲审的证监会全文候选。"
        if is_v89 else
        "本轮唯一新增数据必须是V90国家统计局PMI纪元；已失败的双扩张风险开关及其"
        "阈值、方向、窗口、样本、成本或资产变体一律视为重包装。"
        if is_v90
        else
        "本轮唯一新增数据必须是V120中国人民银行月度金融统计纪元；不得迁移PMI"
        "失败机制，M1的2025年统计口径断点必须显式处理。"
        if is_v120
        else
        "本轮唯一新增数据必须是V143国家统计局CPI/PPI纪元；不得迁移PMI、M2加速度"
        "或利率阈值失败族，只有日期的发布必须使用次日保守边界。"
        if is_v143
        else
        "本轮唯一新增数据必须是V145国家统计局工业利润纪元；不得把累计同比当单月值，"
        "不得迁移或反转V144失败的PPI-CPI权益方向。"
        if is_v145
        else "本轮候选必须使用当前data_epoch_id对应的数据，不得冒用其他未验收纪元。"
    )
    scout_instruction = (
        "提出一个只读、不可执行的学术证伪规格，只定义V89公开事件元数据与冻结基线"
        "组合差异之间的可检验关系；不得输出证券、仓位或操作。"
        if is_v89 else
        "提出一个只读、不可执行的学术证伪规格，只使用V90已验收的官方PMI字段和"
        "真实发布时间；必须与已封存双扩张风险开关具有不同经济机制，不得输出证券、"
        "仓位或操作。"
        if is_v90 else
        "提出一个只读、不可执行的学术证伪规格，只使用V120已验收的人民银行月度"
        "金融统计字段和真实发布时间；阈值必须有样本分布之外的先验，不得输出证券、"
        "仓位或操作。"
        if is_v120 else
        "提出一个只读、不可执行的学术证伪规格，只使用V143已验收的CPI/PPI同比"
        "字段和点时可用边界；阈值必须有样本分布之外的先验，不得输出证券、仓位或操作。"
        if is_v143 else
        "提出一个只读、不可执行的学术证伪规格，只使用V145已验收的工业利润累计同比"
        "和点时边界；必须处理累计值依赖与资产映射，不得输出证券、仓位或操作。"
        if is_v145 else
        "提出一个只读、不可执行的学术证伪规格，只使用当前已验收数据纪元；不得输出"
        "证券、仓位或操作。"
    )
    common = f"""

【连续研究轮次】
数据纪元 `{data_epoch_id or 'legacy'}` 第 {round_number} 轮。上一轮完成后立即启动
本轮，不代表门禁更容易通过；每个数据纪元最多30轮。

【最近连续探索记录；不得重复其中已经提出的机制】
{recent_history}

【上一轮机器裁决；被拒绝的机制与变体不得再次提出】
{json.dumps(assessment_feedback, ensure_ascii=False, indent=2)}

【当前数据纪元焦点】
{epoch_focus}

本轮只能提出一个主要机制。若当前日线字段不足以无泄漏检验，必须明确输出
“需要新数据”，并逐项列出字段、真实发布时间、历史覆盖与点时可得性要求；
不得用旧因子交互项、换窗口、换持有期或子样本包装成新机制。

所有角色还必须给出两项独立硬裁决：`方向一致性：PASS|FAIL`和
`重包装审查：CLEAR|REJECT`。标签到组合的映射只允许两种：正标签用于长仓纳入，
负标签只用于事前排除或降权；其他映射必须FAIL。若候选只是从已失败
或未验收的同一事件族切子类、换名字、换窗口/阈值，重包装审查必须REJECT。
"""
    roles = (
        {
            "task_id": "mechanism_scout",
            "title": "全新经济机制侦察",
            "max_tokens": 12000,
            "system_prompt": FACTOR_HYPOTHESIS_SYSTEM_PROMPT,
            "instruction": (
                scout_instruction + "该规格必须与既有失败族不同且只有一个机制。"
                "输出第一行必须是`因子ID：唯一英文编号`，第二行必须是"
                "`点时数据：READY`或`点时数据：NEW_DATA_REQUIRED`，第三行必须是"
                "`方向一致性：PASS`或`方向一致性：FAIL`，第四行必须是"
                "`重包装审查：CLEAR`或`重包装审查：REJECT`。"
            ),
        },
        {
            "task_id": "data_feasibility",
            "title": "点时数据可行性审查",
            "max_tokens": 10000,
            "system_prompt": CODE_DATA_AUDIT_SYSTEM_PROMPT,
            "instruction": (
                "只审查随后提供的本轮侦察候选，不得改审上一轮候选。"
                "独立判断它能否由当前已验收数据纪元字段支持；如不能，设计"
                "最小新增数据集和防幸存者偏差、防公告回填的验收规则。"
                "输出第一行必须原样重复侦察候选的因子ID，第二行必须是"
                "`点时数据：READY`或`点时数据：NEW_DATA_REQUIRED`，第三、四行"
                "必须分别给出`方向一致性：PASS|FAIL`和`重包装审查：CLEAR|REJECT`。"
            ),
        },
        {
            "task_id": "falsification_referee",
            "title": "预注册证伪裁判",
            "max_tokens": 10000,
            "system_prompt": FACTOR_ROBUSTNESS_SYSTEM_PROMPT,
            "instruction": (
                "只审查随后提供的本轮侦察候选，不得改审上一轮候选。"
                "针对该机制设计固定的时间切分、成本、多重检验、经济显著性"
                "和失败即封存条件，并主动识别它是否只是旧机制重包装。"
                "输出第一行必须原样重复侦察候选的因子ID，第二行必须是"
                "`点时数据：READY`或`点时数据：NEW_DATA_REQUIRED`，第三、四行"
                "必须分别给出`方向一致性：PASS|FAIL`和`重包装审查：CLEAR|REJECT`。"
            ),
        },
        {
            "task_id": "open_source_registry_referee",
            "title": "GitHub来源、许可证、重复机制和未来函数审查",
            "max_tokens": 9000,
            "system_prompt": CODE_DATA_AUDIT_SYSTEM_PROMPT,
            "instruction": (
                "只审查随后提供的本轮侦察候选，不得改审V18或上一轮候选。"
                "核对该候选的论文、官方数据或开源实现来源、许可证或使用"
                "边界、固定公式、所需字段、与旧失败机制的重复度及前视风险。"
                "输出第一行必须原样重复侦察候选的因子ID，第二行必须是"
                "`注册裁决：ACCEPT`或`注册裁决：REJECT`，第三、四行必须分别给出"
                "`方向一致性：PASS|FAIL`和`重包装审查：CLEAR|REJECT`；没有可核验来源、"
                "与已有候选重复、许可证/使用边界不清或存在未来函数时必须"
                "拒绝。不得改公式、调窗口、挑持有期，也不得替代本地回测。"
            ),
        },
    )
    return [
        {
            **role,
            "prompt": (
                evidence
                + common
                + "\n\n【本路独立任务】\n"
                + role["instruction"]
                + "\n不得声称已经运行回测，不得承诺解锁日期或收益。"
            ),
            "mode": "continuous_read_only_discovery_not_training",
        }
        for role in roles
    ]
