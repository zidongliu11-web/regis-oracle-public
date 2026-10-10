"""Declared-time industry coverage diagnostic; no quote arrays or factor returns."""
import argparse,hashlib,json,time,logging,sys
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter,defaultdict
import pypdf
from csrc_industry_members import parse_pdf_texts
from read_instruments_static import parse,date_int
import inspect_short_rq_full as base
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_MEMBER_COVERAGE_PROTOCOL_20261003.json';RESULT=ROOT/'research/CSRC_MEMBER_COVERAGE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def utc():return datetime.now(timezone.utc).isoformat()
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen coverage source changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('native original/firstattempt changed')
def run(s):
 check(s)
 if pypdf.__version__!=s['pypdf_version_expected']:raise ValueError('frozen PDF runtime version changed')
 if read(RESULT)['state']!='not_run':raise ValueError('preserve first full coverage diagnostic')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True)
 start=time.monotonic();started=utc();quarter_results=[];snapshots=[];pages_total=0
 source=read(s['source_packet'])['quarter_sources'];first=read(ROOT/'research/CSRC_INDUSTRY_MEMBER_RESULT_20261003.json');prior={x['quarter']:x for x in first['quarter_results']}
 if len(source)!=s['max_quarters'] or sum(Path(q['attachment']['native_path']).suffix=='.pdf' for q in source)!=s['expected_PDF_quarters']:raise ValueError('source quarter format census changed')
 logger=logging.getLogger('pypdf');handler=logging.FileHandler(local/'pypdf-layout.log',encoding='utf-8');logger.addHandler(handler)
 try:
  for q in sorted(source,key=lambda x:x['declared_publication_date']):
   quarter=q['quarter'];p=prior[quarter];entry={'quarter':quarter,'publication_date':p['publication_date'],'effective_session':p['effective_session'],'state':'unknown_source_member_marker','source_rows':p.get('rows'),'layout_checked':False};members={}
   try:
    if time.monotonic()-start>s['max_seconds']:raise TimeoutError('coverage diagnostic budget')
    if quarter=='2012Q4':raise ValueError('legacy source remained unknown after retained bounded attempts')
    if quarter=='2017Q1':
     repair=read(ROOT/'research/CSRC_MEMBER_REPAIR_RESULT_20261003.json');one=next(x for x in repair['specific_quarters'] if x['quarter']==quarter);members=read(one['native_path'])['members'];entry.update(source_rows=one['known_source_rows'],quarantined_rows=2,source_snapshot_complete=False)
    else:members=read(p['local_member_path'])['members'];entry['source_snapshot_complete']=True
    path=Path(q['attachment']['native_path'])
    if path.suffix=='.pdf':
     pdf=pypdf.PdfReader(str(path),strict=True);texts=[]
     for page in pdf.pages:
      if len(page.get_contents().get_data())>s['max_page_stream_bytes']:raise ValueError('page stream ceiling')
      texts.append(page.extract_text(extraction_mode='layout',layout_mode_space_vertically=False) or '')
     pages_total+=len(texts);target=local/(quarter+'.layout.txt');target.write_text('\n\f\n'.join(texts),encoding='utf-8')
     filtered=['\n'.join(line for line in text.splitlines() if quarter!='2017Q1' or '603026' not in line) for text in texts]
     alternative,counts=parse_pdf_texts(filtered)
     missing=set(members)-set(alternative);extra=set(alternative)-set(members);different=[k for k in set(members)&set(alternative) if members[k]['source_industry_major_numeric']!=alternative[k]['source_industry_major_numeric']]
     comparison=local/(quarter+'.comparison.json');write(comparison,{'missing_source_codes':sorted(missing),'extra_source_codes':sorted(extra),'different_source_class_codes':sorted(different),'same_library_distinct_algorithms':True})
     entry.update(layout_checked=True,pages=len(texts),layout_rows=len(alternative),missing_codes=len(missing),extra_codes=len(extra),class_disagreements=len(different),layout_reconciliation=counts)
     if missing or extra or different:raise ValueError('source layout/default classification mismatch')
     entry['state']='distinct_layout_and_default_match_source_not_PIT_certified'
    else:entry['state']='original_XLS_merged_anchor_structural_only_no_independent_engine'
   except Exception as e:
    members={};entry.update(state='unknown_source_member_marker',error_type=type(e).__name__,error=str(e)[:180])
   snapshots.append({'quarter':quarter,'effective_session':p['effective_session'],'state':entry['state'],'source_snapshot_complete':entry.get('source_snapshot_complete',False),'quarantined_rows':entry.get('quarantined_rows',0),'members':members})
   quarter_results.append(entry);print(json.dumps({'quarter':quarter,'state':entry['state'],'layout_rows':entry.get('layout_rows'),'class_disagreements':entry.get('class_disagreements')},ensure_ascii=False),flush=True)
 finally:logger.removeHandler(handler);handler.close()
 # This is a current producer-vintage metadata census, explicitly conditional.
 identities,_=parse(Path(s['instruments']).read_bytes());_,np=base.load_dependencies();calendar=list(map(int,base.read_calendar(Path(s['calendar']),np)));sessions=[x for x in calendar if s['start_session']<=x<=s['end_session']]
 census={x['order_book_id'].split('.')[0]:x for x in identities if x.get('type')=='CS'}
 if len(census)!=sum(x.get('type')=='CS' for x in identities):raise ValueError('nonunique CS identity')
 lifetimes={k:(date_int(x.get('listed_date')),date_int(x.get('de_listed_date'))) for k,x in census.items()};unknown_listing=sum(v[0] is None for v in lifetimes.values());monthly=defaultdict(list);daily=[];index=-1
 for day in sessions:
  while index+1<len(snapshots) and snapshots[index+1]['effective_session'] is not None and snapshots[index+1]['effective_session']<=day:index+=1
  snapshot=snapshots[index] if index>=0 else None;member={} if snapshot is None else snapshot['members']
  active={k for k,(first,last) in lifetimes.items() if first is not None and first<=day and (last is None or day<last)}
  assigned=active&set(member);unknown=active-assigned;groups=Counter(member[k]['source_industry_major_numeric'] for k in assigned)
  row={'session':day,'quarter':snapshot['quarter'] if snapshot else None,'snapshot_state':snapshot['state'] if snapshot else 'no_prior_published_snapshot','known_lifetime_active_CS':len(active),'known_source_classified_CS':len(assigned),'known_lifetime_unclassified_CS':len(unknown),'classified_fraction_known_lifetime':len(assigned)/len(active) if active else None,'unknown_listing_identities':unknown_listing,'group_member_counts':dict(sorted(groups.items())),'all_lifetime_known_CS_classified':not unknown,'full_A_census_certified':False}
  daily.append(row);monthly[day//100].append(row)
 target=local/'declared_time_daily_coverage.json';write(target,{'daily':daily,'complete_publication_markers_including_failures':True,'no_backward_or_skip_unknown_snapshot_carry':True,'lifetime_policy':s['lifetime_policy']})
 aggregates=[]
 for month,rows in sorted(monthly.items()):
  fractions=[x['classified_fraction_known_lifetime'] for x in rows if x['classified_fraction_known_lifetime'] is not None]
  aggregates.append({'month':month,'sessions':len(rows),'known_active_min':min(x['known_lifetime_active_CS'] for x in rows),'known_active_max':max(x['known_lifetime_active_CS'] for x in rows),'unclassified_min':min(x['known_lifetime_unclassified_CS'] for x in rows),'unclassified_max':max(x['known_lifetime_unclassified_CS'] for x in rows),'coverage_min':min(fractions) if fractions else None,'coverage_max':max(fractions) if fractions else None,'zero_coverage_sessions':sum(x['known_source_classified_CS']==0 for x in rows),'quarters_used':sorted({x['quarter'] for x in rows if x['quarter']}),'every_known_active_CS_classified_sessions':sum(x['all_lifetime_known_CS_classified'] for x in rows)})
 write(local/'explicit_snapshot_sequence.json',{'snapshots':snapshots,'failures_do_not_silently_carry_old_snapshot':True});check(s)
 manifest=local/'coverage_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}})
 r={'schema':'regis.csrc.member_coverage.v1','state':'actual_bounded_source_coverage_diagnostic','started_utc':started,'ended_utc':utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'pypdf_version':pypdf.__version__,'PDF_engine_independent':False,'distinct_algorithms_same_library':True,'quarter_results':quarter_results,'PDF_pages_layout_read':pages_total,'unknown_quarter_markers':sum(x['state']=='unknown_source_member_marker' for x in quarter_results),'source_metadata_CS':len(census),'unknown_listing_identities':unknown_listing,'sessions':len(sessions),'first_nonzero_class_coverage_session':next((x['session'] for x in daily if x['known_source_classified_CS']),None),'all_known_active_CS_classified_sessions':sum(x['all_lifetime_known_CS_classified'] for x in daily),'monthly_coverage':aggregates,'local_details':{'path':str(manifest),'sha256':sha(manifest),'contains_quote_arrays':False},'source_calendar_identities_originals_unchanged':True,'price_arrays_read':False,'source_full_A_census_certified':False,'PIT_full_member_semantics_certified':False,'sector_factor_computed':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'limits':s['limits']}
 write(RESULT,r);print(json.dumps({k:r[k] for k in ['state','elapsed_seconds','PDF_pages_layout_read','unknown_quarter_markers','sessions','first_nonzero_class_coverage_session','all_known_active_CS_classified_sessions','sector_factor_computed']},ensure_ascii=False))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']!='actual_bounded_source_coverage_diagnostic' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('coverage evidence identity changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('native coverage output changed')
 if any(r[k] for k in ['PDF_engine_independent','price_arrays_read','source_full_A_census_certified','PIT_full_member_semantics_certified','sector_factor_computed','economic_runs','model_admitted','full_goal_complete']):raise ValueError('coverage scope escalated')
 print(json.dumps({'coverage_evidence_verified':True,'explicit_failed_snapshot_markers':True,'economic_runs':0}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.run==a.verify:raise SystemExit('Select one mode')
 run(s) if a.run else verify(s)
