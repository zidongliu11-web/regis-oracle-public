from __future__ import annotations

import json
import hmac
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request
from service_readiness import capability_readiness

from .candles import (
    CachedCandleProvider,
    CandleDataUnavailable,
    FallbackCandleProvider,
    TencentCandleProvider,
    research_signals,
    technical_snapshot,
)
from .db import connect, initialize
from .deepseek import (
    PAID_API_DISABLED_MESSAGE,
    deepseek_status,
)
from .market import (
    CachedQuoteProvider,
    FallbackQuoteProvider,
    MarketDataUnavailable,
    SinaBatchQuoteProvider,
    TencentBatchQuoteProvider,
)
from .notifications import (
    NotificationError,
    send_wecom_markdown,
    send_wecom_once,
    wecom_configured,
)
from .paper import (
    fee_profile,
    paper_account_status,
    paper_signals,
    save_fee_profile,
)
from .product_contract import (
    build_product_readiness,
    product_dashboard_payload,
    product_quote_payload,
    product_stock_payload,
)
from .risk import RISK_PROFILE, stock_is_eligible
from .research_factory import (
    build_research_factory,
    build_research_live_status,
)
from .signals import SignalEngine
from .stock_analysis import (
    build_stock_analysis,
)
from .tradingagents_bridge import tradingagents_status
from research.autonomous_worker import (
    EVENTS_PATH as RESEARCH_WORKER_EVENTS_PATH,
    SERVICE_LOG_PATH as RESEARCH_WORKER_SERVICE_LOG_PATH,
    STATUS_PATH as RESEARCH_WORKER_STATUS_PATH,
    STOP_PATH as RESEARCH_WORKER_STOP_PATH,
    launch_worker,
    read_worker_status,
    request_worker_stop,
)


CODE_PATTERN = re.compile(r"^(?:0|3|6)\d{5}$")
MARKET_INDEX_CODES = ("sh000001", "sz399001", "sz399006")
SUPPORTED_CANDLE_INTERVALS = {"1m", "5m", "15m", "30m", "1d"}


def _product_api_token() -> str:
    direct = str(os.environ.get("PRODUCT_API_TOKEN") or "").strip()
    if direct:
        return direct
    token_path = str(os.environ.get("PRODUCT_API_TOKEN_FILE") or "").strip()
    if not token_path:
        return ""
    try:
        return Path(token_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config.from_mapping(
        DATABASE=str(Path(app.instance_path) / "trading_assistant.sqlite3"),
        # 2026-08-07：门禁系统已移除。所有研究/分析功能直接可用。
        QUOTE_PROVIDER=CachedQuoteProvider(
            FallbackQuoteProvider([
                TencentBatchQuoteProvider(),
                SinaBatchQuoteProvider(),
            ]),
            ttl_seconds=30,
        ),
        LIVE_QUOTE_PROVIDER=CachedQuoteProvider(
            FallbackQuoteProvider([
                TencentBatchQuoteProvider(),
                SinaBatchQuoteProvider(),
            ]),
            ttl_seconds=1,
        ),
        SIGNAL_ENGINE=SignalEngine(research_verified=False),
        CANDLE_PROVIDER=CachedCandleProvider(
            FallbackCandleProvider(
                [TencentCandleProvider()]
            ),
            ttl_seconds=8,
        ),
        CANDLE_RESEARCH_REPORT=str(
            Path(__file__).resolve().parent.parent
            / "reports"
            / "ema5_20_5m_preliminary.json"
        ),
        RESEARCH_REPORT=str(
            Path(__file__).resolve().parent.parent
            / "reports"
            / "multifactor_daily_walk_forward.json"
        ),
        BASELINE_RESEARCH_REPORT=str(
            Path(__file__).resolve().parent.parent
            / "reports"
            / "multifactor_daily_walk_forward_baseline.json"
        ),
        V3_RESEARCH_REPORT=str(
            Path(__file__).resolve().parent.parent
            / "reports"
            / "v3_strategy_validation.json"
        ),
        FACTOR_MINING_REPORT=str(
            Path(__file__).resolve().parent.parent
            / "reports"
            / "factor_mining_report.json"
        ),
        DEEPSEEK_REPORT_DIR=str(
            Path(__file__).resolve().parent.parent / "reports"
        ),
        RESEARCH_REPORT_DIR=str(
            Path(__file__).resolve().parent.parent / "reports"
        ),
        RESEARCH_WORKER_STATUS=str(RESEARCH_WORKER_STATUS_PATH),
        RESEARCH_WORKER_EVENTS=str(RESEARCH_WORKER_EVENTS_PATH),
        RESEARCH_WORKER_STOP=str(RESEARCH_WORKER_STOP_PATH),
        RESEARCH_WORKER_SERVICE_LOG=str(RESEARCH_WORKER_SERVICE_LOG_PATH),
        PRODUCT_API_TOKEN=_product_api_token(),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    initialize(app.config["DATABASE"])
    app.extensions["manual_candidate_cache"] = {
        "lock": threading.Lock(),
        "expires_at": 0.0,
        "codes": (),
        "payload": None,
    }
    app.extensions["research_factory_cache"] = {
        "lock": threading.Lock(),
        "expires_at": 0.0,
        "payload": None,
    }
    app.extensions["product_readiness_cache"] = {
        "lock": threading.Lock(),
        "expires_at": 0.0,
        "payload": None,
    }
    app.extensions["quant_state"] = {
        "lock": threading.Lock(),
        "status": "idle",      # idle | running | done | error
        "message": "",
        "log_lines": [],
        "payload": None,       # 最近一次回测报告
        "started_at": None,
        "finished_at": None,
        "last_params": None,
        "error_type": None,
        "error_request_id": None,
    }
    app.extensions["kronos_state"] = {
        "lock": threading.Lock(),
        "status": "idle",      # idle | running | done | error
        "message": "",
        "log_lines": [],
        "payload": None,       # 最近一次 Kronos 快照 (kronos_stock_snapshot.json 内容)
        "started_at": None,
        "finished_at": None,
        "last_params": None,
        "error_type": None,
        "error_request_id": None,
    }

    @app.before_request
    def assign_request_id() -> None:
        g.request_id = uuid.uuid4().hex

    @app.after_request
    def attach_request_id(response):
        response.headers["X-Request-ID"] = g.request_id
        return response

    def public_service_error(exc: Exception, message: str):
        app.logger.exception(
            "5100 data service error request_id=%s error_type=%s",
            g.request_id,
            type(exc).__name__,
        )
        return jsonify({"error": message, "request_id": g.request_id}), 503

    def local_runtime_health() -> dict[str, object]:
        database = {"ready": False, "error_type": None}
        database_path = Path(app.config["DATABASE"])
        try:
            if not database_path.is_file():
                raise FileNotFoundError(database_path)
            with connect(str(database_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            required = {"positions", "settings", "watchlist"}
            database["ready"] = required <= tables
            if not database["ready"]:
                database["error_type"] = "SchemaIncomplete"
        except Exception as exc:
            database["error_type"] = type(exc).__name__
            app.logger.warning(
                "5100 database health check failed: %s",
                type(exc).__name__,
                exc_info=True,
            )

        quote = {
            "ready": False,
            "last_success_at": None,
            "last_exchange_data_at": None,
            "last_freshness": [],
            "last_error_type": None,
            "observable": False,
        }
        provider = app.config.get("QUOTE_PROVIDER")
        health_snapshot = getattr(provider, "health_snapshot", None)
        if callable(health_snapshot):
            try:
                raw = dict(health_snapshot() or {})
                quote.update({
                    key: raw.get(key)
                    for key in (
                        "last_success_at",
                        "last_exchange_data_at",
                        "last_freshness",
                        "last_error_type",
                        "inflight",
                    )
                    if key in raw
                })
                quote["observable"] = True
                last_success = raw.get("last_success_at")
                if isinstance(last_success, str):
                    try:
                        last_success = datetime.fromisoformat(
                            last_success.replace("Z", "+00:00")
                        )
                    except ValueError:
                        last_success = None
                recent = False
                if isinstance(last_success, datetime):
                    if last_success.tzinfo is None:
                        last_success = last_success.astimezone()
                    age = datetime.now().astimezone() - last_success.astimezone()
                    quote["last_success_age_seconds"] = max(
                        0, round(age.total_seconds(), 3)
                    )
                    recent = -timedelta(minutes=5) <= age <= timedelta(minutes=15)
                raw_freshness = raw.get("last_freshness") or []
                freshness = {
                    raw_freshness
                } if isinstance(raw_freshness, str) else {
                    str(value) for value in raw_freshness
                }
                quote["ready"] = bool(
                    recent
                    and not raw.get("last_error_type")
                    and "stale" not in freshness
                )
            except Exception as exc:
                quote["last_error_type"] = type(exc).__name__
                app.logger.warning(
                    "5100 quote health snapshot failed: %s",
                    type(exc).__name__,
                    exc_info=True,
                )
        else:
            quote["last_error_type"] = "HealthSnapshotUnavailable"

        data_ready = bool(quote["ready"])
        return {
            "operational_ready": bool(database["ready"] and data_ready),
            "data_ready": data_ready,
            "checks": {"database": database, "quote_provider": quote},
        }

    def readiness_payload(snapshot: dict[str, object]) -> dict[str, object]:
        product = product_readiness().get("readiness") or {}
        return {
            "ready": snapshot["operational_ready"],
            "checks": snapshot["checks"],
            **capability_readiness(
                operational_ready=bool(snapshot["operational_ready"]),
                research_product_ready=bool(product.get("researchProductReady")),
                trade_signal_product_ready=bool(
                    product.get("tradeSignalProductReady")
                ),
            ),
        }

    def product_plan() -> str:
        value = str(request.headers.get("X-Product-Plan") or "standard").lower()
        return value if value in {"standard", "pro"} else "standard"

    def product_api_authorized():
        expected = str(app.config.get("PRODUCT_API_TOKEN") or "")
        if not expected:
            return jsonify({"error": "product_api_not_configured"}), 503
        supplied = str(request.headers.get("Authorization") or "")
        if not supplied.startswith("Bearer ") or not hmac.compare_digest(
            supplied[7:], expected,
        ):
            return jsonify({"error": "product_api_unauthorized"}), 401
        return None

    def product_readiness() -> dict:
        cache = app.extensions["product_readiness_cache"]
        now = time.monotonic()
        with cache["lock"]:
            if cache["payload"] is not None and now < cache["expires_at"]:
                return cache["payload"]
            live = build_research_live_status(
                report_dir=Path(app.config["RESEARCH_REPORT_DIR"]),
                worker_status_path=Path(app.config["RESEARCH_WORKER_STATUS"]),
                worker_events_path=Path(app.config["RESEARCH_WORKER_EVENTS"]),
            )
            cache["payload"] = build_product_readiness(
                live,
                token_configured=bool(app.config.get("PRODUCT_API_TOKEN")),
            )
            cache["expires_at"] = time.monotonic() + 5.0
            return cache["payload"]

    @app.get("/")
    def index():
        return render_template("index.html", risk=RISK_PROFILE.to_dict())

    @app.get("/analysis")
    def analysis_page():
        return render_template(
            "analysis.html",
            risk=RISK_PROFILE.to_dict(),
        )

    @app.get("/research")
    def research_page():
        return render_template("research.html")

    # ── 量化研究（64因子库 · IC测试 · 复合策略 · 回测） ──────────────
    @app.get("/quant")
    def quant_page():
        return render_template("quant.html")

    def quant_run_worker(state: dict, universe: int, request_id: str) -> None:
        """后台线程：子进程跑诚实 walk-forward 生产研究，完成后装填报告。"""
        def progress(line: str) -> None:
            with state["lock"]:
                state["log_lines"].append(line)
                if len(state["log_lines"]) > 200:
                    state["log_lines"] = state["log_lines"][-200:]

        try:
            from .quant.runner import run_production_study
            payload = run_production_study(universe=universe, progress=progress)
            with state["lock"]:
                state["payload"] = payload
                state["status"] = "done"
                state["finished_at"] = (
                    datetime.now().astimezone().isoformat()
                )
        except Exception as exc:
            app.logger.exception(
                "quant production worker failed request_id=%s error_type=%s",
                request_id, type(exc).__name__,
            )
            progress("❌ 生产研究失败；详情见服务端日志")
            with state["lock"]:
                state["status"] = "error"
                state["message"] = "生产研究失败，请凭错误编号查看服务端日志"
                state["error_type"] = type(exc).__name__
                state["error_request_id"] = request_id
                state["finished_at"] = (
                    datetime.now().astimezone().isoformat()
                )

    @app.get("/api/quant/status")
    def quant_status():
        state = app.extensions["quant_state"]
        with state["lock"]:
            payload = state["payload"]
            running = state["status"] == "running"
            # 站点重启后若有已生成的 production_results.json，自动加载展示
            if payload is None and state["status"] == "idle":
                try:
                    from .quant.runner import load_production_report
                    payload = load_production_report()
                    if payload is not None:
                        state["payload"] = payload
                        state["status"] = "done"
                except Exception:
                    payload = None
            summary = None
            if payload is not None:
                data_summary = payload.get("data_summary") or {}
                ic_summary = payload.get("ic_summary") or {}
                verdict = payload.get("verdict") or {}
                best = payload.get("best") or {}
                best_strat = next(
                    (s for s in (payload.get("strategies") or []) if s.get("preferred")),
                    (payload.get("strategies") or [{}])[0] or {},
                )
                summary = {
                    "n_stocks": payload.get("n_stocks") or data_summary.get("n_stocks"),
                    "n_dates": data_summary.get("n_dates"),
                    "factor_count": ic_summary.get("total_factors"),
                    "t1": ic_summary.get("t1_excellent"),
                    "t2": ic_summary.get("t2_good"),
                    "elapsed_seconds": payload.get("elapsed_seconds"),
                    "data_source": payload.get("data_source"),
                    "verdict_status": verdict.get("status"),
                    "generated_at": payload.get("generated_at"),
                    "method": payload.get("method"),
                    "best_name": best.get("name"),
                    "best_annual": best_strat.get("metrics", {}).get("annualized_pct"),
                    "best_sharpe": best_strat.get("metrics", {}).get("sharpe"),
                    "years_positive": best_strat.get("years_positive"),
                    "years_total": best_strat.get("years_total"),
                    "portfolio_ic": best_strat.get("portfolio_ic"),
                }
            return jsonify({
                "status": state["status"],
                "message": state["message"],
                "log_lines": state["log_lines"][-80:],
                "started_at": state["started_at"],
                "finished_at": state["finished_at"],
                "last_params": state["last_params"],
                "error_type": state["error_type"],
                "error_request_id": state["error_request_id"],
                "has_result": payload is not None,
                "summary": summary,
            })

    @app.get("/api/quant/report")
    def quant_report():
        state = app.extensions["quant_state"]
        with state["lock"]:
            if state["payload"] is None:
                return jsonify({"error": "no_report"}), 404
            return jsonify(state["payload"])

    @app.get("/api/quant/factor-library")
    def quant_factor_library():
        from .quant.a_share_library import (
            ALL_A_SHARE_FACTORS,
            get_factor_group_info,
        )
        return jsonify({
            "total": len(ALL_A_SHARE_FACTORS),
            "groups": get_factor_group_info(),
        })

    @app.post("/api/quant/pipeline/run")
    def quant_pipeline_run():
        state = app.extensions["quant_state"]
        data = request.get_json(silent=True) or {}
        try:
            universe = int(data.get("universe") or data.get("n_stocks") or 500)
        except (TypeError, ValueError):
            universe = 500
        universe = max(100, min(universe, 500))

        with state["lock"]:
            if state["status"] == "running":
                return jsonify({
                    "error": "already_running",
                    "message": "生产研究已在运行中，请等待完成",
                }), 409
            state["status"] = "running"
            state["message"] = ""
            state["log_lines"] = []
            state["payload"] = None
            state["last_params"] = {"universe": universe, "method": "walkforward"}
            state["started_at"] = datetime.now().astimezone().isoformat()
            state["finished_at"] = None
            state["error_type"] = None
            state["error_request_id"] = None

        thread = threading.Thread(
            target=quant_run_worker,
            args=(state, universe, g.request_id),
            name="quant-pipeline",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            with state["lock"]:
                state["status"] = "error"
                state["message"] = "生产研究启动失败，请凭错误编号查看服务端日志"
                state["error_type"] = type(exc).__name__
                state["error_request_id"] = g.request_id
                state["finished_at"] = datetime.now().astimezone().isoformat()
            return public_service_error(exc, "生产研究启动失败")
        return jsonify({
            "status": "running",
            "universe": universe,
            "method": "walkforward",
            "started_at": state["started_at"],
        })

    # ── Kronos 预测区（NeoQuasar/Kronos-small 未来K线预测） ────────────
    @app.get("/kronos")
    def kronos_page():
        return render_template("kronos.html")

    def _kronos_snapshot_path() -> Path:
        return Path(__file__).resolve().parent.parent / "research" / "kronos_stock_snapshot.json"

    def _load_kronos_snapshot() -> dict | None:
        """读取 Kronos 快照 JSON；不存在或损坏返回 None。"""
        path = _kronos_snapshot_path()
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_quant_snapshot() -> dict | None:
        """读取量化快照 JSON（提供复合排名/持仓/年化指标用于融合展示）。"""
        path = (Path(__file__).resolve().parent.parent / "research"
                / "quant_stock_snapshot.json")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def kronos_run_worker(
        state: dict, universe: int, pred_len: int, request_id: str,
    ) -> None:
        """后台线程：用 Kronos venv 子进程重算全市场快照。"""
        def progress(line: str) -> None:
            with state["lock"]:
                state["log_lines"].append(line)
                if len(state["log_lines"]) > 200:
                    state["log_lines"] = state["log_lines"][-200:]

        import subprocess
        root = Path(__file__).resolve().parent.parent
        worker = root / "research" / "kronos_snapshot.py"
        python = Path.home() / ".kronos-env" / "bin" / "python"
        try:
            progress(f"启动 Kronos 快照 (universe={universe}, pred_len={pred_len}) …")
            proc = subprocess.run(
                [str(python), str(worker),
                 "--universe", str(universe), "--pred_len", str(pred_len)],
                capture_output=True, text=True, timeout=1800,
            )
            for line in proc.stdout.splitlines():
                progress(line)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"退出码 {proc.returncode}")
            payload = _load_kronos_snapshot()
            with state["lock"]:
                state["payload"] = payload
                state["status"] = "done"
                state["finished_at"] = (
                    datetime.now().astimezone().isoformat()
                )
        except Exception as exc:
            app.logger.exception(
                "Kronos snapshot worker failed request_id=%s error_type=%s",
                request_id, type(exc).__name__,
            )
            progress("❌ Kronos 快照失败；详情见服务端日志")
            with state["lock"]:
                state["status"] = "error"
                state["message"] = "Kronos 快照失败，请凭错误编号查看服务端日志"
                state["error_type"] = type(exc).__name__
                state["error_request_id"] = request_id
                state["finished_at"] = (
                    datetime.now().astimezone().isoformat()
                )

    @app.get("/api/kronos")
    def kronos_snapshot_api():
        """合并 Kronos 预测 + 量化快照，供 /kronos 页面与分析页共用。"""
        state = app.extensions["kronos_state"]
        kronos = _load_kronos_snapshot()
        quant = _load_quant_snapshot()
        with state["lock"]:
            running = state["status"] == "running"

        # 量化快照的股票排名/持仓信息（quant stocks 为列表，转成 code→记录）
        quant_stocks = (quant or {}).get("stocks") or []
        if isinstance(quant_stocks, dict):
            quant_by_code = quant_stocks
        else:
            quant_by_code = {r.get("code"): r for r in quant_stocks if isinstance(r, dict)}
        stocks_out = {}
        kronos_stocks = (kronos or {}).get("stocks") or {}
        for code, krow in kronos_stocks.items():
            qrow = quant_by_code.get(code) or {}
            stocks_out[code] = {
                **krow,
                "quant_rank": qrow.get("rank"),
                "quant_composite": qrow.get("composite"),
                "in_portfolio": qrow.get("in_portfolio", False),
            }

        return jsonify({
            "snapshot_date": (kronos or {}).get("snapshot_date"),
            "generated_at": (kronos or {}).get("generated_at"),
            "model": (kronos or {}).get("model"),
            "pred_len": (kronos or {}).get("pred_len"),
            "universe": (kronos or {}).get("universe", 0),
            "elapsed_seconds": (kronos or {}).get("elapsed_seconds"),
            "peak_memory_mb": (kronos or {}).get("peak_memory_mb"),
            "top_20d": (kronos or {}).get("top_20d") or [],
            "bottom_20d": (kronos or {}).get("bottom_20d") or [],
            "quant_headline": (quant or {}).get("headline"),
            "quant_snapshot_date": (quant or {}).get("snapshot_date"),
            "status": state["status"],
            "running": running,
            "message": state["message"],
            "log_lines": state["log_lines"][-80:],
            "started_at": state["started_at"],
            "finished_at": state["finished_at"],
            "last_params": state["last_params"],
            "error_type": state["error_type"],
            "error_request_id": state["error_request_id"],
            "has_result": kronos is not None,
            "stocks": stocks_out,
        })

    @app.get("/api/kronos/status")
    def kronos_status():
        state = app.extensions["kronos_state"]
        with state["lock"]:
            payload = state["payload"]
            running = state["status"] == "running"
            # 站点重启后自动加载磁盘快照
            if payload is None and state["status"] == "idle":
                payload = _load_kronos_snapshot()
                if payload is not None:
                    state["payload"] = payload
                    state["status"] = "done"
            summary = None
            if payload is not None:
                summary = {
                    "snapshot_date": payload.get("snapshot_date"),
                    "generated_at": payload.get("generated_at"),
                    "model": payload.get("model"),
                    "universe": payload.get("universe"),
                    "pred_len": payload.get("pred_len"),
                    "elapsed_seconds": payload.get("elapsed_seconds"),
                    "peak_memory_mb": payload.get("peak_memory_mb"),
                }
            return jsonify({
                "status": state["status"],
                "message": state["message"],
                "log_lines": state["log_lines"][-80:],
                "started_at": state["started_at"],
                "finished_at": state["finished_at"],
                "last_params": state["last_params"],
                "error_type": state["error_type"],
                "error_request_id": state["error_request_id"],
                "has_result": payload is not None,
                "summary": summary,
            })

    @app.post("/api/kronos/run")
    def kronos_run():
        state = app.extensions["kronos_state"]
        data = request.get_json(silent=True) or {}
        try:
            universe = int(data.get("universe") or 200)
            pred_len = int(data.get("pred_len") or 30)
        except (TypeError, ValueError):
            universe, pred_len = 200, 30
        universe = max(50, min(universe, 300))
        pred_len = max(5, min(pred_len, 60))
        with state["lock"]:
            if state["status"] == "running":
                return jsonify({
                    "error": "already_running",
                    "message": "Kronos 快照已在运行中，请等待完成",
                }), 409
            state["status"] = "running"
            state["message"] = ""
            state["log_lines"] = []
            state["payload"] = None
            state["last_params"] = {"universe": universe, "pred_len": pred_len}
            state["started_at"] = datetime.now().astimezone().isoformat()
            state["finished_at"] = None
            state["error_type"] = None
            state["error_request_id"] = None
        thread = threading.Thread(
            target=kronos_run_worker,
            args=(state, universe, pred_len, g.request_id),
            name="kronos-snapshot",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            with state["lock"]:
                state["status"] = "error"
                state["message"] = "Kronos 快照启动失败，请凭错误编号查看服务端日志"
                state["error_type"] = type(exc).__name__
                state["error_request_id"] = g.request_id
                state["finished_at"] = datetime.now().astimezone().isoformat()
            return public_service_error(exc, "Kronos 快照启动失败")
        return jsonify({
            "status": "running",
            "universe": universe,
            "pred_len": pred_len,
            "started_at": state["started_at"],
        })

    @app.get("/api/health/live")
    def health_live():
        return jsonify({"status": "alive", "live": True})

    @app.get("/api/health/ready")
    def health_ready():
        snapshot = local_runtime_health()
        payload = readiness_payload(snapshot)
        payload["status"] = "ready" if payload["ready"] else "not_ready"
        return jsonify(payload), 200 if payload["ready"] else 503

    @app.get("/api/health/data")
    def health_data():
        snapshot = local_runtime_health()
        payload = {
            "status": "healthy" if snapshot["data_ready"] else "degraded",
            "ready": snapshot["data_ready"],
            "checks": {"quote_provider": snapshot["checks"]["quote_provider"]},
        }
        return jsonify(payload), 200 if payload["ready"] else 503

    @app.get("/api/health")
    def health():
        snapshot = local_runtime_health()
        readiness = readiness_payload(snapshot)
        data_health = {
            "status": "healthy" if snapshot["data_ready"] else "degraded",
            "ready": snapshot["data_ready"],
            "checks": {"quote_provider": snapshot["checks"]["quote_provider"]},
        }
        payload = {
            "status": "healthy" if readiness["ready"] else "degraded",
            "mode": "manual_confirmation_only",
            "auto_trading": False,
            "wecom_configured": wecom_configured(),
            "deepseek_configured": deepseek_status()["configured"],
            "liveness": {"status": "alive", "live": True},
            "readiness": readiness,
            "data_health": data_health,
        }
        return jsonify(payload), 200 if readiness["ready"] else 503

    @app.get("/api/dashboard")
    def dashboard():
        watchlist = _watchlist(app)
        positions = _positions(app)
        stock_codes = sorted({item["code"] for item in watchlist + positions})
        codes = [*MARKET_INDEX_CODES, *stock_codes]
        quotes, market_error = _quotes(app, codes)
        stock_quotes = {code: quotes[code] for code in stock_codes if code in quotes}
        _, triggered_observations = app.config["SIGNAL_ENGINE"].generate(
            stock_quotes
        )
        enriched_watchlist = _merge_quotes(watchlist, quotes)
        enriched_positions = _merge_quotes(positions, quotes)
        observations = _research_observations(
            enriched_watchlist,
            enriched_positions,
            triggered_observations,
        )
        market_status = _market_status(quotes, market_error)
        research = app.config["SIGNAL_ENGINE"].no_verified_strategy_notice()
        preliminary = _research_summary(app)
        if preliminary:
            research["latest_preliminary"] = preliminary
            research["message"] += (
                f" 最新日线多因子走步回测：{preliminary['trading_days']}个样本外交易日、"
                f"收益 {preliminary['total_return_pct']:+.2f}%、"
                f"胜率 {preliminary['win_rate_pct']:.2f}%，"
                f"已完成。"
            )
        v3 = _v3_summary(app)
        if v3:
            research["v3"] = v3
            research["message"] += (
                " V3冻结规则验证："
                + "、".join(
                    f"{item['strategy_id']} {item['total_return_pct']:+.3f}%"
                    for item in v3["strategies"]
                )
                + "。"
            )
        data_gate = _data_gate_summary()
        if data_gate:
            research["data_gate"] = data_gate
        factor_mining = _factor_mining_summary(app)
        if factor_mining:
            research["factor_mining"] = factor_mining
            research["message"] += (
                f" 因子挖掘已测试{factor_mining['candidate_count']}个候选，"
                f"{factor_mining['heldout_confirmed_count']}个通过独立IC测试。"
            )
        factory = _research_factory_payload(app)
        manual_pilot = factory.get("v21_manual_pilot") or {}
        current_decision = manual_pilot.get("current_decision") or {}
        opportunities = _manual_pilot_opportunities(manual_pilot)
        research["status"] = "research_active"
        research["message"] = "研究系统正常运行。"
        research["manual_pilot_open"] = True
        research["current_manual_decision"] = current_decision or None
        research["daily_candidate_policy"] = {
            "maximum": 3,
            "current": min(3, len(opportunities)),
            "allow_zero": True,
            "manual_confirmation_only": True,
        }
        return jsonify({
            "mode": "trial_manual_confirmation",
            "auto_trading": False,
            "risk": RISK_PROFILE.to_dict(),
            "watchlist": enriched_watchlist,
            "positions": enriched_positions,
            "portfolio": _portfolio_status(enriched_positions),
            "opportunities": opportunities,
            "observations": observations,
            "research": research,
            "fees": fee_profile(app.config["DATABASE"]),
            "deepseek": deepseek_status(),
            "research_worker": read_worker_status(
                Path(app.config["RESEARCH_WORKER_STATUS"])
            ),
            "paper": {
                **paper_account_status(
                app.config["DATABASE"],
                research_gate_passed=True,
                forward_status=_paper_forward_status(app),
                ),
                "autonomous_research_forward": (
                    _v44_forward_account_status(app)
                ),
            },
            "integrations": _integration_items(),
            "market": market_status,
            "wecom": {"configured": wecom_configured()},
        })

    @app.get("/api/watchlist")
    def get_watchlist():
        return jsonify({"items": _watchlist(app)})

    @app.get("/api/manual-candidates")
    def manual_candidates():
        """Return fast, explainable watchlist scenarios without bypassing research.

        This endpoint is deliberately separate from the dashboard.  Quote/K-line
        collection can be slower than a dashboard refresh, and an observation
        must never become a buy recommendation merely because its technical
        score is high.
        """
        selected: list[dict] = []
        seen: set[str] = set()
        for item in _watchlist(app) + _positions(app):
            code = str(item["code"])
            if code not in seen:
                selected.append(item)
                seen.add(code)
        selected = selected[:6]
        cache = app.extensions["manual_candidate_cache"]
        cache_codes = tuple(item["code"] for item in selected)
        now = time.monotonic()
        with cache["lock"]:
            if (
                cache["payload"] is not None
                and cache["codes"] == cache_codes
                and now < cache["expires_at"]
            ):
                return jsonify(cache["payload"])

        if not selected:
            payload = {
                "items": [],
                "mode": "manual_observation_only",
                "refresh_after_seconds": 8,
                "message": "添加1至6只重点监视股票后，系统才会读取它们的实时5分钟K线。",
                "research_gate_passed": _historical_strategy_gate(app),
            }
        else:
            codes = [item["code"] for item in selected]
            quotes, quote_error = _quotes(app, [*MARKET_INDEX_CODES, *codes])
            market = _market_status(quotes, quote_error)
            gate_passed = _historical_strategy_gate(app)
            items: list[dict] = []
            errors: list[str] = []
            for item in selected:
                code = str(item["code"])
                quote_object = quotes.get(code)
                quote = quote_object.to_dict() if quote_object else None
                eligible, reason = stock_is_eligible(
                    code,
                    (quote or {}).get("name") or str(item.get("name") or ""),
                    (quote or {}).get("price"),
                )
                if not eligible:
                    errors.append(f"{code}：{reason}")
                    continue
                try:
                    candles = app.config["CANDLE_PROVIDER"].get_candles(
                        code, 80, interval="5m"
                    )
                    analysis = build_stock_analysis(
                        code=code,
                        name=(quote or {}).get("name") or str(item.get("name") or ""),
                        candles=candles,
                        quote=quote,
                        interval="5m",
                        market_regime=market["regime"],
                        research_gate_passed=gate_passed,
                    )
                except (CandleDataUnavailable, ValueError):
                    app.logger.exception(
                        "5100 candidate analysis failed request_id=%s code=%s",
                        g.request_id,
                        code,
                    )
                    errors.append(f"{code}：K线数据暂时不可用，请稍后重试")
                    continue
                state = analysis["state"]
                decision = analysis["decision"]
                items.append({
                    "code": code,
                    "name": analysis["name"],
                    "quote": quote,
                    "information_time": analysis["information_time"],
                    "state": state,
                    "trigger": decision["buy_scenario"]["trigger"],
                    "buy_range": decision["buy_scenario"]["range"],
                    "protective_stop": decision["sell_scenario"]["protective_stop"],
                    "first_target": decision["sell_scenario"]["first_target"],
                    "research_gate_passed": gate_passed,
                    "manual_action": "观察技术条件，不构成买入建议",
                    "execution_allowed": False,
                    "recommendation_status": "research_only",
                    "reasons": state["reasons"],
                    "cautions": state["cautions"],
                    "analysis_url": f"/analysis?code={code}&interval=5m",
                })
            items.sort(
                key=lambda value: (
                    value["state"]["code"] == "prepare",
                    value["state"]["score"],
                ),
                reverse=True,
            )
            payload = {
                "items": items[:3],
                "mode": "manual_observation_only",
                "refresh_after_seconds": 8,
                "research_gate_passed": gate_passed,
                "message": (
                    "仅对你已添加的自选股进行公开实时行情与5分钟K线分析；"
                    "该区展示研究条件链，不扫描全A股，也不替代今日机会策略裁决。"
                ),
                "errors": errors,
                "market": market["regime"],
            }
        with cache["lock"]:
            cache.update({
                "expires_at": time.monotonic() + 8,
                "codes": cache_codes,
                "payload": payload,
            })
        return jsonify(payload)

    @app.get("/api/candles/<code>")
    def five_minute_candles(code: str):
        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({"error": "仅支持沪深主板和创业板6位股票代码"}), 400
        interval = str(request.args.get("interval", "5m")).strip().lower()
        if interval not in SUPPORTED_CANDLE_INTERVALS:
            return jsonify({
                "error": "K线周期仅支持 1m、5m、15m、30m、1d"
            }), 400
        try:
            limit = max(60, min(int(request.args.get("limit", 160)), 400))
        except ValueError:
            return jsonify({"error": "K线数量参数无效"}), 400
        try:
            candles = app.config["CANDLE_PROVIDER"].get_candles(
                normalized,
                limit,
                interval=interval,
            )
        except CandleDataUnavailable as exc:
            return public_service_error(exc, "K线数据暂时不可用，请稍后重试")
        latest_date = candles[-1].time.split(" ", 1)[0]
        today = datetime.now().astimezone().date().isoformat()
        now = datetime.now().astimezone()
        freshness = (
            "live"
            if latest_date == today and _a_share_continuous_session(now)
            else "closed_snapshot"
        )
        return jsonify({
            "code": normalized,
            "interval": interval,
            "source": _candle_source(interval),
            "freshness": freshness,
            "latest_time": candles[-1].time,
            "candles": [candle.to_dict() for candle in candles],
            "signals": research_signals(candles),
            "technical_snapshot": technical_snapshot(candles),
            "signal_status": "research_unverified",
            "backtest": _research_summary(app, "CANDLE_RESEARCH_REPORT"),
            "message": (
                "图中买卖标记按对应K线收盘确认，是未验证研究信号，"
                "不是对盘中最佳价的预测，只用于回测研究，"
                "不会进入强信号、企业微信买入提醒或自动交易。"
            ),
        })

    @app.get("/api/analysis/<code>")
    def stock_analysis(code: str):
        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({
                "error": "仅支持沪深主板和创业板6位股票代码"
            }), 400
        interval = str(request.args.get("interval", "5m")).strip().lower()
        if interval not in SUPPORTED_CANDLE_INTERVALS:
            return jsonify({
                "error": "分析周期仅支持 1m、5m、15m、30m、1d"
            }), 400
        try:
            candles = app.config["CANDLE_PROVIDER"].get_candles(
                normalized,
                400,
                interval=interval,
            )
        except CandleDataUnavailable as exc:
            return public_service_error(exc, "K线数据暂时不可用，请稍后重试")
        quotes, quote_error = _quotes(
            app,
            [*MARKET_INDEX_CODES, normalized],
        )
        quote_object = quotes.get(normalized)
        quote = quote_object.to_dict() if quote_object else None
        eligible, reason = stock_is_eligible(
            normalized,
            quote.get("name", "") if quote else "",
            quote.get("price") if quote else None,
        )
        if not eligible:
            return jsonify({"error": reason}), 400
        market = _market_status(quotes, quote_error)
        v3 = _v3_summary(app)
        try:
            result = build_stock_analysis(
                code=normalized,
                name=(quote or {}).get("name", ""),
                candles=candles,
                quote=quote,
                interval=interval,
                market_regime=market["regime"],
                research_gate_passed=_historical_strategy_gate(app),
            )
        except ValueError as exc:
            return public_service_error(exc, "股票分析暂时不可用，请稍后重试")
        result["source"] = {
            "quote": (quote or {}).get("source"),
            "candles": _candle_source(interval),
            "quote_error": quote_error,
        }
        result["deepseek"] = deepseek_status()
        return jsonify(result)

    @app.get("/api/analysis/<code>/live")
    def stock_analysis_live_quote(code: str):
        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({
                "error": "仅支持沪深主板和创业板6位股票代码"
            }), 400
        try:
            quotes = app.config["LIVE_QUOTE_PROVIDER"].get_quotes(
                [normalized]
            )
        except MarketDataUnavailable as exc:
            return public_service_error(exc, "实时行情暂时不可用，请稍后重试")
        quote = quotes.get(normalized)
        if quote is None or quote.price is None:
            return jsonify({"error": "实时行情未返回有效价格"}), 503
        eligible, reason = stock_is_eligible(
            normalized,
            quote.name,
            quote.price,
        )
        if not eligible:
            return jsonify({"error": reason}), 400
        response = jsonify({
            "code": normalized,
            "quote": quote.to_dict(),
            "server_time": datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            ),
            "market_session": (
                "open" if quote.freshness == "live" else "closed_or_stale"
            ),
            "poll_seconds": 2,
            "full_candle_reconcile_seconds": 10,
            "message": (
                "开盘时最新价用于更新正在形成的K线；"
                "完整分钟K线每10秒重新校准。"
            ),
        })
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/product/v1/status")
    def product_status():
        unauthorized = product_api_authorized()
        if unauthorized:
            return unauthorized
        return jsonify(product_readiness())

    @app.get("/api/product/v1/dashboard")
    def product_dashboard():
        unauthorized = product_api_authorized()
        if unauthorized:
            return unauthorized
        raw_response = dashboard()
        if isinstance(raw_response, tuple):
            return raw_response
        raw = raw_response.get_json(silent=True) or {}
        return jsonify(product_dashboard_payload(
            raw, product_readiness(), plan=product_plan(),
        ))

    @app.get("/api/product/v1/quote/<code>")
    def product_quote(code: str):
        unauthorized = product_api_authorized()
        if unauthorized:
            return unauthorized
        raw_response = stock_analysis_live_quote(code)
        if isinstance(raw_response, tuple):
            return raw_response
        return jsonify(product_quote_payload(
            raw_response.get_json(silent=True) or {},
        ))

    @app.get("/api/product/v1/stock/<code>")
    def product_stock(code: str):
        unauthorized = product_api_authorized()
        if unauthorized:
            return unauthorized
        raw_response = stock_analysis(code)
        if isinstance(raw_response, tuple):
            return raw_response
        raw = raw_response.get_json(silent=True) or {}
        return jsonify(product_stock_payload(
            raw, product_readiness(), plan=product_plan(),
        ))

    def paid_api_disabled_response(service: str):
        status = (
            tradingagents_status()
            if service == "TradingAgents"
            else deepseek_status()
        )
        return jsonify({
            **status,
            "ok": False,
            "code": "external_paid_api_disabled",
            "error": PAID_API_DISABLED_MESSAGE,
        }), 410

    @app.post("/api/ai/deepseek/analyze-stock")
    def analyze_stock_with_deepseek():
        return paid_api_disabled_response("DeepSeek")

    @app.get("/api/ai/tradingagents/status")
    def get_tradingagents_status():
        return jsonify(tradingagents_status())

    @app.post("/api/ai/tradingagents/analyze-stock")
    def analyze_stock_with_tradingagents():
        return paid_api_disabled_response("TradingAgents")

    @app.get("/api/market/index-candles/<code>")
    def market_index_candles(code: str):
        normalized = code.strip().lower()
        if normalized not in MARKET_INDEX_CODES:
            return jsonify({
                "error": "指数分时仅支持上证指数、深证成指和创业板指"
            }), 400
        interval = str(request.args.get("interval", "5m")).strip().lower()
        if interval not in SUPPORTED_CANDLE_INTERVALS:
            return jsonify({
                "error": "指数周期仅支持 1m、5m、15m、30m、1d"
            }), 400
        try:
            limit = max(60, min(int(request.args.get("limit", 160)), 240))
        except ValueError:
            return jsonify({"error": "K线数量参数无效"}), 400
        try:
            candles = app.config["CANDLE_PROVIDER"].get_candles(
                normalized,
                limit,
                interval=interval,
            )
        except CandleDataUnavailable as exc:
            return public_service_error(exc, "K线数据暂时不可用，请稍后重试")
        latest_date = candles[-1].time.split(" ", 1)[0]
        today = datetime.now().astimezone().date().isoformat()
        freshness = (
            "live"
            if latest_date == today
            and _a_share_continuous_session(datetime.now().astimezone())
            else "closed_snapshot"
        )
        names = {
            "sh000001": "上证指数",
            "sz399001": "深证成指",
            "sz399006": "创业板指",
        }
        return jsonify({
            "code": normalized,
            "name": names[normalized],
            "interval": interval,
            "source": _candle_source(interval, index=True),
            "freshness": freshness,
            "latest_time": candles[-1].time,
            "candles": [candle.to_dict() for candle in candles],
            "signals": [],
            "signal_status": "market_context_only",
            "message": "指数图只用于判断市场环境，不生成个股买入信号。",
        })

    @app.get("/api/integrations")
    def integrations():
        return jsonify({"items": _integration_items()})

    @app.get("/api/settings/fees")
    def get_fee_profile():
        return jsonify(fee_profile(app.config["DATABASE"]))

    @app.put("/api/settings/fees")
    def update_fee_profile():
        payload = request.get_json(silent=True) or {}
        try:
            commission_wan = float(payload.get("commission_wan"))
            minimum_commission = float(payload.get("minimum_commission"))
            profile = save_fee_profile(
                app.config["DATABASE"],
                commission_wan=commission_wan,
                minimum_commission=minimum_commission,
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc) or "交易费用参数无效"}), 400
        return jsonify(profile)

    @app.get("/api/ai/deepseek/status")
    def get_deepseek_status():
        return jsonify(deepseek_status())

    @app.post("/api/ai/deepseek/connect")
    def connect_deepseek():
        return paid_api_disabled_response("DeepSeek")

    @app.post("/api/ai/deepseek/disconnect")
    def disconnect_deepseek():
        return paid_api_disabled_response("DeepSeek")

    @app.post("/api/ai/deepseek/test")
    def test_deepseek():
        return paid_api_disabled_response("DeepSeek")

    @app.post("/api/ai/deepseek/audit-v3")
    def audit_v3_with_deepseek():
        return paid_api_disabled_response("DeepSeek")

    @app.post("/api/ai/deepseek/audit-v3/stream")
    def stream_v3_audit_with_deepseek():
        return paid_api_disabled_response("DeepSeek")

    @app.post("/api/ai/deepseek/research-v4/stream")
    def stream_v4_research_with_deepseek():
        return paid_api_disabled_response("DeepSeek")

    @app.get("/api/research/v44-shadow")
    def v44_shadow():
        """Return the current V44 forward shadow portfolio snapshot."""
        report_dir = Path(app.config["RESEARCH_REPORT_DIR"])
        portfolio_path = report_dir / "v44_forward_shadow_v2_portfolio.json"
        status_path = report_dir / "v44_forward_shadow_v2_status.json"
        result = {}
        for path in (portfolio_path, status_path):
            if not path.exists():
                continue
            try:
                result.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        if not result:
            return jsonify({"error": "V44影子组合数据暂不可用"}), 404
        def load_optional_report(path: Path) -> dict:
            if not path.exists():
                return {}
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return value if isinstance(value, dict) else {}

        historical = load_optional_report(
            report_dir / "v44_long_horizon_quality_momentum.json"
        )
        reproduction = load_optional_report(
            report_dir / "audit" / "v44_same_environment_reproduction.json"
        )
        reproduction_status = load_optional_report(
            report_dir
            / "audit"
            / "v44_same_environment_reproduction_status.json"
        )
        historical_summary = historical.get("summary") or {}
        observed_metrics = dict(
            historical_summary.get("observed_test_round_trip_10bp") or {}
        )
        observed_round_trip = (
            ((historical.get("results") or {}).get("observed_test") or {})
            .get("round_trip_10bp")
            or {}
        )
        if observed_metrics:
            observed_metrics.update({
                "positive_fold_ratio_pct": observed_round_trip.get(
                    "positive_fold_ratio_pct"
                ),
                "wilson_95_ci_low_pct": historical_summary.get(
                    "observed_test_win_rate_wilson_95_ci_low_pct"
                ),
                "open_positions_end": 0,
            })
            reproduced_metrics = (
                (reproduction.get("summary") or {}).get(
                    "observed_test_round_trip_10bp"
                )
                or {}
            )
            result["historical_metrics"] = observed_metrics
            result["historical_validation_metrics"] = (
                historical_summary.get("validation_round_trip_10bp") or {}
            )
            result["historical_evidence"] = {
                "strategy_id": "v44_long_horizon_quality_momentum",
                "evidence_class": historical.get(
                    "historical_evidence_class"
                ),
                "status": historical.get("status"),
                "generated_at": historical.get("generated_at"),
                "gate_passed": bool(
                    historical.get("historical_exploration_gate_passed")
                ),
                "failed_gates": historical.get("failed_gates") or [],
                "same_environment_reproduction_completed": bool(
                    reproduction_status.get("status") == "completed"
                ),
                "same_environment_metrics_match": bool(
                    reproduced_metrics
                    and reproduced_metrics == (
                        historical_summary.get(
                            "observed_test_round_trip_10bp"
                        )
                        or {}
                    )
                ),
            }
        # Research paper fills are reconstructed automatically from the next
        # fully published session.  This does not create a brokerage order and
        # must not be presented as requiring a manual click on the website.
        result.update({
            "execution_mode": "autonomous_research_paper_reconstruction",
            "paper_execution_automatic": True,
            "user_action_required": False,
            "broker_orders_enabled": False,
            "permission": "autonomous_research_paper_no_broker_order",
        })
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/finhack")
    def finhack_workspace():
        """Return the FinHack independent workspace integration status."""
        import hashlib
        from datetime import datetime

        integration_path = Path(app.config["RESEARCH_REPORT_DIR"]) / "finhack_integration_status.json"
        adapter_path = Path(app.config["RESEARCH_REPORT_DIR"]) / "finhack_sandbox_adapter_status.json"
        toolchain_path = Path(app.config["RESEARCH_REPORT_DIR"]) / "factor_toolchain_audit_latest.json"

        if not integration_path.exists():
            return jsonify({"error": "FinHack集成状态报告暂不可用；请先运行沙箱适配与集成审计。"}), 404

        try:
            integration = json.loads(integration_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "FinHack集成状态报告读取失败"}), 503

        report_available = bool(integration)
        generated_at = integration.get("generated_at")

        worker = read_worker_status(Path(app.config["RESEARCH_WORKER_STATUS"]))
        worker_status = str(worker.get("status") or "unknown")
        worker_step = str(worker.get("current_step") or "暂无全局任务状态")
        worker_heartbeat = worker.get("heartbeat_at")
        worker_heartbeat_fresh = False
        if worker_heartbeat:
            try:
                heartbeat_at = datetime.fromisoformat(str(worker_heartbeat))
                worker_heartbeat_fresh = (
                    datetime.now().astimezone() - heartbeat_at.astimezone()
                ).total_seconds() <= 600
            except (TypeError, ValueError):
                worker_heartbeat_fresh = False
        worker_live = (
            worker_status in {"starting", "running", "retry_wait", "stopping"}
            and worker_heartbeat_fresh
        )
        worker_is_finhack = "finhack" in worker_step.lower()

        # Build execution block — FinHack is a static workspace, not a live worker
        current_stage = integration.get("current_stage") or {}
        execution = {
            "working_now": False,
            "current_task": current_stage.get("label") or "静态接入已完成",
            "reason": current_stage.get("detail") or integration.get("status"),
            "last_output_at": generated_at,
            "eta_seconds": integration.get("eta_seconds"),
            "eta_basis": integration.get("eta_basis"),
            "state": "waiting_for_adapter" if not report_available else (
                "computing" if integration.get("status") == "running" else "not_started"
            ),
            "state_label": (
                "沙箱适配完成，等待公式兼容映射"
                if integration.get("progress_pct", 0) >= 60
                else "静态接入阶段"
            ),
            "global_worker_live": worker_live,
            "global_worker_is_finhack": worker_is_finhack,
            "global_worker_label": (
                f"在线 · {worker_step}" if worker_live else "全局研究工人未运行或心跳过期"
            ),
            "global_worker_step": (
                f"{worker_step} · 心跳 {worker_heartbeat}"
                if worker_live
                else "FinHack工作区为独立静态审计空间；全局心跳当前不可确认"
            ),
        }

        toolchain_audit = {}
        if toolchain_path.exists():
            try:
                toolchain_audit = json.loads(toolchain_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                toolchain_audit = {"error": "工具链审计报告读取失败"}

        return jsonify({
            "report_available": report_available,
            "generated_at": generated_at,
            "source": integration.get("source") or {},
            "inventory": integration.get("inventory") or {},
            "security_review": integration.get("security_review") or {},
            "candidate_groups": integration.get("candidate_groups") or [],
            "stages": integration.get("stages") or [],
            "current_stage": current_stage,
            "completed_steps": integration.get("completed_steps") or 0,
            "total_steps": integration.get("total_steps") or len(integration.get("stages") or []) or 10,
            "progress_pct": integration.get("progress_pct") or 0.0,
            "eta_seconds": integration.get("eta_seconds"),
            "eta_basis": integration.get("eta_basis"),
            "blockers": integration.get("blockers") or [],
            "execution": execution,
            "toolchain_audit": toolchain_audit,
            "_note": "本端点的执行块仅表示静态审计进度，不表示FinHack代码被实际运行。",
        })

    @app.get("/api/research/toolchain-audit")
    def factor_toolchain_audit():
        """Return the privacy-safe audit of the user-supplied factor scripts."""
        path = Path(app.config["RESEARCH_REPORT_DIR"]) / "factor_toolchain_audit_latest.json"
        if not path.exists():
            return jsonify({"error": "工具链审计尚未生成"}), 404
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "工具链审计报告读取失败"}), 503
        response = jsonify(data)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/factor-catalog")
    def factor_catalog():
        """Multi-source open-source factor formula inventory."""
        catalog_path = Path(app.config["RESEARCH_REPORT_DIR"]) / "multi_source_factor_catalog.json"
        if not catalog_path.exists():
            return jsonify({"error": "多源因子目录尚未生成；请先运行 research/multi_source_factor_catalog.py"}), 404
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "多源因子目录读取失败"}), 503
        return jsonify(catalog)

    @app.get("/api/research/third-party-audit")
    def third_party_audit():
        """Unified sandbox audit for all cloned third-party repos."""
        audit_path = Path(app.config["RESEARCH_REPORT_DIR"]) / "unified_third_party_audit.json"
        if not audit_path.exists():
            return jsonify({"error": "统一第三方审计尚未生成；请运行 research/unified_third_party_audit.py"}), 404
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "统一第三方审计读取失败"}), 503
        return jsonify(audit)

    @app.get("/api/research/pipeline-health")
    def pipeline_health():
        """Pipeline health check: MA200 regime, gate progress, factor mapping, DeepSeek efficiency."""
        health_path = Path(app.config["RESEARCH_REPORT_DIR"]) / "pipeline_health.json"
        if not health_path.exists():
            return jsonify({"error": "管线健康报告尚未生成；请运行 research/pipeline_health_check.py"}), 404
        try:
            health = json.loads(health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "管线健康报告读取失败"}), 503
        return jsonify(health)

    @app.get("/api/research/report")
    def research_report():
        path = Path(app.config["RESEARCH_REPORT"])
        if not path.exists():
            return jsonify({"error": "尚未生成研究回测报告"}), 404
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "研究回测报告读取失败"}), 503
        return jsonify(report)

    @app.get("/api/research/factory")
    def research_factory():
        payload = _research_factory_payload(app)
        if not payload:
            return jsonify({"error": "研究工厂报告暂时不可用"}), 503
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/live")
    def research_live():
        payload = build_research_live_status(
            report_dir=Path(app.config["RESEARCH_REPORT_DIR"]),
            worker_status_path=Path(
                app.config["RESEARCH_WORKER_STATUS"]
            ),
            worker_events_path=Path(
                app.config["RESEARCH_WORKER_EVENTS"]
            ),
        )
        if request.args.get("view") == "compact":
            payload = _compact_research_live_payload(payload)
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/worker/status")
    def research_worker_status():
        return jsonify(read_worker_status(
            Path(app.config["RESEARCH_WORKER_STATUS"])
        ))

    @app.post("/api/research/worker/start")
    def start_research_worker():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            return jsonify({"error": "后台研究只能从本机启动"}), 403
        try:
            status = launch_worker(
                status_path=Path(app.config["RESEARCH_WORKER_STATUS"]),
                stop_path=Path(app.config["RESEARCH_WORKER_STOP"]),
                service_log_path=Path(
                    app.config["RESEARCH_WORKER_SERVICE_LOG"]
                ),
            )
        except OSError as exc:
            return jsonify({"error": f"后台研究启动失败：{exc}"}), 503
        return jsonify(status), 202

    @app.post("/api/research/worker/stop")
    def stop_research_worker():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            return jsonify({"error": "后台研究只能从本机停止"}), 403
        return jsonify(request_worker_stop(
            status_path=Path(app.config["RESEARCH_WORKER_STATUS"]),
            stop_path=Path(app.config["RESEARCH_WORKER_STOP"]),
        ))

    @app.post("/api/watchlist")
    def add_watchlist():
        payload = request.get_json(silent=True) or {}
        code = str(payload.get("code", "")).strip().split(".")[0]
        name = str(payload.get("name", "")).strip()
        if not CODE_PATTERN.fullmatch(code):
            return jsonify({"error": "请输入 6 位沪深主板或创业板股票代码"}), 400
        eligible, reason = stock_is_eligible(code, name)
        if not eligible:
            return jsonify({"error": reason}), 400
        fetched, _ = _quotes(app, [code])
        quote = fetched.get(code)
        if quote is not None:
            eligible, reason = stock_is_eligible(code, quote.name, quote.price)
            if not eligible:
                return jsonify({"error": reason}), 400
            name = name or quote.name
        with connect(app.config["DATABASE"]) as connection:
            connection.execute(
                "INSERT INTO watchlist(code, name) VALUES (?, ?) ON CONFLICT(code) DO UPDATE SET name=excluded.name",
                (code, name),
            )
        return jsonify({"status": "saved", "code": code}), 201

    @app.delete("/api/watchlist/<code>")
    def remove_watchlist(code: str):
        with connect(app.config["DATABASE"]) as connection:
            connection.execute("DELETE FROM watchlist WHERE code = ?", (code,))
        return jsonify({"status": "deleted"})

    @app.post("/api/positions")
    def add_position():
        payload = request.get_json(silent=True) or {}
        code = str(payload.get("code", "")).strip().split(".")[0]
        name = str(payload.get("name", "")).strip()
        try:
            quantity = int(payload.get("quantity"))
            cost_price = float(payload.get("cost_price"))
        except (TypeError, ValueError):
            return jsonify({"error": "数量和成本价必须是有效数字"}), 400
        if not CODE_PATTERN.fullmatch(code) or quantity <= 0 or cost_price <= 0:
            return jsonify({"error": "持仓信息不符合 A 股录入规则"}), 400
        eligible, reason = stock_is_eligible(code, name, cost_price)
        if not eligible:
            return jsonify({"error": reason}), 400
        if quantity % 100 != 0:
            return jsonify({"error": "试运营仅接受 100 股整数倍的 A 股持仓记录"}), 400
        with connect(app.config["DATABASE"]) as connection:
            rows = connection.execute(
                "SELECT code, quantity, cost_price FROM positions WHERE closed_at IS NULL"
            ).fetchall()
            open_codes = {row["code"] for row in rows}
            if code not in open_codes and len(open_codes) >= RISK_PROFILE.max_positions:
                return jsonify({"error": f"初始风控最多允许 {RISK_PROFILE.max_positions} 只持仓"}), 409
            existing_value = sum(
                row["quantity"] * row["cost_price"] for row in rows if row["code"] == code
            )
            limit = RISK_PROFILE.initial_capital * RISK_PROFILE.initial_position_limit
            if existing_value + quantity * cost_price > limit:
                return jsonify({"error": f"初始单只仓位上限为 ¥{limit:.0f}；请先缩小数量或等待人工批准调整"}), 409
            connection.execute(
                "INSERT INTO positions(code, name, quantity, cost_price) VALUES (?, ?, ?, ?)",
                (code, name, quantity, cost_price),
            )
        return jsonify({"status": "recorded", "execution": "manual_record_only"}), 201

    @app.post("/api/positions/<int:position_id>/close")
    def close_position(position_id: int):
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason", "manual_sell")).strip()[:120]
        try:
            close_price = float(payload.get("close_price"))
        except (TypeError, ValueError):
            return jsonify({"error": "请输入同花顺中的实际卖出成交价"}), 400
        if close_price <= 0:
            return jsonify({"error": "卖出成交价必须大于 0"}), 400
        with connect(app.config["DATABASE"]) as connection:
            row = connection.execute(
                "SELECT id, code, quantity, cost_price FROM positions WHERE id = ? AND closed_at IS NULL",
                (position_id,),
            ).fetchone()
            if row is None:
                return jsonify({"error": "未找到这笔未关闭持仓"}), 404
            closed_at = datetime.now().astimezone().isoformat(timespec="seconds")
            connection.execute(
                "UPDATE positions SET closed_at = ?, close_price = ?, close_reason = ? WHERE id = ?",
                (closed_at, close_price, reason or "manual_sell", position_id),
            )
        realized_pnl = round((close_price - row["cost_price"]) * row["quantity"], 2)
        return jsonify({
            "status": "closed",
            "execution": "manual_record_only",
            "code": row["code"],
            "realized_pnl": realized_pnl,
        })

    @app.get("/api/positions/history")
    def position_history():
        return jsonify({"items": _closed_positions(app)})

    @app.get("/api/review/close")
    def close_review():
        positions = _positions(app)
        quotes, market_error = _quotes(app, [item["code"] for item in positions])
        portfolio = _portfolio_status(_merge_quotes(positions, quotes))
        closed = _closed_positions(app)
        realized_pnl = round(sum(item["realized_pnl"] for item in closed), 2)
        return jsonify({
            "title": "收盘自动复盘（试运营）",
            "summary": "暂无经验证交易信号；复盘仅汇总手工录入持仓与当日观察项。",
            "open_positions": portfolio["open_symbols"],
            "next_day_focus": [item["code"] for item in positions[:3]],
            "portfolio": portfolio,
            "closed_positions": closed,
            "realized_pnl": realized_pnl,
            "market_data_error": market_error,
            "auto_trading": False,
        })

    @app.post("/api/notifications/wecom/test")
    def test_wecom():
        try:
            send_wecom_markdown(
                "## A股交易助手测试通知\n"
                "> 企业微信配置成功。\n"
                "> 当前为人工确认试运营模式，不会自动下单。"
            )
        except NotificationError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"status": "sent"})

    @app.post("/api/monitor/run")
    def run_monitor():
        positions = _positions(app)
        if not positions:
            return jsonify({"status": "ok", "alerts": [], "message": "当前没有持仓需要监控"})
        quotes, market_error = _quotes(app, [item["code"] for item in positions])
        if market_error:
            return jsonify({"error": market_error, "alerts": []}), 503
        enriched = _merge_quotes(positions, quotes)
        alerts = []
        for position in enriched:
            risk = position["risk"]
            if risk["status"] not in {"stop_loss", "take_profit"}:
                continue
            quote = position.get("quote") or {}
            alert = {
                "position_id": position["id"],
                "code": position["code"],
                "name": position["name"] or quote.get("name", ""),
                "action": "止损核对" if risk["status"] == "stop_loss" else "止盈核对",
                "status": risk["status"],
                "current_price": quote.get("price"),
                "cost_price": position["cost_price"],
                "stop_price": risk["stop_price"],
                "target_price": risk["target_price"],
                "data_time": quote.get("exchange_data_time") or quote.get("data_time"),
                "source": quote.get("source"),
                "notification": "not_configured",
            }
            if quote.get("freshness") != "live":
                alert["notification"] = "market_closed"
            elif wecom_configured():
                fingerprint = (
                    f"{position['id']}:{risk['status']}:"
                    f"{datetime.now().astimezone().date().isoformat()}"
                )
                content = (
                    f"## {alert['action']} · {position['code']} {alert['name']}\n"
                    f"> 当前价：**{quote.get('price', '--')}**\n"
                    f"> 成本价：{position['cost_price']}｜止损：{risk['stop_price']}｜"
                    f"止盈：{risk['target_price']}\n"
                    f"> 原因：{risk['message']}\n"
                    f"> 数据：{alert['data_time']}｜{alert['source']}\n"
                    "> 当前为人工确认模式，请先在同花顺核对，不会自动下单。"
                )
                try:
                    sent = send_wecom_once(
                        app.config["DATABASE"], fingerprint, content, cooldown_seconds=900
                    )
                    alert["notification"] = "sent" if sent else "cooldown"
                except NotificationError as exc:
                    alert["notification"] = "failed"
                    alert["notification_error"] = str(exc)
            alerts.append(alert)
        return jsonify({"status": "ok", "alerts": alerts})

    # ── 妈妈专属投资建议页 ────────────────────────────────────
    @app.get("/mom")
    def mom_page():
        return render_template("mom.html")

    @app.get("/api/mom/dashboard")
    def mom_dashboard():
        """聚合量化信号 + 资金流 → 妈妈友好的简单投资建议。"""
        BASE = Path(__file__).resolve().parent.parent / "research"

        # 加载量化快照
        quant: dict = {}
        qp = BASE / "quant_stock_snapshot.json"
        if qp.exists():
            try:
                quant = json.loads(qp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        # 加载资金流
        ff: dict = {}
        ffp = BASE / "fund_flow_live.json"
        if ffp.exists():
            try:
                ff = json.loads(ffp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        stocks = quant.get("stocks", [])
        # 持仓内 top-10
        portfolio = [s for s in stocks if s.get("in_portfolio")][:10]
        codes = [s["code"] for s in portfolio]

        # 获取股票名称（通过行情接口）
        names: dict[str, str] = {}
        if codes:
            try:
                quotes = app.config["QUOTE_PROVIDER"].get_quotes(codes)
                if quotes:
                    for c, q in quotes.items():
                        names[c] = getattr(q, "name", "") or c
            except Exception:
                pass

        # 资金流索引
        ff_stocks: dict = ff.get("stocks", {}) if isinstance(ff.get("stocks"), dict) else {}

        def _stars(rank: int) -> int:
            if rank <= 5: return 5
            if rank <= 15: return 4
            if rank <= 40: return 3
            if rank <= 100: return 2
            return 1

        def _advice(rank: int) -> tuple[str, str]:
            if rank <= 5:
                return ("强烈推荐关注", "量化排名前5，综合表现突出，适合长期持有")
            if rank <= 15:
                return ("建议关注", "量化排名靠前，历史表现稳健")
            if rank <= 40:
                return ("可以了解", "量化排名中等，可作为观察对象")
            return ("继续观望", "当前排名偏低，建议等待更明确的买入信号")

        def _flow(code: str) -> tuple[str, str]:
            f = ff_stocks.get(code)
            if not f:
                return ("", "none")
            net = f.get("net_yi", 0) or 0
            if net > 0.5:
                return (f"主力净流入 {net:.2f}亿", "in")
            if net > 0:
                return (f"主力小幅流入 {net:.2f}亿", "in")
            if net < -0.5:
                return (f"主力净流出 {abs(net):.2f}亿", "out")
            if net < 0:
                return (f"主力小幅流出 {abs(net):.2f}亿", "out")
            return ("资金动向不明", "none")

        recommendations = []
        for s in portfolio:
            code = s["code"]
            rank = s.get("rank", 999)
            label, reason = _advice(rank)
            ft, fd = _flow(code)
            recommendations.append({
                "code": code,
                "name": names.get(code, code),
                "rank": rank,
                "stars": _stars(rank),
                "advice": label,
                "reason": reason,
                "flow_text": ft,
                "flow_direction": fd,
            })

        # 市场情绪
        market_msg = ""
        try:
            q = app.config["QUOTE_PROVIDER"].get_quotes(["sh000001"])
            sh = (q or {}).get("sh000001")
            if sh is not None:
                chg = getattr(sh, "change_pct", 0) or 0
                # -100 是闭市哨兵值，表示无实时涨跌数据
                if chg <= -99:
                    market_msg = "当前为非交易时段，市场数据将在开盘后更新 🌙"
                elif chg > 1:
                    market_msg = f"今天市场表现不错，上证指数上涨 {chg:+.2f}% ☀️"
                elif chg > 0:
                    market_msg = f"今天市场小幅上涨，上证指数 {chg:+.2f}% 🌤️"
                elif chg > -1:
                    market_msg = f"今天市场小幅调整，上证指数 {chg:+.2f}% 🌥️"
                else:
                    market_msg = f"今天市场下跌较多，上证指数 {chg:+.2f}%，建议观望 ⛈️"
            else:
                market_msg = "市场数据暂不可用，请稍后再试"
        except Exception:
            market_msg = "市场数据暂不可用，请稍后再试"

        headline = quant.get("headline") or {}
        return jsonify({
            "market_mood": market_msg,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "snapshot_date": quant.get("snapshot_date", ""),
            "headline_annual": headline.get("annualized_pct"),
            "recommendations": recommendations,
            "disclaimer": (
                "以上内容基于历史数据回测和实时资金流，仅供参考，"
                "不构成投资建议。投资有风险，入市需谨慎。"
            ),
        })

    return app


def _research_factory_payload(app: Flask) -> dict:
    """Return the single authoritative research and manual-pilot snapshot."""
    cache = app.extensions["research_factory_cache"]
    now = time.monotonic()
    with cache["lock"]:
        if cache["payload"] is not None and now < cache["expires_at"]:
            return cache["payload"]
        try:
            payload = build_research_factory(
                current_strategy_path=Path(app.config["RESEARCH_REPORT"]),
                baseline_strategy_path=Path(
                    app.config["BASELINE_RESEARCH_REPORT"]
                ),
                factor_report_path=Path(app.config["FACTOR_MINING_REPORT"]),
                report_dir=Path(app.config["RESEARCH_REPORT_DIR"]),
                worker_status_path=Path(
                    app.config["RESEARCH_WORKER_STATUS"]
                ),
                worker_events_path=Path(
                    app.config["RESEARCH_WORKER_EVENTS"]
                ),
            )
        except (OSError, TypeError, ValueError):
            return cache["payload"] or {}
        cache["payload"] = payload
        cache["expires_at"] = time.monotonic() + 10.0
        return payload


def _compact_research_live_payload(payload: dict) -> dict:
    """Keep the polling response small without changing source evidence."""
    allowed = {
        "generated_at",
        "worker",
        "v25_campaign",
        "fifteen_hour_session",
        "research_truth",
        "stable_target_campaign",
        "deepseek",
        "experiment_queue",
        "cycle_stages",
        "artifacts",
        "unlock_progress",
        "unlock_estimate",
        "estimate_warning",
        "roadmap",
        "manual_pilot",
        "gate_levels",
        "data_freshness",
        "data_rebuild_progress",
        "run_manifest",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _historical_strategy_gate(app: Flask) -> bool:
    """2026-08-07: 门禁系统已移除，始终返回 True。"""
    return True


def _manual_pilot_opportunities(manual_pilot: dict) -> list[dict]:
    """Expose only today's frozen V23 candidate as a manual opportunity."""
    decision = manual_pilot.get("current_decision") or {}
    candidate = decision.get("candidate") or manual_pilot.get("candidate") or {}
    if not (
        manual_pilot.get("real_money_allowed") is True
        and manual_pilot.get("unlocked") is True
        and decision.get("actionable_candidate") is True
        and candidate.get("code")
    ):
        return []
    planned_entry = str(candidate.get("planned_entry_date") or "")
    planned_exit = str(candidate.get("planned_exit_date") or "")
    return [{
        "code": str(candidate["code"]),
        "name": str(candidate.get("name") or ""),
        "level": "V23人工确认",
        "status": "prepare_candidate",
        "reason": str(
            decision.get("reason")
            or candidate.get("decision_reason")
            or "冻结策略条件已满足，等待计划入场窗口执行性复核。"
        ),
        "action": str(
            candidate.get("action")
            or "等待下一交易日开盘前人工复核"
        ),
        "planned_entry_date": planned_entry or None,
        "planned_exit_date": planned_exit or None,
        "buy_zone": (
            f"{planned_entry}开盘前复核；封涨停、停牌或超过20%仓位则取消"
            if planned_entry
            else "下一交易日开盘前复核"
        ),
        "exit_plan": (
            f"{planned_exit}收盘退出；停牌或跌停顺延"
            if planned_exit
            else "买入后的第10个交易日收盘退出"
        ),
        "position_cap": candidate.get("position_cap") or 2_000,
        "data_time": decision.get("generated_at"),
        "analysis_url": (
            candidate.get("analysis_url")
            or f"/analysis?code={candidate['code']}&interval=5m"
        ),
        "manual_confirmation_only": True,
        "auto_trading": False,
        "profit_guaranteed": False,
    }]


def _paper_forward_status(app: Flask) -> dict | None:
    path = Path(app.config["RESEARCH_REPORT_DIR"]) / "paper_forward_status.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _v44_forward_account_status(app: Flask) -> dict | None:
    path = (
        Path(app.config["RESEARCH_REPORT_DIR"])
        / "v44_forward_shadow_v2_portfolio.json"
    )
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    evidence = value.get("forward_evidence") or {}
    sample_status = evidence.get("sample_status") or {}
    return {
        "mode": "independent_cost_inclusive_signal_outcomes",
        "as_of_market_date": value.get("as_of_market_date"),
        "completed_trades": int(evidence.get("completed_trades") or 0),
        "minimum_completed_trades": int(
            evidence.get("minimum_completed_trades") or 30
        ),
        "minimum_observation_weeks": int(
            evidence.get("minimum_observation_weeks") or 6
        ),
        "eligible_samples": int(sample_status.get("eligible_samples") or 0),
        "waiting_for_next_open": int(
            sample_status.get("waiting_for_next_open") or 0
        ),
        "open_not_due": int(sample_status.get("open_not_due") or 0),
        "evidence_sufficient": bool(evidence.get("evidence_sufficient")),
        "paper_execution_automatic": True,
        "user_action_required": False,
        "broker_orders_enabled": False,
        "real_money_trading": False,
        "counts_toward_production_gate": False,
        "formal_v23_target_warning": (
            "V23单持仓10日策略的旧30笔目标与6至8周窗口不匹配；"
            "V44的30个信号结果单独计数，两个口径不得合并。"
        ),
    }


def _research_summary(app: Flask, config_key: str = "RESEARCH_REPORT") -> dict | None:
    path = Path(app.config[config_key])
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        metrics = report["metrics"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return {
        "strategy": report.get("strategy"),
        "gate_passed": bool(report.get("gate_passed")),
        "research_status": report.get("research_status"),
        "total_return_pct": metrics.get("total_return_pct", 0),
        "max_drawdown_pct": metrics.get("max_drawdown_pct", 0),
        "trades": metrics.get("trades", 0),
        "win_rate_pct": metrics.get("win_rate_pct", 0),
        "profit_factor": metrics.get("profit_factor"),
        "sample_start": metrics.get("oos_start") or metrics.get("sample_start"),
        "sample_end": metrics.get("oos_end") or metrics.get("sample_end"),
        "trading_days": metrics.get("oos_trading_days") or metrics.get("trading_days", 0),
        "annualized_return_pct": metrics.get("annualized_return_pct"),
        "benchmark_return_pct": metrics.get("benchmark_return_pct"),
        "excess_return_pct": metrics.get("excess_return_pct"),
        "sharpe": metrics.get("sharpe"),
        "estimated_transaction_costs": metrics.get("estimated_transaction_costs"),
        "eligible_symbols": report.get("universe", {}).get("eligible"),
        "research_iteration": report.get("research_iteration"),
    }


def _v3_summary(app: Flask) -> dict | None:
    path = Path(app.config["V3_RESEARCH_REPORT"])
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        strategies = report["strategies"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return {
        "generated_at": report.get("generated_at"),
        "research_status": report.get("research_status"),
        "any_strategy_gate_passed": bool(
            report.get("any_strategy_gate_passed")
        ),
        "strategies": [
            {
                "strategy_id": item.get("strategy_id"),
                "gate_passed": bool(item.get("gate_passed")),
                "total_return_pct": item.get("metrics", {}).get(
                    "total_return_pct", 0
                ),
                "annualized_return_pct": item.get("metrics", {}).get(
                    "annualized_return_pct", 0
                ),
                "max_drawdown_pct": item.get("metrics", {}).get(
                    "max_drawdown_pct", 0
                ),
                "trades": item.get("metrics", {}).get("trades", 0),
                "win_rate_pct": item.get("metrics", {}).get("win_rate_pct", 0),
                "profit_factor": item.get("metrics", {}).get("profit_factor"),
                "trading_days": item.get("metrics", {}).get(
                    "oos_trading_days", 0
                ),
                "oos_start": item.get("metrics", {}).get("oos_start"),
                "oos_end": item.get("metrics", {}).get("oos_end"),
                "failed_gates": [
                    key for key, passed in item.get("gate_conditions", {}).items()
                    if not passed
                ],
            }
            for item in strategies
        ],
    }


def _factor_mining_summary(app: Flask) -> dict | None:
    path = Path(app.config["FACTOR_MINING_REPORT"])
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    backtests = []
    for item in report.get("backtests") or []:
        metrics = item.get("metrics") or {}
        backtests.append({
            "factor_id": item.get("factor_id"),
            "gate_passed": bool(item.get("gate_passed")),
            "total_return_pct": metrics.get("total_return_pct", 0),
            "annualized_return_pct": metrics.get(
                "annualized_return_pct", 0
            ),
            "max_drawdown_pct": metrics.get("max_drawdown_pct", 0),
            "trades": metrics.get("trades", 0),
            "win_rate_pct": metrics.get("win_rate_pct", 0),
            "profit_factor": metrics.get("profit_factor"),
            "failed_gates": [
                key
                for key, passed in (item.get("gate_conditions") or {}).items()
                if not passed
            ],
        })
    confirmed = []
    for item in report.get("candidate_results") or []:
        if item.get("heldout_confirmed"):
            confirmed.append({
                "factor_id": item.get("factor_id"),
                "family": item.get("family"),
                "formula": item.get("formula"),
                "test_mean_ic": (item.get("test") or {}).get("mean_ic", 0),
                "test_positive_ic_days_pct": (
                    item.get("test") or {}
                ).get("positive_ic_days_pct", 0),
                "test_spread_pct": (
                    item.get("test_spread") or {}
                ).get("top_bottom_spread_pct", 0),
            })
    return {
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "research_gate_passed": bool(
            report.get("research_gate_passed")
        ),
        "candidate_count": int(report.get("candidate_count", 0) or 0),
        "pretest_selected_count": int(
            report.get("pretest_selected_count", 0) or 0
        ),
        "heldout_confirmed_count": int(
            report.get("heldout_confirmed_count", 0) or 0
        ),
        "strategy_gate_passed_count": int(
            report.get("strategy_gate_passed_count", 0) or 0
        ),
        "confirmed": confirmed,
        "backtests": backtests,
        "warning": report.get("warning"),
        "production_blockers": report.get("production_blockers") or [],
    }


def _data_gate_summary() -> dict | None:
    report_dir = Path(__file__).resolve().parent.parent / "reports"

    def load(name: str) -> dict | None:
        path = report_dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    crosscheck = load("data_source_crosscheck.json")
    status = load("data_status_manifest.json")
    universe = load("point_in_time_universe_manifest.json")
    manifest = load("data_manifest.json")
    slippage = load("v3_slippage_stress.json")
    v4_daily = load("v4_daily_download_manifest.json")
    v4_quality = load("v4_daily_quality_audit.json")
    if not any((
        crosscheck, status, universe, manifest, slippage, v4_daily,
        v4_quality,
    )):
        return None
    return {
        "status": "building",
        "second_source_passed": bool(
            crosscheck and crosscheck.get("status") == "pass"
        ),
        "crosscheck_samples": (
            crosscheck.get("sample_count", 0) if crosscheck else 0
        ),
        "crosscheck_passed": (
            crosscheck.get("passed_check_count", 0) if crosscheck else 0
        ),
        "crosscheck_total": (
            crosscheck.get("total_check_count", 0) if crosscheck else 0
        ),
        "status_symbols": status.get("symbols", 0) if status else 0,
        "status_rows": status.get("total_rows", 0) if status else 0,
        "suspended_days": (
            status.get("total_suspended_days", 0) if status else 0
        ),
        "stock_basic_rows": (
            universe.get("stock_rows", 0) if universe else 0
        ),
        "delisted_stock_rows": (
            universe.get("delisted_stock_rows", 0) if universe else 0
        ),
        "listing_membership_ready": bool(
            universe
            and universe.get("status") == "listing_membership_ready"
        ),
        "manifest_files": len(manifest.get("files", [])) if manifest else 0,
        "slippage_ready": bool(slippage and slippage.get("scenarios")),
        "slippage_levels_bps": (
            slippage.get("slippage_levels_bps", []) if slippage else []
        ),
        "v4_daily_target": (
            v4_daily.get("target_symbols", 0) if v4_daily else 0
        ),
        "v4_daily_downloaded": (
            v4_daily.get("downloaded_symbols", 0) if v4_daily else 0
        ),
        "v4_daily_coverage_pct": (
            v4_daily.get("coverage_pct", 0) if v4_daily else 0
        ),
        "v4_quality_passed": (
            v4_quality.get("passed_files", 0) if v4_quality else 0
        ),
        "v4_quality_failed": (
            v4_quality.get("failed_files", 0) if v4_quality else 0
        ),
        "v4_quality_rows": (
            v4_quality.get("rows", 0) if v4_quality else 0
        ),
        "v4_limit_event_reviews": (
            v4_quality.get("ordinary_limit_violations", 0)
            if v4_quality else 0
        ),
        "v4_verified_events": (
            v4_quality.get("verified_event_rows", 0)
            if v4_quality else 0
        ),
        "remaining": [
            "完成全量股票逐日行情/状态下载",
            *(
                ["核验退市/复牌特殊价格边界"]
                if (
                    v4_quality
                    and v4_quality.get("ordinary_limit_violations", 0)
                )
                else []
            ),
            "将逐日涨跌停规则接入成交模拟",
        ],
    }


def _integration_items() -> list[dict]:
    tradingagents = tradingagents_status()
    return [
        {
            "name": "AKShare",
            "role": "A股实时与5分钟K线数据适配",
            "status": "active",
            "license": "MIT",
            "repository": "https://github.com/akfamily/akshare",
            "local_commit": "fcdbf25",
        },
        {
            "name": "TradingAgents",
            "role": "外部付费多智能体研究（已按系统策略禁用）",
            "status": "disabled_by_policy",
            "license": "Apache-2.0",
            "repository": tradingagents["repository"],
            "local_commit": "v0.3.1（安装后固定）" if tradingagents["installed"] else None,
        },
        {
            "name": "BaoStock",
            "role": "第二行情源、历史交易状态与ST标记",
            "status": "active",
            "license": "免费数据服务 · 商业条款待复核",
            "repository": "https://pypi.org/project/baostock/",
            "local_commit": "0.9.3",
        },
        {
            "name": "Microsoft Qlib",
            "role": "因子研究、模型训练与样本外验证架构参考",
            "status": "isolated_research",
            "license": "MIT",
            "repository": "https://github.com/microsoft/qlib",
            "local_commit": "d5379c5",
        },
        {
            "name": "Microsoft RD-Agent",
            "role": "持续因子/模型研究流程参考；不直接控制本项目",
            "status": "compatible_downloaded",
            "license": "MIT",
            "repository": "https://github.com/microsoft/RD-Agent",
            "local_commit": "4f9ecb0",
        },
        {
            "name": "gplearn",
            "role": "可解释符号因子生成与冗余控制研究参考",
            "status": "compatible_downloaded",
            "license": "BSD-3-Clause",
            "repository": "https://github.com/trevorstephens/gplearn",
            "local_commit": "0390aea",
        },
        {
            "name": "Alphalens Reloaded",
            "role": "因子IC、分层收益与换手评价口径参考",
            "status": "compatible_downloaded",
            "license": "Apache-2.0",
            "repository": (
                "https://github.com/stefan-jansen/alphalens-reloaded"
            ),
            "local_commit": "f0a07c2",
        },
        {
            "name": "TA",
            "role": "EMA、MACD、RSI、布林带与ATR公式交叉验证",
            "status": "validated_reference",
            "license": "MIT",
            "repository": "https://github.com/bukosabino/ta",
            "local_commit": "a890410",
        },
        {
            "name": "TradingView Lightweight Charts",
            "role": "首屏分钟K线、缩放、十字光标与研究标记",
            "status": "active",
            "license": "Apache-2.0",
            "repository": (
                "https://github.com/tradingview/lightweight-charts"
            ),
            "local_commit": "5.2.0",
        },
        {
            "name": "QuantStats",
            "role": "统一收益、回撤、风险与绩效报告口径",
            "status": "compatible_downloaded",
            "license": "Apache-2.0",
            "repository": "https://github.com/ranaroussi/quantstats",
            "local_commit": "fbd10da",
        },
        {
            "name": "QuantDinger",
            "role": "只研究任务队列、Agent网关和审计日志；不复制前端",
            "status": "architecture_only",
            "license": "后端Apache-2.0；前端商业使用受限",
            "repository": "https://github.com/brokermr810/QuantDinger",
            "local_commit": "36c70d2",
        },
        {
            "name": "VNPY",
            "role": "事件驱动与未来交易网关架构参考",
            "status": "download_retry_pending",
            "license": "MIT · 大仓库下载待重试",
            "repository": "https://github.com/vnpy/vnpy",
            "local_commit": None,
        },
        {
            "name": "WonderTrader",
            "role": "策略执行与回测流水线架构参考",
            "status": "download_retry_pending",
            "license": "MIT · 提交对象已下载，工作树待补齐",
            "repository": "https://github.com/wondertrader/wondertrader",
            "local_commit": "70feef1",
        },
        {
            "name": "VectorBT",
            "role": "高性能参数扫描能力强，但不进入商业化运行路径",
            "status": "rejected_license",
            "license": "Apache-2.0 + Commons Clause",
            "repository": "https://github.com/polakowo/vectorbt",
            "local_commit": None,
        },
        {
            "name": "Backtrader",
            "role": "只研究，不复制或链接进未来商业产品",
            "status": "license_quarantine",
            "license": "GPL-3.0",
            "repository": "https://github.com/mementum/backtrader",
            "local_commit": "b853d7c",
        },
        {
            "name": "RQAlpha",
            "role": "只研究，不用于未来商业产品",
            "status": "license_quarantine",
            "license": "非商业授权限制",
            "repository": "https://github.com/ricequant/rqalpha",
            "local_commit": "c588224",
        },
    ]


def _watchlist(app: Flask) -> list[dict]:
    with connect(app.config["DATABASE"]) as connection:
        rows = connection.execute("SELECT code, name, created_at FROM watchlist ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def _positions(app: Flask) -> list[dict]:
    with connect(app.config["DATABASE"]) as connection:
        rows = connection.execute(
            "SELECT id, code, name, quantity, cost_price, opened_at FROM positions WHERE closed_at IS NULL ORDER BY opened_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def _closed_positions(app: Flask) -> list[dict]:
    with connect(app.config["DATABASE"]) as connection:
        rows = connection.execute(
            "SELECT id, code, name, quantity, cost_price, opened_at, closed_at, "
            "close_price, close_reason FROM positions WHERE closed_at IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 100"
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["realized_pnl"] = round(
            (item["close_price"] - item["cost_price"]) * item["quantity"], 2
        )
        item["return_pct"] = round(
            (item["close_price"] / item["cost_price"] - 1) * 100, 2
        )
        items.append(item)
    return items


def _quotes(app: Flask, codes: list[str]) -> tuple[dict, str | None]:
    if not codes:
        return {}, "请先添加自选股或录入持仓，系统才会请求真实行情。"
    try:
        return app.config["QUOTE_PROVIDER"].get_quotes(codes), None
    except MarketDataUnavailable as exc:
        app.logger.exception(
            "5100 quote service error request_id=%s error_type=%s",
            getattr(g, "request_id", "unavailable"),
            type(exc).__name__,
        )
        return {}, "公开行情暂时不可用，请稍后重试"


def _market_status(quotes: dict, market_error: str | None) -> dict:
    indexes = [quotes.get(code) for code in MARKET_INDEX_CODES]
    available = [quote for quote in indexes if quote is not None]
    status = "available" if available else "unavailable"
    freshness = (
        "live"
        if available and all(quote.freshness == "live" for quote in available)
        else "closed_snapshot"
        if available and all(quote.freshness == "closed_snapshot" for quote in available)
        else "stale"
        if available
        else "unavailable"
    )
    if market_error:
        message = market_error
    elif freshness == "live":
        message = "交易时段公开行情已连接，每 60 秒刷新。"
    elif freshness == "closed_snapshot":
        message = "当前为非交易时段或休市，展示最近公开收盘快照。"
    else:
        message = "行情时间可能滞后，已暂停生成新的交易信号。"

    index_labels = {
        "sh000001": "上证",
        "sz399001": "深证",
        "sz399006": "创业板",
    }
    change_items = [
        (index_labels.get(quote.code, quote.name), quote.change_pct)
        for quote in available
        if quote.change_pct is not None
    ]
    changes = [change for _, change in change_items]
    if len(changes) < 2:
        regime = {
            "status": "unavailable",
            "label": "市场状态待数据",
            "message": "指数数据不足；长期牛熊识别仍需历史模型验证。",
        }
    else:
        average = sum(changes) / len(changes)
        if all(change > 0 for change in changes) and average >= 1:
            label = "当日偏强"
            state = "strong"
        elif all(change < 0 for change in changes) and average <= -1:
            label = "当日偏弱"
            state = "weak"
        elif any(change > 0 for change in changes) and any(
            change < 0 for change in changes
        ):
            label = "指数分化"
            state = "divergent"
        else:
            label = "当日震荡"
            state = "range"
        regime = {
            "status": state,
            "label": label,
            "message": (
                " · ".join(
                    f"{name} {change:+.2f}%"
                    for name, change in change_items
                )
                + "。"
                "这是当日行情描述，不等同于已验证的牛熊策略。"
            ),
        }
    return {
        "status": status,
        "message": message,
        "refresh_seconds": 60,
        "freshness": freshness,
        "universe_validation": "pending_historical_universe_audit",
        "new_signal_allowed": False,
        "indexes": [quote.to_dict() for quote in available],
        "regime": regime,
    }


def _merge_quotes(items: list[dict], quotes: dict) -> list[dict]:
    merged = []
    for item in items:
        quote = quotes.get(item["code"])
        output = dict(item)
        output["quote"] = quote.to_dict() if quote else None
        if "quantity" in output and "cost_price" in output:
            output["risk"] = _position_risk(output, quote)
        merged.append(output)
    return merged


def _research_observations(
    watchlist: list[dict],
    positions: list[dict],
    triggered: list[dict],
) -> list[dict]:
    """让已选股票始终可研究，但绝不自动提升为买入候选。"""

    triggered_by_code = {
        str(item.get("code")): item for item in triggered if item.get("code")
    }
    observations: list[dict] = []
    seen: set[str] = set()
    for item in [*watchlist, *positions]:
        code = str(item.get("code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        quote = item.get("quote") or {}
        trigger = triggered_by_code.get(code) or {}
        observations.append({
            "code": code,
            "name": item.get("name") or quote.get("name") or "",
            "level": "research_watch",
            "data_time": (
                quote.get("exchange_data_time")
                or quote.get("data_time")
                or trigger.get("data_time")
            ),
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "reason": trigger.get("reason") or (
                "已加入研究观察；可切换1分、5分、15分、30分和日K，"
                "查看真实行情与可复核技术条件。"
            ),
            "observation_available": True,
            "recommendation_status": "research_only",
            "analysis_url": f"/analysis?code={code}&interval=5m",
        })
    return observations


def _a_share_continuous_session(now: datetime) -> bool:
    """Return true only during the two continuous A-share sessions."""
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return (570 <= minute <= 690) or (780 <= minute <= 900)


def _candle_source(interval: str, *, index: bool = False) -> str:
    if interval == "1d":
        return (
            "真实指数日K（本地审计库 + 新浪近期补齐）"
            if index else "真实日K（本地审计库 + 新浪近期补齐）"
        )
    prefix = "AKShare / 新浪财经指数" if index else "AKShare / 新浪财经"
    return f"{prefix}{interval} K线"


def _position_risk(position: dict, quote) -> dict:
    cost_price = float(position["cost_price"])
    quantity = int(position["quantity"])
    stop_price = round(cost_price * (1 - RISK_PROFILE.stop_loss), 2)
    target_price = round(cost_price * (1 + RISK_PROFILE.take_profit), 2)
    result = {
        "cost_value": round(cost_price * quantity, 2),
        "stop_price": stop_price,
        "target_price": target_price,
        "status": "waiting_for_quote" if quote is None or quote.price is None else "hold",
        "message": "等待可用行情后计算浮盈亏与卖出风控。",
    }
    if quote is None or quote.price is None:
        return result
    market_value = round(quote.price * quantity, 2)
    unrealized_pnl = round((quote.price - cost_price) * quantity, 2)
    return_pct = round((quote.price / cost_price - 1) * 100, 2)
    result.update({
        "market_value": market_value,
        "unrealized_pnl": unrealized_pnl,
        "return_pct": return_pct,
        "message": "持有观察：未触及初始止损或止盈参考。",
    })
    if quote.price <= stop_price:
        result.update({"status": "stop_loss", "message": "触及初始止损参考；请人工核对后考虑卖出或减仓。"})
    elif quote.price >= target_price:
        result.update({"status": "take_profit", "message": "触及初始止盈参考；请人工核对后考虑卖出或减仓。"})
    return result


def _portfolio_status(positions: list[dict]) -> dict:
    symbols = {item["code"] for item in positions}
    cost_value = sum(item.get("risk", {}).get("cost_value", 0) for item in positions)
    known = [item["risk"] for item in positions if "market_value" in item.get("risk", {})]
    market_value = sum(item["market_value"] for item in known)
    pnl = sum(item["unrealized_pnl"] for item in known)
    return {
        "open_symbols": len(symbols),
        "cost_value": round(cost_value, 2),
        "market_value": round(market_value, 2) if known else None,
        "unrealized_pnl": round(pnl, 2) if known else None,
        "exposure_pct": round(cost_value / RISK_PROFILE.initial_capital * 100, 2),
        "risk_limit_message": f"初始总资金 ¥{RISK_PROFILE.initial_capital:.0f}；单只 ≤ {RISK_PROFILE.initial_position_limit * 100:.0f}%；最多 {RISK_PROFILE.max_positions} 只。",
    }
