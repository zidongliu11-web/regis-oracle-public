"""Small accounting counterexamples; these invented prices are never market evidence."""
import unittest
from decimal import Decimal
from run_etf_daily_reference import run_scenario

class ReferenceAccounting(unittest.TestCase):
    def setUp(self):
        self.spec={'initial_CNY':'150000','lot_size':100}
        self.scenario={'id':'test','entry_session_index':1,'entry_price_field':'open','fill_fraction':'1',
                       'all_in_fee_rate':'0','slippage_rate':'0','minimum_fee_CNY':'0'}
        self.dates=[20260112,20260113,20260114,20260115,20260116]
        self.bars={'open':[10,10,9,9,9],'close':[10,10,9,9,9]}
        self.dividends=[{'record_date':20260113,'ex_date':20260114,'pay_date':20260116,
                         'cash_per_unit':Decimal('1')}]
    def test_ex_date_receivable_offsets_price_drop_without_double_credit(self):
        r=run_scenario(self.scenario,self.bars,self.dates,self.dividends,self.spec)
        self.assertEqual(r['terminal_CNY'],'150000.00')
        self.assertEqual(r['max_drawdown_on_daily_marks'],0)
        self.assertEqual(r['model_dividend_cash_paid_CNY'],'15000.00')
    def test_buy_after_record_date_does_not_receive_dividend(self):
        self.scenario['entry_session_index']=2
        r=run_scenario(self.scenario,self.bars,self.dates,self.dividends,self.spec)
        self.assertEqual(r['model_dividend_cash_paid_CNY'],'0')
        self.assertEqual(r['total_return'],0)
    def test_no_fill_charges_no_minimum_and_leaves_all_cash(self):
        self.scenario.update(fill_fraction='0',minimum_fee_CNY='1000',all_in_fee_rate='0.001')
        r=run_scenario(self.scenario,self.bars,self.dates,self.dividends,self.spec)
        self.assertEqual(r['terminal_CNY'],'150000.00')
        self.assertEqual(r['model_transaction_fees_CNY'],'0')
        self.assertEqual(r['filled_fund_units'],0)
    def test_minimum_fee_is_funded_from_cash_not_overdraft(self):
        self.scenario['minimum_fee_CNY']='5'
        r=run_scenario(self.scenario,self.bars,self.dates,self.dividends,self.spec)
        self.assertEqual(r['filled_fund_units'],14900)
        self.assertEqual(r['entry_uninvested_CNY'],'995.00')
    def test_negative_fee_model_is_rejected(self):
        self.scenario['all_in_fee_rate']='-0.01'
        with self.assertRaises(ValueError):run_scenario(self.scenario,self.bars,self.dates,self.dividends,self.spec)

if __name__=='__main__':unittest.main()
