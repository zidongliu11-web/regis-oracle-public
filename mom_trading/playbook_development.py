"""Versioned emotion/mainline research recipes on the original strategy site.

Recipes select a known price mechanism, causal environment and factor checks.
Saving or matching a recipe neither runs a backtest nor changes trade authority.
"""
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from math import isfinite
from pathlib import Path
import re

from flask import Blueprint, current_app, jsonify, request
from .emotion_pattern_research import PLAYBOOKS, FACTORS, research_catalog
from .emotion_system import PHASES
from .mainline_system import STAGES
from .cloud_sector_constituents import contract as mainline_input_contract
from .market_state_daily import _atomic_publish, _canonical_bytes
from . import playbook_entry_timing as timing

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "playbook_development.v1"
BOUNDARY = {"research_only": True, "production_eligible": False, "generates_alert": False,
            "automatic_trading": False, "account_write": False, "backtest_started": False,
            "conditional_returns_validated": False, "suggested_shares": None}
blueprint = Blueprint("playbook_development", __name__)
WRITABLE_ENDPOINTS = frozenset({"playbook_development.save", "playbook_development.preview"})
DEFAULT_PHASES = {
    "nr7_mainline_breakout": ["rising"],
    "mainline_dry_pullback": ["rising", "divergence_repair"],
    "repair_failed_breakdown": ["early_repair", "weak_repair"],
    "climax_failed_extension": ["overheat_pending", "crowded_climax"],
    "retreat_defense": ["unknown", "extreme_stress", "retreat", "weak_decline", "repair_fading", "strong_divergence", "overheat_pending", "crowded_climax"],
}


def digest(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def catalog():
    old = research_catalog()
    mechanisms = deepcopy(PLAYBOOKS)+[{
        "id": "retreat_defense", "name": "退潮防守与已有风险复核", "kind": "risk_review", "family": "风险管理",
        "characteristic": "情绪转弱、拥挤或输入不明时优先复核已有风险；不把防守解释为抄底。",
        "entry": "不新开仓，不因缺少主线或因子而阻断已有持仓风险复核。",
        "stop": "沿用各笔原计划的有效风险线", "exit": "按当前可卖库存、T+1和实际成交条件复核；停牌或跌停未成交继续保留。",
        "related_rejected_family": "风险减少与正收益是两种检验，不计入盈利入场策略数量。",
    }]
    for row in mechanisms:
        entry = row["kind"] == "entry_research"
        row.update(default_phases=DEFAULT_PHASES[row["id"]],
                   default_mainline_stages=["persistent"] if entry else [],
                   source_rule_sha256=old["rule_sha256"],
                   development_status="calculable_hypothesis_unvalidated" if entry else "risk_review_not_entry",
                   factor_role="可选条件对照；没有填写过滤条件时不把因子当成入场门槛。" if entry else "防守与退出不等待因子筛选。",
                   timing="完成日线信号后研究下一交易日开盘；盘中主线只能影响其实际可得时点之后的判断。" if entry else "已有风险在新观察时点复核；意图不等于成交。",
                   forbidden="未完成K线、过期信号、停牌、涨停排队未知、价格越过追价上限、风险线无效、实际费用或账户约束不齐。" if entry else "不得凭旧账户算卖出数量，不得假定T+1锁定库存或跌停仓位已卖出。",
                   invalidation="所选情绪或主线条件失效、价格形态失效或信号有效期到期；缺数据单列待核。",
                   additions="尚无验证过的加仓规则；本规格不启用加仓、摊平或自动调仓。",
                   execution=deepcopy(old["execution"]) if entry else {"position": "只提出风险复核，不计算数量或宣称已成交。"},
                   exit_priority=["风险线", "情绪条件失效", "主线条件失效", "形态失效", "2R目标", "最多5个交易日"] if entry else ["原风险线与原计划", "当前库存和可卖数量", "实际可成交条件"],
                   **BOUNDARY)
    # Read the published retirement rule itself, without loading account or
    # outcome archives. Missing identity cannot resurrect the legacy entries.
    from .system_route_joint_v2_adapter import ADAPTER_PROTOCOL_PATH, ADAPTER_PROTOCOL_SHA256
    legacy = {"status": "source_unavailable", "entries": [], "source_sha256": ADAPTER_PROTOCOL_SHA256}
    try:
        raw = ADAPTER_PROTOCOL_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ADAPTER_PROTOCOL_SHA256:
            raise ValueError("legacy_identity")
        protocol = json.loads(raw)
        retired = protocol["terminal_playbook_retirement"]["retired_entry_playbooks"]
        legacy.update(status="verified_source", entries=[{
            "id": item, "name": {"mainline_start_confirmation": "旧主线启动确认"}.get(item, item),
            "status": "retired", "reason": "既有冻结协议已退役；不提供克隆入口，不重新授予研究独立性或实盘资格。"} for item in retired]+[{
                "id": "leader_divergence_restrengthening", "name": "旧核心分歧转强", "status": "deferred",
                "reason": "旧路由仍未启用；新核心观察不等于该战法获得资格。"}])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result = {"schema": SCHEMA, "mechanisms": mechanisms, "legacy": legacy, "factors": deepcopy(FACTORS),
              "timing_protocols": [{"id": key, **value} for key, value in timing.PROTOCOLS.items()],
              "phases": [{"id": k, "name": v[0]} for k,v in PHASES.items()],
              "mainline_stages": [{"id": k, "name": v[0]} for k,v in STAGES.items()],
              "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "emotion_implementation_sha256": hashlib.sha256((ROOT/'mom_trading/emotion_system.py').read_bytes()).hexdigest(),
              "mainline_implementation_sha256": hashlib.sha256((ROOT/'mom_trading/mainline_system.py').read_bytes()).hexdigest(),
              "source_implementations": {
                  "entry_timing": hashlib.sha256(Path(timing.__file__).read_bytes()).hexdigest(),
                  "price_mechanism": hashlib.sha256((ROOT/'mom_trading/emotion_pattern_research.py').read_bytes()).hexdigest(),
                  "daily_emotion_formula": hashlib.sha256((ROOT/'research/build_market_state_feature_epoch_v1.py').read_bytes()).hexdigest(),
                  "mainline_inputs": mainline_input_contract(),
              },
              "scope": "可计算形态的情绪／主线适用性研发；保存、当前环境匹配与收益资格分开。", **BOUNDARY}
    result["catalog_sha256"] = digest(result)
    return result


def template(mechanism, library):
    return {"name": mechanism["name"], "hypothesis": mechanism["characteristic"], "base_playbook_id": mechanism["id"],
            "emotion_phases": mechanism["default_phases"], "mainline_stages": mechanism["default_mainline_stages"],
            "timing_protocol": timing.DAILY if mechanism["kind"] == "entry_research" else timing.RISK,
            "factor_filters": [], "parent_id": None, "catalog_sha256": library["catalog_sha256"]}


def validate(payload, library=None):
    library = library or catalog()
    if not isinstance(payload, dict) or set(payload) != {"name", "hypothesis", "base_playbook_id", "emotion_phases", "mainline_stages", "factor_filters", "timing_protocol", "parent_id", "catalog_sha256"}:
        raise ValueError("战法规格字段不完整或含不允许的字段。")
    if payload["catalog_sha256"] != library["catalog_sha256"]:
        raise ValueError("战法或情绪主线程序版本已变化，请重新载入模板并保留原研究版本。")
    mechanism = next((r for r in library["mechanisms"] if r["id"] == payload["base_playbook_id"]), None)
    if mechanism is None:
        raise ValueError("请选择当前可研发的价格机制；旧退役战法不接受改名克隆。")
    allowed_timing = {timing.DAILY, timing.LIVE} if mechanism["kind"] == "entry_research" else {timing.RISK}
    if not isinstance(payload["timing_protocol"], str) or payload["timing_protocol"] not in allowed_timing:
        raise ValueError("请选择与入场或风险复核相符的证据时点协议。")
    result = deepcopy(payload)
    for key,maximum in (("name",80),("hypothesis",1200)):
        if not isinstance(result[key], str) or not 1 <= len(result[key].strip()) <= maximum:
            raise ValueError("请填写名称和可证伪的机制假设，并遵守长度限制。")
        result[key] = result[key].strip()
    for key,allowed in (("emotion_phases",set(PHASES)),("mainline_stages",set(STAGES))):
        values = result[key]
        if not isinstance(values,list) or any(type(v) is not str or v not in allowed for v in values) or len(values)!=len(set(values)):
            raise ValueError("情绪或主线阶段无效、重复或格式错误。")
        result[key] = sorted(values)
    if not result["emotion_phases"] or (mechanism["kind"] == "entry_research" and (not result["mainline_stages"] or "unknown" in result["emotion_phases"] or "input_incomplete" in result["mainline_stages"])):
        raise ValueError("入场研究必须选择明确情绪和主线阶段，不能把输入不足当作行情条件。")
    filters = result["factor_filters"]
    if not isinstance(filters,list) or len(filters)>len(FACTORS):
        raise ValueError("最多使用五项已实现的因子条件。")
    seen = set()
    for row in filters:
        if (not isinstance(row,dict) or set(row)!={"factor_id","operator","threshold"}
                or type(row["factor_id"]) is not str or row["factor_id"] not in {r["id"] for r in FACTORS} or row["factor_id"] in seen
                or type(row["operator"]) is not str or row["operator"] not in {"gte","lte"} or type(row["threshold"]) not in (int,float)
                or not isfinite(row["threshold"]) or abs(row["threshold"])>1000):
            raise ValueError("因子条件必须使用不同的已实现因子、比较方向和有限数值。")
        row["threshold"] = float(row["threshold"])
        seen.add(row["factor_id"])
    result["factor_filters"] = sorted(filters,key=lambda r:r["factor_id"])
    if mechanism["kind"] != "entry_research" and (filters or result["mainline_stages"]):
        raise ValueError("防守与退出不能依赖主线或因子齐备，请清空这两项条件。")
    if result["parent_id"] is not None and (not isinstance(result["parent_id"],str) or not re.fullmatch(r"[0-9a-f]{64}",result["parent_id"])):
        raise ValueError("父版本标识无效。")
    return result


def condition_check(spec, *, shape, factors):
    """Same fixed Boolean/numeric predicates for later stock and replay callers.

    Callers must bind real input identity/time first. No truthy missing values.
    """
    missing, failed = [], []
    if type(shape) is not bool: missing.append("price_shape")
    elif not shape: failed.append("price_shape")
    for row in spec["factor_filters"]:
        value = factors.get(row["factor_id"])
        if type(value) not in (int,float) or not isfinite(value):
            missing.append(row["factor_id"])
        elif not (value >= row["threshold"] if row["operator"] == "gte" else value <= row["threshold"]):
            failed.append(row["factor_id"])
    return {"status": "input_incomplete" if missing else "conditions_not_met" if failed else "conditions_met",
            "missing": missing, "failed": failed, **BOUNDARY}


def route(spec, context, library=None):
    library = library or catalog()
    base = next((r for r in library["mechanisms"] if r["id"]==spec["base_playbook_id"]), None)
    if base is None:
        return {"name":spec["name"],"base_playbook_id":spec["base_playbook_id"],"status":"source_unavailable",
                "label":"原机制已不可用","reasons":["保留历史版本，不用另一机制代替。"],"sectors":[],
                "next_action":"核查原机制和规则版本。","stock_trigger_evaluated":False,"factor_values_read":False,**BOUNDARY}
    emotion, mainline = context.get("emotion") or {}, timing.source_for(spec, context)
    protocol = timing.selected(spec)
    phase = emotion.get("phase", "unknown") if emotion.get("status") == "computed" else "unknown"
    entry = base["kind"] == "entry_research"
    reasons, sectors = [], []
    if spec["catalog_sha256"] != library["catalog_sha256"]:
        reasons.append("保存时程序版本与当前不同，须创建新版本后再比较。")
    if phase not in spec["emotion_phases"]:
        reasons.append("当前日线情绪不在该版本事先选择的适用阶段。")
    if entry:
        if not timing.usable(mainline, protocol):
            reasons.append(mainline.get("summary") or ("信号日尾盘主线原件未就绪，不借用次日盘中主线。" if protocol == timing.DAILY
                           else "本时段主线尚未形成新鲜、完整的可比较观察；盘前名单只用于准备。"))
        elif protocol == timing.DAILY and mainline.get("signal_day") != emotion.get("data_as_of"):
            reasons.append("完成日线情绪与尾盘主线不是同一信号日。")
        elif (mainline.get("emotion_context") or {}).get("relation") not in {"previous_completed_session","same_completed_session"}:
            reasons.append("日线情绪与主线交易日未对齐。")
        else:
            sectors = [row["sector"] for row in mainline.get("rows",[]) if row.get("stage") in spec["mainline_stages"] and row.get("metrics")]
            if not sectors: reasons.append("当前主线阶段没有命中该版本的适用条件。")
    status = "environment_not_matched" if reasons else "environment_matched_shape_pending" if entry else "risk_review_required"
    return {"name": spec["name"], "base_playbook_id": spec["base_playbook_id"], "status": status,
            "label": {"environment_not_matched":"当前环境未匹配", "environment_matched_shape_pending":"环境匹配，个股触发待核", "risk_review_required":"进入已有风险复核"}[status],
            "phase": phase, "emotion_day": emotion.get("data_as_of"), "mainline_at": mainline.get("observed_at"),
            "timing_protocol": protocol, "timing_label": timing.PROTOCOLS.get(protocol, {}).get("name", "旧时点协议待核验"),
            "timing_description": timing.PROTOCOLS.get(protocol, {}).get("description", ""),
            "sectors": [{k:r.get(k) for k in ("kind","code","name")} for r in sectors], "reasons": reasons,
            "stock_trigger_evaluated": False, "factor_values_read": False,
            "next_action": ("用同一股票、同一可得时点的已核验价格和因子执行条件检查，再验证次日成交与风险约束。" if entry
                            else "依原计划、当前真实账户、可卖库存和成交条件复核；主线缺失不阻止风险检查。"), **BOUNDARY}


def directory(root):
    return Path(root)/"reports/playbook_development"


def read_record(path):
    if path.is_symlink(): raise ValueError("战法记录路径不能是符号链接。")
    record = json.loads(path.read_text())
    created = datetime.fromisoformat(record["created_at"])
    if (record.get("schema") != "playbook_spec_record.v1" or digest(record["spec"]) != path.stem
            or created.tzinfo is None or not isinstance(record["spec"].get("name"),str)
            or record["mechanism"]["id"] != record["spec"]["base_playbook_id"]
            or record.get("spec_id") != path.stem or digest({k:v for k,v in record.items() if k!="record_sha256"}) != record.get("record_sha256")
            or any(record.get(k)!=v for k,v in BOUNDARY.items())):
        raise ValueError("保存的战法规格身份校验失败。")
    return record


def save_spec(root, payload, library=None):
    library = library or catalog()
    spec = validate(payload,library)
    folder = directory(root);folder.mkdir(parents=True,exist_ok=True)
    with (folder/".lock").open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if spec["parent_id"]:
            parent = read_record(folder/(spec["parent_id"]+".json"))
            if parent["spec"]["base_playbook_id"] != spec["base_playbook_id"]:
                raise ValueError("新版本必须保留原价格机制，不能用版本关系掩盖家族变化。")
        spec_id = digest(spec);path=folder/(spec_id+".json")
        if path.exists(): return read_record(path)
        semantics = {k:v for k,v in spec.items() if k not in {"name","hypothesis","parent_id"}}
        record = {"schema":"playbook_spec_record.v1", "spec_id":spec_id, "rule_identity":digest(semantics),
                  "created_at":datetime.now(timezone.utc).isoformat(), "spec":spec,
                  "mechanism":deepcopy(next(r for r in library["mechanisms"] if r["id"]==spec["base_playbook_id"])),
                  "state":"saved_research_specification", "independent_trial":False, **BOUNDARY}
        record["record_sha256"] = digest(record)
        _atomic_publish(path,_canonical_bytes(record))
        return read_record(path)


def saved(root):
    paths = list(directory(root).glob("[0-9a-f]"*64+".json"))
    records, invalid = [], []
    for path in paths:
        try: records.append(read_record(path))
        except (OSError,ValueError,KeyError,TypeError,AttributeError): invalid.append(path.stem)
    records.sort(key=lambda r:(r["created_at"],r["spec_id"]),reverse=True)
    return {"records":records[:100], "total":len(records), "invalid_ids":invalid, "truncated":len(records)>100}


def development_context(context, *, root=ROOT):
    library = catalog();archive=saved(root)
    defaults = [template(r,library) for r in library["mechanisms"]]
    library.update(templates=defaults, archive=archive,
                   current_routes=[route(spec,context,library) for spec in defaults],
                   saved_routes=[{"spec_id":record["spec_id"],**route(record["spec"],context,library)} for record in archive["records"]],
                   capabilities={"save_version":True,"preview_environment":True,"arbitrary_code":False,"run_backtest":False})
    return library


def app_root():
    return Path(current_app.config.get("RESEARCH_WORKBENCH_ROOT",ROOT))


@blueprint.post("/api/strategy-lab/playbook-specs")
def save():
    try: return jsonify({"record":save_spec(app_root(),request.get_json(silent=True)),**BOUNDARY}),201
    except ValueError as exc: return jsonify({"error":str(exc)}),400
    except (OSError,KeyError,TypeError,AttributeError): return jsonify({"error":"战法研究记录不可读写或已损坏；原版本保留，请核查运行目录。"}),409


@blueprint.post("/api/strategy-lab/playbook-specs/preview")
def preview():
    from .emotion_system import public_context
    try:
        spec=validate(request.get_json(silent=True))
        return jsonify({"spec":spec,"route":route(spec,public_context(root=app_root())),**BOUNDARY})
    except ValueError as exc: return jsonify({"error":str(exc)}),400
