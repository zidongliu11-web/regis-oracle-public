import unittest
import inspect_short_rq_full as base
from stock_reversal_alias_intents import identifier_bounds,planned_identifier_dependencies,top_two

class IdentifierSelectionCases(unittest.TestCase):
 @classmethod
 def setUpClass(cls):_,cls.np=base.load_dependencies()
 def setUp(self):
  self.pair={'old':'000022.XSHE','new':'001872.XSHE','trading_identifier_effective_session':20181226}
  self.lookup={x:self.pair for x in ['000022.XSHE','001872.XSHE']}
 def identity(self,key,end='0000-00-00',kind='CS'):
  return {'order_book_id':key,'listed_date':'1993-05-05','de_listed_date':end,'type':kind}
 def test_new_code_not_eligible_before_change_despite_shared_IPO_fact(self):
  identity=self.identity('001872.XSHE');before=dict(identity)
  self.assertEqual(identifier_bounds(identity,self.lookup),(20181226,None));self.assertEqual(identity,before)
 def test_old_until_exclusive_and_new_from_inclusive_no_duplicate_day(self):
  intervals=[identifier_bounds(self.identity('000022.XSHE','2018-12-26'),self.lookup),identifier_bounds(self.identity('001872.XSHE'),self.lookup)]
  for day in [20181225,20181226,20181227]:
   active=[a<=day and (b is None or day<b) for a,b in intervals]
   self.assertEqual(sum(active),1)
 def test_same_numeric_code_other_exchange_never_inherits_alias(self):
  self.assertEqual(identifier_bounds(self.identity('001872.XSHG'),self.lookup),(19930505,None))
 def test_non_stock_type_and_cross_exchange_change_rejected(self):
  with self.assertRaises(ValueError):identifier_bounds(self.identity('001872.XSHE',kind='INDX'),self.lookup)
  pair=dict(self.pair,new='001872.XSHG')
  with self.assertRaises(ValueError):identifier_bounds(self.identity('001872.XSHG'),{'001872.XSHG':pair})
 def test_source_old_end_mismatch_stops_instead_of_guessing(self):
  with self.assertRaises(ValueError):identifier_bounds(self.identity('000022.XSHE','2018-12-25'),self.lookup)
 def test_future_cutover_is_execution_dependency_not_past_signal_filter(self):
  a,b=identifier_bounds(self.identity('000022.XSHE','2018-12-26'),self.lookup)
  self.assertTrue(a<=20181225<b)
  self.assertEqual(planned_identifier_dependencies('000022.XSHE',20181226,20181227,self.lookup),['identifier_expired_before_planned_entry'])
  self.assertEqual(planned_identifier_dependencies('000022.XSHE',20181225,20181226,self.lookup),['identifier_cutover_during_planned_hold'])
  self.assertEqual(planned_identifier_dependencies('000022.XSHE',20181224,20181225,self.lookup),[])
 def test_full_rerank_fills_second_slot_instead_of_post_delete(self):
  n=self.np;score=n.full((1,2),n.inf);amount=n.full((1,2),-n.inf);codes=n.full((1,2),-1,dtype=n.int32)
  for idx,loss,valid in [(0,-.05,True),(1,-.05,False),(2,-.03,True)]:
   top_two(n.array([loss]),n.array([50.]),n.array([valid]),idx,score,amount,codes,n)
  self.assertEqual(codes.tolist(),[[0,2]])

if __name__=='__main__':unittest.main()
