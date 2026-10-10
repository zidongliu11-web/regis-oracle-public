(function () {
  'use strict';
  const el = id => document.getElementById(id);
  if (!el('pbScan')) return;
  const escape = value => { const node = document.createElement('span'); node.textContent = value == null ? '' : String(value); return node.innerHTML; };
  const labels = {checked: '已检查', scope_incomplete: '成分覆盖待核', no_selected_stage: '当前没有所选阶段',
    partial: '本轮检查未完成', invalid_record: '原件校验失败', input_incomplete: '输入待核验',
    version_unavailable: '旧版本需重新核验', version_after_check: '保存时点晚于扫描', risk_review_separate: '风险独立复核'};
  let data = null, page = 0, request = null, expiry = null, catalog = null;
  function clear(message) {
    clearTimeout(expiry); data = null;
    el('pbScanStatus').textContent = message;
    el('pbScanCoverage').textContent = el('pbScanRows').textContent = el('pbScanPage').textContent = '';
    el('pbScanPrevious').disabled = el('pbScanNext').disabled = true;
  }
  function render() {
    if (!data) return;
    const expanded = new Set([...el('pbScanRows').querySelectorAll('details[data-pb-scan-plan][open]')].map(node => node.dataset.pbScanPlan));
    const selected = el('pbScanVersion').value, status = el('pbScanFilter').value;
    const versions = data.versions.filter(v => !selected || v.spec_id === selected);
    el('pbScanCoverage').innerHTML = versions.map(v => '<details><summary>' + escape(v.name) + ' · '
      + escape(labels[v.status] || v.status) + '</summary><p>成分：' + (v.total_members == null ? '范围待核' : v.total_members)
      + '；已检查：' + v.checked_members + '。' + escape(v.scope_label || '') + '</p>'
      + (v.environment ? '<p>' + escape(v.environment.label) + '；' + escape(v.environment.reasons.join('；')) + '</p>' : '')
      + v.issues.map(text => '<p>' + escape(text) + '</p>').join('')
      + v.unresolved_sectors.map(r => '<p>覆盖缺口：' + escape(r.sector.name) + ' · ' + escape(r.reason) + '</p>').join('') + '</details>').join('');
    const rows = versions.flatMap(v => v.rows.filter(r => !status || r.status === status).map(row => ({version: v, row})));
    page = Math.min(page, Math.max(0, Math.ceil(rows.length / 100)-1));
    const shown = rows.slice(page*100, (page+1)*100);
    el('pbScanRows').innerHTML = shown.length ? '<table><thead><tr><th>股票</th><th>研究版本与主线</th><th>检查结果</th><th>证据</th></tr></thead><tbody>'
      + shown.map(({version: v, row: r}) => {
        const plan = r.research_plan;
        const bound = r.status === 'research_conditions_met_execution_pending' && plan
          && plan.spec_id === v.spec_id && plan.rule_identity === v.rule_identity
          && plan.input_identity === r.price_input_identity && plan.signal_day === r.signal_day
          && plan.expires_at === r.timing?.expires_at && r.timing?.status === 'next_open_pending';
        const content = bound ? window.RegisPlaybookDevelopment?.planHTML(plan) || '' : '';
        const key = content ? encodeURIComponent(JSON.stringify([v.spec_id, r.stock_code, plan.input_identity,
          plan.rule_identity, plan.expires_at, plan.entry_terms])) : '';
        return '<tr><td>' + escape(r.stock_name) + '<br>' + escape(r.stock_code) + '</td><td>'
        + escape(v.name) + '<br>' + escape((r.sectors || []).map(s => s.name).join('、')) + '</td><td>' + escape(r.label)
        + (r.signal_day ? '<br>日线：' + escape(r.signal_day) : '') + '</td><td>'
        + '<button type="button" class="slab-btn" data-pb-scan-stock="' + escape(r.stock_code) + '" data-pb-scan-spec="' + escape(v.spec_id)
        + '">重新核验个股证据</button></td></tr>'
        + (content ? '<tr><td colspan="4"><details data-pb-scan-plan="' + key + '"' + (expanded.has(key) ? ' open' : '')
          + '><summary>查看 ' + escape(r.stock_name) + ' 的入场与退出研究预案（非买单）</summary>'
          + content + '</details></td></tr>' : '');
      }).join('') + '</tbody></table>'
      : '<p>当前筛选下没有个股记录；请同时查看版本状态和成分覆盖缺口，不能一律解释为没有机会。</p>';
    el('pbScanPage').textContent = rows.length ? '第 ' + (page+1) + ' / ' + Math.ceil(rows.length / 100) + ' 页，共 ' + rows.length + ' 条版本与股票检查' : '0 条个股记录';
    el('pbScanPrevious').disabled = page === 0;
    el('pbScanNext').disabled = (page+1)*100 >= rows.length;
  }
  async function refresh() {
    if (request || document.hidden || !el('pbScan').open) return;
    const controller = new AbortController(); request = controller;
    const started = performance.now(), timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch('/api/strategy-lab/candidate-scan', {cache: 'no-store', signal: controller.signal});
      const value = await response.json();
      if (!response.ok || value.schema !== 'playbook_candidate_scan.v1' || value.production_eligible !== false || !value.scan_snapshot) throw new Error('扫描响应未通过核验，旧结果已清除。');
      if (request !== controller) return;
      if (!value.scan_snapshot.current) { clear(value.scan_snapshot.summary); return; }
      if (catalog && value.catalog_sha256 !== catalog) { clear('战法目录与扫描版本不同，等待后台重新核验。'); return; }
      clearTimeout(expiry);
      const remaining = value.scan_snapshot.valid_for_seconds*1000-(performance.now()-started);
      if (!Number.isFinite(remaining) || remaining <= 0) { clear('扫描已过期，等待后台刷新。'); return; }
      data = value;
      const selected = el('pbScanVersion').value;
      el('pbScanVersion').innerHTML = '<option value="">全部版本</option>' + data.versions.map(v => '<option value="' + escape(v.spec_id) + '">' + escape(v.name) + ' · ' + escape(labels[v.status] || v.status) + '</option>').join('');
      if (data.versions.some(v => v.spec_id === selected)) el('pbScanVersion').value = selected;
      el('pbScanStatus').textContent = '扫描于 ' + data.checked_at + '；完成 ' + data.checked_pairs + ' / ' + data.known_member_pairs
        + ' 条已知成分检查，涉及 ' + data.distinct_members + ' 只股票。' + data.current_entry_versions + ' 个当前入场版本；'
        + (data.status === 'incomplete' ? '部分范围或检查尚未完成。' : '') + '研究条件匹配不代表收益合格、可成交或买入指令。';
      render();
      expiry = setTimeout(() => { clear('扫描或证据已过期，旧结果已清除，等待后台刷新。'); refresh(); }, remaining);
    } catch (error) {
      if (request === controller) clear(error.name === 'AbortError' ? '读取扫描超时，旧结果已清除。' : error.message);
    } finally { clearTimeout(timeout); if (request === controller) request = null; }
  }
  ['pbScanVersion', 'pbScanFilter'].forEach(id => el(id).addEventListener('change', () => { page = 0; render(); }));
  el('pbScanPrevious').addEventListener('click', () => { page--; render(); });
  el('pbScanNext').addEventListener('click', () => { page++; render(); });
  el('pbScanRefresh').addEventListener('click', refresh);
  el('pbScan').addEventListener('toggle', refresh);
  el('pbScanRows').addEventListener('click', event => {
    const button = event.target.closest('[data-pb-scan-stock]');
    if (!button || !data) return;
    el('pbStockCode').value = button.dataset.pbScanStock;
    el('pbArchive').open = true;
    window.RegisPlaybookDevelopment.checkStock(button.dataset.pbScanSpec);
    el('pbStockResult').scrollIntoView({block: 'center'});
  });
  window.addEventListener('regis-playbook-catalog', event => {
    if (catalog === event.detail.catalog) { refresh(); return; }
    catalog = event.detail.catalog;
    if (request) { request.abort(); request = null; }
    clear('战法目录已刷新，正在读取对应扫描。'); refresh();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('pagehide', () => { clearTimeout(expiry); if (request) request.abort(); });
  clear('等待后台扫描快照…'); refresh();
  setInterval(refresh, 15000);
}());
