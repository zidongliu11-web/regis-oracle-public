const $ = (selector) => document.querySelector(selector);
let dashboard = null;
let toastTimer = null;
let focusCode = null;
let focusMode = 'stock';
let focusInterval = '5m';
let focusChartInstance = null;
let focusChartResizeObserver = null;
let focusChartLastLoadedAt = 0;
let focusChartRequestKey = null;
let manualCandidates = [];
let manualCandidateRequestRunning = false;
let realMoneyAllowed = false;

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]
  ));
}
function money(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '--';
}
function signedPct(value) {
  if (!Number.isFinite(Number(value))) return '<span class="flat">--</span>';
  const number = Number(value);
  const className = number > 0 ? 'up' : number < 0 ? 'down' : 'flat';
  return `<span class="${className}">${number > 0 ? '+' : ''}${number.toFixed(2)}%</span>`;
}
function amount(value) {
  if (!Number.isFinite(Number(value))) return '--';
  const number = Number(value);
  return number >= 100000000 ? `${(number / 100000000).toFixed(2)}亿` : `${(number / 10000).toFixed(0)}万`;
}
function readableTime(value) {
  if (!value) return '暂无';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? esc(value) : parsed.toLocaleString('zh-CN', {hour12: false});
}
function showToast(message, type = 'ok') {
  const toast = $('#toast');
  toast.textContent = message;
  toast.className = `toast show ${type === 'error' ? 'error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3200);
}
async function api(path, options) {
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.error || '请求失败');
  return body;
}

function renderIndexes(indexes) {
  if (!indexes.length) return '<div class="empty-state compact">指数行情暂不可用。</div>';
  return indexes.map((quote) => `
    <button class="index-card index-focus-button" type="button" data-code="${esc(quote.code)}">
      <span>${esc(quote.name)}</span>
      <strong>${money(quote.price)}</strong>
      <small>${signedPct(quote.change_pct)} · ${readableTime(quote.exchange_data_time)}</small>
    </button>
  `).join('');
}

function renderWatchlist(items) {
  if (!items.length) return '<div class="empty-state">还没有重点监视股票。</div>';
  return items.map((item) => {
    const quote = item.quote || {};
    return `<div class="watch-compact-row">
      <button class="stock-button focus-button" data-code="${esc(item.code)}">
        <strong>${esc(item.code)} ${esc(item.name || quote.name || '')}</strong>
        <small>${money(quote.price)} · ${readableTime(quote.exchange_data_time || quote.data_time)}</small>
      </button>
      <strong>${signedPct(quote.change_pct)}</strong>
      <button class="danger-button remove-watch" data-code="${esc(item.code)}" type="button" aria-label="移除 ${esc(item.code)}">×</button>
    </div>`;
  }).join('');
}

function renderPositions(items) {
  if (!items.length) return '<div class="empty-state">暂无持仓。录入后系统才会追踪卖出条件。</div>';
  return items.map((item) => {
    const quote = item.quote || {};
    const risk = item.risk || {};
    return `<div class="position-row">
      <button class="stock-button detail-button" data-code="${esc(item.code)}"><strong>${esc(item.code)} ${esc(item.name || quote.name || '')}</strong><small>${item.quantity} 股 · 成本 ${money(item.cost_price)}</small></button>
      <span><small>当前价</small><strong>${money(quote.price)}</strong></span>
      <span><small>浮盈亏</small><strong>${risk.unrealized_pnl === undefined ? '--' : `${risk.unrealized_pnl >= 0 ? '+' : ''}${money(risk.unrealized_pnl)}`}</strong><small>${risk.return_pct === undefined ? '' : signedPct(risk.return_pct)}</small></span>
      <span class="position-risk ${esc(risk.status)}">${esc(risk.message || '等待风控计算')}<small>止损 ${money(risk.stop_price)} · 止盈 ${money(risk.target_price)}</small></span>
      <span class="row-actions"><button class="sell-button close-position" data-id="${item.id}" data-code="${esc(item.code)}" data-name="${esc(item.name || quote.name || '')}" type="button">记录卖出</button></span>
    </div>`;
  }).join('');
}

function renderOpportunities(items, research = {}, executionAllowed = false) {
  if (!executionAllowed) {
    return `<div class="empty-state">
      <b>真钱门禁锁定 · 不生成买入候选</b>
      <span>${esc(research.message || '历史、前向、模拟和数据门禁尚未全部通过。')}</span>
      <small>图表和研究观察保持可用；本区不会把历史候选包装成今天可执行的机会。</small>
    </div>`;
  }
  if (!items.length) {
    const decision = research.current_manual_decision || {};
    return `<div class="empty-state opportunity-open-empty">
      <b>今日策略裁决：空仓 · 候选 0 / 3</b>
      <span>${esc(decision.reason || '今日没有符合冻结规则的新候选。')}</span>
      <small>这代表已授权策略主动不交易，不是功能故障。</small>
    </div>`;
  }
  return items.slice(0, 3).map((item) => `
    <article class="signal-card candidate-card">
      <span class="tag red">候选 ${esc(item.level || '强')}</span>
      <a class="candidate-name" href="${esc(item.analysis_url || `/analysis?code=${encodeURIComponent(item.code)}&interval=5m`)}">
        <strong>${esc(item.code)} ${esc(item.name)}</strong>
        <small>${esc(item.action || '打开实时K线、买入触发与卖出条件')} →</small>
      </a>
      <p>${esc(item.reason)}</p>
      <p><b>计划买入：</b>${esc(item.buy_zone || '--')}<br><b>计划卖出：</b>${esc(item.exit_plan || '--')}<br><b>仓位上限：</b>¥${money(item.position_cap)}，人工确认</p>
      <div class="candidate-actions">
        <button class="ghost-button compact-button focus-button" data-code="${esc(item.code)}" type="button">首页主图</button>
        <a href="${esc(item.analysis_url || `/analysis?code=${encodeURIComponent(item.code)}&interval=5m`)}">完整分析</a>
      </div>
    </article>
  `).join('');
}

function renderManualCandidates(payload) {
  const target = $('#manualCandidates');
  if (!target) return;
  const items = payload?.items || [];
  if (!items.length) {
    target.className = 'empty-state compact';
    target.innerHTML = `<b>暂无可用盯盘候选</b><span>${esc(payload?.message || '添加重点监视股票后，显示实时条件链。')}</span>`;
    return;
  }
  target.className = 'signal-list manual-candidate-list';
  target.innerHTML = items.map((item) => {
    const state = item.state || {};
    const quote = item.quote || {};
    const tier = state.code === 'prepare' ? 'yellow' : 'gray';
    const label = state.code === 'prepare' ? '技术条件接近' : '技术观察';
    const reasonChain = [...(item.reasons || []), ...(item.cautions || []).map((value) => `注意：${value}`)].join('；') || '指标条件不足';
    const buyRange = Array.isArray(item.buy_range)
      ? `${money(item.buy_range[0])} – ${money(item.buy_range[1])}` : '--';
    return `<article class="signal-card candidate-card manual-candidate-card">
      <span class="tag ${tier}">${esc(label)}</span>
      <a class="candidate-name" href="${esc(item.analysis_url)}">
        <strong>${esc(item.code)} ${esc(item.name || '')} · ${money(quote.price)}</strong>
        <small>数据 ${esc(item.information_time || '暂无')} · ${esc(item.manual_action || '')}</small>
      </a>
      <p><b>为什么关注：</b>${esc(reasonChain)}</p>
      <p><b>研究触发：</b>${esc(item.trigger || '--')}<br><b>研究价格区间：</b>${esc(buyRange)} · <b>失效线：</b>${money(item.protective_stop)} · <b>首个复核目标：</b>${money(item.first_target)}</p>
      <div class="candidate-actions">
        <button class="ghost-button compact-button focus-button" data-code="${esc(item.code)}" type="button">首页主图</button>
        <a href="${esc(item.analysis_url)}">完整论证</a>
      </div>
    </article>`;
  }).join('');
}

async function refreshManualCandidates() {
  if (manualCandidateRequestRunning || document.visibilityState === 'hidden') return;
  manualCandidateRequestRunning = true;
  try {
    const payload = await api('/api/manual-candidates');
    manualCandidates = payload.items || [];
    renderManualCandidates(payload);
    bindDynamicActions();
    if (dashboard) renderFocus(dashboard);
  } catch (error) {
    const target = $('#manualCandidates');
    if (target) {
      target.className = 'empty-state compact';
      target.innerHTML = `<b>实时盯盘暂不可用</b><span>${esc(error.message)}；不会据此生成交易提示。</span>`;
    }
  } finally {
    manualCandidateRequestRunning = false;
  }
}

async function refreshV21Pilot() {
  const panel = $('#v21DashboardPilot');
  if (!panel) return;
  try {
    const payload = await api('/api/research/live?view=compact');
    const pilot = payload.manual_pilot || {};
    const audit = pilot.integrity_audit || {};
    const winTarget = audit.win_rate_target || {};
    const candidate = pilot.candidate || null;
    realMoneyAllowed = payload.gate_levels?.real_money?.passed === true
      && pilot.real_money_allowed === true;
    $('#v21DashboardStatus').textContent = realMoneyAllowed ? '人工执行已授权' : '真钱锁定';
    $('#v21DashboardStatus').className = `tag ${realMoneyAllowed ? 'green' : 'red'}`;
    panel.className = `panel dashboard-pilot ${realMoneyAllowed ? 'unlocked' : 'locked'}`;
    $('#v21DashboardMessage').textContent = pilot.message || '等待V21独立裁决。';
    $('#v21DashboardEntry').textContent = pilot.execution_rule?.entry || '--';
    $('#v21DashboardExit').textContent = pilot.execution_rule?.exit || '--';
    $('#v21DashboardPosition').textContent = pilot.execution_rule?.position_limit || '--';
    $('#v21DashboardEvidence').textContent = Object.keys(winTarget).length
      ? `历史${Number(winTarget.trades || 0)}笔 · 胜率${Number(winTarget.observed_pct || 0).toFixed(3)}% · 60%${winTarget.supported ? '已支持' : '未达到'}`
      : '等待独立复核';
    if (realMoneyAllowed && candidate) {
      $('#v21DashboardCandidate').className = 'dashboard-pilot-candidate actionable';
      $('#v21DashboardCandidate').innerHTML = `
        <span>当前候选</span>
        <a href="${esc(candidate.analysis_url)}"><strong>${esc(candidate.name || candidate.code)} · ${esc(candidate.code)}</strong></a>
        <small>计划 ${esc(candidate.planned_entry_date || '--')} 开盘前复核 · ${esc(candidate.planned_exit_date || '--')} 收盘退出 · 错过不追</small>
      `;
    } else {
      $('#v21DashboardCandidate').className = 'dashboard-pilot-candidate';
      $('#v21DashboardCandidate').innerHTML = realMoneyAllowed
        ? `<strong>当前决策：空仓</strong><small>${esc(pilot.current_decision?.reason || '保持空仓等待，不追旧公告。')}</small>`
        : '<strong>仅历史证据观察</strong><small>前向、模拟、数据新鲜度与独立复现未全部通过，禁止买入。</small>';
    }
    if (dashboard) {
      $('#strongSignals').innerHTML = renderOpportunities(
        dashboard.opportunities || [], dashboard.research || {}, realMoneyAllowed,
      );
      $('#opportunityGate').textContent = realMoneyAllowed
        ? (dashboard.opportunities?.length
          ? `人工执行候选 ${Math.min(3, dashboard.opportunities.length)} / 3`
          : '人工执行已授权 · 今日空仓')
        : '真钱锁定 · 候选 0 / 3';
      $('#opportunityGate').className = `tag ${realMoneyAllowed ? 'green' : 'red'}`;
    }
  } catch (error) {
    $('#v21DashboardStatus').textContent = '状态读取失败';
    $('#v21DashboardStatus').className = 'tag red';
    $('#v21DashboardMessage').textContent = error.message;
  }
}

function renderResearch(research) {
  renderFactorMining(research.factor_mining);
  const result = research.latest_preliminary;
  if (!result) {
    $('#researchGate').textContent = '无报告';
    $('#researchMetrics').innerHTML = '<div class="empty-state compact">尚未生成真实回测报告。</div>';
    $('#researchLimitations').textContent = '没有报告时，不允许生成买入建议。';
    return;
  }
  $('#researchGate').textContent = result.gate_passed ? '该报告通过' : '旧报告已封存失败';
  $('#researchGate').className = `tag ${result.gate_passed ? 'green' : 'red'}`;
  const metrics = [
    ['样本区间', `${result.sample_start.slice(0, 10)} 至 ${result.sample_end.slice(0, 10)}`],
    ['交易日', `${result.trading_days} 天`],
    ['合格股票', result.eligible_symbols === null || result.eligible_symbols === undefined ? '--' : `${result.eligible_symbols} 只`],
    ['交易笔数', `${result.trades} 笔`],
    ['胜率', `${Number(result.win_rate_pct).toFixed(2)}%`],
    ['总收益', `${Number(result.total_return_pct).toFixed(3)}%`],
    ['年化收益', result.annualized_return_pct === null || result.annualized_return_pct === undefined ? '--' : `${Number(result.annualized_return_pct).toFixed(3)}%`],
    ['同期上证', result.benchmark_return_pct === null || result.benchmark_return_pct === undefined ? '--' : `${Number(result.benchmark_return_pct).toFixed(3)}%`],
    ['最大回撤', `${Number(result.max_drawdown_pct).toFixed(3)}%`],
    ['盈利因子', result.profit_factor === null ? '--' : Number(result.profit_factor).toFixed(3)],
    ['夏普比率', result.sharpe === null || result.sharpe === undefined ? '--' : Number(result.sharpe).toFixed(3)],
    ['估算交易成本', result.estimated_transaction_costs === null || result.estimated_transaction_costs === undefined ? '--' : `¥${Number(result.estimated_transaction_costs).toFixed(2)}`],
  ];
  $('#researchMetrics').innerHTML = metrics.map(([label, value]) => `
    <div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>
  `).join('');
  const dataGate = research.data_gate;
  $('#dataGateSummary').innerHTML = dataGate ? `
    <div class="v3-heading"><strong>V4 数据门禁进度</strong><small>真实数据优先 · 尚未完成</small></div>
    <div class="data-gate-grid">
      <span><small>第二源抽查</small><b>${dataGate.crosscheck_passed}/${dataGate.crosscheck_total} 项</b></span>
      <span><small>历史状态</small><b>${dataGate.status_symbols} 只 · ${Number(dataGate.status_rows).toLocaleString('zh-CN')} 条</b></span>
      <span><small>上市/退市清单</small><b>${Number(dataGate.stock_basic_rows).toLocaleString('zh-CN')} 只 · 退市 ${dataGate.delisted_stock_rows}</b></span>
      <span><small>数据哈希</small><b>${dataGate.manifest_files} 个文件</b></span>
      <span><small>滑点压力</small><b>${dataGate.slippage_ready ? dataGate.slippage_levels_bps.map((value) => `${value}BP`).join(' / ') : '未完成'}</b></span>
      <span><small>V4 全量日线</small><b>${Number(dataGate.v4_daily_downloaded).toLocaleString('zh-CN')} / ${Number(dataGate.v4_daily_target).toLocaleString('zh-CN')} · ${Number(dataGate.v4_daily_coverage_pct).toFixed(3)}%</b></span>
      <span><small>V4 质量审计</small><b>${Number(dataGate.v4_quality_passed).toLocaleString('zh-CN')} 通过 / ${Number(dataGate.v4_quality_failed).toLocaleString('zh-CN')} 失败 · ${Number(dataGate.v4_quality_rows).toLocaleString('zh-CN')} 行</b></span>
      <span><small>特殊价格事件</small><b>${Number(dataGate.v4_verified_events).toLocaleString('zh-CN')} 已核验 / ${Number(dataGate.v4_limit_event_reviews).toLocaleString('zh-CN')} 待核验</b></span>
    </div>
    <small class="data-gate-remaining">待完成：${dataGate.remaining.map(esc).join('、')}</small>
  ` : '';
  const v3 = research.v3;
  const v3First = v3?.strategies?.[0];
  $('#v3Strategies').innerHTML = v3 ? `
    <div class="v3-heading"><strong>V3 冻结规则验证</strong><small>${esc(v3First?.oos_start || '--')} 至 ${esc(v3First?.oos_end || '--')} · 真实日线 · 成本后</small></div>
    ${v3.strategies.map((item) => `
      <div class="v3-row">
        <span><b>策略 ${esc(item.strategy_id)}</b><small>${item.gate_passed ? '门禁通过' : `失败 ${item.failed_gates.length} 项`}</small></span>
        <span><small>收益</small><b class="${Number(item.total_return_pct) >= 0 ? 'up' : 'down'}">${Number(item.total_return_pct) >= 0 ? '+' : ''}${Number(item.total_return_pct).toFixed(3)}%</b></span>
        <span><small>胜率</small><b>${Number(item.win_rate_pct).toFixed(2)}%</b></span>
        <span><small>最大回撤</small><b>${Number(item.max_drawdown_pct).toFixed(3)}%</b></span>
        <span><small>交易</small><b>${item.trades} 笔</b></span>
      </div>
    `).join('')}
  ` : '<div class="empty-state compact">V3冻结规则尚未运行。</div>';
  $('#researchLimitations').textContent = result.gate_passed
    ? '历史结果仍需V3门禁及6–8周模拟盘确认，不能直接投入真钱。'
    : `当前策略在 ${result.trading_days} 个样本外交易日仍未通过门禁；V3三套候选也均未通过，研究标记不进入今日机会、微信买入提醒或模拟盘。`;
}

function renderFactorMining(mining) {
  if (!mining) {
    $('#factorMiningStatus').textContent = '尚未运行';
    $('#factorMiningStatus').className = 'tag gray';
    return;
  }
  const passed = Boolean(mining.research_gate_passed);
  $('#factorMiningStatus').textContent = passed ? '研究门禁通过' : '仅研究 · 策略未通过';
  $('#factorMiningStatus').className = `tag ${passed ? 'green' : 'red'}`;
  $('#factorCandidateCount').textContent = `${Number(mining.candidate_count || 0)} 个`;
  $('#factorConfirmedCount').textContent = `${Number(mining.heldout_confirmed_count || 0)} 个`;
  $('#factorStrategyPassCount').textContent = `${Number(mining.strategy_gate_passed_count || 0)} 个`;
  $('#factorMiningTime').textContent = readableTime(mining.generated_at);

  const confirmed = (mining.confirmed || []).map((item) => `
    <div class="factor-result-row">
      <span>
        <b>${esc(item.factor_id)}</b>
        <small>${esc(item.family)} · 测试集IC ${Number(item.test_mean_ic).toFixed(4)}</small>
      </span>
      <span>
        <small>正IC日</small>
        <b>${Number(item.test_positive_ic_days_pct).toFixed(2)}%</b>
      </span>
      <span>
        <small>五日分层差</small>
        <b>${Number(item.test_spread_pct) >= 0 ? '+' : ''}${Number(item.test_spread_pct).toFixed(4)}%</b>
      </span>
    </div>
  `).join('');
  const backtests = (mining.backtests || []).map((item) => `
    <div class="factor-result-row backtest">
      <span>
        <b>${esc(item.factor_id)} · 含成本</b>
        <small>${item.gate_passed ? '策略门禁通过' : `失败 ${item.failed_gates.length} 项`}</small>
      </span>
      <span>
        <small>收益 / 回撤</small>
        <b>${Number(item.total_return_pct) >= 0 ? '+' : ''}${Number(item.total_return_pct).toFixed(3)}% / ${Number(item.max_drawdown_pct).toFixed(3)}%</b>
      </span>
      <span>
        <small>胜率 / 盈利因子</small>
        <b>${Number(item.win_rate_pct).toFixed(2)}% / ${item.profit_factor === null ? '--' : Number(item.profit_factor).toFixed(3)}</b>
      </span>
    </div>
  `).join('');
  $('#factorMiningResults').className = 'factor-mining-results';
  $('#factorMiningResults').innerHTML = `
    <div class="v3-heading"><strong>独立测试确认</strong><small>确认不等于可交易</small></div>
    ${confirmed || '<div class="empty-state compact">当前没有候选通过独立测试。</div>'}
    <div class="v3-heading"><strong>单因子策略回测</strong><small>¥10,000 · T+1 · 含成本</small></div>
    ${backtests || '<div class="empty-state compact">尚未完成含成本回测。</div>'}
  `;
  $('#factorMiningWarning').textContent = mining.warning
    || '固定股票池结果不能进入实盘，仍需全量V4验证。';
}

function renderIntegrations(items) {
  const statusMap = {
    active: ['运行中', 'green'],
    validated_reference: ['公式已交叉验证', 'green'],
    isolated_research: ['隔离研究', 'yellow'],
    compatible_downloaded: ['已下载待适配', 'yellow'],
    architecture_only: ['仅架构研究', 'yellow'],
    download_retry_pending: ['下载重试中', 'gray'],
    rejected_license: ['商业化冲突', 'red'],
    candidate_download_deferred: ['候选待下载', 'gray'],
    license_quarantine: ['许可证隔离', 'red'],
  };
  $('#integrationList').innerHTML = items.map((item) => {
    const status = statusMap[item.status] || [item.status, 'gray'];
    return `<div class="integration-row">
      <span><strong>${esc(item.name)}</strong><small>${esc(item.role)}</small></span>
      <span><small>${esc(item.license)}${item.local_commit ? ` · ${esc(item.local_commit)}` : ''}</small><b class="tag ${status[1]}">${status[0]}</b></span>
    </div>`;
  }).join('');
}

function renderFeeProfile(fees) {
  if (!fees) return;
  const confirmed = fees.status === 'user_confirmed';
  $('#feeStatus').textContent = confirmed ? '已人工核对' : '保守默认值';
  $('#feeStatus').className = `tag ${confirmed ? 'green' : 'yellow'}`;
  const form = $('#feeProfileForm');
  if (!form.contains(document.activeElement)) {
    form.elements.commission_wan.value = Number(fees.commission_wan);
    form.elements.minimum_commission.value = Number(fees.minimum_commission);
  }
  $('#feeMessage').textContent = `${fees.message} 费率上限参考：万${Number(fees.official_boundary.maximum_wan).toFixed(0)}。`;
}

function renderPaper(paper) {
  if (!paper) return;
  const autonomous = paper.autonomous_research_forward || null;
  const statusMap = {
    waiting_for_research_gate: ['等待历史门禁', 'red'],
    ready_for_manual_start: ['等待人工启动', 'yellow'],
    formal_forward_validation: ['正式模拟验证中', 'green'],
    ready_for_forward_gate_review: ['等待独立复核', 'yellow'],
    running: ['模拟运行中', 'green'],
  };
  const status = autonomous
    ? ['自主前向运行中', 'green']
    : (statusMap[paper.status] || [paper.status, 'gray']);
  $('#paperStatus').textContent = status[0];
  $('#paperStatus').className = `tag ${status[1]}`;
  $('#paperCash').textContent = `¥${Number(paper.cash).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  $('#paperSignals').textContent = autonomous
    ? `${Number(autonomous.eligible_samples || 0)} 条`
    : `${paper.signal_count} 条`;
  $('#paperTrades').textContent = autonomous
    ? `${Number(autonomous.completed_trades || 0)} / ${Number(autonomous.minimum_completed_trades || 30)} 笔`
    : `${paper.completed_trades} / ${paper.validation_target.completed_trades_min} 笔`;
  $('#paperMessage').textContent = autonomous
    ? `${Number(autonomous.waiting_for_next_open || 0)} 个等待下一开盘，${Number(autonomous.open_not_due || 0)} 个持仓未到退出日；系统无人值守推进，不需人工确认，不向券商下单。`
    : paper.message;
}

function renderDeepSeek() {
  $('#deepseekStatus').textContent = '已禁用';
  $('#deepseekStatus').className = 'tag gray';
  $('#deepseekMessage').textContent = '外部付费 API 已禁用，本地确定性研究继续。';
}

function renderResearchWorker(worker) {
  const current = worker || {};
  const progress = current.progress || {};
  const statusMap = {
    not_started: ['未启动', 'gray'],
    starting: ['启动中', 'yellow'],
    running: ['运行中', 'green'],
    retry_wait: ['自动重试', 'yellow'],
    stopping: ['停止中', 'yellow'],
    stopped: ['已停止', 'gray'],
    stale: ['进程失联', 'red'],
  };
  const status = statusMap[current.status] || [current.status || '未知', 'gray'];
  const coverage = Math.max(0, Math.min(100, Number(progress.coverage_pct || 0)));
  const active = ['starting', 'running', 'retry_wait', 'stopping'].includes(current.status);
  $('#researchWorkerStatus').textContent = status[0];
  $('#researchWorkerStatus').className = `tag ${status[1]}`;
  $('#workerProgressText').textContent = `${Number(progress.downloaded_symbols || 0).toLocaleString('zh-CN')} / ${Number(progress.target_symbols || 0).toLocaleString('zh-CN')} · ${coverage.toFixed(3)}%`;
  $('#workerProgressBar').style.width = `${coverage}%`;
  $('#workerStep').textContent = current.current_step || '等待启动';
  $('#workerCycle').textContent = `${Number(current.cycle || 0)} 轮`;
  $('#workerAudit').textContent = `${Number(progress.audit_passed || 0).toLocaleString('zh-CN')} 通过 / ${Number(progress.audit_failed || 0)} 失败`;
  $('#workerFactors').textContent = `${Number(progress.factor_symbols || 0).toLocaleString('zh-CN')} 只 · ${Number(progress.factor_rows || 0).toLocaleString('zh-CN')} 行`;
  $('#workerMinedFactors').textContent = `${Number(progress.mined_candidates || 0)} 候选 · ${Number(progress.heldout_confirmed_factors || 0)} 独立确认`;
  $('#workerFactorGates').textContent = `${Number(progress.factor_strategy_gates_passed || 0)} 个通过`;
  $('#workerDeepSeek').textContent = '已禁用 · 本地研究继续';
  $('#workerHeartbeat').textContent = readableTime(current.heartbeat_at);
  const baseMessage = current.last_error
    ? `${current.message || ''} 最近错误：${current.last_error}`
    : (current.message || '等待启动。');
  $('#workerMessage').textContent = baseMessage;
  $('#workerStart').disabled = active;
  $('#workerStop').disabled = !active || current.status === 'stopping';
}

function focusItems(data = dashboard) {
  if (!data) return [];
  const merged = [
    ...(data.opportunities || []),
    ...manualCandidates,
    ...(data.observations || []),
    ...(data.watchlist || []),
    ...(data.positions || []),
  ];
  return merged.filter((item, index) => (
    item?.code && merged.findIndex((candidate) => candidate?.code === item.code) === index
  ));
}

function focusItemForCode(code) {
  return focusItems().find((item) => item.code === code);
}

function focusQuote(item) {
  if (!item) return {};
  if (item.quote) return item.quote;
  const fromWatchlist = dashboard?.watchlist?.find((entry) => entry.code === item.code);
  const fromPosition = dashboard?.positions?.find((entry) => entry.code === item.code);
  return fromWatchlist?.quote || fromPosition?.quote || item;
}

function updateFocusHeader(code) {
  const item = focusItemForCode(code);
  const quote = focusQuote(item);
  const opportunity = dashboard?.opportunities?.find((entry) => entry.code === code);
  const manualCandidate = manualCandidates.find((entry) => entry.code === code);
  const observation = dashboard?.observations?.find((entry) => entry.code === code);
  const position = dashboard?.positions?.find((entry) => entry.code === code);
  const name = item?.name || quote.name || '';
  $('#focusSymbol').textContent = `${code} ${name}`.trim();
  $('#focusPrice').textContent = money(quote.price);
  $('#focusChange').innerHTML = signedPct(quote.change_pct);
  $('#focusQuoteTime').textContent = readableTime(quote.exchange_data_time || quote.data_time);

  let state = ['重点观察', 'gray'];
  let decision = ['禁止买入', '等待策略门禁', '未生成', '未生成'];
  if (opportunity) {
    state = ['可以买入 · 待确认', 'red'];
    decision = [
      '可以买入 · 人工确认',
      opportunity.reason || '实时条件满足',
      opportunity.buy_zone || '查看信号详情',
      opportunity.stop_loss ? money(opportunity.stop_loss) : '查看信号详情',
    ];
  } else if (manualCandidate) {
    state = [
      '研究观察 · 真钱未授权',
      'yellow',
    ];
    decision = [
      '仅研究，不得买入',
      manualCandidate.trigger || '等待技术条件变化，仅用于观察',
      '完整分析页查看不可执行参考区间',
      '不构成止损指令',
    ];
  } else if (observation) {
    state = ['研究观察 · 可分析', 'green'];
    decision = [
      '查看条件，不直接买入',
      observation.reason,
      '分钟K与日K均已开放',
      '以条件失效线为准',
    ];
  } else if (position) {
    state = ['持仓监控', 'green'];
    decision = [
      position.risk?.message || '继续监控',
      '按持仓风控条件处理',
      `成本 ${money(position.cost_price)}`,
      money(position.risk?.stop_price),
    ];
  }
  $('#focusSignalState').textContent = state[0];
  $('#focusSignalState').className = `tag ${state[1]}`;
  $('#focusDecision').innerHTML = [
    ['当前动作', decision[0]],
    ['触发条件', decision[1]],
    ['价格区间 / 成本', decision[2]],
    ['失效 / 止损线', decision[3]],
  ].map(([label, value]) => `<div><span>${esc(label)}</span><strong title="${esc(value)}">${esc(value)}</strong></div>`).join('');
}

function updateIndexFocusHeader(code) {
  const quote = dashboard?.market?.indexes?.find((entry) => entry.code === code) || {};
  $('#focusSymbol').textContent = `${quote.name || '市场指数'} ${code.replace(/^(sh|sz)/, '')}`;
  $('#focusPrice').textContent = money(quote.price);
  $('#focusChange').innerHTML = signedPct(quote.change_pct);
  $('#focusQuoteTime').textContent = readableTime(
    quote.exchange_data_time || quote.data_time,
  );
  $('#focusSignalState').textContent = '大盘环境 · 不可交易';
  $('#focusSignalState').className = 'tag blue';
  $('#focusDecision').innerHTML = [
    ['当前动作', '观察市场环境'],
    ['用途', '辅助判断牛市、熊市与震荡'],
    ['个股买入', '不由指数图直接触发'],
    ['风险原则', '研究门禁继续生效'],
  ].map(([label, value]) => `<div><span>${esc(label)}</span><strong title="${esc(value)}">${esc(value)}</strong></div>`).join('');
}

function emaSeries(candles, period) {
  const alpha = 2 / (period + 1);
  let previous = null;
  return candles.map((candle) => {
    previous = previous === null
      ? Number(candle.close)
      : alpha * Number(candle.close) + (1 - alpha) * previous;
    return {time: candleTime(candle.time), value: previous};
  });
}

function candleTime(value) {
  const raw = String(value);
  const normalized = raw.includes(' ')
    ? raw.replace(' ', 'T')
    : /^\d{4}-\d{2}-\d{2}$/.test(raw) ? `${raw}T00:00:00` : raw;
  const parsed = new Date(`${normalized}+08:00`);
  return Math.floor(parsed.getTime() / 1000);
}

function clearFocusChart() {
  if (focusChartResizeObserver) {
    focusChartResizeObserver.disconnect();
    focusChartResizeObserver = null;
  }
  if (focusChartInstance) {
    focusChartInstance.remove();
    focusChartInstance = null;
  }
  $('#focusChart').innerHTML = '';
}

function showFocusChartEmpty(title, message) {
  clearFocusChart();
  $('#focusChart').innerHTML = `
    <div id="focusChartEmpty" class="chart-empty">
      <strong>${esc(title)}</strong>
      <span>${esc(message)}</span>
    </div>`;
}

function drawFocusChart(result) {
  if (!window.LightweightCharts) {
    showFocusChartEmpty('K线组件没有加载', '本地开源图表资源不可用。');
    return;
  }
  clearFocusChart();
  const container = $('#focusChart');
  const rawCandles = result.candles || [];
  if (!rawCandles.length) {
    showFocusChartEmpty('暂无可核验K线', '系统不会用空数据绘制研究触发标记。');
    return;
  }
  const chart = window.LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight,
    layout: {
      background: {type: window.LightweightCharts.ColorType.Solid, color: '#07101a'},
      textColor: '#8190a4',
      attributionLogo: true,
    },
    grid: {
      vertLines: {color: '#152131'},
      horzLines: {color: '#152131'},
    },
    rightPriceScale: {borderColor: '#263449'},
    timeScale: {
      borderColor: '#263449',
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 5,
    },
    crosshair: {
      vertLine: {color: '#6f86a6', labelBackgroundColor: '#263b58'},
      horzLine: {color: '#6f86a6', labelBackgroundColor: '#263b58'},
    },
    handleScroll: true,
    handleScale: true,
  });
  focusChartInstance = chart;
  const candleSeries = chart.addSeries(
    window.LightweightCharts.CandlestickSeries,
    {
      upColor: '#ff5b63',
      downColor: '#20bd7a',
      borderUpColor: '#ff5b63',
      borderDownColor: '#20bd7a',
      wickUpColor: '#ff767d',
      wickDownColor: '#38cc8d',
    },
  );
  const candleData = rawCandles.map((item) => ({
    time: candleTime(item.time),
    open: Number(item.open),
    high: Number(item.high),
    low: Number(item.low),
    close: Number(item.close),
  }));
  candleSeries.setData(candleData);

  const volumeSeries = chart.addSeries(
    window.LightweightCharts.HistogramSeries,
    {
      priceFormat: {type: 'volume'},
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    },
  );
  volumeSeries.priceScale().applyOptions({
    scaleMargins: {top: 0.82, bottom: 0},
  });
  volumeSeries.setData(rawCandles.map((item) => ({
    time: candleTime(item.time),
    value: Number(item.volume),
    color: Number(item.close) >= Number(item.open)
      ? 'rgba(255,91,99,.35)'
      : 'rgba(32,189,122,.35)',
  })));

  const ema5 = chart.addSeries(window.LightweightCharts.LineSeries, {
    color: '#f0b94a',
    lineWidth: 1,
    lastValueVisible: false,
    priceLineVisible: false,
  });
  const ema20 = chart.addSeries(window.LightweightCharts.LineSeries, {
    color: '#4f8cff',
    lineWidth: 1,
    lastValueVisible: false,
    priceLineVisible: false,
  });
  ema5.setData(emaSeries(rawCandles, 5));
  ema20.setData(emaSeries(rawCandles, 20));

  if (window.LightweightCharts.createSeriesMarkers) {
    const markers = (result.signals || []).map((signal) => ({
      time: candleTime(signal.time),
      position: signal.action === 'research_buy' ? 'belowBar' : 'aboveBar',
      color: realMoneyAllowed
        ? (signal.action === 'research_buy' ? '#f0b94a' : '#20bd7a')
        : '#8190a4',
      shape: signal.action === 'research_buy' ? 'arrowUp' : 'arrowDown',
      text: realMoneyAllowed
        ? (signal.action === 'research_buy' ? '执行触发' : '风险触发')
        : (signal.action === 'research_buy' ? '研究触发·不可执行' : '风险观察'),
    }));
    window.LightweightCharts.createSeriesMarkers(candleSeries, markers);
  }
  chart.timeScale().fitContent();
  focusChartResizeObserver = new ResizeObserver(() => {
    if (focusChartInstance) {
      focusChartInstance.applyOptions({
        width: container.clientWidth,
        height: container.clientHeight,
      });
    }
  });
  focusChartResizeObserver.observe(container);
}

async function loadFocusChart(code, interval = focusInterval) {
  const requestedCode = code;
  const requestedInterval = interval;
  const requestedMode = focusMode;
  const requestKey = `${requestedMode}:${requestedCode}:${requestedInterval}`;
  if (focusChartRequestKey === requestKey) return;
  focusChartRequestKey = requestKey;
  $('#focusChartMessage').textContent = `正在读取 ${interval} 真实行情…`;
  try {
    const endpoint = requestedMode === 'index'
      ? `/api/market/index-candles/${code}?limit=240&interval=${interval}`
      : `/api/candles/${code}?limit=240&interval=${interval}`;
    const result = await api(endpoint);
    if (
      focusCode !== requestedCode
      || focusInterval !== requestedInterval
      || focusMode !== requestedMode
    ) return;
    drawFocusChart(result);
    focusChartLastLoadedAt = Date.now();
    const liveLabel = result.freshness === 'live' ? '交易时段' : '最近收盘';
    const suffix = requestedMode === 'index'
      ? '指数图只用于市场环境判断'
      : '研究标记仅作观察；真钱买入以今日机会裁决为准';
    $('#focusChartMessage').textContent = `${liveLabel} · ${result.source} · 最新 ${result.latest_time} · ${suffix}`;
  } catch (error) {
    if (
      focusCode !== requestedCode
      || focusInterval !== requestedInterval
      || focusMode !== requestedMode
    ) return;
    showFocusChartEmpty(
      requestedMode === 'index' ? '指数分时暂时不可用' : 'K线暂时不可用',
      error.message,
    );
    $('#focusChartMessage').textContent = error.message;
  } finally {
    if (focusChartRequestKey === requestKey) focusChartRequestKey = null;
  }
}

function selectFocusStock(code, {forceReload = true} = {}) {
  if (!code) return;
  const changed = focusMode !== 'stock' || focusCode !== code;
  focusMode = 'stock';
  focusCode = code;
  updateFocusHeader(code);
  if (changed || forceReload || Date.now() - focusChartLastLoadedAt > 30_000) {
    loadFocusChart(code, focusInterval);
  }
}

function selectFocusIndex(code = 'sh000001', {forceReload = true} = {}) {
  const changed = focusMode !== 'index' || focusCode !== code;
  focusMode = 'index';
  focusCode = code;
  updateIndexFocusHeader(code);
  if (changed || forceReload || Date.now() - focusChartLastLoadedAt > 30_000) {
    loadFocusChart(code, focusInterval);
  }
}

function renderFocus(data) {
  const candidates = focusItems(data);
  if (!candidates.length) {
    const indexCode = (
      focusMode === 'index'
      && data.market?.indexes?.some((item) => item.code === focusCode)
    ) ? focusCode : 'sh000001';
    selectFocusIndex(indexCode, {
      forceReload: Date.now() - focusChartLastLoadedAt > 30_000,
    });
    return;
  }
  const preferred = data.opportunities?.[0]?.code
    || (focusCode && candidates.some((item) => item.code === focusCode) ? focusCode : null)
    || data.observations?.[0]?.code
    || data.watchlist?.[0]?.code
    || data.positions?.[0]?.code;
  selectFocusStock(preferred, {
    forceReload: preferred !== focusCode || Date.now() - focusChartLastLoadedAt > 30_000,
  });
}

function render(data) {
  dashboard = data;
  const available = data.market.status === 'available';
  $('#connectionDot').className = `status-dot ${available ? (data.market.freshness === 'live' ? 'live' : '') : 'error'}`;
  $('#connection').textContent = data.market.message;
  $('#marketLabel').textContent = data.market.regime.label;
  $('#marketReason').textContent = data.market.regime.message;
  const freshnessMap = {live: '交易时段可用', closed_snapshot: '休市快照', stale: '数据滞后', unavailable: '行情不可用'};
  $('#freshnessLabel').textContent = freshnessMap[data.market.freshness] || '待检查';
  $('#freshnessTime').textContent = available ? '每 60 秒刷新 · 以行情时间为准' : '新信号已暂停';
  $('#exposureValue').textContent = `${Number(data.portfolio.exposure_pct || 0).toFixed(2)}%`;
  $('#portfolioPnl').innerHTML = data.portfolio.unrealized_pnl === null ? '等待持仓行情' : `浮盈亏 ${data.portfolio.unrealized_pnl >= 0 ? '+' : ''}${money(data.portfolio.unrealized_pnl)} 元`;
  $('#researchNotice').textContent = data.research.message;
  $('#strongSignals').innerHTML = renderOpportunities(
    data.opportunities,
    data.research,
    realMoneyAllowed,
  );
  $('#opportunityGate').textContent = realMoneyAllowed
    ? (data.opportunities.length
      ? `人工执行候选 ${Math.min(3, data.opportunities.length)} / 3`
      : '人工执行已授权 · 今日空仓')
    : '真钱锁定 · 候选 0 / 3';
  $('#opportunityGate').className = `tag ${realMoneyAllowed ? 'green' : 'red'}`;
  $('#indexQuotes').innerHTML = renderIndexes(data.market.indexes || []);
  $('#observations').className = data.observations.length
    ? 'signal-list'
    : 'empty-state compact';
  $('#observations').innerHTML = data.observations.length
    ? data.observations.map((item) => `
      <article class="signal-card candidate-card observation-card">
        <span class="tag green">观察已启用</span>
        <a class="candidate-name" href="/analysis?code=${encodeURIComponent(item.code)}&interval=5m">
          <strong>${esc(item.code)} ${esc(item.name)}</strong>
          <small>查看分钟K、日K与可复核条件地图 →</small>
        </a>
        <p>${esc(item.reason)}</p>
        <div class="candidate-actions">
          <button class="ghost-button compact-button focus-button" data-code="${esc(item.code)}" type="button">首页主图</button>
          <a href="/analysis?code=${encodeURIComponent(item.code)}&interval=5m">完整分析</a>
        </div>
      </article>
    `).join('')
    : '<div class="empty-state compact"><b>研究观察已启用</b><span>添加重点监视股票后，这里会持续显示真实行情、分钟K与日K入口。</span></div>';
  $('#watchlist').className = data.watchlist.length
    ? 'watch-compact-list'
    : 'empty-state compact';
  $('#watchlist').innerHTML = renderWatchlist(data.watchlist);
  $('#positions').innerHTML = renderPositions(data.positions);
  $('#universeDisclaimer').textContent = '股票池：沪深主板 + 创业板；已拦截 ST、退市整理、科创板、北交所和录入时低于 ¥5 的股票。自选区负责行情研究，真钱买入只来自“今日机会”的冻结策略裁决。';
  $('#wecomStatus').textContent = data.wecom.configured ? '已配置' : '未配置';
  $('#wecomStatus').className = `tag ${data.wecom.configured ? 'green' : 'gray'}`;
  $('#wecomMetric').textContent = data.wecom.configured ? '已连接' : '未配置';
  renderResearch(data.research);
  renderIntegrations(data.integrations || []);
  renderFeeProfile(data.fees);
  renderPaper(data.paper);
  renderDeepSeek();
  renderResearchWorker(data.research_worker);
  renderFocus(data);
  bindDynamicActions();
}

function bindDynamicActions() {
  document.querySelectorAll('.detail-button').forEach((button) => button.addEventListener('click', () => showDetail(button.dataset.code)));
  document.querySelectorAll('.focus-button').forEach((button) => button.addEventListener('click', () => {
    selectFocusStock(button.dataset.code);
  }));
  document.querySelectorAll('.index-focus-button').forEach((button) => button.addEventListener('click', () => {
    selectFocusIndex(button.dataset.code);
  }));
  document.querySelectorAll('.remove-watch').forEach((button) => button.addEventListener('click', async () => {
    if (!confirm(`确认从重点监视中移除 ${button.dataset.code}？`)) return;
    try {
      await api(`/api/watchlist/${button.dataset.code}`, {method: 'DELETE'});
      showToast('已移除重点监视');
      await refresh(false);
    } catch (error) { showToast(error.message, 'error'); }
  }));
  document.querySelectorAll('.close-position').forEach((button) => button.addEventListener('click', () => {
    const form = $('#closePositionForm');
    form.elements.position_id.value = button.dataset.id;
    $('#closePositionTitle').textContent = `记录卖出 · ${button.dataset.code} ${button.dataset.name}`;
    $('#closePositionDialog').showModal();
  }));
}

function showDetail(code) {
  const items = [...(dashboard.watchlist || []), ...(dashboard.positions || []), ...(dashboard.opportunities || [])];
  const item = items.find((value) => value.code === code);
  if (!item) return;
  const quote = item.quote || item;
  const risk = item.risk || {};
  $('#detailContent').innerHTML = `
    <p class="section-kicker">股票详情</p>
    <h2>${esc(code)} ${esc(item.name || quote.name || '')}</h2>
    <p class="detail-warning">当前研究策略未通过真实样本外验证，不提供虚构的买入区间或必然盈利承诺。</p>
    <div class="detail-list">
      <div><span>当前价</span><strong>${money(quote.price)}</strong></div>
      <div><span>涨跌幅</span><strong>${signedPct(quote.change_pct)}</strong></div>
      <div><span>今开 / 最高 / 最低</span><strong>${money(quote.open_price)} / ${money(quote.high)} / ${money(quote.low)}</strong></div>
      <div><span>成交额</span><strong>${amount(quote.amount)}</strong></div>
      <div><span>行情时间</span><strong>${readableTime(quote.exchange_data_time)}</strong></div>
      <div><span>数据来源</span><strong>${esc(quote.source || '暂无')}</strong></div>
      <div><span>持仓止损参考</span><strong>${risk.stop_price ? money(risk.stop_price) : '录入持仓后计算'}</strong></div>
      <div><span>持仓止盈参考</span><strong>${risk.target_price ? money(risk.target_price) : '录入持仓后计算'}</strong></div>
    </div>
    <div class="kline-section">
      <div class="kline-heading"><div><p class="section-kicker">实时研究图</p><h2>5分钟K线</h2></div><span id="klineMeta" class="tag gray">正在读取</span></div>
      <div class="chart-wrap"><canvas id="klineCanvas" aria-label="${esc(code)} 5分钟K线"></canvas></div>
      <div class="chart-legend"><span><i class="legend-up"></i>上涨K线</span><span><i class="legend-down"></i>下跌K线</span><span><i class="legend-buy"></i>黄色：研究买点</span><span><i class="legend-sell"></i>绿色：研究卖点</span></div>
      <p id="klineMessage" class="notice">正在加载真实5分钟行情…</p>
      <div id="technicalSnapshot" class="technical-snapshot"></div>
      <div id="signalList" class="signal-list"></div>
    </div>`;
  $('#detailDialog').showModal();
  loadFiveMinuteChart(code);
}

async function loadFiveMinuteChart(code) {
  const meta = $('#klineMeta');
  const message = $('#klineMessage');
  try {
    const result = await api(`/api/candles/${code}?limit=160`);
    meta.textContent = result.freshness === 'live' ? '交易时段实时' : '最近收盘快照';
    meta.className = `tag ${result.freshness === 'live' ? 'green' : 'gray'}`;
    const backtest = result.backtest;
    const backtestText = backtest ? ` 初步真实回测：${backtest.trades}笔、胜率${Number(backtest.win_rate_pct).toFixed(2)}%、收益${Number(backtest.total_return_pct).toFixed(2)}%，门禁${backtest.gate_passed ? '通过' : '未通过'}。` : '';
    message.textContent = `${result.message}${backtestText} 数据：${result.source} · 最新 ${result.latest_time}`;
    drawCandles($('#klineCanvas'), result.candles, result.signals);
    const snapshot = result.technical_snapshot || {};
    $('#technicalSnapshot').innerHTML = snapshot.lenses?.length ? `
      <div class="technical-heading"><strong>多维K线诊断</strong><small>${esc(snapshot.message)}</small></div>
      <div class="technical-grid">${snapshot.lenses.map((lens) => `
        <div>
          <span>${esc(lens.name)}</span>
          <strong>${esc(lens.value)}</strong>
          <small>${esc(lens.interpretation)}</small>
        </div>
      `).join('')}</div>
    ` : '';
    const recentSignals = result.signals.slice(-6).reverse();
    $('#signalList').innerHTML = recentSignals.length ? recentSignals.map((signal) => `
      <div class="signal-row">
        <span class="tag ${signal.action === 'research_buy' ? 'yellow' : 'green'}">${esc(signal.label)}</span>
        <strong>${money(signal.price)}</strong>
        <span>${esc(signal.time)}</span>
        <small>${esc(signal.reason)} · 未验证</small>
      </div>
    `).join('') : '<div class="empty-state compact">最近区间没有触发研究交叉点。</div>';
  } catch (error) {
    meta.textContent = 'K线不可用';
    meta.className = 'tag gray';
    message.textContent = error.message;
    $('#technicalSnapshot').innerHTML = '';
    $('#signalList').innerHTML = '<div class="empty-state compact">无法读取5分钟K线。</div>';
  }
}

function drawCandles(canvas, allCandles, allSignals) {
  const candles = allCandles.slice(-90);
  if (!candles.length) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(canvas.parentElement.clientWidth, 520);
  const height = 360;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const context = canvas.getContext('2d');
  context.scale(ratio, ratio);
  const pad = {left: 8, right: 58, top: 18, bottom: 24};
  const priceBottom = 270;
  const innerWidth = width - pad.left - pad.right;
  const candleStep = innerWidth / candles.length;
  const candleWidth = Math.max(2, candleStep * .62);
  const high = Math.max(...candles.map((item) => item.high));
  const low = Math.min(...candles.map((item) => item.low));
  const span = Math.max(high - low, high * .002);
  const topPrice = high + span * .08;
  const bottomPrice = low - span * .08;
  const maxVolume = Math.max(...candles.map((item) => item.volume), 1);
  const yForPrice = (price) => pad.top + (topPrice - price) / (topPrice - bottomPrice) * (priceBottom - pad.top);

  context.fillStyle = '#10151d';
  context.fillRect(0, 0, width, height);
  context.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
  context.textAlign = 'left';
  for (let line = 0; line <= 4; line += 1) {
    const y = pad.top + (priceBottom - pad.top) * line / 4;
    const price = topPrice - (topPrice - bottomPrice) * line / 4;
    context.strokeStyle = '#27303d';
    context.beginPath();
    context.moveTo(pad.left, y);
    context.lineTo(width - pad.right, y);
    context.stroke();
    context.fillStyle = '#8792a2';
    context.fillText(price.toFixed(2), width - pad.right + 7, y + 4);
  }

  const signalByTime = new Map(allSignals.map((signal) => [signal.time, signal]));
  candles.forEach((candle, index) => {
    const x = pad.left + index * candleStep + candleStep / 2;
    const up = candle.close >= candle.open;
    const color = up ? '#ef4b52' : '#35b57d';
    const openY = yForPrice(candle.open);
    const closeY = yForPrice(candle.close);
    context.strokeStyle = color;
    context.fillStyle = color;
    context.beginPath();
    context.moveTo(x, yForPrice(candle.high));
    context.lineTo(x, yForPrice(candle.low));
    context.stroke();
    context.fillRect(x - candleWidth / 2, Math.min(openY, closeY), candleWidth, Math.max(1, Math.abs(closeY - openY)));
    const volumeHeight = candle.volume / maxVolume * 58;
    context.globalAlpha = .48;
    context.fillRect(x - candleWidth / 2, height - pad.bottom - volumeHeight, candleWidth, volumeHeight);
    context.globalAlpha = 1;

    const signal = signalByTime.get(candle.time);
    if (signal) {
      const isBuy = signal.action === 'research_buy';
      const markerY = isBuy ? yForPrice(candle.low) + 13 : yForPrice(candle.high) - 13;
      context.fillStyle = isBuy ? '#e8b84b' : '#35b57d';
      context.beginPath();
      if (isBuy) {
        context.moveTo(x, markerY - 7); context.lineTo(x - 6, markerY + 4); context.lineTo(x + 6, markerY + 4);
      } else {
        context.moveTo(x, markerY + 7); context.lineTo(x - 6, markerY - 4); context.lineTo(x + 6, markerY - 4);
      }
      context.closePath();
      context.fill();
    }
  });

  const firstTime = candles[0].time.slice(5, 16);
  const lastTime = candles[candles.length - 1].time.slice(5, 16);
  context.fillStyle = '#8792a2';
  context.textAlign = 'left';
  context.fillText(firstTime, pad.left, height - 6);
  context.textAlign = 'right';
  context.fillText(lastTime, width - pad.right, height - 6);
}

async function runMonitor() {
  try {
    const result = await api('/api/monitor/run', {method: 'POST'});
    const triggered = result.alerts || [];
    if (!triggered.length) {
      $('#monitorStatus').textContent = dashboard?.positions?.length ? '持仓风控正常 · 每 60 秒检查' : '当前没有持仓';
      return;
    }
    const sent = triggered.filter((item) => item.notification === 'sent').length;
    $('#monitorStatus').textContent = `触发 ${triggered.length} 条风险 · 已推送 ${sent} 条`;
    showToast(`检测到 ${triggered.length} 条持仓风险，请立即核对`, 'error');
  } catch (error) {
    $('#monitorStatus').textContent = `监控异常：${error.message}`;
  }
}

async function refresh(runRiskMonitor = true) {
  void refreshV21Pilot();
  try {
    render(await api('/api/dashboard'));
    void refreshManualCandidates();
    if (runRiskMonitor) await runMonitor();
  } catch (error) {
    $('#connectionDot').className = 'status-dot error';
    $('#connection').textContent = `读取失败：${error.message}`;
    showToast(error.message, 'error');
  }
}

$('#watchlistForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    await api('/api/watchlist', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(Object.fromEntries(form))});
    event.target.reset();
    showToast('已加入重点监视');
    await refresh();
  } catch (error) { showToast(error.message, 'error'); }
});

$('#positionForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    await api('/api/positions', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(Object.fromEntries(form))});
    event.target.reset();
    showToast('持仓已记录，系统不会自动下单');
    await refresh();
  } catch (error) { showToast(error.message, 'error'); }
});

$('#closePositionForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  try {
    const result = await api(`/api/positions/${values.position_id}/close`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)});
    $('#closePositionDialog').close();
    event.target.reset();
    showToast(`卖出记录已保存，实际盈亏 ${result.realized_pnl >= 0 ? '+' : ''}${money(result.realized_pnl)} 元`);
    await refresh();
  } catch (error) { showToast(error.message, 'error'); }
});

$('#wecomTest').addEventListener('click', async () => {
  $('#wecomResult').textContent = '正在发送…';
  try {
    await api('/api/notifications/wecom/test', {method: 'POST'});
    $('#wecomResult').textContent = '测试通知已发送，请检查企业微信群。';
  } catch (error) { $('#wecomResult').textContent = error.message; }
});

$('#feeProfileForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  try {
    await api('/api/settings/fees', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(values),
    });
    showToast('交易费用档案已保存');
    await refresh(false);
  } catch (error) {
    showToast(error.message, 'error');
  }
});

$('#workerStart').addEventListener('click', async () => {
  if (!confirm('启动后将持续下载公开A股数据、执行本地审计、因子挖掘与含成本回测；外部付费 API 保持禁用，也不会下单。是否启动？')) return;
  try {
    const result = await api('/api/research/worker/start', {method: 'POST'});
    renderResearchWorker(result);
    showToast('24小时研究流水线已启动');
    setTimeout(() => refresh(false), 1500);
  } catch (error) {
    showToast(error.message, 'error');
  }
});

$('#workerStop').addEventListener('click', async () => {
  if (!confirm('安全停止后台研究？当前本地步骤可能需要先收尾。')) return;
  try {
    const result = await api('/api/research/worker/stop', {method: 'POST'});
    renderResearchWorker(result);
    showToast('已请求安全停止');
  } catch (error) {
    showToast(error.message, 'error');
  }
});

$('#reviewBtn').addEventListener('click', async () => {
  try {
    const review = await api('/api/review/close');
    const pnl = review.realized_pnl >= 0 ? `+${money(review.realized_pnl)}` : money(review.realized_pnl);
    $('#review').innerHTML = `<div class="detail-list"><div><span>当前持仓</span><strong>${review.open_positions} 只</strong></div><div><span>累计已实现盈亏</span><strong>${pnl} 元</strong></div><div><span>资金暴露</span><strong>${Number(review.portfolio.exposure_pct).toFixed(2)}%</strong></div><div><span>次日关注</span><strong>${review.next_day_focus.join('、') || '暂无'}</strong></div></div><p class="notice">${esc(review.summary)}</p>`;
  } catch (error) { showToast(error.message, 'error'); }
});

document.querySelectorAll('.timeframe-switcher button').forEach((button) => {
  button.addEventListener('click', () => {
    focusInterval = button.dataset.interval;
    document.querySelectorAll('.timeframe-switcher button').forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    if (focusCode) loadFocusChart(focusCode, focusInterval);
  });
});

document.querySelector('a[href="#researchCenter"]')?.addEventListener('click', () => {
  $('#researchCenter').open = true;
});

$('#refreshBtn').addEventListener('click', () => refresh());
$('#closeDetailDialog').addEventListener('click', () => $('#detailDialog').close());
$('#cancelClosePosition').addEventListener('click', () => $('#closePositionDialog').close());

function tickClock() {
  $('#clock').textContent = new Date().toLocaleString('zh-CN', {hour12: false});
}
tickClock();
setInterval(tickClock, 1000);
refresh();
setInterval(refresh, 60000);
setInterval(refreshManualCandidates, 8000);
