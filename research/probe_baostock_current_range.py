"""First official-advertised current-range boundary minute-source feasibility probe, original SDK, native values only."""
import argparse,hashlib,json,math,os,subprocess,sys,time
from pathlib import Path
from decimal import Decimal
from datetime import datetime,timezone
R=Path(__file__).resolve().parents[1];P=R/'research/BAOSTOCK_CURRENT_RANGE_PROTOCOL_20261003.json';O=R/'research/BAOSTOCK_CURRENT_RANGE_RESULT_20261003.json'
sys.path.insert(0,str(R))
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']};g=s['execution_gate']
 assert t['status']=='in_progress' and t['id']==s['parent_task'] and t['owner_session']==g['owner_session'] and t['contract_sha256']==g['expected_contract_sha256']==contract_hash(t) and read(P)==s
 assert sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert s['date']=='2020-01-03' and s['code']=='sz.000001' and s['public_host']=='public-api.baostock.com' and s['public_port']==10030 and s['socket_timeout_seconds']==8 and s['child_timeout_seconds']==45
 assert s['queries']==[{'frequency':'5','fields':'date,time,code,open,high,low,close,volume,amount,adjustflag','row_cap':64},{'frequency':'d','fields':'date,code,open,high,low,close,volume,amount,adjustflag','row_cap':1}]
 inputs(s);return t

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
 sys.addaudithook(audit);sys.dont_write_bytecode=True;sys.path.insert(0,s['original_package_directory']);import baostock as bs;import baostock.common.context as ctx;assert not hasattr(ctx,'apiKey');p=Path(s['native_directory'])/'probe_details.json';d={'handshake_attempts':0,'query_attempts':0,'logout_attempts':0,'queries':{}};write(p,d)
 try:
  d['handshake_attempts']+=1;lg=bs.login();d['handshake_error_code']=str(lg.error_code);write(p,d)
  if lg.error_code=='0':
   for q in s['queries']:
    write(p,d);d['query_attempts']+=1;rs=bs.query_history_k_data_plus(s['code'],q['fields'],start_date=s['date'],end_date=s['date'],frequency=q['frequency'],adjustflag='3');rows=[]
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
def launch(argv,local,env,timeout):
 try:
  p=subprocess.run(argv,cwd=local,env=env,capture_output=True,timeout=timeout,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
  return {'returncode':p.returncode,'timed_out':False,'error_type':None,'stdout':p.stdout,'stderr':p.stderr}
 except subprocess.TimeoutExpired as e:return {'returncode':None,'timed_out':True,'error_type':'TimeoutExpired','stdout':e.stdout or b'','stderr':e.stderr or b''}
 except Exception as e:return {'returncode':None,'timed_out':False,'error_type':type(e).__name__,'stdout':b'','stderr':b''}

def terminal(local,s,c):
 errors=[]
 def attempt(label,action):
  try:
   value=action();return True if value is None else value
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 c['terminal_recording_errors']=errors
 attempt('receipt_write',lambda:write(local/'receipt.json',c))
 files=attempt('native_file_hashes',lambda:{str(p):sha(p) for p in local.iterdir() if p.is_file()})
 manifest=local/'manifest.json';attempt('manifest_write',lambda:write(manifest,{'files':files or {}}))
 hs={key:attempt(key,lambda path=path:sha(path)) for key,path in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_protocol_sha256',local/'protocol.json'),('native_task_sha256',local/'frozen_task.json'),('native_receipt_sha256',local/'receipt.json'),('native_manifest_sha256',manifest)]}
 d=c.get('details',{});failure=c.get('failure_type') or c.get('launch_error') or d.get('failure_type');certified=c.get('child_returncode')==0 and not c.get('child_timed_out') and not failure and not errors
 r={'schema':'regis.baostock.current_range_result.v1','state':'actual_native_current_boundary_source_probe' if certified else 'actual_source_failure_preserved',**hs,
 'observed_utc':c['ended_utc'],'elapsed_seconds':c['elapsed_seconds'],'actual_child_returncode':c.get('child_returncode'),'actual_child_timed_out':c.get('child_timed_out',False),'failure_type':failure,'terminal_recording_errors':errors,'method_counters_certified':certified,
 'public_anonymous_handshake_attempts':d.get('handshake_attempts') if certified else None,'actual_history_query_attempts':d.get('query_attempts') if certified else None,'logout_attempts':d.get('logout_attempts') if certified else None,'handshake_error_code':d.get('handshake_error_code'),'predicates':c.get('predicates',{}),'local_details':{'path':str(manifest),'sha256':hs['native_manifest_sha256']},
 'native_quote_arrays_only':True,'own_account_or_token_read':False,'website_login_or_registration':False,'one_minute_or_quote_side_size_recovered':False,'M24_actual_execution_or_fill_proven':False,'historical_hour_vintage_certified':False,'full_market_coverage_certified':False,'data_AI_cloud_commercial_rights_certified':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 if attempt('result_write',lambda:write(O,r)) is None:
  r['state']='actual_source_failure_preserved';attempt('fallback_write',lambda:write(local/'terminal_failure.json',r))
 return r

def run(s):
 t=gate(s);assert read(O)['state']=='not_run';local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();start=time.monotonic()
 c={'started_utc':datetime.now(timezone.utc).isoformat(),'failure_type':None,'child_returncode':None,'child_timed_out':False,'stage':'directory_create'}
 try:
  local.mkdir();c['stage']='protocol_write';write(local/'protocol.json',s);c['stage']='frozen_task_write';write(local/'frozen_task.json',t)
  env={k:v for k,v in os.environ.items() if k.upper() in ['SYSTEMROOT','WINDIR','TEMP','TMP','PATH']};env.update(PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
  c['stage']='child_launch';out=launch([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--child'],local,env,s['child_timeout_seconds']);c.update(child_returncode=out['returncode'],child_timed_out=out['timed_out'],launch_error=out['error_type'])
  c['stage']='stdout_write';(local/'child.stdout.log').write_bytes(out['stdout']);c['stage']='stderr_write';(local/'child.stderr.log').write_bytes(out['stderr'])
  c['stage']='details_read';p=local/'probe_details.json';c['details']=read(p) if p.exists() else {};c['predicates']=source_checks(s,c['details']);c['stage']='input_revalidation';inputs(s)
 except Exception as e:c['failure_type']=type(e).__name__
 c['ended_utc']=datetime.now(timezone.utc).isoformat();c['elapsed_seconds']=round(time.monotonic()-start,3);r=terminal(local,s,c)
 print(json.dumps({'state':r['state'],'elapsed_seconds':r['elapsed_seconds'],'history_query_attempts':r['actual_history_query_attempts'],'handshake_error_code':r['handshake_error_code'],'source_checks':r['predicates'],'failure_type':r['failure_type'],'raw_values_exported':False,'model_admitted':False}))

def verify(s):
 inputs(s);r=read(O);D=Path(s['native_directory']);assert r['protocol_sha256']==sha(P) and r['implementation_sha256']==sha(__file__)
 if r['local_details']['sha256'] is not None:
  assert sha(r['local_details']['path'])==r['local_details']['sha256']
  for p,h in read(r['local_details']['path'])['files'].items():assert sha(p)==h
 if r['native_task_sha256'] is not None:
  from scripts.workflow_guard import contract_hash
  frozen=read(D/'frozen_task.json');assert sha(D/'frozen_task.json')==r['native_task_sha256'] and frozen['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(frozen)
 if r['native_protocol_sha256'] is not None:assert sha(D/'protocol.json')==r['native_protocol_sha256'] and read(D/'protocol.json')==s
 if r['native_receipt_sha256'] is not None:
  assert sha(D/'receipt.json')==r['native_receipt_sha256'];c=read(D/'receipt.json');d=c.get('details',{});assert r['predicates']==c.get('predicates',{})
  if (D/'probe_details.json').exists():assert source_checks(s,read(D/'probe_details.json'))==r['predicates']
 if r['method_counters_certified']:assert r['actual_history_query_attempts']<=2 and r['public_anonymous_handshake_attempts']<=1
 else:assert r['actual_history_query_attempts'] is r['public_anonymous_handshake_attempts'] is None
 if r['state']=='actual_native_current_boundary_source_probe':assert not r['failure_type'] and not r['terminal_recording_errors'] and r['actual_child_returncode']==0 and not r['actual_child_timed_out']
 else:assert r['state']=='actual_source_failure_preserved' and (r['failure_type'] or r['terminal_recording_errors'] or r['actual_child_returncode']!=0 or r['actual_child_timed_out'])
 assert not any(r[k] for k in ['own_account_or_token_read','website_login_or_registration','one_minute_or_quote_side_size_recovered','M24_actual_execution_or_fill_proven','historical_hour_vintage_certified','full_market_coverage_certified','data_AI_cloud_commercial_rights_certified','economic_runs','model_admitted','full_goal_complete'])
 print(json.dumps({'actual_native_probe_predicates_rebuilt':True,'network_reruns':0,'M24_execution_admitted':False,'quote_values_exported':False}))

if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');m.add_argument('--child',action='store_true');m.add_argument('--child-gate-only',action='store_true');a=ap.parse_args();s=read(P)
 if a.child_gate_only:gate(s);print(json.dumps({'child_gate_only_passed':True,'SDK_calls':0}))
 elif a.child:gate(s);child(s)
 elif a.run:run(s)
 else:verify(s)
