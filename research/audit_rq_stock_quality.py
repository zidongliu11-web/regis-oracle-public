"""Draft for next frozen native date/identity audit; no returns, price outputs or SDK."""
import argparse,json,time
from collections import Counter
from pathlib import Path
import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset
from read_instruments_static import parse,date_int
from inspect_rq_stock_history import event_dates

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/RQ_STOCK_QUALITY_PROTOCOL_20261002.json'
RESULT=ROOT/'research/RQ_STOCK_QUALITY_RESULT_20261002.json'

def run(spec):
    started=time.monotonic();peak=0;last_memory=0
    def checkpoint():
        nonlocal peak,last_memory
        clock=time.monotonic()
        if clock-started>spec['limits']['seconds']:raise ValueError('quality wallclock limit')
        if clock-last_memory>=1:
            peak=max(peak,base.peak_rss());last_memory=clock
            if peak>spec['limits']['peak_rss_bytes']:raise ValueError('quality memory limit')
    for path,expected in spec['input_hashes'].items():
        if base.digest(ROOT/path)!=expected:raise ValueError('quality frozen input changed')
    cap=json.loads((ROOT/spec['acquisition_receipt']).read_text(encoding='utf-8'))
    local=Path(cap['raw_directory']);h5py,np=base.load_dependencies();identities,_=parse((local/'instruments.pk').read_bytes(),checkpoint=checkpoint)
    identities={r['order_book_id']:r for r in identities if r.get('type')=='CS'}
    cal=base.read_calendar(local/'trading_dates.npy',np);cal=np.array([int(d) for d in cal if spec['start_date']<=int(d)<=spec['end_date']]);cal_set=set(map(int,cal))
    states={};state_stats={}
    for member in ['st_stock_days.h5','suspended_days.h5']:
        sets={};stats=Counter()
        with h5py.File(local/member,'r',rdcc_nbytes=1048576) as file:
            for key in file:
                checkpoint();ds=safe_dataset(file,key,h5py,np);dates,invalid=event_dates(ds[:],np)
                unique=set(map(int,dates));sets[key]=unique;stats['keys']+=1;stats['numeric_invalid']+=invalid
                duplicates=len(dates)-len(unique);stats['duplicate_rows']+=duplicates;stats['keys_with_duplicates']+=int(duplicates>0)
                stats['keys_not_ascending']+=int(bool(np.any(np.diff(dates)<=0)))
                stats['keys_strictly_descending']+=int(len(dates)>1 and bool(np.all(np.diff(dates)<0)))
                stats['selected_unique_events']+=len(unique&cal_set)
        states[member]=sets;state_stats[member]=dict(stats)
    counts=Counter();missing_groups=Counter();annual_missing=Counter();detail={};priced=set()
    with h5py.File(local/'stocks.h5','r',rdcc_nbytes=1048576) as file:
        if len(file)>spec['limits']['datasets']:raise ValueError('quality dataset limit')
        for key in file:
            checkpoint();priced.add(key);ds=safe_dataset(file,key,h5py,np)
            dates=ds.fields('datetime')[:].astype(np.int64);dates=np.where(dates>100000000,dates//1000000,dates)
            observed=set(int(d) for d in dates if spec['start_date']<=int(d)<=spec['end_date'])
            identity=identities.get(key);listed=date_int(identity.get('listed_date')) if identity else None;delisted=date_int(identity.get('de_listed_date')) if identity else None
            counts['priced_keys']+=1;counts['priced_unknown_listing']+=int(listed is None)
            if listed is None:continue
            # Explicit alternative boundary tallies: delist date excluded for lifetime expectation.
            expected=set(int(d) for d in cal if int(d)>=listed and (delisted is None or int(d)<delisted))
            missing=expected-observed;halts=states['suspended_days.h5'].get(key,set());st=states['st_stock_days.h5'].get(key,set())
            halted_missing=missing&halts;unexplained=missing-halts
            counts['expected_lifetime_sessions']+=len(expected);counts['missing_lifetime_sessions']+=len(missing);counts['missing_matching_reported_halt']+=len(halted_missing);counts['missing_without_reported_halt']+=len(unexplained)
            counts['priced_keys_missing_sessions']+=int(bool(missing));counts['priced_keys_unexplained_missing']+=int(bool(unexplained))
            counts['priced_keys_without_ST_dataset']+=int(key not in states['st_stock_days.h5']);counts['priced_keys_without_halt_dataset']+=int(key not in states['suspended_days.h5'])
            counts['observed_rows_on_reported_halt']+=len(observed&halts);counts['observed_rows_on_delist_date']+=int(delisted is not None and delisted in observed)
            for d in unexplained:annual_missing[str(d//10000)]+=1
            detail[key]={'missing_dates':sorted(missing),'missing_reported_halts':sorted(halted_missing),'unexplained_dates':sorted(unexplained),'listed_date':listed,'de_listed_date':delisted,'ST_dataset_present':key in states['st_stock_days.h5'],'halt_dataset_present':key in states['suspended_days.h5']}
    for key,identity in identities.items():
        if key in priced:continue
        listed=date_int(identity.get('listed_date'));delisted=date_int(identity.get('de_listed_date'))
        reason='unknown_listing' if listed is None else 'listed_after_window' if listed>spec['end_date'] else 'delisted_before_or_at_window_start' if delisted is not None and delisted<=spec['start_date'] else 'lifetime_overlaps_window_unpriced'
        missing_groups[reason]+=1
        detail[key]={'no_price_dataset':True,'reason':reason,'listed_date':listed,'de_listed_date':delisted}
    path=local/'stock_calendar_gap_audit.json';path.write_text(json.dumps(detail,ensure_ascii=False,separators=(',',':')),encoding='utf-8');checkpoint()
    result={'schema':'regis.rq.stock_quality.v1','state':'computed','ended_utc':base.utc(),'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'counts':dict(counts),'identities_without_price_dataset':dict(missing_groups),'annual_unexplained_sessions':dict(sorted(annual_missing.items())),'event_sets':state_stats,'local_details':{'path':str(path),'sha256':base.digest(path),'contains_prices':False},'source_semantics':spec['source_semantics'],'elapsed_seconds':round(time.monotonic()-started,3),'peak_rss_bytes':peak,'economic_run':False,'model_admitted':False,'full_A_coverage_certified':False,'source_independent_completeness_certified':False,'historical_feature_PIT_certified':False}
    RESULT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');print(json.dumps({k:result[k] for k in ['state','counts','identities_without_price_dataset','event_sets','economic_run']},ensure_ascii=False))

def verify(spec):
    r=json.loads(RESULT.read_text(encoding='utf-8'))
    if r['state']!='computed' or r['protocol_sha256']!=base.digest(PROTOCOL) or r['implementation_sha256']!=base.digest(__file__):raise ValueError('quality source changed')
    for p,h in spec['input_hashes'].items():
        if base.digest(ROOT/p)!=h:raise ValueError('quality input changed')
    if base.digest(r['local_details']['path'])!=r['local_details']['sha256'] or any(r[k] for k in ['economic_run','model_admitted','full_A_coverage_certified','source_independent_completeness_certified']):raise ValueError('quality evidence scope')
    print(json.dumps({'quality_source_verified':True,'counts':r['counts'],'economic_run':False}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument('--inspect',action='store_true');mode.add_argument('--verify',action='store_true');args=parser.parse_args();spec=json.loads(PROTOCOL.read_text(encoding='utf-8'))
    if args.verify:verify(spec)
    else:
        if json.loads(RESULT.read_text(encoding='utf-8'))['state']!='not_run':raise ValueError('quality already attempted')
        started_utc=base.utc()
        try:run(spec)
        except Exception as e:
            RESULT.write_text(json.dumps({'schema':'regis.rq.stock_quality.v1','state':'failed','started_utc':started_utc,'ended_utc':base.utc(),'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'failure':{'type':type(e).__name__,'reason':str(e)},'economic_run':False,'model_admitted':False},indent=2)+'\n',encoding='utf-8');print(json.dumps({'state':'failed','type':type(e).__name__,'reason':str(e)}));raise SystemExit(1)
