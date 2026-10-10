"""Bounded reconciliation and descriptive ablations of frozen cash controls.

No signals, fills, factor groups, strategy rules or admission states are changed.
The temporary SQLite join avoids loading both historical ledgers into memory.
"""
from collections import defaultdict
from contextlib import closing
import hashlib
import json
import sqlite3
from statistics import fmean

import pandas as pd

from . import conditioned_research as conditioned
from .emotion_factor_attribution import freeze_factor_groups
from .emotion_pattern_research import PLAYBOOKS
from .workbench_artifact_paths import research_artifact_path
from research.run_emotion_control_cash_settlement import cash_outcomes, SPEC as CASH_SPEC
from research.run_emotion_pattern_controls import SPEC as CONTROL_SPEC


ARTIFACTS = (
    ("output/research/emotion_pattern_controls_v1.json", "dc74285d44b18255b4e47e258c13ae7968994a6c55274e2eea6eeb90580d44c0"),
    ("output/research/emotion_control_cash_settlement_v1.json", "f56f4316d16c9a44b11d5b1008f61563094461b422fcae523445063f137e48d7"),
    ("output/research/emotion_pattern_controls_v1.events.jsonl", "51222e2bd0b8f65141af0190318826dec757f08650302c4ee983b4e826bf2e93"),
    ("output/research/emotion_control_cash_settlement_v1.events.jsonl", "0c1c13e4b3a32f38f653718168d672ed4ff9819580f342eafbb69e5dc53de75e"),
    ("output/research/emotion_factor_attribution_v1.groups.json", "ca9a06ecde6f4bbcffc70e8c9440c4d328ca280e2bec00974e6bdb728dc164b5"),
)
TRACKS = {"original_routed": "原规则", "same_entries_no_emotion_exit": "相同入场 · 取消情绪退出",
          "no_emotion": "同形态 · 取消入场与退出情绪限制", "same_dates_no_shape": "同日同板块 · 取消形态筛选"}
EFFECTS = {"emotion_exit": "情绪退出的增量", "emotion_entry": "入场情绪过滤的增量", "shape_entry": "战法形态过滤的增量"}
SPEC = {
    "schema": "control_increment_research.v1", "source_controls": CONTROL_SPEC, "cash_contract": CASH_SPEC,
    "analysis": conditioned.SPEC,
    "emotion_exit": "same_signal_same_buy_orders; original_minus_no_emotion_exit",
    "emotion_entry": "all_no_emotion_shape_opportunities; excluded_keep_idle_zero_budget; exits_disabled_on_both_sides",
    "shape_entry": "all_same_dates_no_shape_opportunities; excluded_keep_idle_zero_budget; original_exits_on_both_sides",
    "overlap": "selected_opportunities_must_match_the_counterfactual_path; missing_not_zero",
    "factor_groups": "exact_original_signal_first_groups; never_reselected_from_cash_labels",
    "source_boundary": "reconcile_byte_bound_saved_cash_labels; no_new_independent_announcement_or_price_audit",
    "unknown_outcome": "any_unknown_suppresses_full_effect; never_delete_or_impute",
    "new_blind_trial": False, "causal_claim": False, "statistically_validated": False,
    "production_eligible": False, "generates_alert": False, "portfolio_return": None,
}
KEYS = ("track", "playbook_id", "signal_day", "code", "slippage_bps")


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def join_ledgers(connection, old_path, new_path, progress):
    connection.execute("PRAGMA cache_size=-8192")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("CREATE TABLE events(track TEXT, playbook TEXT, day TEXT, code TEXT, bps INTEGER, old TEXT NOT NULL, new TEXT, PRIMARY KEY(track,playbook,day,code,bps)) WITHOUT ROWID")
    counts = []
    with connection:
        for source, path in enumerate((old_path, new_path)):
            count = 0
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    event = json.loads(line)
                    key = tuple(event[k] for k in KEYS)
                    if event["track"] not in TRACKS or type(event["slippage_bps"]) is not int or event["slippage_bps"] not in (10, 20, 30):
                        raise ValueError("control_increment_track_or_cost_invalid")
                    try:
                        if source == 0:
                            connection.execute("INSERT INTO events VALUES(?,?,?,?,?,?,NULL)", (*key, line))
                        elif connection.execute("UPDATE events SET new=? WHERE track=? AND playbook=? AND day=? AND code=? AND bps=? AND new IS NULL", (line, *key)).rowcount != 1:
                            raise ValueError("control_increment_duplicate_or_extra_cash_event")
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("control_increment_duplicate_source_event") from exc
                    count += 1
                    if count % 8192 == 0:
                        progress(encoded({"phase": "control_ledger_join", "events": count}))
            counts.append(count)
    if counts[0] != counts[1] or connection.execute("SELECT 1 FROM events WHERE new IS NULL LIMIT 1").fetchone():
        raise ValueError("control_increment_cash_population_missing")
    return counts[0]


def prepare(connection, contexts, groups, *, distribution_paths=()):
    frames, outcomes, identities = defaultdict(list), defaultdict(dict), defaultdict(dict)
    grouped = connection.execute("SELECT DISTINCT track,playbook,day FROM events ORDER BY track,playbook,day").fetchall()
    expected = defaultdict(list)
    overrides = {tuple(e[k] for k in KEYS): e for e in distribution_paths}
    if len(overrides) != len(distribution_paths):
        raise ValueError('control_distribution_duplicate_override')
    used = set()
    for group in groups:
        expected[group["track"], group["playbook_id"], group["signal_day"]].append(group)
    if set(expected) != set(grouped):
        raise ValueError("control_increment_group_population_changed")
    for track, playbook, day in grouped:
        pairs = connection.execute("SELECT old,new FROM events WHERE track=? AND playbook=? AND day=? ORDER BY code,bps", (track, playbook, day))
        old, new = [], []
        for a, b in pairs:
            old.append(json.loads(a)); new.append(json.loads(b))
        inputs, labels = cash_outcomes(old, new)
        if freeze_factor_groups(inputs) != expected[track, playbook, day]:
            raise ValueError("control_increment_factor_groups_changed")
        for event in new:
            override_key = tuple(event[k] for k in KEYS)
            if override_key in overrides:
                # The frozen checker above still validates the full original
                # population. Only source-audited physical outcomes replace labels.
                from .control_distribution_research import checked_override
                revised = overrides[override_key]
                labels[(override_key[:-1], override_key[-1])] = checked_override(event, revised)
                event = revised
                used.add(override_key)
            context = contexts.get(day)
            if (context is None or not "2021-01-04" <= day <= "2025-12-31"
                    or event["signal_emotion"] != context["signal_emotion"]):
                raise ValueError("control_increment_observed_emotion_mismatch")
            entry, end = event.get("entry_day"), event.get("exit_day")
            if entry is not None and (entry not in contexts or entry <= day):
                raise ValueError("control_increment_entry_date_invalid")
            if end is not None and (entry is None or end not in contexts or end <= entry):
                raise ValueError("control_increment_exit_not_T_plus_1")
            buy = {"entry_day": entry, "shares": event["simulated_entry_shares"],
                   "orders": [o for o in event["execution_events"] if o["side"] == "buy"]}
            path = {k: event.get(k) for k in ("status", "reason", "entry_day", "exit_day", "remaining_shares",
                                            "execution_events", "net_pnl_yuan", "cash_entitlements")}
            key = (playbook, day, event["code"], event["slippage_bps"])
            identities[track][key] = tuple(hashlib.sha256(encoded(v).encode()).hexdigest() for v in (buy, path))
        frames[track].append(inputs)
        outcomes[track].update(labels)
    if set(frames) != set(TRACKS):
        raise ValueError("control_increment_track_missing")
    if used != overrides.keys():
        raise ValueError('control_distribution_override_outside_population')
    return {track: pd.concat(parts, ignore_index=True) for track, parts in frames.items()}, outcomes, identities


def increments(frames, outcomes, identities, contexts):
    original, no_exit = identities["original_routed"], identities["same_entries_no_emotion_exit"]
    if original.keys() != no_exit.keys() or any(original[k][0] != no_exit[k][0] for k in original):
        raise ValueError("control_increment_same_entry_pair_changed")
    for selected, universe in (("same_entries_no_emotion_exit", "no_emotion"), ("original_routed", "same_dates_no_shape")):
        if not identities[selected].keys() <= identities[universe].keys() or any(identities[selected][k] != identities[universe][k] for k in identities[selected]):
            raise ValueError("control_increment_selected_path_not_equal_to_control")
    result = []
    for effect, policy, baseline in (("emotion_exit", "original_routed", "same_entries_no_emotion_exit"),
                                      ("emotion_entry", "same_entries_no_emotion_exit", "no_emotion"),
                                      ("shape_entry", "original_routed", "same_dates_no_shape")):
        rows = []
        for signal in frames[baseline].to_dict("records"):
            key = (signal["playbook_id"], signal["signal_day"], signal["code"])
            for bps in (10, 20, 30):
                right = outcomes[baseline][((baseline, *key), bps)]["fixed_budget_return"]
                selected = (*key, bps) in identities[policy]
                left = outcomes[policy][((policy, *key), bps)]["fixed_budget_return"] if selected else 0.
                rows.append({**signal, "slippage_bps": bps, "selected": selected, "baseline_return": right,
                             "policy_return": left, "delta": left-right if left is not None and right is not None else None})
        for key, group in sorted(conditioned.grouping(rows, contexts).items()):
            complete = all(r["delta"] is not None for r in group)
            row = {**conditioned.dimensions(key, group, contexts), "effect_id": effect, "effect_name": EFFECTS[effect],
                   "opportunities": len(group), "selected": sum(r["selected"] for r in group),
                   "unresolved_pairs": sum(r["delta"] is None for r in group), "causal_claim": False}
            for field in ("baseline_return", "policy_return", "delta"):
                row[field] = fmean(r[field] for r in group) if complete else None
            result.append(row)
    return result


def run(output, *, input_root, references, source_code, progress, liquidity_repair=False, distribution_repair=False):
    from .research_workbench import digest
    from research.run_emotion_pattern_replay import write_new

    if distribution_repair and not liquidity_repair:
        raise ValueError('control_distribution_requires_input_repair')
    expected_inputs = 11 if liquidity_repair else 7
    if distribution_repair:
        from .control_distribution_research import ARTIFACTS as DISTRIBUTION_INPUTS
        expected_inputs += len(DISTRIBUTION_INPUTS)
    if len(references) != expected_inputs:
        raise ValueError("control_increment_input_count_mismatch")
    if distribution_repair and references[11:] != [
            {'path': path, 'sha256': sha} for path, sha in DISTRIBUTION_INPUTS]:
        raise ValueError('control_distribution_input_identity_mismatch')
    paths = [research_artifact_path(ref["path"], project_root=input_root) for ref in references]
    extra_references = []
    def verify_inputs():
        if any(digest(p) != r["sha256"] for p, r in zip(paths, references)):
            raise ValueError("control_increment_input_hash_changed")
        if any(digest(research_artifact_path(r["path"], project_root=input_root)) != r["sha256"] for r in extra_references):
            raise ValueError("control_increment_response_hash_changed")
    verify_inputs()
    prior, cash, selection, state, calendar = [json.loads(paths[i].read_text(encoding="utf-8")) for i in (0, 1, 4, 5, 6)]
    if (prior.get("spec") != CONTROL_SPEC or cash.get("spec") != CASH_SPEC
            or prior.get("event_ledger") != references[2] or cash.get("event_ledger") != references[3]
            or cash.get("selection") != references[4] or cash.get("factor_groups_unchanged") is not True
            or cash.get("source_results_unchanged") is not True
            or any(r not in cash.get("bindings", []) for r in (references[0], references[2], references[4], *references[5:7]))
            or any(r not in prior.get("bindings", []) for r in references[5:7])
            or any(d.get("production_eligible") is not False or d.get("generates_alert") is not False for d in (prior, cash))):
        raise ValueError("control_increment_source_contract_mismatch")
    contexts = conditioned.market_context(state["rows"], calendar["trading_dates"])
    # The archived cash report also contains large, obsolete metric tables.
    # After checking its contract, retain only provenance and population counts.
    cash = {k: cash[k] for k in ("bindings", "cost_path_count", "signal_count")}
    scratch = output.with_name("ledger-join.sqlite3")
    if scratch.exists() or output.exists():
        raise ValueError("control_increment_attempt_exists")
    with closing(sqlite3.connect(scratch)) as connection:
        count = join_ledgers(connection, paths[2], paths[3], progress)
        if count != prior.get("path_count") or count != cash.get("cost_path_count"):
            raise ValueError("control_increment_source_count_mismatch")
        repair, distribution_paths = None, []
        if liquidity_repair:
            from .control_liquidity_research import repair_inputs
            notices = None
            if distribution_repair:
                from .control_distribution_research import notice_inputs
                notices = notice_inputs(paths, references)
            repair = repair_inputs(connection, paths[:11], references[:11], input_root, cash, progress,
                                   distribution_notices=notices)
            extra_references = repair["official_responses"]
            distribution_paths = repair.pop('distribution_paths', [])
        frames, outcomes, identities = prepare(connection, contexts, selection["groups"], distribution_paths=distribution_paths)
    if sum(len(f) for f in frames.values()) != cash.get("signal_count"):
        raise ValueError("control_increment_signal_count_mismatch")
    progress(encoded({"phase": "control_increment_evaluation", "events": count}))
    effects = increments(frames, outcomes, identities, contexts)
    tracks = {}
    for track in TRACKS:
        groups = [g for g in selection["groups"] if g["track"] == track]
        factors = conditioned.factor_summary(groups, outcomes[track], contexts)
        tracks[track] = {**conditioned.conditional_summary(frames[track], outcomes[track], contexts, PLAYBOOKS),
                         "factor_summaries": factors["factor_summaries"], "candidate_count": len(frames[track])}
    result = {"schema": SPEC["schema"], "spec": SPEC, "lane": "control_increment",
              "candidate_count": sum(len(f) for f in frames.values()), "costed_event_count": count,
              "tracks": TRACKS, "track_results": tracks, "increment_summaries": effects,
              "state_labels": conditioned.STATES, "direction_labels": conditioned.DIRECTIONS,
              "market_scope": state.get("scope", "not_declared"), "inputs": references, "source_code": source_code,
              "original_factor_groups_unchanged": True, "production_eligible": False, "generates_alert": False,
              "portfolio_return": None,
              "interpretation": "完整四路线现金标签与原因子分组已核验；用相同机会预算比较情绪退出、情绪入场过滤和形态过滤，结果仅作已见历史诊断。",
              "admission_blockers": ["正的增量也可能只是少亏，不代表规则自身赚钱", "按既定信号日和板块筛选的历史对照，不代表全市场或账户组合收益",
                                     "已见历史的描述性比较；尚无因果、独立样本外或真实前向资格", "五因子仍是辅助排序，不自动选方向、调参或启用战法"]}
    verify_inputs()
    if repair is not None:
        result.update(input_repair=repair, spec={**SPEC, "execution_input_repair": repair["spec"]},
                      interpretation="保留全部原信号及因子分组，复现退出输入缺失并核验停牌零成交窗口后比较四路线；旧结果不覆盖，未解决权益和期末持仓不填零。")
    if distribution_repair:
        from .control_distribution_research import SPEC as DISTRIBUTION_SPEC
        result['distribution_repair'] = {'spec':DISTRIBUTION_SPEC,'cost_paths':len(distribution_paths),
            'closed_cost_paths':sum(e['status']=='closed' for e in distribution_paths),
            'unresolved_cost_paths':sum(e['status']=='unresolved' for e in distribution_paths),
            'reference_cost_records':sum(e['slippage_bps']==20 for e in distribution_paths),
            'paths':distribution_paths,'implementation_sources':references[11:],
            'interpretation':'只修正已绑定实施公告所覆盖的既有未知路径；按原始股数、上市日期和税费独立复算后进入同一四路线与因子分组。其余缺口保留，不增加策略或因子资格。'}
        result['spec'] = {**result['spec'],'distribution_repair':DISTRIBUTION_SPEC}
        result['interpretation'] = result['distribution_repair']['interpretation']
    write_new(output, result)
    scratch.unlink()  # Only this attempt's temporary join; source ledgers stay intact.
    summary = {k: v for k, v in result.items() if k not in {"track_results", "increment_summaries", "inputs", "source_code"}}
    if repair is not None:
        summary["input_repair"] = {k: v for k, v in repair.items() if k not in {"changed_paths", "official_responses"}}
        summary["input_repair"]["corporate_audit"] = {k: v for k, v in repair["corporate_audit"].items() if k not in {"records", "actions"}}
    if distribution_repair:
        summary['distribution_repair'] = {k:v for k,v in result['distribution_repair'].items() if k!='paths'}
    return summary
