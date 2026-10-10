"""Website operations and bounded local recovery, independent of research jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from flask import jsonify, render_template, request

from .project_input_recovery import audit, restore_registry, _database, _fixed

UPLOAD_LIMIT = 32 * 1024 * 1024
ERRORS = {
    "expected_sha256_required": "请输入事先记录的64位 SHA256。",
    "source_hash_mismatch": "本次文件与期望 SHA256 不一致，未恢复。",
    "destination_exists": "目标研究库已经存在；本入口不会覆盖现有记录。",
    "nonempty_readable_registry_required": "文件不是可读取的非空因子研究库。",
    "compatible_registry_schema_required": "研究库结构与当前版本不兼容，未恢复。",
    "frozen_snapshot_required": "请提供已关闭或用 SQLite 备份得到的一致快照。",
    "snapshot_oversized": "文件超过32MiB网页上限；请使用受控命令行恢复。",
    "source_changed_during_import": "上传内容发生变化，未恢复。",
    "snapshot_changed_during_import": "快照发生变化，未恢复。",
}


def _loopback_request() -> bool:
    try:
        local = ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        local = False
    host = urlsplit(request.host_url).hostname
    return local and host in {"127.0.0.1", "localhost", "::1"}


def _local_upload_boundary(app) -> bool:
    """Existing remote auth/CSRF remains in force; recovery is local only."""
    if app.config.get("REMOTE_MODE") or not _loopback_request():
        return False
    origin = (request.headers.get("Origin") or "").rstrip("/")
    return bool(origin == request.host_url.rstrip("/")
                and request.headers.get("X-Regis-Request") == "data-center")


@contextmanager
def _snapshot():
    # Flask 3.1 per-request limit: do not widen ordinary JSON/account endpoints.
    request.max_content_length = UPLOAD_LIMIT + 64 * 1024
    request.max_form_parts = 4
    files = request.files
    if set(files) != {"snapshot"} or len(files.getlist("snapshot")) != 1:
        raise ValueError("请选择一份因子研究库快照。")
    uploaded = files["snapshot"]
    if Path(uploaded.filename or "").suffix.casefold() not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("仅接受 .db、.sqlite 或 .sqlite3 一致快照。")
    # Never join the untrusted client filename into a server path.
    with tempfile.TemporaryDirectory(prefix="regis-registry-upload-") as directory:
        path = Path(directory) / "snapshot.db"
        consumed = 0
        digest = hashlib.sha256()
        with path.open("xb") as target:
            while chunk := uploaded.stream.read(1024 * 1024):
                consumed += len(chunk)
                if consumed > UPLOAD_LIMIT:
                    raise ValueError("snapshot_oversized")
                target.write(chunk)
                digest.update(chunk)
        yield path, digest.hexdigest(), consumed


def register_routes(app, *, root: Path, readiness) -> None:
    def project_root():
        return Path(app.config.get("RESEARCH_WORKBENCH_ROOT") or root)

    def reply(data, code=200):
        response = jsonify(data)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response, code

    @app.get("/data-center")
    def data_center_page():
        return render_template("data_center.html")

    @app.get("/api/site/status")
    def website_status():
        inputs = audit(project_root())
        account_access = request.headers.get("X-Regis-Account-Access") != "false"
        # Readiness invokes account trade capacity. Opt out before calling it.
        service = readiness() if account_access else {"status": "account_readiness_not_requested"}
        registry = inputs.get("registry") or {}
        panels = inputs.get("panels") or []
        store = inputs.get("market_store") or {}
        remote = bool(app.config.get("REMOTE_MODE"))
        payload = {
            "schema": "website_foundation_status.v1",
            "account_access": account_access,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "service": {"reachable": True, "operational_ready": service.get("operational_ready") is True,
                        "status": service.get("status"), "execution_ready": False},
            "runtime": {
                "background_enabled": bool(app.extensions.get("background_monitoring_enabled")),
                "research_jobs_started_by_this_request": False,
                "automatic_trading": False,
                "local_recovery_allowed": not remote and _loopback_request(),
            },
            "inputs": inputs,
            "features": [
                {"label": "账户录入与人工记录", "href": "/positions", "state": "read_only" if remote else "available",
                 "detail": "远程账户入口只读；人工录入请使用本机。" if remote else "人工录入可用；精确仓位计算还需真实现金、持仓和当前行情。"},
                {"label": "个股查询与资料", "href": "/stock", "state": "available",
                 "detail": "可查询；价格和图表按各来源时点显示，缺失不填成零。"},
                {"label": "策略模板与规格", "href": "/strategy-lab#slabBuildLayer", "state": "read_only" if remote else "available",
                 "detail": "远程旧策略规格固定只读，可载入和检查；保存请使用本机。" if remote else "可载入模板、编辑、校验及保存符合规格的草稿；不代表回测或投产。"},
                {"label": "因子研究历史", "href": "/factor-system",
                 "state": "needs_input" if registry.get("state") != "nonempty_unvalidated" else "needs_audit",
                 "detail": "需恢复原研究库；不能新建空库代替历史，也不沿用未经核验的准入。"},
                {"label": "历史科学回测", "href": "/strategy-lab",
                 "state": "needs_input" if any(p.get("state") != "bytes_present_unvalidated" for p in panels) else "needs_audit",
                 "detail": "三份原面板、时点审计与共享面板原子发布均需核验后才能运行。"},
                {"label": "市场数据存储", "href": "/market-panorama",
                 "state": "needs_input" if store.get("state") != "nonempty_unvalidated" else "needs_audit",
                 "detail": "实际表行数见下方；数据库存在不等于数据完整、授权或实时。"},
            ],
        }
        if not account_access:
            payload["features"] = [row for row in payload["features"] if row["href"] != "/positions"]
            payload["service"]["operational_ready"] = None
        return reply(payload)

    def handle_snapshot(*, restore: bool):
        if not _local_upload_boundary(app):
            return reply({"ok": False, "code": "local_recovery_only",
                          "error": "快照检查与恢复仅允许本机同源页面操作；远程入口保持只读。"}, 403)
        try:
            with _snapshot() as (path, sha, size):
                inspection = _database(path, ("factors",), max_bytes=UPLOAD_LIMIT)
                compatible = inspection.get("registry_schema_compatible") is True
                accepted = inspection.get("state") == "nonempty_unvalidated" and compatible
                if not restore:
                    return reply({"ok": True, "sha256": sha, "size": size,
                                  "inspection": inspection, "restorable": accepted,
                                  "authenticity_verified": False, "production_eligible": False})
                expected = request.form.get("expected_sha256", "")
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise ValueError("expected_sha256_required")
                # Share the same research-mutation lock as other cooperating writers.
                import fcntl
                lock_path = _fixed(project_root(), "factor_system/state/research_mutation.lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+b") as handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return reply({"ok": False, "code": "research_storage_busy",
                                      "error": "研究存储正在写入，未恢复；请稍后重新检查。"}, 409)
                    try:
                        result = restore_registry(project_root(), path, expected)
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return reply({"ok": True, "restoration": result,
                              "message": "原快照字节已恢复；来源、配方和研究证据仍需核验，不产生交易权限。"}, 201)
        except ValueError as exc:
            code = str(exc)
            return reply({"ok": False, "code": code if code in ERRORS else "snapshot_rejected",
                          "error": ERRORS.get(code, "快照检查未通过，请确认文件类型、结构与内容。")},
                         409 if code == "destination_exists" else 400)
        except FileExistsError:
            return reply({"ok": False, "code": "destination_exists", "error": ERRORS["destination_exists"]}, 409)
        except OSError:
            return reply({"ok": False, "code": "storage_unavailable",
                          "error": "存储暂不可读写，未确认恢复成功；请刷新核对研究库状态。"}, 503)

    @app.post("/api/site/registry/inspect")
    def registry_snapshot_inspect():
        return handle_snapshot(restore=False)

    @app.post("/api/site/registry/restore")
    def registry_snapshot_restore():
        return handle_snapshot(restore=True)
