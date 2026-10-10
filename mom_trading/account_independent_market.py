"""Historical market context for research pages, without account/runtime state."""
from __future__ import annotations


def historical_context(*, load_base, latest_observation, adapt_observation):
    base = load_base()
    if not isinstance(base, dict):
        raise ValueError('invalid_research_context')
    observation = latest_observation(base.get('model_observation'))
    if not isinstance(observation, dict):
        raise ValueError('invalid_research_observation')
    route = adapt_observation(observation, as_of=observation.get('trade_date'))
    if not isinstance(route, dict):
        raise ValueError('invalid_research_route')
    # Deliberately project only the fields the research page consumes. Do not
    # return base payloads, opportunity caches, monitoring or account health.
    state = route.get('state')
    if route.get('status') != 'ready' or state not in {
        'risk_closed', 'weak_repair', 'sentiment_rising', 'crowded_climax', 'retreat'
    }:
        state = 'unknown'
    as_of = route.get('as_of')
    if not isinstance(as_of, str) or not as_of:
        state, as_of = 'unknown', None
    return {
        'schema': 'regis.account_independent_market.v1',
        'status': 'historical_context' if state != 'unknown' else 'evidence_insufficient',
        'as_of': as_of,
        'decision_state': {'state': state, 'as_of': as_of,
                           'status': 'historical_research_only',
                           'applied_to_execution': False, 'production_strategy': False},
        'account_access': False, 'research_only': True,
        'current_market_freshness_verified': False,
        'production_eligible': False, 'generates_alert': False,
        'automatic_orders': False,
        'note': '仅展示原记录日期的历史研究状态；未核实时新鲜度，不读取账户，不授权交易。',
    }
