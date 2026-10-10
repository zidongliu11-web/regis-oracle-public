"""One fixed Oct8 five-minute public source query; retained daily reference."""
from pathlib import Path
import argparse, json, socket, subprocess, sys
ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from research.capture_completed_daily_pair import read, write, sha, clock, first_response, claim_child_dispatch, completed_cutoff
from research.compute_retained_current_lookback import read_verified_source
from research.current_minute_source import analyse, FIELDS, CODE, DATE
P = ROOT / 'research/CURRENT_MINUTE_SOURCE_PROTOCOL_20261009.json'
O = ROOT / 'research/CURRENT_MINUTE_SOURCE_RESULT_20261009.json'


def gate(p):
    from scripts.workflow_guard import contract_hash
    t = read(ROOT / 'WORKFLOW_TASK.json')
    idx = {a['path']: a['sha256'] for a in t['artifacts']}
    assert t['id'] == p['parent_task'] and t['owner_session'] == p['owner_session'] and t['status'] == 'in_progress'
    assert t['contract_sha256'] == contract_hash(t) and idx[P.relative_to(ROOT).as_posix()] == sha(P)
    assert p['code'] == CODE and p['date'] == DATE and p['frequency'] == '5' and p['adjustflag'] == '3'
    assert p['fields'] == FIELDS
    for f, h in p['input_hashes'].items(): assert sha(ROOT / f) == h
    for f, h in p['native_sdk_hashes'].items(): assert sha(f) == h
    for ref in [p['daily_reference'], p['primary_document']]:
        assert sha(ref['path']) == ref['sha256'] and Path(ref['path']).stat().st_size == ref['bytes']
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    return t


def reference(p):
    j = read_verified_source(p['daily_reference'])
    stock = j['observations']['stock']
    if not isinstance(stock, dict) or set(stock) != {'rows', 'observed_at_utc'} or not isinstance(stock['rows'], list):
        raise ValueError('fixed_daily_reference_container')
    matches = [r for r in stock['rows'] if r['date'] == DATE and r['code'] == CODE]
    if len(matches) != 1: raise ValueError('fixed_daily_reference_unique_row')
    return matches[0]


def child(p):
    gate(p)
    # Verify the exact retained bytes before the sole source dispatch marker.
    daily = reference(p)
    completed_cutoff(clock(), DATE)
    claim_child_dispatch(p)
    socket.setdefaulttimeout(8)
    def audit(event, args):
        if event == 'socket.connect' and args[1] != ('public-api.baostock.com', 10030): raise PermissionError('unexpected_target')
        if event in ('subprocess.Popen', 'os.system'): raise PermissionError('child_spawn')
    sys.addaudithook(audit)
    sys.path.insert(0, p['sdk_directory'])
    import baostock as bs
    import baostock.common.context as context
    import baostock.common.contants as constants
    out = Path(p['native_directory']) / 'capture'; path = out / 'journal.json'
    j = {'started_utc': clock(), 'guest_handshake_attempts': 0, 'semantic_query_attempts': 0,
         'next_calls': 0, 'logout_attempts': 0, 'stage': 'before_guest', 'failure_type': None}
    try:
        j['guest_handshake_attempts'] = 1; write(path, j)
        lg = bs.login(); j['handshake_error_code'] = str(lg.error_code); write(path, j)
        if str(lg.error_code) != '0': raise ValueError('anonymous_guest_handshake_failed')
        j.update(stage='fixed_symbol_five_minute', semantic_query_attempts=1, sent_at_utc=clock()); write(path, j)
        rs = bs.query_history_k_data_plus(CODE, ','.join(FIELDS), start_date=DATE, end_date=DATE, frequency='5', adjustflag='3')
        j.update(returned_at_utc=clock(), error_code=str(rs.error_code), fields=list(rs.fields),
                 cached_rows=len(rs.data) if isinstance(rs.data, list) else None); write(path, j)
        rows = first_response(rs, FIELDS, 48)
        payload = {'schema': 'regis.current_minute_raw.v1', 'fields': FIELDS, 'rows': rows,
                   'observed_at_utc': j['returned_at_utc'], 'client_identifier': str(constants.BAOSTOCK_CLIENT_VERSION),
                   'SDK_distribution_version': p['SDK_distribution_version'], 'code': CODE, 'date': DATE,
                   'frequency': '5', 'adjustflag': '3'}
        f = out / 'source.json'; write(f, payload, exclusive=True)
        j['source_ref'] = {'path': str(f), 'sha256': sha(f)}; write(path, j)
        j['summary'] = analyse(rows, daily); j['stage'] = 'source_shape_and_daily_comparison_recorded'
        if not j['summary']['same_source_daily_agreement']: raise ValueError('minute_daily_disagreement')
    except Exception as exc:
        j['failure_type'] = type(exc).__name__
        if isinstance(exc, ValueError): j['validation_label'] = str(exc)[:180]
    finally:
        if j.get('handshake_error_code') == '0':
            j['logout_attempts'] = 1; write(path, j)
            try: j['logout_error_code'] = str(bs.logout().error_code)
            except Exception as exc: j['logout_failure_type'] = type(exc).__name__
        if hasattr(context, 'default_socket'):
            try: context.default_socket.close()
            except Exception: pass
        j['ended_utc'] = clock(); write(path, j)
    return 0 if j['failure_type'] is None else 1


def run(p):
    t = gate(p); protocol_hash, implementation_hash = sha(P), sha(__file__)
    out = Path(p['native_directory']) / 'capture'; out.mkdir(exist_ok=False)
    write(out / 'protocol.json', p, exclusive=True); write(out / 'frozen_task.json', t, exclusive=True)
    r = {'schema': 'regis.current_minute_result.v1', 'started_utc': clock(), 'child_returncode': None,
         'child_timed_out': False, 'failure_type': None, 'fresh_source_dispatches': 1, 'retries': 0,
         'economic_runs': 0, 'new_formulas': 0, 'raw_market_rows_exported': False, 'model_admitted': False,
         'full_goal_complete': False, 'historical_publication_PIT_verified': False, 'fills_verified': False}
    phase = 'single_child_source_capture'
    try:
        q = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(Path(__file__).resolve()), '--child'],
                           cwd=out, capture_output=True, timeout=55, creationflags=0x08000000 if sys.platform == 'win32' else 0)
        r['child_returncode'] = q.returncode
        (out / 'stdout.log').write_bytes(q.stdout); (out / 'stderr.log').write_bytes(q.stderr)
        j = read(out / 'journal.json'); r['diagnostics'] = j; r['failure_type'] = j.get('failure_type')
        if q.returncode != 0 or r['failure_type']: raise ValueError('current_minute_source_not_passed')
        phase = 'post_source_binding'; gate(p)
        r['state'] = 'fixed_current_minute_source_daily_consistent'
    except subprocess.TimeoutExpired as exc:
        r.update(child_timed_out=True, failure_type='TimeoutExpired', failure_phase=phase)
        (out / 'stdout.log').write_bytes(exc.stdout or b''); (out / 'stderr.log').write_bytes(exc.stderr or b'')
    except Exception as exc:
        r['failure_type'] = r['failure_type'] or type(exc).__name__; r['failure_phase'] = phase
        if phase == 'post_source_binding': r['native_source_invalidated_by_binding_failure'] = True
    if 'diagnostics' not in r:
        try: r['diagnostics'] = read(out / 'journal.json')
        except (OSError, ValueError): r['diagnostics'] = {'partial_journal_unavailable': True}
    if r['failure_type']: r['state'] = 'current_minute_source_failure_preserved'
    r.update(ended_utc=clock(), protocol_sha256=protocol_hash, implementation_sha256=implementation_hash,
             contract_sha256=t['contract_sha256'], source_clock_role='actual_receipt_not_historical_PIT',
             source_identifier_role='SDK_client_protocol_identifier_not_server_release',
             SDK_internal_wire_bytes_or_retry_counts='unknown', full_A_verified=False, orders_enabled=False)
    write(O, r); write(out / 'terminal.json', r, exclusive=True); print(json.dumps(r))
    return 0 if r['failure_type'] is None else 1


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__); g = ap.add_mutually_exclusive_group()
    g.add_argument('--validate-plan', action='store_true'); g.add_argument('--child', action='store_true')
    args = ap.parse_args(); p = read(P)
    if args.validate_plan: gate(p); reference(p); print(json.dumps({'frozen_scope_checked': True, 'source_calls': 0}))
    else: raise SystemExit(child(p) if args.child else run(p))
