"""Project already published conditional aggregates, never run a replay.

Only pinned repository records are opened. Native prices, per-order ledgers and
the earlier withheld stock totals are not read or exposed by this adapter.
"""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from flask import current_app, request
from mom_trading.local_research_history import local_request, local_response

PROTOCOL = 'research/MATCHED_ETF_REFERENCE_PROTOCOL_20261003.json'
RESULT = 'research/MATCHED_ETF_REFERENCE_RESULT_20261003.json'
STOCK = 'research/STOCK_RECORD_OWNER_RESULT_20261003.json'
PINNED = {'research/ETF_DAILY_REFERENCE_PROTOCOL_20261002.json': '162e07a9eeb988e1da899e5516f87b81f58326cfe96bb8fea05317ef53cebcad', 'research/RQ_ETF_HISTORY_RECOVERY_RECEIPT_20261002.json': 'cd752d56d1b38c200bac26c5dd6ecd01ad7c5c50d650361e6285f9d2062cca87', 'research/ETF_ACTION_CASH_COMPLETION_RESULT_20261002.json': 'b7a33137f7a57c6243cdec50f05447ca522d956f877681d823116d399ef18d21', 'research/run_etf_daily_reference.py': 'adc05fb6f62045116b3374133476b0cb233f11aeb489c2cccf9911b070a077e1', 'research/inspect_short_rq_full.py': '8e6ab30772d50631a0d8f18a2deda65d2ba4add3b8525e7d040a236f06dc27e3', 'research/recover_rq_etf_history.py': '28515446702dddbf2dddffc558b92234ba74fb916a1a178579299a19b700c58f', 'research/STOCK_EVENT_CASH_PROTOCOL_20261003.json': '19098e22d9e440aaaf22f1c89de30136af99c3e37df2125e39e9e4feff719ff2', 'research/STOCK_RECORD_OWNER_PROTOCOL_20261003.json': '7dd34058dfdff0daea84ad0a753b793edc517b5ebffd10ee36906eb614b487b8', 'research/STOCK_RECORD_OWNER_RESULT_20261003.json': 'fcca4f8f987b40922a8b1f27e4db0dcdf3dd3c00cdc43fb0047f2ef1ff16da8c', 'research/stock_event_cash_book.py': 'f8e4f4755de98c5aff7a10949eed731c95d7ba258a6dff459a42d329776c43df', 'research/matched_etf_cash_book.py': '7862043bb658d0c9766c51490b94d0c7d18f0049bb5043749d2fe012d9d4e1d7', 'research/run_matched_etf_reference.py': 'f44908812c7d2d05653d4dd23a00364b26bc6b8d01aa19548c41c70135df38d4', 'research/test_matched_etf_cash_book.py': 'f98c4be0702e85919f44c995a9158e32e35cde0e51c8803e2ffebee5ff05bee4', 'research/MATCHED_ETF_SOURCE_GAPS_20261003.json': '3fb895e04fa4c8e30676e0c592e2071b942b01a53820ed5edb4f65a0fbc935a7', 'research/MATCHED_ETF_REFERENCE_PROTOCOL_20261003.json': 'c52af081335531621b95ec16552a9fc576ce286cc1ef83ffe88a765003b872cc', 'research/MATCHED_ETF_REFERENCE_RESULT_20261003.json': 'cd657088515136d23228aaa51ba89f768adbbc593934ea46b0d7fd525e8b77c4'}
IDS = ('daily_standard_full', 'daily_partial_half', 'one_session_delayed_adverse', 'no_fill_cash')
LABELS = ('标准成交假设', '半额成交假设', '多延迟一个交易日且不利成交', '未成交，保留现金')
FLAGS = ('PIT_certified', 'OOS_computed', 'model_admitted', 'full_goal_complete',
         'original_M24_changed_or_enabled', 'full_historical_action_coverage_certified')


def require(ok):
    if not ok:
        raise ValueError('matched_record_invalid')


def decimal(value):
    require(isinstance(value, str))
    d = Decimal(value)
    require(d.is_finite())
    return d


def account_summary(row):
    nav = decimal(row['terminal_marked_nav_CNY'])
    drawdown = decimal(row['maximum_marked_drawdown'])
    fees = decimal(row['total_fees_CNY'])
    require(nav >= 0 and fees >= 0 and 0 <= drawdown <= 1)
    return {'terminal_marked_CNY': str(nav), 'net_total_return': str(nav / Decimal('150000') - 1),
            'maximum_marked_drawdown': str(drawdown), 'fees_CNY': str(fees)}


def load_matched(root):
    documents = {}
    for name, expected in PINNED.items():
        with (Path(root) / name).open('rb') as stream:
            raw = stream.read(512001)
        require(len(raw) <= 512000 and hashlib.sha256(raw).hexdigest() == expected)
        if name in (PROTOCOL, RESULT, STOCK):
            documents[name] = json.loads(raw)
    p, r, stock = (documents[k] for k in (PROTOCOL, RESULT, STOCK))
    require(p['schema'] == 'regis.matched_etf_reference_protocol.v1'
            and r['schema'] == 'regis.matched_etf_reference.v1'
            and r['state'] == 'fixed_matched_period_ETF_reference_computed'
            and p['stock_result'] == STOCK
            and p['input_hashes'] == {k: v for k, v in PINNED.items() if k not in (PROTOCOL, RESULT)}
            and r['protocol_sha256'] == PINNED[PROTOCOL]
            and r['implementation_sha256'] == PINNED['research/run_matched_etf_reference.py']
            and r['core_sha256'] == PINNED['research/matched_etf_cash_book.py']
            and p['order_book_id'] == '510300.XSHG' and p['initial_CNY'] == '150000' and p['lot_size'] == 100
            and (r['start_session'], r['end_session'], r['session_count']) == (20130104, 20201231, 1945)
            and (p['start_date'], p['end_date']) == (20130101, 20201231)
            and r['limits'] == p['limits']
            and all(r[k] is False and stock[k] is False for k in FLAGS)
            and tuple(s['id'] for s in p['scenarios']) == IDS
            and tuple(s['id'] for s in r['scenarios']) == IDS
            and tuple(s['id'] for s in stock['scenarios']) == IDS)
    rows = []
    for i, (case, prior, assumption) in enumerate(zip(r['scenarios'], stock['scenarios'], p['scenarios'])):
        require(case['financial_totals_admitted'] is False and case['OOS_computed'] is False
                and case['model_admitted'] is False and prior['model_admitted'] is False
                and case['cash_terminal_CNY'] == '150000'
                and case['stock'] == prior.get('conditional_daily_financial_summary')
                and case['ETF']['model_admitted'] is False and case['ETF']['real_trades_executed'] == 0
                and case['ETF']['terminal_sale_executed'] is False)
        failed = case['id'] == 'daily_partial_half'
        require(case['state'] == ('stock_arm_actual_failure_no_comparison' if failed else 'limited_conditional_comparison_available'))
        if failed:
            require(case['stock'] is None and 'stock_minus_ETF_terminal_CNY' not in case
                    and case['stock_failure_dependency']['kind'] == 'transformation_fractional_share_allocation_unresolved')
            stock_summary, difference = None, None
        else:
            require(case['stock_failure_dependency'] is None)
            stock_summary = account_summary(case['stock'])
            difference = decimal(case['stock_minus_ETF_terminal_CNY'])
            require(difference == (decimal(case['stock']['terminal_marked_nav_CNY'])
                    - decimal(case['ETF']['terminal_marked_nav_CNY'])).quantize(Decimal('.01')))
        rows.append({'id': case['id'], 'label': LABELS[i], 'state': case['state'],
                     'stock': stock_summary, 'ETF': account_summary(case['ETF']), 'cash_CNY': '150000',
                     'stock_minus_ETF_CNY': None if difference is None else str(difference),
                     'failure': '股票半额成交路径遇到转增碎股分配未核清，保留失败，未发布股票总额及比较。' if failed else None,
                     'assumptions': {k: assumption[k] for k in ('entry_delay_sessions', 'entry_field', 'slippage',
                         'buy_all_in_fee_rate', 'sell_all_in_fee_rate', 'minimum_fee', 'entry_fill_fraction', 'exit_fill_fraction')}})
    return {'schema': 'regis.local_matched_research.v1', 'status': 'available', 'initial_CNY': '150000',
            'lot_size': 100, 'first_date': '2013-01-04', 'last_date': '2020-12-31', 'sessions': 1945,
            'recorded_at': r['ended_utc'], 'scenarios': rows,
            'source_versions': [{'name': Path(k).name, 'sha256': v} for k, v in PINNED.items()],
            'stock_lineage': 'initial_cash_failures_preserved_then_specific_record_owner_dependencies_resolved',
            'earlier_failures_not_overwritten_by_this_reader': True, 'native_ledger_reverified': False,
            'financial_totals_admitted': False, 'PIT_certified': False, 'OOS_computed': False,
            'model_admitted': False, 'full_goal_complete': False, 'new_economic_runs': 0, 'actual_orders': 0,
            'winner_selection': False, 'exposure_neutral_comparison': False}


def install(app, root):
    @app.get('/api/local-research/matched-reference')
    def matched_reference():
        if not current_app.config.get('LOCAL_RESEARCH_HISTORY_ENABLED') or not local_request():
            return local_response({'status': 'disabled', 'message': '仅供已启用的本机历史研究查看。'}, 403)
        if request.args:
            return local_response({'status': 'invalid_request', 'message': '只读取原封存结果；不接受资金、日期或情景参数变更。'}, 400)
        try:
            payload = load_matched(root)
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            return local_response({'status': 'unavailable', 'message': '原对照记录缺失或校验未通过；请恢复原件后重试。'}, 503)
        return local_response(payload, 200)
