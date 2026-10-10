"""Synthetic storage and callback integration; no retained data or network."""
import hashlib
import socket
from decimal import Decimal
from types import SimpleNamespace

import pytest
from research import daily_execution_inputs as m
from research.stock_daily_cash_book import replay
from mom_trading.local_research_history import HistoryReader

CAL = [20250102, 20250103, 20250106, 20250107, 20250108]
KEY = '000001.XSHE'
S = {'id': 'synthetic', 'entry_delay_sessions': 0, 'entry_field': 'open', 'slippage': '0',
     'buy_all_in_fee_rate': '0.0005', 'sell_all_in_fee_rate': '0.0015', 'minimum_fee': '5',
     'entry_fill_fraction': '1', 'exit_fill_fraction': '1'}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture(scope='module')
def deps():
    from research.inspect_short_rq_full import load_dependencies
    return load_dependencies()


@pytest.fixture
def make(tmp_path, deps):
    h5, np = deps
    def create(fault=None, context=None):
        fields = ['datetime', 'open', 'high', 'low', 'close', 'volume', 'total_turnover', *m.EXTRA_FIELDS]
        if fault == 'absent': fields.remove('prev_close'); fields.remove('limit_down')
        dtype = [(f, '<u8' if f == 'datetime' else ('?' if f == 'bool_extra' else '<f8')) for f in fields]
        if fault == 'boolean': dtype = [(f, '?' if f == 'prev_close' else d) for f,d in dtype]
        data = np.zeros(len(CAL), dtype=dtype)
        for f in fields: data[f] = {'datetime':CAL, 'open':10., 'close':10.1, 'low':9.5, 'high':10.5,
                                   'prev_close':9.9, 'limit_down':9., 'limit_up':11.,
                                   'volume':10000., 'total_turnover':100000.}[f]
        if fault == 'nonfinite': data['prev_close'][1]=np.nan; data['limit_up'][1]=np.inf
        if fault == 'zero_limit': data['limit_down'][1]=0
        if fault == 'inconsistent': data['limit_up'][1]=10.2
        if fault == 'duplicate': data['datetime'][1]=CAL[0]
        if fault == 'off_calendar': data['datetime'][1]=20250104
        if fault == 'invalid_ohlc': data['high'][1]=8
        path=tmp_path/'stocks.h5';calendar=tmp_path/'trading_dates.npy'
        np.save(calendar, np.array(CAL,dtype='<u8'))
        with h5.File(path,'w') as f:
            f.create_dataset(KEY,data=data)
            if fault == 'soft': f.move(KEY,'other');f[KEY]=h5.SoftLink('other')
        sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        reader=HistoryReader(path,sha(path),path.stat().st_size,calendar,sha(calendar),calendar.stat().st_size,
                             'synthetic-capture',dependencies=deps)
        return m.DailyExecutionInputs(reader,context),path
    return create


def synthetic_bar(**kwargs):
    return dict(open='10',high='11',low='9',close='10',prev_close='9.7',limit_up='11',limit_down='9',
                volume='1000',total_turnover='10000',st=False,halted=False,outside_lifetime=False,**kwargs)


def records(): return {(KEY,d):synthetic_bar() for d in CAL}
def intent(): return {'instrument_id':KEY,'signal_date':CAL[0],'rank':1}


def test_source_extra_fields_are_exact_bounded_and_not_inferred(make):
    reader,_=make();out=reader.read('000001',CAL[0],CAL[-1],2)
    assert out['schema']==m.SCHEMA and out['returned_rows']==2 and out['truncated'] is True
    assert [r['time'] for r in out['rows']]==['2025-01-07','2025-01-08']
    assert all(r['prev_close']==9.9 and r['prev_close']!=10.1 for r in out['rows'])
    assert all(r['limit_band_status']=='consistent_source_band' and r['context'] is None for r in out['rows'])
    assert out['volume_semantics'].startswith('Positive-value condition')
    assert out['callback_available'] is out['execution_eligible'] is out['model_admitted'] is False
    with pytest.raises(m.ExecutionInputUnavailable) as e: reader.cashbook_callback()
    assert e.value.reasons==m.UNRESOLVED


@pytest.mark.parametrize('fault,field,status,value',[
    ('absent','prev_close','missing_field',None),('absent','limit_down','missing_field',None),
    ('nonfinite','prev_close','nonfinite',None),('nonfinite','limit_up','nonfinite',None),
    ('zero_limit','limit_down','unsupported_nonpositive',0),
])
def test_missing_or_unsupported_field_does_not_get_imputed(make,fault,field,status,value):
    reader,_=make(fault);row=reader.read('000001',CAL[1],CAL[1],1)['rows'][0]
    assert row[field]==value and row['field_status'][field]==status
    assert row['execution_eligible'] is False


def test_positive_but_inconsistent_limits_not_qualified(make):
    reader,_=make('inconsistent');row=reader.read('000001',CAL[1],CAL[1],1)['rows'][0]
    assert row['limit_up']==10.2 and row['limit_band_status']=='inconsistent_source_band'


@pytest.mark.parametrize('fault',['boolean','duplicate','off_calendar','invalid_ohlc','soft'])
def test_corrupt_source_is_rejected(make,fault):
    reader,_=make(fault)
    with pytest.raises(ValueError):reader.read('000001',CAL[0],CAL[-1],5)


def test_empty_window_does_not_invent_rows_or_callback(make):
    reader,_=make();out=reader.read('000001',20250109,20250110,1)
    assert out['rows']==[] and out['status']=='empty_window' and out['callback_available'] is False


def test_missing_instrument_and_excess_limit_raise_before_context(make):
    reader,_=make()
    with pytest.raises(LookupError):reader.read('000002',CAL[0],CAL[-1],1)
    with pytest.raises(ValueError):reader.read('000001',CAL[0],CAL[-1],251)


def test_source_mutation_before_or_during_read_is_rejected(make,monkeypatch):
    reader,path=make();old=reader.stock.read
    def changed(*args):
        out=old(*args)
        with path.open('ab') as f:f.write(b'changed')
        return out
    monkeypatch.setattr(reader.stock,'read',changed)
    with pytest.raises(ValueError):reader.read('000001',CAL[0],CAL[-1],2)
    monkeypatch.setattr(reader.stock,'read',old)
    with pytest.raises(ValueError):reader.read('000001',CAL[0],CAL[-1],2)


def test_context_not_reported_and_unknown_are_preserved_not_boolean(make):
    def context(code,days):
        return {d:{'ST':'not_reported','halt':'unknown','lifetime':'after_recorded_listing_end_unknown'} for d in days},{'status':'partial'}
    reader,_=make(context=SimpleNamespace(read=context));out=reader.read('000001',CAL[0],CAL[0],1)
    assert out['rows'][0]['context']['halt']=='unknown' and out['context_source']['status']=='partial'
    with pytest.raises(m.ExecutionInputUnavailable):m.StrictCashbookCallback({(KEY,CAL[0]):out['rows'][0]})


@pytest.mark.parametrize('value',[None,True,False,'NaN','Infinity','-Infinity',float('nan'),float('inf'),0,-1,{},'1e999','1'*65])
def test_invalid_numeric_value_cannot_reach_decimal_price(value):
    row=synthetic_bar();row['prev_close']=value
    with pytest.raises(m.ExecutionInputUnavailable):m.validate_cashbook_bar(row)


@pytest.mark.parametrize('value',[None,0,1,'False','not_reported',[],{}])
@pytest.mark.parametrize('field',m.STATE_FIELDS)
def test_state_must_be_an_actual_boolean(field,value):
    row=synthetic_bar();row[field]=value
    with pytest.raises(m.ExecutionInputUnavailable):m.StrictCashbookCallback({(KEY,CAL[0]):row})


def test_missing_state_and_contradictory_band_rejected():
    row=synthetic_bar();del row['st']
    with pytest.raises(m.ExecutionInputUnavailable):m.validate_cashbook_bar(row)
    row=synthetic_bar();row['limit_down']='10.5'
    with pytest.raises(m.ExecutionInputUnavailable):m.validate_cashbook_bar(row)


def test_callback_copies_values_and_missing_row_is_failure_not_none():
    rows=records();cb=m.StrictCashbookCallback(rows);rows[(KEY,CAL[0])]['open']='999'
    got=cb(KEY,CAL[0]);got['open']='555'
    assert cb(KEY,CAL[0])['open']=='10'
    with pytest.raises(m.ExecutionInputUnavailable) as e:cb(KEY,20250109)
    assert e.value.reasons==('missing_callback_row',)


@pytest.mark.parametrize('key,day',[('688001.XSHG',CAL[0]),('000001.XSHG',CAL[0]),(KEY,True),(KEY,20250230)])
def test_callback_identity_and_dates(key,day):
    with pytest.raises(m.ExecutionInputUnavailable):m.StrictCashbookCallback({(key,day):synthetic_bar()})


@pytest.mark.parametrize('key,day',[(KEY,float(CAL[0])),([],CAL[0]),(KEY,[]),(KEY,str(CAL[0])),
                                 (KEY,True),(None,CAL[0]),('688001.XSHG',CAL[0]),(KEY,20250230)])
def test_callback_queries_revalidate_types_before_dictionary_equality(key,day):
    cb=m.StrictCashbookCallback(records())
    with pytest.raises(m.ExecutionInputUnavailable):cb(key,day)


def test_synthetic_150k_t1_fees_and_whole_lot_integration():
    summary,native=replay(CAL,[intent()],m.StrictCashbookCallback(records()),{},S)
    events=native['fill_events_without_prices']
    assert [e['date'] for e in events]==[CAL[1],CAL[2]]
    assert all(e['qty']%100==0 for e in events) and events[0]['qty']==2900
    assert Decimal(summary['total_fees_CNY'])>0 and Decimal(summary['terminal_cash_CNY'])<150000


def test_missing_row_after_entry_aborts_instead_of_carrying_old_price():
    rows=records();del rows[(KEY,CAL[2])]
    with pytest.raises(m.ExecutionInputUnavailable) as e:replay(CAL,[intent()],m.StrictCashbookCallback(rows),{},S)
    assert e.value.reasons==('missing_callback_row',)


def test_halt_postpones_exit_and_st_does_not_prohibit_existing_exit():
    rows=records();rows[(KEY,CAL[2])]['halted']=True;rows[(KEY,CAL[3])]['st']=True
    summary,native=replay(CAL,[intent()],m.StrictCashbookCallback(rows),{},S)
    assert summary['counts']['exit_no_fill_state']==1
    assert [e['date'] for e in native['fill_events_without_prices']]==[CAL[1],CAL[3]]


def test_limit_up_blocks_entry_but_allows_existing_exit_at_limit_up():
    rows=records();rows[(KEY,CAL[1])]['open']='11'
    summary,native=replay(CAL,[intent()],m.StrictCashbookCallback(rows),{},S)
    assert native['fill_events_without_prices']==[] and summary['counts']['entry_no_fill_price_boundary']==1
    rows=records();rows[(KEY,CAL[2])]['close']='11'
    _,native=replay(CAL,[intent()],m.StrictCashbookCallback(rows),{},S)
    assert [e['date'] for e in native['fill_events_without_prices']]==[CAL[1],CAL[2]]


def test_partial_exit_retains_ownership_and_delay_shifts_t1():
    summary,native=replay(CAL,[intent()],m.StrictCashbookCallback(records()),{},dict(S,exit_fill_fraction='.5'))
    assert summary['residual_positions']==1 and len(native['fill_events_without_prices'])>2
    _,native=replay(CAL,[intent()],m.StrictCashbookCallback(records()),{},dict(S,entry_delay_sessions=1))
    assert [e['date'] for e in native['fill_events_without_prices']]==[CAL[2],CAL[3]]
