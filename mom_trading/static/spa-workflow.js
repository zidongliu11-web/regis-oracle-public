/* Dedicated planning and review views share the existing source APIs and gates. */
(function (RegisSpa) {
  'use strict';
  const { $, state, api, apiError, esc, fn } = RegisSpa.core;
  const home = RegisSpa.home;
  let timer = null;
  let generation = 0;
  let loading = false;
  let reviewData = null;
  let refreshAfterTimeChange = null;
  let planExpiry = null;
  let refreshAfterVisibility = null;
  const money = value => value != null && value !== '' && Number.isFinite(Number(value)) ? `¥${fn(Number(value), 2)}` : '待核验';
  const metric = (label, value, note = '') => `<div class="workflow-metric"><small>${esc(label)}</small><strong>${esc(value)}</strong><span>${esc(note)}</span></div>`;
  const section = (title, content, id = '') => `<section class="workflow-card"${id ? ` id="${esc(id)}"` : ''}><h3>${esc(title)}</h3>${content}</section>`;
  const list = rows => `<ul class="workflow-list">${rows.map(row => `<li>${esc(row)}</li>`).join('')}</ul>`;

  function accountHtml(portfolio) {
    if (!portfolio?.summary) return '<p class="workflow-warning">账户接口不可用，不显示为零持仓或满额现金。请刷新重试。</p>';
    const summary = portfolio.summary;
    const valued = summary.valuation_status === 'complete';
    return `<div class="workflow-metrics">
      ${metric('真实可用现金', summary.cash_user_recorded === true ? money(summary.available_cash) : '尚未录入', summary.cash_as_of || '账户时点待核验')}
      ${metric('账户权益', valued && summary.cash_user_recorded === true ? money(summary.account_equity) : '现金与估值待核验', '账户中心同一份账本')}
      ${metric('当前仓位', valued && summary.exposure_pct != null ? `${fn(summary.exposure_pct, 1)}%` : '待核验', '不是建议仓位')}
      ${metric('今日可卖数量', Array.isArray(portfolio.open) && !portfolio.open.length ? '账本无持仓' : summary.sellable_inventory_status === 'complete' ? '已核验' : '待确认', '按持仓逐项核验，遵守 T+1')}
      </div><div class="workflow-actions"><a href="/positions">查看持仓与现金</a><a href="/?account=record">录入 / 更新账户事实</a><a href="/risk">查看风险与仓位限制</a></div>`;
  }

  function riskPreparationHtml(portfolio) {
    if (!portfolio?.summary || !Array.isArray(portfolio.open)) return '<p class="workflow-warning">账户风险输入未返回，不能把缺失当作没有风险；本区旧结论已清除。</p>';
    const s = portfolio.summary;
    const policy = s.position_policy || {};
    const budget = s.position_risk_budget || {};
    const validNumber = x => typeof x === 'number' && Number.isFinite(x);
    const percent = x => validNumber(x) ? `${fn(x, 2)}%` : '待核验';
    const positions = portfolio.open.map(p => {
      const risk = p.risk_action || {};
      const level = risk.effective_risk_level;
      const line = level?.active === true && validNumber(level.value) && level.value > 0 ? level.value : null;
      const current = validNumber(p.current) && p.current > 0 ? p.current : null;
      const distance = current != null && line != null ? (current / line - 1) * 100 : null;
      const inventory = p.sellable_as_of ? `记录于 ${p.sellable_as_of}；下一交易日重新核验` : '当日可卖库存待核验';
      const lineName = level?.kind === 'trailing_stop' ? '移动风险复核线' : level?.kind === 'hard_stop' ? '冻结损失复核线' : '风险线';
      return `<article class="workflow-playbook"><h4><a href="/stock?code=${encodeURIComponent(p.code || '')}">${esc(p.name || p.code || '持仓名称待核验')}</a> <small>${esc(p.code)}</small></h4>
        <p>${esc(risk.action || '风险判断未就绪')}：${esc(risk.reason || '持仓风险输入不完整，不能默认继续持有或生成卖出数量。')}</p>
        <dl><div><dt>已录入持仓</dt><dd>${validNumber(p.shares) ? esc(p.shares) + ' 股' : '待核验'}；${esc(inventory)}</dd></div>
        <div><dt>估值参考价</dt><dd>${money(current)} · 非下一交易日成交价</dd></div>
        <div><dt>${lineName}</dt><dd>${money(line)}；${esc(level?.data_as_of || risk.atr14_as_of || '依据时点待核验')}</dd></div>
        <div><dt>现价相对风险线</dt><dd>${percent(distance)}；${distance == null ? '不判断是否触发' : distance <= 0 ? '已到线或低于线，优先复核' : '高于参考线，不等于安全或应继续持有'}</dd></div>
        <div><dt>开盘处理条件</dt><dd>更新行情与可卖库存；复核现有风险线及计划失效条件。跳空、停牌、跌停可能无法按参考价退出。</dd></div>
        <div><dt>执行边界</dt><dd>${esc(level?.semantics || '风险线尚未核验')}；本预案不生成卖出股数，不覆盖已有人工计划。</dd></div></dl></article>`;
    }).join('');
    return `<p><b>${esc(s.portfolio_action?.label || '组合风险待核验')}</b>：${esc(s.portfolio_action?.reason || '缺少组合处置依据。')}</p>
      <div class="workflow-metrics">${metric('总仓风险上限', policy.status === 'complete_risk_ceiling' ? percent(policy.live_total_exposure_ceiling_pct) : '待核验', policy.binding_total_exposure_label || '不是目标仓位')}
      ${metric('现有持仓计划风险', money(budget.existing_open_risk_yuan), '按现价至有效风险线及退出成本估计')}
      ${metric('剩余计划风险预算', money(budget.remaining_open_risk_yuan), '不是可买金额，实际亏损可能超过预算')}
      ${metric('单笔计划亏损上限', percent(s.new_position_risk_budget_pct), '占真实账户权益；逐股还需风险线、成本与流动性')}</div>
      ${budget.unknown_codes?.length ? `<p class="workflow-warning">风险未核验的持仓：${esc(budget.unknown_codes.join('、'))}。不能据此推算新增股数。</p>` : ''}
      ${s.account_fact_gaps?.length ? `<p class="workflow-warning">账户事实缺口：${esc(s.account_fact_gaps.join('、'))}</p>` : ''}
      <p>以下与账户中心使用同一次接口返回的持仓、行情、风险线和预算；不会把上限当建议仓位。</p>
      <div class="workflow-playbooks workflow-risk-positions">${positions || '<p class="workflow-empty">账本没有已录入持仓；如与真实账户不符，先更新账户事实。</p>'}</div>`;
  }

  function planModel(route, opportunities, session, scan = null) {
    if (route?.research_snapshot?.current === false) route = {research_snapshot:route.research_snapshot};
    const decision = route?.today_decision_workspace || {};
    const completed = decision.completed_market_review || route?.completed_market_review || {};
    const preparation = completed.next_session_preparation || {};
    // A previous close's nominal prices and sizes never become tomorrow's orders.
    const reviewReady = completed.valid === true && completed.display_only === true
      && preparation.status === 'review_ready_requires_next_session_revalidation'
      && Array.isArray(preparation.plans);
    const observed = route?.mainline_diagnostics?.next_session_preparation;
    const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
      && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
    const identityValid = observed?.schema === 'next_session_sector_preparation.v1'
      && observed.research_only === true && observed.historical_only === true
      && observed.generates_alert === false && observed.changes_trade_authority === false;
    const datesValid = identityValid && date(observed.reference_day) && date(observed.planning_day)
      && observed.reference_day < observed.planning_day;
    const nextDate = opportunities?.buy_window?.next_session;
    const dateConflict = datesValid && date(nextDate) && nextDate !== observed.planning_day;
    const sectorPreparation = datesValid && !dateConflict ? observed : null;
    const afterClose = sectorPreparation?.observation_mode === 'after_close';
    const sourceClock = afterClose ? sectorPreparation.source_as_of : sectorPreparation?.observed_at;
    const captureValid = !afterClose || (Number.isFinite(Date.parse(sectorPreparation.observed_at))
      && Date.parse(sectorPreparation.observed_at) >= Date.parse(sourceClock));
    const preparationReady = sectorPreparation?.status === 'observed'
      && captureValid && String(sourceClock || '').slice(0, 10) === sectorPreparation.reference_day
      && Array.isArray(sectorPreparation.rows);
    const taxonomyReady = preparationReady && sectorPreparation.selection_scope === 'preparation_only_preserving_original_ranks'
      && Array.isArray(sectorPreparation.sector_watch_rows) && Array.isArray(sectorPreparation.market_cohort_rows);
    const validRows = rows => preparationReady ? rows.filter(row => row && row.name
      && String(row.data_as_of || '').slice(0, 10) === sectorPreparation.reference_day) : [];
    const isCohort = row => row.market_cohort === true || (row.kind === 'concept' && /^(昨日涨停|昨日连板|昨日炸板)/.test(row.name));
    const sectorRows = validRows(taxonomyReady ? sectorPreparation.sector_watch_rows : sectorPreparation?.rows || []).filter(row => !isCohort(row)).slice(0, 3);
    const cohortRows = validRows(taxonomyReady ? sectorPreparation.market_cohort_rows : sectorPreparation?.rows || []).filter(isCohort).slice(0, 3);
    const planningDay = dateConflict ? null : sectorPreparation?.planning_day || (date(nextDate) ? nextDate : null);
    const referenceDay = sectorPreparation?.reference_day;
    const development = window.RegisPlanResearch.model(scan, referenceDay, planningDay, sectorRows);
    const reviewMatches = !!referenceDay && completed.valid === true && completed.display_only === true
      && completed.decision_day === referenceDay;
    const reviewMarketMatches = reviewMatches && String(completed.market_state_as_of || '').slice(0,10) === referenceDay;
    const marketAsOf = reviewMarketMatches ? completed.market_state_as_of : route?.market_state_as_of;
    const marketReferenceMatches = !!referenceDay && String(marketAsOf || '').slice(0,10) === referenceDay;
    const candidates = reviewReady && reviewMatches ? preparation.plans : [];
    return {
      date: planningDay, development,
      capturedAt: development.emotionMatches ? development.emotionDay : marketReferenceMatches ? marketAsOf
        : `需要 ${referenceDay || '经日历核验的参考日'} 情绪；已有记录 ${marketAsOf || '时点未核验'}`,
      sectors: sectorRows.map(row => row.name), sectorRows, cohortRows, sectorPreparation,
      originalSectorRows: validRows(sectorPreparation?.rows || []).slice(0, 3),
      preparationGap: dateConflict ? '板块预案与买入窗口的交易日不一致，已清除混日名单；需恢复一致的交易日历。'
        : sectorPreparation?.summary || (identityValid ? observed.summary : null) || '上一完成交易日的板块原件未就绪，不能判断是否存在可观察主线。',
      marketState: development.emotionMatches ? development.emotionName : marketReferenceMatches
        ? home.homeStatePlain(reviewMarketMatches ? completed.market_state : route?.market_state || 'unknown')
        : referenceDay ? '该日情绪待恢复' : home.homeStatePlain('unknown'),
      candidates: candidates.map(row => ({code:row.stock_code, name:row.stock_name || row.stock_code})),
      candidateGap: reviewReady && !reviewMatches ? '原复盘候选日期与当前预案参考日不一致或未核验，未结转旧名单。' : null,
      playbooks: development.playbooks,
      executionGap: route?.research_snapshot?.current === false ? route.research_snapshot.summary : decision.reason || '实时路由尚未提供完整的交易资格判断。',
      currentBuys: route && route.research_snapshot?.current !== false ? home.homeCompletePlans(route, session).filter(row => row.side === 'BUY').length : null,
    };
  }

  function sectorPreparationHtml(model) {
    const pct = value => typeof value === 'number' && Number.isFinite(value) ? `${fn(value, 2)}%` : '待核验';
    const count = value => Number.isInteger(value) && value >= 0 ? String(value) : '待核验';
    const cards = model.sectorRows.map(row => {
      const m = ['verified_observation', 'verified_preparation'].includes(row.constituent_status) ? row.constituent_metrics : null;
      const cores = Array.isArray(m?.core_observations) ? m.core_observations : [];
      return `<article class="workflow-playbook"><div class="workflow-playbook-title"><h4>${esc(row.name)}</h4><span>${row.market_cohort === true ? '行情分组 · 非产业主线' : row.kind === 'industry' ? '行业' : row.kind === 'concept' ? '概念' : '板块'} · 上一交易日领涨观察</span></div>
        ${row.scope_note ? `<p>${esc(row.scope_note)}</p>` : ''}
        <dl><div><dt>为什么列入观察</dt><dd>涨幅 ${pct(row.change_pct)}；同类涨幅排名分位 ${pct(row.change_rank_pct)}；上涨 / 涨跌家数 ${pct(row.advance_coverage_pct)}（涨 ${count(row.up_count)}、跌 ${count(row.down_count)}）。不代表已通过主线确认。</dd></div>
        <div><dt>来源时点</dt><dd>${esc(row.data_as_of || '待核验')} · ${esc(model.sectorPreparation.source || '来源待核验')}</dd></div>
        ${model.sectorPreparation.observation_mode === 'after_close' ? `<div><dt>实际获取时间</dt><dd>${esc(model.sectorPreparation.observed_at)}；盘后历史观察，不是盘中当时已知证据。</dd></div>` : ''}
        <div><dt>成分扩散</dt><dd>${m ? `完整成分 ${count(m.count)} 家；涨 ${count(m.up)} / 跌 ${count(m.down)} / 平 ${count(m.flat)}；全体上涨占比 ${pct(m.advance_pct)}；剔除成交额前三后 ${pct(m.ex_core_advance_pct)}。` : `${model.sectorPreparation.observation_mode === 'after_close' ? '盘后成分原件缺失或未通过完整性、日期核验' : '同一观察桶的成分原件缺失或未通过校验'}；不能认定普涨、扩散或龙头。`}</dd></div>
        ${m ? `<div><dt>成分证据时点</dt><dd>实际获取 ${esc(row.constituent_observed_at || '未提供')}；行情 ${esc(m.source_as_of_min)} 至 ${esc(m.source_as_of_max)}。${esc(row.constituent_scope_note || '同一盘中观察桶的成分原件。')}</dd></div>` : ''}
        <div><dt>核心观察名单</dt><dd>${cores.length ? cores.map(core => `<a href="/stock?code=${encodeURIComponent(core.stock_code || '')}">${esc(core.stock_name || core.stock_code)}</a>（${pct(core.change_pct)}）`).join('、') + `；成交额前三合计占比 ${pct(m.top_three_amount_share_pct)}。仅按当时成交额排序，不是买入候选或龙头认定。` : '暂无经原件核验的名单，不以板块名称猜个股。'}</dd></div>
        <div><dt>下一日确认条件</dt><dd>${esc(row.confirmation_condition || '确认条件未提供，不能推进')}</dd></div>
        <div><dt>放弃或等待条件</dt><dd>${esc(row.abandon_condition || '失效条件未提供，不能推进')}</dd></div></dl></article>`;
    }).join('');
    const cohorts = (model.cohortRows || []).map(row => `<li><strong>${esc(row.name)}</strong> · 涨幅 ${pct(row.change_pct)} · 上涨比例 ${pct(row.advance_coverage_pct)} · 原始同类排名分位 ${pct(row.change_rank_pct)}。行情分组 · 非产业主线。${esc(row.scope_note || '按昨日交易行为分组，不是产业或题材主线。')}</li>`).join('');
    const originals = (model.originalSectorRows || []).map(row => row.name).join('、');
    return `<p>${esc(model.preparationGap)}</p><p>${esc(model.sectorPreparation?.selection_note || '')}</p>${cards ? `<div class="workflow-playbooks workflow-sector-preparation">${cards}</div>` : `<p class="workflow-empty">${cohorts ? '已读到行情分组观察，但没有达标行业/题材；不是来源缺失，也不等于市场没有机会。' : '观察名单未形成；这是证据或条件的具体缺口，不是“市场一定没有机会”。'}</p>`}
      ${cohorts ? `<aside class="workflow-note"><h4>情绪参考分组 · 不占行业/题材名额</h4><ul>${cohorts}</ul><p>这些对象按过去涨停、连板或炸板行为分组，不说明现在应买哪个产业或题材，也不产生买入候选。</p></aside>` : ''}
      ${originals ? `<div class="workflow-note"><h4>核对未改动的原始前三榜单</h4><p>${esc(originals)}</p><p>预案分类只改变阅读组织，不重新定义原冻结规则的盘中确认结果。</p></div>` : ''}
      <p>${esc(model.sectorPreparation?.observation_boundary || '昨日观察与今日确认分开；没有旧价格、旧股数自动结转。')}</p>
      <p>板块只有在新交易日重新确认后，才与下方适用情绪的战法共同核验。战法未准入时，即使板块满足条件也不生成买入计划。</p>`;
  }

  function planHtml(route, opportunities, portfolio, session, scan = null) {
    const model = planModel(route, opportunities, session, scan);
    const candidates = window.RegisPlanResearch.candidatesHtml(model.development, portfolio)
      + (model.candidates.length ? `<p>同一参考日原复盘名单，仅供核对：${model.candidates.map(row => `<a href="/stock?code=${encodeURIComponent(row.code || '')}">${esc(row.name)}</a>`).join('、')}。不沿用原股数、价格或风险预算。</p>` : '')
      + (model.candidateGap ? `<p class="workflow-warning">${esc(model.candidateGap)}</p>` : '');
    return `<div class="workflow-metrics">
      ${metric('准备交易日', model.date || '交易日历待核验', '明日指下一交易日，不是自然日')}
      ${metric('市场情绪参考', model.marketState, model.capturedAt || '证据时点待核验')}
      ${metric('准备观察行业 / 题材', model.sectors.join('、') || (model.cohortRows.length ? '只有行情分组 · 无达标行业/题材' : model.sectorPreparation?.status === 'no_matching_observation' ? '数据齐备 · 无达标观察' : '观察原件待核验'), model.sectorPreparation?.reference_day ? `引用 ${model.sectorPreparation.reference_day}；不是下一日已确认主线` : '未用旧计划板块补齐名单')}
      ${metric('当前完整买入提示', model.currentBuys == null ? '未核验' : `${model.currentBuys} 个`, '不是明日信号数量预测')}
    </div>
    <p class="workflow-warning"><b>当前交易链的实际结论：</b>${esc(model.executionGap)} 条件研究不等于收益准入；未完成验证仍是研发缺口，不是等到开盘就能自动消失的条件。</p>
    <nav class="workflow-actions" aria-label="预案快速定位"><a href="#plan-sectors">准备观察哪些板块</a><a href="#plan-holdings">先处理持仓风险</a><a href="#plan-candidates">个股候选</a><a href="#plan-playbooks">情绪与战法</a><a href="#mainline-evidence">当前主线原始证据</a></nav>
    ${section('准备观察哪些板块 · 逐项确认与放弃条件', sectorPreparationHtml(model), 'plan-sectors')}
    ${section('持仓先处理什么 · 风险与资金约束', riskPreparationHtml(portfolio), 'plan-holdings')}
    ${section('开盘前账户与资金核对', accountHtml(portfolio))}
    ${section('准备研究哪些个股', candidates, 'plan-candidates')}
    ${section('不同情绪下的战法预案', window.RegisPlanResearch.html(model.development), 'plan-playbooks')}
    ${window.RegisMainlineEvidence?.render(route?.mainline_diagnostics) || ''}
    ${section('从预案到人工买卖计划', list(['盘前：核对最新持仓、现金与可卖数量，读取上一交易日复盘。', '开盘：重新确认市场情绪及同一时点主线板块，排除拥挤与退潮条件。', '触发：仅使用符合当前情绪、已获准入的战法，逐股核验价格、风险线、成本及仓位。', '退出：按有效风险线与计划失效条件复核；没有完整优势时不新开仓。']))}`;
  }

  function reviewHtml(raw) {
    if (!raw?.date) return '<p class="workflow-warning">复盘接口未返回有效日期，不能把缺失记录当成零成交。</p>';
    const review = RegisSpa.review.normalizeReviewData(raw);
    const calendarKnown = typeof review.market_session?.is_trading_day === 'boolean';
    const costs = home.homeAfterCostReviewStatus(review);
    const perf = review.execution_performance || {};
    const events = review.sell_events || [];
    return `${calendarKnown ? '' : '<p class="workflow-warning">交易日历未就绪。以下为接口返回日期的记录，尚不能确认是最近已完成交易日；日历恢复后需重新核验。</p>'}<div class="workflow-metrics">
      ${metric('复盘记录日期', review.date, review.time_basis === 'execution' ? '用户补记的实际成交日期；不是券商独立核验' : '按录入日期；不等于实际成交日期')}
      ${metric('可操作提醒 / 已知买入', `${review.alert_counts?.total ?? '—'} / ${review.bought_count ?? '—'}`, review.unassigned_buy_time_count ? `另有 ${review.unassigned_buy_time_count} 笔买入日期待补记` : '研究观察与导入持仓均不计为买入')}
      ${metric(costs.status === 'verified' ? '成本后已实现盈亏' : '已实现记录盈亏', money(review.realized_pnl), costs.label)}
      ${metric('卖出事件', String(review.sell_event_count ?? events.length), '逐笔核对部分卖出和全部平仓')}
    </div>
    ${section('提醒与人工执行', home.homeSignalHistorySection(review) + `<div class="workflow-timeline">${home.homeTimelineRows(review)}</div>`)}
    ${section('逐笔卖出成本与净盈亏', RegisSpa.review.renderFillReview(review))}
    ${section('策略归因与验证边界', `<p>${esc(perf.interpretation || '手工成交的账户收益不等于策略已通过验证。')}</p>${list([`完整平仓样本：${perf.completed_positions ?? '待核验'} 笔`, `费用明细完整：${perf.cost_breakdown_complete_positions ?? '待核验'} 笔；费用缺口：${perf.cost_breakdown_missing_positions ?? '待核验'} 笔`, '策略评价必须按情绪分组，并区分扣成本、样本外与前向证据。'])}<div class="workflow-actions"><a href="/strategy-lab">查看分情绪策略证据</a><a href="/next-session-plan">进入下一交易日计划</a></div>`)}
    ${section('账户状态（当前，不是历史账户快照）', accountHtml(review._currentPortfolio))}`;
  }

  function stop() {
    generation += 1; window.clearInterval(timer); timer = null; loading = false;
    window.clearTimeout(planExpiry); planExpiry = null;
    if (refreshAfterVisibility) document.removeEventListener('visibilitychange', refreshAfterVisibility);
    refreshAfterVisibility = null;
    if (refreshAfterTimeChange) window.removeEventListener('regis-fill-time-recorded', refreshAfterTimeChange);
    refreshAfterTimeChange = null;
  }

  function render(page) {
    stop();
    const token = generation;
    const view = $(`[data-page="${page}"]`);
    const isPlan = page === 'plan';
    view.innerHTML = `<div class="workflow-page"><header class="workflow-header"><div><small>${isPlan ? '收盘 → 盘前 → 开盘确认' : '提醒 → 成交 → 扣成本归因'}</small><h2>${isPlan ? '明日计划' : '盘后复盘'}</h2><p>${isPlan ? '明确研究板块、战法与账户约束；新交易日重新核验。' : '核对真实记录，区分研究观察、人工成交与策略证据。'}</p></div><div class="workflow-actions"><button type="button" data-workflow-refresh>刷新数据</button>${isPlan ? '<a href="/review">查看上一交易日复盘</a>' : '<button type="button" data-workflow-copy disabled>复制复盘</button>'}</div></header><p data-workflow-status role="status">正在读取同源数据…</p><div data-workflow-body aria-busy="true"></div></div>`;
    if (!isPlan) {
      view.querySelector('.workflow-header').insertAdjacentHTML('afterend', '<div class="workflow-actions"><button type="button" data-fill-plan-open data-server-write>核对成交与原计划</button><small>保留计划快照与更正历史；后补关联不改账本、提醒状态或策略资格。</small></div>');
      view.querySelector('.workflow-header').insertAdjacentHTML('afterend', '<div class="workflow-actions"><label>复盘日期 <input type="date" data-review-date aria-label="复盘日期"></label><label>时间口径 <select data-review-basis aria-label="复盘时间口径"><option value="recorded">录入日期</option><option value="execution">实际成交日期（用户补记）</option></select></label><button type="button" data-fill-time-open data-server-write>补记 / 更正成交时间</button><button type="button" data-fill-fee-open data-server-write>补记实费并核对现金</button><small>日期留空取最近可复盘日；时间补记不改现金，费用更正须明确核对当前现金。</small></div>');
      const query = new URLSearchParams(window.location.search);
      view.querySelector('[data-review-date]').value = query.get('date') || '';
      view.querySelector('[data-review-basis]').value = query.get('basis') === 'execution' ? 'execution' : 'recorded';
    }
    const refreshButton = view.querySelector('[data-workflow-refresh]');
    const status = view.querySelector('[data-workflow-status]');
    const body = view.querySelector('[data-workflow-body]');
    let queuedRefresh = false;
    let rotationPending = false;
    function attachRotation() {
      const panel = document.createElement('section');
      panel.className = 'workflow-card mainline-evidence';
      panel.dataset.sectorRotation = 'true';
      panel.innerHTML = '<h3>最近交易日板块轮动</h3><p>正在重新核验盘后原件；账户与成交复盘可先使用。</p>';
      body.appendChild(panel);
      if (rotationPending) return;
      rotationPending = true;
      void api('/api/research/sector-rotation').then(packet => {
        if (token !== generation || state.page !== page) return;
        const current = body.querySelector('[data-sector-rotation]');
        if (!current) return;
        const checkedAt = new Date().toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'});
        current.innerHTML = `<p class="mainline-source">本次页面核查：${checkedAt}（上海时间）；行情时点与原件获取时点见下方。</p>`
          + (window.RegisMainlineEvidence?.rotation(packet) || '<p>板块复盘组件暂不可用；旧观察已清除。</p>');
      }).catch(() => {
        const current = body.querySelector('[data-sector-rotation]');
        if (current) current.innerHTML = '<h4>最近交易日板块轮动</h4><p>本次复盘无法处理；旧观察已清除。</p>';
      }).finally(() => { rotationPending = false; });
    }
    async function refresh() {
      if (token !== generation || state.page !== page || document.hidden) return;
      if (loading) { queuedRefresh = true; return; }
      loading = true; refreshButton.disabled = true; body.setAttribute('aria-busy', 'true');
      let reviewUrl = '/api/timing/review?scope=last_review';
      if (!isPlan) {
        const dateInput = view.querySelector('[data-review-date]'), basisInput = view.querySelector('[data-review-basis]');
        const query = new URLSearchParams({scope:'last_review', basis:basisInput.value});
        if (dateInput.value) query.set('date', dateInput.value);
        reviewUrl = '/api/timing/review?' + query;
        dateInput.disabled = basisInput.disabled = true;
        view.querySelector('[data-workflow-copy]').disabled = true;
        reviewData = null;
      }
      const endpoints = isPlan ? ['/api/strategy-route/research?view=dashboard', '/api/opportunities?view=monitor', '/api/timing/portfolio?view=planning', '/api/market/status'] : [reviewUrl, '/api/timing/portfolio'];
      const started = performance.now();
      try {
        const data = await Promise.all(endpoints.map(url => api(url)));
        if (token !== generation || state.page !== page) return;
        const failures = endpoints.filter((_, i) => !data[i]);
        const routePending = isPlan && data[0]?.research_snapshot?.current === false;
        status.textContent = failures.length ? `部分数据读取失败：${failures.map(url => apiError(url, '暂不可用')).join('；')}。下方缺失项不生成交易建议，可点击刷新重试。` : routePending ? `${data[0].research_snapshot.summary} 前台每分钟更新。` : `已刷新 · ${new Date().toLocaleTimeString('zh-CN', {timeZone:'Asia/Shanghai'})}（上海时间） · 前台每分钟更新`;
        status.classList.toggle('workflow-warning', failures.length > 0 || routePending);
        if (isPlan) {
          window.clearTimeout(planExpiry);
          const expired = {scan_snapshot:{current:false,summary:'战法扫描或价格预案已过期，旧匹配和价格已撤下；等待重新核验。'}};
          const currentScan = data[2]?.planning?.candidate_scan, review = data[2]?.planning?.account_review;
          const remaining = Math.min(currentScan?.scan_snapshot?.valid_for_seconds, review?.valid_for_seconds) * 1000 - (performance.now()-started);
          const paint = scan => {
            const open = new Set([...body.querySelectorAll('details[data-plan-detail][open]')].map(d => d.dataset.planDetail));
            body.innerHTML = planHtml(data[0], data[1], data[2], data[3]?.session, scan);
            body.querySelectorAll('details[data-plan-detail]').forEach(d => {d.open = open.has(d.dataset.planDetail);});
            attachRotation();
          };
          paint(Number.isFinite(remaining) && remaining > 0 ? currentScan : currentScan?.scan_snapshot?.current ? expired : currentScan);
          if (currentScan?.scan_snapshot?.current && Number.isFinite(remaining) && remaining > 0) {
            planExpiry = window.setTimeout(() => {if (token === generation) {paint(expired); void refresh();}}, remaining);
          }
        }
        else {
          reviewData = data[0]?.date ? {...data[0], _currentPortfolio:data[1]} : null;
          body.innerHTML = reviewHtml(reviewData);
          attachRotation();
          view.querySelector('[data-workflow-copy]').disabled = !reviewData;
        }
      } catch (error) {
        if (token === generation) {
          status.textContent = '页面数据处理失败，请刷新重试；旧内容已清除。';
          body.innerHTML = '<p class="workflow-warning">本次预案未能完成渲染，不沿用上次名单或账户结论。</p>';
          reviewData = null;
          const copy = view.querySelector('[data-workflow-copy]');
          if (copy) copy.disabled = true;
        }
        RegisSpa.core.reportClientIssue('workflow-render', error);
      } finally {
        if (token === generation) {
          loading = false; refreshButton.disabled = false; body.setAttribute('aria-busy', 'false');
          view.querySelectorAll('[data-review-date],[data-review-basis]').forEach(input => {input.disabled = false;});
          if (queuedRefresh) {queuedRefresh = false; void refresh();}
        }
      }
    }
    refreshButton.addEventListener('click', () => { void refresh(); });
    if (isPlan) {
      refreshAfterVisibility = () => {if (!document.hidden) {body.innerHTML = '<p>重新核验预案时效…</p>'; void refresh();}};
      document.addEventListener('visibilitychange', refreshAfterVisibility);
    }
    if (!isPlan) {
      view.querySelectorAll('[data-review-date],[data-review-basis]').forEach(input => input.addEventListener('change', () => {
        const url = new URL(window.location.href);
        const day = view.querySelector('[data-review-date]').value;
        if (day) url.searchParams.set('date', day); else url.searchParams.delete('date');
        url.searchParams.set('basis', view.querySelector('[data-review-basis]').value);
        window.history.replaceState(window.history.state, '', url);
        void refresh();
      }));
      refreshAfterTimeChange = refresh;
      window.addEventListener('regis-fill-time-recorded', refreshAfterTimeChange);
    }
    view.querySelector('[data-workflow-copy]')?.addEventListener('click', async event => {
      if (!reviewData) return;
      await RegisSpa.review.copyReviewReport(reviewData);
      event.target.textContent = '已复制复盘';
    });
    refresh();
    timer = window.setInterval(refresh, 60000);
  }
  window.addEventListener('pagehide', stop);
  RegisSpa.workflow = { stop, planModel, planHtml, reviewHtml, accountHtml, riskPreparationHtml };
  RegisSpa.registerPage('plan', {render:() => render('plan')});
  RegisSpa.registerPage('review', {render:() => render('review')});
})(window.RegisSpa);
