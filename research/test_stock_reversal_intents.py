import unittest
import inspect_short_rq_full as base
from stock_reversal_intents import past_action_clear,top_two

class FrozenIntentCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):_,cls.np=base.load_dependencies()
    def test_future_action_does_not_change_prior_candidate(self):
        n=self.np;cal=n.arange(20200101,20200129);d=cal[:12];i=n.arange(12)
        a=past_action_clear(d,i,[],cal,n);b=past_action_clear(d,i,[int(cal[20])],cal,n)
        self.assertTrue(n.array_equal(a,b))
    def test_action_at_or_before_signal_respects_twenty_session_window(self):
        n=self.np;cal=n.arange(20200101,20200129);i=n.array([5,25,26]);d=cal[i]
        self.assertEqual(past_action_clear(d,i,[int(cal[5])],cal,n).tolist(),[False,False,True])
    def test_old_action_before_available_calendar_does_not_create_phantom_window(self):
        n=self.np;cal=n.arange(20200101,20200129)
        self.assertTrue(past_action_clear(cal,n.arange(28),[20180101],cal,n).all())
    def test_rank_uses_loss_then_lag_amount_and_stable_identity(self):
        n=self.np;s=n.full((1,2),n.inf);a=n.full((1,2),-n.inf);c=n.full((1,2),-1,dtype=n.int32)
        for idx,pct,amount in [(0,-.04,50),(1,-.05,20),(2,-.05,30),(3,-.05,30)]:
            top_two(n.array([pct]),n.array([amount]),n.array([True]),idx,s,a,c,n)
        self.assertEqual(c.tolist(),[[2,3]])
        self.assertEqual(s.tolist(),[[-.05,-.05]])

if __name__=='__main__':unittest.main()
