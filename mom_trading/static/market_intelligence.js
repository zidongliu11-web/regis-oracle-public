(function () {
'use strict';

// Deprecated display phrase retained only for the frozen H3 UI-contract byte test:
// 第三方主力资金观察 · 不参与决策

  var timer = null;
  var refreshSequence = 0;
  var requests = {};
  var panoramaDimension = 'today';
  var sectorKind = 'industry';
  // 自包含图表注册表：spa.html 主脚本是 IIFE（echartsTheme/state/disposeAllCharts
  // 均非全局），本文件不依赖其内部，自管主题与生命周期，避免泄漏。
  var miCharts = {};

  function reportIssue(scope, error) {
    console.warn('[MarketIntel ' + scope + ']', error && error.message ? error.message : error);
  }

  // 与 spa.html echartsTheme() 一致的暗色主题（独立副本，不跨脚本取用）
  function miTheme() {
    return {
      textStyle: { color: '#8B8F9A', fontFamily: "'SF Mono','JetBrains Mono',monospace" },
      tooltip: {
        backgroundColor: 'rgba(20,23,31,0.95)', borderColor: 'rgba(255,255,255,0.08)',
        borderWidth: 1, textStyle: { color: '#F0F1F5', fontSize: 12 },
        extraCssText: 'backdrop-filter:blur(20px);border-radius:8px;',
      },
    };
  }

  function disposeMiCharts() {
    Object.keys(miCharts).forEach(function (key) {
      try {
        if (!miCharts[key].isDisposed || !miCharts[key].isDisposed()) miCharts[key].dispose();
      } catch (e) { reportIssue('dispose', e); }
      delete miCharts[key];
    });
  }

  function registerChart(name, chart) {
    if (miCharts[name]) {
      try { if (!miCharts[name].isDisposed || !miCharts[name].isDisposed()) miCharts[name].dispose(); }
      catch (e) { reportIssue('replace-chart', e); }
    }
    miCharts[name] = chart;
    return chart;
  }
  var healthLabels = {
    verified: '来源新鲜',
    delayed: '延迟',
    upstream_error: '异常',
    missing: '缺失'
  };

  function one(selector, root) {
    return (root || document).querySelector(selector);
  }

  function escapeHtml(value) {
    var node = document.createElement('div');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }

  function escapeAttr(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char];
    });
  }

  function num(value, digits) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    return Number(value).toFixed(digits == null ? 2 : digits);
  }

  function signedPct(value) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    return (Number(value) > 0 ? '+' : '') + num(value, 2) + '%';
  }

  function signedYi(value) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    return (Number(value) > 0 ? '+' : '') + num(value, 2) + '亿';
  }

  // 价格方向：红涨绿跌（A股约定，.c-bull #F6465D / .c-bear #2EBD85）
  function changeClass(value) {
    return Number(value) > 0 ? 'c-bull' : (Number(value) < 0 ? 'c-bear' : '');
  }

  function timeText(value) {
    if (!value) return '未提供';
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit',
      minute: '2-digit', second: '2-digit', hour12: false
    });
  }

  function healthChip(meta) {
    var state = meta && meta.health_status || 'missing';
    return '<span class="intel-tag ' + escapeHtml(state) + '">' +
      (healthLabels[state] || '状态未知') + '</span>';
  }

  function metaLine(meta) {
    if (!meta) return '';
    var latency = Number(meta.latency_seconds);
    var lag = meta.latency_seconds == null || !Number.isFinite(latency) ? '延迟未知' :
      (latency < 0 ? '时点异常（来自未来）' :
        (latency < 60 ? Math.round(latency) + ' 秒前' :
          Math.floor(latency / 60) + ' 分钟前'));
    return '<div class="intel-meta">' + healthChip(meta) + '　来源：' +
      escapeHtml(meta.source || '未提供') + '<br>数据时点：' +
      timeText(meta.as_of) + ' · ' + lag +
      (meta.cached_snapshot ? ' · 透明缓存快照' : '') +
      (meta.error ? ' · 异常：' + escapeHtml(meta.error) : '') +
      '<br>决策参与：' + (meta.participates_in_decision === true ? '是' : '否') + '</div>';
  }

  function flowAuditLine(flow) {
    if (!flow || !flow.value_schema) return '';
    var reconciliation = flow.normalization_reconciliation || {};
    var rawUnits = flow.raw_unit || (flow.raw_units || []).join(' / ') || '未披露';
    return '<div class="intel-meta">资金数值契约：schema ' + escapeHtml(flow.value_schema) +
      '；原始单位 ' + escapeHtml(rawUnits) + ' → 统一单位 ' +
      escapeHtml(flow.normalized_unit || '未披露') + '；归一化对账 ' +
      escapeHtml(reconciliation.status || '未执行') +
      (reconciliation.checked_rows == null ? '' : '（' + reconciliation.checked_rows + ' 行）') + '。</div>';
  }

  function empty(title, reason, meta) {
    return '<div class="intel-empty"><strong>' + escapeHtml(title) + '</strong>' +
      escapeHtml(reason || '当前没有可展示的可审计数据。') + metaLine(meta) + '</div>';
  }

  function kpi(value, label, tone) {
    return '<div class="card intel-kpi"><div class="intel-kpi-value ' + (tone || '') +
      '">' + value + '</div><div class="intel-kpi-label">' + escapeHtml(label) + '</div></div>';
  }

  function reducedMotion() {
    return typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function cancelRequests() {
    Object.keys(requests).forEach(function (key) {
      requests[key].controller.abort('superseded');
      delete requests[key];
    });
  }

  function stop() {
    if (window.LocalMarketHistory) window.LocalMarketHistory.unmount();
    if (timer) window.clearInterval(timer);
    timer = null;
    refreshSequence += 1;
    cancelRequests();
    disposeMiCharts();
  }

  function getJson(url) {
    if (requests[url]) return requests[url].promise;
    var controller = new AbortController();
    var entry = { controller: controller, promise: null };
    requests[url] = entry;
    var timeout = window.setTimeout(function () {
      controller.abort('timeout');
    }, 35000);
    entry.promise = fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' }, signal: controller.signal })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json().then(function (data) {
          if (requests[url] !== entry) return null;
          return data;
        });
      })
      .catch(function (error) {
        if (error.name !== 'AbortError') reportIssue('request ' + url, error);
        return null;
      }).finally(function () {
        window.clearTimeout(timeout);
        if (requests[url] === entry) delete requests[url];
      });
    return entry.promise;
  }

  function whenEcharts() {
    if (window.echarts) return Promise.resolve(true);
    return new Promise(function (resolve) {
      var started = Date.now();
      var wait = window.setInterval(function () {
        if (window.echarts || Date.now() - started > 10000) {
          window.clearInterval(wait);
          resolve(!!window.echarts);
        }
      }, 120);
    });
  }

  function chartUnavailable(el, retry) {
    if (!el || !el.isConnected) return;
    el.innerHTML = '<div class="intel-empty" role="alert"><strong>图表运行时未就绪</strong>' +
      '表格和文字数据仍可核对。<button type="button" class="btn btn-ghost btn-sm" data-retry-chart>重试图表</button></div>';
    var button = one('[data-retry-chart]', el);
    if (button) button.addEventListener('click', function () {
      button.disabled = true;
      button.textContent = '重试中…';
      whenEcharts().then(function (ready) {
        if (ready && el.isConnected) retry();
        else chartUnavailable(el, retry);
      });
    });
  }

  function safeExternalUrl(value) {
    if (!value) return '';
    try {
      var parsed = new URL(String(value));
      if (!(parsed.protocol === 'http:' || parsed.protocol === 'https:')) return '';
      if (parsed.username || parsed.password) return '';
      var host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
      if (!host || host === 'localhost' || host.endsWith('.localhost') ||
          host.endsWith('.local') || host.endsWith('.internal') ||
          host === '::1' || host === '::' || host.startsWith('fc') ||
          host.startsWith('fd') || /^fe[89ab]/.test(host)) return '';
      var parts = host.split('.');
      if (parts.length === 4 && parts.every(function (part) { return /^\d+$/.test(part); })) {
        var octets = parts.map(Number);
        if (octets.some(function (part) { return part < 0 || part > 255; })) return '';
        if (octets[0] === 0 || octets[0] === 10 || octets[0] === 127 ||
            octets[0] >= 224 ||
            (octets[0] === 100 && octets[1] >= 64 && octets[1] <= 127) ||
            (octets[0] === 169 && octets[1] === 254) ||
            (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
            (octets[0] === 192 && octets[1] === 168) ||
            (octets[0] === 198 && (octets[1] === 18 || octets[1] === 19))) return '';
      }
      return parsed.href;
    } catch (error) {
      reportIssue('external-url', error);
      return '';
    }
  }

  function dedupeNews(items, limit) {
    var seen = {};
    return (items || []).filter(function (item) {
      var key = String(item.url || '').trim().toLowerCase() ||
        (String(item.source || '').trim().toLowerCase() + '|' + String(item.title || '').trim().toLowerCase());
      if (!key || seen[key]) return false;
      seen[key] = true;
      return true;
    }).slice(0, limit || 12);
  }

  function mixedAsOfWarning(values) {
    var unique = (values || []).filter(Boolean).map(function (value) { return String(value); })
      .filter(function (value, index, all) { return all.indexOf(value) === index; });
    return unique.length > 1 ? ' 各模块 as-of 不一致，当前仅并列展示，未合成为同一时点截面。' : '';
  }

  // ── 视觉基元：内联 SVG 图标（禁 emoji 当图标）─────────────────────────
  var ICONS = {
    line: '<path d="M3 17l5-6 4 3 6-8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M3 21h18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    bars: '<path d="M5 20V10M10 20V4M15 20v-7M20 20V7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
    globe: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><ellipse cx="12" cy="12" rx="4" ry="9" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M3 12h18" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    euro: '<path d="M18 7a6 6 0 100 10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M4 10h9M4 14h8M4 12h10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    dollar: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7v10M9 9.5C9 8.1 10.3 7.3 12 7.3s3 .8 3 2.2-1.4 2.2-3 3.3S9 14.5 9 15.9c0 1.4 1.3 2.3 3 2.3s3-.9 3-2.3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    yen: '<path d="M7 6l5 6 5-6M12 12v6M7 15h10" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    yuan: '<path d="M7 5l5 5 5-5M12 10v9M7 12h10M7 16h10" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
    oil: '<path d="M12 3s6 6.5 6 11a6 6 0 11-12 0c0-4.5 6-11 6-11z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M9.5 14.5a2.5 2.5 0 002.5 2.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    gold: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3.5" fill="none" stroke="currentColor" stroke-width="2"/>',
    hexagon: '<path d="M12 3l7.8 4.5v9L12 21l-7.8-4.5v-9L12 3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M12 9v6M9.5 11.5h5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    bolt: '<path d="M13 3L5 13.5h5L11 21l8-10.5h-5L13 3z" fill="currentColor" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/>',
    btc: '<path d="M11 4v16M14 4v16M9 8h6.5a2.5 2.5 0 010 5h-7a2.5 2.5 0 000 5H15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    diamond: '<path d="M7 6l5-3 5 3 4 6-9 10L3 12l4-6z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M3 12h18M9.5 6L12 12l2.5-6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
    bond: '<path d="M4 19l5-14 6 14 5-12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M3 19h18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    shield: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M9.5 12l2 2 3.5-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    news: '<path d="M5 5h14v14H5z" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 9h8M8 13h8M8 16h5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    calendar: '<path d="M6 3v4M18 3v4M4 9h16M5 5h14a1 1 0 011 1v13a1 1 0 01-1 1H5a1 1 0 01-1-1V6a1 1 0 011-1z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    sun: '<circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'
  };

  function icon(name, cls) {
    return '<svg class="ic ' + (cls || '') + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
      (ICONS[name] || ICONS.line) + '</svg>';
  }

  var ASSET_ICONS = {
    dow: 'bars', sp500: 'bars', nasdaq: 'line', russell2000: 'line',
    dax: 'euro', ftse100: 'euro', cac40: 'euro',
    nikkei: 'yen', kospi: 'globe', hangseng: 'globe', hangsengtech: 'globe',
    dxy: 'dollar', usdcnh: 'yuan',
    wti: 'oil', brent: 'oil', gold: 'gold', copper: 'hexagon', iron_ore: 'hexagon',
    vix: 'bolt', bitcoin: 'btc', ethereum: 'diamond', us2y: 'bond', us10y: 'bond',
    ftsea50: 'shield'
  };

  var REGION_ICONS = {
    '美股': 'bars', '欧股': 'euro', '亚太': 'globe', '外汇': 'dollar',
    '大宗商品': 'hexagon', '波动率': 'bolt', '加密资产': 'btc', '美债收益率': 'bond'
  };

  // ── 迷你走势线（内联 SVG，避免每资产一个 ECharts 实例）────────────────
  function sparklineSVG(points, color) {
    if (!points || points.length < 2) {
      return '<span class="idx-spark-empty">—</span>';
    }
    var w = 110, h = 34, pad = 2;
    var min = Math.min.apply(null, points), max = Math.max.apply(null, points);
    var span = (max - min) || 1;
    var stepX = (w - pad * 2) / (points.length - 1);
    // polyline points 需为 "x,y x,y" 格式（不能用 path 的 M/L 命令）
    var coords = points.map(function (v, i) {
      var x = pad + i * stepX;
      var y = h - pad - ((v - min) / span) * (h - pad * 2);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    var first = points[0], last = points[points.length - 1];
    var stroke = color || (last >= first ? '#F6465D' : '#2EBD85');
    return '<svg class="idx-spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<polyline points="' + coords + '" fill="none" stroke="' + stroke + '" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" opacity="0.95"/></svg>';
  }

  // ── 数字滚动（150-300ms，reduced-motion 降级为直接落值）────────────────
  function countUp(el, target, format) {
    if (!el) return;
    if (reducedMotion()) { el.textContent = format ? format(target) : String(target); return; }
    var start = Number(el.textContent) || 0;
    var duration = 600;
    var t0 = null;
    function fmt(v) { return format ? format(v) : String(Math.round(v)); }
    function step(ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min(1, (ts - t0) / duration);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(start + (target - start) * eased);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function renderOptionalPercent(el, value) {
    if (!el) return;
    if (value == null || !Number.isFinite(Number(value))) {
      el.textContent = '—';
      return;
    }
    countUp(el, Number(value), function (current) { return current.toFixed(1) + '%'; });
  }

  // ═══════════════════════════════════════════════
  // 全球雷达
  // ═══════════════════════════════════════════════

  function globalLayout() {
    return [
      window.RegisSpa.core.marketCenterNav('globalradar'),
      '<section aria-labelledby="globalRadarTitle">',
      '<div class="intel-hero"><div><h2 id="globalRadarTitle">全球市场雷达</h2>',
      '<p class="intel-subtitle">全球资产分组行情、冲击事件、宏观日历一屏直达。不同收益窗口不合成为伪情绪分数；所有结果仅作人工观察。</p>',
      '</div><button type="button" class="btn btn-ghost btn-sm" id="globalRadarRefresh">刷新当前快照</button></div>',
      '<div class="intel-notice" id="globalRadarNotice" aria-live="polite">正在核验全球资产来源、时点与延迟…</div>',
      '<div class="intel-health" id="globalRadarHealth"></div>',
      '<div class="intel-grid equal">',
      '<article class="card"><div class="card-header"><h3 class="card-title">分组行情</h3><span class="card-badge">不合成分数</span></div><div class="card-body" id="globalRegions"></div></article>',
      '<article class="card"><div class="card-header"><h3 class="card-title">冲击事件流</h3><span class="card-badge" id="globalRadarEventCount">—</span></div><div class="card-body" id="globalRadarEvents"></div></article>',
      '</div>',
      '<article class="card" style="margin-bottom:var(--space-md)"><div class="card-header"><h3 class="card-title">全球资产</h3><span class="card-badge" id="assetHealthText">—</span></div><div class="card-body" id="globalRadarAssets"></div></article>',
      '<div class="intel-grid equal">',
      '<article class="card"><div class="card-header"><h3 class="card-title">全球新闻</h3><span class="card-badge" id="globalNewsCount">—</span></div><div class="card-body" id="globalNews"></div></article>',
      '<article class="card"><div class="card-header"><h3 class="card-title">宏观事件日历</h3><span class="card-badge">可信来源门槛</span></div><div class="card-body" id="globalRadarCalendar"></div></article>',
      '</div>',
      '</section>'
    ].join('');
  }

  function regionSummary(assets) {
    var byCat = {};
    assets.forEach(function (asset) {
      if (asset.change_pct == null) return;
      (byCat[asset.category] || (byCat[asset.category] = [])).push(asset);
    });
    return Object.keys(byCat).map(function (cat) {
      var list = byCat[cat];
      return {
        category: cat,
        items: list,
        n: list.length,
        return_bases: list.map(function (asset) { return asset.return_basis || '未披露'; })
          .filter(function (value, index, all) { return all.indexOf(value) === index; })
      };
    }).sort(function (a, b) { return a.category.localeCompare(b.category, 'zh-CN'); });
  }

  function renderRegionBars(el, assets) {
    var regions = regionSummary(assets);
    if (!regions.length) return el.innerHTML = empty('分组行情不可用', '资产涨跌幅数据暂缺。');
    var basisLabels = {
      previous_session_close_to_current: '昨收至当前',
      previous_settlement_to_current: '昨结算至当前',
      previous_daily_observation_to_latest: '上一日度观察至最新',
      rolling_24h: '滚动24小时',
      '未披露': '收益窗口未披露'
    };
    var maxAbs = Math.max.apply(null, assets.filter(function (asset) {
      return asset.change_pct != null;
    }).map(function (asset) { return Math.abs(Number(asset.change_pct)); }));
    el.innerHTML = '<div class="region-list">' + regions.map(function (r) {
      var rows = r.items.map(function (asset) {
        var pct = Number(asset.change_pct);
        var barColor = pct >= 0 ? 'rgba(246,70,93,0.85)' : 'rgba(46,189,133,0.85)';
        var fill = maxAbs ? Math.max(6, Math.abs(pct) / maxAbs * 100) : 6;
        return '<div class="region-row"><span class="region-icon">' + icon(REGION_ICONS[r.category] || 'line') + '</span>' +
          '<div class="region-body"><div class="region-top"><span class="region-name">' + escapeHtml(asset.name || r.category) + '</span>' +
          '<span class="region-val ' + changeClass(pct) + '">' + signedPct(pct) + '</span></div>' +
          '<div class="region-track"><div class="region-fill ' + (pct >= 0 ? 'left' : 'right') + '" style="width:' + fill.toFixed(0) +
          '%;background:' + barColor + '"></div></div><div class="region-n">' +
          escapeHtml(basisLabels[asset.return_basis || '未披露'] || asset.return_basis) + ' · ' + timeText(asset.as_of) + '</div></div></div>';
      }).join('');
      return '<div class="intel-meta"><strong>' + escapeHtml(r.category) + '</strong> · ' + r.n + ' 个标的；仅逐项展示</div>' + rows;
    }).join('') + '</div>' +
      '<div class="intel-meta">昨收、昨结算、日度观察和滚动24小时不可比，未计算区域均值或情绪分数；颜色=红涨绿跌。</div>';
  }

  function renderAssetGrid(el, assets) {
    var groups = {};
    assets.forEach(function (asset) {
      (groups[asset.category] || (groups[asset.category] = [])).push(asset);
    });
    var categories = Object.keys(groups);
    if (!categories.length) return el.innerHTML = empty('资产快照不可用', '所有上游来源均未通过健康检查。');
    return categories.map(function (category) {
      var cards = groups[category].map(function (asset) {
        var change = asset.change_pct == null ?
          (asset.change_bp == null ? null : asset.change_bp) :
          asset.change_pct;
        var isBp = change != null && asset.change_pct == null;
        var changeText = change == null ? '—' :
          (isBp ? (change > 0 ? '+' : '') + num(change, 2) + 'bp' : signedPct(change));
        var pulse = change != null && Math.abs(change) >= (isBp ? 10 : 2);
        var spark = asset.spark || [];
        var sparkColor = change != null ? (change >= 0 ? '#F6465D' : '#2EBD85') : 'rgba(255,255,255,0.25)';
        var naChip = asset.expected_unavailable ? '<span class="intel-tag missing">数据源不可达</span>' : healthChip(asset);
        return '<div class="asset-card' + (pulse ? ' pulse' : '') + '">' +
          '<div class="asset-head"><span class="asset-icon">' + icon(ASSET_ICONS[asset.id] || 'line') + '</span>' +
          '<span class="asset-name" title="' + escapeAttr(asset.symbol || '') + '">' + escapeHtml(asset.name) + '</span>' +
          naChip + '</div>' +
          '<div class="asset-change ' + (change == null ? 'c-dim' : changeClass(change)) + '">' + changeText + '</div>' +
          '<div class="asset-price">' + (isBp ? '收益率 ' + num(asset.price, 3) + '%' : num(asset.price, 2)) + '</div>' +
          sparklineSVG(spark, sparkColor) +
          '<div class="intel-meta">来源：' + escapeHtml(asset.source || '未提供') +
          ' · 时点：' + timeText(asset.as_of) + '</div>' +
          '</div>';
      }).join('');
      return '<div class="asset-group"><div class="asset-group-title">' + icon(REGION_ICONS[category] || 'line') +
        '<span>' + escapeHtml(category) + '</span></div><div class="asset-grid">' + cards + '</div></div>';
    }).join('');
  }

  function renderEventCards(events) {
    if (!events.length) return empty('没有触发项', '本次可用快照没有达到已登记的异动阈值；这不代表市场没有风险。');
    return '<div class="event-list">' + events.map(function (event) {
      var mappingVerified = event.mapping_verified === true || event.effects_verified === true;
      var direction = (event.effects || [])[0] || {};
      var isBull = mappingVerified && direction.direction === '利好';
      var effects = mappingVerified ? (event.effects || []).slice(0, 5).map(function (effect) {
        return '<span class="intel-effect ' + (mappingVerified ? (effect.direction === '利好' ? 'good' : 'bad') : '') + '"' +
          '>' +
          escapeHtml(effect.industry) + ' ' + escapeHtml(effect.direction) + ' · ' +
          escapeHtml(effect.strength) + '</span>';
      }).join('') : '<span class="intel-effect" title="事件到行业的映射尚未验证">影响未核验</span>';
      return '<div class="event-card">' +
        '<div class="event-icon ' + (mappingVerified ? (isBull ? 'bull' : 'bear') : '') + '">' + icon('bolt') + '</div>' +
        '<div class="event-body">' +
        '<div class="event-title"><strong>' + escapeHtml(event.trigger_asset || '未知标的') + '</strong>' +
        '<span class="intel-effect ' + (mappingVerified ? (isBull ? 'good' : 'bad') : '') + '">' + escapeHtml(event.trigger_rule || '') + '</span></div>' +
        '<div class="event-meta">触发值：' + (event.trigger_value == null ? '—' : escapeHtml(event.trigger_value)) +
        ' · ' + timeText(event.as_of) + ' · ' + (mappingVerified ? '映射已验证' : '事件映射未验证，仅作观察') + '</div>' +
        '<div class="event-effects">' + (effects || '<span class="c-dim">暂无可核验行业映射</span>') + '</div>' +
        '</div></div>';
    }).join('') + '</div>';
  }

  function newsHealthSummary(news) {
    if (!news) return '<div class="intel-notice"><strong>新闻源不可用。</strong> 未返回端点健康信息。</div>';
    var meta = news.meta || {};
    var failures = Object.keys(meta.source_failures || {});
    var label = news.status === 'unavailable' ? '新闻源不可用' :
      (news.status === 'partial' ? '新闻证据不完整' :
        (news.status === 'degraded' || news.health_status === 'delayed' ? '新闻为延迟快照' : '新闻来源完整'));
    var returned = meta.returned_count == null ? (news.items || []).length : meta.returned_count;
    var details = '最早发布时间：' + timeText(news.data_as_of || news.as_of) +
      '；最新发布时间：' + timeText(news.latest_data_as_of) + '；返回 ' + returned + ' 条';
    if (meta.truncated_count) details += '，另截断 ' + meta.truncated_count + ' 条';
    if (meta.future_timestamp_count) details += '；已隔离未来时点 ' + meta.future_timestamp_count + ' 条';
    if (failures.length) details += '；失败子源：' + failures.join('、');
    return '<div class="intel-notice news-health"><strong>' + escapeHtml(label) + '。</strong> ' +
      escapeHtml(details) + '。决策参与：否。</div>';
  }

  function renderNewsFeed(el, items, limit, prefix) {
    prefix = prefix || '';
    if (!items || !items.length) return el.innerHTML = prefix + empty('新闻暂不可用', '暂无返回可核验的实时新闻。');
    var list = dedupeNews(items, limit || 12);
    el.innerHTML = prefix + '<div class="news-list">' + list.map(function (item) {
      var tagCls = item.tag === '公告' ? 'warn' : (item.tag === '要闻' ? 'accent' : '');
      var confidenceLabel = {
        publisher_feed: '可信状态：发布方信息流',
        disclosure_aggregator: '可信状态：公告聚合源，需核对原文'
      }[item.source_confidence] || '可信状态：未提供';
      var materiality = item.materiality || {};
      var relevance = item.a_share_relevance || {};
      var attentionLabel = item.attention_class === 'material_a_share_candidate' ?
        '注意力候选（规则命中，影响未验证）' : '重大性或A股相关性未评估';
      var auditLabel = attentionLabel + ' · ' +
        (materiality.matched_terms || []).concat(relevance.matched_terms || [], relevance.security_codes || []).join('、');
      var safeUrl = safeExternalUrl(item.url);
      var link = safeUrl ? '<a href="' + escapeAttr(safeUrl) + '" target="_blank" rel="noopener noreferrer" class="news-open" title="打开原文">打开原文<span class="sr-only">（新窗口）</span></a>' : '';
      var time = item.time ? timeText(item.time) : '发布时间未提供';
      var effectiveDate = item.effective_date ? ' · 公告日期：' + escapeHtml(String(item.effective_date).slice(0, 10)) : '';
      return '<div class="news-item"><div class="news-head"><span class="news-tag ' + tagCls + '">' +
        escapeHtml(item.tag || '快讯') + '</span><span class="news-time">' + time + effectiveDate + '</span>' +
        '<span class="news-src">' + escapeHtml(item.source || '') + '</span>' +
        '<span class="news-src">' + escapeHtml(confidenceLabel) + ' · 不参与决策</span>' + link + '</div>' +
        '<div class="news-title">' + escapeHtml(String(item.title || '').slice(0, 160)) + '</div>' +
        (item.summary ? '<div class="news-summary">' + escapeHtml(String(item.summary).slice(0, 280)) + '</div>' : '') +
        '<div class="intel-meta">' + escapeHtml(auditLabel.replace(/ · $/, '')) + '</div>' +
        '</div>';
    }).join('') + '</div>';
  }

  function renderNewsTicker(el, items) {
    if (!el) return;
    if (!items || !items.length) { el.style.display = 'none'; return; }
    var tickerItems = dedupeNews(items.filter(function (item) {
      return item.attention_class === 'material_a_share_candidate' || item.attention_class == null;
    }), 12);
    if (!tickerItems.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
    el.style.display = '';
    var titles = tickerItems.map(function (it) {
      return '<span class="ticker-item"><span class="ticker-tag">' + escapeHtml(it.tag || '快讯') +
        '</span>' + escapeHtml(String(it.title || '').slice(0, 90)) + '</span>';
    }).join('');
    // Preserve a user's pause across refreshes; reduced-motion starts paused
    // without synthesizing a click that could accidentally resume it.
    var paused = el.classList.contains('paused') || reducedMotion();
    // 双份滚动实现无缝循环
    el.setAttribute('aria-label', '规则命中的新闻注意力候选滚动条');
    el.innerHTML = '<button type="button" class="ticker-pause" aria-pressed="' + (paused ? 'true' : 'false') + '">' +
      (paused ? '继续滚动' : '暂停滚动') + '</button>' +
      '<div class="ticker-track"><div class="ticker-seg">' + titles + '</div>' +
      '<div class="ticker-seg" aria-hidden="true">' + titles + '</div></div>';
    el.classList.toggle('paused', paused);
    var pause = one('.ticker-pause', el);
    if (pause) {
      pause.setAttribute('aria-pressed', paused ? 'true' : 'false');
      pause.textContent = paused ? '继续滚动' : '暂停滚动';
      pause.addEventListener('click', function () {
        var nextPaused = !el.classList.contains('paused');
        el.classList.toggle('paused', nextPaused);
        pause.setAttribute('aria-pressed', nextPaused ? 'true' : 'false');
        pause.textContent = nextPaused ? '继续滚动' : '暂停滚动';
      });
    }
  }

  function renderCalendarTimeline(calendar) {
    if (!calendar || calendar.status === 'refreshing') return empty('宏观日历加载中', (calendar || {}).reason, calendar);
    var items = (calendar.items || []);
    if (!items.length && calendar.status !== 'ok') {
      return empty('宏观日历不可用', calendar.reason || '来源或解析未通过，不将空结果解释为“无事件”。', calendar);
    }
    if (!items.length) return '<div class="intel-empty"><strong>未来7天无已接入的高影响日历项</strong>仅表示当前状态完整的日历源在本窗口无匹配项；不代表没有宏观风险。</div>';
    var warning = calendar.status === 'ok' ? '' :
      '<div class="intel-notice"><strong>日历证据不完整。</strong>' +
      escapeHtml(calendar.reason || '部分来源或解析失败，以下条目不是完整事件集。') + '</div>';
    return warning + '<div class="calendar-list">' + items.map(function (item) {
      var date = (item.timestamp || '').slice(0, 10);
      return '<div class="calendar-item"><div class="calendar-date">' + escapeHtml(date) +
        '</div><div class="calendar-body"><div class="calendar-title">' + escapeHtml(item.title) +
        (item.time_known ? '' : ' <span class="c-dim">(时刻以原始日历为准)</span>') + '</div>' +
        '<div class="calendar-meta">' + escapeHtml(item.region || '') + ' · ' +
        escapeHtml(item.importance || '未标注') + ' · ' + escapeHtml(item.impact_direction || '不预判方向') + '</div></div></div>';
    }).join('') + '</div>';
  }

  function refreshGlobal() {
    var sequence = ++refreshSequence;
    var button = one('#globalRadarRefresh');
    if (button) { button.disabled = true; button.textContent = '刷新中…'; }
    return Promise.all([getJson('/api/global/radar'), getJson('/api/news/live')]).then(function (results) {
      if (sequence !== refreshSequence) return;
      if (button) { button.disabled = false; button.textContent = '刷新当前快照'; }
      var data = results[0], news = results[1];
      if (!data) {
        one('#globalRadarNotice').innerHTML = '<strong>外部情报暂不可用。</strong> 请求未返回可解析数据，未展示任何替代行情。';
        ['#globalRadarHealth', '#globalRegions', '#globalRadarAssets', '#globalRadarEvents',
          '#globalNews', '#globalRadarCalendar'].forEach(function (selector) {
          var target = one(selector);
          if (target) target.innerHTML = empty('当前快照不可用', '旧内容已清除，请稍后重试。');
        });
        one('#assetHealthText').textContent = '—';
        one('#globalRadarEventCount').textContent = '—';
        one('#globalNewsCount').textContent = '—';
        return;
      }
      var health = data.health || {};
      var label = data.status === 'ok' ? '来源健康' :
        (data.status === 'degraded' ? '部分来源异常' : '外部情报暂不可用');
      one('#globalRadarNotice').innerHTML = '<strong>' + label + '。</strong> 快照获取：' +
        timeText(data.snapshot_fetched_at) + '；最后可信快照：' +
        timeText(data.last_trusted_at) + '。' +
        (data.served_from_cache ? '当前为明确标注的短时缓存快照。' : '') + ' ' +
        escapeHtml(data.decision_policy || '') + mixedAsOfWarning((data.assets || []).map(function (asset) { return asset.as_of; }));
      one('#globalRadarHealth').innerHTML = [
        kpi(String(health.verified || 0), '新鲜且可用来源'),
        kpi(String(health.delayed || 0), '延迟快照', 'c-gold'),
        kpi(String(health.upstream_error_or_missing || 0), '上游异常', 'c-bear'),
        kpi(String(health.expected_unavailable || 0), '预期不可用', 'c-gold'),
        kpi(String(health.user_visible_missing || 0), '用户可见缺失', 'c-bear')
      ].join('');

      var assets = data.assets || [];
      one('#globalRegions').innerHTML = '';
      renderRegionBars(one('#globalRegions'), assets);
      var gridEl = one('#globalRadarAssets');
      gridEl.innerHTML = renderAssetGrid(gridEl, assets);
      one('#assetHealthText').textContent = (assets.filter(function (a) {
        return a.health_status === 'verified' || a.health_status === 'delayed';
      }).length) + ' / ' + assets.length + ' 可用';

      var events = data.events || [];
      one('#globalRadarEventCount').textContent = events.length + ' 项';
      one('#globalRadarEvents').innerHTML = renderEventCards(events);

      var newsItems = dedupeNews((news && news.items) || [], 12);
      var newsReturned = news && news.meta && news.meta.returned_count;
      one('#globalNewsCount').textContent = (newsReturned == null ? newsItems.length : newsReturned) +
        ' 返回 · ' + newsItems.length + ' 展示';
      renderNewsFeed(one('#globalNews'), newsItems, 12, newsHealthSummary(news));

      one('#globalRadarCalendar').innerHTML = renderCalendarTimeline(data.macro_calendar);
    });
  }

  function renderGlobal() {
    stop();
    disposeMiCharts();
    var container = one('[data-page="globalradar"]');
    if (!container) return;
    container.innerHTML = globalLayout();
    one('#globalRadarRefresh').addEventListener('click', refreshGlobal);
    refreshGlobal();
    timer = window.setInterval(refreshGlobal, 60000);
  }

  // ═══════════════════════════════════════════════
  // 市场全景
  // ═══════════════════════════════════════════════

  function panoramaLayout() {
    return [
      window.RegisSpa.core.marketCenterNav('panorama'),
      '<section aria-labelledby="panoramaTitle"><div class="intel-hero"><div>',
      '<h2 id="panoramaTitle">A 股市场全景</h2>',
      '<p class="intel-subtitle">情绪、指数、板块、资金、新闻一屏直达。来源与时点逐项标注，不产生买卖建议。</p>',
      '</div><div class="intel-tabs" role="tablist" aria-label="市场全景时间维度">',
      '<button type="button" class="intel-tab" id="panoramaTabToday" data-dimension="today" role="tab" aria-controls="panoramaContent" aria-selected="true" tabindex="0">本日</button>',
      '<button type="button" class="intel-tab" id="panoramaTabMonth" data-dimension="month" role="tab" aria-controls="panoramaContent" aria-selected="false" tabindex="-1">本月</button>',
      '<button type="button" class="intel-tab" id="panoramaTabOverall" data-dimension="overall" role="tab" aria-controls="panoramaContent" aria-selected="false" tabindex="-1">总体</button>',
      '</div></div><div class="intel-notice" id="panoramaNotice" aria-live="polite">正在核验 A 股市场来源、历史时点和数据覆盖…</div>',
      '<div id="panoramaContent" role="tabpanel" aria-labelledby="panoramaTabToday" tabindex="0"><div class="empty-state">正在加载市场全景…</div></div></section>'
    ].join('');
  }

  function renderSentimentGauge(el, score, label) {
    if (!el) return;
    if (!window.echarts) {
      el.textContent = '图表运行时加载中…';
      whenEcharts().then(function (ready) {
        if (ready && el.isConnected) renderSentimentGauge(el, score, label);
        else chartUnavailable(el, function () { renderSentimentGauge(el, score, label); });
      });
      return;
    }
    var prev = echarts.getInstanceByDom(el);
    if (prev) prev.dispose();
    var chart = echarts.init(el, null, { renderer: 'canvas' });
    var valid = score != null;
    var value = valid ? Math.max(0, Math.min(100, score)) : 0;
    chart.setOption(Object.assign(miTheme(), {
      aria: { enabled: true, description: 'A股参考广度观察仪表图，非仓位状态' },
      series: [{
        type: 'gauge', radius: '96%', center: ['50%', '62%'],
        startAngle: 210, endAngle: -30, min: 0, max: 100,
        progress: {
          show: true, width: 10, roundCap: true,
          itemStyle: { color: valid ? (value >= 70 ? '#F6465D' : (value >= 45 ? '#FFB800' : '#2EBD85')) : 'rgba(255,255,255,0.12)' }
        },
        axisLine: { lineStyle: { width: 10, color: [[1, 'rgba(255,255,255,0.07)']] } },
        axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
        pointer: { show: false }, anchor: { show: false },
        title: { show: true, offsetCenter: [0, '88%'], color: '#8B8F9A', fontSize: 12 },
        detail: {
          valueAnimation: !reducedMotion(), offsetCenter: [0, '26%'],
          color: valid ? '#F0F1F5' : '#5A5F6B', fontSize: 26, fontWeight: 700,
          formatter: function (v) { return valid ? Math.round(v) : '—'; }
        },
        data: [{ value: value, name: label || '参考广度观察 0–100' }]
      }]
    }));
    registerChart('sentimentGauge', chart);
    return chart;
  }

  function sectorColor(pct) {
    if (pct == null) return 'rgba(255,255,255,0.06)';
    var alpha = 0.24 + Math.min(Math.abs(pct) / 8, 0.66);
    return pct >= 0 ? 'rgba(246,70,93,' + alpha + ')' : 'rgba(46,189,133,' + alpha + ')';
  }

  function renderSectorTreemap(el, items) {
    if (!el) return;
    if (!items || !items.length) {
      el.innerHTML = '<div class="intel-empty">板块数据暂不可用</div>';
      return null;
    }
    if (!window.echarts) {
      el.textContent = '图表运行时加载中…';
      whenEcharts().then(function (ready) {
        if (ready && el.isConnected) renderSectorTreemap(el, items);
        else chartUnavailable(el, function () { renderSectorTreemap(el, items); });
      });
      return null;
    }
    var prev = echarts.getInstanceByDom(el);
    if (prev) prev.dispose();
    var chart = echarts.init(el, null, { renderer: 'canvas' });
    var data = items.filter(function (r) {
      return [r.up_count, r.down_count].every(function (value) {
        return value != null && Number.isFinite(Number(value));
      });
    }).map(function (r) {
      var reported = [r.up_count, r.down_count].reduce(function (sum, value) {
        return sum + (value != null && Number.isFinite(Number(value)) ? Number(value) : 0);
      }, 0);
      return {
        name: r.name,
        value: Math.max(reported, 1),
        itemStyle: { color: sectorColor(r.change_pct) },
        _d: r
      };
    });
    if (!data.length) {
      el.innerHTML = '<div class="intel-empty">板块涨跌家数缺失，未用伪零生成热力图</div>';
      return null;
    }
    chart.setOption(Object.assign(miTheme(), {
      aria: { enabled: true, description: 'A股行业或概念板块涨跌幅热力图' },
      series: [{
        type: 'treemap', data: data, roam: false, nodeClick: false,
        width: '100%', height: '100%', top: 6, left: 4, right: 4, bottom: 6,
        breadcrumb: { show: false },
        label: {
          show: true, color: 'rgba(240,241,245,0.92)', fontSize: 12, lineHeight: 14,
          formatter: function (p) {
            if (!p.data._d) return p.name;
            var name = p.name.length > 5 ? p.name.slice(0, 5) : p.name;
            var pct = p.data._d.change_pct;
            return name + '\n' + (pct >= 0 ? '+' : '') + (pct == null ? '—' : pct.toFixed(1) + '%');
          }
        },
        itemStyle: { borderColor: '#0D0F14', borderWidth: 1, gapWidth: 1.5 },
        emphasis: { itemStyle: { borderColor: '#0A84FF', borderWidth: 1.5 } },
        levels: [{ itemStyle: { borderWidth: 0 } }]
      }],
      tooltip: Object.assign(miTheme().tooltip, {
        formatter: function (p) {
          if (!p.data._d) return p.name;
          var d = p.data._d;
          var chg = d.change_pct;
          var chgColor = chg == null ? '#8B8F9A' : (chg >= 0 ? '#F6465D' : '#2EBD85');
          var net = d.main_net_yi;
          return '<div style="font-weight:700">' + escapeHtml(d.name) + ' <span style="color:#8B8F9A">' +
            escapeHtml(d.code) + '</span></div>' +
            '<div style="color:' + chgColor + ';font-weight:600">' + (chg == null ? '—' : (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%') +
            '</div>' +
            '<div>第三方资金净额代理 <b>' + (net == null ? '—' : (net >= 0 ? '+' : '') + net.toFixed(2) + '亿') + '</b>' +
            (d.main_net_pct == null ? '' : ' · 净占比 ' + (d.main_net_pct > 0 ? '+' : '') + d.main_net_pct.toFixed(2) + '%') + '</div>' +
            '<div>涨 ' + (d.up_count == null ? '—' : d.up_count) + ' 家 · 跌 ' +
            (d.down_count == null ? '—' : d.down_count) + ' 家</div>';
        }
      })
    }));
    registerChart('sectorTreemap', chart);
    return chart;
  }

  function renderSectorTable(el, items) {
    if (!el) return;
    if (!items || !items.length) {
      el.innerHTML = empty('板块表格不可用', '当前没有可核对的板块行。');
      return;
    }
    el.innerHTML = '<div class="table-wrap"><table class="data-table"><caption class="sr-only">板块热力图数据替代表</caption>' +
      '<thead><tr><th scope="col">板块</th><th scope="col">涨跌幅</th><th scope="col">资金净额代理</th>' +
      '<th scope="col">上涨家数</th><th scope="col">下跌家数</th><th scope="col">数据时点</th></tr></thead><tbody>' +
      items.map(function (row) {
        return '<tr><th scope="row">' + escapeHtml(row.name || '名称不可用') + '</th>' +
          '<td class="num ' + changeClass(row.change_pct) + '">' + signedPct(row.change_pct) + '</td>' +
          '<td class="num ' + changeClass(row.main_net_yi) + '">' + signedYi(row.main_net_yi) + '</td>' +
          '<td class="num">' + (row.up_count == null ? '—' : escapeHtml(row.up_count)) + '</td>' +
          '<td class="num">' + (row.down_count == null ? '—' : escapeHtml(row.down_count)) + '</td>' +
          '<td>' + timeText(row.data_as_of || row.as_of) + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  function renderFundRank(el, industries) {
    if (!el) return;
    if (!industries || !industries.length) { el.innerHTML = empty('第三方资金观察榜暂不可用', '板块资金数据暂缺。'); return; }
    var sides = fundRankSides(industries);
    if (!sides.observations) { el.innerHTML = empty('第三方资金观察榜暂不可用', '资金净额均缺失，未按零值排序。'); return; }
    var inflow = sides.inflow;
    var outflow = sides.outflow;
    function rows(list, tone) {
      if (!list.length) return '<div class="intel-empty">当前无符合符号的板块</div>';
      var max = Math.max.apply(null, list.map(function (r) { return Math.abs(Number(r.main_net_yi)); }));
      return list.map(function (r) {
        var v = Number(r.main_net_yi);
        var w = max ? Math.max(8, Math.abs(v) / max * 100) : 8;
        return '<div class="fr-row"><span class="fr-name">' + escapeHtml(r.name) + '</span>' +
          '<div class="fr-track"><div class="fr-fill ' + tone + '" style="width:' + w.toFixed(0) + '%"></div></div>' +
          '<span class="fr-val ' + (v >= 0 ? 'c-bull' : 'c-bear') + '">' + signedYi(v) + '</span></div>';
      }).join('');
    }
    el.innerHTML =
      '<div class="fr-block"><div class="fr-block-title c-bull">净流入 Top5</div>' + rows(inflow, 'in') + '</div>' +
      '<div class="fr-block" style="margin-top:18px"><div class="fr-block-title c-bear">净流出 Top5</div>' + rows(outflow, 'out') + '</div>' +
      '<div class="intel-meta">第三方行业资金净额代理观察（东方财富 push2delay，不能识别真实资金主体）；不进入M_t、个股评分、买点或仓位。</div>';
  }

  function fundRankSides(industries) {
    var sorted = (industries || []).filter(function (row) {
      return row.main_net_yi != null && isFinite(Number(row.main_net_yi));
    }).sort(function (a, b) { return Number(b.main_net_yi) - Number(a.main_net_yi); });
    return {
      observations: sorted.length,
      inflow: sorted.filter(function (row) { return Number(row.main_net_yi) > 0; }).slice(0, 5),
      outflow: sorted.filter(function (row) { return Number(row.main_net_yi) < 0; }).slice(-5).reverse()
    };
  }

  function renderIndexCards(el, items) {
    if (!el) return;
    if (!items || !items.length) { el.innerHTML = empty('指数数据暂不可用', '指数报价或走势线暂缺。'); return; }
    el.innerHTML = items.map(function (it) {
      var chg = it.change_pct;
      var spark = it.spark || [];
      var sparkColor = chg == null ? 'rgba(255,255,255,0.3)' : (chg >= 0 ? '#F6465D' : '#2EBD85');
      var health = it.health_status === 'verified' ? '<span class="idx-dot ok"></span>' :
        (it.health_status === 'delayed' ? '<span class="idx-dot delay"></span>' : '<span class="idx-dot bad"></span>');
      return '<div class="idx-card"><div class="idx-top"><span class="idx-icon">' + icon('line') +
        '</span><span class="idx-name">' + escapeHtml(it.name) + '</span>' + health + '</div>' +
        '<div class="idx-chg ' + changeClass(chg) + '">' + signedPct(chg) + '</div>' +
        '<div class="idx-price">' + num(it.price, 2) + '</div>' +
        sparklineSVG(spark, sparkColor) +
        '<div class="intel-meta">来源：' + escapeHtml(it.source || '未提供') +
        ' · 时点：' + timeText(it.as_of || it.data_as_of) + '</div></div>';
    }).join('');
  }

  function breadthBarWidths(breadth) {
    var sample = Number(breadth && breadth.sample_size);
    var up = breadth && typeof breadth.up_ratio === 'number' ? breadth.up_ratio : NaN;
    var flat = Number(breadth && breadth.flat);
    return {
      up: Number.isFinite(up) ? Math.max(0, Math.min(100, up)) : 0,
      flat: Number.isFinite(sample) && sample > 0 && Number.isFinite(flat) ?
        Math.round(Math.max(0, Math.min(100, flat / sample * 100)) * 100) / 100 : 0
    };
  }

  function visibleRiskAlerts(alerts) {
    return (alerts || []).filter(function (alert) { return alert && alert.state === 'active'; }).slice(0, 3);
  }

  function panoramaStatusLabel(data) {
    if (!data) return '数据不可用';
    if (data.status === 'ok') return '已返回证据可用，交易所全集待核';
    if (data.status === 'partial' || data.status === 'degraded') return '部分证据可用';
    return '数据不可用';
  }

  function todayVisual(data, sectors, news, indexSpark, researchState) {
    var breadth = data.breadth || {};
    var aggregateReady = breadth.aggregate_eligible === true;
    var referenceExpected = breadth.reference_universe_expected;
    var referenceReturned = breadth.reference_universe_returned;
    var coverageNote = Number.isInteger(referenceExpected) && Number.isInteger(referenceReturned)
      ? '参考池已返回 ' + referenceReturned + ' / ' + referenceExpected + '；交易所全集未知。'
      : '参考池报价覆盖待核；交易所全集未知。';
    var coverageWarning = !aggregateReady
      ? '<div class="intel-notice"><strong>参考池总体指标未评估。</strong> ' +
        escapeHtml(coverageNote + ' ' + (breadth.aggregate_issue || '来源时点未核。')) +
        '下方原始涨跌计数仅代表已返回证券，不能外推市场方向。</div>' : '';
    var flow = data.fund_flow || {};
    var risk = data.risk || {};
    var industries = (sectors && sectors.industries) || [];
    var concepts = (sectors && sectors.concepts) || [];
    var newsItems = dedupeNews((news && news.items) || [], 12);

    var research = researchState || {};
    var decision = research.decision_state || {};
    var stateLabels = {
      risk_closed:'风险关闭', weak_repair:'弱修复', sentiment_rising:'情绪上升',
      crowded_climax:'高潮拥挤', retreat:'退潮防守', unknown:'尚未形成'
    };
    var decisionLabel = stateLabels[decision.state] || decision.state || '尚未形成';
    var decisionAction = {
      risk_closed:'不开新仓，只处理持仓风险',
      weak_repair:'仅观察确认型机会，不因修复预期放大仓位',
      sentiment_rising:'等待盘中板块确认后再检查战法与个股',
      crowded_climax:'不追后排，等待分歧与成交确认',
      retreat:'不开新仓，优先持仓风险复核',
      unknown:'证据不足，今日不据此交易'
    }[decision.state] || '证据不足，今日不据此交易';
    var researchStrip = '<div class="intel-notice"><strong>当前决策状态 ' +
      escapeHtml(decisionLabel) + ' · ' + escapeHtml(decisionAction) + '。</strong> ' +
      escapeHtml(decision.semantics || '该状态与今日决策共用，但不单独授权交易。') +
      ' 数据截至 ' + escapeHtml(timeText(decision.as_of)) +
      '；旧研究模型原值仍保留在证据详情中，不再作为本页主结论。</div>';
    // 描述性观察 KPI 条；不把单一广度转换为市场状态。
    var gaugeWrap = '<div class="card intel-kpi gauge-kpi"><div id="sentimentGauge" style="height:104px"></div>' +
      '<div class="intel-kpi-label">参考广度观察 · 非市场状态</div></div>';
    var barWidths = breadthBarWidths(breadth);
    var upBar = !aggregateReady ? '<div class="kpi-sub">' + escapeHtml(coverageNote) +
        '已返回：涨 ' + (breadth.up == null ? '—' : breadth.up) + ' · 跌 ' +
        (breadth.down == null ? '—' : breadth.down) + ' · 平 ' +
        (breadth.flat == null ? '—' : breadth.flat) + '；仅为原始计数。</div>' :
      (breadth.up == null && breadth.down == null) ? '' :
      '<div class="kpi-bar"><div class="kpi-bar-up" style="width:' + barWidths.up + '%"></div>' +
      '<div class="kpi-bar-flat" style="width:' + barWidths.flat + '%"></div></div>' +
      '<div class="kpi-sub">涨 ' + (breadth.up == null ? '—' : breadth.up) + ' · 跌 ' +
      (breadth.down == null ? '—' : breadth.down) + ' · 平 ' + (breadth.flat == null ? '—' : breadth.flat) + '</div>';
    var mainNetSub = '<div class="kpi-sub">近5日 ' + signedYi(flow.five_day_main_net_yi) +
      ' · 本月 ' + signedYi(flow.mtd_main_net_yi) + '</div>';
    var limitRule = breadth.price_limit || {};
    var limitAudit = limitRule.evaluable_count == null ? '有效涨跌停价未返回' :
      '上游有效限价 ' + limitRule.evaluable_count + ' / 有价格样本 ' + (limitRule.reference_count == null ? '—' : limitRule.reference_count) +
      ' · 时点 ' + timeText(limitRule.as_of);

    var kpis = '<div class="intel-health">' + gaugeWrap +
      '<div class="card intel-kpi"><div class="intel-kpi-value c-bull" id="kpiUpRatio">—</div>' +
      '<div class="intel-kpi-label">参考池上涨占比 · 全集未知</div>' + upBar + '</div>' +
      '<div class="card intel-kpi"><div class="intel-kpi-value" id="kpiLimit">—</div>' +
      '<div class="intel-kpi-label">涨停 / 跌停</div>' +
      '<div class="kpi-sub">' + escapeHtml(limitAudit) + '；ST、板块与上市初期限价由逐证券上游字段决定</div></div>' +
      '<div class="card intel-kpi"><div class="intel-kpi-value" id="kpiMainNet">—</div>' +
      '<div class="intel-kpi-label">第三方资金净额代理观察 · 不参与决策</div>' + mainNetSub + '</div>' +
      '</div>';

    var indexSection = '<div class="idx-grid" id="panoramaIndexes"></div>';

    var treemapSection =
      '<div class="intel-grid equal"><article class="card">' +
      '<div class="card-header"><h3 class="card-title">板块热力图</h3>' +
      '<div class="sector-tabs" id="sectorTabs">' +
      '<button type="button" class="sector-tab' + (sectorKind === 'industry' ? ' active' : '') + '" data-kind="industry" aria-pressed="' + (sectorKind === 'industry' ? 'true' : 'false') + '">行业 ' + industries.length + '</button>' +
      '<button type="button" class="sector-tab' + (sectorKind === 'concept' ? ' active' : '') + '" data-kind="concept" aria-pressed="' + (sectorKind === 'concept' ? 'true' : 'false') + '">概念 ' + concepts.length + '</button>' +
      '</div></div>' +
      '<div class="card-body"><div id="sectorTreemap" style="height:430px"></div></div>' +
      '<details class="evidence-details"><summary>查看板块热力图数据表</summary><div id="sectorTreemapTable"></div></details>' +
      '<div class="intel-meta" style="padding:0 20px 14px" id="sectorNote">瓦片大小=已报告涨跌的成分家数（缺失计数的板块不生成瓦片），颜色=涨跌幅（红涨绿跌）。来源：' +
      escapeHtml((sectors && sectors.source) || '东方财富板块实时') + '</div></article>' +
      '<article class="card"><div class="card-header"><h3 class="card-title">第三方板块资金净额代理观察</h3><span class="card-badge">不参与决策</span></div>' +
      '<div class="card-body" id="fundRank"></div></article></div>';

    var newsReturned = news && news.meta && news.meta.returned_count;
    var newsSection =
      '<article class="card"><div class="card-header"><h3 class="card-title">实时新闻与事件（重大性逐条披露）</h3>' +
      '<span class="card-badge" id="newsCount">' + (newsReturned == null ? newsItems.length : newsReturned) +
      ' 返回 · ' + newsItems.length + ' 展示</span></div>' + newsHealthSummary(news) +
      '<div class="news-ticker" id="newsTicker"></div>' +
      '<div class="card-body" id="newsList"></div></article>';

    var activeAlerts = visibleRiskAlerts(risk.alerts);
    var alertsHtml = activeAlerts.length ? activeAlerts.map(function (alert) {
      return '<span class="intel-effect warn">' + escapeHtml(alert.type) + '</span>';
    }).join('') : '';

    var out = coverageWarning + researchStrip + kpis + indexSection + treemapSection + newsSection;
    out += flowAuditLine(flow) + '<div class="intel-meta" style="margin-top:12px">' + alertsHtml +
      (alertsHtml ? '　' : '') + '决策参与：否。' + escapeHtml(data.decision_policy || '') + '</div>';

    // 一次性注入后按数据填充
    return out;
  }

  function refreshPanorama() {
    var sequence = ++refreshSequence;
    var tabs = document.querySelectorAll('.intel-tab');
    tabs.forEach(function (tab) {
      var active = tab.dataset.dimension === panoramaDimension;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
    });
    if (panoramaDimension !== 'today') {
      return getJson('/api/market/panorama?dimension=' + encodeURIComponent(panoramaDimension)).then(function (data) {
        if (sequence !== refreshSequence) return;
        var target = one('#panoramaContent');
        if (!data) { target.innerHTML = empty('请求失败', '未得到可解析的市场全景响应。'); return; }
        one('#panoramaNotice').innerHTML = '<strong>' + panoramaStatusLabel(data) + '。</strong> ' +
          escapeHtml(data.reason || data.decision_policy || '');
        target.innerHTML = panoramaDimension === 'month' ? monthView(data) : overallView(data);
      });
    }
    return Promise.all([
      getJson('/api/market/panorama?dimension=today'),
      getJson('/api/market/sectors'),
      getJson('/api/news/live'),
      getJson('/api/market/index-spark'),
      getJson('/api/market/research-state')
    ]).then(function (results) {
      if (sequence !== refreshSequence) return;
      var data = results[0], sectors = results[1], news = results[2], indexSpark = results[3], researchState = results[4];
      var target = one('#panoramaContent');
      if (!data) {
        one('#panoramaNotice').innerHTML = '<strong>市场全景暂不可用。</strong> 没有以空白或示例数据替代。';
        target.innerHTML = empty('请求失败', '未得到可解析的市场全景响应。');
        return;
      }
      var unavailableComponents = [
        (!sectors || sectors.status === 'unavailable') ? '板块' : '',
        (!news || news.status === 'unavailable') ? '新闻' : '',
        (!indexSpark || indexSpark.status === 'unavailable') ? '指数走势' : ''
      ].filter(Boolean);
      var decisionState = (researchState && researchState.decision_state) || {};
      var decisionStateLabels = {
        risk_closed:'风险关闭', weak_repair:'弱修复', sentiment_rising:'情绪上升',
        crowded_climax:'高潮拥挤', retreat:'退潮防守', unknown:'尚未形成'
      };
      one('#panoramaNotice').innerHTML = '<strong>' +
        escapeHtml('当前决策状态 ' + (decisionStateLabels[decisionState.state] || decisionState.state || '尚未形成')) +
        '。</strong> ' + escapeHtml(decisionState.semantics || '证据不足，不据此交易。') +
        (unavailableComponents.length ? ' 子源不可用：' + escapeHtml(unavailableComponents.join('、')) + '。' : '') +
        mixedAsOfWarning([
          data.as_of, data.source && data.source.as_of, sectors && sectors.as_of,
          news && news.as_of, indexSpark && indexSpark.as_of
        ]);

      target.innerHTML = todayVisual(data, sectors, news, indexSpark, researchState);

      // 情绪 KPI 数字（count-up）
      var upRatio = data.breadth && data.breadth.aggregate_eligible === true
        ? data.breadth.up_ratio : null;
      var kpiUp = one('#kpiUpRatio');
      renderOptionalPercent(kpiUp, upRatio);
      var kpiLimit = one('#kpiLimit');
      if (kpiLimit) kpiLimit.textContent = data.breadth && data.breadth.aggregate_eligible === true
        ? (data.breadth.limit_up != null ? data.breadth.limit_up : '—') +
          ' / ' + (data.breadth.limit_down != null ? data.breadth.limit_down : '—') : '— / —';
      var kpiNet = one('#kpiMainNet');
      if (kpiNet) {
        var mn = data.fund_flow && data.fund_flow.main_net_yi;
        if (mn != null) {
          kpiNet.className = 'intel-kpi-value ' + changeClass(mn);
          countUp(kpiNet, mn, function (v) { return (v >= 0 ? '+' : '') + v.toFixed(1) + '亿'; });
        } else { kpiNet.textContent = '—'; }
      }
      // 每个渲染独立 try/catch：任一图表失败绝不影响其余组件
      function safe(fn, target, label) {
        try { fn(); }
        catch (e) {
          reportIssue('component-render ' + label, e);
          if (target) target.innerHTML = empty(label + '渲染失败', '请刷新本页重试；其他模块不受影响。');
        }
      }
      safe(function () {
        renderSentimentGauge(one('#sentimentGauge'), data.sentiment && data.sentiment.score, '参考广度 · 非状态');
      }, one('#sentimentGauge'), '参考广度图表');
      safe(function () {
        renderIndexCards(one('#panoramaIndexes'), (indexSpark && indexSpark.items) || []);
      }, one('#panoramaIndexes'), '指数卡片');
      safe(function () {
        var sectorRows = sectorKind === 'concept' ? (sectors && sectors.concepts || []) : (sectors && sectors.industries || []);
        renderSectorTreemap(one('#sectorTreemap'), sectorRows);
        renderSectorTable(one('#sectorTreemapTable'), sectorRows);
      }, one('#sectorTreemap'), '板块热力图');
      safe(function () {
        renderFundRank(one('#fundRank'), (sectors && sectors.industries) || []);
      }, one('#fundRank'), '资金榜');
      safe(function () {
        renderNewsTicker(one('#newsTicker'), news ? news.items : null);
      }, one('#newsTicker'), '新闻滚动条');
      safe(function () {
        renderNewsFeed(one('#newsList'), news ? news.items : null, 10);
      }, one('#newsList'), '新闻列表');

      // 板块 tab 切换（委托一次）
      var tabsEl = one('#sectorTabs');
      if (tabsEl && !tabsEl.dataset.bound) {
        tabsEl.dataset.bound = '1';
        tabsEl.addEventListener('click', function (e) {
          var btn = e.target.closest('.sector-tab');
          if (!btn) return;
          sectorKind = btn.dataset.kind;
          try {
            var sectorRows = sectorKind === 'concept' ? (sectors && sectors.concepts || []) : (sectors && sectors.industries || []);
            renderSectorTreemap(one('#sectorTreemap'), sectorRows);
            renderSectorTable(one('#sectorTreemapTable'), sectorRows);
          } catch (err) {
            reportIssue('sector-switch', err);
            var target = one('#sectorTreemap');
            if (target) target.innerHTML = empty('板块热力图渲染失败', '请重新选择板块或刷新页面。');
          }
          tabsEl.querySelectorAll('.sector-tab').forEach(function (t) {
            var active = t.dataset.kind === sectorKind;
            t.classList.toggle('active', active);
            t.setAttribute('aria-pressed', active ? 'true' : 'false');
          });
        });
      }
    });
  }

  function tabDestinationIndex(key, index, length) {
    if (!length) return -1;
    if (key === 'Home') return 0;
    if (key === 'End') return length - 1;
    if (key === 'ArrowLeft') return (index - 1 + length) % length;
    if (key === 'ArrowRight') return (index + 1) % length;
    return -1;
  }

  function renderPanorama() {
    stop();
    disposeMiCharts();
    var container = one('[data-page="panorama"]');
    if (!container) return;
    // Local research previews have no enabled current-market collectors. A
    // historical view must not trigger the old cold-source fetches or timer.
    if (document.body && document.body.getAttribute('data-local-research-preview') === 'true') {
      container.innerHTML = window.RegisSpa.core.marketCenterNav('panorama') +
        '<section><h2>市场研究 · 已保存的历史数据</h2><p>本机预览展示已核验来源的历史统计。实时市场、行业和概念板块快照尚未接入，此页不启动外部采集。</p></section>';
      var localHost = document.createElement('div');
      container.appendChild(localHost);
      if (window.LocalMarketHistory) window.LocalMarketHistory.mount(localHost);
      else localHost.textContent = '历史统计组件未载入，请刷新页面。';
      return;
    }
    container.innerHTML = panoramaLayout();
    if (window.LocalMarketHistory) {
      var historyHost = document.createElement('div');
      container.prepend(historyHost);
      window.LocalMarketHistory.mount(historyHost);
    }
    var dimensionTabs = container.querySelectorAll('.intel-tab');
    dimensionTabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        if (tab.dataset.dimension === panoramaDimension) return;
        panoramaDimension = tab.dataset.dimension;
        cancelRequests();
        var panel = one('#panoramaContent');
        if (panel) panel.setAttribute('aria-labelledby', tab.id);
        refreshPanorama();
      });
      tab.addEventListener('keydown', function (event) {
        var tabs = Array.prototype.slice.call(dimensionTabs);
        var index = tabs.indexOf(tab);
        var destination = tabDestinationIndex(event.key, index, tabs.length);
        if (destination < 0) return;
        var next = tabs[destination];
        event.preventDefault(); next.focus(); next.click();
      });
    });
    refreshPanorama();
    timer = window.setInterval(refreshPanorama, 60000);
  }

  // 保留：本月 / 总体视图（次级 tab）

  function monthView(data) {
    var indices = data.indices || [];
    var index = indices[0];
    var risk = data.risk || {};
    if (!index) return '<article class="card"><div class="card-body">' + empty('本月结论未输出', data.reason, data.source) + '</div></article>';
    var flow = data.fund_flow || {};
    var flowBadge = flow.main_net_yi == null
      ? '来源可追溯 · 资金当前不可用'
      : '第三方来源 · 仅观察';
    var currentBreadth = (data.breadth || {}).current_snapshot || {};
    var rows = indices.map(function (item) {
      return '<tr><td><strong>' + escapeHtml(item.name) + '</strong></td><td class="num ' + changeClass(item.mtd_change_pct) + '">' + signedPct(item.mtd_change_pct) +
        '</td><td class="num">' + num(item.month_high, 2) + '</td><td class="num">' + num(item.month_low, 2) + '</td><td class="num">' + num(item.monthly_drawdown_pct, 2) + '%</td><td>' + healthChip(item) + '</td></tr>';
    }).join('');
    return '<div class="intel-health">' +
      kpi(index.mtd_change_pct == null ? '—' : signedPct(index.mtd_change_pct), index.month + ' 月初至今', changeClass(index.mtd_change_pct)) +
      kpi(signedYi(flow.mtd_main_net_yi), '第三方资金代理月度累计', changeClass(flow.mtd_main_net_yi)) +
      kpi(risk.monthly_drawdown_pct == null ? '—' : num(risk.monthly_drawdown_pct, 2) + '%', '月内最大回撤', 'c-bear') +
      '</div><div class="intel-grid equal"><article class="card"><div class="card-header"><h3 class="card-title">月度指数表现</h3><span class="card-badge">腾讯公开日线</span></div><div class="card-body no-pad"><div class="table-wrap"><table class="data-table"><thead><tr><th>指数</th><th>月初至今</th><th>月高</th><th>月低</th><th>最大回撤</th><th>状态</th></tr></thead><tbody>' + rows + '</tbody></table></div></div></article>' +
      '<article class="card"><div class="card-header"><h3 class="card-title">月度资金与广度</h3><span class="card-badge">' + escapeHtml(flowBadge) + '</span></div><div class="card-body">' +
      (flow.main_net_yi == null ? empty('第三方资金代理日线不可用', flow.reason, flow) : '<div class="metric-grid"><div><span class="num">' + signedYi(flow.main_net_yi) + '</span><div class="c-dim">最近交易日·不参与决策</div></div><div><span class="num">' + signedYi(flow.five_day_main_net_yi) + '</span><div class="c-dim">近5日</div></div><div><span class="num">' + escapeHtml(flow.label || '—') + '</span><div class="c-dim">第三方观察方向</div></div></div>' + metaLine(flow) + flowAuditLine(flow) + northboundLine(flow)) +
      '<hr style="border:0;border-top:1px solid var(--border-subtle);margin:14px 0">' +
      (currentBreadth.up_ratio == null ? empty('广度月度序列仍在积累', (data.breadth || {}).reason, currentBreadth) : '<div class="intel-meta">最近交易日广度：上涨占比 ' + num(currentBreadth.up_ratio, 1) + '%，涨停 ' + escapeHtml(currentBreadth.limit_up) + '。月度逐日广度从本次上线起积累，不将单日快照伪装成月均值。</div>' + metaLine(currentBreadth)) +
      '</div></article></div>';
  }

  function overallView(data) {
    var trend = data.trend;
    if (!trend) return '<article class="card"><div class="card-body">' + empty('总体结论未输出', data.reason, data.source) + '</div></article>';
    var valuation = data.valuation || {};
    var funds = data.long_term_flow || {};
    var style = data.style || {};
    var sentiment = data.sentiment || {};
    var risk = data.risk || {};
    var state = data.market_state || {};
    var stateRows = Object.keys(state.dimensions || {}).map(function (key) {
      return '<span class="intel-effect">' + escapeHtml(key) + '：' + escapeHtml(state.dimensions[key] || '未评估') + '</span>';
    }).join('');
    var stateHealthRows = Object.keys(state.dimension_health || {}).map(function (key) {
      var item = state.dimension_health[key] || {};
      return '<div class="intel-meta">' + escapeHtml(key) + '：' + healthChip(item) + '　' +
        escapeHtml(item.source || '来源未提供') + '　' + timeText(item.as_of) + '</div>';
    }).join('');
    return '<div class="intel-health">' +
      kpi(escapeHtml(trend.label || '—'), '长期趋势') +
      kpi(valuation.aggregate_pe_ttm == null ? '—' : num(valuation.aggregate_pe_ttm, 2), '全市场市值加权PE') +
      kpi(sentiment.score == null ? '—' : num(sentiment.score, 1), '当日参考广度观察·非状态') +
      '</div><div class="intel-grid equal"><article class="card"><div class="card-header"><h3 class="card-title">趋势与五维状态</h3><span class="card-badge">固定描述规则</span></div><div class="card-body"><div class="metric-grid"><div><span class="num">' + num(trend.close, 2) + '</span><div class="c-dim">最新收盘</div></div><div><span class="num">' + num(trend.ma20, 2) + '</span><div class="c-dim">MA20</div></div><div><span class="num">' + num(trend.ma60, 2) + '</span><div class="c-dim">MA60</div></div></div>' + metaLine(data.source) +
      '<div class="intel-notice" style="margin-top:14px"><strong>' + escapeHtml(state.label || '证据不足') + '。</strong> ' + escapeHtml(state.reason || '') + '<div class="intel-event-effects">' + stateRows + '</div><div style="margin-top:8px">' + stateHealthRows + '</div></div></div></article>' +
      '<article class="card"><div class="card-header"><h3 class="card-title">估值与资金</h3><span class="card-badge">横截面+日线</span></div><div class="card-body">' +
      (valuation.aggregate_pe_ttm == null ? empty('估值未输出', valuation.reason, valuation) : '<div class="metric-grid"><div><span class="num">' + num(valuation.aggregate_pe_ttm, 2) + '</span><div class="c-dim">市值加权PE</div></div><div><span class="num">' + num(valuation.aggregate_pb, 2) + '</span><div class="c-dim">市值加权PB</div></div><div><span class="num">' + escapeHtml(valuation.label || '—') + '</span><div class="c-dim">当前截面</div></div></div><div class="intel-meta">PE覆盖 ' + escapeHtml(valuation.pe_coverage) + ' 家；PB覆盖 ' + escapeHtml(valuation.pb_coverage) + ' 家。' + escapeHtml(valuation.reason || '') + '</div>' + metaLine(valuation)) +
      '<hr style="border:0;border-top:1px solid var(--border-subtle);margin:14px 0">' +
      (funds.main_net_yi == null ? empty('第三方资金代理日线未输出', funds.reason, funds) : '<div class="metric-grid"><div><span class="num">' + signedYi(funds.main_net_yi) + '</span><div class="c-dim">最近交易日</div></div><div><span class="num">' + signedYi(funds.mtd_main_net_yi) + '</span><div class="c-dim">本月累计</div></div><div><span class="num">' + escapeHtml(funds.label || '—') + '</span><div class="c-dim">代理方向</div></div></div>' + metaLine(funds) + flowAuditLine(funds)) + '</div></article></div>' +
      '<div class="intel-grid equal"><article class="card"><div class="card-header"><h3 class="card-title">风格与情绪</h3><span class="card-badge">不作预测</span></div><div class="card-body">' +
      (style.large_vs_small_1m_pct == null ? empty('风格未输出', style.reason, style) : '<div class="metric-grid"><div><span class="num">' + signedPct(style.large_vs_small_1m_pct) + '</span><div class="c-dim">大盘-小盘 1月</div></div><div><span class="num">' + signedPct(style.growth_vs_dividend_1m_pct) + '</span><div class="c-dim">成长-红利 1月</div></div><div><span class="num">' + escapeHtml(sentiment.label || '—') + '</span><div class="c-dim">当日温度</div></div></div><div class="intel-meta">' + escapeHtml(style.method || '') + '</div>' + metaLine(style)) + '</div></article>' +
      '<article class="card"><div class="card-header"><h3 class="card-title">长期风险</h3><span class="card-badge">上证近250日</span></div><div class="card-body"><div class="metric-grid"><div><span class="num">' + num(risk.annualized_volatility_pct, 2) + '%</span><div class="c-dim">年化波动率</div></div><div><span class="num c-bear">' + num(risk.one_year_max_drawdown_pct, 2) + '%</span><div class="c-dim">最大回撤</div></div><div><span class="num">' + signedPct(risk.one_year_return_pct) + '</span><div class="c-dim">近1年收益</div></div></div><div class="intel-meta">' + escapeHtml(risk.method || '') + '</div></div></article></div>';
  }

  // P4-2e：北向资金可用/不可用如实披露（沿用）
  function northboundLine(flow) {
    if (!flow || !flow.northbound) return '';
    var nb = flow.northbound;
    if (nb.status === 'ok' && nb.net_buy_yi != null) {
      return '<div class="intel-meta" style="margin-top:8px">北向资金：' +
        signedYi(nb.net_buy_yi) + '（' + escapeHtml(nb.reason || '最新交易）') + '</div>';
    }
    return '<div class="intel-meta" style="margin-top:8px">北向资金：' +
      escapeHtml(nb.reason || '数据暂缺，未计入结论') + '</div>';
  }

  var resizeTimer = null;
  function resize() {
    window.requestAnimationFrame(function () {
      Object.keys(miCharts).forEach(function (key) {
        var chart = miCharts[key];
        try { if (chart && (!chart.isDisposed || !chart.isDisposed())) chart.resize(); }
        catch (error) { reportIssue('resize', error); }
      });
    });
  }
  window.addEventListener('resize', function () {
    if (resizeTimer) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(resize, 150);
  });

  window.MarketIntel = { stop: stop, renderGlobal: renderGlobal, renderPanorama: renderPanorama, resize: resize };
})();
