"""Correct confirmed suspension inputs, preserving every frozen control signal."""
from collections import Counter
from copy import copy
import json
import math

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import control_increment_research as controls
from . import control_corporate_evidence as corporate
from .cash_conditioned_research import reconcile_closed
from .emotion_pattern_cash_settlement import replay_cash_event
from .emotion_pattern_replay import EventMarket
from .workbench_artifact_paths import research_artifact_path
from .suspension_liquidity import SPEC as INPUT_SPEC, suspension_adv_view
from research.run_emotion_pattern_cash_settlement import action_evidence, original_candidate
from research.run_emotion_control_cash_settlement import repair_control

ARTIFACTS = (
    ("research/data/state_conditioned_mainline_swing_v1/stock_features.parquet", "8162b2d46851c991fbb68ee789182d21587ca7207659a69696f28ed9a7955c95"),
    ("research/backtest_project/data/universe_daily.parquet", "505fefc847edbfa4cefaaf56a50ba9636e50098a88fbbabb738103ea30849495"),
    ("research/data/special_price_limit_events.csv", "1059fbc5e78eec32d5ed215dc7acf1ef61e26dbd4f8179077ce9b2326842ea38"),
    ("research/data/v184_cninfo_cash_dividend/query_manifest.json", "f0e754c567a20770c781e73759f49239d293769f1f16e35c7ba313954e0eb358"),
)
SPEC = {"schema": "control_suspension_input_repair.v1", "execution_input": INPUT_SPEC,
        "corporate_input_audit": corporate.SPEC,
        "scope": "all_saved_exit_lagged_ADV_missing_paths_all_three_costs",
        "baseline": "reproduce_original_price_and_cash_paths_before_correction",
        "unchanged": "all_other_event_bytes_and_signal_factor_groups",
        "checks": "independent_20_prior_session_sum_order_prefix_cash_and_inventory",
        "source_boundary": "same_frozen_canonical_lineage_not_independent_market_feed",
        "new_blind_trial": False, "production_eligible": False, "generates_alert": False}


def read_selected(path, columns, field, values):
    # One bounded scan per source. Reopening Dataset per stock retains large
    # decoder/metadata allocations; only matching rows survive each batch.
    parts, allowed = [], pa.array(values, type=pa.string())
    with pq.ParquetFile(path, pre_buffer=False, buffer_size=65536) as file:
        for batch in file.iter_batches(batch_size=8192, columns=columns, use_threads=False):
            selected = batch.filter(pc.is_in(batch.column(field), value_set=allowed))
            if selected.num_rows:
                parts.append(selected.to_pandas())
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)


def audit_repairs(event, canonical, sessions):
    """Sum source windows independently of the correction function."""
    rows = canonical.assign(day=pd.to_datetime(canonical.trade_date).dt.strftime("%Y-%m-%d")).set_index("day")
    for receipt in event["liquidity_repairs"]:
        i = sessions.index(receipt["execution_day"])
        days = sessions[max(0, i-20):i]
        if len(days) != 20 or receipt["denominator"] != 20 or (days[0], days[-1]) != (receipt["window_start"], receipt["window_end"]):
            raise ValueError("control_liquidity_window_mismatch")
        sample = rows.loc[days]
        suspended = sample.trade_status.eq(0)
        if (not sample.trade_status.isin([0, 1]).all() or not suspended.any()
                or not sample.loc[suspended, ["amount", "vol"]].fillna(0).eq(0).all().all()
                or not sample.loc[~suspended, ["amount", "vol"]].notna().all().all()
                or not sample.loc[~suspended, ["amount", "vol"]].ge(0).all().all()
                or receipt["confirmed_no_trade_days"] != list(sample.index[suspended])):
            raise ValueError("control_liquidity_source_window_invalid")
        expected = math.fsum(sample.amount.mask(suspended, 0)) / 20
        if not math.isclose(receipt["ADV_yuan"], expected, rel_tol=1e-12):
            raise ValueError("control_liquidity_ADV_mismatch")
    for order in event["execution_events"]:
        if order["shares"] and order["price"] != rows.loc[order["day"], "open"]:
            raise ValueError("control_liquidity_price_changed")


def repair_event(old_price, old_cash, market, canonical, evidence, *, source_complete):
    if old_cash["status"] != "unresolved" or old_cash["reason"] != "exit_lagged_ADV_missing":
        raise ValueError("control_liquidity_not_failed_exit")
    if repair_control(old_price, market, evidence, source_complete=source_complete) != old_cash:
        raise ValueError("control_liquidity_prior_cash_not_reproduced")
    revised, receipts = suspension_adv_view(market, canonical)
    candidate = original_candidate(old_price, market)
    if old_cash["track"] in {"no_emotion", "same_entries_no_emotion_exit"}:
        allowed = next(p["states"][0] for p in controls.PLAYBOOKS if p["id"] == old_cash["playbook_id"])
        revised = copy(revised)
        revised.states = dict.fromkeys(market.sessions, allowed)
        candidate["confirmed_state"] = allowed
    result = replay_cash_event(candidate, revised, evidence, bps=old_cash["slippage_bps"], source_complete=source_complete)
    event = {**old_cash, **result, "signal_emotion": old_cash["signal_emotion"]}
    if (event["entry_day"] != old_cash["entry_day"] or event["simulated_entry_shares"] != old_cash["simulated_entry_shares"]
            or event["execution_events"][:len(old_cash["execution_events"])] != old_cash["execution_events"]):
        raise ValueError("control_liquidity_changed_prior_orders")
    if all(event.get(k) == old_cash.get(k) for k in ("status", "reason", "execution_events")):
        return old_cash, None
    used = {o["day"] for o in event["execution_events"]} & receipts.keys()
    if not used:
        raise ValueError("control_liquidity_change_without_receipt")
    event["liquidity_repairs"] = [receipts[day] for day in sorted(used)]
    audit_repairs(event, canonical, market.sessions)
    if event["status"] == "closed":
        reconcile_closed(event, {event["code"]: evidence}, market.sessions)
    elif event.get("net_pnl_yuan") is not None or event.get("fixed_budget_return") is not None:
        raise ValueError("control_liquidity_unknown_return_filled")
    return event, event  # Full revised orders and cash remain inspectable.


def repair_inputs(connection, paths, references, input_root, cash, progress, *, distribution_notices=None):
    from .research_workbench import digest
    if len(paths) != 11 or any(ref not in cash["bindings"] for ref in (references[7], references[9], references[10])):
        raise ValueError("control_liquidity_source_bindings_invalid")
    # Filter in the on-disk join instead of loading every event's full orders.
    failures = connection.execute("SELECT old,new FROM events WHERE json_extract(new,'$.status')='unresolved' AND json_extract(new,'$.reason') IN (?,?) ORDER BY track,playbook,day,code,bps",
                                  ('exit_lagged_ADV_missing', corporate.REASON)).fetchall()
    by_code = {}
    for a, b in failures:
        old, new = json.loads(a), json.loads(b)
        by_code.setdefault(new["code"], []).append((old, new))
    manifest = json.loads(paths[10].read_text())["queries"]
    records = {r["code"]: r for r in manifest}
    if len(records) != len(manifest):
        raise ValueError("control_liquidity_duplicate_response")
    all_days = json.loads(paths[6].read_text())["trading_dates"]
    sessions = [d for d in all_days if "2021-01-04" <= d <= "2025-12-31"]
    indices = {pd.Timestamp(d): i for i, d in enumerate(all_days)}
    states = {r["trade_date"]: r["features"].get("confirmed_state", "UNKNOWN") for r in json.loads(paths[5].read_text())["rows"]}
    special = pd.read_csv(paths[9], dtype=str)
    code6 = lambda c: c.split(".")[-1] if c.startswith(("sh.", "sz.")) else c.split(".")[0]
    exceptions = {(code6(r.code), pd.Timestamp(r.date)) for r in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
    details, checked_responses, transitions = [], [], Counter()
    corporate_records, corporate_actions, liquidity_count = [], {}, 0
    distribution_events = []
    columns = sorted((EventMarket.REQUIRED - {"no_price_limit", "lagged_adv"}) | {"adv20", "history_sessions", "session_index"})
    stocks = read_selected(paths[7], columns, "code", sorted(by_code))
    liquidity_codes = [c for c, events in by_code.items() if any(e['reason'] == 'exit_lagged_ADV_missing' for _, e in events)]
    canonical_rows = read_selected(paths[8], ["ts_code", "trade_date", "stable_instrument_id", "trade_status", "amount", "vol", "open"],
                                   "ts_code", [f"{c}.{'SH' if c.startswith('6') else 'SZ'}" for c in sorted(liquidity_codes)])
    for code, events in sorted(by_code.items()):
        record = records.get(code)
        if record is None:
            raise ValueError("control_liquidity_official_response_missing")
        ref = {k: record[k] for k in ("path", "sha256")}
        path = research_artifact_path(ref["path"], project_root=input_root)
        if (not path.resolve().is_relative_to((paths[10].parent / "responses").resolve())
                or ref not in cash["bindings"] or digest(path) != ref["sha256"]):
            raise ValueError("control_liquidity_response_identity_invalid")
        response = json.loads(path.read_text())
        if response.get("code") != code or response.get("status") not in {"ok", "empty"} or any(r.get("code") != code for r in response.get("rows", [])):
            raise ValueError("control_liquidity_response_payload_invalid")
        checked_responses.append((path, ref))
        stock = stocks.loc[stocks.code.eq(code)].sort_values("trade_date").copy()
        if not stock.trade_date.map(indices).eq(stock.session_index).all():
            raise ValueError("control_liquidity_calendar_mismatch")
        stock["lagged_adv"] = stock.adv20.shift(1).where(stock.session_index.diff().eq(1))
        stock["no_price_limit"] = [h <= 5 or (code, d) in exceptions for d, h in zip(stock.trade_date, stock.history_sessions)]
        market = EventMarket(stock, sessions, states)
        evidence = action_evidence(market, response.get("rows", []))
        canonical = canonical_rows.loc[canonical_rows.ts_code.eq(f"{code}.{'SH' if code.startswith('6') else 'SZ'}")]
        for old_price, old_cash in events:
            if old_cash['reason'] == corporate.REASON:
                record, action = corporate.audit_event(old_price, old_cash, market, evidence, ref)
                key = record['action_key']
                if key in corporate_actions and corporate_actions[key] != action:
                    raise ValueError('control_corporate_action_identity_conflict')
                corporate_records.append(record)
                corporate_actions[key] = action
                notices = (distribution_notices or {}).get(code, {})
                if record['failure_day'] in notices:
                    from .control_distribution_research import repair_event as repair_distribution
                    if any(n['summary_source'] != ref for n in notices.values()):
                        raise ValueError('distribution_summary_source_identity_invalid')
                    distribution_events.append(repair_distribution(old_price,old_cash,market,evidence,notices))
                continue  # Audit never edits this row or any return label.
            liquidity_count += 1
            event, receipt = repair_event(old_price, old_cash, market, canonical, evidence, source_complete=True)
            transitions[old_cash["track"], old_cash["slippage_bps"], event["status"], event["reason"]] += 1
            if receipt is not None:
                details.append(receipt)
                connection.execute("UPDATE events SET new=? WHERE track=? AND playbook=? AND day=? AND code=? AND bps=?",
                                   (json.dumps(event, ensure_ascii=False, allow_nan=False), *(event[k] for k in controls.KEYS)))
        progress(json.dumps({"phase": "control_liquidity_repair", "instruments_done": len(checked_responses), "events": len(details), "corporate_paths_audited": len(corporate_records)}))
    connection.commit()
    return {"spec": SPEC, "reproduced_failed_cost_paths": liquidity_count, "changed_cost_paths": len(details),
            "corporate_audit": {**corporate.summarize(corporate_records, corporate_actions),
                                "records": corporate_records, "actions": list(corporate_actions.values())},
            "changed_paths": details, "official_responses": [ref for _, ref in checked_responses],
            **({"distribution_paths": distribution_events} if distribution_notices is not None else {}),
            "transitions": [{"track": t, "slippage_bps": b, "status": s, "reason": r, "paths": n} for (t, b, s, r), n in sorted(transitions.items())],
            "unaffected_event_bytes_preserved": True, "original_factor_groups_unchanged": True,
            "interpretation": "仅修正已确认停牌零成交导致的退出容量缺口；正常交易日缺值不填补，策略、信号及期末平仓规则不变。"}


def run(output, **kwargs):
    return controls.run(output, **kwargs, liquidity_repair=True)
