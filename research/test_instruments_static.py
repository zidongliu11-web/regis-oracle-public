"""Binary parser counterexamples; synthetic metadata only."""
import pickle
from datetime import date
import unittest
from read_instruments_static import parse,date_int,Inert

class StaticMetadata(unittest.TestCase):
    def test_literal_identity_and_date(self):
        rows,ops=parse(pickle.dumps([{'type':'CS','listed_date':date(2005,1,4)}],protocol=2))
        self.assertEqual(date_int(rows[0]['listed_date']),20050104)
        self.assertGreater(ops.get('REDUCE',0),0)
    def test_reducer_is_inert_without_invocation(self):
        # A dangerous global/reducer is represented as inert data; never executed.
        body=b'\x80\x02]q\x00}q\x01X\x01\x00\x00\x00xcos\nsystem\nX\x1c\x00\x00\x00should-never-run-any-command\x85Rs a.'
        body=body.replace(b'\x1c\x00\x00\x00',len(b'should-never-run-any-command').to_bytes(4,'little')).replace(b's a.',b'sa.')
        rows,_=parse(body)
        self.assertIsInstance(rows[0]['x'],Inert)
        self.assertIsNone(date_int(rows[0]['x']))
    def test_cycle_and_unsupported_protocol_rejected(self):
        rows=[];rows.append(rows)
        with self.assertRaises(ValueError):parse(pickle.dumps(rows,protocol=2))
        with self.assertRaises(ValueError):parse(pickle.dumps([{}],protocol=4))
    def test_trailing_payload_rejected(self):
        with self.assertRaises(ValueError):parse(pickle.dumps([{}],protocol=2)+b'extra')
    def test_unknown_delisting_sentinel_all_representations(self):
        rows,_=parse(pickle.dumps([{'date':date(2999,12,31)}],protocol=2))
        for value in ('2999-12-31',29991231,rows[0]['date']):self.assertIsNone(date_int(value))
        self.assertIsNone(date_int(20260231))
    def test_indirect_cycle_rejected_but_shared_containers_allowed(self):
        obj={};obj['nested']=[obj]
        with self.assertRaises(ValueError):parse(pickle.dumps([obj],protocol=2))
        shared=['literal'];rows,_=parse(pickle.dumps([{'a':shared,'b':shared}],protocol=2))
        self.assertIs(rows[0]['a'],rows[0]['b'])
    def test_operation_checkpoint_and_memo_limit(self):
        data=pickle.dumps([{'x':list(range(12000))}],protocol=2);calls=[]
        parse(data,checkpoint=lambda:calls.append(True));self.assertTrue(calls)
        with self.assertRaises(ValueError):parse(data,max_memo=1)
    def test_empty_float_state_dates(self):
        from inspect_rq_stock_history import event_dates
        import inspect_short_rq_full as b
        _,np=b.load_dependencies();dates,invalid=event_dates(np.array([],dtype='float64'),np)
        self.assertEqual(len(dates),0);self.assertEqual(invalid,0)
    def test_exact_float_state_date_and_invalid_rows(self):
        from inspect_rq_stock_history import event_dates
        import inspect_short_rq_full as b
        _,np=b.load_dependencies();dates,invalid=event_dates(np.array([20200102.,float('nan'),20200102.5,20200102000001.],dtype='float64'),np)
        self.assertEqual(list(dates),[20200102]);self.assertEqual(invalid,3)

if __name__=='__main__':unittest.main()
