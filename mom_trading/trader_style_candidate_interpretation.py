"""Read-only public-trader lenses for one already-ranked research candidate."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from .trader_style_distillation import (
    CORRECTION_CONTRACT_SHA256,
    DISTILLATION_CONTRACT_SHA256,
    _LENS_CLAIMS,
)


CONTRACT_VERSION = "candidate_trader_style_interpretation_v2"
LENS_SPECS = (
    ("emotion_cycle", "情绪周期与风险预算"),
    ("sector_front_row", "板块前排与扩散"),
    ("ladder_extremes", "高低位梯队"),
    ("weak_market_and_hot_sector", "弱市与持续热点"),
    ("predict_confirm_follow", "预判·确认·跟随"),
    ("high_energy_and_tradability", "高能期与可成交性"),
    ("mode_discipline", "模式纪律"),
    ("consensus_and_error_exit", "市场合力与纠错退出"),
)
_LENS_IDS = tuple(item[0] for item in LENS_SPECS)
_LENS_EVIDENCE_CONTRACT = {
    "emotion_cycle": (
        "炒股养家", ("A2",), ("hypothesis_seed_only",),
        "research_context_only",
    ),
    "sector_front_row": (
        "北京炒家", ("A2",), ("hypothesis_seed_only",),
        "research_context_only",
    ),
    "ladder_extremes": (
        "92科比", ("C",), ("hypothesis_seed_only",),
        "secondary_catalogue",
    ),
    "weak_market_and_hot_sector": (
        "职业炒手", ("C", "C"),
        ("hypothesis_seed_only", "hypothesis_seed_only"),
        "secondary_catalogue",
    ),
    "predict_confirm_follow": (
        "涅盘重升", ("C", "C"),
        ("provenance_repair_required", "provenance_repair_required"),
        "evidence_insufficient",
    ),
    "high_energy_and_tradability": (
        "龙飞虎", ("A2", "A2"),
        ("hypothesis_seed_only", "hypothesis_seed_only"),
        "research_context_only",
    ),
    "mode_discipline": (
        "赵强/赵老哥", ("B",), ("execution_QA_only",),
        "research_context_only",
    ),
    "consensus_and_error_exit": (
        "Asking", ("C", "C"),
        ("hypothesis_seed_only", "hypothesis_seed_only"),
        "secondary_catalogue",
    ),
}
_LENS_CORRECTION_DISPOSITIONS = {
    "emotion_cycle": ("direct_anonymous_research_or_QA_seed",),
    "sector_front_row": ("direct_anonymous_research_or_QA_seed",),
    "ladder_extremes": ("downgraded_or_suspended",),
    "weak_market_and_hot_sector": (
        "downgraded_or_suspended", "downgraded_or_suspended",
    ),
    "predict_confirm_follow": (
        "downgraded_or_suspended", "downgraded_or_suspended",
    ),
    "high_energy_and_tradability": (
        "downgraded_or_suspended", "downgraded_or_suspended",
    ),
    "mode_discipline": ("narrow_non_alpha_provenance_or_QA_only",),
    "consensus_and_error_exit": (
        "downgraded_or_suspended", "downgraded_or_suspended",
    ),
}
_CLAIM_SOURCE_IDENTITIES = {
    "CGYJ-01": (
        "https://www.tgb.cn/a/1ykwVHqdlf8",
        "65d21251edd6fa7688f6b9abfc9b74b3777f06421982531226629894dea41785",
    ),
    "BJCJ-01": (
        "https://m.tgb.cn/a/1yCuBqCdPhS",
        "f9a3409c24785c57dd6032ab2b99ab5c1c7870656992034311995c126382675c",
    ),
    "KB92-02": (
        "https://m.tgb.cn/a/1P7lsARLS5T?type=hot",
        "8746250959923e210670d2c42cad0d685790b0aa437a80ea656fa63d69249827",
    ),
    "PRO-01": (
        "https://www.xiarj.com/19866.html",
        "63d36ae0a5713341f3980776437a2fb95349ff99303054074e84ec2e2cd1b2ff",
    ),
    "PRO-02": (
        "https://www.xiarj.com/19866.html",
        "63d36ae0a5713341f3980776437a2fb95349ff99303054074e84ec2e2cd1b2ff",
    ),
    "NIR-01": (
        "https://m.tgb.cn/a/25deaxk0lSa?type=hot",
        "d851e0e12bc82ce681c9d201eb9866235c4d962e3de8df97997aedc0226037a1",
    ),
    "NIR-02": (
        "https://m.tgb.cn/a/25deaxk0lSa?type=hot",
        "d851e0e12bc82ce681c9d201eb9866235c4d962e3de8df97997aedc0226037a1",
    ),
    "LFH-01": (
        "https://m.tgb.cn/a/1ykw7bTLAP6?type=hot",
        "599c49494bd1dc6f2249cd6ebb9435a12088d194c7b46e9cd86c75de6092f76c",
    ),
    "LFH-02": (
        "https://m.tgb.cn/a/1ykw7bTLAP6?type=hot",
        "599c49494bd1dc6f2249cd6ebb9435a12088d194c7b46e9cd86c75de6092f76c",
    ),
    "ZQ-01": (
        "https://news.cnstock.com/news,rdsm-201509-3571947.htm",
        "823faa853282181d20406bce76b2cb94f3cba4a8c8296e10be04dd3333672568",
    ),
    "ASK-01": (
        "https://www.xiarj.com/596.html",
        "ea816a25b40cd293ca808d144888a74e786412441b117496e592c8d2017db853",
    ),
    "ASK-02": (
        "https://www.xiarj.com/596.html",
        "ea816a25b40cd293ca808d144888a74e786412441b117496e592c8d2017db853",
    ),
}
_VALID_STANCES = {
    "defensive", "avoid_chasing", "selective_participation",
    "wait_for_confirmation", "sector_confirmed_research", "data_incomplete",
    "confirmed_route", "inside_mode", "outside_mode_veto",
    "requires_predeclared_invalidation",
}
_STATE_LABELS = {
    "support": "支持",
    "counterevidence": "反证",
    "not_applicable": "不适用",
    "missing": "缺失",
}
_AUTHORITY = {
    "changes_unified_score": False,
    "changes_ranking": False,
    "changes_position_or_quantity": False,
    "changes_trade_count": False,
    "generates_buy_or_sell_alert": False,
    "generates_recommendation": False,
    "production": False,
    "automatic_trading": False,
}
_STYLE_AUTHORITY_KEYS = (
    "changes_score",
    "changes_trade_count",
    "changes_position_or_quantity",
    "generates_alert",
    "production",
    "automatic_trading",
)
_ROUTE_FALSE_KEYS = (
    "changes_unified_score",
    "changes_trade_count",
    "changes_position_or_quantity",
    "generates_buy_or_sell_alert",
    "production_strategy",
    "automatic_trading",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read(root: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = root
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _first(
    root: Mapping[str, Any], paths: tuple[tuple[str, ...], ...],
) -> tuple[Any, str | None]:
    for path in paths:
        value = _read(root, path)
        if value is not None and value != "":
            return value, ".".join(path)
    return None, None


def _first_text(
    root: Mapping[str, Any], paths: tuple[tuple[str, ...], ...],
) -> tuple[str | None, str | None]:
    for path in paths:
        value = _read(root, path)
        if isinstance(value, str) and value.strip():
            return value.strip(), ".".join(path)
    return None, None


def _fact(path: str, value: Any) -> dict[str, Any]:
    return {"path": path, "value": deepcopy(value)}


def _sector_identity(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    code = value.get("code")
    name = value.get("name")
    if (
        kind not in {"concept", "industry"}
        or not isinstance(code, str) or not code.strip()
        or not isinstance(name, str) or not name.strip()
    ):
        return None
    return {"kind": kind, "code": code.strip(), "name": name.strip()}


def _confirmed_sector_identities(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or not value:
        return None
    rows = [_sector_identity(item) for item in value]
    return None if any(item is None for item in rows) else list(rows)  # type: ignore[arg-type]


def _sector_matches(
    candidate: dict[str, str] | None,
    confirmed: list[dict[str, str]] | None,
) -> bool:
    if candidate is None or confirmed is None:
        return False
    return any(
        row["kind"] == candidate["kind"]
        and row["code"] == candidate["code"]
        and row["name"] == candidate["name"]
        for row in confirmed
    )


def _valid_as_of(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _style_packet(route: Mapping[str, Any]) -> Mapping[str, Any]:
    role = _mapping(route.get("public_trader_style_role"))
    return _mapping(role.get("style_lenses"))


def _route_authority_safe(route: Mapping[str, Any]) -> bool:
    authority = _mapping(route.get("authority"))
    role = _mapping(route.get("public_trader_style_role"))
    return bool(
        authority.get("research_only") is True
        and all(authority.get(key) is False for key in _ROUTE_FALSE_KEYS)
        and role.get("identity") == "anonymous_public_claim_hypotheses_only"
        and role.get("named_person_signal_input") is False
        and role.get("narrative_or_quote_directly_scored") is False
        and role.get("requires_source_grade_and_oos_validation") is True
    )


def _style_packet_valid(
    packet: Mapping[str, Any], route: Mapping[str, Any],
) -> bool:
    authority = _mapping(packet.get("authority"))
    lenses = packet.get("lenses")
    if (
        packet.get("status") != "research_style_lenses_ready"
        or packet.get("distillation_document_sha256")
        != DISTILLATION_CONTRACT_SHA256
        or packet.get("correction_document_sha256")
        != CORRECTION_CONTRACT_SHA256
        or packet.get("lens_count") != len(_LENS_IDS)
        or packet.get("market_state") != str(route.get("market_state") or "unknown")
        or not isinstance(lenses, list)
        or len(lenses) != len(_LENS_IDS)
        or not all(authority.get(key) is False for key in _STYLE_AUTHORITY_KEYS)
    ):
        return False
    ids = [row.get("lens_id") for row in lenses if isinstance(row, Mapping)]
    return ids == list(_LENS_IDS)


def _expected_route_lens_metadata(
    lens_id: str, context: Mapping[str, Any],
) -> tuple[str, str]:
    state = str(context.get("market_state") or "unknown")
    sector_ready = context.get("sector_ready") is True
    playbooks = context.get("allowed_playbooks") or []
    defensive = state in {"risk_closed", "retreat", "unknown"}
    crowded = state == "crowded_climax"
    rising = state == "sentiment_rising"
    if lens_id == "emotion_cycle":
        if defensive:
            return "defensive", "情绪决定风险预算；当前不新开仓。"
        if crowded:
            return "avoid_chasing", "高潮拥挤，不追后排。"
        if rising:
            return "selective_participation", "情绪上升，仅研究已准入战法。"
        return "wait_for_confirmation", "弱修复，只等待板块确认。"
    if lens_id == "sector_front_row":
        return (
            ("sector_confirmed_research", "仅观察同槽已确认板块的前排候选。")
            if sector_ready else
            ("data_incomplete", "缺同槽板块确认，不输出前排偏好。")
        )
    if lens_id == "ladder_extremes":
        return (
            "data_incomplete",
            "尚无冻结的高低位梯队与排队可成交数据，不把中位股叙事用于评分。",
        )
    if lens_id == "weak_market_and_hot_sector":
        if defensive:
            return "defensive", "弱市减少活动。"
        if sector_ready:
            return "sector_confirmed_research", "只在持续热点获同槽确认后研究。"
        return "wait_for_confirmation", "等待持续热点确认。"
    if lens_id == "predict_confirm_follow":
        stance = (
            "confirmed_route"
            if context.get("route_status") == "research_route_ready"
            else "wait_for_confirmation"
        )
        return stance, "预判不能直接交易；只有固定条件确认后的路由可进入研究。"
    if lens_id == "high_energy_and_tradability":
        if rising and sector_ready:
            return (
                "selective_participation",
                "高能期与板块同时确认，可研究少量机会；一字板仍按不可成交。",
            )
        stance = "defensive" if defensive else "wait_for_confirmation"
        return stance, "非高能确认期，保持防守或等待。"
    if lens_id == "mode_discipline":
        if playbooks:
            return "inside_mode", f"只允许当前路由中的战法：{', '.join(playbooks)}。"
        return "outside_mode_veto", "当前没有准入战法，模式外交易否决。"
    return (
        "requires_predeclared_invalidation",
        "市场合力只作环境证据；任何候选必须在入场前写明失效条件与可执行退出。",
    )


def _lens_identity_valid(
    lens_id: str, lens: Mapping[str, Any], context: Mapping[str, Any],
) -> bool:
    subject, claim_grades, claim_dispositions, source_use_status = (
        _LENS_EVIDENCE_CONTRACT[lens_id]
    )
    claim_ids = _LENS_CLAIMS[lens_id]
    correction_dispositions = _LENS_CORRECTION_DISPOSITIONS[lens_id]
    claim_evidence = lens.get("claim_evidence")
    claim_identity_safe = bool(
        isinstance(claim_evidence, list)
        and len(claim_evidence) == len(claim_ids)
        and all(
            isinstance(row, Mapping)
            and row.get("claim_id") == claim_id
            and row.get("evidence_grade") == grade
            and row.get("source_disposition") == disposition
            and row.get("evidence_use_status") == source_use_status
            and row.get("correction_disposition") == correction_disposition
            and row.get("claim_text_verified") is True
            and row.get("transport_status") == "frozen"
            and row.get("content_hash_bound") is True
            and (
                row.get("source_url"), row.get("content_sha256")
            ) == _CLAIM_SOURCE_IDENTITIES.get(claim_id)
            for row, claim_id, grade, disposition, correction_disposition in zip(
                claim_evidence, claim_ids, claim_grades, claim_dispositions,
                correction_dispositions,
                strict=True,
            )
        )
    )
    expected_stance, expected_view = _expected_route_lens_metadata(
        lens_id, context,
    )
    lens_use_status = (
        "evidence_insufficient"
        if any(value in {"downgraded_or_suspended", "rejected"}
               for value in correction_dispositions)
        else source_use_status
    )
    return bool(
        lens.get("provenance_subject") == subject
        and lens.get("claim_ids") == list(claim_ids)
        and lens.get("evidence_grades") == list(dict.fromkeys(claim_grades))
        and lens.get("source_dispositions")
        == list(dict.fromkeys(claim_dispositions))
        and lens.get("evidence_use_status") == lens_use_status
        and lens.get("claim_correction_dispositions")
        == list(correction_dispositions)
        and claim_identity_safe
        and lens.get("stance") == expected_stance
        and expected_stance in _VALID_STANCES
        and lens.get("view") == expected_view
        and lens.get("named_identity_signal_used") is False
        and lens.get("recommendation") is False
    )


def _candidate_context(
    candidate: Mapping[str, Any], route: Mapping[str, Any],
) -> dict[str, Any]:
    as_of, as_of_path = _first(candidate, (
        ("opportunity_quality", "data_as_of"),
        ("assessment", "as_of"),
        ("as_of",),
    ))
    sector, sector_path = _first(candidate, (
        ("sector_identity",), ("sector",), ("company", "sector_identity"),
    ))
    playbook, playbook_path = _first(candidate, (
        ("playbook_id",), ("strategy_ref", "playbook_id"),
        ("action_plan", "playbook_id"),
    ))
    invalidation, invalidation_path = _first_text(candidate, (
        ("invalidation_condition",),
        ("action_plan", "invalidation_condition"),
        ("alert", "invalidation_condition"),
    ))
    exit_condition, exit_path = _first_text(candidate, (
        ("exit_condition",),
        ("action_plan", "exit_condition"),
        ("action_plan", "exit_observation"),
    ))
    ladder, ladder_path = _first(candidate, (
        ("ladder_position",), ("limit_up_ladder_position",),
        ("limit_up_ladder",),
    ))
    queue_status, queue_path = _first(candidate, (
        ("queue_evidence_status",), ("execution", "queue_evidence_status"),
    ))
    tradable, tradable_path = _first(candidate, (
        ("tradable",), ("execution", "tradable"),
    ))
    execution_eligible, execution_path = _first(candidate, (
        ("assessment", "execution_eligible"),
        ("execution", "eligible"),
    ))
    timing_ok, timing_path = _first(candidate, (
        ("assessment", "gates", "timing", "ok"),
        ("timing", "ok"),
    ))
    timing_phase, timing_phase_path = _first(candidate, (
        ("assessment", "gates", "timing", "phase"),
        ("timing", "phase"),
    ))
    is_limit_up, is_limit_up_path = _first(candidate, (
        ("is_limit_up_candidate",), ("limit_up_candidate",),
    ))
    suspended, suspended_path = _first(candidate, (
        ("suspended",), ("execution", "suspended"),
    ))
    one_price_limit, one_price_path = _first(candidate, (
        ("one_price_limit",), ("execution", "one_price_limit"),
    ))
    confirmed_raw = (
        _mapping(route.get("sector_confirmation")).get("confirmed_sectors") or []
    )
    confirmed = _confirmed_sector_identities(confirmed_raw)
    sector_identity = _sector_identity(sector)
    allowed = [
        str(row.get("id")) for row in
        (_mapping(route.get("research_budget_hypothesis")).get("allowed_playbooks") or [])
        if isinstance(row, Mapping) and row.get("id")
    ]
    return {
        "code": candidate.get("code"),
        "name": candidate.get("name"),
        "as_of": as_of, "as_of_path": as_of_path,
        "sector": sector, "sector_path": sector_path,
        "sector_identity": sector_identity,
        "playbook": playbook, "playbook_path": playbook_path,
        "invalidation": invalidation, "invalidation_path": invalidation_path,
        "exit_condition": exit_condition, "exit_path": exit_path,
        "ladder": ladder, "ladder_path": ladder_path,
        "queue_status": queue_status, "queue_path": queue_path,
        "tradable": tradable, "tradable_path": tradable_path,
        "execution_eligible": execution_eligible,
        "execution_path": execution_path,
        "timing_ok": timing_ok, "timing_path": timing_path,
        "timing_phase": timing_phase, "timing_phase_path": timing_phase_path,
        "is_limit_up": is_limit_up, "is_limit_up_path": is_limit_up_path,
        "suspended": suspended, "suspended_path": suspended_path,
        "one_price_limit": one_price_limit, "one_price_path": one_price_path,
        "route_status": route.get("status"),
        "market_state": route.get("market_state"),
        "sector_ready": _mapping(route.get("sector_confirmation")).get("ready"),
        "confirmed_sectors": confirmed,
        "confirmed_sector_contract_valid": confirmed is not None,
        "sector_confirmed": _sector_matches(sector_identity, confirmed),
        "allowed_playbooks": allowed,
    }


def _evaluate(
    lens_id: str, lens: Mapping[str, Any], context: Mapping[str, Any],
) -> dict[str, Any]:
    support: list[dict[str, Any]] = []
    counter: list[dict[str, Any]] = []
    missing: list[str] = []
    not_applicable: str | None = None
    state = str(context.get("market_state") or "unknown")
    stance = str(lens.get("stance") or "")
    sector = context.get("sector_identity")
    sector_ready = context.get("sector_ready") is True
    sector_contract_valid = (
        context.get("confirmed_sector_contract_valid") is True
    )
    sector_confirmed = context.get("sector_confirmed") is True

    if lens_id == "emotion_cycle":
        if state == "unknown":
            missing.append("research_route.market_state")
        elif stance in {"defensive", "avoid_chasing", "wait_for_confirmation"}:
            counter.extend((
                _fact("research_route.market_state", state),
                _fact("style_lens.stance", stance),
            ))
        elif stance in {"selective_participation", "confirmed_route"}:
            support.extend((
                _fact("research_route.market_state", state),
                _fact("style_lens.stance", stance),
            ))
        else:
            not_applicable = "当前镜头没有形成适用于该市场状态的研究立场"

    elif lens_id == "sector_front_row":
        if not sector_ready:
            missing.append("research_route.sector_confirmation.ready")
        elif not sector_contract_valid:
            missing.append(
                "research_route.sector_confirmation.confirmed_sectors"
            )
        if sector is None:
            missing.append("candidate.sector_identity")
        if not missing:
            fact = _fact(context["sector_path"], sector)
            (support if sector_confirmed else counter).append(fact)

    elif lens_id == "ladder_extremes":
        if context.get("is_limit_up") is False:
            not_applicable = "候选事实明确标记为非涨停梯队候选"
        else:
            if context.get("ladder") is None:
                missing.append("candidate.ladder_position")
            if context.get("queue_status") is None:
                missing.append("candidate.queue_evidence_status")
            tradable = context.get("tradable")
            execution_eligible = context.get("execution_eligible")
            if tradable is not True and tradable is not False:
                missing.append("candidate.tradable")
            if execution_eligible is not True and execution_eligible is not False:
                missing.append("candidate.assessment.execution_eligible")
            if tradable is False:
                counter.append(_fact(context["tradable_path"], False))
            if execution_eligible is False:
                counter.append(_fact(context["execution_path"], False))
            if not missing:
                queue = str(context["queue_status"]).lower()
                positive_queue = queue in {"verified", "available", "tradable", "fillable"}
                eligible = tradable is True and execution_eligible is True
                facts = [
                    _fact(context["ladder_path"], context["ladder"]),
                    _fact(context["queue_path"], context["queue_status"]),
                    _fact(context["tradable_path"], tradable),
                    _fact(context["execution_path"], execution_eligible),
                ]
                if positive_queue and eligible:
                    support.extend(facts)
                elif not counter:
                    counter.extend(facts)

    elif lens_id == "weak_market_and_hot_sector":
        if state == "unknown":
            missing.append("research_route.market_state")
        elif stance == "defensive" or state in {"risk_closed", "retreat"}:
            counter.append(_fact("research_route.market_state", state))
        else:
            if not sector_ready:
                missing.append("research_route.sector_confirmation.ready")
            elif not sector_contract_valid:
                missing.append(
                    "research_route.sector_confirmation.confirmed_sectors"
                )
            if sector is None:
                missing.append("candidate.sector_identity")
            if not missing:
                (support if sector_confirmed else counter).append(
                    _fact(context["sector_path"], sector)
                )

    elif lens_id == "predict_confirm_follow":
        if context.get("route_status") != "research_route_ready":
            missing.append("research_route.status.research_route_ready")
        phase = str(context.get("timing_phase") or "")
        if phase in {"closed", "pre_open", "lunch_break"}:
            not_applicable = f"当前交易阶段为 {phase}"
        elif context.get("timing_ok") is None:
            missing.append("candidate.assessment.gates.timing.ok")
        elif context.get("timing_ok") is True:
            support.append(_fact(context["timing_path"], True))
        else:
            counter.append(_fact(context["timing_path"], False))

    elif lens_id == "high_energy_and_tradability":
        if state == "unknown":
            missing.append("research_route.market_state")
        elif state in {"risk_closed", "retreat"}:
            counter.append(_fact("research_route.market_state", state))
        elif state != "sentiment_rising":
            not_applicable = f"当前市场状态 {state} 不是高能确认状态"
        if not sector_ready:
            missing.append("research_route.sector_confirmation.ready")
        elif not sector_contract_valid:
            missing.append(
                "research_route.sector_confirmation.confirmed_sectors"
            )
        if sector is None:
            missing.append("candidate.sector_identity")
        elif sector_ready and sector_contract_valid and not sector_confirmed:
            counter.append(_fact(context["sector_path"], sector))
        tradable = context.get("tradable")
        execution_eligible = context.get("execution_eligible")
        if tradable is not True and tradable is not False:
            missing.append("candidate.tradable")
        if execution_eligible is not True and execution_eligible is not False:
            missing.append("candidate.assessment.execution_eligible")
        if context.get("suspended") is True:
            counter.append(_fact(context["suspended_path"], True))
        if context.get("one_price_limit") is True:
            counter.append(_fact(context["one_price_path"], True))
        if tradable is False:
            counter.append(_fact(context["tradable_path"], False))
        if execution_eligible is False:
            counter.append(_fact(context["execution_path"], False))
        if not missing and not counter and not_applicable is None:
            support.extend((
                _fact("research_route.market_state", state),
                _fact(context["sector_path"], sector),
                _fact(context["tradable_path"], tradable),
                _fact(context["execution_path"], execution_eligible),
            ))

    elif lens_id == "mode_discipline":
        allowed = context.get("allowed_playbooks") or []
        playbook = context.get("playbook")
        if not allowed:
            not_applicable = "当前研究路由没有准入战法"
        elif playbook is None:
            missing.append("candidate.playbook_id")
        elif str(playbook) in allowed:
            support.extend((
                _fact(context["playbook_path"], playbook),
                _fact("research_route.allowed_playbooks", allowed),
            ))
        else:
            counter.extend((
                _fact(context["playbook_path"], playbook),
                _fact("research_route.allowed_playbooks", allowed),
            ))

    elif lens_id == "consensus_and_error_exit":
        if not sector_ready:
            missing.append("research_route.sector_confirmation.ready")
        elif not sector_contract_valid:
            missing.append(
                "research_route.sector_confirmation.confirmed_sectors"
            )
        if sector is None:
            missing.append("candidate.sector_identity")
        elif sector_ready and sector_contract_valid and not sector_confirmed:
            counter.append(_fact(context["sector_path"], sector))
        if context.get("invalidation") is None:
            missing.append("candidate.invalidation_condition")
        if context.get("exit_condition") is None:
            missing.append("candidate.exit_condition")
        if not missing and not counter:
            support.extend((
                _fact(context["sector_path"], sector),
                _fact(context["invalidation_path"], context["invalidation"]),
                _fact(context["exit_path"], context["exit_condition"]),
            ))

    if counter:
        evidence_state = "counterevidence"
    elif missing:
        evidence_state = "missing"
    elif not_applicable is not None:
        evidence_state = "not_applicable"
    elif support:
        evidence_state = "support"
    else:
        evidence_state = "missing"
        missing.append("candidate_or_route_evidence")
    return {
        "evidence_state": evidence_state,
        "supporting_evidence": support,
        "counterevidence": counter,
        "missing_fields": sorted(set(missing)),
        "not_applicable_reason": (
            not_applicable if evidence_state == "not_applicable" else None
        ),
    }


def _condition_observation(
    *, value: Any, path: str | None, missing_field: str,
    not_applicable_reason: str | None,
) -> dict[str, Any]:
    if not_applicable_reason:
        return {
            "evidence_state": "not_applicable",
            "evidence": [], "missing_fields": [],
            "reason": not_applicable_reason,
        }
    if not isinstance(value, str) or not value.strip() or path is None:
        return {
            "evidence_state": "missing", "evidence": [],
            "missing_fields": [missing_field], "reason": None,
        }
    return {
        "evidence_state": "support", "evidence": [_fact(path, value)],
        "missing_fields": [], "reason": None,
    }


def _apply_source_use_boundary(
    evaluation: dict[str, Any], lens: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    use_status = lens.get("evidence_use_status")
    if use_status not in {"secondary_catalogue", "evidence_insufficient"}:
        return evaluation, [], None
    limitation = (
        "secondary_catalogue_not_primary_evidence"
        if use_status == "secondary_catalogue"
        else "source_provenance_or_semantics_insufficient"
    )
    if evaluation["evidence_state"] != "support":
        return evaluation, [], limitation
    observed = deepcopy(evaluation["supporting_evidence"])
    downgraded = {
        **evaluation,
        "evidence_state": "missing",
        "supporting_evidence": [],
        "missing_fields": sorted(set(
            [*evaluation["missing_fields"], "style_lens.primary_evidence"]
        )),
        "not_applicable_reason": None,
    }
    return downgraded, observed, limitation


def _missing_lens(
    lens_id: str, label: str, lens: Mapping[str, Any], reason: str,
    *, identity_trusted: bool = False,
) -> dict[str, Any]:
    grades = lens.get("evidence_grades") if identity_trusted else None
    claims = lens.get("claim_ids") if identity_trusted else None
    dispositions = lens.get("source_dispositions") if identity_trusted else None
    claim_evidence = lens.get("claim_evidence") if identity_trusted else None
    entry_observation = {
        "evidence_state": "missing",
        "supporting_evidence": [], "counterevidence": [],
        "missing_fields": [reason], "reason": None,
    }
    condition_observation = {
        "evidence_state": "missing", "evidence": [],
        "missing_fields": [reason], "reason": None,
    }
    return {
        "lens_id": lens_id,
        "lens_label": label,
        "style_name": lens.get("provenance_subject") if identity_trusted else None,
        "evidence_grades": list(grades) if isinstance(grades, list) else [],
        "claim_ids": list(claims) if isinstance(claims, list) else [],
        "source_dispositions": (
            list(dispositions) if isinstance(dispositions, list) else []
        ),
        "evidence_use_status": (
            lens.get("evidence_use_status") if identity_trusted else None
        ),
        "claim_evidence": (
            deepcopy(claim_evidence) if isinstance(claim_evidence, list) else []
        ),
        "claim_correction_dispositions": (
            list(lens.get("claim_correction_dispositions"))
            if isinstance(lens.get("claim_correction_dispositions"), list) else []
        ),
        "route_stance": lens.get("stance") if identity_trusted else None,
        "route_view": lens.get("view") if identity_trusted else None,
        "route_plain_language": (
            deepcopy(lens.get("plain_language"))
            if identity_trusted and isinstance(lens.get("plain_language"), Mapping)
            else {}
        ),
        "evidence_explanations": (
            list(lens.get("evidence_explanations"))
            if identity_trusted and isinstance(
                lens.get("evidence_explanations"), list
            ) else []
        ),
        "evidence_state": "missing",
        "evidence_state_label": _STATE_LABELS["missing"],
        "supporting_evidence": [], "counterevidence": [],
        "observed_candidate_facts": [],
        "evidence_limitation": None,
        "missing_fields": [reason], "not_applicable_reason": None,
        "entry_observation": entry_observation,
        "invalidation_observation": deepcopy(condition_observation),
        "exit_observation": deepcopy(condition_observation),
        "named_identity_signal_used": False,
        "recommendation": False,
        "decision_effect": "none",
    }


def interpret_candidate_with_trader_style_lenses(
    candidate: Mapping[str, Any], research_route: Mapping[str, Any],
) -> dict[str, Any]:
    """Explain one existing candidate without changing any decision output."""
    candidate = _mapping(candidate)
    route = _mapping(research_route)
    packet = _style_packet(route)
    context = _candidate_context(candidate, route)
    code = context.get("code")
    candidate_ready = bool(
        isinstance(code, str)
        and len(code.strip()) == 6
        and code.strip().isdigit()
        and _valid_as_of(context.get("as_of"))
    )
    packet_ready = _style_packet_valid(packet, route) and _route_authority_safe(route)
    lens_by_id = {
        str(row.get("lens_id")): row for row in (packet.get("lenses") or [])
        if isinstance(row, Mapping)
    }
    rows = []
    for lens_id, label in LENS_SPECS:
        lens = _mapping(lens_by_id.get(lens_id))
        grades = lens.get("evidence_grades")
        claims = lens.get("claim_ids")
        if not packet_ready:
            rows.append(_missing_lens(
                lens_id, label, lens, "research_route.style_lenses_authority",
            ))
            continue
        if not _lens_identity_valid(lens_id, lens, context):
            rows.append(_missing_lens(
                lens_id, label, lens, "style_lens.evidence_identity",
            ))
            continue
        if not candidate_ready:
            rows.append(_missing_lens(
                lens_id, label, lens, "candidate.code_and_as_of",
                identity_trusted=True,
            ))
            continue
        evaluation = _evaluate(lens_id, lens, context)
        evaluation, observed, limitation = _apply_source_use_boundary(
            evaluation, lens,
        )
        rows.append({
            "lens_id": lens_id,
            "lens_label": label,
            "style_name": lens["provenance_subject"],
            "evidence_grades": list(grades),
            "claim_ids": list(claims),
            "source_dispositions": list(lens["source_dispositions"]),
            "evidence_use_status": lens["evidence_use_status"],
            "claim_evidence": deepcopy(lens["claim_evidence"]),
            "claim_correction_dispositions": list(
                lens["claim_correction_dispositions"]
            ),
            "route_stance": lens.get("stance"),
            "route_view": lens.get("view"),
            "route_plain_language": deepcopy(
                lens.get("plain_language") or {}
            ),
            "evidence_explanations": list(
                lens.get("evidence_explanations") or []
            ),
            **evaluation,
            "observed_candidate_facts": observed,
            "evidence_limitation": limitation,
            "evidence_state_label": _STATE_LABELS[evaluation["evidence_state"]],
            "entry_observation": {
                "evidence_state": evaluation["evidence_state"],
                "supporting_evidence": deepcopy(evaluation["supporting_evidence"]),
                "counterevidence": deepcopy(evaluation["counterevidence"]),
                "missing_fields": list(evaluation["missing_fields"]),
                "reason": evaluation["not_applicable_reason"],
            },
            "invalidation_observation": _condition_observation(
                value=context.get("invalidation"),
                path=context.get("invalidation_path"),
                missing_field="candidate.invalidation_condition",
                not_applicable_reason=evaluation["not_applicable_reason"],
            ),
            "exit_observation": _condition_observation(
                value=context.get("exit_condition"),
                path=context.get("exit_path"),
                missing_field="candidate.exit_condition",
                not_applicable_reason=evaluation["not_applicable_reason"],
            ),
            "named_identity_signal_used": False,
            "recommendation": False,
            "decision_effect": "none",
        })

    state_counts = {
        state: sum(row["evidence_state"] == state for row in rows)
        for state in _STATE_LABELS
    }
    if not candidate_ready or not packet_ready:
        status = "candidate_style_interpretation_unavailable"
    elif any(
        row["evidence_state"] == "missing"
        or bool(row["missing_fields"])
        or row["invalidation_observation"]["evidence_state"] == "missing"
        or row["exit_observation"]["evidence_state"] == "missing"
        for row in rows
    ):
        status = "candidate_style_interpretation_partial"
    else:
        status = "candidate_style_interpretation_ready"
    return {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "candidate": {
            "code": context.get("code"),
            "name": context.get("name"),
            "as_of": context.get("as_of"),
        },
        "lens_count": len(rows),
        "lens_order": list(_LENS_IDS),
        "evidence_state_counts": state_counts,
        "lenses": rows,
        "semantics": "支持仅表示已有事实支持该研究镜头；C级与待修复来源只作二手目录或证据不足，不是买入建议、收益概率或人物能力证明。",
        "authority": dict(_AUTHORITY),
    }


__all__ = [
    "CONTRACT_VERSION",
    "LENS_SPECS",
    "interpret_candidate_with_trader_style_lenses",
]
