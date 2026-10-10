/* ═══════════════════════════════════════════════════════════════
   Regis Oracle — Premium Motion Layer · 高级感动效增强层（JS）
   2026-08-18

   职责（叠加层，零布局改动）：
   1. 入场编排 —— 顶层卡片错峰上浮（.px-in + --px-d 延迟）。
   2. 数字滚动 —— 首屏关键数值 count-up（easeOutCubic）。
   3. 点击涟漪 —— 按钮按下注入 span.px-ripple。

   约束：
   · 尊重 prefers-reduced-motion（直接整体退出）。
   · 只做 DOM 增删 class/span 与 textContent 滚动，不改结构/布局。
   · 用 WeakSet 去重，容忍 innerHTML 高频重渲染（多轮扫描窗口收敛后即停，
     后续重渲染不再触发入场，避免每 8s/10s 卡片再次淡入）。
   ═══════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return; // 用户偏好减少动效：本层完全静止（CSS 侧另有 @media 兜底）
  }

  /* ── 1. 数字滚动（count-up） ─────────────────────────────── */
  var counted = new WeakSet();
  // 前缀(可选) + 符号(可选) + 数字 + 后缀(可选)，容忍 "¥"、"+"、"%"/"万"/"亿" 等
  var NUM_RE = /^([^\d.+-]*)([+-]?)(\d+(?:\.\d+)?)([^\d]*)$/;

  function parseNumber(text) {
    if (text == null) return null;
    var m = NUM_RE.exec(String(text).trim());
    if (!m) return null;
    return {
      prefix: m[1],
      sign: m[2],
      num: parseFloat(m[3]),
      decimals: (m[3].split('.')[1] || '').length,
      suffix: m[4],
    };
  }

  function countUp(el) {
    if (!el || counted.has(el)) return;
    var parts = parseNumber(el.textContent);
    counted.add(el);
    if (!parts || !isFinite(parts.num) || parts.num === 0) return;

    // 裸整数 100~9999（无单位/符号/小数，疑似年份、股数、代码）不滚动，避免 0→2021 视觉错误
    var bare = parts.decimals === 0 && !parts.sign && !parts.prefix && !parts.suffix;
    if (bare && parts.num >= 100 && parts.num < 10000) return;

    var target = parts.num;
    var dur = 650 + Math.min(550, Math.abs(target) * 1.5);
    var t0 = null;
    function frame(ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min(1, (ts - t0) / dur);
      var eased = 1 - Math.pow(1 - p, 3); // easeOutCubic
      el.textContent = parts.prefix + parts.sign + (target * eased).toFixed(parts.decimals) + parts.suffix;
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  var COUNT_SEL = [
    '.sum-val', '.stat-value', '.metric-value', '.method-ann', '.kronos-ret',
    '.itc-score-number', '.itc-quote-value', '.slab-metric .v',
    '.pos-cur', '.pos-pnl', '.fs-kpi-v', '.live-score-big', '.cp-price', '.cp-verdict',
  ].join(',');

  /* ── 2. 入场编排（卡片错峰上浮） ─────────────────────────── */
  var CARD_SEL = [
    '.widget', '.sum-item', '.stat-card', '.metric-item', '.method-card',
    '.lens-card', '.pos-item', '.kronos-row', '.card', '.slab-card',
    '.itc-panel', '.itc-quote-cell', '.fs-kpi', '.fs-zone', '.live-card',
    '.cp-gate', '.signal-card',
  ].join(',');

  var staggered = new WeakSet();

  function nestedInCard(el) {
    var p = el.parentElement;
    while (p) {
      if (p.matches && p.matches(CARD_SEL)) return true;
      p = p.parentElement;
    }
    return false;
  }

  function stagger(scope) {
    var root = scope || document;
    var cards = root.querySelectorAll(CARD_SEL);
    var top = [];
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      if (staggered.has(c) || nestedInCard(c)) continue; // 嵌套卡片随父级一起入场，不二次位移
      top.push(c);
    }
    for (var j = 0; j < top.length; j++) {
      var el = top[j];
      if (staggered.has(el)) continue;
      staggered.add(el);
      el.classList.add('px-in');
      el.style.setProperty('--px-d', Math.min(j * 55, 460));
    }

    var nums = root.querySelectorAll(COUNT_SEL);
    for (var k = 0; k < nums.length; k++) countUp(nums[k]);
  }

  // 多轮扫描：覆盖初始渲染 + 异步填充（行情/持仓/因子等）陆续挂载。
  // 收敛后即停 —— 之后高频重渲染（live_scan 8s / positions 10s / factor 30s）不再触发入场。
  // 末两轮 5s/6.5s 兜底慢首刷（如全市场扫描 ~4s），且仍早于 8s 周期重渲染，避免重复入场。
  [0, 350, 900, 1800, 3200, 5000, 6500].forEach(function (ms) {
    setTimeout(function () { stagger(document); }, ms);
  });

  /* ── 3. 点击涟漪 ─────────────────────────────────────────── */
  document.addEventListener('click', function (e) {
    var t = e.target;
    var btn = t && t.closest ? t.closest('button, .btn, .preset-btn, .tf-tab, .itc-tab, .itc-quick-indicator, .filter-btn, .search-btn') : null;
    if (!btn || btn.disabled) return;
    var rect = btn.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) return;
    var d = Math.max(rect.width, rect.height) * 1.6;
    var span = document.createElement('span');
    span.className = 'px-ripple';
    span.style.width = span.style.height = d + 'px';
    span.style.left = (e.clientX - rect.left - d / 2) + 'px';
    span.style.top = (e.clientY - rect.top - d / 2) + 'px';
    btn.classList.add('px-ripple-host');
    btn.appendChild(span);
    setTimeout(function () {
      if (span.parentNode) span.parentNode.removeChild(span);
    }, 700);
  });

  // 可扩展入口（spa.html 不加载本文件，走自身脚本；此处保留供未来页面复用）
  window.PX = { stagger: stagger, countUp: countUp };
})();
