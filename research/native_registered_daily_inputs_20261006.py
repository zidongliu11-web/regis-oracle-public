"""One native input-only manual command and independent metadata verification."""
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
import ast
import hashlib
import json
import math
import os
import subprocess
import sys
import time

R = Path.cwd()
sys.path.insert(0, str(R))
N = Path(__file__).resolve().parent
PP = R / 'research/REGISTERED_DAILY_INPUT_PROTOCOL_20261006.json'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024*1024): h.update(block)
    return h.hexdigest()


def pins():
    from factor_system.native_snapshot import _native_root
    _native_root(N)
    p = json.loads(PP.read_bytes())
    assert Path(sys.executable).resolve() == Path(p['runtime']['python_path']).resolve()
    assert digest(sys.executable) == p['runtime']['python_exe_sha256']
    assert digest(__file__) == p['helper_sha256']
    for path, h in p['source_hashes'].items(): assert digest(R/path) == h
    for path, h in p['native_metadata_hashes'].items(): assert digest(path) == h
    return p


def benchmark():
    p = pins()
    assert not (N/'benchmark_result.json').exists()
    from factor_system.registered_daily_inputs import calculate_rows, calculate_arrays
    import numpy as np
    raw = (N/'prior_calculate_source.py').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == p['prior_calculate_sha256']
    tree = ast.parse(raw)
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef) and tree.body[0].name == 'calculate'
    namespace = {'math':math,'IDS':('close_acceptance','pullback_volume_3_20')}
    exec(compile(tree, 'frozen_prior_own_calculate', 'exec'),namespace)
    rows = [{'time':str(date(1980,1,1)+timedelta(days=i)), 'high':14.,'low':10.,'close':13.,
             'total_turnover':float(i+1)} for i in range(10000)]
    calendar = {int(r['time'].replace('-','')) for r in rows}
    start = time.perf_counter(); old = namespace['calculate'](rows,calendar); old_seconds = time.perf_counter()-start
    start = time.perf_counter(); new = calculate_rows(rows,calendar); new_seconds = time.perf_counter()-start
    for a,b in zip(old,new):
        assert a['date'] == b['date'] and a['missing'] == b['missing']
        for name in namespace['IDS']:
            assert (a[name] is None and b[name] is None) or math.isclose(a[name],b[name],rel_tol=2e-15,abs_tol=0)
    start = time.perf_counter()
    values = calculate_arrays(np.full(10000,14.),np.full(10000,10.),np.full(10000,13.),
                              np.arange(1,10001,dtype=float),np.arange(10000,dtype=np.int64))
    kernel_seconds = time.perf_counter()-start
    assert int((values[3] == 0).sum()) == 9977
    out = {'schema':1,'synthetic_rows':10000,'old_seconds':old_seconds,'new_rows_seconds':new_seconds,
           'new_kernel_seconds':kernel_seconds,'row_api_speedup':old_seconds/new_seconds,
           'synthetic_only':True,'parity_passed':True,'old_calls':1,'new_rows_calls':1,'new_kernel_calls':1,
           'actual_market_runs':0,'economic_runs':0,'recorded_utc':datetime.now(timezone.utc).isoformat(),
           'protocol_sha256':digest(PP),'prior_source_sha256':p['prior_calculate_sha256']}
    with (N/'benchmark_result.json').open('x',encoding='utf-8') as stream: json.dump(out,stream,indent=2)
    print(json.dumps(out))


def produce():
    p = pins()
    attempt = p['attempt']
    assert attempt == 3  # Separate identity replan after two retained failures.
    review = json.loads((N/f'fixed_precode_review_{attempt}.json').read_bytes())
    assert review['precode_passed'] and review['current_P0_P1_P2'] == {'P0':0,'P1':0,'P2':0}
    assert review['protocol_sha256'] == digest(PP)
    started = datetime.now(timezone.utc).isoformat()
    assert review['primary_transcribed_utc'] < started
    with (N/f'actual_invocation_started_{attempt}.json').open('x',encoding='utf-8') as stream:
        json.dump({'started_utc':started,'protocol_sha256':digest(PP),'invocations':1,'attempt':attempt},stream)
    out = {'started_utc':started,'state':'actual_daily_input_started','protocol_sha256':digest(PP),
           'helper_sha256':digest(__file__),'native_invocations':1,'total_native_invocations':attempt,'raw_values_to_AI_Git':0,
           'GETs':0,'economic_runs':0,'source_or_strategy_admitted':False,'full_goal_complete':False}
    try:
        call = subprocess.run([sys.executable,'-X','utf8','-m','factor_system.run',
            '--native-root',p['native_root'],'--request-relative',p['request_ref']['relative'],
            '--request-sha256',p['request_ref']['sha256']],capture_output=True,text=True,encoding='utf-8',
            timeout=210,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        out['returncode'] = call.returncode
        if call.returncode != 0:
            # The entry emits count/status metadata only. Do not forward stderr.
            try:
                response=json.loads(call.stdout)
                out['entry_failure_metadata']={k:response[k] for k in ('state','ok','reason') if k in response}
            except (ValueError,TypeError):out['entry_failure_metadata_unavailable']=True
        assert call.returncode == 0 and not call.stderr
        response = json.loads(call.stdout)
        assert response['ok'] is True and response['state'] == 'daily_input_production_completed'
        assert response['source_qualified'] is response['model_admitted'] is response['orders_enabled'] is False
        assert response['counts'] == {'files':5487,'source_rows':12934946,'consumed_rows':12934946}
        result_path = Path(p['native_root'])/p['output_directory']/'result.json'
        native = json.loads(result_path.read_bytes())
        assert native['state'] == 'daily_input_production_completed' and native['counts'] == response['counts']
        assert native['elapsed_seconds'] <= 180 and native['peak_rss_bytes'] <= 536870912
        out.update(state='actual_daily_input_completed',counts=response['counts'],reason_counts=native['reason_counts'],
            native_result_path=str(result_path),native_result_sha256=digest(result_path),
            reported_halt_rows=native['reported_halt_rows'],halt_dataset_missing_rows=native['halt_dataset_missing_rows'],
            resources={k:native[k] for k in ('elapsed_seconds','peak_rss_bytes','output_bytes')})
        pins()
    except BaseException as exc:
        out['state']='actual_daily_input_failed';out['error_type']=type(exc).__name__;raise
    finally:
        out['ended_utc']=datetime.now(timezone.utc).isoformat()
        with (N/f'production_result_{attempt}.json').open('x',encoding='utf-8') as stream:
            json.dump(out,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
    print(json.dumps(out))


def verify_benchmark():
    p=pins();out=json.loads((N/'benchmark_result.json').read_bytes())
    prior=json.loads((N/'before_terminal_repair_research__REGISTERED_DAILY_INPUT_PROTOCOL_20261006.json').read_bytes())
    assert out['protocol_sha256']==digest(N/'before_terminal_repair_research__REGISTERED_DAILY_INPUT_PROTOCOL_20261006.json')
    assert out['prior_source_sha256']==p['prior_calculate_sha256']==prior['prior_calculate_sha256']
    assert p['source_hashes']['factor_system/registered_daily_inputs.py']==prior['source_hashes']['factor_system/registered_daily_inputs.py']
    assert out['synthetic_rows']==10000 and out['parity_passed'] and out['synthetic_only']
    assert out['old_calls']==out['new_rows_calls']==out['new_kernel_calls']==1
    assert out['economic_runs']==out['actual_market_runs']==0
    print(json.dumps({'prior_synthetic_benchmark_verified':True,'benchmark_sha256':digest(N/'benchmark_result.json'),
        'original_executed_protocol_sha256':out['protocol_sha256'],'unchanged_kernel_sha256':p['source_hashes']['factor_system/registered_daily_inputs.py'],
        'new_benchmark_calls':0,'source_or_strategy_admitted':False}))


def verify():
    p = pins()
    from factor_system.native_snapshot import _native_root,_no_links
    base = _native_root(p['native_root']);directory=base/p['output_directory']
    _no_links(base,directory.relative_to(base))
    out=json.loads((N/f"production_result_{p['attempt']}.json").read_bytes())
    assert out['state']=='actual_daily_input_completed' and out['returncode']==0 and out['native_invocations']==1
    expected=directory/'result.json'
    assert Path(out['native_result_path'])==expected and digest(expected)==out['native_result_sha256']
    native=json.loads(expected.read_bytes())
    assert native['counts']==out['counts'] and native['spec']==json.loads(Path(p['spec_path']).read_bytes())
    assert native['source_qualified'] is native['model_admitted'] is native['orders_enabled'] is False
    assert sum(x['rows'] for x in native['files'].values())==native['consumed_rows']==12934946
    assert sum(x['bytes'] for x in native['files'].values())==native['output_bytes']<=536870912
    for key,ref in native['files'].items():
        assert ref['relative']==key+'.inputs.npy'
        path=directory/ref['relative'];_no_links(base,path.relative_to(base))
        assert path.stat().st_nlink==1 and path.stat().st_size==ref['bytes'] and digest(path)==ref['sha256']
    pins()
    print(json.dumps({'actual_native_inputs_verified':True,'counts':out['counts'],
                      'vector_values_decoded_by_verifier':False,'source_or_strategy_admitted':False,'full_goal_complete':False}))


if __name__=='__main__':
    if sys.argv[1:]==['--benchmark']:benchmark()
    elif sys.argv[1:]==['--verify']:verify()
    elif sys.argv[1:]==['--verify-benchmark']:verify_benchmark()
    elif not sys.argv[1:]:produce()
    else:raise SystemExit(2)
