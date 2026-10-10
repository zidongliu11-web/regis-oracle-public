"""Base, read-only evidence assembly for the strategy lab.

Dependencies stay explicit so app-level monkeypatches and project-root overrides remain valid.
"""
from __future__ import annotations

from pathlib import Path


def build_strategy_lab_base(
    project_root: Path,
    *,
    read_json,
    latest_paper_forward_report,
    sha256_matches,
) -> dict[str, object]:
    """Return the small, auditable strategy-development view used by the SPA."""
    reports_dir = project_root / "reports"
    report_names = {
        "ridge_v1": "five_day_ridge_strategy_v1.json",
        "trend_cash_v2": "five_day_trend_cash_strategy_v2.json",
        "twenty_day_v3": "twenty_day_ridge_trend_strategy_v3.json",
        "v3_root_cause": "twenty_day_v3_root_cause_audit.json",
        "defensive_v1": "focused_defensive_reversal_v1.json",
        "index_v1": "index_dual_momentum_v1.json",
        "index_v2": "index_risk_premium_trend_v2.json",
        "fundamental_audit": "fundamental_pit_readiness_audit_v1.json",
        "implied_value_v1": "implied_profitability_value_trend_v1.json",
        "implied_value_root": "implied_value_v1_root_cause_audit.json",
        "mechanism_audit": "next_strategy_mechanism_selection_audit.json",
        "v23_extension": "v23_extended_history_replication_v1.json",
        "dividend_readiness": "trailing_dividend_data_readiness_v1.json",
        "sustained_dividend": "sustained_dividend_yield_monthly_v1.json",
        "sustained_dividend_root": "sustained_dividend_v1_root_cause_audit.json",
        "sustained_vol_budget": "sustained_dividend_volatility_budget_v1.json",
        "vol_budget_lag": "volatility_budget_v1_lag_audit.json",
        "absolute_momentum": "sustained_dividend_absolute_momentum_v1.json",
        "execution_integrity": "sustained_dividend_execution_integrity_v1.json",
        "downside_beta": "sustained_dividend_downside_beta_v1.json",
        "repurchase_reachability": "sustained_dividend_repurchase_reachability_v1.json",
        "low_market_beta": "v201_v4_low_market_beta_factor_test.json",
        "residual_momentum": "v203_v4_residual_momentum_factor_test.json",
        "multi_asset_rotation": "v205_multi_asset_defensive_rotation.json",
        "multi_asset_robustness": "v207_multi_asset_robustness_audit.json",
        "global_rotation": "v209_global_defensive_rotation.json",
        "global_robustness": "v211_global_defensive_robustness_audit.json",
        "paper_forward": "v213_global_defensive_paper_forward_baseline.json",
        "cash_sweep": "v217_cash_sweep_rotation.json",
        "qdii_unit_correction": "v219_qdii_split_nav_alignment_correction.json",
    }
    reports = {
        key: read_json(reports_dir / filename)
        for key, filename in report_names.items()
    }
    reports["paper_forward"] = latest_paper_forward_report()
    ridge = reports["ridge_v1"]
    trend = reports["trend_cash_v2"]
    twenty_day = reports["twenty_day_v3"]
    root_cause = reports["v3_root_cause"]
    index_v2 = reports["index_v2"]
    fundamental_audit = reports["fundamental_audit"]
    implied_value = reports["implied_value_v1"]
    implied_value_root = reports["implied_value_root"]
    mechanism_audit = reports["mechanism_audit"]
    v23_extension = reports["v23_extension"]
    dividend_readiness = reports["dividend_readiness"]
    sustained_dividend = reports["sustained_dividend"]
    sustained_dividend_root = reports["sustained_dividend_root"]
    sustained_vol_budget = reports["sustained_vol_budget"]
    vol_budget_lag = reports["vol_budget_lag"]
    absolute_momentum = reports["absolute_momentum"]
    execution_integrity = reports["execution_integrity"]
    downside_beta = reports["downside_beta"]
    repurchase_reachability = reports["repurchase_reachability"]
    low_market_beta = reports["low_market_beta"]
    residual_momentum = reports["residual_momentum"]
    multi_asset_rotation = reports["global_rotation"] or reports["multi_asset_rotation"]
    multi_asset_robustness = reports["global_robustness"] or reports["multi_asset_robustness"]
    paper_forward = reports["paper_forward"]
    cash_sweep = reports["cash_sweep"]
    qdii_unit_correction = reports["qdii_unit_correction"]

    def metric(report: dict[str, object], section: str, key: str):
        values = report.get(section)
        return values.get(key) if isinstance(values, dict) else None

    def current_action(report: dict[str, object]) -> str:
        current = report.get("current_paper_portfolio")
        if not isinstance(current, dict):
            current = report.get("current_paper_allocation")
        if not isinstance(current, dict):
            return "无当前动作"
        return str(current.get("action") or current.get("next_action") or "仅研究观察")

    def strategy_row(key: str, verdict: str) -> dict[str, object]:
        report = reports[key]
        return {
            "strategy_id": report.get("strategy_id"),
            "title": report.get("title") or report_names[key],
            "development_total_pct": metric(
                report, "development_metrics", "total_return_20bp_pct",
            ),
            "forward_total_pct": metric(
                report, "forward_2026_metrics", "total_return_20bp_pct",
            ),
            "forward_excess_pct": metric(
                report, "forward_2026_metrics", "total_excess_return_pct",
            ),
            "forward_drawdown_pct": metric(
                report, "forward_2026_metrics", "max_drawdown_20bp_pct",
            ),
            "current_action": current_action(report),
            "recommendation_eligible": bool(report.get("recommendation_eligible")),
            "verdict": verdict,
            "generated_at": report.get("generated_at"),
        }

    integrity_checks: list[dict[str, object]] = []
    check_specs = [
        ("ridge_v1", "spec_path", "spec_sha256", "Ridge V1 规格"),
        ("ridge_v1", "performance_path", "performance_sha256", "Ridge V1 逐期收益"),
        ("ridge_v1", "holdings_path", "holdings_sha256", "Ridge V1 逐期持仓"),
        ("trend_cash_v2", "spec_path", "spec_sha256", "趋势/现金 V2 规格"),
        ("trend_cash_v2", "evidence_path", "evidence_sha256", "趋势/现金 V2 收益"),
    ]
    if twenty_day:
        check_specs.extend([
            ("twenty_day_v3", "spec_path", "spec_sha256", "20日 V3 规格"),
            ("twenty_day_v3", "performance_path", "performance_sha256", "20日 V3 逐期收益"),
            ("twenty_day_v3", "holdings_path", "holdings_sha256", "20日 V3 逐期持仓"),
        ])
    if root_cause:
        check_specs.extend([
            ("v3_root_cause", "spec_path", "spec_sha256", "V3归因规格"),
            ("v3_root_cause", "evidence_path", "evidence_sha256", "V3归因明细"),
        ])
    if index_v2:
        check_specs.extend([
            ("index_v2", "spec_path", "spec_sha256", "宽基风险溢价 V2 规格"),
            ("index_v2", "monthly_evidence_path", "monthly_evidence_sha256", "宽基风险溢价 V2 逐月收益"),
        ])
    if fundamental_audit:
        check_specs.append((
            "fundamental_audit", "spec_path", "spec_sha256", "基本面PIT准入审计规格",
        ))
    if implied_value:
        check_specs.extend([
            ("implied_value_v1", "spec_path", "spec_sha256", "隐含盈利/价值 V1 规格"),
            ("implied_value_v1", "performance_path", "performance_sha256", "隐含盈利/价值 V1 逐月收益"),
            ("implied_value_v1", "holdings_path", "holdings_sha256", "隐含盈利/价值 V1 逐月持仓"),
        ])
    if implied_value_root:
        check_specs.extend([
            ("implied_value_root", "spec_path", "spec_sha256", "价值 V1 根因审计规格"),
            ("implied_value_root", "ic_path", "ic_sha256", "价值 V1 因子IC明细"),
            ("implied_value_root", "quintile_path", "quintile_sha256", "价值 V1 五分位收益"),
            ("implied_value_root", "attribution_path", "attribution_sha256", "价值 V1 持仓归因"),
        ])
    if mechanism_audit:
        check_specs.extend([
            ("mechanism_audit", "spec_path", "spec_sha256", "下一机制筛选规格"),
            ("mechanism_audit", "inventory_path", "inventory_sha256", "下一机制筛选清单"),
        ])
    if v23_extension:
        check_specs.extend([
            ("v23_extension", "spec_path", "spec_sha256", "V23扩展复验规格"),
            ("v23_extension", "annual_path", "annual_sha256", "V23扩展逐年证据"),
            ("v23_extension", "trades_path", "trades_sha256", "V23扩展逐笔账本"),
        ])
    if dividend_readiness:
        check_specs.extend([
            ("dividend_readiness", "spec_path", "spec_sha256", "持续分红数据准入规格"),
            ("dividend_readiness", "events_path", "events_sha256", "持续分红点时事件"),
        ])
    for report_key, path_key, sha_key, label in check_specs:
        report = reports[report_key]
        relative = report.get(path_key)
        declared = report.get(sha_key)
        path = project_root / str(relative) if relative else None
        passed = False
        if path and declared and path.is_file():
            stat = path.stat()
            passed = sha256_matches(
                str(path), stat.st_size, stat.st_mtime_ns, str(declared),
            )
        integrity_checks.append({"label": label, "passed": passed})
    if fundamental_audit:
        input_hashes = fundamental_audit.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "基本面PIT审计输入", "passed": inputs_passed})
    if mechanism_audit:
        input_hashes = mechanism_audit.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "下一机制筛选输入", "passed": inputs_passed})
    if v23_extension:
        input_hashes = v23_extension.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "V23扩展复验输入", "passed": inputs_passed})
    if dividend_readiness:
        input_hashes = dividend_readiness.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "持续分红数据准入输入", "passed": inputs_passed})
    if sustained_dividend:
        evidence = sustained_dividend.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        for path_key, sha_key, label in (
            ("monthly_path", "monthly_sha256", "持续股息率逐月收益"),
            ("selected_path", "selected_sha256", "持续股息率逐股持仓"),
            ("current_path", "current_sha256", "持续股息率当前研究候选"),
        ):
            relative = evidence.get(path_key)
            declared = evidence.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = sustained_dividend.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "持续股息率冻结输入", "passed": inputs_passed})
    if sustained_dividend_root:
        for path_key, sha_key, label in (
            ("spec_path", "spec_sha256", "持续股息率归因规格"),
            ("monthly_attribution_path", "monthly_attribution_sha256", "持续股息率月度归因"),
            ("selected_attribution_path", "selected_attribution_sha256", "持续股息率逐股归因"),
            ("group_attribution_path", "group_attribution_sha256", "持续股息率分组归因"),
            ("stock_attribution_path", "stock_attribution_sha256", "持续股息率股票贡献"),
            ("rolling_path", "rolling_sha256", "持续股息率滚动收益"),
        ):
            relative = sustained_dividend_root.get(path_key)
            declared = sustained_dividend_root.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = sustained_dividend_root.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "持续股息率归因输入", "passed": inputs_passed})
    if sustained_vol_budget:
        for path_key, sha_key, label in (
            ("spec_path", "spec_sha256", "固定波动预算规格"),
            ("monthly_path", "monthly_sha256", "固定波动预算逐月收益"),
        ):
            relative = sustained_vol_budget.get(path_key)
            declared = sustained_vol_budget.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = sustained_vol_budget.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "固定波动预算冻结输入", "passed": inputs_passed})
    if vol_budget_lag:
        for path_key, sha_key, label in (
            ("spec_path", "spec_sha256", "波动预算滞后归因规格"),
            ("monthly_path", "monthly_sha256", "波动预算逐月滞后归因"),
            ("group_path", "group_sha256", "波动预算仓位分组归因"),
        ):
            relative = vol_budget_lag.get(path_key)
            declared = vol_budget_lag.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = vol_budget_lag.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "波动预算滞后归因输入", "passed": inputs_passed})
    if absolute_momentum:
        for path_key, sha_key, label in (
            ("spec_path", "spec_sha256", "绝对动量现金层规格"),
            ("monthly_path", "monthly_sha256", "绝对动量现金层逐月收益"),
        ):
            relative = absolute_momentum.get(path_key)
            declared = absolute_momentum.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = absolute_momentum.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "绝对动量现金层冻结输入", "passed": inputs_passed})
    if execution_integrity:
        for path_key, sha_key, label in (
            ("spec_path", "spec_sha256", "股息执行完整性修正规格"),
            ("monthly_path", "monthly_sha256", "股息执行修正逐月收益"),
            ("targets_path", "targets_sha256", "股息执行修正信号目标"),
            ("ledger_path", "ledger_sha256", "股息执行修正交易账本"),
            ("current_path", "current_sha256", "股息执行修正当前研究候选"),
        ):
            relative = execution_integrity.get(path_key)
            declared = execution_integrity.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = execution_integrity.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "股息执行完整性冻结输入", "passed": inputs_passed})
    if downside_beta:
        for path_key, sha_key, label in (
            ("spec_path", "spec_sha256", "下行Beta防御筛选规格"),
            ("monthly_path", "monthly_sha256", "下行Beta逐月收益"),
            ("targets_path", "targets_sha256", "下行Beta筛选目标"),
            ("ledger_path", "ledger_sha256", "下行Beta执行账本"),
            ("current_path", "current_sha256", "下行Beta当前研究目标"),
        ):
            relative = downside_beta.get(path_key)
            declared = downside_beta.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = downside_beta.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "下行Beta防御筛选冻结输入", "passed": inputs_passed})
    if repurchase_reachability:
        for path_key, sha_key, label in (
            ("spec_path", "spec_sha256", "分红回购样本可达性规格"),
            ("monthly_path", "monthly_sha256", "分红回购逐月样本证据"),
            ("matched_path", "matched_sha256", "分红回购历史交集证据"),
            ("current_path", "current_sha256", "分红回购当前交集证据"),
        ):
            relative = repurchase_reachability.get(path_key)
            declared = repurchase_reachability.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = repurchase_reachability.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "分红回购样本可达性冻结输入", "passed": inputs_passed})
    if low_market_beta:
        for path_key, sha_key, label in (
            ("monthly_evidence_path", "monthly_evidence_sha256", "V201低市场Beta逐月收益"),
            ("selected_evidence_path", "selected_evidence_sha256", "V201低市场Beta逐股目标"),
        ):
            relative = low_market_beta.get(path_key)
            declared = low_market_beta.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = low_market_beta.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "V201低市场Beta冻结输入", "passed": inputs_passed})
    if residual_momentum:
        for path_key, sha_key, label in (
            ("monthly_evidence_path", "monthly_evidence_sha256", "V203残差动量逐月收益"),
            ("selected_evidence_path", "selected_evidence_sha256", "V203残差动量逐股执行"),
        ):
            relative = residual_momentum.get(path_key)
            declared = residual_momentum.get(sha_key)
            path = project_root / str(relative) if relative else None
            passed = False
            if path and declared and path.is_file():
                stat = path.stat()
                passed = sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                )
            integrity_checks.append({"label": label, "passed": passed})
        input_hashes = residual_momentum.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({"label": "V203残差动量冻结输入", "passed": inputs_passed})
    if multi_asset_rotation:
        input_hashes = multi_asset_rotation.get("input_hashes")
        inputs_passed = bool(input_hashes)
        if isinstance(input_hashes, dict):
            for relative, declared in input_hashes.items():
                path = project_root / str(relative)
                if not path.is_file():
                    inputs_passed = False
                    break
                stat = path.stat()
                if not sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    inputs_passed = False
                    break
        integrity_checks.append({
            "label": f"{multi_asset_rotation.get('strategy_id') or '多资产轮动'}冻结输入",
            "passed": inputs_passed,
        })
    if multi_asset_robustness:
        for key, label in (
            ("input_hashes", f"{multi_asset_robustness.get('audit_id') or '稳健性复核'}冻结输入"),
            ("evidence_hashes", f"{multi_asset_robustness.get('audit_id') or '稳健性复核'}输出证据"),
        ):
            hashes = multi_asset_robustness.get(key)
            hashes_passed = bool(hashes)
            if isinstance(hashes, dict):
                for relative, declared in hashes.items():
                    path = project_root / str(relative)
                    if not path.is_file():
                        hashes_passed = False
                        break
                    stat = path.stat()
                    if not sha256_matches(
                        str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                    ):
                        hashes_passed = False
                        break
            integrity_checks.append({"label": label, "passed": hashes_passed})
    if paper_forward:
        paper_forward_id = str(paper_forward.get("paper_forward_id") or "V213")
        for key, label in (
            ("input_hashes", f"{paper_forward_id}冻结输入"),
            ("evidence_hashes", f"{paper_forward_id}追加证据"),
        ):
            hashes = paper_forward.get(key)
            hashes_passed = bool(hashes)
            if isinstance(hashes, dict):
                for relative, declared in hashes.items():
                    path = project_root / str(relative)
                    if not path.is_file():
                        hashes_passed = False
                        break
                    stat = path.stat()
                    if not sha256_matches(
                        str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                    ):
                        hashes_passed = False
                        break
            integrity_checks.append({"label": label, "passed": hashes_passed})
    if cash_sweep:
        for key, label in (
            ("input_hashes", "V217现金袖套冻结输入"),
            ("evidence_hashes", "V217现金袖套失败证据"),
        ):
            hashes = cash_sweep.get(key)
            hashes_passed = bool(hashes)
            if isinstance(hashes, dict):
                for relative, declared in hashes.items():
                    path = project_root / str(relative)
                    if not path.is_file():
                        hashes_passed = False
                        break
                    stat = path.stat()
                    if not sha256_matches(
                        str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                    ):
                        hashes_passed = False
                        break
            integrity_checks.append({"label": label, "passed": hashes_passed})
    if qdii_unit_correction:
        for key, label in (
            ("input_hashes", "V219 QDII单位纠错冻结输入"),
            ("evidence_hashes", "V219 QDII单位纠错证据"),
        ):
            hashes = qdii_unit_correction.get(key)
            hashes_passed = bool(hashes)
            if isinstance(hashes, dict):
                for relative, declared in hashes.items():
                    path = project_root / str(relative)
                    if not path.is_file():
                        hashes_passed = False
                        break
                    stat = path.stat()
                    if not sha256_matches(
                        str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                    ):
                        hashes_passed = False
                        break
            integrity_checks.append({"label": label, "passed": hashes_passed})

    factor = ridge.get("factor_layer") if isinstance(ridge.get("factor_layer"), dict) else {}
    coefficients = factor.get("latest_coefficients")
    if not isinstance(coefficients, dict):
        coefficients = {}
    factor_v3 = (
        twenty_day.get("factor_layer")
        if isinstance(twenty_day.get("factor_layer"), dict) else {}
    )
    coefficients_v3 = factor_v3.get("latest_coefficients")
    if not isinstance(coefficients_v3, dict):
        coefficients_v3 = {}
    factor_labels = {
        "ret_5d": ("5日收益", "close / close[-5] - 1"),
        "ret_20d": ("20日收益", "close / close[-20] - 1"),
        "vol_20d": ("20日波动", "std(daily_return, 20)"),
        "ma_gap": ("均线偏离", "close / mean(close, 20) - 1"),
        "turnover_proxy": ("成交活跃度", "volume / mean(volume, 20)"),
    }
    features = []
    for name in factor.get("features", []):
        label, formula = factor_labels.get(str(name), (str(name), str(name)))
        features.append({
            "name": name,
            "label": label,
            "formula": formula,
            "coefficient": coefficients.get(name),
            "coefficient_v3": coefficients_v3.get(name),
        })

    latest_report = (
        qdii_unit_correction or multi_asset_rotation or residual_momentum or low_market_beta or repurchase_reachability or downside_beta or execution_integrity or absolute_momentum
        or sustained_vol_budget or sustained_dividend
        or implied_value or index_v2 or twenty_day or trend
    )
    robustness_passed = bool(
        not qdii_unit_correction
        and
        multi_asset_robustness
        and multi_asset_robustness.get("robustness_passed") is True
    )
    catchup = paper_forward.get("pre_freeze_catchup_diagnostic") or {}
    formal_forward = paper_forward.get("formal_forward") or {}
    if qdii_unit_correction:
        correction = qdii_unit_correction.get("correction") or {}
        corrected_metrics = (
            (qdii_unit_correction.get("cost_ladder_metrics") or {}).get("40.0")
            or {}
        )
        validation_2022 = (
            (corrected_metrics.get("validation_2022_2023") or {})
            .get("yearly_total_return_pct", {})
            .get("2022")
        )
        current = {
            "action": (
                "V219纠正了QDII复权价格/未复权净值单位错误，改变"
                f"{correction.get('affected_asset_month_decisions', 0)}条资产月准入、"
                f"{correction.get('affected_execution_months', 0)}个执行月；"
                f"纠正后2022年成本后收益{validation_2022}%未通过原规则，"
                "V209/V211/V213/V215全部撤销。当前无有效买卖信号，保持100%现金。"
            ),
            "signal_date": (
                (qdii_unit_correction.get("current_research_observation") or {})
                .get("signal_date")
            ),
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    elif multi_asset_rotation:
        allocation = (
            multi_asset_rotation.get("current_research_observation")
            or multi_asset_rotation.get("current_paper_allocation")
        )
        if not isinstance(allocation, dict):
            allocation = {}
        names = "/".join(
            f"{row.get('name')} {row.get('weight_pct')}%"
            for row in allocation.get("allocations") or []
            if isinstance(row, dict)
        )
        concentration = multi_asset_robustness.get("return_concentration") or {}
        current = {
            "action": (
                (
                    f"V209/V211稳健性复核通过，仅开放纸面前向观察；当前观察配置 {names}；"
                    f"V213冻结前追赶 {catchup.get('net_return_pct', '—')}%，"
                    f"V215正式前向 {formal_forward.get('completed_post_freeze_common_closes', 0)} 个共同收盘、"
                    f"累计 {formal_forward.get('formal_forward_return_pct', 0)}%"
                    if names else "V209/V211稳健性复核通过，当前观察配置为现金"
                )
                if robustness_passed else
                "多资产历史候选存在，但独立稳健性复核失败："
                f"黄金贡献占 {concentration.get('maximum_positive_asset_contribution_share_pct', '—')}%，"
                f"单一年份占 {concentration.get('maximum_positive_year_log_return_share_pct', '—')}%；"
                "当前ETF权重降级为研究观察，真钱与纸面执行均关闭"
            ),
            "signal_date": allocation.get("signal_date"),
            "cash_weight_pct": allocation.get("cash_weight_pct") if robustness_passed else 100.0,
            "risk_exposure_pct": allocation.get("risk_exposure_pct") if robustness_passed else 0.0,
            "market_regime_on": bool(allocation.get("risk_exposure_pct")) if robustness_passed else False,
            "pre_freeze_catchup_net_return_pct": catchup.get("net_return_pct"),
            "formal_forward_observations": formal_forward.get(
                "completed_post_freeze_common_closes"
            ),
            "formal_forward_return_pct": formal_forward.get("formal_forward_return_pct"),
        }
    elif residual_momentum:
        current = {
            "action": "V203市场中性残差动量验证与测试失败，保持现金",
            "signal_date": "2025-12",
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    elif low_market_beta:
        current = {
            "action": "V201全市场低Beta验证与测试均失败，保持现金",
            "signal_date": "2025-12",
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    elif repurchase_reachability:
        current_signal = repurchase_reachability.get("current_research_signal")
        if not isinstance(current_signal, dict):
            current_signal = {}
        current = {
            "action": "持续分红与公开市场回购交集样本不足，保持现金",
            "signal_date": current_signal.get("signal_date"),
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    elif downside_beta:
        current_signal = downside_beta.get("current_research_signal")
        if not isinstance(current_signal, dict):
            current_signal = {}
        current = {
            "action": "一年下行Beta防御筛选未达到历史稳定条件，保持现金",
            "signal_date": current_signal.get("signal_date"),
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    elif execution_integrity:
        current_signal = execution_integrity.get("current_research_signal")
        if not isinstance(current_signal, dict):
            current_signal = {}
        current = {
            "action": "持续股息率执行完整性修正后仍未达到历史稳定条件，保持现金",
            "signal_date": current_signal.get("signal_date"),
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    elif absolute_momentum:
        current_state = absolute_momentum.get("current_research_state")
        if not isinstance(current_state, dict):
            current_state = {}
        current = {
            "action": "12个月绝对动量方向层历史验证失败，保持现金",
            "signal_date": current_state.get("signal_date"),
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    elif sustained_vol_budget:
        current_allocation = sustained_vol_budget.get("current_research_allocation")
        if not isinstance(current_allocation, dict):
            current_allocation = {}
        current = {
            "action": "固定波动预算仍未达到历史稳定条件，保持现金",
            "signal_date": current_allocation.get("signal_date"),
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    elif sustained_dividend:
        current_signal = sustained_dividend.get("current_research_signal")
        if not isinstance(current_signal, dict):
            current_signal = {}
        current = {
            "action": "两年持续股息率历史稳定性不足，保持现金",
            "signal_date": current_signal.get("signal_date"),
            "cash_weight_pct": 100.0,
            "risk_exposure_pct": 0.0,
            "market_regime_on": False,
        }
    else:
        current = latest_report.get("current_paper_portfolio")
        if not isinstance(current, dict):
            current = latest_report.get("current_paper_allocation")
        if not isinstance(current, dict):
            current = {}
    available = bool(ridge and trend)
    passed_checks = sum(bool(row["passed"]) for row in integrity_checks)
    generated = sorted(
        str(report.get("generated_at"))
        for report in reports.values() if report.get("generated_at")
    )
    return {
        "available": available,
        "generated_at": generated[-1] if generated else None,
        "headline": {
            "status": (
                "corrected_failed" if qdii_unit_correction
                else "paper_candidate" if robustness_passed
                else "robustness_failed" if multi_asset_rotation
                else "cash" if available else "unavailable"
            ),
            "label": (
                "QDII单位纠错后历史候选失败 · 旧信号已撤销" if qdii_unit_correction
                else "稳健性复核通过 · 冻结纸面前向" if robustness_passed
                else "历史候选存在 · 稳健性复核未通过" if multi_asset_rotation
                else "当前无可买策略 · 保持现金" if available
                else "策略证据暂不可用"
            ),
            "reason": current.get("action") or "缺少本地策略报告",
            "signal_date": current.get("signal_date"),
            "cash_weight_pct": current.get("cash_weight_pct"),
            "risk_exposure_pct": current.get("risk_exposure_pct"),
            "market_regime_on": current.get("market_regime_on"),
            **({
                "pre_freeze_catchup_net_return_pct": current.get(
                    "pre_freeze_catchup_net_return_pct"
                ),
                "formal_forward_observations": current.get(
                    "formal_forward_observations"
                ),
                "formal_forward_return_pct": current.get(
                    "formal_forward_return_pct"
                ),
            } if paper_forward else {}),
            "recommendation_eligible": bool(
                not qdii_unit_correction
                and latest_report.get("recommendation_eligible")
            ),
        },
        "factor": {
            "strategy_id": ridge.get("strategy_id"),
            "universe": factor.get("universe"),
            "label": factor.get("label"),
            "features": features,
            "development_ic": metric(
                ridge, "development_metrics", "mean_cross_section_ic",
            ),
            "development_positive_ic_pct": metric(
                ridge, "development_metrics", "positive_ic_rate_pct",
            ),
            "forward_ic": metric(
                ridge, "forward_2026_metrics", "mean_cross_section_ic",
            ),
            "latest_forward_ic": metric(
                twenty_day or ridge,
                "forward_2026_metrics", "mean_cross_section_ic",
            ),
            "forward_positive_ic_pct": metric(
                ridge, "forward_2026_metrics", "positive_ic_rate_pct",
            ),
            "horizons": [
                {
                    "label": "5交易日 V1",
                    "development_ic": metric(ridge, "development_metrics", "mean_cross_section_ic"),
                    "forward_ic": metric(ridge, "forward_2026_metrics", "mean_cross_section_ic"),
                    "development_return_pct": metric(ridge, "development_metrics", "total_return_20bp_pct"),
                    "forward_return_pct": metric(ridge, "forward_2026_metrics", "total_return_20bp_pct"),
                    "average_turnover": None,
                },
                {
                    "label": "20交易日 V3",
                    "development_ic": metric(twenty_day, "development_metrics", "mean_cross_section_ic"),
                    "forward_ic": metric(twenty_day, "forward_2026_metrics", "mean_cross_section_ic"),
                    "development_return_pct": metric(twenty_day, "development_metrics", "total_return_20bp_pct"),
                    "forward_return_pct": metric(twenty_day, "forward_2026_metrics", "total_return_20bp_pct"),
                    "average_turnover": metric(twenty_day, "development_metrics", "average_two_sided_turnover"),
                },
            ] if twenty_day else [],
        },
        "latest_strategy": ({
            "strategy_id": qdii_unit_correction.get("strategy_id"),
            "title": "V219 QDII单位纠错后的全球防御轮动",
            "forward_total_pct": (
                ((((qdii_unit_correction.get("cost_ladder_metrics") or {}).get("40.0") or {}).get("diagnostic_2026_through_july") or {}).get("total_return_pct"))
            ),
            "recommendation_eligible": False,
            "invalidated": True,
        } if qdii_unit_correction else {
            "strategy_id": multi_asset_rotation.get("strategy_id"),
            "title": multi_asset_rotation.get("title"),
            "forward_total_pct": (
                (((multi_asset_rotation.get("cost_ladder_metrics") or {}).get("40.0") or {}).get("diagnostic_2026_through_july") or {}).get("total_return_pct")
            ),
            "recommendation_eligible": False,
        } if multi_asset_rotation else {
            "strategy_id": residual_momentum.get("candidate_id"),
            "title": "市场中性残差动量月度因子 V1",
            "forward_total_pct": (
                (residual_momentum.get("results") or {}).get("test") or {}
            ).get("total_return_20bp_pct"),
            "recommendation_eligible": False,
        } if residual_momentum else {
            "strategy_id": low_market_beta.get("candidate_id"),
            "title": "全市场252日低Beta月度因子 V1",
            "forward_total_pct": (
                (low_market_beta.get("results") or {}).get("test") or {}
            ).get("total_return_20bp_pct"),
            "recommendation_eligible": False,
        } if low_market_beta else {
            "strategy_id": repurchase_reachability.get("candidate_id"),
            "title": "持续股息率 + 公开市场回购交集 V1",
            "forward_total_pct": None,
            "recommendation_eligible": False,
        } if repurchase_reachability else {
            "strategy_id": downside_beta.get("candidate_id"),
            "title": "持续股息率 + 一年下行Beta防御筛选 V1",
            "forward_total_pct": metric(
                downside_beta, "results", "diagnostic_2026",
            ).get("total_return_20bp_pct") if isinstance(
                metric(downside_beta, "results", "diagnostic_2026"), dict
            ) else None,
            "recommendation_eligible": False,
        } if downside_beta else {
            "strategy_id": execution_integrity.get("candidate_id"),
            "title": "持续股息率执行完整性修正 V1",
            "forward_total_pct": metric(
                execution_integrity, "results", "diagnostic_2026",
            ).get("total_return_20bp_pct") if isinstance(
                metric(execution_integrity, "results", "diagnostic_2026"), dict
            ) else None,
            "recommendation_eligible": False,
        } if execution_integrity else {
            "strategy_id": absolute_momentum.get("candidate_id"),
            "title": "持续股息率 + 12个月绝对动量现金层 V1",
            "forward_total_pct": metric(
                absolute_momentum, "results", "diagnostic_2026",
            ).get("total_return_20bp_pct") if isinstance(
                metric(absolute_momentum, "results", "diagnostic_2026"), dict
            ) else None,
            "recommendation_eligible": False,
        } if absolute_momentum else {
            "strategy_id": sustained_vol_budget.get("candidate_id"),
            "title": "持续股息率 + 15%固定波动预算 V1",
            "forward_total_pct": metric(
                sustained_vol_budget, "results", "diagnostic_2026",
            ).get("total_return_20bp_pct") if isinstance(
                metric(sustained_vol_budget, "results", "diagnostic_2026"), dict
            ) else None,
            "recommendation_eligible": False,
        } if sustained_vol_budget else {
            "strategy_id": sustained_dividend.get("candidate_id"),
            "title": "两年持续股息率月度策略 V1",
            "forward_total_pct": metric(
                sustained_dividend, "results", "diagnostic_2026",
            ).get("total_return_20bp_pct") if isinstance(
                metric(sustained_dividend, "results", "diagnostic_2026"), dict
            ) else None,
            "recommendation_eligible": False,
        } if sustained_dividend else (
            strategy_row("implied_value_v1", "") if implied_value else
            strategy_row("index_v2", "") if index_v2 else
            strategy_row("twenty_day_v3", "") if twenty_day else
            strategy_row("trend_cash_v2", "")
        )),
        "strategies": ([
            strategy_row(
                "implied_value_v1", "未通过：开发期毛收益为负且仅2/7年度为正；当前保持现金",
            ),
        ] if implied_value else []) + ([
            strategy_row(
                "index_v2", "未通过：开发期毛收益为负，满仓放大了回撤；当前保持现金",
            ),
        ] if index_v2 else []) + ([
            strategy_row(
                "twenty_day_v3", "未通过：换手下降，但毛收益仍负且2026 IC反转",
            ),
        ] if twenty_day else []) + [
            strategy_row(
                "trend_cash_v2", "未通过：趋势层减少亏损，但2026成本后仍为负",
            ),
            strategy_row(
                "ridge_v1", "未通过：开发期收益为负，2026因子IC接近零",
            ),
            strategy_row(
                "defensive_v1", "未通过：开发期与2026成本后收益均为负",
            ),
            strategy_row(
                "index_v1", "未通过：回撤较低，但绝对收益没有转正",
            ),
        ],
        "diagnosis": [
            *([{
                "stage": "估值隐含盈利能力/多收益率价值 V1",
                "development_pct": metric(
                    implied_value, "development_metrics", "total_return_20bp_pct",
                ),
                "forward_pct": metric(
                    implied_value, "forward_2026_metrics", "total_return_20bp_pct",
                ),
                "finding": "开发期毛收益 -30.79%，成本仅拖累2.38个百分点且只有2/7年度为正；2026毛收益接近零，价值组合没有稳定优势。",
            }] if implied_value else []),
            *([{
                "stage": "宽基风险溢价/趋势现金 V2",
                "development_pct": metric(
                    index_v2, "development_metrics", "total_return_20bp_pct",
                ),
                "forward_pct": metric(
                    index_v2, "forward_2026_metrics", "total_return_20bp_pct",
                ),
                "finding": "开发期毛收益 -9.67%，10bp单边成本仅拖累2.65个百分点；满仓后回撤扩大到 -41.93%，机制本身未形成正收益。",
            }] if index_v2 else []),
            *([{
                "stage": "20交易日低换手 V3",
                "development_pct": metric(
                    twenty_day, "development_metrics", "total_return_20bp_pct",
                ),
                "forward_pct": metric(
                    twenty_day, "forward_2026_metrics", "total_return_20bp_pct",
                ),
                "finding": "换手下降24%，但开发期毛收益仍为负；2026 IC -0.157，成本不是主因。",
            }] if twenty_day else []),
            {
                "stage": "五日 Ridge V1",
                "development_pct": metric(
                    ridge, "development_metrics", "total_return_20bp_pct",
                ),
                "forward_pct": metric(
                    ridge, "forward_2026_metrics", "total_return_20bp_pct",
                ),
                "finding": "2026横截面IC从开发期0.071降至接近0，核心因子优势未延续。",
            },
            {
                "stage": "趋势/现金 V2",
                "development_pct": metric(
                    trend, "development_metrics", "total_return_20bp_pct",
                ),
                "forward_pct": metric(
                    trend, "forward_2026_metrics", "total_return_20bp_pct",
                ),
                "finding": "200日趋势把当前仓位降至0并缩小损失，但不能修复负毛收益。",
            },
        ],
        "root_cause": {
            "available": bool(root_cause),
            "primary": root_cause.get("primary_root_cause"),
            "conclusion": root_cause.get("conclusion"),
            "next_direction": root_cause.get("next_strategy_direction"),
            "cost": root_cause.get("cost_decomposition") or {},
            "concentration": root_cause.get("concentration") or {},
            "diagnostics": root_cause.get("diagnostics") or {},
            "worst_industries_2026": [
                row for row in (root_cause.get("industry_attribution") or [])
                if row.get("scope") == "2026" and row.get("contribution_pct", 0) < 0
            ][:5],
            "liquidity_2026": [
                row for row in (root_cause.get("liquidity_attribution") or [])
                if row.get("scope") == "2026"
            ],
            "honesty": root_cause.get("honesty"),
        },
        "latest_audit": {
            "available": bool(implied_value or index_v2),
            "strategy_id": (implied_value or index_v2).get("strategy_id"),
            "title": (implied_value or index_v2).get("title"),
            "development_gross_pct": metric(
                implied_value or index_v2, "development_metrics", "total_gross_return_pct",
            ),
            "development_net_pct": metric(
                implied_value or index_v2, "development_metrics", "total_return_20bp_pct",
            ),
            "development_cost_drag_pp": metric(
                implied_value or index_v2, "development_metrics", "cost_drag_10bp_each_way_pct_points",
            ),
            "development_drawdown_pct": metric(
                implied_value or index_v2, "development_metrics", "max_drawdown_20bp_pct",
            ),
            "forward_total_pct": metric(
                implied_value or index_v2, "forward_2026_metrics", "total_return_20bp_pct",
            ),
            "forward_excess_pct": metric(
                implied_value or index_v2, "forward_2026_metrics", "total_excess_return_pct",
            ),
            "positive_years": metric(
                implied_value or index_v2, "development_metrics", "positive_years",
            ),
            "development_years": metric(
                implied_value or index_v2, "development_metrics", "years",
            ),
            "pass_checks": (implied_value or index_v2).get("pass_checks") or {},
            "all_passed": bool((implied_value or index_v2).get("all_passed")),
            "current": current,
            "honesty": (implied_value or index_v2).get("honesty"),
        },
        "data_readiness": {
            "available": bool(fundamental_audit),
            "status": fundamental_audit.get("status"),
            "admission": fundamental_audit.get("admission") or {},
            "sources": fundamental_audit.get("sources") or {},
            "prior_mechanisms": fundamental_audit.get("prior_mechanisms") or [],
            "input_contract": fundamental_audit.get("next_strategy_input_contract") or {},
            "honesty": fundamental_audit.get("honesty"),
        },
        "value_root_cause": {
            "available": bool(implied_value_root),
            "primary": implied_value_root.get("primary_root_cause"),
            "conclusion": implied_value_root.get("conclusion"),
            "next_direction": implied_value_root.get("next_strategy_direction"),
            "diagnostics": implied_value_root.get("diagnostics") or {},
            "factor": implied_value_root.get("factor_summary") or {},
            "cost": implied_value_root.get("cost_decomposition") or {},
            "concentration": implied_value_root.get("concentration") or {},
            "development_industries": [
                row for row in (implied_value_root.get("industry_attribution") or [])
                if row.get("scope") == "development" and row.get("contribution_pct", 0) < 0
            ][:5],
            "development_sizes": [
                row for row in (implied_value_root.get("size_attribution") or [])
                if row.get("scope") == "development"
            ],
            "honesty": implied_value_root.get("honesty"),
        },
        "mechanism_selection": {
            "available": bool(mechanism_audit),
            "baseline": mechanism_audit.get("preferred_historical_baseline") or {},
            "decision": mechanism_audit.get("decision") or {},
            "inventory": mechanism_audit.get("inventory") or [],
            "sealed_factor_tests": mechanism_audit.get("sealed_factor_tests") or {},
            "honesty": mechanism_audit.get("honesty") or {},
            "superseded_by_v23_extension": bool(v23_extension),
        },
        "v23_extension": {
            "available": bool(v23_extension),
            "status": v23_extension.get("status"),
            "title": v23_extension.get("title"),
            "period": v23_extension.get("period") or {},
            "summary": v23_extension.get("summary") or {},
            "source_audit": v23_extension.get("source_audit") or {},
            "overlap": v23_extension.get("overlap_reproduction") or {},
            "conditions": v23_extension.get("stability_conditions") or {},
            "stable_profit_evidence_complete": bool(v23_extension.get("stable_profit_evidence_complete")),
            "current_action": v23_extension.get("current_action"),
            "next_action": v23_extension.get("next_action"),
            "honesty": v23_extension.get("honesty") or {},
        },
        "dividend_readiness": {
            "available": bool(dividend_readiness),
            "status": dividend_readiness.get("status"),
            "data_epoch_id": dividend_readiness.get("data_epoch_id"),
            "counts": dividend_readiness.get("counts") or {},
            "gates": dividend_readiness.get("gates") or {},
            "failed_gates": dividend_readiness.get("failed_gates") or [],
            "data_gate_passed": bool(dividend_readiness.get("data_gate_passed")),
            "return_data_read": bool(dividend_readiness.get("return_data_read")),
            "next_strategy_research_allowed": bool(dividend_readiness.get("next_strategy_research_allowed")),
            "safety": dividend_readiness.get("safety") or {},
        },
        "sustained_dividend": {
            "available": bool(sustained_dividend),
            "status": sustained_dividend.get("status"),
            "candidate_id": sustained_dividend.get("candidate_id"),
            "formula": sustained_dividend.get("formula"),
            "execution": sustained_dividend.get("execution") or {},
            "results": sustained_dividend.get("results") or {},
            "gate_conditions": sustained_dividend.get("gate_conditions") or {},
            "validation_folds": sustained_dividend.get("validation_folds") or [],
            "failed_gates": sustained_dividend.get("failed_gates") or [],
            "historical_mechanism_gate_passed": bool(
                sustained_dividend.get("historical_mechanism_gate_passed")
            ),
            "current_research_signal": sustained_dividend.get("current_research_signal") or {},
            "single_frozen_test_consumed": bool(
                sustained_dividend.get("single_frozen_test_consumed")
            ),
            "recommendation_eligible": bool(
                sustained_dividend.get("recommendation_eligible")
            ),
            "next_action": sustained_dividend.get("next_action"),
            "safety": sustained_dividend.get("safety") or {},
        },
        "sustained_dividend_root": {
            "available": bool(sustained_dividend_root),
            "status": sustained_dividend_root.get("status"),
            "primary": sustained_dividend_root.get("primary_root_cause"),
            "conclusion": sustained_dividend_root.get("conclusion"),
            "next_direction": sustained_dividend_root.get("next_strategy_direction"),
            "cost": sustained_dividend_root.get("cost_attribution") or {},
            "regime": sustained_dividend_root.get("market_regime_attribution") or {},
            "continuity": sustained_dividend_root.get("time_continuity") or {},
            "rank_gaps": sustained_dividend_root.get(
                "rank_band_gaps_b1_minus_b5_pct_points"
            ) or {},
            "balance_gaps": sustained_dividend_root.get(
                "balance_gaps_q1_minus_q4_pct_points"
            ) or {},
            "concentration": sustained_dividend_root.get("concentration") or {},
            "decision_flags": sustained_dividend_root.get("decision_flags") or {},
            "group_attribution": sustained_dividend_root.get("group_attribution") or [],
            "recommendation_eligible": bool(
                sustained_dividend_root.get("recommendation_eligible")
            ),
            "safety": sustained_dividend_root.get("safety") or {},
        },
        "sustained_vol_budget": {
            "available": bool(sustained_vol_budget),
            "status": sustained_vol_budget.get("status"),
            "candidate_id": sustained_vol_budget.get("candidate_id"),
            "formula": sustained_vol_budget.get("formula") or {},
            "results": sustained_vol_budget.get("results") or {},
            "gate_conditions": sustained_vol_budget.get("gate_conditions") or {},
            "validation_folds": sustained_vol_budget.get("validation_folds") or [],
            "rolling_12m": sustained_vol_budget.get("rolling_12m") or {},
            "failed_gates": sustained_vol_budget.get("failed_gates") or [],
            "historical_risk_layer_gate_passed": bool(
                sustained_vol_budget.get("historical_risk_layer_gate_passed")
            ),
            "current_research_allocation": sustained_vol_budget.get(
                "current_research_allocation"
            ) or {},
            "recommendation_eligible": bool(
                sustained_vol_budget.get("recommendation_eligible")
            ),
            "next_action": sustained_vol_budget.get("next_action"),
            "safety": sustained_vol_budget.get("safety") or {},
        },
        "vol_budget_lag": {
            "available": bool(vol_budget_lag),
            "status": vol_budget_lag.get("status"),
            "primary": vol_budget_lag.get("primary_root_cause"),
            "conclusion": vol_budget_lag.get("conclusion"),
            "next_direction": vol_budget_lag.get("next_strategy_direction"),
            "sign_attribution": vol_budget_lag.get("base_return_sign_attribution") or {},
            "benchmark_attribution": vol_budget_lag.get("benchmark_regime_attribution") or {},
            "correlation": vol_budget_lag.get(
                "risk_weight_to_next_base_return_correlation"
            ),
            "weight_gap_pp": vol_budget_lag.get(
                "positive_minus_negative_month_mean_weight_pct_points"
            ),
            "worst_10_mean_weight_pct": vol_budget_lag.get(
                "worst_10_mean_risk_weight_pct"
            ),
            "post_loss": vol_budget_lag.get("post_loss_response") or {},
            "decision_flags": vol_budget_lag.get("decision_flags") or {},
            "worst_10_months": vol_budget_lag.get("worst_10_months") or [],
            "group_attribution": vol_budget_lag.get("group_attribution") or [],
            "recommendation_eligible": bool(
                vol_budget_lag.get("recommendation_eligible")
            ),
            "safety": vol_budget_lag.get("safety") or {},
        },
        "absolute_momentum": {
            "available": bool(absolute_momentum),
            "status": absolute_momentum.get("status"),
            "candidate_id": absolute_momentum.get("candidate_id"),
            "formula": absolute_momentum.get("formula") or {},
            "results": absolute_momentum.get("results") or {},
            "gate_conditions": absolute_momentum.get("gate_conditions") or {},
            "validation_folds": absolute_momentum.get("validation_folds") or [],
            "rolling_12m": absolute_momentum.get("rolling_12m") or {},
            "failed_gates": absolute_momentum.get("failed_gates") or [],
            "historical_direction_layer_gate_passed": bool(
                absolute_momentum.get("historical_direction_layer_gate_passed")
            ),
            "current_research_state": absolute_momentum.get(
                "current_research_state"
            ) or {},
            "recommendation_eligible": bool(
                absolute_momentum.get("recommendation_eligible")
            ),
            "next_action": absolute_momentum.get("next_action"),
            "safety": absolute_momentum.get("safety") or {},
        },
        "execution_integrity": {
            "available": bool(execution_integrity),
            "status": execution_integrity.get("status"),
            "candidate_id": execution_integrity.get("candidate_id"),
            "formula_unchanged": bool(execution_integrity.get("formula_unchanged")),
            "selection_correction": execution_integrity.get("selection_correction"),
            "execution_policy": execution_integrity.get("execution_policy") or {},
            "results": execution_integrity.get("results") or {},
            "original_results": execution_integrity.get("original_results") or {},
            "execution_20bp": execution_integrity.get("execution_20bp") or {},
            "execution_40bp": execution_integrity.get("execution_40bp") or {},
            "execution_gates": execution_integrity.get("execution_gates") or {},
            "gate_conditions": execution_integrity.get("gate_conditions") or {},
            "validation_folds": execution_integrity.get("validation_folds") or [],
            "failed_gates": execution_integrity.get("failed_gates") or [],
            "historical_execution_integrity_gate_passed": bool(
                execution_integrity.get("historical_execution_integrity_gate_passed")
            ),
            "current_research_signal": execution_integrity.get(
                "current_research_signal"
            ) or {},
            "recommendation_eligible": bool(
                execution_integrity.get("recommendation_eligible")
            ),
            "next_action": execution_integrity.get("next_action"),
            "safety": execution_integrity.get("safety") or {},
        },
        "downside_beta": {
            "available": bool(downside_beta),
            "status": downside_beta.get("status"),
            "candidate_id": downside_beta.get("candidate_id"),
            "formula": downside_beta.get("formula") or {},
            "results": downside_beta.get("results") or {},
            "corrected_base_results": downside_beta.get("corrected_base_results") or {},
            "coverage": downside_beta.get("coverage") or {},
            "execution_20bp": downside_beta.get("execution_20bp") or {},
            "execution_gates": downside_beta.get("execution_gates") or {},
            "gate_conditions": downside_beta.get("gate_conditions") or {},
            "validation_folds": downside_beta.get("validation_folds") or [],
            "failed_gates": downside_beta.get("failed_gates") or [],
            "historical_defensive_selection_gate_passed": bool(
                downside_beta.get("historical_defensive_selection_gate_passed")
            ),
            "current_research_signal": downside_beta.get(
                "current_research_signal"
            ) or {},
            "recommendation_eligible": bool(
                downside_beta.get("recommendation_eligible")
            ),
            "next_action": downside_beta.get("next_action"),
            "safety": downside_beta.get("safety") or {},
        },
        "repurchase_reachability": {
            "available": bool(repurchase_reachability),
            "status": repurchase_reachability.get("status"),
            "audit_id": repurchase_reachability.get("audit_id"),
            "candidate_id": repurchase_reachability.get("candidate_id"),
            "formula": repurchase_reachability.get("formula") or {},
            "distinction_from_v187": repurchase_reachability.get(
                "distinction_from_v187"
            ),
            "source_epoch": repurchase_reachability.get("source_epoch") or {},
            "coverage": repurchase_reachability.get("coverage") or {},
            "gate_conditions": repurchase_reachability.get("gate_conditions") or {},
            "failed_gates": repurchase_reachability.get("failed_gates") or [],
            "sample_reachability_gate_passed": bool(
                repurchase_reachability.get("sample_reachability_gate_passed")
            ),
            "portfolio_backtest_admitted": bool(
                repurchase_reachability.get("portfolio_backtest_admitted")
            ),
            "backtest_run": bool(repurchase_reachability.get("backtest_run")),
            "return_data_read": bool(
                repurchase_reachability.get("return_data_read")
            ),
            "current_research_signal": repurchase_reachability.get(
                "current_research_signal"
            ) or {},
            "recommendation_eligible": bool(
                repurchase_reachability.get("recommendation_eligible")
            ),
            "next_action": repurchase_reachability.get("next_action"),
            "safety": repurchase_reachability.get("safety") or {},
        },
        "multi_asset_rotation": {
            "available": bool(multi_asset_rotation),
            "strategy_id": multi_asset_rotation.get("strategy_id"),
            "title": multi_asset_rotation.get("title"),
            "status": multi_asset_rotation.get("status"),
            "historical_candidate_passed": bool(
                multi_asset_rotation.get("historical_candidate_passed")
                and not qdii_unit_correction
            ),
            "paper_forward_eligible": bool(
                not qdii_unit_correction
                and
                multi_asset_rotation.get("historical_candidate_passed")
                and multi_asset_robustness.get("paper_forward_eligible")
            ),
            "v205_declared_paper_forward_eligible": bool(
                multi_asset_rotation.get("paper_forward_eligible")
            ),
            "recommendation_eligible": bool(
                multi_asset_rotation.get("recommendation_eligible")
            ),
            "automatic_trading": bool(multi_asset_rotation.get("automatic_trading")),
            "rules": multi_asset_rotation.get("rules") or {},
            "data_source": multi_asset_rotation.get("data_source") or {},
            "data_audit": multi_asset_rotation.get("data_audit") or {},
            "cost_ladder_metrics": multi_asset_rotation.get("cost_ladder_metrics") or {},
            "historical_candidate_checks": (
                multi_asset_rotation.get("historical_candidate_checks")
                or multi_asset_rotation.get("checks") or {}
            ),
            "current_paper_allocation": (
                {}
                if qdii_unit_correction
                else multi_asset_rotation.get("current_research_observation")
                or multi_asset_rotation.get("current_paper_allocation") or {}
            ),
            "research_observation_allocation": (
                multi_asset_rotation.get("current_research_observation")
                or multi_asset_rotation.get("current_paper_allocation") or {}
            ),
            "paid_api_used": bool(multi_asset_rotation.get("paid_api_used")),
            "external_model_used": bool(multi_asset_rotation.get("external_model_used")),
            "honesty": multi_asset_rotation.get("honesty"),
            "revoked_by_v219": bool(qdii_unit_correction),
        },
        "multi_asset_robustness": {
            "available": bool(multi_asset_robustness),
            "audit_id": multi_asset_robustness.get("audit_id"),
            "status": multi_asset_robustness.get("status"),
            "robustness_passed": robustness_passed,
            "paper_forward_eligible": bool(
                multi_asset_robustness.get("paper_forward_eligible")
                and not qdii_unit_correction
            ),
            "checks": multi_asset_robustness.get("checks") or {},
            "independent_reproduction": (
                multi_asset_robustness.get("independent_reproduction") or {}
            ),
            "leave_one_asset_out": (
                multi_asset_robustness.get("leave_one_asset_out") or {}
            ),
            "parameter_neighborhood": (
                multi_asset_robustness.get("parameter_neighborhood") or {}
            ),
            "cost_and_execution_perturbation": (
                multi_asset_robustness.get("cost_and_execution_perturbation")
                or multi_asset_robustness.get("cost_and_signal_delay") or {}
            ),
            "return_concentration": (
                multi_asset_robustness.get("return_concentration") or {}
            ),
            "conclusion": multi_asset_robustness.get("conclusion"),
            "honesty": multi_asset_robustness.get("honesty"),
            "recommendation_eligible": False,
            "automatic_trading": False,
            "revoked_by_v219": bool(qdii_unit_correction),
        },
        "paper_forward": {
            "available": bool(paper_forward),
            "paper_forward_id": paper_forward.get("paper_forward_id"),
            "status": (
                "revoked_parent_qdii_unit_mismatch"
                if qdii_unit_correction
                else paper_forward.get("status") or "missing"
            ),
            "pre_freeze_catchup_diagnostic": catchup,
            "formal_forward": formal_forward,
            "refresh": paper_forward.get("refresh") or {},
            "receipt_chain": paper_forward.get("receipt_chain") or {},
            "stable_profit_proven": bool(paper_forward.get("stable_profit_proven")),
            "scheduled_task_created": bool(
                paper_forward.get("scheduled_task_created")
            ),
            "generated_at": paper_forward.get("generated_at"),
            "recommendation_eligible": False,
            "real_money_eligible": False,
            "automatic_trading": False,
            "honesty": paper_forward.get("honesty"),
            "revoked_by_v219": bool(qdii_unit_correction),
        },
        "qdii_unit_correction": {
            "available": bool(qdii_unit_correction),
            "strategy_id": qdii_unit_correction.get("strategy_id"),
            "status": qdii_unit_correction.get("status") or "missing",
            "correction": qdii_unit_correction.get("correction") or {},
            "cost_ladder_metrics": qdii_unit_correction.get("cost_ladder_metrics") or {},
            "checks": qdii_unit_correction.get("checks") or {},
            "current_research_observation": qdii_unit_correction.get("current_research_observation") or {},
            "invalidates_audit_id": qdii_unit_correction.get("invalidates_audit_id"),
            "invalidates_paper_forward_ids": qdii_unit_correction.get("invalidates_paper_forward_ids") or [],
            "historical_candidate_passed": False,
            "paper_forward_eligible": False,
            "recommendation_eligible": False,
            "real_money_eligible": False,
            "automatic_trading": False,
            "conclusion": qdii_unit_correction.get("conclusion"),
            "honesty": qdii_unit_correction.get("honesty"),
        },
        "cash_sweep": {
            "available": bool(cash_sweep),
            "strategy_id": cash_sweep.get("strategy_id"),
            "status": cash_sweep.get("status") or "missing",
            "historical_candidate_passed": bool(
                cash_sweep.get("historical_candidate_passed")
            ),
            "rules": cash_sweep.get("rules") or {},
            "data_audit": cash_sweep.get("data_audit") or {},
            "cost_ladder_metrics": cash_sweep.get("cost_ladder_metrics") or {},
            "checks": cash_sweep.get("checks") or {},
            "current_research_observation": (
                cash_sweep.get("current_research_observation") or {}
            ),
            "next_action": cash_sweep.get("next_action"),
            "recommendation_eligible": False,
            "real_money_eligible": False,
            "automatic_trading": False,
            "honesty": cash_sweep.get("honesty"),
        },
        "residual_momentum": {
            "available": bool(residual_momentum),
            "status": residual_momentum.get("status"),
            "candidate_id": residual_momentum.get("candidate_id"),
            "factor_registry": residual_momentum.get("factor_registry") or {},
            "execution": residual_momentum.get("execution") or {},
            "results": residual_momentum.get("results") or {},
            "gate_conditions": residual_momentum.get("gate_conditions") or {},
            "test_folds": residual_momentum.get("test_folds") or [],
            "failed_gates": residual_momentum.get("failed_gates") or [],
            "historical_factor_gate_passed": bool(
                residual_momentum.get("historical_factor_gate_passed")
            ),
            "single_factor_test_consumed": bool(
                residual_momentum.get("single_factor_test_consumed")
            ),
            "recommendation_eligible": bool(
                residual_momentum.get("recommendation_eligible")
            ),
            "next_action": residual_momentum.get("next_action"),
            "safety": residual_momentum.get("safety") or {},
        },
        "low_market_beta": {
            "available": bool(low_market_beta),
            "status": low_market_beta.get("status"),
            "candidate_id": low_market_beta.get("candidate_id"),
            "factor_registry": low_market_beta.get("factor_registry") or {},
            "execution": low_market_beta.get("execution") or {},
            "results": low_market_beta.get("results") or {},
            "gate_conditions": low_market_beta.get("gate_conditions") or {},
            "test_folds": low_market_beta.get("test_folds") or [],
            "failed_gates": low_market_beta.get("failed_gates") or [],
            "historical_factor_gate_passed": bool(
                low_market_beta.get("historical_factor_gate_passed")
            ),
            "single_factor_test_consumed": bool(
                low_market_beta.get("single_factor_test_consumed")
            ),
            "recommendation_eligible": bool(
                low_market_beta.get("recommendation_eligible")
            ),
            "next_action": low_market_beta.get("next_action"),
            "safety": low_market_beta.get("safety") or {},
        },
        "pipeline": ([
            {"name": "数据单位", "state": "已纠正", "detail": "收益用复权价 · 溢价用未复权价格/净值"},
            {"name": "影响范围", "state": "前向失效", "detail": "146条资产月准入 · 82个执行月改变"},
            {"name": "开发", "state": "正收益", "detail": "40BP累计+85.87% · 回撤-9.26%"},
            {"name": "历史验证", "state": "未通过", "detail": "累计+10.94% · 2022年-2.50%"},
            {"name": "测试", "state": "正收益", "detail": "40BP累计+42.17% · 120BP仍+34.18%"},
            {"name": "旧候选", "state": "永久失败", "detail": "V209/V211/V213/V215全部撤销"},
            {"name": "当前动作", "state": "当前生效", "detail": "无买卖信号 · 保持100%现金"},
        ] if qdii_unit_correction else [
            {"name": "独立复算", "state": "完全一致", "detail": "150/150个月 · 权重零误差"},
            {"name": "资产剔除", "state": "正收益", "detail": "5/5变体通过"},
            {"name": "参数邻域", "state": "正收益", "detail": "72/72变体通过"},
            {"name": "执行压力", "state": "正收益", "detail": "高成本/信号延迟4/6通过"},
            {"name": "收益集中", "state": "正收益", "detail": "最高资产36.69% · 单一年份40.27% < 65%"},
            {"name": "前向时钟", "state": "证据不足", "detail": "冻结前追赶+0.0585% · 正式前向0个收盘"},
            {"name": "现金袖套", "state": "永久失败", "detail": "验证/测试改善 · 2016年-1.17%击穿逐年条件"},
        ] if multi_asset_robustness else [
            {"name": "可成交资产", "state": "已固定", "detail": "沪深300ETF · 5年国债ETF · 黄金ETF"},
            {"name": "信号与定权", "state": "已冻结", "detail": "12个月正动量Top2 · 6个月逆波动"},
            {"name": "验证", "state": "正收益", "detail": "40BP累计+7.95% · 回撤-1.45%"},
            {"name": "测试", "state": "正收益", "detail": "40BP累计+57.05% · 回撤-2.39%"},
            {"name": "当前动作", "state": "当前生效", "detail": "仅纸面：黄金50.79% · 沪深300 49.21%"},
        ] if multi_asset_rotation else [
            {"name": "机制", "state": "已冻结", "detail": "252日OLS Alpha · 跳过近20日 · 月频最高10%"},
            {"name": "物理输入", "state": "已核验", "detail": "4802/4802份哈希日线 · 上证基准"},
            {"name": "验证", "state": "未通过", "detail": "年化-19.58% · 超额-8.98% · 回撤-30.05%"},
            {"name": "测试", "state": "未通过", "detail": "年化-2.80% · 超额-11.83% · 胜率37.5%"},
            {"name": "当前动作", "state": "当前生效", "detail": "V203永久封存 · 保持100%现金"},
        ] if residual_momentum else [
            {"name": "机制", "state": "已冻结", "detail": "252共同交易日OLS Beta · 月频最低10%"},
            {"name": "物理输入", "state": "已核验", "detail": "4802/4802份哈希日线 · 上证基准"},
            {"name": "验证", "state": "未通过", "detail": "年化-7.24% · 超额+3.37% · Bootstrap下界为负"},
            {"name": "测试", "state": "未通过", "detail": "年化-3.41% · 超额-12.45% · 三折超额全负"},
            {"name": "当前动作", "state": "当前生效", "detail": "V201永久封存 · 保持100%现金"},
        ] if low_market_beta else [
            {"name": "行为数据", "state": "已核验", "detail": "V186 1558条公开市场回购事件"},
            {"name": "点时交集", "state": "已完成", "detail": "365自然日窗口 · 未读取候选收益"},
            {"name": "历史覆盖", "state": "样本不足", "detail": "78个月中位7只 · 仅3个月达到20只"},
            {"name": "当前覆盖", "state": "样本不足", "detail": "184只分红目标仅9只有回购交集"},
            {"name": "当前动作", "state": "当前生效", "detail": "未运行回测 · 保持100%现金"},
        ] if repurchase_reachability else [
            {"name": "修正基线", "state": "已核验", "detail": "+35.73%开发 · +21.42%验证"},
            {"name": "防御筛选", "state": "已完成", "detail": "10551 → 5259个低下行Beta目标"},
            {"name": "开发审计", "state": "证据不足", "detail": "+29.25% · 年化6.77% · 回撤-16.01%"},
            {"name": "历史验证", "state": "证据不足", "detail": "+14.67% · 年化7.08% · 超额-1.95%"},
            {"name": "当前动作", "state": "当前生效", "detail": "2026 +1.72%仅诊断 · 保持100%现金"},
        ] if downside_beta else [
            {"name": "候选时点", "state": "已修正", "detail": "只用信号日字段 · 10551个目标槽"},
            {"name": "真实执行", "state": "已入账", "detail": "75次无法买入 · 23次延期退出"},
            {"name": "开发审计", "state": "正收益", "detail": "+35.73% · 年化8.11% · 回撤-16.50%"},
            {"name": "历史验证", "state": "证据不足", "detail": "+21.42% · 首折-0.81% · Bootstrap下界为负"},
            {"name": "当前动作", "state": "当前生效", "detail": "修正后仍未达标 · 保持100%现金"},
        ] if execution_integrity else [
            {"name": "选股", "state": "正收益", "detail": "持续股息率基线公式与成本均不变"},
            {"name": "开发回撤", "state": "改善", "detail": "+24.21% · 回撤-8.27%"},
            {"name": "历史验证", "state": "未通过", "detail": "成本后-4.06% · 回撤-14.42%"},
            {"name": "时点连续性", "state": "恶化", "detail": "2024 -11.05% · 负12月窗口45%"},
            {"name": "当前动作", "state": "当前生效", "detail": "方向层不采纳 · 保持100%现金"},
        ] if absolute_momentum else [
            {"name": "选股", "state": "正收益", "detail": "持续股息率开发/验证均为正"},
            {"name": "波动层", "state": "部分改善", "detail": "验证回撤改善 · 开发未改善"},
            {"name": "信号辨别", "state": "不足", "detail": "负月仓位92.20% · 正月88.22%"},
            {"name": "最差月份", "state": "高暴露", "detail": "最差10月平均风险仓位86.55%"},
            {"name": "当前动作", "state": "当前生效", "detail": "波动层不采纳 · 保持100%现金"},
        ] if vol_budget_lag else [
            {"name": "选股", "state": "正收益", "detail": "持续股息率公式完全不变"},
            {"name": "根因", "state": "已定位", "detail": "系统性暴露 · 月度Beta 0.99"},
            {"name": "风险层", "state": "部分改善", "detail": "验证+19.09% · 回撤-11.06%"},
            {"name": "连续性", "state": "证据不足", "detail": "2022仍-9.73% · 首折-0.98%"},
            {"name": "当前动作", "state": "当前生效", "detail": "研究仓位不采纳 · 保持100%现金"},
        ] if sustained_vol_budget else [
            {"name": "数据", "state": "已核验", "detail": "78个月 · 10415条逐股账本精确复算"},
            {"name": "选股机制", "state": "正收益", "detail": "开发+37.76% · 验证+15.36%"},
            {"name": "失败主因", "state": "已定位", "detail": "下跌月承担89.39%负贡献 · Beta 0.99"},
            {"name": "排除项", "state": "已完成", "detail": "成本/单股集中/极端尾部均非主因"},
            {"name": "当前动作", "state": "证据不足", "detail": "缺现金风险层 · 保持100%现金"},
        ] if sustained_dividend_root else [
            {"name": "数据", "state": "已核验", "detail": "24965条点时现金分红 · 4802份日线"},
            {"name": "信号", "state": "已冻结", "detail": "两段365天取较小值 / 月末股价"},
            {"name": "开发审计", "state": "正收益", "detail": "20BP +37.76% · 年化8.52%"},
            {"name": "历史验证", "state": "正收益", "detail": "20BP +15.36% · 40BP +9.95%"},
            {"name": "当前动作", "state": "证据不足", "detail": "2026 -0.19% · 保持100%现金"},
        ] if sustained_dividend else [
            {"name": "数据", "state": "已核验", "detail": "763只事件股票 · 指数1098行重叠一致"},
            {"name": "实现复现", "state": "完全一致", "detail": "2024—2026五项核心指标一致"},
            {"name": "扩展样本", "state": "已完成", "detail": "2018—2026 · 118笔"},
            {"name": "成本后", "state": "证据不足", "detail": "10BP +1.43% · 20BP -0.38%"},
            {"name": "当前动作", "state": "当前生效", "detail": "跨周期失效 · 保持100%现金"},
        ] if v23_extension else [
            {"name": "机制盘点", "state": "已完成", "detail": "V23是唯一成本后正收益基线"},
            {"name": "历史基线", "state": "正收益", "detail": "成本后 +5.64% · 回撤 -5.79%"},
            {"name": "稳定性", "state": "证据不足", "detail": "46笔 · 胜率54.35% · 年化2.22%"},
            {"name": "当前动作", "state": "当前生效", "detail": "规则不满足 · 保持100%现金"},
            {"name": "下一项目", "state": "已冻结", "detail": "V23规则不变 · 扩展2018—2023"},
        ] if mechanism_audit else [
            {"name": "数据", "state": "已核验", "detail": "V63 4802/4802 哈希一致"},
            {"name": "因子", "state": "极端尾部失效", "detail": "IC +0.036 · 但Q5弱于Q1"},
            {"name": "组合", "state": "未通过", "detail": "Q5开发毛收益 -2.00%"},
            {"name": "风控", "state": "当前生效", "detail": "趋势关闭 · 100%现金"},
            {"name": "2026诊断", "state": "未通过", "detail": "成本后 -0.05%"},
        ] if implied_value_root else [
            {"name": "数据", "state": "已核验", "detail": "V63 4802/4802 哈希一致"},
            {"name": "因子", "state": "已完成", "detail": "隐含盈利 + 4项价值等权"},
            {"name": "组合", "state": "未通过", "detail": "开发期 -33.17%"},
            {"name": "风控", "state": "当前生效", "detail": "趋势关闭 · 100%现金"},
            {"name": "2026诊断", "state": "未通过", "detail": "成本后 -0.05%"},
        ] if implied_value else [
            {"name": "机制", "state": "已切换", "detail": "宽基风险溢价 + 绝对趋势"},
            {"name": "组合", "state": "已完成", "detail": "最多2指数 · 倒数波动满仓"},
            {"name": "执行", "state": "成本已核", "detail": "开发期拖累 2.65pp"},
            {"name": "风控", "state": "当前生效", "detail": "趋势关闭 · 100%现金"},
            {"name": "2026诊断", "state": "未通过", "detail": "宽基 V2 -4.92%"},
        ] if index_v2 else [
            {"name": "因子", "state": "前向反转", "detail": "20日 V3 IC -0.157"},
            {"name": "组合", "state": "已完成", "detail": "Top 10% · 30只等权"},
            {"name": "执行", "state": "换手已降", "detail": "1.143 → 0.866"},
            {"name": "风控", "state": "当前生效", "detail": "200日趋势 · 100%现金"},
            {"name": "2026诊断", "state": "未通过", "detail": "V3 -22.66%"},
        ]),
        "next_work": ([
            "V219已把QDII溢价统一为未复权收盘价/未复权净值；146条资产月准入和82个执行月变化，旧复算只证明了同一错误，不能继续使用。",
            "纠正后40BP开发/验证/测试累计为+85.87%/+10.94%/+42.17%，120BP验证/测试仍为+7.09%/+34.18%；唯一失败是2022年-2.50%的逐年正收益条件。",
            "当前先按真实状态撤销V209/V211/V213/V215并保持100%现金；下一步只在这份纠正数据上做2022亏损归因和一条ETF组合规则改造。",
        ] if qdii_unit_correction else [
            "V211独立实现已逐月复现V209：150/150个月，月收益最大误差9.89e-17、净值最大误差2.22e-16、权重误差0。",
            "五项稳健性全部通过：剔除任一资产5/5、参数邻域72/72、高成本与额外一月信号延迟4/6；160/200BP叠加一月延迟的验证期为负，边界风险公开保留。",
            "V213纸面基线已建立：8月3—11日追赶诊断40BP后+0.058517%，但该行情在冻结时已存在，不计入独立前向；正式前向0个共同收盘、累计0%，真钱推荐、订单和自动交易关闭。",
            "V217把剩余现金配置到511880后，40BP验证/测试/2026诊断提高至+15.01%/+43.29%/+1.37%，但2016年为-1.17%，击穿预先冻结的逐年为正条件；本版本永久失败，不替代V209。",
        ] if multi_asset_robustness else [
            "已停止逐个股票因子失败循环，固定三只真实可成交ETF：510300、511010、518880；新浪公开日线与累计分红无需密钥，原始快照和实现均已哈希。",
            "40BP下开发累计+43.07%、验证+7.95%、测试+57.05%；2022—2025四个自然年均为正，80BP压力成本下验证与测试仍为正。",
            "2026年1—7月仅+1.51%且期间回撤-8.16%，测试期也已被本轮查看；当前黄金50.79%/沪深300ETF49.21%只能作为冻结纸面配置，继续积累真正前向证据。",
        ] if multi_asset_rotation else [
            "V202在读取收益前冻结252日OLS Alpha、跳过近20日、最高十分组、20/40BP成本和全部失败规则；随后V203逐份核验4802个哈希日线，没有调用外部模型、网络或付费API。",
            "验证年化-19.58%、超额-8.98%、回撤-30.05%；测试年化-2.80%、超额-11.83%、胜率37.5%，第一时间折超额-21.37%，属于明确失效。",
            "执行账本先选股后检查成交：142个目标留现金、122个延期退出、1个数据末端未解决；永久封存本公式，推荐、资金分配、订单和自动交易继续关闭。",
        ] if residual_momentum else [
            "V201在冻结前完成5/5收益前准入，随后核验4802/4802份日线并消耗唯一一次低市场Beta月度检验；没有调用外部模型或付费API。",
            "训练审计年化+15.92%，但验证年化-7.24%、测试年化-3.41%，测试总超额-25.59个百分点，三折超额全部为负，属于明确的跨阶段失效。",
            "永久封存252日上证Beta最低十分组；不改窗口、基准、方向或成本补救，推荐、资金分配、订单和自动交易继续关闭。",
        ] if low_market_beta else [
            "持续分红与V186真实公开市场回购完成点时交集盘点：历史10551个信号目标仅734个命中，78个月每月中位数7只，最少1只。",
            "只有3/78个月达到事前冻结的20只组合门槛；当前2026-07-31的184只分红目标中仅9只命中，因此组合会高度集中且不可稳定复现。",
            "本项目没有读取候选收益、没有运行回测，也没有产生买入清单；淘汰365天回购硬交集，不改窗口或门槛追结果，网站保持100%现金。",
        ] if repurchase_reachability else [
            "一年下行Beta筛选把10551个修正目标压缩到5259个；开发回撤只从-16.50%改善到-16.01%，却把累计收益从+35.73%降到+29.25%、年化降到6.77%。",
            "验证累计从修正基线+21.42%降到+14.67%，年化7.08%、年化超额-1.95%，验证回撤反而从-11.02%变为-11.44%；因此它没有形成有效防御交换。",
            "2026诊断从-0.68%改善到+1.72%，但不能覆盖跨阶段失败；终止252日/50%下行Beta形式，当前92只只是研究目标，网站继续100%现金。",
        ] if downside_beta else [
            "执行泄漏已修正：月度目标从旧账本10415条变为10551条；75个目标无法买入而保留现金，23笔月末无法卖出并真实延期，最长13天，数据截止前全部解决。",
            "修正后开发20BP为+35.73%（年化8.11%、回撤-16.50%），验证为+21.42%（年化10.19%、回撤-11.02%）；旧的+37.76%/+15.36%不再作为最新基线。",
            "仍未达到稳定条件：2022年-10.18%、验证首折-0.81%、两段Bootstrap下界为负，2026诊断-0.68%；当前184只仍只是研究排名，网站保持100%现金。",
        ] if execution_integrity else [
            "12个月绝对动量现金层完成：开发期20BP后+24.21%，回撤从-16.74%改善到-8.27%；但只保留了66.79%的基础年化收益。",
            "历史验证失败：20BP后-4.06%，2024年-11.05%，第二折-14.34%；负滚动12月窗口从26.67%恶化到45.00%，2026诊断仍为-0.19%。",
            "终止252交易日/零阈值这一方向层，不调整窗口或阈值补救；当前公式虽显示风险开启，也仅是研究状态，网站继续100%现金。",
        ] if absolute_momentum else [
            "60日波动预算辨别力不足：基础负收益月平均风险仓位92.20%，高于正收益月88.22%；最差10个月平均仍有86.55%风险仓位。",
            "不是固定晚一个月：亏损后下月平均仓位变化仅+0.02个百分点，只有19.35%的亏损月后出现降仓；继续改波动目标或窗口没有依据。",
            "下一大项目预注册12个月绝对动量现金层：只用信号日前252个上证交易日总收益判断风险开关，不使用MA200，不改股息选股和成本。",
        ] if vol_budget_lag else [
            "15%目标/60日实现波动现金层完成：历史验证20BP从+15.36%改善到+19.09%，回撤从-14.42%改善到-11.06%，2026诊断从-0.19%改善到+0.76%。",
            "仍未解决开发期连续性：开发回撤-16.80%没有改善，2022年-9.73%，验证首折-0.98%；当前84.37%仅是研究权重，不进入资金分配。",
            "下一大项目归因风险层响应滞后：检查最差月份前的60日波动和仓位，决定是否需要完全不同的前瞻风险状态；不调整15%或60日重跑。",
        ] if sustained_vol_budget else [
            "持续股息率失败主因已定位：2020—2025下跌月承担89.39%的全部负月贡献，月度Beta 0.99；下跌月累计-61.13%，非下跌月累计+308.79%。",
            "成本、极端股息尾部和单股集中均非主因：40BP后仍盈利，B1最高股息档未持续落后，最差10只仅占负贡献3.16%/4.89%。",
            "下一大项目预注册固定波动预算现金层，不使用已失败的MA200；只解决系统性暴露，不改持续股息选股公式，当前仍不产生买入提醒。",
        ] if sustained_dividend_root else [
            "两年持续股息率策略一次冻结运行完成：开发期20BP后+37.76%，历史验证+15.36%，40BP压力后仍分别为+25.40%和+9.95%。",
            "尚未达到稳定条件：开发/验证年化8.52%/7.40%，2022年-10.04%，验证首折-0.35%，2026截至7月-0.19%。",
            "下一大项目只做失败归因：核对收益分位、年度月份和持仓贡献，确认优势为何不连续；当前184只仅为研究候选，不进入买入提醒。",
        ] if sustained_dividend else [
            "新机制数据准入完成：4802个CNINFO本地响应全部核验，得到24965条规则性现金分红，覆盖4216只股票。",
            "2026年已有2824条事件、截止2026-08-10；本轮没有读取任何股票收益，也没有调用外部模型。",
            "下一大项目预注册两年持续股息率月度策略；规则冻结后只运行一次成本后历史验证。",
        ] if dividend_readiness else [
            "V23扩展复验完成：118笔、10BP后+1.43%、年化0.18%、胜率45.76%，只剩极弱正收益。",
            "20BP后转为-0.38%，2019—2022连续为负；2024—2026局部盈利不能代表跨周期稳定性。",
            "终止当前V23事件+MA200形式，不以持有期、仓位或MA窗口调参补救；下一机制必须重新选择。",
        ] if v23_extension else [
            "机制盘点完成：V23是唯一成本后正收益且当前账本复核通过的本地历史基线。",
            "V23仍未证明稳定盈利：46笔、胜率54.35%、年化2.22%，当前报告快照尚未完成隔离复现且正式前向为0笔。",
            str((mechanism_audit.get("decision") or {}).get("next_big_project") or "保持V23规则不变，扩展早期历史复验。"),
        ] if mechanism_audit else [
            "价值 V1 根因已定位：综合IC为正但Q5极端便宜尾部弱于Q1，五分位不单调。",
            "行业和规模集中均未达到解释阈值，禁止用行业/规模补丁对本次结果做事后修复。",
            str(implied_value_root.get("next_strategy_direction") or "终止当前极端价值选股形式，转向不同信息机制。"),
        ] if implied_value_root else [
            "估值隐含盈利能力/多收益率价值 V1 已冻结验收并失败，不调整五因子等权或30只持仓规则重跑。",
            "下一大项先归因价值组合：检查收益分位单调性、行业/规模集中、价值陷阱与成本占比，再决定是否终止该信息族。",
            "当前继续100%现金；诊断结论同步完成前不开始新策略。",
        ] if implied_value else [
            "基本面PIT审计已完成：仅V63的4802只日度估值历史获准，V12与THS质量字段不进入策略。",
            "下一大项预注册估值隐含盈利能力+价值策略；不得原样重复V64、V65或V68。",
            "新策略仍须逐一展示开发期、2026诊断、交易成本、最大回撤和当前纸面动作。",
        ] if fundamental_audit else [
            "宽基风险溢价/趋势满仓 V2 已冻结验收并失败，不再围绕同一组参数继续调优。",
            "下一大项先审计本地基本面时点数据，再预注册质量/价值复合策略，避免继续依赖纯量价机制。",
            "新策略仍须逐一展示开发期、2026诊断、交易成本、最大回撤和当前纸面动作。",
        ] if index_v2 else [
            "停止继续拉长持有期或做行业/流动性补丁；归因已证明它们不是主因。",
            str(root_cause.get("next_strategy_direction") or "下一策略必须使用不同信息机制。"),
            "任何新版本必须同时展示开发期、2026前向、成本和当前纸面动作。",
        ]),
        "integrity": {
            "passed": passed_checks,
            "total": len(integrity_checks),
            "all_passed": passed_checks == len(integrity_checks),
            "checks": integrity_checks,
        },
    }
