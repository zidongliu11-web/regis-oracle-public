"""Connect registered factor inputs to retained file identity and old audit metadata.

No HDF5/pickle decoding, prices, accounts, downloads or research calculations.
Only the explicit personal preview installs this endpoint.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from flask import current_app, request
from mom_trading.local_research_history import (
    RECEIPT, RECEIPT_SHA, _signature, _verified, local_request, local_response,
)
from mom_trading.research_catalogue import read_catalogue

PINS = {
    RECEIPT: RECEIPT_SHA,
    'research/RQ_STOCK_INVENTORY_PROTOCOL_20261002.json': 'd8c6469faab3bb6b308896d5b0910c10a18ec3845a6e158bd9a4af5623c5ab29',
    'research/RQ_STOCK_INVENTORY_RESULT_20261002.json': 'c594836f4e3a388ced1d9983bf62deeff7b17fdc500e9b6df09e267d67f4159b',
    'research/RQ_STOCK_QUALITY_PROTOCOL_20261002.json': '8a6e01b5f5e3de5a141107f7b23748c67c9797ccde64f9cfa439a615221a7d5a',
    'research/RQ_STOCK_QUALITY_RESULT_20261002.json': '28588674786f73e7fbca07a6a7e8858c8d8bb65a7f23c8bd5a0b7c4a7d9d5b0f',
}
FILES = ('stocks.h5', 'trading_dates.npy', 'instruments.pk', 'st_stock_days.h5',
         'suspended_days.h5', 'dividends.h5', 'ex_cum_factor.h5', 'split_factor.h5',
         'share_transformation.json')
DEPENDENCIES = {
    'stock_daily': ('stocks.h5',),
    'exchange_calendar': ('trading_dates.npy',),
    'PIT_universe_L_D_P_BSE': ('instruments.pk',),
    'PIT_ST': ('st_stock_days.h5',),
    'PIT_suspension': ('suspended_days.h5',),
    'adjustment_and_actions': ('dividends.h5', 'ex_cum_factor.h5', 'split_factor.h5', 'share_transformation.json'),
}


def audit_documents(root):
    docs = {}
    for name, digest in PINS.items():
        raw = (Path(root) / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('audit_binding_invalid')
        docs[name] = json.loads(raw)
    receipt, invp, inventory, qualityp, quality = docs.values()
    if not (receipt['state'] == 'captured' and receipt['archive_complete'] is True
            and inventory['state'] == quality['state'] == 'computed'
            and inventory['protocol_sha256'] == PINS[list(PINS)[1]]
            and inventory['acquisition_receipt_sha256'] == RECEIPT_SHA
            and quality['protocol_sha256'] == PINS[list(PINS)[3]]
            and all(qualityp['input_hashes'][p] == PINS[p] for p in list(PINS)[:3])):
        raise ValueError('audit_chain_invalid')
    return receipt, inventory, quality


class FactorInputs:
    def __init__(self, root, stock):
        self.root = Path(root).resolve()
        receipt, _, _ = audit_documents(self.root)
        directory = Path(receipt['raw_directory']).resolve()
        if directory.is_relative_to(self.root) or any('onedrive' in p.lower() for p in directory.parts):
            raise ValueError('native_directory_required')
        records = {r['name']: r for r in receipt['retained']}
        if set(records) != set(FILES):
            raise ValueError('unexpected_manifest')
        self.files = {}
        self.verified_at = datetime.now(timezone.utc).isoformat()
        for name in FILES:
            path = directory / name
            record = records[name]
            signature = None
            try:
                if name in ('stocks.h5', 'trading_dates.npy'):
                    if stock is None or stock.instrument != 'stock':
                        raise ValueError('reader_unavailable')
                    reader_path = stock.path if name == 'stocks.h5' else stock.calendar_path
                    signature = stock.signature if name == 'stocks.h5' else stock.calendar_signature
                    if (reader_path != path or path.is_symlink() or _signature(path) != signature
                            or signature[0] != record['bytes']
                            or (name == 'stocks.h5' and stock.sha != record['sha256'])):
                        raise ValueError('reader_source_changed')
                else:
                    signature = _verified(path, record['sha256'], record['bytes'])
            except (OSError, ValueError, TypeError):
                signature = None
            self.files[name] = (path, signature, record['sha256'])

    def read(self):
        receipt, inventory, quality = audit_documents(self.root)
        catalogue = read_catalogue(self.root)
        file_states = {}
        for name, (path, signature, digest) in self.files.items():
            try:
                matched = signature is not None and not path.is_symlink() and _signature(path) == signature
            except OSError:
                matched = False
            file_states[name] = {'name': name, 'state': 'matched' if matched else 'unavailable_or_changed',
                                 'expected_sha256': digest}
        roles = sorted({role for factor in catalogue['factors'] for role in factor['required_inputs']})
        rows = []
        for role in roles:
            files = [file_states[name] for name in DEPENDENCIES.get(role, ())]
            current = ('files_matched' if all(f['state'] == 'matched' for f in files)
                       else 'partial_or_unavailable') if files else 'not_connected'
            audit, gap = self.describe(role, receipt, inventory, quality)
            if role == 'rights_and_retention':
                current = 'scope_limited'
            rows.append({'id': role, 'current_state': current, 'files': files, 'audit': audit,
                         'limitation': gap, 'historical_PIT_certified': False, 'research_admitted': False})
        return {'schema': 'regis.local_factor_inputs.v1', 'status': 'available',
                'checked_at': datetime.now(timezone.utc).isoformat(), 'file_verified_at': self.verified_at,
                'audit_recorded_at': quality['ended_utc'], 'capture_recorded_at': receipt['ended_utc'],
                'catalogue_binding': catalogue['source_bindings'][0],
                'source_bindings': [{'path': p, 'sha256': h} for p, h in PINS.items()],
                'inputs': rows, 'input_count': len(rows), 'new_economic_runs': 0,
                'price_rows_read_by_this_request': 0, 'new_source_requests': 0,
                'historical_feature_PIT_certified': False, 'model_admitted': False,
                'note': '连接状态只核对文件身份；审计是旧记录。重读不重新计算或哈希原件，源文件变化后须重启本机预览重新校验。'}

    @staticmethod
    def describe(role, receipt, inv, qc):
        p, c = inv['prices'], qc['counts']
        if role == 'stock_daily':
            return (f"旧审计：窗口内 {p['counts']['selected_datasets']} 个代码、{p['selected_rows']} 条日线，日期 {p['earliest_selected_date']}–{p['latest_selected_date']}。",
                    '查询需同时通过日线页的价格文件和日历校验；不是每日完整全A，也未认证逐行当时可得时间、单位和可成交性。')
        if role == 'exchange_calendar':
            return (f"旧审计窗口 {inv['window']['start_date']}–{inv['window']['end_date']}，{inv['window']['calendar_sessions']} 个交易日。",
                    '日历覆盖仅限旧审计窗口，不证明所有代码每个交易日都有有效行情。')
        if role == 'PIT_universe_L_D_P_BSE':
            return (f"旧审计股票身份 {inv['identities']['CS_records']} 项，上市日期未知 {inv['identities']['unknown_listing_dates']} 项。",
                    '保存的是身份快照及生命周期；缺历史公布时点和北交所完整覆盖，当前状态不能替代历史股票池。')
        if role in ('PIT_ST', 'PIT_suspension'):
            name = DEPENDENCIES[role][0]; e = qc['event_sets'][name]
            detail = f"旧审计：{e['keys']} 个键、窗口内 {e['selected_unique_events']} 个日期事件、重复 {e['duplicate_rows']}、严格倒序集合 {e['keys_strictly_descending']}。"
            if role == 'PIT_suspension':
                detail += f" {c['priced_keys_without_halt_dataset']} 个有价格的代码缺停牌键；报告停牌日仍有 {c['observed_rows_on_reported_halt']} 条日线。"
            return detail, '缺键或空集合不能独立证明无事件；倒序集合不等于重复。缺逐条历史公告时点，停牌日有日线也不代表可成交。'
        if role == 'adjustment_and_actions':
            return '旧恢复记录留存了分红、复权系数、拆合和代码转换四类文件。', '文件身份匹配不代表完整公司行动链、可用复权价格或历史公布时点已核验；不可据此自动调整回测。'
        if role == 'rights_and_retention':
            return '沿用封存来源审计的个人本机研究与格式核验范围。', '此页面只显示元数据；没有新增授权结论。原始数据不发给AI、Git或云，未来其他用途须单独核实。'
        gaps = {'historical_limits': '尚未接入经过核验的历史涨跌停价格及适用规则。',
                'historical_publication_evidence': '已知快照留存时间为 '+receipt['ended_utc']+'；缺逐条历史首次可得时间。',
                'PIT_sector_membership': '尚未接入带生效区间和公布时点的历史行业/概念成员。',
                'attention_event_truth': '尚未接入可回溯的关注事件原始证据及当时可得时间。'}
        return '未有经核验的已连接输入。', gaps.get(role, '未知输入角色，不能推定可用。')


def install(app, root):
    try:
        reader = FactorInputs(root, app.extensions.get('local_research_history'))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        reader = None
    app.extensions['local_factor_inputs'] = reader

    @app.get('/api/local-research/factor-inputs')
    def factor_inputs():
        if not current_app.config.get('LOCAL_RESEARCH_HISTORY_ENABLED') or not local_request():
            return local_response({'status': 'disabled', 'message': '仅供已启用的本机研究页面查看。'}, 403)
        if request.args:
            return local_response({'status': 'invalid_request', 'message': '此入口只核对已有输入，不接受采集或回算参数。'}, 400)
        try:
            if reader is None:
                raise ValueError('source_unavailable')
            payload = reader.read()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return local_response({'status': 'unavailable', 'message': '输入审计来源未通过校验；登记仍可单独查看。修复原件后重启本机预览。'}, 503)
        return local_response(payload, 200)
