(function () {
  'use strict';
  var root = document.getElementById('institutionalConsole');
  if (!root) return;

  // ── A股看盘配色：红涨、绿跌 ──
  var C = {
    code: root.dataset.defaultCode || '000001', timeframe: '1d', chartType: 'candle',
    overlays: { tradePlan: false, scenario: true, primaryTrend: false, levels: false, signals: false, autoDrawings: true, ma5: false, ma10: false, ma20: false, ma60: false, ema20: false, ema60: false, boll: false },
    drawTool: null, drawPending: [], drawings: [],
    daily: [], planDaily: [], planMeta: {}, current: [], intraday: [], analysis: null, indicators: null, researchSignals: [], quant: null,
    chart: null, indicatorChart: null, series: null, radarChart: null, intel: null, scorePendingProfile: false,
    snapshotState: 'idle', intelState: 'idle', intelError: null, chartState: 'idle',
    indicatorSeries: null,
    indParams: { ma: [5, 10, 20, 60], bollPeriod: 20, bollMult: 2, macdFast: 12, macdSlow: 26, macdSignal: 9, kdjN: 9, kdjK: 3, kdjD: 3, rsiPeriod: 14 },
    manualLines: [], autoLevels: { supports: [], resistances: [], fib: [] }, autoDrawings: [], autoVisibility: {}, aiMarketContext: null, indicatorPanel: 'macd', novice: false, indicatorData: null, loadToken: 0, chartRequestSequence: 0,
    chartMeta: {}, intradayMeta: {}, refreshTimer: null, refreshBusy: false, lastTimingRefresh: 0,
    profileRefreshTimer: null, profileRefreshAttemptToken: null,
    marketSession: null, marketSessionError: null, quoteRefreshError: null, timingError: null, enrichmentError: null, storageErrors: [],
    accountAccess: false, position: null, positionLoaded: false, positionError: null, portfolioSummary: null, lastPositionRefresh: 0,
    strategyRoute: null, strategyRouteError: null, lastStrategyRouteRefresh: 0,
    undoStack: [], redoStack: [], magnet: true, fitOnRender: false, aiViewSeeded: false, observationFocus: null, _syncX: false, verticalGuides: [],
    aiMode: 'balanced', aiDiagnostics: null, aiMeta: null, aiError: null, aiAnalyzedAt: null,
    aiFocus: null, aiScenarios: [], decisionMap: null, focusedAutoId: null, focusedScenarioRole: null, volatilityMode: 'none',
  };
  var LABELS = { '1m':'1分钟', '5m':'5分钟', '15m':'15分钟', '30m':'30分钟', '60m':'60分钟', '1d':'日K', '1w':'周K', '1mo':'月K', '1q':'季K', '1y':'年K' };
  var TOOL_NAMES = { support:'标记止跌价', resistance:'标记遇阻价', horizontal:'水平线', trend:'趋势线', ray:'射线', fib:'斐波那契回撤', channel:'通道', parallel:'平行通道', rectangle:'价格区间', vertical:'垂直线', fibExtension:'斐波那契扩展', measure:'价格/时间测量', longPosition:'多头风险收益框', shortPosition:'空头风险收益框', anchoredVwap:'锚定 VWAP', text:'文字标注' };
  var $ = function (id) { return document.getElementById(id); };
  var escapeHtml = function (value) { return String(value == null ? '—' : value).replace(/[&<>'"]/g, function (c) { return ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[c]; }); };
  var escapeAttr = function (value) { return String(value == null ? '' : value).replace(/[&<>'"]/g, function (c) { return ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[c]; }); };
  var safeHttpUrl = function (value) {
    try {
      var parsed = new URL(String(value || ''));
      return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null;
    } catch (error) { return null; }
  };
  var n = function (value, digits) { return value == null || !isFinite(Number(value)) ? '—' : Number(value).toFixed(digits == null ? 2 : digits); };
  var pct = function (value, digits) { return value == null || !isFinite(Number(value)) ? '—' : (Number(value) > 0 ? '+' : '') + Number(value).toFixed(digits == null ? 2 : digits) + '%'; };
  var colour = function (value) { return Number(value) > 0 ? 'itc-up' : Number(value) < 0 ? 'itc-down' : ''; };
  var finite = function (value) { return value != null && isFinite(Number(value)); };
  var clamp = function (value, low, high) { return Math.max(low, Math.min(high, value)); };
  var mean = function (values) { return values.length ? values.reduce(function (a, b) { return a + b; }, 0) / values.length : null; };
  function displayResearchRating(rating, hasPosition) { return rating === '卖出' && !hasPosition ? '回避' : rating; }
  function researchScoreBand(score) {
    if (!finite(score)) return '研究分档未知';
    if (Number(score) >= 80) return '高分研究候选';
    if (Number(score) >= 65) return '较高分研究候选';
    if (Number(score) >= 50) return '中性研究候选';
    if (Number(score) >= 35) return '低分研究候选';
    return '排除级研究候选';
  }
  function claimOverlayPrice(prices, value) {
    if (!finite(value) || prices.some(function (price) { return Number(price).toFixed(2) === Number(value).toFixed(2); })) return false;
    prices.push(Number(value)); return true;
  }
  var reportTerminalIssue = function (scope, error) {
    console.warn('[stock-terminal:' + scope + ']', error && error.message ? error.message : error);
  };
  function renderStockSection(ids, render) {
    try { render(); return true; } catch (error) {
      reportTerminalIssue('display-' + ids.join('-'), error);
      ids.forEach(function (id) {
        if ($(id)) $(id).textContent = '此区域显示失败；其他区域可继续查看。重新查询当前股票可重试。';
      });
      return false;
    }
  }
  function intelUnavailableText() {
    if (C.intelState === 'pending') return '正在载入投研聚合…';
    if (C.intelState === 'waiting') return '等待当前股票快照；投研尚未请求。';
    if (C.intelState === 'blocked') return '主数据未取得，投研尚未请求；重新查询当前股票可重试。';
    if (C.intelState === 'error') return '投研读取失败：' + (C.intelError || '未知错误') + '；重新查询当前股票可重试。';
    return '投研数据不可用。';
  }
  function markStorageError(scope, label, error) {
    reportTerminalIssue(scope, error);
    if (C.storageErrors.indexOf(label) < 0) C.storageErrors.push(label);
  }
  function setRovingTabState(tablist, selector, selected) {
    if (!tablist || !selected) return;
    tablist.querySelectorAll(selector).forEach(function (button) {
      var active = button === selected;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
      button.setAttribute('tabindex', active ? '0' : '-1');
    });
  }
  function bindRovingTabs(tablist, selector, onActivate) {
    if (!tablist) return;
    var buttons = Array.from(tablist.querySelectorAll(selector));
    if (!buttons.length) return;
    var selected = buttons.filter(function (button) { return button.getAttribute('aria-selected') === 'true'; })[0] || buttons[0];
    setRovingTabState(tablist, selector, selected);
    var activate = function (button, moveFocus) {
      setRovingTabState(tablist, selector, button);
      onActivate(button);
      if (moveFocus) button.focus();
    };
    buttons.forEach(function (button) {
      button.addEventListener('click', function () { activate(button, false); });
      button.addEventListener('keydown', function (event) {
        var index = buttons.indexOf(button), next = null;
        if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
        else if (event.key === 'ArrowLeft') next = (index - 1 + buttons.length) % buttons.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = buttons.length - 1;
        if (next == null) return;
        event.preventDefault();
        activate(buttons[next], true);
      });
    });
  }
  var api = function (url, timeoutMs) {
    var controller = new AbortController(), timeout = setTimeout(function () { controller.abort(); }, timeoutMs || 7000);
    return fetch(url, { signal:controller.signal }).then(function (r) {
      return r.json().then(function (data) { if (!r.ok) throw new Error(data.error || '请求失败'); return data; });
    }).catch(function (error) {
      if (error && error.name === 'AbortError') throw new Error('上游数据请求超时；页面不会无限等待');
      throw error;
    }).finally(function () { clearTimeout(timeout); });
  };
  var postApi = function (url, timeoutMs) {
    var controller = new AbortController(), timeout = setTimeout(function () { controller.abort(); }, timeoutMs || 7000);
    return fetch(url, { method:'POST', headers:{'Accept':'application/json'}, signal:controller.signal }).then(function (r) {
      return r.json().then(function (data) { if (!r.ok) throw new Error(data.error || '刷新请求失败'); return data; });
    }).catch(function (error) {
      if (error && error.name === 'AbortError') throw new Error('公司资料刷新请求超时');
      throw error;
    }).finally(function () { clearTimeout(timeout); });
  };
  // Lightweight Charts formats Unix timestamps as UTC. Treat the source's
  // exchange-local wall clock as UTC so 09:30 is rendered as 09:30, not 01:30.
  var timeValue = function (value) {
    var text = String(value);
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
    var wall = text.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?/);
    var stamp = wall ? Date.parse(wall[1] + 'T' + wall[2] + ':' + (wall[3] || '00') + 'Z') : Date.parse(text);
    return isNaN(stamp) ? text : Math.floor(stamp / 1000);
  };
  var numTime = function (t) {
    if (typeof t === 'number') return t;
    if (t && typeof t === 'object' && t.year != null) {
      var mm = String(t.month).length === 1 ? '0' + t.month : String(t.month);
      var dd = String(t.day).length === 1 ? '0' + t.day : String(t.day);
      return Date.parse(String(t.year) + '-' + mm + '-' + dd + 'T00:00:00Z') / 1000;
    }
    if (/^\d{4}-\d{2}-\d{2}$/.test(String(t))) return Date.parse(String(t) + 'T00:00:00Z') / 1000;
    var converted = timeValue(t);
    return typeof converted === 'number' ? converted : 0;
  };
  // 把任意 time 表示（unix 秒 / "YYYY-MM-DD" / BusinessDay 对象）归一为可比较键。
  // 修复：日K及以上周期 param.time 是 BusinessDay 对象，旧代码 String() 拼出
  // "[object Object]" 导致磁吸/锚点永久失效——这是"日K上画线落不到点"的根因。
  var timeKey = function (t) {
    if (t == null) return -1;
    if (typeof t === 'number') return t;
    if (typeof t === 'string') return /^\d{4}-\d{2}-\d{2}$/.test(t) ? Date.parse(t + 'T00:00:00Z') / 1000 : numTime(t);
    if (t && typeof t === 'object' && t.year != null) return numTime(t);
    return numTime(t);
  };
  var chartAdjustType = function () {
    if (C.chartType === 'intraday') return 'raw';
    var observed = (C.chartMeta || {}).adjust_type;
    if (observed === 'raw' || observed === 'qfq' || observed === 'hfq' || observed === 'unknown') return observed;
    if (C.timeframe === '1d') return (C.planMeta || {}).decision_price_eligible === true ? 'raw' : 'unknown';
    return 'unknown';
  };
  var chartPriceLabel = function () { return {raw:'不复权',qfq:'前复权',hfq:'后复权',unknown:'价格口径待核验'}[chartAdjustType()] || '价格口径待核验'; };
  function syncAdjustLabel() {
    var select = $('terminalAdjust');
    if (!select) return;
    if (C.chartType === 'intraday') select.innerHTML = '<option>不复权（真实分时）</option>';
    else if (chartAdjustType() === 'unknown') select.innerHTML = '<option>价格口径待核验</option>';
    else if (chartAdjustType() === 'qfq') select.innerHTML = '<option>前复权</option>';
    else if (chartAdjustType() === 'hfq') select.innerHTML = '<option>后复权</option>';
    else if (C.timeframe === '1d') select.innerHTML = '<option>不复权（' + ((C.planMeta || {}).decision_price_eligible === true && (C.chartMeta || {}).decision_price_eligible !== false ? '已审计日K' : '日K') + '）</option>';
    else select.innerHTML = '<option>不复权（' + (['1w','1mo','1q','1y'].indexOf(C.timeframe) >= 0 ? '日K聚合' : '分钟K') + '）</option>';
  }
  var chartScope = function () { return (C.chartType === 'intraday' ? 'intraday' : C.timeframe) + ':' + chartAdjustType(); };
  var storageKey = function () { return 'regis-terminal-lines:' + C.code + ':' + chartScope(); };
  var legacyStorageKey = function () { return 'regis-terminal-lines:' + C.code; };
  var drawingsKey = function () { return 'regis-terminal-drawings:' + C.code + ':' + chartScope(); };
  var legacyDrawingsKey = function () { return 'regis-terminal-drawings:' + C.code + ':' + C.timeframe; };
  var autoVisibilityKey = function () { return 'regis-terminal-auto-drawings:' + C.code + ':' + chartScope(); };
  var watchlistKey = 'regis-terminal-watchlist:v1';
  var decisionLedgerKey = 'regis-terminal-decision-ledger:v1';
  var lineColor = function (kind) { return kind === 'support' ? '#2EBD85' : '#F6465D'; };
  // ── 分时图专业视图辅助（与后端同口径的板块涨跌幅） ──
  var boardLimitPct = function (code) { return /^(300|301|688|689)/.test(String(code || '')) ? 20 : 10; };
  // 注：当前图表库构建仅支持 v3 风格 API（无 customTickMarkFormatter/MulticolorSeries/panes）。
  // 因此分时轴沿用墙上时钟时间（内置格式化器正确显示 09:30/11:30/13:00，午休如实留白），
  // 红涨绿跌用"同色连续段拆分为多条线"实现，量能面板复用页面既有副图容器。
  var localCreatedAt = function (value) { var date = new Date(value); return isNaN(date.getTime()) ? String(value || '') : date.toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).replace(/\//g,'-'); };
  function syncResearchData(payload) {
    payload = payload || {};
    var signals = Array.isArray(payload.research_signals) && payload.research_signals.length ? payload.research_signals : (Array.isArray(payload.signals) ? payload.signals : []);
    var minuteAnalysis = (payload.data_status || {}).minute_analysis;
    if (minuteAnalysis) C.researchSignals = minuteAnalysis.status === 'available' ? signals : [];
    else if (signals.length || !C.researchSignals.length) C.researchSignals = signals;
    if (payload.quant) C.quant = payload.quant;
  }
  function researchSignals() { return C.researchSignals || []; }
  function researchSignalIsBearish(signal) {
    return String((signal || {}).direction || '').toLowerCase() === 'bearish'
      || /bearish|sell|卖/.test(String((signal || {}).action || '') + String((signal || {}).label || ''));
  }
  function quantView() { return C.quant || ((C.analysis || {}).quant) || {}; }
  function decisionRecords() {
    try {
      var records = JSON.parse(localStorage.getItem(decisionLedgerKey) || '[]');
      return Array.isArray(records) ? records : [];
    } catch (error) { markStorageError('decision-ledger-read', '决策留痕读取失败', error); return []; }
  }
  function appendDecisionRecord(record) {
    try {
      var records = decisionRecords();
      if (record.fingerprint && records.some(function (item) { return item.fingerprint === record.fingerprint; })) return;
      records.push(Object.assign({recordedAt:new Date().toISOString()}, record));
      localStorage.setItem(decisionLedgerKey, JSON.stringify(records.slice(-200)));
    } catch (error) { markStorageError('decision-ledger-write', '决策留痕写入失败', error); }
  }

  function aggregate(candles, mode) {
    var buckets = {};
    (candles || []).forEach(function (row) {
      var raw = String(row.time), date = raw.slice(0, 10), key = date;
      if (mode === '60m') {
        var match = raw.match(/(\d{2}):(\d{2})/), minute = match ? Number(match[1]) * 60 + Number(match[2]) : 0;
        // Source minute bars are end-labelled. A-share 60-minute buckets end
        // at 10:30, 11:30, 14:00 and 15:00 respectively.
        var slot = minute <= 630 ? 0 : minute <= 690 ? 1 : minute <= 840 ? 2 : 3;
        key = date + ':' + Math.max(0, slot);
      } else if (mode === '1w') {
        var parsed = new Date(date + 'T00:00:00'); var offset = (parsed.getDay() + 6) % 7; parsed.setDate(parsed.getDate() - offset); key = parsed.toISOString().slice(0,10);
      } else if (mode === '1mo') key = date.slice(0, 7);
      else if (mode === '1q') key = date.slice(0,4) + '-Q' + (Math.floor((Number(date.slice(5,7)) - 1) / 3) + 1);
      else if (mode === '1y') key = date.slice(0,4);
      var bucketTime = mode === '60m' ? date + ' ' + ['10:30:00','11:30:00','14:00:00','15:00:00'][slot] : row.time;
      if (!buckets[key]) buckets[key] = { time: bucketTime, open:Number(row.open), high:Number(row.high), low:Number(row.low), close:Number(row.close), volume:Number(row.volume) || 0 };
      else { if (mode !== '60m') buckets[key].time = row.time; buckets[key].high = Math.max(buckets[key].high, Number(row.high)); buckets[key].low = Math.min(buckets[key].low, Number(row.low)); buckets[key].close = Number(row.close); buckets[key].volume += Number(row.volume) || 0; }
    });
    return Object.keys(buckets).sort().map(function (key) { return buckets[key]; });
  }
  function atr(candles) {
    if (!candles || candles.length < 3) return null;
    var tail = candles.slice(-15);
    var ranges = tail.slice(1).map(function (row, index) { var previous = tail[index].close; return Math.max(row.high - row.low, Math.abs(row.high - previous), Math.abs(row.low - previous)); });
    return mean(ranges);
  }
  function deriveLevels(candles) {
    var source = (candles || []).slice(-120), last = source[source.length - 1];
    if (!last || source.length < 10) return { supports: [], resistances: [], fib: [] };
    var tolerance = Math.max((atr(source) || last.close * 0.01) * 1.2, last.close * 0.004), raw = [];
    for (var i = 2; i < source.length - 2; i += 1) {
      var high = source[i].high, low = source[i].low, highs = source.slice(i - 2, i + 3).map(function (v) { return v.high; }), lows = source.slice(i - 2, i + 3).map(function (v) { return v.low; });
      if (high === Math.max.apply(null, highs)) raw.push({ kind:'resistance', price:high });
      if (low === Math.min.apply(null, lows)) raw.push({ kind:'support', price:low });
    }
    function clusters(kind) {
      var bucket = [];
      raw.filter(function (item) { return item.kind === kind; }).forEach(function (item) {
        var cluster = bucket.find(function (entry) { return Math.abs(entry.price - item.price) <= tolerance; });
        if (cluster) { cluster.price = (cluster.price * cluster.count + item.price) / (cluster.count + 1); cluster.count += 1; } else bucket.push({ price:item.price, count:1 });
      });
      return bucket.sort(function (a, b) { return b.count - a.count; }).filter(function (entry) { return kind === 'support' ? entry.price <= last.close * 1.02 : entry.price >= last.close * 0.98; }).slice(0, 2);
    }
    var high = Math.max.apply(null, source.slice(-60).map(function (row) { return row.high; })), low = Math.min.apply(null, source.slice(-60).map(function (row) { return row.low; })), range = high - low;
    return { supports:clusters('support'), resistances:clusters('resistance'), fib:[.236,.382,.5,.618,.786].map(function (ratio) { return { ratio:ratio, price:high - range * ratio }; }), swing:{high:high, low:low} };
  }
  function sma(candles, period, field) {
    var out = Array((candles || []).length).fill(null), total = 0, key = field || 'close';
    (candles || []).forEach(function (row, index) {
      var value = Number(row[key]); total += value;
      if (index >= period) total -= Number(candles[index - period][key]);
      if (index >= period - 1) out[index] = total / period;
    });
    return out;
  }
  function emaNumbers(values, period) {
    var out = Array(values.length).fill(null), seed = [], prior = null, multiplier = 2 / (period + 1);
    values.forEach(function (value, index) {
      if (!finite(value)) return;
      if (prior == null) {
        seed.push(Number(value));
        if (seed.length === period) { prior = mean(seed); out[index] = prior; }
      } else { prior = (Number(value) - prior) * multiplier + prior; out[index] = prior; }
    });
    return out;
  }
  function ema(candles, period, field) { return emaNumbers((candles || []).map(function (row) { return row[field || 'close']; }), period); }
  function calculateIndicators(candles, params) {
    candles = candles || [];
    params = params || C.indParams || {};
    var maP = (params.ma && params.ma.length === 4) ? params.ma : [5, 10, 20, 60];
    var closes = candles.map(function (row) { return Number(row.close); });
    var ma5 = sma(candles, maP[0]), ma10 = sma(candles, maP[1]), ma20 = sma(candles, maP[2]), ma60 = sma(candles, maP[3]);
    var ema20 = ema(candles, 20), ema60 = ema(candles, 60);
    var bollPeriod = params.bollPeriod || 20, bollMult = params.bollMult || 2;
    var bollMid = sma(candles, bollPeriod);
    var std20 = closes.map(function (_, index) {
      if (index < bollPeriod - 1) return null;
      var values = closes.slice(index - bollPeriod + 1, index + 1), avg = bollMid[index];
      return Math.sqrt(values.reduce(function (sum, value) { return sum + Math.pow(value - avg, 2); }, 0) / bollPeriod);
    });
    var macdFast = params.macdFast || 12, macdSlow = params.macdSlow || 26, macdSignal = params.macdSignal || 9;
    var ema12 = ema(candles, macdFast), ema26 = ema(candles, macdSlow), dif = closes.map(function (_, index) { return finite(ema12[index]) && finite(ema26[index]) ? ema12[index] - ema26[index] : null; }), dea = emaNumbers(dif, macdSignal);
    var macd = closes.map(function (_, index) { return finite(dif[index]) && finite(dea[index]) ? 2 * (dif[index] - dea[index]) : null; });
    var kdjN = params.kdjN || 9, kdjK = params.kdjK || 3, kdjD = params.kdjD || 3;
    var k = Array(candles.length).fill(null), d = Array(candles.length).fill(null), j = Array(candles.length).fill(null), priorK = 50, priorD = 50;
    candles.forEach(function (row, index) {
      if (index < kdjN - 1) return;
      var sample = candles.slice(index - kdjN + 1, index + 1), high = Math.max.apply(null, sample.map(function (item) { return Number(item.high); })), low = Math.min.apply(null, sample.map(function (item) { return Number(item.low); }));
      var rsv = high === low ? 50 : (Number(row.close) - low) / (high - low) * 100;
      priorK = priorK * (kdjK - 1) / kdjK + rsv / kdjK; priorD = priorD * (kdjD - 1) / kdjD + priorK / kdjD;
      k[index] = priorK; d[index] = priorD; j[index] = 3 * priorK - 2 * priorD;
    });
    var rsiPeriod = params.rsiPeriod || 14;
    var rsi = Array(candles.length).fill(null), gain = 0, loss = 0;
    for (var i = 1; i < closes.length; i += 1) {
      var change = closes[i] - closes[i - 1], up = Math.max(change, 0), down = Math.max(-change, 0);
      if (i <= rsiPeriod) { gain += up; loss += down; if (i === rsiPeriod) { gain /= rsiPeriod; loss /= rsiPeriod; rsi[i] = loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss); } }
      else { gain = (gain * (rsiPeriod - 1) + up) / rsiPeriod; loss = (loss * (rsiPeriod - 1) + down) / rsiPeriod; rsi[i] = loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss); }
    }
    var atr14 = Array(candles.length).fill(null), atrSeed = 0, priorAtr = null;
    candles.forEach(function (row, index) {
      var previousClose = index ? Number(candles[index - 1].close) : Number(row.close);
      var trueRange = index ? Math.max(Number(row.high) - Number(row.low), Math.abs(Number(row.high) - previousClose), Math.abs(Number(row.low) - previousClose)) : Number(row.high) - Number(row.low);
      if (index < 14) atrSeed += trueRange;
      if (index === 13) { priorAtr = atrSeed / 14; atr14[index] = priorAtr; }
      else if (index >= 14) { priorAtr = (priorAtr * 13 + trueRange) / 14; atr14[index] = priorAtr; }
    });
    var atrUpper = bollMid.map(function (value, index) { return finite(value) && finite(atr14[index]) ? Number(value) + 1.5 * Number(atr14[index]) : null; });
    var atrLower = bollMid.map(function (value, index) { return finite(value) && finite(atr14[index]) ? Number(value) - 1.5 * Number(atr14[index]) : null; });
    var macdCrosses = [];
    for (var crossIndex = 1; crossIndex < candles.length; crossIndex += 1) {
      var priorDif = dif[crossIndex - 1], priorDea = dea[crossIndex - 1], currentDif = dif[crossIndex], currentDea = dea[crossIndex];
      if (!finite(priorDif) || !finite(priorDea) || !finite(currentDif) || !finite(currentDea)) continue;
      if (priorDif <= priorDea && currentDif > currentDea) macdCrosses.push({ index:crossIndex, kind:'golden', time:timeValue(candles[crossIndex].time), displayTime:String(candles[crossIndex].time), label:'金叉' });
      if (priorDif >= priorDea && currentDif < currentDea) macdCrosses.push({ index:crossIndex, kind:'dead', time:timeValue(candles[crossIndex].time), displayTime:String(candles[crossIndex].time), label:'死叉' });
    }
    return { ma5:ma5, ma10:ma10, ma20:ma20, ma60:ma60, ema20:ema20, ema60:ema60, bollMid:bollMid, bollUpper:bollMid.map(function (value, index) { return finite(value) && finite(std20[index]) ? value + bollMult * std20[index] : null; }), bollLower:bollMid.map(function (value, index) { return finite(value) && finite(std20[index]) ? value - bollMult * std20[index] : null; }), atr14:atr14, atrUpper:atrUpper, atrLower:atrLower, dif:dif, dea:dea, macd:macd, macdCrosses:macdCrosses, k:k, d:d, j:j, rsi:rsi };
  }
  function localExtrema(candles, kind) {
    var points = [];
    for (var i = 2; i < candles.length - 2; i += 1) {
      var window = candles.slice(i - 2, i + 3), value = Number(candles[i][kind]);
      var values = window.map(function (row) { return Number(row[kind]); });
      if ((kind === 'low' && value === Math.min.apply(null, values)) || (kind === 'high' && value === Math.max.apply(null, values))) points.push({ time:timeValue(candles[i].time), price:value });
    }
    return points;
  }
  // ── AI 划线 v2：zigzag 摆动点（2026-08-14 升级）──
  // 标准 zigzag：以最小反转幅度过滤噪声，输出交替的摆动高点/低点。
  function zigzagSwings(candles, threshold) {
    var out = [];
    if (!candles || candles.length < 5) return out;
    var dir = 0, last = null;
    for (var i = 2; i < candles.length - 2; i += 1) {
      var row = candles[i], win = candles.slice(i - 2, i + 3);
      var lows = win.map(function (w) { return Number(w.low); }), highs = win.map(function (w) { return Number(w.high); });
      var isLow = Number(row.low) === Math.min.apply(null, lows);
      var isHigh = Number(row.high) === Math.max.apply(null, highs);
      if (!isLow && !isHigh) continue;
      var kind = isHigh ? 'high' : 'low', price = kind === 'high' ? Number(row.high) : Number(row.low);
      if (!last) { dir = kind === 'high' ? 1 : -1; last = { time: timeValue(row.time), price: price, kind: kind }; out.push(last); continue; }
      if (kind === last.kind) {
        if ((kind === 'high' && price > last.price) || (kind === 'low' && price < last.price)) { last.time = timeValue(row.time); last.price = price; }
        continue;
      }
      var move = kind === 'high' ? price - last.price : last.price - price;
      if (move >= threshold) { last = { time: timeValue(row.time), price: price, kind: kind }; out.push(last); }
    }
    return out;
  }
  // 多触点趋势线：以最近一个摆动点为锚，向前扫描同类摆动点做线性拟合，
  // 计算被该线"触碰"（±tolerance）的同向摆动点数，触点越多强度越高。
  function fitMultiTouchLine(swings, kind, tolerance) {
    if (swings.length < 2) return null;
    var sameKind = swings.filter(function (s) { return s.kind === kind; });
    if (sameKind.length < 2) return null;
    var anchor = sameKind[sameKind.length - 1], best = null;
    for (var j = 0; j < sameKind.length - 1; j += 1) {
      var q = sameKind[j];
      if (q.time === anchor.time) continue;
      var dt = numTime(anchor.time) - numTime(q.time);
      if (!dt) continue;
      var slope = (anchor.price - q.price) / dt;
      var touches = 0, touchPoints = [];
      for (var k = j; k < sameKind.length; k += 1) {
        var p = sameKind[k];
        if (p.time === q.time || p.time === anchor.time) continue;
        var expected = q.price + slope * (numTime(p.time) - numTime(q.time));
        if (Math.abs(expected - p.price) <= tolerance) { touches += 1; touchPoints.push(p); }
      }
      // 触点数 + 锚点是否靠近（距离）→ 强度
      var strength = Math.round(Math.min(100, 55 + touches * 15 - Math.abs(slope) * 4000));
      if (touches >= 1 && (!best || touches > best.touches || (touches === best.touches && strength > best.strength))) {
        best = { a: q, b: anchor, slope: slope, touches: touches + 2, strength: Math.max(20, Math.min(98, strength)) };
      }
    }
    return best;
  }
  function normaliseAiDrawing(item) {
    item = item || {};
    var kind = String(item.kind || item.type || '').toLowerCase();
    var type = /impulse_channel/.test(kind) ? 'impulse_channel' : /support|resistance|zone/.test(kind) ? 'zone' : /channel/.test(kind) ? 'channel' : /trend/.test(kind) ? 'trend' : /breakout/.test(kind) ? 'breakout' : /pivot|level|horizontal/.test(kind) ? 'pivot' : item.type;
    var evidence = '';
    if (Array.isArray(item.evidence)) evidence = item.evidence.map(function (entry) {
      if (typeof entry === 'string') return entry;
      return entry && (entry.label || entry.reason || entry.text || entry.value) ? String(entry.label || entry.reason || entry.text || entry.value) : '';
    }).filter(Boolean).join('；');
    else if (item.evidence && typeof item.evidence === 'object') {
      var ev = item.evidence, evBits = [];
      if (ev.method) evBits.push('方法 ' + ev.method);
      if (finite(ev.tolerance)) evBits.push('容差 ' + n(ev.tolerance, 3));
      if (finite(ev.spanBars)) evBits.push('跨度 ' + ev.spanBars + ' 根K线');
      if (finite(ev.violations)) evBits.push('越界 ' + ev.violations + ' 次');
      if (finite(ev.widthAtr)) evBits.push('通道宽 ' + n(ev.widthAtr, 2) + ' ATR');
      var touchList = ev.touchTimes || ev.touchPoints || ev.parallelTouchPoints;
      if (Array.isArray(touchList)) evBits.push('证据点 ' + touchList.length + ' 个');
      evidence = evBits.join('；');
    } else evidence = String(item.evidence || '');
    var color = item.color || (/fib/.test(kind) ? '#BF5AF2' : /support|uptrend/.test(kind) ? '#2EBD85' : /resistance|downtrend/.test(kind) ? '#F6465D' : '#0A84FF');
    return {
      id:'regis-ai:' + String(item.id || kind || Math.random().toString(36).slice(2, 8)), type:type, kind:kind,
      label:item.label || '结构候选', explanation:item.reason || evidence || '模型未提供可解释依据，候选不展示。',
      evidence:evidence, points:(item.points || []).map(function (point) { return { time:timeValue(point.time), price:Number(point.price) }; }).filter(function (point) { return point.time != null && finite(point.price); }),
      low:finite(item.low) ? Number(item.low) : null, high:finite(item.high) ? Number(item.high) : null,
      zoneLow:finite(item.zoneLow) ? Number(item.zoneLow) : null, zoneHigh:finite(item.zoneHigh) ? Number(item.zoneHigh) : null,
      swingLow:finite(item.swingLow) ? Number(item.swingLow) : null, swingHigh:finite(item.swingHigh) ? Number(item.swingHigh) : null,
      price:finite(item.price) ? Number(item.price) : null, time:item.time != null ? timeValue(item.time) : null,
      projectedPrice:finite(item.projectedPrice) ? Number(item.projectedPrice) : null,
      triggerPrice:finite(item.triggerPrice) ? Number(item.triggerPrice) : null,
      invalidationPrice:finite(item.invalidationPrice) ? Number(item.invalidationPrice) : null,
      patternState:item.patternState ? String(item.patternState) : null,
      presentationStyle:item.evidence && item.evidence.presentationStyle ? String(item.evidence.presentationStyle) : null,
      distancePct:finite(item.distancePct) ? Number(item.distancePct) : null,
      distanceAtr:finite(item.distanceAtr) ? Number(item.distanceAtr) : null,
      lastTouchBars:finite(item.lastTouchBars) ? Number(item.lastTouchBars) : null,
      role:String(item.role || 'structure'), state:String(item.state || 'active'), priority:String(item.priority || 'secondary'),
      color:color, strength:finite(item.score) ? clamp(Math.round(Number(item.score)), 0, 100) : null,
      confidence:String(item.confidence || 'low').toLowerCase(), touches:finite(item.touches) ? Number(item.touches) : 0,
      invalidated:item.invalidated === true,
    };
  }
  function aiDecisionContext() {
    var sameDailyBasis = C.chartType !== 'intraday' && C.timeframe === '1d' && chartAdjustType() === 'raw';
    var meta = C.chartMeta || {};
    return {
      tradePlan:sameDailyBasis ? buildTradePlan() : {available:false, reason:'当前图表不是已审计 raw 日K，不混入日线执行计划。'},
      sellPlan:sameDailyBasis ? buildPositionSellPlan() : {available:false, hasPosition:false, reason:'当前图表口径与持仓风险线不同，不混画。'},
      timing:sameDailyBasis ? ((C.intel || {}).timing || {}) : {},
      priceBasis:meta.price_basis || ((C.planMeta || {}).price_basis) || (chartAdjustType() === 'qfq' ? 'tencent_qfq' : 'raw'),
      barStatus:meta.bar_status || (C.timeframe === '1d' ? 'complete' : 'source_declared')
    };
  }
  function aiContextCandles(candles) {
    var contexts = {};
    if (C.chartType === 'intraday' || C.timeframe.indexOf('m') >= 0) {
      if ((C.daily || []).length >= 20) contexts['1d'] = C.daily;
      return contexts;
    }
    if (C.timeframe === '1d') {
      var weekly = aggregate(candles || [], '1w');
      if (weekly.length >= 20) contexts['1w'] = weekly;
    } else if (C.timeframe === '1w') {
      var monthly = aggregate((C.daily || []).length ? C.daily : (candles || []), '1mo');
      if (monthly.length >= 20) contexts['1mo'] = monthly;
    }
    return contexts;
  }
  function deriveAutoDrawings(candles) {
    var levels = deriveLevels(candles);
    if (!window.RegisAIDrawings || typeof window.RegisAIDrawings.analyze !== 'function') {
      var fallback = deriveLegacyAutoDrawings(candles);
      fallback.diagnostics = { status:'fallback', reason:'自研划线引擎未加载，暂用旧版本地规则。' };
      fallback.meta = { engine:'legacy-local-rules' };
      return fallback;
    }
    try {
      var result = window.RegisAIDrawings.analyze(candles || [], { sensitivity:C.aiMode, timeframe:C.timeframe, symbol:C.code, adjustType:chartAdjustType(), contexts:aiContextCandles(candles), decisionContext:aiDecisionContext() }) || {};
      var items = (result.drawings || []).map(normaliseAiDrawing).filter(function (item) {
        if (!item.type || !item.explanation) return false;
        if (item.type === 'zone') return finite(item.low) && finite(item.high) && item.high >= item.low;
        if (item.type === 'pivot' || item.type === 'breakout') return finite(item.price);
        return item.points.length >= (item.type === 'channel' ? 3 : item.type === 'impulse_channel' ? 4 : 2);
      });
      return { levels:levels, items:items, focus:result.focus || null, scenarios:Array.isArray(result.scenarios) ? result.scenarios : [], marketContext:result.marketContext || null, decisionMap:result.decisionMap || null, diagnostics:result.diagnostics || [], meta:Object.assign({engine:'Regis AI v' + (result.version || window.RegisAIDrawings.VERSION || '1'), adjustType:chartAdjustType()}, result.meta || {}) };
    } catch (error) {
      return { levels:levels, items:[], diagnostics:{status:'error', reason:error && error.message ? error.message : '结构识别失败'}, meta:{engine:'Regis AI'} };
    }
  }
  function deriveLegacyAutoDrawings(candles) {
    var levels = deriveLevels(candles), items = [], source = (candles || []).slice(-120);
    if (!source.length) return { levels:levels, items:items };
    var last = source[source.length - 1], lastClose = Number(last.close);
    var atrVal = atr(source) || lastClose * 0.01;
    var tolerance = Math.max(atrVal * .65, lastClose * .002);

    // ── 支撑/压力区（带触点强度）──
    levels.supports.forEach(function (item, index) {
      var strength = Math.round(Math.min(95, 50 + item.count * 14));
      items.push({ id:'support-' + index, type:'zone', label:'支撑区', explanation:'多个局部低点在此聚集（' + item.count + ' 触）· 强度 ' + strength + ' · 只是历史聚集区，不代表一定止跌。', low:item.price - tolerance, high:item.price + tolerance, color:'#2EBD85', strength:strength });
    });
    levels.resistances.forEach(function (item, index) {
      var strength = Math.round(Math.min(95, 50 + item.count * 14));
      items.push({ id:'resistance-' + index, type:'zone', label:'压力区', explanation:'多个局部高点在此聚集（' + item.count + ' 触）· 强度 ' + strength + ' · 只是历史聚集区，不代表一定遇阻。', low:item.price - tolerance, high:item.price + tolerance, color:'#F6465D', strength:strength });
    });

    // ── zigzag 摆动点 + 多触点趋势线 ──
    var swings = zigzagSwings(source, Math.max(atrVal * 1.4, lastClose * .008));
    if (swings.length >= 2) {
      var up = fitMultiTouchLine(swings, 'low', tolerance);
      if (up && up.b.price < up.a.price) up = null; // 上升趋势线要求低点抬高
      if (up) items.push({ id:'up-trend', type:'trend', label:'上升趋势线·' + up.touches + '触', explanation:'zigzag 摆动低点构成的多触点上升趋势线（' + up.touches + ' 触）· 强度 ' + up.strength + ' · 跌破不等同卖出信号。', points:[up.a, up.b], color:'#2EBD85', strength:up.strength });
      var down = fitMultiTouchLine(swings, 'high', tolerance);
      if (down && down.b.price > down.a.price) down = null; // 下降趋势线要求高点降低
      if (down) items.push({ id:'down-trend', type:'trend', label:'下降趋势线·' + down.touches + '触', explanation:'zigzag 摆动高点构成的多触点下降趋势线（' + down.touches + ' 触）· 强度 ' + down.strength + ' · 突破不等同买入信号。', points:[down.a, down.b], color:'#F6465D', strength:down.strength });
      var upA = up && up.a, upB = up && up.b, channelTop = null;
      for (var si = swings.length - 1; si >= 0; si -= 1) { if (swings[si].kind === 'high') { channelTop = swings[si]; break; } }
      if (upA && upB && channelTop && upB.time !== upA.time && channelTop.time !== upA.time) {
        items.push({ id:'price-channel', type:'channel', label:'价格通道', explanation:'沿上升趋势线构造平行通道，仅用于观察价格运行范围。', points:[upA, channelTop, upB], color:'#0A84FF', strength:70 });
      }
    }

    // ── 箱体（横盘整理区间）──
    var boxWin = source.slice(-24);
    if (boxWin.length >= 16) {
      var boxHigh = Math.max.apply(null, boxWin.map(function (r) { return Number(r.high); }));
      var boxLow = Math.min.apply(null, boxWin.map(function (r) { return Number(r.low); }));
      var boxRange = boxHigh - boxLow;
      if (boxRange > 0 && boxRange / lastClose < 0.10 && lastClose >= boxLow * 0.995 && lastClose <= boxHigh * 1.005) {
        var inside = boxWin.filter(function (r) { return Number(r.close) >= boxLow && Number(r.close) <= boxHigh; }).length;
        var boxStrength = Math.round(Math.min(95, 40 + inside * 2 + (boxRange / lastClose < 0.06 ? 15 : 0)));
        items.push({ id:'box-range', type:'box', label:'箱体区间', explanation:'近' + boxWin.length + '根K线横盘整理（振幅' + (boxRange / lastClose * 100).toFixed(1) + '%）· 强度 ' + boxStrength + ' · 突破上沿/下沿才构成方向变化。', low:boxLow, high:boxHigh, color:'#0A84FF', strength:boxStrength });
      }
    }

    // ── 枢轴点（floor/pivot/ceiling）──
    var H = Number(last.high), L = Number(last.low), C = Number(last.close);
    if (H > L) {
      var P = (H + L + C) / 3, R1 = 2 * P - L, S1 = 2 * P - H;
      items.push({ id:'pivot-r1', type:'pivot', label:'枢轴·上方阻力', explanation:'经典枢轴 R1（2×P−L）= ' + n(R1, 2) + ' · 突破参考位，非卖出信号。', price:R1, color:'#F6465D', strength:55 });
      items.push({ id:'pivot-p', type:'pivot', label:'枢轴·中轴', explanation:'经典枢轴 P（(H+L+C)/3）= ' + n(P, 2) + ' · 多空分界参考。', price:P, color:'#8B8F9A', strength:50 });
      items.push({ id:'pivot-s1', type:'pivot', label:'枢轴·下方支撑', explanation:'经典枢轴 S1（2×P−H）= ' + n(S1, 2) + ' · 回踩参考位，非买入信号。', price:S1, color:'#2EBD85', strength:55 });
    }

    // ── 突破：收盘突破箱体上沿/下沿（近3根内）──
    // 参考箱体取"最近3根之前"的区间：若把最近3根纳入 boxHigh，突破永远无法超过
    // 自己所在那根K线，突破分支就成了死代码（2026-08-14 CDP 实测修正）。
    var refWin = source.slice(-27, -3);
    if (refWin.length >= 16) {
      var refHigh = Math.max.apply(null, refWin.map(function (r) { return Number(r.high); }));
      var refLow = Math.min.apply(null, refWin.map(function (r) { return Number(r.low); }));
      var refRange = refHigh - refLow;
      if (refRange > 0) {
        for (var bi = source.length - 3; bi < source.length; bi += 1) {
          var br = source[bi];
          if (Number(br.close) > refHigh && Number(br.high) > refHigh) {
            items.push({ id:'breakout-up', type:'breakout', label:'向上突破箱体', explanation:'收盘突破前箱体上沿 ' + n(refHigh, 2) + ' · 方向信号但需量能确认，不构成买入指令。', price:refHigh, time:timeValue(br.time), color:'#F6465D', strength:88 });
            break;
          }
          if (Number(br.close) < refLow && Number(br.low) < refLow) {
            items.push({ id:'breakout-down', type:'breakout', label:'向下破位箱体', explanation:'收盘跌破前箱体下沿 ' + n(refLow, 2) + ' · 方向信号，注意风控。', price:refLow, time:timeValue(br.time), color:'#2EBD85', strength:88 });
            break;
          }
        }
      }
    }

    if (levels.swing && levels.swing.high > levels.swing.low) items.push({ id:'fib-retracement', type:'fib', label:'斐波那契回撤', explanation:'按当前周期最近60根K线高低点计算，不预测下一根K线。', low:levels.swing.low, high:levels.swing.high, color:'#BF5AF2', strength:45 });
    return { levels:levels, items:items };
  }
  function health(analysis) {
    var quote = analysis.quote || {}, freshness = quote.freshness || 'unknown', source = quote.source || '未知来源';
    var technical = analysis.technical || {}, meta = analysis.meta || {};
    var exchangeTime = String(quote.exchange_data_time || quote.data_time || '');
    if (technical.status === 'insufficient_history') {
      var bars = Number(meta.history_bars || technical.history_bars || (analysis.candles || []).length || 0);
      var required = Number(meta.technical_history_required || technical.required_history_bars || 35);
      return {
        level:'delay',
        label:'审计日线可用 · 新股样本' + bars + '根',
        detail:'交易所截至 ' + (exchangeTime || '未知') + ' · ' + source + '；技术状态需' + required + '根，样本积累前不生成技术买卖点'
      };
    }
    if (meta.terminal_mode === 'public_daily_runtime_fallback') {
      return {
        level:'delay',
        label:'公开日线可用 · 非本机审计',
        detail:'数据截至 ' + (exchangeTime || '未知') + ' · ' + source + '；可看K线与技术状态，但不生成执行价格或提升交易证据等级'
      };
    }
    if (freshness === 'closed_snapshot' && marketSessionPhase() === 'auction' && exchangeTime.slice(0, 10) === marketSessionDate()) {
      return { level:'delay', label:'集合竞价快照', detail:'交易所截至 ' + (exchangeTime || '未知') + ' · ' + source + ' · 不作为连续竞价买卖触发' };
    }
    if (freshness === 'closed_snapshot') return { level:'delay', label:'休市快照', detail:'交易所截至 ' + (exchangeTime || '未知') + ' · ' + source };
    if (freshness === 'live' || freshness === 'fresh') return { level:'ok', label:'已验证可用', detail:'数据截至 ' + (quote.data_time || '未知') + ' · ' + source };
    return { level:'bad', label:'数据状态未知', detail:'数据截至 ' + (quote.data_time || '未知') + ' · ' + source };
  }
  function linesFromStorage() {
    try {
      var raw = localStorage.getItem(storageKey());
      if (raw == null) raw = chartAdjustType() === 'raw' ? localStorage.getItem(legacyStorageKey()) || '[]' : '[]';
      var stored = JSON.parse(raw); return Array.isArray(stored) ? stored.filter(function (item) { return finite(item.price) && ['support','resistance'].indexOf(item.kind) >= 0; }) : [];
    } catch (error) { markStorageError('manual-lines-read', '价格标记读取失败', error); return []; }
  }
  function saveLines() { localStorage.setItem(storageKey(), JSON.stringify(C.manualLines)); }
  function drawingsFromStorage() {
    try {
      var raw = localStorage.getItem(drawingsKey());
      if (raw == null && C.chartType !== 'intraday' && chartAdjustType() === 'raw') raw = localStorage.getItem(legacyDrawingsKey());
      var stored = JSON.parse(raw || '[]');
      return Array.isArray(stored) ? stored.map(function (item) {
        return { id:item.id || ('d' + Date.now() + Math.random().toString(36).slice(2,6)), type:item.type, points:Array.isArray(item.points) ? item.points : [], name:item.name || TOOL_NAMES[item.type] || '研究画线', note:item.note || '', visible:item.visible !== false, createdAt:item.createdAt || new Date().toISOString(), sourceAutoId:item.sourceAutoId || null, aiScore:finite(item.aiScore) ? Number(item.aiScore) : null, aiEvidence:item.aiEvidence || '', origin:item.origin || null, engineVersion:item.engineVersion || null, analysisAsOf:item.analysisAsOf || null, adjustType:item.adjustType || null };
      }).filter(function (item) {
        return !!TOOL_NAMES[item.type];
      }) : [];
    } catch (error) { markStorageError('drawings-read', '图表画线读取失败', error); return []; }
  }
  function saveDrawings() { localStorage.setItem(drawingsKey(), JSON.stringify(C.drawings)); }
  function autoVisibilityFromStorage() {
    try { var stored = JSON.parse(localStorage.getItem(autoVisibilityKey()) || '{}'); return stored && typeof stored === 'object' ? stored : {}; } catch (error) { markStorageError('auto-visibility-read', 'AI画线可见性读取失败', error); return {}; }
  }
  function saveAutoVisibility() { localStorage.setItem(autoVisibilityKey(), JSON.stringify(C.autoVisibility)); }
  function watchlistFromStorage() {
    try {
      var stored = JSON.parse(localStorage.getItem(watchlistKey) || '[]');
      return Array.isArray(stored) ? stored.map(function (item) {
        return { code:String((item || {}).code || ''), name:String((item || {}).name || '') };
      }).filter(function (item) { return /^\d{6}$/.test(item.code); }).slice(0, 100) : [];
    } catch (error) { markStorageError('watchlist-read', '自选股读取失败', error); return []; }
  }
  function saveWatchlist(items) { localStorage.setItem(watchlistKey, JSON.stringify(items.slice(0, 100))); }
  function currentStockName() { return String((C.analysis || {}).name || C.code); }
  function toggleWatchlist() {
    var items = watchlistFromStorage(), index = items.findIndex(function (item) { return item.code === C.code; });
    if (index >= 0) items.splice(index, 1);
    else items.unshift({ code:C.code, name:currentStockName() });
    saveWatchlist(items); renderWatchlist();
  }
  function chartNotice(message) { $('terminalChartNote').innerHTML = message; }
  function cloneDrawingState() { return JSON.parse(JSON.stringify({drawings:C.drawings || [],manualLines:C.manualLines || []})); }
  function updateHistoryButtons() {
    $('terminalUndoDrawing').disabled = !C.undoStack.length;
    $('terminalRedoDrawing').disabled = !C.redoStack.length;
  }
  function checkpointDrawings() {
    C.undoStack.push(cloneDrawingState());
    if (C.undoStack.length > 50) C.undoStack.shift();
    C.redoStack = [];
    updateHistoryButtons();
  }
  function restoreDrawingHistory(source, target) {
    if (!source.length) return;
    target.push(cloneDrawingState());
    var state = source.pop(); C.drawings = state.drawings || []; C.manualLines = state.manualLines || []; saveDrawings(); saveLines();
    C.drawPending = []; C.drawTool = null; updateToolState(); updateHistoryButtons();
    renderManualLines(); renderDrawingList(); renderEvidence(); renderChart();
  }
  function unknownMarketSession(reason) {
    return {status:'unknown', label:'交易状态未知', is_open:null, reason:reason || '服务端未提供审计交易时段', server_time:null};
  }
  function marketSessionPhase() {
    var session = C.marketSession || unknownMarketSession();
    return session.status === 'break' ? 'lunch' : (['open','preopen','auction','closed'].indexOf(session.status) >= 0 ? session.status : 'unknown');
  }
  function marketSessionDate() {
    return String(((C.marketSession || {}).server_time) || '').slice(0, 10);
  }
  function renderMarketSessionBadge() {
    var session = C.marketSession || unknownMarketSession(C.marketSessionError);
    var known = session.status !== 'unknown' && typeof session.label === 'string';
    var live = known && session.is_open === true;
    var badge = $('terminalLiveStatus');
    badge.className = 'itc-live-badge ' + (live ? 'live' : 'delay');
    if (C.quoteRefreshError) badge.textContent = '行情刷新失败：保留上一份真实快照';
    else if (live) badge.textContent = session.label + ' · 行情刷新：10秒';
    else badge.textContent = (known ? session.label : '交易状态未知') + ' · 展示最近快照';
  }
  function refreshMarketSession() {
    return api('/api/market/status', 6000).then(function (payload) {
      var session = payload && payload.session;
      var knownStatus = session && ['open','break','preopen','auction','closed'].indexOf(session.status) >= 0;
      var complete = knownStatus && typeof session.label === 'string' && typeof session.is_open === 'boolean' && typeof session.server_time === 'string';
      if (!complete) {
        C.marketSession = unknownMarketSession((session || {}).reason);
        C.marketSessionError = (session || {}).reason || '服务端未提供审计交易时段';
      } else {
        C.marketSession = session;
        C.marketSessionError = null;
      }
      renderMarketSessionBadge();
      if (C.analysis) renderTradePlan();
      return C.marketSession;
    }).catch(function (error) {
      C.marketSessionError = error && error.message ? error.message : '交易时段请求失败';
      C.marketSession = unknownMarketSession(C.marketSessionError);
      reportTerminalIssue('market-session', error);
      renderMarketSessionBadge();
      if (C.analysis) renderTradePlan();
      return C.marketSession;
    });
  }
  function scheduleRefresh() {
    clearTimeout(C.refreshTimer);
    if (document.hidden) return;
    var session = C.marketSession || unknownMarketSession();
    var seconds = session.is_open === true ? 10 : (session.status === 'auction' ? 30 : 60);
    renderMarketSessionBadge();
    C.refreshTimer = setTimeout(refreshVisibleData, seconds * 1000);
  }
  function refreshVisibleData() {
    if (C.refreshBusy || document.hidden) { scheduleRefresh(); return; }
    C.refreshBusy = true;
    var token = C.loadToken, quote = api('/api/stock/' + C.code, 6000).then(function (live) {
      if (token !== C.loadToken || !live || !live.quote) return;
      C.quoteRefreshError = null; C.enrichmentError = null;
      C.analysis = live; syncResearchData(live); renderQuote(); renderTradePlan(); renderSignals(); renderBook(); renderEvidence();
    }).catch(function (error) {
      if (token !== C.loadToken) return;
      C.quoteRefreshError = error && error.message ? error.message : '行情刷新失败';
      reportTerminalIssue('quote-refresh', error);
      if (C.analysis) renderQuote();
    });
    var chart = (C.chartType === 'intraday' ? refreshIntraday(false) : refreshCurrentFrame(false)).catch(function (error) {
      if (token !== C.loadToken) return;
      reportTerminalIssue('chart-refresh', error);
      chartNotice('图表刷新失败：' + escapeHtml(error && error.message ? error.message : '未知错误') + '；保留上一份带时点图表。');
    });
    var timing = refreshTimingIfDue(), position = refreshPositionIfDue(false), route = refreshStrategyRouteIfDue(false), session = refreshMarketSession();
    Promise.allSettled([quote, chart, timing, position, route, session]).finally(function () {
      if (token !== C.loadToken) return;
      C.refreshBusy = false; scheduleRefresh();
    });
  }
  function refreshTimingIfDue() {
    if ((C.marketSession || {}).is_open !== true || Date.now() - C.lastTimingRefresh < 60000) return Promise.resolve();
    var token = C.loadToken, code = C.code;
    C.lastTimingRefresh = Date.now();
    return api('/api/timing/analyze?code=' + encodeURIComponent(code) + '&account_access=false', 9000).then(function (timing) {
      if (token !== C.loadToken || code !== C.code) return;
      C.timingError = null;
      C.intel = C.intel || {}; C.intel.timing = timing;
      renderTiming(timing, (C.intel || {}).composite); renderTradePlan();
      if (C.current && C.current.length) { runAiAnalysis(C.current, false); renderChart(); }
    }).catch(function (error) {
      if (token !== C.loadToken || code !== C.code) return;
      C.timingError = error && error.message ? error.message : '时机刷新失败';
      reportTerminalIssue('timing-refresh', error);
      renderTiming(((C.intel || {}).timing) || null, (C.intel || {}).composite);
    });
  }
  function refreshPositionIfDue(force) {
    if (C.accountAccess !== true) {
      C.position = null; C.positionLoaded = false; C.positionError = null; C.portfolioSummary = null;
      return Promise.resolve();
    }
    if (!force && Date.now() - C.lastPositionRefresh < 10000) return Promise.resolve();
    var token = C.loadToken, code = C.code;
    C.lastPositionRefresh = Date.now();
    return api('/api/timing/portfolio', 9000).then(function (payload) {
      if (token !== C.loadToken || code !== C.code) return;
      var rows = Array.isArray(payload && payload.open) ? payload.open : [];
      C.position = rows.find(function (row) { return String(row.code || '') === code; }) || null;
      C.portfolioSummary = payload && payload.summary && typeof payload.summary === 'object' ? payload.summary : null;
      C.positionLoaded = true; C.positionError = null;
      if (C.analysis) { renderTradePlan(); renderScore(); if (C.current && C.current.length) { runAiAnalysis(C.current, false); renderChart(); } }
    }).catch(function (error) {
      if (token !== C.loadToken || code !== C.code) return;
      C.positionLoaded = false; C.positionError = error && error.message ? error.message : '持仓账本请求失败';
      if (C.analysis) renderTradePlan();
    });
  }
  function refreshStrategyRouteIfDue(force) {
    if (C.accountAccess !== true) {
      C.strategyRoute = null; C.strategyRouteError = null;
      return Promise.resolve();
    }
    if (!force && Date.now() - C.lastStrategyRouteRefresh < 60000) return Promise.resolve();
    var token = C.loadToken, code = C.code;
    C.lastStrategyRouteRefresh = Date.now();
    return api('/api/strategy-route/research?view=dashboard', 12000).then(function (payload) {
      if (token !== C.loadToken || code !== C.code) return;
      C.strategyRoute = payload && typeof payload === 'object' ? payload : null;
      C.strategyRouteError = null;
      if (C.analysis) renderTradePlan();
    }).catch(function (error) {
      if (token !== C.loadToken || code !== C.code) return;
      C.strategyRouteError = error && error.message ? error.message : '今日严格计划请求失败';
      reportTerminalIssue('strategy-route-refresh', error);
      if (C.analysis) renderTradePlan();
    });
  }

  // ── 渲染：行情 / 自选 / 关键位 / 价格标记 / 画线列表 ──
  function renderQuote() {
    var quote = (C.analysis || {}).quote || {}, change = quote.change_pct, items = [
      ['最新价', finite(quote.price) ? '¥' + n(quote.price, 2) : '—', colour(change)], ['涨跌幅', pct(change, 2), colour(change)], ['今开', finite(quote.open_price) ? n(quote.open_price, 2) : '—', ''], ['最高 / 最低', finite(quote.high) ? n(quote.high, 2) + ' / ' + n(quote.low, 2) : '—', ''], ['昨收', n(quote.previous_close, 2), ''], ['成交额', finite(quote.amount) ? (quote.amount / 1e8).toFixed(2) + ' 亿' : '—', ''], ['成交量', finite(quote.volume) ? (quote.volume / 1e4).toFixed(0) + ' 万' : '—', '']
    ];
    var meanings = { '最新价':'当前快照最后成交价', '涨跌幅':'相对上一交易日收盘的变化', '今开':'当日第一笔成交价格', '最高 / 最低':'当日已出现的最高与最低价', '昨收':'上一个交易日收盘价', '成交额':'成交金额，不等于资金净流入', '成交量':'已成交的股数，不代表买方更强' };
    $('terminalQuote').innerHTML = items.map(function (item) { return '<div class="itc-quote-cell"><div class="itc-quote-label">' + item[0] + '</div><div class="itc-quote-value ' + item[2] + '">' + item[1] + '</div><div class="itc-quote-meaning">' + meanings[item[0]] + '</div></div>'; }).join('');
    var h = health(C.analysis); $('terminalHealth').className = 'itc-status ' + h.level; $('terminalHealth').innerHTML = '数据健康：<b>' + h.label + '</b>';
    var refreshWarnings = [];
    if (C.quoteRefreshError) refreshWarnings.push('行情刷新失败：' + C.quoteRefreshError + '；继续展示上一份带时点快照。');
    if (C.enrichmentError) refreshWarnings.push('实时补充失败：' + C.enrichmentError + '；未将已审计快照冒充为更新结果。');
    var minuteAnalysis = ((C.analysis || {}).data_status || {}).minute_analysis;
    if (minuteAnalysis && minuteAnalysis.status !== 'available') refreshWarnings.push(minuteAnalysis.message || '分钟分析暂不可用，报价独立显示。');
    $('terminalSourceBoundary').innerHTML = '<strong>' + escapeHtml(h.label) + '</strong><br>' + escapeHtml(h.detail) + (refreshWarnings.length ? '<br><br><span class="warn">' + escapeHtml(refreshWarnings.join(' ')) + '</span>' : '') + '<br><br>页面不会把集合竞价或休市快照称为连续竞价实时数据；当前图表以各接口的 as-of 时间为准。';
    renderMarketSessionBadge();
  }
  function renderWatchlist() {
    var items = watchlistFromStorage(), added = items.some(function (item) { return item.code === C.code; }), toggle = $('terminalWatchToggle');
    toggle.textContent = added ? '★ 已加入自选（点击移除）' : '＋ 加入自选';
    toggle.setAttribute('aria-pressed', added ? 'true' : 'false');
    $('terminalWatchlist').innerHTML = items.length ? items.map(function (item) { return '<div class="itc-watch"><button type="button" data-watch-code="' + escapeAttr(item.code) + '"><strong>' + escapeHtml(item.name || item.code) + '</strong><br><small>' + escapeHtml(item.code) + '</small></button><span>' + (item.code === C.code ? '当前' : '查看详情') + '</span></div>'; }).join('') : '<div class="itc-empty">还没有自选股。载入个股后，点击“加入自选”。</div>';
    $('terminalWatchlist').querySelectorAll('[data-watch-code]').forEach(function (button) { button.addEventListener('click', function () { load(button.dataset.watchCode); }); });
  }
  function renderLevels() {
    var focus = C.aiFocus || null, result = [];
    if (focus) {
      var displayLevel = function (item) {
        if (!item) return '—';
        if (item.range && finite(item.range.low) && finite(item.range.high)) return n(item.range.low,2) + '–' + n(item.range.high,2);
        if (finite(item.projectedPrice)) return n(item.projectedPrice,2);
        return finite(item.price) ? n(item.price,2) : '—';
      };
      if (focus.nearestResistance) result.push('<div class="itc-line"><span class="itc-resistance">当前上方观察 · ' + displayLevel(focus.nearestResistance) + '</span><span>距图表基准 ' + n(focus.nearestResistance.distanceAtr,2) + ' ATR</span></div>');
      if (focus.nearestSupport) result.push('<div class="itc-line"><span class="itc-support">当前下方观察 · ' + displayLevel(focus.nearestSupport) + '</span><span>距图表基准 ' + n(focus.nearestSupport.distanceAtr,2) + ' ATR</span></div>');
      else result.push('<div class="itc-note" style="color:var(--itc-warn)">暂无可靠下方支撑；不会用陈旧历史低点填空。</div>');
      if (focus.primary) result.push('<div class="itc-note" style="margin-top:8px">' + escapeHtml(focus.primary) + '</div>');
      $('terminalLevels').innerHTML = result.join('');
      return;
    }
    var L = C.autoLevels || { supports:[], resistances:[], fib:[] };
    L.resistances.forEach(function (item) { result.push('<div class="itc-line"><span class="itc-resistance">可能遇阻 · ' + n(item.price,2) + '</span><span>历史 ' + item.count + ' 次附近回落</span></div>'); });
    L.supports.forEach(function (item) { result.push('<div class="itc-line"><span class="itc-support">可能止跌 · ' + n(item.price,2) + '</span><span>历史 ' + item.count + ' 次附近企稳</span></div>'); });
    if (L.swing) result.push('<div class="itc-note" style="margin-top:8px">这些关键价格来自过去一段时间反复出现的高低价；最近大致在 ' + n(L.swing.low,2) + ' 到 ' + n(L.swing.high,2) + ' 之间波动。不是保证，只是帮助你盯盘的参考位置。</div>');
    $('terminalLevels').innerHTML = result.length ? result.join('') : '<div class="itc-empty">数据不足，不能计算关键位。</div>';
  }
  function renderManualLines() {
    $('terminalLineList').innerHTML = C.manualLines.length ? C.manualLines.map(function (line, index) { return '<div class="itc-line"><span class="' + (line.kind === 'support' ? 'itc-support' : 'itc-resistance') + '">' + (line.kind === 'support' ? '可能止跌价' : '可能遇阻价') + ' ' + n(line.price,2) + '</span><button type="button" aria-label="删除标记" data-delete-line="' + index + '">删除</button></div>'; }).join('') : '<div class="itc-empty">还没有自己的价格标记。</div>';
    $('terminalLineList').querySelectorAll('[data-delete-line]').forEach(function (button) { button.addEventListener('click', function () { checkpointDrawings(); C.manualLines.splice(Number(button.dataset.deleteLine), 1); saveLines(); renderManualLines(); renderEvidence(); renderChart(); }); });
  }
  function renderDrawingList() {
    var list = $('terminalDrawingList');
    list.innerHTML = C.drawings.length ? C.drawings.map(function (d, index) {
      return '<div class="itc-drawing-row"><div class="itc-line"><label><input type="checkbox" data-visible-drawing="' + index + '"' + (d.visible !== false ? ' checked' : '') + '> 显示</label><button type="button" aria-label="删除画线" data-delete-drawing="' + index + '">删除</button></div><input class="itc-drawing-input" data-name-drawing="' + index + '" value="' + escapeAttr(d.name || TOOL_NAMES[d.type] || d.type) + '" aria-label="画线名称"><textarea class="itc-drawing-note" data-note-drawing="' + index + '" rows="2" placeholder="复盘备注（仅本机）" aria-label="画线复盘备注">' + escapeHtml(d.note || '') + '</textarea><small>' + escapeHtml(TOOL_NAMES[d.type] || d.type) + ' · ' + (d.points || []).length + ' 点 · ' + escapeHtml(localCreatedAt(d.createdAt)) + '</small></div>';
    }).join('') : '<div class="itc-empty">还没有画线（水平/趋势/斐波那契/通道）。</div>';
    list.querySelectorAll('[data-delete-drawing]').forEach(function (button) { button.addEventListener('click', function () { checkpointDrawings(); C.drawings.splice(Number(button.dataset.deleteDrawing), 1); saveDrawings(); renderDrawingList(); renderEvidence(); renderChart(); }); });
    list.querySelectorAll('[data-visible-drawing]').forEach(function (input) { input.addEventListener('change', function () { checkpointDrawings(); C.drawings[Number(input.dataset.visibleDrawing)].visible = input.checked; saveDrawings(); renderEvidence(); renderChart(); }); });
    list.querySelectorAll('[data-name-drawing]').forEach(function (input) { input.addEventListener('change', function () { checkpointDrawings(); var item = C.drawings[Number(input.dataset.nameDrawing)]; item.name = input.value.trim() || TOOL_NAMES[item.type]; saveDrawings(); renderDrawingList(); renderEvidence(); }); });
    list.querySelectorAll('[data-note-drawing]').forEach(function (input) { input.addEventListener('change', function () { checkpointDrawings(); C.drawings[Number(input.dataset.noteDrawing)].note = input.value.trim(); saveDrawings(); }); });
  }
  function renderAiDiagnostics() {
    var box = $('terminalAiDiagnostics'), status = $('terminalAiStatus'), headline = $('terminalAiHeadline'), scope = $('terminalAiScope'), diagnostics = C.aiDiagnostics || [], meta = C.aiMeta || {}, focus = C.aiFocus || {}, map = C.decisionMap || {};
    if (!box || !status) return;
    if ((C.snapshotState === 'error' || C.chartState === 'error' || C.chartState === 'empty') && !C.aiAnalyzedAt) {
      status.textContent = '未执行';
      if (headline) headline.textContent = '当前周期无可用K线，结构分析未执行';
      if (scope) scope.textContent = '重新查询当前股票可重试。';
      box.textContent = '本次没有可分析的当前K线。'; return;
    }
    if (C.aiError) {
      status.textContent = '分析失败';
      if (headline) headline.textContent = '当前结构分析失败，图上不保留旧股票的候选线';
      if (scope) scope.textContent = C.aiError;
      box.innerHTML = '<span class="warn">' + escapeHtml(C.aiError) + '</span>';
      return;
    }
    if (!C.aiAnalyzedAt && !(Array.isArray(diagnostics) ? diagnostics.length : Object.keys(diagnostics).length)) {
      status.textContent = '等待K线';
      if (headline) headline.textContent = '载入真实K线后识别当前最值得观察的结构';
      if (scope) scope.textContent = '不预测涨跌，不自动给出买卖指令';
      box.textContent = '载入K线后给出样本、波动率、候选数量与失效检查。'; return;
    }
    var diagnosticItems = Array.isArray(diagnostics) ? diagnostics : [diagnostics];
    var lead = diagnosticItems.filter(function (item) { return item && item.level === 'error'; })[0] || diagnosticItems.filter(function (item) { return item && item.level === 'warning'; })[0] || diagnosticItems[0] || {};
    var sample = meta.usedBars || meta.inputBars || lead.sample_size || lead.sampleSize;
    var atrValue = meta.atr || lead.atr || lead.atr_value;
    var pivots = meta.pivotCount || lead.pivot_count || lead.pivots;
    var regime = meta.regime || lead.regime;
    var reason = lead.message || lead.reason || '';
    var engine = meta.engine || meta.version || 'Regis AI';
    var adjustLabel = {qfq:'前复权',raw:'不复权',unknown:'价格口径待核验'}[meta.adjustType || chartAdjustType()] || '价格口径待核验';
    status.textContent = map.state === 'candidate_ready' ? '候选条件已齐' : map.state === 'position_management' ? '持仓管理' : map.state === 'risk_review' ? '风险复核' : (lead.level === 'error' ? '数据不足' : '等待确认');
    if (headline) headline.textContent = map.headline || focus.primary || (C.autoDrawings.length ? '只展示与图表基准收盘接近、近期仍有效的结构。' : '当前没有达到距离、时效和证据门槛的结构。');
    if (scope) scope.textContent = (LABELS[C.timeframe] || chartScope()) + ' · ' + adjustLabel + (focus.asOf ? ' · 截至 ' + String(focus.asOf) : '') + (finite(focus.currentPrice) ? ' · 图表基准收盘 ' + n(focus.currentPrice, 2) : '') + (finite(focus.atr) ? ' · ATR ' + n(focus.atr, 3) : '');
    box.innerHTML = '<b>' + escapeHtml(engine) + '</b> · ' + escapeHtml(LABELS[C.timeframe] || chartScope()) + ' · ' + adjustLabel +
      (finite(sample) ? ' · 样本 ' + escapeHtml(String(sample)) : '') +
      (finite(pivots) ? ' · 拐点 ' + escapeHtml(String(pivots)) : '') +
      (finite(atrValue) ? ' · ATR ' + n(atrValue, 3) : '') +
      (regime ? ' · 结构 ' + escapeHtml(String(regime)) : '') +
      (C.aiAnalyzedAt ? '<br><span class="ok">已于 ' + escapeHtml(localCreatedAt(C.aiAnalyzedAt)) + ' 完成本地分析</span>' : '') +
      (reason ? '<br><span class="warn">' + escapeHtml(String(reason)) + '</span>' : '');
  }
  function runAiAnalysis(candles, rerender) {
    candles = candles || (C.chartType === 'intraday' ? C.intraday : C.current) || [];
    var button = $('terminalAiRun');
    var panel = root.querySelector('.itc-ai-panel');
    if (button) button.disabled = true;
    if (panel) panel.setAttribute('aria-busy', 'true');
    C.aiError = null;
    if (!candles.length) {
      C.autoDrawings = []; C.aiFocus = null; C.aiScenarios = []; C.aiMarketContext = null; C.decisionMap = null; C.focusedAutoId = null; C.focusedScenarioRole = null; C.aiDiagnostics = {status:'insufficient_data', reason:'当前周期没有可用 OHLCV。'}; C.aiMeta = {engine:'Regis AI'}; C.aiAnalyzedAt = null;
      C.aiViewSeeded = false;
    } else {
      var auto = deriveAutoDrawings(candles);
      C.autoLevels = auto.levels; C.autoDrawings = auto.items.slice(0, 4); C.aiFocus = auto.focus || null; C.aiScenarios = auto.scenarios || []; C.aiMarketContext = auto.marketContext || null; C.decisionMap = auto.decisionMap || null; C.focusedAutoId = null; C.focusedScenarioRole = null; C.aiDiagnostics = auto.diagnostics || {}; C.aiMeta = auto.meta || {}; C.aiAnalyzedAt = new Date().toISOString();
      if (C.decisionMap && !C.aiViewSeeded) { C.fitOnRender = true; C.aiViewSeeded = true; }
      C.autoDrawings.forEach(function (item, index) {
        C.autoVisibility[item.id] = !item.invalidated && index === 0;
      });
      saveAutoVisibility();
    }
    renderAiDiagnostics(); renderAutoDrawingList();
    if (button) button.disabled = false;
    if (panel) panel.setAttribute('aria-busy', 'false');
    if (rerender) { renderEvidence(); renderChart(); }
  }
  function adoptAutoDrawing(id) {
    var item = C.autoDrawings.find(function (candidate) { return candidate.id === id; });
    if (!item || item.invalidated) return;
    if (C.drawings.some(function (drawing) { return drawing.sourceAutoId === item.id; })) { $('terminalAiStatus').textContent = '该候选已采纳'; return; }
    var candles = C.chartType === 'intraday' ? C.intraday : C.current, first = candles[0], last = candles[candles.length - 1], type, points, price;
    if (item.type === 'zone' && first && last && finite(item.low) && finite(item.high)) {
      type = 'rectangle'; points = [{time:timeValue(first.time),price:item.low},{time:timeValue(last.time),price:item.high}];
    } else if (item.type === 'fib' && item.points.length >= 2) {
      type = 'fib'; points = item.points.slice(0, 2);
    } else if (item.type === 'trend' && item.points.length >= 2) {
      type = 'trend'; points = item.points.slice(0, 2);
    } else if (item.type === 'pattern_123' && item.points.length >= 3) {
      type = 'trend'; points = [item.points[0], item.points[2]];
    } else if (item.type === 'channel' && item.points.length >= 3) {
      /* 自动通道为 [基线起点, 平行线证据点, 基线当前投影]；个人画线渲染器需要 [基线起点, 基线终点, 平行线证据点]。 */
      type = 'channel'; points = [item.points[0], item.points[2], item.points[1]];
    } else {
      price = finite(item.price) ? item.price : (finite(item.low) && finite(item.high) ? (item.low + item.high) / 2 : null);
      if (!finite(price)) return;
      type = 'horizontal'; points = [{price:Number(price)}];
    }
    checkpointDrawings();
    var adoptedAt = new Date().toISOString(), analysisAsOf = last ? String(last.time) : null;
    C.drawings.push({ id:'d-ai-' + Date.now().toString(36), type:type, name:'AI采纳 · ' + item.label, points:points, visible:true, note:item.explanation, createdAt:adoptedAt, sourceAutoId:item.id, aiScore:item.strength, aiEvidence:item.evidence || item.explanation, origin:'local-ai', engineVersion:(C.aiMeta || {}).engine || 'Regis AI', analysisAsOf:analysisAsOf, adjustType:chartAdjustType() });
    appendDecisionRecord({kind:'ai_drawing_adopted', code:C.code, timeframe:C.timeframe, adjustType:chartAdjustType(), sourceId:item.id, label:item.label, score:item.strength, analysisAsOf:analysisAsOf, fingerprint:['ai',C.code,chartScope(),item.id,adoptedAt].join(':')});
    saveDrawings(); renderDrawingList(); renderEvidence(); renderChart();
    $('terminalAiStatus').textContent = '已采纳到个人画线';
  }
  function focusAutoDrawing(id) {
    var selected = C.autoDrawings.find(function (item) { return item.id === id; });
    if (!selected) return;
    var returning = C.focusedAutoId === id;
    C.focusedAutoId = returning ? null : id;
    C.autoDrawings.forEach(function (item) { C.autoVisibility[item.id] = returning || item.id === id; });
    C.overlays.autoDrawings = true;
    root.querySelectorAll('[data-overlay="autoDrawings"]').forEach(function (input) { input.checked = true; });
    renderAutoDrawingList(); renderChart();
    var candles = C.chartType === 'intraday' ? (C.intraday || []) : (C.current || []);
    if (!C.chart || !candles.length) return;
    var start = Math.max(0, candles.length - (returning ? 80 : 60));
    if (!returning && selected.points && selected.points.length) {
      var evidenceIndexes = selected.points.map(function (point) { return candles.findIndex(function (row) { return timeKey(row.time) === timeKey(point.time); }); }).filter(function (index) { return index >= 0; });
      if (evidenceIndexes.length) {
        var earliest = Math.min.apply(null, evidenceIndexes);
        if (earliest >= candles.length - 120) start = Math.max(0, Math.min(start, earliest - 8));
      }
    }
    C.chart.timeScale().setVisibleLogicalRange({from:start,to:candles.length + 5});
  }
  function aiScenarioFor(item) {
    var sourceId = String(item.id || '').replace(/^regis-ai:/, '');
    return (C.aiScenarios || []).find(function (scenario) { return scenario && (scenario.relatedDrawingId === item.id || scenario.relatedDrawingId === sourceId); }) || null;
  }
  function aiRuleText(rule, fallback, phase) {
    rule = rule || {};
    var price = finite(rule.price) ? n(rule.price, 2) : '该结构位';
    var labels = {
      two_closes_above:'连续 2 根完成K线收盘站上 ' + price,
      two_closes_below:'连续 2 根完成K线收盘跌破 ' + price,
      closes_back_above_zone:'完成K线收盘重新站上 ' + price,
      holds_above:'完成K线收盘保持在 ' + price + ' 上方',
      close_back_below:'完成K线收盘重新跌回 ' + price + ' 下方',
      close_below_latest_low:'完成K线收盘低于 ' + price
    };
    return labels[rule.rule] || fallback || (phase === 'confirm' ? '等待完成K线再次确认该结构' : '完成K线破坏该结构时停止采用');
  }
  function aiCardState(item) {
    if (item.role === 'breakdown_reclaim') return {key:'triggered', label:'已破位 · 等待收回'};
    if (item.role === 'breakout_retest') return {key:'triggered', label:'已突破 · 等待回踩'};
    if (finite(item.distanceAtr) && item.distanceAtr <= .15) return {key:'testing', label:'正在测试'};
    if (finite(item.distanceAtr) && item.distanceAtr <= 1) return {key:'approaching', label:'接近图表基准'};
    return {key:'watching', label:'观察中'};
  }
  function aiCardPrice(item) {
    if (item.type === 'fib' && finite(item.zoneLow) && finite(item.zoneHigh)) return n(item.zoneLow, 2) + '–' + n(item.zoneHigh, 2);
    if (finite(item.low) && finite(item.high)) return n(item.low, 2) + '–' + n(item.high, 2);
    if (finite(item.projectedPrice)) return n(item.projectedPrice, 2);
    return finite(item.price) ? n(item.price, 2) : '—';
  }
  function decisionScenario(role) {
    return (((C.decisionMap || {}).scenarios) || []).find(function (item) { return item && item.role === role; }) || null;
  }
  function scenarioStatus(item) {
    if (!item) return {label:'等待计算', key:'unavailable'};
    return {
      active:{label:'条件已齐',key:'active'},
      locked:{label:'门禁未过',key:'locked'},
      observation:{label:'研究观察',key:'observation'},
      unavailable:{label:'数据不足',key:'unavailable'}
    }[item.availability] || {label:String(item.availability || '待确认'),key:'unavailable'};
  }
  function renderBattleMap() {
    var map = C.decisionMap || {}, presentation = map.presentation || {}, state = $('terminalBattleState'), scope = $('terminalBattleScope'), bar = $('terminalBattleBar');
    var unavailable = C.snapshotState === 'error' || C.chartState === 'error' || C.chartState === 'empty';
    if (state) state.textContent = map.headline || (unavailable ? '当前周期无可用K线，场景未计算' : '等待真实K线与量化门禁');
    if (scope) scope.textContent = (LABELS[C.timeframe] || chartScope()) + ' · ' + chartPriceLabel() + (map.asOf ? ' · 截至 ' + String(map.asOf) : '') + (map.barStatus ? ' · ' + String(map.barStatus) : '');
    if (bar) bar.setAttribute('aria-busy', map.methodVersion || unavailable ? 'false' : 'true');
    var visibleRoles = C.focusedScenarioRole ? [C.focusedScenarioRole] : (Array.isArray(presentation.visibleRoles) ? presentation.visibleRoles : ['entry','target','risk']);
    if (bar) bar.dataset.visibleCount = String(visibleRoles.length || 1);
    [['entry','terminalBattleEntryPrice','terminalBattleEntryMeta'],['target','terminalBattleTargetPrice','terminalBattleTargetMeta'],['risk','terminalBattleRiskPrice','terminalBattleRiskMeta']].forEach(function (spec) {
      var item = decisionScenario(spec[0]), button = bar && bar.querySelector('[data-focus-scenario="' + spec[0] + '"]'), price = $(spec[1]), condition = $(spec[2]);
      if (price) price.textContent = item ? item.priceText : '—';
      if (condition) condition.textContent = item ? item.condition : (unavailable ? '当前数据不可用，未计算' : '等待本地计算');
      if (!button) return;
      button.hidden = visibleRoles.indexOf(spec[0]) < 0;
      var label = button.querySelector('span');
      if (label) label.textContent = item ? item.label : {entry:'候选入场区',target:'持仓止盈观察',risk:'计划失效边界'}[spec[0]];
      button.disabled = !item || !item.geometry;
      button.classList.toggle('is-active', C.focusedScenarioRole === spec[0]);
      button.setAttribute('aria-pressed', C.focusedScenarioRole === spec[0] ? 'true' : 'false');
    });
  }
  function renderBattleMetrics() {
    var box = $('terminalBattleMetrics'), protocolBox = $('terminalStructureProtocol'), analysis = $('terminalAiAnalysis'), flags = $('terminalAiAnalysisFlags'), map = C.decisionMap || {}, presentation = map.presentation || {}, volatility = map.volatility || {}, atrData = volatility.atr || {}, metrics = map.metrics || {}, trend = map.trend || {}, multi = map.multiTimeframe || {}, marketContext = map.marketContext || C.aiMarketContext || {}, protocol = map.structureProtocol || {};
    if ((C.snapshotState === 'error' || C.chartState === 'error' || C.chartState === 'empty') && !map.methodVersion) {
      [box,protocolBox,analysis,flags].forEach(function (el) { if (el) el.textContent = '当前周期无可用K线，本次结构分析未执行。'; }); return;
    }
    if (box) box.innerHTML = map.methodVersion ? [
      ['图表基准收盘', finite(map.currentPrice) ? n(map.currentPrice,2) : '—'],
      ['ATR14', finite(atrData.value) ? n(atrData.value,3) + ' · ' + n(atrData.pct,2) + '%' : '—'],
      ['主趋势', trend.label || '未成立'],
      ['盈亏比', finite(metrics.rewardRisk) ? '1 : ' + n(metrics.rewardRisk,2) : '—']
    ].map(function (item) { return '<div><dt>' + escapeHtml(item[0]) + '</dt><dd>' + escapeHtml(item[1]) + '</dd></div>'; }).join('') : '<div><dt>计算状态</dt><dd>等待真实K线</dd></div>';
    if (protocolBox) protocolBox.innerHTML = Array.isArray(protocol.steps) && protocol.steps.length ? protocol.steps.map(function (step, index) {
      return '<li class="' + escapeAttr(step.state || 'waiting') + '"><span>' + (index + 1) + '</span><div><b>' + escapeHtml(step.label) + '</b><strong>' + escapeHtml(step.summary) + '</strong><small>' + escapeHtml(step.evidence) + '</small></div></li>';
    }).join('') : '<li class="waiting"><span>1</span><div><b>等待</b><strong>载入完成K线后计算</strong><small>不使用形成中K线</small></div></li>';
    var primary = decisionScenario(presentation.primaryRole), risk = decisionScenario('risk'), notes = [];
    [marketContext.summary, primary && primary.reason, primary && primary.condition, risk && risk.role !== (primary || {}).role ? risk.reason : trend.reason, multi.summary].forEach(function (note) { if (note && notes.indexOf(note) < 0) notes.push(note); });
    if (analysis) analysis.textContent = map.methodVersion ? ((map.headline || '结构决策') + '。价格坐标由确定性量化计算；AI只选择、排序并解释已有候选，不自由生成价格。证据等级：' + (map.evidenceGrade || '未声明') + '。') : '等待结构决策计算。';
    if (flags) flags.innerHTML = notes.map(function (note) { return '<span role="listitem">' + escapeHtml(note) + '</span>'; }).join('');
  }
  function focusDecisionScenario(role) {
    var item = decisionScenario(role), returning = C.focusedScenarioRole === role;
    if (!item || !item.geometry) return;
    C.focusedScenarioRole = returning ? null : role;
    renderAutoDrawingList(); renderChart();
    var candles = C.chartType === 'intraday' ? (C.intraday || []) : (C.current || []), overviewBars = Number((((C.decisionMap || {}).presentation || {}).chartLookbackBars || 60)), bars = returning ? overviewBars : Number((item.focus || {}).visibleBars || overviewBars);
    if (C.chart && candles.length) C.chart.timeScale().setVisibleLogicalRange({from:Math.max(0, candles.length - bars),to:candles.length + 5});
  }
  function renderStructureList() {
    var list = $('terminalStructureEvidenceBody'), statusBox = $('terminalStructureEvidenceStatus');
    if (!list) return;
    if (statusBox) statusBox.textContent = C.autoDrawings.length ? C.autoDrawings.length + ' 个近期结构' : '无可靠结构';
    list.innerHTML = C.autoDrawings.length ? C.autoDrawings.map(function (item) {
      var scenario = aiScenarioFor(item), state = aiCardState(item), confidence = {high:'证据高',medium:'证据中',low:'证据低'}[item.confidence] || '待复核';
      var observe = scenario && scenario.observe && scenario.observe.note ? scenario.observe.note : item.explanation;
      var confirm = aiRuleText(scenario && scenario.confirm, '', 'confirm');
      var invalid = aiRuleText(scenario && scenario.invalidate, finite((item.evidence || {}).invalidationPrice) ? '连续 2 根完成K线越过 ' + n(item.evidence.invalidationPrice, 2) + ' 后结构失效' : '', 'invalidate');
      var evidence = [];
      if (finite(item.touches) && item.touches > 0) evidence.push(item.touches + ' 次结构触达');
      if (finite(item.lastTouchBars)) evidence.push(item.lastTouchBars === 0 ? '最新K线触达' : '最近触达在 ' + item.lastTouchBars + ' 根K线前');
      if (finite(item.strength)) evidence.push('证据分 ' + item.strength + '（非胜率）');
      var focused = C.focusedAutoId === item.id;
      return '<article class="itc-auto-row ' + escapeAttr(item.role || item.kind) + '"><div class="itc-auto-main"><div class="itc-auto-title"><b>' + escapeHtml(item.label) + '</b><div class="itc-auto-meta"><span class="itc-auto-state ' + state.key + '">' + state.label + '</span><span class="itc-auto-confidence ' + escapeAttr(item.confidence) + '">' + confidence + '</span>' + (finite(item.distancePct) && finite(item.distanceAtr) ? '<span class="itc-auto-distance">距离 ' + n(item.distancePct, 2) + '% · ' + n(item.distanceAtr, 2) + ' ATR</span>' : '') + '</div></div><div class="itc-auto-price">' + aiCardPrice(item) + '<small>当前观察位</small></div></div><p class="itc-auto-why">' + escapeHtml(scenario && scenario.rationale ? scenario.rationale : item.explanation) + '</p><div class="itc-auto-conditions"><div class="itc-auto-condition"><b>观察</b><span>' + escapeHtml(observe) + '</span></div><div class="itc-auto-condition confirm"><b>确认</b><span>' + escapeHtml(confirm) + '</span></div><div class="itc-auto-condition invalid"><b>失效</b><span>' + escapeHtml(invalid) + '</span></div><div class="itc-auto-condition"><b>依据</b><span>' + escapeHtml(evidence.join(' · ') || '仅保留当前距离与近期性已达标的结构') + '</span></div></div><div class="itc-auto-actions"><button class="itc-btn" type="button" data-focus-auto="' + escapeAttr(item.id) + '">' + (focused ? '返回全部结构' : '聚焦图表') + '</button><button class="itc-btn primary" type="button" data-adopt-auto="' + escapeAttr(item.id) + '">保存到我的画线</button></div></article>';
    }).join('') : '<div class="itc-auto-empty"><b>暂无可靠当前结构</b><br>' + escapeHtml((C.aiFocus || {}).primary || '当前没有同时通过图表基准距离、近期性和证据门槛的结构；系统不会为了有结果而强行画线。') + '</div>';
    list.querySelectorAll('[data-focus-auto]').forEach(function (button) { button.addEventListener('click', function () { focusAutoDrawing(button.dataset.focusAuto); }); });
    list.querySelectorAll('[data-adopt-auto]').forEach(function (button) { button.addEventListener('click', function () { adoptAutoDrawing(button.dataset.adoptAuto); }); });
  }
  function renderAutoDrawingList() {
    var list = $('terminalAutoDrawingList'), map = C.decisionMap || {}, presentation = map.presentation || {}, items = Array.isArray(map.scenarios) ? map.scenarios : [];
    renderBattleMap(); renderBattleMetrics(); renderStructureList();
    if (!list) return;
    if (!items.length) {
      list.innerHTML = '<div class="itc-auto-empty"><b>AI 辅助划线等待计算</b><br>当前周期没有足够的同口径完成K线，系统不会用旧股票或其他复权口径补空。</div>';
      return;
    }
    var roleLabel = {entry:'候选买点',target:'兑现观察',risk:'止损 / 失效'};
    var primaryRole = C.focusedScenarioRole || presentation.primaryRole || (items[0] || {}).role;
    var primary = items.find(function (item) { return item.role === primaryRole; }) || items[0];
    var primaryStatus = scenarioStatus(primary), primaryFocused = C.focusedScenarioRole === primary.role;
    var deferredRoles = Array.isArray(presentation.deferredRoles) ? presentation.deferredRoles : [];
    var secondary = items.filter(function (item) { return item !== primary; });
    list.innerHTML = '<article class="itc-auto-row itc-scenario-card is-primary ' + escapeAttr(primary.role) + ' ' + escapeAttr(primaryStatus.key) + '"><div class="itc-auto-main"><div class="itc-auto-title"><span class="itc-kicker">PRIMARY DECISION · ' + escapeHtml(roleLabel[primary.role] || primary.role) + '</span><b>' + escapeHtml(primary.label) + '</b><div class="itc-auto-meta"><span class="itc-auto-state ' + escapeAttr(primaryStatus.key) + '">' + escapeHtml(primaryStatus.label) + '</span><span class="itc-auto-confidence">确定性坐标 · AI解释</span></div></div><div class="itc-auto-price">' + escapeHtml(primary.priceText || '—') + '<small>' + (primary.geometry ? '当前同口径坐标' : '不生成坐标') + '</small></div></div><p class="itc-auto-why">' + escapeHtml(primary.reason || '未提供依据') + '</p><div class="itc-auto-conditions"><div class="itc-auto-condition confirm"><b>成立条件</b><span>' + escapeHtml(primary.condition || '—') + '</span></div><div class="itc-auto-condition invalid"><b>失效 / 边界</b><span>' + escapeHtml(primary.invalidation && finite(primary.invalidation.price) ? (primary.invalidation.basis || '完成K线') + ' · ' + n(primary.invalidation.price,2) : primary.role === 'risk' ? primary.condition : '未建立独立失效线') + '</span></div><div class="itc-auto-condition"><b>坐标来源</b><span>' + escapeHtml(primary.source || 'current_chart_ohlcv') + '</span></div></div><div class="itc-auto-actions"><button class="itc-btn" type="button" data-focus-scenario="' + escapeAttr(primary.role) + '"' + (primary.geometry ? '' : ' disabled') + '>' + (primaryFocused ? '返回简洁视图' : '聚焦主图') + '</button></div></article>' +
      '<div class="itc-scenario-secondary" aria-label="其他场景">' + secondary.map(function (item) {
        var status = scenarioStatus(item), focused = C.focusedScenarioRole === item.role, deferred = deferredRoles.indexOf(item.role) >= 0;
        return '<button class="itc-scenario-summary ' + escapeAttr(item.role) + '" type="button" data-focus-scenario="' + escapeAttr(item.role) + '"' + (item.geometry ? '' : ' disabled') + ' aria-pressed="' + (focused ? 'true' : 'false') + '"><span><small>' + escapeHtml(roleLabel[item.role] || item.role) + ' · ' + (deferred ? '按需显示' : '图上可见') + '</small><b>' + escapeHtml(item.label) + '</b></span><strong>' + escapeHtml(item.priceText || '—') + '</strong><em>' + escapeHtml(status.label) + '</em></button>';
      }).join('') + '</div>';
    list.querySelectorAll('[data-focus-scenario]').forEach(function (button) { button.addEventListener('click', function () { focusDecisionScenario(button.dataset.focusScenario); }); });
  }

  // ── 渲染：技术 / 信号 / 盘口 / 预测 / 基本面 ──
  function renderTechnical() {
    // 普通人看图助手只解释近期涨跌、价格位置与成交活跃度，不把指标缩写当作结论。
    var candles = C.chartType === 'intraday' ? (C.intraday || []) : (C.current || []), local = C.indicatorData || {}, lastIndex = Math.max(0, candles.length - 1), ma5 = local.ma5 && local.ma5[lastIndex], ma20 = local.ma20 && local.ma20[lastIndex], macd = local.macd && local.macd[lastIndex], rsi = local.rsi && local.rsi[lastIndex], close = candles[lastIndex] && Number(candles[lastIndex].close), top = local.bollUpper && local.bollUpper[lastIndex], bottom = local.bollLower && local.bollLower[lastIndex], bollPosition = finite(top) && finite(bottom) && Number(top) !== Number(bottom) ? (close - bottom) / (top - bottom) : null, volumeRatio = candles.length >= 20 ? Number(candles[lastIndex].volume || 0) / mean(candles.slice(-20).map(function (row) { return Number(row.volume || 0); })) : null, trend = finite(ma5) && finite(ma20) ? (ma5 > ma20 ? 82 : 28) : null, momentum = finite(macd) ? (macd > 0 ? 74 : 34) : null, heat = finite(rsi) ? (rsi >= 30 && rsi <= 70 ? 70 : 48) : null, position = finite(bollPosition) ? clamp(70 - Math.abs(bollPosition - .5) * 50, 30, 75) : null, activity = finite(volumeRatio) ? clamp(50 + (volumeRatio - 1) * 25, 25, 85) : null, items = [
      ['近期趋势', trend, trend == null ? '数据不足' : trend >= 50 ? '近期价格整体偏强' : '近期价格整体偏弱'],
      ['涨跌力量', momentum, momentum == null ? '数据不足' : momentum >= 50 ? '最近买方力量暂时占优' : '最近卖方力量暂时占优'],
      ['短期冷热', heat, heat == null ? '数据不足' : rsi > 70 ? '短期涨得偏快，注意追高风险' : rsi < 30 ? '短期跌得偏快，波动风险较高' : '短期涨跌速度较平稳'],
      ['价格位置', position, position == null ? '数据不足' : bollPosition > .8 ? '接近近期高位区域' : bollPosition < .2 ? '接近近期低位区域' : '位于近期价格区间中部'],
      ['成交活跃度', activity, activity == null ? '数据不足' : volumeRatio >= 1.2 ? '成交比近期平均更活跃' : '成交活跃度一般']
    ];
    var observations = ['trend', 'momentum', 'heat', 'position', 'activity'];
    $('terminalTechnicals').innerHTML = items.map(function (item, index) { var width = item[1] == null ? 0 : item[1], key = observations[index]; return '<button type="button" class="itc-bar-row itc-observation ' + (C.observationFocus === key ? 'is-active' : '') + '" data-observation="' + key + '" title="在图表中查看对应指标"><span>' + item[0] + '</span><span class="itc-bar-track"><span class="itc-bar-fill ' + (item[1] != null && item[1] < 50 ? 'bad' : '') + '" style="width:' + width + '%"></span></span><span class="itc-bar-value">' + escapeHtml(item[2]) + '<small>图上查看</small></span></button>'; }).join('');
    $('terminalTechnicals').querySelectorAll('[data-observation]').forEach(function (button) { button.addEventListener('click', function () { focusObservation(button.dataset.observation); }); });
  }
  function completedDailyCandles() {
    var phase = marketSessionPhase();
    if (!window.StockExecutionPlan) return [];
    return window.StockExecutionPlan.completedCandles(C.planDaily || [], phase, marketSessionDate());
  }
  function buildTradePlan() {
    if (!window.StockExecutionPlan) return { available:false, reason:'买卖点规则运行时未加载。' };
    var phase = marketSessionPhase();
    return window.StockExecutionPlan.build({
      daily:C.daily || [],
      rawDaily:C.planDaily || [],
      executionPlan:C.planMeta || {},
      timing:(C.intel || {}).timing || {},
      assessment:(C.intel || {}).composite || null,
      quote:(C.analysis || {}).quote || {},
      marketPhase:phase,
      currentDate:marketSessionDate()
    });
  }
  function buildPositionSellPlan() {
    if (!window.StockExecutionPlan || !window.StockExecutionPlan.buildSell) return { available:false, hasPosition:false, reason:'卖出计划规则运行时未加载。' };
    var phase = marketSessionPhase();
    return window.StockExecutionPlan.buildSell({
      positionLoaded:C.positionLoaded,
      position:C.position,
      quote:(C.analysis || {}).quote || {},
      marketPhase:phase,
      currentDate:marketSessionDate()
    });
  }
  function buildStrictManualBuyPlan() {
    if (!window.StockExecutionPlan || !window.StockExecutionPlan.strictManualBuyPlan) return {available:false, reason:'严格人工复核计划规则运行时未加载。'};
    return window.StockExecutionPlan.strictManualBuyPlan({
      route:C.strategyRoute || {}, code:C.code, marketPhase:marketSessionPhase()
    });
  }
  function renderTradePlan() {
    var el = $('terminalTradePlan');
    if (!el) return;
    var plan = buildTradePlan();
    if (C.accountAccess !== true) {
      el.innerHTML = '<div class="itc-plan-overview"><span class="itc-plan-kicker">买卖点研究</span><strong class="itc-plan-action">' + (plan.available ? '行情条件观察' : '研究数据不足') + '</strong><span class="itc-plan-reason">' + escapeHtml(plan.reason || '仅供研究观察') + '</span></div>' +
        '<div class="itc-plan-card observe"><span class="k">价格研究边界</span><strong class="v">' + (plan.available && plan.buyPlanEnabled !== false && finite(plan.entryLimit) ? '¥' + n(plan.entryLimit, 2) : '暂未形成') + '</strong><span class="d">' + escapeHtml(plan.available ? '研究信号日 ' + (plan.signalAsOf || '未提供') + '；' + (plan.method || '') : plan.reason || '需要已核验的完整日线') + '；不是买入指令。</span></div>' +
        '<div class="itc-plan-card observe"><span class="k">账户与持仓</span><strong class="v">账户读取关闭</strong><span class="d">行情、图表和技术观察独立使用。未计算账户现金、实际持仓或买卖股数；回测的15万元只用于模拟。</span></div>';
      return;
    }
    var sellPlan = buildPositionSellPlan(), strictBuyPlan = buildStrictManualBuyPlan();
    if (!plan.available && !(sellPlan.available && sellPlan.hasPosition) && strictBuyPlan.available !== true) {
      var unavailableReason = plan.reason + (sellPlan && sellPlan.reason ? '；' + sellPlan.reason : '');
      var unavailableHtml = '<div class="itc-plan-loading"><b>买卖点研究暂不可用</b><span>' + escapeHtml(unavailableReason) + '</span></div>';
      if (el.innerHTML !== unavailableHtml) el.innerHTML = unavailableHtml;
      return;
    }
    var sellUrgent = sellPlan.hasPosition && ['exit_review','reduce_review'].indexOf(sellPlan.status) >= 0;
    var headlineAction = sellUrgent ? sellPlan.action : (plan.available ? plan.action : sellPlan.action);
    var headlineReason = sellUrgent ? sellPlan.reason : (plan.available ? plan.reason : sellPlan.reason);
    var stateClass = sellUrgent || (plan.available && plan.state === 'risk') ? ' is-risk' : strictBuyPlan.available === true || (plan.available && plan.state === 'ready') ? ' is-ready' : '';
    var buyPlanEnabled = plan.available && plan.buyPlanEnabled !== false;
    var cashRecorded = !!(C.portfolioSummary && C.portfolioSummary.cash_user_recorded === true);
    var actionableBuyPlan = strictBuyPlan.available === true;
    var accountFacts = C.portfolioSummary || {};
    var accountConstraintText = cashRecorded
      ? '账户现金 ¥' + n(accountFacts.available_cash, 0) + ' · 当前实际仓位 ' + n(accountFacts.exposure_pct, 1) + '% · 风险上限 ' + n(accountFacts.market_target_exposure_pct, 0) + '%'
      : '真实剩余资金未录入';
    var buyCard = actionableBuyPlan
      ? '<div class="itc-plan-card buy"><span class="k">今日严格人工复核计划</span><strong class="v">' + strictBuyPlan.shares + ' 股 · ≤ ¥' + n(strictBuyPlan.limitPrice,2) + '</strong><span class="d">预计成本 ¥' + n(strictBuyPlan.estimatedCost,2) + (finite(strictBuyPlan.estimatedCashRequired) ? ' · 现金占用 ¥' + n(strictBuyPlan.estimatedCashRequired,0) : '') + (finite(strictBuyPlan.targetWeightPct) ? ' · 目标仓位 ' + n(strictBuyPlan.targetWeightPct,1) + '%' : '') + '<br>' + escapeHtml(strictBuyPlan.playbookId + ' · ' + strictBuyPlan.strategyId + '/' + strictBuyPlan.strategyVersion) + (finite(strictBuyPlan.riskLine) ? ' · 风险线 ¥' + n(strictBuyPlan.riskLine,2) : '') + '<br>有效至 ' + escapeHtml(strictBuyPlan.validUntil.replace('T',' ').slice(0,19)) + ' · 失效：' + escapeHtml(strictBuyPlan.invalidationCondition) + '</span></div>'
      : buyPlanEnabled
        ? '<div class="itc-plan-card buy"><span class="k">当前买入计划 · 0 股</span><strong class="v">未形成完整计划</strong><span class="d">' + escapeHtml(strictBuyPlan.reason || C.strategyRouteError || '今日市场、板块、战法、个股与账户条件尚未同时完成') + '<br>' + escapeHtml(accountConstraintText) + '；本地价格 ¥' + n(plan.entryLimit,2) + ' 只作不追高研究边界，不是买入建议。</span></div>'
      : '<div class="itc-plan-card buy"><span class="k">买入计划</span><strong class="v">未启用</strong><span class="d">' + escapeHtml(plan.buyBlockReason || plan.reason) + '</span></div>';
    var sellRiskCard, sellObserveCard, sellMethod;
    if (!sellPlan.available) {
      sellRiskCard = '<div class="itc-plan-card risk"><span class="k">持仓卖出复核</span><strong class="v">不可用</strong><span class="d">' + escapeHtml(sellPlan.reason || C.positionError || '持仓账本暂不可用') + '</span></div>';
      sellObserveCard = '<div class="itc-plan-card observe"><span class="k">卖出触发状态</span><strong class="v">—</strong><span class="d">没有真实持仓数据时不生成卖出提示</span></div>';
      sellMethod = '持仓数据不可用，不生成卖出提示。';
    } else if (!sellPlan.hasPosition) {
      sellRiskCard = '<div class="itc-plan-card risk"><span class="k">持仓卖出复核</span><strong class="v">无持仓</strong><span class="d">手工持仓账本没有该股票，不生成卖出指令</span></div>';
      sellObserveCard = '<div class="itc-plan-card observe"><span class="k">卖出触发状态</span><strong class="v">未启用</strong><span class="d">记录真实持仓后才显示成本、数量与风险线</span></div>';
      sellMethod = '无持仓时卖出计划保持关闭。';
    } else {
      var riskLevel = sellPlan.riskLevel || {};
      var riskKind = {hard_stop:'硬风险线',trailing_stop:'移动风险线',research_fallback:'研究回退线'}[riskLevel.kind] || riskLevel.kind || '类型未知';
      var riskPriority = riskLevel.priority == null ? '优先级未知' : '优先级 ' + riskLevel.priority;
      var riskActivity = riskLevel.active === true ? '当前有效' : '未启用';
      var riskExecution = riskLevel.executable === true ? '后端标记可执行（仍需人工确认）' : '不可自动执行 · 仅人工复核';
      var riskTitle = riskLevel.kind === 'research_fallback' ? '研究风险参考线' : '权威持仓风险线';
      var sellQuantityText = sellPlan.quantityReady
        ? ' · 建议复核卖出 ' + sellPlan.suggestedSellShares + ' 股 → 保留 ' + sellPlan.targetShares + ' 股'
        : ' · 当前持仓 ' + sellPlan.shares + ' 股 · 未形成精确卖出数量';
      sellRiskCard = '<div class="itc-plan-card risk"><span class="k">' + riskTitle + '</span><strong class="v">' + (finite(sellPlan.riskLine) ? '¥' + n(sellPlan.riskLine,2) : '—') + '</strong><span class="d">' + escapeHtml(riskKind + ' · 来源 ' + (riskLevel.source || '未知') + ' · ' + riskPriority + ' · ' + riskActivity + ' · ' + riskExecution + sellQuantityText) + '<br>成本 ¥' + n(sellPlan.entryPrice,2) + ' · 当前 ¥' + n(sellPlan.currentPrice,2) + '（' + escapeHtml(sellPlan.quoteAsOf || '时点缺失') + '）<br>' + escapeHtml(sellPlan.action + '：' + sellPlan.reason + '；' + sellPlan.quantityNote) + '</span></div>';
      var trailingText = finite(sellPlan.trailingActivationLine) ? '+15% 启用 ¥' + n(sellPlan.trailingActivationLine,2) : '+15% 启用 —';
      var structureText = finite(sellPlan.structureObservationLine) ? '+25% 结构 ¥' + n(sellPlan.structureObservationLine,2) : '+25% 结构 —';
      sellObserveCard = '<div class="itc-plan-card observe"><span class="k">持仓观察线 · 非卖出信号</span><strong class="v">' + trailingText + '</strong><span class="d">' + structureText + '<br>' + escapeHtml(sellPlan.observationNote || '+15% / +25% 只作观察，不生成卖出复核。') + '</span></div>';
      sellMethod = sellPlan.cancelCondition;
    }
    var html =
      '<div class="itc-plan-overview' + stateClass + '"><span class="itc-plan-kicker">当前买卖点复核</span><strong class="itc-plan-action">' + escapeHtml(sellUrgent ? headlineAction : actionableBuyPlan ? '买入计划待人工复核' : buyPlanEnabled ? '今日无完整买入计划' : headlineAction) + '</strong><span class="itc-plan-reason">' + escapeHtml(sellUrgent ? headlineReason : actionableBuyPlan ? strictBuyPlan.reason : buyPlanEnabled ? strictBuyPlan.reason || '完整条件尚未同时成立，不生成股数' : headlineReason) + (actionableBuyPlan ? '<br>严格计划 as_of ' + escapeHtml(strictBuyPlan.asOf) : buyPlanEnabled ? '<br>研究信号日 ' + escapeHtml(plan.signalAsOf) : '') + '</span></div>' +
      buyCard + sellRiskCard + sellObserveCard +
      '<details class="itc-plan-method"><summary>价格与执行口径</summary><div>' + (actionableBuyPlan ? '股数、限价、成本、风险线、战法和证据身份均来自今日同一观察时点的严格人工复核计划；页面不二次估算股数。' : buyPlanEnabled ? '规则 ' + escapeHtml(plan.ruleVersion) + ' · 价格口径：未复权收盘推导的2%高开边界 · ' + escapeHtml(plan.method) : '统一综合评估未满足时不展示候选价格；价格几何不会越过资格判断变成建议。') + ' ' + escapeHtml(sellMethod) + ' 当前条件只用于人工复核，不证明事后最低/最高，也不自动下单。</div></details>';
    if (el.innerHTML !== html) el.innerHTML = html;
  }
  function focusObservation(kind) {
    C.observationFocus = kind;
    if (kind === 'trend') { C.overlays.ma5 = true; C.overlays.ma20 = true; }
    if (kind === 'momentum') C.indicatorPanel = 'macd';
    if (kind === 'heat') C.indicatorPanel = 'rsi';
    if (kind === 'position') C.overlays.boll = true;
    if (kind === 'activity') C.indicatorPanel = 'volume';
    renderTechnical(); renderChart();
  }
  function renderSignals() {
    var signals = researchSignals(), quant = quantView(), factors = quant.factor_scores || [], asOf = quant.snapshot_date || quant.as_of || quant.data_as_of;
    var factorHtml = factors.length ? '<div class="itc-kicker">历史研究模型 · 非正式策略</div><div class="itc-bars">' + factors.slice(0,5).map(function (factor) { var z = Number(factor.zscore) || 0; return '<div class="itc-bar-row"><span>' + escapeHtml(factor.factor || '因子') + '</span><span class="itc-bar-track"><span class="itc-bar-fill ' + (z < 0 ? 'bad' : '') + '" style="width:' + Math.min(100, Math.abs(z) / 3 * 100) + '%"></span></span><span class="itc-bar-value ' + colour(z) + '">' + (z > 0 ? '+' : '') + n(z,2) + '</span></div>'; }).join('') + '</div><div class="itc-note">旧模型快照 as-of：' + escapeHtml(String(asOf || '未提供')) + ' · 不属于全站统一综合评分，不进入提醒、仓位或买卖资格</div>' : '<div class="itc-empty">当前股票未命中历史研究模型快照；不会移植其他股票的数据。</div>';
    var signalHtml = signals.slice(-3).reverse().map(function (signal) { var bearish = researchSignalIsBearish(signal); return '<div class="itc-signal ' + (bearish ? 'sell' : '') + '"><strong>' + escapeHtml(signal.label || '技术观察') + ' · ' + n(signal.price,2) + '</strong><br>' + escapeHtml(signal.reason || '未提供逻辑') + '<small>' + escapeHtml(signal.time || '—') + ' · ' + escapeHtml(signal.research_status || 'unverified') + ' · 仅技术观察，不是买卖指令</small></div>'; }).join('');
    var v2 = (C.analysis || {}).v2_backtest || {}, v2Html = '';
    if (v2.expected_runs) {
      var phase = v2.running ? '正在执行真实回测' : v2.completed_runs >= v2.expected_runs ? '回测已落盘，等待结果审查' : '回测未运行或已中断';
      v2Html = '<div class="itc-note" style="margin-top:10px"><b>v2 中小盘池验证：</b>' + escapeHtml(phase) + '（' + escapeHtml(String(v2.completed_runs || 0)) + '/' + escapeHtml(String(v2.expected_runs)) + '）<br>' + escapeHtml(v2.candidate_status || '未完成验证不生成推荐。') + '</div>';
    }
    $('terminalSignals').innerHTML = factorHtml + '<div style="margin-top:10px"><div class="itc-kicker">技术观察记录</div>' + (signalHtml || '<div class="itc-empty">当前完整K线未形成 EMA 交叉观察；MACD 金叉/死叉仍会在副图据实显示。</div>') + '</div>' + v2Html;
  }
  function renderBook() {
    var quote = (C.analysis || {}).quote || {}, amount = quote.amount;
    var depth = quote.depth || null;
    var intradayRatio = (((C.indicators || {}).technical || {}).values || {}).intraday_volume_ratio5;
    var volumeRatio = finite(quote.volume_ratio) ? quote.volume_ratio : intradayRatio;
    var fundFlowTurnover = (((C.analysis || {}).quant || {}).fund_flow || {}).turnover;
    var turnover = finite(quote.turnover_rate) ? quote.turnover_rate : fundFlowTurnover;
    function qty(value) { return value == null || !isFinite(Number(value)) ? '—' : (Math.abs(Number(value)) >= 10000 ? (Number(value) / 10000).toFixed(2) + '万' : n(value, 0)); }
    var html = '';
    var hasBook = depth && depth.buy && depth.sell && (depth.buy.length || depth.sell.length);
    var bookLevel = quote.level2_available === true ? 'level2' : (quote.market_depth_level || (hasBook ? 'level1' : 'unavailable'));
    var levelLabel = bookLevel === 'level2' ? 'Level-2 · 已核验来源' : (bookLevel === 'level1' ? 'Level-2 未接入 · 当前 Level-1' : 'Level-2 未接入 · 盘口不可用');
    if ($('terminalBookLevel')) $('terminalBookLevel').textContent = levelLabel;
    if (hasBook) {
      html += '<div class="itc-depth">';
      for (var i = depth.sell.length - 1; i >= 0; i--) {
        var ask = depth.sell[i];
        html += '<div class="itc-depth-row ask"><span>卖' + (i + 1) + '</span><span class="dp">' + n(ask.price, 2) + '</span><span class="dv">' + qty(ask.volume) + '</span></div>';
      }
      html += '<div class="itc-depth-mid"></div>';
      for (var i = 0; i < depth.buy.length; i++) {
        var bid = depth.buy[i];
        html += '<div class="itc-depth-row bid"><span>买' + (i + 1) + '</span><span class="dp">' + n(bid.price, 2) + '</span><span class="dv">' + qty(bid.volume) + '</span></div>';
      }
      html += '</div>';
      if (quote.latest_tick) html += '<div class="itc-book-note">最近一笔成交：' + escapeHtml(String(quote.latest_tick)) + '</div>';
      html += '<div class="itc-book-note">五档盘口：' + escapeHtml(quote.depth_source || '腾讯财经公开行情（qt.gtimg.cn）') + '。当前不是 Level-2：无逐笔委托、撤单序列和完整深度。</div>';
    } else {
      html += '<div class="itc-book-unavailable"><b>Level-2 尚未接入合法授权源。</b><br>当前接口没有逐笔委托、逐笔成交、撤单序列和完整深度；不会用五档或最近一笔冒充。</div>';
    }
    html += '<div class="itc-book-row"><span>成交活跃度（量比）</span><span class="band"></span><span>' + (finite(volumeRatio) ? n(volumeRatio, 2) + 'x' : '不可计算') + '</span></div>';
    html += '<div class="itc-book-row"><span>成交额</span><span class="band"></span><span>' + (finite(amount) ? (amount / 1e8).toFixed(2) + '亿' : '—') + '</span></div>';
    html += '<div class="itc-book-row"><span>换手</span><span class="band"></span><span>' + (finite(turnover) ? n(turnover, 2) + '%' : '未接入') + '</span></div>';
    if (finite(quote.bid_ask_delta)) html += '<div class="itc-book-row"><span>委差</span><span class="band"></span><span class="' + colour(quote.bid_ask_delta) + '">' + (quote.bid_ask_delta > 0 ? '+' : '') + n(quote.bid_ask_delta, 0) + '</span></div>';
    if (finite(quote.avg_price)) html += '<div class="itc-book-row"><span>均价</span><span class="band"></span><span>' + n(quote.avg_price, 2) + '</span></div>';
    if (finite(quote.outer_volume) || finite(quote.inner_volume)) {
      html += '<div class="itc-book-row"><span>外盘（手）</span><span class="band"></span><span class="itc-up">' + qty(quote.outer_volume) + '</span></div>';
      html += '<div class="itc-book-row"><span>内盘（手）</span><span class="band"></span><span class="itc-down">' + qty(quote.inner_volume) + '</span></div>';
    }
    $('terminalBook').innerHTML = html;
  }
  function canShowDirectionalModelOutput(kronos, quant) {
    var validation = String(kronos.validation_status || quant.validation_status || '').toLowerCase();
    var semantics = String(kronos.output_semantics || quant.output_semantics || '').toLowerCase();
    return ['verified','validated','passed','已验证'].indexOf(validation) >= 0 &&
      ['expected_return','return_regression','收益回归值'].indexOf(semantics) >= 0 &&
      kronos.research_only !== true && kronos.decision_eligible !== false;
  }
  function renderPrediction() {
    var quant = (C.analysis || {}).quant || {}, kronos = quant.kronos || {};
    var outputs = [['5日',kronos.pred_ret_5d],['10日',kronos.pred_ret_10d],['20日',kronos.pred_ret_20d],['30日',kronos.pred_ret_30d]].filter(function (item) { return finite(item[1]); });
    var asOf = kronos.snapshot_date || quant.snapshot_date || null;
    var model = kronos.model || 'Kronos（版本未提供）';
    var validation = kronos.validation_status || quant.validation_status || '未验证';
    var available = kronos.available === true && outputs.length > 0;
    var showDirectional = available && canShowDirectionalModelOutput(kronos, quant);
    var block = '<div class="itc-prediction">';
    if ($('terminalPredictionStatus')) $('terminalPredictionStatus').textContent = showDirectional ? ('已验证快照 ' + (asOf || '时点缺失')) : (available ? '未验证 · 中性低权重' : '模型快照不可用');
    if (showDirectional) {
      outputs.forEach(function (item) {
        block += '<div class="itc-pred-card"><span>' + item[0] + '模型收益输出</span><strong class="' + colour(Number(item[1]) * 100) + '">' + pct(Number(item[1]) * 100,2) + '</strong><div class="itc-pred-meta">模型：' + escapeHtml(model) + ' · as-of：' + escapeHtml(asOf || '未提供') + ' · 验证：' + escapeHtml(validation) + '</div></div>';
      });
      if (finite(kronos.pred_close_20d)) block += '<div class="itc-evidence-row"><b>20日价格输出</b><span>¥' + n(kronos.pred_close_20d,2) + (finite(kronos.last_close) ? '（快照收盘 ¥' + n(kronos.last_close,2) + '）' : '') + '</span></div>';
      if (asOf) appendDecisionRecord({kind:'prediction_snapshot', code:C.code, model:model, asOf:String(asOf), validation:validation, outputs:outputs.map(function (item) { return {horizon:item[0], value:Number(item[1])}; }), fingerprint:['prediction',C.code,model,asOf,outputs.map(function (item) { return item[1]; }).join(',')].join(':')});
    } else if (available) {
      block += '<div class="itc-pred-card"><span>模型收益输出</span><strong>具体数值已隐藏</strong><div class="itc-pred-meta">模型：' + escapeHtml(model) + ' · as-of：' + escapeHtml(asOf || '未提供') + ' · 验证：' + escapeHtml(validation) + ' · 仅中性低权重研究观察</div></div>';
    } else {
      block += '<div class="itc-empty">' + escapeHtml(kronos.message || '预测模型快照未提供') + '；系统不会把缺值显示成 0.00%，也不会补造方向概率或目标价。</div>';
    }
    block += '<div class="itc-note">只有验证状态与收益回归语义均由数据源显式确认时才显示方向数值；否则不使用涨跌颜色，也不进入自动决策。</div></div>';
    $('terminalPrediction').innerHTML = block;
  }
  function eventCountEntries(counts) {
    if (finite(counts) && Number(counts) >= 0) return [['事件合计', Math.trunc(Number(counts))]];
    if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return [];
    return [
      ['A/B级公告证据', counts.a_b],
      ['D级资讯证据', counts.d]
    ].filter(function (item) { return finite(item[1]) && Number(item[1]) >= 0; })
      .map(function (item) { return [item[0], Math.trunc(Number(item[1]))]; });
  }
  function eventCountSummary(counts) {
    var entries = eventCountEntries(counts);
    return entries.length ? entries.map(function (item) { return item[0] + ' ' + item[1] + ' 条'; }).join(' · ') : '计数未提供';
  }
  function profileDims(profile) {
    profile = profile || {};
    var dims = Object.assign({}, profile.dims || {});
    var research = dims.research || {};
    dims.research = Object.assign({}, research, {source_status:research.source_status || ((profile.data_sources || {}).research_reports || {}).status});
    var announcements = dims.announcements || {};
    var source = ((profile.data_sources || {}).announcements) || {};
    var gate = ((profile.gates || {}).events) || {};
    dims.announcements = Object.assign({}, announcements, {
      source_status:announcements.source_status || source.status,
      verification_status:announcements.verification_status || gate.verification_status
    });
    return dims;
  }
  function profileDimStatus(key, dim) {
    dim = dim || {};
    var count = function (values) { return values.filter(function (value) { return value != null && value !== '' && value !== '—'; }).length; };
    var available = 0, required = 1;
    if (key === 'basic') { available = count([dim.name, dim.industry, dim.data_time]); required = 3; }
    else if (key === 'financial_health') { available = Object.keys(dim.metrics || {}).length + count([dim.period, dim.data_as_of || dim.observed_at]); required = 4; }
    else if (key === 'valuation') {
      if (dim.role === 'public_observation_only') {
        var hasValue = count([dim.pe_ttm, dim.pb_mrq]) > 0 && !!dim.as_of;
        return {state:hasValue ? 'partial' : 'unavailable',label:hasValue ? (dim.status === 'stale' ? '历史观测' : '公开观测') : '未接入'};
      }
      available = count([dim.pe_ttm, dim.pb_mrq, dim.as_of]); required = 3;
    }
    else if (key === 'shareholder_funds') {
      available = dim.history_verified === true
        ? count([dim.max_total_pledge_pct, dim.fund_net_yi, dim.window_days])
        : count([dim.max_total_pledge_pct, dim.fund_net_yi, dim.turnover]);
      required = 3;
    }
    else if (key === 'research') {
      var researchStates = [dim.status,dim.availability,dim.source_status,dim.verification_status].filter(Boolean).map(function (value) { return String(value).toLowerCase(); });
      if (researchStates.some(function (status) { return /^(unavailable|stale|error|failed|missing|unknown)$/.test(status); })) return {state:'unavailable',label:'未接入'};
      var reports = Array.isArray(dim.reports) ? dim.reports : [];
      if (!reports.length && dim.count === 0 && researchStates.indexOf('verified_empty') >= 0) return {state:'complete',label:'已核验暂无研报'};
      var validReports = reports.filter(function (report) { return report && typeof report.title === 'string' && report.title.trim() && /^\d{4}-\d{2}-\d{2}$/.test(String(report.date || '')); });
      if (!validReports.length) return {state:'unavailable',label:'未接入'};
      var verified = researchStates.some(function (status) { return /^(available|ready|verified|complete|可用)$/.test(status); });
      return {state:verified ? 'complete' : 'partial',label:verified ? '完整' : '部分接入'};
    }
    else if (key === 'announcements') {
      var statuses = [dim.status, dim.availability, dim.source_status, dim.verification_status].filter(Boolean).map(function (value) { return String(value).toLowerCase(); });
      if (statuses.some(function (status) { return /^(unavailable|stale|error|failed|missing|unknown)$/.test(status); })) return {state:'unavailable', label:'未接入'};
      if (dim.available === false) return {state:'unavailable', label:'未接入'};
      var explicitAvailable = dim.available === true || statuses.some(function (status) { return /^(available|ready|verified|verified_empty|complete)$/.test(status); });
      var evidenceLevel = /^[a-e]$/i.test(String(dim.evidence_level || ''));
      available = explicitAvailable || evidenceLevel ? 1 : 0;
      required = 1;
    }
    else if (key === 'technical_chips') {
      available = (dim.lenses || []).length;
      if (!available) return {state:'unavailable', label:'未接入'};
      if (!dim.chip_distribution) return {state:'partial', label:'技术已接·筹码缺失'};
      required = 3;
    }
    var state = available <= 0 ? 'unavailable' : (available >= required ? 'complete' : 'partial');
    return {state:state, label:state === 'complete' ? '完整' : state === 'partial' ? '部分接入' : '未接入'};
  }
  function renderFundamentals() {
    var dims = profileDims(C.intel && C.intel.profile), fin = dims.financial_health || {}, fm = fin.metrics || {}, funds = dims.shareholder_funds || {}, research = dims.research || {}, tech = dims.technical_chips || {}, chip = tech.chip_distribution || {}, lenses = tech.lenses || [], eventDim = dims.announcements || {};
    function money(value) { return finite(value) ? n(value, 2) + '亿' : '—'; }
    function percent(value) { return finite(value) ? n(value, 2) + '%' : '—'; }
    var defs = [
      ['basic','基础信息', escapeHtml((dims.basic || {}).code || '—') + ' · ' + escapeHtml((dims.basic || {}).name || '—') + '<br>行业：' + escapeHtml((dims.basic || {}).industry || '—') + ' · ' + escapeHtml((dims.basic || {}).board || '—') + '<br>行情时点：' + escapeHtml((dims.basic || {}).data_time || '未提供')],
      ['financial_health','财务健康', '报告期：' + escapeHtml(fin.period || '—') + ' · ' + (fin.data_as_of ? '披露：' + escapeHtml(fin.data_as_of) : '抓取观察：' + escapeHtml(fin.observed_at || '未提供')) + '<br>营收 ' + money(fm.revenue) + ' · 净利 ' + money(fm.net_profit) + '<br>营收同比 ' + percent(fm.revenue_yoy) + ' · 净利同比 ' + percent(fm.np_yoy) + '<br>商誉 ' + (finite(fm.goodwill) ? n(Number(fm.goodwill) / 1e8,2) + '亿' : '—') + ' · 占总资产 ' + percent(fm.goodwill_ratio_pct) + '<br>财务摘要：' + escapeHtml(fin.note || '缺失') + (fin.goodwill_source ? '<br>商誉/总资产：' + escapeHtml(fin.goodwill_source) : '')],
      ['valuation','估值分析', 'PE_TTM ' + n((dims.valuation || {}).pe_ttm, 2) + ' · PB ' + n((dims.valuation || {}).pb_mrq, 2) + '<br>数据日期：' + escapeHtml((dims.valuation || {}).as_of || '—') + '<br>' + escapeHtml((dims.valuation || {}).source || '估值来源未接入') + '<br>' + escapeHtml((dims.valuation || {}).note || '估值数据缺失')],
      ['shareholder_funds','股东与资金', '质押占总股本 ' + percent(funds.max_total_pledge_pct) + '<br>' + (funds.history_verified === true ? ('大额成交净额代理 ' + money(funds.fund_net_yi) + ' · 连续净流出 ' + n(funds.negative_streak_days,0) + ' 日<br>已核验 ' + n(funds.window_days,0) + ' 个交易日 · ' + escapeHtml(funds.window_start || '—') + ' 至 ' + escapeHtml(funds.window_end || '—')) : ('大额成交净额代理 ' + money(funds.fund_net_yi) + ' · 换手 ' + percent(funds.turnover))) + '<br>' + escapeHtml(funds.note || '质押/资金流数据缺失')],
      ['research','机构研报', '已接入 ' + (finite(research.count) ? escapeHtml(String(research.count)) : '0') + ' 条元数据 · 最新 ' + escapeHtml(research.latest_date || '—') + '<br>' + escapeHtml(((research.reports || [])[0] || {}).institution || '—') + ' · ' + escapeHtml(((research.reports || [])[0] || {}).rating || '未评级') + '<br>' + escapeHtml(((research.reports || [])[0] || {}).title || research.note || '暂无可验证研报')],
      ['announcements','公告与事件', '事件计数：' + escapeHtml(eventCountSummary(eventDim.counts)) + '<br>' + escapeHtml((eventDim.reasons || []).slice(0, 1)[0] || (eventCountEntries(eventDim.counts).length ? '已取得结构化事件计数；当前未返回事件理由明细' : '本机未取得可核验公告事件')) + '<br>' + escapeHtml(eventDim.note || '来源未接入')],
      ['technical_chips','技术与筹码', (lenses.slice(0, 2).map(function (lens) { return escapeHtml(lens.name || '指标') + '：' + escapeHtml(String(lens.value || '—')); }).join(' · ') || 'K线历史不足') + '<br>近60根回撤 ' + percent((tech.market_risk || {}).max_drawdown_pct) + ' · 年化波动 ' + percent((tech.market_risk || {}).annualized_volatility_pct) + '<br>' + (chip.is_estimate ? '筹码估算 ' + n(chip.sample_days,0) + ' 日 · 中位成本 ' + n(chip.average_cost,2) + ' · 非真实持仓<br>' : '') + escapeHtml(tech.note || '技术快照不可用')]
    ];
    var states = defs.map(function (d) { return profileDimStatus(d[0], dims[d[0]]); });
    var complete = states.filter(function (state) { return state.state === 'complete'; }).length;
    var partial = states.filter(function (state) { return state.state === 'partial'; }).length;
    if ($('terminalFundamentalStatus')) $('terminalFundamentalStatus').textContent = complete + ' 完整 · ' + partial + ' 部分 · ' + (states.length - complete - partial) + ' 未接入';
    $('terminalFundamentals').innerHTML = defs.map(function (d) {
      var status = profileDimStatus(d[0], dims[d[0]]);
      return '<div class="itc-dimension ' + status.state + '"><strong>' + d[1] + '<em>' + status.label + '</em></strong><span>' + d[2] + (status.state === 'unavailable' ? '<br>数据缺失 · 不计入评分' : '') + '</span></div>';
    }).join('');
  }
  function renderEvidence() {
    var q = (C.analysis || {}).quote || {}, technical = ((C.indicators || {}).technical || {}), activeMeta = C.chartType === 'intraday' ? C.intradayMeta : C.chartMeta, activeCandles = C.chartType === 'intraday' ? C.intraday : C.current, rows = [['行情来源',q.source || '未提供'],['行情 as-of',q.exchange_data_time || q.data_time || '未提供'],['当前图表来源',activeMeta.source || (C.current.length ? '站内统一K线层' : '来源待核验')],['当前图表 as-of',activeMeta.data_time || (activeCandles.length ? String(activeCandles[activeCandles.length - 1].time) : '未提供')],['价格口径',{qfq:'前复权（qfq）',hfq:'后复权（hfq）',raw:'不复权（raw）',unknown:'价格口径待核验'}[chartAdjustType()] || '价格口径待核验'],['K线状态',activeMeta.bar_status || ((activeMeta.market || {}).label) || (C.current.length ? '已完成' : '待核验')],['技术快照',technical.information_time || '未提供'],['研究状态',(technical.research_status || 'unverified') + ' · 不作为独立下单依据']];
    var extra = '';
    if (C.intel && C.intel.honesty && C.intel.honesty.data_sources) {
      extra = '<div class="itc-kicker" style="margin:8px 0 2px">投研聚合来源</div>' + Object.keys(C.intel.honesty.data_sources).map(function (k) {
        return '<div class="itc-evidence-row"><b>' + escapeHtml(k) + '</b><span>' + escapeHtml(C.intel.honesty.data_sources[k] || '—') + '</span></div>';
      }).join('') + '<div class="itc-evidence-row"><b>自动下单</b><span>' + (C.intel.honesty.auto_order === false ? 'false（仅审计）' : escapeHtml(String(C.intel.honesty.auto_order))) + '</span></div>';
    }
    var profileSources = (((C.intel || {}).profile || {}).data_sources) || {};
    if (Object.keys(profileSources).length) {
      extra += '<div class="itc-kicker" style="margin:8px 0 2px">公司档案接入状态</div>' + Object.keys(profileSources).map(function (key) {
        var source = profileSources[key] || {};
        return '<div class="itc-evidence-row"><b>' + escapeHtml(key) + '</b><span>' + escapeHtml((source.source || '未提供') + ' · ' + (source.status || 'unknown') + ' · ' + (source.fetched_at || '无时点')) + '</span></div>';
      }).join('');
    }
    $('terminalEvidence').innerHTML = rows.map(function (row) { return '<div class="itc-evidence-row"><b>' + row[0] + '</b><span>' + escapeHtml(row[1]) + '</span></div>'; }).join('') + extra;
    var acceptedAi = C.drawings.filter(function (drawing) { return !!drawing.sourceAutoId; }).length;
    var records = decisionRecords().filter(function (record) { return record.code === C.code; }).slice(-6).reverse();
    var signals = researchSignals(), ledger = [['当前代码',C.code + ' · ' + ((C.analysis || {}).name || '—')],['研究标记数',String(signals.length) + ' 条（非订单）'],['手工价格标记',String(C.manualLines.length) + ' 条 · 本机浏览器'],['图表画线',String(C.drawings.length) + ' 条 · ' + chartScope() + ' 独立保存'],['Regis AI 划线',String(C.autoDrawings.length) + ' 条候选 · 已采纳 ' + String(acceptedAi) + ' 条 · 本地自主模型'],['策略许可','未在此工作台授予；自动交易关闭']];
    if (C.storageErrors.length) ledger.push(['本机存储错误', C.storageErrors.join('；') + '；相关组件已回退为空，不冒充读取成功']);
    var historyHtml = records.length ? '<div class="itc-kicker" style="margin:8px 0 2px">追加式本地记录（最近6条）</div>' + records.map(function (record) {
      var label = record.kind === 'prediction_snapshot' ? '模型快照' : '采纳AI画线';
      var detail = record.kind === 'prediction_snapshot' ? ((record.model || '模型') + ' · as-of ' + (record.asOf || '—') + ' · ' + (record.validation || '未验证')) : ((record.label || record.sourceId || '候选') + ' · ' + (record.timeframe || '—') + '/' + (record.adjustType || '—') + ' · 证据分 ' + (finite(record.score) ? record.score : '—'));
      return '<div class="itc-evidence-row"><b>' + label + '</b><span>' + escapeHtml(detail) + '<br><small>' + escapeHtml(localCreatedAt(record.recordedAt)) + '</small></span></div>';
    }).join('') : '<div class="itc-empty">当前股票还没有可留痕的模型快照或人工采纳动作。</div>';
    if ($('terminalLedgerStatus')) $('terminalLedgerStatus').textContent = records.length + ' 条当前股票记录 · 仅本机';
    $('terminalLedger').innerHTML = ledger.map(function (row) { return '<div class="itc-evidence-row"><b>' + row[0] + '</b><span>' + escapeHtml(row[1]) + '</span></div>'; }).join('') + historyHtml;
  }

  // ── 渲染：七维基本面画像 ──
  var PROFILE_DIMS = [
    { key:'basic', label:'基础信息' }, { key:'financial_health', label:'财务健康' }, { key:'valuation', label:'估值分析' },
    { key:'shareholder_funds', label:'股东资金' }, { key:'research', label:'机构研报' }, { key:'announcements', label:'公告事件' }, { key:'technical_chips', label:'技术/筹码' }
  ];
  function profileRows(key, dim) {
    if (!dim) return '<div class="itc-empty">该维度数据不足。</div>';
    var out = [];
    function add(label, value) { if (value == null || value === '' || value === '—') return; out.push('<div class="itc-prow"><span class="k">' + escapeHtml(label) + '</span><span class="v">' + escapeHtml(String(value)) + '</span></div>'); }
    if (key === 'basic') {
      add('证券代码', dim.code); add('名称', dim.name); add('行业', dim.industry); add('板块', dim.board); add('数据时点', dim.data_time);
    } else if (key === 'financial_health') {
      add('报告期', dim.period); add('披露时点', dim.data_as_of); if (!dim.data_as_of) add('抓取观察时点', dim.observed_at);
      var fm = { revenue:'营收(亿)', net_profit:'净利(亿)', revenue_yoy:'营收同比(%)', np_yoy:'净利同比(%)', deducted:'扣非净利(亿)', gross_margin:'毛利率', net_margin:'净利率', roe:'ROE', debt_ratio:'资产负债率', ocf_ps:'每股经营现金流', eps:'每股收益', current_ratio:'流动比率' }, m = dim.metrics || {};
      Object.keys(fm).forEach(function (k) { add(fm[k], m[k]); });
      add('商誉', finite(m.goodwill) ? n(Number(m.goodwill) / 1e8, 2) + ' 亿' : null);
      add('总资产', finite(m.total_assets) ? n(Number(m.total_assets) / 1e8, 2) + ' 亿' : null);
      add('商誉/总资产', finite(m.goodwill_ratio_pct) ? n(m.goodwill_ratio_pct, 4) + '%' : null);
      add('商誉字段来源', dim.goodwill_source);
      add('经营现金流/净利', dim.cashflow_to_profit);
      add('说明', dim.note);
    } else if (key === 'valuation') {
      add('PE_TTM', dim.pe_ttm); add('PB', dim.pb_mrq); add('数据日期', dim.as_of); add('数据来源', dim.source); add('抓取时点', dim.observed_at); add('说明', dim.note);
    } else if (key === 'shareholder_funds') {
      add('第一大股东质押占总股本', dim.max_total_pledge_pct != null ? dim.max_total_pledge_pct + '%' : null);
      add('大股东质押占其持股', dim.max_holder_pledge_pct != null ? dim.max_holder_pledge_pct + '%' : null);
      add('大额成交净额代理(亿)', dim.fund_net_yi);
      if (dim.history_verified === true) {
        add('核验窗口', dim.window_days + ' 个交易日 · ' + dim.window_start + ' 至 ' + dim.window_end);
        add('当前连续代理净流出', dim.negative_streak_days + ' 日');
        add('历史来源', dim.history_source); add('口径', dim.history_method); add('抓取时点', dim.history_as_of);
      } else add('换手率', dim.turnover != null ? dim.turnover + '%' : null);
      add('说明', dim.note);
    } else if (key === 'research') {
      add('状态', dim.status); add('研报数量', dim.count); add('最新研报日期', dim.latest_date);
      (dim.reports || []).slice(0, 5).forEach(function (report, index) {
        var summary = [report.date, report.institution, report.rating, report.title].filter(Boolean).join(' · ');
        var pdfUrl = safeHttpUrl(report.pdf_url);
        if (pdfUrl) out.push('<div class="itc-prow"><span class="k">研报 ' + (index + 1) + '</span><span class="v"><a href="' + escapeAttr(pdfUrl) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(summary) + '</a></span></div>');
        else add('研报 ' + (index + 1), summary);
      });
      add('说明', dim.note);
    } else if (key === 'announcements') {
      var countEntries = eventCountEntries(dim.counts);
      if (countEntries.length) countEntries.forEach(function (item) { add(item[0], item[1] + ' 条'); });
      else add('事件计数', '计数未提供');
      add('证据级别', dim.evidence_level);
      (dim.reasons || []).forEach(function (r, i) { add('事件' + (i + 1), r); });
      add('说明', dim.note);
    } else if (key === 'technical_chips') {
      add('状态', dim.status);
      add('数据日期', dim.as_of || dim.information_time); add('数据来源', dim.source);
      add('价格口径', {qfq:'前复权',hfq:'后复权',raw:'不复权',unknown:'待核验'}[dim.adjust_type]);
      add('已完成日线', dim.history_bars != null ? dim.history_bars + ' 根' : null);
      (dim.lenses || []).forEach(function (l) { add(l.name || '指标', l.value + '（' + (l.state || '—') + '）'); });
      add('近60根最大回撤', dim.market_risk && dim.market_risk.max_drawdown_pct != null ? dim.market_risk.max_drawdown_pct + '%' : null);
      add('近60根年化波动', dim.market_risk && dim.market_risk.annualized_volatility_pct != null ? dim.market_risk.annualized_volatility_pct + '%' : null);
      var chip = dim.chip_distribution || {};
      add('筹码口径', chip.is_estimate ? '换手衰减估算 · 非真实股东持仓' : null);
      add('筹码样本', chip.sample_days ? chip.sample_days + ' 个交易日 · 截至 ' + (chip.as_of || '—') : null);
      add('估算中位成本', finite(chip.average_cost) ? n(chip.average_cost, 2) : null);
      add('70%估算成本区', finite(chip.cost_70_low) && finite(chip.cost_70_high) ? n(chip.cost_70_low,2) + '–' + n(chip.cost_70_high,2) : null);
      add('90%估算成本区', finite(chip.cost_90_low) && finite(chip.cost_90_high) ? n(chip.cost_90_low,2) + '–' + n(chip.cost_90_high,2) : null);
      add('获利筹码估算', finite(chip.profit_ratio) ? n(Number(chip.profit_ratio) * 100, 2) + '%' : null);
      add('估算方法', chip.method); add('筹码来源', chip.source);
      add('说明', dim.note);
    }
    if (!out.length) out.push('<div class="itc-empty">该维度无可用数据。</div>');
    return out.join('');
  }
  function renderProfile(profile) {
    profile = profile || {};
    var dims = profileDims(profile);
    $('terminalProfileTabs').innerHTML = PROFILE_DIMS.map(function (d, i) {
      var status = profileDimStatus(d.key, dims[d.key]);
      return '<button class="itc-profile-tab' + (i === 0 ? ' active' : '') + '" type="button" id="terminalProfileTab-' + d.key + '" data-profile-tab="' + d.key + '" role="tab" aria-controls="terminalProfilePane-' + d.key + '" aria-selected="' + (i === 0 ? 'true' : 'false') + '" tabindex="' + (i === 0 ? '0' : '-1') + '">' + d.label + '<span class="itc-profile-state ' + status.state + '">' + status.label + '</span></button>';
    }).join('');
    $('terminalProfilePanes').innerHTML = PROFILE_DIMS.map(function (d, i) {
      var status = profileDimStatus(d.key, dims[d.key]);
      return '<div class="itc-profile-pane' + (i === 0 ? ' active' : '') + '" id="terminalProfilePane-' + d.key + '" data-profile-pane="' + d.key + '" role="tabpanel" aria-labelledby="terminalProfileTab-' + d.key + '"' + (i === 0 ? '' : ' hidden') + '><div class="itc-profile-banner ' + status.state + '">' + status.label + '</div>' + profileRows(d.key, dims[d.key]) + '</div>';
    }).join('');
    bindRovingTabs($('terminalProfileTabs'), '[data-profile-tab]', function (button) {
      root.querySelectorAll('[data-profile-pane]').forEach(function (pane) { pane.classList.remove('active'); pane.hidden = true; });
      var pane = root.querySelector('[data-profile-pane="' + button.dataset.profileTab + '"]');
      if (pane) { pane.classList.add('active'); pane.hidden = false; }
    });
    var states = PROFILE_DIMS.map(function (d) { return profileDimStatus(d.key, dims[d.key]); });
    var complete = states.filter(function (state) { return state.state === 'complete'; }).length;
    var partial = states.filter(function (state) { return state.state === 'partial'; }).length;
    var statusText = profile.status === 'loading' ? '正在载入当前股票的公司信息七维；旧股票画像已清空。' : profile.status === 'building' ? '公司档案后台刷新中；本轮不会沿用上一只股票。' : profile.status === 'error' ? ('公司档案载入失败：' + (profile.error || '未知错误')) : ('公司信息七维：' + complete + ' 完整、' + partial + ' 部分、' + (states.length - complete - partial) + ' 未接入。');
    if (profile.status === 'blocked') statusText = '公司档案未请求：' + (profile.error || '主数据未取得。');
    $('terminalProfileHonesty').textContent = statusText + (profile.generated_at ? ' 档案时点 ' + profile.generated_at + '。' : '') + ' 这套七维只作信息画像，不再产生第二个总分；右侧是全站唯一综合评分。';
  }

  // ── 渲染：综合评分 + 评级 + 一票否决 + ECharts 雷达 + 七维卡片 ──
  var DIM_DEFS = [
    { key:'growth', label:'成长' }, { key:'quality', label:'质量' }, { key:'valuation', label:'估值' },
    { key:'technical', label:'技术' }, { key:'funds', label:'资金' }, { key:'risk', label:'风险' },
    { key:'timing', label:'时机' }
  ];
  // 研究动作评级 → CSS token；真正卖出只来自持仓风险/策略退出链。
  var RATING_KEYS = { '强烈买入':'strong-buy', '买入':'buy', '中性':'neutral', '回避':'avoid', '排除':'exclude', '卖出':'sell' };
  function renderRadarECharts(radar) {
    var el = $('terminalRadar');
    if (C.radarChart) { try { C.radarChart.dispose(); } catch (error) { reportTerminalIssue('radar-dispose', error); } C.radarChart = null; }
    if (!radar || !window.echarts) { el.innerHTML = '<div class="itc-empty">雷达图不可用。</div>'; return; }
    var values = DIM_DEFS.map(function (d) { return radar[d.key] == null ? null : Number(radar[d.key]); });
    if (values.every(function (v) { return v == null; })) { el.innerHTML = '<div class="itc-empty">雷达图数据不足。</div>'; return; }
    if (values.some(function (v) { return v == null; })) {
      var missing = DIM_DEFS.filter(function (d, index) { return values[index] == null; }).map(function (d) { return d.label; });
      el.innerHTML = '<div class="itc-empty">七维未齐：缺 ' + escapeHtml(missing.join('、')) + '。雷达图不把缺值伪装成 0 分；请看下方逐维卡片。</div>';
      return;
    }
    C.radarChart = echarts.init(el);
    C.radarChart.setOption({
      backgroundColor: 'transparent',
      radar: { indicator: DIM_DEFS.map(function (d) { return { name: d.label, max: 100 }; }), radius: '65%',
        axisName: { color: '#8B8F9A', fontSize: 12 }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.08)' } },
        splitArea: { areaStyle: { color: ['rgba(255,255,255,0.02)', 'rgba(255,255,255,0.01)'] } }, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.08)' } } },
      series: [{ type: 'radar', data: [{ value: values, name: '七维评分', areaStyle: { color: 'rgba(10,132,255,0.28)' }, lineStyle: { color: '#0A84FF', width: 2 }, itemStyle: { color: '#0A84FF' } }] }]
    });
  }
  function renderComposite(composite, veto, radar) {
    composite = composite || {}; radar = radar || {};
    var scorePending = C.scorePendingProfile === true;
    var score = scorePending ? null : composite.composite;
    var actionRating = displayResearchRating(composite.action_rating || composite.rating, !!C.position);
    var rating = composite.research_band || researchScoreBand(score);
    var rk = RATING_KEYS[actionRating] || 'na';
    var confLabel = { high: '高', medium: '中', low: '低' }[composite.confidence] || '';
    var coverage = finite(composite.coverage_pct) ? Number(composite.coverage_pct) : (finite(composite.weight_used) ? Number(composite.weight_used) * 100 : 0);
    if ($('terminalScoreCoverage')) $('terminalScoreCoverage').textContent = scorePending ? (window.RegisAccess?.canWrite === true ? '资料补全中 · 暂不发布总分' : '资料缺失 · 只读页面不能发起补全，不发布总分') : (coverage ? ('有效权重 ' + n(coverage,0) + '% · 归一化到100%') : '无有效维度');
    var gapNote = '';
    if (composite.data_gap === 'critical') gapNote = '<div class="itc-gapnote danger">基本面数据缺口 critical：不可核验，不授予投资评级（上限回避）</div>';
    else if (composite.data_gap === 'moderate') gapNote = '<div class="itc-gapnote warn">基本面数据缺 1 维：评级上限中性</div>';
    if (composite.coverage_status === 'insufficient') gapNote += '<div class="itc-gapnote danger">有效权重低于60%：覆盖不足，评级上限回避</div>';
    var audit = composite.as_of_audit || null;
    var auditHtml = '';
    if (audit) {
      var auditLabels = {growth:'成长',quality:'质量',valuation:'估值',risk:'风险',technical:'技术',funds:'资金',timing:'时机'};
      var inputRows = Object.keys(audit.inputs || {}).map(function (key) {
        var row = audit.inputs[key] || {};
        var age = row.age_calendar_days == null ? '年龄未知' : row.age_calendar_days + '日';
        var limit = row.freshness_limit_calendar_days == null ? '上限未知' : '上限' + row.freshness_limit_calendar_days + '日';
        return (auditLabels[key] || key) + ' ' + (row.as_of || '未知') + '（' + age + '/' + limit + '）';
      });
      var excluded = (audit.excluded_dimensions || []).map(function (key) { return auditLabels[key] || key; });
      auditHtml = '<div class="itc-honesty"><b>综合分输入时点：</b>' + escapeHtml(inputRows.join(' · ') || '未提供') +
        '<br>最大日历年龄 ' + escapeHtml(audit.max_input_age_calendar_days == null ? '未知' : audit.max_input_age_calendar_days + '日') +
        ' · 超限/未知排除 ' + escapeHtml(excluded.join('、') || '无') + ' · ' + escapeHtml(audit.rule || '时点规则未提供') + '</div>';
    }
    if (score == null) {
      $('terminalScoreHead').innerHTML = '<div class="itc-score-number">—<small>' + (scorePending ? '资料刷新中，避免展示会变化的临时总分' : '全部维度数据不足') + '</small></div>' + gapNote + auditHtml;
    } else {
      $('terminalScoreHead').innerHTML = '<div class="itc-score-number">' + n(score, 1) + '<small>个股唯一综合评分 · 权重归一化</small></div>'
        + '<span class="itc-rating rating-' + rk + '">' + escapeHtml(rating || '—') + '</span>'
        + '<span class="itc-conf">仅研究排序 · 非买卖建议</span>'
        + (confLabel ? '<span class="itc-conf">置信度 ' + confLabel + '</span>' : '')
        + gapNote + auditHtml;
    }
    $('terminalDimCards').innerHTML = DIM_DEFS.map(function (d) {
      var v = radar[d.key];
      if (v == null) return '<div class="itc-dim-card"><span class="l"><span>' + d.label + '</span></span><span class="v miss">数据不足</span><span class="b">未计入加权</span></div>';
      var detail = (composite.detail || []).filter(function (x) { return x.key === d.key; })[0];
      var weightBasis = detail ? '权重 ' + Math.round((detail.base_weight == null ? detail.weight : detail.base_weight) * 100) + '% → 有效 ' + Math.round((detail.effective_weight == null ? detail.weight : detail.effective_weight) * 100) + '%' : '';
      var inputs = detail && Array.isArray(detail.basis) ? detail.basis.filter(Boolean) : [];
      var basis = weightBasis + (inputs.length ? '<br>' + inputs.map(escapeHtml).join('<br>') : '') + (detail && detail.dimension_note ? '<br>' + escapeHtml(detail.dimension_note) : '');
      return '<div class="itc-dim-card"><span class="l"><span>' + d.label + '</span><span class="' + colour(v - 50) + '">' + (v >= 50 ? '↑' : '↓') + '</span></span><span class="v ' + colour(v - 50) + '">' + n(v, 1) + '</span><span class="b">' + basis + '</span></div>';
    }).join('');
    var vetoEl = $('terminalVeto');
    vetoEl.hidden = true; vetoEl.className = 'itc-veto'; vetoEl.innerHTML = '';
    var vetoItems = (veto && veto.items) || [];
    var observations = vetoItems.filter(function (item) { return item.decision_effect === 'risk_review_only'; });
    var observationLabels = {
      '主力连续20日净流出': '大额成交净额代理连续流出观察',
      '跌破关键支撑且放量': '布林下轨与RSI低位组合观察（未含成交量）'
    };
    var observationHtml = observations.length ? '<div class="itc-honesty"><b>风险观察（不参与一票否决、评分、仓位或提醒）</b><br>' + observations.map(function (item) { return '· ' + escapeHtml((observationLabels[item.rule] || item.rule) + '：' + item.basis); }).join('<br>') + '</div>' : '';
    if (veto && veto.vetoed) {
      vetoEl.hidden = false;
      vetoEl.innerHTML = '<strong>' + (C.position ? '一票否决生效，停止新开仓并进入持仓风险复核' : '一票否决生效，排除新开仓') + '</strong><br>' + (veto.reasons || []).map(function (r) { return '· ' + escapeHtml(r); }).join('<br>') + '<div class="itc-honesty">' + escapeHtml(veto.honesty || '') + '</div>' + observationHtml;
    } else if (veto && (veto.items || []).length) {
      var hardItems = vetoItems.filter(function (item) { return item.decision_effect !== 'risk_review_only'; });
      var unverified = hardItems.filter(function (item) { return item.status === 'unverifiable' || item.triggered == null; });
      var cleared = hardItems.filter(function (item) { return item.status === 'clear' || item.triggered === false; });
      if (unverified.length) {
        vetoEl.hidden = false;
        vetoEl.className = 'itc-veto is-warning';
        vetoEl.innerHTML = '<strong>一票否决核验未完成（' + unverified.length + ' 项缺数据）</strong><br>' + unverified.map(function (item) { return '· 无法核验｜' + escapeHtml(item.rule + '：' + item.basis); }).join('<br>') + (cleared.length ? '<br>' + cleared.map(function (item) { return '· ' + (item.evidence_scope ? '限定范围未触发｜' : '已核验通过｜') + escapeHtml(item.rule + '：' + item.basis); }).join('<br>') : '') + '<div class="itc-honesty">未知不等于未触发；未知项不参与强制否决。</div>' + observationHtml;
      } else if (veto.verification_complete && cleared.length) {
        vetoEl.hidden = false;
        vetoEl.className = 'itc-veto is-clear';
        vetoEl.innerHTML = '<strong>已核验规则未触发一票否决</strong><br>' + cleared.map(function (item) { return '· ' + (item.evidence_scope ? '限定范围未触发｜' : '') + escapeHtml(item.rule + '：' + item.basis); }).join('<br>') + observationHtml;
      } else if (observationHtml) {
        vetoEl.hidden = false;
        vetoEl.className = 'itc-veto is-clear';
        vetoEl.innerHTML = observationHtml;
      }
    }
    $('terminalScoreNote').textContent = [
      composite.methodology_version ? ('方法版本：' + composite.methodology_version) : '',
      composite.score_semantics || '',
      composite.note || '评分只用于研究排序和证据展示，不构成投资建议；缺数据时不会猜测或补分。'
    ].filter(Boolean).join('｜');
    renderRadarECharts(radar);
  }
  // 兼容旧调用名：未载入聚合时显示占位
  function renderScore() {
    if (!C.intel) { $('terminalScoreHead').innerHTML = '<div class="itc-score-number">—<small>' + escapeHtml(intelUnavailableText()) + '</small></div>'; $('terminalRadar').innerHTML = ''; $('terminalScoreNote').textContent = intelUnavailableText() + ' 不输出综合评分。'; return; }
    renderComposite(C.intel.composite || {}, C.intel.veto || {}, C.intel.radar || {});
  }

  // ── 渲染：资金筹码 / 9 段式总结 ──
  function renderFundsPanel(funds) {
    funds = funds || {};
    var snap = funds.ff_snapshot || {};
    var rows = [];
    var normalized = funds.normalized_net_yi != null ? funds.normalized_net_yi : funds.net_yi;
    var raw = funds.raw_net_yi;
    var scale = funds.magnitude_scale != null ? funds.magnitude_scale : snap.scale;
    if (!finite(normalized) && funds.turnover == null && !snap.reason) rows.push('<div class="itc-empty">资金流快照无该股数据。</div>');
    if (!finite(normalized) && snap.reason) rows.push('<div class="itc-empty">' + escapeHtml(snap.reason) + '</div>');
    if (finite(normalized)) {
      rows.push('<div class="itc-evidence-row"><b>第三方资金净额观察（统一折算口径）</b><span class="' + colour(normalized) + '">' + n(normalized,2) + ' 亿（' + (Number(normalized) >= 0 ? '净流入' : '净流出') + '）</span></div>');
    }
    if (funds.turnover != null) rows.push('<div class="itc-evidence-row"><b>换手率</b><span>' + n(funds.turnover,2) + '%</span></div>');
    if (finite(funds.history_days)) rows.push('<div class="itc-evidence-row"><b>历史覆盖</b><span>' + n(funds.history_days,0) + ' 个不同交易日 · 每日只取最后审计版本</span></div>');
    if (finite(raw)) rows.push('<details class="itc-honesty"><summary>查看资金流原始口径（仅溯源）</summary><div>原始接口值 ' + n(raw,2) + ' 亿' + (finite(scale) ? ' · 统一折算倍率 ' + n(scale,1) : '') + '；该原始值不作为本卡主显示或二次计算输入。</div></details>');
    var asOf = funds.as_of || snap.ts;
    rows.push('<div class="itc-honesty">' + (asOf ? '最近审计快照 as_of ' + escapeHtml(String(asOf)) + '；' : '最近审计快照无时点；') + (finite(funds.history_days) ? '20日大额成交净额代理仅作风险观察，当前覆盖 ' + n(funds.history_days,0) + '/20 日。' : '20日风险观察历史未接入本轮响应。') + '</div>');
    $('terminalFunds').innerHTML = rows.join('');
  }
  function renderFunds() {
    if (!C.intel) { $('terminalFunds').innerHTML = '<div class="itc-empty">' + escapeHtml(intelUnavailableText()) + '</div>'; return; }
    renderFundsPanel(C.intel.funds || {});
  }
  function renderSummary9(summary) {
    summary = summary || {};
    var segs = summary.segments || [];
    if (!segs.length) { $('terminalSummary').innerHTML = '<div class="itc-empty">AI 总结不可用。</div>'; return; }
    $('terminalSummary').innerHTML = segs.map(function (s) {
      return '<p><b>' + escapeHtml(s.title) + '</b>：' + escapeHtml(s.body) + '</p>';
    }).join('');
    $('terminalSummaryHonesty').innerHTML = '生成方式：' + escapeHtml(summary.generator || '本地规则化生成') + '。' + escapeHtml(summary.honesty || '所有字段来自真实数据源，缺失项如实标注，不编造。');
  }
  function renderSummary() {
    var candles = C.chartType === 'intraday' ? (C.intraday || []) : (C.current || []), local = C.indicatorData || {}, index = candles.length - 1;
    if (index < 0) { $('terminalSummary').innerHTML = '<div class="itc-empty">当前周期K线不足，无法生成本地规则总结。</div>'; $('terminalSummaryHonesty').textContent = '生成方式：本地规则；不调用外部 AI，不补造数据。'; return; }
    var close = Number(candles[index].close), ma20 = local.ma20 && local.ma20[index], rsi = local.rsi && local.rsi[index], macd = local.macd && local.macd[index], volumeAverage = candles.length >= 20 ? mean(candles.slice(-20).map(function (row) { return Number(row.volume || 0); })) : null, volumeRatio = finite(volumeAverage) && volumeAverage > 0 ? Number(candles[index].volume || 0) / volumeAverage : null, levels = C.autoLevels || {}, paragraphs = [];
    paragraphs.push('<p><b>当前周期</b>：' + escapeHtml(C.chartType === 'intraday' ? '真实分时' : (LABELS[C.timeframe] || C.timeframe)) + '截至 ' + escapeHtml(String(candles[index].time)) + '，最新 ' + n(close,2) + '。</p>');
    paragraphs.push('<p><b>趋势结构</b>：' + (finite(ma20) ? (close >= ma20 ? '价格在20期均线之上，说明此周期相对均线偏强；这不是买入建议。' : '价格在20期均线之下，说明此周期相对均线偏弱；这不是卖出建议。') : '样本不足，未计算20期均线。') + '</p>');
    paragraphs.push('<p><b>短期热度</b>：' + (finite(rsi) ? (rsi > 70 ? 'RSI高于70，短期涨速偏快，需注意波动放大。' : rsi < 30 ? 'RSI低于30，短期跌速偏快，需注意波动风险。' : 'RSI处于30到70之间，短期速度未到常用极端阈值。') : '样本不足，未计算RSI。') + '</p>');
    paragraphs.push('<p><b>动量与成交</b>：' + (finite(macd) ? (macd >= 0 ? 'MACD柱在零轴上方。' : 'MACD柱在零轴下方。') : 'MACD样本不足。') + (finite(volumeRatio) ? (' 当前成交量约为20期均量的 ' + n(volumeRatio,2) + ' 倍。') : ' 成交量均值样本不足。') + '</p>');
    paragraphs.push('<p><b>关键观察区</b>：' + (levels.supports && levels.supports.length ? ('下方可能的历史价格聚集区约 ' + n(levels.supports[0].price,2) + '；') : '暂未形成可靠支撑区；') + (levels.resistances && levels.resistances.length ? ('上方可能的历史价格聚集区约 ' + n(levels.resistances[0].price,2) + '。') : '暂未形成可靠压力区。') + '</p>');
    $('terminalSummary').innerHTML = paragraphs.join('');
    $('terminalSummaryHonesty').textContent = '生成方式：浏览器本地规则（当前K线、均线、MACD、RSI、成交量与关键位）；不调用外部 AI，不生成目标价、胜率或下单指令。';
  }

  // ── K 线图画线：手动多锚点（水平/趋势/斐波那契/通道） ──
  function extendLine(p1, p2, minTime, maxTime) {
    var t1 = numTime(p1.time), t2 = numTime(p2.time), dt = (t2 - t1) || 1;
    var slope = (p2.price - p1.price) / dt;
    return [
      { time: minTime, value: p1.price + slope * (numTime(minTime) - t1) },
      { time: maxTime, value: p1.price + slope * (numTime(maxTime) - t1) }
    ];
  }
  function addLineSeries(chart, color, lineStyle) {
    return chart.addLineSeries({ color: color, lineWidth: 1, lineStyle: lineStyle, lastValueVisible: false, crosshairMarkerVisible: false, priceLineVisible: false, autoscaleInfoProvider:function () { return null; } });
  }
  function drawSegment(chart, p1, p2, color, lineStyle) {
    if (!p1 || !p2) return;
    addLineSeries(chart, color, lineStyle == null ? 2 : lineStyle).setData([{ time:p1.time, value:p1.price }, { time:p2.time, value:p2.price }]);
  }
  function drawVerticalGuide(chart, point, label, color) {
    var coordinate = chart.timeScale().timeToCoordinate && chart.timeScale().timeToCoordinate(point.time), holder = $('terminalChart');
    if (coordinate == null || !holder) return;
    var guide = document.createElement('div'); guide.className = 'itc-vertical-guide'; guide.style.left = Math.round(coordinate) + 'px'; guide.style.borderColor = color || '#FFB800'; guide.title = label; holder.appendChild(guide);
    C.verticalGuides.push(guide);
  }
  function renderDrawings(chart, series, candles) {
    if (!C.drawings.length || !candles.length) return;
    var minTime = timeValue(candles[0].time);
    var maxTime = timeValue(candles[candles.length - 1].time);
    C.drawings.forEach(function (d) {
      var p = d.points || [];
      if (d.visible === false) return;
      if (d.type === 'horizontal' && p[0] && p[0].price != null) {
        series.createPriceLine({ price: p[0].price, color: '#FFB800', lineWidth: 1, lineStyle: 2, title: d.name || ('水平 ' + n(p[0].price,2)), axisLabelVisible: true });
      } else if (d.type === 'trend' && p.length >= 2) {
        // 趋势线按两端斜率延伸到当前周期首尾，而不是只在两点间画短线段；
        // 这样平移/缩放后趋势线始终贯穿整图，才是"趋势线"该有的样子。
        addLineSeries(chart, '#0A84FF', 2).setData(extendLine(p[0], p[1], minTime, maxTime));
      } else if (d.type === 'ray' && p.length >= 2) {
        addLineSeries(chart, '#0A84FF', 2).setData(extendLine(p[0], p[1], p[0].time, maxTime));
      } else if (d.type === 'fib' && p.length >= 2) {
        var lo = Math.min(p[0].price, p[1].price), hi = Math.max(p[0].price, p[1].price), span = hi - lo;
        addLineSeries(chart, '#BF5AF2', 1).setData(extendLine(p[0], p[1], minTime, maxTime));
        [0, .236, .382, .5, .618, .786, 1].forEach(function (ratio) {
          series.createPriceLine({ price: lo + span * ratio, color: '#BF5AF2', lineWidth: 1, lineStyle: 2, title: 'FIB ' + ratio.toFixed(3), axisLabelVisible: true });
        });
      } else if ((d.type === 'channel' || d.type === 'parallel') && p.length >= 3) {
        addLineSeries(chart, '#0A84FF', 2).setData(extendLine(p[0], p[1], minTime, maxTime));
        var t1 = numTime(p[0].time), t2 = numTime(p[1].time), dt = (t2 - t1) || 1;
        var slope = (p[1].price - p[0].price) / dt;
        addLineSeries(chart, 'rgba(10,132,255,.55)', 3).setData([
          { time: minTime, value: p[2].price + slope * (numTime(minTime) - numTime(p[2].time)) },
          { time: maxTime, value: p[2].price + slope * (numTime(maxTime) - numTime(p[2].time)) }
        ]);
      } else if (d.type === 'rectangle' && p.length >= 2) {
        var left = p[0], right = p[1], high = Math.max(left.price, right.price), low = Math.min(left.price, right.price), a = {time:left.time,price:high}, b = {time:right.time,price:high}, c = {time:right.time,price:low}, e = {time:left.time,price:low};
        drawSegment(chart, a, b, '#FFB800', 2); drawSegment(chart, b, c, '#FFB800', 2); drawSegment(chart, c, e, '#FFB800', 2); drawSegment(chart, e, a, '#FFB800', 2);
      } else if (d.type === 'fibExtension' && p.length >= 3) {
        var base = p[1].price - p[0].price;
        [0.618, 1, 1.618, 2.618].forEach(function (ratio) { series.createPriceLine({ price:p[2].price + base * ratio, color:'#BF5AF2', lineWidth:1, lineStyle:2, title:'扩展 ' + ratio.toFixed(3), axisLabelVisible:true }); });
      } else if (d.type === 'measure' && p.length >= 2) {
        drawSegment(chart, p[0], p[1], '#FFB800', 2);
        series.createPriceLine({price:p[1].price,color:'rgba(255,184,0,.66)',lineWidth:1,lineStyle:2,title:d.name || TOOL_NAMES.measure,axisLabelVisible:true});
      } else if ((d.type === 'longPosition' || d.type === 'shortPosition') && p.length >= 3) {
        var isLong = d.type === 'longPosition';
        series.createPriceLine({price:p[0].price,color:'#38BDF8',lineWidth:2,lineStyle:0,title:'入场 ' + n(p[0].price,2),axisLabelVisible:true});
        series.createPriceLine({price:p[1].price,color:isLong ? '#2EBD85' : '#F6465D',lineWidth:1,lineStyle:2,title:'目标 ' + n(p[1].price,2),axisLabelVisible:true});
        series.createPriceLine({price:p[2].price,color:isLong ? '#F6465D' : '#2EBD85',lineWidth:2,lineStyle:3,title:'风险 ' + n(p[2].price,2),axisLabelVisible:true});
      } else if (d.type === 'anchoredVwap' && p[0]) {
        var anchorIndex = candles.findIndex(function (row) { return timeKey(row.time) === timeKey(p[0].time); });
        if (anchorIndex >= 0) {
          var cumulativeValue = 0, cumulativeVolume = 0, vwapData = [];
          candles.slice(anchorIndex).forEach(function (row) {
            var volume = Number(row.volume), typical = (Number(row.high) + Number(row.low) + Number(row.close)) / 3;
            if (!finite(volume) || volume <= 0 || !finite(typical)) return;
            cumulativeValue += typical * volume; cumulativeVolume += volume;
            vwapData.push({time:timeValue(row.time),value:cumulativeValue / cumulativeVolume});
          });
          if (vwapData.length) addLineSeries(chart, '#22D3EE', 0).setData(vwapData);
        }
      } else if (d.type === 'text' && p[0]) {
        series.createPriceLine({price:p[0].price,color:'rgba(255,184,0,.72)',lineWidth:1,lineStyle:2,title:d.name || '文字标注',axisLabelVisible:true});
      }
    });
  }
  function renderVerticalDrawings(chart) {
    C.verticalGuides = [];
    C.drawings.filter(function (d) { return d.visible !== false && d.type === 'vertical' && d.points && d.points[0]; }).forEach(function (d) {
      drawVerticalGuide(chart, d.points[0], d.name || '垂直时间线', '#FFB800');
      if (C.verticalGuides.length) C.verticalGuides[C.verticalGuides.length - 1].dataset.drawingId = d.id;
    });
  }
  // 垂直参考线是 DOM 覆盖层：平移/缩放时轻量更新 left，而不是每帧重建。
  function repositionVerticalGuides() {
    if (!C.chart || !C.verticalGuides.length) return;
    var timeScale = C.chart.timeScale(), hasConverter = typeof timeScale.timeToCoordinate === 'function';
    C.verticalGuides.forEach(function (guide) {
      var d = C.drawings.find(function (item) { return item.id === guide.dataset.drawingId; });
      if (!d || d.visible === false || !d.points || !d.points[0]) { guide.style.display = 'none'; return; }
      guide.style.display = '';
      var coordinate = hasConverter ? timeScale.timeToCoordinate(d.points[0].time) : null;
      guide.style.left = coordinate == null ? '-9999px' : Math.round(coordinate) + 'px';
    });
  }
  function timeAtPoint(param) {
    if (param.time != null) return param.time;
    if (C.chart && C.chart.timeScale && param.point && C.chart.timeScale().coordinateToTime) {
      var t = C.chart.timeScale().coordinateToTime(param.point.x);
      if (t == null) return null;
      if (typeof t === 'number') return t;
      if (t.year != null) { var mm = String(t.month).length === 1 ? '0' + t.month : String(t.month), dd = String(t.day).length === 1 ? '0' + t.day : String(t.day); return t.year + '-' + mm + '-' + dd; }
    }
    return null;
  }
  function completedDrawingName(tool, points, candles) {
    if (tool === 'measure' && points.length >= 2) {
      var start = Number(points[0].price), end = Number(points[1].price), change = start ? (end / start - 1) * 100 : 0;
      var firstIndex = (candles || []).findIndex(function (row) { return timeKey(row.time) === timeKey(points[0].time); });
      var secondIndex = (candles || []).findIndex(function (row) { return timeKey(row.time) === timeKey(points[1].time); });
      var bars = firstIndex >= 0 && secondIndex >= 0 ? Math.abs(secondIndex - firstIndex) : null;
      return '测量 ' + (change >= 0 ? '+' : '') + n(change, 2) + '%' + (bars == null ? '' : ' · ' + bars + ' 根K线');
    }
    if ((tool === 'longPosition' || tool === 'shortPosition') && points.length >= 3) {
      var entry = Number(points[0].price), target = Number(points[1].price), stop = Number(points[2].price);
      var reward = tool === 'longPosition' ? target - entry : entry - target, risk = tool === 'longPosition' ? entry - stop : stop - entry;
      return (tool === 'longPosition' ? '多头' : '空头') + '风险收益 · ' + (reward > 0 && risk > 0 ? '1:' + n(reward / risk, 2) : '锚点顺序待复核');
    }
    if (tool === 'anchoredVwap') return '锚定 VWAP · ' + String(points[0] && points[0].time || '当前锚点');
    return TOOL_NAMES[tool] || tool;
  }
  function onChartClick(param) {
    if (!C.drawTool || !param.point || !C.series || !C.series.coordinateToPrice) return;
    var price = C.series.coordinateToPrice(param.point.y);
    if (!finite(price)) return;
    var visibleCandles = C.chartType === 'intraday' ? C.intraday : C.current;
    if (C.magnet && visibleCandles && visibleCandles.length && param.time != null) {
      // 分时图/日K统一用墙上时钟比较（分时 param.time=unix秒 与 timeValue(row.time) 相等；
      // 日K param.time=BusinessDay 对象，必须用 timeKey 归一比较）
      var row = C.chartType === 'intraday'
        ? (visibleCandles.find(function (item) { return timeValue(item.time) === Number(param.time); }) || null)
        : visibleCandles.find(function (item) { return timeKey(item.time) === timeKey(param.time); });
      if (row) {
        var candidates = [row.open, row.high, row.low, row.close].filter(finite).map(Number);
        price = candidates.sort(function (a, b) { return Math.abs(a - price) - Math.abs(b - price); })[0];
      }
    }
    price = Number(price.toFixed(2));
    var tool = C.drawTool;
    if (tool === 'support' || tool === 'resistance') {
      checkpointDrawings();
      C.manualLines.push({ kind: tool, price: price, createdAt: new Date().toISOString() });
      // 工具保持激活，可连续标记多个价位；Esc 退出
      saveLines(); C.drawPending = [];
      updateToolState(); renderManualLines(); renderEvidence(); renderChart();
      return;
    }
    if (tool === 'horizontal') {
      checkpointDrawings();
      C.drawings.push({ id: 'd' + Date.now() + Math.random().toString(36).slice(2, 6), type: 'horizontal', name:TOOL_NAMES[tool], points: [{ price: price }], visible:true, note:'', createdAt: new Date().toISOString() });
      saveDrawings(); C.drawPending = [];
      updateToolState(); renderDrawingList(); renderChart();
      return;
    }
    var t = timeAtPoint(param);
    if (t == null) { $('terminalDrawHint').textContent = '请在K线数据区域内点击，以确定时间锚点。'; return; }
    C.drawPending.push({ time: t, price: price });
    var need = ['channel','parallel','fibExtension','longPosition','shortPosition'].indexOf(tool) >= 0 ? 3 : (['vertical','anchoredVwap','text'].indexOf(tool) >= 0 ? 1 : 2);
    if (C.drawPending.length >= need) {
      checkpointDrawings();
      var completedPoints = C.drawPending.slice();
      C.drawings.push({ id: 'd' + Date.now() + Math.random().toString(36).slice(2, 6), type: tool, name:completedDrawingName(tool, completedPoints, visibleCandles), points: completedPoints, visible:true, note:'', createdAt: new Date().toISOString() });
      // 完成一条后工具保持激活，可继续画下一条；Esc 退出
      saveDrawings(); C.drawPending = [];
      updateToolState(); renderDrawingList(); renderChart();
    } else {
      $('terminalDrawHint').textContent = '已选 ' + C.drawPending.length + '/' + need + ' 个点，继续在图上点击（' + TOOL_NAMES[tool] + '）。';
    }
  }

  // ── K 线主图 ──
  function indicatorLineData(candles, values) {
    return (candles || []).map(function (row, index) { return finite(values && values[index]) ? { time:timeValue(row.time), value:Number(values[index]) } : null; }).filter(Boolean);
  }
  function addIndicatorLine(chart, candles, values, color, title, style, width) {
    var data = indicatorLineData(candles, values);
    if (!data.length) return;
    return chart.addLineSeries({ color:color, lineWidth:width || 1, lineStyle:style == null ? 0 : style, title:title, lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false }).setData(data);
  }
  function addIndicatorLineTail(chart, candles, values, color, title, style, bars) {
    var start = Math.max(0, (candles || []).length - Number(bars || 24));
    return addIndicatorLine(chart, (candles || []).slice(start), (values || []).slice(start), color, title, style);
  }
  function macdCrossSummary(data) {
    var latest = (data.macdCrosses || []).slice(-1)[0];
    return latest ? '最近' + latest.label + '：' + latest.displayTime : '当前周期尚无可计算的 DIF/DEA 交叉';
  }
  function latestMacdValues(data) {
    data = data || {};
    var index = Math.max((data.macd || []).length, (data.dif || []).length, (data.dea || []).length) - 1;
    if (index < 0) return '当前值不可用';
    return '当前 DIF ' + n((data.dif || [])[index], 3)
      + ' · DEA ' + n((data.dea || [])[index], 3)
      + ' · 柱 ' + n((data.macd || [])[index], 3);
  }
  function syncIndicatorControls() {
    root.querySelectorAll('[data-quick-indicator]').forEach(function (button) { button.setAttribute('aria-pressed', button.dataset.quickIndicator === C.indicatorPanel ? 'true' : 'false'); });
    root.querySelectorAll('input[name="terminal-indicator"]').forEach(function (input) { input.checked = input.value === C.indicatorPanel; });
  }
  function renderAutoOverlays(chart, series, candles) {
    if (!C.overlays.autoDrawings) return;
    var minTime = timeValue(candles[0].time), maxTime = timeValue(candles[candles.length - 1].time), occupiedPrices = visibleDecisionPrices();
    C.autoDrawings.forEach(function (item, index) {
      if (C.autoVisibility[item.id] === false) return;
      if (item.type === 'zone') {
        var zoneUp = item.color === '#2EBD85' || item.color === '#00D672';
        var keyPrice = finite(item.projectedPrice) ? item.projectedPrice : (item.low + item.high) / 2;
        var keyColor = zoneUp ? '#168F67' : '#D93D50';
        series.createPriceLine({ price:keyPrice, color:keyColor, lineWidth:3, lineStyle:0, title:item.label, axisLabelVisible:true });
        if (item.points && item.points.length) {
          var touchSeries = chart.addLineSeries({color:'rgba(0,0,0,0)',lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false,autoscaleInfoProvider:function () { return null; }});
          var touchData = [], seenTouch = Object.create(null);
          item.points.forEach(function (point) {
            var key = timeKey(point.time);
            if (seenTouch[key]) return;
            seenTouch[key] = true;
            touchData.push({time:point.time,value:keyPrice});
          });
          touchData.sort(function (a, b) { return numTime(a.time) - numTime(b.time); });
          touchSeries.setData(touchData);
          setSeriesMarkersCompatible(touchSeries, touchData.map(function (point) { return {time:point.time,position:'inBar',color:keyColor,shape:'circle',text:''}; }));
        }
      } else if (item.type === 'fib') {
        var fibLow = finite(item.swingLow) ? item.swingLow : item.low, fibHigh = finite(item.swingHigh) ? item.swingHigh : item.high, span = fibHigh - fibLow;
        var downSwing = item.kind.indexOf('_down') >= 0;
        [0, .236, .382, .5, .618, .786, 1].forEach(function (ratio) {
          var price = downSwing ? fibLow + span * ratio : fibHigh - span * ratio, golden = ratio === .5 || ratio === .618;
          series.createPriceLine({ price:price, color:golden ? 'rgba(191,90,242,.88)' : 'rgba(191,90,242,.34)', lineWidth:golden ? 2 : 1, lineStyle:golden ? 0 : 2, title:golden ? '有效摆动 FIB ' + ratio.toFixed(3) : '', axisLabelVisible:golden });
        });
      } else if (item.type === 'trend' && item.points && item.points.length >= 2) {
        drawSegment(chart, item.points[0], item.points[1], item.color, item.touches >= 3 ? 0 : 2);
      } else if (item.type === 'pattern_123' && item.points && item.points.length >= 3) {
        var reversalColor = item.kind.indexOf('bullish') >= 0 ? '#168F67' : '#D93D50';
        drawSegment(chart, item.points[0], item.points[1], reversalColor, 0);
        drawSegment(chart, item.points[1], item.points[2], reversalColor, 0);
        var reversalSeries = chart.addLineSeries({color:'rgba(0,0,0,0)',lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false,autoscaleInfoProvider:function () { return null; }});
        reversalSeries.setData(item.points.map(function (point) { return {time:point.time,value:point.price}; }));
        setSeriesMarkersCompatible(reversalSeries, item.points.map(function (point, pointIndex) { return {time:point.time,position:'inBar',color:reversalColor,shape:'circle',text:String(pointIndex + 1)}; }));
        if (finite(item.triggerPrice)) series.createPriceLine({price:item.triggerPrice,color:'#0A84FF',lineWidth:2,lineStyle:2,title:'123 触发 ' + n(item.triggerPrice,2),axisLabelVisible:true});
      } else if (item.type === 'box') {
        series.createPriceLine({ price:item.high, color:'rgba(10,132,255,.6)', lineWidth:1, lineStyle:1, title:item.label + '上沿', axisLabelVisible:false });
        series.createPriceLine({ price:item.low, color:'rgba(10,132,255,.6)', lineWidth:1, lineStyle:1, title:item.label + '下沿', axisLabelVisible:false });
      } else if (item.type === 'pivot') {
        series.createPriceLine({ price:item.price, color:item.color, lineWidth:1, lineStyle:2, title:item.label, axisLabelVisible:true });
      } else if (item.type === 'breakout' && finite(item.price)) {
        var boEnd = item.time ? item.time : maxTime;
        if (numTime(boEnd) > minTime) addLineSeries(chart, item.color, 2).setData([{ time:minTime, value:item.price }, { time:boEnd, value:item.price }]);
      } else if (item.type === 'channel' && item.points && item.points.length >= 3) {
        var p = item.points; addLineSeries(chart, item.color, 2).setData(extendLine(p[0], p[2], minTime, maxTime));
        var slope = (p[2].price - p[0].price) / ((numTime(p[2].time) - numTime(p[0].time)) || 1);
        addLineSeries(chart, 'rgba(10,132,255,.52)', 3).setData([{ time:minTime, value:p[1].price + slope * (numTime(minTime) - numTime(p[1].time)) }, { time:maxTime, value:p[1].price + slope * (numTime(maxTime) - numTime(p[1].time)) }]);
      } else if (item.type === 'impulse_channel' && item.points && item.points.length >= 4) {
        var path = item.points;
        drawSegment(chart, path[0], path[1], '#FFB800', 0);
        drawSegment(chart, path[2], path[3], item.kind.indexOf('_up') >= 0 ? '#2EBD85' : '#F6465D', 0);
        series.createPriceLine({price:item.low,color:'rgba(56,189,248,.48)',lineWidth:1,lineStyle:2,title:'通道起点回踩区下沿',axisLabelVisible:false});
        series.createPriceLine({price:item.high,color:'rgba(56,189,248,.82)',lineWidth:2,lineStyle:2,title:'通道起点回踩区 ' + n(item.low,2) + '–' + n(item.high,2),axisLabelVisible:true});
      }
      var scenario = aiScenarioFor(item), showConditions = C.focusedAutoId ? C.focusedAutoId === item.id : index === 0;
      if (scenario && showConditions && claimOverlayPrice(occupiedPrices, (scenario.confirm || {}).price)) series.createPriceLine({ price:Number(scenario.confirm.price), color:'#0A84FF', lineWidth:1, lineStyle:2, title:'结构确认 ' + n(scenario.confirm.price,2), axisLabelVisible:true });
      if (scenario && showConditions && claimOverlayPrice(occupiedPrices, (scenario.invalidate || {}).price)) series.createPriceLine({ price:Number(scenario.invalidate.price), color:'#FFB800', lineWidth:1, lineStyle:2, title:'结构失效 ' + n(scenario.invalidate.price,2), axisLabelVisible:true });
    });
  }
  function visibleDecisionPrices() {
    if (!C.overlays.scenario) return [];
    var map = C.decisionMap || {}, presentation = map.presentation || {}, scenarios = Array.isArray(map.scenarios) ? map.scenarios : [];
    var roles = C.focusedScenarioRole ? [C.focusedScenarioRole] : (Array.isArray(presentation.visibleRoles) ? presentation.visibleRoles : scenarios.map(function (item) { return item.role; }));
    return scenarios.reduce(function (prices, item) {
      if (!item || !item.geometry || roles.indexOf(item.role) < 0) return prices;
      if (item.role === 'entry' && item.availability === 'locked' && finite(item.referencePrice)) claimOverlayPrice(prices, item.referencePrice);
      else { claimOverlayPrice(prices, item.geometry.low); claimOverlayPrice(prices, item.geometry.high); }
      return prices;
    }, []);
  }
  function drawDecisionOverlays(chart, series, candles) {
    var map = C.decisionMap || {}, presentation = map.presentation || {}, scenarios = Array.isArray(map.scenarios) ? map.scenarios : [];
    var visibleRoles = C.focusedScenarioRole ? [C.focusedScenarioRole] : (Array.isArray(presentation.visibleRoles) ? presentation.visibleRoles : scenarios.map(function (item) { return item.role; }));
    if (C.overlays.scenario) scenarios.forEach(function (item) {
      if (!item || !item.geometry || visibleRoles.indexOf(item.role) < 0) return;
      var geometry = item.geometry, low = Number(geometry.low), high = Number(geometry.high);
      if (!finite(low) || !finite(high)) return;
      var color = item.role === 'entry' ? '#38BDF8' : item.role === 'target' ? '#A78BFA' : '#FF4458';
      var style = item.role === 'risk' ? 3 : item.availability === 'active' ? 0 : 2;
      if (item.role === 'entry' && item.availability === 'locked' && finite(item.referencePrice)) {
        series.createPriceLine({price:Number(item.referencePrice),color:color,lineWidth:2,lineStyle:style,title:'收回确认 > ' + n(item.referencePrice,2),axisLabelVisible:true});
        return;
      }
      if (Math.abs(high - low) < .000001) {
        series.createPriceLine({price:low,color:color,lineWidth:item.role === 'risk' ? 2 : 1,lineStyle:style,title:item.label,axisLabelVisible:true});
      } else {
        series.createPriceLine({price:low,color:color,lineWidth:1,lineStyle:style,title:'',axisLabelVisible:false});
        series.createPriceLine({price:high,color:color,lineWidth:2,lineStyle:style,title:item.label + ' ' + item.priceText,axisLabelVisible:true});
      }
    });
    var trend = map.trend || {};
    if (!C.focusedScenarioRole && C.overlays.primaryTrend && presentation.showPrimaryTrend !== false && trend.defaultVisible && Array.isArray(trend.points) && trend.points.length >= 2) {
      drawSegment(chart, {time:timeValue(trend.points[0].time),price:Number(trend.points[0].price)}, {time:timeValue(trend.points[1].time),price:Number(trend.points[1].price)}, '#BF5AF2', 0);
    }
    if (C.volatilityMode === 'boll') {
      addIndicatorLineTail(chart, candles, C.indicatorData.bollMid, 'rgba(148,163,184,.82)', 'BOLL 中轨', 2, 24);
      addIndicatorLineTail(chart, candles, C.indicatorData.bollUpper, 'rgba(100,116,139,.58)', 'BOLL 上轨', 2, 24);
      addIndicatorLineTail(chart, candles, C.indicatorData.bollLower, 'rgba(100,116,139,.58)', 'BOLL 下轨', 2, 24);
    } else if (C.volatilityMode === 'atr') {
      addIndicatorLineTail(chart, candles, C.indicatorData.bollMid, 'rgba(148,163,184,.72)', 'SMA20', 2, 24);
      addIndicatorLineTail(chart, candles, C.indicatorData.atrUpper, 'rgba(56,189,248,.48)', 'ATR 上轨', 2, 24);
      addIndicatorLineTail(chart, candles, C.indicatorData.atrLower, 'rgba(56,189,248,.48)', 'ATR 下轨', 2, 24);
    }
  }
  function drawOverlays(chart, series, candles) {
    if (C.overlays.tradePlan && C.timeframe === '1d') {
      var plan = buildTradePlan();
      if (plan.available && finite(plan.entryLimit)) series.createPriceLine({ price:plan.entryLimit, color:'#38BDF8', lineWidth:2, lineStyle:2, title:'下一开盘上限 ' + n(plan.entryLimit,2), axisLabelVisible:true });
      if (plan.available && finite(plan.riskLine) && Math.abs(Number(plan.riskLine) - Number(plan.entryLimit)) > .0001) series.createPriceLine({ price:plan.riskLine, color:'#FFB800', lineWidth:1, lineStyle:2, title:'收盘失效观察 ' + n(plan.riskLine,2), axisLabelVisible:true });
    }
    if (C.overlays.levels) {
      C.autoLevels.supports.forEach(function (item) { series.createPriceLine({ price:item.price, color:'#00D672', lineWidth:1, lineStyle:2, title:'可能止跌 ' + n(item.price,2), axisLabelVisible:true }); });
      C.autoLevels.resistances.forEach(function (item) { series.createPriceLine({ price:item.price, color:'#FF4458', lineWidth:1, lineStyle:2, title:'可能遇阻 ' + n(item.price,2), axisLabelVisible:true }); });
    }
    C.manualLines.forEach(function (item) { series.createPriceLine({ price:item.price, color:lineColor(item.kind), lineWidth:2, lineStyle:0, title:item.kind === 'support' ? '我的止跌参考' : '我的遇阻参考', axisLabelVisible:true }); });
    drawDecisionOverlays(chart, series, candles);
    renderAutoOverlays(chart, series, candles);
    if (C.overlays.ma5) addIndicatorLine(chart, candles, C.indicatorData.ma5, '#F59E0B', 'MA 5');
    if (C.overlays.ma10) addIndicatorLine(chart, candles, C.indicatorData.ma10, '#38BDF8', 'MA 10');
    if (C.overlays.ma20) addIndicatorLine(chart, candles, C.indicatorData.ma20, '#A78BFA', 'MA 20');
    if (C.overlays.ma60) addIndicatorLine(chart, candles, C.indicatorData.ma60, '#EAB308', 'MA 60');
    if (C.overlays.ema20) addIndicatorLine(chart, candles, C.indicatorData.ema20, '#FB7185', 'EMA 20', 2);
    if (C.overlays.ema60) addIndicatorLine(chart, candles, C.indicatorData.ema60, '#34D399', 'EMA 60', 2);
    if (C.overlays.boll) { addIndicatorLine(chart, candles, C.indicatorData.bollMid, '#94A3B8', 'BOLL 中轨', 2); addIndicatorLine(chart, candles, C.indicatorData.bollUpper, '#64748B', 'BOLL 上轨', 2); addIndicatorLine(chart, candles, C.indicatorData.bollLower, '#64748B', 'BOLL 下轨', 2); }
  }
  function indicatorChartOptions(container, timeVisible) {
    return { width:container.clientWidth, height:container.clientHeight, layout:{attributionLogo:true,background:{type:'solid',color:'#0a0d12'},textColor:'#8B8F9A'}, grid:{vertLines:{color:'rgba(255,255,255,0.035)'},horzLines:{color:'rgba(255,255,255,0.05)'}}, crosshair:{mode:LightweightCharts.CrosshairMode.Normal}, rightPriceScale:{borderColor:'rgba(255,255,255,0.08)',minimumWidth:58,scaleMargins:{top:.16,bottom:.16}}, timeScale:{borderColor:'rgba(255,255,255,0.08)',timeVisible:timeVisible,secondsVisible:false} };
  }
  // Lightweight Charts v5 统一使用 chart.addSeries(Type, options)。保留这层极薄适配，
  // 让现有主图、副图和辅助线代码同时兼容仓库中的 v5 运行时与旧 v4 运行时。
  function createCompatibleChart(container, options) {
    var chart = LightweightCharts.createChart(container, options);
    if (!chart.addLineSeries && chart.addSeries && LightweightCharts.LineSeries) {
      chart.addLineSeries = function (seriesOptions) { return chart.addSeries(LightweightCharts.LineSeries, seriesOptions); };
    }
    if (!chart.addHistogramSeries && chart.addSeries && LightweightCharts.HistogramSeries) {
      chart.addHistogramSeries = function (seriesOptions) { return chart.addSeries(LightweightCharts.HistogramSeries, seriesOptions); };
    }
    if (!chart.addCandlestickSeries && chart.addSeries && LightweightCharts.CandlestickSeries) {
      chart.addCandlestickSeries = function (seriesOptions) { return chart.addSeries(LightweightCharts.CandlestickSeries, seriesOptions); };
    }
    return chart;
  }
  function setSeriesMarkersCompatible(series, markers) {
    if (!series || !markers || !markers.length) return null;
    if (series.setMarkers) {
      series.setMarkers(markers);
      return null;
    }
    if (LightweightCharts.createSeriesMarkers) {
      return LightweightCharts.createSeriesMarkers(series, markers);
    }
    return null;
  }
  // 副图 HUD：给定当前十字线 time，从本地指标数组取该 K 线的指标值
  function indicatorValuesAt(panel, data, candles, t) {
    if (!data || !candles || !candles.length) return null;
    var idx = candles.findIndex(function (row) { return timeKey(row.time) === timeKey(t); });
    if (idx < 0) idx = candles.length - 1;
    if (idx < 0) return null;
    if (panel === 'volume') return '量 ' + n(Number(candles[idx].volume || 0) / 1e4, 0) + '万';
    var parts = [];
    if (panel === 'macd') {
      if (finite(data.dif[idx])) parts.push('DIF ' + n(data.dif[idx], 3));
      if (finite(data.dea[idx])) parts.push('DEA ' + n(data.dea[idx], 3));
      if (finite(data.macd[idx])) parts.push('柱 ' + n(data.macd[idx], 3));
    } else if (panel === 'kdj') {
      if (finite(data.k[idx])) parts.push('K ' + n(data.k[idx], 1));
      if (finite(data.d[idx])) parts.push('D ' + n(data.d[idx], 1));
      if (finite(data.j[idx])) parts.push('J ' + n(data.j[idx], 1));
    } else if (panel === 'rsi') {
      if (finite(data.rsi[idx])) parts.push('RSI ' + n(data.rsi[idx], 1));
    }
    return parts.length ? ({macd:'MACD', kdj:'KDJ', rsi:'RSI'}[panel] || panel) + ' ' + parts.join('  ') : null;
  }
  function renderIndicatorChart(candles) {
    var container = $('terminalIndicatorChart'), label = $('terminalIndicatorLabel');
    if (!container) return;
    if (C.indicatorChart) { try { C.indicatorChart.remove(); } catch (error) { reportTerminalIssue('indicator-dispose', error); } C.indicatorChart = null; }
    container.innerHTML = '';
    if (!candles.length || !window.LightweightCharts) { container.innerHTML = '<div class="itc-empty" style="padding:16px">副图需要当前周期的真实K线数据。</div>'; return; }
    var P = C.indParams || {};
    var title = {volume:'成交量',macd:'MACD (' + (P.macdFast||12) + ',' + (P.macdSlow||26) + ',' + (P.macdSignal||9) + ')',kdj:'KDJ (' + (P.kdjN||9) + ',' + (P.kdjK||3) + ',' + (P.kdjD||3) + ')',rsi:'RSI (' + (P.rsiPeriod||14) + ')'}[C.indicatorPanel] || '成交量';
    label.textContent = title + (C.indicatorPanel === 'macd' ? ' · DIF 蓝线 / DEA 黄线 / 红正绿负 · ' + latestMacdValues(C.indicatorData || {}) + ' · ' + macdCrossSummary(C.indicatorData || {}) : '') + ' · 本地计算';
    C.indicatorChart = createCompatibleChart(container, indicatorChartOptions(container, C.chartType === 'intraday' || C.timeframe.indexOf('m') >= 0));
    var times = candles.map(function (row) { return timeValue(row.time); }), data = C.indicatorData || calculateIndicators(candles, P);
    C.indicatorSeries = null;
    if (C.indicatorPanel === 'volume') {
      var volume = C.indicatorChart.addHistogramSeries({ priceFormat:{type:'volume'}, priceScaleId:'right', title:'成交量' });
      volume.setData(candles.map(function (row, index) { var prior = candles[index - 1] || row; return {time:times[index], value:Number(row.volume || 0), color:row.close >= prior.close ? 'rgba(246,70,93,.56)' : 'rgba(46,189,133,.56)'}; }));
      C.indicatorSeries = volume;
    } else if (C.indicatorPanel === 'macd') {
      var histogram = C.indicatorChart.addHistogramSeries({ priceScaleId:'right', title:'MACD', base:0, lastValueVisible:false, priceLineVisible:false, priceFormat:{type:'custom',minMove:.001,formatter:function (value) { return n(value, 3); }} });
      histogram.setData(candles.map(function (row, index) { return finite(data.macd[index]) ? {time:times[index], value:data.macd[index], color:data.macd[index] >= 0 ? 'rgba(246,70,93,.88)' : 'rgba(46,189,133,.88)'} : null; }).filter(Boolean));
      var zero = C.indicatorChart.addLineSeries({ color:'rgba(255,255,255,0)', lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false });
      zero.createPriceLine({price:0,color:'rgba(139,143,154,.6)',lineWidth:1,lineStyle:2,title:'零轴'});
      C.indicatorSeries = addIndicatorLine(C.indicatorChart, candles, data.dif, '#38BDF8', 'DIF', 0, 2); addIndicatorLine(C.indicatorChart, candles, data.dea, '#F59E0B', 'DEA', 0, 2);
      setSeriesMarkersCompatible(histogram, (data.macdCrosses || []).slice(-1).map(function (cross) { return { time:cross.time, position:cross.kind === 'golden' ? 'belowBar' : 'aboveBar', color:cross.kind === 'golden' ? '#F6465D' : '#2EBD85', shape:cross.kind === 'golden' ? 'arrowUp' : 'arrowDown', text:cross.label }; }));
    } else if (C.indicatorPanel === 'kdj') {
      addIndicatorLine(C.indicatorChart, candles, data.d, '#F59E0B', 'D'); addIndicatorLine(C.indicatorChart, candles, data.j, '#A78BFA', 'J');
      C.indicatorSeries = addIndicatorLine(C.indicatorChart, candles, data.k, '#38BDF8', 'K');
      var kRef = C.indicatorChart.addLineSeries({ color:'rgba(255,255,255,0)', lastValueVisible:false, priceLineVisible:false }); kRef.createPriceLine({price:80,color:'rgba(255,68,88,.55)',lineWidth:1,lineStyle:2,title:'80'}); kRef.createPriceLine({price:20,color:'rgba(0,214,114,.55)',lineWidth:1,lineStyle:2,title:'20'});
    } else if (C.indicatorPanel === 'rsi') {
      C.indicatorSeries = addIndicatorLine(C.indicatorChart, candles, data.rsi, '#A78BFA', 'RSI ' + (P.rsiPeriod||14));
      var rRef = C.indicatorChart.addLineSeries({ color:'rgba(255,255,255,0)', lastValueVisible:false, priceLineVisible:false }); rRef.createPriceLine({price:70,color:'rgba(255,68,88,.55)',lineWidth:1,lineStyle:2,title:'70'}); rRef.createPriceLine({price:30,color:'rgba(0,214,114,.55)',lineWidth:1,lineStyle:2,title:'30'});
    }
    // 副图与主图同视窗：继承主图当前可见范围，切换副图面板时不再整体跳动
    var mainRange = C.chart ? C.chart.timeScale().getVisibleLogicalRange() : null;
    if (mainRange) C.indicatorChart.timeScale().setVisibleLogicalRange(mainRange);
    else C.indicatorChart.timeScale().fitContent();
    if (C.chart && C.indicatorSeries) {
      // 副图十字线同步回主图（_syncX 防回环），并实时刷新副图 HUD 标签
      C.indicatorChart.subscribeCrosshairMove(function (param) {
        if (C._syncX || !C.chart || !C.series || param.time == null || !param.point) return;
        var subPrice = C.indicatorSeries.coordinateToPrice ? C.indicatorSeries.coordinateToPrice(param.point.y) : null;
        if (subPrice == null) return;
        C._syncX = true;
        try { C.chart.setCrosshairPosition(subPrice, param.time, C.series); } catch (error) { reportTerminalIssue('crosshair-main-sync', error); }
        C._syncX = false;
        var iv = indicatorValuesAt(C.indicatorPanel, data, candles, param.time);
        if (iv && label) label.textContent = '副图：' + title + ' · ' + iv;
      });
    }
    syncIndicatorControls();
  }
  // ── 分时图专业视图：连续价格线 + 均价 + 涨跌停/昨收参考线 ──
  // 量能面板由 renderIntradayVolume 复用页面既有副图容器渲染（价上量下布局）。
  // 分时右轴涨跌幅刻度（同花顺口径）：昨收居中为 0%，网格标签按相对昨收百分比显示
  function intradayAxisFormatter(prevClose) {
    return function (value) {
      if (!finite(value) || !finite(prevClose) || Number(prevClose) <= 0) return n(value, 2);
      return pct((Number(value) / Number(prevClose) - 1) * 100, 2);
    };
  }
  function buildIntradayChart(chart, candles, prevClose, limitPct) {
    var manualScale = finite(prevClose);
    var last = candles[candles.length - 1];
    var lineColor = finite(prevClose) && Number(last.close) < Number(prevClose) ? '#2EBD85' : '#F6465D';
    var series = chart.addLineSeries({ color:lineColor, lineWidth:2, title:'分时价', priceLineVisible:true, lastValueVisible:true, crosshairMarkerVisible:true, priceFormat:{ type:'custom', formatter:function (v) { return n(v, 2); } }, autoscaleInfoProvider: manualScale ? function () {
      // Lightweight Charts v4 不支持 rightPriceScale 的 minValue/maxValue。
      // 以昨收为中心、按当天真实价格和均价留白，既不裁切，也不会用 ±10% 区间压扁分时走势。
      var pc = Number(prevClose), distance = Math.max.apply(null, candles.map(function (row) { return Math.max(Math.abs(Number(row.close) - pc), finite(row.vwap) ? Math.abs(Number(row.vwap) - pc) : 0); }));
      var halfRange = Math.max(distance * 1.18, pc * 0.003);
      return { priceRange:{ minValue:pc - halfRange, maxValue:pc + halfRange }, margins:{ above:0.08, below:0.08 } };
    } : undefined });
    series.setData(candles.map(function (row) { return { time:timeValue(row.time), value:Number(row.close) }; }));
    var average = chart.addLineSeries({ color:'#FFB800', lineWidth:1, title:'均价线', lastValueVisible:true, priceLineVisible:false, crosshairMarkerVisible:false, axisLabelVisible:true, priceFormat:{ type:'custom', formatter:function (v) { return n(v, 2); } } });
    average.setData(candles.map(function (row) { return finite(row.vwap) ? { time:timeValue(row.time), value:Number(row.vwap) } : null; }).filter(Boolean));
    if (manualScale) {
      var pc = Number(prevClose), lu = pc * (1 + limitPct / 100), ld = pc * (1 - limitPct / 100);
      series.createPriceLine({ price:pc, color:'#8B8F9A', lineWidth:1, lineStyle:2, title:'昨收 ' + n(pc,2), axisLabelVisible:true });
      series.createPriceLine({ price:lu, color:'#F6465D', lineWidth:1, lineStyle:0, title:'涨停 ' + n(lu,2), axisLabelVisible:true });
      series.createPriceLine({ price:ld, color:'#2EBD85', lineWidth:1, lineStyle:0, title:'跌停 ' + n(ld,2), axisLabelVisible:true });
    }
    return series;
  }
  // 分时量能面板（复用页面既有副图容器）：每根分钟量按对昨收涨跌着色，时间轴与主图双向同步
  function renderIntradayVolume(candles, prevClose) {
    var container = $('terminalIndicatorChart'), label = $('terminalIndicatorLabel');
    if (!container || !window.LightweightCharts) return;
    if (C.indicatorChart) { try { C.indicatorChart.remove(); } catch (error) { reportTerminalIssue('indicator-dispose', error); } C.indicatorChart = null; }
    container.innerHTML = '';
    if (!candles.length) { container.innerHTML = '<div class="itc-empty" style="padding:16px">无分时量能数据。</div>'; return; }
    C.indicatorChart = createCompatibleChart(container, indicatorChartOptions(container, true));
    var vol = C.indicatorChart.addHistogramSeries({ priceFormat:{ type:'volume' }, priceScaleId:'right', title:'分时量' });
    vol.setData(candles.map(function (row, idx) {
      var base = finite(prevClose) ? prevClose : (idx > 0 ? Number(candles[idx - 1].close) : null);
      var up = finite(base) ? Number(row.close) >= Number(base) : true;
      return { time:timeValue(row.time), value:Number(row.volume) || 0, color:up ? 'rgba(246,70,93,.5)' : 'rgba(46,189,133,.5)' };
    }));
    C.indicatorSeries = vol;
    if (label) label.textContent = '副图：分时成交量（手）';
    // 与主图时间轴同视窗：继承主图当前可见范围，并双向同步（与日K副图一致的口径）
    var mainRange = C.chart ? C.chart.timeScale().getVisibleLogicalRange() : null;
    if (mainRange) C.indicatorChart.timeScale().setVisibleLogicalRange(mainRange); else C.indicatorChart.timeScale().fitContent();
    if (C.chart) {
      var syncing = false;
      function syncRange(target, range) { if (!range || syncing) return; syncing = true; target.timeScale().setVisibleLogicalRange(range); syncing = false; }
      C.chart.timeScale().subscribeVisibleLogicalRangeChange(function (range) { syncRange(C.indicatorChart, range); });
      C.indicatorChart.timeScale().subscribeVisibleLogicalRangeChange(function (range) { syncRange(C.chart, range); });
      C.indicatorChart.subscribeCrosshairMove(function (param) {
        if (C._syncX || !C.chart || !C.series || param.time == null || !param.point) return;
        var subPrice = C.indicatorSeries ? C.indicatorSeries.coordinateToPrice(param.point.y) : null;
        if (subPrice == null) return;
        C._syncX = true;
        try { C.chart.setCrosshairPosition(subPrice, param.time, C.series); } catch (error) { reportTerminalIssue('crosshair-main-sync', error); }
        C._syncX = false;
      });
    }
  }
  // 富十字线 tooltip：时间/价格/涨跌额/涨跌幅/均价/分钟量额/累计量额/今开高低
  function intradayTooltipHtml(row, cumVol, cumAmt, today, prevClose) {
    var close = Number(row.close), pc = finite(prevClose) ? Number(prevClose) : null;
    var chg = pc != null ? close - pc : null, chgPct = pc != null ? chg / pc * 100 : null;
    var cls = chg == null ? '' : (chg >= 0 ? 'up' : 'down');
    var sign = chg != null && chg >= 0 ? '+' : '';
    var s = '<div class="t">' + escapeHtml(String(row.time)) + '</div>';
    s += '<div class="r"><span class="k">价格</span><span class="v ' + cls + '">' + n(close,2) + (chg != null ? ' <b class="' + cls + '">' + sign + n(chg,2) + ' / ' + sign + n(chgPct,2) + '%</b>' : '') + '</span></div>';
    s += '<div class="r"><span class="k">均价</span><span class="v">' + n(Number(row.vwap),3) + '</span></div>';
    s += '<div class="r"><span class="k">分钟量 / 额</span><span class="v">' + n(Math.round((Number(row.volume) || 0) / 100),0) + '手 · ' + n((Number(row.amount) || 0) / 10000,2) + '万</span></div>';
    s += '<div class="r"><span class="k">累计量 / 额</span><span class="v">' + n(Math.round((cumVol || 0) / 100),0) + '手 · ' + n((cumAmt || 0) / 10000,2) + '万</span></div>';
    if (today) s += '<div class="r"><span class="k">今开 / 高 / 低</span><span class="v">' + n(today.open,2) + ' / ' + n(today.high,2) + ' / ' + n(today.low,2) + '</span></div>';
    return s;
  }
  function renderIntradayTooltip(row, idx, point, cumVols, cumAmts, today, prevClose) {
    var hud = $('terminalCrosshair');
    if (!hud) return;
    hud.classList.add('itc-crosshair-rich');
    hud.innerHTML = intradayTooltipHtml(row, cumVols[idx], cumAmts[idx], today, prevClose);
    // 用 DOM 容器尺寸定位（不同图表库构建的 clientWidth()/clientHeight() 方法存在差异，不依赖它们）
    var holder = $('terminalChart');
    var w = holder ? (holder.clientWidth || 800) : 800;
    var h = holder ? (holder.clientHeight || 400) : 400;
    if (point) {
      hud.style.left = clamp(point.x + 14, 8, Math.max(8, w - 260)) + 'px';
      hud.style.top = clamp(point.y - 70, 6, Math.max(6, h - 200)) + 'px';
    } else {
      hud.style.left = '10px'; hud.style.top = '8px';
    }
  }
  function resetCrosshairHud() {
    var hud = $('terminalCrosshair');
    if (!hud) return;
    hud.classList.remove('itc-crosshair-rich');
    hud.style.left = '10px'; hud.style.top = '8px';
  }
  function renderChart() {
    syncAdjustLabel();
    var container = $('terminalChart'), intraday = C.chartType === 'intraday', candles = intraday ? C.intraday : (C.current || []);
    var previousRange = C.chart ? C.chart.timeScale().getVisibleLogicalRange() : null;
    // 默认保持用户当前的缩放/平移位置；只有切换周期/加载新股时 fitOnRender 才置 true。
    var shouldFit = C.fitOnRender || !previousRange;
    C.fitOnRender = false;
    if (C.chart) { try { C.chart.remove(); } catch (error) { reportTerminalIssue('chart-dispose', error); } C.chart = null; }
    if (C.indicatorChart) { try { C.indicatorChart.remove(); } catch (error) { reportTerminalIssue('indicator-dispose', error); } C.indicatorChart = null; }
    if (!candles.length || !window.LightweightCharts) { container.innerHTML = '<div class="itc-empty" style="padding:30px">' + (!window.LightweightCharts ? '图表运行时未加载。' : '该周期暂无可用K线。') + '</div>'; $('terminalIndicatorChart').innerHTML = '<div class="itc-empty" style="padding:16px">当前无可计算副图数据。</div>'; return; }
    if ($('terminalCrosshair')) $('terminalCrosshair').hidden = false;
    C.indicatorData = calculateIndicators(candles);
    container.innerHTML = '';
    var series, prevClose = intraday ? (finite(C.intradayMeta.previous_close) ? Number(C.intradayMeta.previous_close) : null) : null;
    var priceFmt = function (value) { return n(value, 2); };
    if (intraday) priceFmt = finite(prevClose) ? intradayAxisFormatter(prevClose) : priceFmt;
    C.chart = createCompatibleChart(container, { width:container.clientWidth, height:container.clientHeight, layout:{attributionLogo:true,background:{type:'solid',color:'#0a0d12'},textColor:'#8B8F9A'},grid:{vertLines:{color:'rgba(255,255,255,0.04)'},horzLines:{color:'rgba(255,255,255,0.04)'}},crosshair:{mode:LightweightCharts.CrosshairMode.Normal},localization:{locale:'zh-CN',priceFormatter:priceFmt},rightPriceScale:{borderColor:'rgba(255,255,255,0.08)',scaleMargins:{top:.09,bottom:.07}},timeScale:{borderColor:'rgba(255,255,255,0.08)',timeVisible:intraday || (C.timeframe !== '1d' && C.timeframe !== '1w' && C.timeframe !== '1mo' && C.timeframe !== '1q' && C.timeframe !== '1y'),secondsVisible:false,rightOffset:intraday ? 3 : 6,barSpacing:intraday ? 5 : 7,minBarSpacing:1,fixLeftEdge:true} });
    if (intraday) {
      series = buildIntradayChart(C.chart, candles, prevClose, C.intradayMeta.board_limit_pct || boardLimitPct(C.code));
    } else {
      series = C.chart.addCandlestickSeries({upColor:'#F6465D',downColor:'#2EBD85',borderUpColor:'#F6465D',borderDownColor:'#2EBD85',wickUpColor:'#F6465D',wickDownColor:'#2EBD85'});
      series.setData(candles.map(function (row) { return {time:timeValue(row.time),open:row.open,high:row.high,low:row.low,close:row.close}; }));
      // 今日日K加涨跌停参考线（锚定昨日收盘）；历史K线不画，避免误导
      if (C.timeframe === '1d' && candles.length >= 2 && marketSessionDate() && String(candles[candles.length - 1].time).slice(0, 10) === marketSessionDate()) {
        var dPc = Number(candles[candles.length - 2].close);
        if (finite(dPc) && dPc > 0) {
          var dPct = C.chartMeta.board_limit_pct || boardLimitPct(C.code);
          series.createPriceLine({ price: dPc * (1 + dPct / 100), color:'#F6465D', lineWidth:1, lineStyle:2, title:'涨停 ' + n(dPc * (1 + dPct / 100),2), axisLabelVisible:true });
          series.createPriceLine({ price: dPc * (1 - dPct / 100), color:'#2EBD85', lineWidth:1, lineStyle:2, title:'跌停 ' + n(dPc * (1 - dPct / 100),2), axisLabelVisible:true });
        }
      }
    }
    C.series = series;
    if (!intraday) {
      drawOverlays(C.chart, series, candles);
      if (C.overlays.signals) { var signals = researchSignals().slice(-8); setSeriesMarkersCompatible(series, signals.map(function (signal) { var bearish = researchSignalIsBearish(signal); return { time:timeValue(signal.time), position:bearish ? 'aboveBar' : 'belowBar', color:bearish ? '#F6465D' : '#FFB800', shape:bearish ? 'arrowDown' : 'arrowUp', text:signal.label || '技术观察' }; }).filter(function (marker) { return candles.some(function (row) { return timeKey(row.time) === timeKey(marker.time); }); })); }
    } else C.manualLines.forEach(function (item) { series.createPriceLine({ price:item.price, color:lineColor(item.kind), lineWidth:2, lineStyle:0, title:item.kind === 'support' ? '我的止跌参考' : '我的遇阻参考', axisLabelVisible:true }); });
    renderDrawings(C.chart, series, candles);
    // 分时图预计算累计量/额，供富十字线 tooltip 使用（对 Tencent 与 AKShare 源通用）
    var cumVols = [], cumAmts = [], cumV = 0, cumA = 0;
    if (intraday) candles.forEach(function (row) { cumV += Number(row.volume) || 0; cumA += Number(row.amount) || 0; cumVols.push(cumV); cumAmts.push(cumA); });
    C.chart.subscribeClick(onChartClick);
    C.chart.subscribeCrosshairMove(function (param) {
      // 分时图 param.time 是墙上时钟 unix 秒（与 timeValue(row.time) 直接相等比较）；
      // 日K param.time 是 BusinessDay 对象，必须用 timeKey 归一比较
      var idx = -1;
      if (param.time == null) idx = candles.length - 1;
      else idx = intraday ? candles.findIndex(function (item) { return timeValue(item.time) === Number(param.time); }) : candles.findIndex(function (item) { return timeKey(item.time) === timeKey(param.time); });
      if (idx < 0 || idx >= candles.length) idx = candles.length - 1;
      var row = candles[idx];
      if (!row) return;
      if (intraday) {
        renderIntradayTooltip(row, idx, param.point, cumVols, cumAmts, C.intradayMeta.today, prevClose);
      } else {
        var hud = $('terminalCrosshair');
        resetCrosshairHud();
        hud.textContent = String(row.time) + '  开 ' + n(row.open,2) + '  高 ' + n(row.high,2) + '  低 ' + n(row.low,2) + '  收 ' + n(row.close,2) + '  量 ' + n(row.volume,0);
        var iv = indicatorValuesAt(C.indicatorPanel, C.indicatorData, candles, param.time);
        if (iv) hud.textContent += '  |  ' + iv;
      }
      // 主图十字线同步到副图（_syncX 防回环）
      if (C._syncX || !C.indicatorChart || !C.indicatorSeries || param.time == null || !param.point) return;
      var mainPrice = series.coordinateToPrice(param.point.y);
      if (mainPrice == null) return;
      C._syncX = true;
      try { C.indicatorChart.setCrosshairPosition(mainPrice, param.time, C.indicatorSeries); } catch (error) { reportTerminalIssue('crosshair-indicator-sync', error); }
      C._syncX = false;
    });
    if (!shouldFit && previousRange) C.chart.timeScale().setVisibleLogicalRange(previousRange);
    else if (!intraday) {
      var battleBars = Number((((C.decisionMap || {}).presentation || {}).chartLookbackBars || 60));
      if (candles.length > battleBars + 4) C.chart.timeScale().setVisibleLogicalRange({from:candles.length - battleBars,to:candles.length + 5});
      else C.chart.timeScale().fitContent();
    }
    else C.chart.timeScale().fitContent();
    renderVerticalDrawings(C.chart);
    if (C.chart.timeScale && C.chart.timeScale().subscribeVisibleLogicalRangeChange) C.chart.timeScale().subscribeVisibleLogicalRangeChange(repositionVerticalGuides);
    // 分时主图=价+量一体：副图固定渲染分时量能面板（快速指标按钮禁用）；
    // 切回日K/周期恢复多指标副图
    var indicatorWrap = $('terminalIndicatorChart'), indicatorLabel = $('terminalIndicatorLabel');
    if (indicatorWrap) indicatorWrap.style.display = '';
    if (indicatorLabel) indicatorLabel.style.display = '';
    if (intraday) {
      root.querySelectorAll('[data-quick-indicator]').forEach(function (b) { b.disabled = true; b.title = '分时图副图固定为成交量'; });
      renderIntradayVolume(candles, prevClose);
    } else {
      root.querySelectorAll('[data-quick-indicator]').forEach(function (b) { b.disabled = false; b.title = ''; });
      renderIndicatorChart(candles);
      if (C.indicatorChart) {
        var syncing = false;
        function syncRange(target, range) { if (!range || syncing) return; syncing = true; target.timeScale().setVisibleLogicalRange(range); syncing = false; }
        C.chart.timeScale().subscribeVisibleLogicalRangeChange(function (range) { syncRange(C.indicatorChart, range); });
        C.indicatorChart.timeScale().subscribeVisibleLogicalRangeChange(function (range) { syncRange(C.chart, range); });
        syncRange(C.indicatorChart, C.chart.timeScale().getVisibleLogicalRange());
      }
    }
    var meta = intraday ? C.intradayMeta : C.chartMeta, market = meta.market || (((C.indicators || {}).meta || {}).market || {}), last = candles[candles.length - 1];
    if (intraday) renderIntradayTooltip(last, candles.length - 1, null, cumVols, cumAmts, meta.today, prevClose);
    else { var hud2 = $('terminalCrosshair'); resetCrosshairHud(); hud2.textContent = String(last.time) + '  开 ' + n(last.open,2) + '  高 ' + n(last.high,2) + '  低 ' + n(last.low,2) + '  收 ' + n(last.close,2) + '  量 ' + n(last.volume,0); }
    var prevCloseNote = (intraday && !finite(meta.previous_close) && meta.previous_close_reason) ? '<span class="c-bear">' + escapeHtml(String(meta.previous_close_reason)) + '</span>' : '';
    var limitNote = (intraday && finite(meta.limit_up_price)) ? ' · <b class="itc-up">涨停 ' + n(meta.limit_up_price,2) + '</b> · <b class="itc-down">跌停 ' + n(meta.limit_down_price,2) + '</b>' : '';
    var adjustLabel = chartPriceLabel();
    chartNotice(intraday ? '<span><b>真实分时</b> · 数据截至 <b>' + escapeHtml(String(last.time)) + '</b></span><span>红涨/绿跌 · 黄线=均价 · 灰线=昨收' + limitNote + '</span><span>' + escapeHtml(market.label || '市场状态未提供') + '</span>' + prevCloseNote : '<span>周期 <b>' + LABELS[C.timeframe] + '</b> · K线截至 <b>' + escapeHtml(String(last.time)) + '</b></span><span>' + escapeHtml(meta.bar_status || '已完成K线') + ' · ' + escapeHtml(market.label || '市场状态未提供') + ' · 价格口径：' + adjustLabel + '</span>');
    renderEvidence();
    $('terminalChartAccessible').textContent = '当前' + (intraday ? '真实分时（含分时量能面板与涨跌停参考线）' : LABELS[C.timeframe]) + '，K线截至' + String(last.time) + '。' + (intraday ? '副图为分时成交量。' : '副图为' + (($('terminalIndicatorLabel') || {}).textContent || '成交量') + '；') + 'Regis 量化作战图的价格坐标由同口径完成K线与明确门禁确定性计算，AI只解释和排序；场景不是自动交易指令。';
  }
  function updateToolState() {
    root.querySelectorAll('[data-draw-tool]').forEach(function (button) {
      var active = C.drawTool === button.dataset.drawTool;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    var hint = $('terminalDrawHint'); hint.hidden = !C.drawTool;
    if (C.drawTool) {
      var clicks = ['channel','parallel','fibExtension','longPosition','shortPosition'].indexOf(C.drawTool) >= 0 ? '三点' : (['trend','ray','fib','rectangle','measure'].indexOf(C.drawTool) >= 0 ? '两点' : '一次点击');
      hint.textContent = '点击图表：' + TOOL_NAMES[C.drawTool] + '（' + clicks + '锚定）' + (C.drawPending.length ? ' · 已选 ' + C.drawPending.length + ' 点' : '') + ' · 完成后可连续画，Esc 退出';
    }
    $('terminalLineHint').textContent = C.drawTool ? '请在K线图内点击。' : '例如：标记“可能止跌/遇阻”价格，或用水平/趋势/斐波那契/通道在图上做自己的研究标记。画线按股票+周期保存在本机浏览器。';
  }
  function clearChartPresentation(message) {
    ['chart','indicatorChart'].forEach(function (key) {
      if (C[key]) { try { C[key].remove(); } catch (error) { reportTerminalIssue(key + '-dispose', error); } C[key] = null; }
    });
    C.series = null; C.indicatorSeries = null;
    C.current = []; C.intraday = []; C.chartMeta = {adjust_type:'unknown'}; C.intradayMeta = {};
    C.indicatorData = null; C.autoLevels = {supports:[],resistances:[],fib:[]}; C.autoDrawings = [];
    C.aiDiagnostics = null; C.aiMeta = null; C.aiError = null; C.aiAnalyzedAt = null;
    C.aiFocus = null; C.aiScenarios = []; C.aiMarketContext = null; C.decisionMap = null;
    C.focusedAutoId = null; C.focusedScenarioRole = null; C.aiViewSeeded = false;
    resetCrosshairHud();
    if ($('terminalCrosshair')) $('terminalCrosshair').hidden = true;
    ['terminalCrosshair','terminalChartNote','terminalIndicatorChart','terminalIndicatorLabel','terminalChartAccessible'].forEach(function (id) {
      if ($(id)) $(id).textContent = message;
    });
    if ($('terminalDrawHint')) { $('terminalDrawHint').hidden = true; $('terminalDrawHint').textContent = ''; }
    $('terminalChart').innerHTML = '<div class="itc-empty" style="padding:30px">' + escapeHtml(message) + '</div>';
    renderStockSection(['terminalLevels'], renderLevels);
    renderStockSection(['terminalAiDiagnostics','terminalAiStatus'], renderAiDiagnostics);
    renderStockSection(['terminalAutoDrawingList'], renderAutoDrawingList);
    renderStockSection(['terminalTechnicals'], renderTechnical);
    renderStockSection(['terminalSummary'], renderSummary);
    renderStockSection(['terminalEvidence'], renderEvidence);
    renderStockSection(['terminalBattleState','terminalBattleEntryMeta','terminalBattleTargetMeta','terminalBattleRiskMeta'], renderBattleMap);
    renderStockSection(['terminalBattleMetrics','terminalStructureProtocol','terminalAiAnalysis','terminalAiAnalysisFlags'], renderBattleMetrics);
    renderStockSection([], syncAdjustLabel);
  }
  function setChartReadState(state) {
    C.chartState = state;
    if ($('terminalBattleBar')) $('terminalBattleBar').setAttribute('aria-busy', state === 'pending' ? 'true' : 'false');
    if ($('terminalCrosshair')) $('terminalCrosshair').hidden = true;
    if (state !== 'ready') ['terminalBattleEntry','terminalBattleTarget','terminalBattleRisk'].forEach(function (id) { if ($(id)) $(id).disabled = true; });
  }
  function renderCurrentChartData(showLoading) {
    var candles = C.chartType === 'intraday' ? C.intraday : C.current;
    setChartReadState(candles.length ? 'ready' : 'empty');
    if (!candles.length) { clearChartPresentation('本次查询没有当前周期K线，未执行结构分析；可切换周期或重新查询。'); return; }
    C.indicatorData = null; C.decisionMap = null; C.aiDiagnostics = null; C.aiAnalyzedAt = null;
    C.aiFocus = null; C.aiMeta = null; C.aiMarketContext = null; C.focusedAutoId = null; C.focusedScenarioRole = null; C.aiViewSeeded = false;
    C.autoDrawings = []; C.aiScenarios = []; C.autoLevels = {supports:[],resistances:[],fib:[]};
    renderStockSection(['terminalAiDiagnostics','terminalAiStatus'], function () { runAiAnalysis(candles, false); });
    var panel = root.querySelector('.itc-ai-panel');
    if (panel) panel.setAttribute('aria-busy', 'false');
    if ($('terminalAiRun')) $('terminalAiRun').disabled = false;
    renderStockSection(['terminalTechnicals','terminalIndicatorChart'], function () { C.indicatorData = calculateIndicators(candles); });
    C.fitOnRender = !!showLoading;
    renderStockSection(['terminalLevels'], renderLevels);
    renderStockSection(['terminalTechnicals'], renderTechnical);
    renderStockSection(['terminalTradePlan'], renderTradePlan);
    renderStockSection(['terminalSummary'], renderSummary);
    renderStockSection(['terminalEvidence'], renderEvidence);
    if (!renderStockSection(['terminalChart','terminalChartNote'], renderChart) && $('terminalCrosshair')) $('terminalCrosshair').hidden = true;
  }
  function beginChartRequest(showLoading) {
    C.chartRequestSequence = (C.chartRequestSequence || 0) + 1;
    var request = {sequence:C.chartRequestSequence,token:C.loadToken,code:C.code,frame:C.timeframe,type:C.chartType};
    setChartReadState('pending');
    if (showLoading) clearChartPresentation('加载 ' + (C.chartType === 'intraday' ? '真实分时' : LABELS[C.timeframe]) + ' 数据…');
    return request;
  }
  function chartRequestIsCurrent(request) {
    return request.sequence === C.chartRequestSequence && request.token === C.loadToken && request.code === C.code && request.frame === C.timeframe && request.type === C.chartType;
  }
  function failChartRequest(request, error) {
    if (!chartRequestIsCurrent(request)) return;
    setChartReadState('error');
    clearChartPresentation((request.type === 'intraday' ? '真实分时' : LABELS[request.frame]) + ' 数据不可用：' + error.message + '；可切换周期或重新查询。');
  }
  function dailyChartMeta() {
    return Object.assign({}, (C.indicators || {}).meta || {}, {source:((C.indicators || {}).meta || {}).snapshot_source || '来源待核验', adjust_type:((C.indicators || {}).meta || {}).adjust_type || (C.planMeta.decision_price_eligible === true ? 'raw' : 'unknown'), price_basis:C.planMeta.price_basis || (C.planMeta.decision_price_eligible === true ? 'audited_raw_unadjusted_close' : 'unverified'), decision_price_eligible:C.planMeta.decision_price_eligible === true, bar_status:'已完成K线'});
  }
  function setTimeframe(frame) {
    C.timeframe = frame;
    var timeframeTabs = $('terminalTimeframes');
    var selectedTimeframe = timeframeTabs && timeframeTabs.querySelector('[data-timeframe="' + frame + '"]');
    setRovingTabState(timeframeTabs, '[data-timeframe]', selectedTimeframe);
    // Snapshot refresh must preserve the user's selected chart mode.
    if (C.chartType === 'intraday') return loadIntraday();
    var request = beginChartRequest(true), code = request.code, source;
    C.manualLines = linesFromStorage(); C.drawings = drawingsFromStorage(); C.autoVisibility = autoVisibilityFromStorage(); C.drawPending = [];
    renderStockSection(['terminalLineList'], renderManualLines);
    renderStockSection(['terminalDrawingList'], renderDrawingList);
    if (frame === '1d' && C.planDaily.length) {
      C.chartMeta = dailyChartMeta(); C.daily = C.planDaily.slice(); source = Promise.resolve(C.daily);
    } else {
      var aggregateDaily = ['1w','1mo','1q','1y'].indexOf(frame) >= 0;
      var interval = aggregateDaily ? '1d' : (frame === '60m' ? '5m' : frame);
      var count = aggregateDaily ? 1200 : (frame === '1d' ? 400 : 540);
      source = api('/api/candles/' + code + '?interval=' + interval + '&count=' + count + '&live=1', 7000).then(function (data) {
        if (!chartRequestIsCurrent(request)) return null;
        C.chartMeta = data.meta || {};
        var rows = data.candles || [];
        if (!Array.isArray(rows)) throw new Error('K线数据格式无效');
        if (interval === '1d') C.daily = rows;
        return aggregateDaily || frame === '60m' ? aggregate(rows, frame) : rows;
      });
    }
    return source.then(function (candles) {
      if (!chartRequestIsCurrent(request) || candles == null) return;
      C.current = candles;
      C.manualLines = linesFromStorage(); C.drawings = drawingsFromStorage(); C.autoVisibility = autoVisibilityFromStorage();
      renderStockSection(['terminalLineList'], renderManualLines);
      renderStockSection(['terminalDrawingList'], renderDrawingList);
      renderCurrentChartData(true);
    }).catch(function (error) { failChartRequest(request, error); });
  }
  function refreshCurrentFrame(showLoading) {
    if (C.chartType === 'intraday' || ['1w','1mo','1q','1y'].indexOf(C.timeframe) >= 0) return Promise.resolve();
    var request = beginChartRequest(showLoading), frame = request.frame, code = request.code;
    if (frame === '1d' && C.planDaily.length) {
      C.chartMeta = dailyChartMeta(); C.current = C.planDaily.slice(); C.daily = C.current.slice();
      renderCurrentChartData(showLoading);
      return Promise.resolve();
    }
    var interval = frame === '60m' ? '5m' : frame;
    return api('/api/candles/' + code + '?interval=' + interval + '&count=' + (frame === '1d' ? '400' : '540') + '&live=1', 7000).then(function (data) {
      if (!chartRequestIsCurrent(request)) return;
      var rows = data.candles || [];
      if (!Array.isArray(rows)) throw new Error('K线数据格式无效');
      C.chartMeta = data.meta || {}; C.current = frame === '60m' ? aggregate(rows, '60m') : rows;
      if (frame === '1d') C.daily = C.current;
      renderCurrentChartData(showLoading);
    }).catch(function (error) {
      if (!chartRequestIsCurrent(request)) return;
      failChartRequest(request, error); throw error;
    });
  }
  function loadIntraday() {
    return refreshIntraday(true).catch(function () { /* Current failures are cleared by refreshIntraday. */ });
  }
  function refreshIntraday(showLoading) {
    var request = beginChartRequest(showLoading);
    return api('/api/candles/' + request.code + '/intraday', 7000).then(function (data) {
      if (!chartRequestIsCurrent(request) || C.chartType !== 'intraday') return;
      C.intraday = data.candles || []; C.intradayMeta = data || {};
      if (!Array.isArray(C.intraday)) throw new Error('分时数据格式无效');
      renderCurrentChartData(showLoading);
    }).catch(function (error) {
      if (!chartRequestIsCurrent(request)) return;
      failChartRequest(request, error); throw error;
    });
  }
  function applyIndicatorParam(input) {
    var key = input.dataset.iparam, value = Number(input.value);
    if (!isFinite(value) || value <= 0) { input.value = String(/^ma[0-3]$/.test(key) ? (C.indParams.ma || [5,10,20,60])[Number(key.slice(2))] : C.indParams[key]); return; }
    if (/^ma[0-3]$/.test(key)) { C.indParams.ma = (C.indParams.ma || [5,10,20,60]).slice(); C.indParams.ma[Number(key.slice(2))] = Math.round(value); input.value = String(C.indParams.ma[Number(key.slice(2))]); }
    else { C.indParams[key] = Math.round(value); input.value = String(C.indParams[key]); }
    C.indicatorData = calculateIndicators(C.chartType === 'intraday' ? C.intraday : C.current);
    renderTechnical(); renderSummary(); renderChart();
  }
  function bind() {
    $('terminalSearch').addEventListener('submit', function (event) { event.preventDefault(); load($('terminalCode').value.trim()); });
    $('terminalWatchToggle').addEventListener('click', toggleWatchlist);
    $('terminalBattleBar').addEventListener('click', function (event) {
      var button = event.target.closest('[data-focus-scenario]');
      if (button && !button.disabled) focusDecisionScenario(button.dataset.focusScenario);
    });
    $('terminalAiRun').addEventListener('click', function () { runAiAnalysis(null, true); });
    $('terminalAiMode').addEventListener('change', function (event) { C.aiMode = event.target.value; C.autoVisibility = {}; runAiAnalysis(null, true); });
    bindRovingTabs($('terminalTimeframes'), '[data-timeframe]', function (button) { if (C.chartType === 'intraday') { C.chartType = 'candle'; $('terminalChartType').value = 'candle'; } setTimeframe(button.dataset.timeframe); });
    $('terminalChartType').addEventListener('change', function (event) {
      C.chartType = event.target.value; C.manualLines = linesFromStorage(); C.drawings = drawingsFromStorage(); C.autoVisibility = autoVisibilityFromStorage(); C.undoStack = []; C.redoStack = []; updateHistoryButtons(); renderManualLines(); renderDrawingList();
      if (C.chartType === 'intraday') loadIntraday(); else setTimeframe(C.timeframe);
    });
    root.querySelectorAll('[data-overlay]').forEach(function (input) { input.addEventListener('change', function () { C.overlays[input.dataset.overlay] = input.checked; renderChart(); }); });
    root.querySelectorAll('[data-volatility-layer]').forEach(function (input) { input.addEventListener('change', function () { if (input.checked) { C.volatilityMode = input.dataset.volatilityLayer; renderChart(); } }); });
    root.querySelectorAll('[data-iparam]').forEach(function (input) { input.addEventListener('change', function () { applyIndicatorParam(input); }); });
    $('terminalIndicatorToggle').addEventListener('click', function () { var menu = $('terminalIndicatorMenu'), open = menu.hidden; menu.hidden = !open; $('terminalIndicatorToggle').setAttribute('aria-expanded', open ? 'true' : 'false'); });
    root.querySelectorAll('input[name="terminal-indicator"]').forEach(function (input) { input.addEventListener('change', function () { if (input.checked && C.chartType !== 'intraday') { C.indicatorPanel = input.value; renderIndicatorChart(C.current); } }); });
    root.querySelectorAll('[data-quick-indicator]').forEach(function (button) { button.addEventListener('click', function () { if (C.chartType === 'intraday') return; C.indicatorPanel = button.dataset.quickIndicator; renderIndicatorChart(C.current); }); });
    $('terminalFocusToggle').addEventListener('click', function () {
      var active = root.classList.toggle('is-chart-focus'), button = $('terminalFocusToggle');
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.textContent = active ? '退出大图' : '大图看盘';
      renderChart();
    });
    $('terminalNoviceToggle').addEventListener('click', function () { C.novice = !C.novice; root.classList.toggle('is-novice', C.novice); $('terminalNoviceToggle').setAttribute('aria-pressed', C.novice ? 'true' : 'false'); $('terminalNoviceToggle').textContent = '新手说明：' + (C.novice ? '开' : '关'); });
    // 副图折叠：分时图初始形态要一屏放下（2026-08-14 用户反馈）。折叠时 display:none，
    // 展开后强制重算副图尺寸，避免画布停在 0 宽高。
    $('terminalIndicatorCollapse').addEventListener('click', function () {
      var el = $('terminalIndicatorChart'), btn = $('terminalIndicatorCollapse');
      var collapsed = el.classList.toggle('collapsed');
      $('terminalIndicatorLabel').hidden = collapsed;
      btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      btn.textContent = collapsed ? '展开副图 ▴' : '收起副图 ▾';
      if (!collapsed && C.indicatorChart) {
        C.indicatorChart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
        C.indicatorChart.timeScale().fitContent();
      }
    });
    root.querySelectorAll('[data-draw-tool]').forEach(function (button) { button.addEventListener('click', function () { var tool = button.dataset.drawTool; C.drawTool = C.drawTool === tool ? null : tool; C.drawPending = []; updateToolState(); }); });
    $('terminalMagnet').addEventListener('change', function (event) { C.magnet = event.target.checked; });
    $('terminalUndoDrawing').addEventListener('click', function () { restoreDrawingHistory(C.undoStack, C.redoStack); });
    $('terminalRedoDrawing').addEventListener('click', function () { restoreDrawingHistory(C.redoStack, C.undoStack); });
    $('terminalClearLines').addEventListener('click', function () { checkpointDrawings(); C.manualLines = []; saveLines(); C.drawings = []; saveDrawings(); C.drawPending = []; C.drawTool = null; updateToolState(); renderManualLines(); renderDrawingList(); renderEvidence(); renderChart(); });
    document.addEventListener('keydown', function (event) {
      var editing = event.target && event.target.closest && event.target.closest('input,textarea,select,[contenteditable="true"]');
      if (editing && (event.ctrlKey || event.metaKey) && ['z','y'].indexOf(event.key.toLowerCase()) >= 0) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); restoreDrawingHistory(event.shiftKey ? C.redoStack : C.undoStack, event.shiftKey ? C.undoStack : C.redoStack); return; }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') { event.preventDefault(); restoreDrawingHistory(C.redoStack, C.undoStack); return; }
      if (event.key === 'Escape') {
        if (C.focusedScenarioRole) { C.focusedScenarioRole = null; renderAutoDrawingList(); renderChart(); }
        // 第一次 Esc：取消已选未完成的点；第二次：退出工具。工具不再被强制关闭。
        if (C.drawPending.length) C.drawPending = [];
        else if (C.drawTool) C.drawTool = null;
        updateToolState();
        if (!$('terminalIndicatorMenu').hidden) { $('terminalIndicatorMenu').hidden = true; $('terminalIndicatorToggle').setAttribute('aria-expanded', 'false'); $('terminalIndicatorToggle').focus(); }
      }
    });
    document.addEventListener('visibilitychange', function () { if (!document.hidden) refreshVisibleData(); });
    window.addEventListener('resize', function () { if (C.chart) C.chart.applyOptions({width:$('terminalChart').clientWidth,height:$('terminalChart').clientHeight}); if (C.indicatorChart) C.indicatorChart.applyOptions({width:$('terminalIndicatorChart').clientWidth,height:$('terminalIndicatorChart').clientHeight}); if (C.radarChart) C.radarChart.resize(); });
  }
  function renderLoadedSnapshot() {
    renderStockSection(['terminalQuote','terminalSourceBoundary','terminalHealth'], renderQuote);
    renderStockSection(['terminalWatchlist'], renderWatchlist);
    renderStockSection(['terminalLineList'], renderManualLines);
    renderStockSection(['terminalDrawingList'], renderDrawingList);
    renderStockSection(['terminalFundamentals'], renderFundamentals);
    renderStockSection(['terminalTechnicals'], renderTechnical);
    renderStockSection(['terminalTradePlan'], renderTradePlan);
    renderStockSection(['terminalSignals'], renderSignals);
    renderStockSection(['terminalBook'], renderBook);
    renderStockSection(['terminalPrediction'], renderPrediction);
    renderStockSection(['terminalScoreHead','terminalScoreNote','terminalRadar'], renderScore);
    renderStockSection(['terminalEvidence','terminalLedger'], renderEvidence);
    renderStockSection(['terminalSummary','terminalSummaryHonesty'], renderSummary);
    renderStockSection(['terminalFunds'], renderFunds);
    // 用户已切到分时图时，不重新拉默认 K 线（否则会把分时视图弹回 K 线）；分时视图由 loadIntraday 独立渲染
    if (C.chartType !== 'intraday') renderStockSection(['terminalChart','terminalChartNote'], function () { setTimeframe(C.timeframe); });
  }
  function enrichDuringMarket(code, token) {
    api('/api/stock/' + code, 6000).then(function (live) {
      if (token !== C.loadToken || !live || !live.quote) return;
      C.enrichmentError = null;
      C.analysis = live; syncResearchData(live);
      renderQuote(); renderWatchlist(); renderTradePlan(); renderSignals(); renderBook(); renderPrediction(); renderScore(); renderEvidence(); renderSummary();
    }).catch(function (error) {
      if (token !== C.loadToken) return;
      C.enrichmentError = error && error.message ? error.message : '实时补充失败';
      reportTerminalIssue('live-enrichment', error);
      if (C.analysis) renderQuote();
    });
  }
  // ── 实时机会观察块：与今日机会同一时机引擎与同一门禁（后端 _gated_recommendation） ──
  var TIMING_LABELS = { volume_breakout:'放量突破', macd_golden_cross:'MACD金叉', rsi_rebound:'RSI超卖回升', vwap_support:'分时均线支撑', northbound:'北向流入', big_order_positive:'大单净买入转正' };
  function timingAction(a) {
    if (a === 'buy_now') return { t: '满足人工复核条件', c: 'var(--color-buy)' };
    if (a === 'avoid')   return { t: '不宜追高', c: 'var(--color-warning)' };
    return { t: '等待时机', c: 'var(--color-text-dim)' };
  }
  function timingChip(sig, name) {
    var state = (sig && sig.state) || 'miss';
    var cls = state === 'hit' ? 'tm-chip hit' : state === 'partial' ? 'tm-chip partial' : state === 'unavailable' ? 'tm-chip na' : 'tm-chip miss';
    var pctW = Math.round((sig.score || 0) * 100);
    return '<div class="' + cls + '" title="' + escapeAttr((sig && sig.detail) || '') + '"><b>' + escapeHtml(name) + '</b><span>' + pctW + '</span></div>';
  }
  function timingCountdown(sec) {
    if (!finite(sec) || sec < 0) return '—';
    var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }
  function renderTiming(timing, composite) {
    var el = $('terminalTiming');
    if (!el) return;
    if (C.timingTimer) { clearInterval(C.timingTimer); C.timingTimer = null; }
    var errorHtml = C.timingError ? '<div class="itc-empty">时机读取失败：' + escapeHtml(C.timingError) + (timing && timing.recommended_action ? '；保留上一份带时点的真实结果。' : '；本次没有可显示的时机结果。') + '</div>' : '';
    if (!timing || !timing.recommended_action) {
      el.innerHTML = errorHtml || '<div class="itc-empty">实时机会观察暂不可用（时机数据不足或计算失败，不影响其它面板）。</div>';
      return;
    }
    var act = timingAction(timing.recommended_action);
    var unifiedScore = C.scorePendingProfile ? null : (composite && finite(composite.composite)
      ? Number(composite.composite)
      : (finite(timing.unified_composite_score) ? Number(timing.unified_composite_score) : null));
    var evidence = timing.timing_evidence || {};
    var availableCount = Number(evidence.available_signal_count || 0);
    var hitCount = Array.isArray(evidence.hit_signals) ? evidence.hit_signals.length : 0;
    var partialCount = Array.isArray(evidence.partial_signals) ? evidence.partial_signals.length : 0;
    var w = timing.buy_window || {}, gate = timing.decision_gate || {}, regime = timing.market_regime || {}, cap = timing.trade_capacity || {};
    var chips = Object.keys(TIMING_LABELS).map(function (k) {
      var sig = (timing.signals || {})[k];
      return sig ? timingChip(sig, TIMING_LABELS[k]) : '<div class="tm-chip na"><b>' + escapeHtml(TIMING_LABELS[k]) + '</b><span>无</span></div>';
    }).join('');
    var countdown = (['actionable', 'best'].indexOf(w.phase) >= 0 && finite(w.countdown_seconds))
      ? '<div class="tm-row"><span>窗口剩余</span><b id="terminalTimingCount">' + timingCountdown(w.countdown_seconds) + '</b></div>' : '';
    if (countdown && C.code) {
      var start = Date.now(), base = w.countdown_seconds;
      C.timingTimer = setInterval(function () {
        var cd = $('terminalTimingCount');
        if (!cd) { clearInterval(C.timingTimer); C.timingTimer = null; return; }
        var left = base - Math.floor((Date.now() - start) / 1000);
        cd.textContent = timingCountdown(left);
        if (left <= 0) { clearInterval(C.timingTimer); C.timingTimer = null; }
      }, 1000);
    }
    var ffRow = '';
    if (timing.ff_net != null) ffRow = '<div class="tm-row"><span>第三方大单观察 <em>不参与统一评分、仓位或提醒</em></span><b class="' + colour(timing.ff_net) + '">' + n(timing.ff_net, 2) + ' 亿</b></div>';
    el.innerHTML = errorHtml +
      '<div class="tm-head">' +
        '<div class="tm-action" style="color:' + act.c + ';border-color:' + act.c + '55">' + act.t + '</div>' +
        '<div class="tm-score"><b>' + hitCount + '/' + availableCount + '</b><small>盘中买点条件命中</small></div>' +
        '<div class="tm-level">时机等级 · ' + escapeHtml(timing.level || '未形成') + (partialCount ? ' · 部分满足 ' + partialCount : '') + '</div>' +
      '</div>' + ffRow +
      '<div class="tm-row"><span>全站统一综合评分</span><b>' + (unifiedScore == null ? '暂不可用' : n(unifiedScore, 1) + ' / 100') + '</b></div>' +
      '<div class="tm-row"><span>条件数据覆盖</span><b>' + (finite(evidence.coverage_pct) ? n(evidence.coverage_pct, 1) + '%' : '未提供') + '</b></div>' +
      '<div class="tm-window"><span>买入窗口：</span><b>' + escapeHtml(w.label || w.phase || '—') + '</b><em>' + escapeHtml(w.note || '') + '</em></div>' + countdown +
      '<div class="tm-chips">' + chips + '</div>' +
      '<div class="tm-gates">' +
        '<div class="tm-row"><span>研究闸门</span><b>' + (gate.recommendation_eligible ? '通过' : '未过·fail-closed') + '</b></div>' +
        '<div class="tm-row"><span>账户暴露容量</span><b>' + (timing.account_access === false ? '账户读取关闭' : regime.allow_new_positions === true ? '可新增' : (regime.allow_new_positions === false ? '已达上限' : '待账户数据')) + '</b></div>' +
        '<div class="tm-row"><span>现行V8指数MA风险预算</span><b>' + (finite(regime.target_exposure_pct) ? regime.target_exposure_pct + '% · ' + ((regime.execution_policy_applied === true || regime.applied_to_execution === true) ? '已应用' : '未应用') : '不可用') + '</b></div>' +
        '<div class="tm-row"><span>新B/L/F/C/P研究状态</span><b>仪表盘查看 · 尚未应用</b></div>' +
        '<div class="tm-row"><span>新增风险容量</span><b>' + (timing.account_access === false ? '未计算' : cap.limit_reached ? '已达上限' : (finite(cap.open_positions) && finite(cap.max_open_positions) ? cap.open_positions + ' / ' + cap.max_open_positions + ' 仓' : '未计算')) + '</b></div>' +
      '</div>' +
      '<div class="tm-honesty">盘中条件只决定入场时机，不是第二个综合评分。' + escapeHtml(evidence.interpretation || timing.honesty || '它不是胜率、收益概率或最佳买点证明。') + '</div>';
  }
  function renderIntelligence(payload) {
    if (!payload) return;
    C.intel = payload; C.intelState = 'ready'; C.intelError = null; C.timingError = null;
    C.scorePendingProfile = !!(window.RegisAccess?.canWrite === true &&
      (payload.profile || {}).refresh_endpoint && !profileDecisionSourcesReady(payload.profile || {}) &&
      C.profileRefreshAttemptToken !== C.loadToken);
    renderStockSection(['terminalProfileTabs','terminalProfilePanes','terminalProfileHonesty'], function () { renderProfile(payload.profile || {}); });
    renderStockSection(['terminalScoreHead','terminalScoreNote','terminalRadar'], renderScore);
    renderStockSection(['terminalTiming'], function () { renderTiming(payload.timing || null, payload.composite || null); });
    C.lastTimingRefresh = Date.now();
    renderStockSection(['terminalTradePlan'], renderTradePlan);
    renderStockSection(['terminalSummary','terminalSummaryHonesty'], renderSummary);
    renderStockSection(['terminalFunds'], renderFunds);
    renderStockSection(['terminalFundamentals'], renderFundamentals);
    renderStockSection(['terminalEvidence','terminalLedger'], renderEvidence);
    if (C.current && C.current.length) { renderStockSection(['terminalAiDiagnostics','terminalChart'], function () { runAiAnalysis(C.current, false); renderChart(); }); }
    refreshCompanyProfileIfNeeded(payload.profile || {});
  }
  function profileDecisionSourcesReady(profile) {
    var sources = (profile || {}).data_sources || {};
    return ['financials', 'balance_sheet', 'business', 'pledge', 'announcements'].every(function (key) {
      return sources[key] && sources[key].status === 'available';
    });
  }
  function finishProfileRefresh(token) {
    if (token !== C.loadToken) return;
    clearTimeout(C.profileRefreshTimer); C.profileRefreshTimer = null;
    C.scorePendingProfile = false;
    var intel = C.intel || {};
    renderComposite(intel.composite || {}, intel.veto || {}, intel.radar || {});
    renderTiming(intel.timing || null, intel.composite || null);
  }
  function reloadIntelligenceAfterProfileRefresh(code, token) {
    if (token !== C.loadToken) return;
    finishProfileRefresh(token);
    api('/api/stock/' + code + '/intelligence?account_access=false', 25000).then(function (intel) {
      if (token !== C.loadToken) return;
      renderIntelligence((intel && intel.data) || {});
    }).catch(function (error) {
      if (token === C.loadToken) {
        var profile = (C.intel || {}).profile || {};
        renderProfile(Object.assign({}, profile, {status:'error', error:'档案资料重载失败，请重新载入个股重试。'}));
        reportTerminalIssue('profile-intelligence-reload', error);
      }
    });
  }
  function refreshCompanyProfileIfNeeded(profile) {
    if (window.RegisAccess?.canWrite !== true) return;
    if (!profile || !profile.refresh_endpoint || C.profileRefreshAttemptToken === C.loadToken) return;
    var token = C.loadToken, code = C.code, attempts = 0;
    C.profileRefreshAttemptToken = token;
    renderProfile(Object.assign({}, profile, {status:'building'}));
    postApi(profile.refresh_endpoint, 7000).then(function (result) {
      if (token !== C.loadToken) return;
      if (result.running !== true) throw new Error('公司档案刷新未被接受，请重新载入个股重试。');
      var statusEndpoint = result.status_endpoint || ('/api/company-profile/' + code + '/summary');
      var poll = function () {
        if (token !== C.loadToken) return;
        attempts += 1;
        api(statusEndpoint, 7000).then(function (summary) {
          if (token !== C.loadToken) return;
          if (profileDecisionSourcesReady(summary) || summary.refresh?.running === false || attempts >= 18) {
            reloadIntelligenceAfterProfileRefresh(code, token);
            return;
          }
          C.profileRefreshTimer = setTimeout(poll, 2500);
        }).catch(function (error) {
          if (token !== C.loadToken) return;
          if (attempts >= 18) {
            reportTerminalIssue('profile-refresh-status', error);
            reloadIntelligenceAfterProfileRefresh(code, token);
            return;
          }
          C.profileRefreshTimer = setTimeout(poll, 2500);
        });
      };
      C.profileRefreshTimer = setTimeout(poll, 1000);
    }).catch(function (error) {
      if (token === C.loadToken) {
        finishProfileRefresh(token);
        renderProfile(Object.assign({}, profile, {status:'error', error:error.message || '档案刷新失败'}));
        reportTerminalIssue('profile-refresh', error);
      }
    });
  }
  function clearStockPresentation(code) {
    ['chart','indicatorChart'].forEach(function (key) {
      if (C[key]) { try { C[key].remove(); } catch (error) { reportTerminalIssue(key + '-dispose', error); } C[key] = null; }
    });
    ['terminalCrosshair','terminalChartNote','terminalIndicatorChart','terminalIndicatorLabel','terminalChartAccessible'].forEach(function (id) {
      if ($(id)) $(id).textContent = code + ' · 等待当前股票图表数据';
    });
    if ($('terminalDrawHint')) { $('terminalDrawHint').hidden = true; $('terminalDrawHint').textContent = ''; }
    if ($('terminalAccountEntry')) $('terminalAccountEntry').href = '/?account=record&code=' + encodeURIComponent(code);
    if ($('terminalSourceBoundary')) $('terminalSourceBoundary').textContent = '正在核验 ' + code + ' 的来源与数据时点；不沿用上一只股票。';
    renderLevels(); renderSignals(); renderBook(); renderPrediction(); renderSummary(); renderFunds();
    syncAdjustLabel(); renderBattleMap(); resetCrosshairHud();
  }
  function settleIntelUnavailable() {
    renderStockSection(['terminalProfileTabs','terminalProfilePanes','terminalProfileHonesty'], function () {
      renderProfile({status:C.intelState === 'blocked' ? 'blocked' : 'error', error:intelUnavailableText(), dims:{}});
    });
    renderStockSection(['terminalFundamentals'], renderFundamentals);
    renderStockSection(['terminalScoreHead','terminalScoreNote','terminalRadar'], renderScore);
    renderStockSection(['terminalFunds'], renderFunds);
    renderStockSection(['terminalTiming'], function () { renderTiming(null); });
  }
  function settleSnapshotFailure(error) {
    var reason = error && error.message ? error.message : '主数据读取失败';
    C.snapshotState = 'error'; C.intelState = 'blocked'; C.intelError = null;
    // Finishing the request must not depend on any chart renderer succeeding.
    if ($('terminalBattleBar')) $('terminalBattleBar').setAttribute('aria-busy', 'false');
    reportTerminalIssue('terminal-snapshot', error);
    $('terminalHealth').className = 'itc-status bad'; $('terminalHealth').textContent = '主数据读取失败：' + reason + '；重新查询当前股票可重试。';
    $('terminalChart').textContent = '没有可用已审计数据：' + reason;
    $('terminalTradePlan').textContent = '买卖点研究不可用：' + reason;
    ['terminalCrosshair','terminalChartNote','terminalIndicatorChart','terminalIndicatorLabel','terminalChartAccessible','terminalSourceBoundary'].forEach(function (id) {
      if ($(id)) $(id).textContent = C.code + ' · 本次主数据未取得，来源和图表不可用。';
    });
    C.aiError = '主数据未取得，本次结构分析未执行。';
    renderStockSection(['terminalAiDiagnostics','terminalAiStatus','terminalAiHeadline','terminalAiScope'], renderAiDiagnostics);
    renderStockSection(['terminalBattleState','terminalBattleEntryMeta','terminalBattleTargetMeta','terminalBattleRiskMeta'], renderBattleMap);
    renderStockSection(['terminalBattleMetrics','terminalStructureProtocol','terminalAiAnalysis','terminalAiAnalysisFlags'], renderBattleMetrics);
    settleIntelUnavailable();
    renderStockSection(['terminalSignals'], renderSignals);
    renderStockSection(['terminalBook','terminalBookLevel'], renderBook);
    renderStockSection(['terminalPrediction','terminalPredictionStatus'], renderPrediction);
    renderStockSection(['terminalSummary','terminalSummaryHonesty'], renderSummary);
    renderStockSection(['terminalEvidence','terminalLedger'], renderEvidence);
  }
  function load(code) {
    if (!/^\d{6}$/.test(code)) { $('terminalHealth').className = 'itc-status bad'; $('terminalHealth').innerHTML = '数据健康：<b>请输入6位沪深股票代码</b>'; return; }
    var stockChanged = C.code !== code;
    C.code = code; C.loadToken += 1; var token = C.loadToken; C.researchSignals = []; C.quant = null; $('terminalCode').value = code;
    // Keep the explicit ETF history shortcut during the first script startup.
    // Later global selections return the history panel to the selected stock.
    var historyShortcut = (C.loadToken === 1 || !stockChanged) && document.getElementById('localResearchHistory')
      && new URLSearchParams(window.location.search).get('history_code') === '510300';
    var terminalMode = new URLSearchParams(window.location.search).get('view') === 'terminal';
    var historyAnchor = window.location.hash === '#localResearchHistory' ? '#localResearchHistory' : '';
    if (window.history && window.history.replaceState) window.history.replaceState(null, '', '/stock?code=' + encodeURIComponent(code) + (historyShortcut ? '&history_code=510300' : '') + (terminalMode ? '&view=terminal' : '') + historyAnchor);
    if (stockChanged && document.getElementById('localResearchHistory') && typeof window.CustomEvent === 'function') {
      window.dispatchEvent(new window.CustomEvent('regis-stock-selected', {detail:{code:code}}));
    }
    clearTimeout(C.refreshTimer); clearTimeout(C.profileRefreshTimer); if (C.timingTimer) clearInterval(C.timingTimer);
    C.snapshotState = 'pending'; C.intelState = 'waiting'; C.intelError = null;
    C.chartState = 'idle'; if ($('terminalCrosshair')) $('terminalCrosshair').hidden = true;
    C.analysis = null; C.indicators = null; C.daily = []; C.planDaily = []; C.planMeta = {}; C.current = []; C.intraday = []; C.chartMeta = {}; C.intradayMeta = {}; C.indicatorData = null; C.intel = null; C.scorePendingProfile = false; C.lastTimingRefresh = 0; C.profileRefreshTimer = null; C.profileRefreshAttemptToken = null; C.position = null; C.positionLoaded = false; C.positionError = null; C.portfolioSummary = null; C.lastPositionRefresh = 0; C.strategyRoute = null; C.strategyRouteError = null; C.lastStrategyRouteRefresh = 0; C.refreshBusy = false; C.quoteRefreshError = null; C.timingError = null; C.enrichmentError = null; C.storageErrors = []; C.aiDiagnostics = null; C.aiMeta = null; C.aiError = null; C.aiAnalyzedAt = null; C.aiFocus = null; C.aiScenarios = []; C.aiMarketContext = null; C.decisionMap = null; C.focusedAutoId = null; C.focusedScenarioRole = null; C.aiViewSeeded = false;
    C.manualLines = linesFromStorage(); C.drawings = drawingsFromStorage(); C.autoVisibility = autoVisibilityFromStorage(); C.drawPending = []; C.autoDrawings = []; C.aiMarketContext = null; C.autoLevels = {supports:[],resistances:[],fib:[]}; C.undoStack = []; C.redoStack = [];
    renderStockSection([], updateHistoryButtons);
    renderStockSection(['terminalAiDiagnostics','terminalAiStatus'], renderAiDiagnostics);
    renderStockSection(['terminalAutoDrawingList'], renderAutoDrawingList);
    if (C.radarChart) { try { C.radarChart.dispose(); } catch (error) { reportTerminalIssue('radar-dispose', error); } C.radarChart = null; }
    $('terminalHealth').className = 'itc-status'; $('terminalHealth').innerHTML = '数据健康：<b>正在请求真实数据</b>';
    $('terminalQuote').innerHTML = '';
    renderStockSection(['terminalSourceBoundary','terminalChartNote'], function () { clearStockPresentation(code); });
    if ($('terminalBookLevel')) $('terminalBookLevel').textContent = '级别待核验';
    if ($('terminalPredictionStatus')) $('terminalPredictionStatus').textContent = '等待模型快照';
    renderStockSection(['terminalProfileTabs','terminalProfilePanes','terminalProfileHonesty'], function () { renderProfile({status:'loading', dims:{}}); });
    renderStockSection(['terminalFundamentals'], renderFundamentals);
    renderStockSection(['terminalScoreHead','terminalScoreNote','terminalRadar'], function () { renderComposite({}, {}, {}); renderScore(); });
    renderStockSection(['terminalEvidence','terminalLedger'], renderEvidence);
    $('terminalTradePlan').innerHTML = '<div class="itc-plan-loading"><b>正在载入该股票的已审计计划</b><span>旧股票状态已清空，不会跨股票复用。</span></div>';
    $('terminalChart').innerHTML = '<div class="itc-empty" style="padding:30px">正在载入 ' + escapeHtml(code) + ' 的K线…</div>';
    renderStockSection(['terminalTiming'], function () { renderTiming(null); });
    var sessionRequest = refreshMarketSession();
    api('/api/stock/' + code + '/terminal-snapshot', 12000).then(function (snapshot) {
      if (token !== C.loadToken) return;
      if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot) || (snapshot.candles != null && !Array.isArray(snapshot.candles))) throw new Error('主数据响应格式无效');
      C.analysis = snapshot; C.snapshotState = 'ready'; C.intelState = 'pending';
      renderStockSection(['terminalSignals'], function () { syncResearchData(snapshot); });
      C.indicators = { candles:snapshot.candles || [], technical:snapshot.technical || {}, meta:snapshot.meta || {} };
      C.planDaily = C.indicators.candles.slice(); C.planMeta = snapshot.execution_plan || {};
      C.daily = C.planDaily.slice();
      renderLoadedSnapshot();
      refreshPositionIfDue(true);
      refreshStrategyRouteIfDue(true);
      // The local audited daily snapshot is authoritative while the exchange
      // is closed.  A public quote refresh can briefly expose a stale or
      // internally inconsistent row, so never let it overwrite the frozen
      // close/K-line pair outside a verified open session.
      sessionRequest.then(function (session) {
        if (token === C.loadToken && session && session.is_open === true) {
          enrichDuringMarket(code, token);
        }
      });
      sessionRequest.finally(scheduleRefresh);
      // 投研聚合：七维画像 / 综合评分 / 一票否决 / 9段式总结（独立请求，失败不影响主图）
      api('/api/stock/' + code + '/intelligence?account_access=false', 25000).then(function (intel) {
        if (token !== C.loadToken) return;
        var d = (intel && intel.data) || {};
        renderIntelligence(d);
      }).catch(function (error) {
        if (token !== C.loadToken) return;
        if (C.intelState === 'ready') { reportTerminalIssue('intelligence-display', error); return; }
        reportTerminalIssue('intelligence', error);
        C.intelState = 'error'; C.intelError = error && error.message ? error.message : '投研聚合请求失败'; C.timingError = C.intelError;
        settleIntelUnavailable();
        renderStockSection(['terminalEvidence','terminalLedger'], renderEvidence);
      });
    }).catch(function (error) {
      if (token !== C.loadToken) return;
      if (C.snapshotState === 'ready') { reportTerminalIssue('snapshot-display', error); return; }
      settleSnapshotFailure(error);
    });
  }
  bind(); syncIndicatorControls(); updateToolState(); updateHistoryButtons(); load(C.code);
  window.InstitutionalConsoleTest = { aggregate:aggregate, deriveLevels:deriveLevels, calculateIndicators:calculateIndicators, deriveAutoDrawings:deriveAutoDrawings, buildTradePlan:buildTradePlan, timeValue:timeValue };
})();
