"""Offline triage of already-captured tree paths; filename clues never prove data."""
import hashlib,json,re,sys
from pathlib import Path
from datetime import datetime,timezone
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/OWNER_TREE_CANDIDATE_PROTOCOL_20261003.json';O=R/'research/OWNER_TREE_CANDIDATE_RESULT_20261003.json'
EXTS=('.csv.gz','.parquet','.hdf5','.sqlite3','.sqlite','.h5','.db','.csv','.zip','.7z')
QUANT=('regis','rqalpha','ricequant','rqdatac','tushare','baostock','cninfo','akshare','jqdata','joinquant','qlib','xtquant','ashare','a_share','quantqc','pit_panel','factor_system','stock','equity','finance','financial','market','quotes','ohlc','kline','factor','backtest','行情','股票','量化','因子','回测','策略')
OTHER=('vex','robot','robotics','tournament','competition','douyin','suibi','赛事','机器人','抖音','视频')
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def clue(repo,path):
 text=(repo+'/'+path).casefold();ext=next((e for e in EXTS if text.endswith(e)),None)
 return ext,any(x in text for x in QUANT),any(x in text for x in OTHER),re.search(r'(?<!\d)[036]\d{5}(?!\d)',text) is not None
def inputs(s):
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes']
 assert {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['id']==s['parent_task'] and t['status']=='in_progress' and t['owner_session']==s['execution_gate']['owner_session']
 assert t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert read(P)==s and sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert s['max_new_network_or_credential_calls']==0 and s['max_file_or_blob_content_reads']==0 and s['max_tree_files']==3 and s['max_candidate_rows']==204
 assert s['keyword_clues']=={'quant':list(QUANT),'other_context':list(OTHER),'code_shape':'(?<!\\d)[036]\\d{5}(?!\\d)'}
 inputs(s);return t
def evaluate(s):
 import runpy
 m=runpy.run_path(str(R/'research/capture_owner_other_tree.py'),run_name='frozen_tree_summary_only')
 source=read(R/'research/OWNER_OTHER_TREE_RESULT_20261003.json');oldp=read(R/'research/OWNER_OTHER_TREE_PROTOCOL_20261003.json');oldreceipt=Path(oldp['native_directory'])/'receipt.json';assert sha(oldreceipt)==source['native_receipt_sha256'];old=read(oldreceipt);rows=[];details=[]
 assert len(s['specs'])==len(source['summaries'])==len(old['entries'])==3
 for spec,existing,entry in zip(s['specs'],source['summaries'],old['entries']):
  assert spec['repo']==existing['repo'];p=Path(spec['native_tree']);raw=p.read_bytes();assert len(raw)<=1048576
  assert spec['repo']==entry['repo'] and str(p)==entry['raw_path'] and sha(p)==entry['raw_sha256']==s['native_input_hashes'][str(p)]
  prior=existing['summary'];assert m['summarize'](raw,200,True)==prior and prior['complete_declared_single_tree']
  j=json.loads(raw.decode('utf-8-sig'));bins={e:0 for e in EXTS};q=other=code=both=neither=large=empty=0;selected=0
  for item in j['tree']:
   if item['type']!='blob':continue
   ext,isq,iso,isc=clue(spec['repo'],item['path'])
   if ext is None:continue
   selected+=1;bins[ext]+=1;q+=isq;other+=iso;code+=isc;both+=isq and iso;neither+=not isq and not iso;large+=item['size']>=1048576;empty+=item['size']==0
   details.append({'repo':spec['repo'],'path':item['path'],'blob_sha':item['sha'],'size':item['size'],'format':ext,'quant_filename_clue':isq,'other_repository_context_clue':iso,'six_digit_code_shape_clue':isc,'actual_data_identity_or_license_verified':False})
  assert selected==prior['extension_only_data_or_archive_candidates']
  rows.append({'repo':spec['repo'],'tree_sha':j['sha'],'candidate_rows':selected,'format_counts':bins,'quant_filename_clue_rows':q,'other_context_clue_rows':other,'both_clue_rows':both,'neither_clue_rows':neither,'six_digit_code_shape_clue_rows':code,'at_least_1MiB_candidates':large,'empty_candidates':empty,'filename_or_repository_context_proves_contents':False})
 assert len(details)==204 and len(details)<=s['max_candidate_rows']
 return {'sources':rows,'total_candidates':len(details),'quant_filename_clue_rows':sum(r['quant_filename_clue_rows'] for r in rows),'new_network_or_credential_calls':0,'file_or_blob_content_reads':0,'original_dataset_recovered':0,'all_other_candidates_are_nonquant_certified':False,'license_or_full_history_absence_certified':False,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False},details
def persist(D,c,summary):
 errors=[]
 def attempt(label,fn):
  try:
   value=fn();return True if value is None else value
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 c['terminal_recording_errors']=errors;attempt('receipt_write',lambda:write(D/'receipt.json',c))
 hs={k:attempt(k,lambda p=p:sha(p)) for k,p in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_protocol_sha256',D/'protocol.json'),('native_task_sha256',D/'frozen_task.json'),('native_receipt_sha256',D/'receipt.json')]}
 r={'state':'actual_offline_tree_candidate_clue_audit' if not errors and not c.get('failure_type') else 'actual_failure_preserved',**hs,'summary':summary,'failure_type':c.get('failure_type'),'terminal_recording_errors':errors,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 if attempt('result_write',lambda:write(O,r)) is None:r['state']='actual_failure_preserved';attempt('fallback_write',lambda:write(D/'terminal_failure.json',r))
 return r
def compute(s):
 t=gate(s);assert read(O)['state']=='not_run';D=Path(s['native_directory']);assert not D.exists() and 'onedrive' not in str(D.resolve()).lower()
 c={'started_utc':datetime.now(timezone.utc).isoformat(),'new_network_or_credential_calls':0,'file_or_blob_content_reads':0};summary=None
 try:
  D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t);summary,details=evaluate(s);write(D/'candidate_path_clues.json',details);c['native_detail_sha256']=sha(D/'candidate_path_clues.json')
 except Exception as e:c['failure_type']=type(e).__name__
 c['ended_utc']=datetime.now(timezone.utc).isoformat();r=persist(D,c,summary);print(json.dumps({'state':r['state'],'summary':summary,'failure_type':r['failure_type']}))
def verify(s):
 inputs(s);r=read(O);D=Path(s['native_directory']);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert r['implementation_sha256']==sha(__file__) and r['protocol_sha256']==sha(P)==r['native_protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s
 assert r['native_receipt_sha256']==sha(D/'receipt.json') and r['native_task_sha256']==sha(D/'frozen_task.json') and t['contract_sha256']==contract_hash(t)==s['execution_gate']['expected_contract_sha256']
 if r['summary'] is not None:
  summary,details=evaluate(s);assert summary==r['summary'] and details==read(D/'candidate_path_clues.json') and c['native_detail_sha256']==sha(D/'candidate_path_clues.json')
 assert not c['new_network_or_credential_calls'] and not c['file_or_blob_content_reads'] and not r['new_economic_runs'] and not r['model_admitted'] and not r['full_goal_complete']
 print(json.dumps({'actual_offline_candidate_clues_verified':True,'new_network_or_credentials':0,'original_dataset_recovered':0}))
def counterexamples():
 assert clue('repo','STOCK.CSV.GZ')[:3]==('.csv.gz',True,False)
 assert clue('vex-research','finance.csv')[:3]==('.csv',True,True)
 assert clue('repo','archive.zip')[:3]==('.zip',False,False)
 assert clue('robot','stock.csv')[1:3]==(True,True)
 assert clue('repo','table.csv.bak')[0] is None
 assert clue('repo','table.csv')[3] is False and clue('repo','300001.csv')[3] is True
 assert clue('repo','13000012.csv')[3] is False
 print(json.dumps({'pure_filename_clue_cases':8,'network_or_credentials_or_filecontents':0,'clues_are_not_contents_or_license':True}))
if __name__=='__main__':
 try:
  if '--compute' in sys.argv:compute(read(P))
  elif '--verify' in sys.argv:verify(read(P))
  elif '--counterexamples' in sys.argv:counterexamples()
  else:raise ValueError('mode')
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'private_paths_not_emitted':True}));raise SystemExit(1)
