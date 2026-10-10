"""One frozen completed-session public pair capture; no alpha or trading admission.

Reuse the original public SDK and existing workflow contract. Values stay in
Native storage; the Git result contains counts, dates, clocks, and digests only.
No automatic retries, scheduler, account/config/environment enumeration, or fills.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
PROTOCOL = ROOT / 'research/COMPLETED_DAILY_CAPTURE_PROTOCOL_20261008.json'
RESULT = ROOT / 'research/COMPLETED_DAILY_CAPTURE_RESULT_20261008.json'
STOCK_FIELDS = 'date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST'.split(',')
ETF_FIELDS = 'date,code,open,high,low,close,volume,amount,adjustflag'.split(',')
CAL_FIELDS = ['calendar_date', 'is_trading_day']


def clock():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with Path(path).open('rb') as stream:
        raw=stream.read(2 * 1024 * 1024 + 1)
    if len(raw)>2 * 1024 * 1024:
        raise ValueError('hash_input_cap')
    return hashlib.sha256(raw).hexdigest()


def write(path, value, *, exclusive=False):
    path = Path(path)
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('native_output_cap')
    if exclusive:
        with path.open('xb') as stream:
            stream.write(raw)
    else:
        tmp = path.with_suffix(path.suffix + '.tmp')
        with tmp.open('wb') as stream:
            stream.write(raw)
        tmp.replace(path)


def read(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('input_cap')
    return json.loads(raw)


def completed_cutoff(now, end):
    parsed = datetime.fromisoformat(now)
    close = datetime.combine(date.fromisoformat(end), datetime.min.time(), timezone.utc) + timedelta(hours=7)
    if parsed.tzinfo is None or parsed < close:
        raise ValueError('session_not_closed_at_capture')


def calendar_days(rows, start, end):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    expected = [(a + timedelta(days=i)).isoformat() for i in range((b-a).days+1)]
    if len(expected) > 10 or [r.get('calendar_date') for r in rows] != expected:
        raise ValueError('calendar_incomplete_or_unordered')
    if any(r.get('is_trading_day') not in ('0', '1') for r in rows):
        raise ValueError('calendar_flag')
    return [r['calendar_date'] for r in rows if r['is_trading_day'] == '1']


def first_response(rs, fields, cap):
    """Consume the first SDK response only: never ResultData.next pagination.

    SDK receive/decode bytes are unknown and not bounded by this consumer cap.
    Oversized first responses fail; there is no next-page or retry request.
    """
    if str(rs.error_code) != '0' or list(rs.fields) != fields:
        raise ValueError('provider_error_or_field_drift')
    data = rs.data
    if not isinstance(data, list) or len(data) > cap:
        raise ValueError('first_response_row_cap')
    rows = []
    for values in data:
        if not isinstance(values, list) or len(values) != len(fields) or not all(isinstance(v, str) for v in values):
            raise ValueError('provider_row_shape')
        rows.append(dict(zip(fields, values)))
    return rows


def bar_checks(rows, expected_days, code, *, stock):
    dates = [r.get('date') for r in rows]
    if dates != expected_days or any(r.get('code') != code for r in rows):
        raise ValueError('pair_date_code_coverage')
    suspended = 0
    for r in rows:
        if r.get('adjustflag') != '3':
            raise ValueError('adjustment_drift')
        if stock:
            if r.get('tradestatus') not in ('0','1') or r.get('isST') not in ('0','1'):
                raise ValueError('status_flags')
            if r['tradestatus'] == '0':
                suspended += 1
                if any(r[k] and Decimal(r[k]) != 0 for k in ('volume','amount')):
                    raise ValueError('suspended_turnover')
                continue
        p = {k: Decimal(r[k]) for k in ('open','high','low','close')}
        if not all(x.is_finite() and x > 0 for x in p.values()) or not p['low'] <= min(p['open'],p['close']) <= max(p['open'],p['close']) <= p['high']:
            raise ValueError('ohlc')
        v,a = Decimal(r['volume']),Decimal(r['amount'])
        if not all(x.is_finite() and x >= 0 for x in (v,a)) or (v == 0) != (a == 0):
            raise ValueError('turnover')
        if stock:
            pre,chg = Decimal(r['preclose']),Decimal(r['pctChg'])
            if not pre.is_finite() or pre <= 0 or not chg.is_finite() or abs((p['close']/pre-1)*100-chg) > Decimal('0.0001'):
                raise ValueError('preclose_change')
    return {'rows':len(rows),'dates':dates,'suspended_rows':suspended,'raw_flag3':True,'source_available_at_unknown':True}


def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json')
    if t['id'] != p['parent_task'] or t['status'] != 'in_progress' or t['owner_session'] != p['owner_session'] or contract_hash(t) != t['contract_sha256']:
        raise ValueError('active_contract')
    idx={x['path']:x['sha256'] for x in t['artifacts']}
    if idx[PROTOCOL.relative_to(ROOT).as_posix()] != sha(PROTOCOL):
        raise ValueError('protocol_binding')
    for path,h in p['input_hashes'].items():
        if sha(ROOT/path) != h:
            raise ValueError('input_binding')
    for path,h in p['native_sdk_hashes'].items():
        if sha(path) != h:
            raise ValueError('sdk_binding')
    if p['start'] != '2026-09-29' or p['end'] != '2026-10-08' or p['stock'] != 'sh.600000' or p['etf'] != 'sh.510300' or p['public_host'] != 'public-api.baostock.com' or p['public_port'] != 10030:
        raise ValueError('fixed_request_scope')
    completed_cutoff(clock(),p['end'])
    return t


def claim_child_dispatch(p):
    out=Path(p['native_directory'])/'capture'
    write(out/'child_dispatched.json',{'claimed_utc':clock(),'source_batches_max':1},exclusive=True)


def child(p):
    gate(p)
    claim_child_dispatch(p)
    socket.setdefaulttimeout(8)
    def audit(event,args):
        if event == 'socket.connect' and args[1] != (p['public_host'],p['public_port']):
            raise PermissionError('unexpected_public_target')
        if event in ('subprocess.Popen','os.system'):
            raise PermissionError('unexpected_child_spawn')
    sys.addaudithook(audit)
    sys.path.insert(0,p['sdk_directory'])
    import baostock as bs
    import baostock.common.context as ctx
    out=Path(p['native_directory'])/'capture'
    j={'schema':'regis.completed_daily_native.v1','started_utc':clock(),'handshake_attempts':0,'semantic_query_attempts':0,'logout_attempts':0,'attempts':[],'observations':{},'failure_type':None,'SDK_distribution_version':p['SDK_distribution_version']}
    path=out/'journal.json'
    def request(label,fn,fields,cap):
        if j['semantic_query_attempts'] >= 12:
            raise ValueError('semantic_request_cap')
        j['semantic_query_attempts']+=1
        a={'label':label,'sent_at_utc':clock(),'returned_at_utc':None,'error_code':None,'failure_type':None}
        j['attempts'].append(a);write(path,j)
        try:
            rs=fn();a['returned_at_utc']=clock();a['error_code']=str(rs.error_code)
            rows=first_response(rs,fields,cap)
            return rows
        except Exception as e:
            a['failure_type']=type(e).__name__
            raise
        finally:
            write(path,j)
    try:
        j['handshake_attempts']=1;write(path,j)
        lg=bs.login();j['handshake_error_code']=str(lg.error_code);write(path,j)
        if lg.error_code != '0':
            raise ValueError('public_guest_handshake')
        cal=request('calendar',lambda:bs.query_trade_dates(start_date=p['start'],end_date=p['end']),CAL_FIELDS,10)
        days=calendar_days(cal,p['start'],p['end'])
        j['observations']['calendar']={'rows':cal,'observed_at_utc':j['attempts'][-1]['returned_at_utc']}
        j['trading_dates']=days;write(path,j)
        if not days:
            raise ValueError('no_completed_trading_dates')
        stock=request('stock',lambda:bs.query_history_k_data_plus(p['stock'],','.join(STOCK_FIELDS),start_date=p['start'],end_date=p['end'],frequency='d',adjustflag='3'),STOCK_FIELDS,10)
        j['observations']['stock']={'rows':stock,'observed_at_utc':j['attempts'][-1]['returned_at_utc']};write(path,j)
        j['stock_checks']=bar_checks(stock,days,p['stock'],stock=True);write(path,j)
        etf=[]
        for day in days:
            bulk=request('ETF_'+day,lambda day=day:bs.query_daily_history_k_ETF(date=day),ETF_FIELDS,5000)
            if any(r['date'] != day for r in bulk) or len({r['code'] for r in bulk}) != len(bulk):
                raise ValueError('ETF_bulk_date_duplicates')
            selected=[r for r in bulk if r['code']==p['etf']]
            j['observations']['ETF_'+day]={'selected_rows':selected,'received_bulk_rows':len(bulk),'observed_at_utc':j['attempts'][-1]['returned_at_utc']};write(path,j)
            if len(selected)!=1:
                raise ValueError('ETF_target_missing_or_duplicate')
            etf.extend(selected)
        j['etf_checks']=bar_checks(etf,days,p['etf'],stock=False)
    except Exception as e:
        j['failure_type']=type(e).__name__
    finally:
        if j.get('handshake_error_code')=='0':
            j['logout_attempts']=1;write(path,j)
            try:j['logout_error_code']=str(bs.logout().error_code)
            except Exception as e:j['logout_failure_type']=type(e).__name__
        if hasattr(ctx,'default_socket'):
            try:ctx.default_socket.close()
            except Exception:pass
        j['ended_utc']=clock();write(path,j)
    return 0 if not j['failure_type'] else 1


def run(p):
    t=gate(p);out=Path(p['native_directory'])/'capture'
    if 'onedrive' in str(out).lower():
        raise ValueError('raw_cloud_path')
    out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    r={'schema':'regis.completed_daily_capture_result.v1','started_utc':clock(),'returncode':None,'timed_out':False,'failure_type':None}
    try:
        process=subprocess.run([sys.executable,'-I','-X','utf8',str(Path(__file__).resolve()),'--child'],cwd=out,capture_output=True,timeout=50,creationflags=0x08000000 if sys.platform=='win32' else 0)
        r['returncode']=process.returncode
        (out/'stdout.log').write_bytes(process.stdout);(out/'stderr.log').write_bytes(process.stderr)
    except subprocess.TimeoutExpired as e:
        r.update(timed_out=True,failure_type='TimeoutExpired')
        (out/'stdout.log').write_bytes(e.stdout or b'');(out/'stderr.log').write_bytes(e.stderr or b'')
    except Exception as e:
        r['failure_type']=type(e).__name__
    r['ended_utc']=clock()
    j=read(out/'journal.json') if (out/'journal.json').exists() else {}
    if not r['failure_type']:r['failure_type']=j.get('failure_type')
    gate(p)
    r.update(protocol_sha256=sha(PROTOCOL),implementation_sha256=sha(__file__),contract_sha256=t['contract_sha256'],source_clock_role='actual_query_receipt_not_historical_publication_or_intraday_availability',requested_start=p['start'],requested_end=p['end'],trading_dates=j.get('trading_dates',[]),handshake_attempts_observed=j.get('handshake_attempts',0),semantic_query_attempts_observed=j.get('semantic_query_attempts',0),attempt_clock_metadata=j.get('attempts',[]),logout_attempts_observed=j.get('logout_attempts',0),stock_checks=j.get('stock_checks'),etf_checks=j.get('etf_checks'),native_files={str(x):sha(x) for x in out.iterdir() if x.is_file()},native_values_only=True,SDK_wire_request_and_receive_byte_counts='unknown_not_consumer_capped',automatic_retries=0,account_or_token_reads=False,economic_runs=0,model_admitted=False,historical_PIT_certified=False,full_A_coverage_certified=False,forward_strategy_validation=False,full_goal_complete=False)
    r['state']='completed_session_pair_captured' if r['returncode']==0 and not r['timed_out'] and not r['failure_type'] and r['stock_checks'] is not None and r['etf_checks'] is not None else 'source_failure_preserved'
    write(RESULT,r);write(out/'terminal.json',r,exclusive=True)
    print(json.dumps({k:r[k] for k in ('state','returncode','timed_out','failure_type','trading_dates','semantic_query_attempts_observed','stock_checks','etf_checks','model_admitted')}))
    return 0 if r['state']=='completed_session_pair_captured' else 1


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--child',action='store_true');args=ap.parse_args();p=read(PROTOCOL)
    raise SystemExit(child(p) if args.child else run(p))
