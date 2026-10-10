"""Two extra native records: date roles and original past-event-filter union only."""
import argparse,json,time
from collections import Counter
from pathlib import Path
import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset
from audit_stock_action_roles import normalize
from audit_stock_alias_concordance import canonical_records
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_ACTION_GAP_PROTOCOL_20261003.json';RESULT=ROOT/'research/STOCK_ALIAS_ACTION_GAP_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if base.digest(ROOT/p)!=h:raise ValueError('action_gap_frozen_input_changed')
 for p,h in s['native_input_hashes'].items():
  if base.digest(p)!=h:raise ValueError('native_action_gap_input_changed')
def derive(s):
 check(s);h5py,np=base.load_dependencies();sets={};divs={};date_roles={};factors={};splits={};cut=s['cutover'];local=Path(s['raw_directory'])
 for key in [s['old'],s['new']]:
  known=set()
  for member,field in [('dividends.h5','ex_dividend_date'),('split_factor.h5','ex_date'),('ex_cum_factor.h5','start_date')]:
   with h5py.File(local/member,'r',rdcc_nbytes=1048576) as f:array=safe_dataset(f,key,h5py,np)[:]
   dates=normalize(array[field],np)
   if np.any(np.diff(dates)<0):raise ValueError('action_gap_dates_not_ascending')
   if member=='ex_cum_factor.h5':
    mask=np.zeros(len(dates),dtype=bool)
    if len(dates)>1:mask[1:]=array['ex_cum_factor'][1:]!=array['ex_cum_factor'][:-1]
    event_dates=dates[mask];factors[key]={int(d) for d in event_dates if s['start_date']<=int(d)<=s['end_date'] and int(d)<cut}
   else:
    event_dates=dates
    if member=='split_factor.h5':splits[key]={int(d) for d in dates if s['start_date']<=int(d)<=s['end_date'] and int(d)<cut}
    else:
     mask=(dates>=s['start_date'])&(dates<=s['end_date'])&(dates<cut);divs[key]=array[mask]
     date_roles[key]=[{field:int(normalize(np.array([row[field]]),np,field=='announcement_date')[0]) for field in ['announcement_date','book_closure_date','ex_dividend_date','payable_date']} for row in divs[key]]
   known|={int(d) for d in event_dates if 0<int(d)<cut and int(d)<=s['end_date']}
  # Compare the complete pre-cutover union, including history before2013;
  # original past_action_clear ignores pre-calendar events rather than windowing them.
  sets[key]=known
 old,new=s['old'],s['new'];a=canonical_records(divs[old],np);b=canonical_records(divs[new],np)
 if sum((a-b).values())!=0 or sum((b-a).values())!=2:raise ValueError('preserved_two_extra_dividend_records_not_rebuilt')
 date_a=Counter(tuple(sorted(x.items())) for x in date_roles[old]);remaining=dict(date_a);extra=[]
 # Match by complete numeric record, not exdate only. No cash field is exported.
 remaining_exact=dict(a)
 for row,roles in zip(divs[new],date_roles[new]):
  marker=next(iter(canonical_records(np.array([row],dtype=divs[new].dtype),np)))
  if remaining_exact.get(marker,0):remaining_exact[marker]-=1
  else:extra.append(roles)
 detail={'extra_current_ID_dividend_record_date_roles':extra,'old_pre_cutover_event_union':sorted(sets[old]),'new_pre_cutover_event_union':sorted(sets[new]),'dividend_cash_or_round_lot_values_exported':False}
 facts={'old':old,'new':new,'cutover':cut,'old_pre_cut_training_dividend_records':len(divs[old]),'new_pre_cut_training_dividend_records':len(divs[new]),'extra_current_ID_exact_dividend_records':len(extra),'extra_record_date_roles':extra,'pre_cutover_original_past_event_union_mismatches':len(sets[old]^sets[new]),'dividend_ex_date_set_mismatches':len({x['ex_dividend_date'] for x in date_roles[old]}^{x['ex_dividend_date'] for x in date_roles[new]}),'extra_ex_dates_already_in_original_old_factor_changes':sum(x['ex_dividend_date'] in factors[old] for x in extra),'actual_cash_records_independently_certified':False,'no_extra_record_deleted_or_copied':True,'individual_cash_or_ratio_values_exported':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 check(s);return detail,facts
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_action_gap_result_preserved')
 local=Path(s['native_directory'])
 if local.exists() or 'onedrive' in str(local).lower():raise ValueError('unsafe_action_gap_directory')
 local.mkdir(parents=True);start=time.monotonic();detail,facts=derive(s);p=local/'two_extra_dividend_date_roles.json';write(p,detail);manifest=local/'action_gap_manifest.json';write(manifest,{'files':{str(p):base.digest(p)}})
 r={'state':'actual_two_extra_dividend_date_roles_and_past_event_union_diagnostic','observed_utc':base.utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'facts':facts,'local_details':{'path':str(manifest),'sha256':base.digest(manifest)},'limitations':s['limitations']};write(RESULT,r);print(json.dumps({'state':r['state'],'facts':facts}))
def verify(s):
 r=read(RESULT)
 if r['state']!='actual_two_extra_dividend_date_roles_and_past_event_union_diagnostic' or r['protocol_sha256']!=base.digest(PROTOCOL) or r['implementation_sha256']!=base.digest(__file__) or base.digest(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('action_gap_evidence_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if base.digest(p)!=h:raise ValueError('action_gap_native_detail_changed')
 detail,facts=derive(s)
 if facts!=r['facts'] or detail!=read(Path(r['local_details']['path']).parent/'two_extra_dividend_date_roles.json'):raise ValueError('action_gap_not_rebuilt')
 print(json.dumps({'two_extra_dividend_date_roles_and_event_union_rebuilt':True,'individual_money_exported':False,'actual_entitlement_or_PIT_certified':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
