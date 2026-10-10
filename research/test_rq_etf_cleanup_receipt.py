"""Fault probes on invented temporary files; no market download."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import recover_rq_etf_history as reader

class CleanupReceipt(unittest.TestCase):
    def probe(self, mock_method, fault):
        with tempfile.TemporaryDirectory(prefix='regis-cleanup-fault-') as directory:
            local=Path(directory).resolve()
            (local/'bundle.tar.bz2').write_bytes(b'test-only')
            target=local/'receipt.json'
            report={'state':'captured','resources':{}}
            with patch.object(reader,'RECEIPT',target), patch.object(Path,mock_method,**fault):
                reader.finish_receipt(report,local,SimpleNamespace(started=time.monotonic()))
            output=json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(output['state'],'failed')
            self.assertFalse(output['temporary_archive_removed'])
            self.assertIn('ended_utc',output)
            self.assertIn('elapsed_seconds',output['resources'])
            self.assertTrue((local/'bundle.tar.bz2').exists())
    def test_unlink_permission_error_still_persists_terminal_receipt(self):
        self.probe('unlink',{'side_effect':PermissionError('test-only')})
    def test_unsafe_symlink_rejection_still_persists_terminal_receipt(self):
        self.probe('is_symlink',{'return_value':True})

if __name__=='__main__':unittest.main()
