"""Native stock-action/date census for a separate daily diagnostic contract.

No prices, volumes, per-event cash amounts, returns or candidate ranking exported.
This is source-structure reconciliation, not independent issuer certification.
"""
import argparse,json,time
from datetime import date
from pathlib import Path
from collections import Counter
import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset
from read_instruments_static import parse,date_int

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_DAILY_ROLE_PROTOCOL_20261003.json'
RESULT=ROOT/'research/STOCK_DAILY_ROLE_RESULT_20261003.json'

def normalize(values,np,allow_missing=False):
    v=np.asarray(values)
    if v.dtype.kind not in 'iuf':raise ValueError('numeric action date required')
    valid=np.isfinite(v)
    if np.any(np.isinf(v)):raise ValueError('infinite action date')
    if np.any(valid&((v<0)|(v!=np.floor(v))|(v>99991231000000))):raise ValueError('nonnegative exact integral action date required')
    if not allow_missing and np.any(~valid):raise ValueError('missing action date')
    v=np.where(valid,v,0)
    v=v.astype(np.int64);tail=(v>100000000)&(v%1000000!=0)
    if np.any(tail):raise ValueError('nonzero action date time suffix')
    v=np.where(v>100000000,v//1000000,v)
    for d in np.unique(v):
        if d:date(int(d)//10000,int(d)//100%100,int(d)%100)
    return v

def role_code(key):
    code,sep,exchange=key.partition('.')
    if not sep or not code.isdigit() or len(code)!=6:return False
    return (exchange=='XSHE' and code.startswith(('000','001','002','003','300','301'))) or (exchange=='XSHG' and code.startswith(('600','601','603','605')))

def run(spec):
    start=time.monotonic();started=base.utc();peak=0;last=0
    def checkpoint():
        nonlocal peak,last
        clock=time.monotonic()
        if clock-start>spec['limits']['seconds']:raise ValueError('action audit wallclock limit')
        if clock-last>=1:
            peak=max(peak,base.peak_rss());last=clock
            if peak>spec['limits']['peak_rss_bytes']:raise ValueError('action audit memory limit')
    for p,h in spec['input_hashes'].items():
        if base.digest(ROOT/p)!=h:raise ValueError('action role frozen input changed')
    cap=json.loads((ROOT/spec['acquisition_receipt']).read_text(encoding='utf-8'))
    local=Path(cap['raw_directory']);h5py,np=base.load_dependencies()
    for item in cap['retained']:
        if item['name'] in ('instruments.pk','dividends.h5','split_factor.h5','ex_cum_factor.h5','share_transformation.json'):
            if base.digest(local/item['name'])!=item['sha256']:raise ValueError('native action input changed')
    ids,_=parse((local/'instruments.pk').read_bytes(),checkpoint=checkpoint)
    identities={r['order_book_id']:r for r in ids if r.get('type')=='CS'}
    with h5py.File(local/'stocks.h5','r',rdcc_nbytes=1048576) as f:
        keys=[key for key in f if role_code(key)]
    if len(keys)>spec['limits']['datasets']:raise ValueError('action dataset limit')
    totals=Counter();details={};schemas={}
    files={member:h5py.File(local/member,'r',rdcc_nbytes=1048576) for member in ['dividends.h5','split_factor.h5','ex_cum_factor.h5']}
    try:
        for key in keys:
            checkpoint();totals['scope_keys']+=1;identity=identities.get(key)
            if identity is None:raise ValueError('action role missing identity')
            datesets={};record={}
            for member,f in files.items():
                counts=Counter();ds=safe_dataset(f,key,h5py,np) if key in f else None
                counts['dataset_missing']=int(ds is None)
                if ds is None:
                    totals[member+':dataset_missing']+=1
                    datesets[member]=set();record[member]=dict(counts);continue
                rows=ds[:];schemas[member]=list(rows.dtype.names or ())
                if member=='dividends.h5':
                    required=['announcement_date','book_closure_date','ex_dividend_date','payable_date','round_lot','dividend_cash_before_tax']
                    if not set(required)<=set(schemas[member]):raise ValueError('dividend schema not supported')
                    ann=normalize(rows['announcement_date'],np,allow_missing=True)
                    book,ex,pay=[normalize(rows[field],np) for field in required[1:4]]
                    selected=(ex>=spec['start_date'])&(ex<=spec['end_date'])
                    bad=(ann>book)|(book>=ex)|(ex>pay)|(ann==0)|(book==0)|(pay==0)
                    units=rows['round_lot'];cash=rows['dividend_cash_before_tax']
                    value_bad=(~np.isfinite(units))|(~np.isfinite(cash))|(units<=0)|(cash<0)
                    counts['selected_rows']=int(selected.sum());counts['selected_chronology_invalid']=int((bad&selected).sum());counts['selected_unit_cash_invalid']=int((value_bad&selected).sum())
                    counts['selected_announcement_missing']=int(((~np.isfinite(rows['announcement_date']))&selected).sum())
                    counts['selected_duplicate_ex_dates']=len(ex[selected])-len(set(map(int,ex[selected])))
                    counts['selected_zero_cash_rows']=int(((cash==0)&selected).sum())
                    datesets[member]=set(map(int,ex[selected]))
                elif member=='split_factor.h5':
                    required=['ex_date','split_factor','split_coefficient_to','split_coefficient_from']
                    if not set(required)<=set(schemas[member]):raise ValueError('split schema not supported')
                    ex=normalize(rows['ex_date'],np);selected=(ex>=spec['start_date'])&(ex<=spec['end_date'])
                    factor,to,frm=[rows[field] for field in required[1:]]
                    bad=(~np.isfinite(factor))|(~np.isfinite(to))|(~np.isfinite(frm))|(factor<=0)|(to<=0)|(frm<=0)
                    mismatch=(~bad)&(~np.isclose(factor,to/np.where(frm>0,frm,1),rtol=1e-12,atol=1e-12))
                    counts['selected_rows']=int(selected.sum());counts['selected_ratio_invalid']=int(((bad|mismatch)&selected).sum());counts['selected_duplicate_ex_dates']=len(ex[selected])-len(set(map(int,ex[selected])))
                    datesets[member]=set(map(int,ex[selected]))
                else:
                    if not {'start_date','ex_cum_factor'}<=set(schemas[member]):raise ValueError('ex-factor schema not supported')
                    ex=normalize(rows['start_date'],np);selected=(ex>=spec['start_date'])&(ex<=spec['end_date']);value=rows['ex_cum_factor']
                    counts['selected_rows']=int(selected.sum());counts['selected_value_invalid']=int(((~np.isfinite(value)|(value<=0))&selected).sum());counts['selected_duplicate_ex_dates']=len(ex[selected])-len(set(map(int,ex[selected])))
                    counts['all_dates_not_ascending']=int(bool(np.any(np.diff(ex)<=0)))
                    changed=np.zeros(len(ex),dtype=bool)
                    if len(ex)>1:changed[1:]=value[1:]!=value[:-1]
                    counts['selected_factor_changes']=int((selected&changed).sum())
                    datesets[member]=set(map(int,ex[selected&changed]))
                record[member]=dict(counts)
                for name,n in counts.items():totals[member+':'+name]+=n
            # Exact source-date concordance only: unmatched dates can be missing
            # action records, timing conventions or other actions. Never identify
            # them as rights issues, or treat matching dates as value certification.
            known=datesets['dividends.h5']|datesets['split_factor.h5']
            other=datesets['ex_cum_factor.h5']-known
            nofactor=known-datesets['ex_cum_factor.h5']
            totals['factor_change_dates_without_cash_or_split_record']+=len(other)
            totals['keys_with_unmatched_factor_changes']+=int(bool(other))
            totals['cash_or_split_dates_without_factor_change']+=len(nofactor)
            record['factor_change_dates_without_cash_or_split_record']=sorted(other)
            record['cash_or_split_dates_without_factor_change']=sorted(nofactor)
            record['listed_date']=date_int(identity.get('listed_date'));record['de_listed_date']=date_int(identity.get('de_listed_date'))
            details[key]=record
    finally:
        for f in files.values():f.close()
    transforms=json.loads((local/'share_transformation.json').read_text(encoding='utf-8'))
    if not isinstance(transforms,dict):raise ValueError('unsupported transformation schema')
    totals['transformation_keys']=len(transforms);totals['scope_transformation_keys']=sum(k in details for k in transforms)
    output=local/'stock_action_role_audit.json'
    output.write_text(json.dumps(details,ensure_ascii=False,separators=(',',':')),encoding='utf-8');checkpoint()
    result={'schema':'regis.stock.daily_role_result.v1','state':'computed_input_role_census','started_utc':started,'ended_utc':base.utc(),'elapsed_seconds':round(time.monotonic()-start,3),'peak_rss_bytes':peak,'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'counts':dict(totals),'schemas':schemas,'local_details':{'path':str(output),'sha256':base.digest(output),'contains_prices_or_cash_amounts':False},'source_date_concordance_only':True,'independent_issuer_actions_certified':False,'economic_runs_added':0,'model_admitted':False,'original_M24_enabled':False,'full_A_coverage_certified':False}
    RESULT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({k:result[k] for k in ['state','counts','schemas','economic_runs_added','model_admitted']}))

def verify(spec):
    r=json.loads(RESULT.read_text(encoding='utf-8'))
    if r['state']!='computed_input_role_census' or r['protocol_sha256']!=base.digest(PROTOCOL) or r['implementation_sha256']!=base.digest(__file__):raise ValueError('action role result changed')
    for p,h in spec['input_hashes'].items():
        if base.digest(ROOT/p)!=h:raise ValueError('action role input changed')
    if base.digest(r['local_details']['path'])!=r['local_details']['sha256'] or r['model_admitted'] or r['original_M24_enabled'] or r['economic_runs_added']:raise ValueError('action role scope or native evidence changed')
    print(json.dumps({'action_role_verified':True,'scope_keys':r['counts']['scope_keys'],'model_admitted':False}))

if __name__=='__main__':
    p=argparse.ArgumentParser();m=p.add_mutually_exclusive_group(required=True);m.add_argument('--inspect',action='store_true');m.add_argument('--verify',action='store_true');a=p.parse_args();spec=json.loads(PROTOCOL.read_text(encoding='utf-8'))
    if a.verify:verify(spec)
    else:
        if json.loads(RESULT.read_text(encoding='utf-8'))['state']!='not_run':raise ValueError('action role audit already attempted')
        started=base.utc()
        try:run(spec)
        except Exception as e:
            RESULT.write_text(json.dumps({'schema':'regis.stock.daily_role_result.v1','state':'failed','started_utc':started,'ended_utc':base.utc(),'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'failure':{'type':type(e).__name__,'reason':str(e)},'economic_runs_added':0,'model_admitted':False},indent=2)+'\n',encoding='utf-8');print(json.dumps({'state':'failed','type':type(e).__name__,'reason':str(e)}));raise SystemExit(1)
