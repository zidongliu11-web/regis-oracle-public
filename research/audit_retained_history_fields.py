"""Bounded offline field audit of retained failed history; counts only."""
from pathlib import Path
from datetime import datetime,timezone
from decimal import InvalidOperation
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,STOCK_FIELDS
from research.compute_retained_current_lookback import read_verified_source
from research.retained_daily_diagnostic import numeric
P=ROOT/'research/RETAINED_HISTORY_FIELD_AUDIT_PROTOCOL_20261009.json'
O=ROOT/'research/RETAINED_HISTORY_FIELD_AUDIT_RESULT_20261009.json'
NUMERIC=['open','high','low','close','preclose','volume','amount','pctChg','turn']
POSITIVE={'open','high','low','close','preclose'}


def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');a={v['path']:v['sha256'] for v in t['artifacts']}
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t) and a[P.relative_to(ROOT).as_posix()]==sha(P)
    assert p['source_code']=='sh.601059' and p['source_queries']==0 and p['formal_batches']==2
    assert p['source_ref']['sha256']=='e2fa6b5e96c144fa1be9506abbcc5d0972449120f3bb8de88f4060a0fcd8bca4'
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    read_verified_source(p['source_ref'])
    return t


def run(p):
    t=gate(p);protocol_hash,implementation_hash=sha(P),sha(__file__)
    out=Path(p['native_directory'])/'audit';out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    def audit(event,args):
        if event.startswith('socket.') or event in ('subprocess.Popen','os.system'):
            raise PermissionError('offline_field_audit_network_or_spawn_denied')
    sys.addaudithook(audit)
    r={'schema':'regis.retained_history_field_audit_result.v1','started_utc':clock(),'failure_type':None}
    try:
        src=read_verified_source(p['source_ref'])
        assert src['schema']=='regis.fixed_sample_SDK_decoded_response.v1' and src['source_code']=='sh.601059' and src['SDK_error_code']=='0'
        assert src['fields']==STOCK_FIELDS and isinstance(src['rows'],list) and len(src['rows'])==21
        assert datetime.fromisoformat(src['observed_at_utc']).tzinfo is not None
        assert datetime.fromisoformat(src['observed_at_utc'])<=datetime.now(timezone.utc)
        counts={f:{'blank':0,'parsed_under_original_rule':0,'InvalidOperation':0,'other_original_parser_failure':0} for f in NUMERIC}
        states={f:{'0':0,'1':0,'unknown':0} for f in ['tradestatus','isST']}
        blank_OHLC_by_status={'0':0,'1':0,'unknown':0};dates=[]
        for values in src['rows']:
            assert isinstance(values,list) and len(values)==len(STOCK_FIELDS) and all(isinstance(v,str) for v in values)
            row=dict(zip(STOCK_FIELDS,values));assert row['code']=='sh.601059' and row['adjustflag']=='3'
            dates.append(row['date'])
            for f in NUMERIC:
                blank=not row[f].strip()
                counts[f]['blank']+=int(blank)
                try:numeric(row[f],positive=f in POSITIVE)
                except InvalidOperation:counts[f]['InvalidOperation']+=1
                except ValueError:counts[f]['other_original_parser_failure']+=1
                else:counts[f]['parsed_under_original_rule']+=1
            for f in states:states[f][row[f] if row[f] in ('0','1') else 'unknown']+=1
            status=row['tradestatus'] if row['tradestatus'] in ('0','1') else 'unknown'
            blank_OHLC_by_status[status]+=int(any(not row[f].strip() for f in ['open','high','low','close']))
        assert dates==sorted(set(dates)) and dates[0]=='2026-09-02' and dates[-1]=='2026-10-08'
        r.update(total_decoded_rows=21,numeric_parser_counts=counts,reported_state_counts=states,
            rows_with_blank_OHLC_by_reported_status=blank_OHLC_by_status,
            observed_at_utc=src['observed_at_utc'],source_code='sh.601059',source_date_range=[dates[0],dates[-1]],
            old_failure_role='Original lookback positive OHLC parser executes before reported tradestatus check; blank cells can raise InvalidOperation. Counts verify actual retained source fields, no missing prices/status/value inferred.',
            decision='retain_missing_source_fields_no_factor_input_or_admission',source_quality_passed=False,
            hypothetical_suspended_quote_relaxation='not_implemented; exact source fields and reported flags only, no invented close/pct/preclose/qty/calendar or tolerance changes')
        gate(p);r['state']='retained_source_field_counts_audited_no_values_exported'
    except Exception as exc:r.update(state='field_audit_failure_preserved',failure_type=type(exc).__name__)
    r.update(ended_utc=clock(),protocol_sha256=protocol_hash,implementation_sha256=implementation_hash,contract_sha256=t['contract_sha256'],source_ref=p['source_ref'],
        source_queries=0,source_dispatches=0,SDK_calls=0,new_factor_values_computed=0,new_formulas=0,economic_runs=0,
        market_raw_values_exported=False,full_A_verified=False,historical_PIT_verified=False,market_gate_ready=False,
        original_107_quarantines_unchanged=True,model_admitted=False,orders_enabled=False,full_goal_complete=False)
    write(O,r);write(out/'terminal.json',r,exclusive=True);print(json.dumps(r))
    return 0 if r['failure_type'] is None else 1


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--validate-plan',action='store_true');args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'source_queries':0}))
    else:raise SystemExit(run(p))
