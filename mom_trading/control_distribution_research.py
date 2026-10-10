"""Source-bound share/cash corrections of existing control paths, not trials."""
from copy import copy
import hashlib
import json

from . import distribution_settlement as settlement
from .emotion_pattern_research import PLAYBOOKS
from research.run_emotion_pattern_cash_settlement import original_candidate

ARTIFACTS = (
    ('research/data/implementation_settlement_v4/notices.json',
     'dcf6b2c3d7ad2423b3b129796ca0d9624e06d696340635ce407ed5962e3a7fce'),
    ('research/data/implementation_settlement_v1/600885-20220622.pdf',
     'f134c47307bfbfff335d9baa2fcbfc1035d5a231e8626c3b1aa205d55bcdfb9f'),
    ('research/data/implementation_settlement_v2/603121-implementation.pdf',
     '354d25fce19602f919d9732ae50cf147e059b2edb91ce78ad20ac3de10305f2f'),
    ('research/data/implementation_settlement_v2/603121-interim.pdf',
     '476a0101b58bed591c675e089ec6b5f3543886415cfa0a3ea15101b38c248f25'),
    ('research/data/implementation_settlement_v2/603319-implementation.pdf',
     '1efae3cad37b6ab3472811327a957c6429ca19effe0bf0b07ce9be04ce4d63ee'),
    ('research/data/implementation_settlement_v2/603319-interim.pdf',
     '470b62908a3a504312adebbe2255ded77adcec465e3f82845f6ba323f9adb67f'),
    ('research/data/implementation_settlement_v2/603730-implementation.pdf',
     'd9d9155100e08f71ae996f88122272c15fa389545a844d0430a3e0e86e9376ee'),
    ('research/data/implementation_settlement_v2/603730-interim.pdf',
     'f5f983b1cd7c787335758a3f5857b63b61f7ae7a58be9bc3b059f7cc5646bde0'),
    ('research/data/implementation_settlement_v3/300742-implementation.pdf',
     '551ba06f00e58b12d9675d031518fa4cace011d14e108da50346bce2733b310a'),
    ('research/data/implementation_settlement_v3/300742-annual.pdf',
     'c63c50cdbb259981ecc1a56f124bbc2e10edbd8633428db27a38a5adce2756c9'),
    ('research/data/implementation_settlement_v3/603809-implementation.pdf',
     'f120d5b6f99cc0a7feecf827c1728aa06d56a95bc80f0b3513dcc54b9aef667f'),
    ('research/data/implementation_settlement_v3/603809-interim.pdf',
     'f08e696cfea39cf5591eae02a117e96598a61ba1c1d67e51a4333fc4bb2f9c8d'),
    ('research/data/implementation_settlement_v4/000581-implementation.pdf',
     '3b0da830a2166a28ad0c91d838a793ca8c34368aeb322cd24f617190987d673d'),
)
SPEC = {'schema':'control_physical_distribution_repair.v1','accounting':settlement.SPEC,
        'selection':'every_original_unknown_path_matching_the_frozen_implementation_notice_all_three_costs',
        'baseline':'exact_original_price_cash_and_pre_action_orders_reproduced',
        'unchanged':'all_signal_keys_factors_groups_and_other_outcome_labels',
        'source_boundary':'issuer_PDF_mirror_and_original_daily_lineage_not_exchange_reference_or_actual_broker_credit',
        'new_blind_trial':False,'production_eligible':False,'generates_alert':False}


def path_sha(event):
    return hashlib.sha256(json.dumps(event,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def notice_inputs(paths, references):
    """The worker hashes the whole input list before and after execution."""
    document=json.loads(paths[11].read_text())
    if (document.get('schema')!='implementation_settlement_inputs.v1'
            or document.get('production_eligible') is not False or document.get('generates_alert') is not False
            or document.get('threshold_policy')!='share_unit_conversion_only_retain_existing_cash_nominal_policy'):
        raise ValueError('distribution_input_contract_invalid')
    notices, artifacts = {}, []
    for notice in document['notices']:
        code,day=notice['code'],notice['ex_date']
        if day in notices.setdefault(code,{}) or not notice.get('source_artifacts') or not notice.get('summary_source'):
            raise ValueError('distribution_input_identity_invalid')
        for source in notice['source_artifacts']:
            ref={k:source[k] for k in ('path','sha256')}
            if ref not in references[12:]:
                raise ValueError('distribution_notice_artifact_unbound')
            artifacts.append(ref)
        notices[code][day]=notice
    if not notices or {tuple(sorted(r.items())) for r in artifacts}!={tuple(sorted(r.items())) for r in references[12:]}:
        raise ValueError('distribution_notice_artifact_population_invalid')
    return notices


def repair_event(old_price, old_cash, market, evidence, notices):
    candidate=original_candidate(old_price,market)
    view=market
    if old_cash['track'] in {'no_emotion','same_entries_no_emotion_exit'}:
        allowed=next(p['states'][0] for p in PLAYBOOKS if p['id']==old_cash['playbook_id'])
        view=copy(market); view.states=dict.fromkeys(market.sessions,allowed)
        candidate['confirmed_state']=allowed
    fixed=settlement.replay_distribution_event(candidate,view,evidence,notices,
                                               bps=old_cash['slippage_bps'],source_complete=True)
    event={**old_cash,**fixed,'signal_emotion':old_cash['signal_emotion']}
    prefix=old_cash['execution_events']
    if event['execution_events'][:len(prefix)]!=prefix:
        raise ValueError('distribution_changed_pre_action_orders')
    _,actions=settlement.distribution_market_view(view,evidence,notices)
    if event['status']=='closed':
        relevant=[d for d in actions if event['signal_day']<d<=event['exit_day']]
        if (len(relevant)!=1 or (float(actions[relevant[0]]['share_ratio'])<=0
                and actions[relevant[0]].get('reference_policy')!='a_share_cash_excluding_repurchase')):
            event.update(status='unresolved',reason='independent_distribution_audit_scope_missing',
                         net_pnl_yuan=None,net_return_on_entry_cash=None,fixed_budget_return=None,
                         cash_settlement_complete=False)
        else:
            settlement.audit_closed_distribution(event,market,actions)
    if event['status']=='unresolved' and (event['net_pnl_yuan'] is not None or event['fixed_budget_return'] is not None):
        raise ValueError('distribution_unknown_return_filled')
    event['prior_cash_path_sha256']=path_sha(old_cash)
    return event


def checked_override(original, revised):
    """Input and full-population checks still use the untouched original row."""
    if (revised.get('prior_cash_path_sha256')!=path_sha(original)
            or revised.get('distribution_schema')!=settlement.SPEC['schema']
            or any(revised.get(k)!=original.get(k) for k in
                   ('track','playbook_id','playbook_name','signal_day','signal_emotion','signal_factors','code','slippage_bps'))
            or revised.get('production_eligible') is not False or revised.get('generates_alert') is not False):
        raise ValueError('distribution_override_identity_invalid')
    status=revised['status']
    if status=='closed':
        if revised.get('cash_settlement_complete') is not True or revised['remaining_shares']:
            raise ValueError('distribution_override_settlement_incomplete')
        value=revised['fixed_budget_return']
        if value is None or abs(value-revised['net_pnl_yuan']/20000)>1e-12:
            raise ValueError('distribution_override_value_invalid')
    elif status=='unfilled' and not revised['execution_events'] and revised['entry_day'] is None and not revised['remaining_shares']:
        value=0.
    elif status=='unresolved' and revised['net_pnl_yuan'] is None and revised['fixed_budget_return'] is None:
        value=None
    else:
        raise ValueError('distribution_override_outcome_invalid')
    return {'status':status,'reason':revised['reason'],'fixed_budget_return':value}


def run(output, **kwargs):
    from .control_increment_research import run as run_controls
    return run_controls(output,**kwargs,liquidity_repair=True,distribution_repair=True)
