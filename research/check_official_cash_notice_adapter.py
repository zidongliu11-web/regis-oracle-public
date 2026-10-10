"""Bounded actual engineering tests plus two public-document normalizations."""
import hashlib,json,pathlib,subprocess,sys
from datetime import datetime,timezone
W=pathlib.Path.cwd();p=json.loads((W/'research/OFFICIAL_CASH_NOTICE_ADAPTER_PROTOCOL_20261007.json').read_bytes());N=pathlib.Path(p['native_evidence_root'])
sys.path.insert(0,str(W))
sha=lambda b:hashlib.sha256(b).hexdigest()
enc=lambda d:(json.dumps(d,ensure_ascii=False,indent=2)+'\n').encode()
number=sum((N/f'batch_{i:02d}.dispatch.json').exists() for i in range(1,3))+1
assert number<=p['limits']['formal_test_batches_max'],'No automatic extra batch'
mark=N/f'batch_{number:02d}.dispatch.json';assert not mark.exists()
src=pathlib.Path(p['actual_notice_input']['path']);assert sha(src.read_bytes())==p['actual_notice_input']['sha256']
inputs=p['check_inputs'];before={f:sha((W/f).read_bytes()) for f in inputs}
mark.write_bytes(enc({'run':number,'started_utc':datetime.now(timezone.utc).isoformat(),'before':before,'source_sha256':sha(src.read_bytes()),'source_requests':0,'economic_runs':0}))
cmd=[sys.executable,'-X','utf8','-m','pytest','-q','tests/test_m24_data_adapter.py','tests/test_m24_official_cash_notices.py']
q=subprocess.run(cmd,cwd=W,capture_output=True,timeout=80,creationflags=subprocess.CREATE_NO_WINDOW)
(N/f'batch_{number:02d}.pytest.stdout').write_bytes(q.stdout);(N/f'batch_{number:02d}.pytest.stderr').write_bytes(q.stderr)
output_file=None
if q.returncode==0:
    from research.m24_data_adapter import normalize_official_cash_notices
    result=normalize_official_cash_notices(src,expected_sha256=p['actual_notice_input']['sha256'],
                                          captured_at=p['actual_notice_input']['captured_at'])
    assert len(result['rows'])==2 and all(row['available_at'] is None and row['cash_credit_at'] is None
                                        and row['status']=='announced_cash_terms_only' for row in result['rows'])
    assert result['quality']['complete_target_interval'] is False and result['authority']['data_admitted'] is False
    output_file=f'batch_{number:02d}.actual_normalized_notices.json'
    (N/output_file).write_bytes(enc(result))
after={f:sha((W/f).read_bytes()) for f in inputs};source_after=sha(src.read_bytes())
proof={'run':number,'argv':cmd,'pytest_returncode':q.returncode,'timed_out':False,
       'before':before,'after':after,'inputs_unchanged':before==after,
       'source_sha256_before':p['actual_notice_input']['sha256'],'source_sha256_after':source_after,
       'actual_normalized_file':output_file,'actual_normalized_sha256':sha((N/output_file).read_bytes()) if output_file else None,
       'actual_public_document_rows':2 if output_file else 0,'market_rows':0,'economic_runs':0,
       'data_or_financial_admitted':False,'full_goal_complete':False}
(N/f'batch_{number:02d}.execution.json').write_bytes(enc(proof))
print(q.stdout.decode('utf-8',errors='replace'));print(q.stderr.decode('utf-8',errors='replace'))
assert before==after and source_after==p['actual_notice_input']['sha256']
raise SystemExit(q.returncode)
