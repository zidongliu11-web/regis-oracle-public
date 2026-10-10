(function () {
  'use strict';
  var root = document.getElementById('localMatchedResearch');
  if (!root) return;
  var el = function (name) { return document.getElementById('localMatched' + name); };
  var sequence = 0, controller = null, timer = null;
  var ids = ['daily_standard_full', 'daily_partial_half', 'one_session_delayed_adverse', 'no_fill_cash'];
  function numeric(v) { return typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)); }
  function summary(v) {
    return v && ['terminal_marked_CNY','net_total_return','maximum_marked_drawdown','fees_CNY'].every(function (k) { return numeric(v[k]); }) &&
      Number(v.terminal_marked_CNY) >= 0 && Number(v.fees_CNY) >= 0 && Number(v.maximum_marked_drawdown) >= 0 && Number(v.maximum_marked_drawdown) <= 1;
  }
  function valid(d) {
    return d && d.schema === 'regis.local_matched_research.v1' && d.status === 'available' &&
      d.initial_CNY === '150000' && d.lot_size === 100 && d.first_date === '2013-01-04' && d.last_date === '2020-12-31' && d.sessions === 1945 &&
      d.research_only === true && d.earlier_failures_not_overwritten_by_this_reader === true &&
      ['account_access','live_market_data','historical_feature_PIT_certified','model_admitted','production_eligible','generates_alert','automatic_orders',
        'financial_totals_admitted','PIT_certified','OOS_computed','full_goal_complete','native_ledger_reverified','winner_selection','exposure_neutral_comparison'].every(function (k) { return d[k] === false; }) &&
      d.actual_orders === 0 && d.new_economic_runs === 0 && typeof d.recorded_at === 'string' &&
      d.stock_lineage === 'initial_cash_failures_preserved_then_specific_record_owner_dependencies_resolved' &&
      Array.isArray(d.source_versions) && d.source_versions.length === 16 && new Set(d.source_versions.map(function (s) { return s && s.name; })).size === 16 &&
      d.source_versions.every(function (s) { return s && typeof s.name === 'string' && /^[A-Za-z0-9_.-]+$/.test(s.name) && /^[a-f0-9]{64}$/.test(s.sha256); }) &&
      Array.isArray(d.scenarios) && d.scenarios.length === 4 && d.scenarios.every(function (s, i) {
        if (!s || s.id !== ids[i] || typeof s.label !== 'string' || !summary(s.ETF) || s.cash_CNY !== '150000') return false;
        var a = s.assumptions;
        if (!a || a.entry_delay_sessions !== (i === 2 ? 1 : 0) || a.entry_field !== 'open' || a.minimum_fee !== '5' ||
          a.slippage !== (i === 2 ? '0.005' : '0.001') || a.buy_all_in_fee_rate !== (i === 2 ? '0.001' : '0.0005') ||
          a.sell_all_in_fee_rate !== (i === 2 ? '0.0025' : '0.0015') ||
          a.entry_fill_fraction !== (i === 1 ? '0.5' : i === 3 ? '0' : '1') ||
          a.exit_fill_fraction !== a.entry_fill_fraction) return false;
        if (i === 1) return s.state === 'stock_arm_actual_failure_no_comparison' && s.stock === null && s.stock_minus_ETF_CNY === null && typeof s.failure === 'string' && s.failure.length > 0;
        return s.state === 'limited_conditional_comparison_available' && summary(s.stock) && numeric(s.stock_minus_ETF_CNY) && s.failure === null &&
          Math.abs(Number(s.stock.terminal_marked_CNY) - Number(s.ETF.terminal_marked_CNY) - Number(s.stock_minus_ETF_CNY)) < .011;
      });
  }
  function money(v) { return Number(v).toLocaleString('zh-CN', {minimumFractionDigits:2,maximumFractionDigits:2}); }
  function pct(v) { return (Number(v) * 100).toFixed(2) + '%'; }
  function item(target, text) { var li = document.createElement('li'); li.textContent = text; el(target).appendChild(li); }
  function clear() { el('Content').hidden = true; ['Rows','Assumptions','Sources'].forEach(function (k) { el(k).replaceChildren(); }); el('Recorded').textContent = ''; }
  function cancel() { sequence++; if (controller) controller.abort(); controller = null; clearTimeout(timer); timer = null; root.setAttribute('aria-busy','false'); }
  function render(d) {
    d.scenarios.forEach(function (s) {
      var tr = document.createElement('tr');
      [s.label, s.stock ? money(s.stock.terminal_marked_CNY) : '未完成', money(s.ETF.terminal_marked_CNY), money(s.cash_CNY),
        s.stock ? money(s.stock_minus_ETF_CNY) : '未完成', s.stock ? '条件总额 · 未准入' : '碎股分配未核清'].forEach(function (v, i) {
        var cell = document.createElement(i ? 'td' : 'th'); if (!i) cell.setAttribute('scope','row'); cell.textContent = v; tr.appendChild(cell);
      });
      el('Rows').appendChild(tr);
      var a = s.assumptions;
      item('Assumptions', s.label + '：开盘价参考，额外延迟 ' + a.entry_delay_sessions + ' 个交易日；买入 / 卖出成交比例 ' + pct(a.entry_fill_fraction) + ' / ' + pct(a.exit_fill_fraction) +
        '；滑点 ' + pct(a.slippage) + '；买 / 卖综合费率 ' + pct(a.buy_all_in_fee_rate) + ' / ' + pct(a.sell_all_in_fee_rate) + '，最低费用 ' + money(a.minimum_fee) +
        ' 元。冻结的敏感性假设，非真实收费认证。ETF 期末未卖出，卖出费率未使用。');
      item('Assumptions', s.label + '：股票' + (s.stock ? '区间收益 ' + pct(s.stock.net_total_return) + '，日线最大回撤 ' + pct(s.stock.maximum_marked_drawdown) + '，模拟费 ' + money(s.stock.fees_CNY) + ' 元。' : '未发布总额。' + s.failure) +
        'ETF 区间收益 ' + pct(s.ETF.net_total_return) + '，日线最大回撤 ' + pct(s.ETF.maximum_marked_drawdown) + '，模拟费 ' + money(s.ETF.fees_CNY) + ' 元；分红采用原税前假设。');
    });
    d.source_versions.forEach(function (s) { item('Sources', s.name + ' · SHA256 ' + s.sha256); });
    el('Recorded').textContent = '记录封存时间：' + d.recorded_at;
    el('Status').textContent = '已读取 1,945 个交易日的同期研究摘要 · 3 条条件比较、1 条股票结算失败 · 本次未重算';
    el('Content').hidden = false;
  }
  async function load() {
    cancel(); clear(); var token = sequence;
    controller = new AbortController(); var own = controller;
    root.setAttribute('aria-busy','true'); el('Status').textContent = '正在核对同期研究记录…';
    try {
      var timeout = new Promise(function (_, reject) { timer = setTimeout(function () { own.abort(); reject(new Error('读取超时，请重新读取同期研究。')); }, 12000); });
      var data = await Promise.race([fetch('/api/local-research/matched-reference', {cache:'no-store',credentials:'same-origin',signal:own.signal}).then(async function (response) {
        if (!response.ok) throw new Error('记录暂不可用，请恢复原件后重新读取。');
        return response.json();
      }), timeout]);
      if (token !== sequence) return;
      if (!valid(data)) throw new Error('同期研究记录校验未通过，请恢复原件后重试。');
      render(data);
    } catch (e) { if (token === sequence) { clear(); el('Status').textContent = e.message || '读取失败，请重试。'; } }
    finally { if (token === sequence) { clearTimeout(timer); timer = null; controller = null; root.setAttribute('aria-busy','false'); } }
  }
  el('Retry').addEventListener('click', load);
  window.addEventListener('pagehide', function () { cancel(); clear(); el('Status').textContent = '已停止读取，点击重新读取同期研究。'; });
  load();
})();
