import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from matched_etf_cash_book import replay_matched_etf
from decimal import Decimal
class MatchedETFCase(unittest.TestCase):
    def setUp(self):
        self.dates=[20200102,20200103,20200106,20200107]
        self.bars=[dict(open='10',close='10',low='9',high='11',limit_down='8',limit_up='12',volume='1') for _ in self.dates]
        self.s=dict(id='S',entry_delay_sessions=0,entry_fill_fraction='1',buy_all_in_fee_rate='0',minimum_fee='0',slippage='0')
    def run_book(self,div=None):return replay_matched_etf(self.bars,self.dates,div or [],self.s)
    def test_no_fill_zero_minimum_fee(self):
        self.s.update(entry_fill_fraction='0',minimum_fee='100');r,_=self.run_book();self.assertEqual(r['terminal_marked_nav_CNY'],'150000.00');self.assertEqual(r['total_fees_CNY'],'0.00')
    def test_minimum_fee_inside_cash_lots(self):
        self.s['minimum_fee']='5';r,_=self.run_book();self.assertEqual(r['terminal_marked_nav_CNY'],'149995.00');self.assertEqual(r['terminal_cash_CNY'],'995.00')
    def test_ex_receivable_offsets_price_drop_once(self):
        self.bars[2]['close']=self.bars[3]['close']='9';r,ledger=self.run_book([dict(record_date=20200103,ex_date=20200106,pay_date=20200107,cash_per_unit=Decimal('1'))]);self.assertEqual(r['terminal_marked_nav_CNY'],'150000.00');self.assertEqual(ledger[2]['nav_CNY'],ledger[3]['nav_CNY']);self.assertEqual(r['paid_dividend_CNY'],'15000.00')
    def test_delayed_after_record_has_no_dividend(self):
        self.s['entry_delay_sessions']=1;r,_=self.run_book([dict(record_date=20200103,ex_date=20200106,pay_date=20200107,cash_per_unit=Decimal('1'))]);self.assertEqual(r['paid_dividend_CNY'],'0.00')
    def test_adverse_price_outside_range_no_fill(self):
        self.s['slippage']='0.2';r,_=self.run_book();self.assertEqual(r['modeled_entry_fills'],0);self.assertEqual(r['conditional_entry_reason'],'price_boundary_no_fill')
    def test_terminal_mark_not_forced_sale(self):
        r,_=self.run_book();self.assertFalse(r['terminal_sale_executed']);self.assertEqual(r['terminal_cash_CNY'],'0.00');self.assertEqual(r['terminal_marked_nav_CNY'],'150000.00')
    def test_negative_fee_rejected(self):
        self.s['buy_all_in_fee_rate']='-1'
        with self.assertRaises(ValueError):self.run_book()
if __name__=='__main__':unittest.main()
