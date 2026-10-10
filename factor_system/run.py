"""Manual entry for explicit, frozen native engineering research.

python -m factor_system.run --native-root DIR --request-relative FILE.json
    --request-sha256 SHA256

Replaces implicit legacy mine/backtest/full routing. A successful synthetic
command verifies computation only; it cannot admit a factor, strategy or order.
"""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from types import FunctionType, CodeType

_ENTRY_PATH = Path(__file__).resolve()
_IMPORTED_ENTRY_SHA = hashlib.sha256(_ENTRY_PATH.read_bytes()).hexdigest()


class EntryArgumentsError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default message can echo paths and arbitrary user values.
        raise EntryArgumentsError('explicit_frozen_request_required')


def _entry_binding(rb):
    raw = _ENTRY_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != _IMPORTED_ENTRY_SHA:
        raise rb.BatchIntegrityError('manual_entry_source_drift')
    functions = []
    for name in _ENTRY_FUNCTION_NAMES:
        fn = globals().get(name)
        if (not isinstance(fn, FunctionType) or fn.__module__ != __name__
                or fn.__qualname__ != name):
            raise rb.BatchIntegrityError('manual_entry_loaded_code_disk_mismatch')
        functions.append(fn)
    functions.append(_Parser.error)
    if (not isinstance(_Parser.error, FunctionType) or _Parser.error.__module__ != __name__
            or _Parser.error.__qualname__ != '_Parser.error'):
        raise rb.BatchIntegrityError('manual_entry_loaded_code_disk_mismatch')
    pending = [compile(raw, str(_ENTRY_PATH), 'exec', dont_inherit=True, optimize=sys.flags.optimize)]
    compiled = set()
    while pending:
        current = pending.pop()
        compiled.add(rb._code_hash(current))
        pending.extend(c for c in current.co_consts if isinstance(c, CodeType))
    live = {}
    for fn in functions:
        code = rb._code_hash(fn.__code__)
        if code not in compiled:
            raise rb.BatchIntegrityError('manual_entry_loaded_code_disk_mismatch')
        live[fn.__qualname__] = code
    return {'sha256': digest, 'live': live}


def _counts(result, names, rb):
    out = {}
    for public, internal in names.items():
        value = result.get(internal)
        if type(value) is not int or value < 0:
            raise rb.BatchIntegrityError('manual_entry_result_count_invalid')
        out[public] = value
    return out


def _dispatch(root, request_ref, chain):
    rb = chain.rb
    binding = _entry_binding(rb)
    core_binding = chain._check_sources()
    request = chain._physical(root, request_ref, decode=True)
    if not isinstance(request, dict):
        raise EntryArgumentsError('request_object_required')
    operation = request.get('operation')
    if operation == 'daily_input_production':
        return _dispatch_daily_input(root, request_ref, request, chain, binding, core_binding)
    if operation == 'quote_input_replay':
        return _dispatch_quote_input(root, request_ref, request, chain, binding, core_binding)
    if operation == 'continuous_strategy_chain':
        return _dispatch_continuous(root, request_ref, request, chain, binding, core_binding)
    keys = {'schema', 'purpose', 'operation', 'factor_spec'}
    if operation == 'strategy_chain':
        keys.add('case')
    if (set(request) != keys or type(request.get('schema')) is not int
            or request['schema'] != 1 or request.get('purpose') != 'engineering_only'
            or operation not in {'batch', 'strategy_chain'}):
        raise EntryArgumentsError('request_contract_invalid')
    spec = chain._physical(root, request['factor_spec'], decode=True)
    spec = rb._specification(spec)
    chain._physical(root, request_ref)
    if operation == 'batch':
        result = rb.run_batch(root, spec)
        counts = _counts(result, {
            'factor_trials':'trials', 'factor_executed':'executed',
            'factor_reused':'terminal_reused', 'factor_failures':'compute_failed',
            'factor_builds':'factor_builds', 'label_builds':'label_builds',
            'cache_hits':'cache_hits'}, rb)
        identity = result.get('batch_id')
        flags = ('source_qualification_verified', 'model_admitted', 'strategy_execution_complete')
        failed = counts['factor_failures']
        valid = (counts['factor_trials'] == len(spec['candidates']) * 3
                 and counts['factor_executed'] + counts['factor_reused'] == counts['factor_trials']
                 and failed <= counts['factor_trials'] and result.get('purpose') == 'engineering_only')
    else:
        result = chain.run_engineering_chain(root, spec, request['case'])
        counts = _counts(result, {
            'factor_trials':'factor_trials', 'factor_failures':'factor_failures',
            'strategy_horizons':'horizons', 'alternative_arms_per_horizon':'alternative_arms_per_horizon',
            'strategy_executed':'executed', 'strategy_reused':'terminal_reused',
            'strategy_failures':'compute_failed'}, rb)
        identity = result.get('case_id')
        flags = ('model_admitted', 'real_source_qualified', 'real_execution_verified',
                 'whole_R4_complete', 'orders_enabled')
        failed = counts['factor_failures'] + counts['strategy_failures']
        valid = (counts['factor_trials'] == len(spec['candidates']) * 3
                 and counts['factor_failures'] <= counts['factor_trials']
                 and counts['strategy_horizons'] == counts['alternative_arms_per_horizon'] == 3
                 and counts['strategy_executed'] + counts['strategy_reused'] == 3
                 and counts['strategy_failures'] <= 3)
    if (not valid or any(result.get(key) is not False for key in flags)
            or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity)):
        raise rb.BatchIntegrityError('manual_entry_result_contract_invalid')
    # Gate output without claiming rollback of valid earlier core commits.
    chain._physical(root, request_ref)
    chain._physical(root, request['factor_spec'])
    if _entry_binding(rb) != binding:
        raise rb.BatchIntegrityError('manual_entry_binding_drift')
    chain._check_sources(core_binding)
    code = 3 if failed else 0
    return code, {
        'schema':1, 'ok':code == 0,
        'state':'engineering_completed_with_compute_failures' if failed else 'engineering_completed',
        'operation':operation, 'count_scope':'this_request_only',
        'request_sha256':request_ref['sha256'], 'runner_sha256':binding['sha256'],
        'core_id':identity, 'counts':counts,
        'source_qualified':False, 'model_admitted':False, 'orders_enabled':False}


def _dispatch_continuous(root, request_ref, request, chain, binding, core_binding):
    rb = chain.rb
    if (set(request) != {'schema','purpose','operation','factor_spec','case'}
            or type(request.get('schema')) is not int or request['schema'] != 1
            or request.get('purpose') != 'engineering_only'):
        raise EntryArgumentsError('continuous_request_invalid')
    spec = rb._specification(chain._physical(root, request['factor_spec'], decode=True))
    from research import m24_continuous_execution as continuous
    continuous_binding = continuous._binding()
    result = continuous.run_continuous_chain(root, spec, request['case'])
    counts = _counts(result, {'factor_trials':'factor_trials','factor_failures':'factor_failures',
        'strategy_horizons':'horizons','alternative_arms_per_horizon':'alternative_arms_per_horizon',
        'strategy_origins':'origins','strategy_executed':'executed','strategy_reused':'terminal_reused',
        'strategy_failures':'compute_failed','strategy_steps':'processed_steps','restored_steps':'restored_steps'}, rb)
    identity = result.get('case_id')
    if (counts['factor_trials'] != len(spec['candidates']) * 3
            or counts['factor_failures'] > counts['factor_trials']
            or counts['strategy_horizons'] != 3 or counts['alternative_arms_per_horizon'] != 3
            or not 1 <= counts['strategy_origins'] <= 64
            or counts['strategy_executed'] + counts['strategy_reused'] != 3
            or counts['strategy_failures'] > 3
            or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity)
            or any(result.get(k) is not False for k in ('model_admitted','real_source_qualified',
                'real_execution_verified','whole_R4_complete','orders_enabled'))):
        raise rb.BatchIntegrityError('continuous_result_invalid')
    chain._physical(root, request_ref); chain._physical(root, request['factor_spec']); chain._physical(root, request['case'])
    chain._check_sources(core_binding)
    if _entry_binding(rb) != binding or continuous._binding() != continuous_binding:
        raise rb.BatchIntegrityError('manual_continuous_binding_drift')
    failed = counts['factor_failures'] + counts['strategy_failures']; code = 3 if failed else 0
    return code, {'schema':1,'ok':not failed,'state':'engineering_completed_with_compute_failures' if failed else 'engineering_completed',
        'operation':'continuous_strategy_chain','count_scope':'this_request_only','request_sha256':request_ref['sha256'],
        'runner_sha256':binding['sha256'],'core_id':identity,'counts':counts,
        'source_qualified':False,'model_admitted':False,'orders_enabled':False}


def _dispatch_quote_input(root, request_ref, request, chain, binding, core_binding):
    """Explicit input-only route; never create a daily Panel or alpha trial."""
    rb = chain.rb
    if (set(request) != {'schema', 'purpose', 'operation', 'quote_spec'}
            or type(request.get('schema')) is not int or request['schema'] != 1
            or request.get('purpose') != 'engineering_only'):
        raise EntryArgumentsError('quote_input_request_invalid')
    spec = chain._physical(root, request['quote_spec'], decode=True)
    from . import quote_replay
    try:
        result = quote_replay.run_input_replay(root, spec)
    except quote_replay.QuoteInputError as exc:
        raise rb.BatchIntegrityError('quote_input_replay_rejected') from exc
    counts = _counts(result['counts'], {k:k for k in ('files', 'source_rows', 'decision_rows')}, rb)
    identity = result.get('input_id')
    if (result.get('state') != 'quote_input_replay_completed'
            or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity)
            or any(result.get(k) is not False for k in ('source_qualified', 'model_admitted', 'orders_enabled'))):
        raise rb.BatchIntegrityError('quote_input_result_invalid')
    chain._physical(root, request_ref)
    chain._physical(root, request['quote_spec'])
    if _entry_binding(rb) != binding:
        raise rb.BatchIntegrityError('manual_entry_binding_drift')
    chain._check_sources(core_binding)
    return 0, {'schema':1, 'ok':True, 'state':'quote_input_replay_completed',
        'operation':'quote_input_replay', 'count_scope':'this_request_only',
        'request_sha256':request_ref['sha256'], 'runner_sha256':binding['sha256'],
        'core_id':identity, 'counts':counts,
        'source_qualified':False, 'model_admitted':False, 'orders_enabled':False}


def _dispatch_daily_input(root, request_ref, request, chain, binding, core_binding):
    rb = chain.rb
    if (set(request) != {'schema', 'purpose', 'operation', 'daily_spec'}
            or type(request.get('schema')) is not int or request['schema'] != 1
            or request.get('purpose') != 'engineering_only'):
        raise EntryArgumentsError('daily_input_request_invalid')
    spec = chain._physical(root, request['daily_spec'], decode=True)
    from . import daily_input_production as daily
    daily_binding = daily._binding()
    result = daily.run_native_production(root, spec)
    if daily._binding() != daily_binding or result.get('source_binding') != daily_binding:
        raise rb.BatchIntegrityError('daily_input_binding_drift')
    counts = _counts(result.get('counts', {}), {k:k for k in ('files', 'source_rows', 'consumed_rows')}, rb)
    identity = result.get('input_id')
    if (result.get('state') != 'daily_input_production_completed' or counts['source_rows'] != counts['consumed_rows']
            or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity)
            or any(result.get(k) is not False for k in ('source_qualified', 'model_admitted', 'orders_enabled'))):
        raise rb.BatchIntegrityError('daily_input_result_invalid')
    chain._physical(root, request_ref)
    chain._physical(root, request['daily_spec'])
    if _entry_binding(rb) != binding:
        raise rb.BatchIntegrityError('manual_entry_binding_drift')
    chain._check_sources(core_binding)
    if daily._binding() != daily_binding:
        raise rb.BatchIntegrityError('daily_input_binding_drift')
    return 0, {'schema':1, 'ok':True, 'state':'daily_input_production_completed',
        'operation':'daily_input_production', 'count_scope':'this_request_only',
        'request_sha256':request_ref['sha256'], 'runner_sha256':binding['sha256'],
        'core_id':identity, 'counts':counts,
        'source_qualified':False, 'model_admitted':False, 'orders_enabled':False}


def _failure(code, state):
    # Unknown counts/identity omitted. Never expose arbitrary exception text.
    return code, {'schema':1, 'ok':False, 'state':state,
                  'source_qualified':False, 'model_admitted':False, 'orders_enabled':False}


def main(argv=None):
    parser = _Parser(prog='python -m factor_system.run', allow_abbrev=False,
                     description='Frozen native engineering research; no financial admission.')
    parser.add_argument('--native-root', required=True)
    parser.add_argument('--request-relative', required=True)
    parser.add_argument('--request-sha256', required=True)
    try:
        args = parser.parse_args(argv)
    except EntryArgumentsError:
        code, output = _failure(2, 'invalid_arguments_or_request')
    else:
        # Core diagnostics cannot leak raw payloads to output/task logs.
        with open(os.devnull, 'w', encoding='utf-8') as sink, redirect_stdout(sink), redirect_stderr(sink):
            try:
                from research import m24_strategy_execution_chain as chain
                try:
                    binding = _entry_binding(chain.rb)
                    code, output = _dispatch(args.native_root, {
                        'relative':args.request_relative, 'sha256':args.request_sha256}, chain)
                    if _entry_binding(chain.rb) != binding:
                        raise chain.rb.BatchIntegrityError('manual_entry_binding_drift')
                except chain.rb.BatchIntegrityError:
                    code, output = _failure(4, 'integrity_rejected')
                except (ValueError, TypeError, KeyError):
                    code, output = _failure(2, 'invalid_arguments_or_request')
            except KeyboardInterrupt:
                code, output = _failure(6, 'interrupted')
            except (ImportError, OSError, sqlite3.Error):
                code, output = _failure(5, 'runtime_or_io_unavailable')
            except Exception:
                code, output = _failure(7, 'unexpected_internal_error')
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
    return code


_ENTRY_FUNCTION_NAMES = tuple(name for name, fn in globals().items()
                              if isinstance(fn, FunctionType) and fn.__module__ == __name__)

if __name__ == '__main__':
    sys.exit(main())
