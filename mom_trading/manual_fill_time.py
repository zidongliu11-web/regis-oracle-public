"""Append-only user-reported execution times; never rebook cash or trade authority."""
from __future__ import annotations

from datetime import datetime
import re

from . import timing_engine as ledger


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("成交时间须为带时区的完整日期时间") from None
    if parsed.tzinfo is None:
        raise ValueError("成交时间必须明确时区；页面按上海时间录入")
    return parsed.astimezone(ledger._SH_TZ)


def list_events():
    conn = ledger._db()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM timing_fill_time_events ORDER BY id")]
    finally:
        conn.close()


def records(positions, events):
    """Expose both clocks and every correction without mutating ledger snapshots."""
    by_fill = {}
    for event in events:
        by_fill.setdefault(event["fill_id"], []).append(event)
    result = []
    for position in positions:
        fills = [(f"sell:{e['id']}", e.get("sold_at"), e.get("shares"), e.get("price"))
                 for e in position.get("sell_events") or []]
        if not ledger.is_position_snapshot(position):
            fills.insert(0, (f"buy:{position['id']}", position.get("created_at"), position.get("initial_shares"), position.get("price")))
        for fill_id, recorded_at, shares, price in fills:
            history = by_fill.get(fill_id, [])
            result.append({"fill_id": fill_id, "position_id": position["id"],
                           "code": position.get("code"), "name": position.get("name"),
                           "shares": shares, "price": price, "recorded_at": recorded_at,
                           "fact": history[-1] if history else None, "history": history})
    return result


def annotate(positions, events):
    latest = {event["fill_id"]: event for event in events}
    return [{**p, "execution_time_fact": latest.get(f"buy:{p['id']}"),
             "sell_events": [{**e, "execution_time_fact": latest.get(f"sell:{e['id']}")}
                             for e in p.get("sell_events") or []]} for p in positions]


def review_time(row, recorded_field, basis):
    return ((row.get("execution_time_fact") or {}).get("executed_at")
            if basis == "execution" else row.get(recorded_field)) or ""


def unassigned_may_belong(row, recorded_field, day):
    if row.get("execution_time_fact"):
        return False
    try:
        recorded_day = _timestamp(row.get(recorded_field)).date().isoformat()
    except ValueError:
        # An absent or invalid recording clock cannot exclude a legacy fill.
        return True
    return recorded_day >= day


def record(fill_id, executed_at, note, expected_event_id, idempotency_key):
    if not isinstance(fill_id, str) or not re.fullmatch(r"(?:buy|sell):[1-9][0-9]{0,9}", fill_id):
        raise ValueError("必须选择一笔已存在的买入记录或卖出事件")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 256:
        raise ValueError("请用1至256字记录核对依据或更正理由；不要填写账户号或密码")
    if type(expected_event_id) is not int or expected_event_id < 0:
        raise ValueError("缺少当前时间记录版本，请刷新后重试")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise ValueError("缺少有效幂等标识")
    time = _timestamp(executed_at)
    now = ledger._now_sh()
    if time > now:
        raise ValueError("不能记录尚未发生的成交时间")
    normalized, note = time.isoformat(timespec="seconds"), note.strip()
    conn = ledger._db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replay = conn.execute("SELECT * FROM timing_fill_time_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if replay:
            if (replay["fill_id"], replay["executed_at"], replay["note"], replay["supersedes_id"] or 0) != (fill_id, normalized, note, expected_event_id):
                raise ledger.IdempotencyConflictError("幂等标识已用于另一项成交时间事实")
            conn.rollback()
            return {**dict(replay), "idempotent_replay": True}
        side, identity = fill_id.split(":")
        if side == "buy":
            row = conn.execute("SELECT * FROM timing_positions WHERE id=?", (int(identity),)).fetchone()
            if row is None or ledger.is_position_snapshot(dict(row)):
                raise ValueError("原始买入记录不存在；持仓快照不能冒充成交")
            position_id, recorded_at = row["id"], row["created_at"]
        else:
            row = conn.execute("SELECT * FROM timing_sell_events WHERE id=?", (int(identity),)).fetchone()
            if row is None:
                raise ValueError("原始卖出事件不存在")
            position_id, recorded_at = row["position_id"], row["sold_at"]
        if time > _timestamp(recorded_at):
            raise ValueError("实际成交时间不能晚于原始录入时间；若原记录有误，须先核对原成交事实")
        latest = {r["fill_id"]: dict(r) for r in conn.execute("SELECT * FROM timing_fill_time_events ORDER BY id")}
        previous = latest.get(fill_id)
        if (previous["id"] if previous else 0) != expected_event_id:
            raise ledger.IdempotencyConflictError("成交时间已被另一操作补记或更正，请刷新后核对")
        latest[fill_id] = {"executed_at": normalized}
        buy_time = latest.get(f"buy:{position_id}")
        if buy_time:
            for sell in conn.execute("SELECT id FROM timing_sell_events WHERE position_id=?", (position_id,)):
                sell_time = latest.get(f"sell:{sell['id']}")
                if sell_time and sell_time["executed_at"] < buy_time["executed_at"]:
                    raise ValueError("卖出成交时间不能早于这笔持仓的买入成交时间")
        cur = conn.execute("""INSERT INTO timing_fill_time_events
            (fill_id, executed_at, recorded_at, supersedes_id, note, idempotency_key)
            VALUES (?,?,?,?,?,?)""", (fill_id, normalized, now.isoformat(timespec="seconds"), expected_event_id or None, note, idempotency_key))
        result = dict(conn.execute("SELECT * FROM timing_fill_time_events WHERE id=?", (cur.lastrowid,)).fetchone())
        conn.commit()
        return {**result, "idempotent_replay": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
