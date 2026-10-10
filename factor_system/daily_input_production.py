"""Source-specific native production for the two existing arithmetic previews.

No economic trials, retrospective PIT grants, selector, labels or orders. Raw
market data and derived vectors stay in native storage. Only metadata is returned.
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
import time
import numpy as np
from . import registered_daily_inputs as inputs
from . import quote_replay as io
from . import native_snapshot as ns
from research import inspect_short_rq_full as reader
from mom_trading.local_research_history import _safe_dataset

_ROOT = Path(__file__).resolve().parent.parent
_SOURCES = ('factor_system/daily_input_production.py', 'factor_system/registered_daily_inputs.py',
            'factor_system/quote_replay.py', 'factor_system/native_snapshot.py',
            'research/inspect_short_rq_full.py', 'mom_trading/local_research_history.py')
_IMPORTED = {p: hashlib.sha256((_ROOT / p).read_bytes()).hexdigest() for p in _SOURCES}
_INPUT_CONSTANTS = (inputs.DTYPE.descr, inputs.IDS, inputs.REASONS)


def _binding():
    from .research_batch import _code_hash
    from types import FunctionType, CodeType
    import sys
    current = {p: hashlib.sha256((_ROOT / p).read_bytes()).hexdigest() for p in _SOURCES}
    if current != _IMPORTED or (inputs.DTYPE.descr, inputs.IDS, inputs.REASONS) != _INPUT_CONSTANTS:
        raise ValueError('daily_input_source_drift')
    functions = [(globals()[name], __name__, name) for name in _FUNCTIONS] + [
        (inputs.calculate_arrays, inputs.__name__, 'calculate_arrays'),
        (inputs.calculate_rows, inputs.__name__, 'calculate_rows'),
        (_safe_dataset, 'mom_trading.local_research_history', '_safe_dataset'),
        (reader.load_dependencies, reader.__name__, 'load_dependencies'),
        (reader.validate_bar_dtype, reader.__name__, 'validate_bar_dtype'),
        (reader.peak_rss, reader.__name__, 'peak_rss')]
    signatures = {}
    for path in _SOURCES:
        pending = [compile((_ROOT / path).read_bytes(), str(_ROOT / path), 'exec',
                           dont_inherit=True, optimize=sys.flags.optimize)]
        while pending:
            code = pending.pop()
            signatures.setdefault((path, code.co_qualname), set()).add(_code_hash(code))
            pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
    live = {}
    for fn, module, name in functions:
        if not isinstance(fn, FunctionType) or fn.__module__ != module or fn.__qualname__ != name:
            raise ValueError('daily_input_loaded_code_mismatch')
        path = Path(fn.__code__.co_filename).resolve().relative_to(_ROOT).as_posix()
        value = _code_hash(fn.__code__)
        if value not in signatures.get((path, fn.__qualname__), set()):
            raise ValueError('daily_input_loaded_code_mismatch')
        live[path + ':' + fn.__qualname__] = value
    return {'files': current, 'live': live, 'io': io._binding(), 'dtype': inputs.DTYPE.descr,
            'formulas': inputs.IDS, 'reasons': inputs.REASONS, 'numpy': np.__version__}


def _budget(started):
    if time.monotonic() - started > 180 or reader.peak_rss() > 512 * 1024**2:
        raise ValueError('daily_input_resource_cap')


def _reference(ref):
    if (not isinstance(ref, dict) or set(ref) != {'relative', 'sha256', 'bytes'}
            or not isinstance(ref['relative'], str) or not isinstance(ref['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])
            or type(ref['bytes']) is not int or not 0 < ref['bytes'] <= 2 * 1024**3):
        raise ValueError('daily_input_reference_invalid')
    text = ref['relative']
    parts = text.replace('\\', '/').split('/')
    if (PureWindowsPath(text).drive or text.startswith(('/', '\\'))
            or any(p in {'', '.', '..'} or p != p.rstrip(' .') or ':' in p or '\0' in p for p in parts)
            or Path(text).suffix.lower() not in {'.h5', '.npy'}):
        raise ValueError('daily_input_path_invalid')
    return Path(*parts)


def _verified(base, ref, started):
    relative = _reference(ref)
    ns._no_links(base, relative)
    target = base / relative
    path_before = os.lstat(target)
    if not stat.S_ISREG(path_before.st_mode) or path_before.st_nlink != 1:
        raise ValueError('daily_input_regular_file_required')
    fd = os.open(target, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (ns._path_identity(before) != ns._path_identity(path_before)
                or before.st_size != ref['bytes'] or before.st_nlink != 1
                or io._hash_stream(stream) != ref['sha256']):
            raise ValueError('daily_input_source_identity_mismatch')
        _budget(started)
        stream.seek(0)
        yield stream
        if io._hash_stream(stream) != ref['sha256'] or ns._identity(os.fstat(stream.fileno())) != ns._identity(before):
            raise ValueError('daily_input_source_changed')
        ns._no_links(base, relative)
        if ns._path_identity(os.lstat(target)) != ns._path_identity(before):
            raise ValueError('daily_input_source_path_changed')
        _budget(started)


def _calendar(stream):
    if np.lib.format.read_magic(stream) != (1, 0):
        raise ValueError('daily_input_calendar_version_invalid')
    shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=4096)
    if (len(shape) != 1 or not 0 < shape[0] <= 30000 or fortran
            or dtype.kind not in 'iu' or dtype.itemsize > 8 or dtype.hasobject or dtype.subdtype
            or stream.tell() + shape[0] * dtype.itemsize != os.fstat(stream.fileno()).st_size):
        raise ValueError('daily_input_calendar_schema_invalid')
    result = np.frombuffer(stream.read(shape[0] * dtype.itemsize), dtype=dtype).astype(np.int64)
    _dates(result)
    return result


def _dates(values):
    if values.ndim != 1 or values.dtype.kind not in 'iu':
        raise ValueError('daily_input_dates_invalid')
    days = np.where(values > 100000000, values // 1000000, values).astype(np.int64)
    if np.any((values > 100000000) & (values % 1000000 != 0)) or np.any(np.diff(days) <= 0):
        raise ValueError('daily_input_date_order_invalid')
    for day in days:
        d = int(day)
        date(d // 10000, d // 100 % 100, d % 100)
    return days


def _halt_dates(values):
    """Decode a DateSet without truncation, rounding or date imputation.

    This retained source also stores integral eight-digit dates as float64.
    Validate the finite, bounded, exact integer representation before casting;
    NumPy 2.3 lacks the newer same_value casting mode. Raw source is preserved.
    """
    if (values.ndim != 1 or values.dtype.kind not in 'iuf'
            or values.dtype.itemsize > 8 or len(values) > 30000
            or np.any(~np.isfinite(values))
            or np.any((values < 10000101) | (values > 99991231))
            or np.any(values != np.floor(values))):
        raise ValueError('daily_input_halt_dates_invalid')
    dates = values.astype(np.int64)
    if not np.array_equal(dates, values):
        raise ValueError('daily_input_halt_date_cast_changed')
    return _dates(np.unique(dates))


def _selected_date_indexes(key, dates, calendar):
    """Scope the snapshot first; source keys are not historical PIT identities.

    302132 is an exact primary-source-confirmed code-change exception, not a
    wildcard for all 302 instruments. Its retrospective source-key history is
    left intact and unqualified. Empty date windows produce no factor input.
    """
    lo = int(np.searchsorted(dates, 20130101, side='left'))
    hi = int(np.searchsorted(dates, 20260930, side='right'))
    if lo == hi:
        return lo, hi, np.empty(0, dtype=np.int64)
    if (key != '302132.XSHE' and not re.fullmatch(
            r'(?:000|001|002|003|300|301)\d{3}\.XSHE|(?:600|601|603|605|688|689)\d{3}\.XSHG', key)):
        raise ValueError('daily_input_research_identity_changed')
    selected = dates[lo:hi]
    positions = np.searchsorted(calendar, selected)
    if np.any(positions >= len(calendar)) or np.any(calendar[positions] != selected):
        raise ValueError('daily_input_noncalendar_date')
    return lo, hi, positions


def _consume_vector(base, directory, reference, expected_dates, started):
    """Factory input consumer: exact fields/masks reach native numeric arrays.

    This cannot compute IC, rank stocks, labels or returns. It rejects corrupted
    or inconsistent fields before the factory reports successful consumption.
    """
    ref = {'relative': (directory.relative_to(base) / reference['relative']).as_posix(),
           'sha256': reference['sha256'], 'bytes': reference['bytes']}
    with contextmanager(_verified)(base, ref, started) as stream:
        if np.lib.format.read_magic(stream) != (1, 0):
            raise ValueError('daily_input_vector_version_invalid')
        shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=4096)
        if (shape != (reference['rows'],) or fortran or dtype != inputs.DTYPE
                or stream.tell() + shape[0] * dtype.itemsize != reference['bytes']):
            raise ValueError('daily_input_vector_schema_invalid')
        rows = np.frombuffer(stream.read(shape[0] * dtype.itemsize), dtype=dtype)
        if not np.array_equal(rows['date'], expected_dates):
            raise ValueError('daily_input_consumer_date_mismatch')
        counts = {}
        for name, mask_name in zip(inputs.IDS, ('close_reason', 'amount_reason')):
            mask, values = rows[mask_name], rows[name]
            if (np.any(mask >= len(inputs.REASONS))
                    or np.any(~np.isfinite(values[mask == 0]))
                    or np.any(~np.isnan(values[mask != 0]))):
                raise ValueError('daily_input_consumer_field_mask_mismatch')
            if name == inputs.IDS[0] and np.any((values[mask == 0] < 0) | (values[mask == 0] > 1)):
                raise ValueError('daily_input_consumer_price_location_invalid')
            if name == inputs.IDS[1] and np.any(values[mask == 0] >= 0):
                raise ValueError('daily_input_consumer_amount_ratio_invalid')
            counts[name] = [int((mask == i).sum()) for i in range(len(inputs.REASONS))]
        if np.any(rows['reported_halt'] & (rows['close_reason'] != 7)):
            raise ValueError('daily_input_consumer_halt_mismatch')
        return counts


def _produce(base, spec, directory, result, started):
    h5, loaded_np = reader.load_dependencies()
    if loaded_np is not np:
        raise ValueError('daily_input_numpy_runtime_mixed')
    with contextmanager(_verified)(base, spec['calendar'], started) as stream:
        calendar = _calendar(stream)
    with contextmanager(_verified)(base, spec['stocks'], started) as stock_stream, contextmanager(_verified)(base, spec['halts'], started) as halt_stream:
        with h5.File(stock_stream, 'r', rdcc_nbytes=1048576) as stocks, h5.File(halt_stream, 'r', rdcc_nbytes=1048576) as halts:
            keys = sorted(stocks.keys())
            if len(keys) != 5537:
                raise ValueError('daily_input_stored_key_count_changed')
            for key in keys:
                _budget(started)
                dataset = _safe_dataset(stocks, key, h5, np)
                reader.validate_bar_dtype(dataset.dtype, ['datetime', 'high', 'low', 'close', 'total_turnover'])
                dates = _dates(dataset.fields('datetime')[:])
                result['datetime_rows_scanned'] += len(dates)
                lo, hi, positions = _selected_date_indexes(key, dates, calendar)
                if lo == hi:
                    result['empty_window_keys'] += 1
                    continue
                n = hi - lo
                if result['source_rows'] + n > 12934946:
                    raise ValueError('daily_input_row_cap')
                # Only the frozen date range's required fields are materialized.
                rows = dataset.fields(['high', 'low', 'close', 'total_turnover'])[lo:hi]
                selected_dates = dates[lo:hi]
                present = key in halts
                reported = np.zeros(n, dtype=bool)
                if present:
                    events = _safe_dataset(halts, key, h5, np)[:]
                    # Source DateSet is a set; do not require raw set ordering.
                    event_dates = _halt_dates(events)
                    reported = np.isin(selected_dates, event_dates)
                values = inputs.calculate_arrays(*(rows[f] for f in ('high', 'low', 'close', 'total_turnover')),
                    positions, reported_halt=reported)
                out = np.empty(n, dtype=inputs.DTYPE)
                out['date'] = selected_dates
                for name, value in zip((*inputs.IDS, 'close_reason', 'amount_reason'), values):
                    out[name] = value
                out['reported_halt'], out['halt_dataset_present'] = reported, present
                predicted = n * inputs.DTYPE.itemsize + 4096
                if result['output_bytes'] + predicted > 512 * 1024**2:
                    raise ValueError('daily_input_output_cap')
                ns._no_links(base, directory.relative_to(base))
                result['files'][key] = io._publish(directory, key + '.inputs.npy', out)
                received = _consume_vector(base, directory, result['files'][key], selected_dates, started)
                result['source_rows'] += n
                result['consumed_rows'] += n
                result['output_bytes'] += result['files'][key]['bytes']
                result['reported_halt_rows'] += int(reported.sum())
                result['halt_dataset_missing_rows'] += 0 if present else n
                for name, mask in zip(inputs.IDS, values[2:]):
                    if received[name] != [int((mask == i).sum()) for i in range(len(inputs.REASONS))]:
                        raise ValueError('daily_input_consumer_reason_count_mismatch')
                    result['reason_counts'][name] = [x + int((mask == i).sum())
                        for i, x in enumerate(result['reason_counts'][name])]
                del out, rows, values
                if len(result['files']) % 250 == 0:
                    _budget(started)


def run_native_production(root, spec):
    binding = _binding()  # Before input decoding, source reads or output mutation.
    keys = {'schema', 'purpose', 'stocks', 'calendar', 'halts', 'source_sha256', 'output_directory'}
    if (not isinstance(spec, dict) or set(spec) != keys or type(spec.get('schema')) is not int
            or spec['schema'] != 1 or spec['purpose'] != 'engineering_only'
            or spec['source_sha256'] != binding['files']['factor_system/daily_input_production.py']
            or not isinstance(spec['output_directory'], str)
            or not re.fullmatch('daily-input-[0-9a-f]{16}', spec['output_directory'])):
        raise ValueError('daily_input_contract_invalid')
    for name in ('stocks', 'calendar', 'halts'):
        _reference(spec[name])
    base = ns._native_root(root)
    directory = base / spec['output_directory']
    directory.mkdir()  # A prior partial or completed invocation is a conflict.
    started = time.monotonic()
    result = {'schema': 1, 'state': 'daily_input_started', 'purpose': 'engineering_only',
        'source_qualified': False, 'model_admitted': False, 'orders_enabled': False,
        'historical_identity_qualified': False,
        'economic_runs': 0, 'source_rows': 0, 'consumed_rows': 0, 'datetime_rows_scanned': 0, 'empty_window_keys': 0,
        'output_bytes': 0, 'reported_halt_rows': 0, 'halt_dataset_missing_rows': 0, 'files': {},
        'reason_counts': {key: [0] * len(inputs.REASONS) for key in inputs.IDS},
        'source_binding': binding, 'spec': spec,
        'assumptions': 'Common HLC units and fixed amount units, source calendar and reported-halt sets are engineering conventions, not certified PIT/identity/units/full-A/no-halt coverage.'}
    original_error = None
    try:
        _produce(base, spec, directory, result, started)
        if (result['source_rows'] != result['consumed_rows'] or result['source_rows'] != 12934946
                or len(result['files']) != 5487 or _binding() != binding):
            raise ValueError('daily_input_final_binding_or_count_changed')
        _budget(started)
        result['state'] = 'daily_input_production_completed'
        result['input_id'] = hashlib.sha256(json.dumps(spec, sort_keys=True, allow_nan=False).encode()).hexdigest()
        result['counts'] = {'files': len(result['files']), 'source_rows': result['source_rows'],
                            'consumed_rows': result['consumed_rows']}
    except BaseException as exc:
        original_error = exc
        result['state'] = 'daily_input_failed'
        result['error_type'] = type(exc).__name__
        # Only own static enum-like codes; no exception values/traceback/data.
        if (type(exc) is ValueError and len(exc.args) == 1
                and isinstance(exc.args[0], str)
                and re.fullmatch(r'daily_input_[a-z_]{1,64}', exc.args[0])):
            result['error_code'] = exc.args[0]
        raise
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - started, 6)
        terminal_error = None
        try:
            result['peak_rss_bytes'] = reader.peak_rss()
        except Exception as exc:
            result['peak_rss_bytes'] = None
            result['resource_probe_error_type'] = type(exc).__name__
            terminal_error = ValueError('daily_input_terminal_resource_unknown')
        try:
            if _binding() != binding:
                raise ValueError('daily_input_terminal_binding_changed')
        except Exception as exc:
            result['terminal_binding_error_type'] = type(exc).__name__
            terminal_error = exc
        if terminal_error is not None:
            result['state'] = 'daily_input_failed'
            if original_error is None:
                result['error_type'] = type(terminal_error).__name__
        raw = (json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode()
        ns._no_links(base, directory.relative_to(base))
        with (directory / 'result.json').open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if original_error is None and terminal_error is not None:
            raise terminal_error
    # A post-publication rejection retains files and the first journal; it is
    # not a rollback. The caller must also attest the new modules after return.
    if _binding() != binding:
        with (directory / 'publication_rejected.json').open('x', encoding='utf-8') as stream:
            json.dump({'state':'publication_binding_rejected','previous_files_retained':True},stream)
        raise ValueError('daily_input_post_publication_binding_changed')
    return result


_FUNCTIONS = tuple(name for name, obj in globals().items()
                   if getattr(obj, '__module__', None) == __name__ and callable(obj))
