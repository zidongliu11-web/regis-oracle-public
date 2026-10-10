"""Apply only source-viewed class repairs and measure remaining metadata gaps."""
import argparse, calendar as month_calendar, copy, hashlib, json, time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from csrc_industry_members import parse_pdf_texts
from read_instruments_static import parse, date_int
import inspect_short_rq_full as base

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / 'research/CSRC_CLASS_ADJUDICATION_PROTOCOL_20261003.json'
RESULT = ROOT / 'research/CSRC_CLASS_ADJUDICATION_RESULT_20261003.json'

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')

def check(spec):
    for path, digest in spec['input_hashes'].items():
        if sha(ROOT / path) != digest:
            raise ValueError('frozen class adjudication input changed')
    for path, digest in spec['native_input_hashes'].items():
        if sha(path) != digest:
            raise ValueError('native first diagnostic/original changed')

def derive(spec):
    observations = read(ROOT / spec['observations'])['pages']
    if len(observations) != 8 or not all(o['primary_actual_visual_viewed'] for o in observations):
        raise ValueError('eight actual source views required before derivation')
    viewed = {(o['quarter'], o['page']): o for o in observations}
    if len(viewed) != 8:
        raise ValueError('source page observation identity duplicated')
    first = {q['quarter']: q for q in read(ROOT / 'research/CSRC_INDUSTRY_MEMBER_RESULT_20261003.json')['quarter_results']}
    source_seq = read(spec['corrected_flags_sequence'])
    sequence = copy.deepcopy(source_seq)
    coverage_root = Path(spec['first_coverage_manifest']).parent
    ledger = []
    repaired = {}
    for snapshot in sequence['snapshots']:
        quarter = snapshot['quarter']
        if quarter not in spec['quarters']:
            continue
        if snapshot['state'] != 'unknown_source_member_marker' or snapshot['members'] or snapshot['source_snapshot_complete']:
            raise ValueError('first conflict/flag repair state was not retained')
        original = read(first[quarter]['local_member_path'])['members']
        texts = (coverage_root / (quarter + '.layout.txt')).read_text(encoding='utf-8').split('\n\f\n')
        layout, counts = parse_pdf_texts(texts)
        comparison = read(coverage_root / (quarter + '.comparison.json'))
        changed = {k for k in original if k in layout and original[k]['source_industry_major_numeric'] != layout[k]['source_industry_major_numeric']}
        if set(original) != set(layout) or changed != set(comparison['different_source_class_codes']):
            raise ValueError('frozen source identity/disagreement census changed')
        fixed = copy.deepcopy(original)
        for code in sorted(changed):
            before, after = original[code], layout[code]
            page = after['source_locator']['page']
            observation = viewed.get((quarter, page))
            pair = before['source_industry_major_numeric'] + '->' + after['source_industry_major_numeric']
            if observation is None or pair not in observation['viewed_class_pairs'] or before['source_locator']['page'] != page:
                raise ValueError('class repair has no exact viewed publisher page/pair')
            row = {'quarter': quarter, 'source_stock_code': code,
                   'original_major': before['source_industry_major_numeric'], 'corrected_major': after['source_industry_major_numeric'],
                   'original_locator': before['source_locator'], 'layout_locator': after['source_locator'],
                   'publisher_image_sha256': observation['native_image_sha256'], 'page': page}
            ledger.append(row)
            fixed[code]['source_industry_major_numeric'] = after['source_industry_major_numeric']
            fixed[code]['targeted_visual_correction'] = copy.deepcopy(row)
        if len(changed) != spec['quarters'][quarter]:
            raise ValueError('registered exact repair count changed')
        for code in set(original) - changed:
            if fixed[code] != original[code]:
                raise ValueError('unviewed member mutated')
        snapshot.update(members=fixed, state='source_classes_with_targeted_visual_repair_not_PIT_certified',
                        source_snapshot_complete=True, first_conflict_marker_retained_in_prior_capture=True,
                        targeted_class_repairs=len(changed), whole_source_semantics_certified=False)
        repaired[quarter] = {'members': fixed, 'source_rows': len(fixed), 'class_repairs': len(changed), 'layout_counts': counts}
    for observation in observations:
        pairs = Counter(x['original_major'] + '->' + x['corrected_major'] for x in ledger
                        if x['quarter'] == observation['quarter'] and x['page'] == observation['page'])
        if dict(pairs) != observation['viewed_class_pairs']:
            raise ValueError('page boundary ledger does not match actual viewed source counts')
    if len(ledger) != 121:
        raise ValueError('only 121 registered source class corrections permitted')
    for old, new in zip(source_seq['snapshots'], sequence['snapshots']):
        if old['quarter'] not in spec['quarters'] and old != new:
            raise ValueError('unregistered quarter changed')
        if (old['quarter'], old['effective_session']) != (new['quarter'], new['effective_session']):
            raise ValueError('publication marker changed')
    sequence.update(targeted_visual_repairs=121, whole_source_semantics_certified=False)
    return sequence, repaired, ledger

def quarter_end(quarter):
    year, month = int(quarter[:4]), int(quarter[-1]) * 3
    return year * 10000 + month * 100 + month_calendar.monthrange(year, month)[1]

def measure(spec, sequence):
    identities, _ = parse(Path(spec['instruments']).read_bytes())
    _, np = base.load_dependencies()
    calendar = list(map(int, base.read_calendar(Path(spec['calendar']), np)))
    sessions = [x for x in calendar if spec['start_session'] <= x <= spec['end_session']]
    census = {x['order_book_id'].split('.')[0]: x for x in identities if x.get('type') == 'CS'}
    if len(census) != sum(x.get('type') == 'CS' for x in identities):
        raise ValueError('nonunique current producer CS identity')
    lifetimes = {k: (date_int(x.get('listed_date')), date_int(x.get('de_listed_date'))) for k, x in census.items()}
    unknown_listing = sorted(k for k, (first, _) in lifetimes.items() if first is None)
    snapshots = sequence['snapshots']
    markers = [x['effective_session'] for x in snapshots]
    if len(snapshots) != 32 or any(x is None for x in markers) or markers != sorted(set(markers)):
        raise ValueError('complete explicit publication markers changed')
    old_daily = read(spec['first_coverage_daily'])['daily']
    daily, unknown_rows, monthly, index = [], [], defaultdict(list), -1
    for i, day in enumerate(sessions):
        while index + 1 < len(snapshots) and snapshots[index + 1]['effective_session'] <= day:
            index += 1
        snapshot = snapshots[index] if index >= 0 else None
        members = snapshot['members'] if snapshot else {}
        active = {k for k, (first, last) in lifetimes.items() if first is not None and first <= day and (last is None or day < last)}
        assigned = active & set(members)
        missing = active - assigned
        reasons = Counter()
        qend = quarter_end(snapshot['quarter']) if snapshot else None
        for code in sorted(missing):
            listed = lifetimes[code][0]
            if snapshot is None:
                reason = 'no_prior_declared_publication'
            elif snapshot['state'] == 'unknown_source_member_marker':
                reason = 'explicit_unknown_source_quarter'
            elif snapshot.get('quarantined_rows') and code == '603026':
                reason = 'publisher_duplicate_identity_quarantined'
            elif listed > qend:
                reason = 'listed_after_declared_quarter_end_possible_reporting_lag_not_proven'
            else:
                reason = 'listed_by_quarter_end_unclassified_source_metadata_gap_unresolved'
            reasons[reason] += 1
            unknown_rows.append({'session': day, 'quarter': snapshot['quarter'] if snapshot else None,
                                 'source_stock_code': code, 'listed_date_current_metadata': listed,
                                 'quarter_end_declared': qend, 'reason': reason})
        row = {'session': day, 'quarter': snapshot['quarter'] if snapshot else None,
               'snapshot_state': snapshot['state'] if snapshot else 'no_prior_published_snapshot',
               'known_lifetime_active_CS': len(active), 'known_source_classified_CS': len(assigned),
               'known_lifetime_unclassified_CS': len(missing),
               'classified_fraction_known_lifetime': len(assigned) / len(active) if active else None,
               'unknown_listing_identities': len(unknown_listing),
               'group_member_counts': dict(sorted(Counter(members[k]['source_industry_major_numeric'] for k in assigned).items())),
               'unclassified_reason_counts': dict(sorted(reasons.items())),
               'all_lifetime_known_CS_classified': not missing, 'full_A_census_certified': False}
        old = old_daily[i]
        if (old['session'], old['quarter'], old['known_lifetime_active_CS'], old['unknown_listing_identities']) != (day, row['quarter'], len(active), len(unknown_listing)):
            raise ValueError('coverage denominator or publication timing changed')
        if row['quarter'] not in spec['quarters'] and any(old[k] != row[k] for k in old):
            raise ValueError('unrepaired quarter coverage changed')
        daily.append(row)
        monthly[day // 100].append(row)
    aggregates = []
    for month, rows in sorted(monthly.items()):
        reasons = Counter()
        for row in rows:
            reasons.update(row['unclassified_reason_counts'])
        fractions = [x['classified_fraction_known_lifetime'] for x in rows]
        aggregates.append({'month': month, 'sessions': len(rows),
                           'known_active_min': min(x['known_lifetime_active_CS'] for x in rows),
                           'known_active_max': max(x['known_lifetime_active_CS'] for x in rows),
                           'unclassified_min': min(x['known_lifetime_unclassified_CS'] for x in rows),
                           'unclassified_max': max(x['known_lifetime_unclassified_CS'] for x in rows),
                           'coverage_min': min(fractions), 'coverage_max': max(fractions),
                           'zero_coverage_sessions': sum(x['known_source_classified_CS'] == 0 for x in rows),
                           'quarters_used': sorted({x['quarter'] for x in rows if x['quarter']}),
                           'every_known_active_CS_classified_sessions': sum(x['all_lifetime_known_CS_classified'] for x in rows),
                           'unclassified_member_days_reason_counts': dict(sorted(reasons.items()))})
    unknown = {'daily_unclassified_members': unknown_rows, 'unknown_listing_identities': unknown_listing,
               'reason_is_diagnostic_only_not_verified_historical_cause': True}
    return daily, aggregates, unknown

def run(spec):
    check(spec)
    if read(RESULT)['state'] != 'not_run':
        raise ValueError('preserve first source adjudication')
    local = Path(spec['native_directory'])
    if local.exists() or 'onedrive' in str(local).lower():
        raise ValueError('new native derivative directory required')
    local.mkdir(parents=True)
    started = time.monotonic()
    sequence, repaired, ledger = derive(spec)
    daily, monthly, unknown = measure(spec, sequence)
    if time.monotonic() - started > spec['max_seconds']:
        raise TimeoutError('bounded class correction/metadata coverage ceiling')
    for quarter, members in repaired.items():
        write(local / (quarter + '.source_view_corrected_members.json'), members)
    write(local / 'class_correction_ledger.json', {'rows': ledger})
    write(local / 'explicit_snapshot_sequence_targeted_corrected.json', sequence)
    write(local / 'declared_time_daily_coverage_targeted_corrected.json', {'daily': daily, 'lifetime_policy': spec['lifetime_policy']})
    write(local / 'remaining_source_member_gaps.json', unknown)
    check(spec)
    manifest = local / 'class_adjudication_manifest.json'
    write(manifest, {'files': {str(p): sha(p) for p in local.iterdir() if p.is_file()}})
    result = {'schema': 'regis.csrc.class_adjudication_result.v1', 'state': 'actual_targeted_visual_class_correction_and_metadata_coverage',
              'observed_utc': datetime.now(timezone.utc).isoformat(), 'elapsed_seconds': round(time.monotonic() - started, 3),
              'protocol_sha256': sha(PROTOCOL), 'implementation_sha256': sha(__file__),
              'source_pages_actually_viewed': 8, 'class_assignments_changed': len(ledger),
              'quarter_repair_counts': {q: x['class_repairs'] for q, x in repaired.items()},
              'source_rows_after': {q: x['source_rows'] for q, x in repaired.items()},
              'publication_markers_retained': len(sequence['snapshots']),
              'explicit_unknown_quarters': [x['quarter'] for x in sequence['snapshots'] if x['state'] == 'unknown_source_member_marker'],
              'partial_quarters': [x['quarter'] for x in sequence['snapshots'] if x.get('quarantined_rows')],
              'sessions': len(daily), 'unknown_listing_identities': len(unknown['unknown_listing_identities']),
              'first_nonzero_class_coverage_session': next((x['session'] for x in daily if x['known_source_classified_CS']), None),
              'all_known_active_CS_classified_sessions': sum(x['all_lifetime_known_CS_classified'] for x in daily),
              'monthly_coverage': monthly, 'native_snapshot_sequence': str(local / 'explicit_snapshot_sequence_targeted_corrected.json'),
              'native_daily_coverage': str(local / 'declared_time_daily_coverage_targeted_corrected.json'),
              'native_remaining_gaps': str(local / 'remaining_source_member_gaps.json'),
              'local_details': {'path': str(manifest), 'sha256': sha(manifest)},
              'first_capture_and_originals_preserved': True, 'price_arrays_read': False, 'sector_factor_computed': False,
              'full_A_census_certified': False, 'PIT_full_member_semantics_certified': False,
              'economic_runs': 0, 'model_admitted': False, 'full_goal_complete': False, 'limits': spec['limits']}
    write(RESULT, result)
    print(json.dumps({k: result[k] for k in ['state', 'elapsed_seconds', 'class_assignments_changed', 'quarter_repair_counts', 'explicit_unknown_quarters', 'sessions', 'all_known_active_CS_classified_sessions']}, ensure_ascii=False))

def verify(spec):
    check(spec)
    result = read(RESULT)
    if result['state'] != 'actual_targeted_visual_class_correction_and_metadata_coverage' or result['protocol_sha256'] != sha(PROTOCOL) or result['implementation_sha256'] != sha(__file__):
        raise ValueError('first class adjudication identity changed')
    if sha(result['local_details']['path']) != result['local_details']['sha256']:
        raise ValueError('native class adjudication manifest changed')
    for path, digest in read(result['local_details']['path'])['files'].items():
        if sha(path) != digest:
            raise ValueError('native class adjudication derivative changed')
    sequence, repaired, ledger = derive(spec)
    daily, monthly, unknown = measure(spec, sequence)
    local = Path(result['local_details']['path']).parent
    if read(result['native_snapshot_sequence']) != sequence or read(local / 'class_correction_ledger.json')['rows'] != ledger:
        raise ValueError('only121 source-viewed class correction replay failed')
    for quarter, members in repaired.items():
        if read(local / (quarter + '.source_view_corrected_members.json')) != members:
            raise ValueError('corrected quarter lineage replay failed')
    if read(result['native_daily_coverage'])['daily'] != daily or result['monthly_coverage'] != monthly or read(result['native_remaining_gaps']) != unknown:
        raise ValueError('declared-time missing-members/coverage replay failed')
    if any(result[k] for k in ['price_arrays_read', 'sector_factor_computed', 'full_A_census_certified', 'PIT_full_member_semantics_certified', 'economic_runs', 'model_admitted', 'full_goal_complete']):
        raise ValueError('source correction scope escalated')
    print(json.dumps({'exact121_source_page_corrections_replayed': True, 'same_denominator_and_32_publication_markers': True, 'missing_members_remain_explicit': True, 'economic_runs': 0}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.run == args.verify:
        raise SystemExit('Select exactly one mode')
    spec = read(PROTOCOL)
    run(spec) if args.run else verify(spec)
