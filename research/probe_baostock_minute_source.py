"""One training-day minute-source sanity probe, original SDK, native values only."""
import argparse,hashlib,json,math,os,subprocess,sys,time
from pathlib import Path
from decimal import Decimal
from datetime import datetime,timezone
R=Path(__file__).resolve().parents[1];P=R/'research/BAOSTOCK_MINUTE_PROBE_PROTOCOL_20261003.json';O=R/'research/BAOSTOCK_MINUTE_PROBE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def inputs(s):
 for p,h in s['input_hashes'].items():assert sha(R/p)==h
 for p,h in s['native_input_hashes'].items():assert sha(p)==h
def predicates(s,d):
 minute=d.get('queries',{}).get('5',{});daily=d.get('queries',{}).get('d',{});m=minute.get('rows',[]);day=daily.get('rows',[]);out={'minute_row_count':len(m),'daily_row_count':len(day),'source_date':s['date'],'issuer_code':s['code'],'minute_returncode':minute.get('error_code'),'daily_returncode':daily.get('error_code'),'both_nonempty_success':minute.get('error_code')=='0' and daily.get('error_code')=='0' and bool(m) and len(day)==1}
 if not out['both_nonempty_success']:return out
 times=[r['time'] for r in m];date=s['date'].replace('-','');numeric=['open','high','low','close','volume','amount'];out.update(unique_ascending_timestamps=len(times)==len(set(times)) and times==sorted(times),all_code_date_exact=all(r['code']==s['code'] and r['date']==s['date'] and r['time'].startswith(date) for r in m+day if 'time' in r) and day[0]['date']==s['date'] and day[0]['code']==s['code'],timestamp_lengths=sorted(set(map(len,times))),has0935_bar_endpoint=sum(t[8:12]=='0935' for t in times)==1,has1455_bar_endpoint=sum(t[8:12]=='1455' for t in times)==1,all_unadjusted_flag3=all(r.get('adjustflag')=='3' for r in m+day))
 values=[[Decimal(r[k]) for k in numeric] for r in m];dv={k:Decimal(day[0][k]) for k in numeric};out['all_ohlc_finite_consistent_nonnegative_volume_amount']=all(all(v.is_finite() for v in row) and row[2]<=min(row[0],row[3])<=max(row[0],row[3])<=row[1] and row[4]>=0 and row[5]>=0 for row in values)
 def equal(a,b,absolute=Decimal('0.0001')):return abs(a-b)<=max(absolute,abs(b)*Decimal('0.00000001'))
 out.update(first_open_matches_daily=equal(values[0][0],dv['open']),last_close_matches_daily=equal(values[-1][3],dv['close']),minute_high_matches_daily=equal(max(v[1] for v in values),dv['high']),minute_low_matches_daily=equal(min(v[2] for v in values),dv['low']),minute_volume_sum_matches_daily=equal(sum(v[4] for v in values),dv['volume'],Decimal('0')),minute_amount_sum_matches_daily=equal(sum(v[5] for v in values),dv['amount'],Decimal('0.01')))
 return out
def source_checks(s,d):
 try:return predicates(s,d)
 except Exception as e:return {'predicate_failure_type':type(e).__name__,'source_validated':False,'raw_values_exported':False}
def child(s):
 import socket
 socket.setdefaulttimeout(s['socket_timeout_seconds'])
 def audit(e,a):
  if e=='socket.connect' and a[1]!=(s['public_host'],s['public_port']):raise PermissionError('unexpected_probe_network_target')
  if e in ['subprocess.Popen','os.system']:raise PermissionError('child_process_not_allowed')
 sys.addaudithook(audit);sys.dont_write_bytecode=True;sys.path.insert(0,s['original_package_directory']);import baostock as bs;import baostock.common.context as ctx;assert not hasattr(ctx,'apiKey');p=Path(s['native_directory'])/'probe_details.json';d={'handshake_attempts':1,'query_attempts':0,'logout_attempts':0,'queries':{}};write(p,d)
 try:
  lg=bs.login();d['handshake_error_code']=str(lg.error_code);write(p,d)
  if lg.error_code=='0':
   for q in s['queries']:
    d['query_attempts']+=1;write(p,d);rs=bs.query_history_k_data_plus(s['code'],q['fields'],start_date=s['date'],end_date=s['date'],frequency=q['frequency'],adjustflag='3');rows=[]
    while rs.error_code=='0' and rs.next():
     rows.append(dict(zip(rs.fields,rs.get_row_data())));assert len(rows)<=q['row_cap']
    assert set(rs.fields)<=set(q['fields'].split(','));d['queries'][q['frequency']]={'error_code':str(rs.error_code),'fields':rs.fields,'rows':rows};write(p,d)
 except Exception as e:d['failure_type']=type(e).__name__
 finally:
  if d.get('handshake_error_code')=='0':
   d['logout_attempts']=1
   try:d['logout_error_code']=str(bs.logout().error_code)
   except Exception as e:d['logout_failure_type']=type(e).__name__
  if hasattr(ctx,'default_socket'):
   try:getattr(ctx,'default_socket').close()
   except Exception:pass
  write(p,d)
def run(s):
 inputs(s);assert read(O)['state']=='not_run';local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir();start=time.monotonic();env={k:v for k,v in os.environ.items() if k.upper() in ['SYSTEMROOT','WINDIR','TEMP','TMP','PATH']};env.update(PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1');timeout=False;rc=None
 try:
  p=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--child'],cwd=local,env=env,capture_output=True,timeout=s['child_timeout_seconds'],creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0);rc=p.returncode;(local/'child.stdout.log').write_bytes(p.stdout);(local/'child.stderr.log').write_bytes(p.stderr)
 except subprocess.TimeoutExpired as e:timeout=True;(local/'child.stdout.log').write_bytes(e.stdout or b'');(local/'child.stderr.log').write_bytes(e.stderr or b'')
 d=read(local/'probe_details.json') if (local/'probe_details.json').exists() else {};pred=source_checks(s,d);manifest=local/'manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}});inputs(s)
 r={'schema':'regis.baostock.minute_probe.v1','state':'actual_native_training_day_five_minute_source_probe','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'actual_child_returncode':rc,'actual_child_timed_out':timeout,'public_anonymous_handshake_attempts':d.get('handshake_attempts',0),'handshake_error_code':d.get('handshake_error_code'),'actual_history_query_attempts':d.get('query_attempts',0),'logout_attempts':d.get('logout_attempts',0),'predicates':pred,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'native_quote_arrays_only':True,'own_account_or_token_read':False,'website_login_or_registration':False,'one_minute_or_quote_side_size_recovered':False,'M24_actual_execution_or_fill_proven':False,'historical_hour_vintage_certified':False,'full_market_coverage_certified':False,'data_AI_cloud_commercial_rights_certified':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(O,r);print(json.dumps({'state':r['state'],'elapsed_seconds':r['elapsed_seconds'],'history_query_attempts':r['actual_history_query_attempts'],'handshake_error_code':r['handshake_error_code'],'source_checks':pred,'raw_values_exported':False,'model_admitted':False}))
def verify(s):
 inputs(s);r=read(O);assert r['protocol_sha256']==sha(P) and r['implementation_sha256']==sha(__file__) and sha(r['local_details']['path'])==r['local_details']['sha256']
 for p,h in read(r['local_details']['path'])['files'].items():assert sha(p)==h
 dpath=Path(s['native_directory'])/'probe_details.json';assert source_checks(s,read(dpath) if dpath.exists() else {})==r['predicates']
 assert r['actual_history_query_attempts']<=2 and r['public_anonymous_handshake_attempts']<=1 and not any(r[k] for k in ['own_account_or_token_read','website_login_or_registration','one_minute_or_quote_side_size_recovered','M24_actual_execution_or_fill_proven','historical_hour_vintage_certified','full_market_coverage_certified','data_AI_cloud_commercial_rights_certified','economic_runs','model_admitted','full_goal_complete'])
 print(json.dumps({'actual_native_probe_predicates_rebuilt':True,'network_reruns':0,'M24_execution_admitted':False,'quote_values_exported':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');m.add_argument('--child',action='store_true');a=ap.parse_args();s=read(P)
 if a.child:child(s)
 elif a.run:run(s)
 else:verify(s)
