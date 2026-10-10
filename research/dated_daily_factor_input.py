"""A retained-current-master-qualified daily feature, never an M24 selection.

The original negative-return formula is unchanged. Whole-market completeness,
historical visibility and missing lookbacks remain explicit. No raw values go
to the aggregate result; the feature table stays in the native capture folder.
"""
from collections import Counter
from decimal import Decimal
from research.baostock_bulk_daily_gate import _daily_rows, DAILY_FIELDS
from research.bounded_baostock_universe_probe import validate_rows
from quantqc.ashare import board_of


def derive(day, basic, listed, daily):
    if (basic.get('schema') != 'bounded_baostock_universe_probe.v1'
            or listed.get('schema') != 'bounded_baostock_universe_probe.v1'
            or daily.get('schema') != 'bulk_astock_exploration.v1'):
        raise ValueError('source_schema_identity')
    if basic.get('query') != 'stock_basic' or basic.get('query_day') is not None:
        raise ValueError('retained_basic_identity')
    if listed.get('query') != 'all_stock' or listed.get('query_day') != day:
        raise ValueError('dated_list_identity')
    if (not listed.get('client_version')
            or listed['client_version'] != daily.get('client_version')):
        raise ValueError('new_source_client_identifier_mismatch')
    master = {r['code']: r for r in validate_rows('stock_basic', basic['fields'], basic['rows'])}
    members = {r['code']: r for r in validate_rows('all_stock', listed['fields'], listed['rows'])}
    status, shape = _daily_rows(daily, day)
    if not set(status) <= set(members):
        raise ValueError('daily_outside_dated_list')
    if any(status[c] != members[c]['tradeStatus'] for c in status):
        raise ValueError('dated_trade_status_mismatch')
    stocks = {c for c, r in master.items() if r['type'] == '1'}
    expected = stocks & set(members)
    observed = set(status)
    missing, extra = expected - observed, observed - expected
    counts = Counter(); features = []
    for values in sorted(daily['rows'], key=lambda r: r[1]):
        r = dict(zip(DAILY_FIELDS, values)); c = r['code']
        if c not in stocks:
            counts['not_typed_stock_in_retained_master'] += 1; continue
        # Source status is a current observation; never a historical-PIT flag.
        if r['tradestatus'] != '1':
            counts['suspended'] += 1; continue
        pct = Decimal(r['pctChg'])
        implied = (Decimal(r['close']) / Decimal(r['preclose']) - 1) * 100
        if abs(pct - implied) > Decimal('0.001'):
            raise ValueError('source_pct_preclose_inconsistent')
        board = board_of(c.split('.')[1])
        counts['board_' + str(board)] += 1
        if board not in {'main', 'gemb', 'star'}:
            counts['outside_A_share_code_scope'] += 1; continue
        if r['isST'] != '0':
            counts['ST_excluded'] += 1; continue
        if Decimal(r['volume']) <= 0 or Decimal(r['amount']) <= 0:
            counts['without_turnover_excluded'] += 1; continue
        if pct >= 0:
            counts['nonnegative_return_excluded'] += 1; continue
        counts['negative_return_feature_rows'] += 1
        features.append({'code': c, 'date': day, 'factor_id': 'm24_negative_daily_return_rank',
                         'value_fraction': str(-pct / 100), 'board': board,
                         'trade_board_in_user_scope': board in {'main', 'gemb'},
                         'full_M24_eligible': False, 'production_eligible': False})
    # No liquidity tie-break, candidate ranking or winner is invented here.
    aggregate = {'day': day, 'dated_security_rows': len(members),
                 'retained_basic_rows': len(master), 'retained_basic_typed_stock_rows': len(stocks),
                 'daily_rows': len(observed), 'expected_from_retained_current_master': len(expected),
                 'missing_relative_to_retained_master': len(missing),
                 'extra_relative_to_retained_master': len(extra),
                 'membership_exact_relative_to_retained_master': not (missing or extra),
                 'daily_shape_counts': shape, 'feature_counts': dict(sorted(counts.items())),
                 'source_pct_preclose_checked_on_retained_typed_active_stocks': True,
                 'new_source_client_identifier': daily['client_version'],
                 'retained_basic_client_identifier': basic.get('client_version'),
                 'old_basic_is_current_observation_not_PIT': True,
                 'full_A_verified': False, 'Beijing_coverage_verified': False,
                 'full_M24_selector_ready': False, 'model_admitted': False,
                 'missing_selector_inputs': ['median_amount20', 'adv20', 'history_sessions_at_least20',
                                             'source_vintage_PIT_identity', 'market_gate',
                                             'applicable_execution_day_limit', 'human_minute_execution'],
                 'new_formula_variants': 0, 'economic_runs': 0,
                 'generates_alert': False}
    return features, aggregate
