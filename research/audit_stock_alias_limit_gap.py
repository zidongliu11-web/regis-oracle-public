"""Localise preserved limit-down disagreement and its frozen comparison predicate."""
import argparse,json,time
from pathlib import Path
import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset
from audit_stock_action_roles import normalize
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_LIMIT_GAP_PROTOCOL_20261003.json';RESULT=ROOT/'research/STOCK_ALIAS_LIMIT_GAP_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if base.digest(ROOT/p)!=h:raise ValueError('limit_gap_input_changed')
 for p,h in s['native_input_hashes'].items():
  if base.digest(p)!=h:raise ValueError('native_limit_gap_input_changed')
def derive(s):
 check(s);h5py,np=base.load_dependencies();a,b=s['old'],s['new'];cut=s['cutover'];data={}
 with h5py.File(s['stocks'],'r',rdcc_nbytes=1048576) as f:
  for key in [a,b]:
   ds=safe_dataset(f,key,h5py,np);dates=normalize(ds.fields('datetime')[:],np);left=int(np.searchsorted(dates,s['start_date']));right=int(np.searchsorted(dates,min(s['end_date'],cut-1),side='right'))
   data[key]=(dates[left:right],ds.fields(['close','limit_down'])[left:right])
 dates,ai,bi=np.intersect1d(data[a][0],data[b][0],assume_unique=True,return_indices=True)
 old,new=data[a][1][ai],data[b][1][bi];mask=old['limit_down']!=new['limit_down'];changed=(old['close']>old['limit_down'])!=(new['close']>new['limit_down'])
 if int(mask.sum())!=9 or not np.array_equal(old['close'],new['close']):raise ValueError('preserved_limit_down_gap_not_reconstructed')
 prior=read(s['native_original_intents'])['intents'];selected={(x['signal_date'],x['instrument_id']) for x in prior};records=[]
 for i in np.flatnonzero(mask):
  day=int(dates[i]);records.append({'session':day,'old':a,'new':b,'close_greater_than_limit_down_old':bool(old['close'][i]>old['limit_down'][i]),'close_greater_than_limit_down_new':bool(new['close'][i]>new['limit_down'][i]),'original_old_selected':(day,a) in selected,'original_new_selected':(day,b) in selected,'new_identifier_valid_by_declared_cutover':day>=cut,'numeric_limit_down_or_close_values_exported':False})
 facts={'old':a,'new':b,'cutover':cut,'actual_limit_down_different_sessions':len(records),'mismatch_first_session':min(x['session'] for x in records),'mismatch_last_session':max(x['session'] for x in records),'frozen_close_greater_than_limit_down_predicate_different_sessions':int((mask&changed).sum()),'original_old_selected_on_mismatch_sessions':sum(x['original_old_selected'] for x in records),'original_new_selected_on_mismatch_sessions':sum(x['original_new_selected'] for x in records),'new_ID_is_invalid_on_all_these_sessions':all(not x['new_identifier_valid_by_declared_cutover'] for x in records),'raw_fields_repaired_or_rescaled':False,'field_correctness_independently_certified':False,'native_values_exported':False,'profit_or_cash_runs':0,'model_admitted':False,'full_goal_complete':False}
 check(s);return records,facts
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_limit_gap_result_preserved')
 local=Path(s['native_directory'])
 if local.exists() or 'onedrive' in str(local).lower():raise ValueError('unsafe_limit_gap_directory')
 local.mkdir(parents=True);start=time.monotonic();rows,facts=derive(s);detail=local/'limit_gap_session_predicates.json';write(detail,rows);manifest=local/'limit_gap_manifest.json';write(manifest,{'files':{str(detail):base.digest(detail)}})
 r={'state':'actual_nine_limit_down_gap_dates_and_frozen_predicate_diagnostic','observed_utc':base.utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'facts':facts,'local_details':{'path':str(manifest),'sha256':base.digest(manifest)},'limitations':s['limitations']};write(RESULT,r);print(json.dumps({'state':r['state'],'facts':facts}))
def verify(s):
 r=read(RESULT)
 if r['state']!='actual_nine_limit_down_gap_dates_and_frozen_predicate_diagnostic' or r['protocol_sha256']!=base.digest(PROTOCOL) or r['implementation_sha256']!=base.digest(__file__) or base.digest(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('limit_gap_evidence_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if base.digest(p)!=h:raise ValueError('native_limit_gap_detail_changed')
 rows,facts=derive(s)
 if facts!=r['facts'] or rows!=read(Path(r['local_details']['path']).parent/'limit_gap_session_predicates.json'):raise ValueError('gap_predicates_not_reconstructed')
 print(json.dumps({'nine_gap_dates_and_comparison_predicates_rebuilt':True,'raw_fields_corrected':False,'native_prices_exported':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
