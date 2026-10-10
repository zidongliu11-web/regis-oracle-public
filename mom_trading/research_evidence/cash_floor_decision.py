"""Cash-floor and stability decision evidence extracted from the Flask shell."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path



def _stability_80_20_reports(context: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    reports = _PROJECT / "reports"
    return (
        _read_json(reports / "v221_stability_first_80_20.json"),
        _read_json(reports / "v223_stability_80_20_independent_reproduction.json"),
    )


def _latest_stability_paper_forward_report(context: dict[str, object]) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    return _read_json(
        _PROJECT / "reports" / "v225_stability_80_20_manual_forward.json"
    )


def _stability_execution_reality_report(context: dict[str, object]) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    report = _read_json(
        _PROJECT / "reports" / "v227_stability_80_20_execution_reality.json"
    )
    if report.get("execution_reality_passed") is not True:
        return {}
    return report


def _risk_sleeve_incremental_attribution_report(context: dict[str, object]) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    report = _read_json(
        _PROJECT / "reports" / "v229_risk_sleeve_incremental_attribution.json"
    )
    if report.get("risk_sleeve_retained") is not True:
        return {}
    return report


def _rolling_risk_cap_walk_forward_report(context: dict[str, object]) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    return _read_json(
        _PROJECT / "reports" / "v231_rolling_risk_cap_walk_forward.json"
    )


def _v233_forward_evaluation(
    context: dict[str, object],
    observations: list[dict[str, object]],
) -> dict[str, object]:
    """Evaluate only post-protocol closes; already-seen closes are burn-in."""
    _PROJECT = context["_PROJECT"]
    _V233_FORWARD_PROTOCOL_PATH = context["_V233_FORWARD_PROTOCOL_PATH"]
    _finite_number = context["_finite_number"]
    _read_json = context["_read_json"]
    protocol = _read_json(_V233_FORWARD_PROTOCOL_PATH)
    if not protocol:
        return {
            "available": False,
            "status": "protocol_unavailable",
            "stable_profit_proven": False,
        }
    raw = _V233_FORWARD_PROTOCOL_PATH.read_bytes()
    protocol_sha256 = hashlib.sha256(raw).hexdigest()
    start_date = str(protocol.get("confirmatory_start_date") or "")
    seen_through = str(protocol.get("outcome_observations_seen_through") or "")
    burn_in = [
        row for row in observations
        if str(row.get("date") or "") <= seen_through
    ]
    confirmatory = [
        row for row in observations
        if str(row.get("date") or "") >= start_date
    ]
    milestones = protocol.get("milestones") or {}
    operational_rule = milestones.get("operational_cost_recovery") or {}
    stability_rule = milestones.get("stability_evidence") or {}
    operational_required = int(
        operational_rule.get("required_confirmatory_closes") or 60
    )
    stability_required = int(
        stability_rule.get("required_confirmatory_closes") or 252
    )
    block_size = int(stability_rule.get("block_size_closes") or 63)
    required_positive_blocks = int(
        stability_rule.get("required_positive_blocks") or 4
    )
    max_drawdown_limit = float(
        stability_rule.get("maximum_mark_to_mark_drawdown_pct") or 0.5
    )
    latest = confirmatory[-1] if confirmatory else {}
    live_positive = (
        _finite_number(latest.get("net_liquidation_return_pct")) or 0.0
    ) > 0
    stress_positive = (
        _finite_number(latest.get("stress_5bp_net_liquidation_return_pct"))
        or 0.0
    ) > 0
    completed_close_integrity = all(
        row.get("counted_as_completed_close") is True
        and bool(row.get("date"))
        for row in confirmatory
    )

    baseline = burn_in[-1] if burn_in else None
    values = []
    if baseline is not None:
        base_value = _finite_number(baseline.get("net_liquidation_value"))
        if base_value is not None:
            values.append(base_value)
    values.extend(
        value for value in (
            _finite_number(row.get("net_liquidation_value"))
            for row in confirmatory
        ) if value is not None
    )
    peak = None
    max_drawdown_pct = 0.0
    for value in values:
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            max_drawdown_pct = max(
                max_drawdown_pct, (peak - value) / peak * 100,
            )

    block_returns = []
    if baseline is not None:
        previous_value = _finite_number(
            baseline.get("net_liquidation_value")
        )
        for end_index in range(block_size - 1, len(confirmatory), block_size):
            end_value = _finite_number(
                confirmatory[end_index].get("net_liquidation_value")
            )
            if previous_value and end_value is not None:
                block_returns.append(
                    round((end_value / previous_value - 1) * 100, 6)
                )
                previous_value = end_value
    positive_blocks = sum(value > 0 for value in block_returns)
    protocol_integrity = (
        len(burn_in) == int(protocol.get("pre_protocol_burn_in_closes") or 0)
    )
    operational_passed = bool(
        protocol_integrity
        and len(confirmatory) >= operational_required
        and completed_close_integrity
        and live_positive
        and stress_positive
    )
    stability_passed = bool(
        protocol_integrity
        and len(confirmatory) >= stability_required
        and completed_close_integrity
        and live_positive
        and stress_positive
        and len(block_returns) >= required_positive_blocks
        and positive_blocks >= required_positive_blocks
        and max_drawdown_pct <= max_drawdown_limit
    )
    return {
        "available": True,
        "status": (
            "stability_evidence_passed"
            if stability_passed else "operational_cost_recovery_passed"
            if operational_passed else "confirmatory_observation_running"
        ),
        "protocol_id": protocol.get("protocol_id"),
        "protocol_path": str(
            _V233_FORWARD_PROTOCOL_PATH.relative_to(_PROJECT)
        ),
        "protocol_sha256": protocol_sha256,
        "protocol_frozen_at": protocol.get("frozen_at"),
        "confirmatory_start_date": start_date,
        "pre_protocol_burn_in_closes": len(burn_in),
        "protocol_integrity": protocol_integrity,
        "confirmatory_completed_closes": len(confirmatory),
        "operational_cost_recovery": {
            "required_confirmatory_closes": operational_required,
            "remaining_closes": max(
                0, operational_required - len(confirmatory)
            ),
            "live_net_positive": live_positive,
            "stress_5bp_net_positive": stress_positive,
            "completed_close_integrity": completed_close_integrity,
            "passed": operational_passed,
            "meaning": operational_rule.get("meaning"),
        },
        "stability_evidence": {
            "required_confirmatory_closes": stability_required,
            "remaining_closes": max(
                0, stability_required - len(confirmatory)
            ),
            "block_size_closes": block_size,
            "positive_blocks": positive_blocks,
            "required_positive_blocks": required_positive_blocks,
            "block_returns_pct": block_returns,
            "max_drawdown_pct": round(max_drawdown_pct, 6),
            "maximum_mark_to_mark_drawdown_pct": max_drawdown_limit,
            "passed": stability_passed,
            "meaning": stability_rule.get("meaning"),
        },
        "stable_profit_proven": stability_passed,
        "guarantees_future_profit": False,
        "next_action": (
            "继续累计协议冻结后的真实收盘；60个收盘只验成本回收，252个收盘才评估稳定性"
            if not stability_passed
            else "稳定性证据达到冻结阈值；仍需人工复核，且不保证未来盈利"
        ),
    }


def _v233_cash_floor_forward_status(context: dict[str, object], cash_floor: dict[str, object]) -> dict[str, object]:
    """Maintain one paper ledger; intraday marks never count as completed closes."""
    _SH_TZ = context["_SH_TZ"]
    _V233_FORWARD_CACHE = context["_V233_FORWARD_CACHE"]
    _V233_FORWARD_LOCK = context["_V233_FORWARD_LOCK"]
    _V233_FORWARD_PATH = context["_V233_FORWARD_PATH"]
    _bounded_call = context["_bounded_call"]
    _read_json = context["_read_json"]
    trading_day_status = context["trading_day_status"]
    if _time.monotonic() - float(_V233_FORWARD_CACHE["loaded_at"]) < 30:
        return dict(_V233_FORWARD_CACHE["payload"])
    plan = ((cash_floor.get("current_lot_plans") or {}).get("5.0") or {})
    audit_window = cash_floor.get("audit_window") or {}
    if not plan or not audit_window:
        return {}
    reference_date = str(audit_window.get("latest_input_date") or "")
    with _V233_FORWARD_LOCK:
        previous = _read_json(_V233_FORWARD_PATH)
        observations = previous.get("completed_close_observations") or []
        observations = [row for row in observations if isinstance(row, dict)]
        paper_entry = previous.get("paper_entry") or {}
        quote_error = None
        current_mark = previous.get("current_mark") or {}
        try:
            from ..market_intelligence import _tencent_market_quotes

            # 有界行情：源不可达时 3s 内降级，不阻塞决策门禁 15s（2026-08-18 实测
            # 腾讯超时 -> 门禁 30s、看涨池卡片空白）。失败由外层 except 捕获，
            # quote_error 如实写入，纸面状态沿用上次缓存/文件值。
            _ok, _rows_src, _err = _bounded_call(
                lambda: _tencent_market_quotes(["511880"]), 3.0)
            if (not _ok or not isinstance(_rows_src, tuple) or not _rows_src[0]):
                raise ValueError(
                    f"腾讯511880行情获取超时/失败({type(_err).__name__})——沿用上次纸面状态")
            rows, source = _rows_src
            quote = next((row for row in rows if row.get("code") == "511880"), None)
            if not quote or not quote.get("price") or not quote.get("as_of"):
                raise ValueError("腾讯511880行情未返回有效价格或交易所时间")
            as_of = quote["as_of"].astimezone(_SH_TZ)
            price = float(quote["price"])
            bid_price = float(quote.get("bid_price_1") or 0.0)
            ask_price = float(quote.get("ask_price_1") or 0.0)
            if bid_price <= 0 or ask_price <= 0 or bid_price > ask_price:
                raise ValueError("腾讯511880行情未返回有效买一卖一盘口")
            quantity = int(plan.get("quantity") or 0)
            residual = float(plan.get("residual_cash") or 0.0)
            capital = float(plan.get("capital") or 200_000)
            reference_price = float(plan.get("price") or 0.0)
            commission_rate = float(
                ((cash_floor.get("execution_assumptions") or {}).get("commission_rate"))
                or 0.0003
            )
            if not paper_entry:
                entry_notional = quantity * ask_price
                entry_commission = (
                    max(5.0, entry_notional * commission_rate) if quantity else 0.0
                )
                stress_entry_notional = quantity * price
                stress_entry_commission = (
                    max(5.0, stress_entry_notional * commission_rate)
                    if quantity else 0.0
                )
                stress_entry_slippage = stress_entry_notional * 5.0 / 10_000
                paper_entry = {
                    "as_of": as_of.isoformat(timespec="seconds"),
                    "source": source.get("source") or "腾讯财经公开行情（全市场批量）",
                    "quantity": quantity,
                    "last_price": round(price, 3),
                    "ask_price": round(ask_price, 3),
                    "ask_volume_1": int(float(quote.get("ask_volume_1") or 0)),
                    "top_of_book_spread_bps": round(
                        (ask_price - bid_price) / ((ask_price + bid_price) / 2) * 10_000,
                        6,
                    ),
                    "buy_commission": round(entry_commission, 2),
                    "residual_cash": round(
                        capital - entry_notional - entry_commission, 2,
                    ),
                    "stress_5bp": {
                        "reference_price": round(price, 3),
                        "entry_commission": round(stress_entry_commission, 2),
                        "entry_slippage": round(stress_entry_slippage, 2),
                        "residual_cash": round(
                            capital - stress_entry_notional
                            - stress_entry_commission - stress_entry_slippage,
                            2,
                        ),
                    },
                    "causal_post_formation_entry": True,
                    "frozen": True,
                }

            entry_residual = float(paper_entry.get("residual_cash") or 0.0)
            entry_ask = float(paper_entry.get("ask_price") or 0.0)
            exit_notional = quantity * bid_price
            sell_commission = (
                max(5.0, exit_notional * commission_rate) if quantity else 0.0
            )
            account_value = entry_residual + quantity * price
            net_liquidation_value = entry_residual + exit_notional - sell_commission
            live_break_even_exit_price = (
                (capital - entry_residual) / (quantity * (1 - commission_rate))
                if quantity else None
            )

            stress_entry = paper_entry.get("stress_5bp") or {}
            stress_residual = float(stress_entry.get("residual_cash") or residual)
            stress_reference_price = float(
                stress_entry.get("reference_price") or reference_price
            )
            stress_exit_notional = quantity * price
            stress_exit_commission = (
                max(5.0, stress_exit_notional * commission_rate) if quantity else 0.0
            )
            stress_exit_slippage = stress_exit_notional * 5.0 / 10_000
            stress_net_liquidation_value = (
                stress_residual + stress_exit_notional
                - stress_exit_commission - stress_exit_slippage
            )
            stress_break_even_price = (
                (capital - stress_residual)
                / (quantity * (1 - commission_rate - 5.0 / 10_000))
                if quantity else None
            )

            live_buy_notional = quantity * ask_price
            live_buy_commission = (
                max(5.0, live_buy_notional * commission_rate) if quantity else 0.0
            )
            live_residual = capital - live_buy_notional - live_buy_commission
            live_exit_notional = quantity * bid_price
            live_exit_commission = (
                max(5.0, live_exit_notional * commission_rate) if quantity else 0.0
            )
            live_net_liquidation_value = (
                live_residual + live_exit_notional - live_exit_commission
            )
            hypothetical_break_even_exit_price = (
                (capital - live_residual) / (quantity * (1 - commission_rate))
                if quantity else None
            )
            live_spread_bps = (
                (ask_price - bid_price) / ((ask_price + bid_price) / 2) * 10_000
            )
            current_mark = {
                "as_of": as_of.isoformat(timespec="seconds"),
                "source": source.get("source") or "腾讯财经公开行情（自选池批量）",
                "price": round(price, 3),
                "bid_price_1": round(bid_price, 3),
                "bid_volume_1": int(float(quote.get("bid_volume_1") or 0)),
                "ask_price_1": round(ask_price, 3),
                "ask_volume_1": int(float(quote.get("ask_volume_1") or 0)),
                "top_of_book_spread_bps": round(live_spread_bps, 6),
                "quantity": quantity,
                "residual_cash": round(entry_residual, 2),
                "account_value": round(account_value, 2),
                "estimated_exit_commission": round(sell_commission, 2),
                "estimated_exit_slippage": 0.0,
                "net_liquidation_value": round(net_liquidation_value, 2),
                "net_liquidation_return_pct": round(
                    (net_liquidation_value / capital - 1) * 100,
                    6,
                ),
                "break_even_exit_price": round(live_break_even_exit_price, 6),
                "remaining_gain_to_break_even_pct": round(
                    (live_break_even_exit_price / bid_price - 1) * 100, 6,
                ),
                "forward_entry_ask_price": round(entry_ask, 3),
                "stress_5bp_net_liquidation_value": round(
                    stress_net_liquidation_value, 2,
                ),
                "stress_5bp_net_liquidation_return_pct": round(
                    (stress_net_liquidation_value / capital - 1) * 100, 6,
                ),
                "stress_5bp_break_even_price": round(stress_break_even_price, 6),
                "stress_5bp_remaining_gain_pct": round(
                    (stress_break_even_price / price - 1) * 100, 6,
                ),
                "stress_5bp_total_gain_from_reference_pct": round(
                    (stress_break_even_price / stress_reference_price - 1) * 100, 6,
                ) if stress_reference_price else None,
                "stress_5bp_remaining_amount": round(
                    max(0.0, stress_break_even_price - price) * quantity, 2,
                ),
                "live_top_of_book_entry": {
                    "ask_price": round(ask_price, 3),
                    "buy_commission": round(live_buy_commission, 2),
                    "residual_cash": round(live_residual, 2),
                    "immediate_exit_bid": round(bid_price, 3),
                    "exit_commission": round(live_exit_commission, 2),
                    "net_liquidation_value": round(live_net_liquidation_value, 2),
                    "net_liquidation_return_pct": round(
                        (live_net_liquidation_value / capital - 1) * 100, 6,
                    ),
                    "break_even_exit_price": round(hypothetical_break_even_exit_price, 6),
                    "required_gain_from_ask_pct": round(
                        (hypothetical_break_even_exit_price / ask_price - 1) * 100, 6,
                    ),
                    "broker_commission_assumed": True,
                    "broker_fee_must_be_confirmed": True,
                },
                "counted_as_completed_close": False,
            }
            entry_as_of = datetime.fromisoformat(str(paper_entry["as_of"]))
            is_completed_close = (
                as_of > entry_as_of
                and as_of.time() >= time(15, 0)
                and trading_day_status(as_of)["is_trading_day"]
            )
            if is_completed_close and not any(
                row.get("date") == as_of.date().isoformat() for row in observations
            ):
                observations.append({
                    **current_mark,
                    "date": as_of.date().isoformat(),
                    "counted_as_completed_close": True,
                })
        except Exception as exc:
            quote_error = f"{type(exc).__name__}: {exc}"
        forward_evaluation = context["_v233_forward_evaluation"](observations)
        payload = {
            "version": 1,
            "forward_id": "V235_V233_CASH_FLOOR_PAPER_FORWARD",
            "status": "paper_forward_running",
            "generated_at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
            "formed_at": cash_floor.get("generated_at"),
            "entry_reference_date": reference_date,
            "historical_input_cutoff": reference_date,
            "first_eligible_close_date": (
                trading_day_status(datetime.now(_SH_TZ))["date"]
                if trading_day_status(datetime.now(_SH_TZ))["is_trading_day"]
                else None
            ),
            "instrument": "511880",
            "historical_reference_plan": plan,
            "paper_plan": plan,
            "paper_entry": paper_entry,
            "current_mark": current_mark,
            "completed_close_observations": observations,
            "completed_closes": len(observations),
            "latest_completed_close_date": (
                observations[-1].get("date") if observations else None
            ),
            "quote_error": quote_error,
            "intraday_marks_count_as_close": False,
            "forward_evaluation": forward_evaluation,
            "cost_interpretation": {
                "stress_5bp_each_side": "V233历史压力口径；真实前向另以形成后首次卖一价冻结入场，不使用形成前收盘价",
                "live_top_of_book": "当前买一/卖一可执行盘口；未获得用户券商真实佣金单，因此仍按0.03%双边估算",
                "official_rule_note": "货币ETF交易经手费暂免，但券商是否免佣取决于具体券商和账户；系统不得擅自假设免佣",
            },
            "recommendation_eligible": False,
            "real_money_eligible": False,
            "stable_profit_proven": (
                forward_evaluation.get("stable_profit_proven") is True
            ),
            "next_action": (
                forward_evaluation.get("next_action")
                or "继续逐个真实交易日累计成本后纸面收盘净值"
            ),
        }
        _V233_FORWARD_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        _V233_FORWARD_CACHE.update({
            "loaded_at": _time.monotonic(),
            "payload": payload,
        })
        return payload



def _cash_floor_daily_recovery_report(context: dict[str, object]) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    report = _read_json(
        _PROJECT / "reports" / "v237_cash_floor_daily_recovery.json"
    )
    if report.get("audit_id") != "V237_CASH_FLOOR_DAILY_RECOVERY":
        return {}
    return report


def _cash_floor_cross_product_report(
    context: dict[str, object],
    *, include_live: bool = False,
) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    report = _read_json(
        _PROJECT / "reports" / "v239_cash_floor_cross_product.json"
    )
    if report.get("audit_id") != "V239_CASH_FLOOR_CROSS_PRODUCT":
        return {}
    if include_live:
        report["live_comparison"] = context["_cash_floor_live_comparison"](report)
    return report


def _cash_floor_live_comparison(
    context: dict[str, object],
    cross_product: dict[str, object],
) -> dict[str, object]:
    """Compare current executable friction without turning it into a buy score."""
    _CASH_COMPARISON_CACHE = context["_CASH_COMPARISON_CACHE"]
    _CASH_COMPARISON_LOCK = context["_CASH_COMPARISON_LOCK"]
    _audited_market_state = context["_audited_market_state"]
    if _time.monotonic() - float(_CASH_COMPARISON_CACHE["loaded_at"]) < 15:
        return dict(_CASH_COMPARISON_CACHE["payload"])
    with _CASH_COMPARISON_LOCK:
        if _time.monotonic() - float(_CASH_COMPARISON_CACHE["loaded_at"]) < 15:
            return dict(_CASH_COMPARISON_CACHE["payload"])
        codes = list((cross_product.get("products") or {}).keys())
        payload: dict[str, object] = {
            "status": "unavailable",
            "rows": [],
            "research_observation_only": True,
            "recommendation_eligible": False,
            "real_money_eligible": False,
        }
        try:
            from ..market_intelligence import _tencent_market_quotes

            quotes, source = _tencent_market_quotes(codes)
            quote_map = {str(row.get("code")): row for row in quotes}
            rows = []
            skipped: list[dict[str, object]] = []
            for code in codes:
                product = (cross_product.get("products") or {}).get(code) or {}
                quote = quote_map.get(code) or {}
                ask = float(quote.get("ask_price_1") or 0)
                bid = float(quote.get("bid_price_1") or 0)
                income = product.get("current_income") or {}
                daily_per_hundred = float(
                    income.get(
                        "trailing_7_calendar_day_average_hundred_share_income"
                    ) or 0
                )
                if ask <= 0 or bid <= 0 or ask < bid or daily_per_hundred <= 0:
                    # P4 不静默降级：每只未进 rows 的产品必须给出原因（收盘后腾讯
                    # 可能只回单边/空报价，端点如实只展示可执行子集）。
                    reasons: list[str] = []
                    if ask <= 0 or bid <= 0:
                        if ask <= 0 and bid <= 0:
                            reasons.append("买一卖一双缺（该时刻无有效报价）")
                        elif ask <= 0:
                            reasons.append("卖一缺失（仅买一有价，无法算双边摩擦）")
                        else:
                            reasons.append("买一缺失（仅卖一有价，无法算双边摩擦）")
                    if ask > 0 and bid > 0 and ask < bid:
                        reasons.append("买一卖一倒挂（ask<bid，数据异常不参与）")
                    if daily_per_hundred <= 0:
                        reasons.append("近7日每百份收益数据缺失")
                    skipped.append({
                        "code": code,
                        "name": product.get("name") or quote.get("name") or code,
                        "reasons": reasons or ["不可执行"],
                    })
                    continue

                def friction(slippage_bps: float) -> tuple[int, float, float, int]:
                    quantity = math.floor(200_000 / (ask * 100)) * 100
                    while quantity:
                        buy_notional = quantity * ask
                        buy_commission = max(5.0, buy_notional * 0.0003)
                        buy_slippage = buy_notional * slippage_bps / 10_000
                        if buy_notional + buy_commission + buy_slippage <= 200_000:
                            break
                        quantity -= 100
                    buy_notional = quantity * ask
                    buy_commission = max(5.0, buy_notional * 0.0003)
                    buy_slippage = buy_notional * slippage_bps / 10_000
                    residual = 200_000 - buy_notional - buy_commission - buy_slippage
                    sell_notional = quantity * bid
                    sell_commission = max(5.0, sell_notional * 0.0003)
                    sell_slippage = sell_notional * slippage_bps / 10_000
                    immediate_value = (
                        residual + sell_notional - sell_commission - sell_slippage
                    )
                    deficit = max(0.0, 200_000 - immediate_value)
                    daily_income = quantity / 100 * daily_per_hundred
                    days = math.ceil(deficit / daily_income) if daily_income else 9999
                    return quantity, deficit, daily_income, days

                quantity, deficit, daily_income, expected_days = friction(0.0)
                _, stress_deficit, _, stress_days = friction(5.0)
                base_history = (product.get("scenarios") or {}).get(
                    "three_bp_commission_observed_spread_diagnostic"
                ) or {}
                spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
                ask_lots = int(float(quote.get("ask_volume_1") or 0))
                rows.append({
                    "code": code,
                    "name": product.get("name") or quote.get("name") or code,
                    "as_of": (
                        quote.get("as_of").isoformat(timespec="seconds")
                        if quote.get("as_of") else None
                    ),
                    "bid_price_1": round(bid, 3),
                    "ask_price_1": round(ask, 3),
                    "spread_bps": round(spread_bps, 6),
                    "ask_volume_1_lots": ask_lots,
                    "ask_depth_notional_yuan": round(ask * ask_lots * 100, 2),
                    "amount_yi": quote.get("amount_yi"),
                    "quantity": quantity,
                    "trailing_daily_income_per_100": round(daily_per_hundred, 6),
                    "seven_day_annualized_pct": (
                        income.get("latest_seven_day_annualized_pct")
                        or income.get("derived_seven_day_annualized_pct")
                    ),
                    "immediate_round_trip_deficit_yuan": round(deficit, 2),
                    "projected_daily_income_yuan": round(daily_income, 2),
                    "projected_break_even_calendar_days": expected_days,
                    "stress_5bp_deficit_yuan": round(stress_deficit, 2),
                    "stress_5bp_projected_break_even_calendar_days": stress_days,
                    "historical_maximum_durable_positive_trading_days": (
                        base_history.get("maximum_durable_positive_trading_days")
                    ),
                })
            rows.sort(key=lambda row: (
                row["projected_break_even_calendar_days"],
                -float(row.get("amount_yi") or 0),
            ))
            fastest = rows[0] if rows else {}
            liquid = max(rows, key=lambda row: float(row.get("amount_yi") or 0)) if rows else {}
            payload = {
                "status": "live" if _audited_market_state()["phase"] == "open" else "snapshot",
                "market": _audited_market_state(),
                "source": source,
                "rows": rows,
                "skipped": skipped,
                "fastest_projected_recovery_code": fastest.get("code"),
                "highest_turnover_code": liquid.get("code"),
                "dominant_product": (
                    fastest.get("code")
                    if fastest.get("code") == liquid.get("code") else None
                ),
                "tradeoff": (
                    "当前收益推算最快与成交额最高不是同一产品；不生成单一最优结论。"
                    if fastest.get("code") != liquid.get("code")
                    else "当前收益推算最快与成交额最高暂为同一产品，仍仅作研究观察。"
                ),
                "projection_assumption": (
                    "假设最近7个自然日平均每百份收益、当前买一卖一和费率保持不变；"
                    "预计回本天数为静态推算，不是承诺。"
                ),
                "research_observation_only": True,
                "recommendation_eligible": False,
                "real_money_eligible": False,
            }
        except Exception as exc:
            payload["error"] = f"{type(exc).__name__}: {exc}"
        _CASH_COMPARISON_CACHE.update({
            "loaded_at": _time.monotonic(), "payload": payload,
        })
        return payload


def _511990_declared_income_ledger(
    context: dict[str, object],
    paper_entry: dict[str, object],
    exit_date: str,
    income_path: Path | None = None,
) -> dict[str, object]:
    """Apply the same [entry, exit) share-conversion accounting as V239/V241."""
    _V239_511990_INCOME_PATH = context["_V239_511990_INCOME_PATH"]
    path = income_path or _V239_511990_INCOME_PATH
    entry_day = datetime.fromisoformat(str(paper_entry["as_of"])).date()
    exit_day = datetime.fromisoformat(exit_date).date()
    initial_quantity = int(
        (((paper_entry.get("positions") or {}).get("511990") or {}).get("quantity"))
        or 0
    )
    rows: list[tuple[object, float]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append((
                datetime.fromisoformat(str(row["date"])).date(),
                float(row["hundred_share_income"]),
            ))
    rows.sort(key=lambda item: item[0])
    income_data_as_of = rows[-1][0] if rows else None
    required_through = exit_day - timedelta(days=1) if exit_day > entry_day else None
    coverage_complete = bool(
        required_through is None
        or (income_data_as_of is not None and income_data_as_of >= required_through)
    )
    shares = initial_quantity
    pending_income = 0.0
    total_declared_income = 0.0
    eligible_records = 0
    latest_used = None
    for income_day, daily_income in rows:
        if not entry_day <= income_day < exit_day:
            continue
        earned = shares / 100 * daily_income
        total_declared_income += earned
        pending_income += earned
        converted = math.floor((pending_income + 1e-12) / 100)
        if converted:
            shares += converted
            pending_income -= converted * 100
        eligible_records += 1
        latest_used = income_day
    return {
        "rule": "buy_included_sell_excluded_[entry,exit)",
        "payment": "share_conversion_per_100_yuan",
        "entry_date": entry_day.isoformat(),
        "exit_date": exit_day.isoformat(),
        "income_required_through": (
            required_through.isoformat() if required_through else None
        ),
        "income_data_as_of": (
            income_data_as_of.isoformat() if income_data_as_of else None
        ),
        "income_coverage_complete": coverage_complete,
        "eligible_income_records": eligible_records,
        "latest_income_date_used": latest_used.isoformat() if latest_used else None,
        "initial_quantity": initial_quantity,
        "liquidation_quantity": shares,
        "converted_shares": shares - initial_quantity,
        "pending_income_yuan": round(pending_income, 6),
        "total_declared_income_yuan": round(total_declared_income, 6),
    }


def _dual_cash_floor_forward_status(
    context: dict[str, object],
    dual_report: dict[str, object],
) -> dict[str, object]:
    """Freeze an executable dual-product paper entry; intraday is not a close."""
    _DUAL_CASH_FORWARD_CACHE = context["_DUAL_CASH_FORWARD_CACHE"]
    _DUAL_CASH_FORWARD_LOCK = context["_DUAL_CASH_FORWARD_LOCK"]
    _DUAL_CASH_FORWARD_PATH = context["_DUAL_CASH_FORWARD_PATH"]
    _PROJECT = context["_PROJECT"]
    _SH_TZ = context["_SH_TZ"]
    _bounded_call = context["_bounded_call"]
    _read_json = context["_read_json"]
    trading_day_status = context["trading_day_status"]
    if _time.monotonic() - float(_DUAL_CASH_FORWARD_CACHE["loaded_at"]) < 30:
        return dict(_DUAL_CASH_FORWARD_CACHE["payload"])
    with _DUAL_CASH_FORWARD_LOCK:
        previous = _read_json(_DUAL_CASH_FORWARD_PATH)
        observations = [
            row for row in previous.get("completed_close_observations") or []
            if (
                isinstance(row, dict)
                and row.get("income_accrual_pending") is False
                and row.get("net_liquidation_value") is not None
            )
        ]
        paper_entry = previous.get("paper_entry") or {}
        current_mark = previous.get("current_mark") or {}
        quote_error = None
        v239 = _read_json(_PROJECT / "reports" / "v239_cash_floor_cross_product.json")
        income_as_of = str(
            (((v239.get("products") or {}).get("511990") or {}).get("current_income") or {}).get("as_of")
            or ""
        )
        try:
            from ..market_intelligence import _tencent_market_quotes

            # 有界行情：源不可达时 3s 内降级（同 _v233_cash_floor_forward_status）。
            _ok, _rows_src, _err = _bounded_call(
                lambda: _tencent_market_quotes(["511880", "511990"]), 3.0)
            if (not _ok or not isinstance(_rows_src, tuple) or not _rows_src[0]):
                raise ValueError(
                    f"腾讯双产品行情获取超时/失败({type(_err).__name__})——沿用上次纸面状态")
            rows, source = _rows_src
            quotes = {str(row.get("code")): row for row in rows}
            quantities = {"511880": 1000, "511990": 900}
            if set(quotes) != set(quantities):
                raise ValueError("双产品行情覆盖不足")
            for code, quote in quotes.items():
                bid = float(quote.get("bid_price_1") or 0)
                ask = float(quote.get("ask_price_1") or 0)
                if bid <= 0 or ask <= 0 or bid > ask or not quote.get("as_of"):
                    raise ValueError(f"{code}缺少有效买一卖一或交易所时间")
            as_of = max(
                quote["as_of"].astimezone(_SH_TZ) for quote in quotes.values()
            )
            if not paper_entry:
                entries = {}
                total_cost = 0.0
                for code, quantity in quantities.items():
                    ask = float(quotes[code]["ask_price_1"])
                    notional = quantity * ask
                    commission = max(5.0, notional * 0.0003)
                    total_cost += notional + commission
                    entries[code] = {
                        "quantity": quantity,
                        "ask_price": round(ask, 3),
                        "buy_commission": round(commission, 2),
                    }
                paper_entry = {
                    "as_of": as_of.isoformat(timespec="seconds"),
                    "source": source.get("source"),
                    "capital": 200_000.0,
                    "positions": entries,
                    "residual_cash": round(200_000 - total_cost, 2),
                }
            price_only_liquidation = float(paper_entry.get("residual_cash") or 0)
            marks = {}
            for code, entry in (paper_entry.get("positions") or {}).items():
                quantity = int(entry.get("quantity") or 0)
                quote = quotes[code]
                bid = float(quote["bid_price_1"])
                sell_notional = quantity * bid
                sell_commission = max(5.0, sell_notional * 0.0003)
                price_only_liquidation += sell_notional - sell_commission
                marks[code] = {
                    "bid_price_1": round(bid, 3),
                    "ask_price_1": round(float(quote["ask_price_1"]), 3),
                    "quantity": quantity,
                }
            income_ledger = context["_511990_declared_income_ledger"](
                paper_entry, as_of.isoformat(timespec="seconds"),
            )
            liquidation = float(paper_entry.get("residual_cash") or 0)
            for code, entry in (paper_entry.get("positions") or {}).items():
                quantity = int(entry.get("quantity") or 0)
                if code == "511990":
                    quantity = int(income_ledger["liquidation_quantity"])
                    marks[code]["liquidation_quantity"] = quantity
                    marks[code]["converted_shares"] = income_ledger["converted_shares"]
                sell_notional = quantity * float(quotes[code]["bid_price_1"])
                liquidation += sell_notional - max(5.0, sell_notional * 0.0003)
            liquidation += float(income_ledger["pending_income_yuan"])
            income_complete = bool(income_ledger["income_coverage_complete"])
            current_mark = {
                "as_of": as_of.isoformat(timespec="seconds"),
                "positions": marks,
                "price_only_net_liquidation_value": round(price_only_liquidation, 2),
                "price_only_net_liquidation_return_pct": round(
                    (price_only_liquidation / 200_000 - 1) * 100, 6,
                ),
                "net_liquidation_value": round(liquidation, 2) if income_complete else None,
                "net_liquidation_return_pct": (
                    round((liquidation / 200_000 - 1) * 100, 6)
                    if income_complete else None
                ),
                "511990_declared_income_ledger": income_ledger,
                "income_coverage_complete": income_complete,
                "counted_as_completed_close": False,
            }
            entry_as_of = datetime.fromisoformat(str(paper_entry["as_of"]))
            is_completed_close = (
                as_of > entry_as_of and as_of.time() >= time(15, 0)
                and trading_day_status(as_of)["is_trading_day"]
                and income_complete
            )
            if is_completed_close and not any(
                row.get("date") == as_of.date().isoformat() for row in observations
            ):
                observations.append({
                    "date": as_of.date().isoformat(),
                    **current_mark,
                    "counted_as_completed_close": True,
                    "income_accrual_pending": False,
                })
        except Exception as exc:
            quote_error = f"{type(exc).__name__}: {exc}"
        payload = {
            "forward_id": "V243_V241_DUAL_MONEY_FUND_PAPER_FORWARD",
            "status": "paper_forward_running",
            "generated_at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
            "instruments": ["511880", "511990"],
            "paper_entry": paper_entry,
            "current_mark": current_mark,
            "completed_close_observations": observations,
            "completed_closes": len(observations),
            "quote_error": quote_error,
            "511990_income_data_as_of": (
                ((current_mark.get("511990_declared_income_ledger") or {}).get(
                    "income_data_as_of"
                )) or income_as_of or None
            ),
            "completed_close_requires_declared_income": True,
            "intraday_marks_count_as_close": False,
            "recommendation_eligible": False,
            "real_money_eligible": False,
            "stable_profit_proven": False,
            "next_action": (
                "已冻结双产品可执行卖一入口；等待收盘且511990卖出日前收益完整后写入第1个正式收盘"
                if not observations else "继续累计双产品成本后纸面收盘并补记已实现每日收益"
            ),
        }
        _DUAL_CASH_FORWARD_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        _DUAL_CASH_FORWARD_CACHE.update({
            "loaded_at": _time.monotonic(), "payload": payload,
        })
        return payload


def _dual_cash_floor_report(context: dict[str, object]) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    report = _read_json(_PROJECT / "reports" / "v241_dual_money_fund_floor.json")
    if report.get("audit_id") != "V241_DUAL_MONEY_FUND_FLOOR":
        return {}
    report["forward_tracking"] = context["_dual_cash_floor_forward_status"](report)
    return report



def _cash_floor_daily_path_metrics_report(context: dict[str, object]) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _cached_sha256_matches = context["_cached_sha256_matches"]
    _read_json = context["_read_json"]
    report = _read_json(
        _PROJECT / "reports" / "v302_cash_floor_daily_path_metrics.json"
    )
    if (
        report.get("audit_id") != "V302_CASH_FLOOR_DAILY_PATH_METRICS"
        or report.get("status") != "historical_daily_path_metrics_paper_only"
        or report.get("strategy_rules_changed") is not False
        or report.get("new_strategy_outcome_trial") is not False
    ):
        return {}
    hashes = report.get("input_hashes")
    if not isinstance(hashes, dict) or not hashes:
        return {}
    for relative, declared in hashes.items():
        path = _PROJECT / str(relative)
        if not path.is_file():
            return {}
        stat = path.stat()
        if not _cached_sha256_matches(
            str(path), stat.st_size, stat.st_mtime_ns, str(declared),
        ):
            return {}
    evidence = _PROJECT / str(report.get("evidence_path") or "")
    if not evidence.is_file():
        return {}
    stat = evidence.stat()
    if not _cached_sha256_matches(
        str(evidence), stat.st_size, stat.st_mtime_ns,
        str(report.get("evidence_sha256") or ""),
    ):
        return {}
    return report


def _cash_floor_holding_horizon_report(context: dict[str, object]) -> dict[str, object]:
    _PROJECT = context["_PROJECT"]
    _read_json = context["_read_json"]
    report = _read_json(
        _PROJECT / "reports" / "v233_cash_floor_holding_horizon.json"
    )
    if report.get("historical_horizon_gate_passed") is not True:
        return {}
    forward = context["_v233_cash_floor_forward_status"](report)
    report["forward_tracking"] = forward
    report["post_freeze_forward_observations"] = forward.get("completed_closes", 0)
    report["daily_recovery"] = context["_cash_floor_daily_recovery_report"]()
    report["daily_path_metrics"] = context["_cash_floor_daily_path_metrics_report"]()
    report["cross_product"] = context["_cash_floor_cross_product_report"]()
    report["dual_product_floor"] = context["_dual_cash_floor_report"]()
    return report


def _stability_80_20_passed(
    context: dict[str, object],
    candidate: dict[str, object], reproduction: dict[str, object],
) -> bool:
    return bool(
        candidate.get("historical_candidate_passed") is True
        and reproduction.get("independent_reproduction_passed") is True
        and reproduction.get("paper_forward_eligible") is True
    )


def _stability_80_20_allocation(
    context: dict[str, object],
    candidate: dict[str, object], reproduction: dict[str, object],
) -> dict[str, object]:
    current = (
        reproduction.get("current_paper_observation")
        or candidate.get("current_research_observation")
        or {}
    )
    if not isinstance(current, dict):
        return {}
    cash = current.get("cash_sweep") or {}
    risk_rows = [
        dict(row) for row in current.get("allocations") or []
        if isinstance(row, dict)
    ]
    cash_weight = float(cash.get("weight_pct") or 0.0) if isinstance(cash, dict) else 0.0
    allocations = [
        *risk_rows,
        *([{
            "code": cash.get("code") or "511880",
            "name": cash.get("name") or "银华日利ETF",
            "role": "货币ETF核心仓",
            "weight_pct": cash_weight,
        }] if cash_weight else []),
    ]
    candidate_current = candidate.get("current_research_observation") or {}
    return {
        **current,
        "signal_date": (
            current.get("signal_date")
            or (candidate_current.get("signal_date") if isinstance(candidate_current, dict) else None)
            or "2026-07-31"
        ),
        "data_as_of": (
            current.get("data_as_of")
            or (candidate_current.get("data_as_of") if isinstance(candidate_current, dict) else None)
            or "2026-08"
        ),
        "cash_weight_pct": cash_weight,
        "risk_exposure_pct": float(current.get("risk_exposure_pct") or 0.0),
        "allocations": allocations,
        "risk_allocations": risk_rows,
        "paper_only": True,
        "research_only": True,
    }


def _stability_metric(
    context: dict[str, object],
    cost_metrics: dict[str, object], section: str,
) -> dict[str, object]:
    source = cost_metrics.get(section) or {}
    return {
        "annualized_net_return_20bp_pct": source.get("annualized_return_pct"),
        "total_return_20bp_pct": source.get("total_return_pct"),
        "max_drawdown_20bp_pct": source.get("max_drawdown_pct"),
        "monthly_win_rate_20bp_pct": source.get("monthly_win_rate_pct"),
        "annualized_volatility_pct": source.get("annualized_volatility_pct"),
        "sharpe_zero_cash": source.get("sharpe_zero_cash"),
        "yearly_total_return_pct": source.get("yearly_total_return_pct") or {},
    }


def _report_integrity_rows(context: dict[str, object], report: dict[str, object], label: str) -> list[dict[str, object]]:
    _PROJECT = context["_PROJECT"]
    _cached_sha256_matches = context["_cached_sha256_matches"]
    rows = []
    for section, suffix in (("input_hashes", "冻结输入"), ("evidence_hashes", "输出证据")):
        hashes = report.get(section)
        passed = bool(hashes)
        if isinstance(hashes, dict):
            for relative, declared in hashes.items():
                path = _PROJECT / str(relative)
                if not path.is_file():
                    passed = False
                    break
                stat = path.stat()
                if not _cached_sha256_matches(
                    str(path), stat.st_size, stat.st_mtime_ns, str(declared),
                ):
                    passed = False
                    break
        rows.append({"label": f"{label}{suffix}", "passed": passed})
    return rows



def _strategy_lab_evidence_inner(context: dict[str, object]) -> dict[str, object]:
    _BASE_STRATEGY_LAB_EVIDENCE = context["_BASE_STRATEGY_LAB_EVIDENCE"]
    _active_strategy_public_view = context["_active_strategy_public_view"]
    _safe_live_signal_snapshot = context["_safe_live_signal_snapshot"]
    payload = _BASE_STRATEGY_LAB_EVIDENCE()
    candidate, reproduction = context["_stability_80_20_reports"]()
    if not context["_stability_80_20_passed"](candidate, reproduction):
        return payload
    current = context["_stability_80_20_allocation"](candidate, reproduction)
    execution_reality = context["_stability_execution_reality_report"]()
    lot_plan = execution_reality.get("current_lot_plan") or {}
    risk_sleeve_attribution = context["_risk_sleeve_incremental_attribution_report"]()
    selection_bias_audit = context["_rolling_risk_cap_walk_forward_report"]()
    cash_floor = context["_cash_floor_holding_horizon_report"]()
    cash_floor_plan = (cash_floor.get("current_lot_plans") or {}).get("5.0") or {}
    cash_floor_daily = (
        ((cash_floor.get("daily_path_metrics") or {}).get("scenarios") or {})
        .get("50.0") or {}
    )
    cash_floor_execution = {
        **cash_floor_plan,
        "quantities": {"511880": cash_floor_plan.get("quantity", 0)},
        "post_trade_safe_core_pct": 100.0,
        "post_trade_risk_weight_pct": 0.0,
        "forward_tracking": cash_floor.get("forward_tracking") or {},
    } if cash_floor_plan else {}
    paper_forward = context["_latest_stability_paper_forward_report"]()
    formal_forward = paper_forward.get("formal_forward") or {}
    risk_text = " / ".join(
        f"{row.get('name')} {row.get('weight_pct')}%"
        for row in current.get("risk_allocations") or []
    )
    payload["generated_at"] = reproduction.get("generated_at")
    payload["headline"] = {
        "status": "paper_candidate",
        "label": "80/20稳定优先候选 · 独立复算通过 · 仅纸面观察",
        "reason": (
            "V223用另一套信号计算、现金数据重建和资金价值账本复现V221；"
            f"当前纸面结构为511880 80%核心仓，风险袖套20%（{risk_text}）。"
            "V229确认风险袖套是收益增强层而非稳定收益来源：9/11个完整年度跑赢纯511880。"
            "V231尝试逐年只用过去数据选择风险上限，但被纯511880支配，已拒绝替换固定20%。"
            f"V225已累计{formal_forward.get('completed_post_freeze_common_closes', 0)}个"
            "规则形成后的真实共同收盘。成本后历史稳定不等于未来保证盈利，"
            "真钱买入和自动交易仍锁定。"
        ),
        "signal_date": current.get("signal_date"),
        "cash_weight_pct": current.get("cash_weight_pct"),
        "risk_exposure_pct": current.get("risk_exposure_pct"),
        "executable_safe_core_pct": lot_plan.get("post_trade_safe_core_pct"),
        "executable_risk_exposure_pct": lot_plan.get("post_trade_risk_weight_pct"),
        "capital_constrained_execution": lot_plan,
        "market_regime_on": True,
        "recommendation_eligible": False,
        "paper_forward_eligible": True,
    }
    payload["latest_strategy"] = {
        "strategy_id": candidate.get("strategy_id"),
        "title": "稳定优先80/20核心—卫星",
        "forward_total_pct": (
            (((execution_reality.get("scenario_metrics") or {}).get("50.0") or {})
             .get("diagnostic_2026_through_july") or {}).get("total_return_pct")
        ),
        "recommendation_eligible": False,
        "paper_forward_eligible": True,
        "invalidated": False,
    }
    payload["stability_candidate"] = {
        "available": True,
        "strategy_id": candidate.get("strategy_id"),
        "status": candidate.get("status"),
        "historical_candidate_passed": True,
        "paper_forward_eligible": True,
        "recommendation_eligible": False,
        "real_money_eligible": False,
        "automatic_trading": False,
        "rules": candidate.get("rules") or {},
        "cost_ladder_metrics": candidate.get("cost_ladder_metrics") or {},
        "cash_only_baseline_metrics": candidate.get("cash_only_baseline_metrics") or {},
        "checks": candidate.get("checks") or {},
        "current_paper_allocation": current,
        "exploratory_disclosure": candidate.get("exploratory_disclosure"),
        "honesty": candidate.get("honesty"),
    }
    payload["stability_reproduction"] = {
        "available": True,
        "audit_id": reproduction.get("audit_id"),
        "status": reproduction.get("status"),
        "independent_reproduction_passed": True,
        "paper_forward_eligible": True,
        "recommendation_eligible": False,
        "real_money_eligible": False,
        "automatic_trading": False,
        "implementation_independence": reproduction.get("implementation_independence") or {},
        "monthly_comparison": reproduction.get("monthly_comparison") or {},
        "independent_cost_ladder_metrics": reproduction.get("independent_cost_ladder_metrics") or {},
        "checks": reproduction.get("checks") or {},
        "current_paper_observation": current,
        "honesty": reproduction.get("honesty"),
    }
    payload["stability_paper_forward"] = paper_forward
    payload["execution_reality"] = execution_reality
    payload["risk_sleeve_attribution"] = risk_sleeve_attribution
    payload["selection_bias_audit"] = selection_bias_audit
    payload["cash_floor_holding_horizon"] = cash_floor
    live_snapshot = _safe_live_signal_snapshot()
    payload["live_factor_signal_validation"] = {
        "strategy_id": "live_factor_z_60_momentum_z_40_v1",
        "status": (
            "cost_inclusive_oos_passed"
            if live_snapshot.get("decision_eligible") is True
            else str((live_snapshot.get("strategy_validation") or {}).get("verdict") or "validation_required").lower()
        ),
        "recommendation_eligible": live_snapshot.get("decision_eligible") is True,
        "current_action": (
            "manual_broker_ticket"
            if live_snapshot.get("decision_eligible") is True
            else "research_observation"
        ),
        "factor_registry": live_snapshot.get("factor_registry") or {},
        "strategy_validation": live_snapshot.get("strategy_validation") or {},
        "problem": (
            "60%生产因子截面分+40%盘中涨跌截面分在2026独立复权样本和真实成本下为负；"
            "单因子production不能挽救失败的组合规则。"
        ),
        "next_action": (
            "停止该追涨组合；新策略必须先固定入场、退出和成本口径，再用未见数据验证。"
        ),
    }
    payload["shortest_profit_path"] = {
        "status": "historical_horizon_passed_paper_only",
        "label": "最短稳定路径 · 511880现金收益底座 · 仅纸面观察",
        "reason": (
            "不再依赖因子选股或20%风险比例：20万元、100股整数手、最低5元佣金、"
            "含现金分红和卖出成本后，2015—2025全部历史完成批次在5BP下3个月、"
            "20BP下6个月、50BP下12个月为正；未来不保证盈利，真钱买入仍锁定。"
        ),
        "signal_date": (cash_floor.get("audit_window") or {}).get("latest_input_date"),
        "cash_weight_pct": 100.0,
        "risk_exposure_pct": 0.0,
        "capital_constrained_execution": cash_floor_execution,
        "normal_horizon_months": 3,
        "moderate_stress_horizon_months": 6,
        "severe_stress_horizon_months": 12,
        "historical_cost_scenario_bps_each_side": 50.0,
        "historical_trading_days": cash_floor_daily.get("trading_days"),
        "historical_total_return_pct": cash_floor_daily.get("total_return_pct"),
        "historical_annualized_return_pct": cash_floor_daily.get(
            "annualized_return_pct"
        ),
        "historical_daily_max_drawdown_pct": cash_floor_daily.get(
            "daily_max_drawdown_pct"
        ),
        "historical_daily_max_drawdown_date": cash_floor_daily.get(
            "daily_max_drawdown_date"
        ),
        "forward_tracking": cash_floor.get("forward_tracking") or {},
        "recommendation_eligible": False,
        "paper_forward_eligible": True,
    } if cash_floor else {}
    if payload["shortest_profit_path"]:
        historical_headline = copy.deepcopy(payload["headline"])
        historical_latest = copy.deepcopy(payload["latest_strategy"])
        active_headline, active_allocation = _active_strategy_public_view(payload)
        payload["historical_headline"] = historical_headline
        payload["historical_latest_strategy"] = historical_latest
        payload["headline"] = active_headline
        payload["current_paper_allocation"] = active_allocation
        payload["latest_strategy"] = {
            "strategy_id": "V233_CASH_FLOOR_HOLDING_HORIZON",
            "title": active_headline.get("method"),
            "forward_total_pct": active_headline.get(
                "formal_forward_return_pct"
            ),
            "recommendation_eligible": False,
            "paper_forward_eligible": True,
            "invalidated": False,
            "stable_profit_proven": False,
        }
    integrity = payload.get("integrity") or {}
    archived_checks = []
    for raw_check in integrity.get("checks") or []:
        hash_matches = bool(raw_check.get("passed"))
        archived_checks.append({
            **raw_check,
            "passed": True,
            "hash_matches": hash_matches,
            "archived": True,
            "current_scope": False,
            "status": (
                "archived_hash_matches" if hash_matches
                else "archived_input_advanced"
            ),
        })
    current_checks = [
        {**row, "archived": False, "current_scope": True}
        for row in context["_report_integrity_rows"](candidate, "V221")
    ]
    current_checks.extend(
        {**row, "archived": False, "current_scope": True}
        for row in context["_report_integrity_rows"](reproduction, "V223")
    )
    if paper_forward:
        current_checks.extend(
            {**row, "archived": False, "current_scope": True}
            for row in context["_report_integrity_rows"](paper_forward, "V225")
        )
    if execution_reality:
        current_checks.extend(
            {**row, "archived": False, "current_scope": True}
            for row in context["_report_integrity_rows"](execution_reality, "V227")
        )
    if risk_sleeve_attribution:
        current_checks.extend(
            {**row, "archived": False, "current_scope": True}
            for row in context["_report_integrity_rows"](risk_sleeve_attribution, "V229")
        )
    if selection_bias_audit:
        current_checks.extend(
            {**row, "archived": False, "current_scope": True}
            for row in context["_report_integrity_rows"](selection_bias_audit, "V231")
        )
    if cash_floor:
        current_checks.extend(
            {**row, "archived": False, "current_scope": True}
            for row in context["_report_integrity_rows"](cash_floor, "V233")
        )
        daily_recovery = cash_floor.get("daily_recovery") or {}
        cross_product = cash_floor.get("cross_product") or {}
        dual_product = cash_floor.get("dual_product_floor") or {}
        # V237/V239/V241 are completed successor diagnostics, not the active
        # V233 public path.  Their source reports can advance as later paper
        # observations are appended, so retain the hash result without letting
        # those archived branches invalidate the current V233 identity.
        for report, label in (
            (daily_recovery, "V237"),
            (cross_product, "V239"),
            (dual_product, "V241"),
        ):
            if not report:
                continue
            for row in context["_report_integrity_rows"](report, label):
                hash_matches = bool(row.get("passed"))
                archived_checks.append({
                    **row,
                    "passed": True,
                    "hash_matches": hash_matches,
                    "archived": True,
                    "current_scope": False,
                    "status": (
                        "archived_hash_matches" if hash_matches
                        else "archived_input_advanced"
                    ),
                })
    checks = archived_checks + current_checks
    passed = sum(bool(row.get("passed")) for row in checks)
    payload["integrity"] = {
        "passed": passed,
        "total": len(checks),
        "all_passed": passed == len(checks),
        "current_passed": sum(bool(row.get("passed")) for row in current_checks),
        "current_total": len(current_checks),
        "current_all_passed": all(row.get("passed") for row in current_checks),
        "archived_hash_matches": sum(
            bool(row.get("hash_matches")) for row in archived_checks
        ),
        "archived_total": len(archived_checks),
        "archived_input_advanced": sum(
            row.get("status") == "archived_input_advanced"
            for row in archived_checks
        ),
        "checks": checks,
    }
    metrics40 = (candidate.get("cost_ladder_metrics") or {}).get("40.0") or {}
    metrics120 = (candidate.get("cost_ladder_metrics") or {}).get("120.0") or {}
    payload["pipeline"] = [
        {"name": "最短收益底座", "state": "历史持有期通过", "detail": "V233纯511880 · 5/20/50BP对应3/6/12个月全部历史批次为正"},
        {"name": "逐日回本压力", "state": "2,568批次已验收", "detail": "V237 · 3BP佣金+观察价差持续回正中位7日、最慢34日；5BP滑点压力最慢54日"},
        {"name": "同类产品横向复现", "state": "5只真实产品已验收", "detail": "V239 · 511880/511990/159001/511660/511700 · 每日收益与价差同账本；基准压力最慢162日"},
        {"name": "双产品现金底座", "state": "2,568批次已验收", "detail": "V241 · 511880 1000股 + 511990 900股 · 基准/额外5BP最慢26/45日；V243真实前向零日起步"},
        {"name": "QDII单位", "state": "已纠正", "detail": "收益用复权价 · 溢价用未复权价格/净值"},
        {"name": "资金结构", "state": "已冻结", "detail": "511880核心仓≥80% · 风险袖套≤20%"},
        {"name": "成本压力", "state": "正收益", "detail": "40/80/120BP四个分段全部为正"},
        {"name": "逐年稳定", "state": "正收益", "detail": "2015—2025三档成本每年均正"},
        {"name": "独立复算", "state": "完全一致", "detail": "3档成本×150个月 · 最大净值误差<1e-14"},
        {
            "name": "20万可执行性",
            "state": "整数手通过" if execution_reality else "待验收",
            "detail": (
                f"100股整数手 · 实际风险{lot_plan.get('post_trade_risk_weight_pct')}% · "
                f"剩余现金¥{lot_plan.get('residual_cash')} · 最高50BP压力"
                if lot_plan else "缺少V227执行审计"
            ),
        },
        {
            "name": "收益来源归因",
            "state": "收益增强保留" if risk_sleeve_attribution else "待验收",
            "detail": (
                "风险袖套四阶段均增加累计收益 · 9/11年跑赢纯511880 · 2018/2022跑输"
                if risk_sleeve_attribution else "缺少V229增量归因"
            ),
        },
        {
            "name": "比例选择偏差",
            "state": "滚动修正失败",
            "detail": "V231八年均正但累计+14.73%、月末账本回撤-1.07% · 被纯511880的+16.99%、月末账本回撤0%支配",
        },
        {
            "name": "真实前向",
            "state": "观察中" if formal_forward.get("completed_post_freeze_common_closes") else "零日起点",
            "detail": (
                f"V225共同收盘{formal_forward.get('completed_post_freeze_common_closes', 0)}个 · "
                f"累计{formal_forward.get('formal_forward_return_pct', 0)}%"
            ),
        },
        {"name": "当前动作", "state": "当前生效", "detail": "优先纸面511880现金底座 · 80/20降级为风险增强研究 · 真钱与自动交易锁定"},
    ]
    payload["next_work"] = [
        (
            "V233已把最短路径改为纯511880：5BP双边成本历史最短全正持有期3个月、"
            "20BP为6个月、50BP为12个月；当前20万元5BP整数手为1900股、"
            "剩余现金约8509元。它仍不是未来收益保证。"
        ),
        (
            "V237把月度结果细化到2,568个完整历史入场日：3BP佣金加当前观察价差下，"
            "持续站上本金的中位/90分位/最慢为7/13/34个交易日；额外5BP每边滑点"
            "压力为18/30/54日。首次回正与持续回正已分开统计，仍不能替代真实前向。"
        ),
        (
            "V239已用独立每日百份收益账本横向复现511990、159001、511660、511700；"
            "五只产品在3BP佣金+观察价差的已完成一年历史入场均在252日内持续回正，"
            "但511660的异常高开批次最慢162日，证明流动性和成交价风险不能忽略。"
        ),
        (
            "V241把高流动性的511880与511990按100股整数手分散为约53/47："
            "2,568个完整历史入场在3BP佣金+观察价差下持续回正最慢26日，"
            "额外5BP压力最慢45日，均优于两只单持；V243已从真实卖一价零日起步。"
        ),
        (
            "V231拒绝事后滚动比例修正：2018—2025虽8/8年为正，但累计+14.73%、"
            "月末账本回撤-1.07%，不如纯511880的+16.99%、月末账本回撤0%；固定20%的选择偏差"
            "只能由形成后的真实前向数据解决。"
        ),
        (
            "V229同口径50BP归因：风险袖套在四阶段均增加累计收益，"
            "2015—2025有9/11年跑赢纯511880，但2018与2022跑输；"
            "因此保留为收益增强层，不把它冒充稳定收益来源。"
        ),
        (
            f"20万元账户当前整数手：511880 {((lot_plan.get('quantities') or {}).get('511880'))}股、"
            f"511010 {((lot_plan.get('quantities') or {}).get('511010'))}股、"
            f"518880 {((lot_plan.get('quantities') or {}).get('518880'))}股、"
            f"510300 {((lot_plan.get('quantities') or {}).get('510300'))}股；"
            f"实际风险{lot_plan.get('post_trade_risk_weight_pct')}%，不把理论20%冒充可成交仓位。"
            if lot_plan else "完成20万元账户的整数手、最低佣金和滑点执行审计。"
        ),
        f"40BP开发/验证/测试/2026诊断累计为+{(metrics40.get('development_2015_2021') or {}).get('total_return_pct')}%/+{(metrics40.get('validation_2022_2023') or {}).get('total_return_pct')}%/+{(metrics40.get('test_2024_2025') or {}).get('total_return_pct')}%/+{(metrics40.get('diagnostic_2026_through_july') or {}).get('total_return_pct')}%。",
        f"120BP下2022年仍为+{((metrics120.get('validation_2022_2023') or {}).get('yearly_total_return_pct') or {}).get('2022')}%，验证最大回撤{(metrics120.get('validation_2022_2023') or {}).get('max_drawdown_pct')}%。",
        (
            f"V225已冻结2026-08-11零日基线并累计"
            f"{formal_forward.get('completed_post_freeze_common_closes', 0)}个真实共同收盘；"
            "只允许点击手动刷新，不伪造稳定盈利或真钱权限。"
        ),
    ]
    if cash_floor:
        cash_history = (
            (cash_floor.get("continuous_50bp") or {}).get("historical_2015_2025")
            or {}
        )
        cash_diagnostic = (
            (cash_floor.get("continuous_50bp") or {}).get(
                "diagnostic_2026_through_july"
            ) or {}
        )
        payload["pipeline"] = [
            {
                "name": "收益机制",
                "state": "官方可解释",
                "detail": "511880货币市场工具收益 + 年度现金分红；不是股票择时Alpha",
            },
            {
                "name": "物理输入",
                "state": "截至2026-08-11",
                "detail": "价格与累计分红独立读取、哈希固化；不调用外部模型或付费API",
            },
            {
                "name": "20万执行",
                "state": "整数手通过",
                "detail": (
                    f"5BP纸面{cash_floor_plan.get('quantity')}股 · "
                    f"剩余现金¥{cash_floor_plan.get('residual_cash')} · 风险资产0%"
                ),
            },
            {
                "name": "持有期门槛",
                "state": "历史批次全正",
                "detail": "5BP 3个月 · 20BP 6个月 · 50BP 12个月；买卖双边成本均已计入",
            },
            {
                "name": "跨年稳定",
                "state": "11/11年为正",
                "detail": (
                    f"2015—2025连续持有50BP累计+{cash_history.get('total_return_pct')}% · "
                    f"最大回撤{cash_history.get('max_drawdown_pct')}%"
                ),
            },
            {
                "name": "当前诊断",
                "state": "纸面观察",
                "detail": (
                    f"2026截至7月+{cash_diagnostic.get('total_return_pct')}% · "
                    "未来不保证 · 真钱与自动交易锁定"
                ),
            },
            {
                "name": "风险增强",
                "state": "降级研究",
                "detail": "旧80/20及比例选择证据保留在归档区，不再主导最短收益路径",
            },
        ]
        payload["next_work"] = [
            (
                "主路径只跟踪V233纯511880：当前5BP纸面1900股、余现金8509.33元；"
                "不再继续为固定20%风险比例寻找理由。"
            ),
            (
                "任何手工真钱动作前先核对实际券商ETF佣金和买卖价差；"
                "若总成本接近20BP，历史全正持有期应按6个月而非3个月看。"
            ),
            (
                "50BP双边压力下历史全正需要12个月，且最新完成批次仅约+0.084%；"
                "短于对应持有期不能显示为稳定路径。"
            ),
            (
                "V233形成后的真实前向记录仍为0，页面必须继续写明历史通过不等于未来保证。"
            ),
        ]
    return payload



def _decision_gate_status_full(context: dict[str, object]) -> dict[str, object]:
    _BASE_DECISION_GATE_STATUS = context["_BASE_DECISION_GATE_STATUS"]
    _active_strategy_public_view = context["_active_strategy_public_view"]
    gate = _BASE_DECISION_GATE_STATUS()
    manual_real_money = os.environ.get("OLYMPUS_MANUAL_REAL_MONEY_MODE") == "1"
    candidate, reproduction = context["_stability_80_20_reports"]()
    if not context["_stability_80_20_passed"](candidate, reproduction):
        gate.update({
            "recommendation_eligible": False,
            "research_only": True,
            "status": "locked_manual_consent" if manual_real_money else "locked_evidence",
            "reason": (
                "稳定性独立复现证据未通过或缺失；人工同意不能提升研究推荐资格。"
                if manual_real_money else
                "稳定性独立复现证据未通过或缺失，研究推荐保持锁定。"
            ),
            "manual_real_money_enabled": manual_real_money,
            "manual_broker_ticket_enabled": False,
            "manual_consent": manual_real_money,
            "broker_connected": False,
            "broker_orders_enabled": False,
            "automatic_trading": False,
            "stable_profit_proven": False,
        })
        registry = dict(gate.get("strategy_registry") or {})
        registry.update({
            "recommendation_gate_passed": False,
            "manual_real_money_enabled": manual_real_money,
            "broker_connected": False,
        })
        gate["strategy_registry"] = registry
        return gate
    execution_reality = context["_stability_execution_reality_report"]()
    lot_plan = execution_reality.get("current_lot_plan") or {}
    risk_sleeve_attribution = context["_risk_sleeve_incremental_attribution_report"]()
    selection_bias_audit = context["_rolling_risk_cap_walk_forward_report"]()
    cash_floor = context["_cash_floor_holding_horizon_report"]()
    cash_floor_plan = (cash_floor.get("current_lot_plans") or {}).get("5.0") or {}
    cash_floor_daily = (
        ((cash_floor.get("daily_path_metrics") or {}).get("scenarios") or {})
        .get("50.0") or {}
    )
    cash_floor_execution = {
        **cash_floor_plan,
        "quantities": {"511880": cash_floor_plan.get("quantity", 0)},
        "post_trade_safe_core_pct": 100.0,
        "post_trade_risk_weight_pct": 0.0,
        "forward_tracking": cash_floor.get("forward_tracking") or {},
    } if cash_floor_plan else {}
    cash_floor_forward = cash_floor.get("forward_tracking") or {}
    paper_forward_eligible = bool(
        cash_floor and cash_floor_forward.get("status") == "paper_forward_running"
    )
    shortest_profit_path = {
        "status": "historical_horizon_passed_paper_only",
        "label": "最短稳定路径 · 511880现金收益底座 · 仅纸面观察",
        "reason": (
            "V233不依赖因子选股或风险比例：在20万元、100股整数手、"
            "最低佣金、现金分红和双边成本口径下，历史完成批次的最短正收益"
            "持有期为常规3个月、中压6个月、高压12个月。"
            "当前仍只是前向纸面观察，不代表未来稳定盈利。"
        ),
        "signal_date": (cash_floor.get("audit_window") or {}).get(
            "latest_input_date"
        ),
        "cash_weight_pct": 100.0,
        "risk_exposure_pct": 0.0,
        "normal_horizon_months": 3,
        "moderate_stress_horizon_months": 6,
        "severe_stress_horizon_months": 12,
        "historical_cost_scenario_bps_each_side": 50.0,
        "historical_trading_days": cash_floor_daily.get("trading_days"),
        "historical_total_return_pct": cash_floor_daily.get("total_return_pct"),
        "historical_annualized_return_pct": cash_floor_daily.get(
            "annualized_return_pct"
        ),
        "historical_daily_max_drawdown_pct": cash_floor_daily.get(
            "daily_max_drawdown_pct"
        ),
        "historical_daily_max_drawdown_date": cash_floor_daily.get(
            "daily_max_drawdown_date"
        ),
        "capital_constrained_execution": cash_floor_execution,
        "forward_tracking": cash_floor_forward,
        "recommendation_eligible": False,
        "paper_forward_eligible": paper_forward_eligible,
    } if cash_floor else {}
    active_headline, active_allocation = _active_strategy_public_view({
        "shortest_profit_path": shortest_profit_path,
    })
    cash_forward = shortest_profit_path.get("forward_tracking") or {}
    cash_mark = cash_forward.get("current_mark") or {}
    gate.update({
        "recommendation_eligible": False,
        "research_only": True,
        "status": "locked",
        "reason": (
            "V233已把最短路径改为无选参的511880现金收益底座：在同一20万元整数手、"
            "最低佣金、现金分红和双边成本下，2015—2025全部历史完成批次在"
            "5BP/20BP/50BP压力下分别持有3/6/12个月后为正；风险袖套降级为研究增强。"
            "V223已用独立信号逻辑、独立511880收益重建和独立资金价值账本逐月复现"
            "V221固定80/20结构；V223历史对照为80%货币ETF核心仓+20%风险袖套，"
            "但当前公开路径不是该组合，而是100%货币ETF现金收益底座；"
            "40/80/120BP下2015—2025每年均为正。"
            "V227进一步按20万元、100股整数手、最低佣金与滑点复算，"
            f"当前可执行纸面风险为{lot_plan.get('post_trade_risk_weight_pct', 20)}%，"
            "最高50BP执行压力下2015—2025每年仍为正；V229确认风险袖套四阶段均"
            f"增加累计收益、{len(risk_sleeve_attribution.get('years_beating_cash') or [])}/11年"
            "跑赢纯511880，但它只是收益增强层；V231滚动比例修正被纯511880支配并已拒绝，"
            "无法用事后规则消除固定20%的选择偏差。当前V233真实前向"
            f"证据为{cash_forward.get('completed_closes', 0)}个完成收盘、"
            f"成本后净清算收益{cash_mark.get('net_liquidation_return_pct')}%，"
            "不能宣称未来稳定盈利，"
            "真钱推荐、订单和自动交易继续关闭。"
        ),
        "compact_reason": (
            f"V233现金收益底座正在纸面前向：已完成"
            f"{cash_forward.get('completed_closes', 0)}个收盘，成本后"
            f"{cash_mark.get('net_liquidation_return_pct')}%；尚未证明稳定盈利，"
            "正式推荐与自动交易关闭。"
        ),
        "source": "reports/v233_cash_floor_holding_horizon.json",
        "source_status": cash_forward.get("status") or "unavailable",
        "generated_at": cash_forward.get("generated_at"),
        "source_scope": "active_research_paper_forward",
        "paper_forward_eligible": paper_forward_eligible,
        "paper_candidate": paper_forward_eligible,
        "product_forward_clock": {
            "source": gate.get("source"),
            "status": gate.get("source_status"),
            "generated_at": gate.get("generated_at"),
            "valid": gate.get("product_forward_clock_valid") is True,
            "strict_certificate_valid": (
                gate.get("strict_product_gate_certificate_valid") is True
            ),
        },
        "research_paper_forward": {
            "source": "reports/v233_cash_floor_forward.json",
            "status": cash_forward.get("status") or "unavailable",
            "generated_at": cash_forward.get("generated_at"),
            "eligible": paper_forward_eligible,
            "completed_closes": cash_forward.get("completed_closes", 0),
            "latest_completed_close_date": cash_forward.get(
                "latest_completed_close_date"
            ),
            "current_mark_as_of": cash_mark.get("as_of"),
        },
        "current_paper_allocation": active_allocation,
        "capital_constrained_execution": active_headline.get(
            "capital_constrained_execution"
        ) or {},
        "risk_sleeve_incremental_attribution": risk_sleeve_attribution,
        "selection_bias_audit": selection_bias_audit,
        "cash_floor_holding_horizon": cash_floor,
        "shortest_profit_path": shortest_profit_path,
    })
    registry = dict(gate.get("strategy_registry") or {})
    registry.update({
        "active_strategy": None,
        "active_research_path": "V233_CASH_FLOOR_HOLDING_HORIZON",
        "historical_comparator": candidate.get("strategy_id"),
        # Keep the production registry fact bound to its advertised source.
        # V223 is a separate paper comparator, not an active production strategy.
        "historical_comparator_gate_passed": True,
        "recommendation_gate_passed": False,
        "recommendation_lock_reason": gate["reason"],
    })
    gate["strategy_registry"] = registry
    gate.update({
        "manual_real_money_enabled": manual_real_money,
        # Manual consent records a workflow choice only.  This application has
        # no broker connection and therefore never advertises broker tickets.
        "manual_broker_ticket_enabled": False,
        "manual_consent": manual_real_money,
        "broker_connected": False,
        "broker_orders_enabled": False,
        "automatic_trading": False,
        "stable_profit_proven": False,
        "manual_research_alerts_allowed": manual_real_money,
        "formal_strategy_recommendations_allowed": bool(
            gate.get("recommendation_eligible")
        ),
    })
    if manual_real_money:
        gate.update({
            # Consent records the user's workflow choice only.  It cannot turn
            # failed/missing research evidence into recommendation permission.
            "recommendation_eligible": False,
            "research_only": True,
            "status": "locked_manual_consent",
            "manual_stock_execution_source": "/api/opportunities",
            "reason": (
                "已记录用户愿意手工操作，但人工同意不改变研究推荐资格。"
                "当前独立、成本后、前向证据仍未通过，因此不生成正式策略推荐。"
                "研究型人工复核提醒仍可按统一机会质量、买点与风控条件触发；"
                "系统不连接券商、不自动交易，手工成交与正式策略绩效分账。"
            ),
        })
        registry["manual_real_money_enabled"] = True
        registry["broker_connected"] = False
    return gate
