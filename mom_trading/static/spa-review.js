(function(RegisSpa){
'use strict';
if (!RegisSpa?.core) throw new Error('Regis SPA core must load before review module');
const { $, fn, esc } = RegisSpa.core;

const feeStatusLabel = status => ({actual:'券商实录费用', estimated:'含模型估费 · 非实际净收益', inconsistent:'金额不一致 · 待核验', missing:'成交或费用缺项 · 待核验', no_fills:'无卖出成交'}[status] || '待核验');
const reviewMoney = value => typeof value === 'number' && Number.isFinite(value) ? `¥${fn(value, 2)}` : '待核验';
function ledgerReceipt(review) {
  const receipt = review?.manual_ledger_snapshot;
  if (!receipt?.combined_sha256) return '账本读取版本未提供，不推定成交、费用和计划已按同一版本核对。';
  return `账本核对版本 ${receipt.combined_sha256.slice(0, 12)} · 最近记录 ${receipt.recorded_as_of || '未记录'}。${receipt.scope || ''} ${receipt.valuation_note || ''}`;
}
function fillIdentity(row) {
  return `买入记录 #${row.position_id ?? '未记录'} → 卖出事件 #${row.sell_event_id ?? '未记录'}；入场提醒 ${row.entry_alert_id ?? '未关联'} / 卖出提醒 ${row.exit_alert_id ?? '未关联'}`;
}
function feeRevisionLabel(row) {
  if (!row.buy_fee_fact && !row.sell_fee_fact) return '';
  return `后补实费：买入版本 ${row.buy_fee_fact?.id ?? '未更正'} / 卖出版本 ${row.sell_fee_fact?.id ?? '未更正'}；原记录扣费后 ${reviewMoney(row.recorded_costs?.net_pnl)}。按最新实费重述，不是当时已知证据。`;
}
function renderPlanReview(review) {
  if (!Array.isArray(review?.plan_review)) return '<p>计划与成交比较尚未读取，不把提醒编号当作完整核验。</p>';
  return `<section style="padding:12px 14px;min-width:0"><h4>原计划 → 实际成交偏差</h4><p>覆盖所选日期口径的买入和卖出；数量按同一原提醒的全部已关联成交累计。符合价格数量不等于满足全部情绪、板块、取消条件或策略准入。</p>${review.plan_review.map(row => {
    const comparison = row.comparison || {}, action = comparison.snapshot?.action_plan?.trade_plan?.action;
    return `<article style="border:1px solid var(--border);padding:12px;margin:10px 0;overflow-wrap:anywhere"><strong>${row.fill_id.startsWith('buy:') ? '买入' : '卖出'} · ${esc(row.name || row.code)} · ${esc(row.shares)}股 @ ${reviewMoney(row.price)}</strong><p>${esc(comparison.label || '计划核对不可用')}</p>${action ? `<p>原计划 ${esc(comparison.planned_shares)}股 / 限价 ${reviewMoney(comparison.planned_limit_price)}；全部关联 ${esc(comparison.linked_total_shares)}股。<br>期限 ${esc(action.valid_until)}；取消条件 ${esc(action.cancel_condition)}。</p><details><summary>保存的证据与核对历史</summary><p>原提醒 #${esc(comparison.alert_id)}；计划指纹 ${esc(comparison.plan_sha256)}。${esc(comparison.unverified_conditions)}</p>${(row.history || []).map(event => `<p>版本 ${esc(event.id)} · ${esc(event.recorded_at)} · ${esc(event.note)}</p>`).join('')}</details>` : ''}<small>${esc(row.fill_id)} · 录入 ${esc(row.recorded_at)} / 实际成交 ${esc(row.executed_at || '未补记')}</small></article>`;
  }).join('') || '<p>所选日期口径暂无已确定归属的原始成交；未补实际日期的缺项不算作零成交结论。</p>'}</section>`;
}
function renderFillReview(review) {
  const fills = review?.fill_review;
  if (!fills) return '<p role="status">逐笔成交核验暂不可用，不把旧汇总当作成本已核验。</p>';
  const rows = (fills.items || []).map(row => `<tr>
    <td>${esc(row.name || row.code || '名称待核验')}<br><small>${esc(row.code || '')}<br>录入 ${esc(row.recorded_at || '未记录')}<br>成交 ${esc(row.execution_time_fact?.executed_at || '未补记')} · 用户填报</small></td>
    <td>${esc(row.shares ?? '—')} 股<br>${reviewMoney(row.price)}</td>
    <td>买 ${reviewMoney(row.allocated_buy_fee_yuan)}<br>卖 ${reviewMoney(row.sell_fee_yuan)}<br><small>${esc(feeStatusLabel(row.cost_status))}</small>${feeRevisionLabel(row) ? `<br><small>${esc(feeRevisionLabel(row))}</small>` : ''}</td>
    <td>${reviewMoney(row.net_pnl_yuan)}</td>
    <td style="max-width:300px;white-space:normal;overflow-wrap:anywhere">${esc(fillIdentity(row))}<br><small>${esc(row.reason || (row.entry_alert_id ? '编号仅供追溯，策略资格须另核验' : '未绑定入场计划，不归因为系统战法'))}</small></td></tr>`).join('');
  return `<section class="manual-fill-review" style="padding:12px 14px;min-width:0"><h4>实际成交 → 费用 → 计划追溯</h4><p data-manual-ledger-receipt style="overflow-wrap:anywhere">${esc(ledgerReceipt(review))}</p><p>${esc(feeStatusLabel(fills.cost_status))} · ${fills.fill_count} 笔卖出 · 实录 ${fills.actual_fee_fill_count} / 含估费 ${fills.estimated_fee_fill_count} / 待核验 ${fills.unverified_fill_count}</p>
    ${rows ? `<div style="overflow:auto;max-height:440px"><table style="width:100%;min-width:680px"><thead><tr><th>个股 / 两种时间（上海）</th><th>实际成交</th><th>费用来源</th><th>扣费后</th><th>记录关联（不是策略名称）</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p>所选口径下没有已确定属于这一天的卖出记录；时间缺口另列。</p>'}
    ${fills.unassigned_fills?.length ? `<details open><summary>实际成交日期待补记 ${fills.unassigned_fills.length} 笔 · 不算作零收益</summary><ul>${fills.unassigned_fills.map(row => `<li>${esc(row.name || row.fill_id)} · 录入 ${esc(row.recorded_at || '未知')} · ${esc(row.reason)}</li>`).join('')}</ul></details>` : ''}<p>${esc(fills.note || '')}</p><p>${esc(fills.fee_revision_note || '')}</p></section>${renderPlanReview(review)}`;
}

function entryAttributionLabel(p) {
  return p?.entry_origin === 'production_strategy' && p.entry_strategy_id ? `生产策略入场 · ${p.entry_strategy_id}` : p?.entry_origin === 'manual_strategy_observation' ? '冻结研究规则入场 · 非正式策略' : p?.entry_origin === 'research_observation' ? '研究观察入场' : p?.entry_origin === 'manual_position_snapshot' ? '导入已有持仓 · 原始买入未记录' : p?.entry_origin === 'manual_record' || p?.entry_origin === 'manual_ticket' ? '手工录入' : '来源未记录';
}

function reviewAlertPlan(alert) {
  const tradePlan = alert?.action_plan?.trade_plan || {};
  const action = tradePlan.action || {};
  const allocation = alert?.action_plan?.allocation || {};
  const shares = Number(action.shares);
  const limitPrice = Number(action.limit_price);
  const estimatedCost = Number(action.estimated_cost);
  const rawTargetShares = action.target_shares ?? allocation.target_shares;
  const targetShares = rawTargetShares === null || rawTargetShares === undefined
    || rawTargetShares === '' ? null : Number(rawTargetShares);
  return {
    shares: Number.isInteger(shares) && shares > 0 ? shares : null,
    limit_price: Number.isFinite(limitPrice) && limitPrice > 0 ? limitPrice : null,
    estimated_cost: Number.isFinite(estimatedCost) && estimatedCost >= 0 ? estimatedCost : null,
    valid_until: action.valid_until || alert?.valid_until || null,
    invalidation_condition: action.invalidation_condition || action.cancel_condition || null,
    playbook_id: action.playbook_id || tradePlan.playbook_id || alert?.playbook_id || null,
    strategy_id: action.strategy_id || tradePlan.strategy_id || alert?.strategy_id || null,
    strategy_version: action.strategy_version || tradePlan.strategy_version || alert?.strategy_version || null,
    evidence_grade: action.evidence_grade || tradePlan.evidence_grade || alert?.evidence_grade || null,
    as_of: action.as_of || tradePlan.as_of || alert?.as_of || null,
    target_weight_pct: Number.isFinite(Number(allocation.target_weight_pct))
      ? Number(allocation.target_weight_pct) : null,
    market_target_exposure_pct: Number.isFinite(Number(allocation.market_target_exposure_pct))
      ? Number(allocation.market_target_exposure_pct) : null,
    target_shares: Number.isInteger(targetShares) && targetShares >= 0 ? targetShares : null,
  };
}

function normalizeReviewData(raw) {
  if (!raw || typeof raw !== 'object') return raw || null;
  if (raw._review_normalized === true) return raw;
  const rawAlerts = Array.isArray(raw.alerts) ? raw.alerts : [];
  const actionable = [], observations = [];
  rawAlerts.forEach(item => {
    const plan = reviewAlertPlan(item);
    const origin = String(item?.origin || '');
    const completePlan = plan.shares !== null && plan.limit_price !== null
      && plan.estimated_cost !== null && Boolean(plan.valid_until)
      && Boolean(plan.invalidation_condition);
    const strictBuyIdentity = ['production_strategy', 'strict_route_manual_review'].includes(origin)
      && Boolean(plan.playbook_id) && Boolean(plan.strategy_id)
      && Boolean(plan.strategy_version) && Boolean(plan.evidence_grade) && Boolean(plan.as_of);
    const admitted = item?.action !== 'buy' || (completePlan && strictBuyIdentity);
    const enriched = {...item, review_plan:plan};
    (admitted ? actionable : observations).push(enriched);
  });
  const statuses = ['triggered', 'confirmed', 'dismissed', 'superseded', 'expired'];
  const counts = {total:actionable.length};
  statuses.forEach(status => { counts[status] = actionable.filter(item => item.status === status).length; });
  const eventTime = item => String(item.created_at || item.sold_at || item.time || '');
  const retainedTimeline = (Array.isArray(raw.timeline) ? raw.timeline : [])
    .filter(item => item?.type !== 'alert' && item?.type !== 'research_observation');
  const mapped = actionable.map(item => ({
    type:'alert', kind:item.action, status:item.status,
    time:eventTime(item).slice(11, 16), created_at:eventTime(item),
    label:item.name || item.code || '提醒',
    origin:item.origin || null,
    timing_interpretation:item.timing_interpretation || null,
  })).concat(observations.map(item => ({
    type:'research_observation', kind:item.action, status:item.status,
    time:eventTime(item).slice(11, 16), created_at:eventTime(item),
    label:item.name || item.code || '研究观察',
  })));
  const timeline = retainedTimeline.concat(mapped).sort((left, right) => {
    const l = String(left.created_at || left.sold_at || left.time || '');
    const r = String(right.created_at || right.sold_at || right.time || '');
    return l.localeCompare(r);
  });
  return {
    ...raw, _review_normalized:true,
    raw_alerts:rawAlerts, raw_alert_counts:raw.alert_counts || {},
    alerts:actionable, alert_counts:counts,
    unresolved:actionable.filter(item => item.status === 'triggered'),
    research_observations:observations,
    research_observation_counts:{total:observations.length},
    suppressed_buy_alert_count:observations.filter(item => item.action === 'buy').length,
    timeline,
  };
}

function buildReviewReport(raw) {
  const d = normalizeReviewData(raw) || {};
  const L = [], perf = d.execution_performance || {};
  const stL = { triggered:'未处理', confirmed:'已确认', dismissed:'已忽略', superseded:'已过期', expired:'已过期' };
  L.push(`Regis Oracle · 每日复盘 ${d.date || ''}`, `市场状态：${d.market_state || ''}`, '');
  L.push(`复盘口径：${d.time_basis === 'execution' ? '用户补记的实际成交日期' : '录入日期，不等于实际成交日期'}；下文“今日”均指 ${d.date || '所选日期'}。`, '');
  L.push(ledgerReceipt(d), '');
  const ac = d.alert_counts || {};
  L.push(`今日可操作提醒 ${ac.total || 0} 次（未处理 ${ac.triggered || 0} · 已忽略 ${ac.dismissed || 0} · 已确认 ${ac.confirmed || 0}）`);
  (d.alerts || []).forEach(a => L.push(`  ${(a.created_at || '').slice(11,16)} ${a.name} ${a.action === 'buy' ? '买入' : '卖出'}提醒 · ${stL[a.status] || a.status}${a.window_label ? `（${a.window_label}）` : ''}`));
  L.push(`研究观察 ${Number(d.research_observation_counts?.total || 0)} 条（不计为买卖提醒）`);
  (d.research_observations || []).forEach(a => {
    const plan = a.review_plan || {};
    L.push(`  ${(a.created_at || '').slice(11,16)} ${a.name} · ${plan.shares ? `旧链路曾计算 ${plan.shares} 股` : '无完整数量'} · 未获情绪→板块→战法统一授权`);
  });
  L.push('', `今日实际买入 ${d.bought_count || 0} 笔`);
  (d.bought_today || []).forEach(p => L.push(`  ${p.name} ${p.initial_shares ?? '未记录'}股 @ ¥${p.price || 0} · 录入 ${p.created_at || '未记录'} / 实际成交 ${p.execution_time_fact?.executed_at || '未补记'} · ${entryAttributionLabel(p)}${p.current ? `（现价 ¥${p.current}${p.pnl != null ? `，浮盈 ¥${p.pnl.toFixed(2)}` : ''}）` : ''}`));
  L.push('', `今日导入已有持仓 ${d.position_snapshot_count || 0} 笔（不计为买入）`);
  (d.position_snapshots_today || []).forEach(p => L.push(`  ${(p.created_at || '').slice(11,16)} ${p.name} ${p.shares}股 · 平均成本 ¥${p.price || 0} · 原始买入日未记录`));
  L.push('', `今日卖出 ${d.fill_review?.fill_count ?? d.sell_event_count ?? '待核验'} 笔 · 扣费后 ${reviewMoney(d.realized_pnl)} · ${feeStatusLabel(d.fill_review?.cost_status)}`);
  (d.fill_review?.items || []).forEach(row => L.push(`  ${row.name || row.code} ${row.shares ?? '未记录'}股 @ ${reviewMoney(row.price)} · ${feeStatusLabel(row.cost_status)} · 扣费后 ${reviewMoney(row.net_pnl_yuan)} · 录入 ${row.recorded_at || '未记录'} / 实际成交 ${row.execution_time_fact?.executed_at || '未补记'} · ${fillIdentity(row)}${feeRevisionLabel(row) ? ' · ' + feeRevisionLabel(row) : ''}`));
  if (d.fill_review?.fee_revision_note) L.push(d.fill_review.fee_revision_note);
  L.push('', '原计划与实际成交偏差（不授予策略资格）：');
  (d.plan_review || []).forEach(row => L.push(`  ${row.fill_id} · ${row.name || row.code} · ${row.shares}股 @ ${reviewMoney(row.price)} · ${row.comparison?.label || '未核对'} · 原提醒 ${row.comparison?.alert_id ?? '未关联'} · 计划 ${row.comparison?.planned_shares ?? '未记录'}股 / 限价 ${reviewMoney(row.comparison?.planned_limit_price)} · 全部关联 ${row.comparison?.linked_total_shares ?? '未记录'}股`));
  L.push(`未能确定是否属于所选日期：买入 ${d.unassigned_buy_time_count || 0} 笔；卖出 ${d.fill_review?.unassigned_fills?.length || 0} 笔。`);
  (d.fill_review?.unassigned_fills || []).forEach(row => L.push(`  ${row.name || row.fill_id} · ${row.reason} · 录入 ${row.recorded_at || '未知'}`));
  (d.closed || []).forEach(p => L.push(`  ${(p.sold_at || '').slice(11,16)} ${p.name} ${p.sold_shares_total ?? p.initial_shares ?? p.shares}股 买¥${p.price} → 卖均¥${p.average_sell_price ?? p.sold_price}，历史账本值 ${reviewMoney(p.pnl)}（以逐笔成本核验为准） · ${entryAttributionLabel(p)}${p.hold_days != null ? `（持 ${p.hold_days} 天）` : ''}`));
  L.push('');
  const openNow = Array.isArray(d.open_now) ? d.open_now : [];
  if (!openNow.length) L.push('当前持仓：无');
  else if (d.open_pnl_total == null) {
    L.push('当前持仓浮盈：行情暂不可得');
    openNow.forEach(p => L.push(`  ${p.name} ${p.shares}股 成本 ¥${p.price}，${p.current != null ? `现价 ¥${p.current}` : '现价行情暂不可得'}，浮盈 ${p.pnl != null ? `¥${p.pnl.toFixed(2)}` : '—'} · ${entryAttributionLabel(p)}`));
  } else {
    L.push(`当前持仓浮盈：¥${d.open_pnl_total.toFixed(2)}`);
    openNow.forEach(p => L.push(`  ${p.name} ${p.shares}股 成本 ¥${p.price}，${p.current != null ? `现价 ¥${p.current}` : '现价行情暂不可得'}，浮盈 ${p.pnl != null ? `¥${p.pnl.toFixed(2)}` : '—'} · ${entryAttributionLabel(p)}`));
  }
  L.push('');
  const groups = perf.by_strategy || [];
  L.push(groups.length ? `策略归因（仅完整生产身份）：${groups.map(g => `${g.strategy_id} · 规格 ${String(g.strategy_spec_sha256 || '—').slice(0,12)} · 证据 ${String(g.evidence_ref_sha256 || '—').slice(0,12)} · ${g.completed_positions}笔`).join('；')}` : '策略归因：暂无完整生产策略平仓样本；全部手工成交（混合来源）不构成策略收益。');
  const account = d.account_performance || {};
  if (account.available === true) {
    L.push(
      '',
      `账户原始变化：${Number(account.account_change_since_baseline_pct || 0).toFixed(4)}%`,
      `已记录现金流调整后：${Number(account.cashflow_adjusted_return_pct || 0).toFixed(4)}% · 外部净流入 ¥${Number(account.net_external_flow_yuan || 0).toFixed(2)}`,
      `日终净值：${Number(account.stored_daily_nav_observations || 0)} 个 · ${account.daily_close_path_ready ? '日频路径已具备初步计算条件' : '日频路径尚不足，最大回撤仅作有限观察'}`,
    );
  }
  L.push('', `历史账本累计值：${reviewMoney(d.all_time_realized)}（可能含估费或旧记录，不是已核实净收益）`, '', 'Regis Oracle · 数据来自人工成交记录与行情，仅供复盘参考');
  return L.join('\n');
}

async function copyReviewReport(raw) {
  const text = buildReviewReport(raw);
  try { await navigator.clipboard.writeText(text); }
  catch (error) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
  }
  const button = $('#copyReviewBtn');
  if (button) {
    button.textContent = '已复制 ✓';
    setTimeout(() => { if (button) button.textContent = '复制复盘'; }, 1600);
  }
}

RegisSpa.review = { normalizeReviewData, buildReviewReport, copyReviewReport, renderFillReview };
})(window.RegisSpa);
