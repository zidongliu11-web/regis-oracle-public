"""Exact original 20-day amount inputs on a supplied 21-session source window.

Source facts only: no historical identity, market gate, ranking or fills assumed.
"""
from datetime import date, timedelta
import math, re
import pandas as pd
from research.capture_completed_daily_pair import STOCK_FIELDS
from research.retained_daily_diagnostic import day, numeric
from research.retained_daily_counterfactual import validate_bar


def lookback(signal_day, history, current, calendar, *, source_code="sh.600000"):
    if not isinstance(source_code,str) or not re.fullmatch(r"(sh|sz)\.[0-9]{6}",source_code):
        raise ValueError("source_code_scope")
    if history.get('schema') != 'baostock_symbol_history_raw.v1' or history.get('fields') != STOCK_FIELDS:
        raise ValueError('stock_history_schema_or_fields')
    if calendar.get('schema') != 'baostock_trade_dates_raw.v1' or calendar.get('fields') != ['calendar_date', 'is_trading_day']:
        raise ValueError('calendar_history_schema_or_fields')
    sessions = {}
    for source, rows in [('history', calendar['rows']), ('current', current['calendar']['rows'])]:
        for values in rows:
            if source == 'history' and (not isinstance(values, list) or len(values) != 2):
                raise ValueError('calendar_history_row_width')
            r = dict(zip(calendar['fields'], values)) if source == 'history' else values
            if not isinstance(r, dict) or set(r) != {'calendar_date', 'is_trading_day'}:
                raise ValueError('calendar_row_shape')
            d = day(r['calendar_date'])
            if d in sessions or r['is_trading_day'] not in ('0', '1'):
                raise ValueError('calendar_duplicate_or_flag')
            sessions[d] = r['is_trading_day']
    target = day(signal_day)
    trading = sorted(d for d, flag in sessions.items() if d <= target and flag == '1')
    if len(trading) < 21 or trading[-1] != target:
        raise ValueError('signal_or_21_calendar_sessions_missing')
    window = trading[-21:]
    first, last = date.fromisoformat(window[0]), date.fromisoformat(target)
    civil = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    if any(d not in sessions for d in civil):
        raise ValueError('calendar_window_civil_coverage_missing')
    stocks = {}
    for role, rows in [('history', history['rows']), ('current', current['stock']['rows'])]:
        for values in rows:
            if role == 'history':
                if not isinstance(values, list) or len(values) != len(STOCK_FIELDS):
                    raise ValueError('stock_history_row_width')
                r = dict(zip(STOCK_FIELDS, values))
            else:
                r = values
            if not isinstance(r, dict) or set(r) != set(STOCK_FIELDS) or r['code'] != source_code:
                raise ValueError('stock_identity_or_fields')
            d = day(r['date'])
            if d in stocks:
                raise ValueError('stock_duplicate_at_join')
            stocks[d] = (r, role)
    if any(d not in stocks for d in window):
        raise ValueError('stock_window_missing_session')
    values = []
    for d in window:
        row, role = stocks[d]
        validate_bar(row)
        if row['tradestatus'] not in ('0', '1') or row['isST'] not in ('0', '1'):
            raise ValueError('stock_window_state_flag')
        amount, volume = numeric(row['amount']), numeric(row['volume'])
        if amount < 0 or volume < 0 or (row['tradestatus'] == '0' and (amount > 0 or volume > 0)):
            raise ValueError('stock_window_turnover_or_status')
        reference = numeric(row['preclose'], positive=True)
        pct = numeric(row['pctChg'])
        if abs(pct - (numeric(row['close'], positive=True) / reference - 1) * 100) > numeric('0.001'):
            raise ValueError('stock_window_pct_preclose_inconsistent')
        float_amount = float(amount)
        if not math.isfinite(float_amount):
            raise ValueError('lookback_amount_float_nonfinite')
        values.append({'date': d, 'amount': float_amount, 'source_role': role})
    # Match original chain pandas arithmetic exactly; do not invent a mean
    # including signal day or a median excluding signal day.
    amount = pd.Series([r['amount'] for r in values], index=window, dtype='float64')
    original = stocks[target][0]
    native = {'code': source_code[3:], 'source_code': source_code, 'trade_date': target,
              'close': float(numeric(original['close'], positive=True)),
              'pct_chg': float(numeric(original['pctChg'])),
              'median_amount20': float(amount.iloc[1:].median()),
              'adv20': float(amount.iloc[:-1].mean()),
              'history_sessions': 21, 'history_scope': 'exact_21_session_snapshot_not_total_provider_history',
              'source_isST': original['isST'], 'source_tradestatus': original['tradestatus'],
              'window_rows': values, 'full_M24_eligible': False,
              'full_M24_selector_ready': False, 'production_eligible': False}
    if any(not math.isfinite(native[k]) for k in ['close', 'pct_chg', 'median_amount20', 'adv20']):
        raise ValueError('lookback_metric_float_nonfinite')
    summary = {'signal_day': target, 'window': [window[0], window[-1]], 'trading_sessions': 21,
               'civil_dates_checked': len(civil), 'median_window_sessions': 20,
               'median_includes_signal_day': True, 'adv_window_sessions': 20,
               'adv_excludes_signal_day': True, 'previous_session': window[-2],
               'source_row_counts': {k: sum(v['source_role'] == k for v in values) for k in ['history', 'current']},
               'original_amount_threshold_CNY': 50_000_000,
               'amount_thresholds_conditional_on_provider_units':
                   native['median_amount20'] >= 50_000_000 and native['adv20'] >= 50_000_000,
               'PIT_verified': False, 'full_A_verified': False, 'selector_ready': False,
               'new_formulas': 0, 'economic_runs': 0, 'generates_alert': False}
    return native, summary
