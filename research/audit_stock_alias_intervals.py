"""Four source-bound code intervals, conditional census and native ID-only impact audit."""
import argparse,bisect,calendar,hashlib,json,time
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import inspect_short_rq_full as base
from read_instruments_static import parse,date_int
from audit_stock_action_roles import normalize
from recover_rq_etf_history import safe_dataset
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_INTERVAL_PROTOCOL_20261003.json';RESULT=ROOT/'research/STOCK_ALIAS_INTERVAL_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen_alias_interval_input_changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('native_alias_interval_input_changed')
def qend(q):
 y,m=int(q[:4]),int(q[-1])*3;return y*10000+m*100+calendar.monthrange(y,m)[1]
def source_claims(s):
 evidence=[]
 for pair in s['pairs']:
  for p,h in pair['document_hashes'].items():
   if sha(p)!=h:raise ValueError('issuer_alias_original_changed')
  for claim in pair['claims']:
   text=''.join(Path(claim['native_text']).read_text(encoding='utf-8').split())
   if claim['fragment'] not in text:raise ValueError('source_alias_labelled_claim_missing')
  if pair['old'].split('.')[1]!=pair['new'].split('.')[1]:raise ValueError('cross_exchange_rename_not_allowed')
  evidence.append({k:pair[k] for k in ['old','new','trading_identifier_effective_session','source_publication_date_declared','document_hashes']})
 return evidence
def derive(s):
 started=time.monotonic();check(s);evidence=source_claims(s)
 rows,_=parse(Path(s['instruments']).read_bytes());cs={r['order_book_id']:r for r in rows if r.get('type')=='CS'}
 if len(cs)!=5581:raise ValueError('source_CS_census_changed')
 if len({x.split('.')[0] for x in cs})!=len(cs):raise ValueError('CS_exchange_code_collision_requires_new_contract')
 life={k:(date_int(v.get('listed_date')),date_int(v.get('de_listed_date'))) for k,v in cs.items()};fixed=dict(life);bykey={};corrections=[]
 for pair in s['pairs']:
  old,new,day=pair['old'],pair['new'],pair['trading_identifier_effective_session']
  if old not in cs or new not in cs or life[old][0]!=life[new][0] or life[old][1]!=day or life[new][0] is None or life[new][0]>=day:raise ValueError('declared_source_metadata_rename_mismatch')
  fixed[new]=(day,life[new][1]);bykey[old]=pair;bykey[new]=pair
  corrections.append({'id':new,'original_IPO_date':life[new][0],'corrected_trading_identifier_valid_from':day,'old_identifier_valid_until_exclusive':day,'lineage_key':'CS:'+old,'not_permanent_issuer_or_business_identity':True})
 def entity(key):return 'CS:'+bykey[key]['old'] if key in bykey else 'CS:'+key
 h5py,np=base.load_dependencies();cal=[int(x) for x in base.read_calendar(Path(s['calendar']),np)];sessions=[x for x in cal if s['start_session']<=x<=s['end_session']]
 if len(sessions)!=1945 or any(p['trading_identifier_effective_session'] not in cal for p in s['pairs']):raise ValueError('source_calendar_interval_boundary_changed')
 sequence=read(s['snapshot_sequence']);snapshots=sequence['snapshots'];markers=[x['effective_session'] for x in snapshots]
 if len(snapshots)!=32 or markers!=sorted(set(markers)):raise ValueError('source_publication_markers_changed')
 prior=read(s['prior_daily'])['daily'];daily=[];monthly=defaultdict(list);links=[];missing_rows=[];unknown=sum(v[0] is None for v in fixed.values())
 for i,day in enumerate(sessions):
  si=bisect.bisect_right(markers,day)-1;snapshot=snapshots[si] if si>=0 else None;members=snapshot['members'] if snapshot else {};quarter=snapshot['quarter'] if snapshot else None
  rawactive={k for k,(a,b) in life.items() if a is not None and a<=day and (b is None or day<b)}
  active={k for k,(a,b) in fixed.items() if a is not None and a<=day and (b is None or day<b)}
  if len({entity(k) for k in active})!=len(active):raise ValueError('duplicate_registered_lineage_still_active')
  assigned={};reasons=Counter()
  for key in sorted(active):
   stockcode=key.split('.')[0];sourcecode=stockcode;pair=bykey.get(key)
   if pair and quarter:
    sourcecode=(pair['old'] if qend(quarter)<pair['trading_identifier_effective_session'] else pair['new']).split('.')[0]
   member=members.get(sourcecode)
   if member is not None:
    assigned[key]=member
    if sourcecode!=stockcode:links.append({'session':day,'quarter':quarter,'active_trade_identifier':key,'source_code_as_of_quarter_end':sourcecode,'source_quarter_end':qend(quarter),'source_publication_effective_session':snapshot['effective_session'],'source_locator':member['source_locator'],'observed_source_class':member['source_industry_major_numeric'],'class_from_later_quarter':False})
   else:
    if snapshot is None:reason='no_prior_declared_publication'
    elif snapshot['state']=='unknown_source_member_marker':reason='explicit_unknown_source_quarter'
    elif pair and (pair['old'].split('.')[0] in members or pair['new'].split('.')[0] in members):reason='source_member_identifier_not_valid_at_declared_quarter_end'
    elif snapshot.get('quarantined_rows') and stockcode=='603026':reason='publisher_duplicate_identity_quarantined'
    elif fixed[key][0]>qend(quarter):reason='identifier_effective_after_quarter_end_possible_reporting_lag_not_proven'
    else:reason='valid_identifier_by_quarter_end_unclassified_source_metadata_gap_unresolved'
    reasons[reason]+=1;missing_rows.append({'session':day,'quarter':quarter,'active_trade_identifier':key,'reason':reason})
  old=prior[i]
  if old['session']!=day or old['quarter']!=quarter or old['known_lifetime_active_CS']!=len(rawactive):raise ValueError('firstconditionalcoverage_not_rebuilt')
  row={'session':day,'quarter':quarter,'raw_ID_denominator_first':len(rawactive),'four_pair_corrected_identifier_denominator':len(active),'registered_duplicate_issuer_lineages_active':0,'source_classified_active_identifiers':len(assigned),'unclassified_active_identifiers':len(active)-len(assigned),'coverage_conditional':len(assigned)/len(active) if active else None,'unknown_listing_identities':unknown,'source_group_counts':dict(sorted(Counter(v['source_industry_major_numeric'] for v in assigned.values()).items())),'reasons':dict(sorted(reasons.items())),'source_completeness_and_full_A_PIT_certified':False}
  daily.append(row);monthly[day//100].append(row)
 if unknown!=19:raise ValueError('unknown_listing_census_changed')
 aggregates=[]
 for month,rs in sorted(monthly.items()):
  rc=Counter()
  for r in rs:rc.update(r['reasons'])
  aggregates.append({'month':month,'sessions':len(rs),'corrected_known_active_min':min(r['four_pair_corrected_identifier_denominator'] for r in rs),'corrected_known_active_max':max(r['four_pair_corrected_identifier_denominator'] for r in rs),'unclassified_min':min(r['unclassified_active_identifiers'] for r in rs),'unclassified_max':max(r['unclassified_active_identifiers'] for r in rs),'coverage_min':min(r['coverage_conditional'] for r in rs),'coverage_max':max(r['coverage_conditional'] for r in rs),'all_known_active_classified_sessions':sum(r['unclassified_active_identifiers']==0 for r in rs),'remaining_member_days_reasons':dict(sorted(rc.items()))})
 # Metadata-only date columns from eight native datasets. No OHLC/volume/amount values.
 date_overlap=[]
 with h5py.File(s['stocks_dates_only'],'r',rdcc_nbytes=1048576) as f:
  for pair in s['pairs']:
   values={}
   for key in [pair['old'],pair['new']]:
    if key not in f:values[key]=set();continue
    ds=safe_dataset(f,key,h5py,np);dates=normalize(ds.fields('datetime')[:],np);values[key]={int(x) for x in dates if s['start_session']<=int(x)<=s['end_session']}
   a,b=values[pair['old']],values[pair['new']];cut=pair['trading_identifier_effective_session']
   date_overlap.append({'old':pair['old'],'new':pair['new'],'old_training_date_rows':len(a),'new_training_date_rows':len(b),'overlap_training_sessions':len(a&b),'new_key_rows_before_identifier_effective_date':sum(x<cut for x in b),'old_key_rows_on_or_after_identifier_effective_date':sum(x>=cut for x in a),'historical_quote_values_equal_or_continuity_verified':False})
 intents=read(s['native_intents'])['intents'];pairs_impact=[];byday=defaultdict(set)
 for row in intents:byday[row['signal_date']].add(row['instrument_id'])
 for pair in s['pairs']:
  cut=pair['trading_identifier_effective_session'];a,b=pair['old'],pair['new'];picks=[x for x in intents if x['instrument_id'] in [a,b]]
  pairs_impact.append({'old':a,'new':b,'registered_intents_old':sum(x['instrument_id']==a for x in picks),'registered_intents_new':sum(x['instrument_id']==b for x in picks),'new_identifier_selected_before_effective_date':sum(x['instrument_id']==b and x['signal_date']<cut for x in picks),'old_identifier_selected_on_or_after_effective_date':sum(x['instrument_id']==a and x['signal_date']>=cut for x in picks),'same_signal_date_both_ids_selected':sum(a in ids and b in ids for ids in byday.values())})
 books=read(s['native_conditional_book']);book_impact=[]
 for scenario,value in books.items():
  partial='native_partial_account_checkpoint' in value;detail=value.get('details',value.get('native_partial_account_checkpoint',{}));held=detail.get('held_dates')
  if not isinstance(held,dict):raise ValueError('registerednativebook_held_dates_unavailable')
  rows=[]
  for pair in s['pairs']:
   a,b,cut=pair['old'],pair['new'],pair['trading_identifier_effective_session'];ha=set(map(int,held.get(a,[])));hb=set(map(int,held.get(b,[])))
   rows.append({'old':a,'new':b,'same_held_date_both_ids':len(ha&hb),'new_identifier_held_dates_before_effective':sum(d<cut for d in hb),'old_identifier_held_dates_after_effective':sum(d>=cut for d in ha)})
  book_impact.append({'scenario':scenario,'partial_before_failure_only':partial,'identity_date_counts':rows,'individual_cash_quantities_or_money_values_emitted':False,'cash_name_caps_recomputed':False})
 if time.monotonic()-started>s['max_seconds']:raise TimeoutError('alias_interval_diagnostic_budget')
 check(s)
 payload={'interval_corrections':corrections,'daily':daily,'source_alias_class_links':links,'remaining_unknown_members':missing_rows,'unknown_listing_identities':sorted(k for k,v in fixed.items() if v[0] is None)}
 facts={'source_bound_pairs':evidence,'corrected_identifier_start_dates':4,'current_CS_metadata_keys':5581,'four_pair_research_lineage_keys':5577,'other_code_changes_audited':False,'sessions':1945,'source_publication_markers':32,'source_class_values_changed':0,'quarterly_source_member_arrays_changed':0,'unknown_listing_identities':19,'all_corrected_known_active_classified_sessions':sum(x['unclassified_active_identifiers']==0 for x in daily),'removed_spurious_ID_member_days':sum(x['raw_ID_denominator_first']-x['four_pair_corrected_identifier_denominator'] for x in daily),'source_previous_identifier_class_link_days':len(links),'remaining_unknown_member_days':len(missing_rows),'monthly_conditional_coverage':aggregates,'native_dataset_date_overlap':date_overlap,'registered_intent_identity_impact':pairs_impact,'registered_conditional_book_identity_impact':book_impact,'quote_timestamp_columns_read':True,'quote_numeric_values_read':False,'individual_cash_or_qty_emitted':False,'new_economic_runs':0,'same_day_source_publication_hour_or_full_PIT_certified':False,'all_registered_actual_trade_IDs_repaired':False,'model_admitted':False,'full_goal_complete':False}
 return payload,facts
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_alias_interval_result_preserved')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True);started=time.monotonic();payload,facts=derive(s)
 write(local/'conditional_identity_coverage_and_provenance.json',payload);manifest=local/'alias_interval_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}})
 r={'schema':'regis.stock.alias_interval_result.v1','state':'actual_four_pair_identifier_interval_census_repair_and_registered_ID_only_audit','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-started,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'facts':facts,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'limitations':s['limitations']};write(RESULT,r)
 print(json.dumps({'state':r['state'],'elapsed_seconds':r['elapsed_seconds'],'corrected_identifier_starts':4,'removed_spurious_ID_member_days':facts['removed_spurious_ID_member_days'],'new_trade_ID_pre_effective_selected':sum(x['new_identifier_selected_before_effective_date'] for x in facts['registered_intent_identity_impact']),'same_signal_both_ids':sum(x['same_signal_date_both_ids_selected'] for x in facts['registered_intent_identity_impact']),'all_corrected_known_active_classified_sessions':facts['all_corrected_known_active_classified_sessions'],'full_PIT_certified':False}))
def verify(s):
 r=read(RESULT)
 if r['state']!='actual_four_pair_identifier_interval_census_repair_and_registered_ID_only_audit' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('alias_interval_evidence_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('native_alias_derivative_changed')
 payload,facts=derive(s)
 if facts!=r['facts'] or payload!=read(Path(r['local_details']['path']).parent/'conditional_identity_coverage_and_provenance.json'):raise ValueError('alias_interval_or_impact_not_rebuilt')
 print(json.dumps({'four_source_bound_intervals_and_coverage_rebuilt':True,'registered_actual_ID_counts_rebuilt':True,'new_economic_run':False,'full_PIT_certified':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args()
 if a.run==a.verify:raise SystemExit('select_one_mode')
 run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
