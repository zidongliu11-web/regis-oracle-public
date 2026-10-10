"""Explain the existing sector adapter; never select sectors or grant authority."""

from math import isfinite
import hashlib
from pathlib import Path


def public_implementation_sha():
    """Expose only the diagnostic code/UI fingerprint, never market or account data."""
    root = Path(__file__).parent
    return hashlib.sha256(b"\0".join(path.read_bytes() for path in (
        Path(__file__), root / "static/mainline-evidence.js", root / "static/mainline-evidence.css",
        root / "static/spa-home.js", root / "static/spa-workflow.js",
        root / "static/workflow-pages.css", root / "templates/spa.html",
        root / "static/spa-plan-research.js", root / "static/research-plan.js",
        root / "planning_account_review.py",
        root / "audited_risk_history.py", root / "risk_control.py", root / "cloud_market_data.py", root / "static/spa-risk.js", root / "static/spa.js",
        root / "market_intelligence.py",
        root / "cloud_sector_observations.py",
        root / "cloud_sector_constituents.py",
        root / "sector_constituent_view.py",
        root / "forward_capture_integrity.py",
        root.parent / "deploy/research-source-bundle.json",
        root.parent / "research/CLOUD_SECTOR_CONSTITUENT_PATH_COMPATIBILITY_V1.json",
        root.parent / "research/MARKET_SECTOR_CONSTITUENT_FORWARD_PROTOCOL_V2_AMENDMENT_2.json",
        root.parent / "research/MARKET_SECTOR_CONSTITUENT_CAPTURE_RECOVERY_PROTOCOL_V1_AMENDMENT_1.json",
        root / "next_session_sectors.py",
        root / "sector_rotation_review.py",
        root / "cloud_sector_close.py", root / "cloud_sector_timer.py",
        root / "cloud_sector_close_members.py",
        root / "app.py", root / "research_route_snapshot.py",
        root / "playbook_entry_timing.py", root / "playbook_development.py",
        root / "playbook_candidate_scan.py", root / "static/playbook_candidate_scan.js",
        root / "playbook_stock_checks.py", root / "emotion_system.py", root / "mainline_system.py",
        root / "static/playbook_development.js", root / "templates/strategy_lab.html",
        root / "market_sector_constituent_forward.py",
        root / "market_sector_constituent_recovery.py",
        root / "system_route_joint_v2_gate.py",
        root / "research_workbench.py", root / "cash_conditioned_research.py",
        root / "control_increment_research.py",
        root / "control_liquidity_research.py", root / "suspension_liquidity.py",
        root / "control_corporate_evidence.py",
        root / "distribution_settlement.py", root / "control_distribution_research.py",
        root / "workbench_artifact_paths.py", root.parent / "deploy/render_start.sh",
        root / "static/research_workbench.js", root / "templates/research_workbench.html",
    ))).hexdigest()


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def build_mainline_diagnostics(snapshot, adapted):
    """Project the very same observation consumed by the router, without writes.

    Broadening the frozen top-decile rule or attributing a leader from an index
    name is out of scope. Missing enrichments are not that rule's entry gates.
    """
    from .cloud_sector_observations import history
    from .sector_constituent_view import observation
    from .next_session_sectors import build as next_session_sectors

    persistence = history()
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    adapted = adapted if isinstance(adapted, dict) else {}
    meta = snapshot.get("meta") or {}
    checks = []

    def check(label, passed, observed, requirement):
        checks.append({"label": label, "passed": passed, "observed": observed,
                       "requirement": requirement})

    check("规则身份", (adapted.get("protocol") or {}).get("identity_valid") is True,
          "已核验" if (adapted.get("protocol") or {}).get("identity_valid") is True else "未核验",
          "既有冻结板块规则的文件身份一致")
    source_label = {"ok": "来源可用", "degraded": "来源不完整或延迟", "unavailable": "来源不可用"}.get(snapshot.get("status"), "来源未核验")
    health_label = {"verified": "时效核验通过", "delayed": "数据延迟", "upstream_error": "上游异常", "missing": "数据缺失"}.get(snapshot.get("health_status"), "时效未知")
    check("来源状态", snapshot.get("status") == "ok" and snapshot.get("health_status") == "verified",
          source_label + " · " + health_label,
          "来源可用且时效核验通过")
    coverage = _number(adapted.get("coverage_pct"))
    check("板块横截面覆盖", coverage == 100 and all(meta.get(key) is True for key in (
          "coverage_complete", "industry_complete", "concept_complete")),
          f"{coverage:g}%" if coverage is not None else "覆盖率未知",
          "行业与概念全部分页完整，有效行覆盖率100%")
    check("源时点与涨跌家数", all(meta.get(key) is True for key in (
          "timestamp_complete", "breadth_count_complete")),
          "完整" if all(meta.get(key) is True for key in ("timestamp_complete", "breadth_count_complete")) else "存在缺失",
          "每行都有来源时间及上涨、下跌家数")
    age = _number(adapted.get("source_age_seconds"))
    same_day = bool(adapted.get("source_as_of") and adapted.get("as_of")
                    and str(adapted["source_as_of"])[:10] == str(adapted["as_of"])[:10])
    check("数据新鲜度", same_day and age is not None and 0 <= age <= 180,
          f"{age:g}秒前" if age is not None else "来源时间未知",
          "同一交易日、不晚于观察时点且不超过180秒")
    slot = adapted.get("observation_slot")
    slot_names = {"morning_1030": "上午确认窗口", "afternoon_1400": "下午确认窗口"}
    check("研究确认窗口", slot in slot_names, slot_names.get(slot, "当前不在确认窗口"),
          "上海时间10:30–10:45、14:00–14:15；窗口外只展示领涨观察")
    # The adapter remains the decision source, including all its completeness
    # gates. An empty list is a market result only after those gates pass.
    observed = adapted.get("observation_ready") is True
    confirmed = adapted.get("status") == "confirmed" and bool(adapted.get("confirmed_sectors"))
    if not observed:
        status, label = "input_unavailable", "主线输入未就绪"
        gaps = "、".join(row["label"] for row in checks[:-1] if not row["passed"])
        reason = f"未通过：{gaps or '观察输入契约'}。不能判断市场是否存在主线。"
    elif slot not in slot_names:
        status, label = "awaiting_window", "领涨观察 · 等待窗口确认"
        reason = "当前横截面可观察，但不在既有研究确认窗口；领涨不等于持续主线。"
    elif confirmed:
        status, label = "research_confirmed", "板块广度已确认 · 仅研究"
        reason = "已有板块满足固定窗口的涨幅和广度规则；尚不能证明持续性、龙头身份或买入优势。"
    else:
        status, label = "criteria_not_met", "数据齐备 · 暂无达标板块"
        reason = "本次完整横截面没有板块同时满足正涨幅、上涨占比和同类排名门槛。"
    rows = adapted.get("confirmed_sectors") if confirmed else adapted.get("observed_sectors")
    sectors = [{key: row.get(key) for key in (
        "name", "code", "kind", "change_pct", "advance_coverage_pct", "change_rank_pct",
        "up_count", "down_count", "constituent_count", "data_as_of",
    )} for row in (rows or [])[:3] if isinstance(row, dict)] if observed else []
    return {
        "schema": "mainline_diagnostics.v1", "status": status, "label": label,
        "reason": reason, "source": snapshot.get("source") or "来源未提供",
        "as_of": adapted.get("as_of"), "source_as_of": adapted.get("source_as_of"),
        "checks": checks, "sectors": sectors,
        "rule_summary": "至少5家有涨跌计数的成分；板块涨幅>0；上涨/(上涨+下跌)≥60%；行业与概念分别计算涨幅中位秩百分位≥90；最多取3个。",
        "rule_boundary": "平盘家数未纳入上涨占比分母；行业和概念可能重叠，不能当作独立风险分散。",
        "missing_capabilities": [
            {"label": "持续性", "reason": "相邻留档仅描述观察记录；仍须验证跨时段持续性和交易优势，不能用连续入选次数代替。"},
            {"label": "核心股与龙头", "reason": "板块指数不能证明龙头身份，须有完整成分行情和事先确定的核心股规则。"},
            {"label": "量能、炸板与拥挤", "reason": "成分观察可描述当时扩散与成交额集中；尚未验证跨时点成交扩张、炸板率或拥挤阈值，不按零值处理。"},
            {"label": "战法与账户", "reason": "板块通过不等于可以买；仍须当前情绪、已验证战法、价格触发、有效风险线及真实账户共同通过。"},
        ],
        "research_only": True, "generates_alert": False, "changes_trade_authority": False,
        "persistence_observation": persistence,
        "constituent_observation": observation(),
        "next_session_preparation": next_session_sectors(),
    }
