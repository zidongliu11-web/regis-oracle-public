import unittest
from csrc_industry_members import *
class MemberSourceTests(unittest.TestCase):
 def test_publication_session_strictly_following_and_weekend(self):
  c=[20130104,20130107,20130108,20130109];self.assertEqual(following_session(c,'2013-01-07'),20130108);self.assertEqual(following_session(c,'2013-01-05'),20130107)
 def test_future_member_no_backfill_and_stale_member_removed(self):
  s=[{'effective_session':20130108,'members':{'000001':'01'}},{'effective_session':20130402,'members':{'000002':'02'}}]
  self.assertIsNone(select_snapshot(s,20130107));self.assertIn('000001',select_snapshot(s,20130401)['members']);self.assertNotIn('000001',select_snapshot(s,20130402)['members'])
 def test_pdf_page_carry_and_page_number_not_group(self):
  r,d=parse_pdf_texts(['01 农业 000001 甲\n000002 乙\n14','门类名称\n000003 丙\n02 林业 000004 丁'])
  self.assertEqual(r['000003']['source_industry_major_numeric'],'01');self.assertEqual(r['000004']['source_industry_major_numeric'],'02');self.assertEqual(d['per_page_rows'],[2,2])
 def test_pdf_ambiguity_unknown_or_duplicate_fail(self):
  for text in ['000001 甲','01 农业 000001 甲\n02 林业 000001 甲','01 农业 000001 甲 000002 乙']:
   with self.assertRaises(ValueError):parse_pdf_texts([text])
 def test_table_numeric_zero_pad_and_merged_empty_group(self):
  r,d=parse_cell_tables([[['门类','行业大类代码','行业名','上市公司股票代码','简称'],['A',1,'农业',1.0,'甲'],['','','','000002','乙']]])
  self.assertEqual(r['000001']['source_industry_major_numeric'],'01');self.assertEqual(r['000002']['source_industry_major_numeric'],'01')
 def test_non_identifier_and_column_shift_fail(self):
  with self.assertRaises(ValueError):normalized_code(1.5)
  with self.assertRaises(ValueError):parse_cell_tables([[['行业大类代码','上市公司股票代码'],['01']]])
if __name__=='__main__':unittest.main()
