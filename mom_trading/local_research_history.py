"""Personal loopback-only view of an already captured historical stock snapshot.

Registered by the opt-in local preview launcher, never by the normal application.
No downloads, accounts, market-store writes, strategy inputs or background jobs.
"""
from __future__ import annotations

from datetime import date
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request

blueprint = Blueprint("local_research_history", __name__)
RECEIPT = "research/RQ_STOCK_HISTORY_RECOVERY_RECEIPT_20261002.json"
RECEIPT_SHA = "5c6c15efbc326a0e5f23a9728dfc5c5f1e3eabf6e80982ada3c32f21759dda99"
ETF_RECEIPT = "research/RQ_ETF_HISTORY_RECOVERY_RECEIPT_20261002.json"
ETF_RECEIPT_SHA = "cd752d56d1b38c200bac26c5dd6ecd01ad7c5c50d650361e6285f9d2062cca87"
FIELDS = ["datetime", "open", "high", "low", "close", "volume", "total_turnover"]
START, END = "2013-01-01", "2026-09-30"
BOUNDARY = {"research_only": True, "live_market_data": False, "account_access": False,
            "historical_feature_PIT_certified": False, "model_admitted": False,
            "production_eligible": False, "generates_alert": False, "automatic_orders": False}


def _require(ok):
    if not ok:
        raise ValueError("historical_source_invalid")


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid_date")
    date.fromisoformat(value)
    return int(value.replace("-", ""))


def parameters(args, include_benchmark=False):
    if set(args) - {"code", "start", "end", "limit"}:
        raise ValueError("invalid_parameters")
    if hasattr(args, "getlist") and any(len(args.getlist(k)) != 1 for k in args):
        raise ValueError("duplicate_parameters")
    code = args.get("code", "")
    if not isinstance(code, str) or not (re.fullmatch(r"(?:000|001|002|003|300|301|600|601|603|605|688|689)\d{3}", code)
                                                 or (include_benchmark and code == "510300")):
        raise ValueError("invalid_stock_code")
    start, end = args.get("start", START), args.get("end", END)
    first, last = _date(start), _date(end)
    if not _date(START) <= first <= last <= _date(END):
        raise ValueError("invalid_window")
    limit = args.get("limit", "250")
    if not isinstance(limit, str) or not re.fullmatch(r"[1-9]\d{0,3}", limit) or int(limit) > 1000:
        raise ValueError("invalid_limit")
    return code, first, last, int(limit)


def _signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def _verified(path, expected, size):
    _require(path.is_file() and not path.is_symlink())
    before = _signature(path)
    _require(before[0] == size)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    _require(digest.hexdigest() == expected and _signature(path) == before)
    return before


def _safe_dataset(file, key, h5py, np):
    # Same bounded storage checks as research/recover_rq_etf_history.safe_dataset,
    # without importing that command-line acquisition module or its transport.
    _require(isinstance(file.get(key, getlink=True), h5py.HardLink))
    ds = file[key]
    _require(isinstance(ds, h5py.Dataset) and not ds.is_virtual and not ds.external)
    _require(ds.ndim == 1 and ds.shape[0] <= 30000 and not ds.dtype.hasobject and ds.dtype.itemsize <= 256)
    _require(not ds.chunks or int(np.prod(ds.chunks)) * ds.dtype.itemsize <= 64 * 1024**2)
    cp = ds.id.get_create_plist()
    _require({cp.get_filter(i)[0] for i in range(cp.get_nfilters())} <= {1, 2, 3, 4, 5, 6})
    return ds


class HistoryReader:
    def __init__(self, path, expected_sha, size, calendar_path, calendar_sha, calendar_size,
                 captured_at, dependencies=None, instrument="stock"):
        from research.inspect_short_rq_full import load_dependencies, read_calendar
        self.path, self.calendar_path = Path(path), Path(calendar_path)
        _require(instrument in {"stock", "benchmark"})
        self.instrument = instrument
        self.signature = _verified(self.path, expected_sha, size)
        self.calendar_signature = _verified(self.calendar_path, calendar_sha, calendar_size)
        self.sha, self.captured_at = expected_sha, captured_at
        self.h5py, self.np = dependencies or load_dependencies()
        calendar = read_calendar(self.calendar_path, self.np)
        _require(not self.np.any(self.np.diff(calendar.astype(self.np.int64)) <= 0))
        self.calendar = set(map(int, calendar))

    def read(self, code, start, end, limit):
        from research.inspect_short_rq_full import validate_bar_dtype
        # Callers outside HTTP receive the same validation and bounded semantics.
        code, start, end, limit = parameters({"code": code, "start": self._iso(start),
                                             "end": self._iso(end), "limit": str(limit)},
                                             include_benchmark=self.instrument == "benchmark")
        _require((code == "510300") == (self.instrument == "benchmark"))
        _require(_signature(self.path) == self.signature
                 and _signature(self.calendar_path) == self.calendar_signature)
        key = code + (".XSHG" if code.startswith("6") or code == "510300" else ".XSHE")
        h5py, np = self.h5py, self.np
        with h5py.File(self.path, "r", rdcc_nbytes=1048576) as file:
            if key not in file:
                raise LookupError("stock_not_in_snapshot")
            ds = _safe_dataset(file, key, h5py, np)
            validate_bar_dtype(ds.dtype, FIELDS)
            times = ds.fields("datetime")[:]
            _require(times.dtype.kind in "iu")
            long = times > 100000000
            _require(bool(np.all((~long) | (times % 1000000 == 0))))
            dates = np.where(long, times // 1000000, times).astype(np.int64)
            _require(not np.any(np.diff(dates) <= 0))
            for day in dates:
                _date(self._iso(int(day)))
                _require(int(day) in self.calendar)
            offsets = np.flatnonzero((dates >= start) & (dates <= end))
            matching = len(offsets)
            offsets = offsets[-limit:]
            selected = ds.fields(FIELDS)[int(offsets[0]):int(offsets[-1]) + 1] if matching else []
            rows = []
            for day, row in zip(dates[offsets], selected):
                values = {f: float(row[f]) for f in FIELDS[1:]}
                _require(all(np.isfinite(v) for v in values.values()))
                _require(0 < values["low"] <= min(values["open"], values["close"])
                         <= max(values["open"], values["close"]) <= values["high"])
                _require(values["volume"] >= 0 and values["total_turnover"] >= 0)
                rows.append({"time": self._iso(int(day)), **values})
        _require(_signature(self.path) == self.signature
                 and _signature(self.calendar_path) == self.calendar_signature)
        return {"status": "available" if rows else "empty_window", "code": code, "rows": rows,
                "instrument": self.instrument,
                "returned_rows": len(rows), "matching_rows": matching, "truncated": matching > limit,
                "requested_start": self._iso(start), "requested_end": self._iso(end),
                "snapshot_start": START, "snapshot_end": END, "captured_at": self.captured_at,
                "source": "RQ 免费历史回测数据快照", "source_sha256": self.sha,
                "price_basis": "提供方历史快照；文档约定未复权，未逐行认证",
                "units": "保留提供方数值；成交量未换算为手，未独立认证单位",
                **BOUNDARY}

    @staticmethod
    def _iso(value):
        if type(value) is not int or not 19000101 <= value <= 29991231:
            raise ValueError("invalid_date")
        text = str(value)
        return text[:4] + "-" + text[4:6] + "-" + text[6:]


def captured_reader(root, instrument="stock"):
    root = Path(root).resolve()
    _require(instrument in {"stock", "benchmark"})
    is_etf = instrument == "benchmark"
    raw = (root / (ETF_RECEIPT if is_etf else RECEIPT)).read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == (ETF_RECEIPT_SHA if is_etf else RECEIPT_SHA))
    receipt = json.loads(raw)
    _require(receipt["state"] == "captured" and receipt["archive_complete"] is True)
    directory = Path(receipt["raw_directory"]).resolve()
    _require(not directory.is_relative_to(root) and not any("onedrive" in p.lower() for p in directory.parts))
    records = {r["name"]: r for r in receipt["retained"]}
    filename = "funds.h5" if is_etf else "stocks.h5"
    stock, calendar = records[filename], records["trading_dates.npy"]
    return HistoryReader(directory / filename, stock["sha256"], stock["bytes"],
                         directory / "trading_dates.npy", calendar["sha256"], calendar["bytes"],
                         receipt["ended_utc"], instrument=instrument)


def install(app, root):
    """Explicit local-launcher hook. Failure leaves a truthful unavailable panel."""
    app.config["LOCAL_RESEARCH_HISTORY_ENABLED"] = True
    app.extensions["local_research_root"] = Path(root).resolve()
    for key, instrument in (("local_research_history", "stock"), ("local_benchmark_history", "benchmark")):
        try:
            app.extensions[key] = captured_reader(root, instrument)
        except (OSError, ValueError, KeyError, ImportError, RuntimeError):
            app.extensions[key] = None
    app.register_blueprint(blueprint)


def local_request():
    try:
        local_peer = ipaddress.ip_address(request.remote_addr or "").is_loopback
        host = urlsplit(request.host_url).hostname
        host_ok = host == "localhost" or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False
    if not local_peer or not host_ok or any(k in request.headers for k in
                                           ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host")):
        return False
    origin = request.headers.get("Origin")
    if origin is not None and origin.rstrip("/") != request.host_url.rstrip("/"):
        return False
    return request.headers.get("Sec-Fetch-Site", "none") in {"same-origin", "none"}


@blueprint.get("/api/local-research/stock-history")
def history():
    return _history(include_benchmark=False)


@blueprint.get("/api/local-research/history")
def instrument_history():
    return _history(include_benchmark=True)


def _history(include_benchmark):
    status = 200
    if not current_app.config.get("LOCAL_RESEARCH_HISTORY_ENABLED") or not local_request():
        payload, status = {"status": "disabled", "message": "历史研究数据仅供已启用的本机页面查看。"}, 403
    else:
        try:
            query = parameters(request.args, include_benchmark=include_benchmark)
        except ValueError:
            payload, status = {"status": "invalid_request", "message": "请检查沪深 A 股或 510300 代码、日期范围和条数。"}, 400
        else:
            reader = current_app.extensions.get("local_benchmark_history" if query[0] == "510300" else "local_research_history")
            try:
                if reader is None:
                    raise OSError("snapshot_unavailable")
                payload = reader.read(*query)
            except LookupError:
                payload, status = {"status": "not_in_snapshot", "message": "此代码未收录于当前历史快照。"}, 404
            except (OSError, ValueError, RuntimeError):
                payload, status = {"status": "unavailable", "message": "历史数据文件不可用或校验未通过；请恢复已核验的本机快照。"}, 503
    return local_response(payload, status)


@blueprint.get("/api/local-research/baseline-reference")
def baseline_reference():
    if not current_app.config.get("LOCAL_RESEARCH_HISTORY_ENABLED") or not local_request():
        return local_response({"status": "disabled", "message": "基准记录仅供已启用的本机研究页面查看。"}, 403)
    if request.args:
        return local_response({"status": "invalid_request", "message": "此入口只读取原封存基准，不接受日期或参数变更。"}, 400)
    try:
        from mom_trading.local_baseline_reference import load_reference
        payload = load_reference(current_app.extensions["local_research_root"])
    except (OSError, ValueError, KeyError, TypeError):
        return local_response({"status": "unavailable", "message": "封存基准缺失或校验未通过，请恢复原件后重试。"}, 503)
    return local_response(payload, 200)


def local_response(payload, status):
    response = jsonify({**payload, **BOUNDARY})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@blueprint.get("/api/local-research/market-history")
def market_history():
    if not current_app.config.get("LOCAL_RESEARCH_HISTORY_ENABLED") or not local_request():
        return local_response({"status": "disabled", "message": "历史样本统计仅供已启用的本机研究页面查看。"}, 403)
    if request.args:
        return local_response({"status": "invalid_request", "message": "此入口只读取已保存的30日统计，不接收重算参数。"}, 400)
    try:
        from mom_trading.local_market_history import load_summary
        payload = load_summary(current_app.extensions["local_research_root"],
                               current_app.extensions.get("local_research_history"))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return local_response({"status": "unavailable", "message": "历史样本统计缺失或校验未通过；当前不显示估算家数。"}, 503)
    return local_response(payload, 200)
