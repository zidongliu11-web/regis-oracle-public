(function () {
  'use strict';

  const $ = (selector) => document.querySelector(selector);
  const MONITOR_OWNER = 'positions';
  const requests = new Map();
  const positions = new Map();
  const reportedErrors = new Set();
  let selectedChip = null;
  let sellId = null;
  let sellableId = null;
  let restoreFocus = null;
  let refreshSequence = 0;
  let refreshing = false;
  let refreshTimer = 0;
  let monitorRunning = false;
  let domReady = false;
  let ownsMonitor = false;
  let idempotencyCounter = 0;
  let recordAttempt = null;
  let snapshotAttempt = null;
  let cashflowAttempt = null;
  let cashflowReconcileAttempt = null;
  let navSnapshotAttemptedDate = null;
  let sellAttempt = null;
  let riskLoadedAt = 0;
  let riskLoading = false;
  let latestRiskReport = null;
  let industryExposureLimitPct = null;
  const deleteAttempts = new Map();
  const sellableAttempts = new Map();

  function createIdempotencyKey(scope) {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return `${scope}-${window.crypto.randomUUID()}`;
    const bytes = new Uint32Array(4);
    if (window.crypto && typeof window.crypto.getRandomValues === 'function') window.crypto.getRandomValues(bytes);
    else { bytes[0] = Date.now(); bytes[1] = ++idempotencyCounter; bytes[2] = performance.now() * 1000; bytes[3] = idempotencyCounter; }
    return `${scope}-${Date.now().toString(36)}-${Array.from(bytes, (value) => Number(value).toString(36)).join('-')}`;
  }

  function fp(value) {
    return value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(2);
  }

  function fn(value, digits = 2) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    return Number(value).toLocaleString('zh-CN', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function colorClass(value) {
    return Number(value) > 0 ? 'c-bull' : Number(value) < 0 ? 'c-bear' : 'c-dim';
  }

  function shanghaiDateValue() {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en', {
      timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
    }).formatToParts(new Date()).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  function shanghaiDateTimeLocalValue() {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en', {
      timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
      hour:'2-digit', minute:'2-digit', hourCycle:'h23',
    }).formatToParts(new Date()).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  }

  function executionSlippageText(position) {
    const entryKnown = position.entry_slippage_yuan != null;
    const fillEvents = Number(position.sell_fill_events || 0);
    const attributedEvents = Number(position.sell_slippage_attributed_events || 0);
    const exitKnown = fillEvents > 0 && attributedEvents === fillEvents && position.sell_slippage_yuan != null;
    const attributed = (entryKnown ? Number(position.entry_slippage_yuan) : 0)
      + (position.sell_slippage_yuan != null ? Number(position.sell_slippage_yuan) : 0);
    if (entryKnown && exitKnown) return attributed > 0 ? `执行偏差：较提醒不利 ¥${fn(attributed)}` : attributed < 0 ? `执行偏差：较提醒改善 ¥${fn(-attributed)}` : '执行偏差：与提醒参考价一致';
    if (entryKnown || attributedEvents > 0) return `执行偏差：仅部分可归因 ¥${fn(attributed)}`;
    return '无提醒参考价 · 执行偏差不可得';
  }

  const riskKindLabels = {
    hard_stop: '硬风险线', trailing_stop: '移动风险线',
    trailing_activation: '移动线启用观察位', research_target: '研究观察位',
  };

  function riskLevelText(level) {
    if (!level || typeof level !== 'object') return '风险线元数据未知 · 不作为可执行指令';
    const kind = riskKindLabels[level.kind] || level.kind || '类型未知';
    const value = level.value == null ? '数值未知' : `¥${fp(level.value)}`;
    const source = level.source || '来源未知';
    const priority = level.priority == null ? '优先级未知' : `优先级 ${level.priority}`;
    const execution = level.executable === false ? '不可执行' : level.executable === true ? '后端标记可执行' : '执行属性未知';
    return `${kind} ${value} · 来源 ${source} · ${priority} · ${execution}`;
  }

  function riskLevels(levels) {
    return Array.isArray(levels?.risk_levels) ? levels.risk_levels : [];
  }

  function effectiveRiskLevel(position) {
    const candidates = [position?.risk_action, position?.levels];
    for (const source of candidates) {
      const explicit = source?.effective_risk_level;
      if (explicit?.active === true && Number.isFinite(Number(explicit.value))) return explicit;
      const active = riskLevels(source)
        .filter((level) => level?.active === true && Number.isFinite(Number(level.value)))
        .sort((left, right) => Number(left.priority ?? 999) - Number(right.priority ?? 999))[0];
      if (active) return active;
    }
    return null;
  }

  function appendRiskMetadata(container, levels) {
    const items = riskLevels(levels);
    const box = node('div', 'c-dim');
    box.style.fontSize = '12px';
    box.style.lineHeight = '1.6';
    box.textContent = items.length
      ? items.map(riskLevelText).join('｜')
      : '风险线元数据未知 · 不作为可执行指令';
    container.appendChild(box);
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = String(text);
    return element;
  }

  function emptyState(text) {
    return node('div', 'empty-state', text);
  }

  async function requestJSON(path, options = {}) {
    if (!['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase()) && window.RegisAccess?.canWrite !== true) {
      throw new Error(window.RegisAccess?.reason || '写入权限未核实，未发送请求');
    }
    const requestKey = options.requestKey || `${options.method || 'GET'}:${path}`;
    const previous = requests.get(requestKey);
    if (previous) previous.abort();

    const controller = new AbortController();
    requests.set(requestKey, controller);
    const timeout = setTimeout(() => controller.abort('timeout'), 10000);
    const fetchOptions = { ...options, signal: controller.signal };
    delete fetchOptions.requestKey;
    delete fetchOptions.idempotencyKey;
    if (options.idempotencyKey) {
      fetchOptions.headers = { ...(options.headers || {}), 'Idempotency-Key': options.idempotencyKey };
    }

    try {
      const response = await fetch(path, fetchOptions);
      const text = await response.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (error) {
          throw new Error(`服务器返回了无法解析的数据（HTTP ${response.status}）`, { cause: error });
        }
      }
      if (!response.ok) throw new Error(data.error || data.message || `请求失败（HTTP ${response.status}）`);
      if (requests.get(requestKey) !== controller) {
        throw new DOMException('已被较新的响应取代', 'AbortError');
      }
      return data;
    } catch (error) {
      if (controller.signal.aborted && controller.signal.reason === 'timeout') {
        throw new Error('请求超时，请重试');
      }
      throw error;
    } finally {
      clearTimeout(timeout);
      if (requests.get(requestKey) === controller) requests.delete(requestKey);
    }
  }

  function assertTradeAccepted(data, fallback) {
    const status = String(data.trade_status || data.status || '').toLowerCase();
    const rejected = ['blocked', 'rejected', 'failed', 'error', 'denied'].includes(status);
    if (data.ok === false || data.success === false || rejected) {
      throw new Error(data.error || data.message || fallback);
    }
    return data;
  }

  function toast(message, type = 'info') {
    const container = $('#toastContainer');
    if (!container) return;
    const element = node('div', `toast ${type}`, message);
    if (type === 'error') element.setAttribute('role', 'alert');
    container.appendChild(element);
    setTimeout(() => {
      element.style.opacity = '0';
      element.style.transform = 'translateX(20px)';
      element.style.transition = 'all 300ms ease-in';
    }, 3000);
    setTimeout(() => element.remove(), 3300);
  }

  function reportIssue(key, error, target) {
    if (error && error.name === 'AbortError') return;
    const message = error instanceof Error ? error.message : String(error || '未知错误');
    console.error(`[positions:${key}]`, error);
    if (target) target.replaceChildren(emptyState(`加载失败：${message}`));
    if (!reportedErrors.has(key)) {
      reportedErrors.add(key);
      toast(`${key}：${message}`, 'error');
    }
  }

  function showResult(ok, text) {
    const element = $('#recResult');
    if (!element) return;
    element.textContent = text;
    element.className = `rec-result ${ok ? 'ok' : 'err'}`;
    element.style.display = '';
    if (ok) setTimeout(() => { element.style.display = 'none'; }, 8000);
  }

  function showAccountResult(ok, text) {
    const element = $('#accountResult');
    if (!element) return;
    element.textContent = text;
    element.className = `rec-result ${ok ? 'ok' : 'err'}`;
    element.style.display = '';
    if (ok) setTimeout(() => { element.style.display = 'none'; }, 8000);
  }

  function renderPortfolioSummary(data) {
    if (!$('#pfEquity')) return;
    const summary = (data && data.summary) || {};
    const action = summary.portfolio_action || {};
    const marketRegime = (data && data.market_regime) || {};
    const cashRecorded = summary.cash_user_recorded === true;
    const gaps = Array.isArray(summary.account_fact_gaps) ? summary.account_fact_gaps.filter(gap => typeof gap === 'string' && gap.trim()) : [];
    $('#accountFactGaps').textContent = summary.account_facts_complete === true
      ? '当前账户事实已齐备；不代表已形成可执行计划。'
      : gaps.length ? `当前缺项：${gaps.join('、')}。估值缺失需恢复行情；可卖数量须照抄券商今日记录。`
        : '账户事实缺项待核验；未将其统一解释为缺少现金。';
    const pnl = Number(summary.unrealized_pnl);
    const pnlKnown = summary.unrealized_pnl != null && Number.isFinite(pnl);
    $('#pfEquity').textContent = !cashRecorded || summary.account_equity == null ? '—' : `¥${fn(summary.account_equity)}`;
    $('#pfCash').textContent = !cashRecorded || summary.available_cash == null ? '未录入' : `¥${fn(summary.available_cash)}`;
    $('#pfMarketValue').textContent = summary.market_value == null ? '—' : `¥${fn(summary.market_value)}`;
    $('#pfExposure').textContent = !cashRecorded || summary.exposure_pct == null ? '—' : `${fn(summary.exposure_pct, 1)}%`;
    $('#pfTarget').textContent = summary.market_target_exposure_pct == null ? '—' : `${fn(summary.market_target_exposure_pct, 1)}%`;
    $('#pfPnl').textContent = pnlKnown ? `${pnl >= 0 ? '+' : ''}¥${fn(pnl)}` : '—';
    $('#pfPnl').className = `v ${pnlKnown ? colorClass(pnl) : ''}`;
    $('#portfolioFreshness').textContent = summary.valuation_status === 'complete'
      ? `${cashRecorded ? '账户已录入' : '账户待补全'} · 行情覆盖 ${fn(summary.quote_coverage_pct, 0)}%`
      : `估值不完整 · 缺 ${Number((summary.missing_quote_codes || []).length)} 只`;
    const cashLine = cashRecorded
      ? `现金来自用户录入 · 时点 ${summary.cash_as_of || '未记录'}`
      : '旧账本推导值仅用于历史核对，已从账户权益、现金和仓位主数字中隐藏';
    const openRisk = summary.position_risk_budget || {};
    const openRiskKnown = openRisk.existing_open_risk_yuan != null
      && openRisk.maximum_open_risk_yuan != null;
    $('#pfOpenRisk').textContent = openRiskKnown
      ? `¥${fn(openRisk.existing_open_risk_yuan, 0)} / ¥${fn(openRisk.maximum_open_risk_yuan, 0)}`
      : '待核算';
    $('#pfOpenRiskNote').textContent = openRiskKnown
      ? (openRisk.remaining_open_risk_yuan == null
        ? `已知风险 ¥${fn(openRisk.existing_open_risk_yuan, 0)}，但 ${((openRisk.unknown_codes || []).join('、') || '部分持仓')} 缺少风险线；暂停精确新增股数`
        : `剩余 ¥${fn(openRisk.remaining_open_risk_yuan, 0)} · 上限权益 ${fn(openRisk.maximum_open_risk_pct, 1)}%`)
      : `缺少风险线：${(openRisk.unknown_codes || []).join('、') || '暂无完整账户事实'}`;
    const industryRisk = summary.industry_risk_budget || {};
    industryExposureLimitPct = industryRisk.maximum_industry_exposure_pct;
    $('#pfIndustryRisk').textContent = industryRisk.largest_industry_exposure_pct == null
      ? '待核算' : `${fn(industryRisk.largest_industry_exposure_pct, 1)}% / ${fn(industryRisk.maximum_industry_exposure_pct, 0)}%`;
    $('#pfIndustryRiskNote').textContent = industryRisk.status === 'bounded_with_unknown_reserve'
      ? `已知最大 ${fn(industryRisk.known_largest_industry_exposure_pct, 1)}% + 未分类 ${fn(industryRisk.unknown_industry_exposure_pct, 1)}%；按最坏情形为所有候选预留`
      : '按账户权益计算，不按已投资金额放大比例';
    $('#pfTradeRisk').textContent = summary.new_position_risk_budget_pct == null
      ? '待核算' : `≤ 权益 ${fn(summary.new_position_risk_budget_pct, 1)}%`;
    const roomPct = Number(summary.remaining_exposure_pct);
    const roomYuan = Number(summary.remaining_exposure_yuan);
    const roomKnown = summary.remaining_exposure_pct != null
      && summary.remaining_exposure_yuan != null
      && Number.isFinite(roomPct) && Number.isFinite(roomYuan);
    $('#pfExposureRoom').textContent = roomKnown
      ? `${fn(roomPct, 1)}% · ¥${fn(roomYuan, 0)}` : '待核算';
    $('#pfExposureRoomNote').textContent = roomKnown
      ? '现金与总仓风险预算的较小值；仍须通过市场、板块、战法、个股、风险线、相关性和流动性'
      : '账户现金或实时估值不完整；不推断可买金额';
    const positionPolicy = summary.position_policy || {};
    const ceiling = Number(positionPolicy.live_total_exposure_ceiling_pct);
    const ceilingKnown = positionPolicy.live_total_exposure_ceiling_pct != null
      && Number.isFinite(ceiling);
    const currentExposure = Number(positionPolicy.current_exposure_pct);
    const currentKnown = positionPolicy.current_exposure_pct != null
      && Number.isFinite(currentExposure);
    $('#pfPolicyConclusion').textContent = ceilingKnown
      ? `当前 ${currentKnown ? `${fn(currentExposure, 1)}%` : '仓位待核验'} · 总仓风险上限 ${fn(ceiling, 1)}%`
      : '账户或市场证据不完整 · 不给仓位数字';
    const executableTarget = Number(positionPolicy.executable_target_exposure_pct);
    const executableReady = positionPolicy.executable_target_exposure_pct != null
      && Number.isFinite(executableTarget);
    $('#pfExecutableTarget').textContent = executableReady
      ? `已核验可执行目标：${fn(executableTarget, 1)}%`
      : '已核验可执行目标：— · 无绑定计划时为0股';
    const layers = $('#pfPolicyLayers');
    const caps = Array.isArray(positionPolicy.total_exposure_caps)
      ? positionPolicy.total_exposure_caps : [];
    layers.replaceChildren(...caps.map((cap) => {
      const row = node('div', `portfolio-policy-layer${cap.binding === true ? ' bind' : ''}`);
      row.append(
        node('b', '', cap.label || cap.id || '约束'),
        node('em', '', cap.value_pct == null ? '—' : `${fn(cap.value_pct, 1)}%`),
        node('span', '', `${cap.binding === true ? '当前约束项 · ' : ''}${cap.basis || '依据未记录'}`),
      );
      return row;
    }));
    const candidateConstraints = Array.isArray(positionPolicy.candidate_level_constraints)
      ? positionPolicy.candidate_level_constraints : [];
    $('#pfPolicyFoot').textContent = `${positionPolicy.model_meaning || '当前仓位模型尚未提供说明。'} 候选级约束：${candidateConstraints.map((item) => `${item.label} ${item.limit}`).join('；') || '待核验'}。`;
    $('#portfolioAction').replaceChildren(
      node('strong', '', action.label || '等待组合数据'),
      document.createTextNode(` · ${action.reason || '暂无可核验建议。'}`),
      node('div', 'c-dim', `现行仓位来源 ${marketRegime.execution_policy || '不可用'} · ${marketRegime.execution_policy_applied === true || marketRegime.applied_to_execution === true ? '已应用' : '未应用'}；情绪—板块—战法联合模型未完成样本外验证前不放大真钱仓位。最大单股暴露 ${cashRecorded ? `${fn(summary.max_single_exposure_pct, 1)}%` : '待核算'} / 上限 ${fn(summary.single_position_limit_pct, 1)}% · ${cashLine} · 不自动下单`),
    );
  }

  async function maybeFreezeDailyNav(data) {
    if (window.RegisAccess?.canWrite !== true) return;
    const session = data?.market_session || {};
    const summary = data?.summary || {};
    const day = String(session.server_time || '').slice(0, 10);
    if (session.is_trading_day !== true || session.phase !== 'closed' || !day || summary.valuation_status !== 'complete') return;
    if (navSnapshotAttemptedDate === day) return;
    navSnapshotAttemptedDate = day;
    try {
      await requestJSON('/api/timing/account-nav-snapshots', {
        method:'POST', headers:{'Content-Type':'application/json'},
        requestKey:`freeze-account-nav-${day}`,
      });
    } catch (error) {
      if (error.name !== 'AbortError') console.warn('[positions:daily-nav]', error.message || error);
    }
  }

  function renderRiskReport(data) {
    const varReport = data?.var || {};
    const betaReport = data?.beta || {};
    const drawdown = data?.drawdown || {};
    const industry = data?.industry || {};
    const dependency = data?.dependency || {};
    const stress = data?.historical_stress || {};
    const currentExposurePct = Number(data?.market_regime?.current_exposure_pct);
    const industryPctOfInvested = Number(industry.max_single);
    const industryPctOfEquity = industry.available === true
      && Number.isFinite(currentExposurePct)
      && Number.isFinite(industryPctOfInvested)
      ? industryPctOfInvested * currentExposurePct / 100 : null;
    if (industryPctOfEquity != null) {
      $('#pfIndustryRisk').textContent = `${fn(industryPctOfEquity, 1)}% / ${fn(industryExposureLimitPct, 0)}%`;
      $('#pfIndustryRiskNote').textContent = `已识别 ${Number(industry.industry_count || 0)} 个行业 · 最大行业占已投资市值 ${fn(industryPctOfInvested, 1)}% · 按账户权益换算`;
    }
    const accountVar95 = varReport.var_95_account_pct ?? varReport.var_95_pct;
    $('#pfVar95').textContent = varReport.available === true && accountVar95 != null
      ? `${fn(accountVar95, 2)}%账户 · ¥${fn(varReport.var_95, 0)}` : '不可用';
    $('#pfVar95Note').textContent = varReport.available === true
      ? `持仓内部 ${fn(varReport.var_95_pct, 2)}% · ${Number(varReport.observations || 0)}个共同交易日 · 截至 ${varReport.data_as_of || '时点未记录'}`
      : (varReport.reason || '需完整现价和至少20个共同交易日');
    const accountBeta = betaReport.account_beta ?? betaReport.beta;
    $('#pfBeta').textContent = betaReport.available === true && accountBeta != null
      ? `${fn(accountBeta, 2)} 账户Beta` : '不可用';
    $('#pfBetaNote').textContent = betaReport.available === true
      ? `持仓内部 Beta ${fn(betaReport.beta, 2)} · 账户暴露 ${fn(betaReport.account_exposure_pct, 2)}% · 截至 ${betaReport.data_as_of || '时点未记录'}`
      : (betaReport.exposure || '当前数据不足');
    $('#pfDrawdown').textContent = drawdown.available === true && drawdown.current_drawdown_pct != null
      ? `${fn(drawdown.current_drawdown_pct, 2)}%` : '不可用';
    $('#pfDrawdownNote').textContent = drawdown.available === true
      ? `${drawdown.intraperiod_path_complete === false ? '基准至当前观测最大' : '历史最大'} ${fn(drawdown.max_drawdown_pct, 2)}% · ${Number(drawdown.observations || 0)}个净值点 · 原始账户变化 ${fn(drawdown.account_change_since_baseline_pct, 2)}% · 已记录现金流调整后 ${fn(drawdown.cashflow_adjusted_return_pct, 2)}%`
      : (drawdown.status || '手工账本净值历史不足');
    $('#pfIndustryHhi').textContent = industry.available === true && industry.max_account_exposure_pct != null
      ? `${fn(industry.max_account_exposure_pct, 2)}% 账户` : '不可用';
    $('#pfIndustryHhiNote').textContent = industry.available === true
      ? `持仓内部最大行业 ${fn(industry.max_single, 1)}% · HHI ${fn(industry.hhi, 0)} · ${Number(industry.industry_count || 0)}个行业`
      : (industry.concentration_risk || '行业归属或现价不完整');
    $('#pfEffectiveBets').textContent = dependency.available === true && dependency.effective_risk_bets != null
      ? `${fn(dependency.effective_risk_bets, 2)} / ${Number((dependency.risk_contributions || []).length)}` : '不可用';
    $('#pfEffectiveBetsNote').textContent = dependency.available === true
      ? `持仓内部最大风险贡献 ${fn(dependency.largest_risk_contribution_pct, 1)}% · 折合账户暴露 ${fn(dependency.dominant_account_exposure_pct, 2)}% · 截至 ${dependency.data_as_of || '时点未记录'}`
      : (dependency.reason || '需完整现价和至少60个共同交易日');
    $('#pfHistoricalStress').textContent = stress.available === true && stress.status === 'ready'
      ? `账户1日 ${fn(stress.worst_1d_account_pct, 2)}% · 5日 ${fn(stress.worst_5d_account_pct, 2)}%` : (stress.status === 'not_applicable_no_positions' ? '无持仓' : '不可用');
    $('#pfHistoricalStressNote').textContent = stress.available === true && stress.status === 'ready'
      ? `持仓内部历史ES95 ${fn(stress.expected_shortfall_95_pct, 2)}% · 折合账户 ${fn(stress.expected_shortfall_95_account_pct, 2)}% · 不代表未来上限`
      : (stress.reason || '需完整现价和至少60个共同交易日');
    $('#pfRiskModelBadge').textContent = data?.risk_assessment_status === 'complete'
      ? `${data.risk_level || '已完成'} · ${String(data.calculated_at || '').slice(11, 19)}`
      : (data?.risk_assessment_status === 'not_applicable' ? '当前无持仓' : '指标不完整 · 已安全停算');
  }

  async function loadRiskReport(sequence, force = false) {
    const badge = $('#pfRiskModelBadge');
    if (!badge) return;
    if (riskLoading || (!force && Date.now() - riskLoadedAt < 60000)) return;
    riskLoading = true;
    badge.textContent = '正在计算…';
    try {
      const data = await requestJSON('/api/risk/report', { requestKey: 'risk-report' });
      if (sequence !== refreshSequence) return;
      latestRiskReport = data;
      renderRiskReport(data);
      riskLoadedAt = Date.now();
      reportedErrors.delete('组合风险模型');
    } catch (error) {
      if (sequence === refreshSequence && error.name !== 'AbortError') {
        badge.textContent = `计算失败：${error.message || '未知错误'}`;
      }
    } finally {
      riskLoading = false;
    }
  }

  async function saveCashBalance() {
    const amount = Number.parseFloat($('#cashBalance').value || '');
    if (!Number.isFinite(amount) || amount < 0) return showAccountResult(false, '请填写不小于 0 的真实剩余资金');
    const button = $('#cashSave');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const data = await requestJSON('/api/timing/account', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ remaining_cash_yuan: amount }), requestKey: 'save-account-cash',
      });
      $('#cashBalance').value = '';
      showAccountResult(true, data.message || '剩余资金已保存');
      await refreshAll();
    } catch (error) {
      if (error.name !== 'AbortError') showAccountResult(false, error.message || '保存失败');
    } finally {
      button.disabled = false;
    }
  }

  async function saveAccountCashflow() {
    const flowType = String($('#cashflowType').value || '');
    const amount = Number.parseFloat($('#cashflowAmount').value || '');
    const localAt = String($('#cashflowAt').value || '');
    const cashAfter = Number.parseFloat($('#cashflowCashAfter').value || '');
    const note = String($('#cashflowNote').value || '').trim();
    if (!['deposit', 'withdrawal'].includes(flowType)) return showAccountResult(false, '请选择入金或出金');
    if (!Number.isFinite(amount) || amount <= 0) return showAccountResult(false, '请填写大于 0 的真实资金变动金额');
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(localAt)) return showAccountResult(false, '请填写北京时间资金变动时点');
    if (!Number.isFinite(cashAfter) || cashAfter < 0) return showAccountResult(false, '请填写券商显示的变动后可用资金');
    const payload = {
      flow_type:flowType, amount_yuan:amount,
      effective_at:`${localAt}:00+08:00`,
      cash_balance_after_yuan:cashAfter,
    };
    if (note) payload.note = note;
    const fingerprint = JSON.stringify(payload);
    if (!cashflowAttempt || cashflowAttempt.fingerprint !== fingerprint) {
      cashflowAttempt = {fingerprint, key:createIdempotencyKey('account-cashflow')};
    }
    const button = $('#cashflowSave');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const data = await requestJSON('/api/timing/account-cashflows', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload), idempotencyKey:cashflowAttempt.key,
        requestKey:'save-account-cashflow',
      });
      $('#cashflowAmount').value = '';
      $('#cashflowCashAfter').value = '';
      $('#cashflowNote').value = '';
      cashflowAttempt = null;
      showAccountResult(true, data.message || '资金变动已记录');
      await refreshAll();
    } catch (error) {
      if (error.name !== 'AbortError') showAccountResult(false, error.message || '资金变动记录失败');
    } finally {
      button.disabled = false;
    }
  }

  function renderCashflowCompleteness(completeness) {
    const node = $('#cashflowReconcileStatus');
    if (!node) return;
    const status = completeness || {};
    node.textContent = status.verified_complete === true
      ? `已核对 · 截至 ${status.confirmed_through_date} 的入金与出金无遗漏 · ${Number(status.cashflow_event_count || 0)} 笔`
      : `未完成 · ${status.reason || '需要核对券商资金流水'}`;
    node.className = `cashflow-reconcile-status ${status.verified_complete === true ? 'c-bull' : 'c-gold'}`;
    if ($('#cashflowReconcileDate') && status.requested_through_date) {
      $('#cashflowReconcileDate').value = status.requested_through_date;
    }
  }

  async function loadCashflowCompleteness(sequence) {
    try {
      const data = await requestJSON('/api/timing/account-cashflows', {
        requestKey:'account-cashflow-completeness',
      });
      if (sequence !== refreshSequence) return;
      renderCashflowCompleteness(data.cashflow_completeness);
      reportedErrors.delete('资金完整性');
    } catch (error) {
      if (sequence === refreshSequence) reportIssue('资金完整性', error, $('#cashflowReconcileStatus'));
    }
  }

  async function confirmCashflowCompleteness() {
    const throughDate = String($('#cashflowReconcileDate')?.value || '');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(throughDate)) return showAccountResult(false, '请选择要核对的截止日期');
    const payload = {through_date:throughDate};
    const fingerprint = JSON.stringify(payload);
    if (!cashflowReconcileAttempt || cashflowReconcileAttempt.fingerprint !== fingerprint) {
      cashflowReconcileAttempt = {fingerprint, key:createIdempotencyKey('cashflow-reconcile')};
    }
    const button = $('#cashflowReconcileSave');
    if (!button || button.disabled) return;
    button.disabled = true;
    try {
      const data = await requestJSON('/api/timing/account-cashflows/reconcile', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload), idempotencyKey:cashflowReconcileAttempt.key,
        requestKey:'confirm-account-cashflow-completeness',
      });
      cashflowReconcileAttempt = null;
      renderCashflowCompleteness(data.cashflow_completeness);
      showAccountResult(true, data.message || '资金完整性已确认');
      await refreshAll();
    } catch (error) {
      if (error.name !== 'AbortError') showAccountResult(false, error.message || '资金完整性确认失败');
    } finally {
      button.disabled = false;
    }
  }

  async function submitPositionSnapshot() {
    const codeOrName = ($('#snapCode').value || '').trim();
    const shares = Number.parseInt($('#snapShares').value || '0', 10);
    const averageCost = Number.parseFloat($('#snapCost').value || '');
    const remainingCash = Number.parseFloat($('#snapCash').value || '');
    const feeRaw = ($('#snapFee').value || '').trim();
    const buyFee = feeRaw === '' ? null : Number.parseFloat(feeRaw);
    if (!codeOrName) return showAccountResult(false, '请填写持仓名称或代码');
    if (!Number.isInteger(shares) || shares <= 0) return showAccountResult(false, '请填写真实持仓股数；已有持仓允许包含零股');
    if (!Number.isFinite(averageCost) || averageCost <= 0) return showAccountResult(false, '请填写有效的持仓成本价');
    if (!Number.isFinite(remainingCash) || remainingCash < 0) return showAccountResult(false, '请填写录入后的真实剩余资金');
    if (buyFee !== null && (!Number.isFinite(buyFee) || buyFee < 0)) return showAccountResult(false, '买入费用需为不小于 0 的金额');
    const payload = { shares, average_cost: averageCost, remaining_cash_yuan: remainingCash };
    if (/^(0|3|6)\d{5}$/.test(codeOrName)) payload.code = codeOrName;
    else payload.name = codeOrName;
    if (buyFee !== null) payload.buy_fee_yuan = buyFee;
    const fingerprint = JSON.stringify(payload);
    if (!snapshotAttempt || snapshotAttempt.fingerprint !== fingerprint) {
      snapshotAttempt = { fingerprint, key: createIdempotencyKey('position-snapshot') };
    }
    const button = $('#snapshotSave');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const data = await requestJSON('/api/timing/position-snapshot', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), requestKey: 'position-snapshot',
        idempotencyKey: snapshotAttempt.key,
      });
      snapshotAttempt = null;
      showAccountResult(true, data.message || '持仓已录入实时监控');
      ['#snapCode', '#snapShares', '#snapCost', '#snapFee', '#snapCash'].forEach((selector) => { $(selector).value = ''; });
      await refreshAll();
    } catch (error) {
      if (error.name !== 'AbortError') showAccountResult(false, error.message || '持仓录入失败');
    } finally {
      button.disabled = false;
    }
  }

  function renderChips(data, sequence) {
    const container = $('#recChips');
    if (!container) return;
    if (sequence !== refreshSequence) return;
    const label = $('#recChipsLabel');
    const assessed = Number(data && data.assessed_count) || 0;
    if (label) label.textContent = `成交记录入口 · 机会池已评估 ${assessed} 只，但账户页不授予买入权限`;
    const link = node('a', 'rec-decision-link', '返回今日决策核对同一观察时点计划、股数与有效期');
    link.href = '/?workspace=intraday';
    container.replaceChildren(link);
    reportedErrors.delete('机会列表');
  }

  async function loadOpportunitySnapshot(sequence) {
    const container = $('#recChips');
    try {
      const data = await requestJSON('/api/opportunities', { requestKey: 'opportunities' });
      if (sequence !== refreshSequence) return null;
      renderChips(data, sequence);
      window.dispatchEvent(new CustomEvent('regis-opportunity-snapshot', { detail: data }));
      reportedErrors.delete('卖出提醒');
      return data;
    } catch (error) {
      if (sequence === refreshSequence) reportIssue('机会列表', error, container);
      return null;
    }
  }

  function selectChip(chip) {
    if (selectedChip) {
      selectedChip.classList.remove('sel');
      selectedChip.setAttribute('aria-pressed', 'false');
    }
    selectedChip = chip;
    chip.classList.add('sel');
    chip.setAttribute('aria-pressed', 'true');
    $('#recCode').value = chip.dataset.code || '';
    $('#recName').value = chip.dataset.name || '';
    $('#recShares').focus();
  }

  async function submitRecord() {
    const codeOrName = ($('#recCode').value || '').trim();
    const recordedName = ($('#recName').value || '').trim();
    const shares = Number($('#recShares').value);
    const price = Number($('#recPrice').value);
    const feeRaw = ($('#recBuyFee').value || '').trim();
    const buyFee = feeRaw === '' ? null : Number.parseFloat(feeRaw);
    if (!/^(0|3|6)\d{5}$/.test(codeOrName)) return showResult(false, '请填写成交明细中的沪深6位股票代码；名称不能代替证券身份');
    if (!Number.isInteger(shares) || shares <= 0) return showResult(false, '请填写实际已成交的正整数股数；不是计划委托量');
    if (!Number.isFinite(price) || price <= 0) return showResult(false, '请填写券商实际成交价；现价不能代替成交记录');
    if (buyFee !== null && (!Number.isFinite(buyFee) || buyFee < 0)) return showResult(false, '实际买入费用需为不小于 0 的金额');

    const payload = { shares, code:codeOrName };
    if (recordedName) payload.name = recordedName;
    payload.price = price;
    if (buyFee !== null) payload.buy_fee_yuan = buyFee;

    const fingerprint = JSON.stringify(payload);
    if (!recordAttempt || recordAttempt.fingerprint !== fingerprint) {
      recordAttempt = { fingerprint, key:createIdempotencyKey('record-position') };
    }

    const saveButton = $('#recSave');
    if (saveButton.disabled) return;
    saveButton.disabled = true;
    saveButton.setAttribute('aria-busy', 'true');
    try {
      const data = assertTradeAccepted(await requestJSON('/api/timing/record', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        requestKey: 'record-position',
        idempotencyKey: recordAttempt.key,
      }), '记录失败');
      recordAttempt = null;
      const levels = data.levels || {};
      let message = data.message || '服务器已确认记录';
      if (riskLevels(levels).length) message += `｜${riskLevels(levels).map(riskLevelText).join('｜')}`;
      if (data.warning && data.warning.reason) message += `｜统一评估未过：${data.warning.reason}`;
      showResult(true, message);
      $('#recCode').value = '';
      $('#recName').value = '';
      $('#recShares').value = '';
      $('#recPrice').value = '';
      $('#recBuyFee').value = '';
      if (selectedChip) {
        selectedChip.classList.remove('sel');
        selectedChip.setAttribute('aria-pressed', 'false');
        selectedChip = null;
      }
      await refreshAll();
    } catch (error) {
      if (error.name !== 'AbortError') showResult(false, error.message || '记录失败，请重试');
    } finally {
      saveButton.disabled = false;
      saveButton.removeAttribute('aria-busy');
    }
  }

  async function deletePosition(id) {
    const position = positions.get(String(id));
    if (!position || !window.confirm(`确定删除「${position.name || position.code || '这条'}」持仓记录？`)) return;
    const fingerprint = String(id);
    let attempt = deleteAttempts.get(fingerprint);
    if (!attempt) {
      attempt = { key:createIdempotencyKey('delete-position') };
      deleteAttempts.set(fingerprint, attempt);
    }
    try {
      const data = await requestJSON(`/api/timing/positions/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        requestKey: `delete-position-${id}`,
        idempotencyKey: attempt.key,
      });
      assertTradeAccepted(data, '删除失败');
      deleteAttempts.delete(fingerprint);
      toast(data.message || '服务器已确认删除', 'success');
      await refreshAll();
    } catch (error) {
      if (error.name !== 'AbortError') toast(error.message || '删除失败，请重试', 'error');
    }
  }

  function updateSellableInventory() {
    const position = positions.get(String(sellableId));
    const input = $('#sellableShares');
    const hint = $('#sellableHint');
    const confirmButton = $('#sellableConfirm');
    const held = Number(position?.shares);
    const raw = String(input.value || '').trim();
    const sellable = Number(raw);
    const valid = raw !== '' && Number.isInteger(held) && held >= 0
      && Number.isInteger(sellable) && sellable >= 0 && sellable <= held;
    input.setAttribute('aria-invalid', valid ? 'false' : 'true');
    hint.classList.toggle('invalid', !valid);
    hint.textContent = valid
      ? `将记录 ${shanghaiDateValue()} 券商可卖 ${sellable} 股；只用于精确卖出复核，不自动卖出。`
      : `请输入 0 至 ${Number.isFinite(held) ? held : '当前持仓'} 之间的整数。`;
    confirmButton.disabled = !valid;
    return {sellable, held, valid};
  }

  function openSellableInventory(id, opener) {
    const position = positions.get(String(id));
    if (!position) return toast('这条持仓已刷新，请重新选择', 'error');
    sellableId = String(id);
    restoreFocus = opener || document.activeElement;
    const today = shanghaiDateValue();
    const priorDay = String(position.sellable_as_of || '').slice(0, 10);
    const prior = priorDay === today && Number.isInteger(Number(position.sellable_shares_snapshot))
      ? String(Number(position.sellable_shares_snapshot)) : '';
    $('#sellableSummary').textContent = `${position.name || position.code} · 当前持仓 ${Number(position.shares) || 0} 股 · ${today}`;
    const input = $('#sellableShares');
    input.max = String(Number(position.shares) || 0);
    input.value = prior;
    const mask = $('#sellableMask');
    mask.style.display = 'flex';
    mask.setAttribute('aria-hidden', 'false');
    updateSellableInventory();
    input.focus();
  }

  function closeSellableInventory() {
    const mask = $('#sellableMask');
    mask.style.display = 'none';
    mask.setAttribute('aria-hidden', 'true');
    sellableId = null;
    const target = restoreFocus;
    restoreFocus = null;
    if (target && target.isConnected) target.focus();
  }

  async function confirmSellableInventory() {
    if (!sellableId) return;
    const position = positions.get(String(sellableId));
    if (!position) return toast('这条持仓已刷新，请关闭后重新选择', 'error');
    const quantity = updateSellableInventory();
    if (!quantity.valid) return;
    const {sellable} = quantity;
    const today = shanghaiDateValue();
    const payload = {sellable_shares_snapshot:sellable, sellable_as_of:today};
    const fingerprint = JSON.stringify(payload);
    let attempt = sellableAttempts.get(String(sellableId));
    if (!attempt || attempt.fingerprint !== fingerprint) {
      attempt = {fingerprint, key:createIdempotencyKey('sellable-inventory')};
      sellableAttempts.set(String(sellableId), attempt);
    }
    const confirmButton = $('#sellableConfirm');
    confirmButton.disabled = true;
    confirmButton.setAttribute('aria-busy', 'true');
    try {
      const data = await requestJSON(`/api/timing/positions/${Number(sellableId)}/sellable-inventory`, {
        method:'PUT', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload), requestKey:`sellable-inventory-${sellableId}`,
        idempotencyKey:attempt.key,
      });
      sellableAttempts.delete(String(sellableId));
      toast(data.message || `已核验今日可卖 ${sellable} 股`, 'success');
      closeSellableInventory();
      await refreshAll();
    } catch (error) {
      if (error.name !== 'AbortError') toast(error.message || '可卖库存保存失败', 'error');
    } finally {
      confirmButton.removeAttribute('aria-busy');
      if ($('#sellableMask').getAttribute('aria-hidden') === 'false') {
        confirmButton.disabled = false;
      }
    }
  }

  function appendSummaryRow(container, label, value, className) {
    const row = node('div', 'ss-row');
    row.append(node('span', '', label), node('b', className || '', value));
    container.appendChild(row);
  }

  function sellQuantityState(position, rawValue) {
    const held = Number(position && position.shares);
    const shares = Number(rawValue);
    const oddLot = Number.isInteger(held) ? held % 100 : 0;
    const valid = Number.isInteger(held) && held >= 1
      && Number.isInteger(shares) && shares >= 1 && shares <= held
      && (shares % 100 === 0 || (oddLot > 0 && shares % 100 === oddLot));
    return { held, shares, remaining: valid ? held - shares : null, valid };
  }

  function exactRiskTarget(position) {
    const held = Number(position && position.shares);
    const planTarget = position && position.trade_plan && position.trade_plan.action
      && position.trade_plan.action.target_shares;
    const intent = position && position.trade_plan && position.trade_plan.manual_exit_intent;
    const intentTarget = intent && intent.configured === true ? intent.target_shares : null;
    const rawTarget = planTarget ?? intentTarget
      ?? (position && position.risk_action && position.risk_action.target_shares);
    if (rawTarget == null || rawTarget === '') return null;
    const target = Number(rawTarget);
    return Number.isInteger(held) && Number.isInteger(target)
      && target >= 0 && target <= held && target % 100 === 0 ? target : null;
  }

  function updateSellQuantity() {
    const position = positions.get(String(sellId));
    const input = $('#sellShares');
    const remaining = $('#sellRemaining');
    const confirmButton = $('#sellConfirm');
    const state = sellQuantityState(position, input.value);
    input.setAttribute('aria-invalid', state.valid ? 'false' : 'true');
    remaining.classList.toggle('invalid', !state.valid);
    if (state.valid) {
      remaining.textContent = state.remaining === 0
        ? '卖出后剩余 0 股 · 按实际成交记录，不代表系统建议'
        : `卖出后剩余 ${state.remaining} 股`;
      confirmButton.textContent = state.remaining === 0
        ? '确认卖出全部'
        : `确认卖出 ${state.shares} 股`;
    } else {
      remaining.textContent = Number.isFinite(state.held)
        ? `请输入 100 的整数倍；如含零股须一次卖完零股，且不超过当前 ${state.held} 股`
        : '当前持仓数量不可用，请刷新后重试';
      confirmButton.textContent = '检查卖出数量';
    }
    return state;
  }

  function openSell(id, opener) {
    const position = positions.get(String(id));
    if (!position) return toast('这条持仓已刷新，请重新选择', 'error');
    sellId = String(id);
    sellAttempt = null;
    restoreFocus = opener || document.activeElement;
    const levels = position.levels || {};
    const current = Number(position.current);
    const cost = Number(position.price);
    const summary = $('#sellSummary');
    summary.replaceChildren();
    appendSummaryRow(summary, `${position.name || ''} · ${position.code || ''}`, `${position.shares || 0} 股`);
    appendSummaryRow(summary, '成本价', `¥${fp(cost)}`);
    appendSummaryRow(summary, '当前价', `¥${fp(position.current)}`, Number.isFinite(current) && current >= cost ? 'c-bull' : 'c-bear');
    const metadata = riskLevels(levels);
    if (metadata.length) metadata.forEach((level) => appendSummaryRow(summary, '分层风险线', riskLevelText(level)));
    else appendSummaryRow(summary, '分层风险线', '元数据未知 · 不作为可执行指令');
    const riskAction = position.risk_action || {};
    const targetShares = exactRiskTarget(position);
    if (riskAction.action) appendSummaryRow(summary, '风险复核', `${riskAction.action}：${riskAction.reason || '未提供原因'}`);
    if (targetShares != null) appendSummaryRow(summary, '精确目标持仓', `${targetShares} 股`);
    else if (['离场复核', '减仓复核'].includes(riskAction.action)) {
      appendSummaryRow(summary, '复核数量', '未给出精确目标股数，不提供数量建议');
    }

    const priceInput = $('#sellPrice');
    priceInput.value = '';
    const sharesInput = $('#sellShares');
    sharesInput.value = '';
    sharesInput.max = position.shares || '';
    $('#sellFee').value = '';
    updateSellQuantity();
    const mask = $('#sellMask');
    mask.style.display = 'flex';
    mask.setAttribute('aria-hidden', 'false');
    priceInput.focus();
  }

  function closeSell() {
    const mask = $('#sellMask');
    mask.style.display = 'none';
    mask.setAttribute('aria-hidden', 'true');
    sellId = null;
    sellAttempt = null;
    const target = restoreFocus;
    restoreFocus = null;
    if (target && target.isConnected) target.focus();
  }

  async function confirmSell() {
    if (!sellId) return;
    const price = Number.parseFloat($('#sellPrice').value || '0');
    if (!Number.isFinite(price) || price <= 0) return toast('请填写有效的卖出价格', 'error');
    const quantity = updateSellQuantity();
    if (!quantity.valid) return toast('卖出数量须为 100 股整数倍，或一次性包含全部零股，且不能超过当前持仓', 'error');
    const feeRaw = ($('#sellFee').value || '').trim();
    const sellFee = feeRaw === '' ? null : Number.parseFloat(feeRaw);
    if (sellFee !== null && (!Number.isFinite(sellFee) || sellFee < 0)) return toast('实际卖出费用需为不小于 0 的金额', 'error');
    const confirmButton = $('#sellConfirm');
    const payload = { id: sellId, price, shares: quantity.shares };
    if (sellFee !== null) payload.sell_fee_yuan = sellFee;
    const fingerprint = JSON.stringify(payload);
    if (!sellAttempt || sellAttempt.fingerprint !== fingerprint) {
      sellAttempt = { fingerprint, key:createIdempotencyKey('sell-position') };
    }
    if (confirmButton.disabled) return;
    confirmButton.disabled = true;
    confirmButton.setAttribute('aria-busy', 'true');
    try {
      const data = assertTradeAccepted(await requestJSON('/api/timing/sell', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        requestKey: 'sell-position',
        idempotencyKey: sellAttempt.key,
      }), '卖出记录未被服务器接受');
      sellAttempt = null;
      toast(data.message || '服务器已确认卖出记录', 'success');
      closeSell();
      await refreshAll();
    } catch (error) {
      if (error.name !== 'AbortError') toast(error.message || '卖出失败，请重试', 'error');
    } finally {
      confirmButton.disabled = false;
      confirmButton.removeAttribute('aria-busy');
    }
  }

  function appendBand(row, position, current) {
    const levels = position?.levels || {};
    const activeRisk = effectiveRiskLevel(position);
    const stopLoss = Number(activeRisk?.value ?? levels.stop_loss);
    const riskLabel = activeRisk ? '当前有效风险线' : '固定-8%参考线';
    const activation = Number(levels && (levels.trailing_activation ?? levels.take_profit_1));
    const researchLevel = riskLevels(levels).find((level) => level.kind === 'research_target');
    const structure = Number(researchLevel?.value ?? (levels && levels.take_profit_2));
    if (![stopLoss, activation, structure].every(Number.isFinite) || structure <= stopLoss) return;

    const percent = (value) => Math.max(0, Math.min(100, (value - stopLoss) / (structure - stopLoss) * 100));
    const activationPercent = percent(activation);
    const currentPercent = percent(Number.isFinite(Number(current)) ? Number(current) : stopLoss);
    const band = node('div', 'sl-band');
    const track = node('div', 'sl-track');
    track.setAttribute('role', 'img');
    track.setAttribute('aria-label', `当前价 ${fp(current)}；${riskLabel} ${fp(stopLoss)}；移动风险线启用观察位 ${fp(activation)}；结构观察位 ${fp(structure)}`);
    const risk = node('div', 'sl-zone sl-risk');
    risk.style.left = '0';
    risk.style.width = `${activationPercent}%`;
    const observe = node('div', 'sl-zone sl-observe');
    observe.style.left = `${activationPercent}%`;
    observe.style.width = `${100 - activationPercent}%`;
    const tick = node('div', 'sl-tick');
    tick.style.left = `${activationPercent}%`;
    const marker = node('div', 'sl-marker');
    marker.style.left = `${currentPercent}%`;
    track.append(risk, observe, tick, marker);
    const labels = node('div', 'sl-labels');
    labels.append(
      node('span', 'risk', `${riskLabel} ${fp(stopLoss)}`),
      node('span', 'c-dim', `移动线启用观察 ${fp(activation)}`),
      node('span', 'observe', `结构观察 ${fp(structure)}`),
    );
    band.append(track, labels);
    row.appendChild(band);
  }

  function appendSuggestion(row, position) {
    const levels = position.levels || {};
    const currentValue = position.current;
    const current = Number(currentValue);
    const activeRisk = effectiveRiskLevel(position);
    const stopLoss = Number(activeRisk?.value ?? levels.stop_loss);
    const riskLabel = activeRisk ? '当前有效风险线' : '固定-8%参考线';
    const activation = Number(levels && (levels.trailing_activation ?? levels.take_profit_1));
    const researchLevel = riskLevels(levels).find((level) => level.kind === 'research_target');
    const structure = Number(researchLevel?.value ?? (levels && levels.take_profit_2));
    const riskAction = position.risk_action || {};
    const targetShares = exactRiskTarget(position);

    let className = 'sl-suggest sug-hold';
    let message;
    if (['离场复核', '减仓复核'].includes(riskAction.action) && targetShares != null) {
      className = 'sl-suggest sug-sell';
      message = `${riskAction.action}：${riskAction.reason || '已触及风险或结构复核条件'}`;
      message += ` · 精确目标剩余 ${targetShares} 股`;
    } else if (Array.isArray(riskAction.quant_signals) && riskAction.quant_signals.length) {
      className = 'sl-suggest sug-observe';
      message = `${riskAction.reason || '技术指标发生变化'} · 仅作观察，不生成卖出数量建议`;
    } else if (riskAction.action === '市场风险观察') {
      className = 'sl-suggest sug-observe';
      message = `${riskAction.reason || '市场风险状态变化'} · 未形成精确减仓计划`;
    } else if (![current, stopLoss, activation, structure].every(Number.isFinite)) {
      message = riskAction.reason || '风险线或行情数据不完整，暂不给出卖出时机判断';
    } else if (current >= structure) {
      className = 'sl-suggest sug-observe';
      message = `已到结构观察位 ¥${fp(structure)}，观察趋势与量价是否延续；这不是卖出订单`;
    } else if (current >= activation) {
      className = 'sl-suggest sug-observe';
      message = `已到移动风险线启用观察位 ¥${fp(activation)}，后续按 ATR 移动风险线复核；这不是卖出指令`;
    } else if (current <= stopLoss) {
      className = 'sl-suggest sug-sell';
      message = `已触及${riskLabel} ¥${fp(stopLoss)}，请人工复核风险处置`;
    } else {
      const toActivation = current > 0 ? (activation - current) / current * 100 : null;
      message = `未触发风险线 · 移动线启用观察 ¥${fp(activation)}${Number.isFinite(toActivation) ? `（${toActivation >= 0 ? '+' : ''}${toActivation.toFixed(1)}%）` : ''} · ${riskLabel} ¥${fp(stopLoss)}`;
    }
    row.appendChild(node('div', className, message));
  }

  function renderPosition(position, triggers) {
    const levels = position.levels || {};
    const serverAction = position.trade_plan && position.trade_plan.action;
    const triggered = Boolean(
      (serverAction && serverAction.side === 'SELL' && Number(serverAction.shares) > 0)
      || (triggers && triggers[position.code])
    );
    const row = node('div', `pos-row${triggered ? ' sell-alert' : ''}`);
    const top = node('div', 'pos-top');
    const link = node('a', 'pos-name', position.name || position.code || '未命名');
    link.href = `/stock?code=${encodeURIComponent(position.code || '')}`;
    top.append(link, node('span', 'pos-code', position.code || ''));
    if (triggered) top.appendChild(node('span', 'sell-badge', '已触发卖出提醒'));
    top.appendChild(node('span', 'pos-shares', `${position.shares || 0} 股 @ ¥${fp(position.price)}`));

    const sellButton = node('button', 'pos-sell', '卖出');
    sellButton.type = 'button';
    sellButton.dataset.action = 'sell';
    sellButton.dataset.id = String(position.id);
    const sellableDay = String(position.sellable_as_of || '').slice(0, 10);
    const sellableCurrent = sellableDay === shanghaiDateValue()
      && Number.isInteger(Number(position.sellable_shares_snapshot));
    const sellableButton = node(
      'button', 'pos-sellable',
      sellableCurrent ? `今日可卖 ${Number(position.sellable_shares_snapshot)}` : '核验今日可卖',
    );
    sellableButton.type = 'button';
    sellableButton.title = '照抄券商今天显示的可卖数量，用于 T+1 精确卖出复核';
    sellableButton.dataset.action = 'sellable';
    sellableButton.dataset.id = String(position.id);
    const deleteButton = node('button', 'pos-del', '删除');
    deleteButton.type = 'button';
    deleteButton.title = '删除这条记录';
    deleteButton.dataset.action = 'delete';
    deleteButton.dataset.id = String(position.id);
    top.append(sellableButton, sellButton, deleteButton);

    const price = node('div', 'pos-px');
    price.append(
      node('span', `pos-cur ${colorClass(position.pnl_pct)}`, `¥${fp(position.current)}`),
      node('span', `pos-chg ${colorClass(position.pnl_pct)}`, `${Number(position.pnl_pct) >= 0 ? '+' : ''}${fp(position.pnl_pct)}%`),
      node('span', 'pos-pnl', `浮盈 ${Number(position.pnl) >= 0 ? '+' : ''}¥${fn(position.pnl)}`),
    );
    const quoteAsOf = position.quote_as_of || (((position.trade_plan || {}).action || {}).as_of);
    const quoteClock = quoteAsOf
      ? String(quoteAsOf).replace('T', ' ').slice(0, 19)
      : '行情时点不可用';
    price.appendChild(node(
      'span', 'c-dim',
      `${quoteClock}${position.quote_age_seconds == null ? '' : ` · ${fn(position.quote_age_seconds, 0)}秒前`}`,
    ));
    row.append(top, price);
    appendBand(row, position, position.current);
    appendRiskMetadata(row, position.risk_action && position.risk_action.risk_levels?.length ? position.risk_action : levels);
    appendSuggestion(row, position);
    return row;
  }

  async function loadPositions(triggers, sequence) {
    const body = $('#positionsBody');
    try {
      const data = await requestJSON('/api/timing/portfolio', { requestKey: 'portfolio' });
      if (sequence !== refreshSequence) return;
      window.dispatchEvent(new CustomEvent('regis-portfolio-snapshot', { detail: data }));
      renderPortfolioSummary(data);
      void maybeFreezeDailyNav(data);
      if (latestRiskReport) renderRiskReport(latestRiskReport);
      const list = Array.isArray(data.open) ? data.open : [];
      positions.clear();
      list.forEach((position) => positions.set(String(position.id), position));
      $('#posCount').textContent = list.length ? `${list.length} 只持仓` : '—';
      if (!list.length) body.replaceChildren(emptyState('还没有持仓 · 在上方录入现有持仓，或记录已发生的买入成交'));
      else body.replaceChildren(...list.map((position) => renderPosition(position, triggers)));
      reportedErrors.delete('持仓');
    } catch (error) {
      if (sequence === refreshSequence) reportIssue('持仓', error, body);
    }
  }

  function opportunityTriggers(data) {
    const triggered = Object.create(null);
    (Array.isArray(data && data.items) ? data.items : []).forEach((item) => {
      const alert = item.alert || {};
      if (alert.status === 'triggered' && alert.action === 'sell' && item.code) triggered[item.code] = true;
    });
    return triggered;
  }

  async function loadClosed(sequence) {
    const body = $('#closedBody');
    try {
      const data = await requestJSON('/api/timing/history', { requestKey: 'closed-positions' });
      if (sequence !== refreshSequence) return;
      const realized = $('#realizedPnl');
      const performance = data.execution_performance || {};
      const cumulative = performance.net_pnl_yuan != null ? Number(performance.net_pnl_yuan) : null;
      if (cumulative != null && Number.isFinite(cumulative)) {
        realized.textContent = `累计净收益 ${cumulative >= 0 ? '+' : ''}¥${fn(cumulative)}`;
        realized.className = `card-badge num ${colorClass(cumulative)}`;
        realized.style.background = 'transparent';
      } else if (performance.cost_breakdown_status === 'legacy_missing') {
        const missing = Number(performance.cost_breakdown_missing_positions || 0);
        realized.textContent = `成本后绩效不可得${missing ? ` · ${missing} 笔旧记录缺费用` : ''}`;
        realized.className = 'card-badge c-dim';
        realized.style.background = 'transparent';
      } else {
        realized.textContent = '—';
      }
      const closed = Array.isArray(data.positions_closed) ? data.positions_closed : [];
      if (!closed.length) {
        body.replaceChildren(emptyState('还没有卖出记录 · 持仓卖出后会记在这里'));
      } else {
        body.replaceChildren(...closed.map((position) => {
          const row = node('div', 'his-row');
          row.append(
            node('span', 'his-name', position.name || position.code || '未命名'),
            node('span', '', `${position.initial_shares || position.shares || 0} 股 · 买 ${fp(position.price)} → 卖 ${fp(position.sold_price)}`),
          );
          const pnl = node('span', `num ${colorClass(position.pnl)}`, `${Number(position.pnl) >= 0 ? '+' : ''}¥${fn(position.pnl)}`);
          pnl.style.marginLeft = 'auto';
          const semantics = position.buy_cost == null || position.sell_cost == null
            ? '记录盈亏 · 费用缺失'
            : position.buy_cost_source === 'actual_user_recorded' && position.sell_cost_source === 'actual_user_recorded'
              ? '券商实际费用后净收益' : '估算费用后净收益';
          row.append(pnl, node('span', 'c-dim', `${semantics} · ${executionSlippageText(position)} · ${String(position.sold_at || '').slice(0, 16)}`));
          return row;
        }));
      }
      reportedErrors.delete('平仓历史');
    } catch (error) {
      if (sequence === refreshSequence) reportIssue('平仓历史', error, body);
    }
  }

  async function refreshAll() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    const sequence = ++refreshSequence;
    try {
      await Promise.all([
        loadOpportunitySnapshot(sequence),
        loadPositions({}, sequence),
        loadClosed(sequence),
        loadRiskReport(sequence),
        loadCashflowCompleteness(sequence),
      ]);
    } finally {
      if (sequence === refreshSequence) refreshing = false;
    }
  }

  function setMonitorOwnership(active) {
    if (active) {
      window.__regisSpaPortfolioMonitorOwner = MONITOR_OWNER;
      if (ownsMonitor) return;
      ownsMonitor = true;
      window.dispatchEvent(new CustomEvent('regis-portfolio-monitor-active', {
        detail: { active: true, owner: MONITOR_OWNER },
      }));
      return;
    }
    ownsMonitor = false;
    if (window.__regisSpaPortfolioMonitorOwner !== MONITOR_OWNER) return;
    window.dispatchEvent(new CustomEvent('regis-portfolio-monitor-active', {
      detail: { active: false, owner: MONITOR_OWNER },
    }));
    if (window.__regisSpaPortfolioMonitorOwner === MONITOR_OWNER) {
      window.__regisSpaPortfolioMonitorOwner = null;
    }
  }

  function stopMonitor() {
    monitorRunning = false;
    refreshSequence += 1;
    refreshing = false;
    if (refreshTimer) window.clearInterval(refreshTimer);
    refreshTimer = 0;
    requests.forEach((controller) => controller.abort('positions-monitor-stopped'));
    requests.clear();
    setMonitorOwnership(false);
  }

  function startMonitor() {
    if (!domReady || document.hidden || monitorRunning) return;
    monitorRunning = true;
    setMonitorOwnership(true);
    refreshAll();
    refreshTimer = window.setInterval(refreshAll, 10000);
  }

  function handleVisibilityChange() {
    if (document.hidden) stopMonitor();
    else if (domReady) startMonitor();
    else setMonitorOwnership(true);
  }

  function handleDialogKeydown(event) {
    const sellableOpen = $('#sellableMask').getAttribute('aria-hidden') === 'false';
    const mask = sellableOpen ? $('#sellableMask') : $('#sellMask');
    if (mask.getAttribute('aria-hidden') !== 'false') return;
    if (event.key === 'Escape') {
      event.preventDefault();
      if (sellableOpen) closeSellableInventory();
      else closeSell();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = [...mask.querySelectorAll('button:not([disabled]), input:not([disabled])')];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function bindEvents() {
    $('#recChips').addEventListener('click', (event) => {
      const chip = event.target.closest('.rec-chip');
      if (chip) selectChip(chip);
    });
    $('#positionsBody').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action][data-id]');
      if (!button) return;
      if (button.dataset.action === 'sellable') openSellableInventory(button.dataset.id, button);
      if (button.dataset.action === 'sell') openSell(button.dataset.id, button);
      if (button.dataset.action === 'delete') deletePosition(button.dataset.id);
    });
    $('#recSave').addEventListener('click', submitRecord);
    if ($('#cashSave')) $('#cashSave').addEventListener('click', saveCashBalance);
    if ($('#snapshotSave')) $('#snapshotSave').addEventListener('click', submitPositionSnapshot);
    if ($('#cashflowSave')) $('#cashflowSave').addEventListener('click', saveAccountCashflow);
    if ($('#cashflowReconcileSave')) $('#cashflowReconcileSave').addEventListener('click', confirmCashflowCompleteness);
    if ($('#cashBalance')) $('#cashBalance').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') saveCashBalance();
    });
    if ($('#snapCash')) $('#snapCash').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') submitPositionSnapshot();
    });
    if ($('#cashflowCashAfter')) $('#cashflowCashAfter').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') saveAccountCashflow();
    });
    if ($('#portfolioRiskModel')) $('#portfolioRiskModel').addEventListener('toggle', () => {
      if ($('#portfolioRiskModel').open) loadRiskReport(refreshSequence, true);
    });
    $('#recPrice').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') submitRecord();
    });
    $('#sellCancel').addEventListener('click', closeSell);
    $('#sellConfirm').addEventListener('click', confirmSell);
    $('#sellableCancel').addEventListener('click', closeSellableInventory);
    $('#sellableConfirm').addEventListener('click', confirmSellableInventory);
    $('#sellableShares').addEventListener('input', updateSellableInventory);
    $('#sellableShares').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') confirmSellableInventory();
    });
    $('#sellPrice').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') confirmSell();
    });
    $('#sellShares').addEventListener('input', updateSellQuantity);
    $('#sellShares').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') confirmSell();
    });
    $('#sellMask').addEventListener('click', (event) => {
      if (event.target === $('#sellMask')) closeSell();
    });
    $('#sellableMask').addEventListener('click', (event) => {
      if (event.target === $('#sellableMask')) closeSellableInventory();
    });
    document.addEventListener('keydown', handleDialogKeydown);
  }

  function init() {
    if (domReady) return;
    domReady = true;
    if ($('#cashflowAt') && !$('#cashflowAt').value) $('#cashflowAt').value = shanghaiDateTimeLocalValue();
    if ($('#cashflowReconcileDate') && !$('#cashflowReconcileDate').value) $('#cashflowReconcileDate').value = shanghaiDateValue();
    bindEvents();
    startMonitor();
    window.addEventListener('pagehide', stopMonitor);
  }

  // Claim before DOMContentLoaded so the earlier base-level alert script never
  // starts a duplicate initial poll. Visibility/pageshow listeners are also
  // registered before that script's runtime listeners, so ownership is final
  // before it chooses foreground or background cadence.
  document.addEventListener('visibilitychange', handleVisibilityChange);
  window.addEventListener('pageshow', handleVisibilityChange);
  if (!document.hidden) setMonitorOwnership(true);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
