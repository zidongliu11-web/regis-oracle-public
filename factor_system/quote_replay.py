"""Field-specific native quote inputs at explicit research decision clocks.

Captured exchange clocks plus an assumed lag are not certified publication times.
This input-only lane has no OHLC dependency, labels, alpha trial or order routing.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys
import time
from types import CodeType, FunctionType
import numpy as np
from .native_snapshot import _native_root, _no_links, _identity, _path_identity
from . import native_snapshot as native_source
from research.inspect_short_rq_full import peak_rss

FEATURES = ('quoted_spread_fraction', 'best_depth_imbalance', 'five_depth_imbalance')
MASKS = ('spread_valid', 'best_imbalance_valid', 'five_imbalance_valid')
INPUT_DTYPE = np.dtype([('date', '<u4'), ('time', '<u4')]
    + [(x, '<f8') for x in FEATURES] + [(x, '?') for x in MASKS])
OUTPUT_DTYPE = np.dtype([('date', '<u4'), ('decision_clock', '<u4'),
    ('source_clock', '<u4'), ('source_rows', '<u4'), ('age_ms', '<i4')]
    + [(x, '<f8') for x in FEATURES] + [(x, '?') for x in MASKS]
    + [(x + '_ambiguous', '?') for x in FEATURES])
_PATH = Path(__file__).resolve()
_IMPORTED_SHA = hashlib.sha256(_PATH.read_bytes()).hexdigest()
_PROBE_PATH = Path(peak_rss.__code__.co_filename).resolve()
_IMPORTED_PROBE_SHA = hashlib.sha256(_PROBE_PATH.read_bytes()).hexdigest()
_NATIVE_PATH = Path(native_source.__file__).resolve()
_IMPORTED_NATIVE_SHA = hashlib.sha256(_NATIVE_PATH.read_bytes()).hexdigest()
_IMPORTED_CONSTANTS = (FEATURES, MASKS, INPUT_DTYPE.descr, OUTPUT_DTYPE.descr, np.__version__)


class QuoteInputError(ValueError):
    pass


def _binding():
    from .research_batch import _code_hash
    raw = _PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != _IMPORTED_SHA or _IMPORTED_CONSTANTS != (
            FEATURES, MASKS, INPUT_DTYPE.descr, OUTPUT_DTYPE.descr, np.__version__):
        raise QuoteInputError('quote_input_source_drift')
    probe_raw = _PROBE_PATH.read_bytes()
    if hashlib.sha256(probe_raw).hexdigest() != _IMPORTED_PROBE_SHA:
        raise QuoteInputError('quote_input_memory_probe_source_drift')
    native_raw = _NATIVE_PATH.read_bytes()
    if hashlib.sha256(native_raw).hexdigest() != _IMPORTED_NATIVE_SHA:
        raise QuoteInputError('quote_input_native_safety_source_drift')
    pending = [compile(b, str(path), 'exec', dont_inherit=True, optimize=sys.flags.optimize)
        for b, path in ((raw, _PATH), (probe_raw, _PROBE_PATH), (native_raw, _NATIVE_PATH))]
    compiled = {}
    while pending:
        code = pending.pop()
        compiled.setdefault(code.co_qualname, set()).add(_code_hash(code))
        pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
    live = {}
    for name in _FUNCTION_NAMES:
        fn = globals().get(name)
        if (not isinstance(fn, FunctionType) or fn.__module__ != __name__
                or fn.__qualname__ != name):
            raise QuoteInputError('quote_input_loaded_code_mismatch')
        current = _code_hash(fn.__code__)
        if current not in compiled.get(name, set()):
            raise QuoteInputError('quote_input_loaded_code_mismatch')
        live[name] = current
    if (not isinstance(peak_rss, FunctionType) or peak_rss.__qualname__ != 'peak_rss'
            or _code_hash(peak_rss.__code__) not in compiled.get('peak_rss', set())):
        raise QuoteInputError('quote_input_memory_probe_loaded_code_mismatch')
    for name in ('_native_root', '_no_links', '_identity', '_path_identity'):
        fn = globals()[name]
        if (not isinstance(fn, FunctionType) or fn is not getattr(native_source, name)
                or fn.__module__ != native_source.__name__ or fn.__qualname__ != name
                or _code_hash(fn.__code__) not in compiled.get(name, set())):
            raise QuoteInputError('quote_input_native_safety_loaded_code_mismatch')
        live['native:' + name] = _code_hash(fn.__code__)
    return {'sha256': digest, 'live': live, 'numpy': np.__version__,
            'memory_probe_sha256': _IMPORTED_PROBE_SHA, 'native_safety_sha256': _IMPORTED_NATIVE_SHA}


def clock_ms(clock):
    if type(clock) is not int or clock < 0:
        raise QuoteInputError('quote_input_clock_invalid')
    h, m, s, ms = clock // 10000000, clock // 100000 % 100, clock // 1000 % 100, clock % 1000
    if h > 23 or m > 59 or s > 59:
        raise QuoteInputError('quote_input_clock_invalid')
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def _date(day):
    if type(day) is not int:
        raise QuoteInputError('quote_input_date_invalid')
    try:
        date(day // 10000, day // 100 % 100, day % 100)
    except ValueError as exc:
        raise QuoteInputError('quote_input_date_invalid') from exc
    return day


def _settings(sessions, clocks, lag_ms, max_age_ms):
    if (not isinstance(sessions, list) or not 1 <= len(sessions) <= 300
            or sessions != sorted(set(sessions))):
        raise QuoteInputError('quote_input_sessions_invalid')
    for x in sessions: _date(x)
    if (not isinstance(clocks, list) or not 1 <= len(clocks) <= 4
            or clocks != sorted(set(clocks))):
        raise QuoteInputError('quote_input_decisions_invalid')
    decision_ms = [clock_ms(x) for x in clocks]
    if (type(lag_ms) is not int or type(max_age_ms) is not int
            or not 0 <= lag_ms <= max_age_ms <= 60000
            or min(decision_ms) < lag_ms):
        raise QuoteInputError('quote_input_visibility_assumption_invalid')
    return np.array(decision_ms, dtype='int64')


def decision_inputs(chunks, sessions, clocks, lag_ms, max_age_ms):
    """Consume ordered chunks; latest clock group must agree per field.

    No fallback to older valid fields, overnight carry, fill or future lookup.
    Equal-clock rows are retained in source_rows, not arbitrarily ordered.
    All chunks must exhaust successfully before the returned input is publishable.
    """
    decisions = _settings(sessions, clocks, lag_ms, max_age_ms)
    positions = {day: i for i, day in enumerate(sessions)}
    selected_ms = np.full((len(sessions), len(clocks)), -1, dtype='int64')
    counts = np.zeros_like(selected_ms)
    values = np.full(selected_ms.shape + (3,), np.nan)
    valid = np.zeros(values.shape, dtype=bool)
    ambiguous = np.zeros(values.shape, dtype=bool)
    source_clocks = np.zeros(selected_ms.shape, dtype='uint32')
    last = None
    consumed = duplicates = 0
    seen_days = set()
    for rows in chunks:
        if not isinstance(rows, np.ndarray) or rows.ndim != 1 or rows.dtype != INPUT_DTYPE:
            raise QuoteInputError('quote_input_dtype_invalid')
        if len(rows) == 0: continue
        days = rows['date'].astype('int64')
        raw_clocks = rows['time'].astype('int64')
        h = raw_clocks // 10000000
        m = raw_clocks // 100000 % 100
        s = raw_clocks // 1000 % 100
        if ((h > 23) | (m > 59) | (s > 59)).any():
            raise QuoteInputError('quote_input_clock_invalid')
        elapsed = ((h * 60 + m) * 60 + s) * 1000 + raw_clocks % 1000
        keys = days * 1000000000 + raw_clocks
        if (np.diff(keys) < 0).any() or last is not None and int(keys[0]) < last:
            raise QuoteInputError('quote_input_clock_backwards')
        duplicates += int((np.diff(keys) == 0).sum()) + int(last == int(keys[0]))
        last = int(keys[-1])
        consumed += len(rows)
        for feature, mask in zip(FEATURES, MASKS):
            numeric = rows[feature]
            if (rows[mask] & ~np.isfinite(numeric)).any() or (~rows[mask] & ~np.isnan(numeric)).any():
                raise QuoteInputError('quote_input_mask_value_inconsistent')
            if feature == FEATURES[0]:
                if (rows[mask] & ((numeric < 0) | (numeric > 2))).any():
                    raise QuoteInputError('quote_input_spread_range_invalid')
            elif (rows[mask] & (np.abs(numeric) > 1)).any():
                raise QuoteInputError('quote_input_imbalance_range_invalid')
        boundaries = np.r_[0, np.flatnonzero(np.diff(days)) + 1, len(rows)]
        for lo, hi in zip(boundaries[:-1], boundaries[1:]):
            day = int(days[lo])
            if day not in positions: raise QuoteInputError('quote_input_unexpected_session')
            seen_days.add(day)
            i = positions[day]
            part, times = rows[lo:hi], elapsed[lo:hi]
            for j, cutoff in enumerate(decisions - lag_ms):
                end = int(np.searchsorted(times, cutoff, side='right'))
                if not end: continue
                latest = int(times[end - 1])
                if latest < selected_ms[i, j]: continue
                start = int(np.searchsorted(times, latest, side='left'))
                group = part[start:end]
                if latest > selected_ms[i, j]:
                    selected_ms[i, j] = latest
                    source_clocks[i, j] = group['time'][0]
                    counts[i, j] = 0
                    ambiguous[i, j] = False
                    for k, (feature, mask) in enumerate(zip(FEATURES, MASKS)):
                        values[i, j, k], valid[i, j, k] = group[feature][0], group[mask][0]
                counts[i, j] += len(group)
                for k, (feature, mask) in enumerate(zip(FEATURES, MASKS)):
                    same_mask = group[mask] == valid[i, j, k]
                    same_value = ((group[feature] == values[i, j, k])
                                  | (np.isnan(group[feature]) & np.isnan(values[i, j, k])))
                    ambiguous[i, j, k] |= not bool((same_mask & same_value).all())
    if seen_days != set(sessions):
        raise QuoteInputError('quote_input_missing_session')
    out = np.zeros(len(sessions) * len(clocks), dtype=OUTPUT_DTYPE)
    out['date'] = np.repeat(sessions, len(clocks))
    out['decision_clock'] = np.tile(clocks, len(sessions))
    out['source_clock'], out['source_rows'] = source_clocks.ravel(), counts.ravel()
    age = np.where(selected_ms < 0, -1, decisions[np.newaxis, :] - selected_ms)
    out['age_ms'] = age.ravel()
    usable = (selected_ms >= 0) & (age <= max_age_ms)
    for k, (feature, mask) in enumerate(zip(FEATURES, MASKS)):
        good = usable & valid[:, :, k] & ~ambiguous[:, :, k]
        out[feature] = np.where(good, values[:, :, k], np.nan).ravel()
        out[mask] = good.ravel()
        out[feature + '_ambiguous'] = ambiguous[:, :, k].ravel()
    return out, {'rows_consumed': consumed, 'duplicate_clock_rows': duplicates,
                 'decision_rows': len(out), 'sessions': len(sessions),
                 'stale_or_missing_decision_rows': int((~usable).sum()),
                 'valid_counts': {mask: int(out[mask].sum()) for mask in MASKS},
                 'ambiguous_counts': {x: int(out[x + '_ambiguous'].sum()) for x in FEATURES}}


def _reference(ref):
    if (not isinstance(ref, dict) or set(ref) != {'relative', 'sha256', 'rows', 'bytes'}
            or not isinstance(ref['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])
            or type(ref['rows']) is not int or not 1 <= ref['rows'] <= 1200000
            or type(ref['bytes']) is not int or not 1 <= ref['bytes'] <= 128 * 1024 * 1024):
        raise QuoteInputError('quote_input_reference_invalid')
    relative = ref['relative']
    if not isinstance(relative, str) or not relative:
        raise QuoteInputError('quote_input_path_invalid')
    parts = relative.replace('\\', '/').split('/')
    if (PureWindowsPath(relative).drive or relative.startswith(('/', '\\'))
            or any(p in {'', '.', '..'} or p != p.rstrip(' .') or ':' in p or '\0' in p for p in parts)
            or Path(relative).suffix.lower() != '.npy'):
        raise QuoteInputError('quote_input_path_invalid')
    return Path(*parts)


def _hash_stream(stream):
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024): digest.update(chunk)
    return digest.hexdigest()


def native_chunks(root, ref, chunk_rows=8192):
    """Verified same-descriptor NPY v1 reader; no pickle or path reopening."""
    if type(chunk_rows) is not int or not 1 <= chunk_rows <= 8192:
        raise QuoteInputError('quote_input_chunk_cap')
    relative = _reference(ref)
    base = _native_root(root)
    target = base / relative
    _no_links(base, relative)
    before_path = os.lstat(target)
    if not stat.S_ISREG(before_path.st_mode) or before_path.st_nlink != 1:
        raise QuoteInputError('quote_input_not_owned_regular_file')
    fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (_path_identity(before) != _path_identity(before_path) or before.st_nlink != 1
                or before.st_size != ref['bytes'] or _hash_stream(stream) != ref['sha256']):
            raise QuoteInputError('quote_input_identity_or_sha_mismatch')
        stream.seek(0)
        if np.lib.format.read_magic(stream) != (1, 0):
            raise QuoteInputError('quote_input_npy_version_invalid')
        shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=4096)
        if (shape != (ref['rows'],) or fortran or dtype != INPUT_DTYPE or dtype.hasobject
                or stream.tell() + ref['rows'] * dtype.itemsize != before.st_size):
            raise QuoteInputError('quote_input_npy_schema_or_extent_invalid')
        remaining = ref['rows']
        while remaining:
            count = min(chunk_rows, remaining)
            raw = stream.read(count * dtype.itemsize)
            if len(raw) != count * dtype.itemsize:
                raise QuoteInputError('quote_input_truncated')
            yield np.frombuffer(raw, dtype=dtype)
            remaining -= count
        if (_hash_stream(stream) != ref['sha256'] or _identity(os.fstat(stream.fileno())) != _identity(before)):
            raise QuoteInputError('quote_input_changed_during_read')
        _no_links(base, relative)
        if _path_identity(os.lstat(target)) != _path_identity(before):
            raise QuoteInputError('quote_input_path_replaced')


def _publish(directory, name, rows):
    final, partial = directory / name, directory / (name + '.partial')
    if final.exists(): raise QuoteInputError('quote_input_output_exists')
    fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(fd, 'w+b') as stream:
        np.lib.format.write_array(stream, rows, version=(1, 0), allow_pickle=False)
        stream.flush()
        os.fsync(stream.fileno())
        digest = _hash_stream(stream)
        identity = os.fstat(stream.fileno())
    if identity.st_nlink != 1 or _path_identity(os.lstat(partial)) != _path_identity(identity):
        raise QuoteInputError('quote_input_output_identity_changed')
    # Exclusive link publication fails if another producer owns the final name.
    os.link(partial, final)
    if (_path_identity(os.lstat(final)) != _path_identity(identity)
            or _path_identity(os.lstat(partial)) != _path_identity(identity)):
        raise QuoteInputError('quote_input_output_publication_changed')
    partial.unlink()
    with final.open('rb') as stream:
        if (_hash_stream(stream) != digest or os.fstat(stream.fileno()).st_nlink != 1
                or _path_identity(os.fstat(stream.fileno())) != _path_identity(identity)
                or _path_identity(os.lstat(final)) != _path_identity(identity)):
            raise QuoteInputError('quote_input_output_publication_changed')
    return {'relative': final.name, 'sha256': digest, 'rows': len(rows), 'bytes': identity.st_size}


def _timed_chunks(root, ref, started):
    for rows in native_chunks(root, ref):
        if time.monotonic() - started > 60: raise QuoteInputError('quote_input_time_cap')
        if peak_rss() > 512 * 1024 * 1024: raise QuoteInputError('quote_input_memory_cap')
        yield rows
    if time.monotonic() - started > 60: raise QuoteInputError('quote_input_time_cap')


def run_input_replay(root, spec):
    binding = _binding()
    keys = {'schema', 'purpose', 'files', 'sessions', 'decision_clocks', 'assumed_visibility_lag_ms',
            'max_age_ms', 'source_sha256', 'output_directory'}
    if (not isinstance(spec, dict) or set(spec) != keys or type(spec.get('schema')) is not int
            or spec['schema'] != 1 or spec['purpose'] != 'engineering_only'):
        raise QuoteInputError('quote_input_spec_invalid')
    _settings(spec['sessions'], spec['decision_clocks'], spec['assumed_visibility_lag_ms'], spec['max_age_ms'])
    if not isinstance(spec['files'], dict) or not 1 <= len(spec['files']) <= 4:
        raise QuoteInputError('quote_input_file_cap')
    for instrument, ref in spec['files'].items():
        if not re.fullmatch(r'[0-9]{6}\.X(SHE|SHG)', instrument):
            raise QuoteInputError('quote_input_instrument_invalid')
        _reference(ref)
    if sum(x['rows'] for x in spec['files'].values()) > 4000000:
        raise QuoteInputError('quote_input_row_cap')
    name = spec['output_directory']
    if not isinstance(name, str) or not re.fullmatch(r'quote-input-[0-9a-f]{16}', name):
        raise QuoteInputError('quote_input_output_name_invalid')
    if binding['sha256'] != spec['source_sha256']:
        raise QuoteInputError('quote_input_source_binding_invalid')
    base = _native_root(root)
    directory = base / name
    directory.mkdir()  # Conflict/partial runs are retained; no implicit rerun.
    started = time.monotonic()
    result = {'schema': 1, 'state': 'input_replay_started', 'source_qualified': False,
              'model_admitted': False, 'orders_enabled': False, 'files': {},
              'source_binding': binding, 'purpose': 'engineering_only',
              'spec_sha256': hashlib.sha256(json.dumps(spec, sort_keys=True, allow_nan=False).encode()).hexdigest(),
              'clock_basis': 'captured_clock_plus_assumed_lag_not_certified_availability',
              'assumed_visibility_lag_ms': spec['assumed_visibility_lag_ms'], 'max_age_ms': spec['max_age_ms']}
    try:
        for instrument, ref in spec['files'].items():
            if time.monotonic() - started > 60: raise QuoteInputError('quote_input_time_cap')
            rows, summary = decision_inputs(_timed_chunks(base, ref, started), spec['sessions'], spec['decision_clocks'],
                spec['assumed_visibility_lag_ms'], spec['max_age_ms'])
            if summary['rows_consumed'] != ref['rows'] or _binding() != binding:
                raise QuoteInputError('quote_input_publication_binding_changed')
            result['files'][instrument] = {'output': _publish(directory, instrument + '.inputs.npy', rows), **summary}
            if time.monotonic() - started > 60: raise QuoteInputError('quote_input_time_cap')
        if _binding() != binding: raise QuoteInputError('quote_input_source_drift')
        if peak_rss() > 512 * 1024 * 1024: raise QuoteInputError('quote_input_memory_cap')
        result['state'] = 'quote_input_replay_completed'
        result['input_id'] = hashlib.sha256(json.dumps(spec, sort_keys=True, allow_nan=False).encode()).hexdigest()
        result['counts'] = {'files': len(result['files']),
            'source_rows': sum(x['rows_consumed'] for x in result['files'].values()),
            'decision_rows': sum(x['decision_rows'] for x in result['files'].values())}
    except BaseException as exc:
        result['state'] = 'input_replay_failed'
        result['error_type'] = type(exc).__name__
        raise
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - started, 6)
        try:
            result['peak_rss_bytes'] = peak_rss()
        except Exception:
            result['peak_rss_bytes'] = None
        # Own metadata only. Raw feature values are retained exclusively in NPY.
        journal = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2).encode() + b'\n'
        fd = os.open(directory / 'result.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(journal)
            stream.flush()
            os.fsync(stream.fileno())
    return result


_FUNCTION_NAMES = tuple(name for name, fn in globals().items()
    if isinstance(fn, FunctionType) and fn.__module__ == __name__)
