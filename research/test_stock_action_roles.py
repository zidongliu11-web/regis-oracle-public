import unittest
import inspect_short_rq_full as base
from audit_stock_action_roles import normalize

class ActionDateCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):_,cls.np=base.load_dependencies()
    def test_exact_float_announcement_is_same_date(self):
        self.assertEqual(normalize(self.np.array([20190104000000.0]),self.np).tolist(),[20190104])
    def test_unknown_announcement_retained_as_unknown_not_fake_date(self):
        self.assertEqual(normalize(self.np.array([self.np.nan]),self.np,True).tolist(),[0])
        with self.assertRaises(ValueError):normalize(self.np.array([self.np.nan]),self.np)
    def test_fractional_date_rejected(self):
        with self.assertRaises(ValueError):normalize(self.np.array([20190104.5]),self.np,True)
    def test_nonzero_time_tail_not_normalized_away(self):
        with self.assertRaises(ValueError):normalize(self.np.array([20190104093500]),self.np)
    def test_invalid_calendar_date_rejected(self):
        with self.assertRaises(ValueError):normalize(self.np.array([20190230]),self.np)
    def test_infinite_announcement_not_accepted_as_unknown(self):
        with self.assertRaises(ValueError):normalize(self.np.array([self.np.inf]),self.np,True)

if __name__=='__main__':unittest.main()
