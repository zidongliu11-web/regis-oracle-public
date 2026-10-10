from __future__ import annotations

from datetime import datetime

from .db import connect


OFFICIAL_COMMISSION_MAX_RATE = 0.003
DEFAULT_COMMISSION_RATE = 0.0003
DEFAULT_MINIMUM_COMMISSION = 5.0


def fee_profile(database: str) -> dict:
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT key, value, updated_at FROM settings "
            "WHERE key IN ('commission_rate', 'minimum_commission', 'fee_profile_status')"
        ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    updated = max((row["updated_at"] for row in rows), default=None)
    rate = float(values.get("commission_rate", DEFAULT_COMMISSION_RATE))
    minimum = float(
        values.get("minimum_commission", DEFAULT_MINIMUM_COMMISSION)
    )
    return {
        "commission_rate": rate,
        "commission_wan": round(rate * 10_000, 4),
        "minimum_commission": minimum,
        "status": values.get("fee_profile_status", "estimated_default"),
        "updated_at": updated,
        "source": "用户账户设置；未确认时采用保守默认值",
        "official_boundary": {
            "maximum_rate": OFFICIAL_COMMISSION_MAX_RATE,
            "maximum_wan": OFFICIAL_COMMISSION_MAX_RATE * 10_000,
            "minimum_per_order_reference": 5.0,
            "url": "https://one.sse.com.cn/onething/gptz/",
        },
        "message": (
            "当前为待确认默认值：佣金万3、单笔最低5元。"
            if values.get("fee_profile_status") != "user_confirmed"
            else "已按用户从券商交割单核对的费率保存。"
        ),
    }


def save_fee_profile(
    database: str, commission_wan: float, minimum_commission: float
) -> dict:
    if not 0 <= commission_wan <= OFFICIAL_COMMISSION_MAX_RATE * 10_000:
        raise ValueError("佣金必须在万0至万30之间")
    if not 0 <= minimum_commission <= 20:
        raise ValueError("最低佣金必须在0至20元之间")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    values = {
        "commission_rate": str(commission_wan / 10_000),
        "minimum_commission": str(minimum_commission),
        "fee_profile_status": "user_confirmed",
    }
    with connect(database) as connection:
        for key, value in values.items():
            connection.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, now),
            )
    return fee_profile(database)


def paper_account_status(
    database: str,
    research_gate_passed: bool,
    forward_status: dict | None = None,
) -> dict:
    """Describe the paper account without allowing a legacy history bypass.

    ``research_gate_passed`` is supplied by the product-gate compatibility
    helper, but the status still independently requires the strict certificate
    carried by the immutable forward report.  This prevents a caller from
    turning an old V23 historical boolean into ``ready_for_manual_start``.
    """
    with connect(database) as connection:
        account = connection.execute(
            "SELECT initial_cash, cash, status, started_at, updated_at "
            "FROM paper_account WHERE id = 1"
        ).fetchone()
        signal_count = connection.execute(
            "SELECT COUNT(*) AS count FROM paper_signal_events"
        ).fetchone()["count"]
        open_positions = connection.execute(
            "SELECT COUNT(*) AS count FROM paper_positions WHERE status = 'open'"
        ).fetchone()["count"]
        completed_trades = connection.execute(
            "SELECT COUNT(*) AS count FROM paper_positions WHERE status = 'closed'"
        ).fetchone()["count"]
    stored_status = account["status"]
    strict_certificate_valid = bool(
        forward_status
        and forward_status.get("strict_product_gate_certificate_valid") is True
    )
    product_forward_clock_valid = bool(
        forward_status
        and forward_status.get("product_forward_clock_valid") is True
    )
    formal_forward_running = bool(
        research_gate_passed
        and strict_certificate_valid
        and product_forward_clock_valid
    )
    formal_decision_count = int(
        (forward_status or {}).get("formal_decision_count") or 0
    )
    formal_completed_trades = int(
        (forward_status or {}).get("completed_trades") or 0
    )
    formal_open_positions = list(
        (forward_status or {}).get("open_positions") or []
    )
    realized_net_pnl = float(
        (forward_status or {}).get("realized_net_pnl") or 0
    )
    formal_cash = (
        float(account["initial_cash"])
        + realized_net_pnl
        - sum(
            float(position.get("entry_cost") or 0)
            for position in formal_open_positions
        )
    )
    if formal_forward_running:
        effective_status = str(
            forward_status.get("status") or "formal_forward_validation"
        )
        message = (
            "正式前向模拟盘正在计时；当前按冻结策略记录候选、"
            "主动拒绝和完成交易，历史事件不可修改。"
        )
    elif not strict_certificate_valid:
        effective_status = "shadow_only_product_gate_required"
        message = (
            "严格产品历史门禁证书尚未验证；模拟盘仅保留影子观察，"
            "禁止接收正式交易信号。"
        )
    elif not research_gate_passed:
        effective_status = "strict_product_gate_inconsistent"
        message = (
            "严格产品证书与产品门禁状态不一致，按失败关闭；"
            "禁止接收正式交易信号。"
        )
    elif not product_forward_clock_valid:
        effective_status = "strict_product_gate_attested_waiting_clock"
        message = (
            "严格产品门禁证书已验证；仅系统可按冻结策略启动不可回填的正式时钟，"
            "不提供人工启动入口。"
        )
    else:
        effective_status = stored_status
        message = "模拟盘正在记录所有信号；历史事件不可修改或删除。"
    return {
        "initial_cash": account["initial_cash"],
        "cash": (
            round(formal_cash, 2)
            if formal_forward_running
            else account["cash"]
        ),
        "stored_status": stored_status,
        "status": effective_status,
        "message": message,
        "started_at": (
            forward_status.get("clock_started_at")
            if formal_forward_running
            else account["started_at"]
        ),
        "updated_at": account["updated_at"],
        "signal_count": (
            formal_decision_count if formal_forward_running else signal_count
        ),
        "database_signal_count": signal_count,
        "open_positions": (
            len(formal_open_positions)
            if formal_forward_running
            else open_positions
        ),
        "formal_open_position_items": (
            formal_open_positions if formal_forward_running else []
        ),
        "realized_net_pnl": (
            round(realized_net_pnl, 2)
            if formal_forward_running
            else None
        ),
        "last_completed_trade": (
            (forward_status or {}).get("last_completed_trade")
            if formal_forward_running
            else None
        ),
        "completed_trades": (
            formal_completed_trades
            if formal_forward_running
            else completed_trades
        ),
        "database_completed_trades": completed_trades,
        "formal_decision_count": formal_decision_count,
        "rejected_decision_count": int(
            (forward_status or {}).get("rejected_decision_count") or 0
        ),
        "last_formal_decision": (
            (forward_status or {}).get("last_formal_decision")
        ),
        "forward_ledger": (
            (forward_status or {}).get("ledger")
            if formal_forward_running
            else None
        ),
        "validation_target": {
            "weeks_min": 6,
            "weeks_max": 8,
            "completed_trades_min": 30,
            "signals_append_only": True,
        },
    }


def paper_signals(database: str, limit: int = 100) -> list[dict]:
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT id, fingerprint, strategy_id, code, name, action, signal_price, "
            "suggested_quantity, reason, data_time, data_source, "
            "research_gate_status, created_at FROM paper_signal_events "
            "ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [dict(row) for row in rows]
