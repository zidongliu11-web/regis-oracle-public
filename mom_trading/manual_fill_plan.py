"""Immutable plan copies and explicit retrospective links; never trading authority."""
import hashlib
import json
import math
import re

from . import timing_engine as ledger, manual_fill_time


def snapshot(alert):
    raw = alert.get("action_plan_json")
    if not isinstance(raw, str) or len(raw) > 200_000:
        raise ValueError("该提醒没有可保存的完整计划")
    try:
        plan = json.loads(raw)
        ledger._canonical_action_plan(alert["action"], plan)
        action = plan["trade_plan"]["action"]
        for key in ("limit_price", "estimated_cost"):
            if isinstance(action[key], bool) or not math.isfinite(float(action[key])):
                raise ValueError
        manual_fill_time._timestamp(action["valid_until"])
        manual_fill_time._timestamp(alert["created_at"])
    except (ValueError, TypeError, KeyError):
        raise ValueError("原提醒计划缺少可信数量、有限价格、期限或来源，不能补成完整计划") from None
    value = {key: alert.get(key) for key in (
        "id", "code", "name", "action", "created_at", "updated_at", "valid_until",
        "origin", "strategy_id", "signal_id", "evidence_ref_json", "reason",
    )}
    value["action_plan"] = plan
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return canonical, hashlib.sha256(canonical.encode()).hexdigest()


def fill(conn, fill_id):
    if not isinstance(fill_id, str) or not re.fullmatch(r"(?:buy|sell):[1-9][0-9]{0,9}", fill_id):
        raise ValueError("请选择已存在的原始成交")
    side, identity = fill_id.split(":")
    if side == "buy":
        row = conn.execute("SELECT * FROM timing_positions WHERE id=?", (int(identity),)).fetchone()
        if row is None or ledger.is_position_snapshot(dict(row)):
            raise ValueError("原始买入不存在；持仓快照不是成交")
        return {"code": row["code"], "side": side, "position_id": row["id"]}
    row = conn.execute("SELECT e.position_id,p.code FROM timing_sell_events e JOIN timing_positions p ON p.id=e.position_id WHERE e.id=?", (int(identity),)).fetchone()
    if row is None:
        raise ValueError("原始卖出事件不存在")
    return {**dict(row), "side": side}


def capture_confirmation(conn, alert, position_id, sell_event_id):
    """Called inside the existing fill/decision transaction, before alert mutation."""
    if not alert.get("action_plan_json"):
        return  # Legacy identifier-only confirmations remain explicitly incomplete.
    fill_id = f"sell:{sell_event_id}" if sell_event_id is not None else f"buy:{position_id}"
    facts = fill(conn, fill_id)
    if (alert["code"], alert["action"]) != (facts["code"], facts["side"]):
        raise ValueError("冻结计划与实际成交证券或方向不符")
    raw, sha = snapshot(alert)
    conn.execute("""INSERT INTO timing_fill_plan_events
        (fill_id,alert_id,plan_snapshot_json,plan_sha256,source,recorded_at,note,idempotency_key)
        VALUES (?,?,?,?,?,?,?,?)""", (fill_id, alert["id"], raw, sha, "confirmation_snapshot",
        ledger._now_sh().isoformat(timespec="seconds"), "原子成交确认时保存；不代替券商成交与策略证据核验", f"confirmation:{fill_id}"))


def list_events():
    conn = ledger._db()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM timing_fill_plan_events ORDER BY id")]
    finally:
        conn.close()


def choices(fill_id):
    conn = ledger._db()
    try:
        ledger._db_init_alerts(conn)
        conn.commit()
        facts = fill(conn, fill_id)
        result = []
        for row in conn.execute("SELECT * FROM timing_alerts WHERE code=? AND action=? AND action_plan_json IS NOT NULL ORDER BY id DESC LIMIT 200", (facts["code"], facts["side"])):
            try:
                raw, sha = snapshot(dict(row))
            except ValueError:
                continue
            result.append({"alert_id": row["id"], "plan_sha256": sha, "snapshot": json.loads(raw)})
        return result
    finally:
        conn.close()


def record(fill_id, alert_id, expected_plan_sha256, note, expected_event_id, idempotency_key):
    if alert_id is not None and (type(alert_id) is not int or not 1 <= alert_id <= 2_147_483_647):
        raise ValueError("计划提醒编号无效")
    if (alert_id is None and expected_plan_sha256 is not None) or (alert_id is not None and not re.fullmatch(r"[a-f0-9]{64}", str(expected_plan_sha256))):
        raise ValueError("缺少所选计划的版本身份，请刷新")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 256:
        raise ValueError("请填写1至256字核对依据或更正理由，不要填写账户号或密码")
    if type(expected_event_id) is not int or expected_event_id < 0:
        raise ValueError("缺少当前关联版本")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise ValueError("缺少有效幂等标识")
    note = note.strip()
    conn = ledger._db()
    try:
        ledger._db_init_alerts(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        replay = conn.execute("SELECT * FROM timing_fill_plan_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if replay:
            if (replay["fill_id"], replay["alert_id"], replay["plan_sha256"], replay["note"], replay["supersedes_id"] or 0) != (fill_id, alert_id, expected_plan_sha256, note, expected_event_id):
                raise ledger.IdempotencyConflictError("幂等标识已用于其他计划关联")
            conn.rollback()
            return {**dict(replay), "idempotent_replay": True}
        facts = fill(conn, fill_id)
        previous = conn.execute("SELECT id FROM timing_fill_plan_events WHERE fill_id=? ORDER BY id DESC LIMIT 1", (fill_id,)).fetchone()
        if (previous["id"] if previous else 0) != expected_event_id:
            raise ledger.IdempotencyConflictError("成交关联已变化，请刷新后核对")
        raw, sha = None, None
        if alert_id is not None:
            row = conn.execute("SELECT * FROM timing_alerts WHERE id=?", (alert_id,)).fetchone()
            if row is None or (row["code"], row["action"]) != (facts["code"], facts["side"]):
                raise ValueError("原提醒不存在或与成交证券、方向不符")
            raw, sha = snapshot(dict(row))
            if sha != expected_plan_sha256:
                raise ledger.IdempotencyConflictError("原计划内容已变化，请重新读取，不使用过期版本关联")
        cur = conn.execute("""INSERT INTO timing_fill_plan_events
            (fill_id,alert_id,plan_snapshot_json,plan_sha256,source,recorded_at,supersedes_id,note,idempotency_key)
            VALUES (?,?,?,?,?,?,?,?,?)""", (fill_id, alert_id, raw, sha, "user_reported_link",
            ledger._now_sh().isoformat(timespec="seconds"), expected_event_id or None, note, idempotency_key))
        result = dict(conn.execute("SELECT * FROM timing_fill_plan_events WHERE id=?", (cur.lastrowid,)).fetchone())
        conn.commit()
        return {**result, "idempotent_replay": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def records(positions, events):
    """Compare saved plans to facts, not to today's quote or live alert version."""
    rows = manual_fill_time.records(positions, [])
    histories = {}
    for event in events:
        histories.setdefault(event["fill_id"], []).append(event)
    quantities, versions, invalid_quantities = {}, {}, set()
    for row in rows:
        row["history"] = histories.get(row["fill_id"], [])
        row["fact"] = row["history"][-1] if row["history"] else None
        event = row["fact"]
        if event and event["alert_id"]:
            key = event["alert_id"]
            if type(row["shares"]) is not int or row["shares"] <= 0:
                invalid_quantities.add(key)
            else:
                quantities[key] = quantities.get(key, 0) + row["shares"]
            versions.setdefault(key, set()).add(event["plan_sha256"])
    time_facts = {f"buy:{p['id']}": p.get("execution_time_fact") for p in positions}
    time_facts.update({f"sell:{e['id']}": e.get("execution_time_fact") for p in positions for e in p.get("sell_events") or []})
    for row in rows:
        event = row["fact"]
        row["executed_at"] = (time_facts.get(row["fill_id"]) or {}).get("executed_at")
        result = {"status": "unlinked", "label": "未关联冻结计划 · 不归因为系统战法",
                  "strategy_qualification_granted": False, "automatic_orders": False,
                  "unverified_conditions": "情绪、板块、取消条件及实际送达仍须原始时点证据，不由数量价格匹配推定"}
        row["comparison"] = result
        if not event or event["alert_id"] is None:
            continue
        try:
            raw = event["plan_snapshot_json"]
            if hashlib.sha256(raw.encode()).hexdigest() != event["plan_sha256"]:
                raise ValueError
            saved = json.loads(raw)
            action = saved["action_plan"]["trade_plan"]["action"]
            if event["alert_id"] in invalid_quantities:
                raise ValueError
            if saved["code"] != row["code"] or saved["action"] != row["fill_id"].split(":")[0]:
                raise ValueError
            total, planned = quantities[event["alert_id"]], action["shares"]
            limit = float(action["limit_price"])
            price = float(row["price"])
            if not math.isfinite(limit) or not math.isfinite(price) or type(planned) is not int or planned <= 0:
                raise ValueError
            quantity_status = "partial" if total < planned else "matched" if total == planned else "exceeded"
            price_status = "within_limit" if (price <= limit + 0.005 if saved["action"] == "buy" else price >= limit - 0.005) else "outside_limit"
            time = (time_facts.get(row["fill_id"]) or {}).get("executed_at")
            time_status = "unknown"
            if time:
                executed = manual_fill_time._timestamp(time)
                created = manual_fill_time._timestamp(saved["created_at"])
                deadline = manual_fill_time._timestamp(action["valid_until"])
                if saved.get("valid_until"):
                    deadline = min(deadline, manual_fill_time._timestamp(saved["valid_until"]))
                updated = manual_fill_time._timestamp(saved.get("updated_at") or saved["created_at"])
                time_status = "before_plan" if executed < created else "after_expiry" if executed > deadline else "version_unproven" if executed < updated else "within_window"
            mixed = len(versions[event["alert_id"]]) != 1
            result.update({"status": "comparison_only", "alert_id": event["alert_id"], "source": event["source"],
                "plan_sha256": event["plan_sha256"], "snapshot": saved, "linked_total_shares": total,
                "planned_shares": planned, "planned_limit_price": limit, "quantity_status": quantity_status,
                "price_status": price_status, "time_status": time_status, "mixed_plan_versions": mixed,
                "label": "；".join([
                    "后补关联（非当时执行证据）" if event["source"] == "user_reported_link" else "确认时保存的计划",
                    {"partial": "关联成交尚不足计划数量", "matched": "关联数量一致", "exceeded": "关联成交超过计划数量"}[quantity_status],
                    "成交价在限价内" if price_status == "within_limit" else "成交价越过限价",
                    {"unknown": "成交时间待补记", "before_plan": "成交早于计划", "after_expiry": "成交晚于计划期限", "version_unproven": "当时计划版本未证实", "within_window": "填报时间在计划窗口内"}[time_status],
                    "同一提醒存在多个计划版本，不能合并认定按计划执行" if mixed else "未核验全部取消条件",
                ])})
        except (ValueError, TypeError, KeyError, AttributeError):
            result.update(status="invalid", label="关联计划快照损坏或不完整 · 不能核对执行")
    return rows
