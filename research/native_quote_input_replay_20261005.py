"""One manual frozen quote-input command; metadata-only output, no economic trial."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,os,subprocess,sys,time
R=Path.cwd();sys.path.insert(0,str(R))
from factor_system.native_snapshot import _native_root,_no_links
N=Path(__file__).resolve().parent
PP=R/'research/QUOTE_INPUT_REPLAY_PROTOCOL_20261005.json'
sha=lambda b:hashlib.sha256(b).hexdigest()
def digest(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as stream:
  while b:=stream.read(1024*1024):h.update(b)
 return h.hexdigest()
def pins():
 _native_root(N)
 p=json.loads(PP.read_bytes())
 assert Path(sys.executable).resolve()==Path(p['runtime']['python_path']).resolve()
 assert digest(sys.executable)==p['runtime']['python_exe_sha256']
 assert sha(Path(__file__).read_bytes())==p['helper_sha256']
 for path,h in p['source_hashes'].items():assert digest(R/path)==h,path
 for path,h in p['native_metadata_hashes'].items():assert digest(path)==h
 assert json.loads(Path(p['prior_sync_proof']).read_bytes())['commit']==p['source_commit']
 return p
def verify():
 p=pins();r=json.loads((N/'replay_result.json').read_bytes())
 assert r['state']=='actual_quote_input_replay_completed' and r['native_invocations']==1
 assert r['protocol_sha256']==digest(PP) and r['helper_sha256']==p['helper_sha256']
 assert r['returncode']==0 and r['counts']=={'files':4,'source_rows':3411198,'decision_rows':1944}
 assert r['raw_values_to_AI_Git']==r['GETs']==r['economic_runs']==0
 base=_native_root(p['native_root']);directory=base/p['output_directory']
 expected_result=directory/'result.json'
 assert Path(r['native_result_path'])==expected_result
 _no_links(base,expected_result.relative_to(base))
 native=json.loads(expected_result.read_bytes())
 assert digest(r['native_result_path'])==r['native_result_sha256']
 assert native['state']=='quote_input_replay_completed' and native['counts']==r['counts']
 assert native['elapsed_seconds']<=60 and native['peak_rss_bytes']<=536870912
 assert native['source_qualified'] is native['model_admitted'] is native['orders_enabled'] is False
 output_bytes=sum(f['output']['bytes'] for f in native['files'].values())
 assert output_bytes==r['resources']['derived_output_bytes']<=p['limits']['derived_output_bytes']
 for f in native['files'].values():
  ref=f['output'];target=directory/ref['relative'];_no_links(base,target.relative_to(base))
  assert target.stat().st_size==ref['bytes'] and target.stat().st_nlink==1 and digest(target)==ref['sha256']
 pins()
 print(json.dumps({'actual_quote_input_replay_verified':True,'counts':r['counts'],'raw_values_to_AI_Git':0,'source_or_strategy_admitted':False}))
def produce():
 p=pins()
 assert not (N/'replay_result.json').exists()
 review=json.loads((N/'fixed_precode_review.json').read_bytes())
 assert review['precode_passed'] and review['current_P0_P1_P2']=={'P0':0,'P1':0,'P2':0}
 assert review['protocol_sha256']==digest(PP)
 started=datetime.now(timezone.utc).isoformat()
 assert datetime.fromisoformat(review['primary_transcribed_utc'])<datetime.fromisoformat(started)
 r={'state':'actual_quote_input_replay_started','started_utc':started,'protocol_sha256':digest(PP),
    'helper_sha256':p['helper_sha256'],'native_invocations':1,'raw_values_to_AI_Git':0,'GETs':0,'economic_runs':0,
    'source_or_strategy_admitted':False,'full_goal_complete':False}
 try:
  call=subprocess.run([sys.executable,'-X','utf8','-m','factor_system.run','--native-root',p['native_root'],
    '--request-relative',p['request_ref']['relative'],'--request-sha256',p['request_ref']['sha256']],
    capture_output=True,text=True,encoding='utf-8',timeout=70,
    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  r['returncode']=call.returncode
  assert call.returncode==0 and not call.stderr
  result=json.loads(call.stdout)
  assert result['state']=='quote_input_replay_completed' and result['ok'] is True
  assert result['source_qualified'] is result['model_admitted'] is result['orders_enabled'] is False
  assert result['counts']=={'files':4,'source_rows':3411198,'decision_rows':1944}
  r['counts']=result['counts']
  path=Path(p['native_root'])/p['output_directory']/'result.json'
  native=json.loads(path.read_bytes())
  assert native['counts']==r['counts'] and native['spec_sha256']==p['spec_content_sha256']
  assert native['elapsed_seconds']<=60 and native['peak_rss_bytes']<=536870912
  r['native_result_path']=str(path);r['native_result_sha256']=digest(path)
  r['valid_counts']={k:sum(x['valid_counts'][k] for x in native['files'].values())
      for k in ('spread_valid','best_imbalance_valid','five_imbalance_valid')}
  r['ambiguous_counts']={k:sum(x['ambiguous_counts'][k] for x in native['files'].values())
      for k in ('quoted_spread_fraction','best_depth_imbalance','five_depth_imbalance')}
  r['stale_or_missing_decision_rows']=sum(x['stale_or_missing_decision_rows'] for x in native['files'].values())
  r['resources']={'elapsed_seconds':native['elapsed_seconds'],'peak_rss_bytes':native['peak_rss_bytes'],
    'derived_output_bytes':sum(x['output']['bytes'] for x in native['files'].values())}
  assert r['resources']['derived_output_bytes']<=p['limits']['derived_output_bytes']
  pins();r['state']='actual_quote_input_replay_completed'
 except BaseException as exc:
  r['state']='actual_quote_input_replay_failed';r['error_type']=type(exc).__name__;raise
 finally:
  r['ended_utc']=datetime.now(timezone.utc).isoformat()
  fd=os.open(N/'replay_result.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
  with os.fdopen(fd,'wb') as stream:
   stream.write((json.dumps(r,ensure_ascii=False,indent=2)+'\n').encode());stream.flush();os.fsync(stream.fileno())
 print(json.dumps({'state':r['state'],'counts':r['counts'],'raw_values_to_AI_Git':0,'source_or_strategy_admitted':False}))
if __name__=='__main__':
 if sys.argv[1:]==['--verify']:verify()
 elif not sys.argv[1:]:produce()
 else:raise SystemExit(2)
