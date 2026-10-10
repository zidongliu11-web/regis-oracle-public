"""Bounded stock coverage/identity audit; only aggregate metadata leaves local storage."""
import argparse,json,re,time
from collections import Counter
from pathlib import Path
import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset
from read_instruments_static import parse,date_int

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/RQ_STOCK_INVENTORY_PROTOCOL_20261002.json'
RESULT=ROOT/'research/RQ_STOCK_INVENTORY_RESULT_20261002.json'

def event_dates(values,np):
    """Normalize exact numeric event dates, preserving invalid-row counts."""
    if values.dtype.kind not in 'iuf' or values.dtype.itemsize>8:raise ValueError('unsupported state scalar type')
    if values.dtype.kind=='f' and len(values) and values.dtype.itemsize!=8:raise ValueError('nonempty float event dates need float64')
    valid=np.isfinite(values)&(values==np.floor(values))&(values>=0)&(values<=29991231000000)
    accepted=values[valid].astype(np.int64)
    dates=np.where(accepted>100000000,accepted//1000000,accepted)
    # Event-date field has no time-of-day component; reject fractional datetime encodings.
    exact=(accepted<=100000000)|(accepted%1000000==0)
    exact&=(dates>=19000101)&(dates<=29991231)
    return dates[exact],int((~valid).sum()+(~exact).sum())

def inspect(spec):
    started=time.monotonic();started_utc=base.utc();peak=0
    def checkpoint():
        nonlocal peak
        if time.monotonic()-started>spec['limits']['seconds']:raise ValueError('whole inventory wallclock ceiling')
        peak=max(peak,base.peak_rss())
        if peak>spec['limits']['peak_rss_bytes']:raise ValueError('whole inventory memory ceiling')
    checkpoint()
    for path,expected in spec['source_hashes'].items():
        if base.digest(ROOT/path)!=expected:raise ValueError('frozen inventory source changed')
    receipt=json.loads((ROOT/spec['acquisition_receipt']).read_text(encoding='utf-8'))
    if receipt['state']!='captured':raise ValueError('no retained capture')
    local=Path(receipt['raw_directory']);h5py,np=base.load_dependencies()
    rows,ops=parse((local/'instruments.pk').read_bytes(),max_memo=spec['limits']['memo_indices'],checkpoint=checkpoint)
    identities={};stock_rows=[];unknown_dates=0
    for r in rows:
        checkpoint()
        if r.get('type')!='CS':continue
        key=r.get('order_book_id')
        if not isinstance(key,str) or key in identities:raise ValueError('invalid/duplicate stock identity')
        listed=date_int(r.get('listed_date'));delisted=date_int(r.get('de_listed_date'))
        unknown_dates+=int(listed is None)
        fields={f:r.get(f) for f in ['order_book_id','type','exchange','board_type','status','special_type','round_lot','market_tplus']}
        fields.update(listed_date=listed,de_listed_date=delisted)
        if any(not isinstance(v,(str,int,float,bool,type(None))) for v in fields.values()):raise ValueError('nonliteral identity scalar')
        identities[key]=fields;stock_rows.append(fields)
    start,end=spec['start_date'],spec['end_date']
    cal=base.read_calendar(local/'trading_dates.npy',np);calendar=set(int(x) for x in cal if start<=int(x)<=end)
    counts=Counter();annual=Counter();firsts=[];lasts=[];nrows=0;dates_malformed=0;identity_missing=0
    local_inventory={}
    fields=['datetime','open','high','low','close','prev_close','limit_up','limit_down','volume','total_turnover']
    with h5py.File(local/'stocks.h5','r',rdcc_nbytes=1048576) as file:
        if len(file)>spec['limits']['datasets']:raise ValueError('dataset ceiling')
        for key in file:
            checkpoint()
            ds=safe_dataset(file,key,h5py,np);base.validate_bar_dtype(ds.dtype,fields)
            arr=ds.fields(fields)[:];times=arr['datetime'].astype(np.int64)
            dates=np.where(times>100000000,times//1000000,times);selected=(dates>=start)&(dates<=end)
            if np.any(np.diff(dates)<=0):dates_malformed+=1
            selected_dates=dates[selected];n=len(selected_dates);nrows+=n
            if nrows>spec['limits']['total_rows']:raise ValueError('stock row ceiling')
            counts['stored_datasets']+=1;counts['stored_rows']+=len(dates)
            code=key.split('.')[0];counts['exchange_suffix_'+key.split('.')[-1]]+=1
            group='B_share' if code.startswith(('200','900')) else 'STAR' if code.startswith(('688','689')) else 'ChiNext' if code.startswith(('300','301')) else 'BSE_candidate' if code.startswith(('4','8','92')) else 'other_main_candidate'
            counts['code_group_'+group]+=1
            identity=identities.get(key);identity_missing+=int(identity is None)
            if n:
                firsts.append(int(selected_dates[0]));lasts.append(int(selected_dates[-1]));counts['selected_datasets']+=1
                for y,c in zip(*np.unique(selected_dates//10000,return_counts=True)):annual[str(int(y))]+=int(c)
                prices=arr[selected]
                valid=np.isfinite(prices['open'])&np.isfinite(prices['high'])&np.isfinite(prices['low'])&np.isfinite(prices['close'])
                counts['rows_nonfinite_ohlc']+=int((~valid).sum())
                counts['rows_nonpositive_ohlc']+=int((valid&((prices['low']<=0)|(prices['open']<=0)|(prices['close']<=0)|(prices['high']<=0))).sum())
                ordered=(prices['low']<=np.minimum(prices['open'],prices['close']))&(np.maximum(prices['open'],prices['close'])<=prices['high'])
                counts['rows_unordered_ohlc']+=int((valid&(~ordered)).sum())
                counts['rows_negative_volume']+=int((prices['volume']<0).sum());counts['rows_negative_turnover']+=int((prices['total_turnover']<0).sum())
                counts['rows_outside_calendar']+=len(set(map(int,selected_dates))-calendar)
                if identity and identity['listed_date'] is not None:
                    counts['price_rows_before_listing']+=int((selected_dates<identity['listed_date']).sum())
                if identity and identity['de_listed_date'] is not None:
                    counts['selected_datasets_with_delist_date']+=1
                    counts['price_rows_after_delisting']+=int((selected_dates>identity['de_listed_date']).sum())
            local_inventory[key]={'stored_rows':len(dates),'selected_rows':n,'first_date':int(selected_dates[0]) if n else None,'last_date':int(selected_dates[-1]) if n else None,'identity':identity}
    state_summary={}
    for member in ['st_stock_days.h5','suspended_days.h5']:
        stats=Counter();all_dates=[]
        with h5py.File(local/member,'r',rdcc_nbytes=1048576) as file:
            for key in file:
                checkpoint()
                ds=safe_dataset(file,key,h5py,np)
                values=ds[:];dates,invalid=event_dates(values,np)
                stats['keys']+=1;stats['dates']+=len(values);stats['invalid_numeric_date_rows']+=invalid
                stats['empty_float_keys']+=int(ds.dtype.kind=='f' and not len(values))
                stats['nonempty_float_keys']+=int(ds.dtype.kind=='f' and bool(len(values)))
                stats['unsorted_or_duplicate_keys']+=int(bool(np.any(np.diff(dates)<=0)))
                relevant=dates[(dates>=start)&(dates<=end)];stats['selected_dates']+=len(relevant)
                stats['selected_dates_outside_calendar']+=len(set(map(int,relevant))-calendar)
                if len(relevant):all_dates.extend([int(relevant.min()),int(relevant.max())])
        state_summary[member]={'counts':dict(stats),'first_date':min(all_dates) if all_dates else None,'last_date':max(all_dates) if all_dates else None,'date_representation':'empty float64 arrays allowed; exact finite integral float64 event dates normalized; invalid rows counted and not admitted','absence_semantics':'missing key does not certify no event without producer/reader semantics and completeness review'}
    metadata=local/'stock_identity_inventory.json'
    metadata.write_text(json.dumps(local_inventory,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    checkpoint()
    result={'schema':'regis.rq.stock_inventory.v1','state':'computed','started_utc':started_utc,'ended_utc':base.utc(),'protocol_sha256':base.digest(PROTOCOL),
      'implementation_sha256':base.digest(__file__),'static_reader_sha256':base.digest(ROOT/'research/read_instruments_static.py'),
      'acquisition_receipt_sha256':base.digest(ROOT/spec['acquisition_receipt']),
      'window':{'start_date':start,'end_date':end,'calendar_sessions':len(calendar)},
      'prices':{'counts':dict(counts),'selected_rows':nrows,'earliest_selected_date':min(firsts) if firsts else None,'latest_selected_date':max(lasts) if lasts else None,'annual_rows':dict(sorted(annual.items())),'unsorted_or_duplicate_datasets':dates_malformed,'missing_identity_datasets':identity_missing},
      'identities':{'all_instrument_records':len(rows),'CS_records':len(identities),'unknown_listing_dates':unknown_dates,'exchanges':dict(Counter(r['exchange'] for r in stock_rows)),'boards':dict(Counter(r['board_type'] for r in stock_rows)),'current_status':dict(Counter(r['status'] for r in stock_rows)),'inert_opcode_counts':ops},
      'state_dates':state_summary,'local_inventory':{'path':str(metadata),'sha256':base.digest(metadata),'raw_quotes_in_inventory':False},
      'elapsed_seconds':round(time.monotonic()-started,3),'peak_rss_bytes':peak,'raw_quotes_printed':False,'pickle_executed':False,'economic_run':False,'full_A_coverage_certified':False,'historical_feature_PIT_certified':False,'model_admitted':False,
      'not_certified':['full-A identity census and board/exchange completeness','historical sector/current special_type','historical announcement availability','absence semantics for state keys','minute execution and actual human fills','source price/volume units and actual historic fee admission','economic robustness or OOS']}
    RESULT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({k:result[k] for k in ['state','window','prices','identities','state_dates','economic_run','full_A_coverage_certified']},ensure_ascii=False))

def verify(spec):
    r=json.loads(RESULT.read_text(encoding='utf-8'))
    for p,h in [(PROTOCOL,r['protocol_sha256']),(Path(__file__),r['implementation_sha256']),(ROOT/'research/read_instruments_static.py',r['static_reader_sha256']),(ROOT/spec['acquisition_receipt'],r['acquisition_receipt_sha256'])]:
        if base.digest(p)!=h:raise ValueError('inventory input changed')
    if base.digest(r['local_inventory']['path'])!=r['local_inventory']['sha256']:raise ValueError('local inventory changed')
    if r['state']!='computed' or any(r[k] for k in ['economic_run','model_admitted','pickle_executed','full_A_coverage_certified']):raise ValueError('scope')
    print(json.dumps({'inventory_source_verified':True,'selected_stock_rows':r['prices']['selected_rows'],'economic_run':False,'full_A_coverage_certified':False}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument('--inspect',action='store_true');mode.add_argument('--verify',action='store_true');args=parser.parse_args()
    spec=json.loads(PROTOCOL.read_text(encoding='utf-8'))
    if args.verify:verify(spec)
    else:
        if json.loads(RESULT.read_text(encoding='utf-8'))['state']!='not_run':raise ValueError('inventory already attempted; preserve result before versioned repair')
        started_utc=base.utc()
        try:inspect(spec)
        except Exception as error:
            RESULT.write_text(json.dumps({'schema':'regis.rq.stock_inventory.v1','state':'failed','started_utc':started_utc,'ended_utc':base.utc(),'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'failure':{'type':type(error).__name__,'reason':str(error)},'economic_run':False,'model_admitted':False},ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
            print(json.dumps({'state':'failed','type':type(error).__name__,'reason':str(error),'raw_quotes_printed':False}));raise SystemExit(1)
