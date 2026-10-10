"""Bounded official-site listing observation, with no trading authority.

An HTTP receipt and local hashes are reproducibility evidence, not a source
signature, historical PIT dataset or verified ordinary-A-share denominator.
Only the SSE formats actually inspected are parsed; other responses stay
unverified until an explicit-field parser has its own real source evidence.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo('Asia/Shanghai')
CAPTURE_SCHEMA = 'official_listing_capture.v1'
REPORT_SCHEMA = 'official_listing_inventory_report.v1'
STATUS_SCHEMA = 'official_listing_inventory_status.v1'
MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ROWS = 10000
HEX = re.compile(r'^[0-9a-f]{64}$')
BOUNDARY = {'research_only': True, 'generates_alert': False,
            'full_a_mainline_validated': False, 'production_eligible': False,
            'economic_target_A_validated': False, 'rl_reward_verified': False,
            'listing_independently_reconciled': False,
            'listing_status_mapping_verified': False,
            'instrument_type_verified': False, 'historical_pit_verified': False,
            'external_time_attested': False, 'source_signature_verified': False}
SSE_PARAMS = {'REG_PROVINCE': '', 'CSRC_CODE': '', 'STOCK_CODE': '',
              'sqlId': 'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L',
              'COMPANY_STATUS': '2,4,5,7,8', 'type': 'inParams',
              'isPagination': 'true', 'pageHelp.cacheSize': '1',
              'pageHelp.beginPage': '1', 'pageHelp.pageSize': '10000',
              'pageHelp.pageNo': '1', 'pageHelp.endPage': '1'}
SPECS = {
    'sse_main': ('SSE', 'GET', 'https://query.sse.com.cn/sseQuery/commonQuery.do',
                 {**SSE_PARAMS, 'STOCK_TYPE': '1'}),
    'sse_star': ('SSE', 'GET', 'https://query.sse.com.cn/sseQuery/commonQuery.do',
                 {**SSE_PARAMS, 'STOCK_TYPE': '8'}),
    'szse_a': ('SZSE', 'GET', 'https://www.szse.cn/api/report/ShowReport',
               {'SHOWTYPE': 'xlsx', 'CATALOGID': '1110', 'TABKEY': 'tab1',
                'random': '0.6935816432433362'}),
    'bse_first': ('BSE', 'POST', 'https://www.bse.cn/nqxxController/nqxxCnzq.do',
                  {'page': '0', 'typejb': 'T', 'xxfcbj[]': '2', 'xxzqdm': '',
                   'sortfield': 'xxzqdm', 'sorttype': 'asc'}),
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError('time_invalid')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('time_invalid')
    return stamp.astimezone(SH_TZ)


def _bounded(path: Path, limit: int) -> bytes:
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError('path_redirected')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('size_limit')
    return raw


def _directory(root: Path) -> Path:
    base = Path(root).resolve()
    reports = base / 'reports'
    directory = reports / 'official_listing_inventory'
    if reports.resolve() != reports or directory.resolve() != directory:
        raise ValueError('output_redirected')
    return directory


def _sse(raw: bytes, kind: str, completed: datetime) -> tuple[dict[str, Any], set[str]]:
    obj = json.loads(raw)
    rows, page = obj.get('result'), obj.get('pageHelp')
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS or not isinstance(page, dict):
        raise ValueError('sse_shape_invalid')
    if (type(page.get('total')) is not int or page['total'] != len(rows)
            or type(page.get('pageCount')) is not int or page['pageCount'] != 1
            or type(page.get('pageNo')) is not int or page['pageNo'] != 1
            or page.get('data') != rows):
        raise ValueError('sse_pagination_incomplete')
    codes: set[str] = set()
    states: dict[str, int] = {}
    dates = []
    expected_type = SPECS[kind][3]['STOCK_TYPE']
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError('sse_row_invalid')
        code, listed = row.get('A_STOCK_CODE'), row.get('LIST_DATE')
        if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code)
                or code + '.SH' in codes or row.get('STOCK_TYPE') != expected_type
                or str(row.get('NUM')) != str(index)
                or not isinstance(listed, str) or not re.fullmatch(r'[0-9]{8}', listed)
                or row.get('DELIST_DATE') not in ('-', '', None)
                or not isinstance(row.get('STATE_CODE'), str)):
            raise ValueError('sse_row_identity_invalid')
        listed_day = datetime.strptime(listed, '%Y%m%d').date()
        if listed_day > completed.date():
            raise ValueError('future_listing')
        codes.add(code + '.SH')
        dates.append(listed_day.isoformat())
        state = row['STATE_CODE']
        states[state] = states.get(state, 0) + 1
    return {'status': 'published_sse_filter_complete', 'rows': len(rows),
            'declared_total': page['total'], 'page_count': 1,
            'unique_codes': len(codes), 'source_state_counts': states,
            'listing_date_range': [min(dates), max(dates)],
            'published_filter_complete': True,
            'ordinary_a_share_scope_verified': False}, codes


def _requests(manifest: object) -> tuple[datetime, list[dict[str, Any]]]:
    """Validate the entire small manifest before looking up any raw file."""
    if not isinstance(manifest, dict) or manifest.get('schema') != CAPTURE_SCHEMA:
        raise ValueError('manifest_invalid')
    observations = manifest.get('requests')
    if not isinstance(observations, list) or len(observations) != len(SPECS):
        raise ValueError('request_count_invalid')
    observed = _time(manifest.get('observed_at'))
    seen: set[str] = set()
    for note in observations:
        if not isinstance(note, dict) or note.get('id') not in SPECS or note['id'] in seen:
            raise ValueError('request_identity_invalid')
        kind = note['id']
        seen.add(kind)
        exchange, method, url, params = SPECS[kind]
        if (note.get('method'), note.get('url'), note.get('filters')) != (method, url, params):
            raise ValueError('request_filter_invalid')
        started, completed = _time(note.get('started_at')), _time(note.get('completed_at'))
        if not started <= completed <= observed or started.date() != observed.date():
            raise ValueError('request_time_invalid')
        digest, status = note.get('raw_sha256'), note.get('http_status')
        if digest is not None:
            if not isinstance(digest, str) or not HEX.fullmatch(digest):
                raise ValueError('raw_identity_invalid')
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError('http_status_invalid')
        elif status is not None or note.get('failure_class') not in ('connection', 'timeout', 'transport_error'):
            raise ValueError('missing_failure_identity')
    return observed, observations


def assess(manifest: object, raw_by_sha: dict[str, bytes]) -> dict[str, Any]:
    observed, observations = _requests(manifest)
    identities: set[str] = set()
    sources: dict[str, Any] = {}
    total_bytes = 0
    for note in observations:
        kind = note['id']
        exchange = SPECS[kind][0]
        started, completed = _time(note['started_at']), _time(note['completed_at'])
        info = {'exchange': exchange, 'status': 'source_unavailable', 'rows': None,
                'published_filter_complete': False, 'ordinary_a_share_scope_verified': False,
                'started_at': started.isoformat(), 'completed_at': completed.isoformat()}
        digest, status = note.get('raw_sha256'), note.get('http_status')
        if digest is not None:
            if not isinstance(digest, str) or not HEX.fullmatch(digest) or digest not in raw_by_sha:
                raise ValueError('raw_identity_invalid')
            raw = raw_by_sha[digest]
            if not isinstance(raw, bytes) or len(raw) > MAX_RAW_BYTES or _sha(raw) != digest:
                raise ValueError('raw_hash_or_size_invalid')
            total_bytes += len(raw)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError('batch_size_limit')
            info.update(raw_sha256=digest, raw_bytes=len(raw))
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError('http_status_invalid')
            info['http_status'] = status
            if status == 200 and kind.startswith('sse'):
                try:
                    parsed, codes = _sse(raw, kind, completed)
                    if identities & codes:
                        raise ValueError('sse_cross_filter_duplicate')
                    info.update(parsed)
                    identities |= codes
                except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
                    info['status'] = 'invalid_sse_listing'
            elif status == 200:
                info['status'] = 'uninspected_format'
            else:
                info['failure_class'] = 'http_' + str(status)
        else:
            if status is not None or note.get('failure_class') not in ('connection', 'timeout', 'transport_error'):
                raise ValueError('missing_failure_identity')
            info['failure_class'] = note['failure_class']
        sources[kind] = info
    received = sum(row['rows'] or 0 for row in sources.values())
    return {'schema': REPORT_SCHEMA, **BOUNDARY,
            'status': 'partial_listing_inventory' if received else 'no_verified_listing_rows',
            'observed_at': observed.isoformat(), 'sources': sources,
            'sse_published_rows': received, 'three_exchange_listing_complete': False,
            'source_publication_time_known': False,
            'note': '沪市发布过滤清单不等于普通A股/正常交易全集；深京、工具类别、上市状态映射及历史PIT仍待核。'}


def publish(manifest: dict[str, Any], raw_by_sha: dict[str, bytes], *, root: Path) -> dict[str, Any]:
    raw_manifest = _canonical(manifest)
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise ValueError('manifest_size_limit')
    result = assess(manifest, raw_by_sha)
    expected_raw = {note['raw_sha256'] for note in manifest['requests'] if 'raw_sha256' in note}
    if set(raw_by_sha) != expected_raw:
        raise ValueError('extra_raw_refused')
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    for digest, raw in raw_by_sha.items():
        if not isinstance(digest, str) or not HEX.fullmatch(digest) or _sha(raw) != digest:
            raise ValueError('raw_identity_invalid')
        saved = directory / (digest + '.bin')
        if saved.exists():
            if _bounded(saved, MAX_RAW_BYTES) != raw:
                raise ValueError('raw_collision_or_tamper')
        else:
            with saved.open('xb') as stream:
                stream.write(raw)
    digest = _sha(raw_manifest)
    saved = directory / (digest + '.json')
    if saved.exists():
        if _bounded(saved, MAX_MANIFEST_BYTES) != raw_manifest:
            raise ValueError('manifest_collision_or_tamper')
    else:
        with saved.open('xb') as stream:
            stream.write(raw_manifest)
    report = {**result, 'manifest_sha256': digest}
    report['receipt_sha256'] = _sha(_canonical(report))
    with tempfile.NamedTemporaryFile(mode='wb', dir=directory, delete=False,
                                     prefix='latest.', suffix='.tmp') as stream:
        stream.write(_canonical(report))
        temporary = Path(stream.name)
    os.replace(temporary, directory / 'latest.json')
    return report


def current_status(*, root: Path, now: datetime | None = None) -> dict[str, Any]:
    base = {'schema': STATUS_SCHEMA, 'status': 'awaiting_official_listing',
            'source_current': False, 'three_exchange_listing_complete': False, **BOUNDARY}
    try:
        directory = _directory(root)
        report = json.loads(_bounded(directory / 'latest.json', MAX_MANIFEST_BYTES))
        body = dict(report)
        if body.pop('receipt_sha256') != _sha(_canonical(body)):
            raise ValueError('report_hash_invalid')
        digest = body['manifest_sha256']
        if not isinstance(digest, str) or not HEX.fullmatch(digest):
            raise ValueError('manifest_identity_invalid')
        raw_manifest = _bounded(directory / (digest + '.json'), MAX_MANIFEST_BYTES)
        if _sha(raw_manifest) != digest:
            raise ValueError('manifest_hash_invalid')
        manifest = json.loads(raw_manifest)
        _, requests = _requests(manifest)
        originals = {}
        remaining = MAX_TOTAL_BYTES
        for note in requests:
            raw_sha = note.get('raw_sha256')
            if raw_sha is not None and raw_sha not in originals:
                raw = _bounded(directory / (raw_sha + '.bin'), min(MAX_RAW_BYTES, remaining))
                remaining -= len(raw)
                originals[raw_sha] = raw
        reassessed = assess(manifest, originals)
        if reassessed != {key: value for key, value in body.items() if key != 'manifest_sha256'}:
            raise ValueError('report_content_invalid')
        observed, current = _time(reassessed['observed_at']), now or datetime.now(SH_TZ)
        if current.tzinfo is None or current < observed:
            raise ValueError('current_time_invalid')
        source_current = current.astimezone(SH_TZ).date() == observed.date()
        return {**reassessed, 'schema': STATUS_SCHEMA, 'source_current': source_current,
                'status': reassessed['status'] if source_current else 'historical_listing_capture',
                'manifest_sha256': digest, 'receipt_sha256': report['receipt_sha256']}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {**base, 'status': ('invalid_or_missing_listing_receipt'
                                  if (Path(root) / 'reports/official_listing_inventory/latest.json').exists()
                                  else base['status'])}


def import_observation(directory: Path, *, root: Path) -> dict[str, Any]:
    """Import the MPR12 measured four-request batch; never fetch via an HTTP view."""
    notes, originals = [], {}
    for kind, (_, method, url, params) in SPECS.items():
        note = json.loads(_bounded(Path(directory) / (kind + '.receipt.json'), MAX_RAW_BYTES))
        item = {key: note.get(key) for key in ('id', 'method', 'url', 'filters', 'started_at', 'completed_at')}
        if (item['method'], item['url'], item['filters']) != (method, url, params):
            raise ValueError('observation_identity_invalid')
        if 'http_status' in note:
            raw = _bounded(Path(directory) / (kind + '.bin'), MAX_RAW_BYTES)
            digest = _sha(raw)
            if note.get('sha256') != digest:
                raise ValueError('observation_hash_invalid')
            originals[digest] = raw
            item.update(http_status=note['http_status'], raw_sha256=digest)
        else:
            failure = {'ConnectionError': 'connection', 'Timeout': 'timeout',
                       'ReadTimeout': 'timeout', 'ConnectTimeout': 'timeout'}.get(note.get('result'))
            if failure is None:
                raise ValueError('observation_error_unclassified')
            item['failure_class'] = failure
        notes.append(item)
    observed = max(_time(note['completed_at']) for note in notes)
    return publish({'schema': CAPTURE_SCHEMA, 'observed_at': observed.isoformat(),
                    'requests': notes}, originals, root=root)


def capture(*, root: Path) -> dict[str, Any]:
    """Explicit single batch; TLS enabled, fixed hosts, no redirect, no retries."""
    import requests
    def fetch(kind: str) -> tuple[dict[str, Any], bytes | None]:
        _, method, url, params = SPECS[kind]
        note = {'id': kind, 'method': method, 'url': url, 'filters': dict(params),
                'started_at': datetime.now(timezone.utc).isoformat()}
        raw = None
        deadline = time.monotonic() + 30
        try:
            with requests.request(method, url, params=params if method == 'GET' else None,
                                  data=params if method == 'POST' else None,
                                  headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.sse.com.cn/assortment/stock/list/share/' if kind.startswith('sse') else url},
                                  timeout=(8, 15), stream=True, allow_redirects=False,
                                  verify=True) as response:
                chunks = bytearray()
                for chunk in response.iter_content(65536):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RAW_BYTES or time.monotonic() > deadline:
                        raise requests.exceptions.Timeout('bounded_response_refused')
                raw = bytes(chunks)
                note.update(http_status=response.status_code, raw_sha256=_sha(raw))
        except requests.exceptions.Timeout:
            note['failure_class'] = 'timeout'
        except requests.exceptions.ConnectionError:
            note['failure_class'] = 'connection'
        except requests.exceptions.RequestException:
            note['failure_class'] = 'transport_error'
        note['completed_at'] = datetime.now(timezone.utc).isoformat()
        return note, raw
    with ThreadPoolExecutor(max_workers=4) as pool:
        obtained = list(pool.map(fetch, SPECS))
    notes = [note for note, _ in obtained]
    originals = {note['raw_sha256']: raw for note, raw in obtained if raw is not None}
    manifest = {'schema': CAPTURE_SCHEMA, 'observed_at': max(note['completed_at'] for note in notes),
                'requests': notes}
    return publish(manifest, originals, root=root)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description='Official listing observations; no trade/RL promotion')
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--capture', action='store_true')
    choice.add_argument('--import-observation', type=Path)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    result = capture(root=args.root) if args.capture else import_observation(args.import_observation, root=args.root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
