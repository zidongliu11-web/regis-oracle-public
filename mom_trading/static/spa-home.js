(function(RegisSpa){
'use strict';
if (!RegisSpa || !RegisSpa.core?.state) throw new Error('Regis SPA core must load before home module');
const { $, $$, state, esc, escAttr, fn, fp, fpp, cc, reportClientIssue, resizeAllCharts } = RegisSpa.core;

const HOME_PHASES = [
  ['盘前规划', '08:30–09:15', '仓位与主线预案'],
  ['集合竞价', '09:15–09:25', '竞价强弱核验'],
  ['开盘30分', '09:30–10:00', '首轮确认'],
  ['上午盘中', '10:00–11:30', '主线延续跟踪'],
  ['午间复核', '11:30–13:00', '持仓与计划复核'],
  ['下午盘中', '13:00–14:30', '持续性再确认'],
  ['尾盘决策', '14:30–15:00', '去留与隔夜风险'],
  ['收盘复盘', '15:00 后', '成交与明日计划'],
];
const HOME_DENSITY_KEY = 'regis-home-density-v1';
const HOME_WORKSPACE_KEY = 'regis-home-workspace-v1';
const HOME_LAYOUT_KEY_PREFIX = 'regis-home-layout-v1:';
const HOME_EVIDENCE_OPEN_KEY = 'regis-home-evidence-open-v2';
const HOME_HYPOTHETICAL_LIQUIDATION_CODE = '603867';
let homeServerClockOffsetMs = null;
let homeServerClockStamp = '';
let homeTimelineTimer = null;
let homeLayoutEditing = false;
let homeEvidenceOpen = false;

function dashboardPhaseFromSession(payload) {
  const session = payload?.session || payload || {};
  const status = session.status;
  if (!status || status === 'unknown') return {index:-1, workspace:'review', status:'unknown'};
  if (status === 'closed') return {index:7, workspace:'review', status};
  if (status === 'preopen') return {index:0, workspace:'premarket', status};
  if (status === 'auction') return {index:1, workspace:'premarket', status};
  if (status === 'break') return {index:4, workspace:'intraday', status};
  if (status !== 'open') return {index:-1, workspace:'review', status:'unknown'};
  const match = String(session.server_time || '').match(/T(\d{2}):(\d{2})/);
  if (!match) return {index:-1, workspace:'intraday', status:'unknown'};
  const minutes = Number(match[1]) * 60 + Number(match[2]);
  return {
    index:minutes < 600 ? 2 : minutes < 690 ? 3 : minutes < 870 ? 5 : 6,
    workspace:'intraday', status,
  };
}

function homeBeijingMinutes(timestamp) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).formatToParts(new Date(timestamp));
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return (Number(values.hour) % 24) * 60 + Number(values.minute)
    + Number(values.second || 0) / 60;
}

function homeBeijingClock(timestamp) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).format(new Date(timestamp));
}

function homeTimelineProgress(session, nowMs) {
  const phase = dashboardPhaseFromSession(session);
  if (phase.index < 0) return null;
  if (phase.status === 'closed') return 100;
  const stamp = String(session?.server_time || '');
  const serverMs = Date.parse(stamp);
  if (Number.isFinite(serverMs) && stamp !== homeServerClockStamp) {
    homeServerClockStamp = stamp;
    homeServerClockOffsetMs = serverMs - Date.now();
  }
  const clock = Number.isFinite(nowMs)
    ? nowMs
    : Date.now() + (Number.isFinite(homeServerClockOffsetMs) ? homeServerClockOffsetMs : 0);
  const minutes = homeBeijingMinutes(clock);
  const phaseMinutes = [510, 555, 570, 600, 690, 780, 870, 900];
  if (minutes <= phaseMinutes[0]) return 0;
  if (minutes >= phaseMinutes.at(-1)) return 100;
  const next = phaseMinutes.findIndex(value => minutes < value);
  const previous = next - 1;
  const fraction = (minutes - phaseMinutes[previous]) / (phaseMinutes[next] - phaseMinutes[previous]);
  return ((previous + fraction) / (HOME_PHASES.length - 1)) * 100;
}

function updateHomeSessionTimeline(home, session, workspace) {
  const details = $('.decision-session-rail', home);
  if (details && details.dataset.defaultWorkspace !== workspace) {
    details.open = workspace === 'intraday';
    details.dataset.defaultWorkspace = workspace;
  }
  const rail = $('.decision-phase-rail', home);
  if (!rail) return;
  let meter = $('.decision-phase-live-meter', rail);
  if (!meter) {
    meter = document.createElement('div');
    meter.className = 'decision-phase-live-meter';
    meter.setAttribute('role', 'progressbar');
    meter.setAttribute('aria-valuemin', '0');
    meter.setAttribute('aria-valuemax', '100');
    meter.innerHTML = '<i aria-hidden="true"></i><b aria-hidden="true"></b><em class="decision-phase-live-clock"></em>';
    rail.prepend(meter);
  }
  const progress = homeTimelineProgress(session);
  if (progress == null) {
    meter.hidden = true;
    return;
  }
  meter.hidden = false;
  meter.style.setProperty('--phase-progress', `${progress}%`);
  const clock = $('.decision-phase-live-clock', meter);
  if (clock) clock.textContent = homeBeijingClock(
    Date.now() + (Number.isFinite(homeServerClockOffsetMs) ? homeServerClockOffsetMs : 0)
  );
  meter.setAttribute('aria-valuenow', String(Math.round(progress)));
  meter.setAttribute('aria-label', `今日交易阶段，北京时间进度 ${Math.round(progress)}%`);
}

function ensureHomeTimelineTimer() {
  if (homeTimelineTimer != null) return;
  homeTimelineTimer = window.setInterval(() => {
    const home = $('#decisionHome');
    if (!home) return;
    const session = state.marketSessionPayload?.session || state.homeData.portfolio?.market_session || {};
    updateHomeSessionTimeline(home, session, state.homeWorkspace || dashboardPhaseFromSession(session).workspace);
  }, 1000);
}

function setHomeDensity(value, persist = true) {
  const density = value === 'compact' ? 'compact' : 'comfortable';
  document.body.dataset.density = density;
  $$('.density-switch [data-density]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.density === density));
  });
  if (persist) {
    try { localStorage.setItem(HOME_DENSITY_KEY, density); }
    catch (error) { reportClientIssue('home-density-save', error); }
  }
  setTimeout(resizeAllCharts, 0);
}

function homeLayoutPanels(home) {
  // The decision surface is the product's fixed truth header.  Saved layouts
  // may reorder supporting evidence, never move the primary decision below it.
  return $$('[data-workspace-panel]:not([data-workspace-panel="decision"])', home);
}

function saveHomeLayout(home, workspace) {
  if (!home) return;
  const panels = homeLayoutPanels(home);
  const value = {
    version: 1,
    order: panels.map(panel => panel.dataset.workspacePanel),
    heights: Object.fromEntries(panels.map(panel => {
      const height = Math.round(panel.getBoundingClientRect().height);
      return [panel.dataset.workspacePanel, Math.max(80, Math.min(900, height))];
    })),
  };
  try { localStorage.setItem(HOME_LAYOUT_KEY_PREFIX + workspace, JSON.stringify(value)); }
  catch (error) { reportClientIssue('home-layout-save', error); }
}

function restoreHomeLayout(home, workspace) {
  if (!home) return;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(HOME_LAYOUT_KEY_PREFIX + workspace) || 'null'); }
  catch (error) { reportClientIssue('home-layout-load', error); }
  const panels = homeLayoutPanels(home);
  const byId = new Map(panels.map(panel => [panel.dataset.workspacePanel, panel]));
  const validOrder = Array.isArray(saved?.order)
    ? saved.order.filter((id, index, rows) => byId.has(id) && rows.indexOf(id) === index)
    : [];
  panels.forEach(panel => {
    if (!validOrder.includes(panel.dataset.workspacePanel)) validOrder.push(panel.dataset.workspacePanel);
  });
  const anchor = $('.decision-ops-drawer', home);
  validOrder.forEach(id => home.insertBefore(byId.get(id), anchor || null));
  panels.forEach(panel => {
    const height = Number(saved?.heights?.[panel.dataset.workspacePanel]);
    panel.style.height = Number.isFinite(height) && height >= 80 && height <= 900 ? `${height}px` : '';
  });
}

function applyHomeLayoutEditing(home) {
  const button = $('#layoutEditButton');
  if (home) home.dataset.layoutEditing = String(homeLayoutEditing);
  if (button) {
    button.setAttribute('aria-pressed', String(homeLayoutEditing));
    button.textContent = homeLayoutEditing ? '完成布局调整' : '调整首页布局';
  }
  homeLayoutPanels(home).forEach(panel => {
    panel.draggable = homeLayoutEditing;
  });
}

function toggleHomeLayoutEditing() {
  const home = $('#decisionHome');
  if (!home) return;
  homeLayoutEditing = !homeLayoutEditing;
  applyHomeLayoutEditing(home);
  if (!homeLayoutEditing) saveHomeLayout(home, state.homeWorkspace || 'review');
}

function installHomeLayoutEditor(home) {
  if (!home || home.dataset.layoutEditorBound === 'true') return;
  home.dataset.layoutEditorBound = 'true';
  let dragged = null;
  home.addEventListener('dragstart', event => {
    if (!homeLayoutEditing) return;
    dragged = event.target.closest('[data-workspace-panel]');
    if (!dragged) return;
    dragged.classList.add('is-dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', dragged.dataset.workspacePanel);
  });
  home.addEventListener('dragover', event => {
    if (!homeLayoutEditing || !dragged) return;
    const target = event.target.closest('[data-workspace-panel]');
    if (!target || target === dragged) return;
    event.preventDefault();
    const after = event.clientY > target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
    home.insertBefore(dragged, after ? target.nextSibling : target);
  });
  home.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('is-dragging');
    dragged = null;
    saveHomeLayout(home, state.homeWorkspace || 'review');
  });
  home.addEventListener('pointerup', event => {
    if (homeLayoutEditing && event.target.closest('[data-workspace-panel]')) {
      saveHomeLayout(home, state.homeWorkspace || 'review');
    }
  });
  applyHomeLayoutEditing(home);
}

function setHomeWorkspace(value, persist = true) {
  const workspace = ['premarket', 'intraday', 'review'].includes(value) ? value : 'review';
  state.homeWorkspace = workspace;
  const home = $('#decisionHome');
  if (home) home.dataset.workspace = workspace;
  $$('#workspaceSwitch [data-workspace]').forEach(button => {
    const selected = button.dataset.workspace === workspace;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  if (persist) {
    try { localStorage.setItem(HOME_WORKSPACE_KEY, workspace); }
    catch (error) { reportClientIssue('home-workspace-save', error); }
  }
  renderDecisionHome();
}

function restoreHomePreferences() {
  let density = 'comfortable';
  let workspace = 'review';
  try {
    density = localStorage.getItem(HOME_DENSITY_KEY) || density;
    workspace = localStorage.getItem(HOME_WORKSPACE_KEY) || workspace;
    const savedEvidenceOpen = localStorage.getItem(HOME_EVIDENCE_OPEN_KEY);
    homeEvidenceOpen = savedEvidenceOpen == null ? true : savedEvidenceOpen === '1';
  } catch (error) { reportClientIssue('home-preferences-load', error); }
  const requestedWorkspace = new URLSearchParams(window.location?.search || '').get('workspace');
  if (['premarket', 'intraday', 'review'].includes(requestedWorkspace)) workspace = requestedWorkspace;
  setHomeDensity(density, false);
  state.homeWorkspace = ['premarket', 'intraday', 'review'].includes(workspace) ? workspace : 'review';
}

function homeCompletePlans(route, session) {
  if (session?.is_open !== true || route?.research_snapshot?.current === false) return [];
  const workspace = route?.today_decision_workspace || {};
  if (workspace.actionable_now !== true) return [];
  const sells = Array.isArray(workspace.sell_review_items) ? workspace.sell_review_items : [];
  const buys = workspace.strict_forward_packet_valid === true
    && workspace.strict_market_budget_valid === true
    && workspace.sector_entry_gate_ready === true
    && Array.isArray(workspace.manual_review_items)
    ? workspace.manual_review_items : [];
  return sells.map(item => ({item, plan:item, side:'SELL'}))
    .concat(buys.map(item => ({item, plan:item?.manual_plan || {}, side:'BUY'})))
    .filter(({item, plan, side}) => {
      const shares = Number(plan.shares);
      const limit = Number(plan.limit_price);
      const cost = Number(plan.estimated_cost);
      const identity = plan.evidence_identity;
      const evidenceIdentity = plan.evidence_id || (identity && typeof identity === 'object'
        ? identity.id || identity.packet_id || identity.method : identity);
      return plan.status === 'ready_for_manual_review' && plan.side === side
        && /^\d{6}$/.test(String(item?.code || ''))
        && Number.isInteger(shares) && shares > 0
        && Number.isFinite(limit) && limit > 0
        && Number.isFinite(cost) && cost >= 0
        && Boolean(plan.valid_until) && Boolean(plan.invalidation_condition)
        && Boolean(plan.playbook_id) && Boolean(plan.strategy_id)
        && Boolean(plan.strategy_version) && Boolean(evidenceIdentity)
        && Boolean(plan.evidence_grade) && Boolean(plan.as_of)
        && plan.human_confirmation_required === true
        && plan.broker_connected === false && plan.automatic_trading === false;
    });
}

function homeAccountSellPlans(portfolio, allocation, session) {
  if (session?.is_open !== true || portfolio?.alerts_active !== true) return [];
  const now = Date.parse(session.server_time || '');
  if (!Number.isFinite(now)) return [];
  const allocationByCode = new Map((Array.isArray(allocation?.items) ? allocation.items : [])
    .filter(row => row && row.code).map(row => [String(row.code), row]));
  const plans = [];
  (Array.isArray(portfolio?.open) ? portfolio.open : []).forEach(position => {
    const direct = position?.trade_plan || {};
    const fallback = allocationByCode.get(String(position?.code || ''))?.trade_plan || {};
    const source = ['EXIT_REVIEW', 'REDUCE_REVIEW'].includes(direct.state) ? direct : fallback;
    const action = source?.action || {};
    const held = Number(position?.shares);
    const shares = Number(action.shares);
    const targetShares = Number(action.target_shares);
    const limitPrice = Number(action.limit_price);
    const estimatedCost = Number(action.estimated_cost);
    const validUntil = Date.parse(action.valid_until || '');
    const identity = action.evidence_identity;
    if (!['EXIT_REVIEW', 'REDUCE_REVIEW'].includes(source?.state)
        || source?.sellable_inventory?.status !== 'current'
        || action.side !== 'SELL' || action.manual_confirmation !== true
        || !Number.isInteger(held) || held <= 0
        || !Number.isInteger(shares) || shares <= 0
        || !Number.isInteger(targetShares) || targetShares < 0
        || shares + targetShares !== held
        || !Number.isFinite(limitPrice) || limitPrice <= 0
        || !Number.isFinite(estimatedCost) || estimatedCost < 0
        || !Number.isFinite(validUntil) || validUntil <= now
        || !action.cancel_condition
        || !action.playbook_id || !action.strategy_id || !action.strategy_version
        || !action.evidence_grade || !action.as_of
        || !identity || typeof identity !== 'object') return;
    plans.push({
      item: {code:String(position.code), name:position.name || position.code,
        stock_research_url:`/stock?code=${encodeURIComponent(position.code)}`,
        current:Number(position.current), held_shares:held},
      side:'SELL', priority:source.state === 'EXIT_REVIEW' ? 0 : 1,
      plan:{status:'ready_for_manual_review', side:'SELL', shares,
        target_shares:targetShares, held_shares:held, limit_price:limitPrice,
        estimated_cost:estimatedCost, valid_until:action.valid_until,
        invalidation_condition:action.cancel_condition,
        playbook_id:action.playbook_id, strategy_id:action.strategy_id,
        strategy_version:action.strategy_version,
        evidence_identity:identity, evidence_grade:action.evidence_grade,
        as_of:action.as_of, human_confirmation_required:true,
        broker_connected:false, automatic_trading:false},
    });
  });
  return plans.sort((a, b) => a.priority - b.priority);
}

function homeProjectedSellExposure(open, sellPlans, summary) {
  const equity = Number(summary?.account_equity);
  if (!sellPlans.length || !Number.isFinite(equity) || equity <= 0) return null;
  const targets = new Map(sellPlans.map(row => [String(row.item.code), row.plan.target_shares]));
  let marketValue = 0;
  for (const position of open) {
    const current = Number(position.current);
    const held = Number(position.shares);
    const shares = targets.has(String(position.code)) ? Number(targets.get(String(position.code))) : held;
    if (!Number.isFinite(current) || current <= 0 || !Number.isInteger(shares) || shares < 0) return null;
    marketValue += current * shares;
  }
  return Math.max(0, Math.min(100, marketValue / equity * 100));
}

function homeResearchReferencePlans(opportunities, todayDecision, session) {
  const allocation = opportunities?.allocation_plan || {};
  const route = allocation.strict_forward_route || {};
  if (session?.is_open !== true || todayDecision?.execution_route_authorized === true
      || route.valid !== true || route.current !== true
      || route.production_strategy_ready === true) return [];
  return (Array.isArray(allocation.items) ? allocation.items : [])
    .filter(item => Number.isInteger(Number(item?.shares)) && Number(item.shares) >= 100
      && Number.isFinite(Number(item?.price)) && Number(item.price) > 0
      && Number.isFinite(Number(item?.amount)) && Number(item.amount) > 0
      && Number.isFinite(Number(item?.risk_line_price)) && Number(item.risk_line_price) > 0)
    .slice(0, 2)
    .map(item => ({
      ...item, packet_id:route.packet_id, slot_id:route.slot_id,
      playbook_id:route.research_playbook_id, as_of:allocation.date,
    }));
}

function homeResearchReferencePlansHtml(plans) {
  if (!plans.length) return '';
  return `<section class="decision-research-plan-list"><header><b>研究复核测算 · ${plans.length} 项</b><span>不触发买入提醒 · 不是最佳买点证明</span></header>${plans.map(plan => {
    const code = String(plan.code || '');
    const name = plan.name || code;
    const plannedLoss = Number(plan.planned_loss_at_risk_line_yuan);
    return `<a href="/stock?code=${encodeURIComponent(code)}#terminalTradePlan"><b>${esc(name)} ${esc(code)}</b><span>参考目标 ${Number(plan.shares)} 股 · 参考价 ¥${fn(plan.price, 2)} · 目标仓位 ${fn(plan.target_weight_pct, 2)}%</span><small>预计占用 ¥${fn(plan.estimated_buy_cash_yuan, 2)} · 费用 ¥${fn(plan.estimated_buy_cost_yuan, 2)} · 风险线 ¥${fn(plan.risk_line_price, 2)}${Number.isFinite(plannedLoss) ? ` · 计划损失 ¥${fn(plannedLoss, 2)}` : ''}</small><small>研究战法 ${esc(plan.playbook_id || '未形成唯一战法')} · 生产策略未绑定</small><small>同槽包 ${esc(plan.packet_id || '待核验')} · ${esc(String(plan.as_of || '时点待核验').replace('T', ' ').slice(0, 19))}</small></a>`;
  }).join('')}<p>用途：在个股页继续核对盘口、成交量、MACD、失效条件与实时价格后由你人工决定；未通过成本后和样本外验证，不生成声音、系统通知或买入按钮。</p></section>`;
}

function homeIntradayPlansHtml(plans, researchPlans = []) {
  if (!plans.length) return `<div class="decision-section-label">今日完整买卖计划 · 0 项 · 持仓硬风险优先</div>
    <div class="decision-empty"><b>当前没有可显示的正式计划</b><span>缺少正式策略、股数、限价、成本、有效期、失效条件或证据身份时，不生成买入提醒。</span><button type="button" data-action="toggle-home-why">查看今日决策证据</button></div>${homeResearchReferencePlansHtml(researchPlans)}`;
  return `<div class="decision-section-label">今日完整买卖计划 · ${plans.length} 项 · 持仓硬风险优先</div>
    <div class="decision-action-list">${plans.map(({item, plan, side}) => {
      const code = String(item.code);
      const name = item.name || code;
      const identity = plan.evidence_identity;
      const evidenceIdentity = plan.evidence_id || (identity && typeof identity === 'object'
        ? identity.id || identity.packet_id || identity.method : identity);
      const href = `${item.stock_research_url || `/stock?code=${encodeURIComponent(code)}`}#terminalTradePlan`;
      const planLabel = side !== 'SELL' ? '买入人工复核'
        : plan.strategy_id === 'portfolio_exposure_rebalance_v1'
          ? '组合仓位减仓复核' : '持仓硬风险卖出复核';
      return `<a href="${escAttr(href)}"><b>${planLabel} · ${esc(name)} ${esc(code)}</b><span>${plan.shares} 股 · 限价 ¥${fn(plan.limit_price, 2)} · 预计成本 ¥${fn(plan.estimated_cost, 2)}</span><small>有效至 ${esc(String(plan.valid_until).replace('T', ' ').slice(0, 19))} · 失效：${esc(plan.invalidation_condition)}</small><small>战法 ${esc(plan.playbook_id)} · 策略 ${esc(plan.strategy_id)} / ${esc(plan.strategy_version)}</small><small>证据 ${esc(evidenceIdentity)} / ${esc(plan.evidence_grade)} · as_of ${esc(plan.as_of)}</small></a>`;
    }).join('')}</div>`;
}

function homeHypotheticalLiquidation(summary, open, opportunities, scenarioAsOf = null) {
  const position = (Array.isArray(open) ? open : []).find(
    row => String(row?.code || '') === HOME_HYPOTHETICAL_LIQUIDATION_CODE,
  );
  if (!position) return null;
  const price = Number(position.current);
  const shares = Number(position.shares);
  const currentCash = Number(summary?.available_cash);
  const currentMarketValue = Number(summary?.market_value);
  const exitCost = Number(position.estimated_exit_cost_yuan);
  if (![price, shares, currentCash, currentMarketValue, exitCost].every(Number.isFinite)
      || price <= 0 || !Number.isInteger(shares) || shares <= 0 || exitCost < 0) {
    return {
      ready: false,
      code: HOME_HYPOTHETICAL_LIQUIDATION_CODE,
      name: position.name || HOME_HYPOTHETICAL_LIQUIDATION_CODE,
      reason: '实时价格、持股数量或预计卖出费用不完整，不能估算假设清仓后的资金。',
    };
  }
  const grossProceeds = price * shares;
  const netProceeds = Math.max(0, grossProceeds - exitCost);
  const projectedCash = currentCash + netProceeds;
  const projectedMarketValue = Math.max(0, currentMarketValue - grossProceeds);
  const projectedEquity = projectedCash + projectedMarketValue;
  const projectedExposurePct = projectedEquity > 0
    ? projectedMarketValue / projectedEquity * 100 : null;
  const exposureCeilingPct = Number(summary?.position_policy?.live_total_exposure_ceiling_pct);
  const singlePositionLimitPct = Number(summary?.single_position_limit_pct);
  const exposureRoom = Number.isFinite(exposureCeilingPct) && projectedEquity > 0
    ? Math.max(0, projectedEquity * exposureCeilingPct / 100 - projectedMarketValue)
    : 0;
  const researchCashRoom = Math.max(0, Math.min(projectedCash, exposureRoom));
  const sourceRows = Array.isArray(opportunities?.hypothetical_watch_items)
    ? opportunities.hypothetical_watch_items : [];
  const candidates = sourceRows
    .filter(row => row?.assessment?.verdict === 'APPROVE'
      && Number.isFinite(Number(row.price)) && Number(row.price) > 0)
    .sort((a, b) => Number(b.score || 0) - Number(a.score || 0))
    .slice(0, 3)
    .map(row => {
      const candidatePrice = Number(row.price);
      const singleCap = Number.isFinite(singlePositionLimitPct)
        ? projectedEquity * singlePositionLimitPct / 100 : researchCashRoom;
      const independentCashCap = Math.max(0, Math.min(researchCashRoom, singleCap));
      return {
        code: String(row.code || ''), name: row.name || row.code,
        price: candidatePrice, score: Number(row.score),
        upperBoundShares: Math.floor(independentCashCap / (candidatePrice * 100)) * 100,
        state: row.assessment?.action_state || 'WAIT_ENTRY',
        reason: row.assessment?.reason || '等待完整买点条件。',
      };
    });
  return {
    ready: true,
    code: HOME_HYPOTHETICAL_LIQUIDATION_CODE,
    name: position.name || HOME_HYPOTHETICAL_LIQUIDATION_CODE,
    price, shares, quoteAsOf: position.quote_as_of || scenarioAsOf || null,
    grossProceeds, exitCost, netProceeds, projectedCash, projectedMarketValue,
    projectedEquity, projectedExposurePct, exposureCeilingPct,
    researchCashRoom, candidates,
  };
}

function homeHypotheticalLiquidationHtml(summary, open, opportunities, scenarioAsOf = null) {
  const scenario = homeHypotheticalLiquidation(summary, open, opportunities, scenarioAsOf);
  if (!scenario) return '';
  if (!scenario.ready) return `<section class="decision-account-scenario" data-status="missing">
    <header><div><small>情景账户 · 未写入真实账本</small><b>假设${esc(scenario.name)}已清仓</b></div><span>估算不可用</span></header>
    <p>${esc(scenario.reason)}</p></section>`;
  const quoteTime = scenario.quoteAsOf
    ? String(scenario.quoteAsOf).replace('T', ' ').slice(5, 19) : '行情时点待核验';
  const candidateRows = scenario.candidates.length ? scenario.candidates.map(row => `
    <a href="/stock?code=${encodeURIComponent(row.code)}">
      <span><b>${esc(row.name)} ${esc(row.code)}</b><small>统一研究分 ${Number.isFinite(row.score) ? fn(row.score, 1) : '—'} · ¥${fn(row.price, 2)}</small></span>
      <span><b>${row.state === 'WAIT_ENTRY' ? '等待买点' : esc(row.state)}</b><small>${esc(row.reason)}</small></span>
      <span><b>${row.upperBoundShares > 0 ? `单股硬上限 ${row.upperBoundShares} 股` : '资金上限不足1手'}</b><small>独立压力上限，不是建议股数</small></span>
    </a>`).join('') : '<div class="decision-empty"><b>候选仍在更新</b><span>只展示统一评分通过且具有实时价格的研究候选。</span></div>';
  return `<section class="decision-account-scenario" data-status="ready">
    <header><div><small>情景账户 · 未成交假设 · 不改真实账本</small><b>假设${esc(scenario.name)} ${scenario.shares}股已按当前价清仓</b></div><span>@ ¥${fn(scenario.price, 2)} · ${esc(quoteTime)}</span></header>
    <div class="decision-account-scenario-metrics">
      <span><small>估算净回笼</small><b>¥${fn(scenario.netProceeds, 0)}</b></span>
      <span><small>情景可用现金</small><b>¥${fn(scenario.projectedCash, 0)}</b></span>
      <span><small>剩余持仓</small><b>${Number.isFinite(scenario.projectedExposurePct) ? `${fn(scenario.projectedExposurePct, 1)}%` : '—'}</b></span>
      <span><small>风险上限内新增空间</small><b>¥${fn(scenario.researchCashRoom, 0)}</b></span>
    </div>
    <div class="decision-section-label"><b>统一候选照常更新</b><span>未到买点不生成建议股数；真实提醒仍以成交后账本为准</span></div>
    <div class="decision-account-scenario-candidates">${candidateRows}</div>
    <p>预计卖出费用 ¥${fn(scenario.exitCost, 2)} 已扣除。这里不代表已经卖出，也不授权买入、卖出或自动下单；实际成交后须录入成交价、数量和费用。</p>
  </section>`;
}

function homeSectorRows(route, todayDecision = {}) {
  const strictRows = Array.isArray(todayDecision.confirmed_sectors)
    ? todayDecision.confirmed_sectors : [];
  if ((todayDecision.sector_confirmation_ready === true
      || todayDecision.sector_entry_gate_ready === true
      || todayDecision.research_sector_confirmation_ready === true) && strictRows.length) {
    return strictRows;
  }
  const completed = todayDecision.completed_market_review || route?.completed_market_review || {};
  const completedRows = Array.isArray(completed.confirmed_sectors)
    ? completed.confirmed_sectors : [];
  if (completed.valid === true && completed.display_only === true && completedRows.length) {
    return completedRows;
  }
  const sector = route?.sector_confirmation || {};
  return sector.ready === true && Array.isArray(sector.confirmed_sectors)
    ? sector.confirmed_sectors : [];
}

function homeSectorNames(route, todayDecision = {}) {
  const rows = homeSectorRows(route, todayDecision).map((row, index) => ({
    row, index, priority:row?.kind === 'industry' ? 0 : row?.kind === 'concept' ? 1 : 2,
  })).sort((a, b) => a.priority - b.priority || a.index - b.index);
  const seen = new Set();
  const names = [];
  for (const {row} of rows) {
    const name = typeof row === 'string'
      ? row : row?.name || row?.sector_name || row?.label || row?.code;
    if (!name) continue;
    const hierarchyStem = String(name).replace(/[ⅠⅡⅢⅣⅤⅥ]+$/u, '');
    if (seen.has(hierarchyStem)) continue;
    seen.add(hierarchyStem);
    names.push(name);
  }
  return names;
}

function homePlaybookNames(route, todayDecision = {}) {
  const library = Array.isArray(route?.small_capital_playbook_library?.playbooks)
    ? route.small_capital_playbook_library.playbooks : [];
  const labels = new Map(library.map(row => [String(row?.id || ''), row?.label || row?.id]));
  const hasDecisionPlaybooks = Array.isArray(todayDecision.eligible_playbooks);
  const decisionPlaybooks = hasDecisionPlaybooks
    ? todayDecision.eligible_playbooks : [];
  const completed = todayDecision.completed_market_review || route?.completed_market_review || {};
  const completedRows = completed.valid === true && completed.display_only === true
    && Array.isArray(completed.eligible_playbooks) ? completed.eligible_playbooks : [];
  // An explicit empty decision list is a real no-playbook result.  Do not fall
  // back to a completed research packet: it can contain a playbook that later
  // failed cost-aware validation and is no longer eligible today.
  const rows = hasDecisionPlaybooks
    ? decisionPlaybooks : completedRows.length
      ? completedRows : (route?.research_budget_hypothesis?.allowed_playbooks || []);
  return rows.map(row => {
    const id = typeof row === 'string' ? row : row?.id;
    return (typeof row === 'object' && row?.label) || labels.get(String(id || '')) || id;
  }).filter(Boolean);
}

function homeTimelineRows(review) {
  const rows = Array.isArray(review?.timeline) ? review.timeline : [];
  if (!rows.length) return '<div class="decision-empty">今日尚无已记录的决策变化或成交事件。</div>';
  return rows.slice(-8).map(row => {
    const riskReview = row?.type === 'alert' && row?.kind === 'sell'
      && row?.timing_interpretation?.best_timing_validated === false;
    const label = riskReview
      ? `风险复核（非最佳卖点） · ${row.label || '未知股票'}`
      : row.label || row.type || '事件';
    const status = {buy:'买入记录', sell:'卖出记录', import:'持仓导入', triggered:'待处理', confirmed:'已确认', dismissed:'已忽略', expired:'已过期', superseded:'已失效'}[row.status || row.kind] || '记录';
    return `<div class="decision-log-row"><time>${esc(row.time || '—')}</time><b>${esc(label)}</b><span>${esc(status)}</span></div>`;
  }).join('');
}

function homeSignalHistoryRows(review) {
  const rows = homeSignalHistoryItems(review);
  if (!rows.length) return '<div class="decision-empty"><b>该交易日尚未产生可执行买卖提醒</b><span>研究候选、数据缺口和未获统一授权的观察不会冒充信号记录。</span></div>';
  const attributionByEvent = new Map(
    (review?.exit_attribution?.items || []).map(row => [Number(row.sell_event_id), row]),
  );
  const costByEvent = new Map((review?.fill_review?.items || []).map(row => [Number(row.sell_event_id), row]));
  const statusNames = {
    triggered:'待人工处理', confirmed:'已记录成交', dismissed:'已忽略',
    superseded:'已失效', expired:'已过期',
  };
  return `<div class="decision-signal-list">${rows.slice(-8).reverse().map(row => {
    if (row.record_kind === 'sell_fill') {
      const shares = Number(row.shares);
      const price = Number(row.price);
      const amount = Number(row.amount);
      const audited = costByEvent.get(Number(row.id));
      const netPnl = audited?.net_pnl_yuan;
      const costText = audited?.cost_status === 'actual' ? '券商实录费用后'
        : audited?.cost_status === 'estimated' ? '含模型估费 · 非实际净收益'
        : '成交或费用待核验';
      const attribution = attributionByEvent.get(Number(row.id));
      const closeGap = Number(attribution?.versus_close?.estimated_net_difference_yuan);
      const highGap = Number(attribution?.versus_session_high?.estimated_net_difference_yuan);
      const attributionText = attribution?.versus_close
        ? `收盘归因：收盘 ¥${fn(attribution.close_price, 2)} · 估算净差 ${closeGap >= 0 ? '+' : ''}¥${fn(closeGap, 2)}；最高价仅后视上限 ${Number.isFinite(highGap) ? `${highGap >= 0 ? '+' : ''}¥${fn(highGap, 2)}` : '—'}`
        : '';
      return `<article class="decision-signal-row" data-side="sell" data-kind="fill">
        <time>${esc(String(row.review_at || row.sold_at || '').slice(11, 16) || '—')}</time>
        <span class="decision-signal-side">实际卖出</span>
        <a href="/stock?code=${encodeURIComponent(row.code || '')}"><b>${esc(row.name || row.code || '未知股票')}</b><small>${esc(row.code || '')}</small></a>
        <span><b>${Number.isInteger(shares) && shares > 0 ? `${shares} 股 @ ¥${fn(price, 2)}` : '成交数量未记录'}</b><small>${Number.isFinite(amount) ? `成交额 ¥${fn(amount, 2)}` : '成交额未记录'}</small></span>
        <span><b>用户实际成交</b><small>${esc(costText)}${typeof netPnl === 'number' && Number.isFinite(netPnl) ? ` · 净盈亏 ¥${fn(netPnl, 2)}` : ' · 净盈亏待核验'}</small></span>
        <em>成交已记录</em>
        ${attributionText ? `<p class="decision-exit-attribution">${esc(attributionText)}（最高价不是可实现基准）</p>` : ''}
      </article>`;
    }
    const plan = row.review_plan || {};
    const side = String(row.action || plan.side || '').toLowerCase() === 'sell' ? 'sell' : 'buy';
    const origin = String(row.origin || '');
    const unvalidatedRiskTiming = row?.timing_interpretation?.best_timing_validated === false;
    const sideLabel = side !== 'sell' ? '买入提醒'
      : origin === 'manual_exit_intent' ? '你的退出计划'
      : origin === 'production_strategy' ? '策略退出提醒'
      : unvalidatedRiskTiming || origin === 'position_risk_review' ? '风险复核·非最佳卖点'
      : '卖出复核';
    const shares = Number(plan.shares);
    const optionalNumber = (value) => value === null || value === undefined || value === ''
      ? null : (Number.isFinite(Number(value)) ? Number(value) : null);
    const targetWeight = optionalNumber(plan.target_weight_pct);
    const totalExposure = optionalNumber(plan.market_target_exposure_pct);
    const targetShares = optionalNumber(plan.target_shares);
    const quantityText = Number.isInteger(shares) && shares > 0 ? `${shares} 股` : '数量未记录';
    const positionFacts = [
      side === 'sell' && Number.isInteger(targetShares) && targetShares >= 0
        ? (targetShares === 0 ? '目标持仓 0 股 · 全部离场复核' : `目标持仓 ${targetShares} 股`)
        : '',
      targetWeight !== null ? `单股目标 ${fn(targetWeight, 2)}%` : '',
      totalExposure !== null ? `当时总仓上限 ${fn(totalExposure, 1)}%` : '',
    ].filter(Boolean).join(' · ') || (Number.isInteger(shares) && shares > 0
      ? '计划数量已记录 · 仓位比例未记录'
      : '仓位未记录');
    const strategyText = plan.strategy_id
      ? `${plan.strategy_id}${plan.strategy_version ? ` / ${plan.strategy_version}` : ''}`
      : '策略身份未记录';
    return `<article class="decision-signal-row" data-side="${side}">
      <time>${esc(String(row.created_at || '').slice(11, 16) || '—')}</time>
      <span class="decision-signal-side">${sideLabel}</span>
      <a href="/stock?code=${encodeURIComponent(row.code || '')}"><b>${esc(row.name || row.code || '未知股票')}</b><small>${esc(row.code || '')}</small></a>
      <span><b>${esc(quantityText)}</b><small>${esc(positionFacts)}</small></span>
      <span><b>战法 ${esc(plan.playbook_id || '未记录')}</b><small>策略 ${esc(strategyText)}</small></span>
      <em>${esc(statusNames[row.status] || row.status || '状态未记录')}</em>
    </article>`;
  }).join('')}</div>`;
}

function homeSignalHistoryItems(review) {
  const alerts = (Array.isArray(review?.alerts) ? review.alerts : []).map(row => ({
    ...row, record_kind:'alert', record_at:row.created_at || '',
  }));
  const fills = (Array.isArray(review?.sell_events) ? review.sell_events : []).map(row => ({
    ...row, record_kind:'sell_fill', record_at:row.review_at || row.sold_at || '',
  }));
  return [...alerts, ...fills].sort((left, right) =>
    String(left.record_at || '').localeCompare(String(right.record_at || ''))
  );
}

function homeSignalHistorySection(review) {
  const count = homeSignalHistoryItems(review).length;
  const reviewDay = String(review?.date || '').slice(0, 10);
  const serverDay = String(review?.market_session?.server_time || '').slice(0, 10);
  const isLastReview = review?.review_scope === 'last_review'
    && !(reviewDay && serverDay && reviewDay === serverDay);
  const scopeLabel = review?.review_scope === 'date' ? '所选日期提醒与成交' : isLastReview ? '上一交易日信号记录' : '今日信号记录';
  const dateLabel = (isLastReview || review?.review_scope === 'date') && review?.date ? ` · ${esc(review.date)}` : '';
  return `<section class="decision-signal-history decision-signal-history-inline" aria-label="${scopeLabel}">
    <div class="decision-section-label"><b>${scopeLabel}${dateLabel} · ${count} 条</b><span>真正写入提醒账本 · 提醒不等于成交</span></div>
    ${homeSignalHistoryRows(review)}
  </section>`;
}

function homeAfterCostReviewStatus(review) {
  if (!review || typeof review !== 'object' || !review.date) {
    return {status:'blocked', label:'复盘数据不可用'};
  }
  const sells = Array.isArray(review.sell_events) ? review.sell_events : [];
  const buys = Array.isArray(review.bought_today) ? review.bought_today : [];
  const fills = review.fill_review;
  const unknownTimes = (fills?.unassigned_fills?.length || 0) + (review.unassigned_buy_time_count || 0);
  if (unknownTimes) return {status:'blocked', label:`${unknownTimes} 笔实际成交日期待补记，不能认定当日记录完整`};
  const eventCount = (fills?.fill_count ?? sells.length) + buys.length;
  if (!eventCount) return {status:'observed', label:'无成交可归因'};
  const sellCostsComplete = fills && ['actual', 'estimated', 'no_fills'].includes(fills.cost_status);
  const buyCostsComplete = buys.every(row =>
    row?.initial_shares > 0 && Number.isInteger(Number(row.initial_shares))
    && row?.price != null && Number.isFinite(Number(row.price))
    && row?.buy_cost != null && Number.isFinite(Number(row.buy_cost))
    && ['actual_user_recorded', 'estimated_model'].includes(row.buy_cost_source)
  );
  if (!sellCostsComplete || !buyCostsComplete) return {status:'blocked', label:`${eventCount} 笔记录中存在成交或费用待核验项`};
  const actual = fills.cost_status !== 'estimated' && buys.every(row => row.buy_cost_source === 'actual_user_recorded');
  return actual ? {status:'verified', label:`${eventCount} 笔实录费用已核对 · 非策略验证`}
    : {status:'observed', label:`${eventCount} 笔含模型估费 · 实际成本后收益未核实`};
}

function homeStatePlain(state) {
  return ({
    UNKNOWN:'状态未知', unknown:'状态未知', RISK_ON:'风险偏好较强', RISK_OFF:'风险关闭 · 防守或空仓', STABILIZING:'修复观察 · 等确认',
    OVERHEATED:'情绪高潮拥挤', sentiment_rising:'情绪上升', weak_repair:'情绪弱修复',
    crowded_climax:'情绪高潮拥挤', retreat:'情绪退潮', risk_closed:'风险关闭',
  })[String(state || '')] || String(state || '状态未知');
}

function homeStateMeaning(state) {
  return ({
    sentiment_rising:'上涨股票覆盖面、强势股延续和成交活跃度都比弱市改善。行动原则：机会在增多但不是普涨，只研究已确认主线里的合格股票；板块未确认时不开新仓。',
    weak_repair:'市场从弱势中修复，但扩散和持续性仍不足；以确认后的小仓研究为主，追高条件更严格。',
    crowded_climax:'强势方向过度一致、后排扩散和拥挤风险上升；不追后排，重点防冲高回落。',
    retreat:'高位负反馈或亏钱效应扩散；停止新开短线仓位，优先处理已有持仓风险。',
    risk_closed:'市场风险条件不支持新开仓；保持低暴露或空仓观察。',
    RISK_ON:'风险偏好较强，但仍须通过板块、战法、个股和账户约束。',
    RISK_OFF:'风险偏好关闭，优先防守与持仓风险。',
    STABILIZING:'市场正在企稳，尚未形成可确认的进攻阶段。',
    OVERHEATED:'市场拥挤度较高，追涨的赔率与成交风险变差。',
  })[String(state || '')] || '当前标签只表示固定规则识别出的市场阶段，仍需结合板块、战法、账户和个股条件。';
}

function homeMarketRouteBridge(research = {}, route = {}) {
  const observation = research?.model_observation || {};
  const confirmed = String(observation.state || '');
  const raw = String(observation.raw_state || '');
  const routed = String(route.requested_market_state || route.market_state || '');
  const missing = value => !value || ['unknown', 'undefined', 'null'].includes(value.toLowerCase());
  if ([confirmed, raw, routed].some(missing)) return '';
  if (confirmed === raw && homeStatePlain(confirmed) === homeStatePlain(routed)) return '';
  const explanation = routed === 'retreat' && confirmed === 'RISK_ON' && raw !== 'RISK_ON'
    ? '确认态仍偏强，但最新数据已经转弱，系统先按退潮防守；这不会自动卖出，也不会放大仓位。'
    : '确认态用于判断较慢的市场阶段，最新原始观察用于发现变化，战法路由取更保守的行动口径。';
  return `状态关系：较慢确认态为${homeStatePlain(confirmed)}；最新原始观察为${homeStatePlain(raw)}；今日战法按${homeStatePlain(routed)}处理。${explanation}`;
}

function homeMarketStatePresentation(research = {}, route = {}, todayDecision = {}) {
  const baselineState = route.requested_market_state || research.state || 'UNKNOWN';
  const baselineReady = !['', 'unknown'].includes(String(baselineState).toLowerCase());
  const baselineLabel = baselineReady ? homeStatePlain(baselineState) : '状态未知';
  const baselineAsOf = route.market_state_as_of || research.as_of || null;
  const completed = todayDecision.completed_market_review || route.completed_market_review || {};
  const sessionActive = ['open', 'break'].includes(todayDecision.session_status);
  const strictReady = todayDecision.strict_market_budget_valid === true
    && route.intraday_emotion_observation?.scope?.full_a_coverage_verified === true;
  const strictState = todayDecision.market_state;
  const strictStateReady = !['', 'unknown', 'undefined', 'null'].includes(String(strictState).toLowerCase());
  if (sessionActive && strictReady && strictStateReady) {
    const intraday = route.intraday_emotion_observation || {};
    const confirmation = String(todayDecision.intraday_confirmation || intraday.confirmation || 'not_evaluable');
    const raw = intraday.raw_observation || {};
    const upRatio = raw.up_ratio == null ? Number.NaN : Number(raw.up_ratio);
    const breadth = Number.isFinite(upRatio) ? ` · 上涨${fn(upRatio, 1)}%` : '';
    const contradicted = confirmation === 'contradicted';
    const confirmed = confirmation === 'confirmed';
    const confidence = todayDecision.market_confidence == null ? Number.NaN : Number(todayDecision.market_confidence);
    const target = todayDecision.target_exposure_pct == null ? Number.NaN : Number(todayDecision.target_exposure_pct);
    const researchTarget = todayDecision.strict_research_target_exposure_pct == null ? Number.NaN : Number(todayDecision.strict_research_target_exposure_pct);
    return {
      value: contradicted ? `昨日偏强 · 今日盘中转弱${breadth}`
        : confirmed ? `${homeStatePlain(strictState)} · 盘中延续${breadth}`
        : homeStatePlain(strictState),
      professional: confirmation === 'not_evaluable'
        ? String(strictState) : `${strictState} · ${confirmation}`,
      semantic: contradicted ? 'danger' : 'info',
      meaning: contradicted
        ? `上一完成日规则识别为${homeStatePlain(strictState)}，但今天盘中上涨覆盖率不足，强势状态未延续。`
        : homeStateMeaning(strictState),
      confidenceMeaning: Number.isFinite(confidence)
        ? `规则匹配完整度 ${fn(confidence * 100, 0)}%，不是上涨概率、胜率或收益承诺`
        : '规则匹配完整度不可用，不推断胜率',
      positionMeaning: contradicted
        ? '盘中广度已反驳昨日强势基线；今天新增交易为0，执行仓位保持当前持仓，研究预算不落到账户'
        : Number.isFinite(target)
        ? `情绪层研究预算 ${Number.isFinite(researchTarget) ? `${fn(researchTarget, 0)}%` : '待核验'}，叠加账户风险后总仓上限 ${fn(target, 0)}%；这仍不是建议仓位，只有绑定完整买卖计划后才成为执行目标`
        : '没有形成可用于今日计划的仓位结果',
      summary: contradicted
        ? `${homeStatePlain(strictState)}是昨日收盘基线；今天盘中广度${breadth || '偏弱'}形成反证，因此今日不新开仓`
        : `${homeStateMeaning(strictState)} 同一观察时点的市场状态与仓位预算已形成；板块入场条件仍单独核验`,
      asOf: raw.as_of || todayDecision.strict_forward_decision_at || todayDecision.phase_clock?.snapshot_server_time || baselineAsOf,
      baselineState, baselineAsOf, liveStatus: 'strict_same_slot', liveAsOf: todayDecision.phase_clock?.snapshot_server_time || null,
      confirmation, currentObservation: true,
    };
  }
  const core = todayDecision.current_slot_research_review
    || route.current_slot_research_review || {};
  const coreState = core.market_state;
  const coreStateReady = core.valid === true
    && !['', 'unknown', 'undefined', 'null'].includes(String(coreState).toLowerCase());
  if (sessionActive && coreStateReady) {
    const confidence = core.market_confidence == null
      ? Number.NaN : Number(core.market_confidence);
    const liveIntraday = route.intraday_emotion_observation || {};
    const useLiveIntraday = liveIntraday.status === 'reference_cohort_only'
      && Boolean(liveIntraday.raw_observation?.as_of);
    const intraday = useLiveIntraday
      ? liveIntraday : core.intraday_emotion_observation || {};
    const raw = intraday.raw_observation || {};
    const confirmation = intraday.scope?.full_a_coverage_verified === true
      ? String(intraday.confirmation || 'not_evaluable') : 'not_evaluable';
    const upRatio = raw.up_ratio == null ? Number.NaN : Number(raw.up_ratio);
    const breadth = Number.isFinite(upRatio) ? ` · 参考样本上涨${fn(upRatio, 1)}%` : '';
    const intradayLabel = confirmation === 'confirmed' ? '盘中确认'
      : confirmation === 'contradicted' ? '盘中反证' : '盘中待核验';
    const sectors = Array.isArray(core.confirmed_sectors)
      ? core.confirmed_sectors.map(row => row?.name).filter(Boolean) : [];
    return {
      value: `${homeStatePlain(coreState)} · ${intradayLabel}${breadth}`,
      professional: `${coreState} · ${confirmation} · verified_core_route_research_only`,
      semantic: confirmation === 'contradicted'
        || coreState === 'retreat' || coreState === 'risk_closed'
        ? 'danger' : 'info',
      meaning: Number.isFinite(upRatio)
        ? `${homeStateMeaning(coreState)} 同槽参考样本上涨 ${raw.up ?? '—'} 家、下跌 ${raw.down ?? '—'} 家、涨停 ${raw.limit_up ?? '—'} 家、跌停 ${raw.limit_down ?? '—'} 家；全 A 覆盖待核验。`
        : homeStateMeaning(coreState),
      confidenceMeaning: Number.isFinite(confidence)
        ? `规则匹配完整度 ${fn(confidence * 100, 0)}%，不是上涨概率、胜率或收益承诺`
        : '规则匹配完整度不可用，不推断胜率',
      positionMeaning: '旧同槽参考协议记录已核；参考样本不能确认全 A 情绪，板块、账户和策略仍需分别核验，保持真实仓位，不生成股数或提醒',
      summary: `${homeStateMeaning(coreState)}${sectors.length ? ` 当前研究主线：${sectors.join('、')}。` : ''} ${useLiveIntraday ? '最新参考样本广度' : '同槽参考样本广度'}为${intradayLabel}，全 A 覆盖待核验，不授权交易。`,
      asOf: raw.as_of || core.decision_at || core.market_state_as_of || baselineAsOf,
      baselineState, baselineAsOf,
      liveStatus: useLiveIntraday
        ? 'live_intraday_observation_research_only'
        : 'verified_core_route_research_only',
      liveAsOf: raw.as_of || core.decision_at || null,
      confirmation, currentObservation: true,
    };
  }
  const intraday = route.intraday_emotion_observation || {};
  const raw = intraday.raw_observation || {};
  const confirmation = intraday.scope?.full_a_coverage_verified === true
    ? String(intraday.confirmation || 'not_evaluable') : 'not_evaluable';
  const liveAsOf = raw.as_of || null;
  const intradayReady = intraday.status === 'reference_cohort_only';
  if (sessionActive && intradayReady) {
    const confirmed = confirmation === 'confirmed';
    const contradicted = confirmation === 'contradicted';
    const upRatio = raw.up_ratio == null ? Number.NaN : Number(raw.up_ratio);
    const breadth = Number.isFinite(upRatio) ? ` · 参考样本上涨${fn(upRatio, 1)}%` : '';
    const value = confirmed ? `盘中延续${breadth}` : contradicted ? `盘中偏弱${breadth}` : `全 A 待核验${breadth}`;
    const boundary = contradicted
      ? '该反证不等于已经识别出退潮或其他新阶段'
      : '即时观察尚未形成同一观察时点的仓位预算';
    return {
      value,
      professional: `${baselineReady ? baselineState : 'UNKNOWN'} · ${confirmation}`,
      semantic: contradicted ? 'danger' : confirmed ? 'warning' : 'muted',
      meaning: `参考样本上涨 ${raw.up ?? '—'} 家、下跌 ${raw.down ?? '—'} 家、涨停 ${raw.limit_up ?? '—'} 家、跌停 ${raw.limit_down ?? '—'} 家；交易所全集覆盖未核验。`,
      confidenceMeaning: '这是参考样本广度观察，不是全 A 情绪确认、上涨概率、胜率或收益承诺',
      positionMeaning: contradicted
        ? '盘中证据反驳上一交易日强势基线；在严格同槽决策包形成前，不据此放大仓位'
        : '盘中证据尚未形成严格同槽决策包，不据此放大仓位',
      summary: `上一完成日基线：${baselineLabel}；盘中即时观察：${intraday.summary || '待核验'}；${boundary}，不控制真实仓位`,
      asOf: liveAsOf || baselineAsOf, baselineState, baselineAsOf,
      liveStatus: intraday.status, liveAsOf, confirmation, currentObservation: true,
    };
  }
  if (sessionActive) {
    return {
      value: '盘中待核验', professional: `基准 ${baselineReady ? baselineState : 'UNKNOWN'}`, semantic: 'muted',
      summary: `上一完成日基线：${baselineLabel}；今日盘中证据缺失或过期，不控制真实仓位`,
      asOf: baselineAsOf, baselineState, baselineAsOf,
      liveStatus: intraday.status || 'unavailable', liveAsOf, confirmation, currentObservation: false,
    };
  }
  const completedStateAsOf = completed.market_state_as_of || null;
  const baselineIsNewer = baselineReady && baselineAsOf && (
    !completedStateAsOf || String(baselineAsOf).slice(0, 10) > String(completedStateAsOf).slice(0, 10)
  );
  if (baselineIsNewer) {
    const preopen = todayDecision.session_status === 'preopen';
    return {
      value: `${preopen ? '盘前基线' : '最新收盘'} · ${baselineLabel}`,
      professional: `${baselineState} · latest_verified_eod`,
      semantic: baselineState === 'retreat' || baselineState === 'risk_closed' ? 'danger' : 'info',
      meaning: homeStateMeaning(baselineState),
      confidenceMeaning: '收盘规则已完整计算，但不是次日涨跌概率、胜率或收益承诺',
      positionMeaning: '用于下一交易时段的研究起点；盘中仍须重新核验板块、战法、账户和可交易性，当前保持真实仓位',
      summary: `截至 ${String(baselineAsOf).slice(0, 10)} 的最新收盘基线为${baselineLabel}；尚未形成同一观察时点的完整买卖计划，不新开仓`,
      asOf: baselineAsOf, baselineState, baselineAsOf,
      liveStatus: 'latest_verified_eod', liveAsOf: null,
      confirmation: 'next_session_baseline', currentObservation: false,
      latestCompletedBaseline: true,
    };
  }
  const completedState = completed.market_state;
  const completedStateReady = completed.valid === true
    && !['', 'unknown', 'undefined', 'null'].includes(String(completedState).toLowerCase());
  if (completedStateReady) {
    const confidence = completed.market_confidence == null ? Number.NaN : Number(completed.market_confidence);
    const researchTarget = completed.target_exposure_pct == null ? Number.NaN : Number(completed.target_exposure_pct);
    const reviewConfirmation = String(completed.intraday_confirmation || 'not_evaluable');
    const reviewContradicted = reviewConfirmation === 'contradicted';
    const sameDay = String(completed.decision_day || '')
      === String(todayDecision.phase_clock?.snapshot_server_time || '').slice(0, 10);
    const reviewPrefix = sameDay ? '今日收盘复盘' : '上一交易日复盘';
    return {
      value: reviewContradicted
        ? `${reviewPrefix} · 偏强基线未延续`
        : `${reviewPrefix} · ${homeStatePlain(completedState)}`,
      professional: `${completedState} · completed_review${reviewContradicted ? ' · contradicted' : ''}`,
      semantic: reviewContradicted ? 'danger' : 'neutral',
      meaning: reviewContradicted
        ? `规则识别出${homeStatePlain(completedState)}基线，但盘中市场广度形成反证，因此没有开放新交易。`
        : homeStateMeaning(completedState),
      confidenceMeaning: Number.isFinite(confidence)
        ? `规则匹配完整度 ${fn(confidence * 100, 0)}%，不是上涨概率、胜率或收益承诺`
        : '规则匹配完整度不可用，不推断胜率',
      positionMeaning: reviewContradicted
        ? `盘中反证已把新增交易降为0；${Number.isFinite(researchTarget) ? `${fn(researchTarget, 0)}%仅是反证前的研究预算` : '研究预算不可用'}，当前只复盘真实账户，不沿用为执行仓位`
        : `该快照曾形成 ${Number.isFinite(researchTarget) ? `${fn(researchTarget, 0)}%` : '未核验'} 的研究预算；当前只用于复盘，执行仓位必须读取真实账户并重新核验，不沿用这个数值`,
      summary: reviewContradicted
        ? `${sameDay ? '今日' : '上一交易日'}盘中证据反驳偏强基线，严格路由将新交易降为0；收盘后只复盘，不生成新提醒`
        : `${homeStateMeaning(completedState)} ${sameDay ? '这是今日收盘前冻结观察' : '这是上一交易日冻结观察'}；当前递归校验因上游候选快照刷新已关闭交易权限，只用于复盘，不生成新提醒`,
      asOf: completed.decision_at || completed.market_state_as_of || baselineAsOf,
      baselineState, baselineAsOf, liveStatus: 'release_bound_completed_review',
      liveAsOf: completed.decision_at || null, confirmation: 'review_only',
      currentObservation: false, reviewSnapshot: true,
    };
  }
  const routeReady = route.status === 'research_route_ready';
  return {
    value: baselineLabel, professional: baselineReady ? String(baselineState) : 'UNKNOWN',
    semantic: routeReady ? 'info' : baselineReady ? 'warning' : 'muted',
    meaning: homeStateMeaning(baselineState),
    confidenceMeaning: '规则匹配完整度不可用，不推断胜率',
    positionMeaning: '未形成当前可用仓位结果',
    summary: baselineReady
      ? `上一完成日研究基线：${baselineLabel}；${routeReady ? '研究路由已就绪' : '路由等待同一观察时点的板块确认'}；不控制真实仓位`
      : '情绪证据不足，路由未就绪，不控制真实仓位',
    asOf: baselineAsOf, baselineState, baselineAsOf,
    liveStatus: intraday.status || 'unavailable', liveAsOf, confirmation, currentObservation: false,
  };
}

function homeMarketStateContext(presentation = {}) {
  if (presentation.reviewSnapshot) return {
    title: '今日收盘复盘状态',
    detail: '收盘前冻结结果 · 只解释今天 · 下个交易时段重新判断',
  };
  if (presentation.liveStatus === 'strict_same_slot') return {
    title: '当前盘中市场状态',
    detail: '当前观察时点已核验 · 仍须通过板块、战法和账户约束',
  };
  if (presentation.liveStatus === 'verified_core_route_research_only') return {
    title: '旧同槽参考记录',
    detail: '参考协议身份已核 · 全 A 情绪待核 · 板块与账户另行验收',
  };
  if (presentation.currentObservation) return {
    title: '盘中状态校验',
    detail: presentation.confirmation === 'contradicted'
      ? '当前广度反驳上一交易日基线 · 尚未识别新的情绪阶段'
      : '参考样本仅供观察 · 全 A 情绪待核 · 尚未形成新的仓位结论',
  };
  return {
    title: '上一交易日市场基线',
    detail: '仅作为今天的起点 · 盘中证据不足时不调整仓位',
  };
}

function homeEvidencePlain(value) {
  const text = String(value || '');
  const percentile = text.match(/^([a-z0-9_]+) lagged-percentile=([0-9.]+)%$/i);
  if (percentile) {
    const labels = {
      up_ratio_pct:'上涨家数占比', above_ma20_ratio_pct:'站上20日均线股票占比',
      new_high_minus_low_ratio_pct:'20日新高相对新低强度',
      yesterday_limit_continuation_ratio_pct:'昨日涨停延续率', highest_board:'连板高度',
      closed_limit_ratio_pct:'触板封住比例', eod_failed_limit_ratio_pct:'收盘触板失败比例',
      top_5pct_amount_share_pct:'成交额头部集中度',
    };
    return `${labels[percentile[1]] || percentile[1]}处于历史约 ${Number(percentile[2]).toFixed(1)} 分位`;
  }
  const h1 = text.match(/^H1 confirmed=([^;]+); raw=(.+)$/);
  if (h1) return `历史日终状态规则：确认 ${homeStatePlain(h1[1])}，原始状态 ${homeStatePlain(h1[2])}`;
  const missingLabels = {
    sector_confirmation:'板块确认', sector_observation_slot:'板块观察时槽',
    sector_advance_coverage_pct:'板块上涨覆盖率', sector_change_rank_pct:'板块强度排名',
    sector_constituent_count:'板块成分数量', sector_confirmed_sectors:'已确认板块',
    sector_point_in_time_snapshot_contract:'板块时点快照', user_recorded_cash:'用户录入现金',
    account_snapshot_sha256:'账户快照身份',
  };
  return missingLabels[text] || text;
}

function homeTraderLensPlain(lensId) {
  return ({
    emotion_cycle:'情绪周期与风险预算', sector_front_row:'主线前排与板块扩散',
    ladder_extremes:'高低位梯队与极端切换', weak_market_and_hot_sector:'弱市控仓与热点聚焦',
    predict_confirm_follow:'预判、确认与跟随', high_energy_and_tradability:'高能期与可成交性',
    mode_discipline:'模式纪律', consensus_and_error_exit:'市场合力与错误退出',
  })[String(lensId || '')] || String(lensId || '研究视角');
}

function homeTraderStancePlain(stance) {
  return ({
    defensive:'防守', avoid_chasing:'不追后排', selective_participation:'选择性参与',
    wait_for_confirmation:'等待确认', sector_confirmed_research:'板块确认研究',
    data_incomplete:'数据不完整', confirmed_route:'路由已确认', inside_mode:'模式内',
    outside_mode_veto:'模式外否决', requires_predeclared_invalidation:'先写失效条件',
  })[String(stance || '')] || '研究解读';
}

function homeTraderEvidencePlain(grades) {
  const labels = {
    A1:'原始或官方材料', A2:'已核验原始平台内容',
    B:'可信媒体或访谈转述', C:'二手资料，仅作研究线索',
  };
  const values = Array.isArray(grades) ? grades : [];
  return values.length
    ? [...new Set(values.map(value => labels[String(value)] || '来源等级待解释'))].join(' / ')
    : '来源等级待核验';
}

function homeTraderLensesHtml(route) {
  const role = route?.public_trader_style_role || {};
  const distillation = role.distillation || {};
  const styleLenses = role.style_lenses || {};
  const lenses = Array.isArray(styleLenses.lenses) ? styleLenses.lenses : [];
  const context = styleLenses.context || {};
  const contextSectors = Array.isArray(context.confirmed_sectors)
    ? context.confirmed_sectors.map(row => row?.name).filter(Boolean) : [];
  const contextText = ['latest_verified_strict_forward_packet', 'release_bound_completed_intraday_packet'].includes(context.source)
    ? `${context.source_label || '最近一次已核验的盘中快照'} · ${context.as_of || '时点未知'}${contextSectors.length ? ` · 已确认 ${contextSectors.join('、')}` : ''}`
    : '当前没有可复用的已核验盘中板块快照；缺失时不推断。';
  return `<section class="decision-trader-console">
    <header><div><small>公开经验研究视角</small><b>顶级游资方法解读</b></div><span>资料覆盖尚未完成 · 定量准入 ${distillation.quantitatively_admitted ?? 0}</span></header>
    <p class="decision-trader-boundary">${esc(contextText)}<br>先由统一量化形成候选，再用公开经验解释；不独立选股，不改变总分、仓位或提醒权限，人物名称不产生推荐。</p>
    <div class="decision-trader-grid">${lenses.length ? lenses.map(lens => `<article data-stance="${esc(lens.stance || 'unknown')}">
      <div><b>${esc(lens.provenance_subject || '匿名研究')}</b><span>${esc(lens.plain_language?.status || homeTraderStancePlain(lens.stance))}</span></div>
      <small>${esc(homeTraderLensPlain(lens.lens_id))} · 来源：${esc(homeTraderEvidencePlain(lens.evidence_grades))}</small>
      <dl><dt>当前结论</dt><dd>${esc(lens.plain_language?.conclusion || lens.view || '当前没有可展示的解读。')}</dd><dt>为什么</dt><dd>${esc(lens.plain_language?.reason || '尚未提供可复核原因。')}</dd><dt>反向提醒</dt><dd>${esc(lens.plain_language?.counterpoint || '仍需核对反向证据。')}</dd></dl>
      <details><summary>适用阶段与下一步观察</summary><p><b>适用：</b>${esc(lens.plain_language?.applies_when || '未说明')}</p><p><b>观察：</b>${esc(lens.plain_language?.watch_next || '未说明')}</p></details>
    </article>`).join('') : '<div class="decision-empty"><b>游资研究镜头不可用</b><span>不会用人物叙事替代市场数据或生成推荐。</span></div>'}</div>
    <footer>${distillation.public_subject_count ?? 0} 位公开市场人物 · ${distillation.evidence_record_count ?? 0} 条来源记录 · 来源可信度不代表策略胜率或可执行性</footer>
  </section>`;
}

function homeMarketCognitionHtml(research, route, todayDecision = {}) {
  const presentation = homeMarketStatePresentation(research, route, todayDecision);
  const presentationContext = homeMarketStateContext(presentation);
  const diagnostic = research?.model_observation || {};
  const values = diagnostic.percentiles_pct || {};
  const factors = [
    ['up_ratio_pct', '上涨覆盖'], ['above_ma20_ratio_pct', '均线广度'],
    ['new_high_minus_low_ratio_pct', '新高强度'], ['yesterday_limit_continuation_ratio_pct', '涨停延续'],
    ['highest_board', '连板高度'], ['closed_limit_ratio_pct', '封板质量'],
    ['eod_failed_limit_ratio_pct', '炸板压力'], ['top_5pct_amount_share_pct', '成交集中'],
  ].filter(([key]) => typeof values[key] === 'number' && Number.isFinite(values[key]));
  const sector = route?.sector_confirmation || {};
  const sectorEntryGateReady = todayDecision.sector_entry_gate_ready === true;
  const sectorConfirmationReady = todayDecision.sector_confirmation_ready === true;
  const researchSectorReady = todayDecision.research_sector_confirmation_ready === true;
  const researchCandidateInputReady = todayDecision.research_candidate_input_ready === true;
  const confirmedSectors = homeSectorRows(route, todayDecision);
  const reviewSectorReady = !sectorEntryGateReady && confirmedSectors.length > 0;
  const completed = todayDecision.completed_market_review || route?.completed_market_review || {};
  const currentDay = String(todayDecision.phase_clock?.snapshot_server_time || '').slice(0, 10);
  const reviewSectorSameDay = Boolean(currentDay && String(completed.decision_day || '') === currentDay);
  const reviewSectorTitle = researchSectorReady
    ? `当前研究确认 ${confirmedSectors.length} 个`
    : reviewSectorSameDay ? `今天曾确认 ${confirmedSectors.length} 个` : `上一交易日确认 ${confirmedSectors.length} 个`;
  const playbooks = homePlaybookNames(route, todayDecision);
  const forecast = route?.next_market_state_forecast || {};
  const forecastRows = forecast.research_display_admitted === true
    ? Object.entries(forecast.probabilities || {}).sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 3)
    : [];
  const routeReady = route?.status === 'research_route_ready'
    || todayDecision.strict_forward_packet_valid === true;
  const routeBridge = homeMarketRouteBridge(research, route);
  return `<section class="decision-cognition-console">
    <header><div><small>市场情绪决策中心</small><b>${esc(presentation.value)}</b><span>${esc(presentationContext.detail)}</span></div><time>${esc(String(presentation.asOf || '时点不可用').slice(0, 16))}</time></header>
    <div class="decision-cognition-status">
      <div><small>${esc(presentationContext.title)}</small><b data-semantic="${presentation.semantic}">${esc(presentation.value)}</b><span>${esc(presentationContext.detail)}</span></div>
      <div><small>${forecastRows.length ? '下一状态' : '下一步确认'}</small><b>${forecastRows.length ? `${esc(homeStatePlain(forecastRows[0][0]))} ${fn(Number(forecastRows[0][1]) * 100, 1)}%` : '按条件准备，不作预测'}</b><span>${forecastRows.length ? '状态概率，不是收益概率' : '<a href="/next-session-plan#plan-sectors">查看观察板块、确认与放弃条件</a>；历史预测未获前向准入，不用于仓位。'}</span></div>
      <div><small>板块确认</small><b>${sectorConfirmationReady ? `${confirmedSectors.length} 个同槽确认` : reviewSectorReady ? reviewSectorTitle : '当前没有确认板块'}</b><span>${sectorConfirmationReady ? (sectorEntryGateReady ? '当前盘中证据已核验，可继续检查战法与个股' : '研究主线已确认；盘中情绪反证或生产策略未准入，不开放买入') : researchSectorReady ? `当前同槽市场与板块已核验；${researchCandidateInputReady ? '成分股候选已核验，但严格账户和策略包未完成' : '成分股候选输入未完整核验'}，不开放买入` : reviewSectorReady ? `${reviewSectorSameDay ? '仅用于今天收盘复盘' : '仅用于上一交易日复盘'}；当前交易日必须重新确认` : '板块未确认，不开放入场战法'}</span></div>
      <div><small>战法研究路由</small><b>${playbooks.length ? esc(playbooks.join(' / ')) : '无准入战法'}</b><span>情景匹配不等于可执行策略；<a href="/risk">实际仓位与约束见账户风控</a>，不采用未应用的研究仓位。</span></div>
    </div>
    ${routeBridge ? `<p class="decision-source-note"><b>为什么看起来不同：</b>${esc(routeBridge)}</p>` : ''}
    <div class="decision-emotion-factor-grid">${factors.length ? factors.map(([key, label]) => {
      const value = Math.max(0, Math.min(100, Number(values[key])));
      return `<div><span>${label}</span><i><em style="width:${value}%"></em></i><b>${fn(value, 1)}<small>分位</small></b></div>`;
    }).join('') : '<div class="decision-empty"><b>情绪分解数据不可用</b><span>不会用单一温度计制造虚假精度。</span></div>'}</div>
    <p class="decision-cognition-conclusion"><b>${routeReady && sectorConfirmationReady ? '今日研究路径已形成' : researchSectorReady ? '当前市场与板块研究已形成，交易权限仍关闭' : presentation.reviewSnapshot ? '今日市场复盘已形成，交易权限已关闭' : '当前结论：检测到情绪，但完整策略路由尚未形成'}</b><span><b>这是什么意思：</b>${esc(presentation.meaning || presentation.summary)}<br><b>置信度：</b>${esc(presentation.confidenceMeaning || '不可用')}<br><b>对仓位的影响：</b>${esc(presentation.positionMeaning || '不改变真实仓位')}<br>${esc(presentation.summary)}；${sectorConfirmationReady ? '当前盘中板块证据已核验。' : researchSectorReady ? '当前同槽板块研究已核验，但账户与策略严格包未完成。' : reviewSectorReady ? '收盘前板块证据已冻结，仅用于复盘。' : '等待同一观察时点的盘中板块证据。'}</span></p>
  </section>`;
}

function homeStrategyAuditHtml(strategyEvidence = {}) {
  const program = strategyEvidence?.current_research_program || {};
  const entry = program.independent_entry_strategy || {};
  const tactics = program.leader_board_tactic_validation || {};
  const factor = program.latest_terminal_factor_candidate || {};
  const sourceReview = program.next_independent_source_review || {};
  const results = Array.isArray(tactics.results) ? tactics.results : [];
  const resultText = results.length ? results.map(row => {
    const net = Number(row.sealed_test_10bp_mean_net_return_pct);
    return `${row.label || row.id} ${Number.isFinite(net) ? `封存段均值 ${fn(net, 2)}%` : '未通过封存检验'}`;
  }).join('；') : '尚无完整检验结果';
  return `<section class="decision-strategy-audit" data-admitted="${Number(entry.admitted_count || 0)}">
    <header><div><small>情绪分段策略验收</small><b>战法、因子与提醒权限</b></div><span>生产入场 ${Number(entry.admitted_count || 0)} · 自动下单 0</span></header>
    <div>
      <p><b>追龙头 / 单次打板</b><span>${esc(`${Number(tactics.tested_count || 0)} 条已检验 · ${Number(tactics.admitted_count || 0)} 条准入`)}</span><small>${esc(resultText)}。${esc(tactics.direct_locked_board_reason || '')}</small></p>
      <p><b>辅助因子</b><span>${esc(factor.label || '暂无新结果')}</span><small>${factor.id ? `验证段 Rank IC ${fn(factor.validation_mean_rank_ic, 4)}，封存段 ${fn(factor.sealed_test_mean_rank_ic, 4)}；已归档且不阻塞非因子主链。` : '因子只参与辅助排序，不阻塞交易主链。'}</small></p>
      <p><b>下一来源审计</b><span>${esc(sourceReview.label || '等待独立来源')}</span><small>${esc(sourceReview.reason || '取得精确公式并冻结输入合同后才允许回测。')}</small></p>
    </div>
  </section>`;
}

function homeIntelligenceHtml(research, route, todayDecision = {}, strategyEvidence = {}) {
  return `<div class="decision-intelligence-grid">${homeStrategyAuditHtml(strategyEvidence)}${homeMarketCognitionHtml(research, route, todayDecision)}${homeTraderLensesHtml(route)}</div>`;
}

function homeMetricValue(id, value, semantic = 'neutral') {
  const element = $(`#${id}`);
  if (!element) return;
  const text = String(value);
  const changed = element.dataset.visualReady === 'true' && element.textContent !== text;
  element.textContent = text;
  element.dataset.semantic = semantic;
  const metric = element.closest('.decision-metric');
  if (metric) metric.dataset.semantic = semantic;
  if (changed && !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    element.classList.remove('decision-value-change');
    metric?.classList.remove('decision-metric-change');
    requestAnimationFrame(() => {
      element.classList.add('decision-value-change');
      metric?.classList.add('decision-metric-change');
    });
  }
  element.dataset.visualReady = 'true';
}

function homeMarketBoardHtml({research, route, portfolio, summary, open, opportunities, sectorNames, targetExposure, targetSource, exposureCeiling, todayDecision, riskReport, strategyEvidence}) {
  const marketBudgetValid = todayDecision.strict_market_budget_valid === true
    && route.intraday_emotion_observation?.scope?.full_a_coverage_verified === true;
  const sectorEntryGateReady = todayDecision.sector_entry_gate_ready === true;
  const sectorConfirmationReady = todayDecision.sector_confirmation_ready === true;
  const reviewSectorReady = !sectorEntryGateReady && sectorNames.length > 0;
  const currentSectorObservation = route.sector_confirmation || {};
  const observedSectors = Array.isArray(currentSectorObservation.observed_sectors)
    ? currentSectorObservation.observed_sectors : [];
  const observedSectorLabels = ['industry', 'concept'].map(kind => {
    const row = observedSectors.find(item => item?.kind === kind);
    return row?.name ? `${kind === 'industry' ? '行业' : '概念'} ${row.name}` : null;
  }).filter(Boolean);
  const completedReview = todayDecision.completed_market_review || route.completed_market_review || {};
  const coreReview = todayDecision.current_slot_research_review
    || route.current_slot_research_review || {};
  const researchSectorConfirmationReady = todayDecision.research_sector_confirmation_ready === true;
  const researchCandidateInputReady = todayDecision.research_candidate_input_ready === true;
  const currentDay = String(todayDecision.phase_clock?.snapshot_server_time || '').slice(0, 10);
  const reviewDecisionDay = String(researchSectorConfirmationReady
    ? coreReview.decision_day || '' : completedReview.decision_day || '');
  const reviewSectorSameDay = Boolean(currentDay && reviewDecisionDay === currentDay);
  const reviewSectorTitle = researchSectorConfirmationReady
    ? `当前同槽确认 ${sectorNames.length} 个`
    : reviewSectorSameDay ? `今天曾确认 ${sectorNames.length} 个` : `上一交易日确认 ${sectorNames.length} 个`;
  const marketPresentation = homeMarketStatePresentation(research, route, todayDecision);
  const marketContext = homeMarketStateContext(marketPresentation);
  const marketBudgetTruth = targetSource === 'bound_trade_plan'
    ? '总仓上限已绑定完整买卖计划 · 仍需人工确认'
    : targetSource === 'account_sell_plans'
      ? '已绑定当前持仓卖出计划 · 仅在人工成交后生效'
    : targetSource === 'hold_current'
      ? '当前没有完整买卖计划 · 执行目标等于实际仓位 · 不主动增加暴露'
      : `当前无法形成执行目标${Number.isFinite(exposureCeiling) ? ` · ${fn(exposureCeiling, 0)}%仅是账户风险上限，不是建议仓位` : ''}`;
  const replay = route.system_replay_readiness || {};
  const historicalStrategies = Array.isArray(strategyEvidence?.strategies)
    ? strategyEvidence.strategies : [];
  const historicalRiskPasses = historicalStrategies.filter(
    item => item.verdict === 'historical_diagnostic_pass'
  ).length;
  const historicalFailures = historicalStrategies.filter(
    item => ['terminal_blind_failure', 'historical_preflight_falsified'].includes(item.verdict)
  ).length;
  const researchProgram = strategyEvidence?.current_research_program || {};
  const admittedEntryCount = Number(researchProgram.independent_entry_strategy?.admitted_count) || 0;
  const activeEntryCandidate = researchProgram.active_entry_candidate || {};
  const namedTacticValidation = researchProgram.leader_board_tactic_validation || {};
  const latestTerminalFactor = researchProgram.latest_terminal_factor_candidate || {};
  const activeRiskOverlay = researchProgram.active_risk_overlay || {};
  const activeRiskOverlayCount = activeRiskOverlay.id ? 1 : 0;
  const activeRiskOverlayRuntimeReady = activeRiskOverlayCount > 0
    && riskReport?.volatility_managed_exposure?.available === true;
  const currentPlaybooks = homePlaybookNames(route, todayDecision);
  const terminalEntryCount = Array.isArray(researchProgram.terminal_entry_strategy_ids)
    ? researchProgram.terminal_entry_strategy_ids.length : historicalFailures;
  const namedTacticEvidence = Number(namedTacticValidation.tested_count || 0)
    ? `追龙头/打板 ${Number(namedTacticValidation.tested_count)} 条已检验、${Number(namedTacticValidation.admitted_count || 0)} 条准入`
    : '追龙头/打板尚无完整验证';
  const auxiliaryFactorEvidence = latestTerminalFactor.id
    ? `辅助因子“${latestTerminalFactor.label || latestTerminalFactor.id}”封存样本外失败，不阻塞主链`
    : '辅助因子不阻塞主链';
  const strategyEvidenceText = strategyEvidence?.historical_daily_replay_available === true
    ? `当前适用战法 ${currentPlaybooks.length ? currentPlaybooks.join('、') : '0 项'} · 生产入场 ${admittedEntryCount} 项 · ${namedTacticEvidence} · ${auxiliaryFactorEvidence} · ${activeEntryCandidate.label ? `${activeEntryCandidate.label}已冻结、待完整PIT输入` : '暂无可推进候选'} · 研究风控覆盖层 ${activeRiskOverlayCount} 项${activeRiskOverlayRuntimeReady ? `（${activeRiskOverlay.label || activeRiskOverlay.id}正在工作）` : '（等待完整输入）'} · ${terminalEntryCount} 个入场方向已淘汰 · 当前不新开仓`
    : replay.evaluation_ready === true ? '已有联合评估 · 尚未接入股数' : '联合回放证据不可用 · 不参与股数';
  const currentState = route.requested_market_state || research.state || route.market_state || 'UNKNOWN';
  const diagnostic = research.model_observation || {};
  const forecast = route.next_market_state_forecast || {};
  const forecastAllowed = forecast.research_display_admitted === true;
  const forecastRows = forecastAllowed
    ? Object.entries(forecast.probabilities || {}).sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 3)
    : [];
  const routeEvidence = Array.isArray(completedReview.evidence) && completedReview.valid === true
    ? completedReview.evidence : Array.isArray(route.evidence) ? route.evidence : [];
  const routeContradictions = Array.isArray(completedReview.contradictions) && completedReview.valid === true
    ? completedReview.contradictions : Array.isArray(route.contradictions) ? route.contradictions : [];
  const missingInputs = Array.isArray(route.missing_inputs) ? route.missing_inputs : [];
  const lenses = route.public_trader_style_role?.style_lenses?.lenses || [];
  const indexDetails = Array.isArray(portfolio.market_regime?.details) ? portfolio.market_regime.details : [];
  const indexStatus = indexDetails.length ? indexDetails.map(item => {
    const state = item.above_both === true ? '双线上' : item.below_both === true ? '双线下' : '均线混合';
    return `${item.name || '指数'} ${state}`;
  }).join(' / ') : '指数趋势不可用';
  const opportunityItems = Array.isArray(opportunities?.items) ? opportunities.items : [];
  const opportunityCount = Number(opportunities?.candidate_count) || opportunityItems.length;
  const riskPending = open.length > 0 && state.homeRiskStatus === 'loading';
  const riskFailed = open.length > 0 && state.homeRiskStatus === 'error';
  const vmp = riskReport?.volatility_managed_exposure || {};
  const vmpReady = open.length > 0 && vmp.available === true
    && Number.isFinite(Number(vmp.target_exposure_pct))
    && Number.isFinite(Number(vmp.current_exposure_pct));
  const vmpText = vmpReady
    ? `研究参考 ${fn(vmp.target_exposure_pct, 1)}% · 当前 ${fn(vmp.current_exposure_pct, 1)}% · ${Number(vmp.reduction_need_pct_points) > 0 ? `复核降低 ${fn(vmp.reduction_need_pct_points, 1)} 个百分点` : '无需降低'}`
    : riskPending ? '账户风险统计载入中 · 暂不推算数量'
      : riskFailed ? '风险报告接口异常 · 暂不推算数量'
      : open.length ? '波动仓位参考不可用 · 不推算数量' : '当前无持仓 · 无需波动缩放';
  const dependency = riskReport?.dependency || {};
  const stress = riskReport?.historical_stress || {};
  const dependencyText = !open.length
    ? '当前无持仓 · 不存在组合相关性'
    : riskPending ? '组合相关性载入中 · 暂不参与仓位'
    : riskFailed ? '风险报告接口异常 · 组合相关性暂不参与仓位'
    : dependency.available === true
      ? `有效独立风险 ${fn(dependency.effective_risk_bets, 2)}/${open.length} · 最大风险贡献 ${fn(dependency.largest_risk_contribution_pct, 1)}%`
      : `组合相关性不可用 · ${dependency.reason || '需完整现价与至少60个共同交易日'}`;
  const dependencyStatus = !open.length || riskPending ? 'neutral' : dependency.available === true ? 'ok' : 'missing';
  const stressText = !open.length
    ? '当前无持仓 · 无需历史压力估算'
    : riskPending ? '历史尾部压力载入中 · 暂不参与仓位'
    : riskFailed ? '风险报告接口异常 · 尾部压力暂不参与仓位'
    : stress.available === true
      ? `历史最差1日 ${fpp(stress.worst_1d_pct)} · 5日 ${fpp(stress.worst_5d_pct)}`
      : `尾部压力不可用 · ${stress.reason || '需完整共同历史'}`;
  const stressStatus = !open.length || riskPending ? 'neutral' : stress.available === true ? 'ok' : 'missing';
  const factorRows = [
    ['市场情绪', marketBudgetValid ? `${marketPresentation.value} · 参与总仓上限` : marketPresentation.currentObservation ? `${marketPresentation.value} · 参考样本观察，全 A 情绪待核` : `${marketPresentation.value} · 仅历史复盘`, marketBudgetValid ? 'ok' : marketPresentation.currentObservation ? 'research' : 'neutral'],
    ['指数趋势', indexDetails.length ? `${indexStatus} · 参与账户风险上限` : '指数证据缺失 · 不提高仓位', indexDetails.length ? 'ok' : 'missing'],
    ['板块确认', sectorEntryGateReady ? `${sectorNames.length} 个确认 · 控制新仓资格` : researchSectorConfirmationReady ? `${reviewSectorTitle} · ${researchCandidateInputReady ? '候选行情已核验，但账户与策略尚未完成' : '成分股候选行情未完整，暂不生成买入提醒'}` : observedSectorLabels.length ? `盘中领涨观察：${observedSectorLabels.join('；')} · 尚未升级为确认主线` : reviewSectorReady ? `${reviewSectorTitle} · 当前仅复盘` : '当前没有确认板块 · 不开新仓', sectorEntryGateReady ? 'ok' : researchSectorConfirmationReady && !researchCandidateInputReady ? 'warn' : 'neutral'],
    ['策略与战法', strategyEvidenceText, admittedEntryCount > 0 ? 'ok' : 'warn'],
    ['波动仓位参考', vmpText, vmpReady ? 'reference' : riskPending ? 'neutral' : open.length ? 'missing' : 'neutral'],
    ['账户事实', summary.account_facts_complete === true
      ? '现金与估值已绑定 · 参与股数'
      : summary.cash_user_recorded === true
        ? summary.valuation_status === 'complete' && summary.sellable_inventory_status !== 'complete'
          ? `现金与持仓估值已就绪 · 今日可卖数量待核验（${(summary.sellable_inventory_missing_codes || []).join('、') || '持仓'}）`
          : '真实现金已录入 · 持仓估值待恢复 · 不生成股数'
        : '真实现金未录入 · 不生成股数', summary.account_facts_complete === true ? 'ok' : 'input'],
    ['单笔计划损失', '账户权益0.5%上限 · 需有效风险线', 'ok'],
    ['组合相关性', dependencyText, dependencyStatus],
    ['历史尾部压力', stressText, stressStatus],
    ['流动性', opportunityCount ? '有ADV20时参与单股上限' : '候选未形成 · 暂无逐股容量', opportunityCount ? 'ok' : 'neutral'],
  ];
  const support = routeEvidence.length ? routeEvidence.slice(0, 3).map(homeEvidencePlain) : ['当前没有完整、可用于真实决策的支持证据'];
  const sellableMissingCodes = Array.isArray(summary.sellable_inventory_missing_codes)
    ? summary.sellable_inventory_missing_codes.filter(Boolean) : [];
  const missingLimits = missingInputs.slice(0, 3).map(item => {
    if (item === 'account_snapshot_sha256'
        && summary.sellable_inventory_status !== 'complete') {
      return `今日可卖数量待核验（${sellableMissingCodes.join('、') || '当前持仓'}），账户快照因此尚未冻结`;
    }
    return `缺少${homeEvidencePlain(item)}`;
  });
  const against = routeContradictions.length
    ? routeContradictions.slice(0, 3).map(homeEvidencePlain)
    : (missingLimits.length ? missingLimits : ['尚未形成独立反证列表']);
  const factorStatusLabels = {
    ok:'已参与', missing:'数据缺失', input:'待人工核验', neutral:'仅参考',
    research:'研究已核验', reference:'研究参考', warn:'交易链未完成',
  };

  return `${homeHypotheticalLiquidationHtml(summary, open, opportunities, portfolio.market_session?.server_time)}
    <div class="decision-state-flow">
      <section data-semantic="${marketBudgetValid ? 'info' : marketPresentation.currentObservation ? 'warning' : 'muted'}"><small>${esc(marketContext.title)}</small><b>${esc(marketPresentation.value)}</b><span>${esc(marketContext.detail)}</span></section>
      <i aria-hidden="true">→</i>
      <section data-semantic="${forecastRows.length ? 'info' : 'muted'}"><small>${forecastRows.length ? '下一状态判断' : '下一步确认'}</small>${forecastRows.length
        ? `<b>${esc(homeStatePlain(forecastRows[0][0]))} ${fpp(Number(forecastRows[0][1]) * 100)}</b><span>仅状态概率，不是收益概率</span>`
        : '<b>按条件准备，不作预测</b><span><a href="/next-session-plan#plan-sectors">观察板块与失效条件</a>；预测未获准入，不用于仓位</span>'}</section>
      <i aria-hidden="true">→</i>
      <section data-semantic="${sectorEntryGateReady ? 'info' : researchSectorConfirmationReady ? 'warning' : reviewSectorReady ? 'neutral' : 'muted'}"><small>板块确认度</small><b>${sectorEntryGateReady ? `${sectorNames.length} 个当前时点确认` : reviewSectorReady ? reviewSectorTitle : '当前没有确认板块'}</b><span>${sectorEntryGateReady ? '盘中证据已核验，可继续检查战法与个股' : researchSectorConfirmationReady ? `${researchCandidateInputReady ? '当前候选行情已核验；账户与策略仍未授权交易' : '当前板块已确认，但成分股候选行情未完整；不生成买入提醒'}` : reviewSectorReady ? `${reviewSectorSameDay ? '仅作今日复盘' : '仅作上一交易日复盘'} · 当前交易日重新确认` : '板块未确认 · 今天不新增交易'}</span></section>
      <i aria-hidden="true">→</i>
      <section class="decision-state-exposure" data-semantic="${Number.isFinite(targetExposure) ? 'info' : 'muted'}"><small>今日执行总仓位</small><b>${Number.isFinite(targetExposure) ? `${fn(targetExposure, 1)}%` : '不可计算'}</b><span>${marketBudgetTruth}</span></section>
    </div>
    <div class="decision-market-details">
      <section class="decision-factor-board"><h4>仓位计算与缺口</h4>${factorRows.map(([label, value, status]) => `<div><span>${esc(label)}</span><b>${esc(value)}</b><i class="${status}">${factorStatusLabels[status] || '待核验'}</i></div>`).join('')}</section>
      <section class="decision-viewpoints"><div><h4>支持证据 <b>${support.length}</b></h4>${support.map(item => `<p><i class="ok">支持</i>${esc(item)}</p>`).join('')}</div><div><h4>反向证据与限制 <b>${against.length}</b></h4>${against.map(item => `<p><i class="risk">限制</i>${esc(item)}</p>`).join('')}</div></section>
    </div>
    <details class="decision-trader-lenses"><summary>顶级游资经验视角 · ${lenses.length || 0} 个来源分级镜头 · 不独立选股</summary><div>${lenses.length ? lenses.map(item => `<p><b>${esc(item.provenance_subject || item.lens_id || '经验视角')}</b><span>${esc(homeTraderLensPlain(item.lens_id))} · ${esc(homeTraderEvidencePlain(item.evidence_grades))}</span><small><b>结论：</b>${esc(item.plain_language?.conclusion || item.view || '暂无当前解读')}<br><b>风险：</b>${esc(item.plain_language?.counterpoint || '仍需核对反向证据')}</small></p>`).join('') : '<p><small>当前没有可展示的来源分级解读。</small></p>'}</div></details>
    <p class="decision-source-note"><b>阶段解释：</b>${esc(marketPresentation.meaning || marketPresentation.summary)}<br><b>仓位含义：</b>${esc(marketPresentation.positionMeaning || '不改变真实仓位')}<br>专业状态与通俗解释并列展示；诊断完整度、规则置信度和历史状态概率均不是胜率或收益预测。${diagnostic.status ? ' 历史诊断当前仅供观察，不控制仓位。' : ''}</p>`;
}

function homeAuditedNextSession(portfolio, session) {
  const window = portfolio?.buy_window || {};
  const calendar = window.calendar || {};
  const next = String(window.next_session || '');
  const sessionDay = String(session?.server_time || '').slice(0, 10);
  const windowDay = String(window.server_time || '').slice(0, 10);
  if (calendar.source !== 'audited_baostock_calendar'
      || !/^\d{4}-\d{2}-\d{2}$/.test(next)
      || !/^\d{4}-\d{2}-\d{2}$/.test(sessionDay)
      || sessionDay !== windowDay
      || next < sessionDay
      || next < String(calendar.coverage_start || '')
      || next > String(calendar.coverage_end || '')) return null;
  const parsed = new Date(`${next}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === next
    ? next : null;
}

function renderDecisionHome() {
  const home = $('#decisionHome');
  if (!home) return;
  const h = state.homeData;
  const sessionPayload = state.marketSessionPayload || {};
  const session = sessionPayload.session || h.portfolio?.market_session || {};
  const phase = dashboardPhaseFromSession(session);
  const portfolioStatus = h.portfolioStatus || (h.portfolio ? 'ready' : 'loading');
  if (portfolioStatus !== 'ready') {
    home.dataset.workspace = state.homeWorkspace || phase.workspace;
    home.dataset.session = phase.status;
    if (portfolioStatus === 'error') {
      $('#homeDecisionMode').textContent = '账户接口异常';
      $('#homeDecisionHeadline').textContent = '账户事实读取失败：暂不生成仓位与买卖计划';
      $('#homeDecisionExplain').textContent = '系统不会把接口失败显示成0持仓或100%现金；请等待下一次自动重试。';
      home.dataset.verdict = 'warning';
    }
    return;
  }
  const research = h.research || {};
  const route = h.strategyRoute || {};
  const portfolio = h.portfolio || {};
  const summary = portfolio.summary || {};
  const review = h.review || {};
  const opportunities = h.opportunities;
  const accountExecution = opportunities?.account_execution || {};
  const accountBuyBlockers = Array.isArray(accountExecution.blocking_reasons)
    ? accountExecution.blocking_reasons.filter(Boolean) : [];
  const accountBuyCapacity = Number(accountExecution.buy_capacity_shares);
  const accountAvailableCashValue = accountExecution.available_cash_yuan
    ?? summary.available_cash;
  const accountAvailableCash = Number(accountAvailableCashValue);
  const accountAvailableCashKnown = accountAvailableCashValue !== null
    && accountAvailableCashValue !== undefined
    && accountAvailableCashValue !== ''
    && Number.isFinite(accountAvailableCash);
  const accountBuyCapacityKnown = Number.isInteger(accountBuyCapacity);
  const workspace = state.homeWorkspace || phase.workspace;
  const isReview = workspace === 'review';
  const accountComplete = summary.account_facts_complete === true;
  const valuationComplete = summary.valuation_status === 'complete';
  const accountValuationReady = summary.cash_user_recorded === true && valuationComplete;
  const buyAccountReady = accountValuationReady && summary.valuation_executable === true;
  const sellableInventoryComplete = summary.sellable_inventory_status === 'complete';
  const visibleAccountBuyBlockers = accountBuyBlockers.map(reason => (
    session.status === 'break' && valuationComplete
      ? String(reason).replace(
        '持仓仅使用最近完成收盘价估值；等待盘中实时价后重新核验账户估值和买入能力',
        '持仓已按当日11:30完成快照估值；13:00恢复交易后须用实时价重新核验买入能力',
      )
      : reason
  ));
  const routeActionable = homeCompletePlans(route, session);
  const todayDecision = route.today_decision_workspace || {};
  const researchReferencePlans = homeResearchReferencePlans(
    opportunities, todayDecision, session,
  );
  const completedReview = todayDecision.completed_market_review || route.completed_market_review || {};
  const marketBudgetValid = todayDecision.strict_market_budget_valid === true
    && route.intraday_emotion_observation?.scope?.full_a_coverage_verified === true;
  const sectorEntryGateReady = todayDecision.sector_entry_gate_ready === true;
  const sectorConfirmationReady = todayDecision.sector_confirmation_ready === true;
  const researchSectorConfirmationReady = todayDecision.research_sector_confirmation_ready === true;
  const sectorNames = homeSectorNames(route, todayDecision);
  const reviewSectorReady = !sectorEntryGateReady && sectorNames.length > 0;
  const coreReview = todayDecision.current_slot_research_review
    || route.current_slot_research_review || {};
  const reviewDecisionDay = String(
    researchSectorConfirmationReady
      ? coreReview.decision_day || '' : completedReview.decision_day || ''
  );
  const currentDecisionDay = String(todayDecision.phase_clock?.snapshot_server_time || session.server_time || '').slice(0, 10);
  const reviewSectorSameDay = Boolean(currentDecisionDay && reviewDecisionDay === currentDecisionDay);
  const routeReady = route.status === 'research_route_ready'
    || todayDecision.strict_forward_packet_valid === true;
  const playbooks = homePlaybookNames(route, todayDecision);
  const targetExposureValue = !marketBudgetValid || todayDecision.target_exposure_pct == null
    ? Number.NaN : Number(todayDecision.target_exposure_pct);
  const targetExposure = Number.isFinite(targetExposureValue) ? targetExposureValue : null;
  const reviewTargetExposureValue = completedReview.target_exposure_pct == null
    ? Number.NaN : Number(completedReview.target_exposure_pct);
  const reviewTargetExposure = isReview && completedReview.valid === true
    && completedReview.display_only === true && Number.isFinite(reviewTargetExposureValue)
    ? reviewTargetExposureValue : null;
  const exposureCeilingValue = todayDecision.existing_market_budget_ceiling_pct == null
    ? Number.NaN : Number(todayDecision.existing_market_budget_ceiling_pct);
  const portfolioCeilingValue = portfolio.market_regime?.target_exposure_pct == null
    ? Number.NaN : Number(portfolio.market_regime.target_exposure_pct);
  const exposureCeiling = Number.isFinite(exposureCeilingValue)
    ? exposureCeilingValue : Number.isFinite(portfolioCeilingValue) ? portfolioCeilingValue : null;
  const capacity = portfolio.trade_capacity || {};
  const open = Array.isArray(portfolio.open) ? portfolio.open : [];
  const accountSellPlans = homeAccountSellPlans(
    portfolio, opportunities?.allocation_plan, session,
  );
  const accountSellCodes = new Set(accountSellPlans.map(row => String(row.item.code)));
  const actionable = accountSellPlans.concat(routeActionable.filter(
    row => !accountSellCodes.has(String(row.item?.code || '')),
  ));
  const projectedSellExposure = homeProjectedSellExposure(open, accountSellPlans, summary);
  const currentExposureValue = summary.exposure_pct == null
    ? Number.NaN : Number(summary.exposure_pct);
  const homeVmp = h.riskReport?.volatility_managed_exposure || {};
  const homeVmpReady = open.length > 0 && homeVmp.available === true
    && Number.isFinite(Number(homeVmp.target_exposure_pct))
    && Number.isFinite(Number(homeVmp.current_exposure_pct));
  const homeVmpReduction = homeVmpReady
    ? Math.max(0, Number(homeVmp.reduction_need_pct_points) || 0) : 0;
  const completePlanReady = !isReview && todayDecision.actionable_now === true
    && routeActionable.length > 0;
  const holdCurrentExposure = !isReview && accountValuationReady
    && !completePlanReady && Number.isFinite(currentExposureValue)
    ? currentExposureValue : null;
  const executionTargetExposure = !isReview && Number.isFinite(projectedSellExposure)
    ? projectedSellExposure : completePlanReady && Number.isFinite(targetExposure)
      ? targetExposure : holdCurrentExposure;
  const executionTargetSource = !isReview && Number.isFinite(projectedSellExposure)
    ? 'account_sell_plans' : completePlanReady && Number.isFinite(targetExposure)
      ? 'bound_trade_plan' : Number.isFinite(holdCurrentExposure)
      ? 'hold_current' : 'unavailable';
  const isKnownSession = Boolean(session.status && session.status !== 'unknown');
  const nextSlotCandidate = research.capture_runtime?.next_slot
    || route.system_replay_readiness?.strict_forward_capture?.next_real_slot_boundary?.not_before;
  const nextSlotAt = Date.parse(nextSlotCandidate || '');
  const referenceNow = Date.parse(session.server_time || '');
  const nextSlot = Number.isFinite(nextSlotAt) && Number.isFinite(referenceNow)
    && nextSlotAt > referenceNow ? nextSlotCandidate : null;

  home.dataset.workspace = workspace;
  home.dataset.session = phase.status;
  $$('.decision-phase', home).forEach((element, index) => {
    element.className = `decision-phase ${phase.index < 0 ? 'unknown' : index < phase.index ? 'complete' : index === phase.index ? 'current' : 'upcoming'}`;
    if (index === phase.index) element.setAttribute('aria-current', 'step');
    else element.removeAttribute('aria-current');
    const status = $('.decision-phase-status', element);
    if (status) status.textContent = phase.index < 0 ? '状态待核验' : index < phase.index ? '已完成' : index === phase.index ? (session.label || '当前阶段') : HOME_PHASES[index][1];
  });
  updateHomeSessionTimeline(home, session, workspace);
  ensureHomeTimelineTimer();

  const dateLabel = String(session.server_time || review.date || '').replace('T', ' ').slice(0, 16) || '服务时间待核验';
  const sessionLabel = isKnownSession ? (session.label || session.status) : '市场时段未知';
  $('#homeSessionStamp').textContent = `${dateLabel} · ${sessionLabel}`;
  $('#homePrimaryAsOf').textContent = String(research.as_of || session.server_time || '时点待核验').replace('T', ' ').slice(0, 16);

  let headline = '正在核验今日决策条件';
  let headlineNote = '任何缺失输入都不会被填成可执行建议。';
  if (!isKnownSession) {
    headline = '市场时段未核验：暂不生成操作计划';
    headlineNote = '等待审计交易日历与服务端会话恢复。';
  } else if (isReview) {
    const reviewBuyAlertCount = Array.isArray(review.alerts)
      ? review.alerts.filter(item => item?.action === 'buy').length : 0;
    const noBuyRecorded = reviewBuyAlertCount === 0
      && (Number(review.bought_count) || 0) === 0;
    headline = session.status === 'closed' && noBuyRecorded
      ? '今日不买入：未形成完整买入计划'
      : session.status === 'closed'
        ? '今日已收盘：先完成复盘，再准备下一交易日'
        : '复盘工作区';
    const reviewDecisionReason = noBuyRecorded
      ? String(todayDecision.reason || visibleAccountBuyBlockers[0] || '').replace(/[。；]+$/u, '')
      : '';
    headlineNote = reviewDecisionReason
      ? `${reviewDecisionReason}。下一交易日开盘后按新时点重新核验，不补发、不追买。`
      : accountValuationReady
      ? `现金与持仓估值已就绪；可用现金 ${accountAvailableCashKnown ? `¥${fn(accountAvailableCash, 2)}` : '待核验'}。${sellableInventoryComplete ? '' : '下一交易日前仍须核验可卖库存；'}具体买入股数只在盘中绑定股票、限价、风险线与完整计划后显示。`
      : summary.cash_user_recorded === true
        ? '可用现金已录入；当前持仓估值缺少合格行情，不伪造总资产与仓位。'
        : '真实剩余资金尚未录入；下一交易日前先补齐账户事实。';
  } else if (!buyAccountReady) {
    if (summary.cash_user_recorded !== true) {
      headline = '先录入真实剩余资金，再生成精确仓位计划';
      headlineNote = summary.portfolio_action?.reason || '历史账本金额不作为今天可用现金。';
    } else if (!valuationComplete) {
      headline = '等待盘中实时行情，再核验账户估值';
      headlineNote = summary.portfolio_action?.reason || '可用现金已录入；当前持仓估值缺少合格行情。';
    } else {
      headline = '等待当前可执行行情，再核验买入能力';
      headlineNote = summary.portfolio_action?.reason || '现金与持仓估值已记录，但当前报价尚不能用于精确买入股数。';
    }
  } else if (todayDecision.headline && actionable.length === 0) {
    headline = todayDecision.headline === '今日不新开仓' ? '今日不交易：不新增仓位' : todayDecision.headline;
    headlineNote = Array.from(new Set([
      todayDecision.reason,
      ...visibleAccountBuyBlockers,
      opportunities?.buy_window?.note,
    ].filter(Boolean).map(value => String(value).replace(/[。；]+$/u, ''))))
      .slice(0, 3).join('；') || '系统继续监控；不会为凑交易次数降低标准。';
  } else if (actionable.length) {
    headline = `${actionable.length} 个买卖计划待人工复核`;
    headlineNote = '提醒不等于下单；请核对限价、数量、失效条件与账户风险。';
  }
  if (route.research_snapshot?.current === false) {
    if (!actionable.length && buyAccountReady && !isReview) headline = '联合研究快照待更新，暂不判断新开仓';
    headlineNote = `${headlineNote} ${route.research_snapshot.summary}`;
  }
  $('#homeDecisionHeadline').textContent = headline;
  $('#homeDecisionExplain').textContent = headlineNote;
  home.dataset.verdict = !isKnownSession || !buyAccountReady
    ? 'warning' : actionable.length ? 'action' : 'neutral';

  const marketPresentation = homeMarketStatePresentation(research, route, {
    ...todayDecision,
    session_status: session.status,
  });
  const marketContext = homeMarketStateContext(marketPresentation);
  const primaryMarketState = String(marketPresentation.value || '状态未知')
    .replace(/^今日收盘复盘 · /u, '复盘 · ')
    .replace(/^上一交易日复盘 · /u, '昨复盘 · ')
    .replace('偏强基线未延续', '偏强未延续');
  const marketPresentationAsOf = String(
    marketPresentation.asOf || research.as_of || session.server_time || ''
  ).replace('T', ' ').slice(0, 16);
  $('#homePrimaryAsOf').textContent = isReview && review.date
    ? `${review.date}${marketPresentationAsOf ? ` · 市场基线 ${marketPresentationAsOf.slice(0, 10)}` : ''}`
    : marketPresentationAsOf || '时点待核验';
  homeMetricValue('homeMarketState', primaryMarketState, marketPresentation.semantic);
  $('#homeMarketState').title = String(marketPresentation.value || '状态未知');
  $('#homeMarketStateSub').textContent = `${marketContext.detail} · ${String(marketPresentation.asOf || '时点不可用').slice(0, 16)}`;

  if (executionTargetSource === 'account_sell_plans') {
    homeMetricValue('homeExposure', `${fn(executionTargetExposure, 1)}%`, 'danger');
    $('#homeExposureSub').textContent = `完成当前 ${accountSellPlans.length} 项卖出计划后的预计仓位 · 当前实际 ${summary.exposure_pct == null ? '待估值' : `${fn(summary.exposure_pct, 1)}%`} · 必须人工成交后才生效`;
  } else if (executionTargetSource === 'bound_trade_plan') {
    homeMetricValue('homeExposure', `${fn(executionTargetExposure, 0)}%`, 'info');
    $('#homeExposureSub').textContent = `已绑定完整买卖计划 · 当前实际 ${summary.exposure_pct == null ? '待估值' : `${fn(summary.exposure_pct, 1)}%`}${Number.isFinite(exposureCeiling) ? ` · 现行风险上限 ${fn(exposureCeiling, 0)}%` : ''}`;
  } else if (executionTargetSource === 'hold_current') {
    homeMetricValue('homeExposure', `${fn(executionTargetExposure, 1)}%`, homeVmpReduction > 0 ? 'warning' : 'neutral');
    $('#homeExposureSub').textContent = homeVmpReduction > 0
      ? `当前没有正式卖出计划；波动风险研究参考总仓 ${fn(homeVmp.target_exposure_pct, 1)}%，仅供人工减仓复核`
      : `今天没有完整买卖计划，执行目标保持当前实际仓位；${Number.isFinite(exposureCeiling) ? `${fn(exposureCeiling, 0)}%只是风险上限` : '风险上限待核验'}`;
  } else if (Number.isFinite(reviewTargetExposure)) {
    homeMetricValue('homeExposure', '不设目标', 'neutral');
    $('#homeExposureSub').textContent = `收盘前曾形成 ${fn(reviewTargetExposure, 0)}% 研究预算；仅用于复盘，不是当前建议仓位`;
  } else if (Number.isFinite(currentExposureValue)) {
    homeMetricValue('homeExposure', `当前 ${fn(currentExposureValue, 1)}%`, homeVmpReduction > 0 ? 'warning' : 'neutral');
    $('#homeExposureSub').textContent = homeVmpReduction > 0
      ? `精确调整目标待核验；波动风险研究参考 ${fn(homeVmp.target_exposure_pct, 1)}%，先确认今日可卖股数`
      : `当前未形成完整买卖计划，先保持并继续复核${Number.isFinite(exposureCeiling) ? `；${fn(exposureCeiling, 0)}%仅是风险上限` : ''}`;
  } else {
    homeMetricValue('homeExposure', '不设目标', 'muted');
    $('#homeExposureSub').textContent = `当前没有有效的新增仓位目标${Number.isFinite(exposureCeiling) ? `；${fn(exposureCeiling, 0)}%仅是账户风险上限` : ''}`;
  }

  const strictMaximumNewTrades = sectorEntryGateReady
    ? Math.max(0, Number(todayDecision.maximum_new_trades) || 0) : 0;
  const readyBuyPlans = routeActionable.filter(row => row.side === 'BUY').length;
  const allowedTrades = session.is_open === true && buyAccountReady && todayDecision.actionable_now === true
    ? Math.min(strictMaximumNewTrades, readyBuyPlans) : 0;
  homeMetricValue('homeTradeCapacity', isKnownSession ? `${allowedTrades} 笔` : '—', allowedTrades > 0 ? 'action' : 'neutral');
  $('#homeTradeCapacitySub').textContent = marketBudgetValid && !sectorEntryGateReady
    ? todayDecision.intraday_confirmation === 'contradicted'
      ? '盘中上涨覆盖不足，反证昨日强势基线 · 今日不新开仓'
      : sectorConfirmationReady
        ? '研究主线已确认 · 生产策略未准入 · 新交易0'
        : '市场仓位预算已形成 · 板块未确认 · 新交易0'
    : `今日严格新开仓上限 ${strictMaximumNewTrades} 笔 · 制度剩余 ${capacity.remaining ?? '—'} 笔 · 持仓风险复核另计`;

  const sectorHeadline = sectorNames.length > 1
    ? `${sectorNames[0]} +${sectorNames.length - 1}` : sectorNames[0];
  const currentSector = route.sector_confirmation || {};
  const liveObservedSectors = Array.isArray(currentSector.observed_sectors)
    ? currentSector.observed_sectors : [];
  const liveObservedIndustry = liveObservedSectors.find(row => row?.kind === 'industry');
  const liveObservedConcept = liveObservedSectors.find(row => row?.kind === 'concept');
  const liveObservedPrimary = liveObservedIndustry || liveObservedConcept || null;
  const liveObservedLabels = [
    liveObservedIndustry?.name ? `行业：${liveObservedIndustry.name}` : null,
    liveObservedConcept?.name ? `概念：${liveObservedConcept.name}` : null,
  ].filter(Boolean);
  const liveSession = ['open', 'break'].includes(session.status);
  const currentSectorAsOf = String(currentSector.source_as_of || currentSector.as_of || '').replace('T', ' ').slice(0, 16);
  homeMetricValue(
    'homeMainSector',
    sectorConfirmationReady && sectorHeadline
      ? `主线 · ${sectorHeadline}`
      : researchSectorConfirmationReady && sectorHeadline
      ? `研究主线 · ${sectorHeadline}`
      : liveSession && !sectorEntryGateReady && liveObservedPrimary
      ? `${liveObservedPrimary.kind === 'industry' ? '行业' : '概念'}领涨 · ${liveObservedPrimary.name}`
      : liveSession && !sectorEntryGateReady
      ? (route.mainline_diagnostics?.label || '主线输入待核验')
      : reviewSectorReady && reviewSectorSameDay && sectorHeadline
      ? `今日复盘 · ${sectorHeadline}`
      : reviewSectorReady
      ? '暂无今日确认主线'
      : '暂无确认主线',
    sectorConfirmationReady || researchSectorConfirmationReady ? 'info' : 'muted',
  );
  $('#homeMainSector').title = (sectorConfirmationReady || researchSectorConfirmationReady) && sectorHeadline
    ? `${sectorConfirmationReady ? '严格主线' : '研究主线'} · ${sectorNames.join('、')}` : '';
  $('#homeMainSectorSub').textContent = sectorConfirmationReady
    ? `${String(todayDecision.strict_forward_decision_at || '核验时点未提供').replace('T', ' ').slice(0, 19)} · 同一观察时点已核验 · ${sectorNames.length} 个板块 · ${sectorEntryGateReady ? '继续检查战法与个股' : '当前不开放买入'}`
    : researchSectorConfirmationReady
      ? `${String(coreReview.decision_at || '当前时点').replace('T', ' ').slice(0, 16)} · 旧同槽参考记录已核；全 A 情绪、板块范围、账户与策略各自待验，不开放买入`
    : liveSession && liveObservedPrimary
      ? `${currentSectorAsOf || '当前时点'} · ${liveObservedLabels.join('；')}；仅为实时领涨观察，固定窗口确认前不开放入场战法`
    : liveSession
      ? (route.mainline_diagnostics?.reason || '主线输入尚未核验，不能判断市场是否存在主线')
    : reviewSectorReady
      ? `${reviewSectorSameDay ? '今日收盘前' : '上一交易日收盘前'}已核验：${sectorNames.join('、')} · 当前交易日必须重算，不沿用买入授权`
      : `当前板块未确认，不开放入场战法${nextSlot ? ` · 协议候选时段 ${String(nextSlot).replace('T', ' ').slice(0, 16)}（运行另核）` : ''}`;

  const riskReviews = Number(summary.risk_review_count) || 0;
  const valuationLabel = session.status === 'preopen'
    && summary.valuation_status === 'complete'
    ? '上一收盘价估值' : session.status === 'closed'
      && summary.valuation_status === 'complete'
      ? '当日收盘价估值' : summary.valuation_basis === 'intraday_fresh_quote'
        ? '盘中实时估值' : summary.valuation_status === 'complete' ? '估值完整' : '估值待核验';
  homeMetricValue('homeHoldingRisk', open.length ? (riskReviews ? `${riskReviews} 项复核` : '持有观察') : '无持仓', riskReviews ? 'danger' : 'neutral');
  const homeDependency = h.riskReport?.dependency || {};
  $('#homeHoldingRiskSub').textContent = open.length
    ? `${open.length} 只 · ${valuationLabel} · ${homeVmpReduction > 0 ? `波动偏高，研究参考复核降低 ${fn(homeVmpReduction, 1)} 个百分点` : homeDependency.available === true && homeDependency.effective_risk_bets != null ? `有效独立风险 ${fn(homeDependency.effective_risk_bets, 2)}` : '组合相关性待核算'}`
    : (accountValuationReady ? '现金与持仓估值已核验 · 当前无组合风险暴露' : '账户现金待补');

  const afterCostReview = homeAfterCostReviewStatus(review);
  const productionEntryCount = Number(
    h.strategyEvidence?.current_research_program?.independent_entry_strategy?.admitted_count
  ) || 0;
  const strictCandidateCount = Number(opportunities?.account_execution?.strict_candidate_count) || 0;
  const researchCandidateStageReady = todayDecision.research_candidate_input_ready === true;
  const waitingEntryCount = Number(opportunities?.waiting_entry_count) || 0;
  const routeStages = [
    marketPresentation.professional !== 'UNKNOWN' ? 'observed' : 'blocked',
    sectorEntryGateReady ? 'verified' : reviewSectorReady ? 'observed' : 'blocked',
    productionEntryCount > 0 && playbooks.length ? 'verified' : playbooks.length ? 'observed' : 'blocked',
    strictCandidateCount > 0 ? 'verified' : researchCandidateStageReady || waitingEntryCount > 0 ? 'observed' : 'blocked',
    buyAccountReady ? 'verified' : 'blocked',
    actionable.length ? 'active' : 'blocked',
    afterCostReview.status,
  ];
  const routeStageElements = $$('.decision-route-stages > span', home);
  routeStageElements.forEach((stage, index) => {
    stage.dataset.stageStatus = routeStages[index] || 'blocked';
  });
  const strategyStageNote = routeStageElements[2]?.querySelector('small');
  if (strategyStageNote) strategyStageNote.textContent = playbooks.length
    ? `${playbooks.join('、')} · 生产入场 ${productionEntryCount}` : '当前无适用战法';
  const candidateStageNote = routeStageElements[3]?.querySelector('small');
  if (candidateStageNote) candidateStageNote.textContent = strictCandidateCount > 0
    ? `严格候选 ${strictCandidateCount}`
    : researchCandidateStageReady ? '研究输入已核验 · 严格候选 0'
      : waitingEntryCount > 0 ? `评分层 ${waitingEntryCount} · 严格候选 0` : '严格候选 0';
  const afterCostStage = $('#homeAfterCostReviewStage');
  if (afterCostStage) afterCostStage.textContent = afterCostReview.label;

  const evidence = Array.isArray(route.evidence) ? route.evidence : [];
  const contradictions = Array.isArray(route.contradictions) ? route.contradictions : [];
  const intraday = route.intraday_emotion_observation || {};
  const intradayRaw = intraday.raw_observation || {};
  const intradayLine = intraday.status === 'reference_cohort_only'
    ? `盘中参考样本：上涨覆盖 ${intradayRaw.up_ratio == null ? '—' : `${fn(intradayRaw.up_ratio, 1)}%`} · 涨停 ${intradayRaw.limit_up ?? '—'} · 跌停 ${intradayRaw.limit_down ?? '—'} · 全 A 覆盖待核验 · ${String(intradayRaw.as_of || '时点不可用').slice(0, 19)}`
    : null;
  const supportEvidence = [
    ...(intraday.confirmation === 'confirmed' && intradayLine ? [intradayLine, intraday.reason] : []),
    ...evidence.map(item => `上一完成日：${homeEvidencePlain(item)}`),
  ].filter(Boolean);
  const limitingEvidence = [
    ...(intradayLine && intraday.confirmation === 'not_evaluable' ? [intradayLine, intraday.reason] : []),
    ...(intraday.confirmation === 'contradicted' ? [`盘中反证：${intraday.reason || '即时证据反驳上一日基线'}`, intradayLine] : []),
    ...(contradictions.length ? contradictions.map(homeEvidencePlain) : ['未形成独立历史反证列表']),
    ...visibleAccountBuyBlockers,
    coreReview.reason,
    research.reason || '研究状态原因不可用',
    portfolio.market_regime?.observed_reason || '现行指数风险预算原因不可用',
  ].filter(Boolean);
  $('#homeWhyDetails').innerHTML = `
    <div><b>支持证据</b>${supportEvidence.length ? `<ul>${supportEvidence.slice(0, 8).map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : '<p>当前没有可用于真实决策的完整支持证据。</p>'}</div>
    <div><b>反向证据与限制</b><ul>${limitingEvidence.slice(0, 8).map(item => `<li>${esc(item)}</li>`).join('')}</ul><p>盘中确认或反证只校验上一日基线，不自动推断为新阶段。规则匹配完整度不是胜率或盈利置信度。</p></div>`;

  const primaryTitle = $('#homePrimaryTitle');
  const primaryBody = $('#homePrimaryBody');
  const previousEvidence = $('.decision-review-evidence', primaryBody);
  const evidenceWasOpen = previousEvidence
    ? previousEvidence.open === true : homeEvidenceOpen;
  homeEvidenceOpen = evidenceWasOpen;
  const evidenceOpenAttribute = evidenceWasOpen ? ' open' : '';
  if (isReview) {
    primaryTitle.textContent = '市场情绪 · 今日复盘';
    $('#homePrimaryBadge').textContent = '状态 → 板块 → 战法 → 仓位 / 真实记录';
    const counts = review.alert_counts || {};
    const realized = review.realized_pnl;
    const openPnl = review.open_pnl_total;
    primaryBody.innerHTML = `
      <div class="decision-summary-row">
        <span><small>可操作提醒</small><b>${counts.total ?? '—'} 次</b></span>
        <span><small>研究观察</small><b>${review.research_observation_counts?.total ?? '—'} 条</b></span>
        <span><small>实际买入</small><b>${review.bought_count ?? '—'} 笔</b></span>
        <span><small>卖出事件</small><b>${review.sell_event_count ?? review.closed_count ?? '—'} 笔</b></span>
        <span><small>${afterCostReview.status === 'verified' ? '已实现成本后净盈亏' : '已实现记录盈亏'}</small><b class="${realized == null ? '' : cc(realized)}">${realized == null ? '—' : `${realized >= 0 ? '+' : ''}¥${fn(realized, 2)}`}</b></span>
        <span><small>持仓浮盈</small><b class="${openPnl == null ? '' : cc(openPnl)}">${open.length && openPnl == null ? '行情不可用' : openPnl == null ? '—' : `${openPnl >= 0 ? '+' : ''}¥${fn(openPnl, 2)}`}</b></span>
      </div>
      ${homeSignalHistorySection(review)}
      ${RegisSpa.review?.renderFillReview(review) || ''}
      <div class="decision-section-label">今日决策与成交时间线</div>
      <div class="decision-log-list">${homeTimelineRows(review)}</div>
      <details class="decision-review-evidence"${evidenceOpenAttribute}><summary>市场研究、策略、板块与游资证据（保持展开状态）</summary>${homeIntelligenceHtml(research, route, todayDecision, h.strategyEvidence)}</details>
      <div class="decision-panel-actions"><button type="button" data-action="copy-review">复制每日复盘</button><a href="/strategy-lab">查看策略证据</a></div>`;
  } else {
    primaryTitle.textContent = workspace === 'intraday' ? '今日决策 · 买卖计划与市场状态' : '市场状态与仓位';
    $('#homePrimaryBadge').textContent = workspace === 'premarket' ? '盘前判断与确认条件' : '盘中状态、板块与账户约束';
    primaryBody.innerHTML = `${workspace === 'intraday' ? homeIntradayPlansHtml(actionable, researchReferencePlans) : ''}${homeSignalHistorySection(review)}${homeMarketBoardHtml({research, route, portfolio, summary, open, opportunities, sectorNames, targetExposure: executionTargetExposure, targetSource: executionTargetSource, exposureCeiling, todayDecision, riskReport: h.riskReport, strategyEvidence: h.strategyEvidence})}<details class="decision-review-evidence"${evidenceOpenAttribute}><summary>市场情绪、策略、板块与游资证据（保持展开状态）</summary>${homeIntelligenceHtml(research, route, todayDecision, h.strategyEvidence)}</details>`;
  }
  const decisionEvidence = $('.decision-review-evidence', primaryBody);
  if (decisionEvidence) {
    decisionEvidence.addEventListener('toggle', () => {
      homeEvidenceOpen = decisionEvidence.open;
      try {
        localStorage.setItem(HOME_EVIDENCE_OPEN_KEY, homeEvidenceOpen ? '1' : '0');
      } catch (error) { reportClientIssue('home-evidence-save', error); }
    });
  }

  // The opportunity monitor can be absent or stale while the audited account
  // session remains current. A planning date never grants trade authority.
  const nextSession = homeAuditedNextSession(portfolio, session);
  const reviewPreparation = completedReview.next_session_preparation || {};
  const reviewPreparationPlans = isReview
    && reviewPreparation.status === 'review_ready_requires_next_session_revalidation'
    && Array.isArray(reviewPreparation.plans)
    ? reviewPreparation.plans : [];
  const reviewPreparationHtml = reviewPreparationPlans.length ? `
    <div class="decision-section-label">收盘冻结的研究候选 · 下一交易日须重新核验</div>
    <div class="decision-holdings-table">${reviewPreparationPlans.map(plan => `<a href="/stock?code=${encodeURIComponent(plan.stock_code || '')}">
      <span><b>${esc(plan.stock_name || plan.stock_code || '候选')}</b><small>${esc(plan.stock_code || '')} · ${esc(playbooks[0] || '主线启动确认')}</small></span>
      <span>数量待重算</span>
      <span>限价待重算</span>
      <span>现金容量上限 ${accountBuyCapacityKnown ? `${accountBuyCapacity} 股` : '待核验'} · 非建议数量</span>
      <span>研究观察 · 不是买入计划</span>
    </a>`).join('')}</div>
    <p class="decision-truth-note">收盘前的名义股数、冻结价和金额已经失效，已从真实账户计划中隐藏。下一交易日只有市场情绪、同一时点板块、开盘价与涨跌停、账户现金、风险线和流动性全部重新通过后，才会生成新的可人工复核数量。</p>` : '';
  $('#homePlanTitle').textContent = isReview ? '下一交易日计划 · 开盘前准备' : '策略与战法';
  $('#homePlanBody').innerHTML = `
    <div class="decision-plan-date"><small>${isReview ? '下一交易日' : '当前时段'}</small><b>${esc(isReview ? (nextSession || '等待审计交易日历') : (session.label || '状态待核验'))}</b></div>
    <dl class="decision-plan-list">
      <div><dt>账户</dt><dd>${accountValuationReady ? `可用现金 ${accountAvailableCashKnown ? `¥${fn(accountAvailableCash, 2)}` : '待核验'}；${accountBuyCapacityKnown ? `按候选最低价估算的现金容量上限 ${accountBuyCapacity} 股，非任何股票建议数量；` : ''}${buyAccountReady ? '盘中按具体股票、限价和风险线重算' : '等待当前可执行行情'}${sellableInventoryComplete ? '' : `；卖出前另须核验今日可卖数量（${esc((summary.sellable_inventory_missing_codes || []).join('、') || '持仓')}）`}` : summary.cash_user_recorded !== true ? '录入真实剩余资金后再计算精确仓位' : '可用现金已录入；等待持仓合格行情恢复估值'}</dd></div>
      <div><dt>今日仓位</dt><dd>${Number.isFinite(executionTargetExposure) ? `${fn(executionTargetExposure, 1)}% 执行总仓位${executionTargetSource === 'hold_current' ? '（无完整计划，保持当前）' : executionTargetSource === 'account_sell_plans' ? '（完成当前卖出计划后）' : '（已绑定完整计划）'}` : '当前无法形成执行目标'}；${Number.isFinite(exposureCeiling) ? `现行风险上限 ${fn(exposureCeiling, 0)}%` : '风险上限待核验'}${homeVmpReady ? `；波动模型仅供风险复核：${fn(homeVmp.current_exposure_pct, 1)}% → ${fn(homeVmp.target_exposure_pct, 1)}%` : ''}；开盘后重新复核</dd></div>
      <div><dt>板块确认</dt><dd>${sectorEntryGateReady ? `${sectorNames.join('、')} · 当前时点已核验` : reviewSectorReady ? `${sectorNames.join('、')} · ${reviewSectorSameDay ? '今日收盘前记录' : '上一交易日记录'}，当前交易日重新确认` : '等待同一盘中观察窗口确认'}</dd></div>
      <div><dt>策略战法</dt><dd>${routeReady && playbooks.length ? `${playbooks.join('、')} · 仅研究` : '尚未开放可执行战法'}</dd></div>
      <div><dt>协议时段</dt><dd>${esc(nextSlot ? String(nextSlot).replace('T', ' ').slice(0, 16) + ' · 未确认启用' : '尚无当前时段；运行另核')}</dd></div>
    </dl>
    ${reviewPreparationHtml}
    <p class="decision-truth-note">历史状态转移诊断不控制仓位，也不是明日涨跌、胜率或收益预测。</p>
    <div class="decision-panel-actions"><a href="/next-session-plan">打开独立明日计划</a><a href="/risk">风险与仓位</a><a href="/review">完整盘后复盘</a></div>`;

  const availableCash = summary.cash_user_recorded === true && Number.isFinite(Number(summary.available_cash)) ? `¥${fn(summary.available_cash, 2)}` : '未录入';
  const pnlBasisLabel = session.status === 'preopen' && summary.valuation_status === 'complete'
    ? '成本后上一收盘价估算' : session.status === 'closed'
      ? '成本后收盘估算' : session.status === 'break' && valuationComplete
        ? '成本后11:30快照估算' : summary.valuation_basis === 'intraday_fresh_quote'
          ? '成本后实时估算' : valuationComplete
            ? '成本后最近完成快照估算' : '成本后估值待核验';
  $('#homeHoldingsMeta').innerHTML = `<span>持仓 <b>${open.length} 只</b></span><span>可用现金 <b>${availableCash}</b></span><span>实际仓位 <b>${summary.exposure_pct == null ? '待核验' : `${fn(summary.exposure_pct, 1)}%`}</b></span><span title="已扣估算买入费用与按当前可用价格清仓费用">盈亏口径 <b>${pnlBasisLabel}</b></span>`;
  $('#homeHoldingsBody').innerHTML = open.length ? `<div class="decision-holdings-table">${open.map(position => {
    const quoteAsOf = (((position.trade_plan || {}).action || {}).as_of) || position.quote_as_of;
    const quoteClock = quoteAsOf ? String(quoteAsOf).replace('T', ' ').slice(11, 19) : null;
    const baseMonitorLabel = session.is_open === true && portfolio.alerts_active === true
      ? `实时复核${quoteClock ? ` · ${quoteClock}` : ''}`
      : session.status === 'closed' && position.current != null
        ? '收盘价复盘 · 不可执行'
        : `下次开盘复核${quoteClock ? ` · 末次 ${quoteClock}` : ''}`;
    const intent = position.trade_plan?.manual_exit_intent || {};
    const monitorLabel = intent.configured === true
      ? `计划 ${intent.effective_date || '待定日期'} 降至 ${Number(intent.target_shares) || 0} 股 · ${baseMonitorLabel}`
      : baseMonitorLabel;
    return `<a href="/stock?code=${encodeURIComponent(position.code || '')}"><span><b>${esc(position.name || position.code)}</b><small>${esc(position.code || '')}</small></span><span>${Number(position.shares) || 0} 股</span><span>成本 ${fp(position.price)}</span><span class="${position.pnl_pct == null ? '' : cc(position.pnl_pct)}">${position.pnl_pct == null ? '行情不可用' : fpp(position.pnl_pct)}${position.pnl == null ? '' : ` · ¥${fn(position.pnl, 0)}`}</span><span>${esc(monitorLabel)}</span></a>`;
  }).join('')}</div>` : '<div class="decision-empty"><b>当前没有已录入持仓</b><span>若账户实际有仓位，请先录入，系统才能计算组合风险和卖出复核。</span></div>';

  const mustHandle = [];
  const routeMustHandle = Array.isArray(todayDecision.must_handle_items)
    ? todayDecision.must_handle_items.filter(item => item && typeof item === 'object') : [];
  routeMustHandle.forEach(item => {
    const matchedPosition = open.find(position => Number(position.id) === Number(item.position_id));
    const priorSellable = item.previous_sellable_shares == null
      ? matchedPosition?.sellable_shares_snapshot : item.previous_sellable_shares;
    const priorSellableAsOf = item.previous_sellable_as_of
      || matchedPosition?.sellable_as_of || '';
    mustHandle.push({
      text: [item.label, item.detail].filter(Boolean).join('：'),
      action: item.kind === 'position_inventory_confirmation'
        && Number.isInteger(Number(item.position_id))
        ? {
            id: Number(item.position_id), name: item.label || item.code || '持仓',
            shares: Number(item.held_shares) || 0,
            sellable: item.sellable_shares == null
              ? (priorSellable == null ? '' : Number(priorSellable))
              : Number(item.sellable_shares),
            asOf: item.sellable_as_of || priorSellableAsOf,
          } : null,
    });
  });
  const plannedExitIntents = open.filter(position =>
    position.trade_plan?.manual_exit_intent?.configured === true
  );
  const routeIncludesExitConfirmation = routeMustHandle.some(
    item => item.kind === 'position_inventory_confirmation'
  );
  if (plannedExitIntents.length && !routeIncludesExitConfirmation) mustHandle.push({text:
    `持仓：${plannedExitIntents.map(position => `${position.name || position.code} ${position.trade_plan.manual_exit_intent.effective_date} 目标${Number(position.trade_plan.manual_exit_intent.target_shares) || 0}股`).join('；')}；当日须核验券商可卖库存`
  });
  const routedInventoryIds = new Set(routeMustHandle.filter(
    item => item.kind === 'position_inventory_confirmation'
      && Number.isInteger(Number(item.position_id))
  ).map(item => Number(item.position_id)));
  const missingInventoryCodes = new Set(
    (summary.sellable_inventory_missing_codes || []).map(String)
  );
  open.filter(position => missingInventoryCodes.has(String(position.code || ''))
    && Number.isInteger(Number(position.id))
    && !routedInventoryIds.has(Number(position.id))).forEach(position => mustHandle.push({
      text: `确认今日可卖股数：${position.name || position.code}当前持仓${Number(position.shares) || 0}股`,
      action: {
        id: Number(position.id), name: position.name || position.code || '持仓',
        shares: Number(position.shares) || 0,
        sellable: position.sellable_shares_snapshot == null
          ? '' : Number(position.sellable_shares_snapshot),
        asOf: position.sellable_as_of || '',
      },
    }));
  (summary.account_fact_gaps || []).filter(gap =>
    !(missingInventoryCodes.size && String(gap).includes('当日可卖数量'))
  ).forEach(gap => mustHandle.push({text:`账户：${gap}`}));
  if (accountBuyCapacityKnown && accountBuyCapacity === 0) {
    mustHandle.push({text:`账户：可用现金 ${accountAvailableCashKnown ? `¥${fn(accountAvailableCash, 2)}` : '待核验'}，当前新增买入能力为0股`});
  }
  if ((review.unresolved || []).length) mustHandle.push({text:`提醒：${review.unresolved.length} 项未处理`});
  if (riskReviews && !routeMustHandle.some(item => item.kind === 'position_risk')) {
    mustHandle.push({text:`持仓：${riskReviews} 项风险复核`});
  }
  if (homeVmpReduction > 0) mustHandle.push({text:
    `持仓：组合20日波动高于历史基准，研究参考总仓 ${fn(homeVmp.target_exposure_pct, 1)}%（当前 ${fn(homeVmp.current_exposure_pct, 1)}%）；${sellableInventoryComplete ? '可卖数量已核验，但该模型没有交易授权，不生成自动卖出数量' : '先核验今日可卖股数，不生成自动卖出数量'}`
  });
  if (!sectorEntryGateReady) mustHandle.push({text:reviewSectorReady
    ? `市场：${reviewSectorSameDay ? '今日' : '上一交易日'}主线仅供复盘；当前交易时段要用当时盘面重新确认`
    : '市场：等待下一盘中确认窗口，用同一分钟的市场与板块快照确认主线'});
  if (reviewPreparationPlans.length) mustHandle.push({text:
    `下一交易日：${reviewPreparationPlans.length} 项冻结候选需在开盘前重算股数、限价与风险线`
  });
  $('#homeMustHandleCount').textContent = `${mustHandle.length} 项`;
  $('#homeMustHandleBody').innerHTML = mustHandle.length ? mustHandle.map(item => item.action
    ? `<button type="button" data-action="sellable-inventory" data-id="${item.action.id}" data-name="${escAttr(item.action.name)}" data-shares="${item.action.shares}" data-sellable="${item.action.sellable}" data-as-of="${escAttr(item.action.asOf)}">${esc(item.text)}<b>核验券商可卖数量</b></button>`
    : `<span>${esc(item.text)}</span>`).join('') : '<span>当前没有阻塞交易链的门禁</span>';
  const mustHandleDetails = $('#homeMustHandleDetails');
  if (mustHandleDetails) mustHandleDetails.open = Boolean(
    missingInventoryCodes.size > 0
    || Number(todayDecision.pending_exit_confirmation_count) > 0
    || Number(todayDecision.sell_review_ready_count) > 0
    || (review.unresolved || []).length > 0
    || riskReviews > 0
    || homeVmpReduction > 0
  );
  $('#homeDecisionLogBody').innerHTML = homeTimelineRows(review);
  $('#homeDecisionMode').textContent = isReview ? '复盘工作区已自动打开' : workspace === 'premarket' ? '盘前工作区' : '盘中工作区';
  const authority = route.authority || {};
  $('#homeRouteAuthority').textContent = authority.generates_buy_or_sell_alert === true ? '可生成提醒 · 仍需人工确认' : '研究路由 · 无交易权限';
  $('#homePlaybookGate').textContent = isReview
    ? '允许：复盘、持仓复核与下一交易日准备'
    : routeReady && playbooks.length ? `允许研究：${playbooks.join('、')}` : '允许：观察与已有持仓风险复核';
  $('#homeNoTradeRule').textContent = !buyAccountReady
    ? '禁止：现金、持仓估值或当前可执行行情不完整时生成精确新仓计划'
    : !sellableInventoryComplete
      ? '卖出股数待核验；买入仍须通过市场、板块、战法与账户风险链'
    : !sectorEntryGateReady ? '没有当前时点板块确认：今日不新开仓'
    : !actionable.length ? '没有完整优势或完整计划：今日不交易'
    : '只复核已列计划；禁止自动下单与未验证收益承诺';
  restoreHomeLayout(home, workspace);
  installHomeLayoutEditor(home);
}


RegisSpa.home = {
  homeStatePlain,
  homeAuditedNextSession,
  phases: HOME_PHASES,
  dashboardPhaseFromSession,
  setHomeDensity,
  setHomeWorkspace,
  restoreHomePreferences,
  renderDecisionHome,
  homeMarketStatePresentation,
  homeMarketRouteBridge,
  homeTimelineProgress,
  homeCompletePlans,
  homeAccountSellPlans,
  homeProjectedSellExposure,
  homeSectorNames,
  homePlaybookNames,
  homeIntradayPlansHtml,
  homeResearchReferencePlans,
  homeResearchReferencePlansHtml,
  homeHypotheticalLiquidation,
  homeHypotheticalLiquidationHtml,
  homeSignalHistoryItems,
  homeSignalHistoryRows,
  homeSignalHistorySection,
  homeAfterCostReviewStatus,
  homeTimelineRows,
  homeIntelligenceHtml,
  toggleHomeLayoutEditing,
  restoreHomeLayout,
  saveHomeLayout,
};
})(window.RegisSpa);
