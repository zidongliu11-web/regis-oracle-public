import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from stock_daily_cash_book import replay,buy_quantity,D,UnresolvedAction
CAL=[20130104,20130107,20130108,20130109,20130110,20130111]
S={'id':'synthetic','entry_delay_sessions':0,'entry_field':'open','slippage':'0','buy_all_in_fee_rate':'0','sell_all_in_fee_rate':'0','minimum_fee':'0','entry_fill_fraction':'1','exit_fill_fraction':'1'}
def intent(key='K',signal=20130104,rank=1):return {'instrument_id':key,'signal_date':signal,'rank':rank}
def bar(price='10',halt=False):return {'open':price,'close':price,'high':D(price)*2,'low':D(price)/2,'volume':1,'limit_down':'.01','limit_up':'1000','halted':halt}
class AccountingCases(unittest.TestCase):
 def test_minimum_fee_stays_inside_cash(self):
  s=dict(S,buy_all_in_fee_rate='.0003',minimum_fee='5')
  self.assertEqual(buy_quantity(D(10),D(1005),D(1005),s,100),100)
  self.assertEqual(buy_quantity(D(10),D(1004),D(1004),s,100),0)
 def test_t_plus_one_and_shared_slot_survives_halt(self):
  def get(k,d):return bar(halt=(k=='K' and d in (20130108,20130109)))
  r,n=replay(CAL,[intent(),intent('J',20130107)],get,{},S,max_positions=1)
  self.assertEqual(r['counts']['exit_no_fill_state'],2);self.assertEqual(r['counts']['entry_cancel_position_slots'],1)
  self.assertEqual([e['date'] for e in n['fill_events_without_prices']],[20130107,20130110])
  self.assertEqual(r['terminal_marked_nav_CNY'],'150000.00')
 def test_record_right_ex_payment_and_halted_split_not_double_counted(self):
  cash={'record_date':20130107,'ex_date':20130108,'pay_date':20130111,'gross_cash_per_share':'.2','valid':True}
  split={'record_date':20130107,'ex_date':20130108,'credit_date':20130108,'trading_date':20130108,'ratio':'2'}
  def get(k,d):return bar('10' if d<=20130108 else '4.9',halt=d==20130108)
  r,n=replay(CAL,[intent()],get,{'K':{'cash':[cash],'split':[split]}},S)
  self.assertEqual(n['daily_aggregate_ledger'][2]['nav_CNY'],'149880.00')
  self.assertEqual(r['terminal_marked_nav_CNY'],'149880.00');self.assertEqual(r['paid_net_dividend_CNY'],'480.00')
  self.assertEqual(n['fill_events_without_prices'][1]['qty'],6000)
 def test_no_fill_has_no_fees_or_phantom_cash(self):
  s=dict(S,entry_fill_fraction='0',minimum_fee='5')
  r,n=replay(CAL,[intent()],lambda k,d:bar(),{},s)
  self.assertEqual(r['terminal_cash_CNY'],'150000.00');self.assertEqual(r['total_fees_CNY'],'0.00');self.assertEqual(n['fill_events_without_prices'],[])
 def test_partial_exit_retains_residual_in_next_session(self):
  s=dict(S,exit_fill_fraction='.5')
  r,n=replay(CAL,[intent()],lambda k,d:bar(),{},s)
  sells=[e for e in n['fill_events_without_prices'] if e['kind']=='exit_fill']
  self.assertGreater(len(sells),1);self.assertEqual(sells[0]['qty'],1500);self.assertEqual(r['residual_positions'],1)
  self.assertEqual(r['terminal_marked_nav_CNY'],'150000.00')
 def test_action_on_postponed_exit_is_retained(self):
  def get(k,d):return bar(halt=d in (20130108,20130109))
  with self.assertRaises(UnresolvedAction) as cm:replay(CAL,[intent()],get,{'K':{'unknown_factor_dates':[20130109]}},S)
  self.assertEqual(cm.exception.dependency['date'],20130109)
 def test_two_record_rights_with_same_pay_day_remain_distinct(self):
  e1={'record_date':20130107,'ex_date':20130108,'pay_date':20130111,'gross_cash_per_share':'.1','valid':True}
  e2={'record_date':20130108,'ex_date':20130109,'pay_date':20130111,'gross_cash_per_share':'.2','valid':True}
  def get(k,d):return bar('10',halt=d in (20130108,20130109))
  r,n=replay(CAL,[intent()],get,{'K':{'cash':[e1,e2]}},S)
  self.assertEqual(r['counts']['cash_payment_events'],2);self.assertEqual(r['paid_net_dividend_CNY'],'720.00')
 def test_unknown_share_listing_never_assumed_sellable(self):
  with self.assertRaises(UnresolvedAction):replay(CAL,[intent()],lambda k,d:bar(),{'K':{'split':[{'ex_date':20130108,'ratio':'2'}]}},S)
 def test_halted_ex_open_cannot_consume_phantom_budget(self):
  cash={'record_date':20130107,'ex_date':20130108,'pay_date':20130111,'gross_cash_per_share':'.2','valid':True}
  split={'record_date':20130107,'ex_date':20130108,'credit_date':20130108,'trading_date':20130108,'ratio':'2'}
  def get(k,d):return bar('10' if k=='J' or d<=20130108 else '4.9',halt=k=='K' and d==20130108)
  r,n=replay(CAL,[intent(),intent('J',20130107)],get,{'K':{'cash':[cash],'split':[split]}},S)
  self.assertTrue(any(e['instrument_id']=='J' and e['kind']=='entry_fill' and e['date']==20130108 for e in n['fill_events_without_prices']))
 def test_zero_exit_fraction_has_no_sales_or_exit_fees(self):
  s=dict(S,exit_fill_fraction='0',minimum_fee='5')
  r,n=replay(CAL,[intent()],lambda k,d:bar(),{},s)
  self.assertEqual(r['counts'].get('exit_fills',0),0);self.assertEqual(r['total_fees_CNY'],'5.00')
  self.assertEqual(len(n['fill_events_without_prices']),1);self.assertEqual(r['residual_positions'],1)
 def test_public_listing_day_gates_pending_shares_without_invented_credit(self):
  split={'record_date':20130107,'ex_date':20130108,'credit_date':None,'trading_date':20130110,'ratio':'2'}
  def get(k,d):return bar('10' if d<20130108 else '5')
  r,n=replay(CAL,[intent()],get,{'K':{'split':[split]}},S)
  sells=[e for e in n['fill_events_without_prices'] if e['kind']=='exit_fill']
  self.assertEqual([(e['date'],e['qty']) for e in sells],[(20130108,3000),(20130110,3000)])
  self.assertEqual(r['terminal_cash_CNY'],'150000.00')
 def test_bonus_share_tax_unknown_is_not_silently_cash_only(self):
  split={'record_date':20130107,'ex_date':20130108,'credit_date':20130108,'trading_date':20130108,'ratio':'2','bonus_share_per_pre_share':'1'}
  with self.assertRaises(UnresolvedAction) as cm:replay(CAL,[intent()],lambda k,d:bar(),{'K':{'split':[split]}},S)
  self.assertEqual(cm.exception.dependency['kind'],'bonus_share_tax_or_distribution_type_unresolved')
if __name__=='__main__':unittest.main(verbosity=2)
