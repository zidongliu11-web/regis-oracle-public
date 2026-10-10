const $ = (selector) => document.querySelector(selector);
let currentCode = null;
let currentInterval = '5m';
let currentResult = null;
let chart = null;
let chartCandleSeries = null;
let chartVolumeSeries = null;
let chartEma5Series = null;
let chartEma20Series = null;
let chartCandles = [];
let resizeObserver = null;
let toastTimer = null;
let liveQuoteTimer = null;
let fullReconcileTimer = null;
let liveQuoteRequestRunning = false;

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]
  ));
}
function money(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(3) : '--';
}
function readableTime(value) {
  if (!value) return '暂无';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? esc(value)
    : parsed.toLocaleString('zh-CN', {hour12: false});
}
function signedPct(value) {
  if (!Number.isFinite(Number(value))) return '<span class="flat">--</span>';
  const number = Number(value);
  const cls = number > 0 ? 'up' : number < 0 ? 'down' : 'flat';
  return `<span class="${cls}">${number > 0 ? '+' : ''}${number.toFixed(2)}%</span>`;
}
function showToast(message, type = 'ok') {
  const toast = $('#toast');
  toast.textContent = message;
  toast.className = `toast show ${type === 'error' ? 'error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3200);
}
async function api(path, options) {
  const response = await fetch(path, options);
  let body = {};
  try { body = await response.json(); } catch {}
  if (!response.ok) throw new Error(body.error || '请求失败');
  return body;
}
function candleTime(value) {
  const raw = String(value);
  const normalized = raw.includes(' ')
    ? raw.replace(' ', 'T')
    : /^\d{4}-\d{2}-\d{2}$/.test(raw) ? `${raw}T00:00:00` : raw;
  const parsed = new Date(`${normalized}+08:00`);
  return Math.floor(parsed.getTime() / 1000);
}
function emaSeries(candles, period) {
  const alpha = 2 / (period + 1);
  let previous = null;
  return candles.map((item) => {
    previous = previous === null
      ? Number(item.close)
      : alpha * Number(item.close) + (1 - alpha) * previous;
    return {time: candleTime(item.time), value: previous};
  });
}
function clearChart() {
  if (resizeObserver) resizeObserver.disconnect();
  resizeObserver = null;
  if (chart) chart.remove();
  chart = null;
  chartCandleSeries = null;
  chartVolumeSeries = null;
  chartEma5Series = null;
  chartEma20Series = null;
  chartCandles = [];
  $('#analysisChart').innerHTML = '';
}
function drawChart(result) {
  clearChart();
  const container = $('#analysisChart');
  if (!window.LightweightCharts) {
    container.innerHTML = '<div class="chart-empty"><strong>K线组件不可用</strong><span>请检查本地图表资源。</span></div>';
    return;
  }
  chart = window.LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight,
    layout: {
      background: {type: window.LightweightCharts.ColorType.Solid, color: '#07101a'},
      textColor: '#8190a4',
      attributionLogo: true,
    },
    grid: {vertLines: {color: '#152131'}, horzLines: {color: '#152131'}},
    rightPriceScale: {borderColor: '#263449'},
    timeScale: {borderColor: '#263449', timeVisible: true, secondsVisible: false, rightOffset: 5},
    crosshair: {
      vertLine: {color: '#6f86a6', labelBackgroundColor: '#263b58'},
      horzLine: {color: '#6f86a6', labelBackgroundColor: '#263b58'},
    },
  });
  const candles = (result.candles || []).map((item) => ({
    time: candleTime(item.time),
    open: Number(item.open),
    high: Number(item.high),
    low: Number(item.low),
    close: Number(item.close),
    volume: Number(item.volume || 0),
  }));
  if (!candles.length) {
    chart.remove();
    chart = null;
    container.innerHTML = '<div class="chart-empty"><strong>暂无可核验K线</strong><span>系统不会用空数据生成研究买卖点。</span></div>';
    return;
  }
  chartCandles = candles;
  const candleSeries = chart.addSeries(window.LightweightCharts.CandlestickSeries, {
    upColor: '#ff5b63', downColor: '#20bd7a',
    borderUpColor: '#ff5b63', borderDownColor: '#20bd7a',
    wickUpColor: '#ff767d', wickDownColor: '#38cc8d',
  });
  chartCandleSeries = candleSeries;
  candleSeries.setData(candles.map((item) => ({
    time: item.time,
    open: item.open,
    high: item.high,
    low: item.low,
    close: item.close,
  })));
  const volume = chart.addSeries(window.LightweightCharts.HistogramSeries, {
    priceFormat: {type: 'volume'}, priceScaleId: '',
    lastValueVisible: false, priceLineVisible: false,
  });
  chartVolumeSeries = volume;
  volume.priceScale().applyOptions({scaleMargins: {top: 0.82, bottom: 0}});
  volume.setData((result.candles || []).map((item) => ({
    time: candleTime(item.time),
    value: Number(item.volume),
    color: Number(item.close) >= Number(item.open)
      ? 'rgba(255,91,99,.32)'
      : 'rgba(32,189,122,.32)',
  })));
  const ema5 = chart.addSeries(window.LightweightCharts.LineSeries, {
    color: '#f0b94a', lineWidth: 1, lastValueVisible: false, priceLineVisible: false,
  });
  const ema20 = chart.addSeries(window.LightweightCharts.LineSeries, {
    color: '#4f8cff', lineWidth: 1, lastValueVisible: false, priceLineVisible: false,
  });
  chartEma5Series = ema5;
  chartEma20Series = ema20;
  ema5.setData(emaSeries(result.candles || [], 5));
  ema20.setData(emaSeries(result.candles || [], 20));
  const decision = result.decision || {};
  const buyScenario = decision.buy_scenario || {};
  const sellScenario = decision.sell_scenario || {};
  const buyRange = Array.isArray(buyScenario.range)
    ? buyScenario.range : [];
  const executionAllowed = decision.execution_allowed === true;
  const levels = [
    [buyRange[0], executionAllowed ? '#ef4b52' : '#8190a4', executionAllowed ? '执行触发线' : '研究参考线·不可执行'],
    [sellScenario.protective_stop, '#35b57d', executionAllowed ? '保护失效线' : '研究失效参考'],
    [sellScenario.first_target, '#e8b84b', executionAllowed ? '第一复核目标' : '研究目标参考'],
  ].filter(([price]) => Number.isFinite(Number(price)));
  levels.forEach(([price, color, title]) => candleSeries.createPriceLine({
    price: Number(price), color, lineWidth: 1, lineStyle: 2,
    axisLabelVisible: true, title,
  }));
  if (window.LightweightCharts.createSeriesMarkers) {
    window.LightweightCharts.createSeriesMarkers(
      candleSeries,
      (result.signals || []).map((signal) => ({
        time: candleTime(signal.time),
        position: signal.action === 'research_buy' ? 'belowBar' : 'aboveBar',
        color: executionAllowed
          ? (signal.action === 'research_buy' ? '#f0b94a' : '#20bd7a')
          : '#8190a4',
        shape: signal.action === 'research_buy' ? 'arrowUp' : 'arrowDown',
        text: executionAllowed
          ? (signal.action === 'research_buy' ? '执行触发' : '风险触发')
          : (signal.action === 'research_buy' ? '研究触发·不可执行' : '风险观察'),
      })),
    );
  }
  chart.timeScale().fitContent();
  resizeObserver = new ResizeObserver(() => {
    if (chart) chart.applyOptions({
      width: container.clientWidth,
      height: container.clientHeight,
    });
  });
  resizeObserver.observe(container);
}
function intervalSeconds(interval = currentInterval) {
  return {'1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1d': 86400}[interval] || 300;
}
function emaLast(candles, period) {
  const alpha = 2 / (period + 1);
  let value = null;
  candles.forEach((item) => {
    value = value === null
      ? Number(item.close)
      : alpha * Number(item.close) + (1 - alpha) * value;
  });
  return value;
}
function updateQuoteHeader(quote) {
  $('#analysisPrice').textContent = money(quote.price);
  $('#analysisChange').innerHTML = signedPct(quote.change_pct);
  $('#analysisQuoteTime').textContent = readableTime(
    quote.exchange_data_time || quote.data_time,
  );
}
function applyLiveQuoteToChart(quote) {
  if (currentInterval === '1d') return;
  if (
    !chartCandleSeries
    || !chartCandles.length
    || !Number.isFinite(Number(quote.price))
    || !quote.exchange_data_time
  ) return;
  const tickTime = Math.floor(
    new Date(quote.exchange_data_time).getTime() / 1000,
  );
  if (!Number.isFinite(tickTime)) return;
  const bucketSeconds = intervalSeconds();
  const bucketTime = Math.ceil(tickTime / bucketSeconds) * bucketSeconds;
  const latest = chartCandles[chartCandles.length - 1];
  const price = Number(quote.price);
  if (bucketTime < latest.time) return;

  let liveBar;
  if (bucketTime === latest.time) {
    liveBar = {
      ...latest,
      high: Math.max(Number(latest.high), price),
      low: Math.min(Number(latest.low), price),
      close: price,
    };
    chartCandles[chartCandles.length - 1] = liveBar;
  } else {
    liveBar = {
      time: bucketTime,
      open: Number(latest.close),
      high: Math.max(Number(latest.close), price),
      low: Math.min(Number(latest.close), price),
      close: price,
      volume: 0,
    };
    chartCandles.push(liveBar);
    if (chartCandles.length > 260) chartCandles.shift();
  }
  chartCandleSeries.update({
    time: liveBar.time,
    open: liveBar.open,
    high: liveBar.high,
    low: liveBar.low,
    close: liveBar.close,
  });
  if (chartVolumeSeries) {
    chartVolumeSeries.update({
      time: liveBar.time,
      value: Number(liveBar.volume || 0),
      color: liveBar.close >= liveBar.open
        ? 'rgba(255,91,99,.32)'
        : 'rgba(32,189,122,.32)',
    });
  }
  if (chartEma5Series) {
    chartEma5Series.update({
      time: liveBar.time,
      value: emaLast(chartCandles, 5),
    });
  }
  if (chartEma20Series) {
    chartEma20Series.update({
      time: liveBar.time,
      value: emaLast(chartCandles, 20),
    });
  }
}
function liveLatencySeconds(quote) {
  if (!quote.exchange_data_time) return null;
  const exchangeAt = new Date(quote.exchange_data_time).getTime();
  if (!Number.isFinite(exchangeAt)) return null;
  return Math.max(0, Math.round((Date.now() - exchangeAt) / 1000));
}
function stopRealtimeLoops() {
  clearTimeout(liveQuoteTimer);
  clearTimeout(fullReconcileTimer);
  liveQuoteTimer = null;
  fullReconcileTimer = null;
}
function realtimeDelayMs() {
  const live = currentResult?.quote?.freshness === 'live';
  if (!live) return 60_000;
  if (currentInterval === '1d') return 60_000;
  return ['1m', '5m'].includes(currentInterval) ? 2_000 : 8_000;
}
function reconcileDelayMs() {
  const live = currentResult?.quote?.freshness === 'live';
  if (!live) return 60_000;
  if (currentInterval === '1d') return 60_000;
  return ['1m', '5m'].includes(currentInterval) ? 10_000 : 30_000;
}
function scheduleRealtimeLoops() {
  stopRealtimeLoops();
  liveQuoteTimer = setTimeout(pollLiveQuote, realtimeDelayMs());
  fullReconcileTimer = setTimeout(() => {
    loadAnalysis(currentCode, currentInterval, {background: true});
  }, reconcileDelayMs());
}
async function pollLiveQuote() {
  const requestedCode = currentCode;
  const requestedInterval = currentInterval;
  if (!currentResult || !requestedCode || liveQuoteRequestRunning) return;
  if (document.hidden) {
    liveQuoteTimer = setTimeout(pollLiveQuote, 10_000);
    return;
  }
  liveQuoteRequestRunning = true;
  try {
    const result = await api(
      `/api/analysis/${encodeURIComponent(requestedCode)}/live`,
    );
    if (
      requestedCode !== currentCode
      || requestedInterval !== currentInterval
    ) return;
    currentResult.quote = result.quote;
    updateQuoteHeader(result.quote);
    applyLiveQuoteToChart(result.quote);
    const isLive = result.market_session === 'open';
    const latency = liveLatencySeconds(result.quote);
    $('#analysisConnectionDot').className = `status-dot ${isLive ? 'live' : ''}`;
    $('#analysisConnection').textContent = isLive
      ? `低延迟行情已连接 · ${result.poll_seconds}秒更新`
      : '当前休市，已自动降低刷新频率';
    $('#liveStreamBadge').textContent = isLive
      ? (currentInterval === '1d' ? '日K定时校准' : 'K线实时绘制中')
      : '休市快照';
    $('#liveStreamBadge').className = `tag ${isLive ? 'live' : 'gray'}`;
    $('#liveUpdateMeta').textContent = isLive
      ? (currentInterval === '1d'
        ? `日K每60秒校准 · 最新报价时间 ${readableTime(result.quote.exchange_data_time)}`
        : `最新价约每${result.poll_seconds}秒更新 · 完整K线每${result.full_candle_reconcile_seconds}秒校准${latency === null ? '' : ` · 当前约${latency}秒延迟`}`)
      : `行情时间 ${readableTime(result.quote.exchange_data_time)}`;
  } catch (error) {
    $('#liveStreamBadge').textContent = '实时通道重试中';
    $('#liveStreamBadge').className = 'tag yellow';
    $('#liveUpdateMeta').textContent = `轻量行情暂不可用：${error.message}；完整K线仍会继续校准`;
  } finally {
    liveQuoteRequestRunning = false;
    clearTimeout(liveQuoteTimer);
    liveQuoteTimer = setTimeout(pollLiveQuote, realtimeDelayMs());
  }
}
function renderList(selector, items, empty) {
  $(selector).innerHTML = items.length
    ? items.map((item) => `<li>${esc(item)}</li>`).join('')
    : `<li>${esc(empty)}</li>`;
}
function kronosBlock(k) {
  if (!k || k.available !== true) {
    return k && k.message
      ? `<h3 class="quant-block-title">Kronos 预测 · 未覆盖</h3>
         <div class="empty-state compact">${esc(k.message)}</div>`
      : '';
  }
  const pct = (v) => v == null ? '--' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`;
  const r5 = Number(k.pred_ret_5d || 0);
  const r10 = Number(k.pred_ret_10d || 0);
  const r20 = Number(k.pred_ret_20d || 0);
  const r30 = Number(k.pred_ret_30d || 0);
  const dir = Number(k.direction || 0);
  const bull = r20 > 0;
  const dirCls = r20 > 0.02 ? 'pos' : r20 < -0.02 ? 'neg' : 'neu';
  const badge = r20 > 0.02 ? ['bull', '看多'] : r20 < -0.02 ? ['bear', '看空'] : ['neu', '中性'];
  const trackW = Math.max(3, Math.min(97, Math.round(dir * 100)));
  const trackCls = dir >= 0.5 ? 'var(--green, #2ecc71)' : 'var(--red, #e74c3c)';
  const metric = (label, value, cls) => `
    <div class="quant-metric">
      <span>${esc(label)}</span>
      <strong class="${cls || ''}">${esc(value)}</strong>
    </div>`;
  return `
    <div class="kronos-strategy-block">
      <div class="kronos-strategy-head">
        <b>Kronos 未来行情预测</b>
        <small>${esc(k.model || 'Kronos-small')} · 快照日 ${esc(k.snapshot_date || '--')}</small>
      </div>
      <div class="kronos-strategy-grid">
        ${metric('预测 5日收益', pct(k.pred_ret_5d), r5 > 0 ? 'kronos-dir-pos' : r5 < 0 ? 'kronos-dir-neg' : '')}
        ${metric('预测 10日收益', pct(k.pred_ret_10d), r10 > 0 ? 'kronos-dir-pos' : r10 < 0 ? 'kronos-dir-neg' : '')}
        ${metric('预测 20日收益', pct(k.pred_ret_20d), r20 > 0 ? 'kronos-dir-pos' : r20 < 0 ? 'kronos-dir-neg' : '')}
        ${metric('预测 30日收益', pct(k.pred_ret_30d), r30 > 0 ? 'kronos-dir-pos' : r30 < 0 ? 'kronos-dir-neg' : '')}
      </div>
      <div class="kronos-signal-row">
        <span class="kronos-signal-badge ${badge[0]}">${badge[1]}</span>
        <span>预测路径在现价以上的占比 ${Math.round(dir * 100)}%</span>
        <div class="kronos-pred-track" style="flex:1"><i style="width:${trackW}%;background:${trackCls}"></i></div>
      </div>
    </div>`;
}

function renderQuantStrategy(quant) {
  const tag = $('#quantStrategyTag');
  const notice = $('#quantStrategyNotice');
  const body = $('#quantStrategyBody');
  const kronosHtml = kronosBlock(quant && quant.kronos);
  if (!quant || quant.available !== true) {
    tag.textContent = '不在宇宙';
    tag.className = 'tag gray';
    notice.textContent = (quant && quant.message) ||
      '本股票不在当前量化研究宇宙内（约300只样本）。';
    body.innerHTML = '<div class="empty-state compact">量化策略未覆盖该股票。</div>' +
      kronosHtml;
    return;
  }
  const strat = quant.strategy || {};
  const legacyTop = quant.legacy_in_portfolio === true;
  tag.textContent = legacyTop ? '历史模型 TOP-20 · 非持仓' : '历史研究观察';
  tag.className = 'tag gray';
  notice.textContent = `旧模型快照日 ${quant.snapshot_date || '--'} · 非全站统一评分 · 不进入提醒、仓位或买卖资格`;
  const yearText = (strat.years_positive != null && strat.years_total != null)
    ? `${strat.years_positive}/${strat.years_total} 年正收益`
    : '';
  const metric = (label, value) => `
    <div class="quant-metric">
      <span>${esc(label)}</span>
      <strong>${esc(value)}</strong>
    </div>`;
  const factors = (quant.factor_scores || []).map((f) => {
    const z = Number(f.zscore) || 0;
    const pct = Math.max(2, Math.min(98, (z + 3) / 6 * 100));
    const color = z >= 0 ? 'red' : 'green';
    return `
      <div class="quant-factor-row">
        <span class="quant-factor-name" title="${esc(f.factor)}">${esc(f.factor)} <small>w=${f.weight}</small></span>
        <div class="quant-factor-bar"><div class="quant-factor-fill ${color}" style="width:${pct}%"></div></div>
        <span class="quant-factor-z ${color}">${z > 0 ? '+' : ''}${z.toFixed(2)}</span>
      </div>`;
  }).join('');
  const ff = quant.fund_flow;
  const fundFlowHtml = ff ? (() => {
    const net = Number(ff.net_yi) || 0;
    const color = net > 0 ? 'red' : net < 0 ? 'green' : 'gray';
    return `
      <div class="quant-fundflow">
        <h3 class="quant-block-title">今日主力资金流（同花顺实时）</h3>
        <div class="quant-metrics-grid compact">
          ${metric('主力净额', `<span class="${color}">${net > 0 ? '+' : ''}${net.toFixed(2)}亿</span>`)}
          ${metric('流入', `${Number(ff.inflow_yi || 0).toFixed(2)}亿`)}
          ${metric('流出', `${Number(ff.outflow_yi || 0).toFixed(2)}亿`)}
          ${metric('涨跌/换手', `${ff.pct_chg != null ? ff.pct_chg + '%' : '--'} / ${ff.turnover != null ? ff.turnover + '%' : '--'}`)}
        </div>
      </div>`;
  })() : '';
  body.innerHTML = `
    <div class="quant-metrics-grid">
      ${metric('历史模型复合分', Number(quant.composite_score || 0).toFixed(3))}
      ${metric('旧研究宇宙排名', `${quant.rank} / ${quant.rank_total || '--'}`)}
      ${metric('历史回测年化', strat.annualized_pct != null ? strat.annualized_pct + '%' : '--')}
      ${metric('历史 Sharpe', strat.sharpe != null ? strat.sharpe : '--')}
    </div>
    <h3 class="quant-block-title">旧研究模型因子 zscore（只读留档）</h3>
    <div class="quant-factor-list">${factors || '<p class="empty-state compact">暂无因子数据</p>'}</div>
    <div class="quant-metrics-grid compact">
      ${metric('Calmar', strat.calmar != null ? strat.calmar : '--')}
      ${metric('最大回撤', strat.max_dd_pct != null ? strat.max_dd_pct + '%' : '--')}
      ${metric('正年数', yearText)}
      ${metric('组合IC', strat.portfolio_ic != null ? strat.portfolio_ic : '--')}
    </div>
    ${fundFlowHtml}
    ${kronosHtml}`;
}
function renderAnalysis(result) {
  currentResult = result;
  const quote = result.quote || {};
  const decision = result.decision || {};
  const state = result.state || {code: 'unknown', label: '等待数据', score: 0};
  const buyScenario = decision.buy_scenario || {};
  const sellScenario = decision.sell_scenario || {};
  const buyRange = Array.isArray(buyScenario.range)
    ? buyScenario.range : [];
  const executionAllowed = decision.execution_allowed === true;
  $('#analysisConnectionDot').className = `status-dot ${quote.freshness === 'live' ? 'live' : ''}`;
  $('#analysisConnection').textContent = quote.freshness === 'live'
    ? '真实行情已连接 · 正在启动低延迟K线'
    : '当前展示最近公开快照';
  $('#analysisSymbol').textContent = `${result.code} ${result.name || quote.name || ''}`.trim();
  updateQuoteHeader({
    ...quote,
    price: quote.price ?? result.candles.at(-1)?.close,
    exchange_data_time: (
      quote.exchange_data_time || quote.data_time || result.information_time
    ),
  });
  const stateColor = state.code === 'prepare' ? 'yellow' : state.code === 'avoid' ? 'green' : 'gray';
  $('#analysisState').textContent = state.label;
  $('#analysisState').className = `tag ${stateColor}`;
  $('#gateBadge').textContent = executionAllowed ? '人工执行已授权' : '真钱未授权';
  $('#gateBadge').className = `tag ${executionAllowed ? 'green' : 'red'}`;
  $('#decisionCard').className = `panel decision-card ${executionAllowed ? '' : 'locked'}`;
  $('#decisionAction').textContent = executionAllowed
    ? (decision.action || '等待人工确认')
    : '仅研究观察，禁止买入';
  $('#decisionGateReason').textContent = decision.gate_reason
    || '执行门禁未通过；以下价格仅用于研究复核。';
  $('#buyZoneLabel').textContent = executionAllowed
    ? '人工确认区间' : '研究参考区间（不可执行）';
  $('#buyZone').textContent = buyRange.length >= 2
    ? `${money(buyRange[0])} – ${money(buyRange[1])}${executionAllowed ? '' : ' · 仅参考'}`
    : '--';
  $('#buyTrigger').textContent = executionAllowed
    ? (buyScenario.trigger || '等待触发条件')
    : `仅作技术复核：${buyScenario.trigger || '暂无完整触发条件'}`;
  $('#protectiveStopLabel').textContent = executionAllowed
    ? '保护失效线' : '研究失效参考';
  $('#firstTargetLabel').textContent = executionAllowed
    ? '第一复核目标' : '研究目标参考';
  $('#protectiveStop').textContent = money(sellScenario.protective_stop);
  $('#firstTarget').textContent = money(sellScenario.first_target);
  renderList('#sellConditions', sellScenario.conditions || [], '等待条件');
  const marketRegime = result.market_regime || {};
  $('#marketContextLabel').textContent = marketRegime.label || '市场环境待核对';
  $('#marketContextText').textContent = marketRegime.message || '市场环境不能代替个股门禁。';
  $('#localScore').textContent = `${state.score >= 0 ? '+' : ''}${state.score} / 4`;
  $('#localScore').className = `tag ${state.score >= 3 ? 'yellow' : state.score <= -2 ? 'green' : 'gray'}`;
  renderList('#bullEvidence', state.reasons || [], '没有足够的偏强证据');
  renderList('#bearEvidence', state.cautions || [], '暂未发现主要技术风险');
  renderQuantStrategy(result.quant);
  const lenses = result.technical?.snapshot?.lenses || [];
  $('#technicalLenses').innerHTML = lenses.map((lens) => `
    <div><span>${esc(lens.name)}</span><strong>${esc(lens.value)}</strong><small>${esc(lens.interpretation)}</small></div>
  `).join('');
  const source = result.source || {};
  $('#analysisDataSource').textContent = `${source.candles || 'K线来源待核对'} · 行情 ${source.quote || '暂缺'} · 最新 ${result.information_time || '暂无'}`;
  $('#liveStreamBadge').textContent = quote.freshness === 'live'
    ? '正在连接实时通道'
    : '休市快照';
  $('#liveStreamBadge').className = `tag ${quote.freshness === 'live' ? 'yellow' : 'gray'}`;
  $('#liveUpdateMeta').textContent = quote.freshness === 'live'
    ? (currentInterval === '1d'
      ? '日K将每60秒重新校准；最新报价仍显示在顶部'
      : '即将进入2秒报价更新与10秒完整K线校准')
    : `行情时间 ${readableTime(quote.exchange_data_time || result.information_time)}`;
  $('#analysisSafety').textContent = result.safety?.message
    || '研究参考不构成买入授权，系统不会自动下单。';
  $('#aiStockStatus').textContent = '已禁用';
  $('#aiStockStatus').className = 'tag gray';
  drawChart(result);
  document.querySelectorAll('.analysis-watchlist button').forEach((button) => {
    button.classList.toggle('active', button.dataset.code === currentCode);
  });
}
async function loadAnalysis(
  code = currentCode,
  interval = currentInterval,
  {background = false} = {},
) {
  if (!code) return;
  currentCode = code;
  currentInterval = interval;
  $('#analysisCode').value = code;
  if (!background) {
    stopRealtimeLoops();
    $('#analysisConnection').textContent = `正在读取 ${interval} 行情…`;
  }
  try {
    const result = await api(`/api/analysis/${encodeURIComponent(code)}?interval=${interval}`);
    if (code !== currentCode || interval !== currentInterval) return;
    renderAnalysis(result);
    const url = new URL(window.location.href);
    url.searchParams.set('code', code);
    url.searchParams.set('interval', interval);
    window.history.replaceState({}, '', url);
    scheduleRealtimeLoops();
  } catch (error) {
    if (!background) {
      $('#analysisConnectionDot').className = 'status-dot error';
      $('#analysisConnection').textContent = `分析失败：${error.message}`;
      showToast(error.message, 'error');
    } else {
      $('#liveUpdateMeta').textContent = `完整K线校准失败，稍后重试：${error.message}`;
      scheduleRealtimeLoops();
    }
  }
}
async function initialize() {
  try {
    const data = await api('/api/dashboard');
    const items = [
      ...(data.opportunities || []).slice(0, 3),
      ...(data.watchlist || []),
      ...(data.positions || []),
      ...(data.observations || []),
    ].filter((item, index, array) => (
      item.code && array.findIndex((candidate) => candidate.code === item.code) === index
    ));
    $('#analysisWatchlist').innerHTML = items.length
      ? items.map((item) => `<button type="button" aria-pressed="false" data-code="${esc(item.code)}">${esc(item.code)} ${esc(item.name || item.quote?.name || '')}</button>`).join('')
      : '<span class="tag gray">自选为空，可直接输入代码</span>';
    document.querySelectorAll('.analysis-watchlist button').forEach((button) => {
      button.addEventListener('click', () => loadAnalysis(button.dataset.code));
    });
    const params = new URLSearchParams(window.location.search);
    const requestedInterval = params.get('interval');
    if (['1m', '5m', '15m', '30m', '1d'].includes(requestedInterval)) {
      currentInterval = requestedInterval;
    }
    document.querySelectorAll('.timeframe-switcher button').forEach((button) => {
      const active = button.dataset.interval === currentInterval;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    const requestedCode = params.get('code');
    const defaultCode = /^\d{6}$/.test(requestedCode || '')
      ? requestedCode
      : (items[0]?.code || '000001');
    await loadAnalysis(defaultCode, currentInterval);
  } catch (error) {
    $('#analysisConnectionDot').className = 'status-dot error';
    $('#analysisConnection').textContent = error.message;
  }
}
$('#stockAnalysisForm').addEventListener('submit', (event) => {
  event.preventDefault();
  const code = new FormData(event.target).get('code').trim();
  if (!/^(0|3|6)\d{5}$/.test(code)) {
    showToast('请输入沪深主板或创业板6位代码', 'error');
    return;
  }
  loadAnalysis(code);
});
document.querySelectorAll('.timeframe-switcher button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.timeframe-switcher button').forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    loadAnalysis(currentCode, button.dataset.interval);
  });
});
setInterval(() => {
  $('#analysisClock').textContent = new Date().toLocaleTimeString('zh-CN', {hour12: false});
}, 1000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && currentCode) {
    clearTimeout(liveQuoteTimer);
    pollLiveQuote();
  }
});
initialize();
