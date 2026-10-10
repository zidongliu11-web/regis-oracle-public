"""Fixed historical reversal intents and action dependencies; no PnL or orders.

The original M24 replay stays disabled. Local native prices never exported.
"""
import argparse,json,time
from pathlib import Path
from collections import Counter
import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset
from read_instruments_static import parse,date_int
from inspect_rq_stock_history import event_dates
from audit_stock_action_roles import role_code,normalize

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_REVERSAL_INTENT_PROTOCOL_20261003.json'
RESULT=ROOT/'research/STOCK_REVERSAL_INTENT_RESULT_20261003.json'

def top_two(pct,amount,eligible,code_index,scores,amounts,codes,np):
    """Insert in stable lexical key order: low pct, high lagged amount, low ID."""
    first=eligible&((pct<scores[:,0])|((pct==scores[:,0])&(amount>amounts[:,0])))
    second=eligible&(~first)&((pct<scores[:,1])|((pct==scores[:,1])&(amount>amounts[:,1])))
    scores[first,1]=scores[first,0];amounts[first,1]=amounts[first,0];codes[first,1]=codes[first,0]
    scores[first,0]=pct[first];amounts[first,0]=amount[first];codes[first,0]=code_index
    scores[second,1]=pct[second];amounts[second,1]=amount[second];codes[second,1]=code_index

def past_action_clear(dates,indices,events,calendar,np):
    known=np.array(sorted(set(events)),dtype=np.int64)
    clear=np.ones(len(dates),dtype=bool)
    if len(known):
        loc=np.searchsorted(known,dates,side='right')-1;has=loc>=0
        last_change=np.zeros(len(dates),dtype=np.int64);last_change[has]=known[loc[has]]
        in_window=has&(last_change>=calendar[0])
        distance=indices-np.searchsorted(calendar,last_change,side='left')
        clear[in_window]=distance[in_window]>20
    return clear

def plan(spec):
    started=base.utc();start=time.monotonic();peak=0;last=0
    def checkpoint():
        nonlocal peak,last
        clock=time.monotonic()
        if clock-start>spec['limits']['seconds']:raise ValueError('intent plan wallclock limit')
        if clock-last>=1:
            peak=max(peak,base.peak_rss());last=clock
            if peak>spec['limits']['peak_rss_bytes']:raise ValueError('intent plan memory limit')
    for p,h in spec['input_hashes'].items():
        if base.digest(ROOT/p)!=h:raise ValueError('intent frozen input changed')
    cap=json.loads((ROOT/spec['acquisition_receipt']).read_text(encoding='utf-8'));local=Path(cap['raw_directory']);h5py,np=base.load_dependencies()
    for item in cap['retained']:
        checkpoint()
        if base.digest(local/item['name'])!=item['sha256']:raise ValueError('native intent input changed')
    for source in spec['sources']:
        if source.get('native_path') and base.digest(ROOT/source['native_path'])!=source['sha256']:raise ValueError('intent source document changed')
    cal=np.array(base.read_calendar(local/'trading_dates.npy',np),dtype=np.int64);cal=cal[(cal>=spec['start_date'])&(cal<=spec['end_date'])]
    identities,_=parse((local/'instruments.pk').read_bytes(),checkpoint=checkpoint);identities={r['order_book_id']:r for r in identities if r.get('type')=='CS'}
    n=len(cal);scores=np.full((n,2),np.inf);amounts=np.full((n,2),-np.inf);codes=np.full((n,2),-1,dtype=np.int32);counts=Counter();daily_eligible=np.zeros(n,dtype=np.int32)
    states={}
    for member in ['st_stock_days.h5','suspended_days.h5']:
        states[member]={}
        with h5py.File(local/member,'r',rdcc_nbytes=1048576) as f:
            for key in f:
                checkpoint();dates,invalid=event_dates(safe_dataset(f,key,h5py,np)[:],np)
                if invalid:raise ValueError('invalid native state date')
                states[member][key]=dates
    action_files={member:h5py.File(local/member,'r',rdcc_nbytes=1048576) for member in ['dividends.h5','split_factor.h5','ex_cum_factor.h5']}
    action_cache={}
    try:
        with h5py.File(local/'stocks.h5','r',rdcc_nbytes=1048576) as prices:
            keys=sorted(k for k in prices if role_code(k))
            if len(keys)>spec['limits']['datasets']:raise ValueError('intent dataset limit')
            for k_index,key in enumerate(keys):
                checkpoint();identity=identities[key];listed=date_int(identity.get('listed_date'));delisted=date_int(identity.get('de_listed_date'))
                if listed is None:raise ValueError('intent listing date unknown')
                ds=safe_dataset(prices,key,h5py,np);all_dates=normalize(ds.fields('datetime')[:],np)
                left=int(np.searchsorted(all_dates,spec['start_date']));right=int(np.searchsorted(all_dates,spec['end_date'],side='right'))
                dates=all_dates[left:right]
                if len(dates)<=20:counts['keys_with_insufficient_history']+=1;continue
                fields=['close','prev_close','volume','total_turnover','limit_down']
                rows=ds.fields(fields)[left:right]
                indices=np.searchsorted(cal,dates)
                if np.any(indices>=n) or not np.array_equal(cal[indices],dates):raise ValueError('intent price calendar mismatch')
                prev=rows['prev_close'];close=rows['close'];amount=rows['total_turnover']
                pct=np.full(len(dates),np.nan);np.divide(close,prev,out=pct,where=prev>0);pct-=1
                median=np.full(len(dates),np.nan)
                median[20:]=np.median(np.lib.stride_tricks.sliding_window_view(amount,20)[:-1],axis=1)
                known_events=[];actions={}
                for member,f in action_files.items():
                    array=safe_dataset(f,key,h5py,np)[:] if key in f else None;actions[member]=array
                    if array is None:continue
                    field='ex_dividend_date' if member=='dividends.h5' else 'ex_date' if member=='split_factor.h5' else 'start_date'
                    d=normalize(array[field],np)
                    if member=='ex_cum_factor.h5':
                        value=array['ex_cum_factor'];mask=np.zeros(len(d),dtype=bool)
                        if len(d)>1:mask[1:]=value[1:]!=value[:-1]
                        d=d[mask]
                    known_events.extend(map(int,d))
                # These are past/today source event dates only. Upcoming actions
                # never suppress a candidate; their uncertainty is an outcome
                # dependency checked separately after ranking is frozen.
                action_clear=past_action_clear(dates,indices,known_events,cal,np)
                st=np.isin(dates,states['st_stock_days.h5'].get(key,np.array([],dtype=np.int64)))
                halted=np.isin(dates,states['suspended_days.h5'].get(key,np.array([],dtype=np.int64)))
                valid=(np.isfinite(pct))&(pct<0)&(np.isfinite(median))&(median>=spec['lag20_turnover_min_CNY_convention'])&(rows['volume']>0)&(~st)&(~halted)&action_clear&(close>rows['limit_down'])&(dates>=listed)
                if delisted is not None:valid&=dates<delisted
                counts['eligible_security_sessions']+=int(valid.sum());daily_eligible[indices]+=valid.astype(np.int32)
                full_pct=np.full(n,np.inf);full_amount=np.full(n,-np.inf);full_valid=np.zeros(n,dtype=bool)
                full_pct[indices]=pct;full_amount[indices]=median;full_valid[indices]=valid
                top_two(full_pct,full_amount,full_valid,k_index,scores,amounts,codes,np)
                # Retain only action arrays for subsequently selected keys after
                # scanning; no raw-price cache, scores or arrays written out.
            selected=sorted({keys[int(c)] for c in codes.flat if c>=0})
            for key in selected:
                action_cache[key]={member:safe_dataset(f,key,h5py,np)[:] if key in f else None for member,f in action_files.items()}
    finally:
        for f in action_files.values():f.close()
    action_audit=json.loads((local/'stock_action_role_audit.json').read_text(encoding='utf-8'))
    role_result=json.loads((ROOT/spec['action_role_result']).read_text(encoding='utf-8'))
    if base.digest(local/'stock_action_role_audit.json')!=role_result['local_details']['sha256']:raise ValueError('intent action concordance changed')
    transforms=json.loads((local/'share_transformation.json').read_text(encoding='utf-8'))
    intents=[];annual=Counter();issues=Counter();issue_events={};checkpoint()
    for i,day in enumerate(cal):
        for rank,c in enumerate(codes[i]):
            if c<0:continue
            key=keys[int(c)];annual[str(int(day)//10000)]+=1
            item={'signal_date':int(day),'rank':rank+1,'instrument_id':key,'horizon':1,'entry_date':int(cal[i+1]) if i+1<n else None,'exit_date':int(cal[i+2]) if i+2<n else None,'intent_only_not_fill':True,'action_dependencies':[]}
            if i+2>=n:
                issues['right_censored_intents']+=1;item['action_dependencies'].append('right_censored_no_exit_session')
            else:
                entry,exit=item['entry_date'],item['exit_date'];a=action_cache[key];detail=action_audit[key]
                div=a['dividends.h5']
                if div is not None:
                    book=normalize(div['book_closure_date'],np);ex=normalize(div['ex_dividend_date'],np);pay=normalize(div['payable_date'],np);ann=normalize(div['announcement_date'],np,True)
                    entitled=(book>=entry)&(book<exit);cross=(ex>entry)&(ex<=exit)
                    if np.any(entitled):item['action_dependencies'].append('cash_entitlement_record_ex_pay_accounting');issues['cash_entitlement_intents']+=1
                    bad=((ann>book)|(book>=ex)|(ex>pay)|(ann==0))&(entitled|cross)
                    if np.any(bad):item['action_dependencies'].append('source_dividend_chronology_unresolved');issues['chronology_exposed_intents']+=1
                    for d in set(map(int,ex[cross|entitled])):
                        group=div[ex==d]
                        if len(group)>1:
                            item['action_dependencies'].append('same_ex_dividend_records_need_identity_reconciliation');issues['same_ex_dividend_intents']+=1
                        issue_events[(key,d,'cash')]=True
                split=a['split_factor.h5']
                if split is not None:
                    ex=normalize(split['ex_date'],np);cross=(ex>entry)&(ex<=exit)
                    if np.any(cross):
                        item['action_dependencies'].append('split_book_and_share_listing_date_missing');issues['split_exposed_intents']+=1
                        for d in ex[cross]:issue_events[(key,int(d),'split_missing_book_pay')]=True
                unmatched=[d for d in detail['factor_change_dates_without_cash_or_split_record'] if entry<d<=exit]
                if unmatched:
                    item['action_dependencies'].append('factor_change_action_unresolved');issues['unmatched_factor_intents']+=1
                    for d in unmatched:issue_events[(key,d,'unknown_factor_action')]=True
                delisted=date_int(identities[key].get('de_listed_date'))
                if delisted is not None and entry<=delisted<=exit:
                    item['action_dependencies'].append('delist_exit_unresolved');issues['delist_exposed_intents']+=1
                    if key in transforms:item['action_dependencies'].append('share_transformation_unresolved')
                halts=states['suspended_days.h5'].get(key,[])
                if entry in halts:item['action_dependencies'].append('entry_halt_no_fill');issues['entry_halt_intents']+=1
                if exit in halts:item['action_dependencies'].append('planned_exit_halt_residual_position');issues['exit_halt_intents']+=1
            intents.append(item)
    # Codes/dates/unknown dependencies are metadata. Never export ranking scores,
    # price/amount/volume arrays, theoretical order quantities or returns.
    native=local/'stock_reversal_intents_training.json'
    native.write_text(json.dumps({'intents':intents,'events_to_resolve':[{'instrument_id':k,'date':d,'kind':kind} for k,d,kind in sorted(issue_events)]},ensure_ascii=False,separators=(',',':')),encoding='utf-8');checkpoint()
    for item in cap['retained']:
        checkpoint()
        if base.digest(local/item['name'])!=item['sha256']:raise ValueError('native intent input changed during run')
    result={'schema':'regis.stock.reversal_intents.v1','state':'computed_frozen_intent_plan','started_utc':started,'ended_utc':base.utc(),'elapsed_seconds':round(time.monotonic()-start,3),'peak_rss_bytes':peak,'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'scope_keys':len(keys),'calendar_sessions':n,'counts':dict(counts),'planned_intents':len(intents),'distinct_selected_instruments':len(selected),'annual_intents':dict(annual),'action_dependencies':dict(issues),'distinct_event_dependencies':dict(Counter(kind for _,_,kind in issue_events)),'local_details':{'path':str(native),'sha256':base.digest(native),'contains_prices':False},'future_actions_used_for_candidate_filter':False,'cash_or_portfolio_accounting_completed':False,'order_or_alert_generated':False,'profit_or_OOS_computed':False,'model_admitted':False,'original_M24_enabled':False,'full_A_certified':False}
    RESULT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');print(json.dumps({k:result[k] for k in ['state','scope_keys','calendar_sessions','planned_intents','distinct_selected_instruments','annual_intents','action_dependencies','distinct_event_dependencies','profit_or_OOS_computed','model_admitted']}))

def verify(spec):
    r=json.loads(RESULT.read_text(encoding='utf-8'))
    if r['state']!='computed_frozen_intent_plan' or r['protocol_sha256']!=base.digest(PROTOCOL) or r['implementation_sha256']!=base.digest(__file__):raise ValueError('intent source changed')
    for p,h in spec['input_hashes'].items():
        if base.digest(ROOT/p)!=h:raise ValueError('intent input changed')
    if base.digest(r['local_details']['path'])!=r['local_details']['sha256'] or any(r[k] for k in ['future_actions_used_for_candidate_filter','profit_or_OOS_computed','model_admitted','original_M24_enabled','order_or_alert_generated']):raise ValueError('intent scope/evidence changed')
    cap=json.loads((ROOT/spec['acquisition_receipt']).read_text(encoding='utf-8'));local=Path(cap['raw_directory'])
    for item in cap['retained']:
        if base.digest(local/item['name'])!=item['sha256']:raise ValueError('native intent input changed')
    for source in spec['sources']:
        if source.get('native_path') and base.digest(ROOT/source['native_path'])!=source['sha256']:raise ValueError('intent source document changed')
    print(json.dumps({'intent_plan_verified':True,'intents':r['planned_intents'],'profit_or_OOS_computed':False}))

if __name__=='__main__':
    p=argparse.ArgumentParser();m=p.add_mutually_exclusive_group(required=True);m.add_argument('--plan',action='store_true');m.add_argument('--verify',action='store_true');a=p.parse_args();spec=json.loads(PROTOCOL.read_text(encoding='utf-8'))
    if a.verify:verify(spec)
    else:
        if json.loads(RESULT.read_text(encoding='utf-8'))['state']!='not_run':raise ValueError('intent plan already attempted')
        start=base.utc()
        try:plan(spec)
        except Exception as e:
            RESULT.write_text(json.dumps({'schema':'regis.stock.reversal_intents.v1','state':'failed','started_utc':start,'ended_utc':base.utc(),'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'failure':{'type':type(e).__name__,'reason':str(e)},'profit_or_OOS_computed':False,'model_admitted':False},indent=2)+'\n',encoding='utf-8');print(json.dumps({'state':'failed','type':type(e).__name__,'reason':str(e)}));raise SystemExit(1)
