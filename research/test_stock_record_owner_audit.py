import unittest
from stock_record_owner_audit import audit_record_owners
class RecordOwnerCases(unittest.TestCase):
    def binding(self,**kw):
        return dict(instrument_id='S',source_ex_date=20200106,issuer_record_date=20200103,record_field_visually_verified=True,original_ex_year_conflict=False,**kw)
    def dep(self):return [{'instrument_id':'S','date':20200106}]
    def test_absent_record_and_ex_sufficient(self):
        r=audit_record_owners(self.dep(),{'S':[20200102]},[self.binding()]);self.assertTrue(r[0]['specific_dependency_resolved_no_owner'])
    def test_record_presence_stays_unresolved_even_if_close_sale_possible(self):
        r=audit_record_owners(self.dep(),{'S':[20200103]},[self.binding()]);self.assertFalse(r[0]['specific_dependency_resolved_no_owner'])
    def test_ex_presence_stays_unresolved(self):
        r=audit_record_owners(self.dep(),{'S':[20200106]},[self.binding()]);self.assertFalse(r[0]['specific_dependency_resolved_no_owner'])
    def test_missing_binding_rejected(self):
        with self.assertRaises(ValueError):audit_record_owners(self.dep(),{},[])
    def test_original_ex_year_conflict_retained(self):
        b=self.binding();b['original_ex_year_conflict']=True;r=audit_record_owners(self.dep(),{},[b]);self.assertTrue(r[0]['original_ex_year_conflict_retained'])
    def test_duplicate_binding_rejected(self):
        with self.assertRaises(ValueError):audit_record_owners(self.dep(),{},[self.binding(),self.binding()])
if __name__=='__main__':unittest.main()
