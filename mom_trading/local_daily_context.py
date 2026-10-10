"""Date joins from a retained snapshot, never historical eligibility or orders.

Missing event keys stay unknown. A negative match means only not reported in a
present provider key. Action dates do not establish their historical publication.
"""
from bisect import bisect_left
from collections import Counter

from mom_trading.local_research_history import _require, _signature, _safe_dataset, _date, HistoryReader
from research.read_instruments_static import parse, date_int

FILES = ('instruments.pk', 'st_stock_days.h5', 'suspended_days.h5',
         'dividends.h5', 'split_factor.h5', 'ex_cum_factor.h5')
EVENTS = ('record', 'ex_dividend', 'payment', 'split', 'factor_change', 'unmatched_factor', 'anomaly')


def dates(values, np, missing=False, zero=False):
    _require(values.dtype.kind in 'iuf' and values.dtype.itemsize <= 8)
    _require(values.dtype.kind != 'f' or not len(values) or values.dtype.itemsize == 8)
    finite = np.isfinite(values)
    _require(not bool(np.any(np.isinf(values))))
    _require(bool(np.all(finite)) or missing)
    v = np.where(finite, values, 0)
    _require(bool(np.all((v == np.floor(v)) & (v >= 0) & (v <= 29991231000000))))
    v = v.astype(np.int64)
    _require(bool(np.all((v <= 100000000) | (v % 1000000 == 0))))
    v = np.where(v > 100000000, v // 1000000, v)
    for d in set(map(int, v)):
        if d == 0 and (missing or zero): continue
        _date(HistoryReader._iso(d))
    return list(map(int, v))


def unavailable():
    return {'schema': 'regis.daily_input_context.v1', 'status': 'unavailable', 'source_files': [],
            'PIT_certified': False, 'eligible': False, 'full_actions_certified': False}


def join(code, days, calendar, identity=None, states=None, actions=None):
    """No future-date event changes an earlier row; the 23-day lag is explicit."""
    states, actions = states or {}, actions or {}
    cal = sorted(calendar)
    output = {}
    in_scope = code.startswith(('000','001','002','003','300','301','600','601','603','605'))
    for day in days:
        i = bisect_left(cal, day)
        _require(i < len(cal) and cal[i] == day)
        low = cal[max(0, i - 23)]
        life = 'unknown'
        if identity:
            begin, end = identity
            if begin is not None and end is not None and begin > end:
                life = 'invalid_recorded_dates'
            elif begin is not None:
                life = ('before_recorded_listing' if day < begin else
                        'after_recorded_listing_end_unknown' if end is None else 'within_recorded_dates')
                if end is not None and day >= end:
                    life = 'at_recorded_end' if day == end else 'after_recorded_end'
        row = {'lifetime': life, 'trade_scope': 'main_or_chinext' if in_scope else 'outside_trade_scope',
               'PIT_certified': False, 'eligible': False}
        for kind in ('ST', 'halt'):
            values = states.get(kind)
            row[kind] = 'unknown' if values is None else 'reported' if day in values else 'not_reported'
        row['window'] = {'sessions_back': 23, 'complete_calendar_window': i >= 23,
                         **{k: None if actions.get(k) is None else sum(low <= d <= day for d in actions[k]) for k in EVENTS}}
        output[day] = row
    return output


class DailyContext:
    def __init__(self, inputs, stock):
        _require(inputs is not None and stock is not None and stock.instrument == 'stock')
        self.files = {k: inputs.files[k] for k in FILES}
        self.h5py, self.np, self.calendar = stock.h5py, stock.np, stock.calendar
        self.identities = {}
        self._check()
        path, signature, _ = self.files['instruments.pk']
        if signature is not None:
            with path.open('rb') as stream: raw = stream.read(32 * 1024**2 + 1)
            records, _ = parse(raw)
            for r in records:
                if r.get('type') != 'CS': continue
                key = r.get('order_book_id')
                _require(isinstance(key, str) and key not in self.identities)
                self.identities[key] = (date_int(r.get('listed_date')), date_int(r.get('de_listed_date')))
            _require(len(self.identities) <= 8000)
        self._check()

    def _check(self):
        for path, signature, _ in self.files.values():
            if signature is not None:
                _require(not path.is_symlink() and _signature(path) == signature)

    def _read(self, filename, key, fields=None):
        path, signature, _ = self.files[filename]
        if signature is None: return None
        with self.h5py.File(path, 'r', rdcc_nbytes=1048576) as f:
            if key not in f: return None
            ds = _safe_dataset(f, key, self.h5py, self.np)
            if fields is not None:
                _require(ds.dtype.names is not None and set(fields) <= set(ds.dtype.names))
                return ds.fields(fields)[:]
            return ds[:]

    def read(self, code, days):
        self._check()
        key = code + ('.XSHG' if code.startswith('6') else '.XSHE')
        np = self.np
        states = {}
        for kind, file in [('ST','st_stock_days.h5'),('halt','suspended_days.h5')]:
            raw = self._read(file, key)
            if raw is None: states[kind] = None
            else:
                values = dates(raw, np)
                # Descending sets are legitimate. Duplicate dates remain a
                # malformed source rather than silently changing coverage.
                _require(len(set(values)) == len(values))
                _require(all(d in self.calendar for d in values))
                states[kind] = set(values)
        div = self._read('dividends.h5', key, ['announcement_date','book_closure_date','ex_dividend_date','payable_date'])
        split = self._read('split_factor.h5', key, ['ex_date'])
        factor = self._read('ex_cum_factor.h5', key, ['start_date','ex_cum_factor'])
        actions = {k: None for k in EVENTS}
        anomalies = []
        if div is not None:
            ann, book, ex, pay = [dates(div[f], np, missing=f=='announcement_date') for f in
                                  ('announcement_date','book_closure_date','ex_dividend_date','payable_date')]
            actions.update(record=book, ex_dividend=ex, payment=pay)
            ex_counts = Counter(ex)
            for a,b,e,p in zip(ann,book,ex,pay):
                if not a or not (a <= b < e <= p) or ex_counts[e] > 1:
                    # Anchor only to events already observed, not future payment
                    # or announcement dates used to select historic candidates.
                    anomalies.append(min(b,e))
        if split is not None: actions['split'] = dates(split['ex_date'], np)
        if factor is not None:
            ds = dates(factor['start_date'], np, zero=True)
            values = factor['ex_cum_factor']
            _require(bool(np.all(np.isfinite(values) & (values > 0))))
            _require(all(a < b for a,b in zip(ds, ds[1:])))
            actions['factor_change'] = [d for i,d in enumerate(ds) if i and values[i] != values[i-1]]
        if all(actions[k] is not None for k in ('ex_dividend','split','factor_change')):
            known = set(actions['ex_dividend']) | set(actions['split'])
            actions['unmatched_factor'] = [d for d in actions['factor_change'] if d not in known]
        if div is not None and split is not None:
            anomalies += [d for d,n in Counter(actions['split']).items() if n > 1]
            actions['anomaly'] = anomalies
        rows = join(code, days, self.calendar, self.identities.get(key), states, actions)
        self._check()
        meta = {**unavailable(), 'status': 'retrospective_connected' if all(s is not None for _,s,_ in self.files.values()) else 'partial',
                'source_files': [{'name': name, 'sha256': sha} for name,(_,sig,sha) in self.files.items() if sig is not None]}
        return rows, meta
