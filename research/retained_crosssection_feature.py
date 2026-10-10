"""Compute the original registered feature once from retained Sep 28 sources.

Local numerical replay only; stale data cannot create current signals.
The tested E179 formula and masks are reused without variants or tuning.
"""
from pathlib import Path
import argparse, json, sys
ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from research.capture_completed_daily_pair import read, write, sha, clock
from research.dated_daily_factor_input import derive
P = ROOT / 'research/RETAINED_CROSSSECTION_FEATURE_PROTOCOL_20261008.json'
O = ROOT / 'research/RETAINED_CROSSSECTION_FEATURE_RESULT_20261008.json'


def gate(p):
    from scripts.workflow_guard import contract_hash
    t = read(ROOT / 'WORKFLOW_TASK.json')
    assert t['id'] == p['parent_task'] and t['status'] == 'in_progress'
    assert t['owner_session'] == p['owner_session']
    assert t['contract_sha256'] == contract_hash(t)
    assert p['day'] == '2026-09-28'
    idx = {a['path']: a['sha256'] for a in t['artifacts']}
    assert idx[P.relative_to(ROOT).as_posix()] == sha(P)
    for f, h in p['input_hashes'].items():
        assert sha(ROOT / f) == h
    for ref in p['source_refs'].values():
        f = Path(ref['path'])
        assert not f.is_absolute() and (ROOT / f).resolve().is_relative_to(ROOT)
        assert sha(ROOT / f) == ref['sha256']
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    return t


def run(p):
    t = gate(p)
    # These preflight hashes describe inputs verified before the one-shot marker.
    # Never re-read a missing/changed input while persisting its failure terminal.
    protocol_hash, implementation_hash = sha(P), sha(__file__)
    out = Path(p['native_directory']) / 'calculation'
    # Exclusive output folder is also the one-shot marker before reading rows.
    out.mkdir(exist_ok=False)
    write(out / 'protocol.json', p, exclusive=True)
    write(out / 'frozen_task.json', t, exclusive=True)
    def audit(event, args):
        if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):
            raise PermissionError('local_replay_network_or_spawn_denied')
    sys.addaudithook(audit)
    r = {'schema': 'regis.retained_crosssection_feature_result.v1',
         'started_utc': clock(), 'failure_type': None, 'raw_values_exported': False,
         'fresh_source_queries': 0, 'economic_runs': 0, 'new_formula_variants': 0,
         'current_signal': False, 'generates_alert': False, 'model_admitted': False,
         'full_goal_complete': False, 'source_available_at': 'unknown_not_backfilled'}
    phase = 'source_validation_and_feature_computation'
    try:
        inputs = {k: read(ROOT / v['path']) for k, v in p['source_refs'].items()}
        features, aggregate = derive(p['day'], inputs['stock_basic'], inputs['all_stock'], inputs['daily'])
        r['aggregate'] = aggregate
        f = out / 'registered_negative_return_feature.json'
        write(f, {'schema': 'regis.registered_feature_input.v1', 'day': p['day'],
                  'computed_at_utc': clock(), 'source_refs': p['source_refs'],
                  'rows': features, 'stale_for_current_signals': True,
                  'full_M24_selector_ready': False, 'production_eligible': False,
                  'generates_alert': False}, exclusive=True)
        r['feature_table'] = {'native_path': str(f), 'sha256': sha(f), 'rows': len(features)}
        phase = 'post_computation_input_binding'
        gate(p)
        r['state'] = 'retained_registered_feature_computed_research_only'
    except Exception as exc:
        r['state'] = 'retained_computation_failure_preserved'
        r['failure_type'] = type(exc).__name__
        r['failure_phase'] = phase
        if phase == 'post_computation_input_binding':
            r['feature_table_invalidated_by_input_binding_failure'] = True
        if isinstance(exc, ValueError):
            r['validation_label'] = str(exc)[:180]
    r.update(ended_utc=clock(), protocol_sha256=protocol_hash,
             implementation_sha256=implementation_hash,
             hash_clock_role='verified_before_computation_marker',
             contract_sha256=t['contract_sha256'], source_refs=p['source_refs'])
    write(O, r)
    write(out / 'terminal.json', r, exclusive=True)
    print(json.dumps(r))
    return 0 if not r['failure_type'] else 1


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--validate-plan', action='store_true')
    args = ap.parse_args(); p = read(P)
    if args.validate_plan:
        gate(p); print(json.dumps({'frozen_scope_checked': True, 'source_queries': 0}))
    else:
        raise SystemExit(run(p))
