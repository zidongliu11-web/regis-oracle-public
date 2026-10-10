(function () {
  'use strict';
  /* ═══ 工具 ═══ */
  function $(id) { return document.getElementById(id); }
  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function escAttr(s) { return esc(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function fn(n, d) { if (n == null || isNaN(n)) return '—'; if (d === undefined) d = 1; return Number(n).toFixed(d); }
  function fpp(p) { if (p == null) return '—'; return (p > 0 ? '+' : '') + Number(p).toFixed(2) + '%'; }
  function fpct(v) { if (v == null || isNaN(v)) return '—'; return (v * 100 > 0 ? '+' : '') + Number(v * 100).toFixed(2) + '%'; }  // 小数→百分比（批量闸门证据全为小数）
  function cc(v) { return v > 0 ? 'c-up' : v < 0 ? 'c-down' : ''; }   // 价格方向：红涨绿跌
  function fmtYi(v) { if (v == null) return '—'; return (v > 0 ? '+' : '') + fn(v, 2) + '亿'; }
  function fmtStamp(value) { return value ? String(value).replace('T', ' ').slice(0, 19) : '未记录'; }
  function formatFailure(value) {
    if (typeof value === 'string') return value;
    if (!value || typeof value !== 'object') return String(value || '未提供失败原因');
    const parts = [value.code || value.check || value.name, value.message || value.reason || value.detail].filter(Boolean);
    return parts.length ? parts.join(' · ') : '结构化失败记录（服务器未提供摘要）';
  }

  const _apiRequests = new Map();
  const _apiErrors = new Map();

  /* Current feasibility registrations load independently of the legacy registry. */
  const catalogueNames = {
    m24_negative_daily_return_rank: '日频反转基线', range_contraction_5_20: '波幅收缩',
    breakout_distance_20: '突破距离', close_acceptance: '收盘承接', pullback_volume_3_20: '回调量比',
    industry_excess_20: '行业相对强弱', sector_breadth_excess_v1: '板块上涨扩散',
    sector_effective_amount_participation_v1: '板块成交参与', attention_price_diffusion_gap_v1: '关注与价格扩散差',
    market_up_ratio_pct: '全市场上涨比例', market_top5pct_amount_share_pct: '成交集中度',
    PB00_RISK_CONTRACTION_CASH: '风险收缩与现金', PB01_M24_EXISTING_BASELINE: 'T+1 反转基线',
    PB04_TREND_BREAKOUT_EXCLUDED: '趋势突破', PB02_SECTOR_BREADTH_DIFFUSION_DRAFT: '板块广度扩散',
    PB03_ATTENTION_PRICE_GAP_DRAFT: '关注与价格差', PB05_LIQUIDITY_CROWDING_DIAGNOSTIC: '流动性拥挤诊断',
    PB06_CASH_ETF_ROTATION_CONTROL_DRAFT: '现金与宽基 ETF 对照',
  };
  const catalogueDecisions = {evidence_insufficient: '证据不足', excluded_pending_lineage: '已排除 · 来源谱系待补', control_only: '仅作对照'};
  const catalogueInputs = {
    stock_daily: '股票日线', etf_daily: 'ETF 日线', PIT_universe_L_D_P_BSE: '历史股票范围',
    PIT_ST: '历史 ST 状态', PIT_suspension: '历史停牌', historical_limits: '历史涨跌停价',
    adjustment_and_actions: '复权与公司行动', ETF_complete_actions: 'ETF 分红与拆合',
    exchange_calendar: '交易日历', rights_and_retention: '许可与留存权',
    historical_publication_evidence: '历史公布时点', minute_execution_quotes: '分钟执行行情',
    PIT_sector_membership: '历史板块成员', attention_event_truth: '关注事件原始证据',
  };
  function catalogueText(value) {
    return typeof value === 'string' ? value : JSON.stringify(value);
  }
  function renderResearchCatalogue(data) {
    const box = $('slabResearchCatalogue'), meta = $('slabResearchCatalogueMeta');
    if (!box || !meta) return;
    const closed = data && ['economic_admitted', 'production_eligible', 'generates_alert', 'auto_orders', 'account_access', 'real_replay_enabled'].every(key => data[key] === false);
    if (!closed || data.status !== 'available' || data.catalogue_only !== true
        || !Array.isArray(data.factors) || !Array.isArray(data.playbooks)
        || !data.factors.length || !data.playbooks.length
        || data.factors.some(row => !row || !catalogueDecisions[row.decision])
        || data.playbooks.some(row => !row || !catalogueDecisions[row.decision])) {
      meta.textContent = '登记暂不可读取';
      box.innerHTML = '<p class="slab-empty">研究登记缺失、读取失败或来源校验未通过；不能据此判定为空库。</p>';
      return;
    }
    meta.textContent = data.factors.length + ' 个因子 · ' + data.playbooks.length + ' 张战法卡 · 可行性登记';
    function card(row, isFactor) {
      const inputs = isFactor ? row.required_inputs : Object.values(row.required_inputs || {}).flat();
      const required = [...new Set(Array.isArray(inputs) ? inputs : [])].map(x => catalogueInputs[x] || x);
      const details = isFactor ? [['公式', row.formula], ['可用时点', row.input_clock], ['失败条件', row.failure_condition]]
        : [['机制', row.mechanism], ['市场条件', row.market_condition], ['板块条件', row.sector_condition],
          ['选股', row.stock_selection], ['入场', row.entry_new_cards], ['仓位', row.position],
          ['退出', row.exit_new_cards], ['冲突处理', row.conflicts], ['费用', row.costs], ['不交易', row.no_trade],
          ['失败条件', row.failure_condition], ['验证要求', row.validation]];
      return '<details class="slab-card slab-disclosure"><summary class="slab-card-hd"><span class="slab-card-title">'
        + esc(catalogueNames[row.id] || row.id) + '</span><span class="slab-card-badge">' + esc(catalogueDecisions[row.decision])
        + '</span></summary><div class="slab-card-body"><p>所需输入（尚待核验）：' + esc(required.join('、') || '见登记条件')
        + '</p>' + details.map(([name, value]) => '<p><b>' + esc(name) + '：</b>' + esc(catalogueText(value)) + '</p>').join('')
        + '<p class="slab-note">登记标识：' + esc(row.id) + '；历史尝试总数未知，保留原记录。</p></div></details>';
    }
    box.innerHTML = '<p class="slab-note">15 万元模拟资金。下列登记尚未获得经济试验或交易资格，所需输入不等于已接入数据。</p>'
      + '<h3>因子登记</h3>' + data.factors.map(row => card(row, true)).join('')
      + '<h3>战法登记</h3>' + data.playbooks.map(row => card(row, false)).join('')
      + '<details><summary>来源核对</summary>' + (data.source_bindings || []).map(ref => '<p>' + esc(ref.path) + '<br>SHA256 ' + esc(ref.sha256) + '</p>').join('') + '</details>';
  }
  async function loadResearchCatalogue() {
    renderResearchCatalogue(await api('/api/research-workbench/catalogue'));
  }
  window._labCatalogueRetry = loadResearchCatalogue;

  function renderSealedResearch(recent) {
    const box = $('slabSealedResearch'), meta = $('slabSealedResearchMeta');
    if (!box || !meta) return;
    if (!recent || !Array.isArray(recent.items)) {
      meta.textContent = '封存档案状态不可读';
      box.innerHTML = '<p class="slab-empty">接口未提供可核验的档案状态；不能据此判定没有研究。</p>';
      return;
    }
    meta.textContent = '可读 ' + recent.items.length + ' 条 / ' + (recent.count == null ? '未知' : recent.count)
      + ' · 文件缺失 ' + (recent.missing_count == null ? '未知' : recent.missing_count)
      + ' · 不可读 ' + (recent.unreadable_count == null ? '未知' : recent.unreadable_count);
    box.innerHTML = '<p class="slab-note">' + esc(recent.note || '历史报告只供复核，不授予当前交易资格。') + '</p>'
      + (recent.items.length ? recent.items.map(row => {
        const verdict = row.historical_factor_gate_evaluated !== true ? '尚未完成历史评估'
          : row.historical_factor_gate_passed === true ? '历史评估通过 · 当前未准入' : '历史评估未通过';
        return '<article class="slab-card"><div class="slab-card-body"><strong>' + esc(row.display_name || row.candidate_id)
          + '</strong><p>' + esc(verdict) + '</p><p class="slab-note">' + esc(row.report_path)
          + '<br>SHA256 ' + esc(row.report_sha256) + '</p></div></article>';
      }).join('') : '<p class="slab-empty">' + (recent.status === 'unreadable' ? '档案不可读取，请检查文件完整性。'
        : recent.status === 'files_missing' ? '列出的历史报告文件缺失，尚未恢复。' : '现有档案未提供可展示的结果。') + '</p>');
  }

  async function requestJSON(url, options = {}, timeoutMs = 15000, includeStatus = false) {
    const timeoutController = new AbortController();
    const externalSignal = options.signal;
    const forwardAbort = function () { timeoutController.abort(externalSignal.reason); };
    if (externalSignal) {
      if (externalSignal.aborted) forwardAbort();
      else externalSignal.addEventListener('abort', forwardAbort, { once: true });
    }
    const timeout = setTimeout(function () { timeoutController.abort('timeout'); }, timeoutMs);
    const fetchOptions = Object.assign({}, options, { signal: timeoutController.signal });
    try {
      const response = await fetch(url, fetchOptions);
      const text = await response.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; }
      catch (error) { throw new Error('服务器返回了无法解析的数据（HTTP ' + response.status + '）', { cause: error }); }
      if (!response.ok) {
        const error = new Error(data.error || data.message || ('请求失败（HTTP ' + response.status + '）'));
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return includeStatus ? Object.assign({}, data, { _httpOk: true, _httpStatus: response.status }) : data;
    } catch (error) {
      if (timeoutController.signal.aborted && timeoutController.signal.reason === 'timeout') {
        throw new Error('请求超时，请重试');
      }
      throw error;
    } finally {
      clearTimeout(timeout);
      if (externalSignal) externalSignal.removeEventListener('abort', forwardAbort);
    }
  }

  async function api(url) {
    let entry = _apiRequests.get(url);
    if (!entry) {
      const controller = new AbortController();
      entry = { controller, promise: null };
      _apiRequests.set(url, entry);
      const current = entry;
      entry.promise = requestJSON(url, { cache: 'no-store', signal: controller.signal })
        .then(function (data) { _apiErrors.delete(url); return data; })
        .catch(function (error) {
          if (!controller.signal.aborted) {
            _apiErrors.set(url, error.message || '接口读取失败');
            console.error('[strategy-lab:read]', url, error);
          }
          return null;
        }).finally(function () {
          if (_apiRequests.get(url) === current) _apiRequests.delete(url);
        });
    }
    const data = await entry.promise;
    return data == null ? null : JSON.parse(JSON.stringify(data));
  }

  async function apiWrite(url, payload, timeoutMs = 45000) {
    const requestKey = 'write:' + url;
    if (_apiRequests.has(requestKey)) return { _httpOk: false, error: '同类操作正在提交，请等待原请求结果。' };
    const controller = new AbortController();
    const entry = { controller };
    _apiRequests.set(requestKey, entry);
    try {
      const data = await requestJSON(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), signal: controller.signal,
      }, timeoutMs, true);
      return data;
    } catch (error) {
      if (error.name !== 'AbortError') console.error('[strategy-lab:write]', url, error);
      const uncertain = error.status == null;
      return { _httpOk: false, _httpStatus: error.status, error: (error.message || '服务连接失败')
        + (uncertain ? '；结果尚未确认，请刷新相关记录，勿重复提交。' : '') };
    } finally {
      if (_apiRequests.get(requestKey) === entry) _apiRequests.delete(requestKey);
    }
  }

  let _wb = null;          // workbench state
  let _activeReport = null;
  let _currentStrategyId = null;   // M3.3：当前工作区绑定的已保存策略（诊断写回 promotion 用）
  let _lastDiagnosis = null;
  let _experimentBaseline = null;
  let _lastAssistant = null;
  let _pendingExperimentHypothesis = null;
  let _factorView = 'focus';
  let _curveChart = null;
  let _curveResizeBound = false;
  let _curveResizeFrame = 0;
  let _forwardChart = null;
  let _forwardTimer = null;
  let _forwardAbort = null;
  let _forwardRequestSeq = 0;
  let _forwardRunId = null;
  let _forwardState = null;
  let _forwardVersion = null;
  let _workflow = null;
  let _factorWorkflow = null;
  let _workflowTimer = null;
  let _workflowAbort = null;
  let _workflowRequestSeq = 0;
  let _factorStatus = null;
  let _strategyEvidence = null;
  let _routeSnapshot = null;
  let _strictPoolCount = null;
  let _batchCapability = null;
  let _latestRunId = null;
  let _historyFollowLatest = true;
  let _reportAbort = null;
  let _reportRequestSeq = 0;
  let _sourceLoaded = false;
  let _sourceLoading = false;
  let _gateLoaded = false;
  let _gateLoading = false;
  let _baselineLoaded = false;
  let _historyLoaded = false;
  let _lastBatchState = null;
  let _diagnosisLoading = false;
  let _assistantLoading = false;
  const _COMPOSE_LABELS = {
    rank_weighted_v1: '固定权重排名',
    ic_weighted: '发现段 IC 加权',
    corr_ic_weighted: '发现段 IC + 相关性惩罚',
  };

  function disposeChart(chart, label) {
    if (!chart) return;
    try {
      if (!chart.isDisposed || !chart.isDisposed()) chart.dispose();
    } catch (error) {
      console.error('[strategy-lab:chart-dispose]', label, error);
    }
  }

  function factorLabel(id) {
    const meta = _wb && ((_wb.factor_catalog || {})[id] || (_wb.draft_factor_catalog || {})[id]);
    return (meta && meta.label) || id || '—';
  }

  function lineageFlowHtml(spec) {
    const factors = Object.entries((spec && spec.factors) || {}).filter(function (row) { return Number(row[1]) !== 0; });
    const factorText = factors.length
      ? factors.slice(0, 3).map(function (row) { return factorLabel(row[0]) + ' × ' + row[1]; }).join('；') + (factors.length > 3 ? '；+' + (factors.length - 3) : '')
      : '未记录因子配方';
    const compose = (spec && spec.compose_method) || 'rank_weighted_v1';
    return '<div class="slab-lineage" aria-label="策略因子到回测的证据链">'
      + '<div class="slab-lineage-step"><div class="k">1 · 因子</div><div class="v">' + esc(factorText) + '</div></div>'
      + '<div class="slab-lineage-step"><div class="k">2 · 合成</div><div class="v">' + esc(_COMPOSE_LABELS[compose] || compose) + '</div></div>'
      + '<div class="slab-lineage-step"><div class="k">3 · 选股</div><div class="v">横截面排名 · top-' + esc((spec && spec.top_n) || '—') + ' · 持有 ' + esc((spec && spec.holding_days) || '—') + ' 日</div></div>'
      + '<div class="slab-lineage-step"><div class="k">4 · 回测证据</div><div class="v">固定发现 / 验证 / 历史确认 · 成本后 · 5 折</div></div>'
      + '</div>';
  }

  function markFlow(stage, verdictText) {
    const order = ['Factors', 'Selection', 'Backtest', 'Verdict'];
    order.forEach(function (name) {
      const el = $('slabFlowStep' + name);
      if (el) el.classList.toggle('active', name.toLowerCase() === stage);
    });
    if (verdictText != null && $('slabFlowVerdict')) $('slabFlowVerdict').textContent = verdictText;
  }

  function updateEditorSummary() {
    updateCapitalSummary();
    const selected = Array.from(document.querySelectorAll('[data-user-factor]')).filter(function (input) { return Number(input.value || 0) !== 0; });
    const summary = $('slabSelectedSummary');
    if (summary) {
      summary.innerHTML = selected.length
        ? '<strong>已选 ' + selected.length + '</strong>' + selected.slice(0, 4).map(function (input) {
            return '<span class="slab-chip c-pos">' + esc(factorLabel(input.dataset.userFactor)) + ' ' + esc(input.value) + '</span>';
          }).join('') + (selected.length > 4 ? '<span>+' + (selected.length - 4) + '</span>' : '')
        : '<strong>尚未选择因子</strong>';
    }
    if ($('slabFlowFactors')) $('slabFlowFactors').textContent = selected.length ? selected.length + ' 个有效因子' : '请选择因子';
    const compose = $('slabCompose') ? $('slabCompose').value : 'rank_weighted_v1';
    const topN = $('slabTopN') ? $('slabTopN').value : '—';
    if ($('slabFlowSelection')) $('slabFlowSelection').textContent = (_COMPOSE_LABELS[compose] || compose) + ' · top-' + topN;
    markFlow(selected.length ? 'selection' : 'factors');
  }

  function filterFactorRows() {
    const wrap = $('slabFactorRows');
    if (!wrap) return;
    const box = $('slabFactorSearch');
    const q = box ? box.value.trim().toLowerCase() : '';
    wrap.querySelectorAll('.slab-factor-row').forEach(function (row) {
      const input = row.querySelector('[data-user-factor]');
      const selected = !!input && Number(input.value || 0) !== 0;
      const status = row.getAttribute('data-status') || '';
      const focus = selected || ['production', 'historical_pass', 'registered', 'validated', 'backtesting'].indexOf(status) >= 0;
      const viewOk = q ? true : (_factorView === 'all' || (_factorView === 'selected' ? selected : focus));
      const queryOk = !q || (row.getAttribute('data-query') || '').indexOf(q) >= 0;
      row.classList.toggle('hidden', !(viewOk && queryOk));
    });
    wrap.querySelectorAll('.slab-fgroup').forEach(function (group) {
      let anyVisible = false;
      let node = group.nextElementSibling;
      while (node && !node.classList.contains('slab-fgroup')) {
        if (node.classList.contains('slab-factor-row') && !node.classList.contains('hidden')) { anyVisible = true; break; }
        node = node.nextElementSibling;
      }
      group.classList.toggle('hidden', !anyVisible);
    });
  }

  function setStatus(text, tone) {
    const el = $('slabStatus');
    if (!el) return;
    el.className = 'slab-status ' + (tone === 'ok' ? 'c-ok' : tone === 'error' ? 'c-bad' : 'c-dim');
    el.textContent = text;
  }

  function syncActiveLayer() {
    const target = (window.location.hash || '#slabStatusLayer').slice(1);
    const known = Array.from(document.querySelectorAll('[data-workbench-layer]')).some(function (layer) { return layer.id === target; });
    const activeId = known ? target : 'slabStatusLayer';
    document.querySelectorAll('[data-workbench-layer]').forEach(function (layer) {
      layer.classList.toggle('active-layer', layer.id === activeId);
    });
    document.querySelectorAll('[data-layer-link]').forEach(function (link) {
      if (link.dataset.layerLink === activeId) link.setAttribute('aria-current', 'step');
      else link.removeAttribute('aria-current');
    });
  }

  function effectiveResearchWorkflow() {
    if (_strictPoolCount === 0 && _factorWorkflow && _factorWorkflow.current_stage) {
      return _factorWorkflow;
    }
    return _workflow;
  }

  function workflowBlockers(workflow) {
    const rows = (workflow && workflow.blockers) || [];
    return rows.map(function (row) { return typeof row === 'string' ? row : (row && (row.message || row.code)); }).filter(Boolean);
  }

  function fallbackStrictPoolCount(status, factors) {
    const epoch = status && status.research_epoch;
    if (!epoch || !Array.isArray(factors)) return null;
    return factors.filter(function (factor) {
      return factor && factor.research_epoch === epoch
        && factor.invalidated !== true && factor.status !== 'invalidated'
        && factor.status !== 'rejected' && factor.status !== 'frozen'
        && factor.is_ic_mean != null && factor.is_ic_ir != null
        && !!factor.sealed_at && !!factor.sealed_data_end && !!factor.sealed_recipe_sha256;
    }).length;
  }

  const ROUTE_STATE_LABELS = {
    risk_closed: '风险关闭', weak_repair: '弱修复', sentiment_rising: '情绪上升',
    crowded_climax: '高潮拥挤', retreat: '退潮', unknown: '尚未确认',
  };
  const ROUTE_PLAYBOOK_LABELS = {
    mainline_start_confirmation: '主线启动确认',
    leader_divergence_restrengthening: '核心分歧转强',
    retreat_defense: '退潮防守',
  };
  const ROUTE_STATE_POSTURES = [
    {
      id: 'risk_closed', label: '风险关闭', posture: '防守与持仓处置', detail: '新开仓 0；只复核减风险',
      characteristic: '广度、趋势或风险预算没有形成完整优势，现金优先。',
      riskActions: ['空仓等待：不因跌幅大就抄底', '持仓复核：只按真实可卖数量与风险线处置'],
    },
    {
      id: 'weak_repair', label: '弱修复', posture: '确认后轻仓研究', detail: '须主线同槽确认与正式策略',
      characteristic: '情绪从低位修复但稳定性不足，只研究早期扩散，不追成熟高位股。',
      riskActions: ['未确认主线时继续空仓', '确认撤销即停止新增风险'],
    },
    {
      id: 'sentiment_rising', label: '情绪上升', posture: '主线进攻候选', detail: '不追价；仍须完整买入计划',
      characteristic: '广度与核心扩散改善，可研究主线进攻，但必须先冻结龙头身份和成交条件。',
      riskActions: ['后排补涨不替代核心股', '封板排队不可验证时不声称成交'],
    },
    {
      id: 'crowded_climax', label: '高潮拥挤', posture: '不追后排', detail: '新开仓 0；防冲高回落',
      characteristic: '一致性过强、拥挤与隔夜回撤风险上升，重点从进攻切换到保护利润。',
      riskActions: ['停止追高：不接后排加速', '持仓降风险：冲高失败时按计划复核'],
    },
    {
      id: 'retreat', label: '退潮', posture: '退潮防守', detail: '新开仓 0；优先控制风险',
      characteristic: '亏钱效应扩散或主线瓦解，不寻找反弹理由，只保留风险处置链。',
      riskActions: ['不新增买入：现金优先', '逐仓核对：现价、可卖数量、止损线缺一不报股数'],
    },
  ];

  function renderRegimeMap(currentState) {
    const target = $('slabRegimeMap');
    if (!target) return;
    target.innerHTML = ROUTE_STATE_POSTURES.map(function (row) {
      const current = row.id === currentState;
      return '<div class="slab-regime-item ' + (current ? 'is-current' : '') + '">'
        + '<span class="state">' + esc(row.label) + (current ? ' · 当前' : '') + '</span>'
        + '<strong class="posture">' + esc(row.posture) + '</strong>'
        + '<small>' + esc(row.detail) + '</small></div>';
    }).join('');
  }

  function publishedLeaderProgram(program) {
    const published = _strategyEvidence && _strategyEvidence.current_research_program;
    const fallback = published && published.emotion_conditioned_leader_limit_program;
    const selected = program && program.schema && !(program.integrity_errors || []).length
      ? program : fallback && fallback.schema ? fallback : program;
    if (selected && selected.schema === 'emotion_conditioned_leader_limit_program_status.v1'
        && (selected.factor || {}).id === 'negative_limit_hit_count_20_v1') {
      // The immutable offline snapshot predates the outcome-selection defect.
      // An API failure must not restore its invalidated historical pass badge.
      const reason = '旧验证先剔除未结算事件再选组，历史通过结论需重验；不代表因子公式无效。';
      return Object.assign({}, selected, {
        headline: Object.assign({}, selected.headline, { historically_applicable_factor_state_count: 0 }),
        factor: Object.assign({}, selected.factor, { aggregate_historical_gate_passed: false,
          historically_applicable_states: [], historical_claim_reason: reason,
          historical_claim_status: 'outcome_selection_invalidated' }),
        states: (selected.states || []).map(function (row) {
          return Object.assign({}, row, { factor_historical_state_applicable: false, factor_reason: reason });
        }),
      });
    }
    return selected;
  }

  function publishedPlaybookLibrary() {
    const live = _routeSnapshot && _routeSnapshot.small_capital_playbook_library;
    if (live && Array.isArray(live.playbooks) && live.playbooks.length) return live;
    const published = _strategyEvidence && _strategyEvidence.current_research_program;
    return (published && published.published_playbook_library) || {};
  }

  function retiredEntryPlaybookIds() {
    const retirement = ((_routeSnapshot && _routeSnapshot.completed_market_review) || {})
      .terminal_playbook_retirement || {};
    return new Set(Array.isArray(retirement.retired_entry_playbooks)
      ? retirement.retired_entry_playbooks : []);
  }

  function tacticCardHtml(row, currentState, routed) {
    const tested = row._source === 'tested';
    const failed = tested && row.verdict === 'terminal_historical_failure_no_retune';
    const retired = row._retired === true;
    const riskOnly = row.kind === 'research_risk_playbook';
    const sealed = row.sealed_10bp || {};
    const evidence = failed
      ? '封存样本 ' + Number(sealed.trades || 0) + ' 笔 · 10BP后均值 ' + fpct(sealed.mean_net_return)
      : retired
        ? '历史规则已终止 · 不再进入候选或买入计划'
      : riskOnly
        ? '风控规则 · 不计算为入场策略'
        : '固定研究合同 · 定量准入 0 项';
    const status = failed ? '已淘汰' : retired ? '该规则已淘汰' : riskOnly ? '仅减风险'
      : currentState && routed ? '当前仅研究复核'
        : currentState ? '当前尚未开放' : '适用于此情绪 · 研究';
    const tone = failed || retired ? 'is-failed' : riskOnly ? 'is-defense'
      : currentState && routed ? 'is-routed' : 'is-research';
    return '<article class="slab-tactic-card ' + tone + '">'
      + '<div class="slab-tactic-title"><strong>' + esc(row.label || row.short_label || '未命名战法') + '</strong>'
      + '<span>' + esc(status) + '</span></div>'
      + '<p>' + esc(row.characteristic || '战法特点尚未形成可核验中文摘要。') + '</p>'
      + '<dl><div><dt>何时考虑</dt><dd>' + esc(row.consider_when || '须先满足当前情绪、主线板块与账户风控。') + '</dd></div>'
      + '<div><dt>执行方式</dt><dd>' + esc(row.entry_method || '未形成完整人工计划前不执行。') + '</dd></div>'
      + '<div><dt>失效/禁止</dt><dd>' + esc(row.invalidation || row.avoid_when || '任一输入失效即取消，不追价。') + '</dd></div></dl>'
      + '<footer>' + esc(evidence) + '</footer></article>';
  }

  function renderEmotionResearchMatrix(rawProgram) {
    const countsTarget = $('slabEmotionCounts');
    const matrixTarget = $('slabEmotionMatrix');
    if (!countsTarget || !matrixTarget) return;
    const program = publishedLeaderProgram(rawProgram);
    const errors = program && Array.isArray(program.integrity_errors)
      ? program.integrity_errors : [];
    if (!program || !program.schema || errors.length) {
      countsTarget.innerHTML = '<div class="slab-empty">证据不完整：正式因子、入场战法和提醒均按 0 处理。</div>';
      matrixTarget.innerHTML = '<div class="slab-empty">情绪分格证据不可用；不会沿用跨时期平均收益。</div>';
      return;
    }
    const counts = program.headline || {};
    countsTarget.innerHTML = [
      ['历史通过因子情绪格', Number(counts.historically_applicable_factor_state_count || 0)],
      ['正式辅助因子', Number(counts.production_auxiliary_factor_count || 0)],
      ['正式入场战法', Number(counts.production_entry_tactic_count || 0)],
      ['已授权买卖提醒', Number(counts.buy_sell_alert_count || 0)],
    ].map(function (row) {
      return '<div class="slab-emotion-count"><span>' + esc(row[0]) + '</span><strong>' + esc(row[1]) + '</strong></div>';
    }).join('');
    if ((program.factor || {}).historical_claim_reason) {
      countsTarget.insertAdjacentHTML('beforeend', '<p class="slab-empty" style="grid-column:1/-1">'
        + esc(program.factor.historical_claim_reason) + '</p>');
    }
    const routeWorkspace = (_routeSnapshot && _routeSnapshot.today_decision_workspace) || {};
    const currentRoute = routeWorkspace.market_state || (_routeSnapshot && _routeSnapshot.market_state) || 'unknown';
    const routedRows = Array.isArray(routeWorkspace.eligible_playbooks)
      ? routeWorkspace.eligible_playbooks : [];
    const routedIds = routedRows.map(function (row) {
      return typeof row === 'string' ? row : row && row.id;
    }).filter(Boolean);
    const tactics = Array.isArray(program.tactics) ? program.tactics : [];
    const library = publishedPlaybookLibrary();
    const playbooks = Array.isArray(library.playbooks) ? library.playbooks : [];
    const retiredPlaybooks = retiredEntryPlaybookIds();
    const states = Array.isArray(program.states) ? program.states : [];
    matrixTarget.innerHTML = states.map(function (row) {
      const applicable = row.factor_historical_state_applicable === true;
      const current = row.route_state === currentRoute;
      const metrics = row.factor_metrics || {};
      const guide = ROUTE_STATE_POSTURES.find(function (item) { return item.id === row.route_state; }) || {};
      const testedTactics = tactics.filter(function (tactic) {
        return tactic.emotion_state === row.emotion_state;
      }).map(function (tactic) {
        return Object.assign({ _source: 'tested' }, tactic);
      });
      const statePlaybooks = playbooks.filter(function (playbook) {
        return (playbook.applicable_market_states || []).indexOf(row.route_state) >= 0;
      }).map(function (playbook) {
        return Object.assign({
          _source: 'library', _retired: retiredPlaybooks.has(playbook.id),
        }, playbook);
      });
      const strategyCards = statePlaybooks.concat(testedTactics).map(function (tactic) {
        return tacticCardHtml(tactic, current, routedIds.indexOf(tactic.id) >= 0);
      }).join('');
      const riskActions = (guide.riskActions || []).map(function (text) {
        return '<li>' + esc(text) + '</li>';
      }).join('');
      const factorText = applicable
        ? '辅助排序因子历史通过：封存 ' + Number(metrics.sealed_weeks || 0) + ' 周，20BP后分组价差 '
          + fpct(metrics.sealed_top_minus_bottom_after_20bp) + '；同状态前向 '
          + Number(row.factor_forward_completed || 0) + '/' + Number(row.factor_forward_required || 0)
        : '辅助排序因子：' + (row.factor_reason || '该情绪格不可用');
      return '<article class="slab-emotion-cell ' + (current ? 'is-current ' : '')
        + (applicable ? 'is-historical-pass' : '') + '">'
        + '<header><div><span class="slab-state-kicker">市场情绪</span><strong>' + esc(row.label || '未确认')
        + (current ? ' · 当前' : '') + '</strong><p>' + esc(guide.characteristic || row.posture || '') + '</p></div>'
        + '<div class="slab-state-posture"><span>交易姿态</span><b>' + esc(row.posture || guide.posture || '等待确认') + '</b>'
        + '<small>本研究最多新增 ' + Number(row.maximum_new_trades_from_this_research || 0) + ' 笔</small></div></header>'
        + '<div class="slab-state-tactics"><div class="slab-state-section-title">本情绪对应的策略与战法</div>'
        + (strategyCards || '<div class="slab-empty">没有已冻结的入场战法；保持不交易。</div>') + '</div>'
        + '<aside><strong>风控动作</strong><ul>' + riskActions + '</ul></aside>'
        + '<div class="factor"><strong>因子只做辅助排序</strong><span>' + esc(factorText) + '</span></div></article>';
    }).join('') || '<div class="slab-empty">没有可核验的情绪状态格。</div>';
  }

  function renderRouteHelpers(route) {
    const errors = [];
    try {
      if (window.RegisApplicabilityMap) window.RegisApplicabilityMap.updateRoute(route);
    } catch (_) { errors.push('适用状态地图'); }
    try {
      if ($('slabMainlineEvidence')) $('slabMainlineEvidence').innerHTML = window.RegisMainlineEvidence.render(route && route.mainline_diagnostics);
    } catch (_) {
      errors.push('主线证据');
      if ($('slabMainlineEvidence')) $('slabMainlineEvidence').textContent = '主线证据显示失败；不能沿用旧内容。';
    }
    if ($('slabRouteHelperNotice')) $('slabRouteHelperNotice').textContent = errors.length
      ? errors.join('、') + '显示失败，其他区域已独立处理；可重新加载基础功能。' : '';
  }

  function renderStateRoute(route) {
    const object = value => value && typeof value === 'object' && !Array.isArray(value);
    const stale = object(route) && object(route.research_snapshot) && route.research_snapshot.current === false;
    route = object(route) && (route.account_access === false || (!stale && object(route.today_decision_workspace))) ? route : null;
    _routeSnapshot = route;
    const badge = $('slabRouteBadge');
    const blocker = $('slabRouteBlocker');
    if (route && route.account_access === false) {
      if (badge) badge.textContent = '账户方案关闭 · 仅研究';
      if (blocker) blocker.textContent = '账户读取已关闭。无账户的市场与板块联合验证尚未接通；因子、战法登记和板块轮动资料可独立查看。';
      ['slabRouteMarket', 'slabRouteSector', 'slabRoutePlaybook', 'slabRouteReplay'].forEach(function (id) {
        if ($(id)) $(id).textContent = '联合验证待接通';
      });
      ['slabRouteMarketNote', 'slabRouteSectorNote', 'slabRouteReplayNote'].forEach(function (id) {
        if ($(id)) $(id).textContent = '本次未读取联合结果；独立资料保留各自核验状态。';
      });
      if ($('slabRouteExposure')) $('slabRouteExposure').textContent = '不设账户仓位';
      if ($('slabPlaybookGrid')) $('slabPlaybookGrid').innerHTML = '<div class="slab-empty">当前联合战法匹配尚未核验；下方可查看已登记战法及所需输入。</div>';
      renderRegimeMap(null);
      updateResearchOverview();
      renderRouteHelpers(route);
      return;
    }
    if (!route || typeof route !== 'object') {
      if (badge) badge.textContent = '路由状态不可用';
      if (blocker) blocker.textContent = '当前无法读取市场状态联合研究链；不会沿用旧结果或生成战法建议。';
      if ($('slabRouteSector')) $('slabRouteSector').textContent = '主线输入待核验';
      if ($('slabRouteSectorNote')) $('slabRouteSectorNote').textContent = '诊断接口未返回，不沿用上次板块。';
      ['slabRouteMarket', 'slabRoutePlaybook', 'slabRouteReplay'].forEach(function (id) {
        if ($(id)) $(id).textContent = '本次未核验';
      });
      if ($('slabRouteExposure')) $('slabRouteExposure').textContent = '不设新目标';
      ['slabRouteMarketNote', 'slabRouteReplayNote'].forEach(function (id) {
        if ($(id)) $(id).textContent = '本次未取得有效快照；不沿用旧说明，不把缺失解释为零样本。';
      });
      renderRegimeMap(null);
      if ($('slabPlaybookGrid')) $('slabPlaybookGrid').innerHTML = '<div class="slab-empty">当前战法匹配未核验。可重新加载基础功能，已登记规则和历史档案仍可分别查看。</div>';
      updateResearchOverview();
      renderRouteHelpers(null);
      return;
    }
    const workspace = route.today_decision_workspace || {};
    const emotion = route.intraday_emotion_observation || {};
    const raw = emotion.raw_observation || {};
    const sector = route.sector_confirmation || {};
    const readiness = route.system_replay_readiness || {};
    const capture = readiness.strict_forward_capture || {};
    const next = capture.next_real_slot_boundary || {};
    const strictReady = workspace.strict_forward_packet_valid === true;
    const currentState = workspace.market_state && workspace.market_state !== 'unknown'
      ? workspace.market_state : route.market_state;
    const checkedState = emotion.market_state_being_checked || route.requested_market_state;
    const stateLabel = ROUTE_STATE_LABELS[currentState] || ROUTE_STATE_LABELS[checkedState] || '尚未确认';
    renderRegimeMap(currentState);
    renderEmotionResearchMatrix(_factorStatus && _factorStatus.leader_limit_program);
    const upRatio = raw.up_ratio;
    const marketText = emotion.confirmation === 'contradicted' && typeof upRatio === 'number' && Number.isFinite(upRatio)
      ? '盘中偏弱 · 上涨' + fn(upRatio, 1) + '%'
      : stateLabel;
    if ($('slabRouteMarket')) $('slabRouteMarket').textContent = marketText;
    if ($('slabRouteMarketNote')) $('slabRouteMarketNote').textContent = emotion.summary || '使用已完成数据与盘中确认';
    const exposure = workspace.strict_research_target_exposure_pct;
    if ($('slabRouteExposure')) $('slabRouteExposure').textContent = exposure == null ? '不设新目标' : fn(exposure, 0) + '%';
    const mainline = route.mainline_diagnostics || {};
    if ($('slabRouteSector')) $('slabRouteSector').textContent = mainline.label || '主线输入待核验';
    if ($('slabRouteSectorNote')) $('slabRouteSectorNote').textContent = mainline.reason || '诊断未返回，不把缺失数据解释为没有主线。';
    const allowedRows = Array.isArray(workspace.eligible_playbooks)
      ? workspace.eligible_playbooks : [];
    const allowed = allowedRows.map(function (row) {
      return typeof row === 'string' ? row : row && row.id;
    }).filter(Boolean);
    const matched = Array.isArray(workspace.eligible_playbooks);
    if ($('slabRoutePlaybook')) $('slabRoutePlaybook').textContent = !matched ? '当前战法匹配未核验'
      : allowed.length ? allowed.map(function (id) { return ROUTE_PLAYBOOK_LABELS[id] || '未命名战法'; }).join('、') : '本次未匹配进攻战法';
    if ($('slabRouteReplay')) $('slabRouteReplay').textContent = strictReady ? '本槽前向包已验证'
      : workspace.strict_forward_packet_valid === false ? '本次前向包未通过核验' : '本次前向包状态未核验';
    const knownCount = value => typeof value === 'number' && Number.isInteger(value) && value >= 0 ? String(value) : '未核验';
    if ($('slabRouteReplayNote')) $('slabRouteReplayNote').textContent = '休市日不计数；接口报告有效同槽包：' + knownCount(readiness.historical_same_slot_packets) + ' · 所需会话：' + knownCount(readiness.minimum_forward_sessions);
    if (badge) badge.textContent = strictReady ? '同槽前向包已形成 · 仍属研究' : '基础接口可读 · 采集尚未确认';

    const library = publishedPlaybookLibrary();
    const playbooks = Array.isArray(library.playbooks) ? library.playbooks : [];
    const retiredPlaybooks = retiredEntryPlaybookIds();
    const grid = $('slabPlaybookGrid');
    if (grid) {
      grid.innerHTML = playbooks.length ? playbooks.map(function (row) {
        const open = allowed.indexOf(row.id) >= 0;
        const retired = retiredPlaybooks.has(row.id);
        const explanation = retired
          ? '扣成本历史验证失败，已永久归档；不调参、不换名恢复。'
          : row.id === 'retreat_defense'
            ? '只作为减风险与空仓对照，不是入场策略，也不产生买入。'
            : row.id === 'leader_divergence_restrengthening'
              ? '盘中路径与可成交输入尚未完整，当前不能准入。'
              : open
                ? '当前路由允许研究复核；不等于买入信号。'
                : !matched ? '本次未提供匹配结果；规则可浏览，适用性待核验。' : '当前状态未准入；不会为当天行情临时切换。';
        const status = retired ? '已永久淘汰' : row.kind === 'research_risk_playbook'
          ? '仅减风险' : open ? '当前仅研究复核' : !matched ? '当前匹配未核验' : '当前情绪不启用';
        return '<article class="slab-playbook ' + (open ? 'is-open' : 'is-blocked') + '">'
          + '<div class="slab-playbook-title"><strong>' + esc(row.label || ROUTE_PLAYBOOK_LABELS[row.id] || '未命名战法') + '</strong><span>' + esc(status) + '</span></div>'
          + '<p>' + esc(row.characteristic || explanation) + '</p>'
          + '<dl><div><dt>何时考虑</dt><dd>' + esc(row.consider_when || '当前情绪、主线板块与账户风控同时满足时。') + '</dd></div>'
          + '<div><dt>怎么执行</dt><dd>' + esc(row.entry_method || explanation) + '</dd></div>'
          + '<div><dt>何时取消</dt><dd>' + esc(row.invalidation || row.avoid_when || '任一前置证据失效即取消。') + '</dd></div></dl>'
          + '<footer>' + esc(explanation) + '</footer></article>';
      }).join('') : '<div class="slab-empty">尚未读取到冻结战法库。</div>';
    }
    if (blocker) {
      if (strictReady) blocker.textContent = '当前前向包只进入联合回放；尚未完成成本后、样本外与规定长度的前向验证，不产生正式买卖提醒。';
      else if (next.available && next.not_before) blocker.textContent = '协议记录的采集时段：' + fmtStamp(next.not_before) + ' 至 ' + String(next.capture_until || '').replace('T', ' ').slice(0, 19) + '；此时段不证明采集器已启用。请在数据与恢复中心核对实际输入、权限与运行状态，不回填旧数据。';
      else blocker.textContent = (workspace.execution_route_reason || route.next_action || '当前没有可用的真实交易时段。') + '；休市日不计样本，也不要求人工观察。';
    }
    if (route.research_snapshot && route.research_snapshot.current === false) {
      if (badge) badge.textContent = '联合研究快照未就绪';
      if (blocker) blocker.textContent = route.research_snapshot.summary;
      if ($('slabRoutePlaybook')) $('slabRoutePlaybook').textContent = '当前战法匹配待核验';
      if ($('slabRouteReplay')) $('slabRouteReplay').textContent = '等待当前研究快照';
      if ($('slabRouteReplayNote')) $('slabRouteReplayNote').textContent = '未读取到当前核验结果，不以缺失表示零样本或休市。';
    }
    updateResearchOverview();
    renderRouteHelpers(route);
  }

  let _rotationPending = false;
  let _rotationTimer = null;
  function loadRotationEvidence() {
    const host = $('slabRotationEvidence');
    if (!host || _rotationPending || document.hidden) return;
    _rotationPending = true;
    host.innerHTML = '<h4>最近交易日板块轮动</h4><p>正在重新核验盘后原件；旧观察已清除。</p>';
    api('/api/research/sector-rotation').then(function (packet) {
      if (!host.isConnected) return;
      const checkedAt = new Date().toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'});
      host.innerHTML = '<p class="mainline-source">本次页面核查：' + checkedAt + '（上海时间）；行情和原件时点见下方。</p>'
        + (window.RegisMainlineEvidence?.rotation(packet)
          || '<p>板块轮动复盘组件暂不可用；旧观察已清除。</p>');
    }).catch(function () {
      if (host.isConnected) host.innerHTML = '<p>本次板块复盘无法处理；旧观察已清除。</p>';
    }).finally(function () { _rotationPending = false; });
  }

  function entryCandidateProgress(candidate) {
    const status = String((candidate && candidate.status) || '');
    if (status === 'formula_frozen_report_version_scan_active_no_outcome_access') {
      return {
        detail: '规则已冻结 · 12份官方财报试采成功 · 公告版本扫描进行中 · 未读取收益',
        tone: 'is-progress',
        verdict: '版本扫描中',
      };
    }
    if (status === 'initial_pdf_validation_collection_active_no_outcome_access') {
      const admission = (candidate && candidate.initial_pdf_validation_admission) || {};
      const targetCount = Number(admission.target_count || 0);
      return {
        detail: '规则已冻结 · ' + (targetCount > 0 ? targetCount + '份' : '') + '官方首份PDF逐份核验中 · 未读取收益',
        tone: 'is-progress',
        verdict: '输入核验中',
      };
    }
    if (candidate && (candidate.id || candidate.label)) {
      return {
        detail: '已有冻结候选，但当前阶段状态待核对；不视为已准入',
        tone: 'is-fail',
        verdict: '状态待核对',
      };
    }
    return {
      detail: '当前没有可推进的独立候选',
      tone: 'is-fail',
      verdict: '无候选',
    };
  }

  function inputCollectionProgress(collection) {
    const observedCount = Number((collection && collection.observed_file_count) || 0);
    const verifiedCount = Number((collection && collection.verified_packet_count) || 0);
    const captureWindow = String((collection && collection.first_real_capture_window) || '').trim();
    if (verifiedCount > 0) {
      return {
        summary: verifiedCount + ' 包严格通过',
        detail: (collection && collection.latest_result) || (collection && collection.scope) || '严格同槽输入已形成有效包',
        tone: 'is-pass',
        verdict: '已有有效包',
      };
    }
    if (captureWindow) {
      return {
        summary: observedCount + ' 次采集留痕 · 0 包通过',
        detail: ((collection && collection.latest_result) || '当前尚无严格有效包') + ' 下一窗口：' + captureWindow + '。',
        tone: 'is-progress',
        verdict: '待真实采样',
      };
    }
    return {
      summary: observedCount ? observedCount + ' 次采集留痕 · 0 包通过' : '当前不可用',
      detail: (collection && collection.latest_result) || (collection && collection.scope) || '只采输入，不排名、不选股、不读收益',
      tone: observedCount ? 'is-progress' : 'is-fail',
      verdict: observedCount ? '尚无有效包' : '输入未就绪',
    };
  }

  function renderStrategyEvidence(payload) {
    _strategyEvidence = payload && typeof payload === 'object' ? payload : null;
    const target = $('slabStrategyEvidence');
    if (!target) return;
    const programTarget = $('slabResearchProgram');
    const program = payload && payload.current_research_program;
    if (programTarget) {
      const entry = (program && program.independent_entry_strategy) || {};
      const candidate = (program && program.active_entry_candidate) || {};
      const latestTerminal = (program && program.latest_terminal_entry_candidate) || {};
      const namedTactics = (program && program.leader_board_tactic_validation) || {};
      const latestFactor = (program && program.latest_terminal_factor_candidate) || {};
      const sourceReview = (program && program.next_independent_source_review) || {};
      const overlay = (program && program.active_risk_overlay) || {};
      const blocked = (program && program.input_blocked_risk_candidate) || {};
      const collection = (program && program.active_input_collection) || {};
      const next = (program && program.next_action) || {};
      const failedCount = Array.isArray(program && program.terminal_entry_strategy_ids)
        ? program.terminal_entry_strategy_ids.length : 0;
      const candidateProgress = entryCandidateProgress(candidate);
      const collectionProgress = inputCollectionProgress(collection);
      const terminalFailedGateCount = Array.isArray(latestTerminal.failed_gates)
        ? latestTerminal.failed_gates.length : 0;
      programTarget.innerHTML = program ? [
        ['独立入场策略', Number(entry.admitted_count || 0) + ' 项可用', entry.reason || '没有完整优势时不交易', 'is-fail', '0 项准入'],
        ['冻结研究候选', candidate.label || '暂无', candidateProgress.detail, candidateProgress.tone, candidateProgress.verdict],
        ['最近完成验证', latestTerminal.label || '暂无', latestTerminal.id
          ? '留档摘要记录 PIT 输入、独立审计与成本后盲测；本页未重验原件 · ' + terminalFailedGateCount + ' 道准入门槛未通过，永久归档'
          : '当前没有刚完成的独立验证结果', latestTerminal.id ? 'is-fail' : 'is-progress', latestTerminal.id ? '盲测失败 · 已归档' : '暂无结果'],
        ['追龙头 / 单次打板', Number(namedTactics.tested_count || 0) + ' 条已检验 · ' + Number(namedTactics.admitted_count || 0) + ' 条准入', namedTactics.direct_locked_board_reason || '尚无可核验结果', 'is-fail', namedTactics.tested_count ? '均未通过' : '待验证'],
        ['辅助因子最近验证', latestFactor.label || '暂无', latestFactor.id
          ? '验证段 Rank IC ' + fn(latestFactor.validation_mean_rank_ic, 4) + '，封存段 Rank IC ' + fn(latestFactor.sealed_test_mean_rank_ic, 4) + ' · 已永久归档，但不阻塞非因子交易主链'
          : '因子只作辅助排序，不阻塞交易主链', latestFactor.id ? 'is-fail' : 'is-progress', latestFactor.id ? '样本外失败' : '暂无结果'],
        ['下一独立来源', sourceReview.label || '等待新机制', sourceReview.reason || '取得精确公式并冻结输入合同后才允许回测', 'is-progress', sourceReview.id ? '公式审计中' : '待来源'],
        ['风控覆盖层', overlay.label || '暂无', overlay.status === 'research_reference_only' ? '只减风险的研究参考，不产生买入或精确股数' : '当前不可用', 'is-pass', '仅研究'],
        ['严格盘中输入采集', collectionProgress.summary, collectionProgress.detail, collectionProgress.tone, collectionProgress.verdict],
        ['已永久淘汰', failedCount + ' 个方向', '不调参、不换名、不重复烧回测', 'is-fail', '已归档'],
        ['留档时的后续安排', next.label || '未记录', next.reason || '未记录当时安排', 'is-progress', '历史记录 · 非实时'],
      ].map(function (row) {
        return '<div class="slab-evidence-row ' + row[3] + '">'
          + '<div><strong>' + esc(row[0]) + '</strong><span>' + esc(row[1]) + '</span><span>' + esc(row[2]) + '</span></div>'
          + '<span class="verdict">' + esc(row[4]) + '</span></div>';
      }).join('') : '<div class="slab-empty">当前研发实况不可用；不会用旧队列代替。</div>';
      if (blocked.status === 'input_history_insufficient_no_return_test') {
        programTarget.innerHTML += '<div class="slab-empty">账户回撤缓冲：真实日净值历史不足，暂不运行无法识别的收益测试。</div>';
      }
    }
    const rows = payload && Array.isArray(payload.strategies) ? payload.strategies : [];
    if (!rows.length) {
      target.innerHTML = '<div class="slab-empty">历史回放摘要不可用；不会把缺失结果显示成通过。</div>';
      if (_routeSnapshot) renderStateRoute(_routeSnapshot);
      else renderEmotionResearchMatrix(_factorStatus && _factorStatus.leader_limit_program);
      return;
    }
    target.innerHTML = rows.map(function (row) {
      const passed = row.verdict === 'historical_diagnostic_pass';
      const preflightRejected = row.verdict === 'historical_preflight_falsified';
      const metrics = row.metrics || {};
      const annual = Number(metrics.annualized_return_pct);
      const drawdown = Number(metrics.maximum_drawdown_pct);
      const metricText = Number.isFinite(annual) && Number.isFinite(drawdown)
        ? '测试段成本后年化 ' + fn(annual, 2) + '% · 最大回撤 ' + fn(drawdown, 2) + '%'
        : (row.metric_summary || '查看原始证据');
      return '<div class="slab-evidence-row ' + (passed ? 'is-pass' : 'is-fail') + '">'
        + '<div><strong>' + esc(row.label || '未命名历史策略') + '</strong><span>' + esc(metricText) + '</span><span>' + esc(row.conclusion || '') + '</span></div>'
        + '<span class="verdict">' + (passed ? '历史诊断通过' : preflightRejected ? '预检淘汰' : '盲测失败归档') + '</span></div>';
    }).join('');
    if (_routeSnapshot) renderStateRoute(_routeSnapshot);
    else renderEmotionResearchMatrix(_factorStatus && _factorStatus.leader_limit_program);
    updateResearchOverview();
  }

  function currentResearchAction(data) {
    if (data && data.local_website_mode === 'interactive_only' && data.execution_role !== 'cloud') {
      return {label: '本机后台研究已停用', reason: '当前网站仅提供查询和手工编辑；不会按历史时段自动采集。请先修复和核验数据基础。'};
    }
    if (!data || data.online !== true) {
      return {label: '当前研究执行状态待核验',
        reason: '尚未取得有效的执行器进程与心跳证据；不沿用历史安排，不声称任务正在运行。'};
    }
    const jobs = Object.values(data.jobs || {});
    const running = jobs.find(function (job) { return job.status === 'running' && job.label === data.current_job; });
    if (running) return {label: '正在执行：' + running.label,
      reason: running.summary || '任务仍在执行，结果尚未返回；不把采集或计算进度当作交易资格。'};
    const pending = jobs.find(function (job) { return job.status !== 'complete' && job.status !== 'scheduled'; });
    if (pending) return {label: '待处理：' + (pending.label || '研究输入'),
      reason: pending.summary || '当前任务尚未完成，须核验实际输入与失败原因。'};
    return {label: '等待下一轮研究检查',
      reason: '执行器在线；已完成的采集或计算仍须分别通过成本后、样本外及前向检验，不自动转为买卖提醒。'};
  }

  window.addEventListener('regis-research-worker-status', function () {
    if (_strategyEvidence) updateResearchOverview();
  });

  function updateResearchOverview() {
    const count = typeof _strictPoolCount === 'number' && Number.isInteger(_strictPoolCount)
      && _strictPoolCount >= 0 ? _strictPoolCount : null;
    const runButton = $('slabRun');
    if (runButton && !_diagnosisLoading) {
      runButton.disabled = !_wb || _wb.available !== true || count == null || count === 0;
    }
    // The published JSON below is an immutable history, not a current admission API.
    // Factor eligibility is also not the number of admitted entry strategies.
    if ($('slabEpoch')) $('slabEpoch').textContent = '当前候选待核验';
    if ($('slabEpochState')) $('slabEpochState').textContent = '本次接口尚未提供独立入场候选状态；历史记录可在下方查看。';
    if ($('slabStrictPoolCount')) $('slabStrictPoolCount').textContent = '未核验';
    if ($('slabStrictPoolNote')) $('slabStrictPoolNote').textContent = '发布版历史数量与可运行因子数，均不能代替当前策略准入。';
    const live = _routeSnapshot && _routeSnapshot.account_access !== false;
    if ($('slabRouteMapCount')) $('slabRouteMapCount').textContent = live ? '研究快照已读取' : '联合验证待核验';
    if ($('slabRouteMapNote')) $('slabRouteMapNote').textContent = live
      ? '市场阶段定义与当次快照分别展示；允许研究不等于允许买入。'
      : '五类阶段是研究框架；接口未返回当前结果时，不声称已经接通。';
    const readiness = live && _routeSnapshot.system_replay_readiness;
    const capture = readiness && readiness.strict_forward_capture;
    const value = capture && capture.packet_count;
    const packets = typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
    if ($('slabResearchGate')) $('slabResearchGate').textContent = packets == null ? '前向数量未核验' : '本次返回 ' + packets + ' 个联合包';
    if ($('slabManualMode')) $('slabManualMode').textContent = packets == null
      ? '本次未取得有效计数；不把接口缺失解释为零样本。'
      : '仅为本次接口报告数量，仍须核验原包、有效会话与采集状态；不代表前向验证通过。';
    const next = currentResearchAction(window.RegisResearchWorkerStatus);
    if ($('slabNextActionTitle')) $('slabNextActionTitle').textContent = next.label;
    if ($('slabNextActionDetail')) $('slabNextActionDetail').textContent = next.reason;
    if ($('slabNextAction')) {
      $('slabNextAction').textContent = '检查数据与恢复原件';
      $('slabNextAction').href = '/data-center';
    }
    if ($('slabCurrentBlocker')) $('slabCurrentBlocker').textContent =
      (!_wb || _wb.available !== true ? '原共享面板未就绪；草稿与独立历史查询仍可使用。' : '')
      + (count == null ? '本次可运行因子数量未核验。' : '本次工作流返回 ' + count + ' 个可运行因子。')
      + '当前入场策略准入须单独核验；下方静态留档不作为当前可用结论。';
  }

  function applyWorkflow(workflow) {
    _workflow = workflow || null;
    const runtime = (_workflow && _workflow.runtime) || {};
    _strictPoolCount = typeof runtime.eligible_factor_count === 'number'
      && Number.isInteger(runtime.eligible_factor_count) && runtime.eligible_factor_count >= 0
      ? runtime.eligible_factor_count : null;
    _batchCapability = runtime.batch || null;
    updateResearchOverview();
  }

  function stopWorkflowSync() {
    if (_workflowTimer) clearTimeout(_workflowTimer);
    if (_workflowAbort) _workflowAbort.abort();
    _workflowTimer = null;
    _workflowAbort = null;
    _workflowRequestSeq += 1;
  }

  function scheduleWorkflowSync(delay) {
    if (_workflowTimer) clearTimeout(_workflowTimer);
    _workflowTimer = null;
    if (document.hidden) return;
    _workflowTimer = setTimeout(syncWorkflow, delay == null ? 30000 : delay);
  }

  async function syncWorkflow() {
    if (document.hidden) return;
    if (_workflowAbort) {
      scheduleWorkflowSync(1000);
      return;
    }
    api('/api/strategy-lab/development-catalog').then(renderDevelopmentPatterns);
    const requestSeq = ++_workflowRequestSeq;
    const controller = new AbortController();
    _workflowAbort = controller;
    try {
      const rows = await Promise.allSettled([
        requestJSON('/api/strategy-lab/user-workbench?view=summary', {
          cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' },
        }),
        requestJSON('/api/factor-system/status?view=summary', {
          cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' },
        }),
        requestJSON('/api/strategy-route/research?view=dashboard&account_access=false', {
          cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' },
        }),
      ]);
      const payload = rows[0].status === 'fulfilled' ? rows[0].value : null;
      const factorStatus = rows[1].status === 'fulfilled' ? rows[1].value : null;
      const route = rows[2].status === 'fulfilled' ? rows[2].value : null;
      if (requestSeq !== _workflowRequestSeq) return;
      _factorStatus = factorStatus;
      _factorWorkflow = factorStatus && factorStatus.workflow || null;
      renderSealedResearch(factorStatus && factorStatus.recent_sealed_research);
      if (factorStatus) {
        renderEmotionResearchMatrix(factorStatus.leader_limit_program);
        updateResearchOverview();
      } else {
        _strictPoolCount = null;
        renderEmotionResearchMatrix(null);
        updateResearchOverview();
      }
      renderStateRoute(route);
      if (!payload || !payload.workflow) {
        throw new Error(payload && (payload.error || payload.source_error) || '研究状态接口异常');
      }
      applyWorkflow(payload.workflow);
      const nextRunId = payload.latest_run_id || null;
      const followNewest = _historyFollowLatest && nextRunId
        && (!_activeReport || _activeReport.run_id !== nextRunId);
      if (nextRunId && _historyFollowLatest) prepareEvidenceArchive(null, nextRunId);
      if (followNewest && $('slabCurrentEvidence') && $('slabCurrentEvidence').open) {
        await loadEvidenceReport(nextRunId, false);
      }
    } catch (error) {
      if (requestSeq !== _workflowRequestSeq || error.name === 'AbortError') return;
      _strictPoolCount = null;
      updateResearchOverview();
      const blocker = $('slabCurrentBlocker');
      if (blocker) blocker.textContent = '工作流本次刷新失败；其上次快照不作为当前准入。其他接口独立核对，可点击重新加载基础功能。';
    } finally {
      if (_workflowAbort === controller) _workflowAbort = null;
      if (requestSeq === _workflowRequestSeq) scheduleWorkflowSync(30000);
    }
  }

  /* ═══ 数据底座 ═══ */
  function renderGate(gate) {
    const el = $('slabGate');
    if (!el) return;
    if (!gate) {
      el.innerHTML = '<span class="slab-badge dim">证据状态未知</span>';
      if ($('slabResearchGate')) $('slabResearchGate').textContent = '状态未知';
      return;
    }
    if (gate.account_access === false) {
      el.innerHTML = '<span class="slab-badge dim">账户读取关闭 · 研究预算15万元</span>'
        + '<p class="slab-note">历史报告保留原冻结口径，当前不授予交易资格。数据准备情况见研究状态和登记清单。</p>';
      if ($('slabResearchGate')) $('slabResearchGate').textContent = '交易资格未启用';
      if ($('slabManualMode')) $('slabManualMode').textContent = '无需个人账户；研究预算15万元';
      return;
    }
    const reg = gate.strategy_registry || {};
    const manualConsent = gate.manual_consent;
    const recommendationEligible = gate.recommendation_eligible;
    const brokerConnected = gate.broker_connected;
    const ok = manualConsent === true && recommendationEligible === true && brokerConnected === true;
    const truth = value => value === true ? '是' : value === false ? '否' : '未知';
    if (!_strategyEvidence) {
      if ($('slabResearchGate')) $('slabResearchGate').textContent = `正式研究资格：${truth(recommendationEligible)}`;
      if ($('slabManualMode')) $('slabManualMode').textContent = `人工同意：${truth(manualConsent)}；不会替代正式研究证据`;
    }
    const color = ok ? '#22c55e' : '#eab308';
    const label = ok ? '三项交易前置条件齐备' : '未形成可执行交易条件';
    const reason = gate.reason || '';
    const researchLockReason = reg.recommendation_lock_reason || '';
    const metaRows = [
      ['当前候选策略', reg.active_strategy || gate.active_strategy || '—'],
      ['人工操作已确认', truth(manualConsent)],
      ['正式研究资格', truth(recommendationEligible)],
      ['券商连接', truth(brokerConnected)],
      ['证据源', gate.source || '—'],
      ['数据时间', gate.generated_at || '—'],
    ];
    el.innerHTML = '<div style="text-align:right">'
      + '<span style="display:inline-block;padding:7px 14px;border-radius:10px;border:1px solid ' + color + '55;background:' + color + '14;color:' + color + ';font-size:var(--text-sm);font-weight:700">'
      + label + '</span>'
      + '<div style="margin-top:8px;display:flex;flex-direction:column;gap:3px;align-items:flex-end;font-size:12px;color:var(--text-tertiary);max-width:320px">'
      + metaRows.map(function (row) { return '<span><b style="color:var(--text-secondary)">' + esc(row[0]) + '</b>：' + esc(row[1] || '—') + '</span>'; }).join('')
      + (reason ? '<details style="text-align:left;width:100%;margin-top:2px"><summary style="cursor:pointer;color:var(--text-secondary)">未通过原因（点开）</summary><div style="margin-top:4px;line-height:1.55;color:var(--text-tertiary)">' + esc(reason) + '</div></details>' : '')
      + (researchLockReason && researchLockReason !== reason ? '<details style="text-align:left;width:100%;margin-top:2px"><summary style="cursor:pointer;color:var(--text-secondary)">研究推荐未通过理由（点开）</summary><div style="margin-top:4px;line-height:1.55;color:var(--text-tertiary)">' + esc(researchLockReason) + '</div></details>' : '')
      + '</div>'
      + '</div>';
  }

  async function loadDecisionGate() {
    if (_gateLoaded || _gateLoading) return;
    _gateLoading = true;
    const gate = $('slabGate');
    if (gate) gate.innerHTML = '<span class="slab-note" role="status">正在按需核对验证状态…</span>';
    const payload = await api('/api/strategy-lab?view=summary&account_access=false');
    _gateLoading = false;
    if (!payload) {
      if (gate) gate.innerHTML = '<span class="slab-note c-bad">验证状态读取失败；不会沿用旧结论。</span>';
      return;
    }
    _gateLoaded = true;
    renderGate(payload.decision_gate || null);
  }

  window._labGateToggle = function (details) {
    if (details && details.open) loadDecisionGate();
  };

  function renderFactorSystem(status, factors) {
    const el = $('slabFactorSystem');
    if (!el) return;
    _factorStatus = status || _factorStatus;
    if (_strictPoolCount == null) _strictPoolCount = fallbackStrictPoolCount(status, factors);
    updateResearchOverview();
    const reg = (status && status.registry) || {};
    if (!status) { el.innerHTML = '<div class="slab-note">因子系统未就绪。</div>'; return; }
    const prods = (factors || []).filter(function (f) { return f.status === 'production'; });
    const historical = (factors || []).filter(function (f) { return f.status === 'historical_pass'; });
    const forward = status.forward_shadow_readiness || {};
    const parts = [
      ['历史确认通过', reg.historical_pass || 0], ['正式因子', reg.production || 0], ['待确认', reg.registered || 0], ['已失效', reg.invalidated || 0], ['淘汰', reg.rejected || 0]
    ].map(function (p) { return '<span class="slab-badge dim">' + p[0] + ' ' + p[1] + '</span>'; }).join(' ');
    const prodHtml = prods.length
      ? prods.map(function (f) {
          const t = f.backtest_t_stat != null ? Number(f.backtest_t_stat).toFixed(2) : '—';
          const ic = f.oos_ic_mean != null ? Number(f.oos_ic_mean).toFixed(3) : '—';
          const ann = f.backtest_annual_return != null ? fpct(f.backtest_annual_return) : '—';
          return '<span class="slab-badge" style="color:#22c55e;border-color:#22c55e55">' + esc(f.name || f.factor_id) + ' · 新增数据检验 t ' + t + ' · IC ' + ic + ' · 年化 ' + ann + '</span>';
        }).join(' ')
      : '<span class="slab-note" style="color:#eab308">当前没有完成冻结配方与新增数据验证的正式因子</span>';
    el.innerHTML = '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><span class="slab-note">因子研究状态：</span>' + parts + '</div>'
      + '<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;align-items:center"><span class="slab-note">全局新增日历（非单因子进度）：</span><span class="slab-badge dim">' + fn(forward.observed_days || 0, 0) + ' 日 · 每因子需 ' + fn(forward.required_days || 120, 0) + ' 个完整标签</span>' + prodHtml + '</div>'
      + '<div class="slab-note" style="margin-top:7px">历史确认通过 ' + historical.length + ' 个，不等于可用于交易；当前研究批次：<span class="mono">' + esc(status.research_epoch || '—') + '</span>。</div>';
  }

  function renderSource(src, fixedSplits, costLadder, safety) {
    const el = $('slabSource');
    const badge = $('slabSourceBadge');
    if (badge) badge.textContent = src && src.date_max ? '面板就绪 · ' + esc(src.date_max) : '面板不可用';
    if (!src || !src.panel_rows) {
      el.innerHTML = '<div class="slab-empty">当前输入面板未就绪，策略与回测档案仍可查看：' + esc((_wb && _wb.source_error) || '缺少面板文件') + '</div>';
      return;
    }
    const scope = src.universe_scope || {};
    const splitHtml = Object.entries(fixedSplits || {}).map(function (kv) {
      const label = { development: '发现', validation: '验证', test: '历史确认', diagnostic_2026: '既有2026诊断（非盲测）' }[kv[0]] || kv[0];
      return '<span class="slab-badge dim" title="' + escAttr(kv[1].join(' ~ ')) + '">' + label + ' ' + esc(kv[1].join('~')) + '</span>';
    }).join(' ');
    el.innerHTML = '<div class="slab-metric-grid" style="margin-bottom:12px">'
      + '<div class="slab-metric"><span class="v">' + fn(src.panel_rows / 10000, 0) + '万</span><span class="l">面板行数</span></div>'
      + '<div class="slab-metric"><span class="v">' + fn(src.symbols, 0) + '</span><span class="l">覆盖股票数</span></div>'
      + '<div class="slab-metric"><span class="v">' + esc(src.date_min || '—') + '</span><span class="l">起始日</span></div>'
      + '<div class="slab-metric"><span class="v">' + esc(src.date_max || '—') + '</span><span class="l">截至日</span></div>'
      + '<div class="slab-metric"><span class="v mono" style="font-size:12px">' + esc((src.metadata_fingerprint || '—').slice(0, 10)) + '</span><span class="l">面板指纹</span></div>'
      + '</div>'
      + '<div class="slab-note" style="margin-bottom:10px">研究截面：' + esc(scope.label || scope.id || '—') + ' · 内容地址：<span class="mono">' + esc(src.panel_cache_key || src.metadata_fingerprint || '—') + '</span> · 数据截至：' + esc(src.date_max || '—') + '</div>'
      + '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px"><span class="slab-note">固定分段：</span>' + splitHtml
      + '<span class="slab-note" style="margin-left:8px">成本梯：</span>' + (costLadder || []).map(function (b) { return '<span class="slab-badge dim">' + b + 'BP</span>'; }).join(' ') + '</div>'
      + '<div class="slab-note">安全边界：' + (safety ? '外部模型 ' + (safety.external_model_used ? '是' : '否') + ' · 网络 ' + (safety.network_used ? '是' : '否') + ' · 自动下单 ' + (safety.automatic_trading ? '是' : '否') + ' · 助手模式 ' + esc(safety.assistant_mode || '—') : '—') + '</div>';
  }

  /* ═══ 年化目标 20% · 宽基基线（beta 腿）═══ */
  function renderBaseline(b) {
    const el = $('slabBaseline');
    const badge = $('slabBaselineBadge');
    if (!el) return;
    if (!b || !b.ok) {
      el.innerHTML = '<div class="slab-empty">宽基基线计算失败。</div>';
      if (badge) badge.textContent = '不可用';
      return;
    }
    const fm = (b.baselines && b.baselines.float_mv) || null;          // 历史确认窗口（牛市段）
    const eq = (b.baselines && b.baselines.equal) || null;
    const fw = (b.full_window && b.full_window.float_mv) || null;      // 全周期 2021-25（含熊市）
    const metColor = fw && fw.annual_return_pct >= 20.0 ? '#22c55e' : '#eab308';
    if (badge) {
      badge.style.color = metColor;
      badge.style.borderColor = metColor + '55';
      badge.style.background = metColor + '14';
      badge.textContent = '目标 20%：全周期 ' + (fw && fw.annual_return_pct >= 20.0 ? '已达成' : '未达成（需 alpha 叠加）');
    }
    const m = function (v, l) { return '<div class="slab-metric"><span class="v ' + cc(v) + '">' + fpp(v) + '</span><span class="l">' + l + '</span></div>'; };
    const yrows = Object.entries(b.yearly || {}).map(function (kv) {
      return '<tr><td class="num">' + kv[0] + '</td><td class="num ' + cc(kv[1]) + '">' + (kv[1] > 0 ? '+' : '') + kv[1].toFixed(1) + '%</td></tr>';
    }).join('');
    el.innerHTML =
      '<div class="slab-metric-grid" style="margin-bottom:12px">'
      + m(20.0, '年化目标线')
      + m(fw ? fw.annual_return_pct : null, '宽基基线净年化·全周期 2021-25（含熊市）')
      + m(fm ? fm.annual_return_pct : null, '历史确认窗口净年化（2024-07~' + esc((b.as_of || '2025-12') .slice(0, 7)) + ' 牛市段）')
      + m(fw ? fw.max_drawdown_pct : null, '全周期最大回撤')
      + m(b.alpha_gap_pct != null ? b.alpha_gap_pct : null, '跨周期缺口 = 20% − beta（需真 alpha 每年叠加）')
      + '</div>'
      + '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px">'
      + '<div style="flex:1;min-width:240px"><div class="slab-note" style="margin-bottom:6px"><b>宽基基线逐自然年（市值加权买持·净年化）</b></div>'
      + '<table class="slab-table"><thead><tr><th>年份</th><th>净年化</th></tr></thead><tbody>' + yrows + '</tbody></table></div>'
      + '<div style="flex:1.4;min-width:280px"><div class="slab-note" style="line-height:1.8"><b>诚实结论（实测，非断言）</b>：<br>'
      + '· 宽基 beta 全周期 2021-25 净年化 <b>' + (fw ? fw.annual_return_pct + '%' : '—') + '</b>，未到 20% —— 20%+ 只在牛市年出现（2021/2024/2025），2022 熊市约 -9%。<br>'
      + '· 跨周期可持续 20% = beta + <b>' + (b.alpha_gap_pct != null ? b.alpha_gap_pct + 'pp/年' : '—') + '</b> 真 alpha；当前因子投产状态以本页上方注册表实时值为准。<br>'
      + '· 结构：无 PASS 因子时 <b>默认持仓 = 宽基基线（不空仓）</b>，因子只在净成本闸门通过且净超额为正时替换/叠加。'
      + '<br><b>口径</b>：市值加权 = 全市场 float_mv 加权 o2o 扣 0.8%/年 ETF 级费率；等权 = 21 日再平衡摊换手成本。'
      + '数据带来源与 as_of，非投资建议。</div></div></div>';
  }

  /* ═══ M3.4 模板库（方向显式）═══ */
  function buildTemplatePicker(templates) {
    const sel = $('slabTemplate');
    if (!sel || !templates || !templates.length) return;
    sel.innerHTML = '<option value="">— 选择模板 —</option>' + templates.map(function (t) {
      return '<option value="' + escAttr(t.id) + '">' + esc(t.name) + '</option>';
    }).join('');
  }
  window._labLoadTemplate = function () {
    const sel = $('slabTemplate');
    const templates = (_wb && _wb.templates) || [];
    const t = templates.find(function (x) { return x.id === (sel && sel.value); });
    if (!t) return setStatus('请先选择一个模板。', 'error');
    applySpec(JSON.parse(JSON.stringify(t.spec)));   // 深拷贝，防止共享引用
    _currentStrategyId = null;                        // 模板是草稿，不绑定旧策略
    setStatus('已载入模板「' + t.name + '」：' + t.direction + '。可改权重后保存/回测/统一引擎诊断。', 'ok');
  };

  /* ═══ 策略编辑器（M7：内置 + 我的因子分组/搜索/证据芯片）═══ */
  function chipHtml(meta) {
    // 旧 epoch 已失效的证据绝不再生成正面芯片。
    if (meta.status === 'builtin') {
      return '<span class="slab-chip c-builtin">内置实证</span>';
    }
    if (meta.status === 'invalidated') {
      return '<span class="slab-chip c-neg">已失效 · 不计PASS</span>';
    }
    if (!meta.evidence) {
      return '<span class="slab-chip c-research">证据待确认</span>';
    }
    const ev = meta.evidence || {};
    const chips = [];
    const ic = ev.oos_ic_mean;
    if (ic != null) chips.push('<span class="slab-chip ' + (ic < 0 ? 'c-neg' : 'c-pos') + '">历史确认 IC ' + fn(ic, 3) + '</span>');
    const ir = ev.oos_ic_ir;
    if (ir != null) chips.push('<span class="slab-chip ' + (ir < 0 ? 'c-neg' : 'c-pos') + '">IC_IR ' + fn(ir, 2) + '</span>');
    const ret = ev.ic_retention;
    if (ret != null) chips.push('<span class="slab-chip">保留 ' + fn(ret, 2) + '</span>');
    const mono = ev.monotonic_aligned;
    if (mono != null) chips.push('<span class="slab-chip ' + (Math.abs(mono) > 0.5 ? 'c-pos' : '') + '">单调 ' + (Math.abs(mono) > 0.5 ? '✓' : '—') + '</span>');
    const statusLabel = { production: 'seal后前向投产', historical_pass: '历史确认通过', registered: '待历史确认', validated: '已验证', backtesting: '历史确认中', rejected: '淘汰', frozen: '冻结', invalidated: '已失效' }[meta.status] || '研究';
    const statusClass = meta.status === 'production' ? 'c-pos' : (meta.status === 'rejected' || meta.status === 'frozen' || meta.status === 'invalidated' ? 'c-neg' : 'c-research');
    chips.push('<span class="slab-chip ' + statusClass + '">' + statusLabel + '</span>');
    return chips.join('');
  }

  function buildEditor(catalog, defaults) {
    const wrap = $('slabFactorRows');
    if (!wrap) return;
    const names = Object.keys(catalog || {}).filter(function (factorId) {
      const meta = catalog[factorId] || {};
      return meta.status !== 'invalidated' && meta.invalidated !== true;
    });
    if (!names.length) { wrap.innerHTML = '<div class="slab-empty">无可用因子目录</div>'; return; }
    // 固定分组顺序：内置 → 遗传挖掘 → 模板 → 组合
    const order = ['内置实证因子', '我的因子·遗传挖掘', '我的因子·模板', '我的因子·组合'];
    const groups = {};
    names.forEach(function (f) {
      const g = (catalog[f] && catalog[f].group) || '其他';
      (groups[g] = groups[g] || []).push(f);
    });
    let rows = order.filter(function (g) { return groups[g]; }).map(function (g) {
      return '<div class="slab-fgroup" data-fgroup="' + escAttr(g) + '">' + esc(g) + ' <span class="slab-fcount">' + groups[g].length + '</span></div>'
        + groups[g].map(function (f) {
          const meta = catalog[f] || {};
          const w = ((defaults && defaults.factors) ? defaults.factors[f] : 0) || 0;
          const formula = meta.formula || (meta.status === 'builtin' ? '' : f);
          const searchable = (meta.label || '') + ' ' + formula + ' ' + f + ' ' + ((meta.group) || '');
          return '<div class="slab-factor-row' + (w ? '' : ' is-zero') + '" data-status="' + escAttr(meta.status || '') + '" data-query="' + escAttr(searchable.toLowerCase()) + '" title="' + escAttr(meta.description || '') + '">'
            + '<div class="slab-frow-head"><span class="fname">' + esc(meta.label || f) + '</span><span class="slab-chip-group">' + chipHtml(meta) + '</span></div>'
            + '<div class="slab-frow-foot"><span class="fdesc">' + esc(formula ? '公式 ' + formula : (meta.description || '')) + '</span>'
            + '<input type="number" step="0.1" min="-3" max="3" data-user-factor="' + escAttr(f) + '" value="' + fn(w, 1) + '" aria-label="' + escAttr((meta.label || f) + ' 因子权重') + '"></div>'
            + '</div>';
        }).join('');
    }).join('');
    // 未归类兜底
    Object.keys(groups).forEach(function (g) {
      if (order.indexOf(g) === -1) {
        rows += '<div class="slab-fgroup" data-fgroup="' + escAttr(g) + '">' + esc(g) + ' <span class="slab-fcount">' + groups[g].length + '</span></div>'
          + groups[g].map(function (f) {
            const meta = catalog[f] || {};
            const w = ((defaults && defaults.factors) ? defaults.factors[f] : 0) || 0;
            return '<div class="slab-factor-row' + (w ? '' : ' is-zero') + '" data-status="' + escAttr(meta.status || '') + '" data-query="' + escAttr(((meta.label || f) + ' ' + (meta.formula || '') + ' ' + f).toLowerCase()) + '">'
              + '<div class="slab-frow-head"><span class="fname">' + esc(meta.label || f) + '</span></div>'
              + '<div class="slab-frow-foot"><span class="fdesc">' + esc(meta.description || '') + '</span>'
              + '<input type="number" step="0.1" min="-3" max="3" data-user-factor="' + escAttr(f) + '" value="' + fn(w, 1) + '" aria-label="' + escAttr((meta.label || f) + ' 因子权重') + '"></div>'
              + '</div>';
          }).join('');
      }
    });
    wrap.innerHTML = rows;
    updateEditorSummary();
    filterFactorRows();
  }

  function collectSpec() {
    const factors = {};
    document.querySelectorAll('[data-user-factor]').forEach(function (input) {
      const v = Number(input.value || 0);
      if (v !== 0) factors[input.dataset.userFactor] = v;   // M7：只传非零，DSL 精简
    });
    return {
      name: $('slabName').value || '',
      holding_days: Number($('slabHolding').value || 5),
      top_n: Number($('slabTopN').value || 30),
      one_way_cost_bps: Number($('slabCost').value || 10),
      rebal_buffer: Number($('slabRebalBuffer').value || 0),
      risk_rule: $('slabRisk').value || 'none',
      entry_rule: $('slabEntry').value || 'none',
      exposure_rule: $('slabExposure').value || 'none',
      compose_method: $('slabCompose').value || 'rank_weighted_v1',
      // Empty is invalid (0), never a hidden change to another budget.
      account_capital: Number($('slabCapital').value),
      factors: factors,
    };
  }

  function updateCapitalSummary() {
    const input = $('slabCapital'), note = $('slabCapitalSummary');
    if (!input || !note) return;
    const capital = Number(input.value), topN = Number($('slabTopN') && $('slabTopN').value);
    if (!Number.isFinite(capital) || capital < 100000 || capital > 50000000) {
      note.textContent = '请输入10万至5000万元内的研究资金；项目目标为15万元。空值不会自动改用其他金额。';
      return;
    }
    const origin = Number(input.dataset.loadedCapital) === capital
      ? (input.dataset.capitalOrigin || '载入规格') : '手工修改';
    const match = capital === 150000 ? '符合项目15万元预算' : '与项目15万元预算不同';
    const allocation = Number.isInteger(topN) && topN >= 10 && topN <= 100
      ? '若等额分配给' + topN + '只，每只预算上限约' + fn(capital / topN, 2) + '元（未扣费用与现金预留）。'
      : '请先填写10至100只的整数持仓数。';
    note.textContent = origin + '：' + fn(capital, 0) + '元 · ' + match + '。' + allocation
      + '这是预算算术；能否买够100股还须按当时价格、费用及共享现金逐笔检验。';
  }

  function applySpec(spec, keepId, capitalContext) {
    if (!spec) return;
    if (!keepId) { /* strategy_id 用最近保存值保留 */ }
    if (spec.strategy_id) _currentStrategyId = spec.strategy_id;   // M3.3：诊断可写回该策略 promotion
    if ($('slabName')) $('slabName').value = spec.name || '';
    if ($('slabHolding')) $('slabHolding').value = String(spec.holding_days || 5);
    if ($('slabTopN')) $('slabTopN').value = spec.top_n || 30;
    if ($('slabCost')) $('slabCost').value = spec.one_way_cost_bps ?? 10;
    if ($('slabRebalBuffer')) $('slabRebalBuffer').value = spec.rebal_buffer ?? 3;
    if ($('slabRisk')) $('slabRisk').value = spec.risk_rule || 'none';
    if ($('slabEntry')) $('slabEntry').value = spec.entry_rule || 'none';
    if ($('slabExposure')) $('slabExposure').value = spec.exposure_rule || 'none';
    if ($('slabCompose')) $('slabCompose').value = spec.compose_method || 'rank_weighted_v1';
    if ($('slabCapital')) {
      // Omitted capital in old specs meant 200000; loading is not rebasing.
      const capital = spec.account_capital ?? 200000;
      $('slabCapital').value = capital;
      $('slabCapital').dataset.loadedCapital = String(capital);
      $('slabCapital').dataset.capitalOrigin = capitalContext
        || (spec.account_capital == null ? '旧规格未写资金，按原规则20万元载入' : '载入规格原资金');
    }
    document.querySelectorAll('[data-user-factor]').forEach(function (input) {
      input.value = spec.factors && spec.factors[input.dataset.userFactor] != null ? spec.factors[input.dataset.userFactor] : 0;
      const row = input.closest('.slab-factor-row');
      if (row) row.classList.toggle('is-zero', Number(input.value || 0) === 0);
    });
    updateEditorSummary();
    filterFactorRows();
    syncDsl();
    updateExperimentDraft();
  }

  function syncDsl() {
    const t = $('slabDsl');
    if (t) t.value = JSON.stringify(collectSpec(), null, 2);
  }

  async function labSyncFromDsl() {
    const t = $('slabDsl');
    const button = $('slabDslLoad');
    try {
      if (button) button.disabled = true;
      setStatus('正在校验并载入 JSON DSL…');
      const spec = JSON.parse(t.value || '{}');
      const res = await apiWrite('/api/strategy-lab/user-strategies/validate', spec);
      if (button) button.disabled = false;
      if (!res._httpOk) return setStatus(res.error || 'DSL 校验失败', 'error');
      applySpec(res.spec);
      setStatus('JSON DSL 已通过白名单校验并同步到可视化构建器。', 'ok');
    } catch (e) {
      if (button) button.disabled = false;
      setStatus('JSON 格式错误：' + e.message, 'error');
    }
  }

  async function labValidate() {
    const button = $('slabValidate');
    if (button) button.disabled = true;
    setStatus('正在校验当前策略规格…');
    const res = await apiWrite('/api/strategy-lab/user-strategies/validate', collectSpec());
    if (button) button.disabled = false;
    if (!res._httpOk) return setStatus(res.error || '规格校验失败', 'error');
    setStatus('草稿规格校验通过：字段、范围和因子在安全白名单内。来源、时点数据与运行资格仍须另行核验。', 'ok');
    syncDsl();
  }

  function labClone() {
    const spec = collectSpec();
    spec.name = (spec.name || '策略').slice(0, 50) + ' · 副本';
    _currentStrategyId = null;
    _lastDiagnosis = null;
    applySpec(spec);
    setStatus('已克隆为未保存的新草稿；原策略没有被覆盖。', 'ok');
  }

  function labProjectBudgetDraft() {
    if (_diagnosisLoading) return setStatus('当前诊断仍在运行，结束后再复制草稿。', 'error');
    const spec = collectSpec();
    spec.account_capital = 150000;
    spec.name = (spec.name || '策略').slice(0, 46) + ' · 15万元草稿';
    _currentStrategyId = null;
    _lastDiagnosis = null;
    if ($('slabUnifiedDiag')) $('slabUnifiedDiag').innerHTML = '';
    applySpec(spec, false, '复制的新草稿');
    setStatus('已复制为未保存的15万元草稿，其余参数保留。原策略与报告未改写；旧结果不适用于新资金，须重新验证。', 'ok');
  }

  async function labSave() {
    const btn = $('slabSave');
    if (btn) btn.disabled = true;
    setStatus('正在保存本地策略规格…');
    const spec = collectSpec();
    if (_currentStrategyId) spec.strategy_id = _currentStrategyId;
    const res = await apiWrite('/api/strategy-lab/user-strategies', spec);
    if (btn) btn.disabled = false;
    if (!res._httpOk) return setStatus(res.error || '保存失败', 'error');
    _currentStrategyId = res.strategy.strategy_id;   // M3.3：后续诊断绑定到刚保存的策略
    _lastDiagnosis = null;
    updateExperimentDraft();
    setStatus('已保存 ' + res.strategy.strategy_id + '；不产生任何买入动作。', 'ok');
    refreshWorkbench();
  }

  async function labRun() {
    if (!_wb || _wb.available !== true) {
      setStatus('当前输入面板未就绪，档案仍可查看；暂不提交科学回测。', 'error');
      return null;
    }
    if (_strictPoolCount == null) {
      setStatus('公开来源、固定规则与历史时点数据尚未核对完成；回测保持禁用。', 'error');
      return null;
    }
    if (_strictPoolCount === 0) {
      setStatus('当前没有同时具备可核验来源、固定配方、本地复现与历史时点数据的因子；不会提交回测。', 'error');
      return null;
    }
    const spec = collectSpec();
    markFlow('backtest', '回测进行中');
    const btn = $('slabRun');
    if (btn) { btn.disabled = true; btn.textContent = '统一回测中…'; }
    setStatus('正在使用统一事件引擎按因子选股并回测；严格全截面通常需数分钟…');
    if ($('slabUnifiedDiag')) $('slabUnifiedDiag').innerHTML = '';
    const res = await window._labUnifiedDiag(
      spec, _currentStrategyId || null, { publishHistoryReport: true }
    );
    if (btn) { btn.disabled = false; btn.textContent = '运行科学回测'; }
    if (res && res._httpOk && res.run_id) {
      _historyFollowLatest = true;
      _latestRunId = res.run_id;
      const url = new URL(window.location.href);
      url.searchParams.delete('run');
      window.history.replaceState(null, '', url);
      prepareEvidenceArchive(res.report_summary || null, res.run_id);
      if ($('slabCurrentEvidence')) $('slabCurrentEvidence').open = true;
      await loadEvidenceReport(res.run_id, true);
    }
    return res;
  }

  /* ═══ 注册表 ═══ */
  function renderRegistry(saved, recent) {
    const el = $('slabRegistry');
    const count = $('slabRegistryCount');
    const savedLen = (saved || []).length;
    if (count) count.textContent = savedLen + ' 已保存';
    if (!savedLen && !(recent || []).length) { el.innerHTML = '<div class="slab-empty">尚无已保存策略或回测记录。设计一条策略并运行回测。</div>'; return; }
    let html = '';
    if (savedLen) {
      html += '<div style="padding:10px 18px;font-size:var(--text-xs);color:var(--text-tertiary);font-weight:600">已保存策略（含历史版本）</div>'
        + '<div style="overflow:auto"><table class="slab-table"><thead><tr><th>名称</th><th>策略ID</th><th>版本</th><th>修订</th><th>保存时间</th><th>统一判定</th><th>结论</th><th>操作</th></tr></thead><tbody>'
        + saved.map(function (s) {
          const p = s.promotion || {};
          const invalidated = p.unified_verdict === 'INVALIDATED' || p.unified_verdict === 'PASS' || s.invalidated === true;
          const historicalPass = p.unified_verdict === 'HISTORICAL_PASS';
          const gate = invalidated ? '<span class="slab-badge bad">旧证据失效</span>'
            : s.recommendation_eligible ? '<span class="slab-badge ok">研究门禁已标记可用 · 仍需人工</span>'
            : historicalPass ? '<span class="slab-badge dim">等待前向终测</span>'
            : '<span class="slab-badge dim">研究</span>';
          const pBadge = p.unified_verdict === 'PASS' ? '<span class="slab-badge bad">INVALIDATED · 无受支持前向证据</span>'
            : historicalPass ? '<span class="slab-badge dim">历史确认通过 · 等待独立前向 runner</span>'
            : p.unified_verdict === 'NO-GO' ? '<span class="slab-badge bad">NO-GO</span>'
            : p.unified_verdict === 'INVALIDATED' ? '<span class="slab-badge bad">INVALIDATED · 旧证据失效</span>'
            : '<span class="slab-badge dim">未判定</span>';
          const obsBtn = p.paper_observe
            ? '<button type="button" class="slab-row-btn" data-action="promotion" data-id="' + escAttr(s.strategy_id || '') + '" data-promotion-action="paper_observe_off">取消观察</button>'
            : '<button type="button" class="slab-row-btn" data-action="promotion" data-id="' + escAttr(s.strategy_id || '') + '" data-promotion-action="paper_observe_on">观察</button>';
          const unlockBtn = p.unlocked
            ? '<button type="button" class="slab-row-btn" data-action="promotion" data-id="' + escAttr(s.strategy_id || '') + '" data-promotion-action="lock">锁回</button>'
            : '<button type="button" class="slab-row-btn" disabled title="当前没有策略前向终测 runner；历史确认不能解锁">解锁</button>';
          return '<tr>'
            + '<td style="max-width:160px;overflow:hidden;text-overflow:ellipsis">' + esc((s.spec && s.spec.name) || '未命名') + '</td>'
            + '<td class="mono">' + esc(s.strategy_id || '—') + '</td>'
            + '<td class="num">v' + fn(s.version, 0) + '</td>'
            + '<td class="num">' + fn(s.revision_count || 0, 0) + '</td>'
            + '<td>' + esc((s.saved_at || '').slice(0, 16)) + '</td>'
            + '<td>' + pBadge + (p.diagnosed_at ? '<div class="c-dim" style="font-size:12px">' + esc(String(p.diagnosed_at).slice(0, 16)) + '</div>' : '') + '</td>'
            + '<td>' + gate + '</td>'
            + '<td><button type="button" class="slab-row-btn" data-action="load-strategy" data-id="' + escAttr(s.strategy_id || '') + '">载入</button><button type="button" class="slab-row-btn" data-action="run-saved" data-id="' + escAttr(s.strategy_id || '') + '">回测</button>' + obsBtn + unlockBtn + '</td>'
            + '</tr>';
        }).join('') + '</tbody></table>'
        + '<div class="c-dim" style="font-size:12px;padding:7px 4px">当前没有策略前向终测 runner，不能生成 PASS 或解锁；历史确认通过始终只用于研究。观察仅为纸面前向记录，不授予资格。</div></div>';
    }
    if ((recent || []).length) {
      html += '<div style="padding:10px 18px;font-size:var(--text-xs);color:var(--text-tertiary);font-weight:600;border-top:1px solid var(--border-subtle)">最近回测</div>'
        + '<div style="overflow:auto"><table class="slab-table"><thead><tr><th>run_id</th><th>名称</th><th>时间</th><th>历史确认累计</th><th>历史确认年化</th><th>门禁</th><th>操作</th></tr></thead><tbody>'
        + recent.map(function (r) {
          const m = (r.metrics && r.metrics.test) || {};
          const invalidated = ((r.promotion || {}).unified_verdict === 'INVALIDATED') || r.invalidated === true;
          const pass = !invalidated && !!r.historical_validation_gate_passed;
          return '<tr>'
            + '<td class="mono">' + esc(r.run_id || '—') + '</td>'
            + '<td style="max-width:160px;overflow:hidden;text-overflow:ellipsis">' + esc(r.name || '未命名') + '</td>'
            + '<td>' + esc((r.generated_at || '').slice(0, 16)) + '</td>'
            + '<td class="num ' + cc(m.total_return_pct) + '">' + fpp(m.total_return_pct) + '</td>'
            + '<td class="num ' + cc(m.annualized_return_pct) + '">' + fpp(m.annualized_return_pct) + '</td>'
            + '<td>' + (invalidated ? '<span class="slab-badge bad">旧证据失效</span>' : pass ? '<span class="slab-badge ok">历史确认通过</span>' : '<span class="slab-badge bad">未通过</span>') + '</td>'
            + '<td><button type="button" class="slab-row-btn" data-action="view-run" data-id="' + escAttr(r.run_id || '') + '">查看报告</button></td>'
            + '</tr>';
        }).join('') + '</tbody></table></div>';
    }
    el.innerHTML = '<div>' + html + '</div>';
  }

  window._labLoad = function (id) {
    const item = ((_wb && _wb.saved_strategies) || []).find(function (row) { return row.strategy_id === id; });
    if (!item) return setStatus('未找到策略 ' + id, 'error');
    applySpec(Object.assign({}, item.spec || {}, { strategy_id: item.strategy_id }));
    setStatus('已载入 ' + id + '，修改后可另存或直接回测。', 'ok');
  };
  window._labPromotion = async function (id, action, runId) {
    const labels = { paper_observe_on: '开启纸面前向观察', paper_observe_off: '取消纸面前向观察', unlock: '解锁晋升资格', lock: '锁回（fail-closed）' };
    const payload = { strategy_id: id, action: action };
    if (runId) payload.run_id = runId;
    const res = await apiWrite('/api/strategy-lab/promotion', payload);
    if (!res._httpOk) {
      setStatus(res.error || '晋升动作失败', 'error');
      return res;
    }
    refreshWorkbench();
    const p = res.promotion || {};
    setStatus((labels[action] || action) + ' 完成：' + id + '，统一判定 ' + (p.unified_verdict || '未判定') + '，解锁=' + (p.unlocked ? '是' : '否') + '，可投=' + (res.recommendation_eligible ? '是' : '否') + '。', res.recommendation_eligible ? 'ok' : 'error');
    return res;
  };
  window._labRunSaved = async function (id) {
    const item = ((_wb && _wb.saved_strategies) || []).find(function (row) { return row.strategy_id === id; });
    if (!item) return setStatus('未找到策略 ' + id, 'error');
    applySpec(Object.assign({}, item.spec || {}, { strategy_id: item.strategy_id }));
    await labRun();
  };
  function prepareEvidenceArchive(summary, runId) {
    const availableRunId = runId || (summary && summary.run_id) || _latestRunId;
    // A ?run= deep link is pinned evidence.  Background workbench refreshes may
    // discover a newer run, but must not silently replace the selected report.
    if (_historyFollowLatest || !_latestRunId) _latestRunId = availableRunId;
    const invalidated = summary && (summary.current_epoch_eligible === false || summary.invalidated === true || ((summary.promotion || {}).unified_verdict === 'INVALIDATED'));
    if ($('slabEvidenceBadge')) $('slabEvidenceBadge').textContent = !_latestRunId
      ? '暂无最近报告'
      : (invalidated ? 'INVALIDATED · 旧窗口档案' : '按需读取 · 历史档案');
    if ($('slabResult') && !_activeReport) {
      $('slabResult').innerHTML = _latestRunId
        ? '<div class="slab-empty">最近报告 ' + esc(_latestRunId) + ' 仅在展开后读取。旧窗口报告只作证据档案，不代表当前 PASS。</div>'
        : '<div class="slab-empty">尚无回测证据报告。</div>';
    }
  }

  async function loadEvidenceReport(runId, reveal) {
    if (!runId) return null;
    if (_activeReport && _activeReport.run_id === runId) {
      renderReport(_activeReport, reveal);
      return _activeReport;
    }
    if (_reportAbort) _reportAbort.abort();
    const requestSeq = ++_reportRequestSeq;
    const controller = new AbortController();
    _reportAbort = controller;
    stopForwardSync(true);
    if (_curveChart) { disposeChart(_curveChart, 'historical'); _curveChart = null; }
    _activeReport = null;
    resetForwardPanel('正在读取所选历史报告，随后核对其独立前向状态…');
    if ($('slabResult')) $('slabResult').innerHTML = '<div class="slab-empty" role="status">正在按 run_id 读取完整本地证据…</div>';
    setStatus('正在读取 ' + runId + ' 的完整本地证据…');
    try {
      const res = await requestJSON('/api/strategy-lab/user-backtests/' + encodeURIComponent(runId), {
        cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' },
      });
      if (requestSeq !== _reportRequestSeq || controller.signal.aborted) return null;
      if (!res || !res.ok) throw new Error((res && res.error) || '报告读取失败');
      if (!res.report || res.report.run_id !== runId) throw new Error('报告响应 run_id 与请求不一致');
      renderReport(res.report, reveal);
      if (res.report.spec) applySpec(Object.assign({}, res.report.spec, { strategy_id: res.report.strategy_id }));
      setStatus('已载入 ' + runId + '，证据完整性 ' + fn((res.report.integrity && res.report.integrity.passed), 0) + '/' + fn((res.report.integrity && res.report.integrity.total), 0) + '。', 'ok');
      return res.report;
    } catch (error) {
      if (requestSeq !== _reportRequestSeq || error.name === 'AbortError') return null;
      if ($('slabResult')) $('slabResult').innerHTML = '<div class="slab-empty">' + esc(error.message || '报告读取失败') + '</div>';
      resetForwardPanel('历史报告读取失败，无法绑定纸面前向状态。');
      setStatus(error.message || '报告读取失败', 'error');
      return null;
    } finally {
      if (_reportAbort === controller) _reportAbort = null;
    }
  }

  window._labViewRun = async function (runId) {
    _historyFollowLatest = false;
    _latestRunId = runId;
    const url = new URL(window.location.href);
    url.searchParams.set('run', runId);
    url.hash = 'slabEvidenceLayer';
    window.history.replaceState(null, '', url);
    syncActiveLayer();
    if ($('slabCurrentEvidence')) $('slabCurrentEvidence').open = true;
    return loadEvidenceReport(runId, true);
  };

  window._labEvidenceToggle = function (details) {
    if (details && details.open && _latestRunId) loadEvidenceReport(_latestRunId, false);
  };

  /* ═══ 历史报告曲线 + 独立纸面前向曲线 ═══ */
  function curvePoints(report) {
    const diagnostics = (report && report.diagnostics) || {};
    return diagnostics.equity_curve && diagnostics.equity_curve.test || [];
  }

  function normalizeCurvePoints(points) {
    let peak = 1;
    return (points || []).map(function (row) {
      const nav = row.nav == null ? NaN : Number(row.nav);
      const benchmark = row.benchmark_nav == null ? NaN : Number(row.benchmark_nav);
      peak = Math.max(peak, isFinite(nav) ? nav : 1);
      const calculated = (isFinite(nav) ? nav / peak - 1 : 0) * 100;
      const reported = Number(row.drawdown_pct != null ? row.drawdown_pct : row.drawdown);
      return {
        date: String(row.date || ''),
        nav: isFinite(nav) ? nav : null,
        benchmark_nav: isFinite(benchmark) ? benchmark : null,
        // 旧报告首期回撤曾遗漏初始 NAV=1；展示层只在口径冲突时按净值纠正，不改原证据文件。
        drawdown_pct: isFinite(reported) && Math.abs(reported - calculated) < 0.01 ? reported : calculated,
      };
    });
  }

  function updateHistoricalCurveText(message, tone) {
    const el = $('slabCurveSync');
    if (!el) return;
    el.textContent = message;
    el.className = tone === 'error' ? 'c-bad' : tone === 'ok' ? 'c-ok' : 'c-dim';
    el.style.fontSize = '12px';
  }

  function curveOption(rows, ariaDescription) {
    const hasBenchmark = rows.some(function (row) { return row.benchmark_nav != null; });
    const legend = ['策略净值'];
    if (hasBenchmark) legend.push('上证基准');
    legend.push('回撤');
    const series = [
      { name: '策略净值', type: 'line', xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, data: rows.map(function (row) { return row.nav; }), lineStyle: { width: 2.5 } },
    ];
    if (hasBenchmark) series.push({ name: '上证基准', type: 'line', xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, data: rows.map(function (row) { return row.benchmark_nav; }), lineStyle: { width: 2 } });
    series.push({ name: '回撤', type: 'line', xAxisIndex: 1, yAxisIndex: 1, showSymbol: false, data: rows.map(function (row) { return row.drawdown_pct; }), lineStyle: { width: 1.5 }, areaStyle: { opacity: 0.22 } });
    return {
      animation: false,
      aria: { enabled: true, description: ariaDescription },
      color: hasBenchmark ? ['#0A84FF', '#F59E0B', '#22C55E'] : ['#0A84FF', '#22C55E'],
      legend: { top: 0, textStyle: { color: '#9CA3AF', fontSize: 12 }, data: legend },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: function (items) {
          if (!items || !items.length) return '';
          const index = items[0].dataIndex;
          const row = rows[index] || {};
          return '<strong>' + esc(row.date) + '</strong><br>'
            + '策略净值：' + fn(row.nav, 4) + '<br>'
            + (hasBenchmark ? '上证基准：' + fn(row.benchmark_nav, 4) + '<br>' : '')
            + '回撤：' + fn(row.drawdown_pct, 2) + '%';
        },
      },
      grid: [
        { left: 54, right: 30, top: 34, height: 190 },
        { left: 54, right: 30, top: 252, height: 72 },
      ],
      xAxis: [
        { type: 'category', gridIndex: 0, data: rows.map(function (row) { return row.date; }), axisLabel: { show: false }, axisLine: { lineStyle: { color: '#374151' } } },
        { type: 'category', gridIndex: 1, data: rows.map(function (row) { return row.date; }), axisLabel: { color: '#9CA3AF', fontSize: 12, hideOverlap: true }, axisLine: { lineStyle: { color: '#374151' } } },
      ],
      yAxis: [
        { type: 'value', gridIndex: 0, scale: true, axisLabel: { color: '#9CA3AF', formatter: function (v) { return Number(v).toFixed(2); } }, splitLine: { lineStyle: { color: 'rgba(156,163,175,.12)' } } },
        { type: 'value', gridIndex: 1, max: 0, axisLabel: { color: '#9CA3AF', formatter: '{value}%' }, splitLine: { lineStyle: { color: 'rgba(156,163,175,.10)' } } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], filterMode: 'none' },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 16, borderColor: '#374151', textStyle: { color: '#9CA3AF' } },
      ],
      series: series,
    };
  }

  function bindCurveResize() {
    if (_curveResizeBound) return;
    window.addEventListener('resize', function () {
      if (_curveResizeFrame) cancelAnimationFrame(_curveResizeFrame);
      _curveResizeFrame = requestAnimationFrame(function () {
        _curveResizeFrame = 0;
        [_curveChart, _forwardChart].forEach(function (chart) {
          if (!chart || (chart.isDisposed && chart.isDisposed())) return;
          try { chart.resize(); }
          catch (error) { console.error('[strategy-lab:chart-resize]', error); }
        });
      });
    });
    _curveResizeBound = true;
  }

  function drawHistoricalCurve(points, meta) {
    const el = $('slabCurveChart');
    if (!el || !points || points.length < 2) return false;
    if (!window.echarts) {
      updateHistoricalCurveText('本地图表运行时未加载，历史数据仍保留在报告中。', 'error');
      return false;
    }
    const rows = normalizeCurvePoints(points);
    if (_curveChart) disposeChart(_curveChart, 'historical');
    _curveChart = window.echarts.init(el, null, { renderer: 'canvas' });
    _curveChart.setOption(curveOption(rows, '历史确认段策略净值、上证基准与策略回撤交互图'));
    el.dataset.sourceUpdatedAt = String((meta && meta.source_updated_at) || (meta && meta.generated_at) || '');
    bindCurveResize();
    return true;
  }

  function drawForwardCurve(points, version) {
    const el = $('slabForwardChart');
    if (!el || !points || points.length < 2 || !window.echarts) return false;
    const rows = normalizeCurvePoints(points);
    let zoom = null;
    if (_forwardChart) {
      const current = _forwardChart.getOption();
      const currentZoom = current && current.dataZoom && current.dataZoom[0];
      if (currentZoom) zoom = { start: currentZoom.start, end: currentZoom.end };
    } else {
      _forwardChart = window.echarts.init(el, null, { renderer: 'canvas' });
    }
    const option = curveOption(rows, '纸面前向策略净值、可用上证基准与策略回撤交互图');
    if (zoom && zoom.start != null && zoom.end != null) option.dataZoom.forEach(function (item) { item.start = zoom.start; item.end = zoom.end; });
    _forwardChart.setOption(option, { notMerge: true, lazyUpdate: true });
    el.dataset.version = String(version || '');
    bindCurveResize();
    return true;
  }

  function stopForwardSync(resetRun) {
    if (_forwardTimer) clearTimeout(_forwardTimer);
    if (_forwardAbort) _forwardAbort.abort();
    _forwardTimer = null;
    _forwardAbort = null;
    _forwardRequestSeq += 1;
    if (resetRun) _forwardRunId = null;
  }

  function resetForwardPanel(message) {
    _forwardState = null;
    _forwardVersion = null;
    if (_forwardChart) { disposeChart(_forwardChart, 'forward'); _forwardChart = null; }
    if ($('slabForwardBadge')) $('slabForwardBadge').textContent = '未选择报告';
    if ($('slabForwardSummary')) $('slabForwardSummary').textContent = message || '选择一份回测报告后，才会按 run_id 核对其纸面前向状态。';
    if ($('slabForwardStatus')) $('slabForwardStatus').textContent = '—';
    if ($('slabForwardUpdated')) $('slabForwardUpdated').textContent = '—';
    if ($('slabForwardAsOf')) $('slabForwardAsOf').textContent = '—';
    if ($('slabForwardSourceVersion')) $('slabForwardSourceVersion').textContent = '—';
    if ($('slabForwardCounts')) $('slabForwardCounts').textContent = '0 / 0 / 0';
    if ($('slabForwardBlockers')) { $('slabForwardBlockers').hidden = true; $('slabForwardBlockers').textContent = ''; }
    if ($('slabForwardNextAction')) $('slabForwardNextAction').textContent = '下一步：先选择一份当前口径的回测报告。';
    if ($('slabForwardError')) { $('slabForwardError').hidden = true; $('slabForwardError').textContent = ''; }
    if ($('slabForwardChartWrap')) $('slabForwardChartWrap').hidden = true;
    if ($('slabForwardObserve')) { $('slabForwardObserve').hidden = true; $('slabForwardObserve').disabled = true; $('slabForwardObserve').dataset.promotionAction = ''; }
    if ($('slabForwardSync')) $('slabForwardSync').textContent = '尚未读取前向服务。';
  }

  function forwardStatusMeta(state) {
    const status = state && state.current_epoch_eligible === false ? 'invalidated' : String((state && state.status) || 'off');
    const rows = {
      off: ['未开启观察', 'OFF', '尚未为该报告冻结前向信号。'],
      waiting_for_new_panel: ['等待新面板', '等待新增数据', '已冻结观察规格，等待 seal 后的新面板。'],
      waiting_for_label: ['等待标签成熟', '等待结算', '已有冻结信号，标签尚未成熟。'],
      active: ['正在纸面前向观察', 'ACTIVE · 仅纸面', '只结算冻结后产生的信号，不回填历史。'],
      degraded: ['前向服务异常', 'DEGRADED', '服务已失败关闭；保留上次有效证据，等待下一次检查。'],
      invalidated: ['已失效', 'INVALIDATED', '报告、规格或研究 epoch 已失效。'],
    };
    return { status: status, values: rows[status] || ['状态未识别', status || 'UNKNOWN', '服务返回了未识别的前向状态。'] };
  }

  function forwardBlockerText(rows) {
    return (rows || []).map(function (row) {
      return typeof row === 'string' ? row : (row && (row.text || row.message || row.code));
    }).filter(Boolean);
  }

  function forwardNextActionText(action, fallback) {
    if (!action) return fallback;
    if (typeof action === 'string') return action;
    return action.text || action.message || action.code || fallback;
  }

  function renderForwardUnavailable() {
    resetForwardPanel('当前本地服务尚未提供该报告的纸面前向读取接口；历史曲线不会冒充前向结果。');
    if ($('slabForwardBadge')) $('slabForwardBadge').textContent = '服务未接入';
    if ($('slabForwardStatus')) $('slabForwardStatus').textContent = '不可用';
    if ($('slabForwardNextAction')) $('slabForwardNextAction').textContent = '下一步：等待本地前向服务启用后再核对；不要用历史报告补画。';
    if ($('slabForwardSync')) $('slabForwardSync').textContent = '已完成能力探测；未创建任何前向点。';
  }

  function renderForwardState(state) {
    _forwardState = state;
    const meta = forwardStatusMeta(state);
    const counts = state.counts || {};
    const points = Array.isArray(state.points) ? state.points : [];
    const blockers = forwardBlockerText(state.blockers);
    const contractSafe = state.paper_only === true
      && state.mode === 'forward_shadow_label_continuation'
      && state.recommendation_eligible === false
      && state.promotion === false
      && state.real_time === false
      && state.orders === false
      && state.automatic_trading === false
      && state.broker_connected === false
      && state.unlocked === false
      && state.counts_toward_promotion === false
      && state.no_backfill === true
      && state.immutable_events === true;
    if (!contractSafe) blockers.push('前向安全契约未完整确认；该快照只可查看，绝不授予晋升资格。');
    const version = state.version || state.source_version || state.updated_at || (points.length + ':' + ((points[points.length - 1] || {}).date || ''));

    if ($('slabForwardBadge')) $('slabForwardBadge').textContent = meta.values[1];
    if ($('slabForwardSummary')) $('slabForwardSummary').textContent = meta.values[2] + (points.length < 2 ? ' 当前只有 ' + points.length + ' 个真实点，不绘制曲线。' : ' 已有 ' + points.length + ' 个真实点。');
    if ($('slabForwardStatus')) $('slabForwardStatus').textContent = meta.values[0];
    if ($('slabForwardUpdated')) $('slabForwardUpdated').textContent = fmtStamp(state.updated_at);
    if ($('slabForwardAsOf')) $('slabForwardAsOf').textContent = fmtStamp(state.data_as_of);
    if ($('slabForwardSourceVersion')) $('slabForwardSourceVersion').textContent = String(state.source_version || state.version || '未记录');
    if ($('slabForwardCounts')) $('slabForwardCounts').textContent = Number(counts.signals || 0) + ' / ' + Number(counts.pending || 0) + ' / ' + Number(counts.settled || 0);
    if ($('slabForwardBlockers')) {
      $('slabForwardBlockers').hidden = blockers.length === 0;
      $('slabForwardBlockers').textContent = blockers.length ? '当前证据缺口：' + blockers.join('；') : '';
    }
    if ($('slabForwardNextAction')) {
      const fallback = meta.status === 'off' ? '开启纸面前向观察并冻结当前报告。' : '等待下一次真实服务快照。';
      $('slabForwardNextAction').textContent = '下一步：' + forwardNextActionText(state.next_action, fallback);
    }
    if ($('slabForwardError')) { $('slabForwardError').hidden = true; $('slabForwardError').textContent = ''; }

    const observe = $('slabForwardObserve');
    if (observe) {
      const canToggle = !!(_activeReport && _activeReport.strategy_id) && ['off', 'waiting_for_new_panel', 'waiting_for_label', 'active', 'degraded'].indexOf(meta.status) >= 0;
      observe.hidden = !canToggle;
      observe.disabled = !canToggle;
      observe.dataset.promotionAction = meta.status === 'off' ? 'paper_observe_on' : 'paper_observe_off';
      observe.textContent = meta.status === 'off' ? '开启纸面前向观察' : '取消纸面前向观察';
    }

    const wrap = $('slabForwardChartWrap');
    if (wrap) wrap.hidden = points.length < 2;
    if (points.length >= 2) {
      if (!window.echarts) {
        if (wrap) wrap.hidden = true;
        if ($('slabForwardError')) { $('slabForwardError').hidden = false; $('slabForwardError').textContent = '本地图表运行时未加载；前向点仍保留在服务快照中。'; }
        if ($('slabForwardSync')) $('slabForwardSync').textContent = '前向数据已读取，但本地图表运行时不可用。';
      } else {
        if (_forwardVersion !== String(version) || !_forwardChart) drawForwardCurve(points, version);
        _forwardVersion = String(version);
        if ($('slabForwardSync')) $('slabForwardSync').textContent = '服务端快照已核对 · 状态更新 ' + fmtStamp(state.updated_at) + ' · 数据截至 ' + fmtStamp(state.data_as_of) + ' · 最快每 30 秒刷新';
      }
    } else if ($('slabForwardSync')) {
      $('slabForwardSync').textContent = '服务端返回 ' + points.length + ' 个真实点；继续等待冻结信号结算，不补造或回填曲线。';
    }
  }

  function renderForwardError(error) {
    const hasSnapshot = !!_forwardState;
    if ($('slabForwardBadge')) $('slabForwardBadge').textContent = hasSnapshot ? 'STALE · 刷新失败' : '读取失败';
    if ($('slabForwardSummary')) $('slabForwardSummary').textContent = hasSnapshot ? '刷新失败，保留并明确标记上次服务快照。' : '前向状态读取失败，未生成任何曲线。';
    if ($('slabForwardError')) {
      $('slabForwardError').hidden = false;
      $('slabForwardError').textContent = '前向服务错误：' + String((error && error.message) || error || '未知错误');
    }
    if ($('slabForwardSync')) $('slabForwardSync').textContent = '自动刷新失败；30 秒后重试。';
  }

  function scheduleForwardSync(runId, seconds) {
    if (!runId || runId !== _forwardRunId || document.hidden) return;
    if (_forwardTimer) clearTimeout(_forwardTimer);
    const delay = Math.max(30, Math.min(Number(seconds) || 30, 300)) * 1000;
    _forwardTimer = setTimeout(function () { _forwardTimer = null; syncForward(runId); }, delay);
  }

  function forwardShouldPoll(state) {
    return state && ['waiting_for_new_panel', 'waiting_for_label', 'active', 'degraded'].indexOf(String(state.status || '')) >= 0
      && state.current_epoch_eligible !== false;
  }

  async function syncForward(runId) {
    if (!runId || runId !== _forwardRunId || document.hidden || _forwardAbort) return;
    const requestSeq = ++_forwardRequestSeq;
    const controller = new AbortController();
    _forwardAbort = controller;
    if (!_forwardState && $('slabForwardSummary')) {
      $('slabForwardBadge').textContent = '读取中';
      $('slabForwardSummary').textContent = '正在按 run_id 读取纸面前向状态…';
      $('slabForwardSync').textContent = '正在核对服务能力与最新快照…';
    }
    try {
      const payload = await requestJSON('/api/strategy-lab/user-backtests/' + encodeURIComponent(runId) + '/forward', { cache: 'no-store', signal: controller.signal }, 8000);
      if (requestSeq !== _forwardRequestSeq || runId !== _forwardRunId) return;
      if (!payload.ok || !payload.forward) throw new Error(payload.error || '前向接口异常');
      const state = payload.forward;
      if (state.run_id && state.run_id !== runId) throw new Error('前向响应 run_id 与所选报告不一致');
      if (_activeReport && _activeReport.strategy_id && state.strategy_id && state.strategy_id !== _activeReport.strategy_id) throw new Error('前向响应 strategy_id 与所选报告不一致');
      renderForwardState(state);
      if (forwardShouldPoll(state)) scheduleForwardSync(runId, state.refresh_after_seconds);
    } catch (error) {
      if (requestSeq !== _forwardRequestSeq || error.name === 'AbortError') return;
      if (error.status === 404) {
        renderForwardUnavailable();
        return;
      }
      renderForwardError(error);
      scheduleForwardSync(runId, 30);
    } finally {
      if (_forwardAbort === controller) _forwardAbort = null;
    }
  }

  window._labForwardObserve = async function () {
    const report = _activeReport;
    const button = $('slabForwardObserve');
    const action = button && button.dataset.promotionAction;
    if (!report || !report.run_id || !report.strategy_id || !action) return setStatus('请先选择绑定已保存策略的回测报告。', 'error');
    button.disabled = true;
    button.textContent = action === 'paper_observe_on' ? '正在开启观察…' : '正在取消观察…';
    if ($('slabForwardSummary')) $('slabForwardSummary').textContent = '正在提交纸面观察状态；不会触发交易或晋升。';
    const res = await window._labPromotion(report.strategy_id, action, report.run_id);
    if (res && res._httpOk && _activeReport && _activeReport.run_id === report.run_id) {
      stopForwardSync(false);
      _forwardRunId = report.run_id;
      await syncForward(report.run_id);
    } else if (_activeReport && _activeReport.run_id === report.run_id) {
      if (_forwardState) renderForwardState(_forwardState);
      else {
        button.disabled = false;
        button.textContent = action === 'paper_observe_on' ? '开启纸面前向观察' : '取消纸面前向观察';
        if ($('slabForwardSummary')) $('slabForwardSummary').textContent = '纸面观察提交失败；未假定服务状态，也未生成任何曲线。';
      }
    }
  };

  function renderReport(report, reveal) {
    stopForwardSync(true);
    _activeReport = report;
    resetForwardPanel('正在绑定所选报告的纸面前向状态…');
    if (reveal && $('slabCurrentEvidence')) $('slabCurrentEvidence').open = true;
    if ($('slabEvidenceBadge')) $('slabEvidenceBadge').textContent = report && report.current_epoch_eligible === false
      ? 'INVALIDATED · 旧窗口档案'
      : (report && report.current_epoch_eligible === true ? '当前口径 · 历史证据' : '口径未确认 · 历史档案');
    $('slabResult').innerHTML = resultHtml(report);
    const points = curvePoints(report);
    requestAnimationFrame(function () {
      if (points.length >= 2) {
        if (drawHistoricalCurve(points, report)) {
          updateHistoricalCurveText(
            (_historyFollowLatest ? '实时监听最新完成报告' : '固定查看所选历史报告')
            + ' · 当前 ' + ((report && report.run_id) || 'run 未记录')
            + ' · 生成于 ' + fmtStamp(report && report.generated_at),
            'ok'
          );
        }
      } else {
        updateHistoricalCurveText('历史报告只有 ' + points.length + ' 个曲线点，点数不足，未绘图。', '');
      }
      if (report && report.run_id) {
        _forwardRunId = report.run_id;
        syncForward(report.run_id);
      }
    });
  }

  function resultHtml(report) {
    if (!report) return '<div class="slab-empty">保存或运行你的第一条策略后，开发/验证/测试结果会显示在这里。</div>';
    const metrics = report.metrics || {};
    const development = metrics.development || {};
    const validation = metrics.validation || {};
    const test = metrics.test || {};
    const diagnostic = metrics.diagnostic_2026 || {};
    const currentEpoch = report.current_epoch_eligible === true;
    const passed = currentEpoch && !!report.historical_validation_gate_passed;
    const failures = report.failed_gates || [];
    const evidence = report.integrity || {};
    const diagnostics = report.diagnostics || {};
    const curve = diagnostics.equity_curve && diagnostics.equity_curve.test || [];
    const costs = diagnostics.cost_ladder && diagnostics.cost_ladder.test || [];
    const costBase = report.quantqc_cost_baseline || null;
    const factorIc = diagnostics.factor_ic && diagnostics.factor_ic.test || [];
    const folds = report.test_folds || [];
    const yearly = Object.entries(test.yearly_total_return_pct || {});
    const assistant = report.assistant || {};
    const actualSplit = report.splits && report.splits.test || [];
    const canonicalSplit = (_wb && _wb.fixed_splits && _wb.fixed_splits.test) || [];
    const splitText = actualSplit.length === 2 ? actualSplit[0] + ' ~ ' + actualSplit[1] : '未记录';
    const canonicalText = canonicalSplit.length === 2 ? canonicalSplit[0] + ' ~ ' + canonicalSplit[1] : '未记录';
    const costCells = [
      ['历史确认年化', test.annualized_return_pct], ['历史确认胜率', test.period_win_rate_pct == null ? null : test.period_win_rate_pct * 1],
      ['历史确认最大回撤', test.max_drawdown_pct], ['历史确认 Sharpe', test.sharpe_ratio],
      ['历史确认累计', test.total_return_pct], ['验证累计', validation.total_return_pct],
      ['发现累计', development.total_return_pct], ['双倍成本历史确认', test.cost_stress_total_return_pct],
      ['既有2026诊断(非盲测)', diagnostic.total_return_pct], ['历史确认 Sortino', test.sortino_ratio],
      ['单期 CVaR 95%', test.period_cvar_95_pct], ['平均单边换手', test.average_one_way_turnover_pct == null ? null : test.average_one_way_turnover_pct * 1],
    ].map(function (c) {
      return '<div class="slab-metric"><span class="v ' + cc(c[1]) + '">' + (c[1] == null ? '—' : (c[0].indexOf('胜率') >= 0 || c[0].indexOf('换手') >= 0 ? fn(c[1], 1) + '%' : c[0].indexOf('Sharpe') >= 0 || c[0].indexOf('Sortino') >= 0 ? fn(c[1], 2) : fpp(c[1]))) + '</span><span class="l">' + c[0] + '</span></div>';
    }).join('');

    markFlow('verdict', !currentEpoch ? 'INVALIDATED · 旧口径' : (passed ? 'HISTORICAL_PASS · 仅研究' : 'NO-GO'));
    return '<div style="display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">'
      + '<div><strong>' + esc((report.spec && report.spec.name) || report.name || '用户策略') + '</strong>'
      + '<div class="c-dim" style="font-size:12px;margin-top:3px">' + esc(report.run_id || '最近运行') + ' · 证据 ' + fn(evidence.passed, 0) + '/' + fn(evidence.total, 0) + ' · 历史验证不会自动进入买入提醒</div></div>'
      + '<div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">'
      + (report.run_id ? '<button type="button" class="slab-btn" style="padding:6px 12px" data-action="assistant">本地智能诊断</button><button type="button" class="slab-btn" style="padding:6px 12px" data-action="unified-diag">统一引擎专业诊断</button><button type="button" class="slab-btn" style="padding:6px 12px" data-action="export-report">导出 JSON 证据</button>' : '')
      + '<span style="padding:6px 9px;border-radius:8px;background:var(--bg-elevated);font-size:var(--text-xs)" class="' + (passed ? 'c-ok' : 'c-bad') + '">' + (!currentEpoch ? '旧研究口径 · 已失效' : (passed ? '历史门禁通过 · 仍仅研究' : '历史门禁未通过')) + '</span>'
      + '</div></div>'
      + lineageFlowHtml(report.spec || {})
      + '<div class="slab-metric-grid" style="margin-bottom:12px">' + costCells + '</div>'
      + '<div style="padding:9px 11px;border-left:3px solid ' + (passed ? '#22c55e' : '#ef4444') + ';background:var(--bg-elevated);font-size:var(--text-xs);line-height:1.55;margin-bottom:12px">'
      + (!currentEpoch
        ? '<strong>INVALIDATED · 该报告窗口为 ' + esc(splitText) + '</strong><div style="margin-top:5px">当前严格历史确认窗口为 ' + esc(canonicalText) + '。原始报告保持不可变，但不计入当前 PASS。</div>'
        : (failures.length ? '<strong>NO-GO · 未通过 ' + failures.length + ' 项</strong><div style="margin-top:5px">' + failures.map(function (failure) { return '<span class="slab-chip c-neg">' + esc(formatFailure(failure)) + '</span>'; }).join(' ') + '</div>' : '历史验证全部通过；仍需锁定规格、独立复现和前向纸面观察。')) + '</div>'
      + '<div style="padding:12px;background:var(--bg-elevated);border:1px solid var(--border-subtle);border-radius:10px;margin-bottom:12px">'
      + '<div style="display:flex;justify-content:space-between;gap:8px;margin-bottom:4px;flex-wrap:wrap"><strong style="font-size:var(--text-sm)">历史确认段净值与回撤 · ' + (_historyFollowLatest ? '实时同步最新报告' : '固定历史报告交互图') + '</strong><span class="c-dim" style="font-size:12px">策略净值 <span style="color:#0A84FF">━━</span> · 上证基准 <span style="color:#F59E0B">━━</span> · 回撤 <span style="color:#22C55E">▰</span></span></div>'
      + '<div class="c-dim" style="font-size:12px;line-height:1.5">实际窗口 ' + esc(splitText) + ' · 生成于 ' + esc(fmtStamp(report.generated_at)) + '。可悬停、缩放；' + (_historyFollowLatest ? '页面每 30 秒检查最新完成的 run 并自动切换。' : '当前按所选 run 固定查看，不自动切换。') + '固定历史窗口不是盘中持仓净值。</div>'
      + (curve.length >= 2
        ? '<div id="slabCurveChart" role="img" aria-label="历史确认段策略净值、上证基准与回撤交互图" style="width:100%;height:350px;margin-top:6px"></div>'
        : '<div class="slab-empty" id="slabCurveEmpty">历史报告只有 ' + curve.length + ' 个曲线点，点数不足，未绘图。</div>')
      + '<div id="slabCurveSync" class="c-dim" aria-live="polite" style="font-size:12px;margin-top:5px">实时监听最新完成报告；当前 run 的证据内容保持不可变。</div></div>'
      + '<details class="slab-inline-disclosure"><summary>展开执行、年度与成本明细</summary><div>'
      + '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;margin-bottom:12px">'
      + '<div style="padding:11px;background:var(--bg-elevated);border-radius:10px"><strong style="font-size:var(--text-sm)">执行与容量</strong><div class="slab-metric-grid" style="grid-template-columns:1fr 1fr;margin-top:8px">'
      + '<div class="slab-metric"><span class="v">' + (test.filled_execution_rate_pct == null ? '—' : fn(test.filled_execution_rate_pct, 1) + '%') + '</span><span class="l">目标标签覆盖</span></div>'
      + '<div class="slab-metric"><span class="v">' + (test.maximum_position_weight_pct == null ? '—' : fn(test.maximum_position_weight_pct, 1) + '%') + '</span><span class="l">单票权重</span></div>'
      + '<div class="slab-metric"><span class="v">' + (test.median_estimated_capacity_cny == null ? '—' : '¥' + fn(test.median_estimated_capacity_cny / 10000, 0) + '万') + '</span><span class="l">容量中位估算</span></div>'
      + '<div class="slab-metric"><span class="v">' + (test.risk_on_rate_pct == null ? '—' : fn(test.risk_on_rate_pct, 1) + '%') + '</span><span class="l">风险开启比例</span></div>'
      + '</div></div>'
      + '<div style="padding:11px;background:var(--bg-elevated);border-radius:10px"><strong style="font-size:var(--text-sm)">年度与时间折</strong><div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:9px">'
      + (yearly.length ? yearly.map(function (kv) { return '<span class="' + cc(kv[1]) + '" style="padding:5px 7px;background:var(--bg-card);border-radius:7px;font-size:var(--text-xs)">' + esc(kv[0]) + ' ' + fpp(kv[1]) + '</span>'; }).join('') : '<span class="c-dim">无完整年度</span>')
      + '</div><div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">'
      + (folds.map(function (row) { return '<span class="' + (row.total_return_pct > 0 && row.excess_return_pct > 0 ? 'c-ok' : 'c-bad') + '" style="padding:5px 7px;background:var(--bg-card);border-radius:7px;font-size:var(--text-xs)">折' + fn(row.fold, 0) + ' ' + fpp(row.total_return_pct) + ' / 超额 ' + fpp(row.excess_return_pct) + '</span>'; }).join(''))
      + '</div></div></div>'
      + (costs.length ? '<div style="overflow:auto;margin-bottom:12px"><table class="slab-table"><thead><tr><th>单边滑点（佣金/印花税/过户费另加）</th><th>历史确认累计</th><th>历史确认年化</th><th>最大回撤</th></tr></thead><tbody>' + costs.map(function (row) { return '<tr><td class="num">' + fn(row.one_way_cost_bps, 0) + ' BP</td><td class="num ' + cc(row.total_return_pct) + '">' + fpp(row.total_return_pct) + '</td><td class="num ' + cc(row.annualized_return_pct) + '">' + fpp(row.annualized_return_pct) + '</td><td class="num c-down">' + fpp(row.max_drawdown_pct) + '</td></tr>'; }).join('') + '</tbody></table></div>' : '')
      + (costBase ? '<div style="padding:11px;border:1px solid var(--border-subtle);background:var(--bg-elevated);border-radius:10px;margin-bottom:12px"><strong style="font-size:var(--text-sm)">成本口径（quantqc 同花顺）</strong><div class="slab-metric-grid" style="margin-top:8px">'
        + '<div class="slab-metric"><span class="v">' + (costBase.round_trip_bp == null ? '—' : fn(costBase.round_trip_bp, 1) + ' BP') + '</span><span class="l">组合等权往返成本</span></div>'
        + '<div class="slab-metric"><span class="v">' + (costBase.one_way_bp_equivalent == null ? '—' : fn(costBase.one_way_bp_equivalent, 1) + ' BP') + '</span><span class="l">单边等效</span></div>'
        + '<div class="slab-metric"><span class="v">¥' + fn(costBase.capital, 0) + '</span><span class="l">本金 / ' + fn(costBase.positions, 0) + ' 仓</span></div></div>'
        + '<div style="margin-top:8px;font-size:12px;line-height:1.6" class="c-dim">' + esc(costBase.note || '') + '</div>'
        + '<div style="margin-top:6px;font-size:12px;line-height:1.6" class="c-dim">执行口径：' + esc(costBase.execution || '') + '</div></div>' : '')
      + '</div></details>'
      + '<div id="slabAssistant">' + assistantHtml(assistant) + '</div>';
  }

  /* ═══ 单变量实验：只配对不可变诊断证据，不产生综合判定 ═══ */
  function experimentSpec(spec) {
    const factors = {};
    Object.keys((spec && spec.factors) || {}).sort().forEach(function (key) {
      const value = Number(spec.factors[key]);
      if (value !== 0) factors[key] = value;
    });
    return {
      holding_days: Number((spec && spec.holding_days) || 5),
      top_n: Number((spec && spec.top_n) || 30),
      one_way_cost_bps: Number((spec && spec.one_way_cost_bps) ?? 0),
      rebal_buffer: Number((spec && spec.rebal_buffer) ?? 3),
      risk_rule: (spec && spec.risk_rule) || 'none',
      entry_rule: (spec && spec.entry_rule) || 'none',
      exposure_rule: (spec && spec.exposure_rule) || 'none',
      account_capital: Number((spec && spec.account_capital) ?? 200000),
      compose_method: (spec && spec.compose_method) || 'rank_weighted_v1',
      factors: factors,
    };
  }
  function experimentLeafChanges(before, after, prefix) {
    let rows = [];
    const keys = Array.from(new Set(Object.keys(before || {}).concat(Object.keys(after || {})))).sort();
    keys.forEach(function (key) {
      const path = prefix ? prefix + '.' + key : key;
      const left = before && before[key], right = after && after[key];
      if (left && right && typeof left === 'object' && typeof right === 'object' && !Array.isArray(left) && !Array.isArray(right)) {
        rows = rows.concat(experimentLeafChanges(left, right, path));
      } else if (left !== right) {
        rows.push({ path: path, before: left, after: right });
      }
    });
    return rows;
  }
  function experimentPathLabel(path) {
    const labels = {
      holding_days: '持有期', top_n: '持仓数量', one_way_cost_bps: '单边滑点',
      rebal_buffer: '换仓缓冲', risk_rule: '风险规则', entry_rule: '买点规则', exposure_rule: '连续市场波动目标仓位',
      account_capital: '资金量', compose_method: '因子合成',
    };
    if (path.indexOf('factors.') === 0) return '因子 ' + factorLabel(path.slice(8));
    return labels[path] || path;
  }
  function updateExperimentDraft() {
    const state = $('slabExperimentState');
    const baselineButton = $('slabExperimentBaseline');
    const runButton = $('slabExperimentRun');
    if (baselineButton) baselineButton.disabled = !(_lastDiagnosis && _lastDiagnosis.evidence && _lastDiagnosis.strategyId);
    if (!_experimentBaseline) {
      if (runButton) runButton.disabled = true;
      if (state) state.textContent = _lastDiagnosis && _lastDiagnosis.evidence
        ? '当前诊断已有不可变证据，可以设为实验基线。'
        : '先保存并完成一次统一诊断，再把当前诊断设为基线。';
      return;
    }
    const changes = experimentLeafChanges(
      experimentSpec(_experimentBaseline.spec), experimentSpec(collectSpec()), ''
    );
    if (runButton) {
      runButton.disabled = !_wb || _wb.available !== true || changes.length !== 1;
    }
    if (!state) return;
    if (changes.length === 1) {
      const change = changes[0];
      state.textContent = '基线 ' + _experimentBaseline.strategyId + ' · 唯一变化：'
        + experimentPathLabel(change.path) + '（' + String(change.before) + ' → ' + String(change.after) + '）';
    } else {
      state.textContent = '基线 ' + _experimentBaseline.strategyId + ' · 当前改变 '
        + changes.length + ' 个叶子字段；候选必须恰好只改一个。';
    }
  }
  function experimentResultHtml(experiment) {
    const labels = {
      annual_return_pct: '净年化（%）', shanghai_excess_pct: '上证超额（百分点）',
      sharpe: 'Sharpe', calmar: 'Calmar', max_drawdown_pct: '最大回撤（%）',
      realized_volatility_pct: '实现波动率（%）', win_rate_pct: '胜率（%）',
      total_fees_yuan: '总费用（元）', traded_notional_yuan: '成交额（元）',
      average_turnover: '平均换手', average_positions: '平均持仓数',
      average_gross_exposure_pct: '平均总仓位（%）', average_cash_pct: '平均现金（%）',
      minimum_cash_weight_pct: '最低现金（%）', maximum_single_weight_pct: '最大单票权重（%）',
      risk_on_dates: '风险开启日', risk_off_dates: '风险关闭日',
      entry_order_fill_rate_pct: '买入委托成交率（%）', entry_order_count: '买入委托数',
      entry_order_cancelled_count: '买入取消数', previous_close_limit_cancelled_count: '高开不追取消数',
    };
    const rows = Object.keys(experiment.metrics || {}).map(function (key) {
      const metric = experiment.metrics[key] || {};
      const delta = metric.delta == null ? '—' : (Number(metric.delta) > 0 ? '+' : '') + fn(metric.delta, 3);
      return '<tr><td>' + esc(labels[key] || key) + '</td><td class="num">' + fn(metric.baseline, 3)
        + '</td><td class="num">' + fn(metric.candidate, 3) + '</td><td class="num">' + esc(delta) + '</td></tr>';
    }).join('');
    const change = experiment.change || {};
    return '<div class="slab-note" style="margin-bottom:8px"><strong>唯一变化：</strong>'
      + esc(experimentPathLabel(change.path || '')) + ' · ' + esc(change.before) + ' → ' + esc(change.after)
      + '；以下 Δ 均为候选减基线。</div>'
      + '<div class="slab-note" style="margin-bottom:8px"><strong>冻结假设：</strong>'
      + esc(experiment.hypothesis_id || '未预注册') + ' · ' + esc(labels[experiment.expected_metric] || experiment.expected_metric || '—')
      + ' · ' + esc(experiment.direction || '—') + ' ≥ ' + esc(experiment.success_threshold)
      + '<br><strong>决策：</strong>' + esc(experiment.decision_category || 'legacy_descriptive_only')
      + '<br><strong>确证状态：</strong>' + esc(experiment.confirmation_status || 'legacy_unregistered')
      + ' · 成本 ' + esc(experiment.cost_seconds == null ? '未记录' : experiment.cost_seconds + '秒')
      + '<br>' + esc(experiment.next_action || '') + '</div>'
      + '<div style="overflow:auto"><table class="slab-table"><thead><tr><th>指标</th><th>基线</th><th>候选</th><th>Δ</th></tr></thead><tbody>'
      + rows + '</tbody></table></div>'
      + '<div class="slab-note" style="margin-top:8px">实验 ' + esc(experiment.experiment_id || '')
      + ' · 证据 SHA ' + esc((experiment.evidence_sha256 || '').slice(0, 16)) + '…</div>';
  }
  function renderRecentExperiments(rows) {
    const target = $('slabExperimentRecent');
    if (!target || !rows || !rows.length) return;
    target.innerHTML = '<details><summary class="slab-note" style="cursor:pointer">最近单变量实验 ' + rows.length + ' 条</summary>'
      + '<div style="margin-top:7px;display:grid;gap:5px">' + rows.slice(0, 8).map(function (row) {
        const change = row.change || {};
        return '<div class="slab-note"><span class="mono">' + esc(row.experiment_id || '') + '</span> · '
          + esc(experimentPathLabel(change.path || '')) + ' · ' + esc(change.before) + ' → ' + esc(change.after)
          + (row.decision_category ? ' · ' + esc(row.decision_category) : '')
          + (row.confirmation_status ? ' · ' + esc(row.confirmation_status) : '')
          + (row.cost_seconds != null ? ' · ' + esc(row.cost_seconds) + '秒' : '') + '</div>';
      }).join('') + '</div></details>';
  }
  window._labSetExperimentBaseline = function () {
    if (!_lastDiagnosis || !_lastDiagnosis.evidence || !_lastDiagnosis.strategyId) {
      return setStatus('当前诊断未绑定已保存策略，不能设为基线。', 'error');
    }
    _experimentBaseline = {
      evidence: _lastDiagnosis.evidence,
      strategyId: _lastDiagnosis.strategyId,
      spec: JSON.parse(JSON.stringify(_lastDiagnosis.spec)),
      hypothesis: _pendingExperimentHypothesis
        ? JSON.parse(JSON.stringify(_pendingExperimentHypothesis)) : null,
    };
    updateExperimentDraft();
    setStatus('已冻结当前诊断为单变量实验基线；现在只改一个字段。', 'ok');
  };
  window._labRunExperimentCandidate = async function () {
    if (!_wb || _wb.available !== true) {
      return setStatus('当前输入面板未就绪，档案仍可查看；不会运行实验候选。', 'error');
    }
    if (!_experimentBaseline) return setStatus('请先设置实验基线。', 'error');
    const spec = collectSpec();
    const changes = experimentLeafChanges(
      experimentSpec(_experimentBaseline.spec), experimentSpec(spec), ''
    );
    if (changes.length !== 1) return setStatus('候选必须恰好只改变一个叶子字段。', 'error');
    const button = $('slabExperimentRun');
    if (button) button.disabled = true;
    const hypothesis = _experimentBaseline.hypothesis
      ? JSON.parse(JSON.stringify(_experimentBaseline.hypothesis)) : null;
    if (!hypothesis) {
      updateExperimentDraft();
      return setStatus('缺少候选运行前的冻结假设；请先使用带实验假设的助手建议。', 'error');
    }
    setStatus('正在服务端冻结基线、候选规格、方向与阈值…');
    const registered = await apiWrite('/api/strategy-lab/experiments/register', {
      baseline_evidence: _experimentBaseline.evidence,
      candidate_spec: spec,
      hypothesis: hypothesis,
    });
    if (!registered._httpOk || !registered.registration || !registered.registration.registration_id) {
      updateExperimentDraft();
      return setStatus(registered.error || '实验先验登记失败；候选未运行。', 'error');
    }
    setStatus('先验登记已冻结；正在保存候选版本并运行统一诊断…');
    const savePayload = Object.assign({}, spec, { strategy_id: _experimentBaseline.strategyId });
    const saved = await apiWrite('/api/strategy-lab/user-strategies', savePayload);
    if (!saved._httpOk) { updateExperimentDraft(); return setStatus(saved.error || '候选保存失败', 'error'); }
    _currentStrategyId = saved.strategy.strategy_id;
    applySpec(saved.strategy.spec || spec, true);
    const diagnosed = await window._labUnifiedDiag(saved.strategy.spec || spec, _currentStrategyId);
    if (!diagnosed || !diagnosed._httpOk || !diagnosed.diagnosis_evidence) {
      updateExperimentDraft();
      return setStatus((diagnosed && diagnosed.error) || '候选诊断未生成不可变证据。', 'error');
    }
    const compared = await apiWrite('/api/strategy-lab/experiments', {
      registration_id: registered.registration.registration_id,
      candidate_evidence: diagnosed.diagnosis_evidence,
    });
    updateExperimentDraft();
    if (!compared._httpOk) return setStatus(compared.error || '单变量证据比较失败', 'error');
    $('slabExperimentResult').innerHTML = experimentResultHtml(compared.experiment || {});
    setStatus('历史样本比较完成；结果永久为探索性，等待独立前向确证。', 'ok');
    refreshWorkbench();
  };

  /* ═══ M3.2 统一引擎专业诊断 ═══ */
  const _BENCH_LABELS = { 'equal_weight_o2o': '等权（o2o）', 'sh000001': '上证指数', 'sh000300': '沪深300', 'sh000905': '中证500', 'sh000852': '中证1000' };
  window._labUnifiedDiag = async function (forcedSpec, forcedStrategyId, options) {
    const target = $('slabUnifiedDiag');
    const spec = forcedSpec || collectSpec();
    const strategyId = forcedStrategyId || _currentStrategyId || null;
    if (!_wb || _wb.available !== true) {
      setStatus('当前输入面板未就绪，档案仍可查看；不会提交统一诊断。', 'error');
      return null;
    }
    if (_strictPoolCount == null) {
      setStatus('来源、预注册与 PIT 门禁尚未核对完成；不会提交统一诊断。', 'error');
      return null;
    }
    if (_strictPoolCount === 0) {
      setStatus('当前没有来源门禁通过的可运行因子；不会提交统一诊断。', 'error');
      return null;
    }
    if (!spec.factors || !Object.values(spec.factors).some(function (w) { return Number(w) !== 0; })) {
      return setStatus('请先选择至少一个因子权重。', 'error');
    }
    if (_diagnosisLoading) return setStatus('统一引擎诊断仍在运行，请勿重复提交。', 'error');
    _diagnosisLoading = true;
    if (target) target.innerHTML = '<div class="c-dim" style="padding:14px;font-size:var(--text-xs)">统一引擎正在载入 factor_system 面板并跑发现/验证/历史确认全链（严格全截面通常需数分钟，完成后同规格缓存即时返回；期间请勿重复点击）…</div>';
    // 手工诊断只消费共享 LOND 并保存探索证据；HISTORICAL_PASS 只由固定批次产生。
    const res = await apiWrite('/api/strategy-lab/diagnose', {
      spec: spec,
      strategy_id: strategyId,
      publish_history_report: !!(options && options.publishHistoryReport),
    }, 2400000);
    _diagnosisLoading = false;
    if (!res._httpOk) { if (target) target.innerHTML = ''; setStatus(res.error || '统一引擎诊断失败', 'error'); return res; }
    if (target) target.innerHTML = unifiedDiagHtml(res.report, !!res.cached);
    const online = res.report.historical_online_test || {};
    const exploratory = online.verdict === 'EXPLORATORY';
    const verdict = exploratory
      ? 'EXPLORATORY（手工探索；仍需固定批次 BY-FDR）'
      : (online.verdict || 'NO-GO');
    const evidenceTxt = res.diagnosis_evidence
      ? '；已保存不可变探索证据，未写入晋升状态'
      : (strategyId ? '；未写入晋升状态' : '；未绑定已保存策略，仅展示');
    _lastDiagnosis = {
      evidence: res.diagnosis_evidence || null,
      strategyId: strategyId,
      spec: JSON.parse(JSON.stringify(spec)),
    };
    updateExperimentDraft();
    setStatus((res.cached ? '统一引擎诊断（缓存结果）：' : '统一引擎诊断完成：') + verdict + evidenceTxt + '。', exploratory ? 'ok' : 'error');
    return res;
  };
  function unifiedDiagHtml(report, cached) {
    const ev = report.evaluate || {};
    const gate = ev.gate || {};
    const online = report.historical_online_test || {};
    const diag = report.diagnose || {};
    const oos = diag.oos_metrics || {};
    const passed = online.verdict === 'EXPLORATORY';
    const verdict = passed
      ? 'EXPLORATORY · 手工探索，需固定批次 BY-FDR'
      : (online.verdict || 'NO-GO');
    const full = gate.full || {};
    const fullCycle = gate.full_cycle || {};
    const fcOk = !!fullCycle.ok;
    const fcRows = (fullCycle.yearly || []).map(function (y) {
      return '<tr><td class="num">' + esc(y.year) + '</td>'
        + '<td class="num ' + cc(y.strategy_annual) + '">' + fpct(y.strategy_annual) + '</td>'
        + '<td class="num ' + cc(y.shanghai_annual) + '">' + fpct(y.shanghai_annual) + '</td>'
        + '<td class="num ' + cc(y.excess_annual) + '">' + fpct(y.excess_annual) + '</td></tr>';
    }).join('');
    const dsr = diag.deflated_sharpe || {};
    const wf = diag.rolling_wf_ic || {};
    const sd = diag.signal_diagnostics_is || {};
    const mono = sd.monotonic || {};
    const icd = sd.ic_decay || {};
    const reg = sd.regime || {};
    const yearly = (reg.yearly || []).map(function (y) { return '<span class="' + cc(y.ic_mean) + '" style="padding:4px 7px;background:var(--bg-card);border-radius:7px;font-size:12px">' + esc(y.year) + ' IC ' + fn(y.ic_mean, 3) + '</span>'; }).join('');
    const hasRegistryFactor = ((ev.spec && ev.spec.factor_lineage) || []).some(function (row) { return row.source === 'factor_system.registry'; });
    const benchRows = Object.keys(diag.multi_benchmark || {}).map(function (k) {
      const b = diag.multi_benchmark[k];
      if (!b || b.basis == null) return '';
      return '<tr><td>' + esc(_BENCH_LABELS[k] || k) + '</td><td class="c-dim" style="font-size:12px">' + esc(b.basis) + '</td>'
        + '<td class="num ' + cc(b.strategy_annualized_pct) + '">' + fpp(b.strategy_annualized_pct) + '</td>'
        + '<td class="num ' + cc(b.benchmark_annualized_pct) + '">' + fpp(b.benchmark_annualized_pct) + '</td>'
        + '<td class="num ' + cc(b.excess_annual_pct) + '">' + fpp(b.excess_annual_pct) + '</td>'
        + '<td class="num">' + fn(b.corr_strategy_bench, 2) + '</td>'
        + '<td class="num c-down">' + fpp(b.strategy_max_drawdown_pct) + '</td>'
        + '<td class="num c-down">' + fpp(b.benchmark_max_drawdown_pct) + '</td></tr>';
    }).join('');
    const subRows = Object.keys(diag.sub_factor_decomposition || {}).map(function (k) {
      const s = diag.sub_factor_decomposition[k];
      return '<tr><td>' + esc(k) + '</td><td class="num">' + fn(s.weight, 1) + '</td>'
        + '<td class="num ' + cc(s.oos_annual_pct) + '">' + fpp(s.oos_annual_pct) + '</td>'
        + '<td class="num ' + cc(s.oos_t) + '">' + fn(s.oos_t, 2) + '</td>'
        + '<td class="num ' + cc(s.oos_excess_pct) + '">' + fpp(s.oos_excess_pct) + '</td>'
        + '<td class="num ' + cc(s.oos_ic) + '">' + fn(s.oos_ic, 3) + '</td>'
        + '<td class="num ' + cc(s.oos_ic_ir) + '">' + fn(s.oos_ic_ir, 2) + '</td></tr>';
    }).join('');
    const wfRows = (wf.folds || []).map(function (r) {
      return '<tr><td class="num mono">' + esc(r.val[0].slice(0, 10)) + ' ~ ' + esc(r.val[1].slice(0, 10)) + '</td>'
        + '<td class="num">' + fn(r.n_dates, 0) + '</td>'
        + '<td class="num ' + cc(r.ic_mean) + '">' + fn(r.ic_mean, 4) + '</td>'
        + '<td class="num ' + cc(r.ic_ir) + '">' + fn(r.ic_ir, 3) + '</td>'
        + '<td class="num ' + cc(r.t_stat) + '">' + fn(r.t_stat, 2) + '</td></tr>';
    }).join('');
    const dsrCells = Object.keys(dsr).map(function (k) {
      const v = dsr[k];
      return '<div class="slab-metric"><span class="v ' + (v == null ? '' : (v >= 0.9 ? 'c-ok' : v >= 0.5 ? '' : 'c-bad')) + '">' + (v == null ? '—' : fn(v, 3)) + '</span><span class="l">' + esc(k) + '</span></div>';
    }).join('');
    const capRows = (diag.capital_sweep || []).map(function (c) {
      if (!c || c.ok == null) return '';
      if (!c.ok) return '<tr><td class="num">' + esc(c.label) + '</td><td colspan="4" class="c-dim">' + esc(c.reason || '回测失败') + '</td></tr>';
      return '<tr><td class="num">' + esc(c.label) + '</td>'
        + '<td class="num ' + cc(c.annual_return) + '">' + fpct(c.annual_return) + '</td>'
        + '<td class="num ' + cc(c.excess_annual) + '">' + fpct(c.excess_annual) + '</td>'
        + '<td class="num c-down">' + fpct(c.max_drawdown) + '</td>'
        + '<td class="num">' + fn(c.sharpe, 2) + '</td></tr>';
    }).join('');
    const cap0 = (diag.capital_sweep || []).find(function (c) { return c && c.label === '20万'; });
    const capMinExcess = (cap0 && cap0.excess_annual != null) ? cap0.excess_annual : null;
    const lineage = (ev.spec && ev.spec.factor_lineage) || [];
    const lineageRows = lineage.map(function (f) {
      return '<tr><td class="mono">' + esc(f.factor_id || '—') + '</td><td>' + esc(f.label || f.factor_id || '—') + '</td>'
        + '<td class="num">' + fn(f.weight, 3) + '</td><td>' + esc(f.status || f.kind || '—') + '</td>'
        + '<td class="mono c-dim" style="max-width:300px;overflow:hidden;text-overflow:ellipsis" title="' + escAttr(f.formula || '') + '">' + esc(f.formula || '内置面板列') + '</td></tr>';
    }).join('');
    return '<div style="padding:14px;border:1px solid var(--border-subtle);background:var(--bg-elevated);border-radius:10px">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px">'
      + '<strong style="font-size:var(--text-sm)">统一引擎专业诊断（factor_system 引擎）</strong>'
      + '<div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">'
      + (cached ? '<span class="c-dim" style="font-size:12px">缓存结果</span>' : '')
      + '<span class="' + (passed ? 'c-ok' : 'c-bad') + '" style="padding:5px 10px;border-radius:8px;background:var(--bg-card);font-size:var(--text-xs)">' + esc(verdict) + '</span>'
      + '</div></div>'
      + '<div style="padding:9px 11px;border-left:3px solid ' + (passed ? '#f59e0b' : '#ef4444') + ';background:var(--bg-card);font-size:var(--text-xs);line-height:1.6;margin-bottom:11px">' + esc(online.reason || gate.reason || '—') + '</div>'
      + '<details class="slab-inline-disclosure"><summary>展开完整诊断证据与大表</summary><div>'
      + (hasRegistryFactor ? '<div style="padding:8px 10px;background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.25);border-radius:8px;font-size:var(--text-xs);line-height:1.6;color:var(--text-secondary);margin-bottom:11px">策略含「我的因子」（配方重建）：评估窗口 = 因子系统面板 <strong>2021-01~2025-12</strong>（发现/验证 2021-01~2024-06；历史确认 2024-07~2025-12）。该区间只能作为历史确认；production 仍需因子自身 seal 后新增数据前向终测。</div>' : '')
      + (lineageRows ? '<div style="overflow:auto;margin-bottom:11px"><div class="slab-note" style="margin-bottom:5px"><b>选股因子血缘</b> · 合成方法 ' + esc((ev.spec && ev.spec.compose_method) || 'rank_weighted_v1') + '</div><table class="slab-table"><thead><tr><th>因子ID</th><th>名称</th><th>最终权重</th><th>状态</th><th>公式</th></tr></thead><tbody>' + lineageRows + '</tbody></table></div>' : '')
      + '<div class="slab-metric-grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:11px">'
      + '<div class="slab-metric"><span class="v ' + cc(oos.annual_return_pct) + '">' + fpp(oos.annual_return_pct) + '</span><span class="l">历史确认年化</span></div>'
      + '<div class="slab-metric"><span class="v ' + cc(oos.sharpe) + '">' + fn(oos.sharpe, 2) + '</span><span class="l">Sharpe</span></div>'
      + '<div class="slab-metric"><span class="v c-down">' + fpp(oos.max_drawdown_pct) + '</span><span class="l">最大回撤</span></div>'
      + '<div class="slab-metric"><span class="v">' + fn(oos.win_rate_pct, 1) + '%</span><span class="l">胜率</span></div>'
      + '<div class="slab-metric"><span class="v ' + cc(oos.t_stat) + '">' + fn(oos.t_stat, 2) + '</span><span class="l">日收益 t</span></div>'
      + '<div class="slab-metric"><span class="v ' + cc(oos.internal_excess_pct) + '">' + fpp(oos.internal_excess_pct) + '</span><span class="l">超额(等权)</span></div>'
      + '<div class="slab-metric"><span class="v">' + (oos.entry_order_fill_rate_pct == null ? '—' : fn(oos.entry_order_fill_rate_pct, 1) + '%') + '</span><span class="l">买入委托成交率</span></div>'
      + '<div class="slab-metric"><span class="v">' + fn(oos.previous_close_limit_cancelled_count, 0) + '</span><span class="l">高开不追取消</span></div>'
      + '</div>'
      + (fcOk ? '<div style="padding:10px 11px;background:var(--bg-card);border-radius:8px;margin-bottom:11px;border:1px solid var(--border-subtle)">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
        + '<strong style="font-size:var(--text-xs);color:var(--text-secondary)">全周期 2021-2025 走查 vs 上证（研究视角 · 不等于前向投产）</strong>'
        + '<span class="' + cc(fullCycle.shanghai_excess_annual) + '" style="font-size:var(--text-sm)">全周期超额 ' + fpct(fullCycle.shanghai_excess_annual) + '</span>'
        + '</div>'
        + '<div class="slab-metric-grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:8px">'
        + '<div class="slab-metric"><span class="v ' + cc(fullCycle.annual_return) + '">' + fpct(fullCycle.annual_return) + '</span><span class="l">全周期年化</span></div>'
        + '<div class="slab-metric"><span class="v ' + cc(fullCycle.shanghai_annual) + '">' + fpct(fullCycle.shanghai_annual) + '</span><span class="l">上证年化</span></div>'
        + '<div class="slab-metric"><span class="v ' + cc(fullCycle.sharpe) + '">' + fn(fullCycle.sharpe, 2) + '</span><span class="l">Sharpe</span></div>'
        + '<div class="slab-metric"><span class="v c-down">' + fpct(fullCycle.max_drawdown) + '</span><span class="l">最大回撤</span></div>'
        + '</div>'
        + '<div style="overflow:auto"><table class="slab-table"><thead><tr><th>年份</th><th>策略</th><th>上证</th><th>超额</th></tr></thead><tbody>' + (fcRows || '<tr><td colspan="4" class="c-dim">无数据</td></tr>') + '</tbody></table></div>'
        + '<div class="c-dim" style="font-size:12px;margin-top:6px;line-height:1.6">' + esc(fullCycle.note || '') + ' 窗口 ' + esc(fullCycle.window || '') + '（' + fn(fullCycle.n_days, 0) + ' 个交易日）。</div>'
        + '</div>' : '')
      + (diag.capital_sweep ? '<div style="padding:10px 11px;background:var(--bg-card);border-radius:8px;margin-bottom:11px;border:1px solid var(--border-subtle)">'
        + '<strong style="font-size:var(--text-xs);color:var(--text-secondary)">资金敏感性（同一组合 · 只换资金档 · 全周期走查 vs 上证）</strong>'
        + '<div style="overflow:auto;margin-top:6px"><table class="slab-table"><thead><tr><th>资金</th><th>年化</th><th>超额(上证)</th><th>回撤</th><th>Sharpe</th></tr></thead><tbody>' + (capRows || '<tr><td colspan="5" class="c-dim">无数据</td></tr>') + '</tbody></table></div>'
        + '<div class="c-dim" style="font-size:12px;margin-top:6px;line-height:1.6">同一组合只换资金档：资金越小，100 股整手与现金拖累越重（20万 超额可为负，本表 20万 超额 ' + fpct(capMinExcess) + '）；资金越大整手约束越松。资金档只加注；当前没有策略前向终测 runner，历史确认不能投产。</div>'
        + '</div>' : '')
      + '<div style="overflow:auto;margin-bottom:11px"><table class="slab-table"><thead><tr><th>基准</th><th>口径</th><th>策略年化</th><th>基准年化</th><th>超额</th><th>相关</th><th>策略回撤</th><th>基准回撤</th></tr></thead><tbody>' + (benchRows || '<tr><td colspan="8" class="c-dim">无数据</td></tr>') + '</tbody></table>'
      + '<div class="c-dim" style="font-size:12px;margin-top:5px">策略=o2o（t+1 开盘执行 + THS 费用 + 100股整手）；买点规则=' + esc((ev.spec && ev.spec.entry_rule) || 'none') + '；指数=close-to-close；等权=o2o 减每日成本。历史确认窗口 2024-07~2025-12。</div></div>'
      + '<div style="overflow:auto;margin-bottom:11px"><table class="slab-table"><thead><tr><th>单因子子策略</th><th>权重</th><th>历史确认年化</th><th>t</th><th>超额</th><th>历史确认 IC</th><th>历史确认 IR</th></tr></thead><tbody>' + (subRows || '<tr><td colspan="7" class="c-dim">无数据</td></tr>') + '</tbody></table>'
      + '<div class="c-dim" style="font-size:12px;margin-top:5px">每个 active 因子单独做多（同一组合口径），归因看谁在驱动组合。</div></div>'
      + '<div style="overflow:auto;margin-bottom:11px"><table class="slab-table"><thead><tr><th>滚动 WF 区间</th><th>天数</th><th>IC</th><th>IR</th><th>t</th></tr></thead><tbody>' + (wfRows || '<tr><td colspan="5" class="c-dim">无数据</td></tr>') + '</tbody></table>'
      + '<div class="c-dim" style="font-size:12px;margin-top:5px">历史确认段滚动折（首折留训练段），IC_IR 波动 ' + (wf.ic_ir_std == null ? '—' : fn(wf.ic_ir_std, 3)) + '，正 IC_IR 折数 ' + fn(wf.ic_ir_pos_folds, 0) + '/' + fn((wf.folds || []).length, 0) + '。</div></div>'
      + '<div class="slab-metric-grid" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr));margin-bottom:11px"><strong style="font-size:var(--text-xs);grid-column:1/-1;color:var(--text-secondary)">Deflated Sharpe（尝试次数敏感性）</strong>' + (dsrCells || '<span class="c-dim">—</span>') + '</div>'
      + '<div style="padding:10px 11px;background:var(--bg-card);border-radius:8px;margin-bottom:8px"><div class="c-dim" style="font-size:12px;margin-bottom:6px">信号级诊断（IS 2021-01~2024-06）</div>'
      + '<div style="display:flex;gap:7px;flex-wrap:wrap;align-items:center;font-size:12px">'
      + '<span class="' + (mono.monotonic_ok ? 'c-ok' : 'c-bad') + '">分层单调 ' + (mono.monotonic_score == null ? '—' : fn(mono.monotonic_score, 2)) + '</span>'
      + (icd.half_life != null ? '<span>IC半衰期 ' + fn(icd.half_life, 0) + '日</span>' : '')
      + '<span>信号换手 ' + (sd.turnover && sd.turnover.turnover_ratio != null ? fn(sd.turnover.turnover_ratio, 2) : '—') + '</span>'
      + '<span>排序稳定 ' + (sd.rank_autocorrelation && sd.rank_autocorrelation.mean_rank_autocorrelation != null ? fn(sd.rank_autocorrelation.mean_rank_autocorrelation, 2) : '—') + '</span>'
      + '<span>容量 ' + (sd.capacity && sd.capacity.capacity_estimate_yi != null ? fn(sd.capacity.capacity_estimate_yi, 0) + '亿' : '—') + '</span>'
      + '</div>'
      + (yearly ? '<div class="c-dim" style="font-size:12px;margin-top:7px">跨年份稳定性（只作样本外切分，不是情绪状态）</div><div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:5px">' + yearly + '</div>' : '')
      + '<div class="c-dim" style="font-size:12px;margin-top:7px;line-height:1.6">手工诊断只供探索并已消费共享 LOND；历史确认晋升只认固定批次的 BY-FDR + LOND，不能由本页产生。' + esc((ev.spec && ev.spec.name) || '') + '</div>'
      + '</div></div></details></div>';
  }

  function assistantHtml(assistant) {
    if (!assistant || !assistant.mode) return '<div style="padding:11px;border:1px dashed var(--border-subtle);border-radius:10px" class="c-dim">点击“本地智能诊断”后，统计规则引擎会解释风险并提出一次只改一个变量的研究假设。零API、零模型费。</div>';
    const risks = assistant.risk_flags || [], suggestions = assistant.suggestions || [];
    return '<div style="padding:12px;border:1px solid rgba(10,132,255,.4);background:rgba(10,132,255,.05);border-radius:10px">'
      + '<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap"><strong>' + esc(assistant.label || '本地智能研究助手') + '</strong><span class="c-ok" style="font-size:12px">无LLM · 无API · 无网络 · ¥0</span></div>'
      + '<div style="font-size:var(--text-sm);line-height:1.55;margin-top:8px">' + esc(assistant.verdict || '') + '</div>'
      + (risks.length ? '<div style="margin-top:9px">' + risks.map(function (row) { return '<div class="' + (row.severity === 'high' ? 'c-bad' : 'c-warn') + '" style="font-size:var(--text-xs);margin:4px 0">' + (row.severity === 'high' ? '高风险' : '中风险') + ' · ' + esc(row.message) + '</div>'; }).join('') + '</div>' : '')
      + (suggestions.length ? '<div style="margin-top:10px;display:grid;gap:6px">' + suggestions.map(function (row) { return '<div style="padding:8px;background:var(--bg-elevated);border-radius:8px;font-size:var(--text-xs)"><strong>' + esc(row.priority) + ' · ' + esc(row.action) + '</strong><div class="c-dim" style="margin-top:3px">' + esc(row.reason) + '</div></div>'; }).join('') + '</div>' : '')
      + (assistant.proposed_spec && Object.keys(assistant.proposed_spec).length ? '<button type="button" class="slab-btn" style="margin-top:9px" data-action="apply-assistant">载入助手假设为新草稿</button>' : '')
      + '<div class="c-dim" style="font-size:12px;margin-top:8px">' + esc(assistant.warning || '') + '</div></div>';
  }

  window._labAssistant = async function () {
    const report = _activeReport || {};
    if (!report.run_id) return setStatus('请先运行或载入一份完整回测报告。', 'error');
    if (_assistantLoading) return setStatus('本地统计诊断仍在运行，请勿重复提交。', 'error');
    _assistantLoading = true;
    const target = $('slabAssistant');
    if (target) target.innerHTML = '<div class="c-dim" style="padding:12px">本地统计规则引擎正在分析，不连接网络…</div>';
    const res = await apiWrite('/api/strategy-lab/user-assistant', { run_id: report.run_id });
    _assistantLoading = false;
    if (!res._httpOk) return setStatus(res.error || '本地诊断失败', 'error');
    _lastAssistant = res.assistant;
    const hypothesisSuggestion = ((res.assistant && res.assistant.suggestions) || []).find(function (row) { return row && row.experiment; });
    _pendingExperimentHypothesis = hypothesisSuggestion ? hypothesisSuggestion.experiment : null;
    report.assistant = res.assistant;
    if (target) target.innerHTML = assistantHtml(res.assistant);
    setStatus('本地智能诊断完成：API 0、网络 0、费用 ¥0。', 'ok');
  };
  window._labApplyAssistant = function () {
    const a = _lastAssistant || (_activeReport && _activeReport.assistant);
    if (!a || !a.proposed_spec || !Object.keys(a.proposed_spec).length) return;
    applySpec(a.proposed_spec);
    setStatus('助手假设已载入为新草稿；它仍需重新跑完整固定分段。', 'ok');
  };
  window._labExport = function () {
    const report = _activeReport;
    if (!report) return;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = (report.run_id || 'strategy-report') + '.json';
    link.click();
    setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
  };

  /* ═══ 历史实验库 ═══ */
  function renderHistory(history) {
    const el = $('slabHistory');
    if (!el) return;
    if (!history || !history.entries || !history.entries.length) { el.innerHTML = '<div class="slab-empty">暂无留痕记录</div>'; return; }
    el.innerHTML = '<div>' + history.entries.map(function (e) {
      const isBad = e.verdict === 'INVALIDATED' || e.verdict === 'NO-GO';
      const color = isBad ? '#ef4444' : e.verdict === 'REVALIDATED' ? '#22c55e' : 'var(--text-tertiary)';
      const verdictTxt = { INVALIDATED: '已否决', 'NO-GO': '已否定', REVALIDATED: '复核' }[e.verdict] || e.verdict || '';
      const afterTxt = e.after ? '<div style="margin-top:6px;font-family:var(--font-number);font-size:var(--text-xs);color:var(--text-secondary)">' + esc(e.after) + '</div>' : '';
      const beforeTxt = e.before ? '<div style="margin-top:4px;font-family:var(--font-number);font-size:var(--text-xs);color:var(--text-tertiary);text-decoration:line-through">' + esc(e.before) + '</div>' : '';
      const pathTxt = e.evidence_path ? '<span style="font-size:12px;color:' + (e.evidence_exists === false ? '#ef4444' : 'var(--text-tertiary)') + '">' + esc(e.evidence_path) + (e.evidence_exists === false ? ' 文件缺失' : '') + '</span>' : '';
      return '<div class="slab-ledger-item">'
        + '<div class="ledger-hd"><strong style="font-size:var(--text-sm)">' + esc(e.title) + '</strong>'
        + '<span class="slab-badge ' + (isBad ? 'bad' : e.verdict === 'REVALIDATED' ? 'ok' : 'dim') + '">' + esc(verdictTxt) + '</span>'
        + '<span class="c-dim" style="font-size:var(--text-xs)">' + esc(e.date || '') + ' · ' + esc(e.status_label || '') + '</span></div>'
        + '<div class="ledger-summary">' + esc(e.summary || '') + '</div>'
        + beforeTxt + afterTxt
        + '<div style="margin-top:6px;font-family:var(--font-number)">' + pathTxt + '</div>'
        + '</div>';
    }).join('') + '</div>';
  }

  /* ═══ 刷新与初始化 ═══ */
  async function refreshWorkbench() {
    const res = await api('/api/strategy-lab/user-workbench?view=compact');
    if (!res) {
      if ($('slabSource')) $('slabSource').innerHTML = '<div class="slab-empty">基础资料读取失败：' + esc(_apiErrors.get('/api/strategy-lab/user-workbench?view=compact') || '接口未返回') + '。可点击「重新加载基础功能」；已有编辑内容保留。</div>';
      if (!_wb) {
        if ($('slabRegistry')) $('slabRegistry').innerHTML = '<div class="slab-empty">注册表尚未读到，不能解释为空。</div>';
        if ($('slabHistory')) $('slabHistory').innerHTML = '<div class="slab-empty">历史尚未读到，不能解释为没有实验。</div>';
      }
      setStatus('基础资料本次未核验；可重新加载。已有编辑内容未覆盖。', 'error');
      return false;
    }
    if (res) {
      const firstLoad = !_wb;
      _wb = res;
      if (firstLoad) {
        buildEditor(Object.assign({}, res.draft_factor_catalog || {}, res.factor_catalog || {}), res.defaults);
        buildTemplatePicker(res.templates || []);
        applySpec(Object.assign({ account_capital: 150000 }, res.defaults || {}), false, '新建草稿');
      }
      if (res.workflow) applyWorkflow(res.workflow);
      prepareEvidenceArchive(res.latest_backtest_summary || res.latest_backtest, res.latest_run_id || _latestRunId);
      renderSource(res.source, res.fixed_splits, res.cost_ladder_bps, res.safety);
    }
    renderRegistry(_wb && _wb.saved_strategies, _wb && _wb.recent_backtests);
    renderRecentExperiments(_wb && _wb.recent_experiments);
    if (_lastBatchState) renderBatch(_lastBatchState);
    return true;
  }

  /* ═══ 因子注册表 → 选股策略 → 统一科学回测 ═══ */
  function renderBatch(b) {
    const el = $('slabBatchResult');
    if (!el) return;
    const prog = $('slabBatchProgress');
    const bar = $('slabBatchProgBar');
    const lbl = $('slabBatchProgLabel');
    const pct = $('slabBatchProgPct');
    const btn = $('slabBatchBtn');
    const badge = $('slabBatchBadge');
    if (!b || !b.ok) {
      el.innerHTML = '<div class="slab-empty">批量状态不可用。</div>';
      if (btn) { btn.disabled = true; btn.textContent = '批量状态不可用'; }
      return;
    }
    _lastBatchState = b;
    if (!_batchCapability && b.start_allowed != null) _batchCapability = b;
    const capability = _batchCapability || {};
    const hasAuthoritativeStart = typeof capability.start_allowed === 'boolean';
    const strictCount = _strictPoolCount == null || !Number.isFinite(Number(_strictPoolCount)) ? null : Number(_strictPoolCount);
    const sourceReady = !!_wb && _wb.available === true;
    const startAllowed = sourceReady && (hasAuthoritativeStart
      ? capability.start_allowed === true && strictCount > 0
      : strictCount > 0);
    const batchBlockers = (capability.blockers || []).map(function (row) { return typeof row === 'string' ? row : (row && (row.message || row.code)); }).filter(Boolean);
    const running = capability.running === true || !!b.running;
    const total = capability.running === true && capability.total != null ? capability.total : (b.total || 0);
    const done = capability.running === true && capability.done != null ? capability.done : (b.done || 0);
    const frac = total ? Math.round(done / total * 100) : 0;
    const currentFactors = (b.current_factor_ids || []).map(function (id) { return factorLabel(id) + ' (' + id + ')'; });
    const currentCompose = _COMPOSE_LABELS[b.current_compose_method] || b.current_compose_method || '';
    const currentDetail = currentFactors.length
      ? currentFactors.join(' + ') + ' → ' + currentCompose + ' → top-' + (b.current_top_n || '—') + ' / ' + (b.current_holding_days || '—') + '日'
      : (b.current || '准备候选');
    prog.setAttribute('aria-valuenow', String(frac));
    if (running) {
      prog.style.display = '';
      if (lbl) lbl.textContent = '评估中：' + currentDetail + '（' + done + '/' + total + '）';
      if (pct) pct.textContent = frac + '%';
      if (bar) bar.style.width = frac + '%';
      if (btn) { btn.disabled = true; btn.textContent = '批量生成进行中…'; }
      if (badge) badge.textContent = '因子策略评估中 ' + done + '/' + total;
      markFlow('backtest', '评估 ' + done + '/' + total);
    } else {
      prog.style.display = 'none';
      if (btn) {
        btn.disabled = !startAllowed;
        btn.textContent = strictCount === 0
          ? '证据完整的可用因子为 0'
          : (startAllowed ? '启动当前批次固定检验' : '批量暂不可启动');
        btn.title = startAllowed ? '' : (batchBlockers[0] || (strictCount === 0 ? '先构建并验证当前批次的新因子' : '当前有任务正在运行或数据尚不完整'));
      }
      if (badge) badge.textContent = (strictCount != null ? strictCount + ' 个证据完整候选' : '可用因子尚未核对')
        + (done ? ' · 档案 ' + done + '/' + total : '') + (b.invalidated ? ' · 旧批已失效' : '') + (b.crashed_stale ? ' · 上次中断' : '');
      if (done) {
        markFlow('verdict', 'HISTORICAL_PASS ' + (b.historical_pass_count || 0) + ' · 前向投产未实现 ' + (b.pass_count || 0) + ' · NO-GO ' + (b.no_go_count || 0) + ' · ERROR/失效 ' + ((b.error_count || 0) + (b.invalidated_count || 0)));
      }
    }
    updateResearchOverview();
    const archive = $('slabBatchArchive');
    if (archive && !archive.open) {
      el.innerHTML = '<div class="slab-empty">批次档案已折叠：' + done + '/' + total + ' 条记录'
        + (b.invalidated ? '，旧批次已失效且 0 条计入 PASS' : '') + '。展开后渲染完整候选与证据。</div>';
      return;
    }
    const results = b.results || [];
    function isHistoricalPass(row) {
      return row.verdict === 'HISTORICAL_PASS' && row.passed === false
        && row.historical_confirmation_passed === true
        && row.is_validation_passed === true && row.gate_passed === true
        && row.batch_fdr_passed === true && row.global_lond_passed === true
        && row.registration_passed === true;
    }
    function stageBadge(value, label) {
      if (value == null) return '<span class="slab-badge dim">' + label + ' —</span>';
      return '<span class="slab-badge ' + (value ? 'ok' : 'bad') + '">' + label + ' ' + (value ? '✓' : '×') + '</span>';
    }
    function verdictBadge(row) {
      const verdict = row.verdict || 'PENDING';
      if (isHistoricalPass(row)) return '<span class="slab-badge dim">HISTORICAL_PASS</span>';
      if (verdict === 'ERROR') return '<span class="slab-badge bad">ERROR</span>';
      if (verdict === 'INVALIDATED') return '<span class="slab-badge dim">INVALIDATED</span>';
      if (verdict === 'PENDING') return '<span class="slab-badge dim">PENDING</span>';
      return '<span class="slab-badge bad">NO-GO</span>';
    }
    const historicalPassCount = results.filter(isHistoricalPass).length;
    const passCount = 0;
    const noGoCount = results.filter(function (row) { return row.verdict === 'NO-GO'; }).length;
    const errorCount = results.filter(function (row) { return row.verdict === 'ERROR'; }).length;
    const invalidatedCount = results.filter(function (row) { return row.verdict === 'INVALIDATED'; }).length;
    const annuals = b.invalidated ? [] : results.filter(function (row) { return row.verdict !== 'ERROR' && row.verdict !== 'INVALIDATED'; }).map(function (row) { return row.net_annual_pct; }).filter(function (v) { return v != null && !isNaN(v); });
    const bestAnnual = annuals.length ? Math.max.apply(null, annuals) : null;
    const invalidatedBanner = b.invalidated
      ? '<div role="alert" style="padding:10px 12px;margin-bottom:10px;border:1px solid rgba(239,68,68,.45);background:rgba(239,68,68,.08);border-radius:9px;color:#fca5a5;font-size:var(--text-xs);line-height:1.65"><strong>旧批次已失效，0 条计入 PASS。</strong> ' + esc(b.invalidated_reason || '缺少当前五段严格证据。') + '</div>'
      : '';
    const capitalWan = b.account_capital == null ? '—' : fn(Number(b.account_capital) / 10000, 0) + '万';
    const batchInputs = '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">'
      + '<span class="slab-badge dim">研究资金 ¥' + capitalWan + '</span>'
      + '<span class="slab-badge dim">单边滑点 ' + fn(b.one_way_cost_bps, 0) + ' BP · 佣金/印花税/过户费另加</span>'
      + '<span class="slab-badge dim">批内 ' + esc(b.fdr_method || 'BY待执行') + ' · q=' + (b.fdr_q == null ? '—' : fn(b.fdr_q, 3)) + ' · 有效q=' + (b.fdr_effective_q == null ? '—' : fn(b.fdr_effective_q, 4)) + '</span>'
      + '<span class="slab-badge dim">跨批 ' + esc(b.global_fdr_method || 'LOND待执行') + '</span>'
      + '<span class="slab-badge dim mono" title="跨批全局检验 epoch">' + esc(b.global_fdr_epoch || 'epoch 待记录') + '</span>'
      + '</div>';
    const summary = '<div class="slab-batch-summary">'
      + '<div class="slab-metric"><span class="v">' + done + '/' + total + '</span><span class="l">历史确认进度</span></div>'
      + '<div class="slab-metric"><span class="v c-research">' + historicalPassCount + '</span><span class="l">HISTORICAL_PASS</span></div>'
      + '<div class="slab-metric"><span class="v">' + passCount + '</span><span class="l">前向投产（未实现）</span></div>'
      + '<div class="slab-metric"><span class="v c-bad">' + noGoCount + '</span><span class="l">NO-GO</span></div>'
      + '<div class="slab-metric"><span class="v c-bad">' + errorCount + '</span><span class="l">ERROR</span></div>'
      + '<div class="slab-metric"><span class="v">' + invalidatedCount + '</span><span class="l">INVALIDATED</span></div>'
      + '<div class="slab-metric"><span class="v ' + cc(bestAnnual) + '">' + fpct(bestAnnual) + '</span><span class="l">当前最高历史确认净年化</span></div>'
      + '</div>';
    const rows = results.map(function (r) {
      const idCell = r.registration_passed && r.strategy_id ? '<span class="mono c-ok">' + esc(r.strategy_id) + '</span>' : '—';
      const factorText = (r.factor_ids || []).map(function (id) { return factorLabel(id) + ' (' + id + ')'; }).join(' + ') || '旧批次未记录';
      const composeText = _COMPOSE_LABELS[r.compose_method] || r.compose_method || '—';
      const londTitle = 'epoch=' + (r.global_fdr_epoch || b.global_fdr_epoch || '—') + '；evidence=' + (r.global_evidence_path || '—') + '；sha256=' + (r.global_evidence_sha256 || '—');
      return '<tr>'
        + '<td style="max-width:170px;overflow:hidden;text-overflow:ellipsis">' + esc(r.name || '—') + '</td>'
        + '<td style="max-width:260px;white-space:normal" title="' + escAttr((r.factor_ids || []).join(', ')) + '">' + esc(factorText) + '</td>'
        + '<td>' + esc(composeText) + '</td>'
        + '<td class="num">' + fn(r.holding_days, 0) + '日 / top-' + fn(r.top_n, 0) + '</td>'
        + '<td>' + stageBadge(r.is_validation_passed, 'IS') + '</td>'
        + '<td>' + stageBadge(r.gate_passed, '历史闸门') + '</td>'
        + '<td>' + stageBadge(r.batch_fdr_passed, 'BY') + '</td>'
        + '<td title="' + escAttr(londTitle) + '">' + stageBadge(r.global_lond_passed, 'LOND') + '<div class="c-dim mono" style="font-size:12px;margin-top:3px">#' + fn(r.global_hypothesis_order, 0) + ' · α=' + (r.global_alpha_i == null ? '—' : Number(r.global_alpha_i).toExponential(2)) + '</div></td>'
        + '<td>' + stageBadge(r.registration_passed, '注册') + '<div style="margin-top:3px">' + idCell + '</div></td>'
        + '<td class="num ' + cc(r.net_annual_pct) + '">' + fpct(r.net_annual_pct) + '</td>'
        + '<td class="num ' + cc(r.shanghai_excess_pct) + '">' + fpct(r.shanghai_excess_pct) + '</td>'
        + '<td>' + verdictBadge(r) + '</td>'
        + '<td class="c-dim" style="max-width:210px;overflow:hidden;text-overflow:ellipsis" title="' + escAttr(r.reason || '') + '">' + esc(r.reason || (r.error || '—')) + '</td>'
        + '<td class="mono c-dim" style="max-width:150px;overflow:hidden;text-overflow:ellipsis" title="' + escAttr(londTitle) + '">' + esc((r.global_evidence_sha256 || '—').slice(0, 12)) + '</td>'
        + '</tr>';
    }).join('');
    const cards = results.map(function (r) {
      const factorText = (r.factor_ids || []).map(function (id) { return factorLabel(id) + ' (' + id + ')'; }).join(' + ') || '旧批次未记录';
      return '<article class="slab-batch-card">'
        + '<div class="slab-batch-card-hd"><div class="slab-batch-card-name">' + esc(r.name || '未命名候选') + '</div>' + verdictBadge(r) + '</div>'
        + '<div class="slab-note" style="margin-top:6px">因子：' + esc(factorText) + '<br>合成：' + esc(_COMPOSE_LABELS[r.compose_method] || r.compose_method || '—') + ' · top-' + fn(r.top_n, 0) + ' · ' + fn(r.holding_days, 0) + '日</div>'
        + '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:8px">' + stageBadge(r.is_validation_passed, 'IS') + stageBadge(r.gate_passed, '历史闸门') + stageBadge(r.batch_fdr_passed, 'BY') + stageBadge(r.global_lond_passed, 'LOND') + stageBadge(r.registration_passed, '注册') + '</div>'
        + '<div class="slab-batch-card-grid"><div class="slab-batch-card-metric"><span>历史确认净年化</span><strong class="' + cc(r.net_annual_pct) + '">' + fpct(r.net_annual_pct) + '</strong></div>'
        + '<div class="slab-batch-card-metric"><span>vs 上证超额</span><strong class="' + cc(r.shanghai_excess_pct) + '">' + fpct(r.shanghai_excess_pct) + '</strong></div>'
        + '<div class="slab-batch-card-metric"><span>全局序号 / alpha</span><strong>#' + fn(r.global_hypothesis_order, 0) + ' / ' + (r.global_alpha_i == null ? '—' : Number(r.global_alpha_i).toExponential(2)) + '</strong></div>'
        + '<div class="slab-batch-card-metric"><span>证据哈希</span><strong class="mono">' + esc((r.global_evidence_sha256 || '—').slice(0, 12)) + '</strong></div></div>'
        + '<div class="slab-batch-card-reason"><strong>判定原因：</strong>' + esc(r.reason || r.error || '未记录') + (r.registration_passed && r.strategy_id ? '<div class="mono c-ok" style="margin-top:4px">' + esc(r.strategy_id) + '</div>' : '') + '<div class="mono c-dim" style="margin-top:4px">' + esc(r.global_fdr_epoch || b.global_fdr_epoch || '—') + '</div></div></article>';
    }).join('');
    el.innerHTML = results.length
      ? invalidatedBanner + batchInputs + summary + '<div class="slab-batch-table" style="overflow:auto"><table class="slab-table"><thead><tr><th>候选</th><th>选股因子</th><th>合成</th><th>持有/选股</th><th>IS验证</th><th>历史闸门</th><th>批内BY</th><th>跨批LOND</th><th>注册</th><th>历史确认净年化</th><th>vs上证</th><th>最终判定</th><th>原因</th><th>证据SHA</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
      + '<div class="slab-batch-cards">' + cards + '</div>'
      + '<div class="c-dim" style="font-size:12px;padding:6px 2px">候选全部展示（含 NO-GO、ERROR、INVALIDATED，不裁剪）。五段通过仅记 HISTORICAL_PASS；当前没有策略前向 runner，因此前向投产计数固定为 0。权重只在发现段形成，历史确认不参与调权。</div>'
      : '<div class="slab-empty">尚未生成。启动后会显示每条策略使用的因子与合成方法（含未通过候选，不裁剪）。</div>';
  }

  let _batchTimer = null;
  window._labBatchArchiveToggle = function (details) {
    if (details && details.open && _lastBatchState) renderBatch(_lastBatchState);
  };
  async function _labBatchPoll() {
    const b = await api('/api/strategy-lab/batch-status');
    renderBatch(b);
    if (b && b.running) { _batchTimer = setTimeout(_labBatchPoll, 3000); }
    else { _batchTimer = null; scheduleWorkflowSync(0); }
  }

  window._labBatchStart = async function () {
    const capability = _batchCapability || {};
    if (!_wb || _wb.available !== true) {
      setStatus('当前输入面板未就绪，档案仍可查看；批量检验保持禁用。', 'error');
      return;
    }
    if (_strictPoolCount == null) {
      setStatus('可用因子尚未核对完成，批量任务保持禁用。', 'error');
      return;
    }
    if (_strictPoolCount === 0) {
      setStatus('当前证据完整的可用因子为 0；请先完成公开来源、本地精确复现与历史时点数据审计。旧内置因子只供归档查看。', 'error');
      return;
    }
    if (typeof capability.start_allowed === 'boolean' && capability.start_allowed !== true) {
      const reason = (capability.blockers || []).map(function (row) { return typeof row === 'string' ? row : (row && (row.message || row.code)); }).filter(Boolean)[0];
      setStatus(reason || '当前运行锁、批次或数据源不允许启动批量检验。', 'error');
      return;
    }
    const btn = $('slabBatchBtn');
    if (btn) { btn.disabled = true; btn.textContent = '正在提交批量任务…'; }
    const res = await apiWrite('/api/strategy-lab/batch-generate', {});
    if (!res._httpOk) {
      if (_lastBatchState) renderBatch(_lastBatchState);
      setStatus(res.error || '批量生成启动失败', 'error');
      return;
    }
    if (_batchCapability) { _batchCapability.start_allowed = false; _batchCapability.running = true; }
    setStatus('已从当前批次生成 ' + res.total + ' 条选股策略，统一引擎将逐条扣除交易成本、检查稳定性并控制大量尝试造成的误报。使用因子：' + ((res.factor_ids || []).join('、') || '—') + '。', 'ok');
    _labBatchPoll();
  };

  async function loadFactorSourceArchive(statusHint) {
    if (_sourceLoaded || _sourceLoading) return;
    _sourceLoading = true;
    if ($('slabFactorSystem')) $('slabFactorSystem').innerHTML = '<div class="slab-empty" role="status">正在读取因子档案与配方冻结证据…</div>';
    try {
      const rows = await Promise.all([
        statusHint ? Promise.resolve(statusHint) : api('/api/factor-system/status'),
        api('/api/factor-system/factors?view=compact&scope=current&status=candidate,validated,registered,backtesting,historical_pass,production&limit=200'),
      ]);
      const status = rows[0], payload = rows[1];
      if (!status || typeof status !== 'object' || Array.isArray(status) || status.ok === false) {
        throw new Error('因子系统状态读取失败');
      }
      _factorStatus = status;
      _factorWorkflow = status.workflow || _factorWorkflow;
      updateResearchOverview();
      if (!payload || payload.ok === false || !Array.isArray(payload.factors)
          || !payload.factors.every(row => row && typeof row === 'object' && !Array.isArray(row))) {
        throw new Error('状态已获取，但因子清单读取失败；不能解释为空库');
      }
      renderFactorSystem(status, payload.factors);
      _sourceLoaded = true;
    } catch (error) {
      _sourceLoaded = false;
      if ($('slabFactorSystem')) $('slabFactorSystem').innerHTML = '<div class="slab-empty" role="status">' + esc(error.message || '因子档案读取失败') + '。<button type="button" class="slab-btn" onclick="window._labSourceRetry()">重新读取因子档案</button></div>';
    } finally {
      _sourceLoading = false;
    }
  }

  window._labSourceRetry = function () { return loadFactorSourceArchive(); };
  window._labSourceToggle = function (details) {
    if (details && details.open) loadFactorSourceArchive();
  };
  window._labBaselineToggle = async function (details) {
    if (!details || !details.open || _baselineLoaded) return;
    _baselineLoaded = true;
    if ($('slabBaseline')) $('slabBaseline').innerHTML = '<div class="slab-empty" role="status">正在计算宽基基线…</div>';
    const baseline = await api('/api/strategy-lab/baseline');
    if (!baseline) _baselineLoaded = false;
    renderBaseline(baseline);
  };
  window._labHistoryToggle = async function (details) {
    if (!details || !details.open || _historyLoaded) return;
    _historyLoaded = true;
    if ($('slabHistory')) $('slabHistory').innerHTML = '<div class="slab-empty" role="status">正在读取历史实验账本…</div>';
    const history = await api('/api/backtest/history');
    if (!history) _historyLoaded = false;
    renderHistory(history);
  };

  function selectFactorView(button) {
    const buttons = Array.from(document.querySelectorAll('.slab-factor-tab'));
    buttons.forEach(function (item) {
      const selected = item === button;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-pressed', selected ? 'true' : 'false');
      item.tabIndex = selected ? 0 : -1;
    });
    _factorView = button.getAttribute('data-factor-view') || 'focus';
    if ($('slabFactorSearch')) $('slabFactorSearch').value = '';
    filterFactorRows();
  }

  function runAction(button) {
    const action = button.dataset.action;
    const staticActions = {
      'load-template': window._labLoadTemplate,
      'refresh-foundation': async function () {
        const loaded = await refreshWorkbench();
        await syncWorkflow();
        if (loaded) setStatus('基础资料已重新读取；保留当前编辑内容。回测与研究资格分别核对。', 'ok');
      },
      'sync-dsl': labSyncFromDsl,
      save: labSave,
      validate: labValidate,
      clone: labClone,
      'project-budget-draft': labProjectBudgetDraft,
      'experiment-baseline': window._labSetExperimentBaseline,
      'experiment-run': window._labRunExperimentCandidate,
      run: labRun,
      'batch-start': window._labBatchStart,
      'forward-observe': window._labForwardObserve,
      assistant: window._labAssistant,
      'unified-diag': window._labUnifiedDiag,
      'export-report': window._labExport,
      'apply-assistant': window._labApplyAssistant,
    };
    let result;
    if (staticActions[action]) result = staticActions[action]();
    else if (action === 'load-strategy') result = window._labLoad(button.dataset.id || '');
    else if (action === 'run-saved') result = window._labRunSaved(button.dataset.id || '');
    else if (action === 'view-run') result = window._labViewRun(button.dataset.id || '');
    else if (action === 'promotion') {
      const allowed = ['paper_observe_on', 'paper_observe_off', 'lock'];
      if (allowed.indexOf(button.dataset.promotionAction) < 0) return;
      result = window._labPromotion(button.dataset.id || '', button.dataset.promotionAction);
    }
    if (result && typeof result.catch === 'function') {
      result.catch(function (error) {
        console.error('[strategy-lab:action]', action, error);
        setStatus(error.message || '操作失败，请重试', 'error');
      });
    }
  }

  function bindEvents() {
    document.querySelectorAll('button:not([type])').forEach(function (button) { button.type = 'button'; });
    const tabs = Array.from(document.querySelectorAll('.slab-factor-tab'));
    tabs.forEach(function (button) { button.tabIndex = button.classList.contains('active') ? 0 : -1; });
    document.addEventListener('click', function (event) {
      const factorTab = event.target.closest('.slab-factor-tab');
      if (factorTab) {
        selectFactorView(factorTab);
        return;
      }
      const button = event.target.closest('button[data-action]');
      if (button && !button.disabled) runAction(button);
    });
    document.addEventListener('keydown', function (event) {
      const tab = event.target.closest('.slab-factor-tab');
      if (!tab || ['ArrowLeft', 'ArrowRight', 'Home', 'End'].indexOf(event.key) < 0) return;
      const buttons = Array.from(document.querySelectorAll('.slab-factor-tab'));
      let index = buttons.indexOf(tab);
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = buttons.length - 1;
      else index = (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      event.preventDefault();
      buttons[index].focus();
      selectFactorView(buttons[index]);
    });
    if ($('slabTemplate')) $('slabTemplate').addEventListener('change', function () {
      const selected = ((_wb && _wb.templates) || []).find(function (template) { return template.id === $('slabTemplate').value; });
      const note = $('slabTemplateNote');
      if (!note) return;
      note.innerHTML = selected
        ? '<b>' + esc(selected.direction || '') + '</b>。' + esc(selected.note || '')
        : '选择模板后点击「载入模板」填充编辑器（方向显式权重），可改权重再保存、回测或诊断。';
    });
    if ($('slabFactorSearch')) $('slabFactorSearch').addEventListener('input', filterFactorRows);
    if ($('slabFactorRows')) $('slabFactorRows').addEventListener('input', function (event) {
      const input = event.target;
      if (!input || !input.hasAttribute('data-user-factor')) return;
      const row = input.closest('.slab-factor-row');
      if (row) row.classList.toggle('is-zero', Number(input.value || 0) === 0);
      updateEditorSummary();
      filterFactorRows();
      updateExperimentDraft();
    });
    ['slabName', 'slabHolding', 'slabTopN', 'slabCost', 'slabRebalBuffer',
      'slabCapital', 'slabRisk', 'slabEntry', 'slabExposure', 'slabCompose'].forEach(function (id) {
      const control = $(id);
      if (control) control.addEventListener('input', function () {
        updateEditorSummary();
        updateExperimentDraft();
      });
    });
    const toggles = [
      ['slabGateDisclosure', window._labGateToggle],
      ['slabBatchArchive', window._labBatchArchiveToggle],
      ['slabCurrentEvidence', window._labEvidenceToggle],
      ['slabSourceArchive', window._labSourceToggle],
      ['slabBaselineArchive', window._labBaselineToggle],
      ['slabHistoryArchive', window._labHistoryToggle],
    ];
    toggles.forEach(function (entry) {
      const details = $(entry[0]);
      if (details) details.addEventListener('toggle', function () { entry[1](details); });
    });
  }

  function renderCloudJointObservation(observation) {
    const target = $('slabCloudJointObservation');
    if (!target) return;
    if (!observation || observation.status !== 'complete' || !Array.isArray(observation.candidates)
        || observation.production_eligible !== false || observation.generates_alert !== false) {
      target.innerHTML = '<div class="slab-empty">' + esc(observation && observation.summary
        || '联合研究快照不可用；不能把数据缺失解释为没有机会。') + '</div>';
      return;
    }
    const sectors = observation.research_sectors || [];
    const defense = observation.route_mode === 'defense';
    const exitOnly = observation.route_mode === 'exit_review';
    target.innerHTML = '<p class="slab-note">日线截至 ' + esc(observation.data_as_of) + ' · '
      + esc(observation.emotion_name) + ' · ' + esc(observation.scope) + '</p>'
      + '<p class="slab-note"><strong>当前研究路线：' + esc(observation.route_label || '主线入场条件研究') + '</strong><br>'
      + (defense ? '情绪来源已核验；本路线不要求新仓板块、个股形态或辅助因子。持仓复核仍需原计划、真实行情及可卖数量。'
        : exitOnly ? '情绪与完整价格窗口参与退出形态复核；不要求新仓板块排序，行业相对强度未计算。'
        : '情绪、完整价格横截面和同日行业板块均参与；辅助因子只排序，缺值不删除已满足战法条件的股票。')
      + '</p>'
      + (defense ? '<p class="slab-note">本路线未执行个股条件筛选；采集缺口仍单独显示，不能解释成没有股票或市场没有机会。</p>'
        : '<p class="slab-note">已核验 ' + esc(observation.covered_stocks) + ' 只；符合' + (exitOnly ? '价格历史' : '横截面') + '条件 '
          + esc(observation.eligible_stocks) + ' 只；ST 或历史不足排除 ' + esc(observation.excluded_stocks) + ' 只。</p>')
      + '<div class="slab-playbook-grid">' + sectors.map(function (row) {
        return '<article class="slab-playbook"><div class="slab-playbook-title"><strong>' + esc(row.industry_code)
          + '</strong><span>日线研究板块 · 非盘中主线</span></div><dl>'
          + '<div><dt>有效成分</dt><dd>' + esc(row.count) + ' 只（至少12只）</dd></div>'
          + '<div><dt>20日收益中位数</dt><dd>' + fpct(row.median_return20) + '（要求大于0）</dd></div>'
          + '<div><dt>站上20日均线比例</dt><dd>' + fn(row.breadth * 100, 1) + '%（至少60%）</dd></div></dl>'
          + '<footer>按20日收益中位数取前两个行业；尚未验证持续性、核心股联动和当前成交条件。</footer></article>';
      }).join('') + '</div>'
      + (sectors.length ? '' : '<p class="slab-note">' + (defense || exitOnly ? '本路线未执行行业主线筛选；不等于市场没有主线。' : '本批完整横截面没有满足固定行业研究条件的板块。') + '</p>')
      + '<p class="slab-note">该情绪适用研究战法：' + ((observation.applicable_playbooks || []).map(function (row) {
        return esc(row.name) + '：' + esc(row.characteristic);
      }).join('；') || '无新入场研究战法') + '</p>'
      + '<p class="slab-note">' + esc(observation.summary) + ' 展示前 '
      + observation.candidates.length + ' 条；排序不是最佳买点排名。</p><div class="slab-playbook-grid">'
      + observation.candidates.map(function (row) {
        const exit = row.kind === 'exit_review';
        return '<article class="slab-playbook is-blocked"><div class="slab-playbook-title"><strong>'
          + esc(row.playbook_name) + '</strong><span>研究条件命中 · 未验证</span></div><p><a href="/stock?code='
          + encodeURIComponent(row.code) + '">个股研究 ' + esc(row.code) + '</a> · ' + esc(row.industry_code || '行业未参与本次退出观察') + '</p><dl>'
          + '<div><dt>参考收盘</dt><dd>¥' + fn(row.reference_close, 2) + '（非实时价）</dd></div>'
          + '<div><dt>' + (exit ? '退出复核' : '次日开盘价格边界') + '</dt><dd>'
          + (exit ? '结合原持仓计划与当日可卖数量，不生成卖出股数' : '研究上限 ¥' + fn(row.maximum_entry_open, 2) + '；不是买入委托') + '</dd></div>'
          + '<div><dt>风险线</dt><dd>' + (exit ? '沿用原持仓风险线' : '¥' + fn(row.risk_line, 2)) + '</dd></div>'
          + '<div><dt>股数与仓位</dt><dd>不生成；缺少策略准入、当前行情与账户联合验证</dd></div></dl>'
          + '<footer>不读未来收益，不等于已成交，不授权买卖提醒。</footer></article>';
      }).join('') + '</div>'
      + (observation.candidates.length ? '' : '<div class="slab-empty">' + (defense ? '已选择退潮防守；未扫描新仓候选，不把它计作一条通过验证的入场策略。' : exitOnly ? '价格输入已完成核验，本批次没有命中高潮退出形态；不替代原持仓风险线。' : '输入已完成联合计算，本批次无满足固定研究条件的股票；不是全市场没有机会。') + '</div>');
  }

  function mainlineSystemHTML(mainline) {
    if (!mainline || mainline.schema !== 'mainline_system_candidate.v1' || mainline.production_eligible !== false) {
      return '<p class="slab-note">主线联动输入不可用；不沿用旧结论。</p>';
    }
    const context = mainline.emotion_context || {};
    let body = '<h4>主线阶段与固定核心股联动</h4><p class="slab-note">' + esc(mainline.summary)
      + (mainline.observed_at ? '<br>板块观察：' + esc(mainline.observed_at) + (mainline.historical_only ? ' · 历史记录，仅供复盘' : ' · 本时段候选观察') : '')
      + (context.label ? '<br>情绪背景：' + esc(context.label) + '（' + esc(context.data_as_of) + ' 完成日线）；盘中情绪仍须独立确认。' : '<br>情绪背景尚未与交易日历匹配。')
      + '</p><div class="slab-playbook-grid">';
    body += (mainline.rows || []).map(function (row) {
      const metric = row.metrics;
      const number = function (v, suffix) { return v == null ? '不可比较' : fn(v, 2) + suffix; };
      return '<article class="slab-playbook"><h4>' + esc(row.sector.name) + '</h4><p><strong>' + esc(row.label)
        + '</strong></p><p>连续可比较观察 ' + esc(row.consecutive_observations) + ' 次；连续联动达标 ' + esc(row.aligned_observations || 0) + ' 次。</p>'
        + (metric ? '<p>上涨比例（含平盘）：' + number(metric.advance_including_flat_pct, '%')
          + '<br>剔除固定核心上涨比例：' + number(metric.ex_fixed_core_advance_pct, '%')
          + '<br>固定核心成交额占比：' + number(metric.fixed_core_amount_share_pct, '%')
          + '<br>相邻新增成交额：' + number(metric.interval_amount_yuan == null ? null : metric.interval_amount_yuan / 10000, '万元') + '</p>' : '')
        + (row.fixed_core.length ? '<dl>' + row.fixed_core.map(function (stock) {
          return '<dt>' + esc(stock.stock_name) + ' · ' + esc(stock.stock_code) + '</dt><dd>较选定时价格变化 '
            + number(stock.price_change_since_selection_pct, '%') + '<br>选定于 ' + esc(stock.selected_at) + '</dd>';
        }).join('') + '</dl><small>固定核心按起点成交额选定，价格变化不是策略收益或龙头资格。</small>' : '')
        + (row.reason_labels.length ? '<p>待满足：' + esc(row.reason_labels.join('；')) + '</p>' : '')
        + (row.comparison_break_label ? '<p>本段起点：' + esc(row.comparison_break_label) + '</p>' : '')
        + '<p>下一步：' + esc(row.next_condition) + '</p></article>';
    }).join('') + '</div>';
    const prep = mainline.preparation;
    if (prep) {
      body += '<p class="slab-note">' + esc(prep.summary) + '</p><div class="slab-playbook-grid">'
        + (prep.sector_watch_rows || []).map(function (row) {
          return '<article class="slab-playbook"><h4>' + esc(row.name) + '</h4><p>上一完成日观察，尚未确认今日持续主线。</p><p>重新确认：'
            + esc(row.confirmation_condition) + '</p><p>失效：' + esc(row.abandon_condition) + '</p></article>';
        }).join('') + '</div>';
    }
    const cohorts = mainline.market_cohorts && mainline.market_cohorts.length ? mainline.market_cohorts : (prep && prep.market_cohort_rows) || [];
    if (cohorts.length) body += '<p class="slab-note">行情行为分组：' + cohorts.map(row => esc(row.name)).join('、') + '。这些分组不占行业与题材的成分采集名额。</p>';
    body += '<details id="slabMainlineRules"><summary>查看主线候选规则、阶段和证据边界</summary><p>' + esc(mainline.rule_summary || '开盘后依当时完整来源计算。') + '</p><dl>'
      + mainline.stage_definitions.map(row => '<dt>' + esc(row.name) + '</dt><dd>' + esc(row.next_condition) + '</dd>').join('')
      + '</dl><p>' + esc(mainline.evidence_boundary || '历史名单不延用为今日确认，下一交易时段须重新采集。') + '</p></details>';
    return body;
  }

  function renderEmotionSystem(context) {
    const target = $('slabEmotionSystem');
    if (!target) return;
    const expanded = Array.from(target.querySelectorAll('details[open]'), row => row.id);
    if (!context || context.schema !== 'emotion_system_context.v1' || context.production_eligible !== false) {
      target.innerHTML = '<div class="slab-empty">情绪体系输入不可用；不沿用旧阶段。</div>';
      return;
    }
    const emotion = context.emotion;
    let body = '<p class="slab-note">' + esc(context.summary || '按同一观察时点连接情绪、主线、战法、因子、买卖点与风控。') + '</p>';
    if (emotion) {
      body += '<p class="slab-note"><strong>' + esc(emotion.label) + '</strong> · 日线截至 ' + esc(emotion.data_as_of)
        + ' · ' + esc(emotion.breadth_trend.label) + ' · 当前细分连续观察 ' + esc(emotion.consecutive_observed_sessions)
        + ' 个交易日<br>当日原始态：' + esc(emotion.raw_baseline_label) + '；慢速确认态：' + esc(emotion.confirmed_baseline_label)
        + (emotion.baseline_agreement ? '。' : '。两者尚未一致，旧战法路线继续遵循原确认规则。')
        + '<br>' + esc(emotion.research_action) + '</p><div class="slab-playbook-grid">'
        + emotion.axes.map(function (row) {
          return '<article class="slab-playbook"><strong>' + esc(row.name) + '</strong><p>'
            + (row.value == null ? '输入不足' : fn(row.value, 1) + ' / 100')
            + '</p><small>较上一完整交易日：' + (row.change_points == null ? '不可比较' : fn(row.change_points, 1) + ' 点') + '</small></article>';
        }).join('') + '</div><p class="slab-note">'
        + emotion.price_direction.map(function (row) {
          return row.sessions + '日市场中位涨跌链：' + (row.median_move_chain_pct == null ? '输入不足' : fn(row.median_move_chain_pct, 2) + '%');
        }).join('；') + '<br>' + esc(emotion.price_direction_definition) + '<br>' + esc(emotion.method)
        + ' 这些强度不是胜率，日线结论不冒充盘中实时情绪。</p><details id="slabEmotionRaw"><summary>查看涨跌、涨停梯队、炸板与成交明细</summary><dl>'
        + emotion.raw_metrics.map(function (row) {
          return '<dt>' + esc(row.name) + '</dt><dd>' + (row.value == null ? '输入不足' : fn(row.value, row.unit === '家' || row.unit === '板' ? 0 : 2) + esc(row.unit)) + '</dd>';
        }).join('') + '</dl></details>';
    }
    body += mainlineSystemHTML(context.mainline_lifecycle);
    body += '<details id="slabEmotionPhases"><summary>查看细分阶段定义与适用观察</summary><dl>'
      + context.phase_definitions.map(function (row) {return '<dt>' + esc(row.name) + '</dt><dd>' + esc(row.action) + '</dd>';}).join('')
      + '</dl></details><ol>' + context.workflow.map(function (row) {
        return '<li><strong>' + esc(row.name) + '</strong>：' + esc(row.requirement) + '</li>';
      }).join('') + '</ol><p class="slab-note">' + esc(context.commission.label)
      + ' 总体回测尚未因本次细分而启动。</p>';
    target.innerHTML = body;
    target.querySelectorAll('details').forEach(row => { row.open = expanded.includes(row.id); });
  }

  function renderDevelopmentPatterns(catalog) {
    renderEmotionSystem(catalog && catalog.emotion_system);
    if (window.RegisPlaybookDevelopment) window.RegisPlaybookDevelopment.render(catalog && catalog.emotion_system && catalog.emotion_system.playbook_development);
    renderCloudJointObservation(catalog && catalog.cloud_joint_observation);
    const target = $('slabDevelopmentPatterns');
    const factors = $('slabDevelopmentFactors');
    if (!target || !factors) return;
    if (!catalog || !Array.isArray(catalog.playbooks)) {
      target.innerHTML = '<div class="slab-empty">新增研究规则暂不可用；不沿用旧准入状态。</div>';
      factors.innerHTML = '';
      return;
    }
    target.innerHTML = catalog.playbooks.map(function (row) {
      return '<article class="slab-playbook is-blocked"><div class="slab-playbook-title"><strong>' + esc(row.name)
        + '</strong><span>' + esc((row.state_names || []).join('、')) + ' · 待验证</span></div><p>' + esc(row.characteristic)
        + '</p><dl><div><dt>入场条件</dt><dd>' + esc(row.entry) + '</dd></div><div><dt>风险线</dt><dd>' + esc(row.stop)
        + '</dd></div><div><dt>退出条件</dt><dd>' + esc(row.exit) + '</dd></div></dl><footer>' + esc(row.related_rejected_family) + '</footer></article>';
    }).join('');
    if (catalog.event_replay && catalog.event_replay.implemented === true) {
      target.insertAdjacentHTML('beforeend', '<p class="slab-empty">' + esc(catalog.event_replay.description) + '</p>');
    }
    if (catalog.diagnostic_controls && catalog.diagnostic_controls.implemented === true) {
      target.insertAdjacentHTML('beforeend', '<p class="slab-empty">' + esc(catalog.diagnostic_controls.description) + '</p>');
    }
    if (catalog.corrected_factor_replay && catalog.corrected_factor_replay.implemented === true) {
      target.insertAdjacentHTML('beforeend', '<p class="slab-empty" style="grid-column:1 / -1;text-align:left" data-crowding-replay>' + esc(catalog.corrected_factor_replay.description) + '</p>');
    }
    factors.innerHTML = (catalog.factors || []).map(function (row) {
      return '<article class="slab-playbook is-blocked"><div class="slab-playbook-title"><strong>' + esc(row.name)
        + '</strong><span>可计算 · 收益未验证</span></div><p>' + esc(row.formula) + '</p><footer>' + esc(row.characteristic) + '</footer></article>';
    }).join('');
    if (catalog.signal_first_factor_attribution && catalog.signal_first_factor_attribution.implemented === true) {
      factors.insertAdjacentHTML('beforeend', '<p class="slab-empty" style="grid-column:1 / -1;text-align:left" data-factor-attribution>' + esc(catalog.signal_first_factor_attribution.description) + '</p>');
    }
  }

  async function init() {
    bindEvents();
    loadResearchCatalogue();
    syncActiveLayer();
    loadRotationEvidence();
    _rotationTimer = setInterval(loadRotationEvidence, 60000);
    scheduleWorkflowSync(30000); // Joint research must refresh even if the legacy workbench is unavailable.
    api('/api/strategy-lab/development-catalog').then(renderDevelopmentPatterns);
    const requestedRun = new URL(window.location.href).searchParams.get('run');
    _historyFollowLatest = !requestedRun;
    api('/api/strategy-route/research?view=dashboard&account_access=false').then(renderStateRoute);
    api('/static/strategy_evidence_status.json?v=8').then(renderStrategyEvidence);
    // 批量生成候选池（2026-08-14）：后台运行中则自动轮询
    api('/api/strategy-lab/batch-status').then(function (b) {
      renderBatch(b);
      if (b && b.running) { _batchTimer = setTimeout(_labBatchPoll, 3000); }
    });
    // 首屏只取轻量状态；旧服务没有 workflow 计数时才回退到完整公开因子清单。
    api('/api/factor-system/status?view=summary').then(function (status) {
      _factorStatus = status;
      _factorWorkflow = status && status.workflow ? status.workflow : null;
      renderSealedResearch(status && status.recent_sealed_research);
      renderEmotionResearchMatrix(status && status.leader_limit_program);
      updateResearchOverview();
      if (_strictPoolCount == null) {
        loadFactorSourceArchive(status).then(function () {
          if (_lastBatchState) renderBatch(_lastBatchState);
        });
      }
    });
    await refreshWorkbench();
    if (requestedRun) {
      const url = new URL(window.location.href);
      url.hash = 'slabEvidenceLayer';
      window.history.replaceState(null, '', url);
      syncActiveLayer();
      if ($('slabCurrentEvidence')) $('slabCurrentEvidence').open = true;
      loadEvidenceReport(requestedRun, true);
    }
    scheduleWorkflowSync(30000);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      stopWorkflowSync();
      stopForwardSync(false);
      if (_forwardRunId && $('slabForwardSync')) $('slabForwardSync').textContent = '页面隐藏，前向刷新已暂停。';
      return;
    }
    scheduleWorkflowSync(0);
    loadRotationEvidence();
    if (_activeReport && _activeReport.run_id && _forwardRunId === _activeReport.run_id) {
      syncForward(_activeReport.run_id);
    }
  });
  window.addEventListener('beforeunload', function () {
    if (_rotationTimer) clearInterval(_rotationTimer);
    if (_reportAbort) _reportAbort.abort();
    _reportRequestSeq += 1;
    _apiRequests.forEach(function (entry) { entry.controller.abort('navigation'); });
    _apiRequests.clear();
    if (_curveResizeFrame) cancelAnimationFrame(_curveResizeFrame);
    if (_curveChart) { disposeChart(_curveChart, 'historical'); _curveChart = null; }
    if (_forwardChart) { disposeChart(_forwardChart, 'forward'); _forwardChart = null; }
    stopForwardSync(true);
    stopWorkflowSync();
  });
  window.addEventListener('hashchange', syncActiveLayer);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
  window._labValidate = labValidate;
  window._labSave = labSave;
  window._labRun = labRun;
  window._labClone = labClone;
  window._labSyncFromDsl = labSyncFromDsl;
})();
