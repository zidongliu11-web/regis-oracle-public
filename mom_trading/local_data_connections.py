"""Current local connection metadata; never read price rows or an account."""
from datetime import datetime, timezone

from flask import current_app, request, render_template

from mom_trading.local_research_history import _signature, local_request, local_response, START, END
from mom_trading.local_market_history import load_summary
from mom_trading.local_baseline_reference import load_reference
from mom_trading.research_catalogue import read_catalogue

ITEMS = (
    ("stock", "股票历史日线", "/stock?code=000001#localResearchHistory"),
    ("etf", "510300 历史日线", "/stock?code=000001&history_code=510300#localResearchHistory"),
    ("market", "历史市场涨跌家数", "/market-panorama"),
    ("baseline", "15万元现金与 ETF 基准记录", "/strategy-lab#localBaselineReference"),
    ("catalogue", "因子与战法登记", "/strategy-lab#slabResearchCatalogue"),
)


def history_metadata(reader):
    if reader is None:
        raise OSError("snapshot_not_loaded")
    # The reader hashed these sources at explicit local startup. Changed files
    # close this view; polling never rehashes the multi-GB source or opens HDF5.
    if (_signature(reader.path) != reader.signature
            or _signature(reader.calendar_path) != reader.calendar_signature):
        raise ValueError("snapshot_changed")
    return {"coverage": f"快照声明范围 {START}–{END}；实际覆盖按代码查询",
            "recorded_at": reader.captured_at, "source_sha256": reader.sha,
            "detail": "本机原始日线可查询；启动时校验文件，当前核对文件状态。未逐行认证历史时点与全市场覆盖。"}


def load_connections(root, stock, etf):
    rows = []
    for key, label, href in ITEMS:
        item = {"id": key, "label": label, "href": href, "state": "unavailable",
                "coverage": "本次未确认", "recorded_at": None, "source_sha256": None,
                "detail": "文件未加载、缺失或校验失败；请核对对应原件后重新检查。"}
        try:
            if key in {"stock", "etf"}:
                fields = history_metadata(stock if key == "stock" else etf)
            elif key == "market":
                data = load_summary(root, stock)
                fields = {"coverage": f"{data['start_date']}–{data['end_date']} · {len(data['days'])}个交易日",
                          "recorded_at": data['source_captured_at'], "source_sha256": data['summary_sha256'],
                          "detail": f"{data['included_datasets']}个快照代码；未复权价格方向。可选日期和交易板分组，没有行业或概念映射。"}
            elif key == "baseline":
                data = load_reference(root)
                fields = {"coverage": f"{data['first_date']}–{data['last_date']} · 现金及{len(data['scenarios'])}个原情景",
                          "recorded_at": data['recorded_at'], "source_sha256": data['sources']['result']['sha256'],
                          "detail": "原封存假设与结果可查看；未重算、未选赢家，不代表可交易基准已经通过。"}
            else:
                data = read_catalogue(root)
                fields = {"coverage": f"{data['factor_count']}项因子 · {data['cards_count']}张战法卡",
                          "recorded_at": data['registered_utc'], "source_sha256": data['source_bindings'][0]['sha256'],
                          "detail": "现有可行性登记及失败/证据不足处置可查看；不是已恢复的历史因子库，也不是回测通过清单。"}
            item.update(fields, state="available")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError):
            pass  # Each connection fails independently, without leaking paths.
        rows.append(item)
    return {"schema": "regis.local_data_connections.v1", "status": "available",
            "checked_at": datetime.now(timezone.utc).isoformat(), "connections": rows,
            "price_rows_read_by_this_request": 0, "new_source_requests": 0, "new_economic_runs": 0,
            "remaining": "实时市场、行业/概念与完整时点研究面板仍缺。历史行情、登记和基准记录各有范围，不能替代这些输入。"}


def install(app, root):
    # Only the explicit personal preview installs this module. Keep the normal
    # handlers for remote requests, other aliases and intentional legacy views.
    def personal_history_page():
        return (current_app.config.get("LOCAL_RESEARCH_HISTORY_ENABLED")
                and not current_app.config.get("REMOTE_MODE") and local_request())

    dashboard = app.view_functions.get("dashboard_page")
    stock_page = app.view_functions.get("stock_page")
    if dashboard is not None:
        def history_home(*args, **kwargs):
            if personal_history_page() and request.path == "/" and not request.args:
                response = app.make_response(render_template("decision_v2.html"))
                response.headers["Cache-Control"] = "no-store"
                return response
            return dashboard(*args, **kwargs)
        app.view_functions["dashboard_page"] = history_home

    if stock_page is not None:
        def history_stock(*args, **kwargs):
            allowed = {"code", "history_code", "start", "end", "limit", "view"}
            simple = (not set(request.args) - allowed
                      and all(len(request.args.getlist(k)) == 1 for k in request.args)
                      and request.args.get("view", "history") == "history")
            if personal_history_page() and simple:
                # The existing reader/controller validates codes and dates.
                # Never import the old snapshot/quote/account startup chain.
                code = request.args.get("history_code", request.args.get("code", "000001"))
                response = app.make_response(render_template("stock_research.html", code=code))
                response.headers["Cache-Control"] = "no-store"
                return response
            return stock_page(*args, **kwargs)
        app.view_functions["stock_page"] = history_stock

    @app.get("/api/local-research/connections")
    def connections():
        if not current_app.config.get("LOCAL_RESEARCH_HISTORY_ENABLED") or not local_request():
            return local_response({"status": "disabled", "message": "仅供已启用的本机研究页面查看。"}, 403)
        if request.args:
            return local_response({"status": "invalid_request", "message": "只检查已接通的数据，不接收采集或回算参数。"}, 400)
        return local_response(load_connections(root, current_app.extensions.get("local_research_history"),
                                               current_app.extensions.get("local_benchmark_history")), 200)
