(function () {
  'use strict';
  var root = document.getElementById('localResearchHistory');
  if (!root) return;
  var primary = root.getAttribute('data-primary-history') === 'true';
  var get = function (id) { return document.getElementById(id); };
  var form = get('historyResearchForm'), status = get('historyResearchStatus');
  var box = get('historyResearchChart'), source = get('historyResearchSource');
  var table = get('historyResearchRows'), wrap = get('historyResearchTableWrap');
  var chart = null, sequence = 0, controller = null, loaded = false, requestTimer = null, leaving = false;
  function removeChart() {
    if (chart) { try { chart.remove(); } catch (error) { /* The readable table remains independent. */ } chart = null; }
  }
  function clear() {
    removeChart();
    box.hidden = true; wrap.hidden = true; table.replaceChildren();
    source.textContent = '历史提供者快照；文档约定未复权，未逐行认证。独立供历史研究查看，不进入当前买卖提示。';
    if (primary && get('historyFactorLink')) {
      get('historyFactorLink').hidden = true; get('historyFactorLink').href = '';
      get('historyFactorNote').textContent = '先读取有效的股票历史，再查看同一证券及日期的因子；因子最多显示最近 250 条。';
    }
  }
  function cancel(message) {
    sequence += 1;
    if (controller) controller.abort();
    controller = null;
    if (requestTimer !== null) clearTimeout(requestTimer);
    requestTimer = null; loaded = false; clear();
    root.setAttribute('aria-busy', 'false');
    if (message) status.textContent = message;
  }
  function chartFallback() {
    removeChart(); box.hidden = true;
    status.textContent += ' · 图表绘制失败，可展开本次已校验的日线表查看。';
  }
  function valid(data, code) {
    return data && data.code === code && data.research_only === true && data.live_market_data === false &&
      data.account_access === false && data.model_admitted === false && data.production_eligible === false &&
      data.generates_alert === false && data.automatic_orders === false &&
      ['available', 'empty_window'].indexOf(data.status) >= 0 && Array.isArray(data.rows) &&
      data.rows.length === data.returned_rows && data.rows.length <= 1000 &&
      (data.status === 'available') === (data.rows.length > 0) &&
      ['source', 'source_sha256', 'price_basis', 'units', 'captured_at'].every(function (k) { return typeof data[k] === 'string' && data[k].length > 0; }) &&
      Number.isInteger(data.matching_rows) && data.matching_rows >= data.returned_rows &&
      data.truncated === (data.matching_rows > data.returned_rows) &&
      data.rows.every(function (r, i) {
        return /^\d{4}-\d{2}-\d{2}$/.test(r.time) && r.time >= data.requested_start && r.time <= data.requested_end && (!i || data.rows[i - 1].time < r.time) &&
          ['open', 'high', 'low', 'close', 'volume', 'total_turnover'].every(function (k) { return typeof r[k] === 'number' && Number.isFinite(r[k]); }) &&
          r.low > 0 && r.low <= Math.min(r.open, r.close) && Math.max(r.open, r.close) <= r.high && r.volume >= 0 && r.total_turnover >= 0;
      });
  }
  function render(data) {
    var rows = data.rows;
    source.textContent = data.source + ' · ' + data.price_basis + ' · ' + data.units + ' · 快照采集 ' + data.captured_at + ' · 来源校验 ' + data.source_sha256.slice(0, 12);
    if (!rows.length) { status.textContent = '该代码在所选日期内没有日线记录；可修改日期后重试。'; return; }
    rows.forEach(function (row) {
      var tr = document.createElement('tr');
      ['time', 'open', 'high', 'low', 'close', 'volume'].forEach(function (key) {
        var td = document.createElement('td'); td.textContent = String(row[key]); tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    wrap.hidden = false;
    status.textContent = data.code + ' · ' + rows[0].time + ' 至 ' + rows[rows.length - 1].time + ' · 显示 ' + rows.length + ' / ' + data.matching_rows + ' 条日线' + (data.truncated ? '（已按条数限制显示最近记录）' : '') + ' · 历史研究';
    if (primary && get('historyFactorLink')) {
      if (data.code === '510300') get('historyFactorNote').textContent = '当前因子预览仅支持股票，尚未实现 ETF 因子；不会跳转计算默认股票。';
      else {
        get('historyFactorLink').href = '/factor-system?' + new URLSearchParams({code:data.code,start:data.requested_start,end:data.requested_end,limit:'250'}) + '#dailyFactorPreview';
        get('historyFactorLink').hidden = false;
        get('historyFactorNote').textContent = '因子预览沿用上图的证券与所选日期范围，独立限制为最近 250 条；不代表策略通过。';
      }
    }
    if (!window.LightweightCharts) { status.textContent += ' · 图表组件不可用，可展开日线表查看。'; return; }
    box.hidden = false;
    try {
      chart = window.LightweightCharts.createChart(box, {width:box.clientWidth || 600, height:330,
      layout:{background:{type:'solid',color:'#0a0d12'},textColor:'#a5b1c2'},
      grid:{vertLines:{color:'#19212d'},horzLines:{color:'#19212d'}},
      localization:{locale:'zh-CN'}, timeScale:{timeVisible:false,secondsVisible:false}});
    var options = {upColor:'#ed6876',downColor:'#4fc1a0',borderVisible:false,wickUpColor:'#ed6876',wickDownColor:'#4fc1a0'};
    var series = chart.addCandlestickSeries ? chart.addCandlestickSeries(options) : chart.addSeries(window.LightweightCharts.CandlestickSeries, options);
    series.setData(rows.map(function (r) { return {time:r.time,open:r.open,high:r.high,low:r.low,close:r.close}; }));
      chart.timeScale().fitContent();
    } catch (error) { chartFallback(); }
  }
  async function load() {
    if (leaving) return;
    var code = get('historyResearchCode').value.trim(), start = get('historyResearchStart').value;
    var end = get('historyResearchEnd').value, limit = get('historyResearchLimit').value;
    cancel(); var token = sequence;
    if (!/^\d{6}$/.test(code) || !start || !end || start > end) { status.textContent = '请输入六位证券代码和有效的起止日期。'; return; }
    if (primary) {
      function dateValid(value) {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value < '2013-01-01' || value > '2026-09-30') return false;
        var parsed = new Date(value + 'T00:00:00Z');
        return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
      }
      if (!/^(?:(?:000|001|002|003|300|301|600|601|603|605|688|689)\d{3}|510300)$/.test(code)
          || !dateValid(start) || !dateValid(end) || !['250','500','1000'].includes(limit)) {
        status.textContent = '请填写支持的沪深 A 股或 510300 代码，并选择快照范围内的日期与条数。'; return;
      }
      if (window.history && window.history.replaceState) window.history.replaceState(null, '', '/stock?' + new URLSearchParams({code:code,start:start,end:end,limit:limit}));
      var terminal = get('historyTerminalLink');
      if (terminal) { terminal.hidden = code === '510300'; terminal.href = '/stock?code=' + code + '&view=terminal'; }
    }
    status.textContent = '正在读取本机历史日线…'; root.setAttribute('aria-busy', 'true');
    controller = new AbortController(); var local = controller;
    requestTimer = setTimeout(function () {
      if (!leaving && token === sequence) cancel('本次读取超时，旧结果已清除；可点击查看历史重新读取。');
    }, 12000);
    try {
      var query = new URLSearchParams({code:code,start:start,end:end,limit:limit});
      var response = await fetch('/api/local-research/history?' + query, {cache:'no-store',credentials:'same-origin',signal:local.signal});
      var data = await response.json();
      if (leaving || token !== sequence) return;
      if (!response.ok) throw new Error(data.message || '历史数据暂不可用，请重试。');
      if (!valid(data, code) || data.requested_start !== start || data.requested_end !== end) throw new Error('历史数据校验未通过，请重试。');
      render(data); loaded = true;
    } catch (error) {
      if (leaving || token !== sequence) return;
      clear(); status.textContent = error.name === 'AbortError' ? '请求已取消，可重新载入。' : (error.message || '读取失败，请重试。');
    } finally {
      if (token === sequence) {
        clearTimeout(requestTimer); requestTimer = null; controller = null;
        root.setAttribute('aria-busy', 'false');
      }
    }
  }
  form.addEventListener('submit', function (event) { event.preventDefault(); load(); });
  if (primary) ['Code','Start','End','Limit'].forEach(function (key) {
    get('historyResearch' + key).addEventListener('input', function () {
      cancel('查询条件已修改，旧图与因子链接已清除；点击查看历史读取。');
    });
    get('historyResearch' + key).addEventListener('change', function () {
      cancel('查询条件已修改，旧图与因子链接已清除；点击查看历史读取。');
    });
  });
  get('historyResearchETF').addEventListener('click', function () { get('historyResearchCode').value = '510300'; load(); });
  root.addEventListener('toggle', function () {
    if (!root.open && controller) cancel('历史面板已收起，本次请求已取消。');
    else if (root.open && !loaded && !leaving && root.getAttribute('aria-busy') !== 'true') load();
  });
  window.addEventListener('regis-stock-selected', function (event) {
    var code = event.detail && event.detail.code;
    if (leaving || typeof code !== 'string' || !/^\d{6}$/.test(code)) return;
    cancel(); get('historyResearchCode').value = code;
    status.textContent = '已切换至 ' + code + '；展开后读取该股票的历史日线。';
    if (root.open) load();
  });
  window.addEventListener('pagehide', function () { leaving = true; cancel('页面已暂停，返回后重新核对历史数据。'); });
  window.addEventListener('pageshow', function (event) {
    if (event.persisted && leaving) { leaving = false; if (root.open) load(); }
  });
  if (window.ResizeObserver) new ResizeObserver(function () {
    if (chart && box.clientWidth) { try { chart.applyOptions({width:box.clientWidth}); } catch (error) { chartFallback(); } }
  }).observe(box);
  var selected = get('terminalCode');
  if (selected && /^\d{6}$/.test(selected.value)) get('historyResearchCode').value = selected.value;
  if (new URLSearchParams(window.location ? window.location.search : '').get('history_code') === '510300') {
    get('historyResearchCode').value = '510300'; root.open = true;
    if (!primary) load();
  }
  if (primary) {
    var params = new URLSearchParams(window.location ? window.location.search : '');
    ['start','end','limit'].forEach(function (key) {
      if (params.has(key)) get('historyResearch' + key[0].toUpperCase() + key.slice(1)).value = params.get(key);
    });
    root.open = true; load();
  }
})();
