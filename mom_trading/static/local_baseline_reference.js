(function () {
  'use strict';
  var root = document.getElementById('localBaselineReference');
  if (!root) return;
  var get = function (id) { return document.getElementById(id); };
  var status = get('localBaselineStatus'), content = get('localBaselineContent');
  var rows = get('localBaselineRows'), assumptions = get('localBaselineAssumptions');
  var limits = get('localBaselineLimits'), sources = get('localBaselineSources');
  var sequence = 0, controller = null;
  var ids = ['standard_full', 'standard_partial_half', 'delayed_adverse', 'no_fill_cash'];
  function numeric(v) { return (typeof v === 'number' || typeof v === 'string' && v.trim() !== '') && Number.isFinite(Number(v)); }
  function valid(d) {
    return d && d.status === 'available' && d.state === 'computed_conditional_reference' && d.order_book_id === '510300.XSHG' &&
      d.initial_CNY === '150000' && d.lot_size === 100 && d.first_date === 20130104 && d.last_date === 20260930 && d.price_sessions === 3338 &&
      d.research_only === true && d.account_access === false && d.live_market_data === false && d.model_admitted === false && d.production_eligible === false &&
      d.automatic_orders === false && d.generates_alert === false && d.OOS_certified === false && d.full_L1_L4_passed === false && d.winner_selection === false && d.actual_orders === 0 && d.new_economic_runs === 0 &&
      d.cash_control && d.cash_control.initial_CNY === '150000' && d.cash_control.terminal_CNY === '150000' && d.cash_control.total_return === 0 &&
      typeof d.recorded_at === 'string' && d.sources && ['protocol', 'result', 'acquisition', 'implementation', 'issuer_census'].every(function (k) { return d.sources[k] && /^[a-f0-9]{64}$/.test(d.sources[k].sha256) && typeof d.sources[k].path === 'string'; }) &&
      Array.isArray(d.conditions_and_limits) && d.conditions_and_limits.length > 0 && d.conditions_and_limits.every(function (s) { return typeof s === 'string'; }) &&
      d.ledger_models && ['entry', 'partial', 'exit', 'cash', 'dividend_tax', 'fee'].every(function (k) { return typeof d.ledger_models[k] === 'string'; }) &&
      Array.isArray(d.scenarios) && d.scenarios.length === 4 && d.scenarios.every(function (s, i) {
        return s.scenario_id === ids[i] && s.initial_CNY === '150000' && typeof s.label === 'string' &&
          ['terminal_CNY', 'total_return', 'CAGR', 'max_drawdown_on_daily_marks', 'model_transaction_fees_CNY', 'model_dividend_cash_paid_CNY', 'filled_fund_units', 'cancelled_fund_units'].every(function (k) { return numeric(s[k]); }) &&
          s.assumptions && s.assumptions.id === ids[i] && Number.isInteger(s.assumptions.entry_session_index) &&
          ['open', 'high'].indexOf(s.assumptions.entry_price_field) >= 0 &&
          ['fill_fraction', 'all_in_fee_rate', 'minimum_fee_CNY', 'slippage_rate'].every(function (k) { return numeric(s.assumptions[k]); });
      });
  }
  function money(v) { return Number(v).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2}); }
  function percent(v) { return (Number(v) * 100).toFixed(2) + '%'; }
  function item(parent, value) { var li = document.createElement('li'); li.textContent = value; parent.appendChild(li); }
  function row(values) { var tr = document.createElement('tr'); values.forEach(function (v, i) { var cell = document.createElement(i ? 'td' : 'th'); cell.textContent = v; if (!i) cell.setAttribute('scope', 'row'); tr.appendChild(cell); }); rows.appendChild(tr); }
  function clear() { content.hidden = true; rows.replaceChildren(); assumptions.replaceChildren(); limits.replaceChildren(); sources.textContent = ''; }
  function render(d) {
    row(['现金（无息）', money(d.cash_control.terminal_CNY), '0.00%', '0.00%', '0.00%', '0.00', '0.00']);
    d.scenarios.forEach(function (s) {
      row([s.label, money(s.terminal_CNY), percent(s.total_return), percent(s.CAGR), percent(s.max_drawdown_on_daily_marks), money(s.model_transaction_fees_CNY), money(s.model_dividend_cash_paid_CNY)]);
      var a = s.assumptions;
      item(assumptions, s.label + '：首个交易日后第 ' + a.entry_session_index + ' 个交易日，以' + (a.entry_price_field === 'open' ? '开盘价' : '最高价') + '为买入参考；成交比例 ' + percent(a.fill_fraction) + '，费率 ' + percent(a.all_in_fee_rate) + '，最低费用 ' + money(a.minimum_fee_CNY) + ' 元/实际模拟订单，滑点 ' + percent(a.slippage_rate) + '；模拟成交 ' + s.filled_fund_units + ' 份，取消 ' + s.cancelled_fund_units + ' 份。');
    });
    ['entry', 'partial', 'exit', 'cash', 'dividend_tax', 'fee'].forEach(function (k) { item(assumptions, d.ledger_models[k]); });
    d.conditions_and_limits.forEach(function (s) { item(limits, s); });
    sources.textContent = '封存时间 ' + d.recorded_at + ' · ' + Object.keys(d.sources).map(function (k) { return d.sources[k].path + ' · SHA256 ' + d.sources[k].sha256; }).join('；');
    status.textContent = '已读取 3,338 个交易日的封存摘要 · 初始 150,000 元 · 现金及全部 4 个固定情景 · 本次未重算';
    content.hidden = false;
  }
  async function load() {
    var token = ++sequence;
    if (controller) controller.abort();
    controller = new AbortController(); clear(); root.setAttribute('aria-busy', 'true'); status.textContent = '正在核对封存记录…';
    try {
      var response = await fetch('/api/local-research/baseline-reference', {cache:'no-store', credentials:'same-origin', signal:controller.signal});
      var data = await response.json();
      if (token !== sequence) return;
      if (!response.ok) throw new Error(data.message || '基准记录暂不可用，可重新读取。');
      if (!valid(data)) throw new Error('基准记录校验未通过，可恢复原件后重试。');
      render(data);
    } catch (error) { if (token === sequence) { clear(); status.textContent = error.name === 'AbortError' ? '请求已取消，可重新读取。' : (error.message || '读取失败，可重试。'); } }
    finally { if (token === sequence) root.setAttribute('aria-busy', 'false'); }
  }
  get('localBaselineRetry').addEventListener('click', load);
  load();
})();
