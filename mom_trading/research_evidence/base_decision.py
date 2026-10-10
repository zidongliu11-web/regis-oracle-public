"""Base decision and profitability evidence, isolated from Flask routing.

The app passes its current namespace so tests and project-root overrides keep working.
"""
from __future__ import annotations

import json


def build_base_decision_gate_status(context: dict[str, object]) -> dict[str, object]:
    _PAPER_FORWARD_STATUS = context['_PAPER_FORWARD_STATUS']
    _STRATEGY_REGISTRY = context['_STRATEGY_REGISTRY']
    _V86_SELECTION_BENCHMARK_GATE = context['_V86_SELECTION_BENCHMARK_GATE']
    _V86_MULTI_BENCHMARK_GATE = context['_V86_MULTI_BENCHMARK_GATE']
    _V86_CONTRIBUTOR_CONCENTRATION_GATE = context['_V86_CONTRIBUTOR_CONCENTRATION_GATE']
    _V86_MODEL_TRAINING_INTEGRITY_GATE = context['_V86_MODEL_TRAINING_INTEGRITY_GATE']
    _V87_SUCCESSOR_ADMISSION_GATE = context['_V87_SUCCESSOR_ADMISSION_GATE']
    _V87_CSRC_MAPPING_FAILURE = context['_V87_CSRC_MAPPING_FAILURE']
    _V88_CSRC_MAPPING_AUDIT = context['_V88_CSRC_MAPPING_AUDIT']
    _V89_SSE_DATA_EPOCH_AUDIT = context['_V89_SSE_DATA_EPOCH_AUDIT']
    _read_json = context['_read_json']
    _V90_SUCCESSOR_STATUS = context['_V90_SUCCESSOR_STATUS']
    _V120_SUCCESSOR_STATUS = context['_V120_SUCCESSOR_STATUS']
    _V143_SUCCESSOR_STATUS = context['_V143_SUCCESSOR_STATUS']
    _V145_SUCCESSOR_STATUS = context['_V145_SUCCESSOR_STATUS']
    _PROJECT = context['_PROJECT']
    _latest_paper_forward_report = context['_latest_paper_forward_report']
    """One fail-closed truth source for every execution-shaped response."""
    try:
        paper = json.loads(_PAPER_FORWARD_STATUS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        paper = {}
    try:
        registry = json.loads(_STRATEGY_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        registry = {}
    safety = paper.get("safety") or {}
    eligible = all((
        registry.get("recommendation_gate_passed") is True,
        bool(registry.get("active_strategy")),
        paper.get("strategy_gate_passed") is True,
        paper.get("strict_product_gate_certificate_valid") is True,
        paper.get("product_forward_clock_valid") is True,
        paper.get("paper_gate_passed") is True,
        safety.get("real_money_trading") is True,
    ))
    historical_passed = registry.get("historical_gate_passed") is True
    lock_reason = (
        registry.get("live_lock_reason")
        or registry.get("recommendation_lock_reason")
    )
    try:
        selection_benchmark = json.loads(_V86_SELECTION_BENCHMARK_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        selection_benchmark = {}
    if selection_benchmark.get("selection_benchmark_gate_passed") is False:
        lock_reason = (
            "当前无有效历史候选；V86 仍缺滚动样本外、自然环境、50bp成本容量、"
            "公司行动结算、时序区组、研究选择偏差、基准超额收益或外部签名证据。"
        )
    try:
        multi_benchmark = json.loads(_V86_MULTI_BENCHMARK_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        multi_benchmark = {}
    if multi_benchmark.get("multi_benchmark_gate_passed") is False:
        lock_reason = (
            "当前无有效历史候选；V86 仍缺滚动样本外、自然环境、50bp成本容量、公司行动结算、"
            "时序区组、研究选择偏差、沪深300/中证500/中证1000多基准稳健性或外部签名证据。"
        )
    try:
        concentration_gate = json.loads(_V86_CONTRIBUTOR_CONCENTRATION_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        concentration_gate = {}
    if concentration_gate.get("contributor_concentration_gate_passed") is False:
        lock_reason = (
            "当前无有效历史候选；V86 仍缺滚动样本外、自然环境、50bp成本容量、公司行动结算、"
            "时序区组、研究选择偏差、沪深300/中证500/中证1000多基准、"
            "收益贡献集中度或外部签名证据。"
        )
    try:
        training_integrity = json.loads(_V86_MODEL_TRAINING_INTEGRITY_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        training_integrity = {}
    if training_integrity.get("model_training_integrity_gate_passed") is False:
        lock_reason = (
            "当前无有效历史候选；V86 训练标签跨越验证边界，净化后验证稳定性失败；"
            "滚动样本外、自然环境、50bp成本容量、公司行动结算、时序区组、研究选择偏差、"
            "沪深300/中证500/中证1000多基准、收益贡献集中度及外部签名证据也未成熟。"
        )
    try:
        successor_admission = json.loads(_V87_SUCCESSOR_ADMISSION_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        successor_admission = {}
    try:
        mapping_failure = json.loads(_V87_CSRC_MAPPING_FAILURE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        mapping_failure = {}
    try:
        mapping_audit = json.loads(_V88_CSRC_MAPPING_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        mapping_audit = {}
    try:
        v89_epoch = json.loads(_V89_SSE_DATA_EPOCH_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        v89_epoch = {}
    if successor_admission.get("successor_admission_gate_passed") is False:
        if (
            successor_admission.get("new_data_epoch_ready") is True
            and successor_admission.get("successor_discovery_allowed") is True
            and successor_admission.get("discovery_round_cap_reached") is not True
        ):
            if (
                mapping_failure.get("mapping_invalidated") is True
                and mapping_audit.get("mapping_data_ready") is False
            ):
                manual = mapping_audit.get("manual_review") or {}
                lock_reason = (
                    "当前无有效历史候选；V86 已因训练标签跨界和净化稳定性失败淘汰；"
                    "证监会新数据纪元已验收，V87全文映射因罚款账户模板污染已封存。"
                    "V88限定区块映射仍待人工盲审"
                    f"{manual.get('completed', 0)}/{manual.get('required', 200)}；"
                    f"上交所V89监管措施纪元已验收{v89_epoch.get('record_count', 0)}条，"
                    f"其中直接唯一A股代码{v89_epoch.get('direct_single_a_share_records', 0)}条，"
                    "当前只允许机制发现；"
                    "当前仅允许后继机制发现，回测、推荐、订单和自动交易全部关闭。"
                )
            else:
                lock_reason = (
                    "当前无有效历史候选；V86 已因训练标签跨界和净化稳定性失败淘汰；"
                    "新数据纪元已验收，仅允许后继机制发现。尚无四路唯一候选共识与"
                    "完整回测前预注册，后继回测、推荐、订单和自动交易全部关闭。"
                )
        else:
            lock_reason = (
                "当前无有效历史候选；V86 已因训练标签跨界和净化稳定性失败淘汰；"
                f"同一数据纪元机制发现已达 {successor_admission.get('discovery_round', 0)}/"
                f"{successor_admission.get('maximum_discovery_rounds_per_epoch', 30)} 轮，"
                "尚无全新已授权点时数据；后继发现、回测、推荐、订单和自动交易全部关闭。"
            )
    v90_successor = _read_json(_V90_SUCCESSOR_STATUS)
    if v90_successor.get("new_data_epoch_ready") is True:
        lock_reason = str(
            v90_successor.get("recommendation_lock_reason") or lock_reason
        )
    v120_successor = _read_json(_V120_SUCCESSOR_STATUS)
    if v120_successor.get("new_data_epoch_ready") is True:
        lock_reason = str(
            v120_successor.get("recommendation_lock_reason") or lock_reason
        )
    v143_successor = _read_json(_V143_SUCCESSOR_STATUS)
    if v143_successor.get("new_data_epoch_ready") is True:
        lock_reason = str(
            v143_successor.get("recommendation_lock_reason") or lock_reason
        )
    v145_successor = _read_json(_V145_SUCCESSOR_STATUS)
    if v145_successor.get("new_data_epoch_ready") is True:
        lock_reason = str(
            v145_successor.get("recommendation_lock_reason") or lock_reason
        )
    v172_result = _read_json(
        _PROJECT
        / "reports"
        / "v172_v170_nbs_industry_profit_breadth_acceleration_factor_test.json"
    )
    if (
        v172_result.get("single_historical_backtest_consumed") is True
        and v172_result.get("historical_strategy_gate_passed") is False
    ):
        train10 = (
            ((v172_result.get("results") or {}).get("train") or {}).get(
                "cost_10bp"
            ) or {}
        )
        observed10 = (
            ((v172_result.get("results") or {}).get("observed_test") or {}).get(
                "cost_10bp"
            ) or {}
        )
        observed20 = (
            ((v172_result.get("results") or {}).get("observed_test") or {}).get(
                "cost_20bp"
            ) or {}
        )
        lock_reason = (
            "当前无有效历史候选；V145/V166工业利润数据和V171中证全指工业行情均已同步验收。"
            "V167因可完成交易数不足在收益计算前淘汰；V170唯一冻结回测已永久失败："
            f"训练10bp {train10.get('total_return_pct', '—')}%，"
            f"观察10bp {observed10.get('total_return_pct', '—')}%，"
            f"观察20bp {observed20.get('total_return_pct', '—')}%，"
            f"最大回撤 {observed10.get('max_drawdown_pct', '—')}%。"
            "推荐、资金分配、订单和自动交易继续关闭。"
        )
    v176_result = _read_json(
        _PROJECT
        / "reports"
        / "v176_v174_nbs_pmi_gap_acceleration_factor_test.json"
    )
    if (
        v176_result.get("single_historical_backtest_consumed") is True
        and v176_result.get("historical_strategy_gate_passed") is False
    ):
        train10 = (
            ((v176_result.get("results") or {}).get("train") or {}).get(
                "cost_10bp"
            ) or {}
        )
        observed10 = (
            ((v176_result.get("results") or {}).get("observed_test") or {}).get(
                "cost_10bp"
            ) or {}
        )
        observed20 = (
            ((v176_result.get("results") or {}).get("observed_test") or {}).get(
                "cost_20bp"
            ) or {}
        )
        lock_reason = (
            "当前无有效历史候选；V145/V166工业利润数据、V171中证全指工业行情、"
            "V173国家统计局PMI六分项和V175中证全指能源行情均已同步验收。"
            "V167因可完成交易数不足在收益计算前淘汰；V170唯一冻结回测已永久失败；"
            "V174唯一冻结回测已永久失败："
            f"训练10bp {train10.get('total_return_pct', '—')}%，"
            f"观察10bp {observed10.get('total_return_pct', '—')}%，"
            f"观察20bp {observed20.get('total_return_pct', '—')}%，"
            f"最大回撤 {observed10.get('max_drawdown_pct', '—')}%。"
            "下一项结构独立机制尚未预注册；推荐、资金分配、订单和自动交易继续关闭。"
        )
    v178_result = _read_json(
        _PROJECT
        / "reports"
        / "v178_sse_disciplinary_event_price_data_epoch_audit.json"
    )
    if (
        v178_result.get("status")
        == "permanently_rejected_before_return_computation"
        and v178_result.get("return_data_computed") is False
    ):
        quality = v178_result.get("quality") or {}
        missing_count = len(quality.get("missing_files") or [])
        lock_reason = (
            "当前无有效历史候选；V145/V166工业利润数据、V171中证全指工业行情、"
            "V173国家统计局PMI六分项、V175中证全指能源行情和V89上交所5500条监管措施数据"
            "均已同步展示。"
            "V170与V174唯一冻结回测已永久失败；V177监管事件持仓风险机制因冻结的"
            f"{quality.get('event_codes', 0)}只事件股票中仅有"
            f"{quality.get('stock_files', 0)}只价格文件、缺少{missing_count}只科创板股票，"
            "已在收益计算前永久淘汰；V178预留的监管事件收益检验从未运行。"
            "下一项结构独立机制尚未预注册；推荐、资金分配、订单和自动交易继续关闭。"
        )
    v180_result = _read_json(
        _PROJECT
        / "reports"
        / "v180_v179_same_calendar_month_seasonality_factor_test.json"
    )
    if (
        v180_result.get("single_factor_test_consumed") is True
        and v180_result.get("historical_factor_gate_passed") is False
    ):
        splits = v180_result.get("splits") or {}
        train = ((splits.get("training_audit") or {}).get("metrics") or {})
        observed = ((splits.get("observed_test") or {}).get("metrics") or {})
        lock_reason = (
            "当前无有效历史候选；全部研究报告已同步展示，但同步不等于可交易。"
            "V179同历月收益季节性已完成唯一冻结因子检验并永久失败："
            f"训练Rank IC {train.get('mean_rank_ic', '—')}，"
            f"训练20BP月度多空 {train.get('mean_d10_minus_d1_after_20bp', '—')}，"
            f"观察10BP月度多空 {observed.get('mean_d10_minus_d1_after_10bp', '—')}，"
            f"观察单调性 {observed.get('mean_monotonicity', '—')}，"
            f"训练/观察Bootstrap下限 {train.get('bootstrap_95pct_lower', '—')}/"
            f"{observed.get('bootstrap_95pct_lower', '—')}。"
            "观察段局部正收益不能覆盖训练反向、成本和稳定性失败；下一候选尚未预注册。"
            "推荐、资金分配、订单和自动交易继续关闭。"
        )
    v182_result = _read_json(
        _PROJECT
        / "reports"
        / "v182_v181_annual_filing_timeliness_factor_test.json"
    )
    if (
        v182_result.get("single_factor_test_consumed") is True
        and v182_result.get("historical_factor_gate_passed") is False
    ):
        splits = v182_result.get("splits") or {}
        train = ((splits.get("training_audit") or {}).get("metrics") or {})
        observed = ((splits.get("observed_test") or {}).get("metrics") or {})
        lock_reason = (
            "当前无有效历史候选；全部研究报告已同步展示，但同步不等于可交易。"
            "V181年度报告披露时效变化已完成唯一冻结因子检验并永久失败："
            f"训练Rank IC {train.get('mean_rank_ic', '—')}，"
            f"训练10BP多空 {train.get('mean_d10_minus_d1_after_10bp', '—')}，"
            f"观察10BP多空 {observed.get('mean_d10_minus_d1_after_10bp', '—')}，"
            f"观察单调性 {observed.get('mean_monotonicity', '—')}，"
            f"观察留一年最差均值 {observed.get('leave_one_year_out_worst_mean_after_10bp', '—')}。"
            "观察段局部正收益不能覆盖训练反向和跨年稳定性失败；下一候选尚未预注册。"
            "推荐、资金分配、订单和自动交易继续关闭。"
        )
    v185_result = _read_json(
        _PROJECT
        / "reports"
        / "v185_cninfo_pre_ex_dividend_drift_factor_test.json"
    )
    if (
        v185_result.get("single_factor_test_consumed") is True
        and v185_result.get("historical_factor_gate_passed") is False
    ):
        results = v185_result.get("results") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        lock_reason = (
            "当前无有效历史候选；V184巨潮资讯现金分红数据纪元已完整验收。"
            "V185除息日前三日漂移已完成唯一冻结成本后检验并永久失败："
            f"验证均值 {validation.get('mean_net_return_pct', '—')}%，"
            f"Bootstrap下限 {validation.get('block_bootstrap_95pct_lower_pct', '—')}%，"
            f"胜率 {validation.get('win_rate_pct', '—')}%；"
            f"测试均值 {test.get('mean_net_return_pct', '—')}%，"
            f"市场调整 {test.get('mean_market_adjusted_return_pct', '—')}%，"
            f"Bootstrap下限 {test.get('block_bootstrap_95pct_lower_pct', '—')}%，"
            f"胜率 {test.get('win_rate_pct', '—')}%。"
            "没有策略通过样本外、成本和稳定性门禁；买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v187_result = _read_json(
        _PROJECT
        / "reports"
        / "v187_cninfo_open_market_repurchase_drift_factor_test.json"
    )
    if (
        v187_result.get("single_factor_test_consumed") is True
        and v187_result.get("historical_factor_gate_passed") is False
    ):
        results = v187_result.get("results") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        validation_yearly = validation.get("yearly_mean_net_return_20bp_pct") or {}
        lock_reason = (
            "当前无有效历史候选；V186巨潮集中竞价回购公告数据纪元已完整验收。"
            "V187公告后20交易日唯一冻结成本后检验永久失败："
            f"验证均值 {validation.get('mean_net_return_20bp_pct', '—')}%，"
            f"胜率 {validation.get('win_rate_20bp_pct', '—')}%，"
            f"Bootstrap下限 {validation.get('block_bootstrap_95pct_lower_20bp_pct', '—')}%，"
            f"2023年 {validation_yearly.get('2023', '—')}%；"
            f"测试均值 {test.get('mean_net_return_20bp_pct', '—')}%，"
            f"市场调整 {test.get('mean_market_adjusted_20bp_pct', '—')}%，"
            f"胜率 {test.get('win_rate_20bp_pct', '—')}%，"
            f"Bootstrap下限 {test.get('block_bootstrap_95pct_lower_20bp_pct', '—')}%。"
            "测试期局部强收益不能覆盖验证期、跨年和时间区组稳定性失败；"
            "买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v189_result = _read_json(
        _PROJECT
        / "reports"
        / "v189_cninfo_ipo_lockup_release_reversal_factor_test.json"
    )
    if (
        v189_result.get("single_factor_test_consumed") is True
        and v189_result.get("historical_factor_gate_passed") is False
    ):
        results = v189_result.get("results") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        folds = v189_result.get("test_time_folds_mean_net_return_20bp_pct") or []
        fold_text = "/".join(f"{value}%" for value in folds) or "—"
        lock_reason = (
            "当前无有效历史候选；V188巨潮首发前限售股上市流通公告数据纪元已完整验收。"
            "V189公告后20交易日唯一冻结成本后检验永久失败："
            f"验证均值 {validation.get('mean_net_return_20bp_pct', '—')}%，"
            f"市场调整 {validation.get('mean_market_adjusted_20bp_pct', '—')}%，"
            f"胜率 {validation.get('win_rate_20bp_pct', '—')}%，"
            f"Bootstrap下限 {validation.get('block_bootstrap_95pct_lower_20bp_pct', '—')}%；"
            f"测试均值 {test.get('mean_net_return_20bp_pct', '—')}%，"
            f"市场调整 {test.get('mean_market_adjusted_20bp_pct', '—')}%，"
            f"胜率 {test.get('win_rate_20bp_pct', '—')}%，"
            f"Bootstrap下限 {test.get('block_bootstrap_95pct_lower_20bp_pct', '—')}%，"
            f"三折 {fold_text}。"
            "测试期局部正收益与正Bootstrap下限不能覆盖验证期全面为负和测试首折为负；"
            "买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v191_result = _read_json(
        _PROJECT
        / "reports"
        / "v191_cninfo_major_contract_5d_drift_factor_test.json"
    )
    if (
        v191_result.get("single_factor_test_consumed") is True
        and v191_result.get("historical_factor_gate_passed") is False
    ):
        results = v191_result.get("results") or {}
        training = results.get("training_audit") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        folds = v191_result.get("test_time_folds_mean_net_return_20bp_pct") or []
        fold_text = "/".join(f"{value}%" for value in folds) or "—"
        lock_reason = (
            "当前无有效历史候选；V190巨潮重大合同签订公告数据纪元已完整验收。"
            "V191公告后5交易日唯一冻结成本后检验永久失败："
            f"训练均值 {training.get('mean_net_return_20bp_pct', '—')}%；"
            f"验证均值 {validation.get('mean_net_return_20bp_pct', '—')}%，"
            f"市场调整 {validation.get('mean_market_adjusted_20bp_pct', '—')}%，"
            f"胜率 {validation.get('win_rate_20bp_pct', '—')}%；"
            f"测试均值 {test.get('mean_net_return_20bp_pct', '—')}%，"
            f"市场调整 {test.get('mean_market_adjusted_20bp_pct', '—')}%，"
            f"胜率 {test.get('win_rate_20bp_pct', '—')}%，三折 {fold_text}。"
            "论文所述公告日即时正反应没有转化为公告后开盘可执行收益；"
            "买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v193_result = _read_json(
        _PROJECT
        / "reports"
        / "v193_v4_52_week_high_monthly_factor_test.json"
    )
    if (
        v193_result.get("single_factor_test_consumed") is True
        and v193_result.get("historical_factor_gate_passed") is False
    ):
        results = v193_result.get("results") or {}
        training = results.get("training_audit") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        folds = v193_result.get("test_folds") or []
        fold_text = "/".join(
            f"{fold.get('excess_return_pct', '—')}%" for fold in folds
        ) or "—"
        lock_reason = (
            "当前无有效历史候选；V192已在收益读取前冻结52周高点月度因子的公式、样本、成本和门禁。"
            "V193唯一冻结成本后检验永久失败："
            f"训练年化 {training.get('annualized_net_return_20bp_pct', '—')}%；"
            f"验证年化 {validation.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {validation.get('annualized_excess_return_pct', '—')}%，"
            f"胜率 {validation.get('monthly_win_rate_20bp_pct', '—')}%；"
            f"测试年化 {test.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {test.get('annualized_excess_return_pct', '—')}%，"
            f"总超额 {test.get('total_excess_return_pct', '—')}%，"
            f"胜率 {test.get('monthly_win_rate_20bp_pct', '—')}%，三折超额 {fold_text}。"
            "训练期正收益未延续到验证和测试，不能据此生成买入信号；"
            "买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v195_result = _read_json(
        _PROJECT
        / "reports"
        / "v195_v4_low_volatility_monthly_factor_test.json"
    )
    if (
        v195_result.get("single_factor_test_consumed") is True
        and v195_result.get("historical_factor_gate_passed") is False
    ):
        results = v195_result.get("results") or {}
        training = results.get("training_audit") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        folds = v195_result.get("test_folds") or []
        fold_text = "/".join(
            f"{fold.get('excess_return_pct', '—')}%" for fold in folds
        ) or "—"
        lock_reason = (
            "当前无有效历史候选；V194已在收益读取前冻结36个月低波动月度因子的公式、样本、成本和门禁。"
            "V195唯一冻结成本后检验永久失败："
            f"训练年化 {training.get('annualized_net_return_20bp_pct', '—')}%；"
            f"验证年化 {validation.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {validation.get('annualized_excess_return_pct', '—')}%，"
            f"Bootstrap下限 {validation.get('block_bootstrap_95pct_lower_monthly_20bp_pct', '—')}%；"
            f"测试年化 {test.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {test.get('annualized_excess_return_pct', '—')}%，"
            f"总超额 {test.get('total_excess_return_pct', '—')}%，"
            f"胜率 {test.get('monthly_win_rate_20bp_pct', '—')}%，三折超额 {fold_text}。"
            "验证期局部正超额没有延续为测试期绝对收益和超额稳定性，不能据此生成买入信号；"
            "买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v197_result = _read_json(
        _PROJECT
        / "reports"
        / "v197_v4_low_max_monthly_factor_test.json"
    )
    if (
        v197_result.get("single_factor_test_consumed") is True
        and v197_result.get("historical_factor_gate_passed") is False
    ):
        results = v197_result.get("results") or {}
        training = results.get("training_audit") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        folds = v197_result.get("test_folds") or []
        fold_text = "/".join(
            f"{fold.get('excess_return_pct', '—')}%" for fold in folds
        ) or "—"
        lock_reason = (
            "当前无有效历史候选；V196已在收益读取前冻结上月低MAX月度因子的公式、样本、成本和门禁。"
            "V197唯一冻结成本后检验永久失败："
            f"训练年化 {training.get('annualized_net_return_20bp_pct', '—')}%；"
            f"验证年化 {validation.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {validation.get('annualized_excess_return_pct', '—')}%，"
            f"Bootstrap下限 {validation.get('block_bootstrap_95pct_lower_monthly_20bp_pct', '—')}%；"
            f"测试年化 {test.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {test.get('annualized_excess_return_pct', '—')}%，"
            f"总超额 {test.get('total_excess_return_pct', '—')}%，"
            f"胜率 {test.get('monthly_win_rate_20bp_pct', '—')}%，三折超额 {fold_text}。"
            "验证期防守性超额没有延续为测试期绝对收益和超额稳定性，不能据此生成买入信号；"
            "买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v201_result = _read_json(
        _PROJECT / "reports" / "v201_v4_low_market_beta_factor_test.json"
    )
    if (
        v201_result.get("single_factor_test_consumed") is True
        and v201_result.get("historical_factor_gate_passed") is False
    ):
        results = v201_result.get("results") or {}
        training = results.get("training_audit") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        folds = v201_result.get("test_folds") or []
        fold_text = "/".join(
            f"{fold.get('excess_return_pct', '—')}%" for fold in folds
        ) or "—"
        lock_reason = (
            "当前无有效历史候选；V200已在未来收益读取前冻结252日上证市场Beta、"
            "月频最低十分组、20/40BP成本和全部门禁。"
            "V201唯一冻结成本后检验永久失败："
            f"训练年化 {training.get('annualized_net_return_20bp_pct', '—')}%；"
            f"验证年化 {validation.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {validation.get('annualized_excess_return_pct', '—')}%，"
            f"Bootstrap下限 {validation.get('block_bootstrap_95pct_lower_monthly_20bp_pct', '—')}%；"
            f"测试年化 {test.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {test.get('annualized_excess_return_pct', '—')}%，"
            f"总超额 {test.get('total_excess_return_pct', '—')}%，"
            f"胜率 {test.get('monthly_win_rate_20bp_pct', '—')}%，三折超额 {fold_text}。"
            "训练期正收益没有延续到验证与测试，不能据此生成买入信号；"
            "买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v203_result = _read_json(
        _PROJECT / "reports" / "v203_v4_residual_momentum_factor_test.json"
    )
    if (
        v203_result.get("single_factor_test_consumed") is True
        and v203_result.get("historical_factor_gate_passed") is False
    ):
        results = v203_result.get("results") or {}
        training = results.get("training_audit") or {}
        validation = results.get("validation") or {}
        test = results.get("test") or {}
        folds = v203_result.get("test_folds") or []
        execution = v203_result.get("execution") or {}
        fold_text = "/".join(
            f"{fold.get('excess_return_pct', '—')}%" for fold in folds
        ) or "—"
        lock_reason = (
            "当前无有效历史候选；V202已在未来收益读取前冻结252日OLS Alpha、"
            "跳过近20日、月频最高十分组、20/40BP成本和全部门禁。"
            "V203唯一冻结成本后检验永久失败："
            f"训练年化 {training.get('annualized_net_return_20bp_pct', '—')}%；"
            f"验证年化 {validation.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {validation.get('annualized_excess_return_pct', '—')}%，"
            f"回撤 {validation.get('max_drawdown_20bp_pct', '—')}%；"
            f"测试年化 {test.get('annualized_net_return_20bp_pct', '—')}%，"
            f"年化超额 {test.get('annualized_excess_return_pct', '—')}%，"
            f"总超额 {test.get('total_excess_return_pct', '—')}%，"
            f"胜率 {test.get('monthly_win_rate_20bp_pct', '—')}%，三折超额 {fold_text}。"
            f"执行现金槽 {execution.get('cash_target_slots', '—')} 个、延期退出 "
            f"{execution.get('delayed_exits', '—')} 个、未解决 "
            f"{execution.get('unresolved_target_slots', '—')} 个。"
            "验证与测试均未证明绝对收益或超额稳定性，不能据此生成买入信号；"
            "买入推荐、资金分配、订单和自动交易继续关闭。"
            "持仓减仓与卖出提醒继续按实时风险规则显示。"
        )
    v205_result = (
        _read_json(_PROJECT / "reports" / "v209_global_defensive_rotation.json")
        or _read_json(_PROJECT / "reports" / "v205_multi_asset_defensive_rotation.json")
    )
    v207_result = (
        _read_json(_PROJECT / "reports" / "v211_global_defensive_robustness_audit.json")
        or _read_json(_PROJECT / "reports" / "v207_multi_asset_robustness_audit.json")
    )
    v213_result = _latest_paper_forward_report()
    v219_result = _read_json(
        _PROJECT / "reports" / "v219_qdii_split_nav_alignment_correction.json"
    )
    if v219_result:
        correction = v219_result.get("correction") or {}
        metrics40 = ((v219_result.get("cost_ladder_metrics") or {}).get("40.0") or {})
        validation = metrics40.get("validation_2022_2023") or {}
        validation_years = validation.get("yearly_total_return_pct") or {}
        test = metrics40.get("test_2024_2025") or {}
        diagnostic = metrics40.get("diagnostic_2026_through_july") or {}
        eligible = False
        historical_passed = False
        lock_reason = (
            "V219发现并纠正V209的QDII单位错误：溢价门禁曾把复权价格与未复权净值直接比较，"
            f"导致{correction.get('affected_asset_month_decisions', 0)}条资产月准入、"
            f"{correction.get('affected_execution_months', 0)}个执行月发生变化。"
            f"纠正后40BP验证累计{validation.get('total_return_pct', '—')}%，其中2022年"
            f"{validation_years.get('2022', '—')}%；测试累计{test.get('total_return_pct', '—')}%，"
            f"2026年1—7月{diagnostic.get('total_return_pct', '—')}%。"
            "V209历史候选、V211复核、V213/V215纸面前向全部撤销；当前无有效买卖信号，"
            "资金分配、订单和自动交易关闭，保持100%现金。"
        )
    elif v205_result.get("historical_candidate_passed") is True:
        cost40 = ((v205_result.get("cost_ladder_metrics") or {}).get("40.0") or {})
        validation = cost40.get("validation_2022_2023") or {}
        test = cost40.get("test_2024_2025") or {}
        diagnostic = cost40.get("diagnostic_2026_through_july") or {}
        catchup = v213_result.get("pre_freeze_catchup_diagnostic") or {}
        formal_forward = v213_result.get("formal_forward") or {}
        manual_refresh = v213_result.get("refresh") or {}
        allocation = (
            v205_result.get("current_research_observation")
            or v205_result.get("current_paper_allocation") or {}
        )
        allocation_text = "/".join(
            f"{row.get('name')} {row.get('weight_pct')}%"
            for row in allocation.get("allocations") or []
            if isinstance(row, dict)
        ) or f"现金 {allocation.get('cash_weight_pct', 100)}%"
        if v207_result.get("robustness_passed") is False:
            concentration = v207_result.get("return_concentration") or {}
            checks = v207_result.get("checks") or {}
            lock_reason = (
                "V205保留可成交历史候选证据，但V207预先冻结的独立稳健性复核未通过。"
                f"五项复核中 {sum(bool(value) for value in checks.values())}/"
                f"{len(checks)} 通过：独立复算、资产剔除、参数邻域和成本/成交扰动均通过；"
                f"黄金占正资产贡献 {concentration.get('maximum_positive_asset_contribution_share_pct', '—')}%，"
                f"单一年份占正对数收益 {concentration.get('maximum_positive_year_log_return_share_pct', '—')}%，"
                "均超过冻结上限70%。"
                f"原信号日研究权重 {allocation_text} 仅保留用于观察，不再作为可执行纸面调仓。"
                "真钱买入推荐、资金分配、订单、纸面执行和自动交易全部关闭；"
                "现有持仓减仓与卖出提醒继续按实时风险规则运行。"
            )
        else:
            lock_reason = (
                "当前通过历史与独立稳健性复核的是V209五资产全球防守轮动："
                "沪深300ETF、5年国债ETF、黄金ETF、纳指ETF和标普500ETF，"
                "使用12个月正动量Top3、6个月逆波动、单资产40%上限与QDII 2%溢价门禁。"
                f"40BP成本后验证累计 {validation.get('total_return_pct', '—')}%、"
                f"年化 {validation.get('annualized_return_pct', '—')}%、回撤 "
                f"{validation.get('max_drawdown_pct', '—')}%；测试累计 "
                f"{test.get('total_return_pct', '—')}%、年化 "
                f"{test.get('annualized_return_pct', '—')}%、回撤 "
                f"{test.get('max_drawdown_pct', '—')}%。"
                f"2026年1—7月诊断累计 {diagnostic.get('total_return_pct', '—')}%、回撤 "
                f"{diagnostic.get('max_drawdown_pct', '—')}%；当前纸面观察配置为 {allocation_text}。"
                f"V213冻结前追赶诊断为 {catchup.get('net_return_pct', '—')}%，不计入独立前向；"
                f"V215手动刷新已读取免费公开数据至 {manual_refresh.get('latest_public_common_close', '—')}；"
                f"正式前向目前 {formal_forward.get('completed_post_freeze_common_closes', 0)} 个共同收盘、"
                f"累计 {formal_forward.get('formal_forward_return_pct', 0)}%。"
                "这是历史候选而非稳定盈利证明：测试与2026诊断已被查看，真钱买入推荐、订单和自动交易继续关闭；"
                "纸面ETF调仓信号已开放显示，现有持仓减仓与卖出提醒继续按实时风险规则运行。"
            )
    existing_rows = registry.get("strategies") or []
    return {
        "recommendation_eligible": eligible,
        "research_only": not eligible,
        "status": "unlocked" if eligible else "locked",
        "reason": (
            "严格产品证书、正式前向、模拟盘和真钱授权均已通过。"
            if eligible else
            lock_reason
            if lock_reason else
            "旧历史回测因前视泄漏已否决；无历史冻结候选，禁止生成买入、加仓或资金分配建议。"
        ),
        "source": "reports/paper_forward_status.json",
        "strategy_registry": {
            "source": "reports/mom_strategy_registry.json",
            "generated_at": registry.get("generated_at"),
            "active_strategy": registry.get("active_strategy"),
            "historical_gate_passed": historical_passed,
            "recommendation_gate_passed": registry.get("recommendation_gate_passed") is True,
            "recommendation_lock_reason": lock_reason,
            "strategies": existing_rows,
        },
        "source_status": paper.get("status") or "missing",
        "generated_at": paper.get("generated_at"),
        "source_scope": "strict_product_forward_clock",
        "product_forward_clock_valid": (
            paper.get("product_forward_clock_valid") is True
        ),
        "strict_product_gate_certificate_valid": (
            paper.get("strict_product_gate_certificate_valid") is True
        ),
    }


def build_base_historical_candidate_headline(context: dict[str, object], gate: dict[str, object]) -> dict[str, object] | None:
    _read_json = context['_read_json']
    _PROJECT = context['_PROJECT']
    _latest_paper_forward_report = context['_latest_paper_forward_report']
    """Present historical evidence without turning it into live permission."""
    v219 = _read_json(
        _PROJECT / "reports" / "v219_qdii_split_nav_alignment_correction.json"
    )
    if v219:
        metrics40 = ((v219.get("cost_ladder_metrics") or {}).get("40.0") or {})
        validation = metrics40.get("validation_2022_2023") or {}
        test = metrics40.get("test_2024_2025") or {}
        diagnostic = metrics40.get("diagnostic_2026_through_july") or {}
        return {
            "method": "V219 QDII单位纠错（V209/V211/V213/V215已撤销）",
            "metric_scope": "v219_qdii_unit_corrected_failed",
            "validation_total_return_pct": validation.get("total_return_pct"),
            "validation_annualized_return_pct": validation.get("annualized_return_pct"),
            "validation_max_drawdown_pct": validation.get("max_drawdown_pct"),
            "validation_yearly_total_return_pct": validation.get("yearly_total_return_pct") or {},
            "test_total_return_pct": test.get("total_return_pct"),
            "test_annualized_return_pct": test.get("annualized_return_pct"),
            "test_max_drawdown_pct": test.get("max_drawdown_pct"),
            "diagnostic_total_return_pct": diagnostic.get("total_return_pct"),
            "diagnostic_max_drawdown_pct": diagnostic.get("max_drawdown_pct"),
            "correction": v219.get("correction") or {},
            "failed_gates": [
                key for key, passed in (v219.get("checks") or {}).items()
                if not passed
            ],
            "research_observation_allocation": v219.get("current_research_observation") or {},
            "current_paper_allocation": {},
            "invalidated": True,
            "historical_gate_passed": False,
            "paper_forward_eligible": False,
            "paper_only": False,
            "research_observation_only": True,
            "recommendation_eligible": False,
            "automatic_trading": False,
            "generated_at": v219.get("generated_at"),
            "audit_message": gate.get("reason"),
        }
    v205 = (
        _read_json(_PROJECT / "reports" / "v209_global_defensive_rotation.json")
        or _read_json(_PROJECT / "reports" / "v205_multi_asset_defensive_rotation.json")
    )
    v207 = (
        _read_json(_PROJECT / "reports" / "v211_global_defensive_robustness_audit.json")
        or _read_json(_PROJECT / "reports" / "v207_multi_asset_robustness_audit.json")
    )
    v213 = _latest_paper_forward_report()
    if v205.get("historical_candidate_passed") is True:
        cost40 = ((v205.get("cost_ladder_metrics") or {}).get("40.0") or {})
        validation = cost40.get("validation_2022_2023") or {}
        test = cost40.get("test_2024_2025") or {}
        diagnostic = cost40.get("diagnostic_2026_through_july") or {}
        catchup = v213.get("pre_freeze_catchup_diagnostic") or {}
        formal_forward = v213.get("formal_forward") or {}
        manual_refresh = v213.get("refresh") or {}
        allocation = (
            v205.get("current_research_observation")
            or v205.get("current_paper_allocation") or {}
        )
        if v207.get("robustness_passed") is False:
            concentration = v207.get("return_concentration") or {}
            return {
                "method": "V205 可成交三资产防守轮动（V207稳健性未通过）",
                "metric_scope": "v207_multi_asset_robustness_failed",
                "validation_total_return_pct": validation.get("total_return_pct"),
                "test_total_return_pct": test.get("total_return_pct"),
                "diagnostic_total_return_pct": diagnostic.get("total_return_pct"),
                "research_observation_allocation": allocation,
                "robustness_checks": v207.get("checks") or {},
                "maximum_positive_asset_contribution_share_pct": (
                    concentration.get("maximum_positive_asset_contribution_share_pct")
                ),
                "maximum_positive_year_log_return_share_pct": (
                    concentration.get("maximum_positive_year_log_return_share_pct")
                ),
                "concentration_threshold_pct": concentration.get("threshold_pct"),
                "invalidated": True,
                "historical_gate_passed": False,
                "paper_forward_eligible": False,
                "paper_only": False,
                "research_observation_only": True,
                "recommendation_eligible": False,
                "automatic_trading": False,
                "generated_at": v207.get("generated_at"),
                "audit_message": gate.get("reason"),
            }
        return {
            "method": "V209 五资产全球防守轮动（V211通过，仅纸面前向观察）",
            "metric_scope": "v209_global_defensive_paper_candidate",
            "annualized_pct": validation.get("annualized_return_pct"),
            "sharpe": validation.get("sharpe_zero_cash"),
            "calmar": None,
            "max_dd_pct": validation.get("max_drawdown_pct"),
            "win_rate": validation.get("monthly_win_rate_pct"),
            "years_positive": 2,
            "years_total": 2,
            "validation_total_return_pct": validation.get("total_return_pct"),
            "validation_annualized_return_pct": validation.get("annualized_return_pct"),
            "validation_max_drawdown_pct": validation.get("max_drawdown_pct"),
            "test_total_return_pct": test.get("total_return_pct"),
            "test_annualized_return_pct": test.get("annualized_return_pct"),
            "test_max_drawdown_pct": test.get("max_drawdown_pct"),
            "diagnostic_total_return_pct": diagnostic.get("total_return_pct"),
            "diagnostic_max_drawdown_pct": diagnostic.get("max_drawdown_pct"),
            "pre_freeze_catchup_net_return_pct": catchup.get("net_return_pct"),
            "formal_forward_observations": formal_forward.get(
                "completed_post_freeze_common_closes"
            ),
            "formal_forward_return_pct": formal_forward.get("formal_forward_return_pct"),
            "formal_forward_status": formal_forward.get("status"),
            "formal_forward_latest_date": formal_forward.get(
                "latest_observation_date"
            ),
            "formal_forward_max_drawdown_pct": formal_forward.get(
                "max_drawdown_pct"
            ),
            "paper_forward_id": v213.get("paper_forward_id"),
            "manual_refresh": manual_refresh,
            "current_paper_allocation": allocation,
            "invalidated": False,
            "historical_gate_passed": True,
            "paper_forward_eligible": bool(v207.get("paper_forward_eligible")),
            "paper_only": True,
            "recommendation_eligible": False,
            "automatic_trading": False,
            "generated_at": (
                v213.get("generated_at")
                or v207.get("generated_at")
                or v205.get("generated_at")
            ),
            "audit_message": gate.get("reason"),
        }
    registry = gate.get("strategy_registry") or {}
    active_id = registry.get("active_strategy")
    active = next(
        (row for row in registry.get("strategies", []) if row.get("id") == active_id),
        None,
    )
    metrics = (active or {}).get("metrics") or {}
    if not active or not metrics:
        return {
            "method": "无有效历史候选",
            "invalidated": True,
            "historical_gate_passed": False,
            "recommendation_eligible": False,
            "audit_message": (
                registry.get("recommendation_lock_reason")
                or "历史候选未通过严格数据、统计与成本门禁；仅作研究观察。"
            ),
        }
    return {
        "method": active.get("label"),
        "metric_scope": active.get("metric_scope") or "taskbook_v2_historical",
        "mean_return_pct": metrics.get("mean_return_pct"),
        "median_return_pct": metrics.get("median_return_pct"),
        "positive_runs": metrics.get("positive_runs"),
        "total_runs": metrics.get("total_runs"),
        "positive_environments": metrics.get("positive_environments"),
        "total_environments": metrics.get("total_environments"),
        "max_dd_pct": metrics.get("worst_drawdown_pct"),
        "annualized_pct": None,
        "sharpe": None,
        "calmar": None,
        "win_rate": (
            round(metrics["positive_runs"] / metrics["total_runs"] * 100, 1)
            if metrics.get("total_runs") else None
        ),
        "invalidated": bool(active.get("invalidated")),
        "historical_gate_passed": registry.get("historical_gate_passed", False),
        "recommendation_eligible": False,
        "audit_message": registry.get("recommendation_lock_reason"),
    }


def build_base_profitability_research_evidence(context: dict[str, object]) -> dict[str, object]:
    _read_json = context['_read_json']
    _PROJECT = context['_PROJECT']
    _latest_paper_forward_report = context['_latest_paper_forward_report']
    _TASKBOOK_V3_GATE = context['_TASKBOOK_V3_GATE']
    _V86_FORWARD_LATEST = context['_V86_FORWARD_LATEST']
    _V86_FORWARD_EVALUATION = context['_V86_FORWARD_EVALUATION']
    _V86_FORWARD_SCHEDULE = context['_V86_FORWARD_SCHEDULE']
    _V86_FORWARD_PROMOTION = context['_V86_FORWARD_PROMOTION']
    _V86_EXTERNAL_REPRODUCTION = context['_V86_EXTERNAL_REPRODUCTION']
    _V86_FINAL_CANDIDATE_GATE = context['_V86_FINAL_CANDIDATE_GATE']
    _V3_PROFIT_SOURCE_AUDIT = context['_V3_PROFIT_SOURCE_AUDIT']
    _V86_REGIME_ROBUSTNESS_GATE = context['_V86_REGIME_ROBUSTNESS_GATE']
    _V86_ROLLING_OOS_GATE = context['_V86_ROLLING_OOS_GATE']
    _V86_COST_CAPACITY_GATE = context['_V86_COST_CAPACITY_GATE']
    _V86_CORPORATE_ACTION_GATE = context['_V86_CORPORATE_ACTION_GATE']
    _V86_TEMPORAL_DEPENDENCE_GATE = context['_V86_TEMPORAL_DEPENDENCE_GATE']
    _V46_TEMPORAL_DEPENDENCE_AUDIT = context['_V46_TEMPORAL_DEPENDENCE_AUDIT']
    _V86_SELECTION_BENCHMARK_GATE = context['_V86_SELECTION_BENCHMARK_GATE']
    _V46_SELECTION_BENCHMARK_AUDIT = context['_V46_SELECTION_BENCHMARK_AUDIT']
    _V86_MULTI_BENCHMARK_GATE = context['_V86_MULTI_BENCHMARK_GATE']
    _V46_MULTI_BENCHMARK_AUDIT = context['_V46_MULTI_BENCHMARK_AUDIT']
    _V86_CONTRIBUTOR_CONCENTRATION_GATE = context['_V86_CONTRIBUTOR_CONCENTRATION_GATE']
    _V46_CONTRIBUTOR_CONCENTRATION_AUDIT = context['_V46_CONTRIBUTOR_CONCENTRATION_AUDIT']
    _V86_MODEL_TRAINING_INTEGRITY_GATE = context['_V86_MODEL_TRAINING_INTEGRITY_GATE']
    _V46_TRAINING_BOUNDARY_PURGE_AUDIT = context['_V46_TRAINING_BOUNDARY_PURGE_AUDIT']
    _V87_SUCCESSOR_ADMISSION_GATE = context['_V87_SUCCESSOR_ADMISSION_GATE']
    _V87_CSRC_MAPPING_FAILURE = context['_V87_CSRC_MAPPING_FAILURE']
    _V88_CSRC_MAPPING_AUDIT = context['_V88_CSRC_MAPPING_AUDIT']
    _V89_SSE_DATA_EPOCH_AUDIT = context['_V89_SSE_DATA_EPOCH_AUDIT']
    _DEEPSEEK_DISCOVERY_ROUND = context['_DEEPSEEK_DISCOVERY_ROUND']
    _DEEPSEEK_DISCOVERY_ASSESSMENT = context['_DEEPSEEK_DISCOVERY_ASSESSMENT']
    _V90_SUCCESSOR_STATUS = context['_V90_SUCCESSOR_STATUS']
    _V120_SUCCESSOR_STATUS = context['_V120_SUCCESSOR_STATUS']
    _V143_SUCCESSOR_STATUS = context['_V143_SUCCESSOR_STATUS']
    _V145_SUCCESSOR_STATUS = context['_V145_SUCCESSOR_STATUS']
    """Expose frozen research evidence without turning it into a recommendation."""
    v205_mechanism = (
        _read_json(_PROJECT / "reports" / "v209_global_defensive_rotation.json")
        or _read_json(_PROJECT / "reports" / "v205_multi_asset_defensive_rotation.json")
    )
    v207_robustness = (
        _read_json(_PROJECT / "reports" / "v211_global_defensive_robustness_audit.json")
        or _read_json(_PROJECT / "reports" / "v207_multi_asset_robustness_audit.json")
    )
    v213_paper_forward = _latest_paper_forward_report()
    v217_cash_sweep = _read_json(
        _PROJECT / "reports" / "v217_cash_sweep_rotation.json"
    )
    v219_qdii_correction = _read_json(
        _PROJECT / "reports" / "v219_qdii_split_nav_alignment_correction.json"
    )
    v203_mechanism = _read_json(
        _PROJECT / "reports" / "v203_v4_residual_momentum_factor_test.json"
    )
    latest_mechanism = (
        v219_qdii_correction or v207_robustness or v205_mechanism or v203_mechanism
    )
    try:
        v3 = json.loads(_TASKBOOK_V3_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        v3 = {}
    try:
        forward = json.loads(_V86_FORWARD_LATEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        forward = {}
    try:
        evaluation = json.loads(_V86_FORWARD_EVALUATION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        evaluation = {}
    try:
        schedule = json.loads(_V86_FORWARD_SCHEDULE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        schedule = {}
    try:
        promotion = json.loads(_V86_FORWARD_PROMOTION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        promotion = {}
    try:
        external = json.loads(_V86_EXTERNAL_REPRODUCTION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        external = {}
    try:
        final_gate = json.loads(_V86_FINAL_CANDIDATE_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        final_gate = {}
    try:
        profit_sources = json.loads(_V3_PROFIT_SOURCE_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        profit_sources = {}
    try:
        regime_gate = json.loads(_V86_REGIME_ROBUSTNESS_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        regime_gate = {}
    try:
        rolling_gate = json.loads(_V86_ROLLING_OOS_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rolling_gate = {}
    try:
        cost_capacity_gate = json.loads(_V86_COST_CAPACITY_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cost_capacity_gate = {}
    try:
        corporate_action_gate = json.loads(_V86_CORPORATE_ACTION_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        corporate_action_gate = {}
    try:
        temporal_gate = json.loads(_V86_TEMPORAL_DEPENDENCE_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        temporal_gate = {}
    try:
        temporal_audit = json.loads(_V46_TEMPORAL_DEPENDENCE_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        temporal_audit = {}
    try:
        selection_benchmark_gate = json.loads(_V86_SELECTION_BENCHMARK_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        selection_benchmark_gate = {}
    try:
        selection_benchmark_audit = json.loads(_V46_SELECTION_BENCHMARK_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        selection_benchmark_audit = {}
    try:
        multi_benchmark_gate = json.loads(_V86_MULTI_BENCHMARK_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        multi_benchmark_gate = {}
    try:
        multi_benchmark_audit = json.loads(_V46_MULTI_BENCHMARK_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        multi_benchmark_audit = {}
    try:
        contributor_concentration_gate = json.loads(_V86_CONTRIBUTOR_CONCENTRATION_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        contributor_concentration_gate = {}
    try:
        contributor_concentration_audit = json.loads(_V46_CONTRIBUTOR_CONCENTRATION_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        contributor_concentration_audit = {}
    try:
        model_training_integrity_gate = json.loads(_V86_MODEL_TRAINING_INTEGRITY_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        model_training_integrity_gate = {}
    try:
        training_boundary_purge_audit = json.loads(_V46_TRAINING_BOUNDARY_PURGE_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        training_boundary_purge_audit = {}
    try:
        successor_admission_gate = json.loads(_V87_SUCCESSOR_ADMISSION_GATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        successor_admission_gate = {}
    try:
        successor_mapping_failure = json.loads(_V87_CSRC_MAPPING_FAILURE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        successor_mapping_failure = {}
    try:
        successor_mapping_audit = json.loads(_V88_CSRC_MAPPING_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        successor_mapping_audit = {}
    try:
        successor_v89_epoch = json.loads(_V89_SSE_DATA_EPOCH_AUDIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        successor_v89_epoch = {}
    try:
        successor_discovery_round = json.loads(_DEEPSEEK_DISCOVERY_ROUND.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        successor_discovery_round = {}
    try:
        successor_discovery_assessment = json.loads(
            _DEEPSEEK_DISCOVERY_ASSESSMENT.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        successor_discovery_assessment = {}
    v90_successor_status = _read_json(_V90_SUCCESSOR_STATUS)
    v120_successor_status = _read_json(_V120_SUCCESSOR_STATUS)
    v143_successor_status = _read_json(_V143_SUCCESSOR_STATUS)
    v145_successor_status = _read_json(_V145_SUCCESSOR_STATUS)
    if v145_successor_status.get("new_data_epoch_ready") is True:
        successor_admission_gate = v145_successor_status
        successor_discovery_round = v145_successor_status
        successor_discovery_assessment = v145_successor_status
    elif v143_successor_status.get("new_data_epoch_ready") is True:
        successor_admission_gate = v143_successor_status
        successor_discovery_round = v143_successor_status
        successor_discovery_assessment = v143_successor_status
    elif v120_successor_status.get("new_data_epoch_ready") is True:
        successor_admission_gate = v120_successor_status
        successor_discovery_round = v120_successor_status
        successor_discovery_assessment = v120_successor_status
    elif v90_successor_status.get("new_data_epoch_ready") is True:
        successor_admission_gate = v90_successor_status
        successor_discovery_round = v90_successor_status
        successor_discovery_assessment = v90_successor_status

    base = v3.get("base") or {}
    double_cost = v3.get("double_cost") or {}
    statistical = v3.get("statistical_evidence") or {}
    bootstrap = statistical.get("bootstrap") or {}
    ci = bootstrap.get("mean_return_pct_ci_95") or [None, None]
    forward_evidence = forward.get("forward_evidence") or {}
    outcome_summary = (evaluation.get("outcome_summary") or {}).get("base") or {}
    portfolio_summary = (evaluation.get("portfolio_summary") or {}).get("base") or {}
    stress_summary = (evaluation.get("portfolio_summary") or {}).get("stress_20bp") or {}
    observations = evaluation.get("signal_observations") or {}
    independent = evaluation.get("independent_reproduction") or {}
    concentration = profit_sources.get("profit_source_concentration") or {}
    cost_sensitivity = profit_sources.get("cost_sensitivity") or {}
    exit_paths = profit_sources.get("exit_path_attribution") or {}
    regime_signature = regime_gate.get("regime_external_signature") or {}
    rolling_aggregate = rolling_gate.get("aggregate") or {}
    cost_scenarios = cost_capacity_gate.get("cost_scenarios") or {}
    capacity = cost_capacity_gate.get("capacity") or {}
    settled_scenarios = corporate_action_gate.get("corrected_cost_scenarios") or {}
    corporate_universe = corporate_action_gate.get("universe") or {}
    temporal_scenarios = temporal_gate.get("scenario_statistics") or {}
    temporal_50bp = temporal_scenarios.get("round_trip_50bp") or {}
    v46_observed_temporal = ((temporal_audit.get("splits") or {}).get("observed_test") or {})
    v46_observed_2026 = ((v46_observed_temporal.get("by_year") or {}).get("2026") or {})
    benchmark_scenarios = selection_benchmark_gate.get("portfolio_benchmark_statistics") or {}
    benchmark_50bp = benchmark_scenarios.get("round_trip_50bp") or {}
    selection_epoch = selection_benchmark_gate.get("research_epoch") or {}
    selection_lineage = selection_benchmark_audit.get("selection_lineage") or {}
    v46_benchmark = ((selection_benchmark_audit.get("benchmark_comparison") or {}).get("observed_test") or {})
    v46_benchmark_2026 = ((v46_benchmark.get("yearly") or {}).get("2026") or {})
    multi_benchmark_50bp = ((multi_benchmark_gate.get("benchmark_statistics") or {}).get("round_trip_50bp") or {})
    multi_comparisons = multi_benchmark_audit.get("benchmark_comparisons") or {}
    v46_multi_observed = {
        code: ((row.get("metrics") or {}).get("observed_test") or {})
        for code, row in multi_comparisons.items()
    }
    concentration_50bp = ((contributor_concentration_gate.get("scenario_statistics") or {}).get("round_trip_50bp") or {})
    v46_concentration_splits = contributor_concentration_audit.get("splits") or {}
    v46_concentration_validation = v46_concentration_splits.get("validation") or {}
    v46_concentration_observed = v46_concentration_splits.get("observed_test") or {}
    purged_validation = model_training_integrity_gate.get("purged_validation") or {}
    training_comparison = training_boundary_purge_audit.get("comparison") or {}
    successor_mapping_summary = successor_mapping_audit.get("mapping_summary") or {}
    successor_mapping_review = successor_mapping_audit.get("manual_review") or {}
    successor_cap_reached = (
        successor_admission_gate.get("discovery_round_cap_reached") is True
    )
    successor_discovery_allowed = bool(
        successor_admission_gate.get("successor_discovery_allowed") is True
        and not successor_cap_reached
    )
    successor_next_action = (
        "已达同一数据纪元30轮上限；必须先验收全新、已授权、含真实发布时间和物理哈希的数据纪元，"
        "在此之前不再生成机制、不启动回测。"
        if successor_cap_reached else
        successor_admission_gate.get("next_action")
    )
    if v219_qdii_correction:
        v219_cost40 = (
            (v219_qdii_correction.get("cost_ladder_metrics") or {}).get("40.0")
            or {}
        )

        def v219_metric(section: str) -> dict[str, object]:
            source = v219_cost40.get(section) or {}
            return {
                "annualized_net_return_20bp_pct": source.get("annualized_return_pct"),
                "total_return_20bp_pct": source.get("total_return_pct"),
                "max_drawdown_20bp_pct": source.get("max_drawdown_pct"),
                "monthly_win_rate_20bp_pct": source.get("monthly_win_rate_pct"),
                "yearly_total_return_pct": source.get("yearly_total_return_pct") or {},
            }

        latest_results = {
            "training_audit": v219_metric("development_2015_2021"),
            "validation": v219_metric("validation_2022_2023"),
            "test": v219_metric("test_2024_2025"),
            "diagnostic_2026": v219_metric("diagnostic_2026_through_july"),
        }
        latest_test_id = "V219"
        latest_candidate_id = v219_qdii_correction.get("strategy_id")
        latest_source = "reports/v219_qdii_split_nav_alignment_correction.json"
        latest_historical_passed = False
        latest_external_model = False
        latest_paid_api = False
        latest_failed_gates = [
            key for key, passed in (v219_qdii_correction.get("checks") or {}).items()
            if not passed
        ]
        latest_folds = []
    elif v207_robustness:
        v207_baseline = (
            v207_robustness.get("baseline_40bp_metrics")
            or ((v205_mechanism.get("cost_ladder_metrics") or {}).get("40.0") or {})
        )

        def v207_metric(section: str) -> dict[str, object]:
            source = v207_baseline.get(section) or {}
            return {
                "annualized_net_return_20bp_pct": source.get("annualized_return_pct"),
                "total_return_20bp_pct": source.get("total_return_pct"),
                "max_drawdown_20bp_pct": source.get("max_drawdown_pct"),
                "monthly_win_rate_20bp_pct": source.get("monthly_win_rate_pct"),
                "yearly_total_return_pct": source.get("yearly_total_return_pct") or {},
            }

        latest_results = {
            "training_audit": v207_metric("development_2015_2021"),
            "validation": v207_metric("validation_2022_2023"),
            "test": v207_metric("test_2024_2025"),
            "diagnostic_2026": v207_metric("diagnostic_2026_through_july"),
        }
        latest_test_id = (
            "V211" if str(v207_robustness.get("audit_id") or "").startswith("V211")
            else "V207"
        )
        latest_candidate_id = (
            v207_robustness.get("strategy_id")
            or v207_robustness.get("subject_strategy_id")
        )
        latest_source = (
            "reports/v211_global_defensive_robustness_audit.json"
            if latest_test_id == "V211"
            else "reports/v207_multi_asset_robustness_audit.json"
        )
        latest_historical_passed = bool(
            v205_mechanism.get("historical_candidate_passed") is True
            and v207_robustness.get("robustness_passed") is True
        )
        latest_external_model = False
        latest_paid_api = False
        latest_failed_gates = [
            key for key, passed in (v207_robustness.get("checks") or {}).items()
            if not passed
        ]
        latest_folds = []
    elif v205_mechanism:
        v205_cost40 = (
            (v205_mechanism.get("cost_ladder_metrics") or {}).get("40.0") or {}
        )

        def v205_metric(section: str) -> dict[str, object]:
            source = v205_cost40.get(section) or {}
            return {
                "annualized_net_return_20bp_pct": source.get("annualized_return_pct"),
                "total_return_20bp_pct": source.get("total_return_pct"),
                "max_drawdown_20bp_pct": source.get("max_drawdown_pct"),
                "monthly_win_rate_20bp_pct": source.get("monthly_win_rate_pct"),
                "annualized_volatility_pct": source.get("annualized_volatility_pct"),
                "sharpe_zero_cash": source.get("sharpe_zero_cash"),
                "yearly_total_return_pct": source.get("yearly_total_return_pct") or {},
            }

        latest_results = {
            "training_audit": v205_metric("development_2015_2021"),
            "validation": v205_metric("validation_2022_2023"),
            "test": v205_metric("test_2024_2025"),
            "diagnostic_2026": v205_metric("diagnostic_2026_through_july"),
        }
        latest_test_id = "V205"
        latest_candidate_id = v205_mechanism.get("strategy_id")
        latest_source = "reports/v205_multi_asset_defensive_rotation.json"
        latest_historical_passed = (
            v205_mechanism.get("historical_candidate_passed") is True
        )
        latest_external_model = v205_mechanism.get("external_model_used") is True
        latest_paid_api = v205_mechanism.get("paid_api_used") is True
        latest_failed_gates: list[object] = []
        latest_folds: list[object] = []
    else:
        latest_results = latest_mechanism.get("results") or {}
        latest_test_id = "V203"
        latest_candidate_id = latest_mechanism.get("candidate_id")
        latest_source = "reports/v203_v4_residual_momentum_factor_test.json"
        latest_historical_passed = (
            latest_mechanism.get("historical_factor_gate_passed") is True
        )
        latest_external_model = (
            (latest_mechanism.get("safety") or {}).get("external_model_used") is True
        )
        latest_paid_api = (
            (latest_mechanism.get("safety") or {}).get("paid_api_used") is True
        )
        latest_failed_gates = latest_mechanism.get("failed_gates") or []
        latest_folds = latest_mechanism.get("test_folds") or []
    return {
        "research_only": True,
        "recommendation_eligible": False,
        "latest_independent_mechanism": {
            "available": bool(latest_mechanism),
            "test_id": latest_test_id,
            "candidate_id": latest_candidate_id,
            "status": latest_mechanism.get("status") or "missing",
            "results": latest_results,
            "test_folds": latest_folds,
            "failed_gates": latest_failed_gates,
            "historical_factor_gate_passed": latest_historical_passed,
            "historical_candidate_passed": latest_historical_passed,
            "paper_forward_eligible": (
                not v219_qdii_correction
                and
                v205_mechanism.get("historical_candidate_passed") is True
                and v207_robustness.get("paper_forward_eligible") is True
                if v205_mechanism else False
            ),
            "current_paper_allocation": (
                v205_mechanism.get("current_research_observation")
                or v205_mechanism.get("current_paper_allocation") or {}
                if (
                    v205_mechanism
                    and v207_robustness.get("robustness_passed") is True
                    and not v219_qdii_correction
                )
                else {}
            ),
            "research_observation_allocation": (
                v205_mechanism.get("current_research_observation")
                or v205_mechanism.get("current_paper_allocation") or {}
                if v205_mechanism else {}
            ),
            "recommendation_eligible": False,
            "external_model_used": latest_external_model,
            "paid_api_used": latest_paid_api,
            "source": latest_source,
        },
        "multi_asset_robustness": {
            "available": bool(v207_robustness),
            "status": v207_robustness.get("status") or "missing",
            "robustness_passed": bool(
                v207_robustness.get("robustness_passed") is True
                and not v219_qdii_correction
            ),
            "checks": v207_robustness.get("checks") or {},
            "independent_reproduction": v207_robustness.get("independent_reproduction") or {},
            "leave_one_asset_out": v207_robustness.get("leave_one_asset_out") or {},
            "parameter_neighborhood": v207_robustness.get("parameter_neighborhood") or {},
            "cost_and_execution_perturbation": (
                v207_robustness.get("cost_and_execution_perturbation")
                or v207_robustness.get("cost_and_signal_delay") or {}
            ),
            "return_concentration": v207_robustness.get("return_concentration") or {},
            "conclusion": v207_robustness.get("conclusion"),
            "recommendation_eligible": False,
            "automatic_trading": False,
            "revoked_by_v219": bool(v219_qdii_correction),
        },
        "paper_forward": {
            "available": bool(v213_paper_forward),
            "status": (
                "revoked_parent_qdii_unit_mismatch"
                if v219_qdii_correction
                else v213_paper_forward.get("status") or "missing"
            ),
            "paper_forward_id": v213_paper_forward.get("paper_forward_id"),
            "pre_freeze_catchup_diagnostic": (
                v213_paper_forward.get("pre_freeze_catchup_diagnostic") or {}
            ),
            "formal_forward": v213_paper_forward.get("formal_forward") or {},
            "refresh": v213_paper_forward.get("refresh") or {},
            "receipt_chain": v213_paper_forward.get("receipt_chain") or {},
            "stable_profit_proven": bool(
                v213_paper_forward.get("stable_profit_proven")
            ),
            "recommendation_eligible": False,
            "real_money_eligible": False,
            "automatic_trading": False,
            "honesty": v213_paper_forward.get("honesty"),
            "revoked_by_v219": bool(v219_qdii_correction),
        },
        "qdii_unit_correction": {
            "available": bool(v219_qdii_correction),
            "strategy_id": v219_qdii_correction.get("strategy_id"),
            "status": v219_qdii_correction.get("status") or "missing",
            "correction": v219_qdii_correction.get("correction") or {},
            "checks": v219_qdii_correction.get("checks") or {},
            "cost_ladder_metrics": v219_qdii_correction.get("cost_ladder_metrics") or {},
            "current_research_observation": (
                v219_qdii_correction.get("current_research_observation") or {}
            ),
            "historical_candidate_passed": False,
            "paper_forward_eligible": False,
            "recommendation_eligible": False,
            "automatic_trading": False,
            "conclusion": v219_qdii_correction.get("conclusion"),
            "honesty": v219_qdii_correction.get("honesty"),
        },
        "cash_sweep": {
            "available": bool(v217_cash_sweep),
            "strategy_id": v217_cash_sweep.get("strategy_id"),
            "status": v217_cash_sweep.get("status") or "missing",
            "historical_candidate_passed": bool(
                v217_cash_sweep.get("historical_candidate_passed")
            ),
            "checks": v217_cash_sweep.get("checks") or {},
            "cost_ladder_metrics": v217_cash_sweep.get("cost_ladder_metrics") or {},
            "current_research_observation": (
                v217_cash_sweep.get("current_research_observation") or {}
            ),
            "recommendation_eligible": False,
            "automatic_trading": False,
            "honesty": v217_cash_sweep.get("honesty"),
        },
        "v3": {
            "available": bool(v3),
            "status": (
                "failed" if v3 and not v3.get("historical_robustness_gate_passed")
                else "passed" if v3 else "unavailable"
            ),
            "positive_runs": base.get("positive_runs"),
            "total_runs": base.get("runs"),
            "mean_return_pct": base.get("mean_return_pct"),
            "median_return_pct": base.get("median_return_pct"),
            "double_cost_positive_runs": double_cost.get("positive_runs"),
            "double_cost_median_return_pct": double_cost.get("median_return_pct"),
            "bootstrap_ci_low_pct": ci[0] if len(ci) > 0 else None,
            "failed_gates": v3.get("failed_gates") or [],
            "rising_environment_positive_return_share": concentration.get("rising_environment_positive_return_share"),
            "mean_return_without_strongest_environment_pct": concentration.get("mean_return_without_strongest_environment_pct"),
            "double_cost_share_of_base_mean_eroded": cost_sensitivity.get("share_of_base_mean_eroded"),
            "ordinary_rule_exit_net_pnl": exit_paths.get("ordinary_rule_exit_net_pnl"),
            "period_end_exit_net_pnl": exit_paths.get("period_end_exit_net_pnl"),
            "environment_robustness_supported": profit_sources.get("environment_robustness_supported") is True,
            "profit_source_failed_diagnostics": profit_sources.get("failed_diagnostics") or [],
            "source": "reports/taskbook_v3_gate_result.json",
        },
        "forward": {
            "available": bool(forward),
            "status": (
                "forward_gate_passed" if evaluation.get("forward_gate_passed")
                else "ready_for_independent_reproduction"
                if evaluation.get("ready_for_independent_reproduction")
                else "waiting_for_outcomes" if forward else "unavailable"
            ),
            "data_date": evaluation.get("as_of_market_date") or forward.get("data_date"),
            "source_files": (evaluation.get("data_quality") or {}).get("source_files") or forward.get("source_files"),
            "failed_source_files": (evaluation.get("data_quality") or {}).get("failed_source_files") if evaluation else forward.get("failed_source_files"),
            "daily_top_score": forward.get("daily_top_score"),
            "frozen_score_threshold": forward.get("frozen_score_threshold"),
            "score_gate_passed": forward.get("score_gate_passed") is True,
            "candidate_count": len(forward.get("candidates") or []),
            "weeks_since_baseline": observations.get("weeks_since_baseline"),
            "eligible_signal_dates": observations.get("eligible_signal_dates"),
            "scheduled_observations": schedule.get("scheduled_observations"),
            "minimum_scheduled_observations": schedule.get("minimum_scheduled_observations"),
            "schedule_integrity_passed": schedule.get("schedule_integrity_passed") is True,
            "calendar_coverage_passed": schedule.get("calendar_coverage_passed") is True,
            "missing_scheduled_dates": schedule.get("missing_scheduled_dates") or [],
            "promotion_schedule_gate_passed": schedule.get("promotion_schedule_gate_passed") is True,
            "completed_signal_outcomes": outcome_summary.get("completed", forward_evidence.get("completed_signal_outcomes")),
            "completed_portfolio_trades": portfolio_summary.get("completed", forward_evidence.get("completed_portfolio_trades")),
            "base_cumulative_net_pnl": portfolio_summary.get("cumulative_net_pnl"),
            "stress_cumulative_net_pnl": stress_summary.get("cumulative_net_pnl"),
            "failed_gates": evaluation.get("failed_gates") or [],
            "ready_for_independent_reproduction": evaluation.get("ready_for_independent_reproduction") is True,
            "independent_reproduction_passed": independent.get("passed") is True,
            "forward_gate_passed": evaluation.get("forward_gate_passed") is True,
            "external_reproducer_registered": external.get("reproducer_registered") is True,
            "external_signed_attestation_passed": external.get("signed_attestation_passed") is True,
            "external_failed_checks": external.get("failed_checks") or [],
            "locked_promotion_gate_passed": promotion.get("historical_candidate_eligible") is True,
            "upstream_final_candidate_eligible": final_gate.get("historical_candidate_eligible") is True,
            "regime_results": regime_gate.get("regimes") or {},
            "regime_robustness_gate_passed": regime_gate.get("regime_robustness_gate_passed") is True,
            "regime_signed_reproduction_passed": regime_signature.get("signed_regime_reproduction_passed") is True,
            "regime_failed_gates": regime_gate.get("failed_gates") or [],
            "rolling_oos_closed_folds": rolling_aggregate.get("closed_folds", 0),
            "rolling_oos_passed_folds": rolling_aggregate.get("passed_folds", 0),
            "rolling_oos_positive_fold_ratio": rolling_aggregate.get("positive_fold_ratio"),
            "rolling_oos_latest_two_folds_passed": rolling_aggregate.get("latest_two_folds_passed") is True,
            "rolling_oos_required_folds": 4,
            "rolling_oos_required_observations": 32,
            "rolling_oos_gate_passed": rolling_gate.get("rolling_oos_gate_passed") is True,
            "rolling_oos_failed_gates": rolling_gate.get("failed_gates") or [],
            "maximum_round_trip_cost_tested_bps": 50,
            "cost_ladder_completed_trades": (cost_scenarios.get("round_trip_50bp") or {}).get("completed_trades"),
            "cost_30bp_cumulative_net_pnl": (cost_scenarios.get("round_trip_30bp") or {}).get("cumulative_net_pnl"),
            "cost_50bp_cumulative_net_pnl": (cost_scenarios.get("round_trip_50bp") or {}).get("cumulative_net_pnl"),
            "capacity_maximum_participation": capacity.get("maximum_participation"),
            "capacity_within_1pct": capacity.get("within_1pct") is True,
            "cost_capacity_gate_passed": cost_capacity_gate.get("cost_capacity_gate_passed") is True,
            "cost_capacity_failed_gates": cost_capacity_gate.get("failed_gates") or [],
            "corporate_action_settlement_gate_passed": corporate_action_gate.get("corporate_action_settlement_gate_passed") is True,
            "corporate_action_reference_events": corporate_action_gate.get("portfolio_reference_events", 0),
            "corporate_action_signal_reference_events": corporate_action_gate.get("signal_reference_events", 0),
            "corporate_action_unresolved_events": corporate_action_gate.get("unresolved_reference_events", 0),
            "corporate_action_external_attestation_passed": (corporate_action_gate.get("external_attestation") or {}).get("passed") is True,
            "corporate_action_failed_gates": corporate_action_gate.get("failed_gates") or [],
            "settled_30bp_cumulative_net_pnl": (settled_scenarios.get("round_trip_30bp") or {}).get("cumulative_net_pnl"),
            "settled_50bp_cumulative_net_pnl": (settled_scenarios.get("round_trip_50bp") or {}).get("cumulative_net_pnl"),
            "universe_eligible_symbols": corporate_universe.get("eligible_symbols"),
            "universe_delisted_or_outdated_symbols": corporate_universe.get("delisted_or_outdated_symbols"),
            "universe_listed_after_research_start": corporate_universe.get("listed_after_research_start"),
            "pure_current_survivor_pool": corporate_universe.get("pure_current_survivor_pool"),
            "temporal_dependence_gate_passed": temporal_gate.get("temporal_dependence_gate_passed") is True,
            "temporal_signal_date_clusters": temporal_gate.get("signal_date_clusters", 0),
            "temporal_required_signal_date_clusters": 32,
            "temporal_50bp_block_bootstrap_ci_low": (temporal_50bp.get("moving_block_bootstrap_ci_95") or [None])[0],
            "temporal_recent_12_mean_return_pct": temporal_50bp.get("recent_cluster_mean_return_pct"),
            "temporal_failed_gates": temporal_gate.get("failed_gates") or [],
            "temporal_external_attestation_passed": (temporal_gate.get("external_attestation") or {}).get("passed") is True,
            "historical_v46_signal_date_clusters": v46_observed_temporal.get("signal_date_clusters"),
            "historical_v46_block_bootstrap_ci_low": (v46_observed_temporal.get("moving_block_bootstrap_ci_95") or [None])[0],
            "historical_v46_2026_net_pnl": v46_observed_2026.get("net_pnl"),
            "historical_v46_2026_win_rate_pct": v46_observed_2026.get("win_rate_pct"),
            "selection_benchmark_gate_passed": selection_benchmark_gate.get("selection_benchmark_gate_passed") is True,
            "selection_epoch_id": selection_epoch.get("epoch_id"),
            "selection_epoch_family_size": selection_epoch.get("family_size"),
            "selection_historical_evidence_excluded": selection_epoch.get("historical_evidence_excluded") is True,
            "benchmark_common_sessions": benchmark_50bp.get("common_sessions", 0),
            "benchmark_required_common_sessions": 60,
            "benchmark_50bp_total_excess_return_pp": benchmark_50bp.get("total_excess_return_pp"),
            "benchmark_50bp_annualized_alpha_pct": benchmark_50bp.get("annualized_regression_alpha_pct"),
            "benchmark_50bp_active_information_ratio": benchmark_50bp.get("active_information_ratio"),
            "selection_benchmark_failed_gates": selection_benchmark_gate.get("failed_gates") or [],
            "selection_benchmark_external_attestation_passed": (selection_benchmark_gate.get("external_attestation") or {}).get("passed") is True,
            "historical_v46_inner_validation_trials": selection_lineage.get("v46_inner_validation_trials"),
            "historical_global_trial_family_reproducible": selection_lineage.get("global_trial_family_size_reproducible") is True,
            "historical_v46_benchmark_total_excess_return_pp": v46_benchmark.get("total_excess_return_pp"),
            "historical_v46_active_information_ratio": v46_benchmark.get("active_information_ratio"),
            "historical_v46_2026_benchmark_excess_return_pp": v46_benchmark_2026.get("total_excess_return_pp"),
            "multi_benchmark_gate_passed": multi_benchmark_gate.get("multi_benchmark_gate_passed") is True,
            "multi_benchmark_common_sessions": multi_benchmark_gate.get("minimum_common_sessions", 0),
            "multi_benchmark_required_common_sessions": 60,
            "multi_benchmark_50bp": multi_benchmark_50bp,
            "multi_benchmark_failed_gates": multi_benchmark_gate.get("failed_gates") or [],
            "multi_benchmark_external_attestation_passed": (multi_benchmark_gate.get("external_attestation") or {}).get("passed") is True,
            "historical_v46_multi_benchmark": v46_multi_observed,
            "historical_v46_multi_benchmark_worst_excess_return_pp": (multi_benchmark_audit.get("observed_test_gate_diagnostic") or {}).get("worst_total_excess_return_pp"),
            "historical_v46_multi_benchmark_worst_active_information_ratio": (multi_benchmark_audit.get("observed_test_gate_diagnostic") or {}).get("worst_active_information_ratio"),
            "contributor_concentration_gate_passed": contributor_concentration_gate.get("contributor_concentration_gate_passed") is True,
            "concentration_signal_quarters": contributor_concentration_gate.get("signal_quarters", 0),
            "concentration_required_signal_quarters": 3,
            "concentration_50bp_max_code_share": (concentration_50bp.get("code") or {}).get("largest_positive_contribution_share"),
            "concentration_50bp_max_industry_share": (concentration_50bp.get("industry") or {}).get("largest_positive_contribution_share"),
            "concentration_50bp_max_quarter_share": (concentration_50bp.get("signal_quarter") or {}).get("largest_positive_contribution_share"),
            "concentration_failed_gates": contributor_concentration_gate.get("failed_gates") or [],
            "concentration_external_attestation_passed": (contributor_concentration_gate.get("external_attestation") or {}).get("passed") is True,
            "historical_v46_validation_max_quarter_share": (v46_concentration_validation.get("signal_quarter") or {}).get("largest_positive_contribution_share"),
            "historical_v46_observed_max_code_share": (v46_concentration_observed.get("code") or {}).get("largest_positive_contribution_share"),
            "historical_v46_observed_max_industry_share": (v46_concentration_observed.get("industry") or {}).get("largest_positive_contribution_share"),
            "historical_v46_observed_max_quarter_share": (v46_concentration_observed.get("signal_quarter") or {}).get("largest_positive_contribution_share"),
            "historical_v46_concentration_mapped_trades": (v46_concentration_observed.get("industry_mapping") or {}).get("mapped_trades"),
            "model_training_integrity_gate_passed": model_training_integrity_gate.get("model_training_integrity_gate_passed") is True,
            "training_overlap_dates": model_training_integrity_gate.get("training_overlap_dates") or [],
            "training_overlap_selected_rows": model_training_integrity_gate.get("training_overlap_selected_rows", 0),
            "training_overlap_finite_label_rows": model_training_integrity_gate.get("training_overlap_finite_label_rows", 0),
            "purged_validation_annualized_return_pct": purged_validation.get("annualized_return_pct"),
            "purged_validation_max_drawdown_pct": purged_validation.get("max_drawdown_pct"),
            "purged_validation_profit_factor": purged_validation.get("profit_factor"),
            "purged_validation_trades": purged_validation.get("trades"),
            "training_validation_annualized_change_pp": training_comparison.get("validation_annualized_change_pp"),
            "training_integrity_failed_gates": model_training_integrity_gate.get("failed_gates") or [],
            "training_integrity_external_attestation_passed": (model_training_integrity_gate.get("external_attestation") or {}).get("passed") is True,
            "strict_historical_candidate_eligible": model_training_integrity_gate.get("historical_candidate_eligible") is True,
            "successor_admission_gate_passed": successor_admission_gate.get("successor_admission_gate_passed") is True,
            "successor_discovery_allowed": successor_discovery_allowed,
            "successor_backtest_allowed": successor_admission_gate.get("successor_backtest_allowed") is True,
            "successor_discovery_round": successor_admission_gate.get("discovery_round"),
            "successor_discovery_round_limit": successor_admission_gate.get("maximum_discovery_rounds_per_epoch"),
            "successor_discovery_round_cap_reached": successor_cap_reached,
            "successor_new_data_epoch_ready": successor_admission_gate.get("new_data_epoch_ready") is True,
            "successor_candidate_id": successor_admission_gate.get("candidate_id"),
            "successor_data_epoch_id": successor_admission_gate.get("data_epoch_id"),
            "successor_latest_candidate_ids": successor_admission_gate.get("latest_discovery_candidate_ids") or [],
            "successor_latest_registry_verdict": successor_admission_gate.get("latest_registry_verdict"),
            "successor_failed_gates": successor_admission_gate.get("failed_gates") or [],
            "successor_next_action": successor_next_action,
            "successor_v87_mapping_invalidated": successor_mapping_failure.get("mapping_invalidated") is True,
            "successor_v87_mapping_citic_bank_rows": successor_mapping_failure.get("rows_mapped_to_citic_bank_count", 0),
            "successor_mapping_epoch_id": successor_mapping_audit.get("mapping_epoch_id"),
            "successor_mapping_status": successor_mapping_audit.get("status"),
            "successor_mapping_data_ready": successor_mapping_audit.get("mapping_data_ready") is True,
            "successor_mapping_candidate_usable_records": successor_mapping_summary.get("candidate_usable_records", 0),
            "successor_mapping_review_completed": successor_mapping_review.get("completed", 0),
            "successor_mapping_review_required": successor_mapping_review.get("required", 200),
            "successor_mapping_failed_gates": successor_mapping_audit.get("failed_gates") or [],
            "successor_mapping_backtest_allowed": successor_mapping_audit.get("factor_backtest_allowed") is True,
            "successor_mapping_next_action": successor_mapping_audit.get("next_action"),
            "successor_v89_data_epoch_id": successor_v89_epoch.get("data_epoch_id"),
            "successor_v89_status": successor_v89_epoch.get("status"),
            "successor_v89_record_count": successor_v89_epoch.get("record_count", 0),
            "successor_v89_direct_single_a_share_records": successor_v89_epoch.get("direct_single_a_share_records", 0),
            "successor_v89_data_ready": successor_v89_epoch.get("new_data_epoch_ready") is True,
            "successor_v89_discovery_allowed": successor_v89_epoch.get("successor_discovery_allowed") is True,
            "successor_v89_backtest_allowed": successor_v89_epoch.get("factor_backtest_allowed") is True,
            "successor_active_discovery_epoch_id": successor_discovery_assessment.get("data_epoch_id") or successor_discovery_round.get("data_epoch_id"),
            "successor_active_discovery_round": successor_discovery_assessment.get("data_epoch_round") or successor_discovery_assessment.get("discovery_round") or successor_discovery_round.get("data_epoch_round") or successor_discovery_round.get("discovery_round"),
            "successor_active_discovery_status": successor_discovery_assessment.get("status"),
            "successor_active_discovery_candidate_ids": successor_discovery_assessment.get("candidate_ids") or [],
            "successor_active_discovery_semantic_integrity_passed": successor_discovery_assessment.get("semantic_integrity_passed") is True,
            "successor_active_discovery_actionable_candidate": successor_discovery_assessment.get("actionable_candidate") is True,
            "final_candidate_failed_gates": final_gate.get("failed_gates") or [],
            "promotion_failed_gates": promotion.get("failed_gates") or [],
            "evidence_sufficient": model_training_integrity_gate.get("historical_candidate_eligible") is True,
            "source": (
                "reports/v86_expectancy_forward_evaluation.json"
                if evaluation else "reports/v86_expectancy_forward_latest.json"
            ),
        },
    }
