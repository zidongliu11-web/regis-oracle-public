"""Check an explicit public projection against preserved actual receipts."""
from pathlib import Path
import argparse,copy,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock
from research.compute_retained_current_lookback import read_verified_source
P=ROOT/'research/MINUTE_AMOUNT_DIAGNOSIS_PROTOCOL_20261009.json'
R=ROOT/'research/MINUTE_AMOUNT_DIAGNOSIS_RESULT_20261009.json'
C=ROOT/'workflow_receipts/minute_amount_diagnosis_compute_20261009.json'
O=ROOT/'research/MINUTE_AMOUNT_PUBLICATION_CHECK_20261009.json'
REMOVED='relative_gap_if_daily_positive'

def projections(p):
    refs=p['publication_projection']['originals']
    original=read_verified_source(refs['result'])
    receipt=read_verified_source(refs['compute_receipt'])
    result=copy.deepcopy(original)
    assert REMOVED in result['summary']
    del result['summary'][REMOVED]
    result['checkpoint_metadata']['actual_checks_are_historical_before_projection']=True
    for cid, name in [('minute-amount-diagnosis-plan','plan_receipt'),
                      ('minute-amount-diagnosis-compute','compute_receipt')]:
        check=result['checkpoint_metadata']['actual_checks'][cid]
        check['original_repository_receipt_before_projection']=check['receipt']
        check['receipt']=refs[name]['path']
        check['sha256']=refs[name]['sha256']
        check['input_binding_role']='Historical original actual inputs, retained in Native; not current revised bindings.'
    result['publication_projection']={
        'schema':'regis.actual_result_public_projection.v1',
        'original_result':refs['result'],'original_compute_receipt':refs['compute_receipt'],
        'original_code':refs['code'],'original_protocol':refs['protocol'],
        'removed_fields':['summary.'+REMOVED],
        'original_computation_reexecuted':False,
        'original_input_bindings_are_revised_input_bindings':False,
        'current_future_code_sha256':p['input_hashes']['research/diagnose_current_minute_amount.py'],
        'role':'One actual original calculation preserved; this is its explicitly reduced publication, not a new calculation receipt.'}
    public_receipt=copy.deepcopy(receipt)
    stdout=json.loads(receipt['stdout'])
    assert REMOVED in stdout['summary']
    del stdout['summary'][REMOVED]
    public_receipt['stdout']=json.dumps(stdout,ensure_ascii=False)+'\n'
    public_receipt['publication_projection']={
        'schema':'regis.actual_receipt_public_projection.v1',
        'original_receipt':refs['compute_receipt'],'removed_fields':['stdout_json.summary.'+REMOVED],
        'stdout_is_original':False,'actual_command_reexecuted':False,
        'input_binding_role':'Original calculation version only; changed future code/protocol were not executed to recompute data.'}
    return result,public_receipt

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t) and idx[P.relative_to(ROOT).as_posix()]==sha(P)
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for name,ref in p['publication_projection']['originals'].items():
        assert sha(ref['path'])==ref['sha256'] and Path(ref['path']).stat().st_size==ref['bytes']
    return t

def validate(p):
    t=gate(p);expected,receipt=projections(p)
    assert read(R)==expected and read(C)==receipt
    for name in ['WORKFLOW_TASK.json','QUANT_GOAL_EXECUTION_STATE_20261001.json','research/PB01_PIPELINE_STATE_20261002.json']:
        j=read(ROOT/name)
        assert REMOVED not in j['current_checkpoint']['E185']['summary']
    assert REMOVED not in Path(ROOT/'research/diagnose_current_minute_amount.py').read_text(encoding='utf-8')
    gate(p)
    out={'schema':'regis.minute_amount_publication_check.v1','state':'public_projection_matches_preserved_actuals',
         'checked_utc':clock(),'contract_sha256':t['contract_sha256'],'protocol_sha256':sha(P),
         'public_result_sha256':sha(R),'public_compute_receipt_sha256':sha(C),
         'original_actual_refs':p['publication_projection']['originals'],
         'deletion_count_result':1,'deletion_count_original_stdout_JSON':1,
         'original_calc_input_bindings_preserved_historical':True,
         'first_nonpass_preserved':True,'original_amount_failure_unchanged':True,
         'current_code_stops_relative_gap_publication':True,
         'source_queries':0,'SDK_executions':0,'source_calculation_reexecutions':0,
         'new_unit_or_economic_cases':0,'model_admitted':False,'full_goal_complete':False}
    write(O,out);print(json.dumps({'projection_matches_originals':True,'source_calculation_reexecutions':0,
                                 'source_queries':0,'model_admitted':False}))

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--validate',action='store_true',required=True)
    ap.parse_args();validate(read(P))
