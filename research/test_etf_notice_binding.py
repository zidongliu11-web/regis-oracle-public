"""Reject the independently discovered cross-fund cash attribution failure."""
import unittest

from research.inspect_etf_cash_census_completion import (
    extract_etf_notice_block, validate_notice_fields,
)

TITLE = '华泰柏瑞沪深300交易型开放式指数证券投资基金收益分配公告'
PREFIX = '华泰柏瑞基金管理有限公司关于'

def notice(code, amount, title='邻基金收益分配公告', denomination='元/10份基金份额'):
    name = '华泰柏瑞沪深300交易型开放式指数证券投资基金' if code == '510300' else '邻基金'
    return (PREFIX + title + '1公告基本信息基金名称' + name + '基金主代码' + code +
            '基金合同生效日2012年5月4日本次分红方案(单位:' + denomination + ')' + amount + '有关年度分红次数1' +
            '2与分红相关的其他信息权益登记日2015年1月19日除息日2015年1月20日' +
            '现金红利发放日2015年1月23日3其他需要提示的事项结束')

def validate(page):
    validate_notice_fields(extract_etf_notice_block(page), '2015-01-19', '2015-01-20', '2015-01-23', '0.35')

class NoticeBindingTests(unittest.TestCase):
    def test_correct_neighbor_cannot_mask_wrong_target_cash(self):
        page = notice('460001', '0.35') + notice('510300', '0.99', TITLE) + notice('460002', '0.35')
        with self.assertRaisesRegex(AssertionError, 'target cash amount mismatch'):
            validate(page)

    def test_target_value_not_number_prefix(self):
        with self.assertRaisesRegex(AssertionError, 'target cash amount mismatch'):
            validate(notice('510300', '0.3599', TITLE))

    def test_duplicate_target_notice_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'ambiguous target notice title'):
            validate(notice('510300', '0.35', TITLE) * 2)

    def test_foreign_fund_inside_target_block_rejected(self):
        page = notice('510300', '0.35', TITLE).replace('3其他需要提示的事项', '基金主代码4600013其他需要提示的事项')
        with self.assertRaisesRegex(AssertionError, 'notice code ambiguity'):
            validate(page)

    def test_cash_unit_mismatch_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'denomination ambiguous'):
            validate(notice('510300', '0.35', TITLE, denomination='元/1份基金份额'))

    def test_valid_target_does_not_inherit_neighbor_wrong_cash_or_dates(self):
        before = notice('460001', '0.99').replace('2015年1月19日', '2015年1月2日')
        after = notice('460002', '0.12').replace('2015年1月20日', '2015年1月3日')
        validate(before + notice('510300', '0.3500', TITLE) + after)

    def test_missing_target_cash_cannot_borrow_neighbor_scheme(self):
        target = notice('510300', '0.35', TITLE).replace('本次分红方案(单位:元/10份基金份额)0.35', '')
        with self.assertRaisesRegex(AssertionError, 'missing or ambiguous cash scheme'):
            validate(notice('460001', '0.35') + target + notice('460002', '0.35'))

    def test_ambiguous_target_date_rejected(self):
        target = notice('510300', '0.35', TITLE).replace('权益登记日2015年1月19日',
            '权益登记日2015年1月19日权益登记日2015年1月22日')
        with self.assertRaisesRegex(AssertionError, 'ambiguous 权益登记日'):
            validate(target)

    def test_numbered_heading_punctuation_keeps_same_binding(self):
        page = notice('510300', '0.35', TITLE).replace('2与', '2.与').replace('3其他', '3.其他')
        validate_notice_fields(page, '2015-01-19', '2015-01-20', '2015-01-23', '0.35')

if __name__ == '__main__':
    unittest.main()
