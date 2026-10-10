"""Bind one original reversal factor input to actual source clocks and gaps."""
from datetime import datetime, timezone, date
import math, re
from research.m24_baseline_preparation import REQUIRED

def aware(value):
    t=datetime.fromisoformat(str(value))
    if t.tzinfo is None or t.utcoffset() is None:raise ValueError('input_clock_timezone_required')
    return t.astimezone(timezone.utc)

def build_input(lookback, identity, *, computed_at):
    if lookback.get('schema')!='regis.original_M24_lookback_source_input.v1' or identity.get('schema')!='regis.single_symbol_current_metadata_raw.v1':
        raise ValueError('original_input_schema_mismatch')
    if not re.fullmatch(r'(sh|sz)\.[0-9]{6}',str(lookback.get('source_code',''))) or lookback.get('code')!=lookback['source_code'][3:]:
        raise ValueError('original_code_identity_mismatch')
    if not isinstance(identity.get('rows'),list) or len(identity['rows'])!=1:
        raise ValueError('single_current_identity_required')
    row=identity['rows'][0]
    if (identity.get('query_code')!=lookback['source_code'] or row.get('code')!=lookback['source_code']
        or row.get('type')!='1' or row.get('status')!='1'):
        raise ValueError('current_identity_join_mismatch')
    day=lookback.get('trade_date')
    if not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day:
        raise ValueError('original_signal_date_invalid')
    now=aware(computed_at)
    clocks={'lookback_computation':lookback.get('available_at'),'current_identity_receipt':identity.get('observed_at_utc')}
    if any(aware(v)>now for v in clocks.values()):raise ValueError('source_input_after_actual_computation')
    if now<aware(day+'T15:00:00+08:00'):raise ValueError('signal_day_not_completed')
    # Reuse original input units and formula. No refit or full-universe ranking.
    numeric={k:lookback.get(k) for k in ['close','pct_chg','median_amount20','adv20']}
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in numeric.values()):
        raise ValueError('original_numeric_input_invalid')
    if any(numeric[k]<=0 for k in ['close','median_amount20','adv20']) or type(lookback.get('history_sessions')) is not int or lookback['history_sessions']!=21:
        raise ValueError('original_21_session_input_required')
    if lookback.get('source_isST') not in ('0','1') or lookback.get('source_tradestatus') not in ('0','1'):
        raise ValueError('reported_flag_shape_invalid')
    fields={k:lookback[k] for k in ['code','trade_date','close','pct_chg','median_amount20','adv20','history_sessions']}
    fields['available_at']=now.isoformat()
    missing=sorted(REQUIRED-set(fields))
    authority={'research_only':True,'production_eligible':False,'model_admitted':False,'generates_alert':False,'orders_enabled':False,
               'historical_PIT_verified':False,'full_A_verified':False,'market_gate_ready':False,'permanent_identity_verified':False}
    native={'schema':'regis.current_original_reversal_input.v1','factor_id':'m24_negative_daily_return_rank',
            'formula':'-pct_chg/100','factor_value':-numeric['pct_chg']/100,'selector_input_fields':fields,
            'reported_source_isST':lookback['source_isST'],'reported_source_tradestatus':lookback['source_tradestatus'],
            'current_identity_role':'current_provider_stock_metadata_not_historical_PIT_or_permanent_id',
            'identity_observed_at':identity['observed_at_utc'],'source_clocks':clocks,'computed_at_utc':now.isoformat(),
            'available_at_role':'actual_feature_computation_not_backfilled_source_receipt',
            'missing_selector_fields':missing,'additional_blockers':['independent_verified_full_A_market_gate','complete_PIT_universe_and_actions','verified_exchange_entry_calendar','actual_quotes_manual_latency_and_fills'],
            'selector_status':'blocked_missing_required_evidence','selection_executed':False,'selected':None,
            'selection_empty_role':'not_evaluated_not_no_eligible_stocks','authority':authority}
    # Native receives numeric values; callers may publish only this finite metadata.
    public={'factor_id':native['factor_id'],'formula_variants':1,'current_input_rows':1,'factor_values_computed':1,
            'signal_day':day,'computed_at_utc':now.isoformat(),'source_clocks':clocks,'missing_selector_fields':missing,
            'additional_blockers':native['additional_blockers'],'selector_status':native['selector_status'],
            'selection_executed':False,'selected_count':None,'sample_is_not_full_universe_or_economic_trial':True,'authority':authority}
    return native,public
