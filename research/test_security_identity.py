"""Synthetic adverse cases for source/identity/knowledge boundaries, not P&L."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from security_identity_evidence import (IdentityEvidenceError, Observation, decode_claim,
    verified_catalog, join_identity_keys, require_historical_or_economic_identity, strict_date, strict_utc)


class IdentityBoundaries(unittest.TestCase):
    def setUp(self):
        self.roles = {'600000.XSHG':'A_SHARE','510300.XSHG':'ETF'}
        self.floor = '2026-10-02T02:40:38.146352+00:00'
        self.now = '2026-10-02T14:00:00+00:00'
        self.o = Observation('synthetic-etf','synthetic-document','0'*64,'2026-10-02T13:00:00+00:00',
            '510300.XSHG','XSHG','510300','ETF','Synthetic ETF','Synthetic Manager',
            '2024-03-29','compiled_date','2012-05-28','2012-05-04','CNY','交易型开放式',None)
        self.keys = [('600000.XSHG','2026-09-01'),('510300.XSHG','2026-09-01')]

    def joined(self, obs=None, keys=None, clock=None):
        return join_identity_keys(self.keys if keys is None else keys, (self.o,) if obs is None else obs,
            selection_roles=self.roles, snapshot_known_at=self.floor, decision_at=clock or self.now)

    def test_partial_join_keeps_unmatched_stock_and_zero_admission(self):
        v=self.joined()
        self.assertEqual(v['510300.XSHG']['filing_candidate_rows'],1)
        self.assertEqual(v['600000.XSHG']['filing_candidate_rows'],0)
        self.assertEqual(v['510300.XSHG']['provider_units_certified_rows'],0)
        self.assertEqual(v['510300.XSHG']['historical_identity_bound_rows'],0)
        self.assertIsNone(v['510300.XSHG']['continuous_valid_interval'])

    def test_old_report_date_cannot_backdate_acquired_bytes(self):
        for at in ['2026-09-30T15:00:00+08:00',self.floor,'2026-10-02T12:59:59+00:00']:
            with self.subTest(clock=at):
                self.assertEqual(self.joined(clock=at)['510300.XSHG']['filing_candidate_rows'],0)

    def test_utc_offset_equivalence_at_capture_boundary(self):
        v=self.joined(clock='2026-10-02T21:00:00+08:00')
        self.assertEqual(v['510300.XSHG']['filing_candidate_rows'],1)

    def test_one_microsecond_before_quote_floor_hides_source(self):
        at=(strict_utc(self.floor)-timedelta(microseconds=1)).isoformat()
        self.assertEqual(self.joined(clock=at)['510300.XSHG']['visible_source_rows'],0)

    def test_date_only_cannot_be_an_exact_capture_clock(self):
        with self.assertRaisesRegex(IdentityEvidenceError,'clock_timezone_missing'):
            self.joined(clock='2026-10-02')

    def test_code_reuse_or_rename_without_chain_is_ambiguous(self):
        other=replace(self.o,observation_id='other',security_name='Another ETF',listing_date='2015-01-01')
        v=self.joined(obs=(self.o,other))['510300.XSHG']
        self.assertTrue(v['identity_anchor_conflict'])
        self.assertEqual(v['filing_candidate_rows'],0)

    def test_operation_mode_discrepancy_is_retained(self):
        other=replace(self.o,observation_id='other',operation_mode='普通开放式')
        v=self.joined(obs=(self.o,other))['510300.XSHG']
        self.assertTrue(v['operation_mode_conflict'])
        self.assertEqual(v['economic_admitted_rows'],0)

    def test_wrong_share_class_is_rejected(self):
        with self.assertRaisesRegex(IdentityEvidenceError,'share_class_mismatch'):
            self.joined(obs=(replace(self.o,asset_class='A_SHARE'),))

    def test_mismatched_exchange_or_code_is_rejected(self):
        with self.assertRaisesRegex(IdentityEvidenceError,'observation_key_mismatch'):
            self.joined(obs=(replace(self.o,security_code='510301'),))

    def test_unknown_quote_key_is_not_implicitly_registered(self):
        with self.assertRaisesRegex(IdentityEvidenceError,'unexpected_quote_key'):
            self.joined(keys=[('000001.XSHE','2026-09-01')])

    def test_duplicate_quote_rows_are_rejected(self):
        with self.assertRaisesRegex(IdentityEvidenceError,'duplicate_quote_key'):
            self.joined(keys=self.keys+self.keys)

    def test_duplicate_observation_id_is_rejected(self):
        with self.assertRaisesRegex(IdentityEvidenceError,'duplicate_observation_id'):
            self.joined(obs=(self.o,self.o))

    def test_before_listing_quote_is_not_a_candidate(self):
        self.assertEqual(self.joined(keys=[('510300.XSHG','2012-05-27')])['510300.XSHG']['filing_candidate_rows'],0)

    def test_future_sample_date_is_rejected(self):
        with self.assertRaisesRegex(IdentityEvidenceError,'sample_date_after_capture'):
            self.joined(keys=[('510300.XSHG','2026-10-03')])

    def test_invalid_calendar_date_is_rejected(self):
        with self.assertRaisesRegex(IdentityEvidenceError,'invalid_date'):
            strict_date('2026-02-30')

    def test_reporting_currency_does_not_certify_quote_units(self):
        v=self.joined()['510300.XSHG']
        self.assertEqual(v['security_trading_currency'],'CNY')
        self.assertEqual(v['provider_units_certified_rows'],0)
        self.assertIsNone(self.o.provider_numeric_units)

    def test_caller_flags_cannot_open_historical_economic_gate(self):
        for x in [(self.o,),{'economic_admitted':True,'units_verified':True}]:
            with self.assertRaisesRegex(IdentityEvidenceError,'historical_interval_units_vintage_and_execution_missing'):
                require_historical_or_economic_identity(x)

    def test_no_midnight_permanent_id_or_interval_inferred(self):
        self.assertIsNone(self.o.historical_available_at)
        self.assertIsNone(self.o.permanent_id)
        self.assertIsNone(self.o.valid_interval)

    def test_claim_value_cannot_disagree_with_literal(self):
        claim={'anchor':'币种','literal':'人民币','fragment':'交易币种人民币','codec':'currency','value':'USD'}
        with self.assertRaisesRegex(IdentityEvidenceError,'currency_value_mismatch'):
            decode_claim(claim,'交易币种 人民币')

    def test_nav_label_cannot_substitute_trading_currency_fragment(self):
        claim={'anchor':'交易币种','literal':'人民币','fragment':'交易币种人民币','codec':'currency','value':'CNY'}
        with self.assertRaisesRegex(IdentityEvidenceError,'labelled_fragment_missing'):
            decode_claim(claim,'净值计算单位人民币')

    def test_cn_date_keeps_date_precision(self):
        claim={'anchor':'编制日期','literal':'2024年03月29日','fragment':'编制日期2024年03月29日','codec':'cn_date','value':'2024-03-29'}
        self.assertEqual(decode_claim(claim,claim['fragment']),'2024-03-29')
        claim['value']='2024-03-29T00:00:00+08:00'
        with self.assertRaisesRegex(IdentityEvidenceError,'date_precision_invalid'):
            decode_claim(claim,claim['fragment'])


class FilingSourceBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.body=self.root/'.codex-local/identity_sources_20261002/synthetic.body'
        self.body.parent.mkdir(parents=True); self.body.write_bytes(b'%PDF-synthetic-fixture')
        sha=hashlib.sha256(self.body.read_bytes()).hexdigest()
        self.source={'id':'d','url':'https://example.invalid/fixture.pdf','sha256':sha,'bytes':self.body.stat().st_size,
            'body_path':'.codex-local/identity_sources_20261002/synthetic.body','http_status':200,'complete_within_cap':True,
            'started_utc':'2026-10-02T13:00:00+00:00','ended_utc':'2026-10-02T13:00:01+00:00'}
        def c(anchor,literal,value=None,codec='text'):
            return {'page':1,'anchor':anchor,'literal':literal,'fragment':anchor+literal,'codec':codec,'value':literal if value is None else value}
        claims={'code':c('基金代码','510300'),'exchange':c('交易所','上海证券交易所','XSHG','exchange'),
            'security_name':c('基金名称','测试交易型开放式指数证券投资基金'),
            'asset_class':c('基金名称','测试交易型开放式指数证券投资基金','ETF','etf_name'),
            'document_date':c('编制日期','2024年3月29日','2024-03-29','cn_date'),
            'as_of_date':c('编制日期','2024年3月29日','2024-03-29','cn_date')}
        self.text='\n'.join(c['fragment'] for c in claims.values())
        self.index={'schema':'regis.identity_observations.v1','selection':['510300.XSHG'],
            'selection_roles':{'510300.XSHG':'ETF'},'documents':[{'id':'d','url':self.source['url'],'sha256':sha}],
            'observations':[{'id':'o','document_id':'d','provider_key':'510300.XSHG','as_of_basis':'compiled_date','claims':claims}]}

    def tearDown(self): self.tmp.cleanup()

    def load(self):
        return verified_catalog(self.root,self.index,[self.source],page_reader=lambda d,p:self.text)

    def test_asof_identity_does_not_require_invented_listing_interval(self):
        o=self.load()[0]
        self.assertIsNone(o.listing_date)
        self.assertIsNone(o.valid_interval)
        self.assertIsNone(o.manager_name)

    def test_failed_or_partial_body_is_not_identity_evidence(self):
        for status,complete in [(301,False),(200,False),(404,True)]:
            with self.subTest(status=status):
                self.source.update(http_status=status,complete_within_cap=complete)
                with self.assertRaisesRegex(IdentityEvidenceError,'source_not_successful'): self.load()

    def test_body_hash_tampering_is_rejected(self):
        self.body.write_bytes(b'%PDF-other-fixture')
        with self.assertRaisesRegex(IdentityEvidenceError,'source_body_changed'): self.load()

    def test_receipt_and_index_source_binding_is_required(self):
        self.index['documents'][0]['sha256']='f'*64
        with self.assertRaisesRegex(IdentityEvidenceError,'document_binding_mismatch'): self.load()

    def test_non_pdf_body_is_rejected_even_with_matching_hash(self):
        self.body.write_bytes(b'not a pdf fixture')
        self.source.update(bytes=self.body.stat().st_size,sha256=hashlib.sha256(self.body.read_bytes()).hexdigest())
        self.index['documents'][0]['sha256']=self.source['sha256']
        with self.assertRaisesRegex(IdentityEvidenceError,'source_not_pdf'): self.load()

    def test_observation_authority_fields_are_rejected(self):
        self.index['observations'][0]['available_at']='2024-03-29T00:00:00+08:00'
        with self.assertRaisesRegex(IdentityEvidenceError,'unsupported_authority_field'): self.load()

    def test_field_codec_cannot_borrow_other_field_semantics(self):
        self.index['observations'][0]['claims']['as_of_date']['codec']='text'
        with self.assertRaisesRegex(IdentityEvidenceError,'field_codec_mismatch'): self.load()

    def test_reporting_or_NAV_currency_label_is_rejected(self):
        for label in ['报表货币','净值币种']:
            with self.subTest(label=label):
                self.index['observations'][0]['claims']['trading_currency']={
                    'page':1,'anchor':label,'literal':'人民币','fragment':label+'人民币','codec':'currency','value':'CNY'}
                self.text+='\n'+label+'人民币'
                with self.assertRaisesRegex(IdentityEvidenceError,'trading_currency_label_missing'): self.load()

    def test_year_precision_cannot_be_promoted_to_date(self):
        item=self.index['observations'][0];item['as_of_basis']='report_year'
        del item['claims']['as_of_date']; del item['claims']['document_date']
        item['claims']['report_year']={'page':1,'anchor':'年度报告','literal':'2023年','fragment':'2023年年度报告','codec':'cn_year','value':'2023'}
        self.text+='\n2023年年度报告'
        o=self.load()[0]
        self.assertEqual(o.report_year,'2023')
        self.assertIsNone(o.as_of_date)
        item['claims']['as_of_date']={'page':1,'anchor':'日期','literal':'2023年12月31日','fragment':'日期2023年12月31日','codec':'cn_date','value':'2023-12-31'}
        self.text+='\n日期2023年12月31日'
        with self.assertRaisesRegex(IdentityEvidenceError,'report_year_precision_invalid'):self.load()

    def test_preferred_share_class_cannot_be_ordinary_A(self):
        claim=self.index['observations'][0]['claims']['asset_class']
        claim.update(anchor='股票种类',literal='优先股',fragment='股票种类优先股',codec='a_share_class',value='A_SHARE')
        self.text+='\n股票种类优先股'
        with self.assertRaisesRegex(IdentityEvidenceError,'asset_class_unproved'):self.load()

    def test_ordinary_share_code_class_name_exchange_bind_one_row(self):
        item=self.index['observations'][0]
        row='普通股A股上海证券交易所测试银行600000-'
        def c(literal,value,codec):
            return {'page':1,'anchor':'普通股A股','literal':literal,'fragment':row,'codec':codec,'value':value}
        item['claims'].update(code=c('600000','600000','text'),exchange=c('上海证券交易所','XSHG','exchange'),
            security_name=c('测试银行','测试银行','text'),asset_class=c('普通股A股','A_SHARE','a_share_class'))
        item['provider_key']='600000.XSHG';self.index['selection']=['600000.XSHG'];self.index['selection_roles']={'600000.XSHG':'A_SHARE'}
        self.text+='\n'+row
        o=self.load()[0]
        self.assertEqual(o.asset_class,'A_SHARE')
        self.assertIsNone(o.trading_currency)
        item['claims']['code'].update(anchor='另处股票代码',fragment='另处股票代码600000')
        self.text+='\n另处股票代码600000'
        with self.assertRaisesRegex(IdentityEvidenceError,'ordinary_share_row_unbound'):self.load()

    def test_page_scope_is_bounded(self):
        self.index['observations'][0]['claims']['code']['page']=9
        with self.assertRaisesRegex(IdentityEvidenceError,'page_scope_invalid'): self.load()

    def test_future_asof_date_is_rejected(self):
        for field in ['as_of_date','document_date']:
            c=self.index['observations'][0]['claims'][field]
            c.update(literal='2027年3月29日',value='2027-03-29',fragment='编制日期2027年3月29日')
        self.text=self.text.replace('2024年3月29日','2027年3月29日')
        with self.assertRaisesRegex(IdentityEvidenceError,'observation_from_future'): self.load()

    def test_body_path_cannot_escape_local_filing_custody(self):
        self.source['body_path']='outside.body'
        with self.assertRaisesRegex(IdentityEvidenceError,'body_custody_invalid'): self.load()


if __name__ == '__main__':
    unittest.main(verbosity=1)
