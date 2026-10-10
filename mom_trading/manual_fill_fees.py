"""Append actual fee corrections and a fresh user-confirmed cash snapshot.

Original fills are never rewritten. Restated fees belong to manual review,
not to point-in-time strategy evidence or broker-verified account returns.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re

from . import manual_fill_time
from . import timing_engine as ledger

CENT = Decimal("0.01")


def money(value, *, maximum=Decimal("10000000000")):
    try:
        if value is None or isinstance(value, bool) or str(value).strip() == "":
            raise InvalidOperation
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= maximum or amount != amount.quantize(CENT):
            raise InvalidOperation
        return amount.quantize(CENT)
    except (InvalidOperation, ValueError):
        raise ValueError("金额须为明确的非负人民币数值，最多两位小数；空白不等于0") from None


def cash_identity(account):
    fields = ("cash_balance", "updated_at", "source", "initial_equity_yuan", "initial_equity_as_of", "initial_equity_source", "initial_equity_recorded_at")
    encoded = json.dumps({k: account.get(k) for k in fields} if account else None, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def list_events():
    conn = ledger._db()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM timing_fill_fee_events ORDER BY id")]
    finally:
        conn.close()


def public_event(event):
    return {key: event[key] for key in ("id", "fill_id", "fee_yuan", "recorded_at", "supersedes_id", "note")}


def records(positions, events):
    by_fill = {}
    for event in events:
        by_fill.setdefault(event["fill_id"], []).append(public_event(event))
    original = {}
    for p in positions:
        original[f"buy:{p['id']}"] = (p.get("buy_cost"), p.get("buy_cost_source"))
        for e in p.get("sell_events") or []:
            original[f"sell:{e['id']}"] = (e.get("sell_cost"), e.get("sell_cost_source"))
    rows = manual_fill_time.records(positions, [])
    for row in rows:
        row["original_fee_yuan"], row["original_fee_source"] = original[row["fill_id"]]
        row["history"] = by_fill.get(row["fill_id"], [])
        row["fact"] = row["history"][-1] if row["history"] else None
    return rows


def record(fill_id, fee_yuan, note, expected_event_id, idempotency_key, remaining_cash_yuan, expected_cash_identity):
    if not isinstance(fill_id, str) or not re.fullmatch(r"(?:buy|sell):[1-9][0-9]{0,9}", fill_id):
        raise ValueError("必须选择一笔已存在的买入或卖出成交")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 256:
        raise ValueError("请填写1至256字核对依据，不要填写账户号或密码")
    if type(expected_event_id) is not int or expected_event_id < 0:
        raise ValueError("缺少当前费用版本，请刷新核对")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise ValueError("缺少有效幂等标识")
    if not isinstance(expected_cash_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_cash_identity):
        raise ValueError("缺少当前现金快照身份，请刷新核对")
    fee = float(money(fee_yuan, maximum=Decimal("100000")))
    cash = float(money(remaining_cash_yuan))
    note = note.strip()
    conn = ledger._db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replay = conn.execute("SELECT * FROM timing_fill_fee_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if replay:
            if (replay["fill_id"], replay["fee_yuan"], replay["note"], replay["supersedes_id"] or 0,
                    replay["confirmed_cash_yuan"], replay["cash_identity"]) != (fill_id, fee, note, expected_event_id, cash, expected_cash_identity):
                raise ledger.IdempotencyConflictError("幂等标识已用于另一项费用或现金确认")
            conn.rollback()
            account = conn.execute("SELECT * FROM timing_account WHERE id=1").fetchone()
            return {**public_event(replay), "idempotent_replay": True,
                    "account_identity": cash_identity(dict(account) if account else None)}
        side, identity = fill_id.split(":")
        if side == "buy":
            row = conn.execute("SELECT * FROM timing_positions WHERE id=?", (int(identity),)).fetchone()
            if row is None or ledger.is_position_snapshot(dict(row)):
                raise ValueError("原始买入不存在；导入持仓不能补造原始费用")
            quantity = row["initial_shares"]
        else:
            row = conn.execute("SELECT * FROM timing_sell_events WHERE id=?", (int(identity),)).fetchone()
            if row is None:
                raise ValueError("原始卖出事件不存在")
            quantity = row["shares"]
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("原始成交股数缺失或无效，不能补记费用")
        try:
            notional = Decimal(str(row["price"])) * quantity
        except InvalidOperation:
            raise ValueError("原始成交价无效") from None
        if not notional.is_finite() or notional <= 0 or Decimal(str(fee)) > max(Decimal("100"), notional * Decimal("0.1")):
            raise ValueError("费用超出原始成交的合理范围，请核对币种与单位")
        previous = conn.execute("SELECT id FROM timing_fill_fee_events WHERE fill_id=? ORDER BY id DESC LIMIT 1", (fill_id,)).fetchone()
        if (previous["id"] if previous else 0) != expected_event_id:
            raise ledger.IdempotencyConflictError("费用已被另一操作更正，请重新打开核对")
        account = conn.execute("SELECT * FROM timing_account WHERE id=1").fetchone()
        if cash_identity(dict(account) if account else None) != expected_cash_identity:
            raise ledger.IdempotencyConflictError("现金账本已变化，请重新打开并核对当前真实剩余现金")
        stamp = ledger._now_sh().isoformat(timespec="seconds")
        result = conn.execute("""INSERT INTO timing_fill_fee_events
            (fill_id,fee_yuan,recorded_at,supersedes_id,note,idempotency_key,cash_identity,previous_cash_yuan,confirmed_cash_yuan)
            VALUES (?,?,?,?,?,?,?,?,?)""", (fill_id, fee, stamp, expected_event_id or None, note, idempotency_key,
                                            expected_cash_identity, account["cash_balance"] if account else None, cash))
        conn.execute("""INSERT INTO timing_account (id,cash_balance,updated_at,source) VALUES (1,?,?, 'manual_snapshot')
            ON CONFLICT(id) DO UPDATE SET cash_balance=excluded.cash_balance,updated_at=excluded.updated_at,source=excluded.source""", (cash, stamp))
        event = dict(conn.execute("SELECT * FROM timing_fill_fee_events WHERE id=?", (result.lastrowid,)).fetchone())
        account = dict(conn.execute("SELECT * FROM timing_account WHERE id=1").fetchone())
        conn.commit()
        return {**public_event(event), "idempotent_replay": False, "account_identity": cash_identity(account)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def from_snapshot(positions, snapshot):
    """Join fills, optional execution clocks and fees from one read transaction."""
    by_position = {}
    for event in snapshot.get("sell_rows", []):
        by_position.setdefault(event["position_id"], []).append(event)
    rows = [{**row, "sell_events": by_position.get(row["id"], row.get("sell_events", []))} for row in positions]
    if "time_rows" in snapshot:
        rows = manual_fill_time.annotate(rows, snapshot["time_rows"])
    return annotate(rows, snapshot.get("fee_rows", []))


def annotate(positions, events):
    """Restate copies for review; retain originals and never conceal broken fills."""
    latest = {event["fill_id"]: event for event in events}
    result = []
    for original in positions:
        p = {**original, "sell_events": [dict(e) for e in original.get("sell_events") or []]}
        result.append(p)
        buy = latest.get(f"buy:{p['id']}")
        exits = p["sell_events"]
        if not buy and not any(f"sell:{e['id']}" in latest for e in exits):
            continue
        p["recorded_buy_cost"] = p.get("buy_cost")
        p["fee_correction_applied"] = True
        if buy:
            p["buy_cost"], p["buy_cost_source"] = buy["fee_yuan"], "actual_user_recorded"
        p["buy_fee_fact"] = public_event(buy) if buy else None
        try:
            initial = p.get("initial_shares")
            if type(initial) is not int or initial <= 0:
                raise ValueError("原始买入数量缺失")
            total_buy_fee = money(p.get("buy_cost"))
            used, allocated = 0, Decimal(0)
            for e in sorted(exits, key=lambda e: e["id"]):
                sell = latest.get(f"sell:{e['id']}")
                e["recorded_costs"] = {k: e.get(k) for k in ("allocated_buy_cost", "sell_cost", "net_pnl")}
                e["buy_fee_fact"] = p["buy_fee_fact"]
                e["sell_fee_fact"] = public_event(sell) if sell else None
                shares = e.get("shares")
                if type(shares) is not int or shares <= 0 or e.get("position_id") != p["id"]:
                    raise ValueError("原始卖出数量或身份不一致")
                used += shares
                if used > initial:
                    raise ValueError("累计卖出超过原始买入数量")
                # Fee correction must not launder a wrong price, gross or net record.
                gross = Decimal(str(e["gross_pnl"]))
                old_net = Decimal(str(e["net_pnl"]))
                old_fees = money(e["allocated_buy_cost"]) + money(e["sell_cost"])
                expected_gross = (Decimal(str(e["price"])) - Decimal(str(p["price"]))) * shares
                if not all(x.is_finite() for x in (gross, old_net, expected_gross)) or abs(expected_gross-gross) > CENT or abs(gross-old_fees-old_net) > Decimal("0.015"):
                    raise ValueError("原始成交金额或净收益不一致，费用更正不能修复原始成交")
                cumulative = (total_buy_fee * used / initial).quantize(CENT, rounding=ROUND_HALF_UP)
                e["allocated_buy_cost"] = float(cumulative-allocated)
                allocated = cumulative
                if sell:
                    e["sell_cost"], e["sell_cost_source"] = sell["fee_yuan"], "actual_user_recorded"
                e["net_pnl"] = float((gross-money(e["allocated_buy_cost"])-money(e["sell_cost"])).quantize(CENT, rounding=ROUND_HALF_UP))
            if type(p.get("shares")) is not int or used + p["shares"] != initial or (p.get("status") == "closed" and used != initial):
                raise ValueError("原始持仓与逐笔卖出数量未闭合")
            if exits:
                p["pnl"] = float(sum(Decimal(str(e["net_pnl"])) for e in exits))
                p["sell_cost"] = float(sum(money(e["sell_cost"]) for e in exits))
                sources = {e.get("sell_cost_source") for e in exits}
                p["sell_cost_source"] = sources.pop() if len(sources) == 1 else "mixed_actual_and_estimated"
        except (ValueError, InvalidOperation, KeyError, TypeError) as exc:
            p["fee_correction_error"] = str(exc) if isinstance(exc, ValueError) else "原始费用或成交字段无效"
            p["pnl"] = None
            for e in exits:
                e["fee_correction_error"] = p["fee_correction_error"]
    return result
