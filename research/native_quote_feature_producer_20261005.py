"""Produce frozen descriptive vectors in native storage; no economic admission."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib, importlib.util, json, os, stat, sys, time

R = Path.cwd()
N = Path(__file__).resolve().parent
P = R / 'research/QUOTE_FEATURE_PRODUCTION_PROTOCOL_20261005.json'
sha = lambda b: hashlib.sha256(b).hexdigest()
p = json.loads(P.read_bytes())
assert sha(Path(__file__).read_bytes()) == p['helper_sha256']
for info in p['native_dependencies'].values():
    assert sha(Path(info['path']).read_bytes()) == info['sha256']
for x, h in p['source_hashes'].items():
    assert sha((R / x).read_bytes()) == h
spec = importlib.util.spec_from_file_location('frozen_quote_input_checks', p['native_dependencies']['opening_role']['path'])
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
qc, OLD = old.qc, old.OLD
sys.path.insert(0, str(R))
from factor_system.quote_state_features import quote_state_features
J = N / 'quote_feature_result.json'
utc = lambda: datetime.now(timezone.utc).isoformat()
FEATURES = ('quoted_spread_fraction', 'best_depth_imbalance', 'five_depth_imbalance')
MASKS = ('spread_valid', 'best_imbalance_valid', 'five_imbalance_valid')


def exclusive_npy_output(path, dtype, count, np):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    raw = os.fdopen(fd, 'w+b')
    try:
        identity = os.fstat(raw.fileno())
        assert stat.S_ISREG(identity.st_mode) and identity.st_nlink == 1
        assert qc.reader._path_identity(identity) == qc.reader._path_identity(os.lstat(path))
        assert not dtype.hasobject and count > 0
        np.lib.format.write_array_header_1_0(raw, {'descr': np.lib.format.dtype_to_descr(dtype),
            'fortran_order': False, 'shape': (count,)})
        offset = raw.tell()
        raw.truncate(offset + count * dtype.itemsize)
        raw.flush()
        output = np.memmap(raw, mode='r+', dtype=dtype, offset=offset, shape=(count,))
        return raw, output, identity
    except BaseException:
        raw.close()
        raise


def produce():
    qc.reader._native_root(N)
    qc.reader._native_root(OLD)
    assert not os.path.lexists(J) and not os.path.lexists(N / 'quote_feature_result.failure_fallback.json')
    r = {'state': 'started', 'started_utc': utc(), 'protocol_sha256': sha(P.read_bytes()),
         'helper_sha256': sha(Path(__file__).read_bytes()), 'quote_rows_projected': 0,
         'GET_invocations': 0, 'raw_values_to_AI_Git': 0, 'economic_runs': 0,
         'source_prices_changed': False, 'source_or_strategy_admitted': False,
         'files': {}, 'resources': {'elapsed_seconds': 0, 'peak_rss_bytes': None,
                                   'decompressed_bytes': 0, 'derived_output_bytes': 0}}
    budget = qc.base.Budget(p, r)
    try:
        qc.reader.prior.atomic(J, r)
        prior = p.get('prior_actual_attempt')
        if prior:
            assert prior['filename'] == 'quote_feature_result.first_failed_before_rows.json'
            prior_raw = (N / prior['filename']).read_bytes()
            assert sha(prior_raw) == prior['sha256']
            first = json.loads(prior_raw)
            assert first['state'] == 'failed' and first['quote_rows_projected'] == 0
            assert first['resources']['derived_output_bytes'] == 0 and first['files'] == {}
        h5py, np = qc.base.load_dependencies()
        raw = (OLD / 'extraction_metadata_result.json').read_bytes()
        assert sha(raw) == p['metadata_sha256']
        m = json.loads(raw)
        assert qc.base.digest(OLD / 'row_quality_result.json') == p['prior_QC_sha256']
        dtype = np.dtype([tuple(x) for x in p['output_dtype_descr']])
        assert not dtype.hasobject and dtype.itemsize == 35
        for name, info in m['selected_files'].items():
            if info['kind'] != 'tick':
                continue
            instrument = info['instrument']
            assert instrument in p['expected_quote_rows']
            expected = p['expected_quote_rows'][instrument]
            entries = [x for x in m['file_metadata'][name]['objects'] if x['type'] == 'dataset']
            assert sum(x['rows'] for x in entries) == expected
            partial = N / (instrument + '.quote_features.npy.partial')
            final = N / (instrument + '.quote_features.npy')
            assert not os.path.lexists(partial) and not os.path.lexists(final)
            qc.reader._native_root(N)
            output_raw, output, output_identity = exclusive_npy_output(partial, dtype, expected, np)
            f = {'source_path': info['path'], 'source_sha256': info['sha256'], 'rows': 0,
                 'sessions': len(entries), 'valid_counts': {x: 0 for x in MASKS},
                 'partial_output_retained_on_failure': True, 'raw_book_prices_saved': False}
            r['files'][instrument] = f
            cursor = 0
            def project(raw_input):
                nonlocal cursor
                with h5py.File(raw_input, 'r', rdcc_nbytes=1048576) as file:
                    for entry in entries:
                        day = int(entry['path'].lstrip('/'))
                        ds = qc.validate_dataset(file, entry, h5py, np)
                        previous_clock = None
                        for start in range(0, entry['rows'], p['limits']['projection_rows_per_chunk']):
                            budget.check('quote_feature_projection')
                            records = ds[start:start+p['limits']['projection_rows_per_chunk']]
                            assert (records['date'] == day).all()
                            clocks = records['time'].astype('int64')
                            assert not (np.diff(clocks) < 0).any()
                            assert previous_clock is None or int(clocks[0]) >= previous_clock
                            previous_clock = int(clocks[-1])
                            values = quote_state_features(
                                np.column_stack([records['a'+str(i)] for i in range(1, 6)]),
                                np.column_stack([records['b'+str(i)] for i in range(1, 6)]),
                                np.column_stack([records['a'+str(i)+'_v'] for i in range(1, 6)]),
                                np.column_stack([records['b'+str(i)+'_v'] for i in range(1, 6)]))
                            end = cursor + len(records)
                            assert end <= expected
                            output['date'][cursor:end] = records['date']
                            output['time'][cursor:end] = records['time']
                            for key in FEATURES + MASKS:
                                output[key][cursor:end] = values[key]
                            for key in MASKS:
                                f['valid_counts'][key] += int(values[key].sum())
                            cursor = end
                            f['rows'] += len(records)
                            r['quote_rows_projected'] += len(records)
                            assert r['quote_rows_projected'] <= p['limits']['quote_rows_projected']
            try:
                old.checked_input(Path(info['path']), info['sha256'], budget, project)
                assert cursor == expected
                output.flush()
                output_raw.flush()
                os.fsync(output_raw.fileno())
                assert os.fstat(output_raw.fileno()).st_nlink == 1
                assert qc.reader._path_identity(os.fstat(output_raw.fileno())) == qc.reader._path_identity(os.lstat(partial))
                output_raw.seek(0)
                output_hash = qc.reader.hash_stream(output_raw, budget)
                output_identity = os.fstat(output_raw.fileno())
            finally:
                try:
                    output._mmap.close()
                finally:
                    output_raw.close()
            budget.check('derived_output')
            assert partial.stat().st_nlink == 1 and not os.path.lexists(final)
            partial.rename(final)
            qc.reader._no_links(N, final.relative_to(N))
            assert final.stat().st_nlink == 1
            assert qc.reader._path_identity(os.lstat(final)) == qc.reader._path_identity(output_identity)
            assert qc.base.digest(final) == output_hash
            f.update(output_path=str(final), output_sha256=output_hash,
                     output_bytes=final.stat().st_size, partial_output_retained_on_failure=False)
            r['resources']['derived_output_bytes'] += f['output_bytes']
            assert r['resources']['derived_output_bytes'] <= p['limits']['derived_output_bytes']
        assert len(r['files']) == 4 and r['quote_rows_projected'] == p['limits']['quote_rows_projected']
        r['state'] = 'native_quote_feature_vectors_produced_not_factor_or_strategy_admitted'
    except Exception as e:
        r.update(state='failed', failure_type=type(e).__name__, no_automatic_retry=True)
    finally:
        r['ended_utc'] = utc()
        r['resources']['elapsed_seconds'] = round(time.monotonic() - budget.started, 3)
        try:
            r['resources']['peak_rss_bytes'] = qc.base.peak_rss()
        except Exception as e:
            r.update(state='failed', resource_probe_failure_type=type(e).__name__)
            r['resources']['peak_rss_bytes'] = None
        finally:
            persisted = qc.reader.prior.terminal(r, J)
    print(json.dumps({k: r[k] for k in ('state', 'quote_rows_projected', 'raw_values_to_AI_Git', 'source_or_strategy_admitted')}))
    if not persisted or r['state'] != 'native_quote_feature_vectors_produced_not_factor_or_strategy_admitted':
        raise SystemExit(1)


def verify():
    qc.reader._native_root(N)
    r = json.loads(J.read_bytes())
    assert r['state'] == 'native_quote_feature_vectors_produced_not_factor_or_strategy_admitted'
    assert r['protocol_sha256'] == sha(P.read_bytes()) and r['helper_sha256'] == p['helper_sha256']
    assert r['quote_rows_projected'] == p['limits']['quote_rows_projected']
    assert r['raw_values_to_AI_Git'] == r['GET_invocations'] == r['economic_runs'] == 0
    assert not r['source_prices_changed'] and not r['source_or_strategy_admitted']
    if p.get('prior_actual_attempt'):
        prior = p['prior_actual_attempt']
        assert prior['filename'] == 'quote_feature_result.first_failed_before_rows.json'
        assert sha((N / prior['filename']).read_bytes()) == prior['sha256']
    for code, f in r['files'].items():
        assert f['rows'] == p['expected_quote_rows'][code]
        for key in MASKS:
            assert 0 <= f['valid_counts'][key] <= f['rows']
        for key, root in [('source', OLD), ('output', N)]:
            path = Path(f[key+'_path'])
            qc.reader._no_links(root, path.relative_to(root))
            assert path.stat().st_nlink == 1 and qc.base.digest(path) == f[key+'_sha256']
        assert Path(f['output_path']).stat().st_size == f['output_bytes']
    assert r['resources']['derived_output_bytes'] == sum(f['output_bytes'] for f in r['files'].values())
    for info in p['native_dependencies'].values():
        assert sha(Path(info['path']).read_bytes()) == info['sha256']
    for x, h in p['source_hashes'].items():
        assert sha((R / x).read_bytes()) == h
    print(json.dumps({'actual_quote_feature_outputs_verified': True, 'quote_rows': r['quote_rows_projected'],
                      'output_files': 4, 'raw_values_to_AI_Git': 0, 'source_or_strategy_admitted': False}))


if __name__ == '__main__':
    if sys.argv[1:] not in (['--produce'], ['--verify']):
        raise SystemExit('explicit produce or verify only')
    produce() if sys.argv[1] == '--produce' else verify()
