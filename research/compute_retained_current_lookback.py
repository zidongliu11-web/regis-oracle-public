"""One frozen local current lookback calculation; no source request or orders."""
from pathlib import Path
from datetime import datetime, timezone
import argparse, ast, hashlib, json, sys
ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from research.capture_completed_daily_pair import read, write, sha, clock
from research.retained_current_lookback import lookback
P = ROOT / 'research/RETAINED_CURRENT_LOOKBACK_PROTOCOL_20261008.json'
O = ROOT / 'research/RETAINED_CURRENT_LOOKBACK_RESULT_20261008.json'


def read_verified_source(ref):
    # Parse precisely the bounded buffer whose identity was verified. A pair
    # of independent path hashes cannot identify intervening parsed bytes.
    with Path(ref['path']).open('rb') as stream:
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024 or len(data) != ref['bytes'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
        raise ValueError('source_parsed_bytes_binding')
    return json.loads(data.decode('utf-8-sig'))


def gate(p):
    from scripts.workflow_guard import contract_hash
    t = read(ROOT / 'WORKFLOW_TASK.json')
    assert t['id'] == p['parent_task'] and t['owner_session'] == p['owner_session']
    assert t['status'] == 'in_progress' and t['contract_sha256'] == contract_hash(t)
    idx = {a['path']: a['sha256'] for a in t['artifacts']}
    assert idx[P.relative_to(ROOT).as_posix()] == sha(P)
    assert p['signal_day'] == '2026-10-08'
    assert p.get('calculation_subdirectory', 'calculation') in ('calculation', 'calculation_schema_repair')
    for f, h in p['input_hashes'].items():
        assert sha(ROOT / f) == h
    expected = {
        'stock_history': ROOT / 'reports/local_closure/m10_history_raw/sh_600000_622660a4cfc6aad2080e003f7ce8c8d655382a465e70e28c89156cf39a159b9b.json',
        'calendar_history': ROOT / 'reports/local_closure/m11_calendar_etf_raw/calendar_2a79457e14d732f4a840f15823d856dd08f91ee1daf73dc5ed9bb1e38cc65a3f.json',
        'current_source_journal': Path(p['native_directory']).parent / 'E176-completed-session-capture-20261008/capture/journal.json'}
    assert set(p['source_refs']) == set(expected)
    for role, ref in p['source_refs'].items():
        assert Path(ref['path']).resolve() == expected[role].resolve()
        assert sha(expected[role]) == ref['sha256']
    e176 = read(ROOT / 'research/COMPLETED_DAILY_CAPTURE_PROTOCOL_20261008.json')
    tag_source = Path(e176['sdk_directory']) / 'baostock/common/contants.py'
    assert str(tag_source) == p['current_identifier_source']['path']
    assert sha(tag_source) == e176['native_sdk_hashes'][str(tag_source)] == p['current_identifier_source']['sha256']
    assignments = [ast.literal_eval(node.value) for node in ast.parse(tag_source.read_text(encoding='utf-8')).body
                   if isinstance(node, ast.Assign) and any(isinstance(v, ast.Name) and v.id == 'BAOSTOCK_CLIENT_VERSION' for v in node.targets)]
    assert assignments == [p['current_client_identifier_from_bound_sdk']]
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    return t


def run(p):
    t = gate(p)
    protocol_hash, implementation_hash = sha(P), sha(__file__)
    out = Path(p['native_directory']) / p.get('calculation_subdirectory', 'calculation')
    out.mkdir(exist_ok=False)
    write(out / 'protocol.json', p, exclusive=True)
    write(out / 'frozen_task.json', t, exclusive=True)
    def audit(event, args):
        if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):
            raise PermissionError('local_lookback_network_or_spawn_denied')
    sys.addaudithook(audit)
    r = {'schema': 'regis.retained_current_lookback_result.v1', 'started_utc': clock(),
         'failure_type': None, 'failure_phase': None, 'raw_values_exported': False,
         'fresh_source_queries': 0, 'economic_runs': 0, 'new_formula_variants': 0,
         'current_signal': False, 'generates_alert': False, 'model_admitted': False,
         'full_goal_complete': False}
    phase = 'source_clock_and_window_validation'
    try:
        objects = {k: read_verified_source(v) for k, v in p['source_refs'].items()}
        old, calendar = objects['stock_history'], objects['calendar_history']
        current = objects['current_source_journal']['observations']
        source_clocks = {'stock_history': old['observed_at_utc'],
                         'calendar_history': calendar['observed_at_utc'],
                         'current_stock': current['stock']['observed_at_utc'],
                         'current_calendar': current['calendar']['observed_at_utc']}
        parsed = [datetime.fromisoformat(v) for v in source_clocks.values()]
        if any(v.tzinfo is None or v.utcoffset() is None for v in parsed):
            raise ValueError('source_receipt_timezone_missing')
        available = max(parsed).astimezone(timezone.utc)
        if available > datetime.fromisoformat(r['started_utc']):
            raise ValueError('source_receipt_after_actual_computation')
        native, aggregate = lookback(p['signal_day'], old, current, calendar)
        computed = clock()
        native.update(schema='regis.original_M24_lookback_source_input.v1',
                      source_refs=p['source_refs'], source_receipt_clocks=source_clocks,
                      source_tags={'history': old.get('client_version'),
                                   'current': p['current_client_identifier_from_bound_sdk']},
                      available_at=computed, computed_at_utc=computed,
                      source_receipts_known_at=available.isoformat(),
                      available_at_role='actual_feature_computation_not_backfilled_source_receipt',
                      current_identifier_provenance=p['current_identifier_source'],
                      historical_publication_clocks='unknown_not_backfilled',
                      missing_selector_inputs=p['missing_selector_inputs'])
        f = out / 'original_amount_lookback_source_input.json'
        write(f, native, exclusive=True)
        r.update(aggregate=aggregate, source_receipt_clocks=source_clocks,
                 available_at=computed, source_receipts_known_at=available.isoformat(),
                 available_at_role='actual_feature_computation_not_backfilled_source_receipt',
                 native_input={'path': str(f), 'sha256': sha(f), 'window_rows': 21},
                 source_tag_role='SDK_protocol_client_identifiers_not_verified_server_releases')
        phase = 'post_computation_input_binding'
        gate(p)
        r['state'] = 'original_current_lookback_computed_source_facts_only'
    except Exception as exc:
        r.update(state='retained_lookback_failure_preserved', failure_type=type(exc).__name__,
                 failure_phase=phase)
        if isinstance(exc, ValueError):
            r['validation_label'] = str(exc)[:180]
        if phase == 'post_computation_input_binding':
            r['native_input_invalidated_by_binding_failure'] = True
    r.update(ended_utc=clock(), protocol_sha256=protocol_hash,
             implementation_sha256=implementation_hash,
             hash_clock_role='verified_before_computation_marker',
             contract_sha256=t['contract_sha256'], source_refs=p['source_refs'])
    write(O, r)
    write(out / 'terminal.json', r, exclusive=True)
    print(json.dumps(r))
    return 0 if r['failure_type'] is None else 1


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--validate-plan', action='store_true')
    args = ap.parse_args(); p = read(P)
    if args.validate_plan:
        gate(p); print(json.dumps({'frozen_scope_checked': True, 'source_queries': 0}))
    else:
        raise SystemExit(run(p))
