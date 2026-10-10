"""One native engineering origin: factors, causal M24 plans, supplied fills, books.

Fixture arithmetic and linkage only. No real source admission, fill fabrication,
orders, legacy registry, account access or price/ledger output outside native.
"""
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import inspect
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import sys
from types import CodeType, FunctionType
import pandas as pd
from factor_system import research_batch as rb
from factor_system.data import raw_close_frame, limit_reference_frame
from factor_system.native_snapshot import _native_root, _no_links, _identity, _path_identity, load_native_panel_snapshot
from research import m24_baseline_preparation as m24, m24_accounting as acct
from research import target_a_confirmed_fill_replay as fills
from quantqc import costs, ashare


class ChainInputError(ValueError):
    pass


def _function_rows():
    rows = []
    for module in (sys.modules[__name__], m24, acct, fills, costs, ashare):
        for fn in vars(module).values():
            if isinstance(fn, FunctionType) and not fn.__code__.co_filename.startswith('<'):
                path = rb._source_path(fn.__code__.co_filename)
                if path.is_relative_to(rb._PROJECT_ROOT): rows.append((fn, path))
    return rows


def _sources():
    original = rb._check_sources()
    paths = {path for _, path in _function_rows()}
    paths |= {rb._PROJECT_ROOT / ('factor_system/' + p if '/' not in p else p)
              for p in original['files']}
    paths |= {rb._PROJECT_ROOT / p for p in ('QUANT_RESEARCH_M24_PROTOCOL_20261001.json',
              'research/GOAL_PARALLEL_PLAYBOOK_REGISTRATION_20261002.json',
              'research/STRATEGY_EXECUTION_CHAIN_PROTOCOL_20261005.json')}
    files = {path.relative_to(rb._PROJECT_ROOT).as_posix(): rb._hash(path.read_bytes()) for path in paths}
    live = {path.relative_to(rb._PROJECT_ROOT).as_posix() + ':' + fn.__qualname__ + ':' + str(fn.__code__.co_firstlineno):
            rb._code_hash(fn.__code__) for fn, path in _function_rows()}
    return {'files':files, 'live':live, 'factor_runtime':original,
            'constants':{'capital':str(acct.CAPITAL), 'horizons':list(m24.HORIZONS),
                         'costs':acct.COSTS.copy(), 'lot':costs.LOT_SIZE,
                         'minimum_commission':costs.MIN_COMMISSION_YUAN}}


def _attest():
    compiled = {}
    for fn, path in _function_rows():
        if path not in compiled:
            pending = [compile(path.read_bytes(), str(path), 'exec', dont_inherit=True, optimize=sys.flags.optimize)]
            codes = set()
            while pending:
                code = pending.pop();codes.add(rb._code_hash(code))
                pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
            compiled[path] = codes
        if rb._code_hash(fn.__code__) not in compiled[path]:
            raise rb.BatchIntegrityError('chain_loaded_code_disk_mismatch')


def _check_sources(expected=None):
    current = _sources()
    if current != _IMPORTED_SOURCES or expected is not None and current != expected:
        raise rb.BatchIntegrityError('chain_source_drift')
    return current


def _physical(root, ref, *, decode=False):
    if not isinstance(ref, dict) or set(ref) != {'relative','sha256'}:
        raise ChainInputError('chain_file_reference_invalid')
    relative, expected = ref['relative'],ref['sha256']
    if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):
        raise ChainInputError('chain_file_sha_invalid')
    if not isinstance(relative,str) or not relative:
        raise ChainInputError('chain_file_path_invalid')
    parts = relative.replace('\\','/').split('/')
    if (PureWindowsPath(relative).drive or relative.startswith(('/','\\'))
            or any(p in {'','.', '..'} or p != p.rstrip(' .') or ':' in p or '\0' in p for p in parts)
            or decode and Path(relative).suffix.lower() != '.json'):
        raise ChainInputError('chain_file_path_invalid')
    base = _native_root(root);target=base/Path(*parts)
    _no_links(base,Path(*parts));resolved=target.resolve(strict=True);resolved.relative_to(base)
    path_before=os.lstat(target)
    if not stat.S_ISREG(path_before.st_mode): raise ChainInputError('chain_file_not_regular')
    fd=os.open(resolved,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _path_identity(before)!=_path_identity(path_before):
            raise rb.BatchIntegrityError('chain_file_identity_changed')
        if decode and before.st_size>8*1024*1024: raise ChainInputError('chain_case_size_cap')
        digest=hashlib.sha256();chunks=[]
        while chunk:=os.read(fd,1024*1024):
            digest.update(chunk)
            if decode:chunks.append(chunk)
        _no_links(base,Path(*parts))
        if (_identity(before)!=_identity(os.fstat(fd)) or _path_identity(before)!=_path_identity(os.lstat(target))
                or target.resolve(strict=True)!=resolved):
            raise rb.BatchIntegrityError('chain_file_changed')
        if digest.hexdigest()!=expected:raise rb.BatchIntegrityError('chain_file_sha_mismatch')
    finally:os.close(fd)
    if not decode:return None
    def unique(pairs):
        out={}
        for key,value in pairs:
            if key in out:raise ChainInputError('chain_case_duplicate_field')
            out[key]=value
        return out
    def nonfinite(token):raise ChainInputError('chain_case_nonfinite_json')
    return json.loads(b''.join(chunks),object_pairs_hook=unique,parse_constant=nonfinite)


def _fact(value):
    if type(value) not in (int,float,str):raise ChainInputError('chain_numeric_fact_type')
    try:result=Decimal(str(value))
    except (ValueError,InvalidOperation) as exc:raise ChainInputError('chain_numeric_fact_invalid') from exc
    if not result.is_finite():raise ChainInputError('chain_numeric_fact_nonfinite')
    return result


def _raw_prices(panel):
    try:
        return raw_close_frame(panel)
    except (ValueError, TypeError) as exc:
        raise ChainInputError("chain_raw_price_evidence_invalid") from exc


def _stock_mark(panel, day, code, mark, *, error):
    if code == acct.ETF:
        return
    raw = _raw_prices(panel)
    base = code.split('.')[0]
    if (base not in raw.columns or pd.Timestamp(day) not in raw.index
            or _fact(mark['close']) != _fact(float(raw.loc[day, base]))):
        raise ChainInputError(error)


def _context(panel, case, factor_spec):
    sessions=case['sessions'];m24._calendar(sessions)
    if not 8<=len(sessions)<=32:raise ChainInputError('chain_calendar_cap_or_incomplete')
    decision=fills._time(case['decision_at'],'decision')
    day=case.get('signal_session',decision.date().isoformat())
    selector_clock={}
    if 'signal_session' in case:
        if not isinstance(day,str):raise ChainInputError('chain_signal_session_invalid')
        selector_clock={'signal_session':day,'sessions':sessions}
    if day!=sessions[0] or day<=factor_spec['periods']['validation'][1]:
        raise ChainInputError('chain_origin_not_after_validation')
    rows=case['selector_rows']
    if not isinstance(rows,list) or len(rows)>5500:raise ChainInputError('chain_selector_population_cap')
    if case['market_gate'].get('open') is False:
        # The mature selector still validates the closed gate's clock/source.
        selection=m24.select_reversal(pd.DataFrame(),decision_at=case['decision_at'],market_gate=case['market_gate'],**selector_clock)
        plan=m24.prepare_horizon_drafts(selection,sessions=sessions,decision_day=day)
        if plan['status']!='conditional_drafts_only':raise ChainInputError('chain_plan_incomplete')
        return selection,plan
    raw=_raw_prices(panel)
    limit_reference=None
    flags=('universe_member','is_st','trade_status','adjustment_trusted','factor_changed','no_price_limit')
    for row in rows:
        if not isinstance(row,dict) or any(type(row.get(k)) is not bool for k in flags):
            raise ChainInputError('chain_unknown_or_invalid_state')
        code=row.get('code')
        if code not in panel.close.columns or pd.Timestamp(day) not in panel.close.index:
            raise ChainInputError('chain_snapshot_identity_or_date')
        for field,frame in [('close',raw),('pct_chg',panel.pct_chg)]:
            if frame is None or _fact(row.get(field))!=_fact(float(frame.loc[day,code])):
                raise ChainInputError('chain_selector_snapshot_value_mismatch')
        history=panel.close.loc[:day,code]
        if _fact(row.get('history_sessions'))!=int(history.notna().sum()):
            raise ChainInputError('chain_selector_history_mismatch')
        if panel.amount is None:raise ChainInputError('chain_amount_input_missing')
        amounts=panel.amount.loc[:day,code].tail(20)
        lagged=panel.amount.loc[panel.amount.index<pd.Timestamp(day),code].tail(20)
        if len(amounts)!=20 or len(lagged)!=20 or amounts.isna().any() or lagged.isna().any():
            raise ChainInputError('chain_amount_history_incomplete')
        for field,value in [('median_amount20',amounts.median()),('adv20',lagged.mean())]:
            if _fact(row.get(field))!=_fact(float(value)):
                raise ChainInputError('chain_selector_snapshot_liquidity_mismatch')
        for field,frame in [('trade_status',panel.tradable),('is_st',panel.is_st_panel)]:
            if frame is None or type(frame.loc[day,code]).__name__ not in {'bool','bool_'} or row[field]!=bool(frame.loc[day,code]):
                raise ChainInputError('chain_snapshot_state_unknown_or_mismatch')
        if str(row.get('trade_date'))!=day:raise ChainInputError('chain_selector_date_mismatch')
        normal=(ashare.board_of(code) in {'main','gemb'} and row['universe_member']
                and row['trade_status'] and not row['is_st'] and row['adjustment_trusted']
                and not row['factor_changed'] and not row['no_price_limit'])
        if normal:
            if limit_reference is None:
                try:limit_reference=limit_reference_frame(panel)
                except (TypeError,ValueError) as exc:
                    raise ChainInputError('chain_execution_limit_reference_invalid') from exc
            reference=_fact(float(limit_reference.loc[pd.Timestamp(day),code]))
            rule=ashare.price_limit_rule(code,trade_date=day,is_risk_warning=False,no_price_limit=False)
            if rule.get('valid') is not True or rule.get('rate') is None:
                raise ChainInputError('chain_price_limit_rule_unverified')
            expected=ashare._limit_price(str(reference),float(rule['rate']),'down',
                minimum_tick_rule_verified=ashare._minimum_tick_rule_verified(code,day))
            if expected is None:raise ChainInputError('chain_execution_reference_or_historical_rule_unverified')
            if _fact(row.get('lower_limit_yuan'))!=_fact(expected):
                raise ChainInputError('chain_lower_limit_snapshot_reference_mismatch')
    selection=m24.select_reversal(pd.DataFrame(rows),decision_at=case['decision_at'],market_gate=case['market_gate'],**selector_clock)
    if selection['status'] not in {'conditional_research_candidates','no_new_risk_cash'}:
        raise ChainInputError('chain_selector_incomplete')
    plan=m24.prepare_horizon_drafts(selection,sessions=sessions,decision_day=day)
    if plan['status']!='conditional_drafts_only':raise ChainInputError('chain_plan_incomplete')
    return selection,plan


def _mapped_records(rows, *, coverage=False):
    if not isinstance(rows,list) or len(rows)>100000:raise ChainInputError('chain_records_invalid')
    mapped={}
    for row in rows:
        if not isinstance(row,dict):raise ChainInputError('chain_record_invalid')
        key=(row.get('date'),row.get('code'))
        if not all(isinstance(x,str) and x for x in key) or key in mapped:
            raise ChainInputError('chain_record_identity_duplicate_or_invalid')
        if coverage:
            actions=row.get('actions')
            if not isinstance(actions,list) or len(actions)!=len(set(actions)) or any(not isinstance(x,str) for x in actions):
                raise ChainInputError('chain_action_coverage_invalid')
            mapped[key]=set(actions)
        else:mapped[key]={'close':row.get('close'),'price_basis':row.get('price_basis')}
    return mapped


def _events(case, plan, hold):
    events=case['events_by_horizon'][str(hold)]
    if not isinstance(events,list) or len(events)>256 or any(not isinstance(e,dict) for e in events):
        raise ChainInputError('chain_event_cap_or_shape')
    selected={d['code']+('.SH' if d['code'].startswith('6') else '.SZ'):d for d in plan['stock_reference_drafts']}
    delayed=set();entry_orders={};buy_shares={};buy_spend={};fee_orders={};active={};filled={}
    for e in sorted(events,key=lambda x:fills._time(x.get('at'),'event')):
        if e.get('kind') not in {'fill','unfilled_order'}:continue  # retained accounting validates supported action events
        at=fills._time(e.get('at'),'event');observed=fills._time(e.get('observed_at'),'observed')
        decided=fills._time(e.get('decided_at'),'decided');submitted=fills._time(e.get('submitted_at'),'submitted')
        if not observed<=decided<=submitted<=at or len({t.date() for t in (observed,decided,submitted,at)})!=1:
            raise ChainInputError('chain_observe_decide_submit_fill_order')
        arm,code,side=e.get('arm'),e.get('code'),e.get('side');key=(arm,code)
        if arm not in {'strategy','etf'} or (arm=='strategy' and code not in selected) or (arm=='etf' and code!=acct.ETF):
            raise ChainInputError('chain_unselected_or_wrong_arm')
        if side=='buy':
            if at.date().isoformat()!=plan['entry_session'] or observed.time()<time(9,35):
                raise ChainInputError('chain_wrong_entry_session_or_time')
            order=e.get('order_id')
            if key in entry_orders and entry_orders[key]!=order:raise ChainInputError('chain_multiple_entry_orders')
            entry_orders[key]=order
            if arm=='strategy' and e.get('requested_shares',0)>selected[code]['reference_shares']:
                raise ChainInputError('chain_conditional_quantity_cap')
        elif side=='sell':
            expected=plan['exit_sessions'][str(hold)]
            if at.date().isoformat()<expected or observed.time()<time(14,55) or at.time()>=time(14,57):
                raise ChainInputError('chain_wrong_horizon_exit_or_time')
            if at.date().isoformat()>expected and key not in delayed:
                raise ChainInputError('chain_delayed_exit_without_original_attempt')
            if at.date().isoformat()==expected and (e['kind']=='unfilled_order' or e.get('shares',0)<e.get('requested_shares',0)):
                delayed.add(key)
        else:raise ChainInputError('chain_side_invalid')
        live_key=(arm,code,side);order_id=e.get('order_id')
        if not isinstance(order_id,str) or not order_id:raise ChainInputError('chain_order_identity_invalid')
        if live_key in active and active[live_key]!=order_id:
            raise ChainInputError('chain_open_order_must_resolve_before_retry')
        active[live_key]=order_id
        if e['kind']=='unfilled_order':active.pop(live_key,None)
        else:
            requested=acct._qty(e.get('requested_shares'),'requested shares')
            quantity=acct._qty(e.get('shares'),'filled shares')
            identity=(arm,order_id)  # alternative accounts may reuse an order ID
            filled[identity]=filled.get(identity,0)+quantity
            if filled[identity]>=requested:active.pop(live_key,None)
        if e['kind']=='fill' and side=='buy' and arm=='strategy':
            shares=fills._number(e.get('shares'),'shares',positive=True)
            gross=acct.money(fills._number(e.get('price'),'fill price',positive=True)*shares)
            order=fee_orders.setdefault(e.get('order_id'),{'gross':Decimal(0),'commission':Decimal(0)})
            fee=sum(acct._fees(arm,side,gross,order).values())
            buy_spend[code]=buy_spend.get(code,Decimal(0))+gross+fee
            buy_shares[code]=buy_shares.get(code,Decimal(0))+shares
            if buy_spend[code]>Decimal('30000') or sum(buy_spend.values())>Decimal('60000'):
                raise ChainInputError('chain_stock_allocation_cap')
            if buy_shares[code]>selected[code]['reference_shares']:raise ChainInputError('chain_conditional_quantity_cap')
    return events


_DDL="""
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,frozen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,batch TEXT,hold INTEGER,state TEXT,started TEXT,ended TEXT);
CREATE TABLE IF NOT EXISTS results(batch TEXT,hold INTEGER,result TEXT,sha TEXT,PRIMARY KEY(batch,hold));
"""


def run_engineering_chain(root, factor_spec, case_reference):
    """Metadata counts only; native full fixtures never certify actual financial use."""
    source=_check_sources();spec=rb._specification(factor_spec)
    case=_physical(root,case_reference,decode=True)
    if (not isinstance(case,dict) or type(case.get('schema')) is not int or case['schema']!=1
            or case.get('purpose')!='engineering_only' or case.get('input_origin')!='synthetic_fixture'
            or not isinstance(case.get('events_by_horizon'),dict) or set(case['events_by_horizon'])!={'1','3','5'}):
        raise ChainInputError('chain_engineering_fixture_contract_required')
    root=_native_root(root);directory=root/'engineering-strategy-chain-v1'
    rb._unlinked(directory);directory.mkdir(exist_ok=True);directory=_native_root(directory)
    dbpath=directory/'chain-journal.sqlite3';rb._unlinked(dbpath)
    frozen=rb._body({'factor_spec':spec,'case_reference':case_reference,'source':source})
    batch=rb._hash(rb._body({'factor_spec':spec,'case_reference':case_reference}).encode())
    with rb._writer(directory):
        db=sqlite3.connect(dbpath,timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL');db.executescript(_DDL)
            with db:
                db.execute('INSERT OR IGNORE INTO cases(id,frozen) VALUES(?,?)',(batch,frozen))
                if db.execute('SELECT frozen FROM cases WHERE id=?',(batch,)).fetchone()[0]!=frozen:
                    raise rb.BatchIntegrityError('chain_frozen_drift')
                db.execute("UPDATE attempts SET state='interrupted',ended=? WHERE batch=? AND state='running'",(datetime.now(timezone.utc).isoformat(),batch))
            factor_result=rb.run_batch(root,spec)
            panel=load_native_panel_snapshot(root,**spec['snapshot'])
            error=None
            try:
                selection,plan=_context(panel,case,spec)
                marks=_mapped_records(case['marks']);coverage=_mapped_records(case['action_coverage'],coverage=True)
                for (day,code),mark in marks.items():
                    _stock_mark(panel,day,code,mark,error='chain_mark_snapshot_value_mismatch')
            except (ValueError,TypeError,KeyError) as exc:error=exc
            executed=reused=failed=0
            for hold in (1,3,5):
                _check_sources(source);_physical(root,case_reference);_physical(root,{'relative':spec['snapshot']['relative'],'sha256':spec['snapshot']['sha256']})
                prior=db.execute('SELECT result,sha FROM results WHERE batch=? AND hold=?',(batch,hold)).fetchone()
                if prior:
                    if rb._hash(prior[0].encode())!=prior[1]:raise rb.BatchIntegrityError('chain_terminal_sha_mismatch')
                    result=json.loads(prior[0])
                    if result['identity']!={'frozen_sha256':rb._hash(frozen.encode()),'hold':hold}:
                        raise rb.BatchIntegrityError('chain_terminal_identity_mismatch')
                    reused+=1;failed+=result['state']=='compute_failed';continue
                with db:
                    cursor=db.execute("INSERT INTO attempts(batch,hold,state,started) VALUES(?,?,'running',?)",(batch,hold,datetime.now(timezone.utc).isoformat()))
                try:
                    if error:raise error
                    events=_events(case,plan,hold)
                    book=acct.replay_fixture_accounts(sessions=case['sessions'],marks=marks,events=events,action_coverage=coverage,horizon=hold,input_origin='synthetic_fixture')
                    result={'state':'computed_engineering','selection':selection,'plan':plan,'book':book,'model_admitted':False}
                except (ValueError,TypeError,KeyError) as exc:
                    result={'state':'compute_failed','reason':type(exc).__name__+':'+str(exc),'model_admitted':False};failed+=1
                result['identity']={'frozen_sha256':rb._hash(frozen.encode()),'hold':hold}
                encoded=rb._body(rb._plain(result))
                with db:
                    _check_sources(source);_physical(root,case_reference);_physical(root,{'relative':spec['snapshot']['relative'],'sha256':spec['snapshot']['sha256']})
                    db.execute('INSERT INTO results(batch,hold,result,sha) VALUES(?,?,?,?)',(batch,hold,encoded,rb._hash(encoded.encode())))
                    db.execute("UPDATE attempts SET state='terminal',ended=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),cursor.lastrowid))
                    _check_sources(source);_physical(root,case_reference);_physical(root,{'relative':spec['snapshot']['relative'],'sha256':spec['snapshot']['sha256']})
                executed+=1
            _check_sources(source)
            return {'case_id':batch,'horizons':3,'alternative_arms_per_horizon':3,'executed':executed,'terminal_reused':reused,'compute_failed':failed,'factor_trials':factor_result['trials'],'factor_failures':factor_result['compute_failed'],'model_admitted':False,'real_source_qualified':False,'real_execution_verified':False,'whole_R4_complete':False,'orders_enabled':False}
        finally:db.close()


_attest()
_IMPORTED_SOURCES=_sources()
