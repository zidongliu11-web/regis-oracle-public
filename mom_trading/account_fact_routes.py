"""Manual account-fact HTTP routes kept separate from the main application."""

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
from typing import Any, Callable, Pattern

from flask import jsonify, request

from .signal_engine import is_recommendable_stock
from . import manual_fill_time, manual_fill_fees, manual_fill_plan
from .timing_engine import (
    IdempotencyConflictError,
    account_cashflow_reconciliation_status,
    compute_levels,
    confirm_buy,
    get_account_cash,
    list_account_cashflows,
    list_account_nav_snapshots,
    list_positions,
    read_account_ledger_facts,
    record_account_cashflow,
    record_account_cashflow_reconciliation,
    set_position_exit_intent,
    set_account_cash,
    set_account_initial_equity,
    update_sellable_inventory_snapshot,
)


def manual_ledger_implementation_sha():
    """Source-only publication fingerprint; not an account or runtime receipt."""
    root = Path(__file__).parent
    return hashlib.sha256(b"\0".join((root / name).read_bytes() for name in (
        "account_fact_routes.py", "app.py", "timing_engine.py",
        "manual_fill_time.py", "manual_fill_fees.py", "static/positions.js",
        "static/fill-time.js", "static/spa-review.js", "templates/positions.html",
        "manual_fill_plan.py", "static/fill-plan.js", "static/spa-workflow.js",
        "templates/access_policy.html", "templates/spa.html",
    ))).hexdigest()


def register_account_fact_routes(
    app: Any, *,
    strict_json_payload: Callable[..., dict[str, object]],
    bounded_int: Callable[..., int],
    bounded_float: Callable[..., float],
    validated_display_name: Callable[..., str],
    resolve_name_to_code: Callable[[str], tuple],
    request_idempotency_key: Callable[[dict[str, object]], str],
    code_pattern: Pattern[str],
    sh_tz: Any,
    current_market_regime: Callable[[dict], dict],
    market_adjusted_trade_capacity: Callable[[dict], dict],
    compact_decision_gate: Callable[[], dict],
) -> None:
    """Register manual account facts and read-only position inventory routes."""

    def access_capabilities() -> dict:
        remote = bool(app.config["REMOTE_MODE"])
        return {
            "schema": "regis.access-policy.v1",
            "scope": "manual_account_and_trade_records",
            "write_policy": "remote_read_only" if remote else "local_same_origin",
            "server_writes_enabled": not remote,
            "reason": (
                "云端人工账本只读：账户、现金、可卖库存和成交不能在此保存。"
                "历史研究实验可在策略/因子工作台按原有权限登记；不代表策略获准交易。"
                "本机记录不会通过 GitHub 同步到云端；云端人工账本尚待独立授权与部署。"
                if remote else
                "本机同源页面可维护人工记录；仍须通过各接口校验，不代表具有买卖资格。"
            ),
            "trading_authority_granted": False,
            "automatic_orders": False,
        }

    @app.context_processor
    def account_access_context():
        return {"regis_access": access_capabilities()}

    @app.get("/api/access-capabilities")
    def read_access_capabilities():
        response = jsonify(access_capabilities())
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Regis-Manual-Ledger"] = manual_ledger_implementation_sha()
        return response

    @app.route("/api/timing/account", methods=["GET", "PUT"])
    def timing_account():
        """Read or update the user's authoritative remaining-cash snapshot."""
        if request.method == "GET":
            snapshot = get_account_cash()
            return jsonify({
                "cash": snapshot,
                "configured": snapshot is not None,
                "automatic_orders": False,
            })
        try:
            data = strict_json_payload(
                allowed={"remaining_cash_yuan"},
                required={"remaining_cash_yuan"},
            )
            amount = bounded_float(
                data.get("remaining_cash_yuan"), "remaining_cash_yuan",
                minimum=0.0, maximum=10_000_000_000,
            )
            snapshot = set_account_cash(amount)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({
            "cash": snapshot, "configured": True,
            "message": f"已记录当前剩余现金 ¥{amount:,.2f}",
            "automatic_orders": False,
        })

    @app.route("/api/timing/fill-times", methods=["GET", "POST"])
    def timing_fill_times():
        boundary = {"automatic_orders": False, "changes_cash_or_inventory": False,
                    "strategy_qualification_granted": False,
                    "evidence_source": "user_reported_not_broker_verified"}
        if request.method == "GET":
            snapshot = read_account_ledger_facts(include_review=True)
            positions = manual_fill_fees.from_snapshot(snapshot["open_rows"] + snapshot["closed_rows"], snapshot)
            positions.sort(key=lambda p: p["id"], reverse=True)
            return jsonify({"items": manual_fill_time.records(positions, snapshot["time_rows"]),
                            "manual_ledger_snapshot": snapshot["review_identity"], **boundary})
        try:
            data = strict_json_payload(
                allowed={"fill_id", "executed_at", "note", "expected_event_id", "idempotency_key"},
                required={"fill_id", "executed_at", "note", "expected_event_id"},
            )
            result = manual_fill_time.record(data["fill_id"], data["executed_at"], data["note"],
                                             data["expected_event_id"], request_idempotency_key(data))
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"event": result, **boundary}), 200 if result["idempotent_replay"] else 201

    @app.route("/api/timing/fill-plans", methods=["GET", "POST"])
    def timing_fill_plans():
        boundary = {"automatic_orders": False, "changes_cash_or_inventory": False,
                    "changes_alert_status": False, "strategy_qualification_granted": False,
                    "evidence_source": "user_reported_not_broker_verified"}
        try:
            if request.method == "GET":
                if request.args.get("fill_id"):
                    return jsonify({"plans": manual_fill_plan.choices(request.args["fill_id"]),
                                    "limit": 200, **boundary})
                snapshot = read_account_ledger_facts(include_review=True)
                positions = manual_fill_fees.from_snapshot(snapshot["open_rows"] + snapshot["closed_rows"], snapshot)
                positions.sort(key=lambda p: p["id"], reverse=True)
                return jsonify({"items": manual_fill_plan.records(positions, snapshot["plan_rows"]),
                                "manual_ledger_snapshot": snapshot["review_identity"], **boundary})
            data = strict_json_payload(
                allowed={"fill_id", "alert_id", "expected_plan_sha256", "note", "expected_event_id", "idempotency_key"},
                required={"fill_id", "alert_id", "expected_plan_sha256", "note", "expected_event_id"},
            )
            result = manual_fill_plan.record(data["fill_id"], data["alert_id"], data["expected_plan_sha256"],
                data["note"], data["expected_event_id"], request_idempotency_key(data))
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"event": result, **boundary}), 200 if result["idempotent_replay"] else 201

    @app.route("/api/timing/fill-fees", methods=["GET", "POST"])
    def timing_fill_fees():
        boundary = {"automatic_orders": False, "changes_trade_or_inventory": False,
                    "strategy_qualification_granted": False, "evidence_source": "user_reported_not_broker_verified",
                    "cash_policy": "explicit_current_cash_snapshot_required_not_fee_delta"}
        if request.method == "GET":
            snapshot = read_account_ledger_facts()
            originals = manual_fill_fees.from_snapshot(snapshot["open_rows"] + snapshot["closed_rows"], {**snapshot, "fee_rows": []})
            return jsonify({"items": manual_fill_fees.records(originals, snapshot["fee_rows"]),
                            "account_identity": manual_fill_fees.cash_identity(snapshot["account_cash"]), **boundary})
        try:
            data = strict_json_payload(
                allowed={"fill_id", "fee_yuan", "note", "expected_event_id", "idempotency_key", "remaining_cash_yuan", "expected_cash_identity"},
                required={"fill_id", "fee_yuan", "note", "expected_event_id", "remaining_cash_yuan", "expected_cash_identity"},
            )
            result = manual_fill_fees.record(data["fill_id"], data["fee_yuan"], data["note"],
                data["expected_event_id"], request_idempotency_key(data), data["remaining_cash_yuan"], data["expected_cash_identity"])
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"event": result, "current_cash_confirmed": not result["idempotent_replay"], **boundary}), 200 if result["idempotent_replay"] else 201

    @app.route("/api/timing/account-baseline", methods=["GET", "PUT"])
    def timing_account_baseline():
        """Read or record the explicit equity baseline used for drawdown."""
        account = get_account_cash()
        baseline = None if account is None else {
            key: account.get(key) for key in (
                "initial_equity_yuan", "initial_equity_as_of",
                "initial_equity_source", "initial_equity_recorded_at",
            )
        }
        if request.method == "GET":
            return jsonify({
                "baseline": baseline,
                "configured": bool(
                    baseline and baseline.get("initial_equity_yuan") is not None
                ),
                "automatic_orders": False,
            })
        try:
            data = strict_json_payload(
                allowed={"initial_equity_yuan", "initial_equity_as_of"},
                required={"initial_equity_yuan", "initial_equity_as_of"},
            )
            amount = bounded_float(
                data.get("initial_equity_yuan"), "initial_equity_yuan",
                minimum=0.01, maximum=10_000_000_000,
            )
            snapshot = set_account_initial_equity(
                amount, str(data.get("initial_equity_as_of") or ""),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({
            "baseline": snapshot,
            "configured": True,
            "message": (
                f"已记录初始权益 ¥{amount:,.2f}；"
                "后续回撤只基于该明确基准和真实账本计算"
            ),
            "automatic_orders": False,
        }), 200

    @app.route("/api/timing/account-cashflows", methods=["GET", "POST"])
    def timing_account_cashflows():
        """Read or append explicit external deposits and withdrawals."""
        if request.method == "GET":
            events = list_account_cashflows()
            snapshots = list_account_nav_snapshots()
            through_date = (
                snapshots[-1].get("trading_date") if snapshots else None
            )
            return jsonify({
                "events": events, "count": len(events),
                "cashflow_completeness": account_cashflow_reconciliation_status(
                    through_date,
                ),
                "automatic_orders": False,
            })
        try:
            data = strict_json_payload(
                allowed={
                    "flow_type", "amount_yuan", "effective_at",
                    "cash_balance_after_yuan", "note",
                },
                required={
                    "flow_type", "amount_yuan", "effective_at",
                    "cash_balance_after_yuan",
                },
            )
            amount = bounded_float(
                data.get("amount_yuan"), "amount_yuan",
                minimum=0.01, maximum=10_000_000_000,
            )
            cash_after = bounded_float(
                data.get("cash_balance_after_yuan"),
                "cash_balance_after_yuan",
                minimum=0.0, maximum=10_000_000_000,
            )
            event = record_account_cashflow(
                str(data.get("flow_type") or ""), amount,
                str(data.get("effective_at") or ""), cash_after,
                request_idempotency_key(data),
                note=(
                    str(data.get("note") or "").strip() or None
                ),
            )
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        replay = event.get("idempotent_replay") is True
        label = "入金" if event["flow_type"] == "deposit" else "出金"
        return jsonify({
            "event": event,
            "message": (
                f"已记录{label} ¥{float(event['amount_yuan']):,.2f}；"
                "后续绩效将与外部资金变动分开计算"
            ),
            "automatic_orders": False,
        }), 200 if replay else 201

    @app.post("/api/timing/account-cashflows/reconcile")
    def timing_account_cashflows_reconcile():
        """Record the user's explicit no-omission confirmation through a date."""
        try:
            data = strict_json_payload(
                allowed={"through_date", "note"},
                required={"through_date"},
            )
            confirmation = record_account_cashflow_reconciliation(
                str(data.get("through_date") or ""),
                request_idempotency_key(data),
                note=str(data.get("note") or "").strip() or None,
            )
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        replay = confirmation.get("idempotent_replay") is True
        status = account_cashflow_reconciliation_status(
            confirmation["through_date"],
        )
        return jsonify({
            "confirmation": confirmation,
            "cashflow_completeness": status,
            "message": status["reason"],
            "automatic_orders": False,
        }), 200 if replay else 201

    @app.get("/api/timing/positions")
    def timing_positions():
        status = request.args.get("status")
        if status not in ("open", "closed", None):
            status = None
        snapshot = read_account_ledger_facts()
        positions = manual_fill_fees.from_snapshot(snapshot["open_rows"] + snapshot["closed_rows"], snapshot)
        positions = sorted((p for p in positions if status is None or p.get("status") == status), key=lambda p: p["id"], reverse=True)
        return jsonify({
            "positions": positions,
            "count": len(positions),
            "trade_capacity": market_adjusted_trade_capacity(
                current_market_regime({}),
            ),
            "decision_gate": compact_decision_gate(),
        })

    @app.post("/api/timing/position-snapshot")
    def timing_position_snapshot():
        """Import an existing holding and remaining cash as account facts."""
        try:
            data = strict_json_payload(
                allowed={
                    "code", "name", "average_cost", "shares", "buy_fee_yuan",
                    "remaining_cash_yuan", "sellable_shares_snapshot",
                    "sellable_as_of", "idempotency_key",
                },
                required={"shares", "average_cost", "remaining_cash_yuan"},
            )
            code = str(data.get("code") or "").strip()
            claimed_name = validated_display_name(data.get("name"))
            if not code_pattern.fullmatch(code) and claimed_name:
                code, _resolved_name = resolve_name_to_code(claimed_name)
            if not code_pattern.fullmatch(code):
                raise ValueError("无效股票代码")
            shares = bounded_int(
                data.get("shares"), "shares", minimum=1, maximum=10_000_000,
            )
            average_cost = bounded_float(
                data.get("average_cost"), "average_cost",
                minimum=0.01, maximum=100_000,
            )
            remaining_cash = bounded_float(
                data.get("remaining_cash_yuan"), "remaining_cash_yuan",
                minimum=0.0, maximum=10_000_000_000,
            )
            buy_fee = None
            if data.get("buy_fee_yuan") not in (None, ""):
                buy_fee = bounded_float(
                    data.get("buy_fee_yuan"), "buy_fee_yuan",
                    minimum=0.0, maximum=100_000,
                )
            sellable_shares = None
            sellable_as_of = None
            sellable_supplied = data.get("sellable_shares_snapshot") not in (None, "")
            sellable_time_supplied = data.get("sellable_as_of") not in (None, "")
            if sellable_supplied != sellable_time_supplied:
                raise ValueError("今日可卖股数与核验日期必须同时填写")
            if sellable_supplied:
                sellable_shares = bounded_int(
                    data.get("sellable_shares_snapshot"),
                    "sellable_shares_snapshot", minimum=0, maximum=shares,
                )
                sellable_as_of = str(data.get("sellable_as_of") or "").strip()
                if len(sellable_as_of) > 64:
                    raise ValueError("今日可卖库存核验时间无效")
            idempotency_key = request_idempotency_key(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            quotes = app.config["QUOTE_PROVIDER"].get_quotes([code])
        except Exception as exc:
            return jsonify({"error": f"行情不可用：{type(exc).__name__}"}), 503
        quote = (quotes or {}).get(code)
        if not quote:
            return jsonify({"error": "行情不可用，未写入持仓"}), 503
        try:
            name = validated_display_name(getattr(quote, "name", ""), fallback=code)
        except ValueError as exc:
            return jsonify({"error": f"行情名称无效：{exc}"}), 503
        if not is_recommendable_stock(code, name):
            return jsonify({"error": "仅支持A股个股持仓"}), 409
        try:
            position = confirm_buy(
                code=code, name=name, price=average_cost, shares=shares,
                score=0.0, level="持仓快照", window_label="用户录入",
                action="position_snapshot", idempotency_key=idempotency_key,
                entry_context={
                    "entry_origin": "manual_position_snapshot",
                    "entry_strategy_id": None,
                    "entry_evidence_ref_json": None,
                    "entry_signal_id": None,
                    "entry_alert_id": None,
                    "entry_reference_price": None,
                },
                buy_fee_yuan=buy_fee, adjust_cash=False,
                cash_snapshot_yuan=remaining_cash,
                sellable_shares_snapshot=sellable_shares,
                sellable_as_of=sellable_as_of,
            )
            cash = get_account_cash()
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        levels = compute_levels(
            average_cost, code=code,
            prev_close=getattr(quote, "previous_close", None) or None,
        )
        replay = bool(position.get("idempotent_replay"))
        current_cash = float((cash or {}).get("cash_balance", remaining_cash))
        return jsonify({
            "position": position, "cash": cash, "levels": levels,
            "idempotent_replay": replay, "automatic_orders": False,
            "message": (
                f"已录入 {name} {shares} 股，成本 ¥{average_cost:.2f}；"
                f"当前剩余现金 ¥{current_cash:,.2f}；"
                + (
                    f"今日可卖 {sellable_shares} 股（{sellable_as_of}），已进入实时组合监控"
                    if sellable_shares is not None
                    else "今日可卖数量未录入，精确卖出建议保持关闭"
                )
            ),
        }), 200 if replay else 201

    @app.put("/api/timing/positions/<int:position_id>/sellable-inventory")
    def timing_position_sellable_inventory(position_id: int):
        """Record a broker/user verified daily sellable-inventory fact."""
        try:
            data = strict_json_payload(
                allowed={
                    "sellable_shares_snapshot", "sellable_as_of",
                    "idempotency_key",
                },
                required={"sellable_shares_snapshot", "sellable_as_of"},
            )
            sellable_shares = bounded_int(
                data.get("sellable_shares_snapshot"),
                "sellable_shares_snapshot", minimum=0, maximum=10_000_000,
            )
            sellable_as_of = str(data.get("sellable_as_of") or "").strip()
            if not sellable_as_of or len(sellable_as_of) > 64:
                raise ValueError("今日可卖库存核验时间无效")
            idempotency_key = request_idempotency_key(data)
            event = update_sellable_inventory_snapshot(
                position_id, sellable_shares, sellable_as_of,
                idempotency_key,
            )
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        normalized_as_of = str(event.get("new_sellable_as_of") or "")
        try:
            if len(normalized_as_of) == 10:
                inventory_day = datetime.strptime(
                    normalized_as_of, "%Y-%m-%d",
                ).date().isoformat()
            else:
                inventory_day = datetime.fromisoformat(
                    normalized_as_of.replace("Z", "+00:00"),
                ).astimezone(sh_tz).date().isoformat()
        except ValueError:
            inventory_day = None
        today = datetime.now(sh_tz).date().isoformat()
        current_for_today = inventory_day == today
        return jsonify({
            "event": event,
            "idempotent_replay": bool(event.get("idempotent_replay")),
            "current_for_today": current_for_today,
            "automatic_orders": False,
            "message": (
                f"已核验今日可卖 {sellable_shares} 股（{inventory_day}）"
                if current_for_today
                else f"已记录 {inventory_day or normalized_as_of} 的可卖库存；非今日数据不生成精确卖出数量"
            ),
        }), 200

    @app.put("/api/timing/positions/<int:position_id>/exit-intent")
    def timing_position_exit_intent(position_id: int):
        """Record a user-directed target holding for a future manual review."""
        try:
            data = strict_json_payload(
                allowed={
                    "target_shares", "effective_date", "note",
                    "idempotency_key",
                },
                required={"target_shares", "effective_date"},
            )
            target_shares = bounded_int(
                data.get("target_shares"), "target_shares",
                minimum=0, maximum=10_000_000,
            )
            effective_date = str(data.get("effective_date") or "").strip()
            note = str(data.get("note") or "").strip() or None
            idempotency_key = request_idempotency_key(data)
            event = set_position_exit_intent(
                position_id, target_shares, effective_date, note,
                idempotency_key,
            )
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({
            "event": event,
            "idempotent_replay": bool(event.get("idempotent_replay")),
            "automatic_orders": False,
            "message": (
                f"已记录 {effective_date} 起将目标持仓降至 {target_shares} 股；"
                "实际成交仍需交易时段实时行情、当日可卖库存与人工确认"
            ),
        }), 200


__all__ = ["register_account_fact_routes"]
