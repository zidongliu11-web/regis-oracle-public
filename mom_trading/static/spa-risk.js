/* Regis Oracle SPA — 风险页 */
(function(RegisSpa){
'use strict';
if (!RegisSpa || typeof RegisSpa.core?.api !== 'function') {
  throw new Error('Regis SPA core must load before risk module');
}
const {
  $, state, api, apiError, cc, decisionGateFacts, esc, escAttr, fn, fpp,
  truthLabel, RISK_KIND_LABELS, echartsTheme, whenEcharts, showEchartsRetry,
} = RegisSpa.core;

async function renderRisk() {
  const renderGeneration = state.navSequence;
  const container = $('[data-page="risk"]');
  container.innerHTML = `
    <header class="workflow-header"><div><small>账户事实 → 风险预算 → 逐股约束</small><h2>风险与仓位控制</h2><p>集中查看持仓、剩余现金、可卖数量及风险缺口。仓位试算不等于真实买入计划。</p></div><div class="workflow-actions"><a href="/positions">账户中心</a><a href="/next-session-plan">明日计划</a></div></header>
    <section class="workflow-card"><h3>当前账户事实</h3><div id="riskAccountFacts" aria-live="polite">正在读取同一份持仓与现金账本…</div></section>
    <div id="riskLoadState" class="stale-banner stale-warn" role="status" hidden></div>
    <div class="summary-strip" style="margin-bottom:16px">
      <div class="sum-item"><span class="sum-val" id="riskScoreVal">—</span><span class="sum-lbl">风险警报指数</span></div>
      <div class="sum-item"><span class="sum-val" id="riskLevelVal">—</span><span class="sum-lbl">风险等级</span></div>
      <div class="sum-item"><span class="sum-val" id="var95Val">—</span><span class="sum-lbl">日VaR(95%)</span></div>
      <div class="sum-item"><span class="sum-val" id="betaVal">—</span><span class="sum-lbl">组合Beta</span></div>
      <div class="sum-item"><span class="sum-val c-bear" id="ddVal">—</span><span class="sum-lbl">当前回撤</span></div>
    </div>
    <div class="stale-banner" id="riskScoreMeaning" style="margin:-8px 0 16px">规则化警报指数，不是亏损概率或收益预测；集中度按协方差边际风险贡献计算，不按持仓只数。</div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">数据同步账本</span>
        <span style="display:flex;align-items:center;gap:8px">
          <span class="card-badge" id="systemEvidenceBadge">按需加载</span>
          <button class="btn btn-sm" id="systemEvidenceLoad" type="button" data-action="load-risk-evidence">加载完整证据</button>
        </span>
      </div>
      <div class="card-body" id="systemEvidenceBody"><div class="empty-state">风控摘要会自动刷新；完整研究证据仅在你点击后读取一次。</div></div>
    </div>

    <!-- 风控驾驶舱 4 状态卡：执行状态 / 市场regime / 交易容量 / 单一集中度 -->
    <div class="risk-cockpit-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
      <div class="card"><div class="card-header"><span class="card-title">执行状态</span><span class="card-badge" id="cockpitGateBadge">—</span></div><div class="card-body" id="cockpitGate"><div class="empty-state">—</div></div></div>
      <div class="card"><div class="card-header"><span class="card-title">市场 Regime</span><span class="card-badge" id="cockpitRegimeBadge">—</span></div><div class="card-body" id="cockpitRegime"><div class="empty-state">—</div></div></div>
      <div class="card"><div class="card-header"><span class="card-title">交易容量</span><span class="card-badge" id="cockpitCapacityBadge">—</span></div><div class="card-body" id="cockpitCapacity"><div class="empty-state">—</div></div></div>
      <div class="card"><div class="card-header"><span class="card-title">单一集中度</span><span class="card-badge" id="cockpitConcentrationBadge">—</span></div><div class="card-body" id="cockpitConcentration"><div class="empty-state">—</div></div></div>
    </div>

    <div class="risk-content-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <!-- Left -->
      <div style="display:flex;flex-direction:column;gap:16px">
        <div class="card">
          <div class="card-header"><span class="card-title">VaR 在险价值</span></div>
          <div class="card-body" style="text-align:center">
            <div class="num num-5xl" id="varGauge95" style="margin:16px 0">—</div>
            <div style="color:var(--text-tertiary);font-size:var(--text-xs)">历史模拟单日损失的95%分位数（非最大亏损）</div>
            <div style="margin-top:8px;font-size:var(--text-sm);color:var(--text-secondary)">99%: <strong id="varGauge99">—</strong></div>
            <div style="margin-top:12px;font-size:var(--text-xs);color:var(--text-tertiary)">
              日波动 <strong id="portVol">—</strong> · 市值 <strong id="portValue">—</strong>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><span class="card-title">回撤追踪</span></div>
          <div class="card-body">
            <div class="risk-grid">
              <div class="risk-metric"><div class="num" id="ddPeak">—</div><div class="lbl">峰值市值</div></div>
              <div class="risk-metric"><div class="num" id="ddCurrent">—</div><div class="lbl">当前市值</div></div>
              <div class="risk-metric"><div class="num" id="ddUnrealized">—</div><div class="lbl">未实现盈亏</div></div>
              <div class="risk-metric"><div class="num" id="ddRealized">—</div><div class="lbl">已实现盈亏</div></div>
            </div>
            <div style="text-align:center;margin-top:12px"><span class="c-dim" style="font-size:var(--text-xs)">状态: </span><strong id="ddStatus">—</strong></div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><span class="card-title">风控告警</span><span class="card-badge" id="alertCount">0</span></div>
          <div class="card-body no-pad" id="alertBody" style="max-height:200px;overflow-y:auto"></div>
        </div>
      </div>

      <!-- Right -->
      <div style="display:flex;flex-direction:column;gap:16px">
        <div class="card">
          <div class="card-header"><span class="card-title">行业暴露</span><span style="font-size:var(--text-xs);color:var(--text-tertiary)" id="industryHHI"></span></div>
          <div class="card-body no-pad" id="industryBody" style="max-height:300px;overflow-y:auto"></div>
        </div>

        <div class="card">
          <div class="card-header"><span class="card-title">Beta 暴露</span></div>
          <div class="card-body no-pad" id="betaBody"></div>
        </div>

        <div class="card">
          <div class="card-header"><span class="card-title">风险雷达（6维）</span></div>
          <div class="card-body no-pad"><div class="chart-echarts" id="riskRadarChart"></div></div>
        </div>

        <div class="card" id="sizingCard">
          <div class="card-header"><span class="card-title">假设资金试算 · 非账户买入建议</span></div>
          <div class="card-body">
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">
              <label for="sizingCapital" style="font-size:var(--text-xs);color:var(--text-tertiary);white-space:nowrap">本金 ¥</label>
              <input class="input" type="number" id="sizingCapital" value="10000" min="1000" max="10000000" step="1000" style="width:120px">
              <button class="btn btn-primary btn-sm" type="button" data-action="risk-sizing">计算</button>
            </div>
            <div id="sizingResult"></div>
          </div>
        </div>

        <div class="card" id="tierCard">
          <div class="card-header"><span class="card-title">仓位方案对比</span></div>
          <div class="card-body no-pad">
            <div class="table-wrap" style="max-height:240px"><table class="data-table">
              <thead><tr><th>本金</th><th>级别</th><th>持仓</th><th>单只%</th><th>最高价</th></tr></thead>
              <tbody id="tierTbody"></tbody></table></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 风险线只按后端 risk_levels 元数据分层；缺元数据时明确未知。 -->
    <div class="card" style="margin-top:16px">
      <div class="card-header">
        <span class="card-title">分层风险线</span>
        <span class="card-badge">active 为当前风险线 · 其余仅研究</span>
      </div>
      <div class="card-body no-pad" id="stopLevelsBody"><div class="empty-state">—</div></div>
    </div>

    <!-- 引擎风控 & 仓位策略（任务书 Step 2） -->
    <div class="card" style="margin-top:16px">
      <div class="card-header">
        <span class="card-title">手工持仓边界审计</span>
        <span class="card-badge" id="engProfileLabel">稳健</span>
      </div>
      <div class="card-body">
        <div id="engineProfileControls" style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
          <span style="font-size:var(--text-xs);color:var(--text-tertiary)">风险偏好</span>
          <button class="btn btn-sm btn-eng-prof" type="button" data-prof="conservative" data-action="risk-profile">保守</button>
          <button class="btn btn-sm btn-eng-prof active" type="button" data-prof="moderate" data-action="risk-profile">稳健</button>
          <button class="btn btn-sm btn-eng-prof" type="button" data-prof="aggressive" data-action="risk-profile">进取</button>
          <span style="font-size:12px;color:var(--text-tertiary);margin-left:8px">边界为先验分层，非回测调参</span>
        </div>
        <div id="engineRiskBody"><div class="empty-state">加载中…</div></div>
      </div>
    </div>
  `;

  // Fetch risk data
  const accountRequest = api('/api/timing/portfolio').then(portfolio => {
    if (state.page === 'risk' && state.navSequence === renderGeneration) {
      $('#riskAccountFacts').innerHTML = RegisSpa.workflow.accountHtml(portfolio) + RegisSpa.workflow.riskPreparationHtml(portfolio);
    }
    return portfolio;
  });
  const [report, portfolio] = await Promise.all([
    api('/api/risk/report'),
    accountRequest,
  ]);
  if (state.page !== 'risk' || state.navSequence !== renderGeneration) return;
  const riskLoadState = $('#riskLoadState');
  if (report) {
    if (riskLoadState) riskLoadState.hidden = true;
    renderRiskReport(report, portfolio);
  } else if (riskLoadState) {
    riskLoadState.hidden = false;
    riskLoadState.textContent = apiError('/api/risk/report', '风控摘要暂不可用');
  }
  if (state.riskEvidencePayload) {
    renderSystemEvidence(state.riskEvidencePayload.system_evidence, state.riskEvidencePayload.strategy_lab);
  }

  // Fetch sizing tiers
  const tiers = await api('/api/sizing/tiers');
  if (state.page !== 'risk' || state.navSequence !== renderGeneration) return;
  if (tiers?.locked) {
    const sizingInput = $('#sizingCapital');
    if (sizingInput) sizingInput.disabled = true;
    const sizingButton = $('#sizingCard button');
    if (sizingButton) sizingButton.disabled = true;
    const sizingResult = $('#sizingResult');
    if (sizingResult) sizingResult.innerHTML = '<div class="empty-state">V233纯511880路径仅允许纸面观察；真钱仓位试算仍锁定。</div>';
    const tierBody = $('#tierTbody');
    if (tierBody) tierBody.innerHTML = `<tr><td colspan="5"><div class="empty-state">纸面权重仅用于观察，不转换成真钱买入仓位或订单</div></td></tr>`;
  } else if (tiers) {
    renderTiersTable(tiers.tiers||[]);
  } else {
    const tierBody = $('#tierTbody');
    if (tierBody) tierBody.innerHTML = `<tr><td colspan="5"><div class="empty-state" role="status">${esc(apiError('/api/sizing/tiers', '仓位方案暂不可用'))}</div></td></tr>`;
  }

  // Sizing fetch
  window._fetchSizing = async function() {
    const capital = parseInt($('#sizingCapital').value) || 10000;
    const d = await api('/api/sizing?capital='+capital);
    if (!d) {
      const el = $('#sizingResult');
      if (el) el.innerHTML = `<div class="empty-state" role="status">${esc(apiError('/api/sizing?capital='+capital, '仓位试算暂不可用'))}</div>`;
      return;
    }
    if (d.locked) {
      const el = $('#sizingResult');
      if (el) el.innerHTML = `<div class="empty-state">${esc(d.note||'当前策略没有买入动作')}</div>`;
      return;
    }

    const nextTier = d.next_tier;
    const nextInfo = nextTier ? `<div style="margin-top:8px;padding:8px 12px;background:rgba(255,184,0,0.08);border-radius:8px;font-size:var(--text-xs)">
      追加 <strong>¥${fn(nextTier.gap,0)}</strong> 可升级到 <strong>${nextTier.tier}</strong>（${nextTier.positions}只）</div>` : '';

    const el = $('#sizingResult');
    if (el) el.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:var(--text-xs)">
        <div><span class="c-dim">级别</span><br><strong>${esc(d.tier)}</strong></div>
        <div><span class="c-dim">建议持仓</span><br><strong>${d.suggested_positions} 只</strong></div>
        <div><span class="c-dim">单只占比</span><br><strong>${d.max_single_pct}%</strong></div>
        <div><span class="c-dim">单行业上限</span><br><strong>${d.max_industry_pct}%</strong></div>
        <div><span class="c-dim">每只资金</span><br><strong>¥${fn(d.per_position_capital,0)}</strong></div>
        <div><span class="c-dim">最高股价</span><br><strong>¥${d.max_stock_price.toFixed(2)}</strong></div>
        <div><span class="c-dim">现金缓冲</span><br><strong>${d.cash_buffer_pct}%</strong></div>
        <div><span class="c-dim">最低分散</span><br><strong>${d.min_diversified_positions} 只</strong></div>
      </div>
      <div style="margin-top:8px;font-size:12px;color:var(--text-tertiary);line-height:1.5">${(d.notes||[]).map(n=>'• '+esc(n)).join('<br>')}</div>
      ${nextInfo}`;
  };

  // 引擎风控 & 仓位策略
  window._engineProfile = 'moderate';
  window._setEngineProfile = function(p) {
    window._engineProfile = p;
    document.querySelectorAll('.btn-eng-prof').forEach(b => {
      b.classList.toggle('active', b.getAttribute('data-prof') === p);
    });
    renderEngineRisk();
  };
  renderEngineRisk();

  // Auto-refresh risk
  if (state._riskInterval) clearInterval(state._riskInterval);
  state._riskInterval = setInterval(async () => {
    if (document.hidden || state.page !== 'risk' || state.navSequence !== renderGeneration) return;
    const [r, portfolio] = await Promise.all([
      api('/api/risk/report'),
      api('/api/timing/portfolio'),
    ]);
    if (state.page !== 'risk' || state.navSequence !== renderGeneration) return;
    $('#riskAccountFacts').innerHTML = RegisSpa.workflow.accountHtml(portfolio) + RegisSpa.workflow.riskPreparationHtml(portfolio);
    if (r) {
      const status = $('#riskLoadState');
      if (status) status.hidden = true;
      renderRiskReport(r, portfolio);
    } else {
      const status = $('#riskLoadState');
      if (status) { status.hidden = false; status.textContent = apiError('/api/risk/report', '风控摘要刷新失败'); }
    }
    renderEngineRisk();
  }, 60000);
}

async function loadRiskEvidence() {
  const renderGeneration = state.navSequence;
  if (state.riskEvidencePayload) {
    renderSystemEvidence(state.riskEvidencePayload.system_evidence, state.riskEvidencePayload.strategy_lab);
    return;
  }
  if (state.riskEvidenceLoading) return;
  state.riskEvidenceLoading = true;
  const button = $('#systemEvidenceLoad');
  const body = $('#systemEvidenceBody');
  const badge = $('#systemEvidenceBadge');
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  if (badge) badge.textContent = '加载中';
  if (body) body.innerHTML = '<div class="empty-state" role="status">正在读取完整研究证据…</div>';
  const report = await api('/api/risk/report?view=evidence');
  if (state.page !== 'risk' || state.navSequence !== renderGeneration) {
    state.riskEvidenceLoading = false;
    return;
  }
  state.riskEvidenceLoading = false;
  if (!report?.system_evidence) {
    if (button && button.isConnected) { button.disabled = false; button.removeAttribute('aria-busy'); }
    if (badge) badge.textContent = '加载失败';
    if (body) body.innerHTML = `<div class="empty-state">${esc(apiError('/api/risk/report?view=evidence', '完整证据暂不可用'))}；可稍后重试。</div>`;
    return;
  }
  state.riskEvidencePayload = report;
  renderSystemEvidence(report.system_evidence, report.strategy_lab);
}

async function renderEngineRisk() {
  const d = await api('/api/engine-risk/report?profile=' + (window._engineProfile||'moderate'));
  if (!d || !d.ok) {
    const el = $('#engineRiskBody');
    if (el) el.innerHTML = '<div class="empty-state">引擎风控模块不可用</div>';
    return;
  }
  const prof = d.profile || {};
  const eng = d.engine || {};
  const live = d.live || {};
  if (d.decision_gate?.research_only) {
    const controls = $('#engineProfileControls');
    if (controls) controls.hidden = true;
    $('#engProfileLabel').textContent = '仅审计';
    const lockedBody = $('#engineRiskBody');
    if (lockedBody) lockedBody.innerHTML = `
      <div class="stale-banner stale-warn" style="margin-bottom:12px">V233纯511880最短路径仅开放纸面观察，真钱买入动作仍关闭；这里只继续检查已有手工持仓风险。</div>
      <div class="risk-grid">
        <div class="risk-metric"><div class="num">${live.positions||0} 只</div><div class="lbl">手工持仓</div></div>
        <div class="risk-metric"><div class="num">¥${fn(live.market_value,0)}</div><div class="lbl">手工持仓市值</div></div>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--text-tertiary);line-height:1.6">${esc(d.honesty||'')}</div>`;
    return;
  }

  // 边界参数 chips
  const bnd = [
    ['总仓位上限', fmtPct(prof.max_total_position)],
    ['单股上限', fmtPct(prof.max_single)],
    ['单行业上限', fmtPct(prof.max_industry)],
    ['现金下限', fmtPct(prof.min_cash)],
    ['回撤预警', '-' + Math.round((prof.drawdown_warn||0)*100) + '%'],
    ['回撤硬止损', '-' + Math.round((prof.drawdown_stop||0)*100) + '%'],
  ];
  const chips = bnd.map(([k,v]) => `
    <div class="risk-metric"><div class="num" style="font-size:var(--text-lg)">${v}</div><div class="lbl">${k}</div></div>
  `).join('');

  // 策略回测口径（Kelly/波动率缩放/回撤）
  const stra = eng.strategies || [];
  let stratHtml;
  if (!stra.length) {
    stratHtml = '<div class="empty-state">回测风控数据生成中（策略批量回测完成后自动填充）</div>';
  } else {
    stratHtml = `
      <div class="table-wrap" style="max-height:220px"><table class="data-table">
        <thead><tr><th>策略</th><th>胜率</th><th>盈亏比</th><th>全凯利</th><th>稳健档凯利</th><th>波动缩放</th><th>建议总暴露</th></tr></thead>
        <tbody>${stra.map(s => `
          <tr>
            <td>${esc(s.strategy)}</td>
            <td class="num">${s.win_rate!=null ? (s.win_rate*100).toFixed(1)+'%' : '—'}</td>
            <td class="num">${s.profit_loss_ratio!=null ? s.profit_loss_ratio.toFixed(2) : '—'}</td>
            <td class="num">${s.kelly_full!=null ? (s.kelly_full*100).toFixed(1)+'%' : '—'}</td>
            <td class="num">${s.kelly_moderate!=null ? (s.kelly_moderate*100).toFixed(1)+'%' : '—'}</td>
            <td class="num">${s.vol_scaler!=null ? s.vol_scaler.toFixed(2)+'×' : '—'}</td>
            <td class="num">${s.suggested_gross!=null ? (s.suggested_gross*100).toFixed(0)+'%' : '—'}</td>
          </tr>`).join('')}</tbody></table></div>
      <div style="margin-top:6px;font-size:12px;color:var(--text-tertiary);line-height:1.5">
        凯利/回撤来自同种子50次回测历史统计（无正期望输出0），建议总暴露 = 目标波动20% / 组合年化波动的缩放 × 稳健档总仓位上限。
      </div>`;
  }

  // 实盘持仓边界
  const liveV = live.violations || [];
  const liveW = live.warnings || [];
  const liveHtml = liveV.length || liveW.length
    ? [...liveV.map(v => `<div style="padding:6px 12px;font-size:var(--text-xs);border-bottom:1px solid var(--border-subtle)"><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#FF4458;margin-right:6px"></span>${esc(v.note)}（${fmtPct(v.value)} vs 上限 ${fmtPct(v.limit)}）</div>`),
       ...liveW.map(v => `<div style="padding:6px 12px;font-size:var(--text-xs);border-bottom:1px solid var(--border-subtle)"><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#FFB800;margin-right:6px"></span>${esc(v.note)}（${fmtPct(v.value)} vs 上限 ${fmtPct(v.limit)}）</div>`)].join('')
    : '<div style="padding:6px 12px;font-size:var(--text-xs);color:var(--bull)">✓ 手工持仓未超边界</div>';

  $('#engProfileLabel').textContent = (d.profile && d.profile.label) || '稳健';
  const el = $('#engineRiskBody');
  if (el) el.innerHTML = `
    <div class="risk-grid" style="margin-bottom:12px">${chips}</div>
    <div style="display:grid;grid-template-columns:1.4fr 1fr;gap:16px">
      <div>
        <div style="font-size:var(--text-xs);color:var(--text-tertiary);margin-bottom:6px">策略回测口径 · 凯利 & 波动率目标缩放</div>
        ${stratHtml}
      </div>
      <div>
        <div style="font-size:var(--text-xs);color:var(--text-tertiary);margin-bottom:6px">手工持仓边界（${live.positions||0} 只 · 市值 ¥${fn(live.market_value,0)}）</div>
        <div style="border:1px solid var(--border-subtle);border-radius:8px">${liveHtml}</div>
        <div style="margin-top:8px;font-size:12px;color:var(--text-tertiary);line-height:1.5">${esc(d.honesty||'')}</div>
      </div>
    </div>`;
}

function fmtPct(v) {
  if (v==null || isNaN(v)) return '—';
  return (v*100).toFixed(0) + '%';
}

function renderRiskReport(d, portfolio = null) {
  if (d.system_evidence) renderSystemEvidence(d.system_evidence, d.strategy_lab);
  renderCockpitCards(d, portfolio);
  const alertIndex = d.risk_alert_index ?? d.risk_score;
  $('#riskScoreVal').textContent = alertIndex==null ? '无法评估' : alertIndex+'/100';
  const riskMeaning = $('#riskScoreMeaning');
  if (riskMeaning) riskMeaning.textContent = d.risk_alert_index_semantics || '规则化风险警报指数，不是亏损概率或收益预测。';
  $('#riskLevelVal').textContent = d.risk_level||'—';

  const v = d.var || {};
  const accountVar95 = v.var_95_account_pct ?? v.var_95_pct;
  $('#var95Val').textContent = v.var_95==null?'—':fn(accountVar95,2)+'%账户';
  $('#varGauge95').textContent = v.var_95==null?'无法计算':'¥'+fn(v.var_95,0)+' ('+fn(accountVar95,2)+'%账户)';
  $('#var95Val').classList.toggle('c-bull', v.var_95 != null);
  $('#varGauge95').classList.toggle('c-bull', v.var_95 != null);
  $('#varGauge99').textContent = v.var_99==null?'—':'¥'+fn(v.var_99,0)+' ('+fn(v.var_99_account_pct ?? v.var_99_pct,2)+'%账户)';
  $('#portVol').textContent = v.portfolio_daily_vol_account_pct==null?'—':fn(v.portfolio_daily_vol_account_pct,2)+'%账户';
  $('#portValue').textContent = v.total_value==null?'—':'¥'+fn(v.total_value,0);

  const b = d.beta || {};
  const accountBeta = b.account_beta ?? b.beta;
  $('#betaVal').textContent = accountBeta!=null?Number(accountBeta).toFixed(2):'—';
  const betaBody = $('#betaBody');
  if (betaBody && b.beta != null && b.details?.length) {
    betaBody.innerHTML = `<div style="padding:12px;font-size:var(--text-xs);font-weight:600">账户Beta: <span class="c-accent">${Number(accountBeta).toFixed(2)}</span> · 持仓内部 ${Number(b.beta).toFixed(2)} · 账户暴露 ${fn(b.account_exposure_pct,2)}%</div>
      <div class="factor-rows" style="padding:0 12px 12px">${b.details.map(d => `
        <div class="factor-row">
          <span class="factor-label">${esc(d.name||d.code)}</span>
          <span class="factor-track"><span class="factor-fill ${d.beta>1?'bull':'bear'}" style="transform:scaleX(${d.beta/2})"></span></span>
          <span class="factor-val">${d.beta.toFixed(2)}</span>
        </div>
      `).join('')}</div>`;
  } else if (betaBody) {
    betaBody.innerHTML = `<div class="empty-state">${esc(b.exposure||'无持仓数据')}</div>`;
  }

  const dd = d.drawdown || {};
  $('#ddVal').textContent = dd.current_drawdown_pct==null?'—':dd.current_drawdown_pct.toFixed(1)+'%';
  $('#ddPeak').textContent = dd.peak_value==null?'—':'¥'+fn(dd.peak_value,0);
  $('#ddCurrent').textContent = dd.current_value==null?'—':'¥'+fn(dd.current_value,0);
  $('#ddUnrealized').innerHTML = dd.unrealized_pnl==null ? '—' :
    `<span class="${cc(dd.unrealized_pnl)}">${dd.unrealized_pnl>0?'+':''}¥${fn(dd.unrealized_pnl,0)}</span>`;
  $('#ddRealized').innerHTML = dd.realized_pnl==null ? '—' :
    `<span class="${cc(dd.realized_pnl)}">${dd.realized_pnl>0?'+':''}¥${fn(dd.realized_pnl,0)}</span>`;
  $('#ddStatus').textContent = dd.intraperiod_path_complete === false
    ? `${dd.status||'—'} · 仅${Number(dd.observations||0)}个观测点，不代表完整期间路径`
    : (dd.status||'—');

  const ind = d.industry || {};
  $('#industryHHI').textContent = ind.max_account_exposure_pct==null
    ? '账户暴露：—' : '最大行业账户暴露: '+fn(ind.max_account_exposure_pct,2)+'%';
  const ibody = $('#industryBody');
  if (ibody && ind.exposures?.length) {
    ibody.innerHTML = ind.exposures.map(e => `
      <div class="factor-row" style="padding:8px 16px">
        <span class="factor-label">${esc(e.industry)}</span>
        <span class="factor-track"><span class="factor-fill ${e.risk==='high'?'bull':e.risk==='medium'?'bear':''}" style="transform:scaleX(${Math.min(1,e.pct/33)})"></span></span>
        <span class="factor-val">${fn(e.account_exposure_pct,2)}%账户</span>
        <span style="font-size:12px;color:var(--text-tertiary);width:60px;text-align:right">¥${fn(e.value,0)}</span>
      </div>
    `).join('');
  } else if (ibody) {
    ibody.innerHTML = ind.available === false
      ? '<div class="empty-state">行业证据不完整，不将集中度伪装为 0。</div>'
      : '<div class="empty-state">无持仓数据</div>';
  }

  const alerts = d.alerts || [];
  $('#alertCount').textContent = alerts.length;
  const ab = $('#alertBody');
  if (ab) {
    if (!alerts.length) {
      ab.innerHTML = d.risk_assessment_status === 'insufficient'
        ? '<div style="padding:16px;color:var(--gold);font-size:var(--text-xs)">关键指标不足，无法判断是否存在风控告警。</div>'
        : '<div style="padding:16px;color:var(--bull);font-size:var(--text-xs)">✓ 当前无风控告警</div>';
    } else {
      ab.innerHTML = alerts.map(a => {
        const icon = a.level==='danger'?'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#FF4458;margin-right:6px"></span>':a.level==='warning'?'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#FFB800;margin-right:6px"></span>':'';
        return `<div style="padding:8px 16px;font-size:var(--text-xs);border-bottom:1px solid var(--border-subtle)">${icon}${esc(a.msg)}</div>`;
      }).join('');
    }
  }

  // 只画后端真实提供的四维风险；不再用行业集中度冒充流动性、
  // 也不再用 Beta 偏离冒充规模暴露。
  renderRiskRadar($('#riskRadarChart'), d);
}

function riskRadarEvidenceReady(report) {
  const beta = report?.beta || {};
  const valueAtRisk = report?.var || {};
  const drawdown = report?.drawdown || {};
  const industry = report?.industry || {};
  if (report?.risk_assessment_status !== 'complete') return false;
  if ([beta, valueAtRisk, drawdown, industry].some(section => section.available === false)) return false;
  return [beta.account_beta ?? beta.beta, valueAtRisk.portfolio_daily_vol_account_pct ?? valueAtRisk.portfolio_daily_vol_pct, drawdown.current_drawdown_pct, industry.max_account_exposure_pct ?? industry.hhi]
    .every(value => value != null && Number.isFinite(Number(value)));
}

// 风控驾驶舱 4 状态卡 + 止损止盈距离表（数据来自 /api/risk/report 新增块）
function renderCockpitCards(d, portfolio = null) {
  const gate = d.decision_gate || {};
  const gateFacts = decisionGateFacts(gate);
  const gateColor = gateFacts.ready ? 'var(--bull)' : 'var(--gold)';
  $('#cockpitGateBadge').textContent = gateFacts.ready ? '条件齐备' : '未就绪';
  $('#cockpitGate').innerHTML = `
    <div style="display:flex;flex-direction:column;gap:6px">
      <div style="font-size:var(--text-sm);font-weight:700;color:${gateColor}">${gateFacts.ready?'三项前置条件齐备':'未形成可执行交易条件'}</div>
      <div style="font-size:var(--text-xs);color:var(--text-secondary)">人工同意：${truthLabel(gateFacts.manualConsent)} · 研究推荐资格：${truthLabel(gateFacts.recommendationEligible)} · 券商连接：${truthLabel(gateFacts.brokerConnected)}</div>
      <div style="font-size:var(--text-xs);color:var(--text-tertiary);line-height:1.5">${esc((gate.reason||'—').slice(0,160))}</div>
    </div>`;
  // 账户暴露策略；只按后端明确声明展示是否已接入执行风险预算。
  const regime = d.market_regime || {};
  const vmp = d.volatility_managed_exposure || {};
  const regimeApplied = regime.applied_to_execution === true || regime.execution_policy_applied === true;
  // Index/MA evidence defines a risk ceiling only.  A green "entry allowed"
  // treatment here would contradict the stricter emotion-sector-playbook route.
  const rgColor = regime.target_exposure_pct === 0 ? 'var(--bear)' : 'var(--gold)';
  $('#cockpitRegimeBadge').textContent = regime.target_exposure_pct==null?'—':regime.target_exposure_pct+'%';
  const vmpLine = vmp.available === true && vmp.status === 'ready_research_risk_overlay'
    ? `组合20日年化波动 ${fn(vmp.current_annualized_volatility_pct,1)}% · 历史基准 ${fn(vmp.baseline_annualized_volatility_pct,1)}% · 波动系数 ${fn(vmp.volatility_multiplier,2)} · 研究目标 ${fn(vmp.target_exposure_pct,1)}%`
    : `波动仓位研究：${esc(vmp.reason || '等待完整持仓与至少272个共同交易日')}`;
  $('#cockpitRegime').innerHTML = `
    <div style="display:flex;flex-direction:column;gap:6px">
      <div style="font-size:var(--text-sm);font-weight:700;color:${rgColor}">风险预算天花板 ${regime.target_exposure_pct==null?'—':regime.target_exposure_pct+'%'}</div>
      <div style="font-size:var(--text-xs);color:var(--text-secondary)">MA20/MA60观察 ${regime.observed_target_exposure_pct==null?'—':regime.observed_target_exposure_pct+'%'} · ${regimeApplied?'已应用于执行风险预算':'未应用于执行'}</div>
      <div style="font-size:var(--text-xs);color:var(--gold)">仅表示账户最多可承受的暴露，不代表今日允许买入；新增交易以“今日决策”的情绪→板块→战法完整路由为准。</div>
      <div style="font-size:var(--text-xs);color:var(--text-secondary)">${vmpLine}</div>
      <div style="font-size:var(--text-xs);color:var(--text-tertiary);line-height:1.5">${esc(regime.reason||'—')}</div>
      <div style="font-size:12px;color:var(--text-tertiary)">波动仓位只用于风险研究；不产生买入、不直接生成卖出股数。</div>
    </div>`;
  // 交易容量（手工账本当日买卖笔数 / 持仓数）
  const cap = d.trade_capacity || {};
  const runtime = portfolio || {};
  const liveCap = runtime.trade_capacity || cap;
  const session = runtime.market_session || d.market_session || {};
  const account = runtime.summary || d.summary || {};
  const limit = Number.isFinite(Number(cap.limit)) ? Number(cap.limit) : null;
  const remaining = Number.isFinite(Number(liveCap.remaining)) ? Number(liveCap.remaining) : null;
  const sessionKnown = typeof session.is_open === 'boolean';
  const accountKnown = typeof account.account_facts_complete === 'boolean';
  const accountComplete = account.account_facts_complete === true;
  const accountGaps = Array.isArray(account.account_fact_gaps)
    ? account.account_fact_gaps.filter(gap => typeof gap === 'string' && gap.trim()) : [];
  const missingCash = account.cash_user_recorded === false;
  const capacityReady = session.is_open === true && accountComplete && remaining != null;
  const currentAvailable = capacityReady ? Math.max(0, remaining)
    : ((sessionKnown && session.is_open === false) || (accountKnown && !accountComplete) ? 0 : null);
  const capacityReason = !accountKnown || !sessionKnown ? '待核验'
    : !accountComplete ? (accountGaps.length ? `待补：${accountGaps.join('、')}` : missingCash ? '待录真实现金' : '账户事实缺项待核验')
    : session.is_open === false ? (session.label || '休市')
    : remaining == null ? '容量待核验' : '容量已核验';
  const capReached = capacityReady && currentAvailable === 0;
  const capColor = capacityReady ? (capReached ? 'var(--bear)' : 'var(--bull)') : 'var(--gold)';
  const availableText = currentAvailable == null ? '待核验' : currentAvailable+' 笔';
  $('#cockpitCapacityBadge').textContent = accountKnown && !accountComplete ? (missingCash ? '待录真实现金' : '账户事实待补') : '当前可用 '+availableText;
  $('#cockpitCapacity').innerHTML = `
    <div style="display:flex;flex-direction:column;gap:6px">
      <div style="font-size:var(--text-sm);font-weight:700;color:${capColor}">制度上限 ${limit==null?'—':limit+' 笔'} · 当前可用 ${availableText}</div>
      <div style="font-size:var(--text-xs);color:var(--text-tertiary)">已用 ${cap.used||0} 笔 · 持仓 ${cap.open_positions||0}/${cap.max_open_positions||'—'} · ${esc(capacityReason)}</div>
      ${accountKnown && !accountComplete ? '<a href="/positions#accountFacts">查看同源账户缺项与核验入口</a>' : ''}
    </div>`;
  // 单一集中度（市值占比，最大单名）
  const conc = d.concentration || {};
  const maxPos = (conc.positions||[])[0] || null;
  const concOver = conc.max_single_pct_threshold!=null && conc.max_single_pct>conc.max_single_pct_threshold;
  $('#cockpitConcentrationBadge').textContent = conc.max_single_pct==null?'—':conc.max_single_pct+'%';
  $('#cockpitConcentration').innerHTML = `
    <div style="display:flex;flex-direction:column;gap:6px">
      <div style="font-size:var(--text-sm);font-weight:700;color:${concOver?'var(--bear)':'var(--bull)'}">${conc.max_single_pct==null?'—':conc.max_single_pct+'%'}${conc.max_single_pct_threshold!=null?(' / 阈值 '+conc.max_single_pct_threshold+'%'):''}</div>
      <div style="font-size:var(--text-xs);color:var(--text-tertiary)">最大单名 ${esc(maxPos?maxPos.name:'—')}${conc.max_single_pct_threshold==null?'（无既定阈值）':''}</div>
    </div>`;
  // Position actions carry the authoritative kind/source/priority/executable
  // metadata. Legacy stop_levels are only a value fallback and stay unknown.
  const actions = Array.isArray(d.position_actions) ? d.position_actions : [];
  const legacyLevels = Array.isArray(d.stop_levels) ? d.stop_levels : [];
  const rows = actions.flatMap(action => {
    const actionLevels = Array.isArray(action.risk_levels) ? action.risk_levels : [];
    return actionLevels.map(level => ({action, level}));
  });
  if (!rows.length) {
    legacyLevels.forEach(legacy => rows.push({
      action: {code:legacy.code, name:legacy.name, current_price:legacy.current_price},
      level: {value:legacy.stop_loss},
    }));
  }
  const lb = $('#stopLevelsBody');
  if (lb) {
    if (!rows.length) {
      lb.innerHTML = '<div class="empty-state">无持仓数据</div>';
    } else {
      lb.innerHTML = '<div class="table-wrap" style="max-height:300px"><table class="data-table"><thead><tr><th>持仓</th><th>现价</th><th>层级</th><th>类型 / 数值</th><th>来源</th><th>优先级</th><th>执行属性</th><th>说明</th></tr></thead><tbody>'
        + rows.map(row => {
          const action = row.action || {};
          const level = row.level || {};
          const active = level.active === true;
          const kind = RISK_KIND_LABELS[level.kind] || level.kind || '类型未知';
          const execution = level.executable === false ? '不可执行' : level.executable === true ? '后端标记可执行' : '未知 · 不作为指令';
          const sourceLabel = {
            entry_price_and_pre_entry_completed_daily_atr14: '账户成本与建仓前审计波动',
            entry_price_percentage_reference_only: '仅账户成本比例 · 无历史波动确认',
            entry_high_water_and_audited_daily_atr14: '同一价格基准的高点与审计波动',
          }[level.source] || '来源未识别 · 仅参考';
          return `<tr><td>${esc(action.name||action.code||'名称不可用')}</td><td class="num">${fn(action.current_price,2)}</td><td>${active?'当前有效风险线':'研究/候选线'}</td><td>${esc(kind)} · ${level.value==null?'—':'¥'+fn(level.value,2)}</td><td>${esc(sourceLabel)}</td><td>${level.priority==null?'未知':fn(level.priority,0)}</td><td>${esc(execution)}</td><td>${esc(level.semantics||'元数据缺失')}</td></tr>`;
        }).join('') + '</tbody></table></div>';
    }
    if (actions.length) {
      const evidenceRows = actions.map(action => {
        const evidence = action.daily_history_evidence || d.daily_history_evidence?.details?.[action.code] || {};
        const label = evidence.status === 'complete' && evidence.historical_risk_eligible === true
          ? '历史输入核验通过' : evidence.source === 'tencent_qfq_runtime_validated' ? '公开历史 · 仅统计参考' : '历史输入待核验';
        return `<div style="margin-top:6px;overflow-wrap:anywhere">${esc(action.name || action.code || '未识别持仓')} · ${esc(label)}${evidence.data_as_of ? ' · 截至 ' + esc(evidence.data_as_of) : ''}<br><span class="text-muted">${esc(evidence.note || '本次没有匹配的日线审计证据；不把缺失当作无风险。')}</span></div>`;
      }).join('');
      lb.innerHTML = `<div class="risk-history-evidence" style="padding:12px;margin-bottom:12px;border:1px solid var(--border);font-size:var(--text-xs)"><strong>风险历史与价格基准</strong>${evidenceRows}<div style="margin-top:8px">历史输入通过不等于策略验证通过，也不授权实时买卖。停牌缺值或除权基准变化会截断绝对价格风险线的历史窗口。</div></div>` + lb.innerHTML;
    }
  }
}

function renderSystemEvidence(e, strategyLab) {
  const body = $('#systemEvidenceBody');
  const badge = $('#systemEvidenceBadge');
  if (!body || !e) return;
  const loadButton = $('#systemEvidenceLoad');
  if (loadButton) loadButton.hidden = true;
  const raw = e.raw_daily || {};
  const snap = e.research_snapshot || {};
  const sync = e.product_sync || {};
  const coverage = e.sync_coverage || {};
  const artifactLedger = e.research_artifact_ledger || {};
  const profit = e.profitability || {};
  const stabilityForward = e.stability_first_80_20?.paper_forward?.formal_forward || {};
  const stabilityExecution = e.stability_first_80_20?.execution_reality || {};
  const stabilityLotPlan = stabilityExecution.current_lot_plan || {};
  const stabilityLotQty = stabilityLotPlan.quantities || {};
  const stabilityAttribution = e.stability_first_80_20?.risk_sleeve_incremental_attribution || {};
  const stabilityAttributionTest = stabilityAttribution.period_attribution?.test_2024_2025 || {};
  const selectionBias = e.stability_first_80_20?.selection_bias_audit || {};
  const selectionComparison = selectionBias.comparison || {};
  const cashFloor = e.cash_floor_holding_horizon || e.stability_first_80_20?.cash_floor_holding_horizon || {};
  const cashFloorHorizons = cashFloor.holding_horizon_cohorts || {};
  const cashFloorPlan = cashFloor.current_lot_plans?.['5.0'] || {};
  const cashFloorHistory = cashFloor.continuous_50bp?.historical_2015_2025 || {};
  const cashFloorDailyPath = cashFloor.daily_path_metrics?.scenarios?.['50.0'] || {};
  const cashFloorDiagnostic = cashFloor.continuous_50bp?.diagnostic_2026_through_july || {};
  const cashDaily = cashFloor.daily_recovery?.scenarios?.three_bp_commission_observed_spread_diagnostic || {};
  const cashDailyStress = cashFloor.daily_recovery?.scenarios?.three_bp_commission_5bp_slippage_stress || {};
  const cashCrossSummary = (e.cash_floor_cross_product || cashFloor.cross_product || {}).cross_product_summary || {};
  const dualCash = e.dual_cash_floor || cashFloor.dual_product_floor || {};
  const dualCashBase = dualCash.scenarios?.three_bp_commission_observed_spread_diagnostic || {};
  const dualCashStress = dualCash.scenarios?.three_bp_commission_5bp_slippage_stress || {};
  const dualCashForward = dualCash.forward_tracking || {};
  const cashForward = e.cash_floor_forward_tracking || cashFloor.forward_tracking || {};
  const cashForwardEntry = cashForward.paper_entry || {};
  const cashForwardMark = cashForward.current_mark || {};
  const closest = profit.closest_candidate || {};
  const strict50 = profit.strict_pit_50_run || {};
  const v9Margin = profit.v9_margin_factor || {};
  const v14Margin = profit.v14_margin_factor || {};
  const v90Pmi = profit.v90_pmi_regime || {};
  const v104 = profit.v104_prebacktest_admission || {};
  const v121 = profit.v121_m2_acceleration || {};
  const v142 = profit.v142_prebacktest_admission || {};
  const v144 = profit.v144_prebacktest_admission || {};
  const v146 = profit.v146_prebacktest_admission || {};
  const v151 = profit.v151_roe_prebacktest_admission || {};
  const v153 = profit.v153_roe_factor_test || {};
  const v155 = profit.v155_asset_growth_prebacktest_admission || {};
  const v157 = profit.v157_asset_growth_factor_test || {};
  const v163 = profit.v163_asset_growth_prebacktest_admission || {};
  const v165 = profit.v165_asset_growth_factor_test || {};
  const v167 = profit.v167_industry_profit_breadth_admission || {};
  const v169 = profit.v169_v167_integrity_stop || {};
  const v170 = profit.v170_industry_profit_acceleration_admission || {};
  const v172 = profit.v172_v170_historical_strategy_test || {};
  const v172Train10 = v172.train_10bp || {};
  const v172Observed10 = v172.observed_10bp || {};
  const v172Observed20 = v172.observed_20bp || {};
  const v174 = profit.v174_pmi_gap_acceleration_admission || {};
  const v176 = profit.v176_v174_historical_strategy_test || {};
  const v176Train10 = v176.train_10bp || {};
  const v176Observed10 = v176.observed_10bp || {};
  const v176Observed20 = v176.observed_20bp || {};
  const v177 = profit.v177_sse_disciplinary_holding_risk_admission || {};
  const v179Seasonality = profit.v179_same_calendar_month_seasonality_admission || {};
  const v180Seasonality = profit.v180_v179_same_calendar_month_seasonality_factor_test || {};
  const v180Train = v180Seasonality.splits?.training_audit?.metrics || {};
  const v180Observed = v180Seasonality.splits?.observed_test?.metrics || {};
  const v181Filing = profit.v181_annual_filing_timeliness_admission || {};
  const v182Filing = profit.v182_v181_annual_filing_timeliness_factor_test || {};
  const v182Train = v182Filing.splits?.training_audit?.metrics || {};
  const v182Observed = v182Filing.splits?.observed_test?.metrics || {};
  const v185Dividend = profit.v185_cninfo_pre_ex_dividend_drift_factor_test || {};
  const v185Validation = v185Dividend.results?.validation || {};
  const v185Test = v185Dividend.results?.test || {};
  const v187Repurchase = profit.v187_cninfo_open_market_repurchase_drift_factor_test || {};
  const v187Validation = v187Repurchase.results?.validation || {};
  const v187Test = v187Repurchase.results?.test || {};
  const v189Lockup = profit.v189_cninfo_ipo_lockup_release_reversal_factor_test || {};
  const v189Validation = v189Lockup.results?.validation || {};
  const v189Test = v189Lockup.results?.test || {};
  const v191Contract = profit.v191_cninfo_major_contract_5d_drift_factor_test || {};
  const v191Validation = v191Contract.results?.validation || {};
  const v191Test = v191Contract.results?.test || {};
  const v192High = profit.v192_v4_52_week_high_prebacktest_admission || {};
  const v193High = profit.v193_v4_52_week_high_monthly_factor_test || {};
  const v193Training = v193High.results?.training_audit || {};
  const v193Validation = v193High.results?.validation || {};
  const v193Test = v193High.results?.test || {};
  const v194LowVol = profit.v194_v4_low_volatility_prebacktest_admission || {};
  const v195LowVol = profit.v195_v4_low_volatility_monthly_factor_test || {};
  const v195Training = v195LowVol.results?.training_audit || {};
  const v195Validation = v195LowVol.results?.validation || {};
  const v195Test = v195LowVol.results?.test || {};
  const v196Max = profit.v196_v4_max_effect_prebacktest_admission || {};
  const v197Max = profit.v197_v4_low_max_monthly_factor_test || {};
  const v197Training = v197Max.results?.training_audit || {};
  const v197Validation = v197Max.results?.validation || {};
  const v197Test = v197Max.results?.test || {};
  const v165Validation = v165.splits?.validation?.metrics || {};
  const v165Observed = v165.splits?.observed_test?.metrics || {};
  const forward = profit.forward_validation || {};
  const margin = e.research_data?.margin_history || {};
  const v12Fundamentals = e.research_data?.v12_fundamentals || {};
  const v148FilingTimes = e.research_data?.v148_cninfo_filing_times || {};
  const v149Xbrl = e.research_data?.v149_sse_xbrl_profitability || {};
  const v150Roe = e.research_data?.v150_sse_xbrl_roe || {};
  const v152Prices = e.research_data?.v152_v151_adjusted_prices || {};
  const v154AssetGrowth = e.research_data?.v154_quarterly_asset_growth || {};
  const v158EastmoneyAssetGrowth = e.research_data?.v158_eastmoney_quarterly_asset_growth || {};
  const v156Prices = e.research_data?.v156_v155_adjusted_prices || {};
  const v162OfficialAssetGrowth = e.research_data?.v162_official_quarterly_asset_growth || {};
  const v164Prices = e.research_data?.v164_v163_adjusted_prices || {};
  const v90Epoch = e.research_data?.v90_pmi_epoch || {};
  const v120Epoch = e.research_data?.v120_pbc_epoch || {};
  const v143Epoch = e.research_data?.v143_cpi_ppi_epoch || {};
  const v145Epoch = e.research_data?.v145_industrial_profit_epoch || {};
  const v166IndustryProfit = e.research_data?.v166_nbs_industry_profit_panel || {};
  const v168IndustrialPrices = e.research_data?.v168_csi300_industrial_prices || {};
  const v171IndustrialPrices = e.research_data?.v171_csi_all_share_industrial_prices || {};
  const v173PmiDemandInventory = e.research_data?.v173_nbs_pmi_demand_inventory || {};
  const v175EnergyPrices = e.research_data?.v175_csi_all_share_energy_prices || {};
  const v89RegulatoryMeasures = e.research_data?.v89_sse_regulatory_measures || {};
  const v178DisciplinaryPrices = e.research_data?.v178_sse_disciplinary_event_prices || {};
  const v183OrderBook = e.research_data?.v183_local_order_book_data_epoch || {};
  const v184CashDividend = e.research_data?.v184_cninfo_cash_dividend_data_epoch || {};
  const v186Repurchase = e.research_data?.v186_cninfo_open_market_repurchase_data_epoch || {};
  const v188Lockup = e.research_data?.v188_cninfo_ipo_lockup_release_data_epoch || {};
  const v190Contract = e.research_data?.v190_cninfo_major_contract_data_epoch || {};
  const decisionIndexes = e.research_data?.decision_indexes || {};
  const decisionCalendar = e.research_data?.decision_calendar || {};
  const benchmarkIndexes = e.research_data?.benchmark_indexes || {};
  const discovery = profit.successor_discovery || {};
  const discoveryCapReached = discovery.round_cap_reached === true ||
    ((discovery.round??0) >= (discovery.round_limit??30));
  const archivedShadow = profit.archived_shadow || {};
  const rawReason = {
    quality_audited_current_epoch: '当前数据日已完成全量质量审计',
    incremental_sync_failed: `增量同步失败 ${raw.sync_failed_symbols||0} 只，沿用已审计数据`,
    current_epoch_not_quality_audited: '最新文件尚未完成同数据日质量审计',
    daily_quality_audit_incomplete: '全量质量审计未完成',
  }[raw.ready_reason] || '状态未说明';
  if (badge) {
    badge.textContent = coverage.all_required_artifacts_accounted
      ? `${coverage.accounted_count??0}/${coverage.tracked_count??0} 项已同步可见`
      : `${coverage.accounted_count??0}/${coverage.tracked_count??0} 项已同步可见 · 仍缺 ${coverage.required_missing_count??0} 项`;
    badge.style.color = coverage.all_required_artifacts_accounted
      ? (e.decision_data_ready ? 'var(--bull)' : 'var(--gold)')
      : 'var(--bear)';
  }
  const tiles = [
    ['研究证据自动账本', artifactLedger.all_report_artifacts_indexed?'全部已索引':'索引不完整', `${artifactLedger.indexed_count??0}/${artifactLedger.report_json_count??0} JSON报告 · 元数据指纹 ${(artifactLedger.metadata_fingerprint_sha256||'—').slice(0,12)} · 新报告自动进入网站`],
    ['网站数据覆盖', `${coverage.accounted_count??0}/${coverage.tracked_count??0}`, `${coverage.artifact_present_count??0} 项物理证据 · ${coverage.gated_not_required_count??0} 项上游门禁后无需生成 · ${coverage.decision_accepted_count??0} 项通过决策数据门禁 · 已同步不等于可交易`],
    ['原始日线', raw.ready?'可用于研究':'暂不可用于新研究', `文件截至 ${raw.latest_data_as_of||raw.as_of||'—'} · 已审计至 ${raw.quality_as_of||'—'} · ${rawReason}`],
    ['研究快照', snap.ready?'已追平':'未追平', `截至 ${snap.as_of||'—'} · ${snap.universe||0} 只`],
    ['外部产品站', sync.ready?'同步成功':'同步失败', sync.error||sync.status||'—'],
    ['V12个股基本面时点', v12Fundamentals.ready?'门禁通过':'门禁失败', `${v12Fundamentals.records??0} 条 · ${v12Fundamentals.symbols??0} 只 · 超期 ${v12Fundamentals.publication_after_deadline_pct??0}% · 独立公告日 ${v12Fundamentals.separate_publication_date_coverage_pct??0}% · ROE ${v12Fundamentals.roe_coverage_pct??0}%`],
    ['V148巨潮官方公告时点', v148FilingTimes.ready?'物理与哈希通过':v148FilingTimes.status==='running'?'构建中':'验收未通过', `${v148FilingTimes.periods??0}/${v148FilingTimes.expected_periods??33} 报告期 · ${v148FilingTimes.mapped_code_periods??0} 代码期 · 覆盖 ${v148FilingTimes.coverage_pct??0}% · 收益数据${v148FilingTimes.return_data_read?'已读取':'未读取'}`],
    ['V149上交所XBRL盈利', v149Xbrl.ready?'物理与哈希通过':v149Xbrl.status==='running'?'构建中':'验收未通过', `${v149Xbrl.processed_codes??0}/${v149Xbrl.universe_codes??1842} 代码 · ${v149Xbrl.records??0} 年报 · 公告连接 ${v149Xbrl.filing_join_coverage_pct??0}% · 应有代码期 ${v149Xbrl.expected_code_period_coverage_pct??0}% · ROE ${v149Xbrl.roe_coverage_pct??0}% · 收益数据${v149Xbrl.return_data_read?'已读取':'未读取'}`],
    ['V150官方ROE时点', v150Roe.ready?'门禁通过':'候选前门禁未通过', `${v150Roe.complete_roe_records??0}/${v150Roe.source_records??0} 完整 · ${v150Roe.codes??0} 只 · ROE源覆盖 ${v150Roe.roe_source_coverage_pct??0}% · 时点覆盖 ${v150Roe.complete_point_in_time_coverage_pct??0}% · 回测${v150Roe.successor_backtest_allowed?'允许':'锁定'}`],
    ['V152专用复权行情', v152Prices.ready?'门禁通过':v152Prices.status==='blocked_before_price_read'?'等待V151准入':'验收未通过', `${v152Prices.codes_with_rows??0}/${v152Prices.eligible_codes??0} 有价 · 最低月覆盖 ${v152Prices.minimum_month_coverage_pct??0}% · 价格${v152Prices.price_data_read?'已读取':'未读取'} · 收益${v152Prices.return_data_computed?'已计算':'未计算'}`],
    ['V154季度资产增长时点', v154AssetGrowth.ready?'门禁通过':v154AssetGrowth.status==='running'?'构建中':v154AssetGrowth.status==='source_rate_limited'?'来源限流，保留断点':'验收未通过', `${v154AssetGrowth.processed_codes??0}/${v154AssetGrowth.universe_codes??1842} 代码 · ${v154AssetGrowth.records??0} 已归档 · 缺 ${v154AssetGrowth.missing_codes??0} 只 · ${v154AssetGrowth.complete_pit_records??0} 已正式验收完整点时 · 覆盖 ${v154AssetGrowth.complete_pit_coverage_pct??0}% · 年报校验 ${v154AssetGrowth.cross_validation_match_pct??0}% · 收益${v154AssetGrowth.return_data_read?'已读取':'未读取'}${v154AssetGrowth.source_error?' · '+v154AssetGrowth.source_error:''}`],
    ['V158独立资产增长时点', v158EastmoneyAssetGrowth.ready?'门禁通过':v158EastmoneyAssetGrowth.status==='data_epoch_incomplete'?'数值通过，时点失败':'验收未通过', `${v158EastmoneyAssetGrowth.periods??0}/${v158EastmoneyAssetGrowth.expected_periods??33} 期 · ${v158EastmoneyAssetGrowth.records??0} 条 · YOY ${v158EastmoneyAssetGrowth.yoy_asset_coverage_pct??0}% · 完整点时 ${v158EastmoneyAssetGrowth.complete_pit_coverage_pct??0}% · 跨源 ${v158EastmoneyAssetGrowth.cross_vendor_match_pct??0}% · 官方年报 ${v158EastmoneyAssetGrowth.official_annual_match_pct??0}% · 年报公告日零匹配 ${v158EastmoneyAssetGrowth.annual_zero_notice_match_periods?.length??0} 期 · 收益${v158EastmoneyAssetGrowth.return_data_read?'已读取':'未读取'}`],
    ['V156资产增长专用行情', v156Prices.ready?'门禁通过':v156Prices.status==='blocked_before_price_read'?'等待V155准入':'验收未通过', `${v156Prices.codes_with_rows??0}/${v156Prices.eligible_codes??0} 有价 · 最低月覆盖 ${v156Prices.minimum_month_coverage_pct??0}% · 价格${v156Prices.price_data_read?'已读取':'未读取'} · 收益${v156Prices.return_data_computed?'已计算':'未计算'}`],
    ['V162官方资产增长时点', v162OfficialAssetGrowth.ready?'官方PDF与时点门禁通过':'验收未通过', `${v162OfficialAssetGrowth.official_pdf_accepted??0}/${v162OfficialAssetGrowth.official_asset_targets??150} 官方PDF · ${v162OfficialAssetGrowth.records??0} 条 · 总体覆盖 ${v162OfficialAssetGrowth.complete_pit_coverage_pct??0}% · 2019年报 ${v162OfficialAssetGrowth.annual_2019_coverage_pct??0}% · 延期年报 ${v162OfficialAssetGrowth.delayed_reports_found??0} 份 · 查询/PDF档案 ${v162OfficialAssetGrowth.query_archives??0}/${v162OfficialAssetGrowth.pdf_archives??0} · 收益${v162OfficialAssetGrowth.return_data_read?'已读取':'未读取'}`],
    ['V164 V163专用行情', v164Prices.ready?'门禁通过':v164Prices.status==='blocked_before_price_read'?'等待V163准入':'验收未通过', `${v164Prices.codes_with_rows??0}/${v164Prices.eligible_codes??0} 有价 · ${v164Prices.rows??0} 行 · 最低月覆盖 ${v164Prices.minimum_month_coverage_pct??0}% · 价格${v164Prices.price_data_read?'已读取':'未读取'} · 收益${v164Prices.return_data_computed?'已计算':'未计算'}`],
    ['V14冻结融资融券历史', margin.passed?'哈希验收通过':'同步与哈希验收中', `${margin.date_range||'2018-01-01~2026-06-30'} · ${margin.physical_files??0}/${margin.declared_trade_dates??0} 可读 · ${margin.hash_verified_files??0} 哈希吻合`],
    ['决策指数', decisionIndexes.ready?'已追平':'未追平', `沪深300 + 上证指数 · 截至 ${decisionIndexes.common_as_of||'—'}`],
    ['交易日历', decisionCalendar.ready?'已覆盖':'未覆盖', `覆盖至 ${decisionCalendar.coverage_end||'—'} · 今天${decisionCalendar.is_trading_day?'开市':'休市/未知'}`],
    ['研究基准', benchmarkIndexes.ready?'全部追平':'存在缺口', `${benchmarkIndexes.count??0}/${benchmarkIndexes.expected_count??4} · 沪深300/中证500/中证1000/上证`],
    ['V90国家统计局PMI数据', v90Epoch.passed?'物理与哈希通过':'验收失败', `${v90Epoch.actual_months??0}/${v90Epoch.expected_months??48} 月 · ${v90Epoch.hash_verified_archives??0}/${v90Epoch.source_archives??0} 官方归档哈希吻合 · 引自国家统计局`],
    ['V120人民银行月度统计', v120Epoch.passed?'物理与哈希通过':'验收失败', `${v120Epoch.actual_months??0}/${v120Epoch.expected_months??96} 月 · ${v120Epoch.hash_verified_listings??0}/${v120Epoch.listing_archives??18} 目录 · ${v120Epoch.hash_verified_reports??0}/${v120Epoch.report_archives??96} 月报 · 内容归档 ${v120Epoch.hash_verified_content_archives??0}/${v120Epoch.report_archives??96}`],
    ['V143国家统计局CPI/PPI', v143Epoch.passed?'物理与哈希通过':'验收失败', `${v143Epoch.actual_months??0}/${v143Epoch.expected_months??120} 月 · ${v143Epoch.hash_verified_searches??0}/${v143Epoch.search_archives??40} 搜索 · ${v143Epoch.hash_verified_releases??0}/${v143Epoch.release_archives??240} 发布 · 保守次日边界 ${v143Epoch.conservative_next_day_boundaries??0}`],
    ['V145国家统计局工业利润', v145Epoch.passed?'物理与哈希通过':'验收失败', `${v145Epoch.actual_observations??0}/${v145Epoch.expected_observations??110} 观测 · ${v145Epoch.hash_verified_searches??0}/${v145Epoch.search_archives??50} 搜索 · ${v145Epoch.hash_verified_releases??0}/${v145Epoch.release_archives??110} 页面 · 官方解读回退 ${v145Epoch.official_interpretation_fallbacks??0}`],
    ['V166国家统计局行业利润面板', v166IndustryProfit.ready?'数据门禁通过':'验收失败', `${v166IndustryProfit.periods??0} 月 · ${v166IndustryProfit.industries??0} 行业 · ${v166IndustryProfit.records??0} 条 · 同比覆盖 ${v166IndustryProfit.yoy_coverage_pct??0}% · 官方不可计算 ${v166IndustryProfit.official_note_missing_yoy_records??0} 条 · 收益数据${v166IndustryProfit.return_data_read?'已读取':'未读取'} · 回测${v166IndustryProfit.successor_backtest_allowed?'允许':'锁定'}`],
    ['V168沪深300工业价格纪元', v168IndustrialPrices.ready?'物理验收通过':'验收失败', `${v168IndustrialPrices.rows??0} 日 · ${v168IndustrialPrices.first_date||'—'} 至 ${v168IndustrialPrices.last_date||'—'} · 日历缺口 ${v168IndustrialPrices.missing_calendar_dates??0} · 价格${v168IndustrialPrices.price_data_read?'已读取':'未读取'} · 收益${v168IndustrialPrices.return_data_computed?'已计算':'未计算'} · 回测锁定`],
    ['V171中证全指工业价格纪元', v171IndustrialPrices.ready?'物理验收通过':'验收失败', `${v171IndustrialPrices.rows??0} 日 · ${v171IndustrialPrices.first_date||'—'} 至 ${v171IndustrialPrices.last_date||'—'} · 日历缺口 ${v171IndustrialPrices.missing_calendar_dates??0} · 价格${v171IndustrialPrices.price_data_read?'已读取':'未读取'} · 收益${v171IndustrialPrices.return_data_computed?'已计算':'未计算'} · 唯一回测${v171IndustrialPrices.single_historical_backtest_consumed?'已消耗':v171IndustrialPrices.single_historical_backtest_allowed?'允许':'锁定'}`],
    ['V173国家统计局PMI六分项', v173PmiDemandInventory.ready?'物理与点时验收通过':'验收失败', `${v173PmiDemandInventory.actual_months??0}/${v173PmiDemandInventory.expected_months??120} 月 · ${v173PmiDemandInventory.component_records??0}/720 分项记录 · 发布页交叉核对 ${v173PmiDemandInventory.release_page_crosschecked_months??0} 月 · 不一致 ${v173PmiDemandInventory.release_page_mismatch_months??0} 月 · 物理档案 ${v173PmiDemandInventory.source_archives??0} · 收益${v173PmiDemandInventory.return_data_read?'已读取':'未读取'}`],
    ['V175中证全指能源价格纪元', v175EnergyPrices.ready?'物理验收通过':'验收失败', `${v175EnergyPrices.rows??0} 日 · ${v175EnergyPrices.first_date||'—'} 至 ${v175EnergyPrices.last_date||'—'} · 日历缺口 ${v175EnergyPrices.missing_calendar_dates??0} · 价格${v175EnergyPrices.price_data_read?'已读取':'未读取'} · 收益${v175EnergyPrices.return_data_computed?'已计算':'未计算'} · 唯一回测${v175EnergyPrices.single_historical_backtest_consumed?'已消耗':v175EnergyPrices.single_historical_backtest_allowed?'允许':'锁定'}`],
    ['V89上交所监管措施', v89RegulatoryMeasures.ready?'物理与哈希验收通过':'验收失败', `${v89RegulatoryMeasures.records??0} 条 · 直接唯一A股 ${v89RegulatoryMeasures.direct_single_a_share_records??0} 条 · 多代码 ${v89RegulatoryMeasures.direct_multiple_a_share_records??0} 条 · 无A股代码 ${v89RegulatoryMeasures.no_a_share_code_records??0} 条 · 收益${v89RegulatoryMeasures.return_data_read?'已读取':'未读取'}`],
    ['V178纪律处分事件价格纪元', v178DisciplinaryPrices.ready?'物理验收通过':'收益前永久失败', `${v178DisciplinaryPrices.stock_files??0}/${v178DisciplinaryPrices.event_codes??0} 股票文件 · 缺失 ${v178DisciplinaryPrices.missing_files??0} 只科创板 · 训练/观察完整路径 ${v178DisciplinaryPrices.complete_paths?.train??0}/${v178DisciplinaryPrices.complete_paths?.observed_test??0} · 价格${v178DisciplinaryPrices.price_data_read?'已读取':'未读取'} · 收益${v178DisciplinaryPrices.return_data_computed?'已计算':'未计算'}`],
    ['V183本地盘口数据目录', v183OrderBook.ready?'物理验收通过':'收益前永久失败', `${v183OrderBook.total_parquet_files??0} 个Parquet · 日线 ${v183OrderBook.daily_files??0}只 · 5分钟 ${v183OrderBook.minute5_files??0}只 · 五档盘口 ${v183OrderBook.realtime_trading_days??0}日/${v183OrderBook.realtime_rows??0}行 · 文件哈希登记 ${v183OrderBook.metadata_indexed_files??0} · 收益${v183OrderBook.return_data_read?'已读取':'未读取'} · 买卖提醒锁定`],
    ['V184巨潮现金分红事件', v184CashDividend.ready?'物理与哈希验收通过':'收益前失败', `${v184CashDividend.response_files??0}/${v184CashDividend.target_codes??0} 代码响应 · 有记录 ${v184CashDividend.query_ok??0} · 空记录 ${v184CashDividend.query_empty??0} · 错误 ${v184CashDividend.query_error??0} · 纯现金事件 ${v184CashDividend.eligible_events??0} · 测试期 ${v184CashDividend.test_events??0}条/${v184CashDividend.test_unique_ex_dates??0}除权日 · 哈希不一致 ${v184CashDividend.response_hash_mismatches??0} · 数据纪元收益${v184CashDividend.return_data_read?'已读取':'未读取'}`],
    ['V185除息日前三日漂移', v185Dividend.historical_factor_gate_passed?'历史门禁通过':v185Dividend.single_factor_test_consumed?'唯一检验永久失败':'等待数据', `${v185Dividend.evaluated_events??0}/${v185Dividend.scheduled_events??0} 可执行 · 验证20BP ${v185Validation.mean_net_return_pct??'—'}% / 胜率 ${v185Validation.win_rate_pct??'—'}% / Bootstrap下限 ${v185Validation.block_bootstrap_95pct_lower_pct??'—'}% · 测试20BP ${v185Test.mean_net_return_pct??'—'}% / 超额 ${v185Test.mean_market_adjusted_return_pct??'—'}% / 胜率 ${v185Test.win_rate_pct??'—'}% · ${v185Dividend.failed_gates?.length??0}项失败 · 买入提醒锁定`],
    ['V186巨潮集中竞价回购公告', v186Repurchase.ready?'物理与哈希验收通过':'收益前失败', `${v186Repurchase.archive_files??0} 年档案 · 原始公告 ${v186Repurchase.raw_announcements??0}条 · 合格且完整 ${v186Repurchase.complete_events??0}/${v186Repurchase.eligible_events??0} · 测试期 ${v186Repurchase.test_events??0}条/${v186Repurchase.test_unique_announcement_dates??0}公告日 · 哈希不一致 ${v186Repurchase.archive_hash_mismatches??0} · 数据纪元收益${v186Repurchase.return_data_read?'已读取':'未读取'}`],
    ['V187公告后20交易日漂移', v187Repurchase.historical_factor_gate_passed?'历史门禁通过':v187Repurchase.single_factor_test_consumed?'唯一检验永久失败':'等待数据', `${v187Repurchase.evaluated_events??0}/${v187Repurchase.scheduled_events??0} 可执行 · 验证20BP ${v187Validation.mean_net_return_20bp_pct??'—'}% / 胜率 ${v187Validation.win_rate_20bp_pct??'—'}% / Bootstrap下限 ${v187Validation.block_bootstrap_95pct_lower_20bp_pct??'—'}% · 测试20BP ${v187Test.mean_net_return_20bp_pct??'—'}% / 超额 ${v187Test.mean_market_adjusted_20bp_pct??'—'}% / 胜率 ${v187Test.win_rate_20bp_pct??'—'}% / Bootstrap下限 ${v187Test.block_bootstrap_95pct_lower_20bp_pct??'—'}% · ${v187Repurchase.failed_gates?.length??0}项失败 · 买入提醒锁定`],
    ['V188巨潮首发前限售股上市流通公告', v188Lockup.ready?'物理与哈希验收通过':'收益前失败', `${v188Lockup.archive_files??0} 年档案 · 原始公告 ${v188Lockup.raw_announcements??0}条 · 合格且完整 ${v188Lockup.complete_events??0}/${v188Lockup.eligible_events??0} · 测试期 ${v188Lockup.test_events??0}条/${v188Lockup.test_unique_announcement_dates??0}公告日 · 哈希不一致 ${v188Lockup.archive_hash_mismatches??0} · 数据纪元收益${v188Lockup.return_data_read?'已读取':'未读取'}`],
    ['V189限售股公告后20交易日反转', v189Lockup.historical_factor_gate_passed?'历史门禁通过':v189Lockup.single_factor_test_consumed?'唯一检验永久失败':'等待数据', `${v189Lockup.evaluated_events??0}/${v189Lockup.scheduled_events??0} 可执行 · 验证20BP ${v189Validation.mean_net_return_20bp_pct??'—'}% / 超额 ${v189Validation.mean_market_adjusted_20bp_pct??'—'}% / 胜率 ${v189Validation.win_rate_20bp_pct??'—'}% / Bootstrap下限 ${v189Validation.block_bootstrap_95pct_lower_20bp_pct??'—'}% · 测试20BP ${v189Test.mean_net_return_20bp_pct??'—'}% / 超额 ${v189Test.mean_market_adjusted_20bp_pct??'—'}% / 胜率 ${v189Test.win_rate_20bp_pct??'—'}% / Bootstrap下限 ${v189Test.block_bootstrap_95pct_lower_20bp_pct??'—'}% · 三折 ${(v189Lockup.test_time_folds_mean_net_return_20bp_pct||[]).join('/')||'—'}% · ${v189Lockup.failed_gates?.length??0}项失败 · 买入提醒锁定`],
    ['V190巨潮重大合同签订公告', v190Contract.ready?'物理与哈希验收通过':'收益前失败', `${v190Contract.archive_files??0} 年档案 · 原始公告 ${v190Contract.raw_announcements??0}条 · 合格且完整 ${v190Contract.complete_events??0}/${v190Contract.eligible_events??0} · 测试期 ${v190Contract.test_events??0}条/${v190Contract.test_unique_announcement_dates??0}公告日 · 哈希不一致 ${v190Contract.archive_hash_mismatches??0} · 数据纪元收益${v190Contract.return_data_read?'已读取':'未读取'}`],
    ['V191重大合同公告后5交易日漂移', v191Contract.historical_factor_gate_passed?'历史门禁通过':v191Contract.single_factor_test_consumed?'唯一检验永久失败':'等待数据', `${v191Contract.evaluated_events??0}/${v191Contract.scheduled_events??0} 可执行 · 验证20BP ${v191Validation.mean_net_return_20bp_pct??'—'}% / 超额 ${v191Validation.mean_market_adjusted_20bp_pct??'—'}% / 胜率 ${v191Validation.win_rate_20bp_pct??'—'}% / Bootstrap下限 ${v191Validation.block_bootstrap_95pct_lower_20bp_pct??'—'}% · 测试20BP ${v191Test.mean_net_return_20bp_pct??'—'}% / 超额 ${v191Test.mean_market_adjusted_20bp_pct??'—'}% / 胜率 ${v191Test.win_rate_20bp_pct??'—'}% / Bootstrap下限 ${v191Test.block_bootstrap_95pct_lower_20bp_pct??'—'}% · 三折 ${(v191Contract.test_time_folds_mean_net_return_20bp_pct||[]).join('/')||'—'}% · ${v191Contract.failed_gates?.length??0}项失败 · 买入提醒锁定`],
    ['V192 52周高点收益前准入', v192High.preregistration_admission_passed?'公式与门禁已冻结':'准入失败', `${v192High.v4_codes??0} 只V4日线 · 收益数据${v192High.return_data_read?'已读取':'准入时未读取'} · 唯一因子检验${v192High.single_factor_test_consumed?'已消耗':v192High.single_factor_test_allowed?'允许':'锁定'} · 不可推荐`],
    ['V193 52周高点月度因子', v193High.historical_factor_gate_passed?'历史门禁通过':v193High.single_factor_test_consumed?'唯一检验永久失败':'等待数据', `${v193High.verified_daily_files??0} 个日线文件哈希通过 · ${v193High.selected_stock_months??0}/${v193High.candidate_stock_months??0} 股票月入选 · 训练年化 ${v193Training.annualized_net_return_20bp_pct??'—'}% · 验证年化 ${v193Validation.annualized_net_return_20bp_pct??'—'}% / 超额 ${v193Validation.annualized_excess_return_pct??'—'}% · 测试年化 ${v193Test.annualized_net_return_20bp_pct??'—'}% / 总超额 ${v193Test.total_excess_return_pct??'—'}% · 三折超额 ${(v193High.test_folds||[]).map(f=>f.excess_return_pct).join('/')||'—'}% · ${v193High.failed_gates?.length??0}项失败 · 买入提醒锁定`],
    ['V194 36个月低波动收益前准入', v194LowVol.preregistration_admission_passed?'公式与门禁已冻结':'准入失败', `${v194LowVol.v4_codes??0} 只V4日线 · 收益数据${v194LowVol.return_data_read?'已读取':'准入时未读取'} · 唯一因子检验${v194LowVol.single_factor_test_consumed?'已消耗':v194LowVol.single_factor_test_allowed?'允许':'锁定'} · 不可推荐`],
    ['V195 36个月低波动月度因子', v195LowVol.historical_factor_gate_passed?'历史门禁通过':v195LowVol.single_factor_test_consumed?'唯一检验永久失败':'等待数据', `${v195LowVol.verified_daily_files??0} 个日线文件哈希通过 · ${v195LowVol.selected_stock_months??0}/${v195LowVol.candidate_stock_months??0} 股票月入选 · 训练年化 ${v195Training.annualized_net_return_20bp_pct??'—'}% · 验证年化 ${v195Validation.annualized_net_return_20bp_pct??'—'}% / 超额 ${v195Validation.annualized_excess_return_pct??'—'}% / Bootstrap下限 ${v195Validation.block_bootstrap_95pct_lower_monthly_20bp_pct??'—'}% · 测试年化 ${v195Test.annualized_net_return_20bp_pct??'—'}% / 总超额 ${v195Test.total_excess_return_pct??'—'}% · 三折超额 ${(v195LowVol.test_folds||[]).map(f=>f.excess_return_pct).join('/')||'—'}% · ${v195LowVol.failed_gates?.length??0}项失败 · 买入提醒锁定`],
    ['V196 上月MAX收益前准入', v196Max.preregistration_admission_passed?'公式与门禁已冻结':'准入失败', `${v196Max.v4_codes??0} 只V4日线 · 收益数据${v196Max.return_data_read?'已读取':'准入时未读取'} · 唯一因子检验${v196Max.single_factor_test_consumed?'已消耗':v196Max.single_factor_test_allowed?'允许':'锁定'} · 不可推荐`],
    ['V197 上月低MAX月度因子', v197Max.historical_factor_gate_passed?'历史门禁通过':v197Max.single_factor_test_consumed?'唯一检验永久失败':'等待数据', `${v197Max.verified_daily_files??0} 个日线文件哈希通过 · ${v197Max.selected_stock_months??0}/${v197Max.candidate_stock_months??0} 股票月入选 · 训练年化 ${v197Training.annualized_net_return_20bp_pct??'—'}% · 验证年化 ${v197Validation.annualized_net_return_20bp_pct??'—'}% / 超额 ${v197Validation.annualized_excess_return_pct??'—'}% / Bootstrap下限 ${v197Validation.block_bootstrap_95pct_lower_monthly_20bp_pct??'—'}% · 测试年化 ${v197Test.annualized_net_return_20bp_pct??'—'}% / 总超额 ${v197Test.total_excess_return_pct??'—'}% · 三折超额 ${(v197Max.test_folds||[]).map(f=>f.excess_return_pct).join('/')||'—'}% · ${v197Max.failed_gates?.length??0}项失败 · 买入提醒锁定`],
    ['当前盈利门禁', profit.strategy==='无有效候选'?'无有效候选':`${profit.passed_count||0}/${profit.total_count||0} 通过`, profit.strategy==='无有效候选'?'推荐、回测与资金分配保持锁定':`${profit.current_trades??'—'}笔 · 胜率 ${profit.current_win_rate_pct??'—'}% · 年化 ${profit.current_annualized_pct??'—'}%`],
    ['旧影子样本', archivedShadow.status==='revoked_not_unlockable'?'已撤销':'当前', `${archivedShadow.trades??'—'}笔 · 胜率 ${archivedShadow.win_rate_pct??'—'}% · 年化 ${archivedShadow.annualized_pct??'—'}% · 不可解锁`],
    ['严格PIT 50次', strict50.historical_robustness_gate_passed?'通过':'失败', `${strict50.positive_runs??'—'}/${strict50.total_runs??50} 盈利 · 中位数 ${strict50.median_return_pct??'—'}% · 双倍成本 ${strict50.double_cost_median_return_pct??'—'}%`],
    ['V9融资买入强度', v9Margin.statistical_gate_passed?'通过':'统计失败', `IC ${v9Margin.test_mean_ic??'—'} · 单调性 ${v9Margin.test_monotonicity??'—'} · 成本差 ${v9Margin.top_minus_middle_after_cost_pct??'—'}%`],
    ['V14融资余额加速度', v14Margin.statistical_gate_passed?'统计通过':v14Margin.status?.includes('blocked')?'等待物理重算':'统计失败', `IC ${v14Margin.test_mean_ic??'—'} · 单调性 ${v14Margin.test_monotonicity??'—'} · 成本差 ${v14Margin.top_minus_middle_after_cost_pct??'—'}%`],
    ['V90 PMI双扩张', v90Pmi.historical_strategy_gate_passed?'历史通过（仍不可推荐）':v90Pmi.available?'永久封存':'缺失', `验证年化 ${v90Pmi.validation_annualized_return_pct??'—'}% · 测试年化 ${v90Pmi.test_annualized_return_pct??'—'}% · PF ${v90Pmi.test_profit_factor??'—'} · 不可推荐`],
    ['V104回测前准入', v104.preregistration_admission_passed?'准入通过':v104.available?'回测前淘汰':'缺失', `${v104.trigger_months??0}/${v104.sample_months??0} 月触发 · 收益数据${v104.return_data_read?'已读取':'未读取'} · 阈值无独立先验 · 回测${v104.successor_backtest_allowed?'允许':'锁定'}`],
    ['V121 M2加速度', v121.historical_strategy_gate_passed?'历史通过（仍不可推荐）':v121.available?'永久封存':'缺失', `测试10BP年化 ${v121.test_10bp_annualized_return_pct??'—'}% · 胜率 ${v121.test_10bp_win_rate_pct??'—'}% · 回撤 ${v121.test_10bp_max_drawdown_pct??'—'}% · PF ${v121.test_10bp_profit_factor??'—'} · 20BP总收益 ${v121.test_20bp_total_return_pct??'—'}%`],
    ['V142同业拆借3%', v142.preregistration_admission_passed?'准入通过':v142.available?'回测前淘汰':'缺失', `${v142.trigger_months??0}/${v142.sample_months??0} 月触发 · 样本最高 ${v142.maximum_observed_rate_pct??'—'}% · 收益数据${v142.return_data_read?'已读取':'未读取'} · 回测${v142.single_historical_backtest_allowed?'允许':'锁定'}`],
    ['V144 PPI-CPI正缺口', v144.preregistration_admission_passed?'准入通过':v144.available?'回测前淘汰':'缺失', `${v144.trigger_months??0}/${v144.sample_months??0} 月触发 · 独立阶段 ${v144.positive_regimes??0} · 状态切换 ${v144.state_transitions??0} · 收益数据${v144.return_data_read?'已读取':'未读取'} · 回测${v144.single_historical_backtest_allowed?'允许':'锁定'}`],
    ['V146工业利润正增长', v146.preregistration_admission_passed?'准入通过':v146.available?'回测前淘汰':'缺失', `正/负观测 ${v146.positive_observations??0}/${v146.negative_observations??0} · 正/负阶段 ${v146.positive_regimes??0}/${v146.negative_regimes??0} · 训练负阶段 ${v146.train_negative_regimes??0} · 收益数据${v146.return_data_read?'已读取':'未读取'} · 回测${v146.single_historical_backtest_allowed?'允许':'锁定'}`],
    ['V151官方ROE十分组', v151.preregistration_admission_passed?'准入通过':v151.available?'候选前拒绝':'缺失', `${v151.records??0} 年报 · ${v151.codes??0} 只 · 准入时收益${v151.return_data_read?'已读取':'未读取'} · 因子检验${v151.single_factor_test_consumed?'已执行并封存':v151.single_factor_backtest_allowed?'允许':'锁定'} · 策略回测${v151.strategy_backtest_allowed?'允许':'锁定'}`],
    ['V153唯一ROE因子检验', v153.historical_factor_gate_passed?'历史门禁通过':v153.return_data_read?'永久失败':v153.available?'等待数据':'缺失', `收益数据${v153.return_data_read?'已读取':'未读取'} · 历史因子${v153.historical_factor_gate_passed?'通过':'未通过'} · 策略回测${v153.strategy_backtest_allowed?'允许':'锁定'}`],
    ['V155季度资产增长十分组', v155.preregistration_admission_passed?'准入通过':v155.available?'候选前拒绝':'缺失', `${v155.records??0} 条 · ${v155.codes??0} 只 · ${v155.periods??0} 期 · 准入时收益${v155.return_data_read?'已读取':'未读取'} · 因子检验${v155.single_factor_test_consumed?'已执行并封存':v155.single_factor_backtest_allowed?'允许':'锁定'}`],
    ['V157唯一资产增长检验', v157.historical_factor_gate_passed?'历史门禁通过':v157.return_data_read?'永久失败':v157.available?'等待数据':'缺失', `收益数据${v157.return_data_read?'已读取':'未读取'} · 历史因子${v157.historical_factor_gate_passed?'通过':'未通过'} · 策略回测${v157.strategy_backtest_allowed?'允许':'锁定'}`],
    ['V163官方资产增长十分组', v163.preregistration_admission_passed?'准入通过':v163.available?'候选前拒绝':'缺失', `${v163.records??0} 条 · ${v163.codes??0} 只 · ${v163.periods??0} 期 · 唯一因子检验${v163.single_factor_test_consumed?'已执行并封存':v163.single_factor_backtest_allowed?'允许':'锁定'}`],
    ['V165唯一资产增长检验', v165.historical_factor_gate_passed?'历史门禁通过':v165.return_data_read?'永久失败':v165.available?'等待数据':'缺失', `验证IC ${v165Validation.mean_rank_ic??'—'} · 验证10BP多空 ${v165Validation.mean_d10_minus_d1_after_10bp==null?'—':(Number(v165Validation.mean_d10_minus_d1_after_10bp)*100).toFixed(3)+'%'} · 观察IC ${v165Observed.mean_rank_ic??'—'} · 观察10BP多空 ${v165Observed.mean_d10_minus_d1_after_10bp==null?'—':(Number(v165Observed.mean_d10_minus_d1_after_10bp)*100).toFixed(3)+'%'} · 策略回测${v165.strategy_backtest_allowed?'允许':'锁定'}`],
    ['V167行业利润扩散候选', v169.status==='permanently_rejected_before_return_computation'?'收益计算前永久淘汰':v167.preregistration_admission_passed?'价格纪元准入':'回测前拒绝', `${v167.monthly_observations??0} 月 · 观察段独立开仓阶段 ${v167.observed_risk_on_regimes??0} · 最多完整交易 ${v169.maximum_possible_completed_trades??0}/${v169.required_completed_trades??12} · 历史回测${v169.historical_backtest_executed?'已执行':'未执行'} · 收益${v169.return_data_computed?'已计算':'未计算'}`],
    ['V170行业利润扩散加速度', v172.historical_strategy_gate_passed?'历史门禁通过（仍不可推荐）':v172.single_historical_backtest_consumed?'唯一回测永久失败':v170.preregistration_admission_passed?'已准入':'回测前拒绝', `${v170.monthly_delta_observations??0} 月差分 · 可完成交易 训练/观察 ${v170.train_completed_trades_possible??0}/${v170.observed_completed_trades_possible??0} · 训练10BP ${v172Train10.total_return_pct??'—'}% · 观察10BP ${v172Observed10.total_return_pct??'—'}% · 20BP ${v172Observed20.total_return_pct??'—'}% · 回撤 ${v172Observed10.max_drawdown_pct??'—'}% · ${v172.failed_gates?.length??0}项失败`],
    ['V174 PMI需求库存缺口加速度', v176.historical_strategy_gate_passed?'历史门禁通过（仍不可推荐）':v176.single_historical_backtest_consumed?'唯一回测永久失败':v174.preregistration_admission_passed?'已准入':'回测前拒绝', `${v174.monthly_delta_observations??0} 月差分 · 可完成交易 训练/观察 ${v174.train_completed_trades_possible??0}/${v174.observed_completed_trades_possible??0} · 训练10BP ${v176Train10.total_return_pct??'—'}% · 观察10BP ${v176Observed10.total_return_pct??'—'}% · 年化 ${v176Observed10.annualized_return_pct??'—'}% · 20BP ${v176Observed20.total_return_pct??'—'}% · 回撤 ${v176Observed10.max_drawdown_pct??'—'}% · ${v176.failed_gates?.length??0}项失败`],
    ['V177纪律处分持仓风险否决', v177.price_data_gate_passed?'价格门禁通过':v177.price_data_epoch_consumed?'收益前永久失败':v177.preregistration_admission_passed?'事件准入通过':'回测前拒绝', `${v177.event_records??0} 事件 · ${v177.event_codes??0} 股票 · 事件准入${v177.preregistration_admission_passed?'通过':'失败'} · 价格纪元${v177.price_data_gate_passed?'通过':'失败'} · 历史事件收益${v177.historical_event_test_executed?'已计算':'未计算'} · ${v177.failed_price_gates?.length??0}项价格门禁失败`],
    ['V179同历月收益季节性', v180Seasonality.single_factor_test_consumed?(v180Seasonality.historical_factor_gate_passed?'历史门禁通过':'唯一因子检验永久失败'):v179Seasonality.preregistration_admission_passed?'已准入':'回测前拒绝', `${v180Seasonality.target_months_computed??v179Seasonality.target_months?.count??0} 月 · 五年历史股票 ${v180Seasonality.v4_st_files_hash_verified??0}/${v180Seasonality.five_year_history_codes??0} 哈希验证 · 训练IC ${v180Train.mean_rank_ic??'—'} · 训练20BP多空 ${v180Train.mean_d10_minus_d1_after_20bp==null?'—':(Number(v180Train.mean_d10_minus_d1_after_20bp)*100).toFixed(3)+'%'} · 观察10BP多空 ${v180Observed.mean_d10_minus_d1_after_10bp==null?'—':(Number(v180Observed.mean_d10_minus_d1_after_10bp)*100).toFixed(3)+'%'} · 观察单调性 ${v180Observed.mean_monotonicity??'—'} · Bootstrap下限 ${v180Observed.bootstrap_95pct_lower==null?'—':(Number(v180Observed.bootstrap_95pct_lower)*100).toFixed(3)+'%'}`],
    ['V181年报披露时效变化', v182Filing.single_factor_test_consumed?(v182Filing.historical_factor_gate_passed?'历史门禁通过':'唯一因子检验永久失败'):v181Filing.preregistration_admission_passed?'已准入':'回测前拒绝', `${v182Filing.target_years_computed??v181Filing.target_years?.length??0} 年 · 价格交集股票 ${v182Filing.filing_history_price_intersection_codes??0} · V4哈希 ${v182Filing.v4_st_files_hash_verified??0} · 训练IC ${v182Train.mean_rank_ic??'—'} · 训练10BP多空 ${v182Train.mean_d10_minus_d1_after_10bp==null?'—':(Number(v182Train.mean_d10_minus_d1_after_10bp)*100).toFixed(3)+'%'} · 观察10BP多空 ${v182Observed.mean_d10_minus_d1_after_10bp==null?'—':(Number(v182Observed.mean_d10_minus_d1_after_10bp)*100).toFixed(3)+'%'} · 观察单调性 ${v182Observed.mean_monotonicity??'—'} · 留一年最差 ${v182Observed.leave_one_year_out_worst_mean_after_10bp==null?'—':(Number(v182Observed.leave_one_year_out_worst_mean_after_10bp)*100).toFixed(3)+'%'}`],
    ['旧V86前向证据', forward.invalidated?'候选已失效':forward.forward_gate_passed?'通过':'等待新数据', `${forward.scheduled_observations??0}/${forward.minimum_scheduled_observations??12} 周 · ${forward.completed_signal_outcomes??0}/60 信号 · ${forward.invalidated?'不再累计':'首次 '+(forward.first_eligible_date||'—')}`],
    ['新机制探索', discovery.latest_completed_test==='V197'?'V196/V197候选已完成并失败':discovery.latest_completed_test==='V195'?'V194/V195候选已完成并失败':discovery.latest_completed_test==='V193'?'V192/V193候选已完成并失败':discovery.latest_completed_test==='V191'?'V190/V191候选已完成并失败':discovery.latest_completed_test==='V189'?'V188/V189候选已完成并失败':discovery.latest_completed_test==='V187'?'V186/V187候选已完成并失败':discovery.latest_completed_test==='V185'?'V184/V185候选已完成并失败':discovery.latest_completed_test==='V182'?'V181候选已完成并失败（V182检验）':discovery.latest_completed_test==='V180'?'V179候选已完成并失败（V180检验）':discovery.latest_completed_test==='V178'?'V177候选在V178价格门禁失败':discovery.latest_completed_test==='V176'?'V174候选已完成并失败（V176检验）':discovery.latest_completed_status==='permanently_rejected_historical_strategy'?'V170已完成并失败':discoveryCapReached?'轮次已封顶':discovery.actionable_candidate?'已接收':'尚未接收', discovery.latest_completed_test==='V197'?'V196收益前冻结准入通过 · V197验证防守性超额但测试超额和稳定性失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V195'?'V194收益前冻结准入通过 · V195验证局部正但测试超额和稳定性失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V193'?'V192收益前冻结准入通过 · V193验证与测试失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V191'?'V190数据验收通过 · V191训练验证测试与三折均为负 · 下一候选尚未预注册':discovery.latest_completed_test==='V189'?'V188数据验收通过 · V189验证全面为负且测试首折失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V187'?'V186数据验收通过 · V187验证与Bootstrap稳定性失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V185'?'V184数据验收通过 · V185验证与测试失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V182'?'V179稳定性失败 · V181训练反向且跨年失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V180'?'V170/V174唯一回测失败 · V177收益前失败 · V179稳定性失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V178'?'V170/V174唯一回测失败 · V177收益前失败 · 下一候选尚未预注册':discovery.latest_completed_test==='V176'?'V167收益前淘汰 · V170唯一回测失败 · V174唯一回测失败 · 下一候选尚未预注册':discovery.latest_completed_status==='permanently_rejected_historical_strategy'?'V167收益前淘汰 · V170唯一回测失败 · 下一候选尚未预注册':`${discovery.round??0}/${discovery.round_limit??30} 轮 · ${(discovery.candidate_ids||[]).join(', ')||'无候选'} · ${discoveryCapReached?'等待全新授权数据纪元':'回测'+(discovery.can_start_backtest?'允许':'锁定')}`],
    ['最接近候选', closest.status==='research_only'?'仅研究':'无', `跨池9年 ${closest.cross_section_9y_annualized_pct??'—'}% · DSR ${closest.cross_section_9y_dsr??'—'} · 近3年 ${closest.cross_section_recent_annualized_pct??'—'}%`],
  ];
  const blockers = e.blockers || [];
  const activeBlockers = e.active_blockers || blockers.filter(b => b.current_scope === true);
  const archivedBlockers = e.archived_blockers || blockers.filter(b => b.current_scope !== true);
  const artifactItems = artifactLedger.items || [];
  const action = strategyLab?.shortest_profit_path || strategyLab?.headline || {};
  const latestStrategy = strategyLab?.latest_strategy || (strategyLab?.strategies || [])[0] || {};
  body.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:10px;margin-bottom:14px">
      <div style="padding:11px 12px;border:1px solid rgba(255,184,0,.35);border-radius:8px"><div class="c-dim" style="font-size:12px">当前策略动作</div><div style="font-size:var(--text-lg);font-weight:700;color:var(--gold);margin:3px 0">${esc(action.reason||'保持现金')}</div><div class="c-dim" style="font-size:12px">现金 ${fn(action.cash_weight_pct??100,0)}% · 最新2026诊断 ${fpp(cashFloorDiagnostic.total_return_pct??latestStrategy.forward_total_pct)}</div></div>
      <div style="padding:11px 12px;border:1px solid rgba(34,197,94,.45);border-radius:8px"><div class="c-dim" style="font-size:12px">V233 · 纯511880最短路径</div><div class="num c-bull" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">5BP ${fn(cashFloorHorizons['5.0']?.shortest_all_positive_horizon_months,0)}月 · 20BP ${fn(cashFloorHorizons['20.0']?.shortest_all_positive_horizon_months,0)}月 · 50BP ${fn(cashFloorHorizons['50.0']?.shortest_all_positive_horizon_months,0)}月</div><div class="c-dim" style="font-size:12px">历史完成批次全正 · 20万纸面 ${fn(cashFloorPlan.quantity,0)}股 · 50BP/边日频年化 ${fpp(cashFloorDailyPath.annualized_return_pct)} · 日频最大回撤 ${fpp(cashFloorDailyPath.daily_max_drawdown_pct)} · 累计 ${fpp(cashFloorDailyPath.total_return_pct??cashFloorHistory.total_return_pct)} · 不保证未来</div></div>
      <div style="padding:11px 12px;border:1px solid rgba(255,184,0,.35);border-radius:8px"><div class="c-dim" style="font-size:12px">V237 · 逐日持续回正</div><div class="num" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">中位 ${fn(cashDaily.median_durable_positive_trading_days,0)}日 · 最慢 ${fn(cashDaily.maximum_durable_positive_trading_days,0)}日</div><div class="c-dim" style="font-size:12px">${fn(cashDaily.completed_entry_cohorts,0)}个完整历史入场日 · 3BP佣金+观察价差 · 额外5BP最慢${fn(cashDailyStress.maximum_durable_positive_trading_days,0)}日 · 非未来保证</div></div>
      <div style="padding:11px 12px;border:1px solid rgba(77,166,255,.35);border-radius:8px"><div class="c-dim" style="font-size:12px">V239 · 同类产品横向复现</div><div class="num" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">${fn(cashCrossSummary.product_count,0)}只 · 最慢 ${fn(cashCrossSummary.base_maximum_durable_days_across_products,0)}日</div><div class="c-dim" style="font-size:12px">每日收益与成交价同账本 · 额外5BP最慢${fn(cashCrossSummary.five_bp_stress_maximum_durable_days_across_products,0)}日 · 单品风险仍存在</div></div>
      <div style="padding:11px 12px;border:1px solid rgba(34,197,94,.42);border-radius:8px"><div class="c-dim" style="font-size:12px">V241/V243 · 双产品底座</div><div class="num c-bull" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">${fn(dualCashBase.maximum_durable_positive_trading_days,0)}日 · 前向${fn(dualCashForward.completed_closes,0)}收盘</div><div class="c-dim" style="font-size:12px">511880 1000股 + 511990 900股 · 额外5BP最慢${fn(dualCashStress.maximum_durable_positive_trading_days,0)}日 · 真钱锁定</div></div>
      <div style="padding:11px 12px;border:1px solid var(--border-subtle);border-radius:8px"><div class="c-dim" style="font-size:12px">网站数据覆盖</div><div class="num" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">${coverage.accounted_count??0}/${coverage.tracked_count??0}</div><div class="c-dim" style="font-size:12px">${coverage.required_missing_count??0} 项待补 · 同步不等于可买</div></div>
      <div style="padding:11px 12px;border:1px solid var(--border-subtle);border-radius:8px"><div class="c-dim" style="font-size:12px">研究报告账本</div><div class="num" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">${artifactLedger.indexed_count??0}/${artifactLedger.report_json_count??0}</div><div class="c-dim" style="font-size:12px">JSON报告已自动索引</div></div>
      <div style="padding:11px 12px;border:1px solid var(--border-subtle);border-radius:8px"><div class="c-dim" style="font-size:12px">原始日线</div><div class="num" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">${raw.processed_symbols||0}/${raw.total_symbols||0}</div><div class="c-dim" style="font-size:12px">截至 ${esc(raw.as_of||'—')} · 质量通过 ${raw.quality_passed_files||0}</div></div>
      ${!cashFloor.historical_horizon_gate_passed?`<div style="padding:11px 12px;border:1px solid rgba(34,197,94,.35);border-radius:8px"><div class="c-dim" style="font-size:12px">V225真实纸面前向</div><div class="num" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">${fn(stabilityForward.completed_post_freeze_common_closes,0)}个共同收盘 · ${fpp(stabilityForward.formal_forward_return_pct)}</div><div class="c-dim" style="font-size:12px">回撤 ${fpp(stabilityForward.max_drawdown_pct)} · 最新 ${esc(stabilityForward.latest_observation_date||'尚无')} · 真钱锁定</div></div>
      <div style="padding:11px 12px;border:1px solid rgba(245,158,11,.35);border-radius:8px"><div class="c-dim" style="font-size:12px">V227 · 历史研究执行复算（非推荐）</div><div class="num" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">风险 ${fn(stabilityLotPlan.post_trade_risk_weight_pct,2)}% · 现金 ¥${fn(stabilityLotPlan.residual_cash,0)}</div><div class="c-dim" style="font-size:12px">511880 ${fn(stabilityLotQty['511880'],0)}股 · 511010 ${fn(stabilityLotQty['511010'],0)}股 · 518880 ${fn(stabilityLotQty['518880'],0)}股 · 510300 ${fn(stabilityLotQty['510300'],0)}股</div></div>
      <div style="padding:11px 12px;border:1px solid rgba(77,166,255,.35);border-radius:8px"><div class="c-dim" style="font-size:12px">V229 · 风险袖套收益归因</div><div class="num" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">测试增量 ${fpp(stabilityAttributionTest.incremental_total_return_pct_points)} · ${(stabilityAttribution.years_beating_cash||[]).length}/11年跑赢</div><div class="c-dim" style="font-size:12px">511880是稳定底座 · 风险袖套是收益增强层 · 2018/2022跑输纯511880</div></div>
      <div style="padding:11px 12px;border:1px solid rgba(255,68,88,.35);border-radius:8px"><div class="c-dim" style="font-size:12px">V231 · 比例选择偏差</div><div class="num c-bear" style="font-size:var(--text-lg);font-weight:700;margin:3px 0">滚动 ${fpp(selectionComparison.rolling_walk_forward_2018_2025?.total_return_pct)} / 纯511880 ${fpp(selectionComparison.cash_only_2018_2025?.total_return_pct)}</div><div class="c-dim" style="font-size:12px">滚动修正已拒绝 · 未消除固定20%的事后选择偏差</div></div>`:''}
    </div>
    <div style="margin-bottom:14px;padding:10px 12px;border:1px solid rgba(34,197,94,.35);border-radius:8px;background:rgba(34,197,94,.04)">
      <div style="font-size:var(--text-xs);font-weight:700;color:var(--bull)">V233真实纸面前向 · 正在运行</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:6px;font-size:var(--text-xs)">
        <span><strong>${fn(cashForward.completed_closes,0)}</strong> 个形成后正式收盘</span>
        <span>冻结入口 <strong>${cashForwardEntry.ask_price==null?'—':'¥'+fn(cashForwardEntry.ask_price,3)}</strong></span>
        <span>盘中价 <strong>${cashForwardMark.price==null?'—':'¥'+fn(cashForwardMark.price,3)}</strong></span>
        <span>成本后估算清算收益 <strong class="${cc(cashForwardMark.net_liquidation_return_pct)}">${cashForwardMark.net_liquidation_return_pct==null?'—':fpp(cashForwardMark.net_liquidation_return_pct)}</strong></span>
        <span>买一/卖一 <strong>${cashForwardMark.bid_price_1==null?'—':'¥'+fn(cashForwardMark.bid_price_1,3)+' / ¥'+fn(cashForwardMark.ask_price_1,3)}</strong></span>
        <span>5BP压力回本价 <strong>${cashForwardMark.stress_5bp_break_even_price==null?'—':'¥'+fn(cashForwardMark.stress_5bp_break_even_price,3)}</strong></span>
        <span>当前盘口新建仓回本价 <strong>${cashForwardMark.live_top_of_book_entry?.break_even_exit_price==null?'—':'¥'+fn(cashForwardMark.live_top_of_book_entry.break_even_exit_price,3)}</strong></span>
        <span>交易所时间 ${esc(cashForwardMark.as_of||'等待行情')}</span>
      </div>
      <div class="c-dim" style="font-size:12px;margin-top:5px">${esc(cashForward.next_action||'等待正式收盘')} · 真实前向仅使用策略形成后的冻结入口；8月11日价格只属于历史输入。5BP是压力成本，不是当前盘口价差；当前盘口仍按未确认的0.03%双边券商佣金估算。盘中估值不会冒充完成收盘，交易前置条件不由本卡改变。</div>
    </div>
    <div style="margin-bottom:14px;padding:10px 12px;border:1px solid rgba(255,184,0,.25);border-radius:8px;background:rgba(255,184,0,.025)">
      <div style="font-size:var(--text-xs);font-weight:700;color:var(--gold)">当前验证进度说明 · ${activeBlockers.length} 项</div>
      ${activeBlockers.map(b=>`<div style="font-size:var(--text-xs);line-height:1.55;margin-top:4px"><strong>${esc(b.label||'未命名断点')}</strong><span class="c-dim"> · ${esc(String(b.current??'—'))} → ${esc(String(b.target??'—'))}</span>${b.detail?`<div class="c-dim">${esc(String(b.detail))}</div>`:''}</div>`).join('') || '<div class="c-dim" style="font-size:var(--text-xs);margin-top:4px">暂无当前验证断点。</div>'}
    </div>
    <details style="margin-bottom:10px;border:1px solid var(--border-subtle);border-radius:8px;padding:8px 10px">
      <summary style="cursor:pointer;font-size:var(--text-xs);font-weight:700">查看 ${tiles.length} 项数据与研究状态</summary>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;max-height:360px;overflow:auto;margin-top:10px">
        ${tiles.map(t=>`<div style="padding:9px 10px;border:1px solid var(--border-subtle);border-radius:8px"><div class="c-dim" style="font-size:12px">${esc(t[0])}</div><div style="font-size:var(--text-sm);font-weight:700;margin:3px 0">${esc(t[1])}</div><div class="c-dim" style="font-size:12px;line-height:1.5">${esc(t[2])}</div></div>`).join('')}
      </div>
    </details>
    <details style="margin-bottom:14px;border:1px solid var(--border-subtle);border-radius:8px;padding:8px 10px">
      <summary style="cursor:pointer;font-size:var(--text-xs);font-weight:700">查看全部 ${artifactItems.length} 个研究报告证据</summary>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:4px 10px;max-height:280px;overflow:auto;margin-top:8px;font-size:12px;color:var(--text-secondary)">
        ${artifactItems.map(item=>`<span title="${escAttr(item.modified_at||'')}">${esc(item.name||item.id||'未命名')} · ${fn(item.bytes||0,0)}B</span>`).join('') || '<span>暂无报告证据</span>'}
      </div>
    </details>
    <details style="border:1px solid var(--border-subtle);border-radius:8px;padding:8px 10px">
      <summary style="cursor:pointer;font-size:var(--text-xs);font-weight:700">查看 ${archivedBlockers.length} 条历史归档原因</summary>
      <div style="display:flex;flex-direction:column;gap:6px;max-height:300px;overflow-y:auto;margin-top:10px">
      ${archivedBlockers.length ? archivedBlockers.map(b=>`<div style="padding:8px 10px;border-left:3px solid ${b.category==='profitability'?'var(--bear)':b.category==='integrity'?'var(--gold)':'var(--accent)'};background:rgba(255,255,255,.025);font-size:var(--text-xs);line-height:1.55">
        <strong>${esc(b.label||'未命名断点')}</strong>
        <span class="c-dim"> · 当前 ${esc(String(b.current??'—'))} · 目标 ${esc(String(b.target??'—'))}</span>
        ${b.gap?`<div class="c-dim">差距：${esc(String(b.gap))}</div>`:''}
        ${b.detail?`<div class="c-dim">${esc(String(b.detail))}</div>`:''}
      </div>`).join('') : '<div style="color:var(--bull);font-size:var(--text-xs)">暂无历史归档记录。</div>'}
      </div>
    </details>`;
}

function renderTiersTable(tiers) {
  const tbody = $('#tierTbody');
  if (!tbody || !tiers.length) return;
  tbody.innerHTML = tiers.map(t => `
    <tr>
      <td class="num">¥${fn(t.capital,0)}</td>
      <td><strong>${esc(t.tier)}</strong></td>
      <td>${t.suggested_positions} 只</td>
      <td>${t.max_single_pct}%</td>
      <td>¥${t.max_stock_price.toFixed(2)}</td>
    </tr>
  `).join('');
}

// 风险雷达图（4个有直接证据的维度）
function renderRiskRadar(el, report) {
  if (!el) return;
  if (!window.echarts) {
    return whenEcharts().then(ready => {
      if (ready && el.isConnected && state.page === 'risk') renderRiskRadar(el, report);
      else showEchartsRetry(el, '本地图表运行时未就绪；风险数字仍可核对。', () => renderRiskRadar(el, report));
    });
  }
  const prev = echarts.getInstanceByDom(el);
  if (prev) prev.dispose();
  const b = report.beta || {};
  const v = report.var || {};
  const dd = report.drawdown || {};
  const ind = report.industry || {};
  if (!riskRadarEvidenceReady(report)) {
    el.removeAttribute('style');
    el.className = 'empty-state';
    el.textContent = 'Beta、VaR、行业或组合净值证据不完整，不生成伪精确风险雷达。';
    return;
  }

  // 4维：市场风险 / 波动率 / 行业集中 / 回撤。
  // 真正的流动性证据在机会质量/仓位计划使用 ADV20 与参与率展示；
  // 组合风险接口未提供相应字段前，这里不制造伪精确刻度。
  const beta = Number(b.account_beta ?? b.beta);
  const dailyVol = Number(v.portfolio_daily_vol_account_pct ?? v.portfolio_daily_vol_pct);
  const industryAccountExposure = Number(ind.max_account_exposure_pct ?? 0);
  const currentDrawdown = Number(dd.current_drawdown_pct);
  const marketRisk = Math.min(Math.abs(beta), 2) / 2 * 100;      // Beta>1 风险高
  const volRisk = Math.min(dailyVol / 3, 1) * 100;
  const concentration = Math.min(industryAccountExposure / 25, 1) * 100;
  const drawdownRisk = Math.min(Math.abs(currentDrawdown) / 30, 1) * 100;

  const chart = echarts.init(el, null, { renderer:'canvas' });
  chart.setOption(Object.assign(echartsTheme(), {
    aria: { enabled: true, description: '组合市场风险、波动率、行业集中和回撤风险雷达图' },
    radar: {
      indicator: [
        { name:'市场', max:100 }, { name:'波动', max:100 },
        { name:'集中度', max:100 }, { name:'回撤', max:100 },
      ],
      radius: '68%', center: ['50%', '52%'],
      axisName: { color: '#8B8F9A', fontSize: 12 },
      splitArea: { areaStyle: { color: ['rgba(255,255,255,0.01)', 'rgba(255,255,255,0.02)'] } },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.08)' } },
    },
    series: [{
      type: 'radar',
      data: [{
        value: [marketRisk, volRisk, concentration, drawdownRisk],
        name: '组合暴露',
        areaStyle: { color: 'rgba(0,102,255,0.2)' },
        lineStyle: { color: '#0066FF', width: 2 },
        itemStyle: { color: '#0A84FF' },
        symbolSize: 4,
      }],
    }],
  }));
  state.charts['riskRadar'] = chart;
  return chart;
}


RegisSpa.registerPage('risk', {
  render: renderRisk,
  loadEvidence: loadRiskEvidence,
});
})(window.RegisSpa);
