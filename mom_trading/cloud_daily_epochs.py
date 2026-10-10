"""Publish complete daily source versions without rewriting prior histories.

Acquisition can advance while historical research gates remain closed. A new
source date never implies that prices, factors or entry strategies are admitted.
"""
import csv
from datetime import date, datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .cloud_daily_revisions import load_files
from .cloud_daily_tail_probe import capture
from .cloud_market_emotion import stock_history
from .cloud_pattern_features import _bound, digest
from .cloud_pattern_inputs import _source_universe, provider_deadline, sha, valid_day, SDK_PAGE_SIZE
from .cloud_research_worker import read_json, publish
from .market_state_daily import _atomic_publish, _canonical_bytes
from research.download_v4_daily_baostock import FIELDS, START_DATE, ALLOWED_PREFIXES

SCHEMA = "cloud_daily_epochs.v1"
IMPLEMENTATION_SHA256 = sha(Path(__file__))


def _save(root, directory, value, prefix):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    path = directory / (prefix+"."+hashlib.sha256(data).hexdigest()+".json")
    _atomic_publish(path, data)
    return {"path": str(path.relative_to(root)), "sha256": sha(path)}


def _calendar(root, base, cutoff, client, clock):
    first, last = date.fromisoformat(base), date.fromisoformat(cutoff)
    days = [(first+timedelta(days=i)).isoformat() for i in range((last-first).days+1)]
    if not 1 <= len(days) <= 512:
        raise ValueError("calendar_extension_requires_review")
    directory = root / "reports/cloud_daily_epochs/calendars"
    path = directory / (base+"_"+cutoff+"."+IMPLEMENTATION_SHA256+".json")
    if path.is_symlink():
        raise ValueError("calendar_receipt_identity_invalid")
    if path.exists():
        packet = read_json(path)
    else:
        packet = {"schema": SCHEMA, "implementation_sha256": IMPLEMENTATION_SHA256,
                  "provider": "BaoStock", "endpoint": "query_trade_dates", "start_date": base, "end_date": cutoff,
                  "started_at": clock().isoformat()}
        logged_in, guard = False, {"expired": False}
        try:
            with provider_deadline() as guard:
                logged_in = client.login().error_code == "0" and not guard["expired"]
                if not logged_in:
                    raise ValueError("calendar_login_failed")
                result = client.query_trade_dates(start_date=base, end_date=cutoff)
                rows = []
                while not guard["expired"] and result.error_code == "0" and result.next():
                    rows.append(result.get_row_data())
                    if len(rows) > len(days):
                        raise ValueError("calendar_extra_rows")
                packet.update(fields=list(result.fields or []), rows=rows, error_code=str(result.error_code),
                              page_rows=len(result.data), cursor=result.cur_row_num, page_number=str(result.cur_page_num))
        except Exception as exc:
            packet["error_class"] = type(exc).__name__
            raise
        finally:
            packet.update(completed_at=clock().isoformat(), deadline_expired=guard["expired"])
            _save(root, directory, packet, "attempt")
            if logged_in:
                with provider_deadline():
                    client.logout()
        # A failed calendar never becomes the persistent successful answer.
    if (packet.get("schema") != SCHEMA or packet.get("implementation_sha256") != IMPLEMENTATION_SHA256
            or packet.get("start_date") != base or packet.get("end_date") != cutoff
            or packet.get("provider") != "BaoStock" or packet.get("endpoint") != "query_trade_dates"
            or packet.get("fields") != ["calendar_date", "is_trading_day"]
            or packet.get("error_code") != "0" or packet.get("deadline_expired") is not False
            or type(packet.get("page_rows")) is not int or packet["page_rows"] != len(days)
            or type(packet.get("cursor")) is not int or packet["cursor"] != len(days)
            or packet.get("page_number") != "1" or len(days) >= SDK_PAGE_SIZE
            or len(packet.get("rows", [])) != len(days)
            or any(len(row) != 2 or row[1] not in {"0", "1"} for row in packet["rows"])
            or [row[0] for row in packet["rows"]] != days):
        raise ValueError("calendar_response_incomplete")
    stamps = [datetime.fromisoformat(packet[key]) for key in ("started_at", "completed_at")]
    if any(stamp.tzinfo is None for stamp in stamps) or not stamps[0] <= stamps[1] <= clock():
        raise ValueError("calendar_timestamp_invalid")
    if not path.exists():
        _atomic_publish(path, _canonical_bytes(packet))
    return [row[0] for row in packet["rows"] if row[1] == "1"], {"path": str(path.relative_to(root)), "sha256": sha(path)}


def _securities(root, target):
    result = {}
    with (root / "research/data/stock_basic_baostock.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            code = row["code"]
            if row["type"] != "1" or not code.startswith(ALLOWED_PREFIXES):
                continue
            if (not re.fullmatch(r"(?:sh|sz)\.\d{6}", code) or not valid_day(row["ipo_date"])
                    or (row["out_date"] and (not valid_day(row["out_date"]) or row["out_date"] < row["ipo_date"])) or code in result):
                raise ValueError("epoch_security_identity_invalid")
            if row["ipo_date"] <= target and (not row["out_date"] or row["out_date"] >= START_DATE):
                result[code] = row
    return dict(sorted(result.items()))


def _next_file(root, proposal, code, security, clock, client, *, source_retry=False):
    base = proposal["sources"].get(code.split(".")[1])
    if not base and security["ipo_date"] <= proposal["base_date"]:
        raise ValueError("epoch_base_source_missing")
    rows = []
    if base:
        with gzip.open(_bound(root, base, root / "research/data/v4_daily"), "rt", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if set(reader.fieldnames or []) != set(FIELDS.split(",")):
                raise ValueError("epoch_source_schema_invalid")
            rows = list(reader)
    days = [day for day in proposal["calendar"] if proposal["base_date"] < day
            and security["ipo_date"] <= day <= (security["out_date"] or proposal["target_date"])]
    if not days:
        if not base:
            raise ValueError("epoch_base_source_missing")
        return {"source": base, "receipt": None, "added_rows": 0}
    additions, receipt, observed = capture(root, code=code, days=days,
        basis=proposal["base_manifest"], basis_observed_at=proposal["observed_at"], raw=base,
        calendar_sha=digest(proposal["calendar"]), clock=clock, client=client, reuse_complete=not source_retry)
    if rows and rows[-1]["date"] > proposal["base_date"]:
        raise ValueError("epoch_base_contains_future_rows")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS.split(","))
    writer.writeheader()
    writer.writerows(rows+additions)
    # ponytail: immutable full gzip versions preserve existing readers. This
    # trades disk for compatibility; retain the 2GB gate, use delta-aware
    # readers before introducing any future retention/garbage collection.
    data = gzip.compress(output.getvalue().encode(), mtime=0)
    content_sha = hashlib.sha256(data).hexdigest()
    path = root / "research/data/v4_daily" / (code.split(".")[1]+"."+content_sha+".epoch.gz")
    # Validate actual source values and listing/calendar identity; historical
    # gaps or special-event questions remain visible to their existing gates.
    with tempfile.TemporaryDirectory(prefix="regis-daily-epoch-") as temporary:
        candidate = Path(temporary) / "candidate.gz"
        candidate.write_bytes(data)
        stock_history(candidate, code=code, ipo=security["ipo_date"], out_date=security["out_date"],
                      calendar=proposal["calendar"], special_events={})
    _atomic_publish(path, data)
    return {"source": {"code": code.split(".")[1], "path": str(path.relative_to(root)), "sha256": content_sha,
                       "bytes": len(data), "rows": len(rows)+len(additions), "start": (rows+additions)[0]["date"],
                       "end": (rows+additions)[-1]["date"]},
            "receipt": receipt, "source_observed_at": observed, "added_rows": len(additions)}


def advance(root, *, cutoff, batch_limit=40, now=None, client=None):
    """Return None when no later completed session exists; never run old updater."""
    clock = (lambda: datetime.now(timezone.utc)) if now is None else (lambda: now)
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return None
    if type(batch_limit) is not int or not 1 <= batch_limit <= 200 or not valid_day(cutoff):
        raise ValueError("epoch_request_invalid")
    if clock().tzinfo is None or cutoff > clock().astimezone(timezone(timedelta(hours=8))).date().isoformat():
        raise ValueError("epoch_cutoff_invalid")
    directory = root / "reports/cloud_daily_epochs"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current_path = root / "reports/v4_daily_download_manifest.json"
        state_path = root / "reports/cloud_market_data.json"
        pending_path = directory / "pending.json"
        if pending_path.is_symlink():
            raise ValueError("epoch_pending_path_invalid")
        pending = read_json(pending_path)
        if client is None:
            import baostock as client
        # Publication is recoverable after either pointer write. Immutable
        # prepared outputs remain the sole authority; no source is overwritten.
        if pending.get("status") == "prepared":
            return _publish_epoch(root, pending, now=clock())
        base_date, master_sha, base_universe = _source_universe(root)
        manifest, daily = read_json(current_path), read_json(state_path)
        if cutoff <= base_date:
            return None
        if shutil.disk_usage(root).free < 2*1024**3:
            raise ValueError("epoch_storage_insufficient")
        if pending.get("status") == "collecting":
            proposal = read_json(_bound(root, pending["proposal"], directory))
        else:
            dates, calendar_ref = _calendar(root, base_date, cutoff, client, clock)
            if not dates or dates[0] != base_date:
                raise ValueError("epoch_calendar_base_mismatch")
            if dates[-1] == base_date:
                return None
            if clock() < datetime.fromisoformat(dates[-1]+"T18:00:00+08:00"):
                raise ValueError("epoch_source_not_final")
            sources = {row["code"]: row for row in manifest["files"]}
            if (len(sources) != len(manifest["files"]) or set(sources) != {code.split(".")[1] for code in base_universe}
                    or manifest["query"]["end_date"] != base_date or manifest["query"].get("start_date") != START_DATE
                    or manifest["query"].get("frequency") != "d" or manifest["query"].get("fields") != FIELDS
                    or manifest["query"].get("adjustflag") != "3 (不复权)"
                    or not str(manifest.get("source", "")).startswith("BaoStock ")
                    or manifest["universe_security_master"]["sha256"] != master_sha):
                raise ValueError("epoch_manifest_identity_invalid")
            sources = load_files(root, sources, base_date, now=clock())
            proposal = {"schema": SCHEMA, "implementation_sha256": IMPLEMENTATION_SHA256,
                        "base_date": base_date, "target_date": dates[-1], "cutoff": cutoff,
                        "master_sha256": master_sha, "calendar": daily["calendar_dates"]+dates[1:],
                        "calendar_receipt": calendar_ref, "sources": sources, "base_state": daily,
                        "base_manifest": _save(root, directory, current_path.read_bytes(), "base-manifest"),
                        "observed_at": clock().isoformat()}
            pending = {"status": "collecting", "proposal": _save(root, directory, proposal, "proposal"), "items": {}}
            publish(pending_path, pending)
        if (proposal.get("schema") != SCHEMA or proposal["implementation_sha256"] != IMPLEMENTATION_SHA256 or proposal["base_date"] != base_date
                or proposal["master_sha256"] != master_sha or sha(current_path) != proposal["base_manifest"]["sha256"]
                or daily.get("calendar_dates") != proposal["base_state"]["calendar_dates"]):
            raise ValueError("epoch_basis_changed")
        securities = _securities(root, proposal["target_date"])
        todo = [code for code in securities if code not in pending["items"]]
        failures = pending.setdefault("source_failures", {})
        for code in todo[:batch_limit]:
            try:
                result = _next_file(root, proposal, code, securities[code], clock, client, source_retry=code in failures)
            except ValueError as exc:
                if not str(exc).startswith(("market_history_", "revision_tail_receipt_invalid")):
                    raise
                failures[code] = {"reason": str(exc), "checked_at": clock().isoformat()}
                publish(pending_path, pending)
                continue
            failures.pop(code, None)
            pending["items"][code] = result
            publish(pending_path, pending)
        if set(pending["items"]) != set(securities):
            return {"status": "collecting", "data_as_of": base_date, "pending_date": proposal["target_date"],
                    "downloaded": len(pending["items"]), "target": len(securities), "audited": False,
                    "source_failures": len(failures),
                    "summary": f"保留已发布{base_date}；新交易日{proposal['target_date']}已留存{len(pending['items'])}/{len(securities)}只不可变版本，{len(failures)}只待源核验；继续采集其余证券，尚未整批发布。"}
        base_manifest = read_json(_bound(root, proposal["base_manifest"], directory))
        files = [pending["items"][code]["source"] for code in securities]
        next_manifest = {**base_manifest, "generated_at": clock().isoformat(),
                         "query": {**base_manifest["query"], "end_date": proposal["target_date"]},
                         "files": files, "downloaded_symbols": len(files), "target_symbols": len(files),
                         "stored_bytes": sum(row["bytes"] for row in files), "coverage_pct": 100,
                         "ready_for_factor_research": True, "source_acquisition_only": True,
                         "daily_epoch": pending["proposal"]}
        pending.update(status="prepared", manifest=_save(root, directory, next_manifest, "manifest"))
        publish(pending_path, pending)
        return _publish_epoch(root, pending, now=clock())


def _publish_epoch(root, pending, *, now):
    directory = root / "reports/cloud_daily_epochs"
    proposal = read_json(_bound(root, pending["proposal"], directory))
    manifest = read_json(_bound(root, pending["manifest"], directory))
    current_path = root / "reports/v4_daily_download_manifest.json"
    state_path = root / "reports/cloud_market_data.json"
    daily = read_json(state_path)
    expected = _securities(root, proposal["target_date"])
    stamps = [datetime.fromisoformat(value) for value in (proposal["observed_at"], manifest["generated_at"])]
    if (proposal.get("schema") != SCHEMA or proposal["implementation_sha256"] != IMPLEMENTATION_SHA256
            or any(stamp.tzinfo is None for stamp in stamps) or not stamps[0] <= stamps[1] <= now
            or now < datetime.fromisoformat(proposal["target_date"]+"T18:00:00+08:00")
            or sha(root / "research/data/stock_basic_baostock.csv") != proposal["master_sha256"]
            or sha(current_path) not in {proposal["base_manifest"]["sha256"], pending["manifest"]["sha256"]}
            or daily["target_date"] not in {proposal["base_date"], proposal["target_date"]}
            or daily.get("calendar_dates") not in (proposal["calendar"], proposal["base_state"]["calendar_dates"])
            or set(pending["items"]) != set(expected)
            or manifest["files"] != [pending["items"][code]["source"] for code in expected]
            or manifest.get("daily_epoch") != pending["proposal"]
            or manifest["query"]["end_date"] != proposal["target_date"]):
        raise ValueError("epoch_publication_basis_changed")
    _bound(root, proposal["calendar_receipt"], directory / "calendars")
    base_manifest = read_json(_bound(root, proposal["base_manifest"], directory))
    for original in base_manifest["files"]:
        _bound(root, original, root / "research/data/v4_daily")
    for code, item in pending["items"].items():
        _bound(root, item["source"], root / "research/data/v4_daily")
        if item.get("receipt"):
            _bound(root, item["receipt"], root / "reports/cloud_daily_tail_probe" / item["source"]["end"])
        old = proposal["sources"].get(code.split(".")[1])
        if old:
            _bound(root, old, root / "research/data/v4_daily")
            if old.get("revision"):
                base_day = proposal["base_date"]
                record = read_json(_bound(root, old["revision"], root / "reports/cloud_daily_revisions" / base_day))
                for key, parent in (("receipt", "reports/cloud_daily_gap_probe"), ("basis_snapshot", "reports/cloud_market_emotion"),
                                    ("tail_receipt", "reports/cloud_daily_tail_probe")):
                    if record.get(key):
                        _bound(root, record[key], root / parent / base_day)
                if record.get("adjustment_receipt"):
                    _bound(root, record["adjustment_receipt"], root / "reports/cloud_pattern_inputs" / base_day / "receipts")
    # This legacy field means only full source coverage. audited remains False;
    # all existing price, research and trading gates still run independently.
    publish(current_path, manifest)
    publish(state_path, {**proposal["base_state"], "target_date": proposal["target_date"],
                        "calendar_dates": proposal["calendar"], "calendar_checked_through": proposal["cutoff"],
                        "audited": False, "daily_epoch": pending["proposal"], "last_audit_inputs": None})
    publish(directory / "pending.json", {**pending, "status": "published"})
    return {"status": "collecting", "data_as_of": proposal["target_date"], "downloaded": len(manifest["files"]),
            "target": len(manifest["files"]), "audited": False,
            "summary": f"已发布{proposal['target_date']}完整日线来源版本；旧原件保留，价格、研究审计与策略准入尚须各自验证。"}
