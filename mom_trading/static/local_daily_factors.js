(function () {
  'use strict';
  var root = document.getElementById('dailyFactorPreview');
  if (!root) return;
  var el = function (name) { return document.getElementById('dailyFactor' + name); };
  var generation = 0, controller = null, timer = null;
  var reasons = {insufficient_history: '前23日不足', calendar_gap: '交易日不连续', nonpositive_amount: '历史成交额非正', zero_range: '当日价格范围为零', numeric_range: '数值超出计算范围'};
  var events = ['record','ex_dividend','payment','split','factor_change','unmatched_factor','anomaly'];
  var life = {unknown:'身份日期未知',before_recorded_listing:'早于来源上市日',within_recorded_dates:'处于来源日期区间',after_recorded_listing_end_unknown:'来源上市日后，结束日未知',at_recorded_end:'处于来源摘牌边界',after_recorded_end:'晚于来源摘牌日',invalid_recorded_dates:'身份日期矛盾'};
  var state = {unknown:'未知',reported:'有报告',not_reported:'未报告'};
  var executionFields = ['prev_close','limit_down','limit_up'];
  var fieldState = {unavailable:'未接通',missing_field:'来源无此字段',nonfinite:'来源数值无效',unsupported_nonpositive:'非正值，含义未核清',positive_source_value:'来源值'};
  function validExecution(data) {
    var e=data.execution_fields;
    if (!e || e.schema!=='regis.daily_execution_fields.v1' || e.execution_eligible!==false || e.callback_available!==false) return false;
    if (e.status==='source_values_connected') {
      if (e.source_sha256!==data.source_sha256 || !/^[a-f0-9]{64}$/.test(e.source_sha256) || e.captured_at!==data.captured_at) return false;
    } else if (e.status!=='unavailable' || e.source_sha256!==null || e.captured_at!==null) return false;
    return data.rows.every(function(r) {
      var x=r.execution_fields;
      return x && x.execution_eligible===false && x.field_status &&
        ['unknown','consistent_source_band','inconsistent_source_band'].includes(x.limit_band_status) &&
        executionFields.every(function(k) {
          var s=x.field_status[k],v=x[k];
          if (!Object.prototype.hasOwnProperty.call(fieldState,s)) return false;
          if (e.status==='unavailable') return s==='unavailable' && v===null && x.limit_band_status==='unknown';
          if (s==='unavailable') return false;
          return s==='positive_source_value' ? typeof v==='number' && Number.isFinite(v) && v>0 :
            s==='unsupported_nonpositive' ? typeof v==='number' && Number.isFinite(v) && v<=0 : v===null;
        });
    });
  }
  function describeExecution(x) {
    var labels=['昨收','跌停','涨停'];
    var lines=executionFields.map(function(k,i) {
      var s=x.field_status[k];
      return labels[i]+'：'+(s==='positive_source_value'?number(x[k]):fieldState[s]+(s==='unsupported_nonpositive'?'（'+String(x[k])+'）':''));
    });
    if (x.limit_band_status==='inconsistent_source_band') lines.push('来源价带与行情矛盾');
    else if (x.limit_band_status==='unknown') lines.push('价格范围未核清');
    return lines.join('\n');
  }
  function validContext(data) {
    var c = data.input_context;
    return c && c.schema === 'regis.daily_input_context.v1' && ['unavailable','partial','retrospective_connected'].includes(c.status) &&
      c.PIT_certified === false && c.eligible === false && c.full_actions_certified === false &&
      Array.isArray(c.source_files) && c.source_files.length <= 6 && c.source_files.every(function (f) { return f && typeof f.name === 'string' && /^[a-f0-9]{64}$/.test(f.sha256); }) &&
      data.rows.every(function (r) {
        var x = r.input_context, w = x && x.window;
        return x && Object.prototype.hasOwnProperty.call(life,x.lifetime) && Object.prototype.hasOwnProperty.call(state,x.ST) && Object.prototype.hasOwnProperty.call(state,x.halt) &&
          ['main_or_chinext','outside_trade_scope'].includes(x.trade_scope) && x.PIT_certified === false && x.eligible === false &&
          w && w.sessions_back === 23 && typeof w.complete_calendar_window === 'boolean' &&
          events.every(function (k) { return w[k] === null || Number.isInteger(w[k]) && w[k] >= 0; });
      });
  }
  function describe(c) {
    var w=c.window, count=function(k){return w[k]===null?'未知':String(w[k]);};
    return life[c.lifetime] + (c.trade_scope==='outside_trade_scope'?'；不在主板/创业板交易范围':'') +
      '；ST '+state[c.ST]+'，停牌 '+state[c.halt]+'；前23交易日至当天：登记 '+count('record')+'、除权 '+count('ex_dividend')+'、支付 '+count('payment')+'、拆并 '+count('split')+'、系数变化 '+count('factor_change')+
      '；无对应现金/拆并记录的系数变化 '+count('unmatched_factor')+'，回溯记录异常 '+count('anomaly')+(w.complete_calendar_window?'':'；日历不足23日前史')+'。';
  }
  function clearResult() { el('Rows').replaceChildren(); el('Results').hidden = true; el('Source').textContent = ''; el('Digest').textContent = ''; }
  function cancel() { generation++; if (controller) controller.abort(); controller = null; clearTimeout(timer); timer = null; root.setAttribute('aria-busy', 'false'); }
  function changed() { cancel(); clearResult(); el('Status').textContent = '选择已更改，点击计算查看结果。'; }
  function number(value) { return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(4) : '—'; }
  function validDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value < '2013-01-01' || value > '2026-09-30') return false;
    var d = new Date(value + 'T00:00:00Z');
    return Number.isFinite(d.getTime()) && d.toISOString().slice(0,10) === value;
  }
  function render(data) {
    var rows = data.rows.slice().reverse();
    rows.forEach(function (row) {
      var tr = document.createElement('tr'), missing = row.missing || {};
      var why = Object.keys(missing).filter(function (k) { return missing[k]; }).map(function (k) {
        return (k === 'close_acceptance' ? '收盘承接：' : '回踩量能：') + (reasons[missing[k]] || '未计算');
      }).join('；');
      [row.date, number(row.close_acceptance), number(row.pullback_volume_3_20), why || '—', describe(row.input_context), describeExecution(row.execution_fields)].forEach(function (value) {
        var td = document.createElement('td'); td.textContent = value; tr.appendChild(td);
      });
      el('Rows').appendChild(tr);
    });
    el('Results').hidden = !rows.length;
    el('Status').textContent = rows.length ? data.code + ' · 已计算并显示 ' + rows.length + ' 个日期；仅公式数值，未获交易资格。' : '所选日期内没有已保存日线，请调整日期。';
    el('Source').textContent = data.source + '；留存时间 ' + data.captured_at + '；本次用于计算 ' + data.source_price_rows_used + ' 条行情（含预热）。条件记录：' + ({unavailable:'尚未关联',partial:'部分来源缺失',retrospective_connected:'已关联回溯来源，逐日覆盖仍须核实'}[data.input_context.status]) + '。昨收与涨跌停字段：'+(data.execution_fields.status==='source_values_connected'?'已关联同一历史快照；缺值和异常逐行显示':'尚未接通')+'。';
    el('Digest').textContent = '定位日期时核对该证券完整日期字段。行情摘要：' + data.source_sha256 + '；条件记录来源：' + (data.input_context.source_files.map(function(f){return f.name+' · '+f.sha256;}).join('；') || '缺失，所有条件保留未知');
  }
  async function load(event) {
    if (event) event.preventDefault();
    cancel(); clearResult();
    var seq = generation, code = el('Code').value.trim(), start = el('Start').value, end = el('End').value, limit = el('Limit').value;
    if (!/^(000|001|002|003|300|301|600|601|603|605|688|689)\d{3}$/.test(code) || !validDate(start) || !validDate(end) || start > end || !['30','100','250'].includes(limit)) {
      el('Status').textContent = code === '510300' ? '请检查证券类型：当前仅支持股票因子，ETF 因子尚未实现。' : '请检查沪深 A 股代码、有效日期及条数（最多250）；无效条件不会自动改成默认股票。'; return;
    }
    var params = new URLSearchParams({code: code, start: start, end: end, limit: limit});
    if (window.history && window.history.replaceState) {
      var bookmark = new URLSearchParams(window.location ? window.location.search : '');
      ['code','start','end','limit'].forEach(function (key) { bookmark.set(key, params.get(key)); });
      window.history.replaceState(null, '', '/factor-system?' + bookmark + '#dailyFactorPreview');
    }
    controller = new AbortController(); var ownController = controller;
    root.setAttribute('aria-busy', 'true'); el('Status').textContent = '正在读取已保存日线并计算…';
    try {
      var timeout = new Promise(function (_, reject) { timer = setTimeout(function () { ownController.abort(); reject(new Error('读取超时，请点击计算重试。')); }, 12000); });
      var data = await Promise.race([fetch('/api/local-research/daily-factors?' + params, {signal: ownController.signal, cache: 'no-store'}).then(async function (response) {
        var body = await response.json();
        if (!response.ok) throw new Error(body.message || '读取失败，请重试。');
        return body;
      }), timeout]);
      if (seq !== generation) return;
      if (!data || data.code !== code || !Array.isArray(data.rows) || data.rows.length > Number(el('Limit').value) || data.rows.length !== data.returned_rows || !['available', 'empty_window'].includes(data.status) || data.arithmetic_only !== true) throw new Error('结果格式校验失败，请重试。');
      if (data.requested_start !== start || data.requested_end !== end || (data.status === 'available') !== (data.rows.length > 0) ||
          !data.rows.every(function (row, i) { return validDate(row.date) && row.date >= start && row.date <= end && (!i || data.rows[i-1].date < row.date); })) throw new Error('结果日期与本次查询不一致，未显示；请重试。');
      if (!validContext(data)) throw new Error('条件记录校验失败，请重试；本次未显示旧结果。');
      if (!validExecution(data)) throw new Error('执行字段校验失败，请重试；本次未显示旧结果。');
      render(data);
    } catch (error) {
      if (seq === generation) { clearResult(); el('Status').textContent = error.message || '本次计算失败，请重试。'; }
    } finally {
      if (seq === generation) { clearTimeout(timer); timer = null; controller = null; root.setAttribute('aria-busy', 'false'); }
    }
  }
  el('Form').addEventListener('submit', load);
  ['Code', 'Start', 'End', 'Limit'].forEach(function (name) { el(name).addEventListener('input', changed); el(name).addEventListener('change', changed); });
  window.addEventListener('pagehide', function () { cancel(); clearResult(); el('Status').textContent = '已停止读取，点击计算重新加载。'; });
  var initial = new URLSearchParams(window.location ? window.location.search : '');
  var duplicate = false;
  ['code','start','end','limit'].forEach(function (key) {
    if (initial.getAll(key).length > 1) duplicate = true;
    if (initial.has(key)) el(key[0].toUpperCase() + key.slice(1)).value = initial.get(key);
  });
  if (duplicate) { clearResult(); el('Status').textContent = '查询网址含重复条件，未自动计算；请核对表单后点击计算。'; }
  else load();
})();
