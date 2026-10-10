"""Isolate known stock-data failures while retaining original lookback rules.

Integrity, calendar and programming failures remain fatal. This adapter neither
fills missing prices/turnover nor relaxes source or model admission.
"""
from decimal import InvalidOperation
from research.retained_current_lookback import lookback

DATA_REASONS=frozenset({
    "invalid_source_number","invalid_source_OHLC","negative_source_volume",
    "stock_window_missing_session","stock_window_state_flag",
    "stock_window_turnover_or_status","stock_window_pct_preclose_inconsistent",
    "lookback_amount_float_nonfinite","lookback_metric_float_nonfinite",
})


def evaluate_history(signal_day, history, current, calendar, *, source_code):
    """Return a usable research lookback or a value-free quarantined disposition."""
    reason=None
    try:
        native,summary=lookback(signal_day,history,current,calendar,source_code=source_code)
    except InvalidOperation:
        reason="original_numeric_parser_invalid"
    except ValueError as exc:
        if str(exc) not in DATA_REASONS:raise
        reason=str(exc)
    if reason is not None:
        return None,{"source_code":source_code,"signal_day":signal_day,
            "disposition":"quarantined_original_data_QA","reason":reason,
            "original_lookback_QA_passed":False,"factor_input_computed":False,
            "source_or_status_imputed":False,"model_admitted":False,
            "production_eligible":False,"generates_alert":False,"orders_enabled":False}
    return native,{"source_code":source_code,"signal_day":signal_day,
        "disposition":"original_lookback_QA_passed_research_only","reason":None,
        "original_lookback_QA_passed":True,"factor_input_computed":False,
        "lookback_summary":summary,"source_or_status_imputed":False,
        "model_admitted":False,"production_eligible":False,
        "generates_alert":False,"orders_enabled":False}
