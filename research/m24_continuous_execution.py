"""Continuous, bounded fictional M24 accounts; no real execution admission.

Reuse the selector, native snapshot, factor batch, fees and incremental ledger.
Intent reservations are distinct from fills. Only confirmed closure releases an
unfilled remainder. Journals contain full fixtures only under the native root;
the public entry returns counts and identities.
"""
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
import sys
from types import CodeType, FunctionType, ModuleType

import pandas as pd
from research import m24_strategy_execution_chain as chain

acct, rb, fills = chain.acct, chain.rb, chain.fills
_PATH = Path(__file__).resolve()
_PROTOCOL = _PATH.with_name('CONTINUOUS_STRATEGY_PROTOCOL_20261006.json')
_LIMITS = (64, 240, 2048)


def _current_binding(*, attest=False):
    source = chain._check_sources()
    raw = _PATH.read_bytes()
    compiled = set()
    if attest:
        pending = [compile(raw, str(_PATH), 'exec', dont_inherit=True, optimize=sys.flags.optimize)]
        while pending:
            code = pending.pop(); compiled.add(rb._code_hash(code))
            pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
    live = {}
    for name in _FUNCTIONS:
        fn = globals().get(name)
        if (not isinstance(fn, FunctionType) or fn.__module__ != __name__
                or fn.__qualname__ != name or attest and rb._code_hash(fn.__code__) not in compiled):
            raise rb.BatchIntegrityError('continuous_loaded_code_disk_mismatch')
        live[name] = rb._code_hash(fn.__code__)
    return {'chain': source, 'file': rb._hash(raw), 'live': live,
            'protocol': rb._hash(_PROTOCOL.read_bytes()), 'limits': list(_LIMITS)}


def _binding(expected=None):
    current = _current_binding()
    if current != _IMPORTED or expected is not None and current != expected:
        raise rb.BatchIntegrityError('continuous_source_drift')
    return current


def _step_references():
    """Freeze live callable/dependency identities; code objects are immutable.

    Full runtime/graph attestation still runs at each horizon and terminal. Each
    event checks every source byte and these live edges without rediscovering
    package metadata and normalizing hundreds of unchanged project paths.
    """
    rows = [*chain._function_rows(), *rb._functions()]
    rows += [(globals()[name], _PATH) for name in _FUNCTIONS]
    refs, codes, seen = [], [], set()
    for fn, _ in rows:
        if id(fn) in seen: continue
        seen.add(id(fn)); codes.append((fn, fn.__code__))
        names = fn.__code__.co_names
        for name in names:
            if name not in fn.__globals__: continue
            obj = fn.__globals__[name]; refs.append((fn.__globals__, name, obj))
            if (isinstance(obj, ModuleType) and vars(obj).get('__file__')
                    and rb._source_path(obj.__file__).is_relative_to(rb._PROJECT_ROOT)):
                namespace = vars(obj)
                refs.extend((namespace, n, namespace[n]) for n in names if n in namespace)
            elif isinstance(obj, type) and obj.__module__.startswith(('factor_system','research','quantqc')):
                namespace = vars(obj)
                refs.extend((namespace, n, namespace[n]) for n in names if n in namespace)
    return refs, codes


def _step_binding(expected):
    if expected != _IMPORTED:
        raise rb.BatchIntegrityError('continuous_source_drift')
    if (any(namespace.get(name) is not obj for namespace, name, obj in _STEP_REFS)
            or any(fn.__code__ is not code for fn, code in _STEP_CODES)):
        raise rb.BatchIntegrityError('continuous_loaded_code_drift')
    if (any(rb._hash(path.read_bytes()) != digest for path, digest in _STEP_FILES)
            or {'capital':str(acct.CAPITAL), 'horizons':list(chain.m24.HORIZONS),
                'costs':acct.COSTS.copy(), 'lot':chain.costs.LOT_SIZE,
                'minimum_commission':chain.costs.MIN_COMMISSION_YUAN} != expected['chain']['constants']
            or list(_LIMITS) != expected['limits']
            or {t.tpl_id:rb._plain(t.params()) for t in rb.TEMPLATES}
               != expected['chain']['factor_runtime']['template_domains']):
        raise rb.BatchIntegrityError('continuous_source_drift')


def _case_stream(case, spec):
    if (not isinstance(case, dict) or type(case.get('schema')) is not int or case['schema'] != 1
            or case.get('purpose') != 'engineering_only' or case.get('input_origin') != 'synthetic_fixture'
            or not isinstance(case.get('origins'), list) or not 1 <= len(case['origins']) <= _LIMITS[0]
            or not isinstance(case.get('events_by_horizon'), dict)
            or set(case['events_by_horizon']) != {'1', '3', '5'}):
        raise chain.ChainInputError('continuous_synthetic_case_required')
    sessions = case.get('sessions'); chain.m24._calendar(sessions)
    if not 8 <= len(sessions) <= _LIMITS[1]:
        raise chain.ChainInputError('continuous_calendar_cap')
    origins, origin_days = {}, []
    for origin in case['origins']:
        if not isinstance(origin, dict): raise chain.ChainInputError('continuous_origin_shape')
        ident = origin.get('origin_id'); decision = fills._time(origin.get('decision_at'), 'origin decision')
        day = decision.date().isoformat()
        if (not isinstance(ident, str) or not ident or ident in origins or day in origin_days
                or day not in sessions or day <= spec['periods']['validation'][1]):
            raise chain.ChainInputError('continuous_origin_identity_or_date')
        origins[ident] = origin; origin_days.append(day)
    if origin_days != sorted(origin_days): raise chain.ChainInputError('continuous_origins_not_ordered')
    streams = {}
    for hold in (1, 3, 5):
        events = case['events_by_horizon'][str(hold)]
        if not isinstance(events, list) or len(events) > _LIMITS[2]:
            raise chain.ChainInputError('continuous_event_cap')
        items = [(fills._time(o['decision_at'], 'origin'), {'kind':'origin', 'origin_id':ident})
                 for ident, o in origins.items()]
        seen, clocks = set(), set()
        for event in events:
            if not isinstance(event, dict): raise chain.ChainInputError('continuous_event_shape')
            ident = event.get('event_id'); at = fills._time(event.get('at'), 'event')
            if not isinstance(ident, str) or not ident or ident in seen:
                raise chain.ChainInputError('continuous_event_identity')
            if not sessions[0] <= at.date().isoformat() <= sessions[-1]:
                raise chain.ChainInputError('continuous_event_outside_calendar')
            if event.get('arm') not in {'strategy','etf'} or (event['arm'], at) in clocks:
                raise chain.ChainInputError('continuous_ambiguous_arm_clock')
            clocks.add((event['arm'], at))
            seen.add(ident); items.append((at, {'kind':'event', 'event':event}))
        # Origin controls and execution events are causal. End-of-day controls
        # follow every supplied event that day, including nontrading cash credit.
        grouped = {}
        for at, item in sorted(items, key=lambda p:(p[0], p[1]['kind'] != 'origin')):
            grouped.setdefault(at.date().isoformat(), []).append(item)
        streams[hold] = [item for day in sorted(set(sessions) | set(grouped))
                         for item in [*grouped.get(day, []), {'kind':'close', 'date':day}]]
    return origins, streams


def _new_flow(case, hold):
    declared = {(fills._time(e.get('at'), 'event').date().isoformat(), e.get('code'))
                for e in case['events_by_horizon'][str(hold)] if e.get('kind') == 'cash_dividend_entitlement'}
    return {'book':acct.start_fixture_accounts(sessions=case['sessions'],
                marks=chain._mapped_records(case['marks']),
                action_coverage=chain._mapped_records(case['action_coverage'], coverage=True),
                horizon=hold, input_origin='synthetic_fixture', declared_distributions=declared),
            'hold':hold, 'contexts':{}, 'orders':{}, 'origin_units':{}, 'bought':{},
            'original_exit_attempts':set(), 'identities':{}}


def _remaining(order):
    return order['requested'] - order['filled'] if order['status'] == 'pending' else 0


def _reserve(flow, arm, order):
    """Upper bound for arbitrarily split positive-integer fills at limit price.

    One cent per remaining share bounds each independently rounded gross and
    transfer fragment. Commission uses the existing cumulative per-order rule.
    This conservative envelope is an engineering assumption, not broker fees.
    """
    quantity = _remaining(order)
    if not quantity or order['side'] != 'buy': return Decimal(0)
    gross = acct.money(order['limit'] * quantity) + acct.CENT * quantity
    ledger = flow['book']['states'][arm]['orders'].get(order['order_id'])
    paid = ledger['commission'] if ledger else Decimal(0)
    actual = ledger['gross'] if ledger else Decimal(0)
    commission = max(Decimal(0), acct.money(max(Decimal(str(acct.MIN_COMMISSION_YUAN)),
        (actual + gross) * Decimal(str(acct.COSTS['commission'])))) - paid)
    transfer = acct.money(gross * Decimal(str(acct.COSTS['transfer']))) + acct.CENT * quantity if arm == 'strategy' else Decimal(0)
    return gross + commission + transfer


def _risk_reason(event):
    observed = fills._time(event['observed_at'], 'observed')
    risk = event.get('risk_observation')
    if not isinstance(risk, dict): return 'risk_unknown'
    if risk.get('status') not in {'open','weak','unknown'}: return 'risk_unknown'
    try: available = fills._time(risk.get('available_at'), 'risk availability')
    except (ValueError, TypeError): return 'risk_unknown'
    if (risk.get('trade_date') != observed.date().isoformat() or available.date() != observed.date()
            or available > observed or risk.get('full_population_verified') is not True
            or not isinstance(risk.get('source_sha256'), str)
            or not chain.re.fullmatch('[0-9a-f]{64}', risk['source_sha256'])):
        return 'risk_unknown_or_future'
    return None if risk['status'] == 'open' else 'risk_' + risk['status']


def _intent(flow, event):
    arm, code, side = event.get('arm'), event.get('code'), event.get('side')
    origin = event.get('origin_id'); context = flow['contexts'].get(origin)
    if context is None or arm not in {'strategy','etf'} or side not in {'buy','sell'}:
        raise chain.ChainInputError('continuous_unavailable_origin_or_arm')
    selection, plan = context['selection'], context['plan']
    selected = {d['code'] + ('.SH' if d['code'].startswith('6') else '.SZ'):d
                for d in plan['stock_reference_drafts']}
    if arm == 'strategy' and code not in selected or arm == 'etf' and code != acct.ETF:
        raise chain.ChainInputError('continuous_unselected_instrument')
    at = fills._time(event.get('at'), 'intent'); observed = fills._time(event.get('observed_at'), 'observed')
    decided = fills._time(event.get('decided_at'), 'decided'); submitted = fills._time(event.get('submitted_at'), 'submitted')
    if (not observed <= decided <= submitted == at
            or len({v.date() for v in (at, observed, decided, submitted)}) != 1):
        raise chain.ChainInputError('continuous_intent_causal_clock')
    day = at.date().isoformat(); ident = event.get('order_id'); key = (arm, ident)
    if not isinstance(ident, str) or not ident or key in flow['orders']:
        raise chain.ChainInputError('continuous_order_identity_reused')
    quantity = acct._qty(event.get('requested_shares'), 'requested shares')
    position = (origin, arm, code)
    if side == 'buy':
        if day != plan['entry_session'] or observed.time() < time(9,35) or quantity % 100:
            raise chain.ChainInputError('continuous_entry_session_time_or_lot')
        pending = sum(_remaining(o) for o in flow['orders'].values()
                      if o['position'] == position and o['side'] == 'buy')
        if arm == 'strategy' and quantity + pending + flow['bought'].get(position, 0) > selected[code]['reference_shares']:
            raise chain.ChainInputError('continuous_origin_reference_quantity_cap')
        limit = fills._number(event.get('limit_price'), 'limit price', positive=True)
    else:
        expected = plan['exit_sessions'][str(flow['hold'])]
        if day < expected or observed.time() < time(14,55) or at.time() >= time(14,57):
            raise chain.ChainInputError('continuous_wrong_horizon_exit')
        if day > expected and position not in flow['original_exit_attempts']:
            raise chain.ChainInputError('continuous_delayed_exit_without_original_attempt')
        pending = sum(_remaining(o) for o in flow['orders'].values()
                      if o['position'] == position and o['side'] == 'sell')
        available = flow['origin_units'].get(position, 0) - pending
        total_sellable = sum(q for bought,q in flow['book']['states'][arm]['lots'].get(code, []) if bought < day)
        all_pending = sum(_remaining(o) for (a,_),o in flow['orders'].items()
                          if a == arm and o['code'] == code and o['side'] == 'sell')
        if (quantity > available or quantity > total_sellable - all_pending
                or quantity % 100 and quantity != available):
            raise chain.ChainInputError('continuous_reserved_sell_inventory_or_T1')
        limit = Decimal(0)
    order = {'origin_id':origin, 'arm':arm, 'code':code, 'side':side, 'order_id':ident,
             'position':position, 'requested':quantity, 'filled':0, 'limit':limit, 'status':'pending',
             'submitted_at':event['submitted_at']}
    reason = None
    if side == 'buy':
        reason = _risk_reason(event)
        quote = event.get('proposal_quote')
        try:
            if (not isinstance(quote, dict) or quote.get('price_basis') != 'synthetic_limit_price'
                    or quote.get('trade_date') != day or fills._time(quote.get('available_at'), 'quote') > observed
                    or fills._time(quote.get('available_at'), 'quote').date() != observed.date()
                    or fills._number(quote.get('price'), 'quote', positive=True) != limit):
                reason = reason or 'proposal_quote_unknown_or_future'
        except (TypeError, ValueError): reason = reason or 'proposal_quote_unknown_or_future'
        state = flow['book']['states'][arm]
        reserved = sum(_reserve(flow, a, o) for (a,_),o in flow['orders'].items() if a == arm)
        cost = _reserve(flow, arm, order)
        if reserved + cost > state['cash']: reason = reason or 'insufficient_unreserved_cash'
        if arm == 'strategy':
            exposure = {}
            refs = event.get('reference_marks', {})
            for held, lots in state['lots'].items():
                if not lots: continue
                mark = refs.get(held) if isinstance(refs, dict) else None
                try:
                    if (not isinstance(mark, dict) or mark.get('price_basis') != 'synthetic_current_reference'
                            or mark.get('trade_date') != day or fills._time(mark.get('available_at'), 'reference') > observed):
                        reason = reason or 'held_reference_unknown_or_future'; continue
                    if fills._time(mark.get('available_at'), 'reference').date() != observed.date():
                        reason = reason or 'held_reference_unknown_or_future'; continue
                    exposure[held] = acct.money(fills._number(mark.get('price'), 'reference', positive=True) * sum(q for _,q in lots))
                except (ValueError, TypeError): reason = reason or 'held_reference_unknown_or_future'
            for (a,_), pending_order in flow['orders'].items():
                if a == arm and pending_order['side'] == 'buy' and _remaining(pending_order):
                    exposure[pending_order['code']] = exposure.get(pending_order['code'], Decimal(0)) + _reserve(flow, a, pending_order)
            exposure[code] = exposure.get(code, Decimal(0)) + cost
            identities = {held for held,lots in state['lots'].items() if lots} | set(exposure)
            if len(identities) > 2 or any(v > Decimal('30000') for v in exposure.values()) or sum(exposure.values()) > Decimal('60000'):
                reason = reason or 'continuous_stock_allocation_cap'
            stable = next(r['stable_instrument_id'] for r in selection['selected'] if r['code'] == code[:6])
            if code in flow['identities'] and flow['identities'][code] != stable:
                raise chain.ChainInputError('continuous_stable_identity_changed')
    if reason:
        order['status'] = 'blocked'; order['reason'] = reason
        flow['orders'][key] = order
        return {'outcome':'blocked_new_risk', 'reason':reason}
    acct.apply_fixture_event(flow['book'], event)
    flow['orders'][key] = order
    if arm == 'strategy' and side == 'buy': flow['identities'][code] = stable
    if side == 'sell' and day == expected: flow['original_exit_attempts'].add(position)
    return {'outcome':'intent_reserved'}


def _event(flow, event):
    kind = event.get('kind')
    if kind == 'order_intent': return _intent(flow, event)
    if kind in {'cash_dividend_entitlement','cash_dividend_credit'}:
        acct.apply_fixture_event(flow['book'], event); return {'outcome':kind}
    if kind not in {'fill','cancel_request','unfilled_order'}:
        raise chain.ChainInputError('continuous_event_kind')
    order = flow['orders'].get((event.get('arm'), event.get('order_id')))
    if order is None or order['status'] != 'pending' or event.get('origin_id') != order['origin_id']:
        raise chain.ChainInputError('continuous_event_without_pending_intent')
    if any(event.get(k) != order[k] for k in ('code','side','submitted_at')) or event.get('requested_shares') != order['requested']:
        raise chain.ChainInputError('continuous_order_binding_changed')
    quantity = 0
    if kind == 'fill':
        at = fills._time(event.get('at'), 'fill'); submitted = fills._time(order['submitted_at'], 'submitted')
        if at.date() != submitted.date(): raise chain.ChainInputError('continuous_previous_session_order_fill')
        quantity = acct._qty(event.get('shares'), 'fill shares')
        if order['side'] == 'sell' and at.time() >= time(14,57):
            raise chain.ChainInputError('continuous_exit_fill_after_cutoff')
        if order['side'] == 'buy' and fills._number(event.get('price'), 'price', positive=True) > order['limit']:
            raise chain.ChainInputError('continuous_fill_above_reserved_limit')
    acct.apply_fixture_event(flow['book'], event)
    if kind == 'fill':
        order['filled'] += quantity
        position = order['position']; direction = 1 if order['side'] == 'buy' else -1
        flow['origin_units'][position] = flow['origin_units'].get(position, 0) + direction * quantity
        if direction > 0: flow['bought'][position] = flow['bought'].get(position, 0) + quantity
        if not _remaining(order): order['status'] = 'filled'
    elif kind == 'unfilled_order': order['status'] = 'confirmed_closed'
    return {'outcome':kind, 'remaining_shares':_remaining(order)}


def _step(flow, item, origins, panel, case, spec):
    if item['kind'] == 'origin':
        origin = origins[item['origin_id']]; day = fills._time(origin['decision_at'], 'origin').date().isoformat()
        tail = case['sessions'][case['sessions'].index(day):][:32]
        selection, plan = chain._context(panel, {**origin, 'sessions':tail}, spec)
        flow['contexts'][item['origin_id']] = {'selection':selection, 'plan':plan}
        outcome = {'outcome':'origin_planned', 'selected_count':len(selection['selected'])}
    elif item['kind'] == 'close':
        acct.close_fixture_day(flow['book'], item['date']); outcome = {'outcome':'day_closed'}
    else: outcome = _event(flow, item['event'])
    outcome['reserved_cash_yuan'] = {arm:str(sum(_reserve(flow, a, o) for (a,_),o in flow['orders'].items() if a == arm))
                                    for arm in ('strategy','etf')}
    if item['kind'] == 'close' and item['date'] == case['sessions'][-1]:
        outcome['terminal_material_sha256'] = rb._hash(rb._body(rb._plain(_computed(flow))).encode())
    return outcome


def _computed(flow):
    return {'state':'computed_engineering', 'book':acct.finish_fixture_accounts(flow['book']),
            'origins':flow['contexts'], 'origin_units':[{'origin_id':o,'arm':a,'code':c,'shares':q}
               for (o,a,c),q in flow['origin_units'].items()],
            'pending_orders':[{'arm':a,'order_id':ident,'remaining_shares':_remaining(order),
                'reserved_yuan':str(_reserve(flow,a,order))}
                for (a,ident),order in flow['orders'].items() if _remaining(order)],
            'blocked_orders':[{'arm':a,'order_id':ident,'reason':order['reason']}
                for (a,ident),order in flow['orders'].items() if order['status']=='blocked']}


def _terminal_valid(result, rows, stream, origins, case, hold):
    """Validate completeness and bind cached payload to the final committed trace."""
    if result['state'] == 'compute_failed':
        return (isinstance(result.get('reason'), str) and bool(result['reason'])
                and not any(k in result for k in ('book','origins','origin_units','pending_orders','blocked_orders')))
    if len(rows) != len(stream) or not rows: return False
    book = result.get('book'); contexts = result.get('origins')
    if (not isinstance(book, dict) or not isinstance(contexts, dict) or set(contexts) != set(origins)
            or book.get('schema') != 'regis.m24.fixture_accounting.v1'
            or book.get('input_origin') != 'synthetic_fixture' or book.get('horizon') != hold
            or book.get('initial_yuan_per_arm') != 150000
            or book.get('capital_relationship') != 'alternative_accounts_never_sum'
            or book.get('authority') != {'economic_run_enabled':False,'model_admitted':False,
                                        'orders_enabled':False,'actual_fill_evidence':False}): return False
    daily = book.get('daily')
    closed_days = [item['date'] for item in stream if item['kind']=='close']
    if (not isinstance(daily, list) or [d.get('date') for d in daily if isinstance(d, dict)] != closed_days
            or any(d.get('is_trading_session') is not (d['date'] in case['sessions']) for d in daily)
            or any(not isinstance(d.get('accounts'),dict) or set(d['accounts']) != {'cash','strategy','etf'} for d in daily)
            or any(not isinstance(c,dict) or set(c) != {'selection','plan'} for c in contexts.values())): return False
    payload = {k:v for k,v in result.items() if k not in
               {'identity','trace_head','committed_steps','model_admitted','orders_enabled'}}
    return json.loads(rows[-1][2]).get('terminal_material_sha256') == rb._hash(rb._body(payload).encode())


_DDL = """
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,frozen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,batch TEXT,hold INTEGER,state TEXT,started TEXT,ended TEXT);
CREATE TABLE IF NOT EXISTS event_journal(batch TEXT,hold INTEGER,seq INTEGER,input_sha TEXT,trace TEXT,sha TEXT,previous_sha TEXT,PRIMARY KEY(batch,hold,seq));
CREATE TABLE IF NOT EXISTS results(batch TEXT,hold INTEGER,result TEXT,sha TEXT,PRIMARY KEY(batch,hold));
CREATE TRIGGER IF NOT EXISTS case_no_update BEFORE UPDATE ON cases BEGIN SELECT RAISE(ABORT,'immutable continuous case'); END;
CREATE TRIGGER IF NOT EXISTS case_no_delete BEFORE DELETE ON cases BEGIN SELECT RAISE(ABORT,'immutable continuous case'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON event_journal BEGIN SELECT RAISE(ABORT,'immutable continuous journal'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON event_journal BEGIN SELECT RAISE(ABORT,'immutable continuous journal'); END;
CREATE TRIGGER IF NOT EXISTS result_no_update BEFORE UPDATE ON results BEGIN SELECT RAISE(ABORT,'immutable continuous terminal'); END;
CREATE TRIGGER IF NOT EXISTS result_no_delete BEFORE DELETE ON results BEGIN SELECT RAISE(ABORT,'immutable continuous terminal'); END;
"""


def _prefix(db, batch, hold, stream, frozen_sha):
    rows = db.execute('SELECT seq,input_sha,trace,sha,previous_sha FROM event_journal WHERE batch=? AND hold=? ORDER BY seq', (batch,hold)).fetchall()
    head = rb._hash(rb._body({'frozen':frozen_sha,'hold':hold}).encode())
    for index, (seq, source, trace, digest, previous) in enumerate(rows):
        if (index >= len(stream) or seq != index or source != rb._hash(rb._body(stream[index]).encode())
                or previous != head or digest != rb._hash(rb._body({'input':source,'trace':trace,'previous':head}).encode())):
            raise rb.BatchIntegrityError('continuous_journal_prefix_tampered')
        head = digest
    return rows, head


def run_continuous_chain(root, factor_spec, case_reference):
    binding = _binding(); spec = rb._specification(factor_spec)
    case = chain._physical(root, case_reference, decode=True)
    origins, streams = _case_stream(case, spec)
    root = chain._native_root(root); directory = root/'engineering-continuous-strategy-v1'
    rb._unlinked(directory); directory.mkdir(exist_ok=True); directory = chain._native_root(directory)
    dbpath = directory/'journal.sqlite3'; rb._unlinked(dbpath)
    frozen = rb._body({'spec':spec,'case_reference':case_reference,'binding':binding})
    batch = rb._hash(rb._body({'spec':spec,'case_reference':case_reference}).encode()); frozen_sha = rb._hash(frozen.encode())
    executed = reused = failed = processed = restored = 0
    with rb._writer(directory):
        db = sqlite3.connect(dbpath, timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL'); db.executescript(_DDL)
            with db:
                db.execute('INSERT OR IGNORE INTO cases(id,frozen) VALUES(?,?)',(batch,frozen))
                if db.execute('SELECT frozen FROM cases WHERE id=?',(batch,)).fetchone()[0] != frozen:
                    raise rb.BatchIntegrityError('continuous_frozen_drift')
                db.execute("UPDATE attempts SET state='interrupted',ended=? WHERE batch=? AND state='running'",(datetime.now(timezone.utc).isoformat(),batch))
            # Exactly one shared batch and one panel load for all origins/horizons.
            factor_result = rb.run_batch(root, spec)
            panel = chain.load_native_panel_snapshot(root, **spec['snapshot'])
            input_error = None
            try:
                for (day, code), mark in chain._mapped_records(case['marks']).items():
                    chain._stock_mark(panel,day,code,mark,error='continuous_mark_snapshot_mismatch')
            except (ValueError, TypeError, KeyError) as exc: input_error = exc
            for hold in (1,3,5):
                _binding(binding); chain._physical(root, case_reference)
                rows, head = _prefix(db, batch, hold, streams[hold], frozen_sha)
                prior = db.execute('SELECT result,sha FROM results WHERE batch=? AND hold=?',(batch,hold)).fetchone()
                if prior:
                    result = json.loads(prior[0])
                    if (rb._hash(prior[0].encode()) != prior[1] or result.get('identity') != {'frozen_sha256':frozen_sha,'hold':hold}
                            or result.get('trace_head') != head or result.get('committed_steps') != len(rows)
                            or result.get('state') not in {'computed_engineering','compute_failed'}
                            or result.get('model_admitted') is not False or result.get('orders_enabled') is not False
                            or not _terminal_valid(result, rows, streams[hold], origins, case, hold)):
                        raise rb.BatchIntegrityError('continuous_terminal_tampered')
                    reused += 1; failed += result['state'] == 'compute_failed'; continue
                with db:
                    attempt = db.execute("INSERT INTO attempts(batch,hold,state,started) VALUES(?,?,'running',?)",(batch,hold,datetime.now(timezone.utc).isoformat())).lastrowid
                try:
                    if input_error: raise input_error
                    flow = _new_flow(case, hold)
                    # Only a restart restores its committed prefix; no replay per origin.
                    for index, row in enumerate(rows):
                        actual = rb._body(_step(flow, streams[hold][index], origins, panel, case, spec))
                        if actual != row[2]: raise rb.BatchIntegrityError('continuous_restored_prefix_mismatch')
                        restored += 1
                    for index in range(len(rows), len(streams[hold])):
                        item = streams[hold][index]; source = rb._hash(rb._body(item).encode())
                        trace = rb._body(_step(flow, item, origins, panel, case, spec))
                        digest = rb._hash(rb._body({'input':source,'trace':trace,'previous':head}).encode())
                        with db:
                            _step_binding(binding)
                            db.execute('INSERT INTO event_journal VALUES(?,?,?,?,?,?,?)',(batch,hold,index,source,trace,digest,head))
                        head = digest; processed += 1
                    result = _computed(flow)
                except rb.BatchIntegrityError: raise
                except (ValueError, TypeError, KeyError) as exc:
                    result = {'state':'compute_failed','reason':type(exc).__name__+':'+str(exc)}; failed += 1
                count = db.execute('SELECT COUNT(*) FROM event_journal WHERE batch=? AND hold=?',(batch,hold)).fetchone()[0]
                result.update({'identity':{'frozen_sha256':frozen_sha,'hold':hold},'trace_head':head,
                               'committed_steps':count,'model_admitted':False,'orders_enabled':False})
                encoded = rb._body(rb._plain(result))
                with db:
                    _binding(binding); chain._physical(root, case_reference); chain._physical(root, spec['snapshot'])
                    db.execute('INSERT INTO results VALUES(?,?,?,?)',(batch,hold,encoded,rb._hash(encoded.encode())))
                    db.execute("UPDATE attempts SET state='terminal',ended=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),attempt))
                executed += 1
            _binding(binding); chain._physical(root, case_reference); chain._physical(root, spec['snapshot'])
            return {'case_id':batch,'horizons':3,'alternative_arms_per_horizon':3,
                    'origins':len(origins),'executed':executed,'terminal_reused':reused,'compute_failed':failed,
                    'processed_steps':processed,'restored_steps':restored,'factor_trials':factor_result['trials'],
                    'factor_failures':factor_result['compute_failed'],'model_admitted':False,'real_source_qualified':False,
                    'real_execution_verified':False,'whole_R4_complete':False,'orders_enabled':False}
        finally: db.close()


_FUNCTIONS = tuple(name for name, fn in tuple(globals().items()) if isinstance(fn, FunctionType) and fn.__module__ == __name__)
_IMPORTED = _current_binding(attest=True)
_STEP_REFS, _STEP_CODES = _step_references()
_STEP_FILES = [(rb._PROJECT_ROOT/path, digest) for path,digest in _IMPORTED['chain']['files'].items()]
_STEP_FILES += [(_PATH,_IMPORTED['file']),(_PROTOCOL,_IMPORTED['protocol'])]
