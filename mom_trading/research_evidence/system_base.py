"""Base, read-only system-evidence assembly with explicit app dependencies."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path


def build_system_evidence(
    project_root: Path,
    decision_gate: dict[str, object] | None = None,
    *,
    sh_tz,
    decision_gate_status,
    latest_paper_forward_report,
    manifest_physical_evidence,
    read_json,
    v90_pmi_physical_evidence,
    v120_pbc_physical_evidence,
    v143_nbs_cpi_ppi_physical_evidence,
    v145_nbs_industrial_profit_physical_evidence,
    index_data_evidence,
    trading_day_status,
) -> dict[str, object]:
    """Summarize the real data chain and measurable profitability gaps."""
    _PROJECT = project_root
    _SH_TZ = sh_tz
    _decision_gate_status = decision_gate_status
    _latest_paper_forward_report = latest_paper_forward_report
    _manifest_physical_evidence = manifest_physical_evidence
    _read_json = read_json
    _v90_pmi_physical_evidence = v90_pmi_physical_evidence
    _v120_pbc_physical_evidence = v120_pbc_physical_evidence
    _v143_nbs_cpi_ppi_physical_evidence = v143_nbs_cpi_ppi_physical_evidence
    _v145_nbs_industrial_profit_physical_evidence = (
        v145_nbs_industrial_profit_physical_evidence
    )
    reports = _PROJECT / "reports"
    report_artifacts = []
    report_fingerprint = hashlib.sha256()
    for path in sorted(reports.glob("*.json")):
        try:
            stat = path.stat()
        except OSError:
            continue
        report_fingerprint.update(
            f"{path.name}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8")
        )
        report_artifacts.append({
            "id": path.stem,
            "name": path.name,
            "bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, _SH_TZ
            ).isoformat(timespec="seconds"),
            "website_visible": True,
        })
    progress = _read_json(reports / "v4_daily_incremental_progress.json")
    update = _read_json(reports / "v4_daily_incremental_update.json")
    daily_quality = _read_json(reports / "v4_daily_quality_audit.json")
    snapshot = _read_json(_PROJECT / "research" / "quant_stock_snapshot.json")
    production = _read_json(_PROJECT / "research" / "production_results.json")
    regime_candidate = _read_json(
        _PROJECT / "research" / "reports" / "strict_pit_regime_candidate.json"
    )
    cross_holdout = _read_json(
        _PROJECT / "research" / "reports" / "low_vol_cross_section_holdout.json"
    )
    taskbook_v3 = _read_json(reports / "taskbook_v3_gate_result.json")
    v12_fundamentals = _read_json(
        reports / "v12_fundamental_builder_status.json"
    )
    v148_filing_times = _read_json(
        reports / "v148_cninfo_filing_time_data_epoch_audit.json"
    )
    v148_filing_status = _read_json(
        reports / "v148_cninfo_filing_time_data_epoch_status.json"
    )
    v149_xbrl_profitability = _read_json(
        reports / "v149_sse_xbrl_profitability_data_epoch_audit.json"
    )
    v149_xbrl_status = _read_json(
        reports / "v149_sse_xbrl_profitability_data_epoch_status.json"
    )
    v150_roe_epoch = _read_json(
        reports / "v150_sse_xbrl_roe_data_epoch_audit.json"
    )
    v151_roe_admission = _read_json(
        reports / "v151_sse_annual_roe_decile_prebacktest_gate.json"
    )
    v152_adjusted_prices = _read_json(
        reports / "v152_v151_adjusted_price_data_epoch_audit.json"
    )
    v153_roe_factor = _read_json(
        reports / "v153_v151_sse_annual_roe_decile_factor_test.json"
    )
    v154_asset_growth = _read_json(
        reports / "v154_quarterly_asset_growth_data_epoch_audit.json"
    )
    v154_asset_growth_status = _read_json(
        reports / "v154_quarterly_asset_growth_data_epoch_status.json"
    )
    v155_asset_growth_admission = _read_json(
        reports / "v155_quarterly_asset_growth_decile_prebacktest_gate.json"
    )
    v156_asset_growth_prices = _read_json(
        reports / "v156_v155_adjusted_price_data_epoch_audit.json"
    )
    v157_asset_growth_factor = _read_json(
        reports / "v157_v155_quarterly_asset_growth_factor_test.json"
    )
    v158_eastmoney_asset_growth = _read_json(
        reports / "v158_eastmoney_quarterly_asset_growth_data_epoch_audit.json"
    )
    v158_eastmoney_asset_growth_status = _read_json(
        reports / "v158_eastmoney_quarterly_asset_growth_data_epoch_status.json"
    )
    v162_asset_growth = _read_json(
        reports / "v162_sse_delayed_pdf_asset_growth_data_epoch_audit.json"
    )
    v162_asset_growth_status = _read_json(
        reports / "v162_sse_delayed_pdf_asset_growth_data_epoch_status.json"
    )
    v163_asset_growth_admission = _read_json(
        reports / "v163_quarterly_asset_growth_decile_prebacktest_gate.json"
    )
    v164_asset_growth_prices = _read_json(
        reports / "v164_v163_adjusted_price_data_epoch_audit.json"
    )
    v165_asset_growth_factor = _read_json(
        reports / "v165_v163_quarterly_asset_growth_factor_test.json"
    )
    v166_industry_profit_panel = _read_json(
        reports / "v166_nbs_industry_profit_panel_data_epoch_audit.json"
    )
    v167_industry_profit_admission = _read_json(
        reports / "v167_nbs_industry_profit_breadth_prebacktest_gate.json"
    )
    v168_industrial_index_prices = _read_json(
        reports / "v168_csi300_industrial_price_data_epoch_audit.json"
    )
    v169_industry_profit_integrity = _read_json(
        reports / "v169_v167_nbs_industry_profit_breadth_factor_test.json"
    )
    v170_industry_profit_acceleration_admission = _read_json(
        reports / "v170_nbs_industry_profit_breadth_acceleration_prebacktest_gate.json"
    )
    v171_all_share_industrial_prices = _read_json(
        reports / "v171_csi_all_share_industrial_price_data_epoch_audit.json"
    )
    v172_industry_profit_acceleration = _read_json(
        reports / "v172_v170_nbs_industry_profit_breadth_acceleration_factor_test.json"
    )
    v173_pmi_demand_inventory = _read_json(
        reports / "v173_nbs_pmi_demand_inventory_data_epoch_audit.json"
    )
    v174_pmi_gap_acceleration_admission = _read_json(
        reports / "v174_nbs_pmi_demand_inventory_gap_acceleration_prebacktest_gate.json"
    )
    v175_all_share_energy_prices = _read_json(
        reports / "v175_csi_all_share_energy_price_data_epoch_audit.json"
    )
    v176_pmi_gap_acceleration = _read_json(
        reports / "v176_v174_nbs_pmi_gap_acceleration_factor_test.json"
    )
    v89_sse_regulatory_measures = _read_json(
        reports / "v89_sse_regulatory_measures_data_epoch_audit.json"
    )
    v177_sse_disciplinary_admission = _read_json(
        reports / "v177_sse_public_disciplinary_holding_risk_prebacktest_gate.json"
    )
    v178_sse_disciplinary_prices = _read_json(
        reports / "v178_sse_disciplinary_event_price_data_epoch_audit.json"
    )
    v179_seasonality_admission = _read_json(
        reports / "v179_sse_same_calendar_month_seasonality_prebacktest_gate.json"
    )
    v180_seasonality_factor = _read_json(
        reports / "v180_v179_same_calendar_month_seasonality_factor_test.json"
    )
    v181_filing_timeliness_admission = _read_json(
        reports / "v181_cninfo_annual_filing_timeliness_prebacktest_gate.json"
    )
    v182_filing_timeliness_factor = _read_json(
        reports / "v182_v181_annual_filing_timeliness_factor_test.json"
    )
    v183_local_order_book = _read_json(
        reports / "v183_local_order_book_data_epoch_audit.json"
    )
    v184_cash_dividend_epoch = _read_json(
        reports / "v184_cninfo_cash_dividend_data_epoch_audit.json"
    )
    v185_pre_ex_dividend_drift = _read_json(
        reports / "v185_cninfo_pre_ex_dividend_drift_factor_test.json"
    )
    v186_open_market_repurchase_epoch = _read_json(
        reports / "v186_cninfo_open_market_repurchase_epoch_audit.json"
    )
    v187_open_market_repurchase_drift = _read_json(
        reports / "v187_cninfo_open_market_repurchase_drift_factor_test.json"
    )
    v188_ipo_lockup_release_epoch = _read_json(
        reports / "v188_cninfo_ipo_lockup_release_epoch_audit.json"
    )
    v189_ipo_lockup_release_reversal = _read_json(
        reports / "v189_cninfo_ipo_lockup_release_reversal_factor_test.json"
    )
    v190_major_contract_epoch = _read_json(
        reports / "v190_cninfo_major_contract_epoch_audit.json"
    )
    v191_major_contract_5d_drift = _read_json(
        reports / "v191_cninfo_major_contract_5d_drift_factor_test.json"
    )
    v192_52_week_high_admission = _read_json(
        reports / "v192_v4_52_week_high_prebacktest_admission.json"
    )
    v193_52_week_high_factor = _read_json(
        reports / "v193_v4_52_week_high_monthly_factor_test.json"
    )
    v194_low_volatility_admission = _read_json(
        reports / "v194_v4_low_volatility_prebacktest_admission.json"
    )
    v195_low_volatility_factor = _read_json(
        reports / "v195_v4_low_volatility_monthly_factor_test.json"
    )
    v196_max_effect_admission = _read_json(
        reports / "v196_v4_max_effect_prebacktest_admission.json"
    )
    v197_low_max_factor = _read_json(
        reports / "v197_v4_low_max_monthly_factor_test.json"
    )
    v200_low_market_beta_admission = _read_json(
        reports / "v200_v4_low_market_beta_prebacktest_admission.json"
    )
    v201_low_market_beta_factor = _read_json(
        reports / "v201_v4_low_market_beta_factor_test.json"
    )
    v202_residual_momentum_admission = _read_json(
        reports / "v202_v4_residual_momentum_prebacktest_admission.json"
    )
    v203_residual_momentum_factor = _read_json(
        reports / "v203_v4_residual_momentum_factor_test.json"
    )
    v205_multi_asset_rotation = (
        _read_json(reports / "v209_global_defensive_rotation.json")
        or _read_json(reports / "v205_multi_asset_defensive_rotation.json")
    )
    v207_multi_asset_robustness = (
        _read_json(reports / "v211_global_defensive_robustness_audit.json")
        or _read_json(reports / "v207_multi_asset_robustness_audit.json")
    )
    v213_paper_forward = _latest_paper_forward_report()
    v217_cash_sweep = _read_json(reports / "v217_cash_sweep_rotation.json")
    v219_qdii_correction = _read_json(
        reports / "v219_qdii_split_nav_alignment_correction.json"
    )
    v9_margin_factor = _read_json(reports / "v9_margin_factor_research.json")
    v14_margin_factor = _read_json(
        reports / "v14_margin_balance_acceleration.json"
    )
    v14_margin_strategy = _read_json(
        reports / "v14_margin_balance_strategy_validation.json"
    )
    v90_pmi_regime = _read_json(reports / "v90_nbs_pmi_regime_gate.json")
    v104_prebacktest = _read_json(
        reports / "v104_prebacktest_admission_gate.json"
    )
    v121_m2_acceleration = _read_json(
        reports / "v121_pbc_m2_acceleration_gate.json"
    )
    v142_interbank = _read_json(
        reports / "v142_pbc_interbank_3pct_prebacktest_gate.json"
    )
    v143_successor = _read_json(reports / "v143_successor_research_status.json")
    v145_successor = _read_json(reports / "v145_successor_research_status.json")
    v144_ppi_cpi_gap = _read_json(
        reports / "v144_nbs_ppi_cpi_gap_prebacktest_gate.json"
    )
    v146_industrial_profit = _read_json(
        reports / "v146_nbs_industrial_profit_sign_prebacktest_gate.json"
    )
    forward_evaluation = _read_json(
        reports / "v86_expectancy_forward_evaluation.json"
    )
    forward_schedule = _read_json(
        reports / "v86_forward_observation_schedule_status.json"
    )
    training_integrity = _read_json(
        reports / "v86_model_training_integrity_gate.json"
    )
    discovery_assessment = _read_json(
        reports / "DEEPSEEK_DISCOVERY_ASSESSMENT_LATEST.json"
    )
    successor_admission = _read_json(
        reports / "v87_successor_research_admission_gate.json"
    )
    v90_successor = _read_json(reports / "v90_successor_research_status.json")
    v120_successor = _read_json(
        reports / "v120_successor_research_status.json"
    )
    if v145_successor.get("new_data_epoch_ready") is True:
        discovery_assessment = v145_successor
        successor_admission = v145_successor
    elif v143_successor.get("new_data_epoch_ready") is True:
        discovery_assessment = v143_successor
        successor_admission = v143_successor
    elif v120_successor.get("new_data_epoch_ready") is True:
        discovery_assessment = v120_successor
        successor_admission = v120_successor
    elif v90_successor.get("new_data_epoch_ready") is True:
        discovery_assessment = v90_successor
        successor_admission = v90_successor
    margin_physical = _manifest_physical_evidence(
        reports / "v9_margin_history_manifest.json", project_root=_PROJECT,
    )
    v90_pmi_physical = _v90_pmi_physical_evidence(
        _PROJECT / "research" / "data" / "v90_nbs_pmi" / "manifest.json",
        reports / "v90_nbs_pmi_data_epoch_audit.json",
        project_root=_PROJECT,
    )
    v120_pbc_physical = _v120_pbc_physical_evidence(
        _PROJECT / "research" / "data" / "v120_pbc_liquidity" / "manifest.json",
        reports / "v120_pbc_liquidity_data_epoch_audit.json",
        project_root=_PROJECT,
    )
    v143_cpi_ppi_physical = _v143_nbs_cpi_ppi_physical_evidence(
        _PROJECT / "research" / "data" / "v143_nbs_cpi_ppi" / "manifest.json",
        reports / "v143_nbs_cpi_ppi_data_epoch_audit.json",
        project_root=_PROJECT,
    )
    v145_industrial_profit_physical = _v145_nbs_industrial_profit_physical_evidence(
        _PROJECT / "research" / "data" / "v145_nbs_industrial_profit"
        / "manifest.json",
        reports / "v145_nbs_industrial_profit_data_epoch_audit.json",
        project_root=_PROJECT,
    )
    v12_temporal = v12_fundamentals.get("temporal_audit") or {}
    v12_fields = v12_fundamentals.get("field_coverage") or {}
    v12_ready = bool(
        v12_fundamentals.get("data_gate_passed") is True
        and v12_temporal.get("initial_announcement_semantics_verified") is True
        and v12_fields.get("v47_required_fields_at_least_95pct") is True
    )
    v148_ready = bool(
        v148_filing_times.get("filing_time_data_gate_passed") is True
        and v148_filing_times.get("return_data_read") is False
    )
    v149_ready = bool(
        v149_xbrl_profitability.get("profitability_data_gate_passed") is True
        and v149_xbrl_profitability.get("return_data_read") is False
    )
    v150_ready = bool(
        v150_roe_epoch.get("roe_data_gate_passed") is True
        and v150_roe_epoch.get("return_data_read") is False
    )
    v152_ready = v152_adjusted_prices.get("price_data_gate_passed") is True
    v154_ready = bool(
        v154_asset_growth.get("quarterly_asset_growth_data_gate_passed") is True
        and v154_asset_growth.get("price_data_read") is False
        and v154_asset_growth.get("return_data_read") is False
    )
    v156_ready = v156_asset_growth_prices.get("price_data_gate_passed") is True
    v158_ready = bool(
        v158_eastmoney_asset_growth.get(
            "quarterly_asset_growth_data_gate_passed"
        ) is True
        and v158_eastmoney_asset_growth.get("price_data_read") is False
        and v158_eastmoney_asset_growth.get("return_data_read") is False
    )
    v162_ready = bool(
        v162_asset_growth.get("quarterly_asset_growth_data_gate_passed") is True
        and v162_asset_growth.get("price_data_read") is False
        and v162_asset_growth.get("return_data_read") is False
    )
    v164_ready = v164_asset_growth_prices.get("price_data_gate_passed") is True
    decision_indexes = index_data_evidence()
    decision_calendar = trading_day_status()
    current_index_update = _read_json(
        reports / "current_market_index_update.json"
    )
    product = _read_json(reports / "product_sync_worker_status.json")
    worker = _read_json(reports / "research_worker_status.json")
    pipeline = _read_json(reports / "pipeline_health.json")
    gate_progress = pipeline.get("gate_unlock_progress") or {}
    gate = decision_gate or _decision_gate_status()
    gate_registry = gate.get("strategy_registry") or {}
    progress_active = bool(
        gate_registry.get("active_strategy")
        and "revoked" not in str(gate.get("source_status") or "").lower()
    )

    processed = int(progress.get("processed_symbols") or 0)
    total = int(
        daily_quality.get("target_symbols")
        or daily_quality.get("file_count")
        or progress.get("total_symbols")
        or 0
    )
    daily_quality_passed = int(daily_quality.get("passed_files") or 0)
    daily_quality_failed = int(daily_quality.get("failed_files") or 0)
    raw_asof = str(
        update.get("actual_market_data_end")
        or progress.get("requested_end_date")
        or ""
    )
    quality_asof = str((daily_quality.get("query_window") or {}).get("end_date") or "")
    completed_checked = int(update.get("checked_symbols") or 0)
    completed_universe = bool(
        update.get("complete_universe_check") is True
        and completed_checked == total
        and raw_asof == quality_asof
    )
    progress_completed = bool(
        progress.get("status") == "completed"
        and processed == total
    )
    sync_failed_symbols = int(
        progress.get("failed_symbols")
        or (progress.get("status_counts") or {}).get("failed")
        or 0
    )
    effective_processed = processed if progress_completed else completed_checked
    snapshot_asof = str(snapshot.get("data_asof") or "")
    try:
        same_daily_epoch = date.fromisoformat(raw_asof).isoformat() == raw_asof == quality_asof
    except ValueError:
        same_daily_epoch = False
    raw_ready = bool(
        total > 0
        and same_daily_epoch
        and (progress_completed or completed_universe)
        and daily_quality.get("ready_for_factor_research") is True
        and daily_quality_failed == 0
        and daily_quality_passed == total
    )
    if raw_ready:
        raw_ready_reason = "quality_audited_current_epoch"
    elif progress.get("status") == "failed" or sync_failed_symbols:
        raw_ready_reason = "incremental_sync_failed"
    elif raw_asof != quality_asof:
        raw_ready_reason = "current_epoch_not_quality_audited"
    else:
        raw_ready_reason = "daily_quality_audit_incomplete"
    snapshot_current = bool(
        raw_asof
        and snapshot_asof >= raw_asof
        and snapshot.get("audit_invalidated") is not True
    )
    product_ok = product.get("status") == "ok"

    blockers: list[dict[str, object]] = []
    if v219_qdii_correction:
        correction = v219_qdii_correction.get("correction") or {}
        metrics40 = (
            (v219_qdii_correction.get("cost_ladder_metrics") or {}).get("40.0")
            or {}
        )
        validation = metrics40.get("validation_2022_2023") or {}
        yearly = validation.get("yearly_total_return_pct") or {}
        blockers.append({
            "category": "profitability",
            "label": "V219纠正QDII单位错误后，旧候选与纸面信号已撤销",
            "current": (
                f"改变{correction.get('affected_asset_month_decisions', 0)}条资产月准入/"
                f"{correction.get('affected_execution_months', 0)}个执行月；"
                f"纠正后验证累计{validation.get('total_return_pct')}%，"
                f"2022年{yearly.get('2022')}%"
            ),
            "target": "在同一纠正数据上形成成本后可复现、跨阶段为正的唯一ETF组合",
            "gap": "V209/V211/V213/V215不可继续使用；当前100%现金且无买卖提醒",
        })
    elif (
        v207_multi_asset_robustness
        and v207_multi_asset_robustness.get("robustness_passed") is False
    ):
        concentration = v207_multi_asset_robustness.get("return_concentration") or {}
        blockers.append({
            "category": "research",
            "label": "当前多资产历史候选的独立稳健性复核未通过",
            "current": (
                f"独立复算、资产剔除、参数邻域和执行压力通过；黄金贡献占 "
                f"{concentration.get('maximum_positive_asset_contribution_share_pct')}%，"
                f"单一年份占 {concentration.get('maximum_positive_year_log_return_share_pct')}%"
            ),
            "target": "单一正贡献资产和单一正收益年份占比均不超过冻结上限70%",
            "gap": "当前ETF权重仅保留研究观察；纸面执行、真钱推荐和自动交易均关闭",
        })
    elif (
        v205_multi_asset_rotation.get("historical_candidate_passed") is True
        and v205_multi_asset_rotation.get("recommendation_eligible") is not True
    ):
        cost40 = (
            (v205_multi_asset_rotation.get("cost_ladder_metrics") or {}).get("40.0")
            or {}
        )
        validation = cost40.get("validation_2022_2023") or {}
        test = cost40.get("test_2024_2025") or {}
        diagnostic = cost40.get("diagnostic_2026_through_july") or {}
        blockers.append({
            "category": "research",
            "label": "V209/V211历史与稳健性复核已通过，冻结后前向证据尚不足",
            "current": (
                f"验证 {validation.get('total_return_pct')}% · 测试 {test.get('total_return_pct')}% · "
                f"2026诊断 {diagnostic.get('total_return_pct')}% / 回撤 {diagnostic.get('max_drawdown_pct')}%"
            ),
            "target": "冻结规则后积累独立前向记录，再评估是否允许真钱决策",
            "gap": "测试与2026诊断已在本轮查看；当前只允许纸面ETF配置",
        })
    if not raw_ready:
        blockers.append({
            "category": "data",
            "label": "原始日线或质量审计未完成",
            "current": (
                f"{processed}/{total}，质量通过 {daily_quality_passed}，"
                f"失败 {daily_quality_failed}"
            ),
            "target": "全部股票完成且质量失败为 0",
            "gap": "先补齐或修复原始数据",
        })
    if not snapshot_current:
        reason = (
            "旧快照含已否决的前视口径"
            if snapshot.get("audit_invalidated") is True
            else "研究快照落后于原始日线"
        )
        blockers.append({
            "category": "data",
            "label": reason,
            "current": snapshot_asof or "缺失",
            "target": raw_asof or "原始数据末日",
            "gap": "重建无前视研究缓存和量化快照",
        })
    if not product_ok:
        blockers.append({
            "category": "sync",
            "label": "网站产品同步未成功",
            "current": str(product.get("error") or product.get("status") or "缺失"),
            "target": "status=ok",
            "gap": "恢复隐私安全产品接口并完成一次推送",
        })
    if not v12_ready:
        blockers.append({
            "category": "data",
            "label": "V12 个股基本面公告时点数据门禁失败",
            "current": (
                f"记录 {int(v12_fundamentals.get('fundamental_records') or 0):,} · "
                f"法定截止日后 {int(v12_temporal.get('publication_after_statutory_deadline_rows') or 0):,} 条 "
                f"({v12_temporal.get('publication_after_statutory_deadline_pct', 0)}%) · "
                f"利润/现金流独立公告日覆盖 "
                f"{v12_temporal.get('separate_publication_date_coverage_pct', 0)}%"
            ),
            "target": "初始公告语义可核验 · 独立公告日覆盖 ≥95% · 必需字段覆盖 ≥95%",
            "gap": (
                "旧库不得继续进入因子或回测；先用巨潮资讯官方公告时间重建时点映射，"
                "并完成原文抽样复核与物理哈希验收"
            ),
            "detail": (
                "ROE覆盖 "
                f"{((v12_fields.get('fields') or {}).get('roe_avg') or {}).get('coverage_pct', 0)}%；"
                "V47/V48旧收益结果保留为失败历史，不得换名重跑。"
            ),
        })
    if not v148_ready:
        v148_counts = v148_filing_times.get("counts") or {}
        blockers.append({
            "category": "data",
            "label": "V148 巨潮资讯官方财报时点数据纪元尚未验收",
            "current": (
                f"状态 {v148_filing_status.get('status') or v148_filing_times.get('status') or '缺失'} · "
                f"已成档报告期 {v148_counts.get('periods', 0)}/33 · "
                f"旧库代码期覆盖 {v148_counts.get('overall_legacy_code_coverage_pct', 0)}% · "
                f"收益数据读取 {v148_filing_times.get('return_data_read') is True}"
            ),
            "target": "33/33已完成报告期 · 总体覆盖≥95% · 单期覆盖≥90% · 归档/CSV哈希全部吻合",
            "gap": (
                "逐日分片抓取并验收官方公告时间；通过前不得提出个股盈利候选或读取收益"
            ),
        })
    if not v149_ready:
        v149_counts = v149_xbrl_profitability.get("counts") or {}
        v149_fields = v149_xbrl_profitability.get("field_coverage_pct") or {}
        blockers.append({
            "category": "data",
            "label": "V149 上交所官方 XBRL 盈利能力数据纪元尚未验收",
            "current": (
                f"状态 {v149_xbrl_status.get('status') or v149_xbrl_profitability.get('status') or '缺失'} · "
                f"代码 {v149_counts.get('processed_codes', 0)}/{v149_counts.get('universe_codes', 1842)} · "
                f"年报 {v149_counts.get('records', 0)} 条 · "
                f"公告连接 {v149_counts.get('overall_filing_join_coverage_pct', 0)}% · "
                f"应有代码期 {v149_counts.get('expected_code_period_coverage_pct', 0)}% · "
                f"ROE {v149_fields.get('roe_pct', 0)}%"
            ),
            "target": "沪市主板全代码 · 2018—2025八年 · 公告连接和应有代码期覆盖均≥95% · 必需值覆盖≥95% · 全部哈希通过",
            "gap": (
                "XBRL全量归档、公告连接和ROE均已完成；广义净利润字段覆盖仅 "
                f"{v149_fields.get('net_profit_10k_cny', 0)}%，因此广义盈利数据集保持失败。"
                "ROE只可沿已独立验收的V150路径使用。"
            ),
        })
    if not v150_ready:
        v150_counts = v150_roe_epoch.get("counts") or {}
        blockers.append({
            "category": "data",
            "label": "V150 官方 ROE 单字段时点数据尚未通过候选前门禁",
            "current": (
                f"完整ROE {v150_counts.get('complete_roe_records', 0)}/"
                f"{v150_counts.get('source_records', 0)} 条 · "
                f"源覆盖 {v150_roe_epoch.get('roe_source_coverage_pct', 0)}% · "
                f"完整时点覆盖 {v150_roe_epoch.get('complete_point_in_time_coverage_pct', 0)}% · "
                f"收益数据读取 {v150_roe_epoch.get('return_data_read') is True}"
            ),
            "target": "2018—2025八年 · ROE及完整时点覆盖≥95% · 原始XBRL与公告CSV哈希全部通过",
            "gap": "V148和V149全量完成后复算；通过前不得冻结ROE候选或读取收益",
        })
    if v151_roe_admission and (
        v151_roe_admission.get("preregistration_admission_passed") is not True
    ):
        v151_audit = v151_roe_admission.get("data_audit") or {}
        blockers.append({
            "category": "research",
            "label": "V151 官方 ROE 十分组候选尚未获准读取收益",
            "current": (
                f"年报 {v151_audit.get('records', 0)} 条 · "
                f"代码 {v151_audit.get('codes', 0)} 只 · "
                f"收益数据读取 {v151_roe_admission.get('return_data_read') is True} · "
                f"单次因子检验 {v151_roe_admission.get('single_factor_backtest_allowed') is True}"
            ),
            "target": "V150通过 · 八年均有≥500只 · 至少1,000只独立代码 · 公式/方向/反证全部冻结",
            "gap": "；".join(
                str(value) for value in v151_roe_admission.get("reasons") or []
            ) or "候选前数据准入尚未通过",
            "detail": "准入前禁止打开任何价格或收益；失败时不得放宽数据和文献门禁。",
        })
    if (
        v151_roe_admission.get("preregistration_admission_passed") is True
        and not v152_ready
    ):
        v152_counts = v152_adjusted_prices.get("counts") or {}
        v152_coverage = v152_adjusted_prices.get("coverage") or {}
        blockers.append({
            "category": "data",
            "label": "V152 V151专用复权行情与退市覆盖尚未验收",
            "current": (
                f"有价代码 {v152_counts.get('codes_with_rows', 0)}/"
                f"{v152_counts.get('eligible_codes', 0)} · "
                f"最低月覆盖 {v152_coverage.get('minimum_month_coverage_pct', 0)}% · "
                f"收益计算 {v152_adjusted_prices.get('return_data_computed') is True}"
            ),
            "target": "窗口股票覆盖≥95% · 每月≥90% · 有效复权价≥99.9% · 输入输出哈希完整",
            "gap": "先补齐退市公司复权价格并验收；失败前不得执行唯一因子检验",
        })
    if v153_roe_factor.get("return_data_read") is True and (
        v153_roe_factor.get("historical_factor_gate_passed") is not True
    ):
        blockers.append({
            "category": "profitability",
            "label": "V153 官方ROE十分组已在成本后历史检验中永久失败",
            "current": "验证段或观察测试段至少一项预注册统计门禁失败",
            "target": "两段同时通过IC、单调性、成本后价差、长端、bootstrap与时间折",
            "gap": "该公式、方向、持有期、成本和总体已经封存，不得换参数重跑",
        })
    if not v154_ready and not v162_ready:
        v154_counts = v154_asset_growth.get("counts") or {}
        v154_coverage = v154_asset_growth.get("coverage_pct") or {}
        v154_cross = v154_asset_growth.get("annual_sse_xbrl_cross_validation") or {}
        blockers.append({
            "category": "data",
            "label": "V154 季度资产增长点时数据纪元尚未验收",
            "current": (
                f"状态 {v154_asset_growth_status.get('status') or v154_asset_growth.get('status') or '缺失'} · "
                f"代码 {v154_asset_growth_status.get('processed_codes', v154_counts.get('processed_codes', 0))}/"
                f"{v154_asset_growth_status.get('total_codes', v154_counts.get('universe_codes', 1842))} · "
                f"已归档记录 {v154_asset_growth_status.get('records', v154_counts.get('records_with_complete_pit_yoy_asset', 0))} 条 · "
                f"覆盖 {v154_coverage.get('complete_pit_yoy_asset_overall', 0)}% · "
                f"年报交叉校验 {v154_cross.get('match_pct', 0)}%"
            ),
            "target": "1842只、33期 · 完整点时覆盖总体≥95%/每期≥90% · 年报交叉校验≥5000对且≥95%",
            "gap": "全量归档、官方公告连接或独立交叉校验仍有门禁未通过；通过前禁止冻结候选或读取收益",
        })
    if v154_ready and (
        v155_asset_growth_admission.get("preregistration_admission_passed") is not True
    ):
        v155_counts = v155_asset_growth_admission.get("counts") or {}
        blockers.append({
            "category": "research",
            "label": "V155 季度资产增长十分组尚未获准读取收益",
            "current": (
                f"完整点时 {v155_counts.get('complete_pit_records', 0)} 条 · "
                f"代码 {v155_counts.get('codes', 0)} 只 · "
                f"报告期 {v155_counts.get('periods', 0)} · "
                f"收益读取 {v155_asset_growth_admission.get('return_data_read') is True}"
            ),
            "target": "V154通过 · ≥50000条完整点时记录 · 33期/≥1700只 · 公式、方向和失败规则冻结",
            "gap": "；".join(str(value) for value in v155_asset_growth_admission.get("failed_gates") or [])
            or "候选前准入尚未执行",
        })
    if (
        v155_asset_growth_admission.get("preregistration_admission_passed") is True
        and not v156_ready
    ):
        v156_counts = v156_asset_growth_prices.get("counts") or {}
        v156_coverage = v156_asset_growth_prices.get("coverage") or {}
        blockers.append({
            "category": "data",
            "label": "V156 V155专用复权行情尚未验收",
            "current": (
                f"有价代码 {v156_counts.get('codes_with_rows', 0)}/"
                f"{v156_counts.get('eligible_codes', 0)} · "
                f"最低月覆盖 {v156_coverage.get('minimum_month_coverage_pct', 0)}% · "
                f"收益计算 {v156_asset_growth_prices.get('return_data_computed') is True}"
            ),
            "target": "代码覆盖≥95% · 每月≥90% · 有效复权价≥99.9% · 哈希完整",
            "gap": "从底层原始行情建立独立数据纪元；验收前不得执行V155唯一因子检验",
        })
    if v157_asset_growth_factor.get("return_data_read") is True and (
        v157_asset_growth_factor.get("historical_factor_gate_passed") is not True
    ):
        blockers.append({
            "category": "profitability",
            "label": "V157 季度资产增长十分组已在成本后历史检验中永久失败",
            "current": "验证段或观察测试段至少一项预注册统计门禁失败",
            "target": "两段同时通过IC、单调性、成本后价差、长端、bootstrap与时间折",
            "gap": "正方向、缩尾、总体、持有期、成本和切分已永久封存，不得翻负或换参数重跑",
        })
    if v158_eastmoney_asset_growth and not v158_ready and not v162_ready:
        v158_counts = v158_eastmoney_asset_growth.get("counts") or {}
        v158_coverage = v158_eastmoney_asset_growth.get("coverage_pct") or {}
        v158_cross = (
            v158_eastmoney_asset_growth.get("cross_vendor_baostock_validation")
            or {}
        )
        v158_official = (
            (
                v158_eastmoney_asset_growth.get(
                    "official_sse_xbrl_annual_validation"
                )
                or {}
            ).get("overall")
            or {}
        )
        v158_diagnosis = (
            v158_eastmoney_asset_growth.get("point_in_time_diagnosis") or {}
        )
        blockers.append({
            "category": "data",
            "label": "V158 独立季度资产增长源数值通过但历史时点失败",
            "current": (
                f"{v158_counts.get('periods', 0)}/33期 · "
                f"{v158_counts.get('records', 0)}条 · "
                f"YOY覆盖 {v158_coverage.get('yoy_asset_overall', 0)}% · "
                f"完整点时 {v158_coverage.get('complete_pit_yoy_asset_overall', 0)}% · "
                f"跨源数值 {v158_cross.get('match_pct', 0)}% · "
                f"官方年报 {v158_official.get('within_0_02_pct', 0)}%"
            ),
            "target": "完整点时覆盖总体≥95%且每期≥90%，公告日必须与V148官方首次披露时间一致",
            "gap": (
                f"{len(v158_diagnosis.get('annual_periods_with_zero_3_day_notice_match') or [])}个"
                "年度报告期三日内公告日匹配为0；来源存在后续修订时间覆盖风险，"
                "禁止回填首次公告日、读取收益或放行候选"
            ),
        })
    if not v162_ready:
        v162_counts = v162_asset_growth.get("counts") or {}
        v162_coverage = v162_asset_growth.get("coverage_pct") or {}
        blockers.append({
            "category": "data",
            "label": "V162 官方季度资产增长数据纪元尚未通过",
            "current": (
                f"官方PDF {v162_counts.get('official_pdf_accepted', 0)}/"
                f"{v162_counts.get('official_asset_targets', 150)} · "
                f"总体时点覆盖 {v162_coverage.get('complete_pit_yoy_asset_overall', 0)}% · "
                f"2019年报覆盖 "
                f"{(v162_coverage.get('complete_pit_yoy_asset_by_period') or {}).get('2019-12-31', 0)}%"
            ),
            "target": "官方PDF至少140份 · 总体≥95%且每期≥90% · 全部查询/PDF哈希有效",
            "gap": "；".join(str(value) for value in v162_asset_growth.get("failed_gates") or [])
            or "V162 尚未执行或报告缺失",
        })
    if v162_ready and (
        v163_asset_growth_admission.get("preregistration_admission_passed") is not True
    ):
        v163_counts = v163_asset_growth_admission.get("counts") or {}
        blockers.append({
            "category": "research",
            "label": "V163 季度资产增长候选尚未获准读取收益",
            "current": (
                f"完整时点 {v163_counts.get('complete_pit_records', 0)} 条 · "
                f"代码 {v163_counts.get('codes', 0)} 只 · "
                f"报告期 {v163_counts.get('periods', 0)}"
            ),
            "target": "≥50000条 · 33期/≥1700只 · 公式、方向、成本、切分和失败规则冻结",
            "gap": "；".join(
                str(value) for value in v163_asset_growth_admission.get("failed_gates") or []
            ) or "候选前准入尚未执行",
        })
    if (
        v163_asset_growth_admission.get("preregistration_admission_passed") is True
        and not v164_ready
    ):
        v164_counts = v164_asset_growth_prices.get("counts") or {}
        v164_coverage = v164_asset_growth_prices.get("coverage") or {}
        blockers.append({
            "category": "data",
            "label": "V164 V163专用复权行情尚未验收",
            "current": (
                f"有价代码 {v164_counts.get('codes_with_rows', 0)}/"
                f"{v164_counts.get('eligible_codes', 0)} · "
                f"最低月覆盖 {v164_coverage.get('minimum_month_coverage_pct', 0)}%"
            ),
            "target": "代码覆盖≥95% · 每月≥90% · 有效复权价≥99.9% · 原始档案哈希完整",
            "gap": "；".join(
                str(value) for value in v164_asset_growth_prices.get("failed_gates") or []
            ) or "行情纪元尚未执行",
        })
    if v165_asset_growth_factor.get("return_data_read") is True and (
        v165_asset_growth_factor.get("historical_factor_gate_passed") is not True
    ):
        v165_splits = v165_asset_growth_factor.get("splits") or {}
        validation = (v165_splits.get("validation") or {}).get("metrics") or {}
        observed = (v165_splits.get("observed_test") or {}).get("metrics") or {}
        validation_failed = (v165_splits.get("validation") or {}).get("failed_gates") or []
        observed_failed = (v165_splits.get("observed_test") or {}).get("failed_gates") or []
        blockers.append({
            "category": "profitability",
            "label": "V165 官方季度资产增长因子已在成本后永久失败",
            "current": (
                f"验证期 Rank IC {validation.get('mean_rank_ic')} · "
                f"10BP后多空 {validation.get('mean_d10_minus_d1_after_10bp')} · "
                f"观察期 Rank IC {observed.get('mean_rank_ic')} · "
                f"10BP后多空 {observed.get('mean_d10_minus_d1_after_10bp')}"
            ),
            "target": "验证与观察期同时通过IC、单调性、成本后多空、长端、bootstrap与时间折",
            "gap": (
                "验证失败：" + "、".join(str(value) for value in validation_failed)
                + "；观察失败：" + "、".join(str(value) for value in observed_failed)
            ),
            "detail": "方向、缩尾、分组、持有期、成本、切分和总体已经封存；策略回测保持锁定。",
        })
    if margin_physical.get("passed") is not True:
        blockers.append({
            "category": "data",
            "label": "V14 冻结融资融券历史尚未完成物理与哈希验收",
            "current": (
                f"声明 {margin_physical.get('declared_trade_dates', 0)} 个交易日 · "
                f"列出 {margin_physical.get('listed_files', 0)} 个文件 · "
                f"实际可读 {margin_physical.get('physical_files', 0)} 个 · "
                f"哈希吻合 {margin_physical.get('hash_verified_files', 0)} 个"
            ),
            "target": "清单、物理文件与 SHA-256 逐项一致",
            "gap": "重新取得并完成哈希验收前，V9 融资融券因子必须保持封存",
        })
    if v90_pmi_physical.get("passed") is not True:
        blockers.append({
            "category": "data",
            "label": "V90 国家统计局 PMI 新数据纪元物理验收失败",
            "current": (
                f"月份 {v90_pmi_physical.get('actual_months', 0)}/"
                f"{v90_pmi_physical.get('expected_months', 48)} · "
                f"归档 {v90_pmi_physical.get('physical_archives', 0)}/"
                f"{v90_pmi_physical.get('source_archives', 0)} · "
                f"哈希 {v90_pmi_physical.get('hash_verified_archives', 0)}"
            ),
            "target": "48/48 月份、全部官方页面归档与 SHA-256 一致",
            "gap": "物理验收恢复前，V90 结果不得被读取或展示为有效研究证据",
        })
    if v120_pbc_physical.get("passed") is not True:
        blockers.append({
            "category": "data",
            "label": "V120 中国人民银行月度金融统计物理验收失败",
            "current": (
                f"月份 {v120_pbc_physical.get('actual_months', 0)}/"
                f"{v120_pbc_physical.get('expected_months', 96)} · "
                f"目录哈希 {v120_pbc_physical.get('hash_verified_listings', 0)}/"
                f"{v120_pbc_physical.get('listing_archives', 18)} · "
                f"月报哈希 {v120_pbc_physical.get('hash_verified_reports', 0)}/"
                f"{v120_pbc_physical.get('report_archives', 96)}"
            ),
            "target": "96/96 月、18 个目录页、96 个月报与内容归档及 CSV 哈希全部一致",
            "gap": "物理验收恢复前，不得用 V120 数据发现或回测货币信用机制",
        })
    if v143_cpi_ppi_physical.get("passed") is not True:
        blockers.append({
            "category": "data",
            "label": "V143 国家统计局 CPI/PPI 数据纪元物理验收失败",
            "current": (
                f"月份 {v143_cpi_ppi_physical.get('actual_months', 0)}/"
                f"{v143_cpi_ppi_physical.get('expected_months', 120)} · "
                f"搜索哈希 {v143_cpi_ppi_physical.get('hash_verified_searches', 0)}/"
                f"{v143_cpi_ppi_physical.get('search_archives', 40)} · "
                f"发布哈希 {v143_cpi_ppi_physical.get('hash_verified_releases', 0)}/"
                f"{v143_cpi_ppi_physical.get('release_archives', 240)}"
            ),
            "target": "120/120月、40份搜索、240份发布响应、条款和CSV哈希全部一致",
            "gap": "物理验收恢复前，不得用V143发现或回测通胀传导机制",
        })
    if v145_industrial_profit_physical.get("passed") is not True:
        blockers.append({
            "category": "data",
            "label": "V145 国家统计局工业利润数据纪元物理验收失败",
            "current": (
                f"观测 {v145_industrial_profit_physical.get('actual_observations', 0)}/"
                f"{v145_industrial_profit_physical.get('expected_observations', 110)} · "
                f"搜索哈希 {v145_industrial_profit_physical.get('hash_verified_searches', 0)}/"
                f"{v145_industrial_profit_physical.get('search_archives', 50)} · "
                f"页面哈希 {v145_industrial_profit_physical.get('hash_verified_releases', 0)}/"
                f"{v145_industrial_profit_physical.get('release_archives', 110)}"
            ),
            "target": "110/110观测、50份搜索、110份官网页面、条款和CSV哈希全部一致",
            "gap": "物理验收恢复前，不得用V145发现或回测工业利润机制",
        })
    indexes_current = bool(
        decision_indexes.get("ready") is True
        and raw_asof
        and str(decision_indexes.get("common_as_of") or "") >= raw_asof
    )
    if not indexes_current:
        blockers.append({
            "category": "data",
            "label": "决策指数与个股日线未同步",
            "current": str(decision_indexes.get("common_as_of") or "缺失"),
            "target": raw_asof or "个股日线末日",
            "gap": "沪深300和上证指数都追平后才允许计算市场状态",
        })
    benchmark_rows = current_index_update.get("decision_indexes") or {}
    benchmark_indexes_current = bool(
        len(benchmark_rows) == 4
        and raw_asof
        and all(
            isinstance(row, dict)
            and row.get("source_current_for_requested_end") is True
            and str(row.get("latest_available_date") or "") >= raw_asof
            for row in benchmark_rows.values()
        )
    )
    if not benchmark_indexes_current:
        blockers.append({
            "category": "data",
            "label": "四个决策与研究指数没有全部追平",
            "current": (
                f"{sum(1 for row in benchmark_rows.values() if isinstance(row, dict) and row.get('source_current_for_requested_end') is True)}/4"
            ),
            "target": f"4/4 截至 {raw_asof or '个股日线末日'}",
            "gap": "上证、沪深300、中证500和中证1000完整收盘数据缺一即失败关闭",
        })
    calendar_current = bool(
        decision_calendar.get("source") == "audited_baostock_calendar"
        and str(decision_calendar.get("coverage_end") or "")
        >= datetime.now(_SH_TZ).date().isoformat()
    )
    if not calendar_current:
        blockers.append({
            "category": "data",
            "label": "交易日历没有覆盖当前决策日",
            "current": str(decision_calendar.get("coverage_end") or "缺失"),
            "target": datetime.now(_SH_TZ).date().isoformat(),
            "gap": "同步公开交易日程前，买入窗口必须保持静默",
        })

    failed_conditions = []
    for condition in (gate_progress.get("conditions") or []) if progress_active else []:
        if not isinstance(condition, dict) or condition.get("passed") is True:
            continue
        row = {
            "id": condition.get("id"),
            "label": condition.get("label"),
            "current": condition.get("current"),
            "target": condition.get("target"),
            "unit": condition.get("unit"),
            "gap": condition.get("gap"),
            "detail": condition.get("detail"),
        }
        failed_conditions.append(row)
        blockers.append({"category": "profitability", **row})
    if gate_progress and not progress_active:
        blockers.append({
            "category": "integrity",
            "label": "旧 V23 影子计时已经撤销",
            "current": (
                f"{gate_progress.get('current_trades', 0)} 笔 · "
                f"胜率 {gate_progress.get('current_win_rate_pct')}% · "
                f"年化 {gate_progress.get('current_annualized_pct')}%"
            ),
            "target": "新策略 ID、新数据纪元和独立前向验证",
            "gap": "旧样本只作历史诊断，增加交易数也不能解锁推荐或资金分配",
        })

    selected_method: dict[str, object] = {}
    for method in production.get("methods") or []:
        if not isinstance(method, dict):
            continue
        if all((
            method.get("method") == production.get("best_method"),
            method.get("topk") == production.get("best_topk"),
            method.get("buffer_mult") == production.get("best_buffer"),
            method.get("port_vol_target") == production.get("best_vol"),
            (method.get("max_per_group") or 0)
            == (production.get("best_max_per_group") or 0),
        )):
            selected_method = method
            break
    strict_metrics = selected_method.get("metrics") or {}
    strict_diagnostics = selected_method.get("diagnostics") or {}
    strict_annualized = strict_metrics.get("annualized_pct")
    strict_total_return = strict_metrics.get("total_return_pct")
    strict_dsr = strict_diagnostics.get("dsr")
    strict_passed = bool(
        isinstance(strict_annualized, (int, float))
        and strict_annualized >= 15.0
        and float(strict_metrics.get("sharpe") or 0) > 0
        and abs(float(strict_metrics.get("max_dd_pct") or 100)) <= 15.0
        and float(strict_dsr or 0) >= 0.95
    )
    if selected_method and not strict_passed:
        blockers.append({
            "category": "profitability",
            "label": "严格点时多因子走步回测未盈利",
            "current": (
                f"总收益 {strict_total_return}% · 年化 {strict_annualized}% · "
                f"最大回撤 {strict_metrics.get('max_dd_pct')}% · DSR {strict_dsr}"
            ),
            "target": "含成本年化 ≥15% · 最大回撤 ≤15% · DSR ≥0.95",
            "gap": "当前配置没有可交易优势，禁止因数据已更新就启用策略",
        })

    regime_result = (regime_candidate.get("results") or {}).get("candidate") or {}
    holdout_results = cross_holdout.get("results") or {}
    holdout_full = holdout_results.get("frozen_candidate") or {}
    holdout_recent = holdout_results.get("recent_2024_2026") or {}
    if regime_candidate and regime_candidate.get("recommendation_eligible") is not True:
        blockers.append({
            "category": "profitability",
            "label": "风险降档候选来自已查看全样本",
            "current": (
                f"开发样本年化 {regime_result.get('annualized_pct')}% · "
                f"回撤 {regime_result.get('max_drawdown_pct')}% · "
                f"DSR {regime_result.get('dsr')}"
            ),
            "target": "冻结后未见时间样本独立通过",
            "gap": "当前没有未见未来数据，不能把探索收益当成稳定收益",
        })
    if cross_holdout and cross_holdout.get("verdict") != "HOLDOUT_PASS_RESEARCH_ONLY":
        blockers.append({
            "category": "profitability",
            "label": "冻结低波候选跨周期收益不足",
            "current": (
                f"跨股票池9年年化 {holdout_full.get('annualized_pct')}% · "
                f"DSR {holdout_full.get('dsr')} · "
                f"近3年年化 {holdout_recent.get('annualized_pct')}%"
            ),
            "target": "跨股票池年化 ≥15% · DSR ≥0.95 · 未见时间样本通过",
            "gap": "近三年有效但长期不稳定，仍为研究候选",
        })

    v3_base = taskbook_v3.get("base") or {}
    v3_stress = taskbook_v3.get("double_cost") or {}
    v3_bootstrap = (taskbook_v3.get("statistical_evidence") or {}).get(
        "bootstrap"
    ) or {}
    if taskbook_v3 and taskbook_v3.get("historical_robustness_gate_passed") is not True:
        blockers.append({
            "category": "profitability",
            "label": "严格 PIT 50 次历史稳健门禁失败",
            "current": (
                f"盈利 {v3_base.get('positive_runs')}/50 · "
                f"中位数 {v3_base.get('median_return_pct')}% · "
                f"双倍成本中位数 {v3_stress.get('median_return_pct')}% · "
                f"买入换手 {v3_base.get('median_buy_turnover_pct')}%"
            ),
            "target": (
                "盈利 ≥32/50 · 置信区间下界 >0 · 双倍成本均值/中位数 >0 · "
                "买入换手 ≥10%"
            ),
            "gap": (
                "失败门禁 "
                + ", ".join(str(value) for value in taskbook_v3.get("failed_gates") or [])
                + "；同一历史结果禁止继续追参"
            ),
            "detail": (
                "分环境 bootstrap 均值 95% 区间 "
                f"{v3_bootstrap.get('mean_return_pct_ci_95')}"
            ),
        })

    v9_result = v9_margin_factor.get("result") or {}
    v9_test = (v9_result.get("splits") or {}).get("test") or {}
    if v9_margin_factor and v9_margin_factor.get("statistical_gate_passed") is not True:
        blockers.append({
            "category": "profitability",
            "label": "V9 融资买入强度统计门禁失败",
            "current": (
                f"测试IC {v9_test.get('mean_ic')} · 单调性 "
                f"{v9_test.get('decile_monotonicity_spearman')} · "
                f"扣10BP头部减中间 {v9_test.get('top_minus_middle_after_cost_pct')}%"
            ),
            "target": "单调性 ≥0.6 · 扣10BP后头部减中间 >0 · 行业集中度验收通过",
            "gap": (
                "失败门禁 "
                + ", ".join(str(value) for value in v9_result.get("failed_gates") or [])
                + "；预注册规则要求永久封存，不得改方向或门槛补救"
            ),
        })

    v14_result = v14_margin_factor.get("result") or {}
    v14_test = (v14_result.get("splits") or {}).get("test") or {}
    v14_status = str(v14_margin_factor.get("status") or "")
    if margin_physical.get("passed") is True and "blocked" in v14_status:
        blockers.append({
            "category": "research",
            "label": "V14 融资余额加速度尚待真实物理输入重算",
            "current": str(v14_margin_factor.get("next_action") or "旧报告仍为阻塞状态"),
            "target": "使用已哈希验收的2058个交易日文件与新建V9标签面板运行一次",
            "gap": "旧阻塞报告不能替代本次真实计算",
        })
    elif v14_margin_factor and "blocked" not in v14_status and (
        v14_margin_factor.get("statistical_gate_passed") is not True
    ):
        blockers.append({
            "category": "profitability",
            "label": "V14 融资余额加速度统计门禁失败",
            "current": (
                f"测试IC {v14_test.get('mean_ic')} · 单调性 "
                f"{v14_test.get('decile_monotonicity_spearman')} · "
                f"扣10BP头部减中间 {v14_test.get('top_minus_middle_after_cost_pct')}%"
            ),
            "target": "预注册的7项统计门禁全部通过",
            "gap": (
                "失败门禁 "
                + ", ".join(str(value) for value in v14_result.get("failed_gates") or [])
                + "；不得事后修改公式、方向、持有期或门槛"
            ),
        })
    elif v14_margin_factor.get("statistical_gate_passed") is True:
        if not v14_margin_strategy:
            blockers.append({
                "category": "research",
                "label": "V14 因子统计通过但严格成本回测尚未完成",
                "current": "尚无 ¥10,000、100股整数手、佣金税费与滑点结果",
                "target": "完成冻结策略的0/10/20BP三档成本回测",
                "gap": "成本回测前不能成为历史候选",
            })
        elif v14_margin_strategy.get("historical_strategy_gate_passed") is not True:
            blockers.append({
                "category": "profitability",
                "label": "V14 严格成本策略门禁失败",
                "current": "历史策略未达到统一胜率、收益、回撤与成本压力条件",
                "target": "统一严格历史门禁全部通过",
                "gap": (
                    "失败门禁 "
                    + ", ".join(
                        str(value)
                        for value in v14_margin_strategy.get("failed_gates") or []
                    )
                ),
            })

    v90_validation = (v90_pmi_regime.get("splits") or {}).get("validation") or {}
    v90_test = (v90_pmi_regime.get("splits") or {}).get("test") or {}
    v90_validation_10 = v90_validation.get("cost_10bp") or {}
    v90_test_10 = v90_test.get("cost_10bp") or {}
    if v90_pmi_regime and v90_pmi_regime.get("historical_strategy_gate_passed") is not True:
        blockers.append({
            "category": "profitability",
            "label": "V90 国家统计局 PMI 双扩张策略历史门禁失败",
            "current": (
                f"验证年化 {v90_validation_10.get('annualized_return_pct')}% · "
                f"回撤 {v90_validation_10.get('max_drawdown_pct')}% · "
                f"测试年化 {v90_test_10.get('annualized_return_pct')}% · "
                f"利润因子 {v90_test_10.get('profit_factor')}"
            ),
            "target": "验证/测试含10BP年化 ≥15% · 回撤 ≤15% · 测试利润因子 ≥1.20 · 稳健门禁全通过",
            "gap": (
                "失败门禁 "
                + ", ".join(str(value) for value in v90_pmi_regime.get("failed_gates") or [])
                + "；已永久封存，不得事后改 PMI 阈值、方向、样本、成本或持有资产"
            ),
        })

    if v104_prebacktest and (
        v104_prebacktest.get("preregistration_admission_passed") is not True
    ):
        blockers.append({
            "category": "research",
            "label": "V104 PMI 原材料库存与就业背离候选已在回测前淘汰",
            "current": (
                f"触发 {v104_prebacktest.get('trigger_months', 0)}/"
                f"{v104_prebacktest.get('sample_months', 0)} 月 · "
                f"最大差值 {v104_prebacktest.get('maximum_observed_spread')} · "
                f"收益数据读取 {v104_prebacktest.get('return_data_read') is True}"
            ),
            "target": "信号数量足以跨时期检验，且阈值有独立于样本分布的先验来源",
            "gap": "；".join(
                str(value) for value in v104_prebacktest.get("reasons") or []
            ),
            "detail": "该公式永久封存；未运行回测，也不得通过修改阈值重新尝试。",
        })

    v121_test_10 = (
        ((v121_m2_acceleration.get("results") or {}).get("test") or {})
        .get("cost_10bp") or {}
    )
    v121_test_20 = (
        ((v121_m2_acceleration.get("results") or {}).get("test") or {})
        .get("cost_20bp") or {}
    )
    if v121_m2_acceleration and (
        v121_m2_acceleration.get("historical_strategy_gate_passed") is not True
    ):
        blockers.append({
            "category": "profitability",
            "label": "V121 人民银行 M2 加速度策略历史门禁失败",
            "current": (
                f"测试10BP年化 {v121_test_10.get('annualized_return_pct')}% · "
                f"胜率 {v121_test_10.get('win_rate_pct')}% · "
                f"回撤 {v121_test_10.get('max_drawdown_pct')}% · "
                f"利润因子 {v121_test_10.get('profit_factor')} · "
                f"20BP总收益 {v121_test_20.get('total_return_pct')}%"
            ),
            "target": "测试10BP年化 ≥15% · 胜率 ≥60% · 回撤 ≤15% · PF ≥1.20 · 20BP净收益为正",
            "gap": (
                "失败门禁 "
                + ", ".join(
                    str(value)
                    for value in v121_m2_acceleration.get("failed_gates") or []
                )
                + "；一次回测已消耗并永久封存，不得修改规格补救"
            ),
        })

    if v142_interbank and (
        v142_interbank.get("preregistration_admission_passed") is not True
    ):
        blockers.append({
            "category": "research",
            "label": "V142 同业拆借利率 3% 候选已在回测前淘汰",
            "current": (
                f"触发 {v142_interbank.get('trigger_months', 0)}/"
                f"{v142_interbank.get('sample_months', 0)} 月 · "
                f"样本最高 {v142_interbank.get('maximum_observed_rate_pct')}% · "
                f"收益数据读取 {v142_interbank.get('return_data_read') is True}"
            ),
            "target": "至少6个触发月 · 3.0%阈值有直接独立先验 · 完整冻结组合规格",
            "gap": "；".join(
                str(value) for value in v142_interbank.get("reasons") or []
            ),
            "detail": "未运行回测；公式及其阈值、变量和组合变体永久封存。",
        })

    if v144_ppi_cpi_gap and (
        v144_ppi_cpi_gap.get("preregistration_admission_passed") is not True
    ):
        blockers.append({
            "category": "research",
            "label": "V144 PPI-CPI 正缺口候选已在回测前淘汰",
            "current": (
                f"触发 {v144_ppi_cpi_gap.get('trigger_months', 0)}/"
                f"{v144_ppi_cpi_gap.get('sample_months', 0)} 月 · "
                f"独立正缺口阶段 {v144_ppi_cpi_gap.get('positive_regimes', 0)} · "
                f"收益数据读取 {v144_ppi_cpi_gap.get('return_data_read') is True}"
            ),
            "target": "至少4个独立阶段 · 外部来源直接支持固定沪深300择时方向",
            "gap": "；".join(
                str(value) for value in v144_ppi_cpi_gap.get("reasons") or []
            ),
            "detail": "未运行回测；公式、阈值、方向、资产和执行变体永久封存。",
        })

    if v146_industrial_profit and (
        v146_industrial_profit.get("preregistration_admission_passed") is not True
    ):
        train = (v146_industrial_profit.get("halves") or {}).get("train") or {}
        blockers.append({
            "category": "research",
            "label": "V146 工业利润正增长候选已在回测前淘汰",
            "current": (
                f"正/负观测 {v146_industrial_profit.get('positive_observations', 0)}/"
                f"{v146_industrial_profit.get('negative_observations', 0)} · "
                f"独立正/负阶段 {v146_industrial_profit.get('positive_regimes', 0)}/"
                f"{v146_industrial_profit.get('negative_regimes', 0)} · "
                f"训练负阶段 {train.get('negative_regimes', 0)} · "
                f"收益数据读取 {v146_industrial_profit.get('return_data_read') is True}"
            ),
            "target": "两半各≥2个正负独立阶段 · 统计总体匹配资产 · 文献直接支持宽基择时",
            "gap": "；".join(
                str(value) for value in v146_industrial_profit.get("reasons") or []
            ),
            "detail": "未运行回测；公式、阈值、方向、资产和执行变体永久封存。",
        })

    if v169_industry_profit_integrity and (
        v169_industry_profit_integrity.get("status")
        == "permanently_rejected_before_return_computation"
    ):
        blockers.append({
            "category": "integrity",
            "label": "V167 行业利润扩散候选在收益计算前永久淘汰",
            "current": (
                f"最多 {v169_industry_profit_integrity.get('maximum_possible_completed_trades', 0)} "
                f"笔完整交易 · 收益计算 "
                f"{v169_industry_profit_integrity.get('return_data_computed') is True}"
            ),
            "target": (
                f"冻结门禁至少 {v169_industry_profit_integrity.get('required_completed_trades', 12)} "
                "笔完整交易"
            ),
            "gap": "样本结构使交易数门禁不可达；不得伪造平仓重开或修改冻结规则",
            "detail": "V168价格数据保留为已同步但研究拒绝的物理证据，不进入推荐或回测。",
        })

    if v172_industry_profit_acceleration and (
        v172_industry_profit_acceleration.get("historical_strategy_gate_passed")
        is not True
    ):
        train10 = (
            ((v172_industry_profit_acceleration.get("results") or {}).get("train") or {})
            .get("cost_10bp") or {}
        )
        observed10 = (
            ((v172_industry_profit_acceleration.get("results") or {}).get("observed_test") or {})
            .get("cost_10bp") or {}
        )
        observed20 = (
            ((v172_industry_profit_acceleration.get("results") or {}).get("observed_test") or {})
            .get("cost_20bp") or {}
        )
        blockers.append({
            "category": "profitability",
            "label": "V170 行业利润扩散加速度已在唯一历史回测中永久失败",
            "current": (
                f"训练10bp {train10.get('total_return_pct', '—')}% · "
                f"观察10bp {observed10.get('total_return_pct', '—')}% · "
                f"20bp {observed20.get('total_return_pct', '—')}% · "
                f"回撤 {observed10.get('max_drawdown_pct', '—')}%"
            ),
            "target": "训练与观察成本后为正 · 观察年化≥15% · 回撤≤15% · Sharpe≥0.30 · PF≥1.20",
            "gap": "未通过 " + ", ".join(
                str(value)
                for value in v172_industry_profit_acceleration.get("failed_gates") or []
            ),
            "detail": "历史机会已消耗；规格、资产、阈值和成本均未改变。永久封存，不进入信号或交易。",
        })

    if v176_pmi_gap_acceleration and (
        v176_pmi_gap_acceleration.get("historical_strategy_gate_passed") is not True
    ):
        train10 = (
            ((v176_pmi_gap_acceleration.get("results") or {}).get("train") or {})
            .get("cost_10bp") or {}
        )
        observed10 = (
            ((v176_pmi_gap_acceleration.get("results") or {}).get("observed_test") or {})
            .get("cost_10bp") or {}
        )
        observed20 = (
            ((v176_pmi_gap_acceleration.get("results") or {}).get("observed_test") or {})
            .get("cost_20bp") or {}
        )
        stability = v176_pmi_gap_acceleration.get("observed_stability") or {}
        bootstrap = stability.get("monthly_bootstrap") or {}
        blockers.append({
            "category": "profitability",
            "label": "V174 PMI需求库存缺口加速度已在唯一历史回测中永久失败",
            "current": (
                f"训练10bp {train10.get('total_return_pct', '—')}% · "
                f"观察10bp {observed10.get('total_return_pct', '—')}% · "
                f"20bp {observed20.get('total_return_pct', '—')}% · "
                f"观察回撤 {observed10.get('max_drawdown_pct', '—')}% · "
                f"观察年化 {observed10.get('annualized_return_pct', '—')}%"
            ),
            "target": (
                "训练与观察成本后为正 · 观察年化≥15% · 回撤≤15% · "
                "Sharpe≥0.50 · PF≥1.25 · 2021–2025至少4年为正"
            ),
            "gap": "未通过 " + ", ".join(
                str(value)
                for value in v176_pmi_gap_acceleration.get("failed_gates") or []
            ),
            "detail": (
                f"观察期仅 {stability.get('positive_calendar_years_2021_2025', 0)}/5 年为正；"
                f"月度Bootstrap年化95%区间 {bootstrap.get('annualized_return_ci_95_pct') or '—'}。"
                "历史机会已消耗，永久封存，不生成买入提醒。"
            ),
        })

    if v178_sse_disciplinary_prices and (
        v178_sse_disciplinary_prices.get("price_data_gate_passed") is not True
    ):
        quality = v178_sse_disciplinary_prices.get("quality") or {}
        missing_files = quality.get("missing_files") or []
        blockers.append({
            "category": "data",
            "label": "V177 上交所纪律处分持仓风险机制在收益计算前永久淘汰",
            "current": (
                f"事件 {quality.get('event_records', 0)} 个 · "
                f"事件股票文件 {quality.get('stock_files', 0)}/"
                f"{quality.get('event_codes', 0)} · 缺失 {len(missing_files)} 个 · "
                f"收益计算 {v178_sse_disciplinary_prices.get('return_data_computed') is True}"
            ),
            "target": "冻结的535只事件股票价格文件全部存在并通过物理验收",
            "gap": "缺失的46只均为科创板688xxx；预注册禁止事后删除样本或补齐后重跑",
            "detail": (
                "V89监管事件数据仍作为已同步物理证据保留；V178预留的监管事件收益检验"
                "从未运行。其后另行预注册的V179同历月季节性机制结果见V180，"
                "两者不可混淆，均不生成买卖提醒。"
            ),
        })

    if (
        v180_seasonality_factor.get("single_factor_test_consumed") is True
        and v180_seasonality_factor.get("historical_factor_gate_passed") is not True
    ):
        train = (
            ((v180_seasonality_factor.get("splits") or {}).get("training_audit") or {})
            .get("metrics") or {}
        )
        observed = (
            ((v180_seasonality_factor.get("splits") or {}).get("observed_test") or {})
            .get("metrics") or {}
        )
        failed = {
            name: section.get("failed_gates") or []
            for name, section in (v180_seasonality_factor.get("splits") or {}).items()
        }
        blockers.append({
            "category": "profitability",
            "label": "V179 同历月收益季节性已在唯一历史因子检验中永久失败",
            "current": (
                f"训练IC {train.get('mean_rank_ic')} · 训练20BP价差 "
                f"{train.get('mean_d10_minus_d1_after_20bp')} · 观察IC "
                f"{observed.get('mean_rank_ic')} · 观察10BP价差 "
                f"{observed.get('mean_d10_minus_d1_after_10bp')}"
            ),
            "target": (
                "训练与观察均通过IC、单调性、10/20BP价差、D10长端、"
                "块自助置信区间和时间折"
            ),
            "gap": "未通过 " + json.dumps(failed, ensure_ascii=False),
            "detail": (
                f"观察段看似较强，但单调性仅 {observed.get('mean_monotonicity')}，"
                f"Bootstrap下限 {observed.get('bootstrap_95pct_lower')}；"
                "训练段方向和20BP压力结果不合格。不得生成买入提醒或改窗口重跑。"
            ),
        })

    if (
        v182_filing_timeliness_factor.get("single_factor_test_consumed") is True
        and v182_filing_timeliness_factor.get("historical_factor_gate_passed") is not True
    ):
        train = (
            ((v182_filing_timeliness_factor.get("splits") or {}).get("training_audit") or {})
            .get("metrics") or {}
        )
        observed = (
            ((v182_filing_timeliness_factor.get("splits") or {}).get("observed_test") or {})
            .get("metrics") or {}
        )
        failed = {
            name: section.get("failed_gates") or []
            for name, section in (v182_filing_timeliness_factor.get("splits") or {}).items()
        }
        blockers.append({
            "category": "profitability",
            "label": "V181 年报披露时效变化已在唯一历史因子检验中永久失败",
            "current": (
                f"训练IC {train.get('mean_rank_ic')} · 训练10BP价差 "
                f"{train.get('mean_d10_minus_d1_after_10bp')} · 观察IC "
                f"{observed.get('mean_rank_ic')} · 观察10BP价差 "
                f"{observed.get('mean_d10_minus_d1_after_10bp')}"
            ),
            "target": "训练与观察均通过IC、单调性、10/20BP价差、逐年、留一年和自助稳定性",
            "gap": "未通过 " + json.dumps(failed, ensure_ascii=False),
            "detail": (
                f"观察段看似为正，但单调性仅 {observed.get('mean_monotonicity')}，"
                f"留一年最差均值 {observed.get('leave_one_year_out_worst_mean_after_10bp')}；"
                "训练段方向、长端和成本后价差均为负。不得生成买入提醒或反向重跑。"
            ),
        })

    if v183_local_order_book.get("data_gate_passed") is not True:
        inventory = v183_local_order_book.get("inventory") or {}
        partitions = inventory.get("partitions") or {}
        blockers.append({
            "category": "data",
            "label": "V183 本地盘口目录不具备可回测的L2历史覆盖",
            "current": (
                f"日线 {((partitions.get('daily') or {}).get('files') or 0)} 只 · "
                f"5分钟 {((partitions.get('minute5') or {}).get('files') or 0)} 只 · "
                f"盘口 {((inventory.get('realtime_time_coverage') or {}).get('trading_days') or 0)} 日 · "
                f"文件哈希登记 {inventory.get('metadata_indexed_files', 0)}/"
                f"{inventory.get('total_parquet_files', 0)}"
            ),
            "target": "逐文件哈希 · 日线≥98%且零失败 · 5分钟/五档盘口≥1000只且≥120日 · 点时基本面",
            "gap": "未通过 " + ", ".join(
                str(value) for value in v183_local_order_book.get("failed_gates") or []
            ),
            "detail": "目录已同步展示，但验收阶段没有读取价格收益，也不会生成任何买卖提醒。",
        })

    if (
        v185_pre_ex_dividend_drift.get("single_factor_test_consumed") is True
        and v185_pre_ex_dividend_drift.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v185_pre_ex_dividend_drift.get("results") or {}).get("validation") or {}
        )
        test = (v185_pre_ex_dividend_drift.get("results") or {}).get("test") or {}
        blockers.append({
            "category": "profitability",
            "label": "V185 除息日前三日漂移已在唯一成本后检验中永久失败",
            "current": (
                f"验证均值 {validation.get('mean_net_return_pct')}% / 胜率 "
                f"{validation.get('win_rate_pct')}% · 测试均值 "
                f"{test.get('mean_net_return_pct')}% / 超额 "
                f"{test.get('mean_market_adjusted_return_pct')}% / 胜率 "
                f"{test.get('win_rate_pct')}%"
            ),
            "target": "验证与测试均需20BP后为正、Bootstrap下界>0、胜率≥60%、逐年及三个时间折全正",
            "gap": "未通过 " + ", ".join(
                str(value) for value in v185_pre_ex_dividend_drift.get("failed_gates") or []
            ),
            "detail": (
                "V184巨潮纯现金分红数据已完整通过；V185测试期方向转负，"
                "不得改成高股息子样本、改变持有窗口或反向重跑。"
            ),
        })

    if (
        v187_open_market_repurchase_drift.get("single_factor_test_consumed") is True
        and v187_open_market_repurchase_drift.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v187_open_market_repurchase_drift.get("results") or {}).get("validation") or {}
        )
        test = (
            (v187_open_market_repurchase_drift.get("results") or {}).get("test") or {}
        )
        blockers.append({
            "category": "profitability",
            "label": "V187 集中竞价回购公告漂移已在唯一成本后检验中永久失败",
            "current": (
                f"验证20BP {validation.get('mean_net_return_20bp_pct')}% / 胜率 "
                f"{validation.get('win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{validation.get('block_bootstrap_95pct_lower_20bp_pct')}% · 测试20BP "
                f"{test.get('mean_net_return_20bp_pct')}% / 超额 "
                f"{test.get('mean_market_adjusted_20bp_pct')}% / 胜率 "
                f"{test.get('win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{test.get('block_bootstrap_95pct_lower_20bp_pct')}%"
            ),
            "target": "验证与测试均需20BP及40BP后为正、Bootstrap下界>0、胜率≥60%、逐年及三个时间折全正",
            "gap": "未通过 " + ", ".join(
                str(value)
                for value in v187_open_market_repurchase_drift.get("failed_gates") or []
            ),
            "detail": (
                "V186巨潮集中竞价回购公告数据已完整通过；V187测试期局部强收益不能覆盖"
                "验证胜率、2023年收益和验证/测试时间区组稳定性失败，不得改窗口、改筛选或重跑。"
            ),
        })

    if (
        v189_ipo_lockup_release_reversal.get("single_factor_test_consumed") is True
        and v189_ipo_lockup_release_reversal.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v189_ipo_lockup_release_reversal.get("results") or {}).get("validation") or {}
        )
        test = (
            (v189_ipo_lockup_release_reversal.get("results") or {}).get("test") or {}
        )
        folds = v189_ipo_lockup_release_reversal.get(
            "test_time_folds_mean_net_return_20bp_pct"
        ) or []
        blockers.append({
            "category": "profitability",
            "label": "V189 首发前限售股上市流通公告后反转已在唯一成本后检验中永久失败",
            "current": (
                f"验证20BP {validation.get('mean_net_return_20bp_pct')}% / 超额 "
                f"{validation.get('mean_market_adjusted_20bp_pct')}% / 胜率 "
                f"{validation.get('win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{validation.get('block_bootstrap_95pct_lower_20bp_pct')}% · 测试20BP "
                f"{test.get('mean_net_return_20bp_pct')}% / 超额 "
                f"{test.get('mean_market_adjusted_20bp_pct')}% / 胜率 "
                f"{test.get('win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{test.get('block_bootstrap_95pct_lower_20bp_pct')}% · 三折 "
                + "/".join(str(value) + "%" for value in folds)
            ),
            "target": "验证与测试均需20BP及40BP后为正、Bootstrap下界>0、胜率≥60%、逐年及三个时间折全正",
            "gap": "未通过 " + ", ".join(
                str(value)
                for value in v189_ipo_lockup_release_reversal.get("failed_gates") or []
            ),
            "detail": (
                "V188巨潮首发前限售股上市流通公告数据已完整通过；V189测试期局部正收益"
                "不能覆盖验证期全面为负和测试第一时间折为负，不得改窗口、改筛选或重跑。"
            ),
        })

    if (
        v191_major_contract_5d_drift.get("single_factor_test_consumed") is True
        and v191_major_contract_5d_drift.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v191_major_contract_5d_drift.get("results") or {}).get("validation") or {}
        )
        test = (
            (v191_major_contract_5d_drift.get("results") or {}).get("test") or {}
        )
        folds = v191_major_contract_5d_drift.get(
            "test_time_folds_mean_net_return_20bp_pct"
        ) or []
        blockers.append({
            "category": "profitability",
            "label": "V191 重大合同公告后5交易日漂移已在唯一成本后检验中永久失败",
            "current": (
                f"验证20BP {validation.get('mean_net_return_20bp_pct')}% / 超额 "
                f"{validation.get('mean_market_adjusted_20bp_pct')}% / 胜率 "
                f"{validation.get('win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{validation.get('block_bootstrap_95pct_lower_20bp_pct')}% · 测试20BP "
                f"{test.get('mean_net_return_20bp_pct')}% / 超额 "
                f"{test.get('mean_market_adjusted_20bp_pct')}% / 胜率 "
                f"{test.get('win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{test.get('block_bootstrap_95pct_lower_20bp_pct')}% · 三折 "
                + "/".join(str(value) + "%" for value in folds)
            ),
            "target": "验证与测试均需20BP及40BP后为正、Bootstrap下界>0、胜率≥60%、逐年及三个时间折全正",
            "gap": "未通过 " + ", ".join(
                str(value) for value in v191_major_contract_5d_drift.get("failed_gates") or []
            ),
            "detail": (
                "V190巨潮重大合同签订公告数据已完整通过；V191训练、验证、测试和测试三折"
                "均为负，公告日即时反应没有转化为公告后可执行收益，不得改窗口或重跑。"
            ),
        })

    if (
        v193_52_week_high_factor.get("single_factor_test_consumed") is True
        and v193_52_week_high_factor.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v193_52_week_high_factor.get("results") or {}).get("validation") or {}
        )
        test = (
            (v193_52_week_high_factor.get("results") or {}).get("test") or {}
        )
        folds = v193_52_week_high_factor.get("test_folds") or []
        blockers.append({
            "category": "profitability",
            "label": "V193 52周高点月度因子已在唯一冻结成本后检验中永久失败",
            "current": (
                f"验证年化20BP {validation.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{validation.get('annualized_excess_return_pct')}% / 胜率 "
                f"{validation.get('monthly_win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{validation.get('block_bootstrap_95pct_lower_monthly_20bp_pct')}% · 测试年化20BP "
                f"{test.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{test.get('annualized_excess_return_pct')}% / 胜率 "
                f"{test.get('monthly_win_rate_20bp_pct')}% / 总超额 "
                f"{test.get('total_excess_return_pct')}% · 三折超额 "
                + "/".join(
                    str(fold.get("excess_return_pct")) + "%" for fold in folds
                )
            ),
            "target": "验证与测试均需年化净收益≥10%、年化超额≥5%、Bootstrap下界>0、胜率≥60%、逐年及三个时间折总收益和超额全正",
            "gap": "未通过 " + ", ".join(
                str(value) for value in v193_52_week_high_factor.get("failed_gates") or []
            ),
            "detail": (
                "V192已在收益读取前冻结252交易日窗口、月频前10%、20/40BP成本与全部门禁；"
                "V193训练期正收益没有延续到验证和测试。4802只V4日线已逐文件哈希核验，"
                "不得改分组、窗口、频率、成本、方向或筛选条件重跑。"
            ),
        })

    if (
        v195_low_volatility_factor.get("single_factor_test_consumed") is True
        and v195_low_volatility_factor.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v195_low_volatility_factor.get("results") or {}).get("validation") or {}
        )
        test = (
            (v195_low_volatility_factor.get("results") or {}).get("test") or {}
        )
        folds = v195_low_volatility_factor.get("test_folds") or []
        blockers.append({
            "category": "profitability",
            "label": "V195 36个月低波动月度因子已在唯一冻结成本后检验中永久失败",
            "current": (
                f"验证年化20BP {validation.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{validation.get('annualized_excess_return_pct')}% / 胜率 "
                f"{validation.get('monthly_win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{validation.get('block_bootstrap_95pct_lower_monthly_20bp_pct')}% · 测试年化20BP "
                f"{test.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{test.get('annualized_excess_return_pct')}% / 总超额 "
                f"{test.get('total_excess_return_pct')}% · 三折超额 "
                + "/".join(
                    str(fold.get("excess_return_pct")) + "%" for fold in folds
                )
            ),
            "target": "验证与测试均需年化净收益≥10%、年化超额≥5%、Bootstrap下界>0、胜率≥60%、逐年及三个时间折总收益和超额全正",
            "gap": "未通过 " + ", ".join(
                str(value) for value in v195_low_volatility_factor.get("failed_gates") or []
            ),
            "detail": (
                "V194已在收益读取前冻结论文核心的36个月月收益波动率、月频最低10%、"
                "20/40BP成本与全部门禁；V195验证期局部正超额没有延续为测试期绝对收益和超额稳定性。"
                "4802只V4日线已逐文件哈希核验，不得改窗口、方向、分组、成本或筛选重跑。"
            ),
        })

    if (
        v197_low_max_factor.get("single_factor_test_consumed") is True
        and v197_low_max_factor.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v197_low_max_factor.get("results") or {}).get("validation") or {}
        )
        test = (
            (v197_low_max_factor.get("results") or {}).get("test") or {}
        )
        folds = v197_low_max_factor.get("test_folds") or []
        blockers.append({
            "category": "profitability",
            "label": "V197 上月低MAX月度因子已在唯一冻结成本后检验中永久失败",
            "current": (
                f"验证年化20BP {validation.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{validation.get('annualized_excess_return_pct')}% / 胜率 "
                f"{validation.get('monthly_win_rate_20bp_pct')}% / Bootstrap下限 "
                f"{validation.get('block_bootstrap_95pct_lower_monthly_20bp_pct')}% · 测试年化20BP "
                f"{test.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{test.get('annualized_excess_return_pct')}% / 总超额 "
                f"{test.get('total_excess_return_pct')}% · 三折超额 "
                + "/".join(
                    str(fold.get("excess_return_pct")) + "%" for fold in folds
                )
            ),
            "target": "验证与测试均需年化净收益≥10%、年化超额≥5%、Bootstrap下界>0、胜率≥60%、逐年及三个时间折总收益和超额全正",
            "gap": "未通过 " + ", ".join(
                str(value) for value in v197_low_max_factor.get("failed_gates") or []
            ),
            "detail": (
                "V196已在收益读取前冻结上月最大单日收益、月频最低10%、至少252正常交易日、"
                "20/40BP成本与全部门禁；V197验证期防守性超额没有延续为测试期绝对收益和超额稳定性。"
                "4802只V4日线已逐文件哈希核验，不得改MAX天数、方向、分组、成本或筛选重跑。"
            ),
        })

    if (
        v201_low_market_beta_factor.get("single_factor_test_consumed") is True
        and v201_low_market_beta_factor.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v201_low_market_beta_factor.get("results") or {}).get("validation") or {}
        )
        test = (
            (v201_low_market_beta_factor.get("results") or {}).get("test") or {}
        )
        folds = v201_low_market_beta_factor.get("test_folds") or []
        blockers.append({
            "category": "profitability",
            "label": "V201 全市场252日低市场Beta已在唯一冻结检验中永久失败",
            "current": (
                f"验证年化20BP {validation.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{validation.get('annualized_excess_return_pct')}% / Bootstrap下限 "
                f"{validation.get('block_bootstrap_95pct_lower_monthly_20bp_pct')}% · 测试年化20BP "
                f"{test.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{test.get('annualized_excess_return_pct')}% / 总超额 "
                f"{test.get('total_excess_return_pct')}% · 三折超额 "
                + "/".join(str(fold.get("excess_return_pct")) + "%" for fold in folds)
            ),
            "target": "验证与测试均需年化净收益≥10%、年化超额≥5%、Bootstrap下界>0、胜率≥60%、逐年及测试三折总收益和超额全正",
            "gap": "未通过 " + ", ".join(
                str(value) for value in v201_low_market_beta_factor.get("failed_gates") or []
            ),
            "detail": (
                "V200已在未来收益读取前冻结252共同交易日上证Beta、月频最低十分组、"
                "20/40BP成本与全部门禁；V201训练正收益没有延续到验证和测试。"
                "4802只V4日线已逐文件哈希核验，不得改窗口、基准、方向、分组或成本重跑。"
            ),
        })

    if (
        v203_residual_momentum_factor.get("single_factor_test_consumed") is True
        and v203_residual_momentum_factor.get("historical_factor_gate_passed") is not True
    ):
        validation = (
            (v203_residual_momentum_factor.get("results") or {}).get("validation") or {}
        )
        test = (
            (v203_residual_momentum_factor.get("results") or {}).get("test") or {}
        )
        folds = v203_residual_momentum_factor.get("test_folds") or []
        execution = v203_residual_momentum_factor.get("execution") or {}
        blockers.append({
            "category": "profitability",
            "label": "V203 市场中性残差动量已在唯一冻结检验中永久失败",
            "current": (
                f"验证年化20BP {validation.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{validation.get('annualized_excess_return_pct')}% / 回撤 "
                f"{validation.get('max_drawdown_20bp_pct')}% · 测试年化20BP "
                f"{test.get('annualized_net_return_20bp_pct')}% / 年化超额 "
                f"{test.get('annualized_excess_return_pct')}% / 胜率 "
                f"{test.get('monthly_win_rate_20bp_pct')}% / 总超额 "
                f"{test.get('total_excess_return_pct')}% · 三折超额 "
                + "/".join(str(fold.get("excess_return_pct")) + "%" for fold in folds)
            ),
            "target": "验证与测试均需年化净收益≥10%、年化超额≥5%、Bootstrap下界>0、胜率≥60%、逐年及测试三折总收益和超额全正，且执行目标全部解决",
            "gap": "未通过 " + ", ".join(
                str(value) for value in v203_residual_momentum_factor.get("failed_gates") or []
            ),
            "detail": (
                "V202在未来收益读取前冻结252日OLS Alpha、跳过近20日、月频最高十分组、"
                "20/40BP成本与全部门禁；V203先选股再检查执行，"
                f"现金槽{execution.get('cash_target_slots')}个、延期退出{execution.get('delayed_exits')}个、"
                f"未解决{execution.get('unresolved_target_slots')}个。不得改窗口、基准、方向、分组、成本或执行规则重跑。"
            ),
        })

    forward_signals = forward_evaluation.get("signal_observations") or {}
    forward_outcomes = (forward_evaluation.get("outcome_summary") or {}).get("base") or {}
    forward_portfolio = (forward_evaluation.get("portfolio_summary") or {}).get("base") or {}
    forward_candidate_invalidated = (
        training_integrity.get("model_training_integrity_gate_passed") is False
    )
    if forward_evaluation and forward_evaluation.get("forward_gate_passed") is not True:
        blockers.append({
            "category": "profitability",
            "label": (
                "旧 V86 前向证据不足且候选已失效"
                if forward_candidate_invalidated else
                "冻结候选尚无未见数据前向盈利证据"
            ),
            "current": (
                f"周度观察 {forward_schedule.get('scheduled_observations', 0)}/12 · "
                f"信号结果 {forward_outcomes.get('completed', 0)}/60 · "
                f"组合交易 {forward_portfolio.get('completed', 0)}/12"
            ),
            "target": (
                "由新策略 ID 重新完成独立前向验证"
                if forward_candidate_invalidated else
                "≥12 周 · ≥60 个信号结果 · ≥12 笔组合交易 · 成本压力后净利为正"
            ),
            "gap": (
                "V86 不再累计或等待样本；旧计时不能解锁任何推荐"
                if forward_candidate_invalidated else
                f"首次可计数日期 {forward_schedule.get('first_eligible_date') or '未知'}；"
                "只能等待新增交易日，禁止回填"
            ),
            "detail": (
                "未通过门禁 "
                + ", ".join(
                    str(value) for value in forward_evaluation.get("failed_gates") or []
                )
            ),
        })

    purged_validation = training_integrity.get("purged_validation") or {}
    if training_integrity and training_integrity.get("model_training_integrity_gate_passed") is not True:
        blockers.append({
            "category": "integrity",
            "label": "候选训练边界与净收益质量不合格",
            "current": (
                f"重叠标签 {training_integrity.get('training_overlap_finite_label_rows', 0)} 行 · "
                f"净化验证年化 {purged_validation.get('annualized_return_pct')}% · "
                f"回撤 {purged_validation.get('max_drawdown_pct')}% · "
                f"利润因子 {purged_validation.get('profit_factor')}"
            ),
            "target": "训练/验证标签零重叠 · 年化 ≥15% · 回撤 ≤15% · 利润因子 ≥1.20",
            "gap": "当前候选必须维持失效状态，不能进入推荐或资金分配",
        })

    discovery_round = int(
        discovery_assessment.get("data_epoch_round")
        or discovery_assessment.get("discovery_round")
        or 0
    )
    discovery_limit = int(
        successor_admission.get("maximum_discovery_rounds_per_epoch") or 30
    )
    discovery_cap_reached = discovery_round >= discovery_limit
    discovery_ids = discovery_assessment.get("candidate_ids") or []
    discovery_reasons = discovery_assessment.get("reasons") or []
    if (
        discovery_assessment
        and discovery_assessment.get("actionable_candidate") is not True
        and v205_multi_asset_rotation.get("historical_candidate_passed") is not True
    ):
        latest_test = (
            v203_residual_momentum_factor
            if v203_residual_momentum_factor.get("future_return_data_read") is True
            else v201_low_market_beta_factor
            if v201_low_market_beta_factor.get("future_return_data_read") is True
            else v197_low_max_factor
            if v197_low_max_factor.get("return_data_read") is True
            else v195_low_volatility_factor
            if v195_low_volatility_factor.get("return_data_read") is True
            else v193_52_week_high_factor
            if v193_52_week_high_factor.get("return_data_read") is True
            else v191_major_contract_5d_drift
            if v191_major_contract_5d_drift.get("return_data_read") is True
            else v189_ipo_lockup_release_reversal
            if v189_ipo_lockup_release_reversal.get("return_data_read") is True
            else v187_open_market_repurchase_drift
            if v187_open_market_repurchase_drift.get("return_data_read") is True
            else v185_pre_ex_dividend_drift
            if v185_pre_ex_dividend_drift.get("return_data_read") is True
            else v182_filing_timeliness_factor
            if v182_filing_timeliness_factor.get("return_data_read") is True
            else v180_seasonality_factor
            if v180_seasonality_factor.get("return_data_read") is True
            else v178_sse_disciplinary_prices
            if v178_sse_disciplinary_prices.get("price_data_read") is True
            else v176_pmi_gap_acceleration
            if v176_pmi_gap_acceleration.get("single_historical_backtest_consumed") is True
            else v172_industry_profit_acceleration
        )
        if latest_test.get("single_factor_test_consumed") is True:
            is_v203 = latest_test is v203_residual_momentum_factor
            is_v201 = latest_test is v201_low_market_beta_factor
            is_v197 = latest_test is v197_low_max_factor
            is_v195 = latest_test is v195_low_volatility_factor
            is_v193 = latest_test is v193_52_week_high_factor
            is_v191 = latest_test is v191_major_contract_5d_drift
            is_v189 = latest_test is v189_ipo_lockup_release_reversal
            is_v187 = latest_test is v187_open_market_repurchase_drift
            is_v185 = latest_test is v185_pre_ex_dividend_drift
            is_v182 = latest_test is v182_filing_timeliness_factor
            blockers.append({
                "category": "research",
                "label": (
                    "V203失败后，下一独立机制尚未预注册"
                    if is_v203 else
                    "V201失败后，下一独立机制尚未预注册"
                    if is_v201 else
                    "V197失败后，下一独立机制尚未预注册"
                    if is_v197 else
                    "V195失败后，下一独立机制尚未预注册"
                    if is_v195 else
                    "V193失败后，下一独立机制尚未预注册"
                    if is_v193 else
                    "V191失败后，下一独立机制尚未预注册"
                    if is_v191 else
                    "V189失败后，下一独立机制尚未预注册"
                    if is_v189 else
                    "V187失败后，下一独立机制尚未预注册"
                    if is_v187 else
                    "V185失败后，下一独立机制尚未预注册"
                    if is_v185 else
                    "V182失败后，下一独立机制尚未预注册"
                    if is_v182 else "V180失败后，下一独立机制尚未预注册"
                ),
                "current": (
                    "V202冻结准入已通过 · V203唯一成本后检验失败 · 下一候选为空"
                    if is_v203 else
                    "V200冻结准入已通过 · V201唯一成本后检验失败 · 下一候选为空"
                    if is_v201 else
                    "V196冻结准入已通过 · V197唯一成本后检验失败 · 下一候选为空"
                    if is_v197 else
                    "V194冻结准入已通过 · V195唯一成本后检验失败 · 下一候选为空"
                    if is_v195 else
                    "V192冻结准入已通过 · V193唯一成本后检验失败 · 下一候选为空"
                    if is_v193 else
                    "V190重大合同公告数据已验收 · V191唯一成本后检验失败 · 下一候选为空"
                    if is_v191 else
                    "V188首发前限售股上市流通公告数据已验收 · V189唯一成本后检验失败 · 下一候选为空"
                    if is_v189 else
                    "V186集中竞价回购公告数据已验收 · V187唯一成本后检验失败 · 下一候选为空"
                    if is_v187 else
                    "V184现金分红数据已验收 · V185唯一成本后检验失败 · 下一候选为空"
                    if is_v185 else
                    "V179季节性与V181披露时效唯一因子检验失败 · 下一候选为空"
                    if is_v182 else
                    "V170/V174唯一策略回测失败 · V177收益前失败 · "
                    "V179唯一因子检验失败 · 下一候选为空"
                ),
                "target": "提出与既有宏观、基本面、事件、日历、价格动量、低波动、彩票型极端收益、披露时效、分红、回购、限售供给和重大合同机制不同的机制，并先冻结全部门禁",
                "gap": "尚无新的来源审查、数据可行性检查和收益读取前预注册",
                "detail": "观察段局部正收益不能覆盖训练反向和稳定性失败；禁止改窗口、改方向或组合旧失败因子补救。",
            })
        elif latest_test.get("single_historical_backtest_consumed") is True:
            blockers.append({
                "category": "research",
                "label": "V176失败后，下一独立机制尚未预注册",
                "current": (
                    "V167收益前淘汰 · V170唯一回测永久失败 · "
                    "V174唯一回测永久失败 · 下一候选为空"
                ),
                "target": "提出与既有候选不同的机制，并先冻结公式、资产、时点、成本和证伪门槛",
                "gap": "尚无完成来源审查、样本可行性检查和回测前预注册的新候选",
                "detail": (
                    "PMI需求库存/能源与行业利润扩散/工业指数机制均已污染或失败；"
                    "新候选必须结构独立。准入前禁止读取收益、放宽门槛或解锁推荐与交易。"
                ),
            })
        elif latest_test.get("status") == "permanently_rejected_before_return_computation":
            blockers.append({
                "category": "research",
                "label": "V178收益前失败后，下一独立机制尚未预注册",
                "current": (
                    "V170与V174唯一回测永久失败 · V177价格门禁永久失败 · 下一候选为空"
                ),
                "target": "提出与既有宏观、价格、基本面和监管事件候选不同的机制，并先冻结全部门禁",
                "gap": "尚无完成来源审查、全量数据可行性检查和收益读取前预注册的新候选",
                "detail": "新候选准入前禁止读取收益、删除缺失样本、放宽门槛或解锁推荐与交易。",
            })
        else:
            blockers.append({
                "category": "research",
                "label": (
                    "后继探索已达轮次上限且无可检验候选"
                    if discovery_cap_reached else
                    "后继新机制探索尚无可检验候选"
                ),
                "current": (
                    f"第 {discovery_round}/{discovery_limit} 轮 · "
                    f"{', '.join(str(value) for value in discovery_ids) or '无候选'} · 已拒绝"
                ),
                "target": (
                    "先验收全新授权点时数据纪元，再启动新的有限轮次探索"
                    if discovery_cap_reached else
                    "全新机制、点时数据、证伪方案和开源注册四方一致接收"
                ),
                "gap": (
                    "同一数据纪元已停止继续生成；"
                    if discovery_cap_reached else ""
                ) + (
                    "；".join(str(value) for value in discovery_reasons)
                    or "本轮没有通过语义完整性与来源审查"
                ),
                "detail": (
                    "新数据纪元验收前禁止继续发现、回测、改策略代码、放宽门禁或解锁交易。"
                    if discovery_cap_reached else
                    "接收前禁止回测、改策略代码、放宽门禁或解锁交易。"
                ),
            })

    if gate.get("recommendation_eligible") is not True:
        blockers.append({
            "category": "integrity",
            "label": "严格策略门禁仍锁定",
            "current": "无可启用策略",
            "target": "同一冻结候选通过独立复现、含成本历史和前向验证",
            "gap": str(gate.get("reason") or "未通过严格证据门禁"),
        })

    data_ready = bool(raw_ready and snapshot_current)
    decision_data_ready = bool(
        data_ready
        and product_ok
        and margin_physical.get("passed") is True
        and v90_pmi_physical.get("passed") is True
        and v120_pbc_physical.get("passed") is True
        and v143_cpi_ppi_physical.get("passed") is True
        and v145_industrial_profit_physical.get("passed") is True
        and v12_ready
        and v148_ready
        and v149_ready
        and v150_ready
        and v162_ready
        and indexes_current
        and benchmark_indexes_current
        and calendar_current
    )
    result = {
        "generated_at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
        "status": "degraded",
        "data_ready": data_ready,
        "fully_synced": False,
        "raw_daily": {
            "ready": raw_ready,
            "as_of": raw_asof,
            "latest_data_as_of": raw_asof,
            "quality_as_of": quality_asof,
            "sync_status": progress.get("status") or "missing",
            "sync_failed_symbols": sync_failed_symbols,
            "ready_reason": raw_ready_reason,
            "processed_symbols": effective_processed,
            "total_symbols": total,
            "rows_added": int(progress.get("rows_added") or 0),
            "quality_passed_files": daily_quality_passed,
            "quality_failed_files": daily_quality_failed,
            "newer_sync_pending": bool(
                progress.get("status") == "running"
                and str(progress.get("requested_end_date") or "") > raw_asof
            ),
            "pending_sync_requested_end": progress.get("requested_end_date"),
        },
        "research_snapshot": {
            "ready": snapshot_current,
            "as_of": snapshot_asof,
            "generated_at": snapshot.get("generated_at"),
            "universe": int(snapshot.get("universe") or len(snapshot.get("stocks") or [])),
            "audit_invalidated": snapshot.get("audit_invalidated") is True,
        },
        "product_sync": {
            "ready": product_ok,
            "status": product.get("status") or "missing",
            "heartbeat_at": product.get("heartbeatAt"),
            "error": product.get("error"),
        },
        "research_data": {
            "v12_fundamentals": {
                "ready": v12_ready,
                "status": v12_fundamentals.get("status") or "missing",
                "records": int(v12_fundamentals.get("fundamental_records") or 0),
                "symbols": int(v12_fundamentals.get("symbols_with_records") or 0),
                "periods": int(v12_fundamentals.get("stat_periods") or 0),
                "publication_after_deadline_rows": int(
                    v12_temporal.get("publication_after_statutory_deadline_rows") or 0
                ),
                "publication_after_deadline_pct": v12_temporal.get(
                    "publication_after_statutory_deadline_pct"
                ),
                "separate_publication_date_coverage_pct": v12_temporal.get(
                    "separate_publication_date_coverage_pct"
                ),
                "initial_announcement_semantics_verified": (
                    v12_temporal.get("initial_announcement_semantics_verified") is True
                ),
                "roe_coverage_pct": (
                    ((v12_fields.get("fields") or {}).get("roe_avg") or {}).get(
                        "coverage_pct"
                    )
                ),
                "return_data_allowed": v12_ready,
            },
            "v148_cninfo_filing_times": {
                "ready": v148_ready,
                "status": (
                    v148_filing_status.get("status")
                    or v148_filing_times.get("status")
                    or "missing"
                ),
                "scope": v148_filing_times.get("scope"),
                "periods": int(
                    ((v148_filing_times.get("counts") or {}).get("periods") or 0)
                ),
                "expected_periods": 33,
                "mapped_code_periods": int(
                    ((v148_filing_times.get("counts") or {}).get("mapped_code_periods") or 0)
                ),
                "coverage_pct": (
                    (v148_filing_times.get("counts") or {}).get(
                        "overall_legacy_code_coverage_pct"
                    )
                ),
                "failed_gates": v148_filing_times.get("failed_gates") or [],
                "return_data_read": v148_filing_times.get("return_data_read") is True,
                "successor_discovery_allowed": (
                    v148_filing_times.get("successor_discovery_allowed") is True
                ),
                "successor_backtest_allowed": (
                    v148_filing_times.get("successor_backtest_allowed") is True
                ),
            },
            "v149_sse_xbrl_profitability": {
                "ready": v149_ready,
                "status": (
                    v149_xbrl_status.get("status")
                    or v149_xbrl_profitability.get("status")
                    or "missing"
                ),
                "scope": v149_xbrl_profitability.get("scope"),
                "processed_codes": int(
                    ((v149_xbrl_profitability.get("counts") or {}).get("processed_codes") or 0)
                ),
                "universe_codes": int(
                    ((v149_xbrl_profitability.get("counts") or {}).get("universe_codes") or 1842)
                ),
                "records": int(
                    ((v149_xbrl_profitability.get("counts") or {}).get("records") or 0)
                ),
                "filing_join_coverage_pct": (
                    (v149_xbrl_profitability.get("counts") or {}).get(
                        "overall_filing_join_coverage_pct"
                    )
                ),
                "expected_code_period_coverage_pct": (
                    (v149_xbrl_profitability.get("counts") or {}).get(
                        "expected_code_period_coverage_pct"
                    )
                ),
                "roe_coverage_pct": (
                    (v149_xbrl_profitability.get("field_coverage_pct") or {}).get(
                        "roe_pct"
                    )
                ),
                "failed_gates": v149_xbrl_profitability.get("failed_gates") or [],
                "return_data_read": v149_xbrl_profitability.get("return_data_read") is True,
                "successor_discovery_allowed": (
                    v149_xbrl_profitability.get("successor_discovery_allowed") is True
                ),
                "successor_backtest_allowed": (
                    v149_xbrl_profitability.get("successor_backtest_allowed") is True
                ),
            },
            "v150_sse_xbrl_roe": {
                "ready": v150_ready,
                "status": v150_roe_epoch.get("status") or "missing",
                "source_records": int(
                    ((v150_roe_epoch.get("counts") or {}).get("source_records") or 0)
                ),
                "complete_roe_records": int(
                    ((v150_roe_epoch.get("counts") or {}).get("complete_roe_records") or 0)
                ),
                "codes": int(
                    ((v150_roe_epoch.get("counts") or {}).get("codes") or 0)
                ),
                "roe_source_coverage_pct": v150_roe_epoch.get(
                    "roe_source_coverage_pct"
                ),
                "complete_point_in_time_coverage_pct": v150_roe_epoch.get(
                    "complete_point_in_time_coverage_pct"
                ),
                "failed_gates": v150_roe_epoch.get("failed_gates") or [],
                "return_data_read": v150_roe_epoch.get("return_data_read") is True,
                "successor_discovery_allowed": (
                    v150_roe_epoch.get("successor_discovery_allowed") is True
                ),
                "successor_backtest_allowed": (
                    v150_roe_epoch.get("successor_backtest_allowed") is True
                ),
            },
            "v152_v151_adjusted_prices": {
                "ready": v152_ready,
                "status": v152_adjusted_prices.get("status") or "missing",
                "eligible_codes": int(
                    ((v152_adjusted_prices.get("counts") or {}).get("eligible_codes") or 0)
                ),
                "codes_with_rows": int(
                    ((v152_adjusted_prices.get("counts") or {}).get("codes_with_rows") or 0)
                ),
                "minimum_month_coverage_pct": (
                    (v152_adjusted_prices.get("coverage") or {}).get(
                        "minimum_month_coverage_pct"
                    )
                ),
                "price_data_read": v152_adjusted_prices.get("price_data_read") is True,
                "return_data_computed": (
                    v152_adjusted_prices.get("return_data_computed") is True
                ),
                "failed_gates": v152_adjusted_prices.get("failed_gates") or [],
                "single_factor_test_allowed": (
                    v152_adjusted_prices.get("single_factor_test_allowed") is True
                    and v153_roe_factor.get("return_data_read") is not True
                ),
            },
            "v154_quarterly_asset_growth": {
                "ready": v154_ready,
                "status": (
                    v154_asset_growth_status.get("status")
                    or v154_asset_growth.get("status") or "missing"
                ),
                "processed_codes": int(
                    v154_asset_growth_status.get("processed_codes")
                    or ((v154_asset_growth.get("counts") or {}).get("processed_codes") or 0)
                ),
                "universe_codes": int(
                    v154_asset_growth_status.get("total_codes")
                    or ((v154_asset_growth.get("counts") or {}).get("universe_codes") or 1842)
                ),
                "periods": int(
                    ((v154_asset_growth.get("counts") or {}).get("periods") or 0)
                ),
                "records": int(
                    v154_asset_growth_status.get("records")
                    or ((v154_asset_growth.get("counts") or {}).get("records") or 0)
                ),
                "complete_pit_records": int(
                    ((v154_asset_growth.get("counts") or {}).get(
                        "records_with_complete_pit_yoy_asset"
                    ) or 0)
                ),
                "complete_pit_coverage_pct": (
                    (v154_asset_growth.get("coverage_pct") or {}).get(
                        "complete_pit_yoy_asset_overall"
                    )
                ),
                "cross_validation_pairs": (
                    (v154_asset_growth.get("annual_sse_xbrl_cross_validation") or {}).get("pairs")
                ),
                "cross_validation_match_pct": (
                    (v154_asset_growth.get("annual_sse_xbrl_cross_validation") or {}).get("match_pct")
                ),
                "failed_gates": v154_asset_growth.get("failed_gates") or [],
                "missing_codes": int(v154_asset_growth_status.get("missing_codes") or 0),
                "source_error": v154_asset_growth_status.get("source_error"),
                "price_data_read": v154_asset_growth.get("price_data_read") is True,
                "return_data_read": v154_asset_growth.get("return_data_read") is True,
                "successor_admission_allowed": (
                    v154_asset_growth.get("successor_admission_allowed") is True
                ),
            },
            "v158_eastmoney_quarterly_asset_growth": {
                "ready": v158_ready,
                "status": (
                    v158_eastmoney_asset_growth_status.get("status")
                    or v158_eastmoney_asset_growth.get("status")
                    or "missing"
                ),
                "periods": int(
                    ((v158_eastmoney_asset_growth.get("counts") or {}).get("periods") or 0)
                ),
                "expected_periods": 33,
                "records": int(
                    ((v158_eastmoney_asset_growth.get("counts") or {}).get("records") or 0)
                ),
                "yoy_asset_coverage_pct": (
                    (v158_eastmoney_asset_growth.get("coverage_pct") or {}).get(
                        "yoy_asset_overall"
                    )
                ),
                "notice_within_3_days_coverage_pct": (
                    (v158_eastmoney_asset_growth.get("coverage_pct") or {}).get(
                        "notice_within_3_days_overall"
                    )
                ),
                "complete_pit_coverage_pct": (
                    (v158_eastmoney_asset_growth.get("coverage_pct") or {}).get(
                        "complete_pit_yoy_asset_overall"
                    )
                ),
                "cross_vendor_match_pct": (
                    v158_eastmoney_asset_growth.get(
                        "cross_vendor_baostock_validation"
                    )
                    or {}
                ).get("match_pct"),
                "official_annual_match_pct": (
                    (
                        v158_eastmoney_asset_growth.get(
                            "official_sse_xbrl_annual_validation"
                        )
                        or {}
                    ).get("overall")
                    or {}
                ).get("within_0_02_pct"),
                "annual_zero_notice_match_periods": (
                    (
                        v158_eastmoney_asset_growth.get("point_in_time_diagnosis")
                        or {}
                    ).get("annual_periods_with_zero_3_day_notice_match")
                    or []
                ),
                "numeric_values_cross_validated": (
                    (
                        v158_eastmoney_asset_growth.get("point_in_time_diagnosis")
                        or {}
                    ).get("numeric_values_cross_validated") is True
                ),
                "failed_gates": v158_eastmoney_asset_growth.get("failed_gates") or [],
                "price_data_read": v158_eastmoney_asset_growth.get("price_data_read") is True,
                "return_data_read": v158_eastmoney_asset_growth.get("return_data_read") is True,
                "successor_admission_allowed": (
                    v158_eastmoney_asset_growth.get("successor_admission_allowed") is True
                ),
            },
            "v162_official_quarterly_asset_growth": {
                "ready": v162_ready,
                "status": (
                    v162_asset_growth_status.get("status")
                    or v162_asset_growth.get("status") or "missing"
                ),
                "records": int(
                    ((v162_asset_growth.get("counts") or {}).get("records") or 0)
                ),
                "periods": int(
                    ((v162_asset_growth.get("counts") or {}).get("periods") or 0)
                ),
                "official_asset_targets": int(
                    ((v162_asset_growth.get("counts") or {}).get("official_asset_targets") or 0)
                ),
                "official_pdf_accepted": int(
                    ((v162_asset_growth.get("counts") or {}).get("official_pdf_accepted") or 0)
                ),
                "delayed_reports_found": int(
                    ((v162_asset_growth.get("counts") or {}).get("delayed_reports_found") or 0)
                ),
                "complete_pit_coverage_pct": (
                    (v162_asset_growth.get("coverage_pct") or {}).get(
                        "complete_pit_yoy_asset_overall"
                    )
                ),
                "annual_2019_coverage_pct": (
                    (v162_asset_growth.get("coverage_pct") or {}).get(
                        "complete_pit_yoy_asset_by_period", {}
                    ).get("2019-12-31")
                ),
                "query_archives": int(
                    ((v162_asset_growth.get("physical_evidence") or {}).get("query_archives") or 0)
                ),
                "pdf_archives": int(
                    ((v162_asset_growth.get("physical_evidence") or {}).get("pdf_archives") or 0)
                ),
                "failed_gates": v162_asset_growth.get("failed_gates") or [],
                "price_data_read": v162_asset_growth.get("price_data_read") is True,
                "return_data_read": v162_asset_growth.get("return_data_read") is True,
                "successor_admission_allowed": (
                    v162_asset_growth.get("successor_admission_allowed") is True
                ),
            },
            "v164_v163_adjusted_prices": {
                "ready": v164_ready,
                "status": v164_asset_growth_prices.get("status") or "missing",
                "eligible_codes": int(
                    ((v164_asset_growth_prices.get("counts") or {}).get("eligible_codes") or 0)
                ),
                "codes_with_rows": int(
                    ((v164_asset_growth_prices.get("counts") or {}).get("codes_with_rows") or 0)
                ),
                "rows": int(
                    ((v164_asset_growth_prices.get("counts") or {}).get("rows") or 0)
                ),
                "minimum_month_coverage_pct": (
                    (v164_asset_growth_prices.get("coverage") or {}).get(
                        "minimum_month_coverage_pct"
                    )
                ),
                "price_data_read": v164_asset_growth_prices.get("price_data_read") is True,
                "return_data_computed": (
                    v164_asset_growth_prices.get("return_data_computed") is True
                ),
                "failed_gates": v164_asset_growth_prices.get("failed_gates") or [],
                "single_factor_test_allowed": (
                    v164_asset_growth_prices.get("single_factor_test_allowed") is True
                    and v165_asset_growth_factor.get("return_data_read") is not True
                ),
            },
            "v156_v155_adjusted_prices": {
                "ready": v156_ready,
                "status": v156_asset_growth_prices.get("status") or "missing",
                "eligible_codes": int(
                    ((v156_asset_growth_prices.get("counts") or {}).get("eligible_codes") or 0)
                ),
                "codes_with_rows": int(
                    ((v156_asset_growth_prices.get("counts") or {}).get("codes_with_rows") or 0)
                ),
                "minimum_month_coverage_pct": (
                    (v156_asset_growth_prices.get("coverage") or {}).get(
                        "minimum_month_coverage_pct"
                    )
                ),
                "price_data_read": v156_asset_growth_prices.get("price_data_read") is True,
                "return_data_computed": (
                    v156_asset_growth_prices.get("return_data_computed") is True
                ),
                "failed_gates": v156_asset_growth_prices.get("failed_gates") or [],
                "single_factor_test_allowed": (
                    v156_asset_growth_prices.get("single_factor_test_allowed") is True
                    and v157_asset_growth_factor.get("return_data_read") is not True
                ),
            },
            "v166_nbs_industry_profit_panel": {
                "ready": v166_industry_profit_panel.get("data_gate_passed") is True,
                "status": v166_industry_profit_panel.get("status") or "missing",
                "periods": int(v166_industry_profit_panel.get("periods") or 0),
                "industries": int(v166_industry_profit_panel.get("industries") or 0),
                "records": int(v166_industry_profit_panel.get("records") or 0),
                "yoy_coverage_pct": v166_industry_profit_panel.get("yoy_coverage_pct"),
                "official_note_missing_yoy_records": int(
                    v166_industry_profit_panel.get("official_note_missing_yoy_records") or 0
                ),
                "return_data_read": v166_industry_profit_panel.get("return_data_read") is True,
                "successor_discovery_allowed": (
                    v166_industry_profit_panel.get("successor_discovery_allowed") is True
                ),
                "successor_backtest_allowed": (
                    v166_industry_profit_panel.get("successor_backtest_allowed") is True
                ),
                "failed_gates": v166_industry_profit_panel.get("failed_gates") or [],
            },
            "v168_csi300_industrial_prices": {
                "ready": (
                    v168_industrial_index_prices.get("price_data_gate_passed") is True
                ),
                "status": v168_industrial_index_prices.get("status") or "missing",
                "rows": int(
                    (v168_industrial_index_prices.get("quality") or {}).get("rows") or 0
                ),
                "first_date": (
                    v168_industrial_index_prices.get("quality") or {}
                ).get("first_date"),
                "last_date": (
                    v168_industrial_index_prices.get("quality") or {}
                ).get("last_date"),
                "missing_calendar_dates": len(
                    (v168_industrial_index_prices.get("quality") or {}).get(
                        "missing_calendar_dates"
                    ) or []
                ),
                "price_data_read": (
                    v168_industrial_index_prices.get("price_data_read") is True
                ),
                "return_data_computed": (
                    v168_industrial_index_prices.get("return_data_computed") is True
                ),
                "single_historical_backtest_allowed": False,
                "failed_gates": v168_industrial_index_prices.get("failed_gates") or [],
            },
            "v171_csi_all_share_industrial_prices": {
                "ready": (
                    v171_all_share_industrial_prices.get("price_data_gate_passed")
                    is True
                ),
                "status": v171_all_share_industrial_prices.get("status") or "missing",
                "source": v171_all_share_industrial_prices.get("source"),
                "rows": int(
                    (v171_all_share_industrial_prices.get("quality") or {}).get("rows")
                    or 0
                ),
                "first_date": (
                    v171_all_share_industrial_prices.get("quality") or {}
                ).get("first_date"),
                "last_date": (
                    v171_all_share_industrial_prices.get("quality") or {}
                ).get("last_date"),
                "missing_calendar_dates": len(
                    (v171_all_share_industrial_prices.get("quality") or {}).get(
                        "missing_calendar_dates"
                    ) or []
                ),
                "price_data_read": (
                    v171_all_share_industrial_prices.get("price_data_read") is True
                ),
                "return_data_computed": (
                    v171_all_share_industrial_prices.get("return_data_computed")
                    is True
                ),
                "single_historical_backtest_allowed": (
                    v171_all_share_industrial_prices.get(
                        "single_historical_backtest_allowed"
                    ) is True
                    and v172_industry_profit_acceleration.get(
                        "single_historical_backtest_consumed"
                    ) is not True
                ),
                "single_historical_backtest_consumed": (
                    v172_industry_profit_acceleration.get(
                        "single_historical_backtest_consumed"
                    ) is True
                ),
                "failed_gates": (
                    v171_all_share_industrial_prices.get("failed_gates") or []
                ),
            },
            "v173_nbs_pmi_demand_inventory": {
                "ready": v173_pmi_demand_inventory.get("data_gate_passed") is True,
                "status": v173_pmi_demand_inventory.get("status") or "missing",
                "data_epoch_id": v173_pmi_demand_inventory.get("data_epoch_id"),
                "actual_months": int(
                    v173_pmi_demand_inventory.get("actual_months") or 0
                ),
                "expected_months": int(
                    v173_pmi_demand_inventory.get("expected_months") or 120
                ),
                "component_records": int(
                    v173_pmi_demand_inventory.get("actual_months") or 0
                ) * 6,
                "release_page_crosschecked_months": int(
                    v173_pmi_demand_inventory.get(
                        "release_page_crosschecked_months"
                    ) or 0
                ),
                "release_page_mismatch_months": len(
                    v173_pmi_demand_inventory.get("release_page_mismatch_months")
                    or []
                ),
                "source_archives": int(
                    v173_pmi_demand_inventory.get("source_archives") or 0
                ),
                "price_data_read": (
                    v173_pmi_demand_inventory.get("price_data_read") is True
                ),
                "return_data_read": (
                    v173_pmi_demand_inventory.get("return_data_read") is True
                ),
                "successor_admission_allowed": (
                    v173_pmi_demand_inventory.get("successor_admission_allowed")
                    is True
                ),
                "failed_gates": v173_pmi_demand_inventory.get("failed_gates") or [],
            },
            "v175_csi_all_share_energy_prices": {
                "ready": (
                    v175_all_share_energy_prices.get("price_data_gate_passed")
                    is True
                ),
                "status": v175_all_share_energy_prices.get("status") or "missing",
                "source": v175_all_share_energy_prices.get("source"),
                "source_symbol": v175_all_share_energy_prices.get("source_symbol"),
                "rows": int(
                    (v175_all_share_energy_prices.get("quality") or {}).get("rows")
                    or 0
                ),
                "first_date": (
                    v175_all_share_energy_prices.get("quality") or {}
                ).get("first_date"),
                "last_date": (
                    v175_all_share_energy_prices.get("quality") or {}
                ).get("last_date"),
                "missing_calendar_dates": len(
                    (v175_all_share_energy_prices.get("quality") or {}).get(
                        "missing_calendar_dates"
                    ) or []
                ),
                "price_data_read": (
                    v175_all_share_energy_prices.get("price_data_read") is True
                ),
                "return_data_computed": (
                    v175_all_share_energy_prices.get("return_data_computed") is True
                ),
                "single_historical_backtest_allowed": (
                    v175_all_share_energy_prices.get(
                        "single_historical_backtest_allowed"
                    ) is True
                    and v176_pmi_gap_acceleration.get(
                        "single_historical_backtest_consumed"
                    ) is not True
                ),
                "single_historical_backtest_consumed": (
                    v176_pmi_gap_acceleration.get(
                        "single_historical_backtest_consumed"
                    ) is True
                ),
                "failed_gates": v175_all_share_energy_prices.get("failed_gates") or [],
            },
            "v89_sse_regulatory_measures": {
                "ready": (
                    v89_sse_regulatory_measures.get("data_quality_gate_passed")
                    is True
                ),
                "status": v89_sse_regulatory_measures.get("status") or "missing",
                "data_epoch_id": v89_sse_regulatory_measures.get("data_epoch_id"),
                "records": int(
                    v89_sse_regulatory_measures.get("record_count") or 0
                ),
                "direct_single_a_share_records": int(
                    v89_sse_regulatory_measures.get(
                        "direct_single_a_share_records"
                    ) or 0
                ),
                "direct_multiple_a_share_records": int(
                    v89_sse_regulatory_measures.get(
                        "direct_multiple_a_share_records"
                    ) or 0
                ),
                "no_a_share_code_records": int(
                    v89_sse_regulatory_measures.get("no_a_share_code_records")
                    or 0
                ),
                "coverage_start": v89_sse_regulatory_measures.get("coverage_start"),
                "coverage_end": v89_sse_regulatory_measures.get("coverage_end"),
                "price_data_read": False,
                "return_data_read": False,
                "successor_discovery_allowed": (
                    v89_sse_regulatory_measures.get("successor_discovery_allowed")
                    is True
                ),
                "factor_backtest_allowed": False,
                "failed_gates": v89_sse_regulatory_measures.get("failed_gates") or [],
            },
            "v178_sse_disciplinary_event_prices": {
                "ready": (
                    v178_sse_disciplinary_prices.get("price_data_gate_passed")
                    is True
                ),
                "status": v178_sse_disciplinary_prices.get("status") or "missing",
                "event_records": int(
                    (v178_sse_disciplinary_prices.get("quality") or {}).get(
                        "event_records"
                    ) or 0
                ),
                "event_codes": int(
                    (v178_sse_disciplinary_prices.get("quality") or {}).get(
                        "event_codes"
                    ) or 0
                ),
                "stock_files": int(
                    (v178_sse_disciplinary_prices.get("quality") or {}).get(
                        "stock_files"
                    ) or 0
                ),
                "missing_files": len(
                    (v178_sse_disciplinary_prices.get("quality") or {}).get(
                        "missing_files"
                    ) or []
                ),
                "benchmark_rows": int(
                    (v178_sse_disciplinary_prices.get("quality") or {}).get(
                        "benchmark_rows"
                    ) or 0
                ),
                "complete_paths": (
                    (v178_sse_disciplinary_prices.get("quality") or {}).get(
                        "complete_paths"
                    ) or {}
                ),
                "price_data_read": (
                    v178_sse_disciplinary_prices.get("price_data_read") is True
                ),
                "return_data_computed": (
                    v178_sse_disciplinary_prices.get("return_data_computed")
                    is True
                ),
                "historical_event_test_executed": (
                    v178_sse_disciplinary_prices.get(
                        "historical_event_test_executed"
                    ) is True
                ),
                "single_historical_event_test_allowed": False,
                "failed_gates": v178_sse_disciplinary_prices.get("failed_gates") or [],
            },
            "v183_local_order_book_data_epoch": {
                "ready": v183_local_order_book.get("data_gate_passed") is True,
                "status": v183_local_order_book.get("status") or "missing",
                "source_root": v183_local_order_book.get("source_root"),
                "total_parquet_files": int(
                    (v183_local_order_book.get("inventory") or {}).get(
                        "total_parquet_files"
                    ) or 0
                ),
                "metadata_indexed_files": int(
                    (v183_local_order_book.get("inventory") or {}).get(
                        "metadata_indexed_files"
                    ) or 0
                ),
                "daily_files": int(
                    ((((v183_local_order_book.get("inventory") or {}).get(
                        "partitions"
                    ) or {}).get("daily") or {}).get("files") or 0)
                ),
                "minute5_files": int(
                    ((((v183_local_order_book.get("inventory") or {}).get(
                        "partitions"
                    ) or {}).get("minute5") or {}).get("files") or 0)
                ),
                "realtime_rows": int(
                    ((((v183_local_order_book.get("inventory") or {}).get(
                        "partitions"
                    ) or {}).get("realtime") or {}).get("sample_rows") or 0)
                ),
                "realtime_trading_days": int(
                    ((v183_local_order_book.get("inventory") or {}).get(
                        "realtime_time_coverage"
                    ) or {}).get("trading_days") or 0
                ),
                "failed_gates": v183_local_order_book.get("failed_gates") or [],
                "return_data_read": v183_local_order_book.get("return_data_read") is True,
                "return_data_computed": (
                    v183_local_order_book.get("return_data_computed") is True
                ),
                "single_historical_backtest_allowed": False,
            },
            "v184_cninfo_cash_dividend_data_epoch": {
                "ready": v184_cash_dividend_epoch.get("data_gate_passed") is True,
                "status": v184_cash_dividend_epoch.get("status") or "missing",
                "target_codes": int(
                    (v184_cash_dividend_epoch.get("counts") or {}).get("target_codes") or 0
                ),
                "query_ok": int(
                    (v184_cash_dividend_epoch.get("counts") or {}).get("query_ok") or 0
                ),
                "query_empty": int(
                    (v184_cash_dividend_epoch.get("counts") or {}).get("query_empty") or 0
                ),
                "query_error": int(
                    (v184_cash_dividend_epoch.get("counts") or {}).get("query_error") or 0
                ),
                "eligible_events": int(
                    (v184_cash_dividend_epoch.get("counts") or {}).get(
                        "eligible_cash_events_2018_2025"
                    ) or 0
                ),
                "test_events": int(
                    (v184_cash_dividend_epoch.get("counts") or {}).get(
                        "test_events_2024_2025"
                    ) or 0
                ),
                "test_unique_ex_dates": int(
                    (v184_cash_dividend_epoch.get("counts") or {}).get(
                        "test_unique_ex_dates"
                    ) or 0
                ),
                "response_files": int(
                    (v184_cash_dividend_epoch.get("physical_evidence") or {}).get(
                        "response_files"
                    ) or 0
                ),
                "response_hash_mismatches": len(
                    (v184_cash_dividend_epoch.get("physical_evidence") or {}).get(
                        "response_hash_mismatches"
                    ) or []
                ),
                "return_data_read": v184_cash_dividend_epoch.get("return_data_read") is True,
                "single_factor_test_allowed": (
                    v184_cash_dividend_epoch.get("single_factor_test_allowed") is True
                    and v185_pre_ex_dividend_drift.get("return_data_read") is not True
                ),
            },
            "v186_cninfo_open_market_repurchase_data_epoch": {
                "ready": v186_open_market_repurchase_epoch.get("data_gate_passed") is True,
                "status": v186_open_market_repurchase_epoch.get("status") or "missing",
                "target_codes": int(
                    (v186_open_market_repurchase_epoch.get("counts") or {}).get("target_codes") or 0
                ),
                "raw_announcements": int(
                    (v186_open_market_repurchase_epoch.get("counts") or {}).get(
                        "raw_announcements"
                    ) or 0
                ),
                "eligible_events": int(
                    (v186_open_market_repurchase_epoch.get("counts") or {}).get(
                        "eligible_events"
                    ) or 0
                ),
                "complete_events": int(
                    (v186_open_market_repurchase_epoch.get("counts") or {}).get(
                        "complete_events"
                    ) or 0
                ),
                "test_events": int(
                    (v186_open_market_repurchase_epoch.get("counts") or {}).get(
                        "test_events_2024_2025"
                    ) or 0
                ),
                "test_unique_announcement_dates": int(
                    (v186_open_market_repurchase_epoch.get("counts") or {}).get(
                        "test_unique_announcement_dates"
                    ) or 0
                ),
                "archive_files": len(
                    (v186_open_market_repurchase_epoch.get("physical_evidence") or {}).get(
                        "archives"
                    ) or []
                ),
                "archive_hash_mismatches": sum(
                    1
                    for row in (
                        (v186_open_market_repurchase_epoch.get("physical_evidence") or {}).get(
                            "archives"
                        ) or []
                    )
                    if row.get("hash_verified") is not True
                ),
                "failed_gates": v186_open_market_repurchase_epoch.get("failed_gates") or [],
                "return_data_read": (
                    v186_open_market_repurchase_epoch.get("return_data_read") is True
                ),
                "single_factor_test_allowed": (
                    v186_open_market_repurchase_epoch.get("single_factor_test_allowed") is True
                    and v187_open_market_repurchase_drift.get("return_data_read") is not True
                ),
            },
            "v188_cninfo_ipo_lockup_release_data_epoch": {
                "ready": v188_ipo_lockup_release_epoch.get("data_gate_passed") is True,
                "status": v188_ipo_lockup_release_epoch.get("status") or "missing",
                "target_codes": int(
                    (v188_ipo_lockup_release_epoch.get("counts") or {}).get("target_codes") or 0
                ),
                "raw_announcements": int(
                    (v188_ipo_lockup_release_epoch.get("counts") or {}).get(
                        "raw_announcements"
                    ) or 0
                ),
                "eligible_events": int(
                    (v188_ipo_lockup_release_epoch.get("counts") or {}).get(
                        "eligible_events"
                    ) or 0
                ),
                "complete_events": int(
                    (v188_ipo_lockup_release_epoch.get("counts") or {}).get(
                        "complete_events"
                    ) or 0
                ),
                "test_events": int(
                    (v188_ipo_lockup_release_epoch.get("counts") or {}).get(
                        "test_events_2024_2025"
                    ) or 0
                ),
                "test_unique_announcement_dates": int(
                    (v188_ipo_lockup_release_epoch.get("counts") or {}).get(
                        "test_unique_announcement_dates"
                    ) or 0
                ),
                "archive_files": len(
                    (v188_ipo_lockup_release_epoch.get("physical_evidence") or {}).get(
                        "archives"
                    ) or []
                ),
                "archive_hash_mismatches": sum(
                    1
                    for row in (
                        (v188_ipo_lockup_release_epoch.get("physical_evidence") or {}).get(
                            "archives"
                        ) or []
                    )
                    if row.get("hash_verified") is not True
                ),
                "failed_gates": v188_ipo_lockup_release_epoch.get("failed_gates") or [],
                "return_data_read": v188_ipo_lockup_release_epoch.get("return_data_read") is True,
                "single_factor_test_allowed": (
                    v188_ipo_lockup_release_epoch.get("single_factor_test_allowed") is True
                    and v189_ipo_lockup_release_reversal.get("return_data_read") is not True
                ),
            },
            "v190_cninfo_major_contract_data_epoch": {
                "ready": v190_major_contract_epoch.get("data_gate_passed") is True,
                "status": v190_major_contract_epoch.get("status") or "missing",
                "target_codes": int(
                    (v190_major_contract_epoch.get("counts") or {}).get("target_codes") or 0
                ),
                "raw_announcements": int(
                    (v190_major_contract_epoch.get("counts") or {}).get(
                        "raw_announcements"
                    ) or 0
                ),
                "eligible_events": int(
                    (v190_major_contract_epoch.get("counts") or {}).get(
                        "eligible_events"
                    ) or 0
                ),
                "complete_events": int(
                    (v190_major_contract_epoch.get("counts") or {}).get(
                        "complete_events"
                    ) or 0
                ),
                "test_events": int(
                    (v190_major_contract_epoch.get("counts") or {}).get(
                        "test_events_2024_2025"
                    ) or 0
                ),
                "test_unique_announcement_dates": int(
                    (v190_major_contract_epoch.get("counts") or {}).get(
                        "test_unique_announcement_dates"
                    ) or 0
                ),
                "archive_files": len(
                    (v190_major_contract_epoch.get("physical_evidence") or {}).get(
                        "archives"
                    ) or []
                ),
                "archive_hash_mismatches": sum(
                    1
                    for row in (
                        (v190_major_contract_epoch.get("physical_evidence") or {}).get(
                            "archives"
                        ) or []
                    )
                    if row.get("hash_verified") is not True
                ),
                "failed_gates": v190_major_contract_epoch.get("failed_gates") or [],
                "return_data_read": v190_major_contract_epoch.get("return_data_read") is True,
                "single_factor_test_allowed": (
                    v190_major_contract_epoch.get("single_factor_test_allowed") is True
                    and v191_major_contract_5d_drift.get("return_data_read") is not True
                ),
            },
            "margin_history": margin_physical,
            "v90_pmi_epoch": v90_pmi_physical,
            "v120_pbc_epoch": v120_pbc_physical,
            "v143_cpi_ppi_epoch": v143_cpi_ppi_physical,
            "v145_industrial_profit_epoch": v145_industrial_profit_physical,
            "decision_indexes": {
                "ready": indexes_current,
                "common_as_of": decision_indexes.get("common_as_of"),
                "source": decision_indexes.get("source"),
                "files": decision_indexes.get("files") or {},
            },
            "benchmark_indexes": {
                "ready": benchmark_indexes_current,
                "count": len(benchmark_rows),
                "expected_count": 4,
                "indexes": {
                    code: {
                        "latest_available_date": row.get("latest_available_date"),
                        "ready": row.get("source_current_for_requested_end") is True,
                        "output": row.get("output"),
                    }
                    for code, row in benchmark_rows.items()
                    if isinstance(row, dict)
                },
            },
            "decision_calendar": {
                "ready": calendar_current,
                "coverage_start": decision_calendar.get("coverage_start"),
                "coverage_end": decision_calendar.get("coverage_end"),
                "is_trading_day": decision_calendar.get("is_trading_day") is True,
                "source": decision_calendar.get("source"),
            },
        },
        "research_worker": {
            "status": worker.get("status") or "missing",
            "current_step": worker.get("current_step"),
            "heartbeat_at": worker.get("heartbeat_at"),
        },
        "profitability": {
            "strategy": (
                "V219 QDII单位纠错 · 旧候选已撤销"
                if v219_qdii_correction
                else f"{v205_multi_asset_rotation.get('title')}（独立稳健性未通过）"
                if v207_multi_asset_robustness.get("robustness_passed") is False
                else v205_multi_asset_rotation.get("title")
                if v205_multi_asset_rotation.get("historical_candidate_passed") is True
                else gate_progress.get("strategy") if progress_active else "无有效候选"
            ),
            "passed_count": (
                int(gate_progress.get("passed_count") or 0) if progress_active else 0
            ),
            "total_count": (
                int(gate_progress.get("total_count") or 0) if progress_active else 0
            ),
            "all_passed": (
                gate_progress.get("all_passed") is True if progress_active else False
            ),
            "current_trades": (
                gate_progress.get("current_trades") if progress_active else None
            ),
            "current_win_rate_pct": (
                gate_progress.get("current_win_rate_pct") if progress_active else None
            ),
            "current_annualized_pct": (
                gate_progress.get("current_annualized_pct") if progress_active else None
            ),
            "failed_conditions": failed_conditions,
            "v205_multi_asset_rotation": {
                "available": bool(v205_multi_asset_rotation),
                "status": v205_multi_asset_rotation.get("status") or "missing",
                "historical_candidate_passed": (
                    v205_multi_asset_rotation.get("historical_candidate_passed") is True
                    and not v219_qdii_correction
                ),
                "paper_forward_eligible": (
                    not v219_qdii_correction
                    and
                    v205_multi_asset_rotation.get("historical_candidate_passed") is True
                    and v207_multi_asset_robustness.get("paper_forward_eligible") is True
                ),
                "recommendation_eligible": False,
                "automatic_trading": False,
                "cost_ladder_metrics": (
                    v205_multi_asset_rotation.get("cost_ladder_metrics") or {}
                ),
                "historical_candidate_checks": (
                    v205_multi_asset_rotation.get("historical_candidate_checks")
                    or v205_multi_asset_rotation.get("checks") or {}
                ),
                "current_paper_allocation": (
                    v205_multi_asset_rotation.get("current_research_observation")
                    or v205_multi_asset_rotation.get("current_paper_allocation") or {}
                ),
                "paid_api_used": (
                    v205_multi_asset_rotation.get("paid_api_used") is True
                ),
                "external_model_used": (
                    v205_multi_asset_rotation.get("external_model_used") is True
                ),
                "revoked_by_v219": bool(v219_qdii_correction),
            },
            "v207_multi_asset_robustness": {
                "available": bool(v207_multi_asset_robustness),
                "status": v207_multi_asset_robustness.get("status") or "missing",
                "robustness_passed": (
                    v207_multi_asset_robustness.get("robustness_passed") is True
                    and not v219_qdii_correction
                ),
                "paper_forward_eligible": (
                    v207_multi_asset_robustness.get("paper_forward_eligible") is True
                    and not v219_qdii_correction
                ),
                "recommendation_eligible": False,
                "automatic_trading": False,
                "checks": v207_multi_asset_robustness.get("checks") or {},
                "independent_reproduction": (
                    v207_multi_asset_robustness.get("independent_reproduction") or {}
                ),
                "leave_one_asset_out": (
                    v207_multi_asset_robustness.get("leave_one_asset_out") or {}
                ),
                "parameter_neighborhood": (
                    v207_multi_asset_robustness.get("parameter_neighborhood") or {}
                ),
                "cost_and_execution_perturbation": (
                    v207_multi_asset_robustness.get("cost_and_execution_perturbation")
                    or v207_multi_asset_robustness.get("cost_and_signal_delay") or {}
                ),
                "return_concentration": (
                    v207_multi_asset_robustness.get("return_concentration") or {}
                ),
                "paid_api_used": False,
                "external_model_used": False,
                "revoked_by_v219": bool(v219_qdii_correction),
            },
            "v213_paper_forward": {
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
                "paid_api_used": False,
                "external_model_used": False,
                "revoked_by_v219": bool(v219_qdii_correction),
            },
            "v219_qdii_unit_correction": {
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
                "paid_api_used": False,
                "external_model_used": False,
                "conclusion": v219_qdii_correction.get("conclusion"),
            },
            "v217_cash_sweep": {
                "available": bool(v217_cash_sweep),
                "strategy_id": v217_cash_sweep.get("strategy_id"),
                "status": v217_cash_sweep.get("status") or "missing",
                "historical_candidate_passed": bool(
                    v217_cash_sweep.get("historical_candidate_passed")
                ),
                "checks": v217_cash_sweep.get("checks") or {},
                "cost_ladder_metrics": (
                    v217_cash_sweep.get("cost_ladder_metrics") or {}
                ),
                "current_research_observation": (
                    v217_cash_sweep.get("current_research_observation") or {}
                ),
                "recommendation_eligible": False,
                "automatic_trading": False,
            },
            "archived_shadow": {
                "strategy": gate_progress.get("strategy"),
                "status": "active" if progress_active else "revoked_not_unlockable",
                "trades": gate_progress.get("current_trades"),
                "win_rate_pct": gate_progress.get("current_win_rate_pct"),
                "annualized_pct": gate_progress.get("current_annualized_pct"),
            },
            "strict_walkforward": {
                "cache_key": production.get("cache_key"),
                "strict_pit": production.get("strict_pit") is True,
                "total_return_pct": strict_total_return,
                "annualized_pct": strict_annualized,
                "sharpe": strict_metrics.get("sharpe"),
                "max_drawdown_pct": strict_metrics.get("max_dd_pct"),
                "dsr": strict_dsr,
                "years_positive": selected_method.get("years_positive"),
                "years_total": selected_method.get("years_total"),
                "passed": strict_passed,
            },
            "strict_pit_50_run": {
                "historical_robustness_gate_passed": (
                    taskbook_v3.get("historical_robustness_gate_passed") is True
                ),
                "positive_runs": v3_base.get("positive_runs"),
                "total_runs": v3_base.get("runs"),
                "median_return_pct": v3_base.get("median_return_pct"),
                "double_cost_median_return_pct": v3_stress.get("median_return_pct"),
                "median_buy_turnover_pct": v3_base.get("median_buy_turnover_pct"),
                "bootstrap_ci_95": v3_bootstrap.get("mean_return_pct_ci_95"),
                "failed_gates": taskbook_v3.get("failed_gates") or [],
            },
            "v9_margin_factor": {
                "available": bool(v9_margin_factor),
                "statistical_gate_passed": (
                    v9_margin_factor.get("statistical_gate_passed") is True
                ),
                "test_mean_ic": v9_test.get("mean_ic"),
                "test_monotonicity": v9_test.get("decile_monotonicity_spearman"),
                "top_minus_middle_after_cost_pct": v9_test.get(
                    "top_minus_middle_after_cost_pct"
                ),
                "failed_gates": v9_result.get("failed_gates") or [],
            },
            "v14_margin_factor": {
                "available": bool(v14_margin_factor),
                "status": v14_margin_factor.get("status") or "missing",
                "statistical_gate_passed": (
                    v14_margin_factor.get("statistical_gate_passed") is True
                ),
                "test_mean_ic": v14_test.get("mean_ic"),
                "test_monotonicity": v14_test.get("decile_monotonicity_spearman"),
                "top_minus_middle_after_cost_pct": v14_test.get(
                    "top_minus_middle_after_cost_pct"
                ),
                "failed_gates": v14_result.get("failed_gates") or [],
                "strategy_backtest_allowed": (
                    v14_margin_factor.get("strategy_backtest_allowed") is True
                ),
            },
            "v14_margin_strategy": {
                "available": bool(v14_margin_strategy),
                "historical_strategy_gate_passed": (
                    v14_margin_strategy.get("historical_strategy_gate_passed") is True
                ),
                "failed_gates": v14_margin_strategy.get("failed_gates") or [],
            },
            "v90_pmi_regime": {
                "available": bool(v90_pmi_regime),
                "status": v90_pmi_regime.get("status") or "missing",
                "data_epoch_ready": v90_pmi_regime.get("data_epoch_ready") is True,
                "historical_strategy_gate_passed": (
                    v90_pmi_regime.get("historical_strategy_gate_passed") is True
                ),
                "signal_months": v90_pmi_regime.get("signal_months"),
                "risk_on_months": v90_pmi_regime.get("risk_on_months"),
                "validation_annualized_return_pct": v90_validation_10.get("annualized_return_pct"),
                "validation_max_drawdown_pct": v90_validation_10.get("max_drawdown_pct"),
                "test_annualized_return_pct": v90_test_10.get("annualized_return_pct"),
                "test_max_drawdown_pct": v90_test_10.get("max_drawdown_pct"),
                "test_profit_factor": v90_test_10.get("profit_factor"),
                "failed_gates": v90_pmi_regime.get("failed_gates") or [],
                "recommendation_eligible": (
                    v90_pmi_regime.get("recommendation_eligible") is True
                ),
            },
            "v104_prebacktest_admission": {
                "available": bool(v104_prebacktest),
                "status": v104_prebacktest.get("status") or "missing",
                "candidate_id": v104_prebacktest.get("candidate_id"),
                "sample_months": v104_prebacktest.get("sample_months"),
                "trigger_months": v104_prebacktest.get("trigger_months"),
                "maximum_observed_spread": v104_prebacktest.get(
                    "maximum_observed_spread"
                ),
                "return_data_read": v104_prebacktest.get("return_data_read") is True,
                "preregistration_admission_passed": (
                    v104_prebacktest.get("preregistration_admission_passed") is True
                ),
                "successor_backtest_allowed": (
                    v104_prebacktest.get("successor_backtest_allowed") is True
                ),
                "failed_gates": v104_prebacktest.get("failed_gates") or [],
                "reasons": v104_prebacktest.get("reasons") or [],
            },
            "v121_m2_acceleration": {
                "available": bool(v121_m2_acceleration),
                "status": v121_m2_acceleration.get("status") or "missing",
                "historical_strategy_gate_passed": (
                    v121_m2_acceleration.get("historical_strategy_gate_passed")
                    is True
                ),
                "single_backtest_consumed": (
                    v121_m2_acceleration.get("single_backtest_consumed") is True
                ),
                "test_10bp_total_return_pct": v121_test_10.get("total_return_pct"),
                "test_10bp_annualized_return_pct": v121_test_10.get(
                    "annualized_return_pct"
                ),
                "test_10bp_win_rate_pct": v121_test_10.get("win_rate_pct"),
                "test_10bp_max_drawdown_pct": v121_test_10.get(
                    "max_drawdown_pct"
                ),
                "test_10bp_sharpe": v121_test_10.get("sharpe"),
                "test_10bp_profit_factor": v121_test_10.get("profit_factor"),
                "test_20bp_total_return_pct": v121_test_20.get(
                    "total_return_pct"
                ),
                "failed_gates": v121_m2_acceleration.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v142_prebacktest_admission": {
                "available": bool(v142_interbank),
                "status": v142_interbank.get("status") or "missing",
                "sample_months": v142_interbank.get("sample_months"),
                "trigger_months": v142_interbank.get("trigger_months"),
                "maximum_observed_rate_pct": v142_interbank.get(
                    "maximum_observed_rate_pct"
                ),
                "return_data_read": v142_interbank.get("return_data_read") is True,
                "preregistration_admission_passed": (
                    v142_interbank.get("preregistration_admission_passed") is True
                ),
                "single_historical_backtest_allowed": (
                    v142_interbank.get("single_historical_backtest_allowed") is True
                ),
                "failed_gates": v142_interbank.get("failed_gates") or [],
                "reasons": v142_interbank.get("reasons") or [],
            },
            "v144_prebacktest_admission": {
                "available": bool(v144_ppi_cpi_gap),
                "status": v144_ppi_cpi_gap.get("status") or "missing",
                "sample_months": v144_ppi_cpi_gap.get("sample_months"),
                "trigger_months": v144_ppi_cpi_gap.get("trigger_months"),
                "train_trigger_months": v144_ppi_cpi_gap.get(
                    "train_trigger_months"
                ),
                "test_trigger_months": v144_ppi_cpi_gap.get(
                    "test_trigger_months"
                ),
                "positive_regimes": v144_ppi_cpi_gap.get("positive_regimes"),
                "state_transitions": v144_ppi_cpi_gap.get("state_transitions"),
                "return_data_read": v144_ppi_cpi_gap.get("return_data_read") is True,
                "preregistration_admission_passed": (
                    v144_ppi_cpi_gap.get("preregistration_admission_passed") is True
                ),
                "single_historical_backtest_allowed": (
                    v144_ppi_cpi_gap.get("single_historical_backtest_allowed") is True
                ),
                "failed_gates": v144_ppi_cpi_gap.get("failed_gates") or [],
                "reasons": v144_ppi_cpi_gap.get("reasons") or [],
            },
            "v146_prebacktest_admission": {
                "available": bool(v146_industrial_profit),
                "status": v146_industrial_profit.get("status") or "missing",
                "sample_observations": v146_industrial_profit.get(
                    "sample_observations"
                ),
                "positive_observations": v146_industrial_profit.get(
                    "positive_observations"
                ),
                "negative_observations": v146_industrial_profit.get(
                    "negative_observations"
                ),
                "positive_regimes": v146_industrial_profit.get("positive_regimes"),
                "negative_regimes": v146_industrial_profit.get("negative_regimes"),
                "train_negative_regimes": (
                    (v146_industrial_profit.get("halves") or {}).get("train") or {}
                ).get("negative_regimes"),
                "return_data_read": (
                    v146_industrial_profit.get("return_data_read") is True
                ),
                "preregistration_admission_passed": (
                    v146_industrial_profit.get("preregistration_admission_passed")
                    is True
                ),
                "single_historical_backtest_allowed": (
                    v146_industrial_profit.get("single_historical_backtest_allowed")
                    is True
                ),
                "failed_gates": v146_industrial_profit.get("failed_gates") or [],
                "reasons": v146_industrial_profit.get("reasons") or [],
            },
            "v167_industry_profit_breadth_admission": {
                "available": bool(v167_industry_profit_admission),
                "status": v167_industry_profit_admission.get("status") or "missing",
                "candidate_id": v167_industry_profit_admission.get("candidate_id"),
                "monthly_observations": v167_industry_profit_admission.get(
                    "monthly_observations"
                ),
                "observed_risk_on_regimes": (
                    (v167_industry_profit_admission.get("halves") or {}).get(
                        "observed_test"
                    ) or {}
                ).get("risk_on_regimes"),
                "preregistration_admission_passed": (
                    v167_industry_profit_admission.get(
                        "preregistration_admission_passed"
                    ) is True
                ),
                "return_data_read": (
                    v167_industry_profit_admission.get("return_data_read") is True
                ),
            },
            "v169_v167_integrity_stop": {
                "available": bool(v169_industry_profit_integrity),
                "status": v169_industry_profit_integrity.get("status") or "missing",
                "maximum_possible_completed_trades": (
                    v169_industry_profit_integrity.get(
                        "maximum_possible_completed_trades"
                    )
                ),
                "required_completed_trades": v169_industry_profit_integrity.get(
                    "required_completed_trades"
                ),
                "historical_backtest_executed": (
                    v169_industry_profit_integrity.get("historical_backtest_executed")
                    is True
                ),
                "return_data_computed": (
                    v169_industry_profit_integrity.get("return_data_computed") is True
                ),
                "recommendation_eligible": False,
            },
            "v170_industry_profit_acceleration_admission": {
                "available": bool(v170_industry_profit_acceleration_admission),
                "status": (
                    v170_industry_profit_acceleration_admission.get("status")
                    or "missing"
                ),
                "candidate_id": v170_industry_profit_acceleration_admission.get(
                    "candidate_id"
                ),
                "monthly_delta_observations": (
                    v170_industry_profit_acceleration_admission.get(
                        "monthly_delta_observations"
                    )
                ),
                "train_completed_trades_possible": (
                    (
                        v170_industry_profit_acceleration_admission.get("halves")
                        or {}
                    ).get("train") or {}
                ).get("completed_risk_on_regimes_within_split"),
                "observed_completed_trades_possible": (
                    (
                        v170_industry_profit_acceleration_admission.get("halves")
                        or {}
                    ).get("observed_test") or {}
                ).get("completed_risk_on_regimes_within_split"),
                "preregistration_admission_passed": (
                    v170_industry_profit_acceleration_admission.get(
                        "preregistration_admission_passed"
                    ) is True
                ),
                "return_data_read_at_admission": (
                    v170_industry_profit_acceleration_admission.get(
                        "return_data_read"
                    ) is True
                ),
                "failed_gates": (
                    v170_industry_profit_acceleration_admission.get("failed_gates")
                    or []
                ),
            },
            "v172_v170_historical_strategy_test": {
                "available": bool(v172_industry_profit_acceleration),
                "status": v172_industry_profit_acceleration.get("status") or "missing",
                "return_data_read": (
                    v172_industry_profit_acceleration.get("return_data_read") is True
                ),
                "single_historical_backtest_consumed": (
                    v172_industry_profit_acceleration.get(
                        "single_historical_backtest_consumed"
                    ) is True
                ),
                "historical_strategy_gate_passed": (
                    v172_industry_profit_acceleration.get(
                        "historical_strategy_gate_passed"
                    ) is True
                ),
                "train_10bp": (
                    {
                        key: value for key, value in (
                            (((v172_industry_profit_acceleration.get("results") or {}).get("train") or {})
                            .get("cost_10bp") or {})
                        ).items() if key != "trades"
                    }
                ),
                "observed_10bp": (
                    {
                        key: value for key, value in (
                            (((v172_industry_profit_acceleration.get("results") or {}).get("observed_test") or {})
                            .get("cost_10bp") or {})
                        ).items() if key != "trades"
                    }
                ),
                "observed_20bp": (
                    {
                        key: value for key, value in (
                            (((v172_industry_profit_acceleration.get("results") or {}).get("observed_test") or {})
                            .get("cost_20bp") or {})
                        ).items() if key != "trades"
                    }
                ),
                "failed_gates": (
                    v172_industry_profit_acceleration.get("failed_gates") or []
                ),
                "recommendation_eligible": False,
            },
            "v174_pmi_gap_acceleration_admission": {
                "available": bool(v174_pmi_gap_acceleration_admission),
                "status": (
                    v174_pmi_gap_acceleration_admission.get("status") or "missing"
                ),
                "candidate_id": v174_pmi_gap_acceleration_admission.get(
                    "candidate_id"
                ),
                "asset": v174_pmi_gap_acceleration_admission.get("asset"),
                "monthly_delta_observations": (
                    v174_pmi_gap_acceleration_admission.get(
                        "monthly_delta_observations"
                    )
                ),
                "train_completed_trades_possible": (
                    (
                        v174_pmi_gap_acceleration_admission.get("halves") or {}
                    ).get("train") or {}
                ).get("completed_risk_on_regimes_within_split"),
                "observed_completed_trades_possible": (
                    (
                        v174_pmi_gap_acceleration_admission.get("halves") or {}
                    ).get("observed_test") or {}
                ).get("completed_risk_on_regimes_within_split"),
                "preregistration_admission_passed": (
                    v174_pmi_gap_acceleration_admission.get(
                        "preregistration_admission_passed"
                    ) is True
                ),
                "return_data_read_at_admission": (
                    v174_pmi_gap_acceleration_admission.get("return_data_read")
                    is True
                ),
                "single_historical_backtest_allowed": (
                    v175_all_share_energy_prices.get(
                        "single_historical_backtest_allowed"
                    ) is True
                    and v176_pmi_gap_acceleration.get(
                        "single_historical_backtest_consumed"
                    ) is not True
                ),
                "single_historical_backtest_consumed": (
                    v176_pmi_gap_acceleration.get(
                        "single_historical_backtest_consumed"
                    ) is True
                ),
                "failed_gates": (
                    v174_pmi_gap_acceleration_admission.get("failed_gates") or []
                ),
            },
            "v176_v174_historical_strategy_test": {
                "available": bool(v176_pmi_gap_acceleration),
                "status": v176_pmi_gap_acceleration.get("status") or "missing",
                "candidate_id": v176_pmi_gap_acceleration.get("candidate_id"),
                "return_data_read": (
                    v176_pmi_gap_acceleration.get("return_data_read") is True
                ),
                "single_historical_backtest_consumed": (
                    v176_pmi_gap_acceleration.get(
                        "single_historical_backtest_consumed"
                    ) is True
                ),
                "historical_strategy_gate_passed": (
                    v176_pmi_gap_acceleration.get(
                        "historical_strategy_gate_passed"
                    ) is True
                ),
                "train_10bp": {
                    key: value
                    for key, value in (
                        (
                            (
                                (v176_pmi_gap_acceleration.get("results") or {})
                                .get("train") or {}
                            ).get("cost_10bp") or {}
                        ).items()
                    )
                    if key != "trades"
                },
                "observed_10bp": {
                    key: value
                    for key, value in (
                        (
                            (
                                (v176_pmi_gap_acceleration.get("results") or {})
                                .get("observed_test") or {}
                            ).get("cost_10bp") or {}
                        ).items()
                    )
                    if key != "trades"
                },
                "observed_20bp": {
                    key: value
                    for key, value in (
                        (
                            (
                                (v176_pmi_gap_acceleration.get("results") or {})
                                .get("observed_test") or {}
                            ).get("cost_20bp") or {}
                        ).items()
                    )
                    if key != "trades"
                },
                "observed_stability": (
                    v176_pmi_gap_acceleration.get("observed_stability") or {}
                ),
                "failed_gates": v176_pmi_gap_acceleration.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v177_sse_disciplinary_holding_risk_admission": {
                "available": bool(v177_sse_disciplinary_admission),
                "status": v177_sse_disciplinary_admission.get("status") or "missing",
                "candidate_id": v177_sse_disciplinary_admission.get("candidate_id"),
                "event_records": v177_sse_disciplinary_admission.get("event_records"),
                "event_codes": v177_sse_disciplinary_admission.get("event_codes"),
                "halves": v177_sse_disciplinary_admission.get("halves") or {},
                "preregistration_admission_passed": (
                    v177_sse_disciplinary_admission.get(
                        "preregistration_admission_passed"
                    ) is True
                ),
                "return_data_read_at_admission": (
                    v177_sse_disciplinary_admission.get("return_data_read") is True
                ),
                "price_data_epoch_consumed": (
                    v178_sse_disciplinary_prices.get("price_data_read") is True
                ),
                "price_data_gate_passed": (
                    v178_sse_disciplinary_prices.get("price_data_gate_passed")
                    is True
                ),
                "historical_event_test_executed": (
                    v178_sse_disciplinary_prices.get(
                        "historical_event_test_executed"
                    ) is True
                ),
                "single_historical_event_test_allowed": False,
                "failed_price_gates": (
                    v178_sse_disciplinary_prices.get("failed_gates") or []
                ),
                "recommendation_eligible": False,
            },
            "v179_same_calendar_month_seasonality_admission": {
                "available": bool(v179_seasonality_admission),
                "status": v179_seasonality_admission.get("status") or "missing",
                "candidate_id": v179_seasonality_admission.get("candidate_id"),
                "target_months": v179_seasonality_admission.get("target_months") or {},
                "preregistration_admission_passed": (
                    v179_seasonality_admission.get("preregistration_admission_passed") is True
                ),
                "candidate_return_data_read_at_admission": (
                    v179_seasonality_admission.get("candidate_return_data_read") is True
                ),
                "single_factor_test_allowed": (
                    v179_seasonality_admission.get("single_factor_test_allowed") is True
                    and v180_seasonality_factor.get("return_data_read") is not True
                ),
                "single_factor_test_consumed": (
                    v180_seasonality_factor.get("single_factor_test_consumed") is True
                ),
                "recommendation_eligible": False,
            },
            "v180_v179_same_calendar_month_seasonality_factor_test": {
                "available": bool(v180_seasonality_factor),
                "status": v180_seasonality_factor.get("status") or "missing",
                "candidate_id": v180_seasonality_factor.get("candidate_id"),
                "target_months_computed": v180_seasonality_factor.get("target_months_computed"),
                "five_year_history_codes": v180_seasonality_factor.get("five_year_history_codes"),
                "v4_st_files_hash_verified": v180_seasonality_factor.get("v4_st_files_hash_verified"),
                "return_data_read": v180_seasonality_factor.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v180_seasonality_factor.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v180_seasonality_factor.get("historical_factor_gate_passed") is True
                ),
                "successor_long_only_preregistration_allowed": (
                    v180_seasonality_factor.get("successor_long_only_preregistration_allowed") is True
                ),
                "splits": v180_seasonality_factor.get("splits") or {},
                "recommendation_eligible": False,
            },
            "v181_annual_filing_timeliness_admission": {
                "available": bool(v181_filing_timeliness_admission),
                "status": v181_filing_timeliness_admission.get("status") or "missing",
                "candidate_id": v181_filing_timeliness_admission.get("candidate_id"),
                "target_years": v181_filing_timeliness_admission.get("target_years") or [],
                "annual_filing_files": v181_filing_timeliness_admission.get("annual_filing_files") or [],
                "holding_sessions": v181_filing_timeliness_admission.get("holding_sessions"),
                "preregistration_admission_passed": (
                    v181_filing_timeliness_admission.get("preregistration_admission_passed") is True
                ),
                "candidate_return_data_read_at_admission": (
                    v181_filing_timeliness_admission.get("candidate_return_data_read") is True
                ),
                "single_factor_test_allowed": (
                    v181_filing_timeliness_admission.get("single_factor_test_allowed") is True
                    and v182_filing_timeliness_factor.get("return_data_read") is not True
                ),
                "single_factor_test_consumed": (
                    v182_filing_timeliness_factor.get("single_factor_test_consumed") is True
                ),
                "recommendation_eligible": False,
            },
            "v182_v181_annual_filing_timeliness_factor_test": {
                "available": bool(v182_filing_timeliness_factor),
                "status": v182_filing_timeliness_factor.get("status") or "missing",
                "candidate_id": v182_filing_timeliness_factor.get("candidate_id"),
                "target_years_computed": v182_filing_timeliness_factor.get("target_years_computed"),
                "filing_history_price_intersection_codes": (
                    v182_filing_timeliness_factor.get("filing_history_price_intersection_codes")
                ),
                "v4_st_files_hash_verified": v182_filing_timeliness_factor.get("v4_st_files_hash_verified"),
                "return_data_read": v182_filing_timeliness_factor.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v182_filing_timeliness_factor.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v182_filing_timeliness_factor.get("historical_factor_gate_passed") is True
                ),
                "successor_long_only_preregistration_allowed": (
                    v182_filing_timeliness_factor.get("successor_long_only_preregistration_allowed") is True
                ),
                "splits": v182_filing_timeliness_factor.get("splits") or {},
                "recommendation_eligible": False,
            },
            "v185_cninfo_pre_ex_dividend_drift_factor_test": {
                "available": bool(v185_pre_ex_dividend_drift),
                "status": v185_pre_ex_dividend_drift.get("status") or "missing",
                "candidate_id": v185_pre_ex_dividend_drift.get("candidate_id"),
                "scheduled_events": (
                    (v185_pre_ex_dividend_drift.get("execution") or {}).get(
                        "scheduled_events"
                    )
                ),
                "evaluated_events": (
                    (v185_pre_ex_dividend_drift.get("execution") or {}).get(
                        "evaluated_events"
                    )
                ),
                "return_data_read": v185_pre_ex_dividend_drift.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v185_pre_ex_dividend_drift.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v185_pre_ex_dividend_drift.get("historical_factor_gate_passed") is True
                ),
                "results": v185_pre_ex_dividend_drift.get("results") or {},
                "failed_gates": v185_pre_ex_dividend_drift.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v187_cninfo_open_market_repurchase_drift_factor_test": {
                "available": bool(v187_open_market_repurchase_drift),
                "status": v187_open_market_repurchase_drift.get("status") or "missing",
                "candidate_id": v187_open_market_repurchase_drift.get("candidate_id"),
                "scheduled_events": (
                    (v187_open_market_repurchase_drift.get("execution") or {}).get(
                        "scheduled_events"
                    )
                ),
                "evaluated_events": (
                    (v187_open_market_repurchase_drift.get("execution") or {}).get(
                        "evaluated_events"
                    )
                ),
                "return_data_read": (
                    v187_open_market_repurchase_drift.get("return_data_read") is True
                ),
                "single_factor_test_consumed": (
                    v187_open_market_repurchase_drift.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v187_open_market_repurchase_drift.get("historical_factor_gate_passed") is True
                ),
                "results": v187_open_market_repurchase_drift.get("results") or {},
                "test_time_folds_mean_net_return_20bp_pct": (
                    v187_open_market_repurchase_drift.get(
                        "test_time_folds_mean_net_return_20bp_pct"
                    ) or []
                ),
                "test_all_three_time_folds_positive": (
                    v187_open_market_repurchase_drift.get(
                        "test_all_three_time_folds_positive"
                    ) is True
                ),
                "failed_gates": v187_open_market_repurchase_drift.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v189_cninfo_ipo_lockup_release_reversal_factor_test": {
                "available": bool(v189_ipo_lockup_release_reversal),
                "status": v189_ipo_lockup_release_reversal.get("status") or "missing",
                "candidate_id": v189_ipo_lockup_release_reversal.get("candidate_id"),
                "scheduled_events": (
                    (v189_ipo_lockup_release_reversal.get("execution") or {}).get(
                        "scheduled_events"
                    )
                ),
                "evaluated_events": (
                    (v189_ipo_lockup_release_reversal.get("execution") or {}).get(
                        "evaluated_events"
                    )
                ),
                "return_data_read": v189_ipo_lockup_release_reversal.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v189_ipo_lockup_release_reversal.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v189_ipo_lockup_release_reversal.get("historical_factor_gate_passed") is True
                ),
                "results": v189_ipo_lockup_release_reversal.get("results") or {},
                "test_time_folds_mean_net_return_20bp_pct": (
                    v189_ipo_lockup_release_reversal.get(
                        "test_time_folds_mean_net_return_20bp_pct"
                    ) or []
                ),
                "test_all_three_time_folds_positive": (
                    v189_ipo_lockup_release_reversal.get(
                        "test_all_three_time_folds_positive"
                    ) is True
                ),
                "failed_gates": v189_ipo_lockup_release_reversal.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v191_cninfo_major_contract_5d_drift_factor_test": {
                "available": bool(v191_major_contract_5d_drift),
                "status": v191_major_contract_5d_drift.get("status") or "missing",
                "candidate_id": v191_major_contract_5d_drift.get("candidate_id"),
                "scheduled_events": (
                    (v191_major_contract_5d_drift.get("execution") or {}).get(
                        "scheduled_events"
                    )
                ),
                "evaluated_events": (
                    (v191_major_contract_5d_drift.get("execution") or {}).get(
                        "evaluated_events"
                    )
                ),
                "return_data_read": v191_major_contract_5d_drift.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v191_major_contract_5d_drift.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v191_major_contract_5d_drift.get("historical_factor_gate_passed") is True
                ),
                "results": v191_major_contract_5d_drift.get("results") or {},
                "test_time_folds_mean_net_return_20bp_pct": (
                    v191_major_contract_5d_drift.get(
                        "test_time_folds_mean_net_return_20bp_pct"
                    ) or []
                ),
                "test_all_three_time_folds_positive": (
                    v191_major_contract_5d_drift.get(
                        "test_all_three_time_folds_positive"
                    ) is True
                ),
                "failed_gates": v191_major_contract_5d_drift.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v192_v4_52_week_high_prebacktest_admission": {
                "available": bool(v192_52_week_high_admission),
                "status": v192_52_week_high_admission.get("status") or "missing",
                "candidate_id": v192_52_week_high_admission.get("candidate_id"),
                "v4_codes": (
                    (v192_52_week_high_admission.get("data") or {}).get("v4_codes")
                ),
                "return_data_read": v192_52_week_high_admission.get("return_data_read") is True,
                "preregistration_admission_passed": (
                    v192_52_week_high_admission.get("preregistration_admission_passed") is True
                ),
                "single_factor_test_allowed": (
                    v192_52_week_high_admission.get("single_factor_test_allowed") is True
                    and v193_52_week_high_factor.get("return_data_read") is not True
                ),
                "single_factor_test_consumed": (
                    v193_52_week_high_factor.get("single_factor_test_consumed") is True
                ),
                "recommendation_eligible": False,
            },
            "v193_v4_52_week_high_monthly_factor_test": {
                "available": bool(v193_52_week_high_factor),
                "status": v193_52_week_high_factor.get("status") or "missing",
                "candidate_id": v193_52_week_high_factor.get("candidate_id"),
                "periods": (
                    (v193_52_week_high_factor.get("execution") or {}).get("periods")
                ),
                "candidate_stock_months": (
                    (v193_52_week_high_factor.get("execution") or {}).get(
                        "candidate_stock_months"
                    )
                ),
                "selected_stock_months": (
                    (v193_52_week_high_factor.get("execution") or {}).get(
                        "selected_stock_months"
                    )
                ),
                "verified_daily_files": v193_52_week_high_factor.get("verified_daily_files"),
                "return_data_read": v193_52_week_high_factor.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v193_52_week_high_factor.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v193_52_week_high_factor.get("historical_factor_gate_passed") is True
                ),
                "results": v193_52_week_high_factor.get("results") or {},
                "test_folds": v193_52_week_high_factor.get("test_folds") or [],
                "test_all_three_folds_total_and_excess_positive": (
                    v193_52_week_high_factor.get(
                        "test_all_three_folds_total_and_excess_positive"
                    ) is True
                ),
                "failed_gates": v193_52_week_high_factor.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v194_v4_low_volatility_prebacktest_admission": {
                "available": bool(v194_low_volatility_admission),
                "status": v194_low_volatility_admission.get("status") or "missing",
                "candidate_id": v194_low_volatility_admission.get("candidate_id"),
                "v4_codes": (
                    (v194_low_volatility_admission.get("data") or {}).get("v4_codes")
                ),
                "return_data_read": v194_low_volatility_admission.get("return_data_read") is True,
                "preregistration_admission_passed": (
                    v194_low_volatility_admission.get("preregistration_admission_passed") is True
                ),
                "single_factor_test_allowed": (
                    v194_low_volatility_admission.get("single_factor_test_allowed") is True
                    and v195_low_volatility_factor.get("return_data_read") is not True
                ),
                "single_factor_test_consumed": (
                    v195_low_volatility_factor.get("single_factor_test_consumed") is True
                ),
                "recommendation_eligible": False,
            },
            "v195_v4_low_volatility_monthly_factor_test": {
                "available": bool(v195_low_volatility_factor),
                "status": v195_low_volatility_factor.get("status") or "missing",
                "candidate_id": v195_low_volatility_factor.get("candidate_id"),
                "periods": (
                    (v195_low_volatility_factor.get("execution") or {}).get("periods")
                ),
                "candidate_stock_months": (
                    (v195_low_volatility_factor.get("execution") or {}).get(
                        "candidate_stock_months"
                    )
                ),
                "selected_stock_months": (
                    (v195_low_volatility_factor.get("execution") or {}).get(
                        "selected_stock_months"
                    )
                ),
                "verified_daily_files": v195_low_volatility_factor.get("verified_daily_files"),
                "return_data_read": v195_low_volatility_factor.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v195_low_volatility_factor.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v195_low_volatility_factor.get("historical_factor_gate_passed") is True
                ),
                "results": v195_low_volatility_factor.get("results") or {},
                "test_folds": v195_low_volatility_factor.get("test_folds") or [],
                "test_all_three_folds_total_and_excess_positive": (
                    v195_low_volatility_factor.get(
                        "test_all_three_folds_total_and_excess_positive"
                    ) is True
                ),
                "failed_gates": v195_low_volatility_factor.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v196_v4_max_effect_prebacktest_admission": {
                "available": bool(v196_max_effect_admission),
                "status": v196_max_effect_admission.get("status") or "missing",
                "candidate_id": v196_max_effect_admission.get("candidate_id"),
                "v4_codes": (
                    (v196_max_effect_admission.get("data") or {}).get("v4_codes")
                ),
                "return_data_read": v196_max_effect_admission.get("return_data_read") is True,
                "preregistration_admission_passed": (
                    v196_max_effect_admission.get("preregistration_admission_passed") is True
                ),
                "single_factor_test_allowed": (
                    v196_max_effect_admission.get("single_factor_test_allowed") is True
                    and v197_low_max_factor.get("return_data_read") is not True
                ),
                "single_factor_test_consumed": (
                    v197_low_max_factor.get("single_factor_test_consumed") is True
                ),
                "recommendation_eligible": False,
            },
            "v197_v4_low_max_monthly_factor_test": {
                "available": bool(v197_low_max_factor),
                "status": v197_low_max_factor.get("status") or "missing",
                "candidate_id": v197_low_max_factor.get("candidate_id"),
                "periods": (
                    (v197_low_max_factor.get("execution") or {}).get("periods")
                ),
                "candidate_stock_months": (
                    (v197_low_max_factor.get("execution") or {}).get(
                        "candidate_stock_months"
                    )
                ),
                "selected_stock_months": (
                    (v197_low_max_factor.get("execution") or {}).get(
                        "selected_stock_months"
                    )
                ),
                "verified_daily_files": v197_low_max_factor.get("verified_daily_files"),
                "return_data_read": v197_low_max_factor.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v197_low_max_factor.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v197_low_max_factor.get("historical_factor_gate_passed") is True
                ),
                "results": v197_low_max_factor.get("results") or {},
                "test_folds": v197_low_max_factor.get("test_folds") or [],
                "test_all_three_folds_total_and_excess_positive": (
                    v197_low_max_factor.get(
                        "test_all_three_folds_total_and_excess_positive"
                    ) is True
                ),
                "failed_gates": v197_low_max_factor.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v200_v4_low_market_beta_prebacktest_admission": {
                "available": bool(v200_low_market_beta_admission),
                "status": v200_low_market_beta_admission.get("status") or "missing",
                "candidate_id": v200_low_market_beta_admission.get("candidate_id"),
                "v4_codes": (
                    (v200_low_market_beta_admission.get("data") or {}).get("v4_codes")
                ),
                "future_return_data_read": (
                    v200_low_market_beta_admission.get("future_return_data_read") is True
                ),
                "preregistration_admission_passed": (
                    v200_low_market_beta_admission.get("preregistration_admission_passed") is True
                ),
                "single_factor_test_allowed": (
                    v200_low_market_beta_admission.get("single_factor_test_allowed") is True
                    and v201_low_market_beta_factor.get("single_factor_test_consumed") is not True
                ),
                "single_factor_test_consumed": (
                    v201_low_market_beta_factor.get("single_factor_test_consumed") is True
                ),
                "recommendation_eligible": False,
            },
            "v201_v4_low_market_beta_factor_test": {
                "available": bool(v201_low_market_beta_factor),
                "status": v201_low_market_beta_factor.get("status") or "missing",
                "candidate_id": v201_low_market_beta_factor.get("candidate_id"),
                "execution": v201_low_market_beta_factor.get("execution") or {},
                "future_return_data_read": (
                    v201_low_market_beta_factor.get("future_return_data_read") is True
                ),
                "single_factor_test_consumed": (
                    v201_low_market_beta_factor.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v201_low_market_beta_factor.get("historical_factor_gate_passed") is True
                ),
                "results": v201_low_market_beta_factor.get("results") or {},
                "test_folds": v201_low_market_beta_factor.get("test_folds") or [],
                "failed_gates": v201_low_market_beta_factor.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v202_v4_residual_momentum_prebacktest_admission": {
                "available": bool(v202_residual_momentum_admission),
                "status": v202_residual_momentum_admission.get("status") or "missing",
                "candidate_id": v202_residual_momentum_admission.get("candidate_id"),
                "v4_codes": (
                    (v202_residual_momentum_admission.get("data") or {}).get("v4_codes")
                ),
                "future_return_data_read": (
                    v202_residual_momentum_admission.get("future_return_data_read") is True
                ),
                "preregistration_admission_passed": (
                    v202_residual_momentum_admission.get("preregistration_admission_passed") is True
                ),
                "single_factor_test_allowed": (
                    v202_residual_momentum_admission.get("single_factor_test_allowed") is True
                    and v203_residual_momentum_factor.get("single_factor_test_consumed") is not True
                ),
                "single_factor_test_consumed": (
                    v203_residual_momentum_factor.get("single_factor_test_consumed") is True
                ),
                "recommendation_eligible": False,
            },
            "v203_v4_residual_momentum_factor_test": {
                "available": bool(v203_residual_momentum_factor),
                "status": v203_residual_momentum_factor.get("status") or "missing",
                "candidate_id": v203_residual_momentum_factor.get("candidate_id"),
                "execution": v203_residual_momentum_factor.get("execution") or {},
                "future_return_data_read": (
                    v203_residual_momentum_factor.get("future_return_data_read") is True
                ),
                "single_factor_test_consumed": (
                    v203_residual_momentum_factor.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v203_residual_momentum_factor.get("historical_factor_gate_passed") is True
                ),
                "results": v203_residual_momentum_factor.get("results") or {},
                "test_folds": v203_residual_momentum_factor.get("test_folds") or [],
                "failed_gates": v203_residual_momentum_factor.get("failed_gates") or [],
                "recommendation_eligible": False,
            },
            "v205_multi_asset_defensive_rotation": {
                "available": bool(v205_multi_asset_rotation),
                "status": v205_multi_asset_rotation.get("status") or "missing",
                "strategy_id": v205_multi_asset_rotation.get("strategy_id"),
                "historical_candidate_passed": (
                    v205_multi_asset_rotation.get("historical_candidate_passed") is True
                    and not v219_qdii_correction
                ),
                "paper_forward_eligible": (
                    not v219_qdii_correction
                    and
                    v205_multi_asset_rotation.get("historical_candidate_passed") is True
                    and v207_multi_asset_robustness.get("paper_forward_eligible") is True
                ),
                "recommendation_eligible": False,
                "automatic_trading": False,
                "cost_ladder_metrics": v205_multi_asset_rotation.get("cost_ladder_metrics") or {},
                "historical_candidate_checks": (
                    v205_multi_asset_rotation.get("historical_candidate_checks")
                    or v205_multi_asset_rotation.get("checks") or {}
                ),
                "current_paper_allocation": (
                    v205_multi_asset_rotation.get("current_research_observation")
                    or v205_multi_asset_rotation.get("current_paper_allocation") or {}
                ),
                "revoked_by_v219": bool(v219_qdii_correction),
            },
            "v207_multi_asset_robustness_audit": {
                "available": bool(v207_multi_asset_robustness),
                "status": v207_multi_asset_robustness.get("status") or "missing",
                "audit_id": v207_multi_asset_robustness.get("audit_id"),
                "robustness_passed": (
                    v207_multi_asset_robustness.get("robustness_passed") is True
                    and not v219_qdii_correction
                ),
                "paper_forward_eligible": (
                    v207_multi_asset_robustness.get("paper_forward_eligible") is True
                    and not v219_qdii_correction
                ),
                "recommendation_eligible": False,
                "automatic_trading": False,
                "checks": v207_multi_asset_robustness.get("checks") or {},
                "independent_reproduction": v207_multi_asset_robustness.get("independent_reproduction") or {},
                "leave_one_asset_out": v207_multi_asset_robustness.get("leave_one_asset_out") or {},
                "parameter_neighborhood": v207_multi_asset_robustness.get("parameter_neighborhood") or {},
                "cost_and_execution_perturbation": (
                    v207_multi_asset_robustness.get("cost_and_execution_perturbation")
                    or v207_multi_asset_robustness.get("cost_and_signal_delay") or {}
                ),
                "return_concentration": v207_multi_asset_robustness.get("return_concentration") or {},
                "revoked_by_v219": bool(v219_qdii_correction),
            },
            "v151_roe_prebacktest_admission": {
                "available": bool(v151_roe_admission),
                "status": v151_roe_admission.get("status") or "missing",
                "candidate_id": v151_roe_admission.get("candidate_id"),
                "records": (
                    (v151_roe_admission.get("data_audit") or {}).get("records")
                ),
                "codes": (
                    (v151_roe_admission.get("data_audit") or {}).get("codes")
                ),
                "return_data_read": v151_roe_admission.get("return_data_read") is True,
                "preregistration_admission_passed": (
                    v151_roe_admission.get("preregistration_admission_passed") is True
                ),
                "single_factor_backtest_allowed": (
                    v151_roe_admission.get("single_factor_backtest_allowed") is True
                    and v153_roe_factor.get("return_data_read") is not True
                ),
                "single_factor_test_consumed": (
                    v153_roe_factor.get("return_data_read") is True
                ),
                "strategy_backtest_allowed": (
                    v151_roe_admission.get("strategy_backtest_allowed") is True
                ),
                "failed_gates": v151_roe_admission.get("failed_gates") or [],
                "reasons": v151_roe_admission.get("reasons") or [],
            },
            "v153_roe_factor_test": {
                "available": bool(v153_roe_factor),
                "status": v153_roe_factor.get("status") or "missing",
                "return_data_read": v153_roe_factor.get("return_data_read") is True,
                "historical_factor_gate_passed": (
                    v153_roe_factor.get("historical_factor_gate_passed") is True
                ),
                "strategy_backtest_allowed": (
                    v153_roe_factor.get("strategy_backtest_allowed") is True
                ),
                "splits": v153_roe_factor.get("splits") or {},
            },
            "v155_asset_growth_prebacktest_admission": {
                "available": bool(v155_asset_growth_admission),
                "status": v155_asset_growth_admission.get("status") or "missing",
                "candidate_id": v155_asset_growth_admission.get("candidate_id"),
                "records": (
                    (v155_asset_growth_admission.get("counts") or {}).get("complete_pit_records")
                ),
                "codes": (
                    (v155_asset_growth_admission.get("counts") or {}).get("codes")
                ),
                "periods": (
                    (v155_asset_growth_admission.get("counts") or {}).get("periods")
                ),
                "return_data_read": (
                    v155_asset_growth_admission.get("return_data_read") is True
                ),
                "preregistration_admission_passed": (
                    v155_asset_growth_admission.get("preregistration_admission_passed") is True
                ),
                "single_factor_backtest_allowed": (
                    v155_asset_growth_admission.get("single_factor_backtest_allowed") is True
                    and v157_asset_growth_factor.get("return_data_read") is not True
                ),
                "single_factor_test_consumed": (
                    v157_asset_growth_factor.get("return_data_read") is True
                ),
                "strategy_backtest_allowed": (
                    v155_asset_growth_admission.get("strategy_backtest_allowed") is True
                ),
                "failed_gates": v155_asset_growth_admission.get("failed_gates") or [],
            },
            "v157_asset_growth_factor_test": {
                "available": bool(v157_asset_growth_factor),
                "status": v157_asset_growth_factor.get("status") or "missing",
                "return_data_read": v157_asset_growth_factor.get("return_data_read") is True,
                "historical_factor_gate_passed": (
                    v157_asset_growth_factor.get("historical_factor_gate_passed") is True
                ),
                "strategy_backtest_allowed": (
                    v157_asset_growth_factor.get("strategy_backtest_allowed") is True
                ),
                "splits": v157_asset_growth_factor.get("splits") or {},
            },
            "v163_asset_growth_prebacktest_admission": {
                "available": bool(v163_asset_growth_admission),
                "status": v163_asset_growth_admission.get("status") or "missing",
                "candidate_id": v163_asset_growth_admission.get("candidate_id"),
                "records": (
                    (v163_asset_growth_admission.get("counts") or {}).get("complete_pit_records")
                ),
                "codes": (v163_asset_growth_admission.get("counts") or {}).get("codes"),
                "periods": (v163_asset_growth_admission.get("counts") or {}).get("periods"),
                "price_data_read": v163_asset_growth_admission.get("price_data_read") is True,
                "return_data_read": v163_asset_growth_admission.get("return_data_read") is True,
                "preregistration_admission_passed": (
                    v163_asset_growth_admission.get("preregistration_admission_passed") is True
                ),
                "single_factor_backtest_allowed": (
                    v163_asset_growth_admission.get("single_factor_backtest_allowed") is True
                    and v165_asset_growth_factor.get("return_data_read") is not True
                ),
                "single_factor_test_consumed": (
                    v165_asset_growth_factor.get("return_data_read") is True
                ),
                "strategy_backtest_allowed": (
                    v163_asset_growth_admission.get("strategy_backtest_allowed") is True
                ),
                "failed_gates": v163_asset_growth_admission.get("failed_gates") or [],
            },
            "v165_asset_growth_factor_test": {
                "available": bool(v165_asset_growth_factor),
                "status": v165_asset_growth_factor.get("status") or "missing",
                "return_data_read": v165_asset_growth_factor.get("return_data_read") is True,
                "single_factor_test_consumed": (
                    v165_asset_growth_factor.get("single_factor_test_consumed") is True
                ),
                "historical_factor_gate_passed": (
                    v165_asset_growth_factor.get("historical_factor_gate_passed") is True
                ),
                "strategy_backtest_allowed": (
                    v165_asset_growth_factor.get("strategy_backtest_allowed") is True
                ),
                "splits": v165_asset_growth_factor.get("splits") or {},
            },
            "forward_validation": {
                "strategy_id": forward_evaluation.get("strategy_id"),
                "as_of_market_date": forward_evaluation.get("as_of_market_date"),
                "first_eligible_date": forward_schedule.get("first_eligible_date"),
                "scheduled_observations": forward_schedule.get("scheduled_observations", 0),
                "minimum_scheduled_observations": forward_schedule.get(
                    "minimum_scheduled_observations", 12
                ),
                "eligible_signal_dates": forward_signals.get("eligible_signal_dates", 0),
                "completed_signal_outcomes": forward_outcomes.get("completed", 0),
                "completed_portfolio_trades": forward_portfolio.get("completed", 0),
                "schedule_integrity_passed": (
                    forward_schedule.get("schedule_integrity_passed") is True
                ),
                "forward_gate_passed": (
                    forward_evaluation.get("forward_gate_passed") is True
                ),
                "invalidated": forward_candidate_invalidated,
                "failed_gates": forward_evaluation.get("failed_gates") or [],
            },
            "successor_discovery": {
                "data_epoch_id": discovery_assessment.get("data_epoch_id"),
                "round": discovery_round,
                "round_limit": discovery_limit,
                "round_cap_reached": discovery_cap_reached,
                "candidate_ids": discovery_ids,
                "candidate_consensus": (
                    discovery_assessment.get("candidate_consensus") is True
                ),
                "open_source_audit_accepted": (
                    discovery_assessment.get("open_source_audit_accepted") is True
                ),
                "semantic_integrity_passed": (
                    discovery_assessment.get("semantic_integrity_passed") is True
                ),
                "actionable_candidate": (
                    discovery_assessment.get("actionable_candidate") is True
                ),
                "status": discovery_assessment.get("status") or "missing",
                "reasons": discovery_reasons,
                "can_start_backtest": (
                    (discovery_assessment.get("safety") or {}).get("can_start_backtest")
                    is True
                ),
                **({
                    "latest_completed_candidate": v219_qdii_correction.get("strategy_id"),
                    "latest_completed_status": v219_qdii_correction.get("status"),
                    "latest_completed_test": "V219",
                    "latest_return_computed": True,
                    "next_candidate_preregistered": False,
                } if v219_qdii_correction else {
                    "latest_completed_candidate": (
                        v207_multi_asset_robustness.get("strategy_id")
                        or v207_multi_asset_robustness.get("subject_strategy_id")
                    ),
                    "latest_completed_status": (
                        v207_multi_asset_robustness.get("status")
                    ),
                    "latest_completed_test": (
                        "V211" if str(v207_multi_asset_robustness.get("audit_id") or "").startswith("V211")
                        else "V207"
                    ),
                    "latest_return_computed": True,
                    "next_candidate_preregistered": False,
                } if v207_multi_asset_robustness else {
                    "latest_completed_candidate": (
                        v205_multi_asset_rotation.get("strategy_id")
                    ),
                    "latest_completed_status": (
                        v205_multi_asset_rotation.get("status")
                    ),
                    "latest_completed_test": "V205",
                    "latest_return_computed": True,
                    "next_candidate_preregistered": False,
                } if v205_multi_asset_rotation else {
                    "latest_completed_candidate": (
                        v203_residual_momentum_factor.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v203_residual_momentum_factor.get("status")
                    ),
                    "latest_completed_test": "V203",
                    "latest_return_computed": (
                        v203_residual_momentum_factor.get("future_return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v203_residual_momentum_factor else {
                    "latest_completed_candidate": (
                        v201_low_market_beta_factor.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v201_low_market_beta_factor.get("status")
                    ),
                    "latest_completed_test": "V201",
                    "latest_return_computed": (
                        v201_low_market_beta_factor.get("future_return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v201_low_market_beta_factor else {
                    "latest_completed_candidate": (
                        v197_low_max_factor.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v197_low_max_factor.get("status")
                    ),
                    "latest_completed_test": "V197",
                    "latest_return_computed": (
                        v197_low_max_factor.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v197_low_max_factor else {
                    "latest_completed_candidate": (
                        v195_low_volatility_factor.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v195_low_volatility_factor.get("status")
                    ),
                    "latest_completed_test": "V195",
                    "latest_return_computed": (
                        v195_low_volatility_factor.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v195_low_volatility_factor else {
                    "latest_completed_candidate": (
                        v193_52_week_high_factor.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v193_52_week_high_factor.get("status")
                    ),
                    "latest_completed_test": "V193",
                    "latest_return_computed": (
                        v193_52_week_high_factor.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v193_52_week_high_factor else {
                    "latest_completed_candidate": (
                        v191_major_contract_5d_drift.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v191_major_contract_5d_drift.get("status")
                    ),
                    "latest_completed_test": "V191",
                    "latest_return_computed": (
                        v191_major_contract_5d_drift.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v191_major_contract_5d_drift else {
                    "latest_completed_candidate": (
                        v189_ipo_lockup_release_reversal.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v189_ipo_lockup_release_reversal.get("status")
                    ),
                    "latest_completed_test": "V189",
                    "latest_return_computed": (
                        v189_ipo_lockup_release_reversal.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v189_ipo_lockup_release_reversal else {
                    "latest_completed_candidate": (
                        v187_open_market_repurchase_drift.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v187_open_market_repurchase_drift.get("status")
                    ),
                    "latest_completed_test": "V187",
                    "latest_return_computed": (
                        v187_open_market_repurchase_drift.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v187_open_market_repurchase_drift else {
                    "latest_completed_candidate": (
                        v185_pre_ex_dividend_drift.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v185_pre_ex_dividend_drift.get("status")
                    ),
                    "latest_completed_test": "V185",
                    "latest_return_computed": (
                        v185_pre_ex_dividend_drift.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v185_pre_ex_dividend_drift else {
                    "latest_completed_candidate": (
                        v182_filing_timeliness_factor.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v182_filing_timeliness_factor.get("status")
                    ),
                    "latest_completed_test": "V182",
                    "latest_return_computed": (
                        v182_filing_timeliness_factor.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v182_filing_timeliness_factor else {
                    "latest_completed_candidate": (
                        v180_seasonality_factor.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v180_seasonality_factor.get("status")
                    ),
                    "latest_completed_test": "V180",
                    "latest_return_computed": (
                        v180_seasonality_factor.get("return_data_read") is True
                    ),
                    "next_candidate_preregistered": False,
                } if v180_seasonality_factor else {
                    "latest_completed_candidate": (
                        v178_sse_disciplinary_prices.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v178_sse_disciplinary_prices.get("status")
                    ),
                    "latest_completed_test": "V178",
                    "latest_return_computed": False,
                    "next_candidate_preregistered": False,
                } if v178_sse_disciplinary_prices else {
                    "latest_completed_candidate": (
                        v176_pmi_gap_acceleration.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v176_pmi_gap_acceleration.get("status")
                    ),
                    "latest_completed_test": "V176",
                    "next_candidate_preregistered": False,
                } if v176_pmi_gap_acceleration else {
                    "latest_completed_candidate": (
                        v172_industry_profit_acceleration.get("candidate_id")
                    ),
                    "latest_completed_status": (
                        v172_industry_profit_acceleration.get("status")
                    ),
                    "latest_completed_test": "V172",
                    "next_candidate_preregistered": False,
                } if v172_industry_profit_acceleration else {}),
            },
            "closest_candidate": {
                "label": "冻结低波因子 + 20日市场风险降档",
                "status": "research_only",
                "development_annualized_pct": regime_result.get("annualized_pct"),
                "development_dsr": regime_result.get("dsr"),
                "cross_section_9y_annualized_pct": holdout_full.get("annualized_pct"),
                "cross_section_9y_max_drawdown_pct": holdout_full.get("max_drawdown_pct"),
                "cross_section_9y_dsr": holdout_full.get("dsr"),
                "cross_section_recent_annualized_pct": holdout_recent.get("annualized_pct"),
                "cross_section_recent_years": (
                    f"{holdout_recent.get('years_positive')}/"
                    f"{holdout_recent.get('years_total')}"
                ),
                "holdout_verdict": cross_holdout.get("verdict"),
                "recommendation_eligible": False,
            },
        },
        "blockers": blockers,
    }
    presence = {
        "raw_daily": bool(progress and update and daily_quality),
        "research_snapshot": bool(snapshot),
        "product_sync": bool(product),
        "v12_fundamentals": bool(v12_fundamentals),
        "v148_cninfo_filing_times": bool(v148_filing_times),
        "v149_sse_xbrl_profitability": bool(v149_xbrl_profitability),
        "v150_sse_xbrl_roe": bool(v150_roe_epoch),
        "v152_v151_adjusted_prices": bool(v152_adjusted_prices),
        "v154_quarterly_asset_growth": bool(v154_asset_growth),
        "v156_v155_adjusted_prices": bool(v156_asset_growth_prices),
        "v158_eastmoney_quarterly_asset_growth": bool(v158_eastmoney_asset_growth),
        "v162_official_quarterly_asset_growth": bool(v162_asset_growth),
        "v164_v163_adjusted_prices": bool(v164_asset_growth_prices),
        "v166_nbs_industry_profit_panel": bool(v166_industry_profit_panel),
        "v168_csi300_industrial_prices": bool(v168_industrial_index_prices),
        "v171_csi_all_share_industrial_prices": bool(
            v171_all_share_industrial_prices
        ),
        "v173_nbs_pmi_demand_inventory": bool(v173_pmi_demand_inventory),
        "v175_csi_all_share_energy_prices": bool(v175_all_share_energy_prices),
        "v89_sse_regulatory_measures": bool(v89_sse_regulatory_measures),
        "v178_sse_disciplinary_event_prices": bool(
            v178_sse_disciplinary_prices
        ),
        "v183_local_order_book_data_epoch": bool(v183_local_order_book),
        "v184_cninfo_cash_dividend_data_epoch": bool(v184_cash_dividend_epoch),
        "v186_cninfo_open_market_repurchase_data_epoch": bool(
            v186_open_market_repurchase_epoch
        ),
        "v188_cninfo_ipo_lockup_release_data_epoch": bool(
            v188_ipo_lockup_release_epoch
        ),
        "v190_cninfo_major_contract_data_epoch": bool(
            v190_major_contract_epoch
        ),
        "margin_history": (reports / "v9_margin_history_manifest.json").is_file(),
        "v90_pmi_epoch": (
            reports / "v90_nbs_pmi_data_epoch_audit.json"
        ).is_file(),
        "v120_pbc_epoch": (
            reports / "v120_pbc_liquidity_data_epoch_audit.json"
        ).is_file(),
        "v143_cpi_ppi_epoch": (
            reports / "v143_nbs_cpi_ppi_data_epoch_audit.json"
        ).is_file(),
        "v145_industrial_profit_epoch": (
            reports / "v145_nbs_industrial_profit_data_epoch_audit.json"
        ).is_file(),
        "decision_indexes": bool(decision_indexes),
        "benchmark_indexes": bool(current_index_update),
        "decision_calendar": bool(decision_calendar),
    }
    tracked = [
        ("raw_daily", result["raw_daily"]),
        ("research_snapshot", result["research_snapshot"]),
        ("product_sync", result["product_sync"]),
        *list(result["research_data"].items()),
    ]
    gated_absence = {
        "v152_v151_adjusted_prices": not v150_ready,
        "v156_v155_adjusted_prices": not v154_ready,
        "v164_v163_adjusted_prices": not v162_ready,
        "v168_csi300_industrial_prices": (
            v167_industry_profit_admission.get("preregistration_admission_passed")
            is not True
        ),
        "v171_csi_all_share_industrial_prices": (
            v170_industry_profit_acceleration_admission.get(
                "preregistration_admission_passed"
            ) is not True
        ),
        "v175_csi_all_share_energy_prices": (
            v174_pmi_gap_acceleration_admission.get(
                "preregistration_admission_passed"
            ) is not True
        ),
    }
    catalog = []
    for name, item in tracked:
        artifact_present = bool(presence.get(name))
        accepted = bool(item.get("ready") is True or item.get("passed") is True)
        gated_not_required = bool(
            not artifact_present
            and (
                gated_absence.get(name) is True
                or str(item.get("status") or "").startswith("blocked_before_")
            )
        )
        if accepted:
            state = "accepted"
        elif gated_not_required:
            state = "gated_not_required"
        elif artifact_present:
            state = "rejected_or_incomplete"
        else:
            state = "required_missing"
        catalog.append({
            "id": name,
            "website_visible": True,
            "artifact_present": artifact_present,
            "decision_accepted": accepted,
            "state": state,
        })
    required_missing = sum(row["state"] == "required_missing" for row in catalog)
    fully_synced = bool(
        data_ready
        and product_ok
        and required_missing == 0
        and len(report_artifacts) == len(list(reports.glob("*.json")))
    )
    result["status"] = (
        "ready" if fully_synced and decision_data_ready
        else "synced_with_rejections" if fully_synced
        else "degraded"
    )
    result["fully_synced"] = fully_synced
    result["decision_data_ready"] = decision_data_ready
    result["research_artifact_ledger"] = {
        "status": "complete",
        "source": "reports/*.json live filesystem index",
        "report_json_count": len(report_artifacts),
        "indexed_count": len(report_artifacts),
        "all_report_artifacts_indexed": True,
        "metadata_fingerprint_sha256": report_fingerprint.hexdigest(),
        "fingerprint_scope": "filename + byte size + mtime_ns",
        "items": report_artifacts,
    }
    result["sync_coverage"] = {
        "status": "complete" if required_missing == 0 else "incomplete",
        "tracked_count": len(catalog),
        "website_visible_count": len(catalog),
        "accounted_count": sum(
            row["state"] != "required_missing" for row in catalog
        ),
        "artifact_present_count": sum(row["artifact_present"] for row in catalog),
        "decision_accepted_count": sum(row["decision_accepted"] for row in catalog),
        "rejected_or_incomplete_count": sum(
            row["state"] == "rejected_or_incomplete" for row in catalog
        ),
        "gated_not_required_count": sum(
            row["state"] == "gated_not_required" for row in catalog
        ),
        "required_missing_count": required_missing,
        "all_tracked_visible": True,
        "all_required_artifacts_accounted": required_missing == 0,
        "report_artifact_count": len(report_artifacts),
        "report_artifact_indexed_count": len(report_artifacts),
        "all_report_artifacts_indexed": True,
        "total_visible_evidence_count": len(catalog) + len(report_artifacts),
        "note": "已同步可见不等于通过决策门禁；失败和拒绝证据也必须同步展示。",
        "items": catalog,
    }
    if (
        v205_multi_asset_rotation.get("historical_candidate_passed") is True
        and not v219_qdii_correction
    ):
        result["blockers"] = [
            row for row in result["blockers"]
            if row.get("category") != "profitability"
        ]
    return result
