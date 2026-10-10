"""Read-only, source-bound filing observations. No historical registry or trade gate.

Observations identify a security in a particular document. Listing dates are facts,
not continuity intervals; issuer/manager names are not permanent security IDs.
The PDF byte hash is a reproducibility check, not a tamper-proof signature.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re


class IdentityEvidenceError(ValueError):
    """Errors contain safe codes only, never quote values or document contents."""


def require(ok, code):
    if not ok:
        raise IdentityEvidenceError(code)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compact(value):
    require(isinstance(value, str), 'invalid_text')
    return re.sub(r'\s+', '', value)


def strict_date(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value), 'date_precision_invalid')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise IdentityEvidenceError('invalid_date') from None


def strict_utc(value):
    require(isinstance(value, str), 'clock_missing')
    try:
        v = datetime.fromisoformat(value)
    except ValueError:
        raise IdentityEvidenceError('clock_invalid') from None
    require(v.tzinfo is not None and v.utcoffset() is not None, 'clock_timezone_missing')
    return v.astimezone(timezone.utc)


def decode_claim(claim, text):
    """Check a short labelled fragment in a specific PDF page, then its value."""
    fragment = compact(claim['fragment'])
    literal = compact(claim['literal'])
    anchor = compact(claim['anchor'])
    require(fragment and anchor and literal and fragment in compact(text), 'labelled_fragment_missing')
    require(anchor in fragment and literal in fragment, 'claim_fragment_unbound')
    codec, value = claim['codec'], claim['value']
    if codec == 'text':
        decoded = literal
        require(compact(value) == decoded, 'text_value_mismatch')
        return value
    if codec == 'cn_date':
        match = re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日', literal)
        require(match is not None, 'date_literal_invalid')
        try:
            decoded = date(*map(int, match.groups())).isoformat()
        except ValueError:
            raise IdentityEvidenceError('date_literal_invalid') from None
        require(strict_date(value).isoformat() == decoded, 'date_value_mismatch')
    elif codec == 'cn_year':
        match = re.fullmatch(r'(\d{4})年', literal)
        require(match is not None and value == match.group(1) and 1900 <= int(value) <= 2100,
                'year_value_mismatch')
        decoded = value
    elif codec == 'exchange':
        require(literal in {'上海证券交易所', '深圳证券交易所'}, 'exchange_literal_invalid')
        decoded = {'上海证券交易所': 'XSHG', '深圳证券交易所': 'XSHE'}[literal]
        require(value == decoded, 'exchange_value_mismatch')
    elif codec == 'currency':
        require(literal == '人民币' and value == 'CNY', 'currency_value_mismatch')
        decoded = value
    elif codec == 'etf_name':
        require('交易型开放式指数证券投资基金' in literal and value == 'ETF', 'asset_class_unproved')
        decoded = value
    elif codec == 'a_share_class':
        require(literal == '普通股A股' and value == 'A_SHARE', 'asset_class_unproved')
        decoded = value
    elif codec == 'share_label':
        require(literal == '份' and value == 'fund_units_in_report', 'report_share_label_unproved')
        decoded = value
    else:
        raise IdentityEvidenceError('unsupported_claim_codec')
    return decoded


@dataclass(frozen=True)
class Observation:
    observation_id: str
    document_id: str
    document_sha256: str
    observed_at: str
    provider_key: str
    exchange: str
    security_code: str
    asset_class: str
    security_name: str
    manager_name: str | None
    as_of_date: str | None
    as_of_basis: str
    listing_date: str | None
    contract_date: str | None
    trading_currency: str | None
    operation_mode: str | None
    report_share_label: str | None
    report_year: str | None = None
    issuer_name: str | None = None
    report_money_unit: str | None = None
    # These cannot be supplied by callers/JSON to promote a filing observation.
    @property
    def permanent_id(self): return None
    @property
    def valid_interval(self): return None
    @property
    def historical_available_at(self): return None
    @property
    def provider_numeric_units(self): return None


def verified_catalog(root, index, sources, page_reader=None):
    """Only successful, hash-bound PDF bodies can support an observation.

    page_reader is an injected pure reader for synthetic tests; production uses
    the local PDF parser. Reading a failed body is never evidence of identity.
    """
    require(index['schema'] == 'regis.identity_observations.v1', 'index_schema_invalid')
    require(len(sources) == len({x['id'] for x in sources}), 'duplicate_source_id')
    source_map = {x['id']: x for x in sources}
    docs, ids, pages, observations, page_cache = {}, set(), {}, [], {}
    allowed = {'code','exchange','security_name','asset_class','manager_name','as_of_date',
               'listing_date','contract_date','trading_currency','operation_mode','report_share_label',
               'document_date','sent_date','report_start','report_end','report_year','issuer_name','report_money_unit'}
    required = {'code','exchange','security_name','asset_class'}
    for doc in index['documents']:
        ident = doc['id']
        require(ident not in docs, 'duplicate_document_id')
        require(ident in source_map, 'source_missing')
        src = source_map[ident]
        require(src.get('http_status') == 200 and src.get('complete_within_cap') is True
                and src.get('bytes', 0) > 0 and not src.get('error_type'), 'source_not_successful')
        require(doc['sha256'] == src['sha256'] and doc['url'] == src['url'], 'document_binding_mismatch')
        body = (Path(root)/src['body_path']).resolve()
        require(body.is_relative_to(Path(root).resolve()/'.codex-local/identity_sources_20261002'), 'body_custody_invalid')
        require(body.stat().st_size == src['bytes'] and digest(body) == src['sha256'], 'source_body_changed')
        require(body.read_bytes().startswith(b'%PDF-'), 'source_not_pdf')
        observed = strict_utc(src['ended_utc']).isoformat()
        require(strict_utc(src['started_utc']) <= strict_utc(observed), 'capture_clock_reversed')
        if page_reader is None:
            from pypdf import PdfReader
            reader = PdfReader(body)
            pages[ident] = lambda i, r=reader: r.pages[i-1].extract_text()
        else:
            pages[ident] = lambda i, d=ident: page_reader(d, i)
        docs[ident] = (doc, observed)
    for item in index['observations']:
        ident, docid = item['id'], item['document_id']
        require(ident not in ids and docid in docs, 'observation_id_invalid')
        ids.add(ident)
        # A historical claim cannot be smuggled into the observation index.
        forbidden = {'available_at','permanent_id','valid_from','valid_to','provider_numeric_units',
                     'historical_continuity_verified','economic_admitted'}
        require(not (forbidden & item.keys()), 'unsupported_authority_field')
        fields = item['claims']
        require(required <= fields.keys() <= allowed, 'observation_fields_invalid')
        values = {}
        used_pages = set()
        for field, claim in fields.items():
            require(type(claim['page']) is int and 1 <= claim['page'] <= 8, 'page_scope_invalid')
            used_pages.add(claim['page'])
            codec = {'exchange': {'exchange'}, 'asset_class': {'etf_name', 'a_share_class'},
                     'trading_currency': {'currency'}, 'report_share_label': {'share_label'},
                     'report_year': {'cn_year'}}.get(field)
            if codec is None:
                codec = {'cn_date'} if field.endswith('_date') or field in {'report_start','report_end'} else {'text'}
            require(claim['codec'] in codec, 'field_codec_mismatch')
            if field == 'trading_currency':
                require(compact(claim['anchor']).rstrip('：:') == '交易币种', 'trading_currency_label_missing')
            page_key = (docid, claim['page'])
            if page_key not in page_cache:
                page_cache[page_key] = pages[docid](claim['page'])
            values[field] = decode_claim(claim, page_cache[page_key])
        require(len(used_pages) <= 8, 'page_budget_exceeded')
        code, exchange, kind = values['code'], values['exchange'], values['asset_class']
        require(re.fullmatch(r'\d{6}', code) is not None, 'security_code_invalid')
        key = f'{code}.{exchange}'
        require(key == item['provider_key'] and key in index['selection'], 'provider_key_mismatch')
        require(kind == index['selection_roles'][key], 'share_class_mismatch')
        if kind == 'A_SHARE':
            # Bind all four identity fields to the single ordinary-share row,
            # so a preferred share or convertible code in the same filing
            # cannot borrow the ordinary-share class/name/exchange cells.
            fragments = {compact(fields[f]['fragment']) for f in ['code','exchange','security_name','asset_class']}
            exchange_cn = {'XSHG':'上海证券交易所','XSHE':'深圳证券交易所'}[exchange]
            expected_row = '普通股A股'+exchange_cn+compact(values['security_name'])+code
            require(len(fragments) == 1 and next(iter(fragments)).rstrip('-—') == expected_row,
                    'ordinary_share_row_unbound')
        require(item['as_of_basis'] in {'compiled_date','report_end','report_year'}, 'asof_basis_invalid')
        if item['as_of_basis'] == 'report_year':
            require('report_year' in values and 'as_of_date' not in values, 'report_year_precision_invalid')
            asof = None
        else:
            asof_field = {'compiled_date':'document_date','report_end':'report_end'}[item['as_of_basis']]
            require('as_of_date' in values and values.get(asof_field) == values['as_of_date'], 'asof_date_unbound')
            asof = strict_date(values['as_of_date'])
        listing, contract = values.get('listing_date'), values.get('contract_date')
        require(not listing or (strict_date(listing) <= asof if asof else strict_date(listing).year <= int(values['report_year'])), 'identity_dates_reversed')
        require(not contract or (strict_date(contract) <= asof if asof else strict_date(contract).year <= int(values['report_year'])), 'identity_dates_reversed')
        require(not (listing and contract) or strict_date(contract) <= strict_date(listing), 'identity_dates_reversed')
        doc, observed = docs[docid]
        require(asof <= strict_utc(observed).date() if asof else int(values['report_year']) <= strict_utc(observed).year, 'observation_from_future')
        if 'report_start' in values:
            require('report_end' in values and strict_date(values['report_start']) <= strict_date(values['report_end']), 'report_dates_reversed')
        observations.append(Observation(ident, docid, doc['sha256'], observed, key, exchange, code, kind,
            values['security_name'], values.get('manager_name'), values.get('as_of_date'), item['as_of_basis'],
            listing, contract, values.get('trading_currency'),
            values.get('operation_mode'), values.get('report_share_label'), values.get('report_year'),
            values.get('issuer_name'), values.get('report_money_unit')))
    return tuple(observations)


def join_identity_keys(keys, observations, *, selection_roles, snapshot_known_at, decision_at):
    """Join source key/date only; a candidate is NOT a historical identity bind.

    A decision clock governs which newly retrieved filing observations may be
    inspected. No as-of report date or listing date backdates knowledge.
    """
    floor, decision = strict_utc(snapshot_known_at), strict_utc(decision_at)
    require(len(keys) == len(set(keys)), 'duplicate_quote_key')
    require(len(observations) == len({o.observation_id for o in observations}), 'duplicate_observation_id')
    for o in observations:
        require(o.provider_key in selection_roles and o.asset_class == selection_roles[o.provider_key], 'share_class_mismatch')
        require(o.provider_key == f'{o.security_code}.{o.exchange}', 'observation_key_mismatch')
    counts = {k:{'source_rows':0,'visible_source_rows':0,'filing_candidate_rows':0,'candidate_observations':0,
                 'filing_asof_dates':[],'filing_report_years':[],'security_trading_currency':None,'report_share_label':None,
                 'security_names':[],'issuer_names':[],'manager_names':[],'report_money_units':[],
                 'operation_mode_conflict':False,'identity_anchor_conflict':False,'historical_identity_bound_rows':0,
                 'provider_units_certified_rows':0,'economic_admitted_rows':0,
                 'permanent_security_id':None,'continuous_valid_interval':None}
              for k in selection_roles}
    visible = tuple(o for o in observations if strict_utc(o.observed_at) <= decision)
    for key, value in keys:
        require(key in counts, 'unexpected_quote_key')
        day = strict_date(value)
        require(day <= floor.date(), 'sample_date_after_capture')
        counts[key]['source_rows'] += 1
        if decision < floor:
            continue
        counts[key]['visible_source_rows'] += 1
        candidates = [o for o in visible if o.provider_key == key]
        anchors = {(compact(o.security_name), o.exchange, o.asset_class, o.listing_date) for o in candidates}
        # A pre-listing row cannot even match a listing-anchor candidate.
        if candidates and len(anchors) == 1 and all(not o.listing_date or day >= strict_date(o.listing_date) for o in candidates):
            counts[key]['filing_candidate_rows'] += 1
    for key, record in counts.items():
        candidates = [o for o in visible if o.provider_key == key]
        if decision < floor:
            candidates = []
        record['candidate_observations'] = len(candidates)
        record['identity_anchor_conflict'] = len({(compact(o.security_name), o.exchange, o.asset_class, o.listing_date) for o in candidates}) > 1
        record['filing_asof_dates'] = sorted({o.as_of_date for o in candidates if o.as_of_date})
        record['filing_report_years'] = sorted({o.report_year for o in candidates if o.report_year})
        record['security_names'] = sorted({o.security_name for o in candidates})
        record['issuer_names'] = sorted({o.issuer_name for o in candidates if o.issuer_name})
        record['manager_names'] = sorted({o.manager_name for o in candidates if o.manager_name})
        record['report_money_units'] = sorted({o.report_money_unit for o in candidates if o.report_money_unit})
        currencies = {o.trading_currency for o in candidates if o.trading_currency}
        require(len(currencies) <= 1, 'currency_conflict')
        record['security_trading_currency'] = next(iter(currencies), None)
        modes = {o.operation_mode for o in candidates if o.operation_mode}
        record['operation_mode_conflict'] = len(modes) > 1
        labels = {o.report_share_label for o in candidates if o.report_share_label}
        require(len(labels) <= 1, 'report_share_label_conflict')
        record['report_share_label'] = next(iter(labels), None)
        # No field above certifies continuity, provider units or economic use.
    return counts


def require_historical_or_economic_identity(_observations):
    # Even edited caller flags cannot turn two old PDFs into a PIT registry.
    raise IdentityEvidenceError('historical_interval_units_vintage_and_execution_missing')
