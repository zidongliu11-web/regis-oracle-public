"""Two bounded daily arithmetic previews; no historical eligibility or trial.

Reuse the captured reader and registered mechanisms, without substituting inputs
for the frozen legacy workbench. Installed only by the opt-in loopback preview.
"""
from __future__ import annotations

import math

from flask import current_app, request

from mom_trading.local_research_history import local_request, local_response, parameters, START, _date
from mom_trading.research_catalogue import read_catalogue

IDS = ("close_acceptance", "pullback_volume_3_20")
EXECUTION_FIELDS = ('prev_close', 'limit_down', 'limit_up')


def execution_fields(rows, data, query, execution):
    """Join source values to dates, without qualifying or invoking a callback."""
    meta = {'schema': 'regis.daily_execution_fields.v1', 'status': 'unavailable',
            'source_sha256': None, 'captured_at': None, 'source_rows_read': 0,
            'execution_eligible': False, 'callback_available': False}
    by_date = {}
    if execution is not None:
        source = execution.read(*query)
        if not (source['schema'] == 'regis.daily_execution_inputs.v1'
                and source['source_sha256'] == data['source_sha256'] and source['captured_at'] == data['captured_at']
                and source['code'] == query[0] and source['callback_available'] is False
                and source['execution_eligible'] is False and source['model_admitted'] is False):
            raise ValueError('execution_source_mismatch')
        by_date = {r['time']: r for r in source['rows']}
        target = [r['date'] for r in rows]
        for day in [*by_date, *target]: _date(day)
        if (len(by_date) > query[3] or len(by_date) != len(source['rows']) or len(set(target)) != len(target)
                or set(by_date) != set(target)):
            raise ValueError('execution_date_mismatch')
        meta.update(status='source_values_connected', source_sha256=source['source_sha256'], captured_at=source['captured_at'], source_rows_read=len(by_date))
    for row in rows:
        raw = by_date.get(row['date'])
        row['execution_fields'] = ({**{f: raw[f] for f in EXECUTION_FIELDS},
                                    'field_status': {f: raw['field_status'][f] for f in EXECUTION_FIELDS},
                                    'limit_band_status': raw['limit_band_status'], 'execution_eligible': False}
                                   if raw is not None else
                                   {**dict.fromkeys(EXECUTION_FIELDS), 'field_status': dict.fromkeys(EXECUTION_FIELDS, 'unavailable'),
                                    'limit_band_status': 'unknown', 'execution_eligible': False})
    if execution is not None: execution._check()
    return meta


def calculate(rows, calendar):
    """Same-day location and strictly lagged amount ratio, never future rows.

These are arithmetic components, not the legacy function's qualified features:
its adjustment/trade-status/PIT gates have not been established for this input.
"""
    from factor_system.registered_daily_inputs import calculate_rows
    return calculate_rows(rows, calendar)


def preview(reader, query, root, context=None, execution=None):
    code, start, end, limit = query
    registered = {f["id"]: f for f in read_catalogue(root)["factors"]}
    formulas = [{"id": key, "formula": registered[key]["formula"],
                 "source_binding": registered[key]["source_binding"]} for key in IDS]
    # Warmup can precede the selected start. No future data or full-market scan.
    data = reader.read(code, int(START.replace("-", "")), end, limit + 23)
    rows = calculate(data["rows"], reader.calendar)
    rows = [r for r in rows if start <= int(r["date"].replace("-", "")) <= end][-limit:]
    from mom_trading.local_daily_context import join, unavailable
    days = [int(r["date"].replace("-", "")) for r in rows]
    contexts, context_meta = (context.read(code, days) if context is not None else
                              (join(code, days, reader.calendar), unavailable()))
    for row, day in zip(rows, days):
        row["input_context"] = contexts[day]
    execution_meta = execution_fields(rows, data, query, execution)
    return {"schema": "regis.local_daily_factor_preview.v1", "status": "available" if rows else "empty_window",
            "code": code, "rows": rows, "returned_rows": len(rows), "source_price_rows_used": len(data["rows"]),
            "timestamp_scan_scope": "selected_security_full_datetime_field",
            "requested_start": reader._iso(start), "requested_end": reader._iso(end),
            "source_sha256": data["source_sha256"], "captured_at": data["captured_at"],
            "source": data["source"], "formulas": formulas, "arithmetic_only": True,
            "legacy_feature_validity_applied": False, "new_economic_runs": 0,
            "input_context": context_meta,
            "execution_fields": execution_meta,
            "note": "固定公式的日线数值预览；不含原模型的复权可信、交易状态、历史股票池及公告时点资格检查。不是因子通过或交易信号。"}


def install(app, root):
    from mom_trading.local_daily_context import DailyContext
    from research.daily_execution_inputs import DailyExecutionInputs
    try:
        context = DailyContext(app.extensions.get("local_factor_inputs"), app.extensions.get("local_research_history"))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
        context = None
    app.extensions["local_daily_context"] = context
    try:
        app.extensions['local_daily_execution_inputs'] = DailyExecutionInputs(app.extensions.get('local_research_history'))
    except (ValueError, AttributeError, TypeError):
        app.extensions['local_daily_execution_inputs'] = None
    @app.get("/api/local-research/daily-factors")
    def daily_factors():
        if not current_app.config.get("LOCAL_RESEARCH_HISTORY_ENABLED") or not local_request():
            return local_response({"status": "disabled", "message": "仅供已启用的本机历史研究页面查看。"}, 403)
        try:
            query = parameters(request.args)
            if query[3] > 250:
                raise ValueError("preview_limit")
        except ValueError:
            return local_response({"status": "invalid_request", "message": "请填写沪深 A 股代码、快照内日期，最多250条。"}, 400)
        reader = current_app.extensions.get("local_research_history")
        if reader is None:
            return local_response({"status": "unavailable", "message": "已保存日线未通过校验；请恢复原件后重启本机预览。"}, 503)
        try:
            payload = preview(reader, query, root, context, app.extensions.get('local_daily_execution_inputs'))
        except KeyError:
            return local_response({"status": "unavailable", "message": "日线或公式字段校验失败；本次没有生成结果。"}, 503)
        except LookupError:
            return local_response({"status": "not_in_snapshot", "message": "此代码未收录于已保存快照。"}, 404)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return local_response({"status": "unavailable", "message": "日线或公式来源校验失败；本次没有生成结果。"}, 503)
        return local_response(payload, 200)
