"""Read-only, source-bound stock checks for saved research specifications.

Daily shapes and current mainline observations keep their own availability
times. A later intraday observation cannot justify an earlier opening fill.
"""
import csv
from datetime import datetime, timedelta
import math
from pathlib import Path
import re
import time

from flask import Blueprint, current_app, jsonify

from . import cloud_pattern_features as price
from . import mainline_system as mainline
from . import playbook_development as dev
from . import playbook_entry_timing as timing
from .cloud_pattern_inputs import _source_universe, sha
from .cloud_research_worker import read_json
from .emotion_system import public_context
from .research_release_guard import source_lease

blueprint = Blueprint("playbook_stock_checks", __name__)
SCHEMA = "playbook_stock_check.v1"
IMPLEMENTATION_SHA256 = sha(Path(__file__))
ERRORS = {
    "saved_mechanism_mismatch": "保存的机制或规则身份与当前目录不一致，须重新核验原研究版本。",
    "stock_outside_current_scope": "股票不在当前已核验的沪深主板／创业板范围。",
    "price_observation_missing": "这只股票的日线形态观察尚未生成。",
    "price_history_ineligible": "历史窗口含停牌或上市时间不足，不能补值计算形态。",
    "price_inputs_need_review": "日线或复权链尚未通过核验，不能计算本次条件。",
    "source_file_identity_mismatch": "原始输入或观察文件发生变化，须重新核验。",
    "price_observation_identity_invalid": "价格观察的程序、日期或证券身份不一致。",
    "price_observation_after_check": "价格观察在本次检查时点之后才可得。",
    "price_recomputation_mismatch": "重算结果与已保存观察不一致，暂不采用。",
    "price_manifest_changed": "核验期间输入版本发生变化，请重新检查。",
    "industry_cross_section_incomplete": "同日行业横截面尚未完整，行业相对强度保留缺失。",
    "industry_check_budget_exceeded": "完整行业核验超时，当前保留待核；再次检查会复用未变动原件的核验结果。",
    "mainline_packet_changed": "主线与成分原件未能绑定同一次观察。",
    "mainline_members_missing": "该主线当时的完整成分尚未通过核验。",
}


def _message(exc):
    return ERRORS.get(str(exc), "输入、日历或来源身份未通过核验，请查看数据任务状态。")


def _file_stamp(path):
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode


def _verified_file(root, path, directory, expected_sha, stamp):
    # File-system timestamps are not an identity proof. Recheck the bytes on
    # every use, then reject a replacement that occurred during that read.
    price._bound(root, {"path": str(path), "sha256": expected_sha}, directory)
    if _file_stamp(path) != stamp:
        raise ValueError("source_file_identity_mismatch")


def _bound(root, entry, directory):
    path = root / entry["path"]
    expected = directory.resolve()
    if path.is_symlink() or path.parent.resolve() != expected:
        raise ValueError("source_file_identity_mismatch")
    _verified_file(root, path, expected, entry["sha256"], _file_stamp(path))
    return path


def _inputs(root, now):
    target, master, universe = _source_universe(root)
    daily = read_json(root / "reports/cloud_market_data.json")
    calendar = daily.get("calendar_dates") or []
    if (len(calendar) < 62 or calendar[-1] != target or calendar != sorted(set(calendar))
            or now < datetime.fromisoformat(target+"T15:00:00+08:00")):
        raise ValueError("price_observation_identity_invalid")
    raw_path = root / "reports/v4_daily_download_manifest.json"
    raw = read_json(raw_path)
    if (not str(raw.get("source", "")).startswith("BaoStock ")
            or raw.get("query", {}).get("end_date") != target
            or raw.get("query", {}).get("frequency") != "d"
            or raw.get("universe_security_master", {}).get("sha256") != master):
        raise ValueError("price_observation_identity_invalid")
    files = {r["code"]: r for r in raw["files"]}
    if len(files) != len(raw["files"]):
        raise ValueError("price_observation_identity_invalid")
    from .cloud_daily_revisions import load_files
    files = load_files(root, files, target, now=now)
    with (root / "research/data/stock_basic_baostock.csv").open(encoding="utf-8-sig", newline="") as stream:
        codes = {r["code"] for r in csv.DictReader(stream) if r["code"] in universe and (not r["out_date"] or r["out_date"] >= target)}
    directory = root / "reports/cloud_pattern_features" / target
    manifest = read_json(directory / "manifest.json")
    if (manifest.get("schema") != price.SCHEMA or manifest.get("target_date") != target
            or manifest.get("master_sha256") != master or not isinstance(manifest.get("items"), dict)):
        raise ValueError("price_observation_missing")
    contract = {"feature_implementation_sha256": price.IMPLEMENTATION_SHA256,
                "rule_sha256": price.research_catalog()["rule_sha256"], "rule_implementation_sha256": price.ENGINE_SHA256,
                "receipt_validation_sha256": sha(Path(price.__file__).with_name("cloud_pattern_inputs.py")),
                "daily_industry_validation_sha256": price.INDUSTRY_SHA256,
                "daily_revision_validation_sha256": sha(Path(price.__file__).with_name("cloud_daily_revisions.py")),
                "calendar_sha256": price.digest(calendar), "master_sha256": master}
    watched = [root / "reports/cloud_market_data.json", raw_path, root / "research/data/stock_basic_baostock.csv", directory / "manifest.json"]
    return {"target": target, "calendar": calendar, "codes": codes, "raw": files, "contract": contract,
            "directory": directory, "manifest": manifest, "watched": {p: sha(p) for p in watched}, "now": now}


def _observation(root, inputs, code, *, sources=False, recompute=False):
    entry = inputs["manifest"]["items"].get(code)
    if not entry:
        raise ValueError("price_observation_missing")
    row = read_json(_bound(root, entry, inputs["directory"] / "observations"))
    _bound(root, entry, inputs["directory"] / "observations")
    refs = row.get("input_references") or {}
    identity = price.digest({"code": code, "target": inputs["target"], "source_contract": inputs["contract"],
                             "source_sha256": {k: v["sha256"] if v else None for k, v in refs.items()}})
    if (row.get("schema") != price.SCHEMA or row.get("source_contract") != inputs["contract"]
            or row.get("code") != code or row.get("data_as_of") != inputs["target"]
            or row.get("input_identity") != identity or entry.get("input_identity") != identity
            or set(refs) != {"raw", "adjustment", "industry"}
            or refs["raw"] != inputs["raw"].get(code.split(".")[1])
            or any(row.get(k) != v for k, v in price.BOUNDARY.items())):
        raise ValueError("price_observation_identity_invalid")
    stamp = datetime.fromisoformat(row["observed_at"])
    if stamp.tzinfo is None or stamp > inputs["now"]:
        raise ValueError("price_observation_after_check")
    if sources or recompute:
        receipt_dir = root / "reports/cloud_pattern_inputs" / inputs["target"] / "receipts"
        raw = _bound(root, refs["raw"], root / "research/data/v4_daily")
        adjustment = read_json(_bound(root, refs["adjustment"], receipt_dir))
        industry = None
        if refs["industry"]:
            parent = (root / refs["industry"]["path"]).parent
            if parent not in {receipt_dir, root / "reports/cloud_industry_daily" / inputs["target"] / "receipts"}:
                raise ValueError("price_observation_identity_invalid")
            industry = read_json(_bound(root, refs["industry"], parent))
        if recompute and row.get("status") == "price_features_computed":
            checked = price.stock_observation(raw, adjustment, industry, calendar=inputs["calendar"],
                                             target=inputs["target"], code=code, as_of=row["observed_at"])
            if any(row.get(k) != v for k, v in checked.items()):
                raise ValueError("price_recomputation_mismatch")
            # Recheck bytes after reading; none of these sources is rewritten.
            _bound(root, refs["raw"], root / "research/data/v4_daily")
            _bound(root, refs["adjustment"], receipt_dir)
            if refs["industry"]: _bound(root, refs["industry"], parent)
    return row


def _industry_cross_section(root, inputs, *, stop_event=None):
    if set(inputs["manifest"]["items"]) != inputs["codes"]:
        raise ValueError("industry_cross_section_incomplete")
    # Reuse verified immutable files, never partial industry results. A timed
    # out attempt retains successful file checks for the next bounded request.
    deadline = time.monotonic()+10
    rows = []
    for symbol in sorted(inputs["codes"]):
        if stop_event and stop_event.is_set():
            raise ValueError("scan_stopped")
        if time.monotonic() >= deadline:
            raise ValueError("industry_check_budget_exceeded")
        rows.append(_observation(root, inputs, symbol, sources=True))
    cross = price.cross_section(rows, target_count=len(inputs["codes"]))
    if cross["status"] != "complete":
        raise ValueError("industry_cross_section_incomplete")
    return {"rows": {r["code"]: r for r in cross["rows"]}, "coverage": len(rows),
            "input_identity": price.digest(sorted(r["input_identity"] for r in rows)),
            "available_at": max((r["observed_at"] for r in rows), key=datetime.fromisoformat)}


def _industry_value(cross, code):
    match = cross["rows"].get(code)
    if match is None or not math.isfinite(match["factor_values"]["industry_excess_20"]):
        raise ValueError("industry_cross_section_incomplete")
    return {"value": match["factor_values"]["industry_excess_20"], "industry_code": match["industry_code"],
            **{k: cross[k] for k in ("coverage", "input_identity", "available_at")}}


def _industry_factor(root, inputs, code):
    return _industry_value(_industry_cross_section(root, inputs), code)


def _membership_universe(root, context, spec, now):
    source = timing.source_for(spec, context)
    if not timing.usable(source, timing.selected(spec)):
        return {"status": "input_incomplete", "members": {}, "sectors": [], "scope_complete": False,
                "unresolved_sectors": [], "valid_until": None,
                "label": source.get("summary") or "指定时点主线成分待核；不以其他时段观察替代。"}
    stamp = datetime.fromisoformat(source["observed_at"])
    bucket = mainline.boards.bucket(stamp)
    frame = mainline._frame(root, root / "reports/cloud_sector_observations" / stamp.date().isoformat() / f"{bucket:02d}.json", stamp.date().isoformat())
    if (frame.get("error") or frame.get("packet_id") != source.get("board_packet_id")
            or not frame.get("member_packet_id") or frame["member_packet_id"] != source.get("member_packet_id")
            or frame["member_stamp"] > now
            or (timing.selected(spec) == timing.DAILY and (bucket != 47
                or frame["member_stamp"] > datetime.fromisoformat(source["evidence_cutoff_at"])))):
        raise ValueError("mainline_packet_changed")
    selected = [r for r in source["rows"] if r["stage"] in spec["mainline_stages"] and r.get("metrics")]
    found, unresolved = {}, []
    # Unknown stages are coverage gaps, never an empty, successfully scanned
    # sector. Known non-selected stages remain outside this recipe's scope.
    for row in source["rows"]:
        if row["stage"] == "input_incomplete":
            unresolved.append({"sector": row["sector"], "reason": "主线阶段或成分待核验。"})
    for row in selected:
        key = mainline._key(row["sector"])
        members = frame["members"].get(key)
        if not members:
            unresolved.append({"sector": row["sector"], "reason": ERRORS["mainline_members_missing"]})
            continue
        for code, member in members.items():
            found.setdefault(code, []).append({"sector": {k: row["sector"][k] for k in ("kind", "code", "name")},
                          "stock_name": member["stock_name"], "stage": row["stage"],
                          "member_quote_at": member["data_as_of"]})
    valid_until = None
    if timing.selected(spec) == timing.LIVE:
        quotes = list(frame["values"].values()) + [r for group in frame["members"].values() for r in group.values()]
        oldest = min(datetime.fromisoformat(r["data_as_of"]) for r in quotes)
        bucket_end = stamp.replace(second=0, microsecond=0) + timedelta(minutes=5-stamp.minute % 5)
        valid_until = min(oldest+timedelta(seconds=180), bucket_end).isoformat()
    return {"status": "observed", "members": found, "sectors": [r["sector"] for r in selected],
            "scope_complete": not unresolved, "unresolved_sectors": unresolved, "valid_until": valid_until,
            "label": "已读取所选阶段的完整成分；缺失板块单列。",
            "observed_at": frame["member_stamp"].isoformat(), "member_packet_id": frame["member_packet_id"]}


def _member_result(universe, code):
    if universe["status"] != "observed":
        return {"status": "input_incomplete", "sectors": [], "label": universe["label"]}
    # The single-stock check still requires complete selected-sector evidence.
    if any(r["reason"] == ERRORS["mainline_members_missing"] for r in universe["unresolved_sectors"]):
        raise ValueError("mainline_members_missing")
    found = universe["members"].get(code, [])
    return {"status": "matched" if found else "not_matched", "sectors": found,
            "label": "属于指定时点、所选阶段的主线成分" if found else "不在指定时点、所选阶段的主线成分内",
            "observed_at": universe["observed_at"], "member_packet_id": universe["member_packet_id"]}


def _membership(root, context, spec, code, now):
    return _member_result(_membership_universe(root, context, spec, now), code)


class Evidence:
    """One scan's shared reads; never reuse evidence across scan revisions."""

    def __init__(self, root, now, library, *, stop_event=None):
        self.root, self.now, self.library = Path(root), now, library
        self.stop_event = stop_event
        self.context = public_context(root=self.root, now=now)
        self.cache = {}

    def once(self, key, read):
        if key not in self.cache:
            try: self.cache[key] = (True, read())
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                # Re-raising one cached exception grows a traceback retaining
                # every checked stock. Keep its reason, not the exception.
                self.cache[key] = (False, str(exc))
        success, value = self.cache[key]
        if not success: raise ValueError(value)
        return value

    def inputs(self):
        return self.once("inputs", lambda: _inputs(self.root, self.now))

    def observation(self, code):
        return self.once(("stock", code), lambda: _observation(self.root, self.inputs(), code, recompute=True))

    def code(self, symbol):
        return self.once("codes", lambda: {c.split(".")[1]: c for c in self.inputs()["codes"]}).get(symbol)

    def industry(self, code):
        cross = self.once("industry", lambda: _industry_cross_section(self.root, self.inputs(), stop_event=self.stop_event))
        return _industry_value(cross, code)

    def universe(self, spec):
        key = ("members", timing.selected(spec), tuple(spec["mainline_stages"]))
        return self.once(key, lambda: _membership_universe(self.root, self.context, spec, self.now))


def _timing(root, target, now, available_at):
    from .next_session_sectors import session_dates
    session_dates(root, now)  # Validate actual calendar coverage, not weekdays.
    calendar = read_json(root / "reports/current_market_calendar.json")["trading_dates"]
    following = next((day for day in calendar if day > target), None)
    if target not in calendar or following is None:
        raise ValueError("price_observation_identity_invalid")
    deadline = datetime.fromisoformat(following+"T09:30:00+08:00")
    late = any(datetime.fromisoformat(value) >= deadline for value in available_at)
    status = "evidence_after_entry_window" if late else "expired" if now >= deadline else "next_open_pending"
    return {"status": status, "signal_day": target, "expires_at": deadline.isoformat(),
            "label": {"evidence_after_entry_window": "部分证据在次日开盘后才可得，不能回填开盘买点。",
                      "expired": "本日线信号的次日开盘窗口已过；只保留研究观察。",
                      "next_open_pending": "等待下一交易日开盘核验；尚未验证开盘价、成交与账户约束。"}[status]}


def _entry_terms(root, observation, mechanism):
    # Recover from the same verified raw/adjustment files; do not change the
    # frozen feature producer or use a current quote as the signal-day close.
    from .cloud_pattern_route import _price_terms, IMPLEMENTATION_SHA256 as terms_sha
    target = observation["data_as_of"]
    terms = _price_terms(root, observation, target)
    refs = observation["input_references"]
    price._bound(root, refs["raw"], root / "research/data/v4_daily")
    price._bound(root, refs["adjustment"], root / "reports/cloud_pattern_inputs" / target / "receipts")
    close = terms["close"]
    risk = terms["pullback_stop"] if mechanism["id"] == "mainline_dry_pullback" else terms["low"]
    if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in (close, risk)):
        raise ValueError("price_inputs_need_review")
    execution = mechanism["execution"]
    distance = (close - risk) / close
    valid = 0 < distance <= execution["maximum_initial_stop_distance"]
    cap = close * (1 + execution["maximum_open_gap"])
    return {"status": "within_signal_risk_boundary" if valid else "signal_risk_boundary_failed",
            "implementation_sha256": terms_sha,
            "label": "信号日价格与风险线通过原规则核验；仍待实际开盘验证。" if valid else "信号日风险线无效或初始风险超过原战法上限；不形成入场预案。",
            "reference_close": close, "risk_line": risk, "signal_risk_fraction": distance,
            "maximum_entry_open": cap,
            "maximum_risk_fraction": execution["maximum_initial_stop_distance"],
            "entry_range": {"lower_exclusive": risk,
                            "upper_inclusive": min(cap, risk / (1 - execution["maximum_initial_stop_distance"]))} if valid else None,
            "price_basis": "信号日未复权价格；多日风险线按已核验复权链折算至信号日。次日除权除息和报价单位仍须另行核验。"}


def _research_plan(result, record):
    mechanism = record["mechanism"]
    return {"status": "conditional_research_not_order", "spec_id": result["spec_id"],
            "implementation_sha256": IMPLEMENTATION_SHA256,
            "rule_identity": record["rule_identity"], "checked_at": result["checked_at"],
            "input_identity": result["price"]["input_identity"], "signal_day": result["price"]["data_as_of"],
            "entry_terms": result["entry_terms"], "expires_at": result["timing"]["expires_at"],
            "entry_rule": "只研究下一交易日开盘；须严格高于风险线，且不超过追价与8%初始风险共同上限。不是盘中买入区间。",
            "profit_target_formula": "实际成交价 + 2 ×（实际成交价 − 风险线）", "profit_target": None,
            "exit_rule": mechanism["exit"], "holding_sessions": mechanism["execution"]["holding_sessions"],
            "cancel_condition": mechanism["invalidation"],
            "unresolved": ["所选细分情绪、主线和形态的持仓后退出尚未形成经验证执行协议。",
                           "下一交易日开盘报价、除权除息、停牌与涨跌停可成交性尚未核验；排队不等于成交。",
                           "本检查未读取账户；真实现金、库存、可卖数量、组合风险和成本尚未联算，不代表账户未录入。",
                           "该研究版本的成本后、样本外与前向收益资格未通过，不接入正式计划或提醒。"],
            "estimated_cost": None, "account_evaluated": False, **dev.BOUNDARY}


def validated_spec(record, library):
    spec = dev.validate(record["spec"], library)
    mechanism = next(r for r in library["mechanisms"] if r["id"] == spec["base_playbook_id"])
    semantics = {k: v for k, v in spec.items() if k not in {"name", "hypothesis", "parent_id"}}
    if record["mechanism"] != mechanism or record["rule_identity"] != dev.digest(semantics):
        raise ValueError("saved_mechanism_mismatch")
    return spec


def evaluate(root, spec_id, symbol, *, now=None):
    root = Path(root); now = now or datetime.now(price.SHANGHAI)
    if not re.fullmatch(r"[0-9a-f]{64}", spec_id) or not re.fullmatch(r"[0-9]{6}", symbol):
        raise ValueError("请输入已保存版本和六位股票代码。")
    record = dev.read_record(dev.directory(root) / (spec_id+".json"))
    library = dev.catalog()
    validated_spec(record, library)
    return evaluate_record(record, symbol, Evidence(root, now, library))


def evaluate_record(record, symbol, evidence, *, verify_inputs=True):
    root, now, context = evidence.root, evidence.now, evidence.context
    spec_id = record["spec_id"]
    spec = validated_spec(record, evidence.library)
    environment = dev.route(spec, context, evidence.library)
    kind = record["mechanism"]["kind"]
    result = {"schema": SCHEMA, "spec_id": spec_id, "spec_name": spec["name"], "stock_code": symbol,
              "checked_at": now.isoformat(), "implementation_sha256": IMPLEMENTATION_SHA256,
              "timing_protocol": spec["timing_protocol"], "timing_label": timing.PROTOCOLS[spec["timing_protocol"]]["name"],
              "environment": environment, "membership": None, "price": None, "conditions": None,
              "timing": None, "entry_terms": None, "research_plan": None, "issues": [], "all_entry_requirements_met": False,
              "actual_fill": False, "stock_trigger_evaluated": False, "factor_values_read": False, **dev.BOUNDARY}
    if kind == "risk_review":
        return {**result, "status": environment["status"], "label": (
            "防守情绪已匹配；不等待新仓形态，请按原计划核对已有风险与可卖库存。"
            if environment["status"] == "risk_review_required" else
            "本版本的防守情绪条件未匹配；已有持仓仍按原计划复核，不依赖新仓输入。")}
    try:
        inputs = evidence.inputs()
        code = evidence.code(symbol)
        if code is None: raise ValueError("stock_outside_current_scope")
        observation = evidence.observation(code)
        if observation["status"] != "price_features_computed":
            raise ValueError("price_history_ineligible" if observation["status"] == "ineligible_history" else "price_inputs_need_review")
        factors = dict(observation["factor_values"])
        industry = None
        if any(f["factor_id"] == "industry_excess_20" for f in spec["factor_filters"]):
            try:
                industry = evidence.industry(code)
                factors["industry_excess_20"] = industry["value"]
            except (OSError, ValueError, KeyError, TypeError) as exc: result["issues"].append(_message(exc))
        shape = observation["shape_conditions"][spec["base_playbook_id"]]
        result["conditions"] = dev.condition_check(spec, shape=shape, factors=factors)
        result.update(stock_trigger_evaluated=True, factor_values_read=True,
                      price={"status": "verified", "data_as_of": inputs["target"], "available_at": observation["observed_at"],
                             "input_identity": observation["input_identity"], "checked_rows": observation["checked_price_rows"],
                             "shape": shape, "factor_values": factors, "industry_cross_section": industry,
                             "is_st": observation["is_st"], "raw_sha256": observation["input_references"]["raw"]["sha256"]})
        if kind == "entry_research":
            result["entry_terms"] = _entry_terms(root, observation, record["mechanism"])
            try: result["membership"] = _member_result(evidence.universe(spec), symbol)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result["membership"] = {"status": "input_incomplete", "sectors": [], "label": _message(exc)}
            available = [observation["observed_at"]]
            if industry: available.append(industry["available_at"])
            if (context.get("source") or {}).get("captured_at"): available.append(context["source"]["captured_at"])
            if result["membership"].get("observed_at"): available.append(result["membership"]["observed_at"])
            try: result["timing"] = _timing(root, inputs["target"], now, available)
            except (OSError, ValueError, KeyError, TypeError):
                result["timing"] = {"status": "input_incomplete", "label": "交易日历覆盖不足，信号有效期尚未确认。"}
            if (context.get("emotion") or {}).get("data_as_of") != inputs["target"]:
                result["issues"].append("日线形态与情绪日期不一致，不能联合解释为同次信号。")
            if observation["is_st"]: result["issues"].append("该日为ST状态，原入场机制不纳入。")
        if verify_inputs and any(sha(p) != value for p, value in inputs["watched"].items()):
            raise ValueError("price_manifest_changed")
        result["status"] = "conditions_checked"
        result["label"] = "已检查个股研究条件；环境、成分、信号有效期与成交资格分别展示。"
        if kind == "entry_research":
            result["joint_research_status"] = (
                "input_incomplete" if result["issues"] or result["conditions"]["status"] == "input_incomplete"
                or result["membership"]["status"] == "input_incomplete" or result["timing"]["status"] == "input_incomplete" else
                "conditions_not_met" if environment["status"] != "environment_matched_shape_pending"
                or result["membership"]["status"] != "matched" or result["conditions"]["status"] != "conditions_met"
                or result["entry_terms"]["status"] != "within_signal_risk_boundary" else
                "entry_window_unavailable" if result["timing"]["status"] != "next_open_pending" else
                "research_conditions_met_execution_pending")
        else:
            result["joint_research_status"] = ("shape_review_required" if shape is True and environment["status"] == "risk_review_required" else "conditions_not_met")
        result["joint_research_label"] = {
            "input_incomplete": "联合证据尚未齐备，缺口不是无机会。",
            "conditions_not_met": "本版本研究条件未同时命中。",
            "entry_window_unavailable": "研究条件命中，但开盘窗口已过或证据迟到；不补发买点。",
            "research_conditions_met_execution_pending": "研究条件已匹配；开盘成交、账户与收益资格仍待验证，不是买入指令。",
            "shape_review_required": "退出形态与环境命中，已有持仓仍须按原计划复核。",
        }[result["joint_research_status"]]
        if result["joint_research_status"] == "research_conditions_met_execution_pending":
            result["research_plan"] = _research_plan(result, record)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result.update(status="input_incomplete", label=_message(exc), price=None, conditions=None,
                      entry_terms=None, research_plan=None, stock_trigger_evaluated=False, factor_values_read=False)
    return result


@blueprint.get("/api/strategy-lab/playbook-specs/<spec_id>/stocks/<symbol>")
def check(spec_id, symbol):
    try:
        with source_lease(required=bool(current_app.config.get("REMOTE_MODE"))) as lease:
            if lease is None:
                return jsonify({"error": "源码正在更新，本次不读取混合版本；更新完成后请重新检查。"}), 503
            response = jsonify({**evaluate(dev.app_root(), spec_id, symbol), "source_version_locked": bool(lease)})
        response.headers["Cache-Control"] = "no-store"
        return response
    except ValueError as exc: return jsonify({"error": ERRORS.get(str(exc), str(exc))}), 400
    except RuntimeError: return jsonify({"error": "源码版本保护不可用，暂停本次个股检查。"}), 503
    except (OSError, KeyError, TypeError, AttributeError): return jsonify({"error": "已保存版本缺失或不可核验；请刷新版本列表。"}), 409
