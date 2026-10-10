/* Regis Oracle SPA — 股票扫描页 */
(function(RegisSpa){
'use strict';
if (!RegisSpa || typeof RegisSpa.core?.api !== 'function'
    || typeof RegisSpa.registerPage !== 'function') {
  throw new Error('Regis SPA core must load before scanner module');
}
const { $, $$, state, api, apiError, cc, esc, fmtYi, fn, fp, marketCenterNav } = RegisSpa.core;

// ═══════════════════════════════════════════════
// SCANNER PAGE
// ═══════════════════════════════════════════════
async function renderScanner() {
  const renderGeneration = state.navSequence;
  let scanGeneration = 0;
  const container = $('[data-page="scanner"]');
  container.innerHTML = `
    ${marketCenterNav('scanner')}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
      <button class="pill active" type="button" data-action="scan-preset" data-preset="bargain">低价筛选</button>
      <button class="pill" type="button" data-action="scan-preset" data-preset="momentum">动量筛选</button>
      <button class="pill" type="button" data-action="scan-preset" data-preset="kronosBull">模型高分</button>
      <button class="pill" type="button" data-action="scan-preset" data-preset="all">全部</button>
    </div>
    <div class="filter-bar">
      <label for="scanRank" style="font-size:var(--text-xs);color:var(--text-tertiary)">排名≤</label><input class="input" id="scanRank" value="50" style="width:70px" type="number">
      <label for="scanPrice" style="font-size:var(--text-xs);color:var(--text-tertiary)">价格≤</label><input class="input" id="scanPrice" value="25" style="width:80px" type="number">
      <label class="sr-only" for="scanFlow">资金流方向筛选</label>
      <select class="select" id="scanFlow"><option value="">全部资金流</option><option value="in">净流入</option><option value="out">净流出</option></select>
      <label class="sr-only" for="scanKronos">模型评分方向筛选</label>
      <select class="select" id="scanKronos"><option value="">全部模型评分</option><option value="up">正值</option><option value="down">负值</option></select>
      <label for="scanResearch" style="display:inline-flex;align-items:center;gap:6px;font-size:var(--text-xs);color:var(--text-secondary)">
        <input id="scanResearch" type="checkbox"> 显示未验证研究结果（不可执行）
      </label>
      <button class="btn btn-primary btn-sm" type="button" data-action="scan-run">执行扫描</button>
    </div>
    <div class="summary-strip" id="scanStats" style="margin-bottom:12px"></div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>排名</th><th>代码</th><th>名称</th><th>验证状态</th><th>验证原因</th><th>用途</th><th>来源时点</th><th>评分</th><th>价格</th><th>净额</th><th>换手</th><th>模型研究分</th></tr></thead>
      <tbody id="scanTbody"></tbody>
    </table></div>
    <div style="margin-top:8px;font-size:var(--text-xs);color:var(--text-tertiary)" id="scanSummary"></div>
  `;

  window._scanPreset = function(preset, btn) {
    $$('[data-page="scanner"] [data-action="scan-preset"]').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    const map = {
      bargain: [50,25,'in','',false],
      momentum: [80,50,'in','',false],
      kronosBull: [100,100,'','up',true],
      all: [100,100,'','',false]
    };
    const v = map[preset]||map.all;
    $('#scanRank').value=v[0]; $('#scanPrice').value=v[1];
    $('#scanFlow').value=v[2]; $('#scanKronos').value=v[3];
    $('#scanResearch').checked=v[4];
    return window._doScan();
  };

  window._doScan = async function() {
    const currentScan = ++scanGeneration;
    const params = new URLSearchParams({
      max_rank: $('#scanRank').value,
      max_price: $('#scanPrice').value,
      flow: $('#scanFlow').value,
      kronos: $('#scanKronos').value,
      min_turnover: 0,
      limit: '50',
    });
    const includeResearch = $('#scanResearch').checked;
    if (includeResearch) params.set('include_research', '1');
    const endpoint = '/api/scanner/scan?' + params.toString();
    $('#scanStats').innerHTML = '';
    $('#scanSummary').textContent = '正在按当前筛选条件读取…';
    $('#scanTbody').innerHTML = '<tr><td colspan="12"><div class="empty-state" role="status">正在按当前筛选条件读取…</div></td></tr>';
    const data = await api(endpoint);
    if (state.page !== 'scanner' || state.navSequence !== renderGeneration
        || currentScan !== scanGeneration) return;
    if (!data) {
      const summary = $('#scanSummary');
      if (summary) summary.textContent = apiError(endpoint, '扫描接口暂不可用');
      const tbody = $('#scanTbody');
      if (tbody) tbody.innerHTML = `<tr><td colspan="12"><div class="empty-state" role="status">${esc(apiError(endpoint, '扫描接口暂不可用'))}</div></td></tr>`;
      return;
    }

    const allResults = data.results || [];
    const hiddenStatuses = new Set(['failed', 'failed_validation', 'rejected', 'invalidated', 'no-go', 'nogo']);
    const results = allResults.filter(row => includeResearch || (
      row.research_only !== true
      && !hiddenStatuses.has(String(row.validation_status || 'unknown').toLowerCase())
    ));
    const validation = data.validation || {};
    const validationReason = validation.reason || '未提供验证原因';
    // Stats
    const withFlow = results.filter(r => r.net_yi>0.1).length;
    const withKronos = results.filter(r =>
      r.research_only === false
      && r.decision_eligible === true
      && ['validated', 'passed'].includes(String(r.validation_status || '').toLowerCase())
      && r.kronos_20d != null
    ).length;
    const avgScore = results.length ? (results.reduce((s,r)=>s+(r.composite||0),0)/results.length).toFixed(1) : '—';
    const stats = $('#scanStats');
    if (stats) stats.innerHTML = `
      <div class="sum-item"><span class="sum-val">${results.length} 只</span><span class="sum-lbl">可见匹配</span></div>
      <div class="sum-item"><span class="sum-val c-bull">${withFlow} 只</span><span class="sum-lbl">净流入</span></div>
      <div class="sum-item"><span class="sum-val c-accent">${withKronos} 只</span><span class="sum-lbl">含模型评分</span></div>
      <div class="sum-item"><span class="sum-val">${avgScore}</span><span class="sum-lbl">平均评分</span></div>
    `;

    const summary = $('#scanSummary');
    if (summary) summary.textContent = `顶层验证：${validation.status || '未知'} · ${validationReason} · 来源时点 ${validation.source_data_as_of || '未提供'} · ${results.length} 只可见 · ${allResults.length - results.length} 只研究/失败记录已默认隐藏 · 模型分不是预测收益率，筛选结果不构成买入建议`;

    const tbody = $('#scanTbody');
    if (!results.length) {
      tbody.innerHTML = `<tr><td colspan="12"><div class="empty-state">无可展示匹配。验证：${esc(validation.status || '未知')} · ${esc(validationReason)}；研究、失败、拒绝和失效记录默认隐藏。</div></td></tr>`;
      return;
    }

    const validationLabels = {validated:'已验证', passed:'已验证', pending:'待验证', unverified:'未验证', failed_validation:'验证失败', unknown:'验证状态未知'};
    tbody.innerHTML = results.map(r => {
      const validation = String(r.validation_status || 'unknown').toLowerCase();
      const researchOnly = r.research_only === true || r.research_only == null || !['validated', 'passed'].includes(validation);
      const modelValueVerified = !researchOnly && r.decision_eligible === true;
      return `
      <tr>
        <td class="num">#${r.rank||'—'}</td>
        <td><a href="/stock?code=${encodeURIComponent(r.code || '')}" style="color:var(--accent);font-weight:600">${esc(r.code)}</a></td>
        <td><strong>${esc(r.name || '名称不可用')}</strong></td>
        <td>${esc(validationLabels[validation] || r.validation_status || '验证状态未知')}</td>
        <td>${esc(r.validation_reason || validationReason)}</td>
        <td>${researchOnly ? '仅研究 · 不可执行' : '非仅研究标记 · 仍需执行条件确认'}</td>
        <td>${esc(r.source_data_as_of || '未提供')}</td>
        <td class="num">${fn(r.composite,1)}</td>
        <td class="num">${r.price?'¥'+fp(r.price):'—'}</td>
        <td class="num ${cc(r.net_yi)}">${fmtYi(r.net_yi)}</td>
        <td class="num">${r.turnover!=null?fn(r.turnover,1)+'%':'—'}</td>
        ${modelValueVerified && r.kronos_20d != null
          ? `<td class="num ${cc(r.kronos_20d)}">${fn(r.kronos_20d,1)}</td>`
          : '<td class="c-dim">未验证值已隐藏</td>'}
      </tr>`;
    }).join('');
  };

  await window._doScan();
}

RegisSpa.registerPage('scanner', { render: renderScanner });
})(window.RegisSpa);
