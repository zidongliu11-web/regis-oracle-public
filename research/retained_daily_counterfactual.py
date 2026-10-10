"""Finite, source-bound daily counterfactual. Never a tradable M24 admission.

The existing cash engines own accounting. This module only binds the selected
source roles and the explicitly frozen hypothetical conventions to those books.
No source quote arrays or per-day factor values are printed or placed in Git.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'research'))
from research import retained_daily_diagnostic as source
from research.stock_daily_cash_book import replay, UnresolvedAction
from research.matched_etf_cash_book import replay_matched_etf
from quantqc import ashare

D = Decimal
STOCK = '600000.XSHG'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def recheck_source(path, expected_sha, maximum_bytes, expected_size=None):
    with Path(path).open('rb') as stream:
        raw = stream.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise ValueError('source_byte_cap_after_read')
    if ((expected_size is not None and len(raw) != expected_size)
            or hashlib.sha256(raw).hexdigest() != expected_sha):
        raise ValueError('source_drift_after_read')
    return len(raw)


def date_int(value):
    return int(source.day(value).replace('-', ''))


def source_tables(root, specs, limits):
    tables, metadata, total = {}, [], 0
    for spec in specs:
        path = (Path(root) / spec['path']).resolve()
        if not path.is_relative_to(Path(root).resolve()):
            raise ValueError('source_outside_project')
        with path.open('rb') as stream:
            raw = stream.read(limits['max_bytes_per_object'] + 1)
        total += len(raw)
        if len(raw) > limits['max_bytes_per_object'] or total > limits['max_total_bytes']:
            raise ValueError('source_byte_cap')
        if hashlib.sha256(raw).hexdigest() != spec['sha256']:
            raise ValueError('source_SHA_changed')
        obj = source.parse(raw)
        if obj.get('schema') != spec['expected_schema']:
            raise ValueError('source_schema_changed')
        fields, rows = obj['fields'], obj['rows']
        if len(fields) != len(set(fields)) or len(rows) > limits['max_rows_per_object']:
            raise ValueError('source_shape_or_row_cap')
        capture = datetime.fromisoformat(obj[spec['capture_field']])
        if capture.tzinfo is None:
            raise ValueError('source_capture_clock_invalid')
        role = spec['role']; keyfield = 'calendar_date' if role == 'calendar' else 'code' if role == 'identity' else 'date'
        table = {}
        for values in rows:
            if not isinstance(values, list) or len(values) != len(fields):
                raise ValueError('source_row_width')
            row = dict(zip(fields, values)); key = row[keyfield]
            if key in table:
                raise ValueError('duplicate_source_key')
            if role in ('stock', 'etf') and row['code'] != spec['code']:
                raise ValueError('unexpected_source_instrument')
            table[key] = row
        recheck_source(path, spec['sha256'], limits['max_bytes_per_object'], len(raw))
        tables[role] = table
        metadata.append({'role': role, 'sha256': spec['sha256'], 'capture_at': capture.isoformat(), 'rows': len(rows), 'bytes': len(raw), 'historical_available_at': None})
    return tables, metadata


def sessions(calendar, window):
    start, end = date.fromisoformat(window['start']), date.fromisoformat(window['end'])
    if end < start:
        raise ValueError('window_invalid')
    civil = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    if any(day not in calendar for day in civil):
        raise ValueError('calendar_civil_gap')
    if any(calendar[day]['is_trading_day'] not in ('0', '1') for day in civil):
        raise ValueError('calendar_flag_invalid')
    return [day for day in civil if calendar[day]['is_trading_day'] == '1']


def validate_bar(row):
    if row['adjustflag'] != '3':
        raise ValueError('nonraw_adjustment_role')
    values = {key: source.numeric(row[key], positive=True) for key in ('open', 'high', 'low', 'close')}
    if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
        raise ValueError('invalid_source_OHLC')
    volume = source.numeric(row['volume'])
    if volume < 0:
        raise ValueError('negative_source_volume')
    # Positive/nonpositive indicator only. No capacity participation inference.
    return {**{key: str(value) for key, value in values.items()}, 'volume': str(volume)}


def stock_bar(row, day):
    bar = validate_bar(row)
    if row['isST'] not in ('0', '1') or row['tradestatus'] not in ('0', '1'):
        raise ValueError('source_state_flag_invalid')
    reference = source.numeric(row['preclose'], positive=True)
    risk = row['isST'] == '1'
    rule = ashare.price_limit_rule('600000.SH', trade_date=day, is_risk_warning=risk, no_price_limit=False)
    if not rule['valid'] or rule['rate'] is None:
        raise ValueError('stock_limit_rule_unknown')
    bounds = {side: ashare._limit_price(str(reference), rule['rate'], side,
                minimum_tick_rule_verified=ashare._minimum_tick_rule_verified('600000.SH', day)) for side in ('up', 'down')}
    if any(value is None for value in bounds.values()):
        raise ValueError('stock_limit_bound_unknown')
    return {**bar, 'limit_up': str(bounds['up']), 'limit_down': str(bounds['down']),
            'halted': row['tradestatus'] != '1', 'st': risk, 'outside_lifetime': False}


def negative_intents(stock, days):
    # Single fixed name. No historical universe, cross-sectional rank, liquidity
    # selection or market/sector gate is represented by this arithmetic rule.
    return [{'instrument_id': STOCK, 'signal_date': date_int(day), 'rank': 1}
            for day in days if source.numeric(stock[day]['pctChg']) < 0]


def etf_entry_reference(bars, dates, entry_index, known_ex_dates):
    # ETF source lacks preclose. ONLY a declared counterfactual non-action entry
    # uses prior raw close; this never enters Panel or its production labels.
    if entry_index >= len(dates):
        return bars
    if entry_index < 1 or dates[entry_index] in known_ex_dates:
        raise ValueError('ETF_entry_reference_unknown_on_action_or_first_day')
    reference = D(bars[entry_index-1]['close'])
    bounded = [dict(bar) for bar in bars]
    for side, multiplier in [('limit_down', D('.9')), ('limit_up', D('1.1'))]:
        bounded[entry_index][side] = str((reference*multiplier).quantize(D('.001'), rounding=ROUND_HALF_UP))
    return bounded


def benchmark_overlay(summary, ledger, idle_cash, capital):
    # Reuse the ETF engine on the frozen20% budget; idle cash is part of the
    # same150k alternative account, not a separately credited account.
    reserve, initial = D(idle_cash), D(capital)
    full = [{**row, 'nav_CNY': str(D(row['nav_CNY'])+reserve), 'cash_CNY': str(D(row['cash_CNY'])+reserve)} for row in ledger]
    if reserve < 0 or initial <= 0:
        raise ValueError('benchmark_capital_invalid')
    peak, maximum = initial, D(0)
    annual, year_start = {}, initial
    for index, row in enumerate(full):
        nav = D(row['nav_CNY']); peak = max(peak, nav); maximum = max(maximum, D(1)-nav/peak)
        if index == len(full)-1 or full[index+1]['date']//10000 != row['date']//10000:
            annual[str(row['date']//10000)] = {'net_return': str(nav/year_start-1), 'end_nav_CNY': str(nav)}
            year_start = nav
    result = dict(summary, terminal_marked_nav_CNY=str((D(summary['terminal_marked_nav_CNY'])+reserve).quantize(D('.01'))), terminal_cash_CNY=str((D(summary['terminal_cash_CNY'])+reserve).quantize(D('.01'))), maximum_marked_drawdown=str(maximum), initial_allocation_target='0.2', account_initial_CNY=str(initial), annual_marked_results=annual)
    return result, full


def run(root, protocol_path):
    root = Path(root).resolve(); p = source.read(protocol_path); n = Path(p['native_evidence_root'])
    with (n/'counterfactual.dispatched').open('x', encoding='utf-8') as stream:
        stream.write(sha(protocol_path))
    started = time.monotonic()
    out = {'schema': 1, 'stage': 'E175', 'state': 'daily_counterfactual_started', 'protocol_sha256': sha(protocol_path), 'implementation_sha256': sha(__file__), 'started_utc': datetime.now(timezone.utc).isoformat(), 'failure': None, 'case_attempts': 0, 'scenarios': [], 'PIT_certified': False, 'financial_admitted': False, 'model_admitted': False, 'full_goal_complete': False, 'OOS_computed': False, 'orders': 0, 'new_alpha_candidates': 0, 'raw_values_exported': 0}
    native = {}
    try:
        if len(p['objects'])!=4 or len(p['scenarios'])!=4 or D(p['ETF_budget_CNY'])+D(p['ETF_idle_cash_CNY'])!=D(p['initial_CNY']):
            raise ValueError('frozen_scope_or_capital_invalid')
        for path, digest in p['input_hashes'].items():
            if sha(root/path) != digest:
                raise ValueError('frozen_input_changed')
        tables, out['source_objects'] = source_tables(root, p['objects'], p['limits'])
        days = sessions(tables['calendar'], p['window']); dates = [date_int(day) for day in days]
        if len(days) != 179 or days[0] != p['window']['start'] or days[-1] != p['window']['end']:
            raise ValueError('selected_window_calendar_changed')
        for role in ('stock', 'etf'):
            if any(day not in tables[role] for day in days):
                raise ValueError('selected_price_missing')
            if any(p['window']['start']<=day<=p['window']['end'] and day not in days for day in tables[role]):
                raise ValueError('price_on_closed_calendar_day')
        if any(code not in tables['identity'] for code in p['codes']):
            raise ValueError('current_source_identity_missing')
        stock = {date_int(day): stock_bar(tables['stock'][day], day) for day in days}
        etf = [{**validate_bar(tables['etf'][day]), 'limit_up': None, 'limit_down': None} for day in days]
        intents = negative_intents(tables['stock'], days)
        out.update(session_count=len(days), negative_fragment_intents=len(intents), window=p['window'])
        notice = p['known_notices'][STOCK]
        action = {STOCK: {'cash': [{**{key: notice[key] for key in ('record_date','ex_date','pay_date')}, 'gross_cash_per_share': notice['gross_cash_per_unit'], 'valid': True}], 'split': [], 'unknown_factor_dates': []}}
        fund_notice = p['known_notices']['510300.XSHG']
        dividends = [{**{key: fund_notice[key] for key in ('record_date','ex_date','pay_date')}, 'cash_per_unit': D(fund_notice['gross_cash_per_unit'])}]
        for scenario in p['scenarios']:
            if time.monotonic()-started > p['limits']['max_seconds']:
                raise ValueError('counterfactual_time_cap')
            out['case_attempts'] += 1
            stock_summary, stock_detail = replay(dates, intents, lambda key, day: stock[day] if key==STOCK else None, action, scenario, initial=p['initial_CNY'], lot=p['lot_size'], name_cap=p['name_allocation_target'], gross_cap=p['name_allocation_target'], max_positions=1, tax=p['fixed_stock_dividend_reserve'])
            native[scenario['id']]={'stock':stock_detail,'stock_summary':stock_summary}
            out['case_attempts'] += 1
            entry_index = 1+scenario['entry_delay_sessions']
            bounded = etf_entry_reference(etf, dates, entry_index, {fund_notice['ex_date']})
            etf_summary, etf_ledger = replay_matched_etf(bounded, dates, dividends, scenario, initial=p['ETF_budget_CNY'], lot=p['lot_size'], tick='.001')
            etf_summary, etf_ledger = benchmark_overlay(etf_summary, etf_ledger, p['ETF_idle_cash_CNY'], p['initial_CNY'])
            # The reference curves differ in exposure and holding schedule.
            # Comparison is descriptive, never an exposure-adjusted alpha test.
            delta = D(stock_summary['terminal_marked_nav_CNY'])-D(etf_summary['terminal_marked_nav_CNY'])
            out['scenarios'].append({'id':scenario['id'], 'stock':stock_summary, 'ETF':etf_summary, 'cash_terminal_CNY':p['initial_CNY'], 'stock_minus_ETF_marked_CNY':str(delta.quantize(D('.01'))), 'exposure_adjusted_alpha_claimed':False})
            native[scenario['id']]['ETF']=etf_ledger
        after_total = 0
        for spec, observed in zip(p['objects'], out['source_objects']):
            after_total += recheck_source(root/spec['path'], spec['sha256'],
                            p['limits']['max_bytes_per_object'], observed['bytes'])
            if after_total > p['limits']['max_total_bytes']:
                raise ValueError('source_total_byte_cap_after_replay')
        for path, digest in p['input_hashes'].items():
            if sha(root/path) != digest:
                raise ValueError('frozen_input_changed_after_replay')
        if time.monotonic()-started > p['limits']['max_seconds']:
            raise ValueError('counterfactual_time_cap')
        out['state']='finite_conditional_daily_books_computed_no_admission'
    except Exception as exc:
        if isinstance(exc,UnresolvedAction):
            native['failed_account_checkpoint']=exc.native_checkpoint
            out['unresolved_action']=exc.dependency
        out['failure']={'type':type(exc).__name__, 'reason':str(exc) if isinstance(exc,(ValueError,AssertionError)) else 'source_or_accounting_failure'}
        out['state']='daily_counterfactual_failed_preserved'
    with (n/'native_daily_books.json').open('x', encoding='utf-8') as stream:
        json.dump(native,stream,ensure_ascii=False,separators=(',',':'))
    out['native_ledger_sha256']=sha(n/'native_daily_books.json')
    out['limitations']=p['limitations']; out['ended_utc']=datetime.now(timezone.utc).isoformat(); out['elapsed_seconds']=round(time.monotonic()-started,6)
    with (n/'terminal.json').open('x', encoding='utf-8') as stream:
        json.dump(out,stream,ensure_ascii=False,indent=2)
    brief={'state':out['state'],'failure':out['failure'],'case_attempts':out['case_attempts'],'session_count':out.get('session_count'),'negative_fragment_intents':out.get('negative_fragment_intents'),'native_terminal_sha256':sha(n/'terminal.json'),'native_ledger_sha256':out['native_ledger_sha256'],'scenarios':[{'id':x['id'],'stock_terminal_marked_CNY':x['stock']['terminal_marked_nav_CNY'],'stock_drawdown':x['stock']['maximum_marked_drawdown'],'ETF_terminal_marked_CNY':x['ETF']['terminal_marked_nav_CNY'],'ETF_drawdown':x['ETF']['maximum_marked_drawdown'],'stock_minus_ETF_marked_CNY':x['stock_minus_ETF_marked_CNY']} for x in out['scenarios']],'financial_admitted':False,'model_admitted':False,'full_goal_complete':False}
    print(json.dumps(brief,ensure_ascii=False))
    return 1 if out['failure'] else 0


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--protocol',required=True);args=parser.parse_args()
    raise SystemExit(run(ROOT,ROOT/args.protocol))
