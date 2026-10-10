"""Bounded native four-pair comparisons; no quote/action amounts leave native RAM."""
import argparse,json,time
from collections import Counter
from pathlib import Path
import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset
from audit_stock_action_roles import normalize
from inspect_rq_stock_history import event_dates

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_CONCORDANCE_PROTOCOL_20261003.json'
RESULT=ROOT/'research/STOCK_ALIAS_CONCORDANCE_RESULT_20261003.json'

def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if base.digest(ROOT/p)!=h:raise ValueError('frozen_concordance_input_changed')
 for p,h in s['native_input_hashes'].items():
  if base.digest(p)!=h:raise ValueError('native_concordance_input_changed')

def exact_numeric_counts(a,b,np):
 if a.shape!=b.shape or a.dtype.kind not in 'iuf' or b.dtype.kind not in 'iuf':raise ValueError('unsupported_numeric_compare')
 equal=(a==b)|(np.isnan(a)&np.isnan(b))
 return {'compared':int(a.size),'different':int((~equal).sum()),'old_nonfinite':int((~np.isfinite(a)).sum()),'new_nonfinite':int((~np.isfinite(b)).sum())}

def canonical_records(rows,np):
 # Only local tuples; all monetary fields remain inside this native process.
 names=rows.dtype.names
 if not names or any(rows.dtype.fields[n][0].kind not in 'iuf' for n in names):raise ValueError('unsupported_action_record_schema')
 def value(v):
  return ('missing',) if np.isnan(v) else ('number',int(v) if v.dtype.kind in 'iu' else float(v))
 return Counter(tuple(value(row[n]) for n in names) for row in rows)

def derive(s):
 started=time.monotonic();check(s);h5py,np=base.load_dependencies();local=Path(s['raw_directory']);out=[]
 members=['stocks.h5','st_stock_days.h5','suspended_days.h5','dividends.h5','split_factor.h5','ex_cum_factor.h5']
 files={name:h5py.File(local/name,'r',rdcc_nbytes=1048576) for name in members}
 try:
  for pair in s['pairs']:
   old,new=pair['old'],pair['new'];cut=pair['trading_identifier_effective_session'];f=files['stocks.h5'];p={}
   for key in [old,new]:
    if key not in f:raise ValueError('declared_pair_prices_missing')
    ds=safe_dataset(f,key,h5py,np);dates=normalize(ds.fields('datetime')[:],np)
    if np.any(np.diff(dates)<=0):raise ValueError('pair_dates_not_strictly_ascending')
    if not set(s['price_fields'])<=set(ds.dtype.names):raise ValueError('pair_price_schema_changed')
    left=int(np.searchsorted(dates,s['start_date']));right=int(np.searchsorted(dates,min(s['end_date'],cut-1),side='right'))
    p[key]=(dates[left:right],ds.fields(s['price_fields'])[left:right])
   common,ai,bi=np.intersect1d(p[old][0],p[new][0],assume_unique=True,return_indices=True)
   quotes={name:exact_numeric_counts(p[old][1][name][ai],p[new][1][name][bi],np) for name in s['price_fields']}
   result={'old':old,'new':new,'cutover':cut,'old_pre_cut_training_rows':len(p[old][0]),'new_pre_cut_training_rows':len(p[new][0]),'overlap_rows':len(common),'old_dates_only':len(p[old][0])-len(common),'new_dates_only':len(p[new][0])-len(common),'native_numeric_field_compare_counts':quotes,'states':{},'actions':{}}
   for name in ['st_stock_days.h5','suspended_days.h5']:
    sets={};present={}
    for key in [old,new]:
     present[key]=key in files[name]
     if not present[key]:sets[key]=set();continue
     vals,bad=event_dates(safe_dataset(files[name],key,h5py,np)[:],np)
     if bad:raise ValueError('invalid_pair_state_dates')
     sets[key]={int(x) for x in vals if s['start_date']<=int(x)<=s['end_date'] and int(x)<cut}
    # Also compare only actual shared bar dates, not absent securities' dates.
    shared=set(map(int,common));a=sets[old]&shared;b=sets[new]&shared
    result['states'][name]={'old_dataset_present':present[old],'new_dataset_present':present[new],'old_pre_cut_events':len(sets[old]),'new_pre_cut_events':len(sets[new]),'shared_quote_date_state_mismatches':len(a^b),'all_pre_cut_date_set_mismatches':len(sets[old]^sets[new]),'missing_False_convention_only_not_PIT_truth':True}
   for name,datefield in [('dividends.h5','ex_dividend_date'),('split_factor.h5','ex_date'),('ex_cum_factor.h5','start_date')]:
    rows={};present={};dates={};changes={}
    for key in [old,new]:
     present[key]=key in files[name]
     rows[key]=safe_dataset(files[name],key,h5py,np)[:] if present[key] else None
     if rows[key] is None:dates[key]=np.array([],dtype=np.int64);changes[key]=set();continue
     dates[key]=normalize(rows[key][datefield],np)
     if np.any(np.diff(dates[key])<0):raise ValueError('pair_action_dates_not_ascending')
     if name=='ex_cum_factor.h5':
      changed=np.zeros(len(rows[key]),dtype=bool)
      if len(rows[key])>1:changed[1:]=rows[key]['ex_cum_factor'][1:]!=rows[key]['ex_cum_factor'][:-1]
      changes[key]={int(x) for x in dates[key][changed] if s['start_date']<=int(x)<=s['end_date'] and int(x)<cut}
    selected={key:None if rows[key] is None else rows[key][(dates[key]>=s['start_date'])&(dates[key]<=s['end_date'])&(dates[key]<cut)] for key in [old,new]}
    item={'old_dataset_present':present[old],'new_dataset_present':present[new],'old_selected_rows':0 if selected[old] is None else len(selected[old]),'new_selected_rows':0 if selected[new] is None else len(selected[new]),'independent_issuer_actions_certified':False}
    if selected[old] is not None and selected[new] is not None:
     if selected[old].dtype!=selected[new].dtype:raise ValueError('pair_action_schema_mismatch')
     aa=canonical_records(selected[old],np);bb=canonical_records(selected[new],np)
     item.update(old_unmatched_exact_records=sum((aa-bb).values()),new_unmatched_exact_records=sum((bb-aa).values()))
    else:item.update(old_unmatched_exact_records=None,new_unmatched_exact_records=None)
    if name=='ex_cum_factor.h5':
     item.update(factor_change_date_mismatches=len(changes[old]^changes[new]),factor_level_rescaling_assumed=False)
    result['actions'][name]=item
   out.append(result)
   if time.monotonic()-started>s['max_seconds']:raise TimeoutError('pair_concordance_budget')
 finally:
  for f in files.values():f.close()
 check(s)
 return {'pairs':out,'native_price_values_exported':False,'native_action_cash_or_ratio_values_exported':False,'independent_price_action_or_PIT_certification':False,'research_past_data_reuse_condition_only':True,'new_GETs':0,'economic_runs':0,'reference_intents_repaired':False,'model_admitted':False,'full_goal_complete':False}

def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_concordance_result_preserved')
 local=Path(s['native_directory'])
 if local.exists() or 'onedrive' in str(local).lower():raise ValueError('unsafe_native_output_directory')
 local.mkdir(parents=True);start=time.monotonic();facts=derive(s);detail=local/'four_pair_concordance_counts.json';write(detail,facts)
 manifest=local/'concordance_manifest.json';write(manifest,{'files':{str(detail):base.digest(detail)}})
 r={'schema':'regis.stock.alias_concordance_result.v1','state':'actual_bounded_four_pair_native_concordance','observed_utc':base.utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'facts':facts,'local_details':{'path':str(manifest),'sha256':base.digest(manifest)},'limitations':s['limitations']};write(RESULT,r)
 print(json.dumps({'state':r['state'],'elapsed_seconds':r['elapsed_seconds'],'compared_overlap_rows':sum(x['overlap_rows'] for x in facts['pairs']),'quote_field_mismatches':sum(v['different'] for x in facts['pairs'] for v in x['native_numeric_field_compare_counts'].values()),'new_GETs':0,'economic_runs':0}))

def verify(s):
 r=read(RESULT)
 if r['state']!='actual_bounded_four_pair_native_concordance' or r['protocol_sha256']!=base.digest(PROTOCOL) or r['implementation_sha256']!=base.digest(__file__):raise ValueError('concordance_source_changed')
 if base.digest(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('concordance_manifest_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if base.digest(p)!=h:raise ValueError('concordance_native_detail_changed')
 if derive(s)!=r['facts']:raise ValueError('concordance_counts_not_rebuilt')
 print(json.dumps({'four_pair_native_concordance_rebuilt':True,'native_numeric_values_exported':False,'PIT_or_economic_certification':False}))

if __name__=='__main__':
 ap=argparse.ArgumentParser();mode=ap.add_mutually_exclusive_group(required=True);mode.add_argument('--run',action='store_true');mode.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.verify:verify(s)
 else:
  try:run(s)
  except Exception as e:
   # Keep failure evidence; never retry or overwrite the original attempted batch.
   if read(RESULT)['state']=='not_run':write(RESULT,{'state':'failed','observed_utc':base.utc(),'failure_type':type(e).__name__,'failure_reason':str(e),'economic_runs':0})
   print(json.dumps({'state':'failed','reason':str(e)}));raise SystemExit(1)
