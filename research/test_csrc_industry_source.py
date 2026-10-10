import unittest
from recover_csrc_industry_sources import parse_index,metadata

class SourceIdentityTests(unittest.TestCase):
 def test_target_filter_preserves_publication_not_quarter_end(self):
  html='<li><a href="a.shtml">2017年1季度上市公司行业分类结果</a><span>2017-06-01</span></li><li><a href="future.shtml">2021年1季度上市公司行业分类结果</a>2021-04-26</li>'
  got=parse_index(html,'https://www.csrc.gov.cn/csrc/list.shtml','a'*64,['2017Q1'])
  self.assertEqual(len(got),1);self.assertEqual(got[0]['index_declared_publication_date'],'2017-06-01');self.assertTrue(got[0]['article_url'].endswith('/a.shtml'))
 def test_index_ambiguous_dates_rejected(self):
  with self.assertRaises(ValueError):parse_index('<li><a href="a">2013年1季度上市公司行业分类结果</a>2013-04-01 2013-04-02</li>','https://www.csrc.gov.cn/list','a'*64,['2013Q1'])
 def test_conflicting_article_date_conservative_later(self):
  got=metadata('日期：<span>2017-06-03</span><a href="file.pdf">附件</a>','https://www.csrc.gov.cn/csrc/a.shtml','2017-06-01')
  self.assertTrue(got['publication_date_conflict']);self.assertEqual(got['declared_publication_date'],'2017-06-03')
 def test_multiple_attachments_not_silent_choice(self):
  with self.assertRaises(ValueError):metadata('日期：2017-06-01<a href="a.pdf">a</a><a href="b.pdf">b</a>','https://www.csrc.gov.cn/a','2017-06-01')
 def test_duplicate_same_url_not_ambiguity_and_bad_date_reject(self):
  got=metadata('日期：2017-06-01<a href="a.pdf">a</a><a href="a.pdf">again</a>','https://www.csrc.gov.cn/a','2017-06-01')
  self.assertEqual(got['attachment_url'],'https://www.csrc.gov.cn/a.pdf')
  with self.assertRaises(ValueError):metadata('日期：2017-99-01<a href="a.pdf">a</a>','https://www.csrc.gov.cn/a','2017-06-01')

if __name__=='__main__':unittest.main()
