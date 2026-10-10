"""Reconcile cached official cash actions against every original event window.

No return is repaired here. This input audit preserves mixed/unknown actions and
checks also the previously closed paths, not only convenient unresolved names.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from mom_trading.cash_dividend_evidence import SPEC, reconcile_cash_reference
from research.run_emotion_pattern_replay import INPUTS, sha, write_new
from research.run_emotion_pattern_controls import market_chunks

PRIMARY = ROOT / "output/research/recent_limit_crowding_cash_share_v1a.json"
PRIMARY_SHA = "9659d9cf66a477a057ff7f33392912b90191ccd42d92b98110cf1995b8e7c72d"
MANIFEST = ROOT / "research/data/v184_cninfo_cash_dividend/query_manifest.json"
MANIFEST_SHA = "f0e754c567a20770c781e73759f49239d293769f1f16e35c7ba313954e0eb358"


def run(output: Path, progress=print) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("entitlement_audit_output_outside_project")
    started = output.with_suffix(".started.json")
    if output.exists() or started.exists():
        raise FileExistsError("entitlement audit exists; do not overwrite")
    if sha(PRIMARY) != PRIMARY_SHA or sha(MANIFEST) != MANIFEST_SHA:
        raise ValueError("entitlement_audit_input_identity_mismatch")
    prior = json.loads(PRIMARY.read_text())
    refs = prior["bindings"] + [prior["selection"], prior["event_ledger"],
                                {"path": str(PRIMARY.relative_to(ROOT)), "sha256": PRIMARY_SHA},
                                {"path": str(MANIFEST.relative_to(ROOT)), "sha256": MANIFEST_SHA}]
    refs += [{"path": path, "sha256": sha(ROOT / path)} for path in (
        "mom_trading/cash_dividend_evidence.py", "research/audit_crowding_entitlement_coverage.py")]
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"entitlement_binding_mismatch:{ref['path']}")
    write_new(started, {"status": "cash_entitlement_input_audit_started", "spec": SPEC, "bindings": refs,
                        "repaired_returns_computed": False})
    try:
        events = defaultdict(list)
        with (ROOT / prior["event_ledger"]["path"]).open() as stream:
            for line in stream:
                row = json.loads(line)
                if row["slippage_bps"] == 10:
                    events[row["code"]].append({k: row.get(k) for k in (
                        "signal_day", "code", "signal_emotion", "quintile", "status", "reason", "entry_day", "exit_day")})
        manifest = json.loads(MANIFEST.read_text())["queries"]
        if len({r["code"] for r in manifest}) != len(manifest):
            raise ValueError("entitlement_response_duplicate_code")
        by_code = {r["code"]: r for r in manifest}
        all_days = json.loads(INPUTS[2][0].read_text())["trading_dates"]
        sessions = [day for day in all_days if "2021-01-04" <= day <= "2025-12-31"]
        index = {day: i for i, day in enumerate(sessions)}
        calendar_index = {pd.Timestamp(day): i for i, day in enumerate(all_days)}
        special = pd.read_csv(INPUTS[3][0], dtype=str)
        def normalize(code):
            return code.split(".")[-1] if code.startswith(("sh.", "sz.")) else code.split(".")[0]
        special_keys = {(normalize(r.code), pd.Timestamp(r.date)) for r in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
        action_evidence, affected = {}, []
        counters, response_refs = Counter(), []
        for n, (code, stock) in enumerate(market_chunks(sorted(events), sessions, calendar_index, special_keys)):
            record = by_code.get(code)
            actions = defaultdict(list)
            response_problem = None
            if record is None:
                response_problem = "official_security_response_missing"
            else:
                path = ROOT / record["path"]
                if not path.resolve().is_relative_to((ROOT / "research/data/v184_cninfo_cash_dividend/responses").resolve()) or sha(path) != record["sha256"]:
                    raise ValueError(f"official_response_identity_mismatch:{code}")
                response_refs.append({"path": record["path"], "sha256": record["sha256"]})
                payload = json.loads(path.read_text())
                if payload.get("code") != code or payload.get("status") not in {"ok", "empty"}:
                    raise ValueError(f"official_response_code_or_status_invalid:{code}")
                for action in payload.get("rows", []):
                    if action.get("ex_date") in index:
                        actions[action["ex_date"]].append(action)
            stock = stock.copy()
            stock["trade_date"] = stock.trade_date.dt.strftime("%Y-%m-%d")
            rows = {r["trade_date"]: r for r in stock.to_dict("records")}
            # Scan all changed factors and all reported actions; a source's
            # unchanged rounded factor must not hide a cash entitlement.
            changed = set(stock.loc[stock.adj_factor.diff().abs().gt(1e-12), "trade_date"])
            dates = sorted(changed | set(actions))
            for event in events[code]:
                start = index[event["signal_day"]]
                stop = index[event["exit_day"]] if event["status"] == "closed" else min(start + (1 if event["status"] == "unfilled" else 11), len(sessions)-1)
                relevant = dates[bisect_right(dates, event["signal_day"]):bisect_right(dates, sessions[stop])]
                if not relevant:
                    if event["reason"] == "corporate_action_cash_and_share_ledger_missing":
                        raise ValueError("prior_corporate_action_gap_not_reproduced")
                    continue
                proofs = []
                for day in relevant:
                    key = f"{code}:{day}"
                    if key not in action_evidence:
                        previous_day = sessions[index[day]-1]
                        before, after = rows.get(previous_day), rows.get(day)
                        if response_problem or before is None or after is None:
                            proof = {"verified": False, "reason": response_problem or "action_market_session_missing"}
                        else:
                            proof = reconcile_cash_reference(before, after, actions[day], previous_session=previous_day)
                        action_evidence[key] = {**proof, "code": code, "ex_date": day,
                                                "factor_changed": day in changed,
                                                "official_actions": actions[day],
                                                "response_sha256": record["sha256"] if record else None}
                    proofs.append(action_evidence[key])
                supported = all(r["verified"] for r in proofs)
                affected.append({**event, "action_keys": [f"{code}:{d}" for d in relevant], "all_cash_actions_supported": supported})
                counters[event["status"], event["reason"], supported] += 1
            if n % 500 == 0:
                progress(json.dumps({"phase": "entitlement_coverage", "instruments_done": n+1, "affected_paths": len(affected)}), flush=True)
        for ref in refs + response_refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("entitlement_inputs_changed_during_audit")
        result = {"schema": "crowding_entitlement_coverage.v1", "spec": SPEC, "bindings": refs,
                  "official_response_bindings": response_refs, "audited_signal_paths": sum(map(len, events.values())),
                  "affected_paths": affected, "action_evidence": action_evidence,
                  "path_summary": [{"original_status": status, "original_reason": reason, "all_cash_actions_supported": supported, "paths": count}
                                   for (status, reason, supported), count in sorted(counters.items())],
                  "action_reason_counts": dict(Counter(r["reason"] for r in action_evidence.values())),
                  "repaired_returns_computed": False, "production_eligible": False,
                  "limitations": ["Cash-only identities; mixed share distributions remain unresolved.",
                                  "Reference price cross-check is not a separate exchange quote.",
                                  "Coverage does not prove final execution, cash payment, or tax settlement."]}
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "official_response_bindings", "affected_paths", "action_evidence"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "entitlement_audit_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
