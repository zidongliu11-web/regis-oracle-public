"""All captured mainline constituents, checked against saved research recipes.

One process-owned producer shares source verification across stocks/recipes.
HTTP only reads its current projection. No quotes, accounts or outcomes fetched.
"""
from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import time

from flask import Blueprint, current_app, jsonify

from . import playbook_stock_checks as checks
from . import playbook_development as dev
from .directory_revision import revision as directory_revision
from .research_release_guard import source_lease
from .research_route_snapshot import ResearchRouteSnapshot

SCHEMA = "playbook_candidate_scan.v1"
BUILD_SECONDS = 45
blueprint = Blueprint("playbook_candidate_scan", __name__)
MESSAGES = {
    "current": "已读取本轮全成分研究检查；不代表收益合格或可买入。",
    "pending": "后台尚未完成本轮成分扫描，不能据此判断没有机会。",
    "inputs_changed": "主线、价格或战法版本发生变化，旧扫描已清除，等待重新核验。",
    "stale": "扫描快照已过期，等待后台刷新。",
    "evidence_expired": "主线证据或入场窗口已过期，旧候选已清除。",
    "calculation_expired": "本轮扫描超过快照时效，等待重新核验。",
    "failed": "后台扫描失败，稍后重试；不能据此判断没有机会。",
    "inputs_unavailable": "输入版本不可核验，旧扫描已清除。",
    "stopped": "后台扫描已停止，旧扫描已清除。",
    "source_update": "源码正在更新或版本保护不可用，旧扫描已清除。",
}


def input_revision(root, *, now=None):
    """File metadata only; byte/semantic checks belong to the producer.

    Watch physical prices and receipts as well as manifests: an unchanged
    manifest must not hide an in-place raw-source edit. No account paths.
    """
    root = Path(root); now = now or datetime.now(checks.price.SHANGHAI)
    paths = {root / "reports" / name for name in (
        "current_market_calendar.json", "cloud_market_data.json", "v4_daily_download_manifest.json")}
    paths.add(root / "research/data/stock_basic_baostock.csv")
    daily = root / "research/data/v4_daily"
    daily_revision = directory_revision(daily)
    if daily_revision is None:
        paths.update(daily.glob("*"))
    paths.update(dev.directory(root).glob("*.json"))
    target = checks.read_json(root / "reports/cloud_market_data.json").get("target_date")
    if target is not None and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(target)):
        raise ValueError("invalid_target_date")
    for name in ("cloud_sector_observations", "cloud_sector_constituents"):
        dates = {now.date().isoformat(), target} - {None}
        for day in dates:
            directory = root / "reports" / name / day
            paths.add(directory)
            paths.update(directory.glob("*.json"))
    if target:
        for name in ("cloud_market_emotion", "cloud_pattern_features", "cloud_pattern_inputs",
                     "cloud_industry_daily", "cloud_daily_revisions", "cloud_daily_gap_probe", "cloud_daily_tail_probe"):
            directory = root / "reports" / name / target
            paths.add(directory)
            paths.update(directory.glob("*.json"))
            paths.update(directory.glob("*/*.json"))
    versions = []
    for path in sorted(paths):
        try: stamp = checks._file_stamp(path)
        except FileNotFoundError: stamp = None
        versions.append((str(path.relative_to(root)), stamp))
    if daily_revision is not None:
        # A writer can change a daily file while the other inputs are being
        # stamped. Never publish a mixed pre/post-change identity.
        if directory_revision(daily) != daily_revision:
            raise OSError("daily_source_changed_during_revision")
        versions.append(("research/data/v4_daily/*", daily_revision))
    for path in (Path(__file__), Path(checks.__file__), Path(dev.__file__), Path(checks.price.__file__),
                 Path(checks.mainline.__file__), Path(checks.timing.__file__), Path(__file__).with_name("emotion_system.py")):
        versions.append(("source/" + path.name, checks._file_stamp(path)))
    # Also invalidate exactly at an opening deadline or a new observation
    # bucket; a still-young cache must not carry a pre-open candidate past it.
    clock = (now.date().isoformat(), checks.mainline.boards.bucket(now), now.hour >= 15,
             (now.hour, now.minute) >= (9, 30))
    return dev.digest([clock, versions])


def _compact(row, members):
    price, conditions = row.get("price") or {}, row.get("conditions") or {}
    return {"stock_code": row["stock_code"], "stock_name": members[0]["stock_name"],
            "sectors": [{"name": r["sector"]["name"], "kind": r["sector"]["kind"], "code": r["sector"]["code"],
                         "stage": r["stage"]} for r in members],
            "checked": True, "status": row.get("joint_research_status", row["status"]),
            "label": row.get("joint_research_label", row["label"]),
            "signal_day": price.get("data_as_of"), "price_input_identity": price.get("input_identity"),
            "checked_price_rows": price.get("checked_rows"),
            "shape": price.get("shape"), "factor_values": price.get("factor_values", {}),
            "failed_conditions": conditions.get("failed", []), "missing_conditions": conditions.get("missing", []),
            "timing": row.get("timing"), "entry_terms": row.get("entry_terms"),
            "research_plan": row.get("research_plan"), "issues": row["issues"]}


def build(root, *, now=None, stop_event=None):
    root = Path(root); now = now or datetime.now(checks.price.SHANGHAI)
    deadline = time.monotonic() + BUILD_SECONDS
    library = dev.catalog(); evidence = checks.Evidence(root, now, library, stop_event=stop_event)
    versions, valid_until, distinct = [], [], set()
    # Read the complete archive, not the website's 100-record display window.
    for path in sorted(dev.directory(root).glob("[0-9a-f]"*64+".json")):
        version = {"spec_id": path.stem, "name": "原件待核验", "status": "invalid_record", "rows": [],
                   "total_members": None, "checked_members": 0, "scope_complete": False,
                   "unresolved_sectors": [], "environment": None, "issues": []}
        versions.append(version)
        try:
            record = dev.read_record(path)
            version.update(name=record["spec"]["name"], timing_protocol=record["spec"].get("timing_protocol"),
                           rule_identity=record["rule_identity"], kind=record["mechanism"]["kind"])
            try: spec = checks.validated_spec(record, library)
            except ValueError as exc:
                version.update(status="version_unavailable", issues=[checks.ERRORS.get(str(exc), str(exc))])
                continue
            if datetime.fromisoformat(record["created_at"]) > now:
                version.update(status="version_after_check", issues=["该版本在扫描时点之后才保存，不能回填。"])
                continue
            version["spec"] = spec
            version["environment"] = dev.route(spec, evidence.context, library)
            if record["mechanism"]["kind"] != "entry_research":
                version.update(status="risk_review_separate", issues=["退出与防守继续独立复核，不以新仓主线扫描代替持仓风险管理。"])
                continue
            universe = evidence.universe(spec)
            version.update(scope_complete=universe["scope_complete"], unresolved_sectors=universe["unresolved_sectors"],
                           scope_label=universe["label"], member_packet_id=universe.get("member_packet_id"),
                           mainline_at=universe.get("observed_at"))
            if universe["status"] != "observed":
                version.update(status="scope_incomplete", issues=[universe["label"]])
                continue
            if universe["valid_until"]: valid_until.append(universe["valid_until"])
            version["total_members"] = len(universe["members"])
            for symbol, members in sorted(universe["members"].items()):
                distinct.add(symbol)
                if time.monotonic() >= deadline or (stop_event and stop_event.is_set()):
                    # ponytail: one bounded pass; explicit unprocessed rows if
                    # the source scope exceeds it, never a falsely complete scan.
                    version["rows"].append({"stock_code": symbol, "stock_name": members[0]["stock_name"],
                                            "checked": False, "status": "not_scanned", "label": "本轮预算已到或后台停止，尚未检查。"})
                    continue
                row = checks.evaluate_record(record, symbol, evidence, verify_inputs=False)
                version["rows"].append(_compact(row, members))
                version["checked_members"] += 1
                if row.get("joint_research_status") == "research_conditions_met_execution_pending":
                    valid_until.append(row["timing"]["expires_at"])
            version["status_counts"] = dict(Counter(row["status"] for row in version["rows"]))
            version["status"] = ("partial" if version["checked_members"] != version["total_members"] else
                                 "scope_incomplete" if not version["scope_complete"] else
                                 "no_selected_stage" if not version["total_members"] else "checked")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            version.update(status="input_incomplete" if version.get("kind") else "invalid_record", issues=[checks._message(exc)])
    entries = [v for v in versions if v.get("kind") == "entry_research"
               and v["status"] not in {"version_unavailable", "version_after_check", "invalid_record"}]
    return {"schema": SCHEMA, "checked_at": now.isoformat(), "catalog_sha256": library["catalog_sha256"],
            "implementation_sha256": checks.sha(Path(__file__)), "stock_check_sha256": checks.IMPLEMENTATION_SHA256,
            "status": "no_current_entry_versions" if not entries else
                      "checked" if all(v["status"] in {"checked", "no_selected_stage"} for v in entries) else "incomplete",
            "scope": "当前保存入场版本所选主线阶段的全部已核验成分；受现有主线采集范围限制，不是全市场题材扫描。",
            "versions": versions, "current_entry_versions": len(entries), "archive_records": len(versions),
            "distinct_members": len(distinct), "checked_pairs": sum(v["checked_members"] for v in versions),
            "known_member_pairs": sum(v["total_members"] or 0 for v in versions),
            "emotion": {k: (evidence.context.get("emotion") or {}).get(k) for k in ("phase", "label", "data_as_of")},
            "development": {**{k: library[k] for k in ("schema", "catalog_sha256", "mechanisms", "phases", "factors",
                                "mainline_stages", "timing_protocols")},
                            "current_routes": [dev.route(dev.template(m, library), evidence.context, library)
                                               for m in library["mechanisms"]], **dev.BOUNDARY},
            "valid_until": min(valid_until, key=datetime.fromisoformat) if valid_until else None,
            "account_evaluated": False, "actual_fill": False, **dev.BOUNDARY}


def attach(app):
    def root(): return Path(app.config.get("RESEARCH_WORKBENCH_ROOT", dev.ROOT))
    def produce():
        with source_lease(required=app.config["REMOTE_MODE"]) as lease:
            if lease is None: raise RuntimeError("publication_wait")
            result = build(root(), stop_event=snapshot.stop_event)
            result["source_version_locked"] = bool(lease)
            return result
    snapshot = ResearchRouteSnapshot(produce, lambda: input_revision(root()))
    app.extensions["playbook_candidate_snapshot"] = snapshot
    app.register_blueprint(blueprint)
    return snapshot


def read(snapshot, *, now=None):
    now = now or datetime.now(checks.price.SHANGHAI)
    result = snapshot.read(); metadata = result.pop("research_snapshot")
    if metadata["current"] and result.get("valid_until") and now >= datetime.fromisoformat(result["valid_until"]):
        metadata.update(current=False, state="evidence_expired")
    metadata["summary"] = MESSAGES[metadata["state"]]
    metadata["valid_for_seconds"] = 0
    if metadata["current"]:
        remaining = metadata["maximum_age_seconds"] - metadata["age_seconds"]
        if result.get("valid_until"):
            remaining = min(remaining, (datetime.fromisoformat(result["valid_until"])-now).total_seconds())
        metadata["valid_for_seconds"] = max(0, remaining)
    if not metadata["current"]:
        result = {"schema": SCHEMA, "status": "scan_pending", "versions": [], "account_evaluated": False, **dev.BOUNDARY}
    result["scan_snapshot"] = metadata
    return result


def current_snapshot():
    """Same lease-protected, non-computing read for the scan and planning APIs."""
    try:
        with source_lease(required=bool(current_app.config.get("REMOTE_MODE"))) as lease:
            if lease is None: raise RuntimeError("publication_wait")
            result = read(current_app.extensions["playbook_candidate_snapshot"])
    except (OSError, RuntimeError):
        result = {"schema": SCHEMA, "status": "scan_pending", "versions": [],
                  "scan_snapshot": {"state": "source_update", "current": False, "summary": MESSAGES["source_update"]},
                  "account_evaluated": False, **dev.BOUNDARY}
    return result


@blueprint.get("/api/strategy-lab/candidate-scan")
def candidate_scan():
    result = current_snapshot()
    response = jsonify(result)
    response.status_code = 200 if result["scan_snapshot"]["current"] else 202
    response.headers["Cache-Control"] = "no-store"
    return response
