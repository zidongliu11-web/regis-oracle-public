"""Bounded original public anonymous SDK metadata check; never request quotes."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
R=Path(__file__).resolve().parents[1]
P=R/'research/BAOSTOCK_METADATA_SOURCE_PROTOCOL_20261003.json';O=R/'research/BAOSTOCK_METADATA_SOURCE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def inputs(s):
 for p,h in s['input_hashes'].items():assert sha(R/p)==h
 for p,h in s['native_input_hashes'].items():assert sha(p)==h
def child(s):
 import socket
 socket.setdefaulttimeout(s['socket_timeout_seconds'])
 allowed=(s['public_host'],s['public_port'])
 def audit(event,args):
  if event=='socket.connect' and args[1]!=allowed:raise PermissionError('unexpected_metadata_network_target')
  if event in ['subprocess.Popen','os.system']:raise PermissionError('child_process_not_allowed')
 sys.addaudithook(audit);sys.dont_write_bytecode=True;sys.path.insert(0,s['original_package_directory'])
 import baostock as bs
 import baostock.common.context as ctx
 assert not hasattr(ctx,'apiKey')
 details={'observed_utc':datetime.now(timezone.utc).isoformat(),'SDK_version':bs.__version__,'public_anonymous_handshake_attempts':1,'own_account_or_token_read':False,'website_login_or_registration':False,'basic_query_attempts':0,'logout_attempts':0,'events':[]}
 write(Path(s['native_directory'])/'actual_metadata_details.json',details)
 try:
  login=bs.login();details['handshake_error_code']=str(login.error_code);write(Path(s['native_directory'])/'actual_metadata_details.json',details)
  if login.error_code=='0':
   for item in s['targets']:
    details['basic_query_attempts']+=1;write(Path(s['native_directory'])/'actual_metadata_details.json',details);rs=bs.query_stock_basic(code=item['query_code']);rows=[]
    while rs.error_code=='0' and rs.next():
     rows.append(rs.get_row_data());assert len(rows)<=s['rows_per_target_cap']
    assert set(rs.fields)<=set(s['allowed_metadata_fields'])
    data=[dict(zip(rs.fields,row)) for row in rows]
    assert all(row.get('code')==item['query_code'] for row in data)
    details['events'].append({'query_code':item['query_code'],'role':item['role'],'error_code':str(rs.error_code),'fields':rs.fields,'native_basic_rows':data,'row_count':len(data),'unique_exact_code_match':len(data)==1});write(Path(s['native_directory'])/'actual_metadata_details.json',details)
 except Exception as e:details['failure_type']=type(e).__name__
 finally:
  if details.get('handshake_error_code')=='0':
   details['logout_attempts']=1
   try:details['logout_error_code']=str(bs.logout().error_code)
   except Exception as e:details['logout_failure_type']=type(e).__name__
  if hasattr(ctx,'default_socket'):
   try:getattr(ctx,'default_socket').close()
   except Exception:pass
 write(Path(s['native_directory'])/'actual_metadata_details.json',details)
def run(s):
 inputs(s);assert read(O)['state']=='not_run';local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir();start=time.monotonic()
 env={k:v for k,v in os.environ.items() if k.upper() in ['SYSTEMROOT','WINDIR','TEMP','TMP','PATH']};env.update(PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
 timed_out=False;rc=None
 try:
  process=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--child'],cwd=local,env=env,capture_output=True,timeout=s['child_timeout_seconds'],creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0);rc=process.returncode;(local/'child.stdout.log').write_bytes(process.stdout);(local/'child.stderr.log').write_bytes(process.stderr)
 except subprocess.TimeoutExpired as e:
  timed_out=True;(local/'child.stdout.log').write_bytes(e.stdout or b'');(local/'child.stderr.log').write_bytes(e.stderr or b'')
 details_path=local/'actual_metadata_details.json';details=read(details_path) if details_path.exists() else {};events=[]
 for event in details.get('events',[]):
  rows=event['native_basic_rows'];events.append({k:v for k,v in event.items() if k!='native_basic_rows'}|{'returned_type_values':sorted(set(row.get('type','') for row in rows)),'listing_date_present_count':sum(bool(row.get('ipoDate')) for row in rows),'historical_lifetime_repaired':False,'source_ID_removed':False})
 inputs(s);manifest=local/'manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}})
 r={'schema':'regis.baostock.metadata_source.v1','state':'actual_original_anonymous_metadata_source_attempt','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'actual_child_returncode':rc,'actual_child_timed_out':timed_out,'public_anonymous_handshake_attempts':details.get('public_anonymous_handshake_attempts',0),'handshake_error_code':details.get('handshake_error_code'),'actual_basic_query_attempts':details.get('basic_query_attempts',0),'logout_attempts':details.get('logout_attempts',0),'events':events,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'own_account_or_token_read':False,'website_login_or_registration':False,'quote_data_requested':False,'source_originals_changed':False,'data_retention_AI_cloud_commercial_rights_certified':False,'historical_PIT_certified':False,'unknown_IDs_removed':0,'source_classes_activated':0,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(O,r)
 print(json.dumps({k:r[k] for k in ['state','elapsed_seconds','actual_child_returncode','actual_child_timed_out','public_anonymous_handshake_attempts','handshake_error_code','actual_basic_query_attempts','events','own_account_or_token_read','quote_data_requested','unknown_IDs_removed']},ensure_ascii=False))
def verify(s):
 inputs(s);r=read(O);assert r['protocol_sha256']==sha(P) and r['implementation_sha256']==sha(__file__) and sha(r['local_details']['path'])==r['local_details']['sha256']
 for p,h in read(r['local_details']['path'])['files'].items():assert sha(p)==h
 for event in r['events']:assert not event['historical_lifetime_repaired'] and not event['source_ID_removed'] and event['query_code'] in [i['query_code'] for i in s['targets']]
 assert not any(r[k] for k in ['own_account_or_token_read','website_login_or_registration','quote_data_requested','source_originals_changed','data_retention_AI_cloud_commercial_rights_certified','historical_PIT_certified','unknown_IDs_removed','source_classes_activated','new_economic_runs','model_admitted','full_goal_complete'])
 assert r['actual_basic_query_attempts']<=3 and r['public_anonymous_handshake_attempts']<=1
 print(json.dumps({'actual_metadata_attempt_verified':True,'network_reruns':0,'historical_ID_repair_certified':False,'model_admitted':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');m.add_argument('--child',action='store_true');a=ap.parse_args();s=read(P)
 if a.child:child(s)
 elif a.run:run(s)
 else:verify(s)
