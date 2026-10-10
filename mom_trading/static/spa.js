(function(){
'use strict';
const RegisSpa = window.RegisSpa = {
  core: {},
  pages: Object.create(null),
  registerPage(pageId, pageModule) {
    if (!pageId || typeof pageModule?.render !== 'function') throw new Error('Invalid Regis SPA page module');
    this.pages[pageId] = pageModule;
  },
  renderPage(pageId, ...args) {
    const renderer = this.pages[pageId]?.render;
    if (typeof renderer !== 'function') throw new Error(`Regis SPA page module not registered: ${pageId}`);
    return renderer(...args);
  },
};
const $ = (s,c) => (c||document).querySelector(s);
const $$ = (s,c) => [...(c||document).querySelectorAll(s)];
const state = {
  page: 'dashboard',
  sidebarExpanded: true,
  marketLive: false,
  clockInterval: null,
  charts: {},
  dataCache: {},
  requests: new Map(),
  apiErrors: new Map(),
  navSequence: 0,
  navTimer: null,
  marketStatusTimer: null,
  oppGeneration: 0,
  kronosSnapshot: null,
  marketSessionPayload: null,
  homeWorkspace: 'review',
  homeAutoWorkspace: null,
  homeData: Object.create(null),
  homeRiskFetchedAt: 0,
  homeRiskSignature: '',
  homeRiskInFlight: false,
  homeRiskStatus: 'idle',
};
const navIcon = body => `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const ICONS = {
  grid: navIcon('<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
  bolt: navIcon('<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/>'), flask: navIcon('<path d="M9 3h6"/><path d="M10 3v5.5L4.7 17a2 2 0 0 0 1.8 3h11a2 2 0 0 0 1.8-3L14 8.5V3"/><path d="M7.5 14.5h9"/>'),
  flow: navIcon('<path d="M3 12h4l2.5-7 4 14 2.5-7h5"/>'), crosshair: navIcon('<circle cx="12" cy="12" r="7.5"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>'),
  shield: navIcon('<path d="M12 3l7 3v5c0 4.6-3 7.4-7 10-4-2.6-7-5.4-7-10V6l7-3z"/><path d="M9 12l2 2 4-4"/>'), globe: navIcon('<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.6 2.5 4 5.5 4 9s-1.4 6.5-4 9c-2.6-2.5-4-5.5-4-9s1.4-6.5 4-9z"/>'),
  columns: navIcon('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/><path d="M15 4v16"/>'), stock: navIcon('<path d="M3 3v18h18"/><path d="M8 15v3"/><path d="M13 10v8"/><path d="M18 6v12"/>'),
};
const NAV_ITEMS = [
  { id:'dashboard',   label:'今日决策',  sidebarLabel:'今日决策', sidebarGroup:'决策', icon:ICONS.grid, title:'今日决策' },
  { id:'plan', label:'明日计划', sidebarLabel:'明日计划', sidebarGroup:'决策', icon:ICONS.columns, title:'明日计划 · 下一交易日准备' },
  { id:'risk', label:'风险与仓位', sidebarLabel:'风险与仓位', sidebarGroup:'决策', icon:ICONS.shield, title:'风险与仓位控制' },
  { id:'review', label:'盘后复盘', sidebarLabel:'盘后复盘', sidebarGroup:'决策', icon:ICONS.stock, title:'盘后复盘 · 成本与执行归因' },
  { id:'signals', label:'买卖提醒记录', sidebarLabel:'买卖提醒记录', sidebarGroup:'决策', icon:ICONS.bolt, title:'买卖提醒记录' },
  { id:'fundflow',    label:'资金流',    sidebarHidden:true, icon:ICONS.flow, title:'资金流排名' },
  { id:'scanner',     label:'扫描器',    sidebarHidden:true, icon:ICONS.crosshair, title:'股票扫描器' },
  { id:'panorama',    label:'市场中心',  sidebarLabel:'市场中心', sidebarGroup:'市场', icon:ICONS.columns, title:'市场中心' },
  { id:'stock',       label:'个股工作台',sidebarLabel:'个股研究', sidebarGroup:'研究', icon:ICONS.stock, title:'个股工作台', href:'/stock' },
  { id:'strategy-lab',label:'策略研发',  sidebarLabel:'策略与战法', sidebarGroup:'研究', icon:ICONS.flask, title:'策略研发 · 科学回测中心（统一入口）', href:'/strategy-lab' },
  { id:'factor-system',label:'因子挖掘', sidebarLabel:'因子研究', sidebarGroup:'研究', icon:ICONS.flask, title:'因子挖掘', href:'/factor-system' },
  { id:'positions',   label:'账户中心',  sidebarLabel:'账户中心', sidebarGroup:'管理', icon:ICONS.columns, title:'账户中心', href:'/positions' },
  { id:'data-center', label:'数据中心', sidebarLabel:'数据中心', sidebarGroup:'管理', icon:ICONS.columns, title:'数据与恢复', href:'/data-center' },
  { id:'globalradar', label:'全球雷达',  sidebarHidden:true, icon:ICONS.globe, title:'全球市场冲击雷达' },
];
const PAGE_PATHS = {
  plan:'/next-session-plan', review:'/review',
  dashboard:'/', signals:'/signals', fundflow:'/fund-flow', scanner:'/scanner',
  risk:'/risk', globalradar:'/global-radar', panorama:'/market-panorama',
  'strategy-lab':'/strategy-lab',
};
const MARKET_CENTER_MODULES = [
  ['panorama', '/market-panorama', '市场全景'],
  ['fundflow', '/fund-flow', '资金流'],
  ['scanner', '/scanner', '全市场扫描'],
  ['globalradar', '/global-radar', '全球影响'],
];
function marketCenterNav(activePage) {
  return `<nav class="market-center-nav" aria-label="市场中心模块">${MARKET_CENTER_MODULES.map(([id, href, label]) =>
    `<a href="${href}"${id === activePage ? ' aria-current="page"' : ''}>${label}</a>`
  ).join('')}</nav>`;
}
function navigate(pageId, updateHash = true) {
  if (pageId === 'strategy-lab') { window.location.href = '/strategy-lab'; return; }
  if (!PAGE_PATHS[pageId]) return;
  if (state.page === pageId) {
    if (updateHash && window.location.pathname !== PAGE_PATHS[pageId]) {
      history.pushState({page:pageId}, '', PAGE_PATHS[pageId]);
    }
    return;
  }
  const leavingPage = state.page;
  const navSequence = ++state.navSequence;
  abortPageRequests(leavingPage);
  RegisSpa.workflow?.stop();
  if (state.navTimer) { clearTimeout(state.navTimer); state.navTimer = null; }
  if (state.page === 'dashboard' && state._dashInterval) {
    clearInterval(state._dashInterval); state._dashInterval = null;
  }
  if (state.page === 'signals' && _sigTimer) {
    clearInterval(_sigTimer); _sigTimer = null;
  }
  if (state.page === 'signals' && window.__regisSpaPortfolioMonitorOwner === 'signals') {
    setSpaPortfolioMonitorOwner(null);
  }
  if (state.page === 'risk' && state._riskInterval) {
    clearInterval(state._riskInterval); state._riskInterval = null;
  }
  if (pageId !== 'dashboard') { stopOpportunityMonitor(); hideAlertOverlay(); }
  if (pageId !== 'globalradar' && pageId !== 'panorama') window.MarketIntel && window.MarketIntel.stop();
  const oldView = $(`.page-view[data-page="${state.page}"]`);
  const newView = $(`.page-view[data-page="${pageId}"]`);
  if (!newView) return;
  if (updateHash && window.location.pathname !== PAGE_PATHS[pageId]) {
    history.pushState({page:pageId}, '', PAGE_PATHS[pageId]);
  }
  if (oldView) {
    oldView.style.opacity = '0';
    oldView.style.transform = 'translateY(8px)';
    oldView.style.transition = 'all 200ms ease-in';
  }
  state.navTimer = setTimeout(() => {
    if (navSequence !== state.navSequence) return;
    if (oldView) { oldView.classList.remove('active'); oldView.style.opacity=''; oldView.style.transform=''; oldView.style.transition=''; }
    newView.classList.add('active');
    state.page = pageId;
    updateNavActive();
    updatePageTitle();
    disposeAllCharts();
    switch(pageId) {
      case 'dashboard': renderDashboard(); break;
      case 'signals': renderSignalsPage(); break;
      case 'fundflow': renderFundFlow(); break;
      case 'scanner':
      case 'risk': RegisSpa.renderPage(pageId); break;
      case 'plan':
      case 'review': RegisSpa.renderPage(pageId); break;
      case 'globalradar': window.MarketIntel && window.MarketIntel.renderGlobal(); break;
      case 'panorama': window.MarketIntel && window.MarketIntel.renderPanorama(); break;
    }
    staggerPasses(newView);
    const main = $('#mainContent');
    if (main) { main.scrollTop = 0; main.focus({preventScroll:true}); }
    state.navTimer = null;
  }, 200);
}
function updateNavActive() {
  $$('.nav-item').forEach(el => {
    const active = el.dataset.nav === state.page;
    el.classList.toggle('active', active);
    if (active) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
  $$('.page-view').forEach(el => el.setAttribute('aria-hidden', el.dataset.page === state.page ? 'false' : 'true'));
}
function updatePageTitle() {
  const item = NAV_ITEMS.find(n => n.id === state.page);
  if (item) {
    $('#pageTitle').textContent = item.title;
    document.title = `${item.title} · Regis Oracle`;
  }
  $('#workspaceSwitch').hidden = state.page !== 'dashboard';
  $('#layoutEditButton').hidden = state.page !== 'dashboard';
  $('#pageTitle').classList.toggle('sr-only', state.page === 'dashboard');
}
function buildSidebar() {
  const nav = $('#sidebarNav');
  const groups = ['决策', '市场', '研究', '管理'];
  nav.innerHTML = groups.map(group => {
    const items = NAV_ITEMS.filter(item => !item.sidebarHidden && item.sidebarGroup === group);
    const rows = items.map(item => {
      const label = item.sidebarLabel || item.label;
      if (item.href) {
        return `<a class="nav-item" href="${item.href}" data-nav="${item.id}" aria-label="${item.title}">
          <span class="nav-icon">${item.icon}</span>
          <span class="nav-label">${label}</span>
        </a>`;
      }
      return `<a class="nav-item" href="${PAGE_PATHS[item.id]}" data-nav="${item.id}" aria-label="${item.title}">
        <span class="nav-icon">${item.icon}</span>
        <span class="nav-label">${label}</span>
      </a>`;
    }).join('');
    return `<section class="nav-group" aria-label="${group}"><div class="nav-group-label">${group}</div>${rows}</section>`;
  }).join('');
  updateNavActive();
  state.sidebarExpanded = true;
  $('#sidebar').classList.add('expanded');
  nav.addEventListener('click', event => {
    const target = event.target.closest('[data-nav]');
    if (target && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey
        && !NAV_ITEMS.find(item => item.id === target.dataset.nav)?.href) {
      event.preventDefault();
      navigate(target.dataset.nav);
    }
  });
}
function toggleSidebar() {
  state.sidebarExpanded = true;
  $('#sidebar')?.classList.add('expanded');
}
function resizeAllCharts() {
  requestAnimationFrame(() => {
    Object.values(state.charts).forEach(chart => {
      try { if (chart && (!chart.isDisposed || !chart.isDisposed())) chart.resize(); }
      catch (error) { reportClientIssue('chart-resize', error); }
    });
    window.MarketIntel?.resize?.();
  });
}
window._navigate = navigate;
function startClock() {
  const shanghaiClock = new Intl.DateTimeFormat('zh-CN', {
    timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false,
  });
  function tick() {
    const now = new Date();
    const ts = shanghaiClock.format(now);
    const clock = $('#topbarClock');
    if (clock) clock.textContent = ts;
    const st = $('#sidebarTime');
    if (st) st.textContent = ts.slice(0,5);
  }
  tick();
  state.clockInterval = setInterval(tick, 1000);
  function renderSession(payload) {
    const session = payload && payload.session;
    const known = session && session.status !== 'unknown' && typeof session.label === 'string';
    const live = known && session.is_open === true;
    const ms = $('#marketStatus');
    const mst = $('#marketStatusText');
    if (ms) ms.className = 'market-status' + (live ? ' live' : (known && session.status === 'break' ? ' break' : known ? ' closed' : ' unknown'));
    if (mst) mst.textContent = known ? session.label : '状态未知';
    state.marketLive = live;
    state.marketSessionPayload = payload || null;
    state.homeData.session = session || null;
    const phase = dashboardPhaseFromSession(session || {});
    if (state.homeAutoWorkspace !== phase.workspace) {
      state.homeAutoWorkspace = phase.workspace;
      setHomeWorkspace(phase.workspace, false);
    } else {
      renderDecisionHome();
    }
  }
  async function refreshSession() {
    const payload = await api('/api/market/status', {global:true});
    renderSession(payload);
  }
  function scheduleSession() {
    if (state.marketStatusTimer) clearInterval(state.marketStatusTimer);
    state.marketStatusTimer = null;
    if (document.hidden) return;
    refreshSession();
    state.marketStatusTimer = setInterval(refreshSession, 60000);
  }
  scheduleSession();
  document.addEventListener('visibilitychange', scheduleSession);
  window.addEventListener('pagehide', () => {
    clearInterval(state.clockInterval);
    clearInterval(state.marketStatusTimer);
    state.requests.forEach(controller => controller.abort('pagehide'));
    state.requests.clear();
  }, {once:true});
}
function initMouseGlow() {
  const glow = $('#bgGlow');
  document.addEventListener('mousemove', e => {
    if (glow) { glow.style.top = e.clientY + 'px'; glow.style.left = e.clientX + 'px'; }
  });
}
function abortPageRequests(page) {
  state.requests.forEach(controller => {
    if (!controller._globalRequest && controller._requestPage === page) {
      controller.abort('navigation');
    }
  });
}
async function api(url, options={}) {
  const requestPage = state.page;
  const requestGeneration = state.navSequence;
  const key = `GET:${url}`;
  let controller = state.requests.get(key);
  if (!controller || controller.signal.aborted || (!controller._globalRequest && controller._requestGeneration !== requestGeneration)) {
    if (controller && !controller.signal.aborted) controller.abort('superseded');
    controller = new AbortController();
    controller._globalRequest = options.global === true;
    controller._requestPage = state.page;
    controller._requestGeneration = state.navSequence;
    state.requests.set(key, controller);
    const start = Date.now();
    const timeout = setTimeout(() => controller.abort('timeout'), 10000);
    // Coalesce in-flight GETs only. No account-response cache, automatic write
    // retry, or cancellation of another component's valid account read.
    controller._promise = (async () => {
      try {
        const r = await fetch(url, {cache:'no-store', headers:{Accept:'application/json'}, signal:controller.signal});
        const raw = await r.text();
        let data = null;
        try { data = raw ? JSON.parse(raw) : {}; }
        catch (parseError) {
          if (r.ok) throw new Error('服务端返回了无法解析的数据');
          data = {};
        }
        if (!r.ok) {
          const label = {401:'登录已失效，请重新登录网站',403:'当前会话没有此接口的读取权限',
            502:'云端网关暂时无法连接应用服务',503:'服务暂不可用，请稍后重试',504:'服务端请求超时'}[r.status];
          const detail = (typeof data?.error === 'string' ? data.error.trim() : '') || (typeof data?.message === 'string' ? data.message.trim() : '');
          const reason = [401,403].includes(r.status) ? label : detail || label;
          throw new Error(`HTTP ${r.status} · ${reason || '接口请求失败'}`);
        }
        if (state.requests.get(key) !== controller || controller.signal.aborted) return null;
        state.apiErrors.delete(url);
        updateLatency(Date.now() - start);
        return data;
      } catch(e) {
        // AbortController.abort(reason) may reject fetch with a string, not
        // an AbortError. Navigation cancellation must not mark service down.
        const timedOut = controller.signal.aborted && controller.signal.reason === 'timeout';
        const cancelled = controller.signal.aborted && !timedOut;
        if (!cancelled && state.requests.get(key) === controller) {
          const local = ['localhost','127.0.0.1','::1','[::1]'].includes(window.location?.hostname);
          const message = timedOut ? '请求超时（10秒），请重试' : e instanceof TypeError
            ? `无法连接${local ? '本地' : '云端'}服务，请检查网络或服务状态`
            : (e?.message || '服务请求失败');
          state.apiErrors.set(url, message);
          if (!timedOut) console.error('API error:', url, e);
          updateLatency(-1);
        }
        return null;
      } finally {
        clearTimeout(timeout);
        if (state.requests.get(key) === controller) state.requests.delete(key);
      }
    })();
  } else if (options.global === true) {
    controller._globalRequest = true;
  }
  const data = await controller._promise;
  if (options.global !== true && (state.page !== requestPage || state.navSequence !== requestGeneration)) return null;
  return data == null ? null : structuredClone(data);
}
function apiError(url, fallback='接口暂不可用') {
  return state.apiErrors.get(url) || fallback;
}
async function apiWrite(url, payload, method='POST', options={}) {
  if (window.RegisAccess?.canWrite !== true) {
    return {_httpOk:false, code:'client_write_not_enabled', error:window.RegisAccess?.reason || '写入权限未核实，未发送请求'};
  }
  const start = Date.now();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort('timeout'), 10000);
  try {
    const headers = {'Content-Type':'application/json', Accept:'application/json'};
    if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
    const response = await fetch(url, {
      method,
      headers,
      body: payload == null ? undefined : JSON.stringify(payload),
      signal: controller.signal,
    });
    let data;
    try {
      data = await response.json();
    } catch (parseError) {
      updateLatency(Date.now() - start);
      console.error('API write JSON error:', url, parseError);
      return {
        _httpOk:false,
        _httpStatus:response.status,
        error:`服务端返回了无效 JSON（HTTP ${response.status}）`,
      };
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      updateLatency(Date.now() - start);
      return {
        _httpOk:false,
        _httpStatus:response.status,
        error:`服务端返回了无效 JSON 对象（HTTP ${response.status}）`,
      };
    }
    updateLatency(Date.now() - start);
    return {...data, _httpOk: response.ok, _httpStatus: response.status};
  } catch (error) {
    const timedOut = controller.signal.aborted && controller.signal.reason === 'timeout';
    if (!timedOut) console.error('API write error:', url, error);
    updateLatency(-1);
    return {_httpOk:false, error:timedOut ? '保存结果尚未确认（请求超时），请先刷新核对，勿重复提交。' : '与服务的连接中断，保存结果尚未确认；请先刷新核对，勿重复提交。'};
  } finally {
    clearTimeout(timeout);
  }
}
let _idempotencyCounter = 0;
function createIdempotencyKey(scope) {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') return `${scope}-${window.crypto.randomUUID()}`;
  const bytes = new Uint32Array(4);
  if (window.crypto && typeof window.crypto.getRandomValues === 'function') window.crypto.getRandomValues(bytes);
  else { bytes[0] = Date.now(); bytes[1] = ++_idempotencyCounter; bytes[2] = performance.now() * 1000; bytes[3] = _idempotencyCounter; }
  return `${scope}-${Date.now().toString(36)}-${Array.from(bytes, n => Number(n).toString(36)).join('-')}`;
}
function buildBuyConfirmationPayload(ticketId, price, shares) {
  return {ticket_id:ticketId, price, shares};
}
const _latencySamples = [];
function updateLatency(ms) {
  const dot = $('#latencyDot');
  const txt = $('#latencyText');
  if (ms < 0) { if(dot) dot.style.background = 'var(--bear)'; if(txt) txt.textContent = '连接失败'; return; }
  _latencySamples.push(ms);
  if (_latencySamples.length > 20) _latencySamples.shift();
  const sorted = [..._latencySamples].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  if (dot) dot.style.background = median < 300 ? 'var(--bull)' : median < 800 ? 'var(--gold)' : 'var(--bear)';
  if (txt) {
    txt.textContent = `接口中位 ${median}ms`;
    txt.title = `最近一次 ${ms}ms · 最近 ${sorted.length} 次本地接口中位数`;
  }
}
function esc(s) { const d = document.createElement('div'); d.textContent = s||''; return d.innerHTML; }
function escAttr(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fp(p) { if(p==null||p===0) return '—'; return Number(p).toFixed(2); }
function fpp(p) { if(p==null) return '—'; return (p>0?'+':'')+Number(p).toFixed(2)+'%'; }
function fpc(p) { if(p==null||p<=-99) return '—'; return (p>0?'+':'')+Number(p).toFixed(2)+'%'; }
function fn(n,d) { if(n==null) return '—'; if(d===undefined) d=1; return Number(n).toFixed(d); }
function cc(v) { return v>0?'c-bull':v<0?'c-bear':''; }
function fmtYi(v) { if(v==null) return '—'; return (v>0?'+':'')+fn(v,2)+'亿'; }
function toast(msg, type='info') {
  const container = $('#toastContainer');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity='0'; el.style.transform='translateX(20px)'; el.style.transition='all 300ms ease-in'; }, 3000);
  setTimeout(() => el.remove(), 3300);
}
function reportClientIssue(scope, error) {
  console.warn(`[Regis ${scope}]`, error && error.message ? error.message : error);
}
function trapDialog(dialog, onEscape) {
  const opener = document.activeElement;
  const panel = dialog.querySelector('.timing-modal, .buy-alert, .cmd-panel') || dialog;
  if (!panel.hasAttribute('tabindex')) panel.tabIndex = -1;
  const onKeydown = event => {
    if (event.key === 'Escape') { event.preventDefault(); onEscape(); return; }
    if (event.key !== 'Tab') return;
    const focusable = [...dialog.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
      .filter(el => !el.hidden && el.getClientRects().length);
    if (!focusable.length) { event.preventDefault(); panel.focus(); return; }
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  dialog.addEventListener('keydown', onKeydown);
  requestAnimationFrame(() => {
    const initial = dialog.querySelector('input:not([disabled]),button:not([disabled]),[tabindex="0"]');
    (initial || panel).focus();
  });
  return () => {
    dialog.removeEventListener('keydown', onKeydown);
    if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
  };
}
function dashboardPhaseFromSession(payload) { return RegisSpa.home.dashboardPhaseFromSession(payload); }
function setHomeDensity(v, p = true) { return RegisSpa.home.setHomeDensity(v, p); }
function setHomeWorkspace(value, persist = true) { return RegisSpa.home.setHomeWorkspace(value, persist); }
function restoreHomePreferences() { return RegisSpa.home.restoreHomePreferences(); }
function renderDecisionHome() { return RegisSpa.home.renderDecisionHome(); }
async function renderDashboard() {
  const renderGeneration = state.navSequence;
  const container = $('[data-page="dashboard"]');
  state.homeData.portfolioStatus = 'loading';
  state.homeData.portfolio = null;
  state.homeRiskStatus = 'idle';
  const decisionMetric = (tone, icon, label, valueId, subId) => `<section class="decision-metric" data-tone="${tone}"><i class="decision-metric-icon">${icon}</i><small>${label}</small><b id="${valueId}">—</b><span id="${subId}">核验中</span></section>`;
  container.innerHTML = `
    <div class="decision-home" id="decisionHome" data-workspace="${state.homeWorkspace}">
      <section class="decision-verdict" aria-labelledby="homeDecisionHeadline">
        <div class="decision-verdict-main">
          <div class="decision-verdict-copy"><small id="homeDecisionMode">正在核验工作区</small><h2 id="homeDecisionHeadline">正在核验今日决策条件</h2><p id="homeDecisionExplain">任何缺失输入都不会被填成可执行建议。</p></div>
          <div class="decision-verdict-actions"><button id="homeWhyButton" type="button" aria-expanded="false" aria-controls="homeWhyDetails" data-action="toggle-home-why">为什么变化</button><time class="num" id="homeSessionStamp">服务时间待核验</time></div>
        </div>
        <div class="decision-metrics" aria-label="今日五项核心决策指标">
          ${decisionMetric('state', ICONS.flow, '市场情绪阶段', 'homeMarketState', 'homeMarketStateSub')}
          ${decisionMetric('exposure', ICONS.columns, '今日执行总仓位', 'homeExposure', 'homeExposureSub')}
          ${decisionMetric('capacity', ICONS.bolt, '今日允许交易', 'homeTradeCapacity', 'homeTradeCapacitySub')}
          ${decisionMetric('sector', ICONS.grid, '主线板块', 'homeMainSector', 'homeMainSectorSub')}
          ${decisionMetric('risk', ICONS.shield, '当前持仓风险', 'homeHoldingRisk', 'homeHoldingRiskSub')}
        </div>
      </section>
      <div class="decision-why" id="homeWhyDetails" hidden></div>
      <div id="staleBanner" class="stale-banner decision-stale" hidden></div>
      <div class="decision-review-grid" data-workspace-panel="decision">
        <section class="decision-panel decision-primary"><header><div><h3 id="homePrimaryTitle">每日复盘</h3><small id="homePrimaryBadge">真实记录</small></div><span id="homePrimaryAsOf">时点核验中</span></header><div id="homePrimaryBody"><div class="decision-empty">正在读取复盘记录…</div></div></section>
        <div class="decision-side-stack">
          <section class="decision-panel"><header><h3 id="homePlanTitle">下一交易日计划 · 开盘前准备</h3><span>非买卖指令</span></header><div id="homePlanBody"><div class="decision-empty">正在核验下一交易日…</div></div></section>
          <section class="decision-panel"><header><h3>当前持仓</h3><div id="homeHoldingsMeta" class="decision-holdings-meta"></div></header><div id="homeHoldingsBody"><div class="decision-empty">正在读取账户记录…</div></div></section>
        </div>
      </div>
      <section class="decision-route-strip" data-workspace-panel="route" aria-label="决策路径">
        <header><div><b>决策路径</b><small>严格执行 · 任一关键输入缺失即停止</small></div><span id="homeRouteAuthority">正在核验权限</span></header>
        <div class="decision-route-stages">
          <span><b>情绪</b><small>市场状态</small></span><i aria-hidden="true">→</i>
          <span><b>板块</b><small>同一观察时点确认</small></span><i aria-hidden="true">→</i>
          <span><b>战法</b><small>固定准入</small></span><i aria-hidden="true">→</i>
          <span><b>个股</b><small>统一候选</small></span><i aria-hidden="true">→</i>
          <span><b>仓位</b><small>账户约束</small></span><i aria-hidden="true">→</i>
          <span><b>人工决定</b><small>不自动下单</small></span><i aria-hidden="true">→</i>
          <span><b>复盘</b><small id="homeAfterCostReviewStage">成本后归因</small></span>
        </div>
        <div class="decision-route-rules"><span id="homePlaybookGate">允许：等待核验</span><span id="homeFactorRole">因子：仅辅助排序，不阻塞非因子战法</span><span id="homeNoTradeRule">没有完整优势或完整计划：今日不交易</span></div>
      </section>
      <details class="decision-session-rail" data-workspace-panel="session">
        <summary>今日交易阶段</summary>
        <div class="decision-phase-rail" aria-label="今日交易阶段">
          ${RegisSpa.home.phases.map((item, index) => `<div class="decision-phase upcoming" data-phase-index="${index}"><span class="decision-phase-node" aria-hidden="true"></span><b>${item[0]}</b><small class="decision-phase-status">${item[1]}</small><small class="decision-phase-note">${item[2]}</small></div>`).join('')}
        </div>
      </details>
      <div class="decision-bottom-rail" data-workspace-panel="tasks">
        <details id="homeMustHandleDetails"><summary>交易门禁与待核验 <b id="homeMustHandleCount">—</b></summary><div id="homeMustHandleBody" class="decision-task-list"></div></details>
        <details><summary>今日决策日志</summary><div id="homeDecisionLogBody" class="decision-log-list"></div></details>
      </div>
      <details class="decision-ops-drawer">
        <summary>运维与数据状态</summary>
        <div id="sectorQuotePanelOps" role="status">盘中战法研究输入：正在核验采集窗口…</div>
        <div id="dataInfrastructureCard">
          <div class="decision-ops-head"><span id="dataInfrastructureSummary" role="status">正在核验核心数据…</span><span id="dataInfrastructureBadge">自检中</span><button type="button" data-action="refresh-health">刷新</button><a href="/data-center">数据与恢复</a></div>
          <details id="dataInfrastructureDetails"><summary>展开数据源明细</summary><div id="dataInfrastructureBody"></div></details>
        </div>
      </details>
    </div>
    <div class="dashboard-legacy-bridge" hidden aria-hidden="true">
      <div id="marketIndexes"></div><span id="marketRegime"></span><span id="breadthInfo"></span>
      <span id="marketStateBadge"></span><div id="marketStateBody">研究市场状态与现行仓位预算</div>
      <span id="modelPosCount"></span><div id="portfolioBody"></div><div id="riskSnapshot"></div>
      <span id="oppPhase">机会候选榜 · 综合质量优先 · 沪深审计市场扫描（北交所待授权） + 模型旁证 · 统一评分</span><div id="signalsBody"></div>
      <span id="manualPosCount"></span><div id="positionsBody"></div><span id="timingPosCount"></span><div id="timingPosMini"></div>
      <span id="reviewDate"></span><div id="reviewBody"></div><span id="strategyMethod"></span><div id="strategyMetrics"></div>
      <span id="strategyRegCount"></span><div id="strategyRegistryBody"></div><span id="kronosUpdated"></span><div id="kronosBody"></div>
      <div id="flowWidgetBody">第三方资金流观察 Top · 不参与决策</div><span id="eventCount"></span><div id="eventsBody"></div><span id="factorCount">实时评分与信号唯一目录</span><div id="factorBody"></div>
    </div>
    <div id="treemapCard" hidden><div id="treemapChart">第三方资金流观察热力图 · 不参与综合评分、买点或仓位</div></div>
  `;
  const treemapCard = container.querySelector('#treemapCard'); if (treemapCard) container.appendChild(treemapCard);
  state.flowDir = 'in';
  renderDecisionHome();
  startOpportunityMonitor();
  await Promise.all([
    fetchResearchMarketState(),
    fetchHomeStrategyEvidence(),
    fetchDataInfrastructureStatus(),
  ]);
  if (state.page !== 'dashboard' || state.navSequence !== renderGeneration) return;
  if (state._dashInterval) clearInterval(state._dashInterval);
  state._dashInterval = setInterval(() => {
    if (document.hidden || state.page !== 'dashboard') return;
    fetchResearchMarketState();
    fetchDataInfrastructureStatus();
  }, 60000);
}
async function fetchDataInfrastructureStatus() {
  const requestGeneration = state.navSequence;
  const body = $('#dataInfrastructureBody');
  const badge = $('#dataInfrastructureBadge');
  const summaryLine = $('#dataInfrastructureSummary');
  if (!body || !badge || !summaryLine) return;
  const response = await api('/api/data/health');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  if (!response || response.code !== 0 || !response.data) {
    badge.textContent = '健康接口不可用';
    badge.style.color = 'var(--bear)';
    summaryLine.innerHTML = '<strong style="color:var(--bear)">核心数据状态未知</strong> · 未能读取健康接口，当前数据不得视为正常。';
    body.innerHTML = '<div class="c-dim" style="font-size:var(--text-xs)">未能读取数据健康状态；网站不会把此情况伪装为“数据正常”。</div>';
    return;
  }
  const data = response.data;
  const rows = data.presentation_types || data.types || data.sources || [];
  const currentRows = data.presentation_types || data.current_sources || rows;
  const historyCounts = data.historical_counts || {};
  const historicalProblems = (historyCounts.error||0) + (historyCounts.missing||0);
  const labels = {validated:'已验证', closed_snapshot:'收盘快照', pending_next_open:'待下次开盘', delayed:'延迟', error:'源异常', missing:'缺失'};
  const colors = {validated:'var(--bull)', closed_snapshot:'var(--text-tertiary)', pending_next_open:'var(--gold)', delayed:'var(--bear)', error:'var(--bear)', missing:'var(--text-tertiary)'};
  const decisionRows = currentRows.filter(row => row.participates_in_decision === true), supportingRows = currentRows.filter(row => row.participates_in_decision !== true);
  const countStatuses = sourceRows => sourceRows.reduce((acc, row) => { const key = row.presentation_status || row.health_status || 'missing'; acc[key] = (acc[key] || 0) + 1; return acc; }, {});
  const decisionCounts = countStatuses(decisionRows), supportingCounts = countStatuses(supportingRows);
  const badCount = counts => (counts.error || 0) + (counts.missing || 0) + (counts.delayed || 0);
  const decisionUnavailable = badCount(decisionCounts), supportingUnavailable = badCount(supportingCounts);
  const decisionVerified = decisionCounts.validated || 0;
  const decisionSummary = ['validated','closed_snapshot','pending_next_open','delayed','error','missing'].map(status => `${labels[status]} ${decisionCounts[status]||0}`).join(' · ');
  badge.textContent = currentRows.length ? `${decisionRows.length} 类决策数据 · ${supportingRows.length} 类辅助数据` : '等待首次真实自检';
  badge.style.color = decisionUnavailable ? 'var(--bear)' : (!decisionVerified || supportingUnavailable ? 'var(--gold)' : 'var(--accent)');
  const decisionHeadline = decisionRows.length === 0
    ? '<strong style="color:var(--gold)">当前无可参与决策的数据源</strong>'
    : decisionUnavailable
      ? `<strong style="color:var(--bear)">${decisionUnavailable} 类决策数据异常/缺失/延迟</strong>`
      : decisionVerified === 0
        ? '<strong style="color:var(--gold)">决策数据尚未验证</strong>'
        : '<strong style="color:var(--bull)">决策数据未发现当前异常</strong>';
  summaryLine.innerHTML = `${decisionHeadline} · ${esc(decisionSummary)}` + (supportingUnavailable ? ` · <span style="color:var(--gold)">${supportingUnavailable} 类辅助数据延迟/不可用，不参与决策</span>` : '');
  const details = currentRows.length ? currentRows.map(row => `
    <span style="display:inline-flex;align-items:center;gap:5px">
      <i style="width:6px;height:6px;border-radius:50%;background:${colors[row.presentation_status||row.health_status]||colors.missing};display:inline-block"></i>
      ${esc(row.data_type||'数据')} · ${esc(row.source||'未知来源')} · ${labels[row.presentation_status||row.health_status]||'未知'} · 时点 ${esc(row.as_of||'—')}${row.participates_in_decision ? ' · 可参与决策' : ' · 不参与决策'}${row.sources_checked>1?` · 已查${row.sources_checked}源`:''}${row.presentation_detail?` · ${esc(row.presentation_detail)}`:''}
    </span>`).join('<span class="c-dim">|</span>') : '<span class="c-dim">调度器会在启动后使用真实行情完成首轮自检；未自检的数据不显示为正常。</span>';
  body.innerHTML = `
    <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap">
      <div style="font-size:var(--text-xs);color:var(--text-secondary);line-height:1.7;display:flex;flex-wrap:wrap;column-gap:10px;row-gap:4px;flex:1;min-width:220px">${details}</div>
      <div style="font-size:var(--text-xs);color:var(--text-tertiary);text-align:right">当前决策源：${decisionSummary}${historicalProblems?`<br>历史失败尝试 ${historicalProblems} 条 · 仅审计、不代表当前异常`:''}<br>存储 ${esc(data.storage_backend||'—')} · 缓存 ${esc(data.cache_backend||'—')}</div>
    </div>`;
}
function whenEcharts() {
  if (window.echarts) return Promise.resolve(true);
  return new Promise(resolve => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (window.echarts || Date.now() - started > 15000) {
        clearInterval(timer);
        resolve(Boolean(window.echarts));
      }
    }, 200);
  });
}
function showEchartsRetry(el, message, retry) {
  if (!el || !el.isConnected) return;
  el.innerHTML = `<div class="empty-state" role="status">${esc(message)}<br><button type="button" class="btn btn-ghost btn-sm" data-retry-echarts>重试图表</button></div>`;
  const button = el.querySelector('[data-retry-echarts]');
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = '重试中…';
    if (await whenEcharts()) retry();
    else showEchartsRetry(el, message, retry);
  });
}
async function fetchMarketMap() {
  const requestGeneration = state.navSequence;
  const data = await api('/api/fund-flow/ranking?sort=net&dir=desc&limit=120');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  if (data && data.items) {
    const el = $('#treemapChart');
    const chartReady = await whenEcharts();
    if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
    if (chartReady) renderMarketTreemap(el, data.items);
    else showEchartsRetry(el, '本地图表运行时未就绪；资金流表仍可核对。', () => renderMarketTreemap(el, data.items));
  } else {
    const el = $('#treemapChart');
    if (el) el.innerHTML = `<div class="empty-state" role="status">${esc(apiError('/api/fund-flow/ranking?sort=net&dir=desc&limit=120', '热力图数据暂不可用'))}</div>`;
  }
}
function truthLabel(value) { return value === true ? '是' : value === false ? '否' : '未知'; }
function executionSlippageLabel(p) { const entry=p?.entry_slippage_yuan, fills=Number(p?.sell_fill_events||0), attributed=Number(p?.sell_slippage_attributed_events||0), exit=p?.sell_slippage_yuan, total=(entry==null?0:Number(entry))+(exit==null?0:Number(exit)); return entry!=null&&fills>0&&attributed===fills&&exit!=null ? (total>0?`较提醒不利 ¥${fn(total,2)}`:total<0?`较提醒改善 ¥${fn(-total,2)}`:'与提醒参考价一致') : entry!=null||attributed>0 ? `执行偏差部分可得 ¥${fn(total,2)}` : '无提醒参考价 · 执行偏差不可得'; }
function entryAttributionLabel(p) {
  return p?.entry_origin === 'production_strategy' && p.entry_strategy_id ? `生产策略入场 · ${p.entry_strategy_id}` : p?.entry_origin === 'manual_strategy_observation' ? '冻结研究规则入场 · 非正式策略' : p?.entry_origin === 'research_observation' ? '研究观察入场' : p?.entry_origin === 'manual_position_snapshot' ? '导入已有持仓 · 原始买入未记录' : p?.entry_origin === 'manual_record' || p?.entry_origin === 'manual_ticket' ? '手工录入' : '来源未记录';
}
function isProductionAlert(item) { return item?.alert?.provenance?.origin === 'production_strategy'; }
function strategyAttributionHtml(perf) {
  const groups = perf?.by_strategy || [];
  if (!groups.length) return '<div style="padding:7px 14px;font-size:12px;color:var(--text-tertiary)">策略归因：暂无完整生产策略平仓样本；全部手工成交（混合来源）不构成策略收益。</div>';
  return `<div style="padding:7px 14px;font-size:12px;color:var(--text-tertiary)">策略归因（完整版本）：${groups.map(g => `${esc(g.strategy_id || '未标识')} · 规格 ${esc(String(g.strategy_spec_sha256 || '—').slice(0,12))} · 证据 ${esc(String(g.evidence_ref_sha256 || '—').slice(0,12))} · ${fn(g.completed_positions,0)}笔`).join('；')}</div>`;
}
function decisionGateFacts(gate) {
  const source = gate || {};
  const manualConsent = source.manual_consent, manualAlertAllowed = source.manual_research_alerts_allowed;
  const recommendationEligible = source.recommendation_eligible, brokerConnected = source.broker_connected;
  return {
    manualConsent, manualAlertAllowed, recommendationEligible, brokerConnected,
    manualAlertReady: manualConsent === true && manualAlertAllowed === true,
    formalStrategyReady: recommendationEligible === true,
    ready: manualConsent === true && recommendationEligible === true && brokerConnected === true,
  };
}
function decisionGateSummary(gate) {
  const facts = decisionGateFacts(gate);
  const conclusion = facts.manualAlertReady ? '研究级人工提醒已启用；正式策略与券商执行单独核验。' : '研究级人工提醒未启用。';
  return `${conclusion} 人工同意 ${truthLabel(facts.manualConsent)} · 研究提醒 ${truthLabel(facts.manualAlertAllowed)} · 正式策略资格 ${truthLabel(facts.recommendationEligible)} · 券商连接 ${truthLabel(facts.brokerConnected)}`;
}
function accountExecutionSummary(execution) {
  const source = execution || {};
  const blockers = Array.isArray(source.blocking_reasons)
    ? source.blocking_reasons.filter(Boolean) : [];
  const cash = Number(source.available_cash_yuan);
  const minimumLot = Number(source.minimum_candidate_lot_cost_yuan);
  const shares = Number(source.buy_capacity_shares);
  return {
    reason: blockers.join('；') || source.reason || '账户级买入计划尚未就绪。',
    capacity: [
      Number.isFinite(cash) ? `可用现金 ¥${fn(cash, 2)}` : '',
      Number.isFinite(minimumLot) ? `候选最低一手约 ¥${fn(minimumLot, 2)}` : '',
      Number.isInteger(shares) ? `当前新增最多 ${shares} 股` : '',
    ].filter(Boolean).join(' · '),
  };
}
const RISK_KIND_LABELS = { hard_stop:'成本-波动风险复核线', trailing_stop:'移动风险线', trailing_activation:'移动线启用观察位', research_target:'研究观察位' };
function riskLevelMeta(level) {
  if (!level || typeof level !== 'object') return '风险线元数据未知 · 不作为可执行指令';
  const kind = RISK_KIND_LABELS[level.kind] || (level.kind ? String(level.kind) : '类型未知');
  const value = level.value == null ? '数值未知' : `¥${fp(level.value)}`;
  const source = level.source ? `来源 ${level.source}` : '来源未知';
  const priority = level.priority == null ? '优先级未知' : `优先级 ${level.priority}`;
  const execution = level.executable === false ? '不可执行' : level.executable === true ? '后端标记可执行' : '执行属性未知';
  const calibration = level.calibration_status === 'rule_based_unvalidated'
    ? '规则型·未做退出收益校准' : level.calibration_status ? String(level.calibration_status) : '校准状态未知';
  const formula = level.formula ? `公式 ${level.formula}` : '公式未知';
  const atrAsOf = level.data_as_of ? `ATR截至 ${level.data_as_of}` : '';
  return `${kind} ${value} · ${formula} · ${atrAsOf ? atrAsOf + ' · ' : ''}${calibration} · ${source} · ${priority} · ${execution}`;
}
function effectiveRiskLevel(levels, explicit) {
  if (explicit && typeof explicit === 'object') return explicit;
  return (Array.isArray(levels) ? levels : []).find(level => level?.active === true) || null;
}
const COMPOSITE_DIMENSION_LABELS = {
  growth:'成长', quality:'质量', valuation:'估值', risk:'风险',
  technical:'技术', funds:'资金', timing:'时机',
};
function compositeAsOfHtml(audit, compact = false) {
  if (!audit || typeof audit !== 'object') return '';
  const inputs = Object.entries(audit.inputs || {}).map(([key, row]) => {
    const label = COMPOSITE_DIMENSION_LABELS[key] || key;
    const asOf = row?.as_of || '未知';
    const age = row?.age_calendar_days == null ? '年龄未知' : `${row.age_calendar_days}日`;
    const limit = row?.freshness_limit_calendar_days == null ? '上限未知' : `上限${row.freshness_limit_calendar_days}日`;
    return `${label} ${asOf}（${age}/${limit}）`;
  });
  const maximum = audit.max_input_age_calendar_days == null ? '未知' : `${audit.max_input_age_calendar_days}日`;
  const excluded = (audit.excluded_dimensions || []).map(key => COMPOSITE_DIMENSION_LABELS[key] || key);
  if (compact) {
    const summary = `输入最旧 ${maximum} · 排除 ${excluded.length ? excluded.join('、') : '无'} · 展开核验`;
    const detail = `输入时点：${inputs.length ? inputs.join(' · ') : '未提供'}；${audit.rule || '时点规则未提供'}`;
    return `<details style="padding:0 12px 8px;color:var(--text-tertiary);font-size:12px;line-height:1.5"><summary style="cursor:pointer">${esc(summary)}</summary><div style="padding-top:5px">${esc(detail)}</div></details>`;
  }
  return `<div class="c-dim" style="font-size:12px;line-height:1.5;margin-top:4px">输入时点：${inputs.length ? inputs.map(esc).join(' · ') : '未提供'}<br>最大日历年龄 ${esc(maximum)} · 超限/未知排除 ${excluded.length ? excluded.map(esc).join('、') : '无'} · ${esc(audit.rule || '时点规则未提供')}</div>`;
}
async function fetchDashboard() {
  const requestGeneration = state.navSequence;
  const data = await api('/api/dashboard');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  state.homeData.dashboard = data || null;
  renderDecisionHome();
  if (!data) {
    const message = esc(apiError('/api/dashboard', '仪表盘摘要暂不可用'));
    const banner = $('#staleBanner');
    if (banner) { banner.hidden = false; banner.className = 'stale-banner stale-warn'; banner.innerHTML = `<span>${message}</span>`; }
    ['#portfolioBody', '#strategyMetrics', '#riskSnapshot'].forEach(selector => {
      const el = $(selector); if (el) el.innerHTML = `<div class="empty-state" role="status">${message}</div>`;
    });
    return;
  }
  const banner = $('#staleBanner');
  if (banner) {
    if (data.strategy_lab?.available) {
      const action = data.strategy_lab.headline || {};
      const summary = data.strategy_lab.summary || {};
      banner.hidden = false;
      banner.className = 'stale-banner stale-warn';
      const method = summary.method || action.label || '纸面研究候选', forwardDays = Number(summary.forward_completed) || 0;
      const eligibility = summary.recommendation_eligible === true ? '可进入人工决策复核' : '仅纸面研究 · 不进入推荐';
      const dataAsOf = data.data_asof
        ? `日频截至 <strong>${esc(data.data_asof)}</strong>`
        : '当前无 production 因子日期';
      banner.innerHTML = `<span>策略证据：<strong>${esc(method)}</strong> · ${esc(eligibility)} · 独立前向 ${forwardDays} 个共同收盘 · <a href="/strategy-lab" style="color:inherit;text-decoration:underline">查看依据</a> · ${dataAsOf}</span>`;
    } else if (data.decision_gate?.research_only) {
      banner.hidden = false;
      banner.className = 'stale-banner stale-warn';
      banner.innerHTML = `<span>${esc(decisionGateSummary(data.decision_gate))} · 日频截至 <strong>${esc(data.data_asof||'—')}</strong></span>`;
    } else if (data.data_stale) {
      banner.hidden = false;
      banner.className = 'stale-banner stale-warn';
      banner.innerHTML = `<span>量化日频数据已 <strong>${data.data_staleness_days||'?'}</strong> 天未更新（截至 <strong>${esc(data.data_asof||'—')}</strong>），请检查数据更新任务</span>`;
    } else if (data.data_asof) {
      banner.hidden = false;
      banner.className = 'stale-banner stale-ok';
      banner.innerHTML = `<span>量化日频截至 <strong>${esc(data.data_asof)}</strong>；行情是否可决策以数据健康卡的来源、时点和延迟为准</span>`;
    } else {
      banner.hidden = true;
    }
  }
  renderPortfolioWidget(data.portfolio || {});
  renderStrategyWidget(data.strategy_lab, data.headline, data.decision_gate);
  renderRiskSnapshot(data.account || {}, data.portfolio || {});
}
function renderSignalsWidget(signals, summary) {
  const count = $('#signalCount');
  if (count) count.textContent = (summary.total||0) + ' 条';
  const body = $('#signalsBody');
  if (!body) return;
  if (!signals.length) {
    body.innerHTML = '<div class="empty-state">暂无今日信号</div>';
    return;
  }
  body.innerHTML = '<div class="signal-list">' + signals.map((s,i) => {
    const isBuy = s.action === 'BUY';
    const conf = s.confidence || 0;
    const confLvl = conf >= 70 ? 'high' : conf >= 40 ? 'med' : 'low';
    const fd = s.flow_direction || 'none';
    const ind = s.industry || '';
    const kr = s.kronos_20d;
    return `<div class="signal-card ${isBuy?'buy':'sell'}" style="--card-index:${i}" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(s.code || '')}">
      <div class="signal-top">
        <div>
          <span class="signal-action-tag ${isBuy?'buy':'sell'}">${isBuy?'买入':'卖出'}</span>
          <span class="signal-name">${esc(s.name)}</span>
          <span class="signal-code">${esc(s.code)}</span>
          ${ind?`<span class="signal-industry">${esc(ind)}</span>`:''}
        </div>
        <span class="c-dim" style="font-size:var(--text-xs)">#${s.rank||'—'}</span>
      </div>
      <div class="signal-meta">
        ${s.price?`<span>现价 ¥${fp(s.price)}</span>`:''}
        ${s.prev_close?`<span>昨收 ¥${fp(s.prev_close)}</span>`:''}
        ${s.estimated_cost?`<span>约需 ¥${s.estimated_cost}</span>`:''}
      </div>
      ${isBuy&&s.entry_zone?`
      <div class="signal-zone">
        <div class="zone-item"><span class="zone-label">入场</span><span class="zone-val">¥${fp(s.entry_zone.min)}–${fp(s.entry_zone.max)}</span></div>
        <div class="zone-item"><span class="zone-label">止损</span><span class="zone-val c-bear">¥${fp(s.stop_target?.stop_loss)}</span></div>
        <div class="zone-item"><span class="zone-label">目标</span><span class="zone-val c-bull">¥${fp(s.stop_target?.target_1)}/${fp(s.stop_target?.target_2)}</span></div>
      </div>`:''}
      <div class="signal-reason">${esc(s.reason||'')}</div>
      <div class="signal-bottom">
        <div class="confidence-row">
          <span style="color:var(--text-tertiary)">置信度</span>
          <div class="conf-bar"><div class="conf-fill ${confLvl}" style="width:${conf}%"></div></div>
          <span>${Math.round(conf)}%</span>
        </div>
        ${s.flow_text?`<span style="display:flex;align-items:center;gap:4px"><span class="flow-dot ${fd}"></span><span class="${fd==='in'?'c-bull':fd==='out'?'c-bear':''}">${esc(s.flow_text)}</span></span>`:''}
        ${kr!=null?`<span class="c-accent">AI强度 ${kr>0?'+':''}${kr.toFixed(0)}</span>`:''}
      </div>
    </div>`;
  }).join('') + '</div>';
}
function renderPortfolioWidget(data) {
  const count = $('#modelPosCount');
  const body = $('#portfolioBody');
  const positions = data.positions || [];
  if (count) count.textContent = positions.length + ' 只';
  if (!body) return;
  if (!positions.length) {
    const cash = data.cash_user_recorded === true && data.available_cash != null ? Number(data.available_cash) : NaN;
    const cashText = Number.isFinite(cash) ? ` · 真实可用现金 ¥${fn(cash, 2)}` : ' · 真实可用现金未录入';
    body.innerHTML = `<div class="empty-state">暂无持仓${cashText}</div>`;
    return;
  }
  body.innerHTML = positions.map(p => {
    const upnl = p.unrealized_pnl_pct;
    return `<div class="pos-item" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(p.code || '')}">
      <div>
        <div style="font-weight:600;font-size:var(--text-sm)">${esc(p.name)} ${p.industry?`<span class="pos-industry-tag">${esc(p.industry)}</span>`:''}</div>
        <div style="font-size:var(--text-xs);color:var(--text-tertiary)">入场 ¥${fp(p.entry_price)} · ${(p.entry_date||'').slice(0,10)}</div>
      </div>
      <div style="text-align:right">
        ${p.current_price?`<div class="num" style="font-weight:600">¥${fp(p.current_price)}</div>`:''}
        ${upnl!=null?`<div class="num ${cc(upnl)}" style="font-weight:600">${fpp(upnl)}</div>`:''}
      </div>
    </div>`;
  }).join('');
}
function renderStrategyWidget(strategyLab, headline, decisionGate) {
  const grid = $('#strategyMetrics');
  if (!grid) return;
  const summary = strategyLab?.summary || {};
  const action = strategyLab?.headline || headline || {};
  const gate = strategyLab?.decision_gate || decisionGate || {};
  const available = strategyLab?.available === true;
  const status = summary.status || action.status || 'unverified';
  const statusText = summary.recommendation_eligible === true
    ? '研究资格通过'
    : status === 'paper_candidate' || status === 'historical_horizon_passed_paper_only'
      ? '仅纸面研究'
      : '不可执行';
  const metrics = [];
  if (summary.safe_core_pct != null) metrics.push(['安全核心', fn(summary.safe_core_pct, 2) + '%', 'c-gold']);
  if (summary.risk_weight_pct != null) metrics.push(['研究风险权重', fn(summary.risk_weight_pct, 2) + '%', '']);
  if (summary.residual_cash != null) metrics.push(['纸面剩余现金', '¥' + fn(summary.residual_cash, 0), '']);
  if (summary.latest_return_pct != null) metrics.push(['最新历史诊断', fpp(summary.latest_return_pct), cc(summary.latest_return_pct)]);
  if (summary.factor_ic != null) metrics.push(['最新历史 IC', fn(summary.factor_ic, 4), cc(summary.factor_ic)]);
  if (summary.forward_completed != null) metrics.push(['独立前向收盘', fn(summary.forward_completed, 0), '']);
  if (summary.forward_return_pct != null) metrics.push(['前向成本后累计', fpp(summary.forward_return_pct), cc(summary.forward_return_pct)]);
  if (summary.forward_max_drawdown_pct != null) metrics.push(['前向最大回撤', fpp(summary.forward_max_drawdown_pct), 'c-bear']);
  if (summary.historical_annualized_return_pct != null) metrics.push(['历史年化 · 50BP/边', fpp(summary.historical_annualized_return_pct), 'c-bull']); if (summary.historical_daily_max_drawdown_pct != null) metrics.push(['历史日频最大回撤 · 50BP/边', fpp(summary.historical_daily_max_drawdown_pct), 'c-bear']);
  [['normal_horizon_months','常规成本历史持有期'],['moderate_stress_horizon_months','中压历史持有期'],['severe_stress_horizon_months','高压历史持有期']].forEach(([key,label]) => { if (summary[key] != null) metrics.push([label, fn(summary[key], 0) + '个月', '']); });
  metrics.push(['执行状态', statusText, summary.recommendation_eligible === true ? 'c-bull' : 'c-bear']);
  grid.innerHTML = (available ? '' : '<div class="stale-banner stale-warn" style="grid-column:1/-1">策略摘要暂不可用。</div>')
    + metrics.map(row => `<div class="metric-cell"><span class="metric-val ${row[2]}">${esc(row[1])}</span><span class="metric-lbl">${esc(row[0])}</span></div>`).join('')
    + `<div style="grid-column:1/-1;padding:8px 10px;background:var(--bg-elevated);border-radius:8px;font-size:var(--text-xs);color:var(--text-secondary)">${esc(summary.reason || action.reason || decisionGateSummary(gate))} · <a href="/strategy-lab" style="color:var(--accent)">查看完整策略证据</a></div>`;
  const method = $('#strategyMethod');
  if (method) method.textContent = summary.method || action.label || '策略摘要';
}
// 策略开发注册表
function strategyFailedGateRows(bt) {
  const specs = {
    minimum_45_nonoverlapping_periods: ['非重叠样本至少45期', 'periods', v => `${fn(v, 0)}期`],
    median_at_least_500_eligible_stocks: ['合格股票中位数至少500只', 'median_eligible_stocks', v => `${fn(v, 0)}只`],
    total_return_positive: ['总收益必须为正', 'total_return_pct', fpp],
    annualized_return_at_least_10pct: ['年化收益至少10%', 'annualized_return_pct', fpp],
    annualized_excess_positive: ['相对上证年化超额必须为正', 'annualized_excess_return_pct', fpp],
    double_cost_total_positive: ['双倍成本压力后总收益必须为正', 'cost_stress_total_return_pct', fpp],
    max_drawdown_not_worse_than_20pct: ['最大回撤不差于-20%', 'max_drawdown_pct', fpp],
    period_win_rate_at_least_55pct: ['周期胜率至少55%', 'period_win_rate_pct', fpp],
    bootstrap_95pct_lower_mean_positive: ['区块Bootstrap 95%下限必须为正', 'block_bootstrap_95pct_lower_mean_pct', fpp],
    every_calendar_year_positive: ['每个自然年收益必须为正', 'yearly_total_return_pct', v => v && typeof v === 'object' ? Object.entries(v).map(([y, x]) => `${y} ${fpp(x)}`).join(' / ') : '—'],
  };
  const metrics = (bt && bt.metrics) || {};
  return ((bt && bt.failed_gates) || []).map(function (raw) {
    const text = String(raw || '');
    const split = text.startsWith('validation.') ? 'validation' : text.startsWith('test.') ? 'test' : 'test';
    const key = text.replace(/^(validation|test)\./, '').replace(/\(.+$/, '');
    const spec = specs[key];
    if (!spec) return { label:text || '未命名失败条件', actual:'详见完整策略证据' };
    const source = metrics[split] || metrics;
    const value = source && source[spec[1]];
    return { label:spec[0], actual:value == null ? '—' : spec[2](value) };
  });
}
async function fetchStrategyRegistry() {
  const requestGeneration = state.navSequence;
  const body = $('#strategyRegistryBody');
  if (!body) return;
  const data = await api('/api/strategy-lab/user-workbench?view=compact');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  if (!data) {
    body.innerHTML = '<div class="empty-state" style="padding:14px">策略注册表暂不可用。</div>';
    const c = $('#strategyRegCount'); if (c) c.textContent = '';
    return;
  }
  const sourceNotice = data.available === false
    ? '<div class="stale-banner stale-warn" style="margin:0 0 8px">当前输入面板未就绪，策略与回测档案仍可查看。</div>'
    : '';
  const strategies = data.saved_strategies || [];
  const backtests = (data.recent_backtests || []).slice(0, 8);
  const cnt = $('#strategyRegCount');
  if (cnt) cnt.textContent = strategies.length ? strategies.length + ' 个策略' : '空';
  const verdictOf = function (bt) {
    const promo = (bt && bt.promotion) || {};
    const unlocked = promo && promo.observation === 'unlocked';
    if (bt && bt.current_epoch_eligible === false) {
      return { cls: 'c-bear', txt: 'INVALIDATED·旧证据' };
    }
    const gatePassed = !!(bt && (bt.historical_confirmation_passed || bt.historical_validation_gate_passed));
    if (gatePassed) return { cls: 'c-dim', txt: 'HISTORICAL_PASS·仅研究' };
    if (bt && bt.failed_gates && bt.failed_gates.length) return { cls: 'c-bear', txt: 'NO-GO' };
    return { cls: 'c-dim', txt: unlocked ? '研究观察' : '—' };
  };
  const rows = strategies.map(function (s) {
    const id = s.strategy_id || '';
    const bt = backtests.find(function (b) { return b.strategy_id === id; }) || backtests[0];
    const v = verdictOf(bt);
    const m = (bt && bt.metrics) || {};
    const testMetrics = m.test || m;
    const oos = (testMetrics.annualized_return_pct != null)
      ? fpp(testMetrics.annualized_return_pct) : '—';
    const factors = Object.keys((s.factors || {})).filter(function (k) { return s.factors[k]; }).join(',');
    const failedRows = strategyFailedGateRows(bt);
    return `<a class="pos-row" href="/strategy-lab">
      <div class="pos-top">
        <span class="pos-name">${esc(s.name || '未命名策略')}</span>
        <span class="pos-code">${esc(s.strategy_id || '').slice(0, 14)}</span>
        <span class="verdict ${v.cls}" style="margin-left:auto;font-size:12px;font-weight:600">${v.txt}</span>
      </div>
      <div class="pos-px">
        <span class="pos-shares" style="color:var(--text-tertiary);font-size:12px">${s.revision_count != null ? 'v' + s.revision_count + ' 版' : ''}${factors ? ' · ' + esc(factors.slice(0, 40)) : ''}</span>
        <span style="margin-left:auto;font-size:12px;color:var(--text-tertiary)">OOS ${oos} · ${esc((bt && bt.generated_at || s.saved_at || '').slice(0, 16).replace('T', ' '))}</span>
      </div>
      ${failedRows.length ? `<div class="pos-px" style="display:grid;gap:3px;color:var(--bear);font-size:12px">${failedRows.slice(0, 2).map(row => `<span>${esc(row.label)} · 实际 ${esc(row.actual)}</span>`).join('')}</div>` : ''}
    </a>`;
  }).join('');
  body.innerHTML = sourceNotice + (rows
    ? rows
    : '<div class="empty-state" style="padding:14px">暂无已保存策略。<span style="display:block;margin-top:4px"><a href="/strategy-lab" style="color:var(--accent)">去策略研发页创建 →</a></span></div>');
}
function renderRiskSnapshot(account, portfolio) {
  const el = $('#riskSnapshot');
  if (!el) return;
  const pnl = portfolio.unrealized_pnl, pnlPct = portfolio.unrealized_pnl_pct;
  const marketValue = portfolio.market_value, availableCash = portfolio.available_cash, realized = portfolio.realized_pnl;
  el.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div class="metric-cell"><span class="metric-val">${availableCash==null?'—':'¥'+fn(availableCash,0)}</span><span class="metric-lbl">${portfolio.cash_user_recorded === true ? '真实可用现金' : '真实可用现金未录入'}</span></div>
      <div class="metric-cell"><span class="metric-val">${marketValue==null?'—':'¥'+fn(marketValue,0)}</span><span class="metric-lbl">手工持仓市值</span></div>
      <div class="metric-cell"><span class="metric-val ${realized==null?'':cc(realized)}">${realized==null?'—':`${realized>=0?'+':''}¥${fn(realized,0)}`}</span><span class="metric-lbl">已实现记录盈亏</span></div>
      <div class="metric-cell"><span class="metric-val ${pnl==null?'':cc(pnl)}">${pnl==null||pnlPct==null?'—':`${pnl>=0?'+':''}¥${fn(pnl,0)} · ${fpp(pnlPct)}`}</span><span class="metric-lbl">未实现盈亏</span></div>
    </div>
  `;
}
async function fetchMarket() {
  const requestGeneration = state.navSequence;
  const data = await api('/api/market');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  state.homeData.market = data || null;
  renderDecisionHome();
  if (!data) {
    const message = apiError('/api/market', '市场概览暂不可用');
    const indexes = $('#marketIndexes'); if (indexes) indexes.innerHTML = `<div class="empty-state" role="status">${esc(message)}</div>`;
    const regime = $('#marketRegime'); if (regime) { regime.textContent = '状态未知'; regime.className = 'regime-tag'; }
    const breadth = $('#breadthInfo'); if (breadth) breadth.textContent = message;
    return;
  }
  const el = $('#marketIndexes');
  if (el && data.indexes) {
    el.innerHTML = data.indexes.map(ix => {
      const chg = ix.change_pct;
      const ok = chg != null && chg > -99;
      return `<div class="idx-block">
        <span class="idx-name">${esc(ix.name)}</span>
        <span class="idx-price">${ix.price!=null?fp(ix.price):'休市'}</span>
        <span class="idx-change ${ok?cc(chg):''}">${ok?fpc(chg):''}</span>
      </div>`;
    }).join('');
  }
  const re = $('#marketRegime');
  if (re) {
    const breadth = data.breadth_up_ratio != null ? ` · 涨跌比 ${data.breadth_up_ratio}%` : '';
    re.textContent = (data.regime_label||data.regime) + breadth + ' · 描述性观察';
    re.className = 'regime-tag ' + (data.regime||'');
  }
  const bi = $('#breadthInfo');
  if (bi && data.regime) {
    bi.textContent = data.regime === 'strong' ? '↑ 市场普涨' : data.regime === 'weak' ? '↓ 市场普跌' : data.regime === 'divergent' ? '⇄ 分化' : '— 震荡';
  }
}
async function fetchResearchMarketState() {
  const requestGeneration = state.navSequence;
  const [research, strategyRoute] = await Promise.all([
    api('/api/market/research-state'),
    api('/api/strategy-route/research?view=dashboard'),
  ]);
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return null;
  state.homeData.research = research || null;
  state.homeData.strategyRoute = strategyRoute || null;
  renderSectorQuotePanelOps();
  window.dispatchEvent(new CustomEvent(
    'regis-strategy-route-snapshot', {detail:strategyRoute || null}
  ));
  renderDecisionHome();
  void fetchHomeRiskSnapshot(state.homeData.portfolio);
  return {research:research || null, strategyRoute:strategyRoute || null};
}
function renderSectorQuotePanelOps() {
  const target = $('#sectorQuotePanelOps');
  if (!target) return;
  const panel = state.homeData.research?.capture_runtime?.sector_quote_panel || {};
  const labels = {
    awaiting_window:'等待真实采集窗口', collecting:'正在采集',
    closed_complete:'本时槽路径完整', closed_incomplete:'本时槽路径不完整',
    error:'采集错误',
  };
  const phase = panel.phase || 'awaiting_window';
  target.dataset.phase = phase;
  const next = panel.next_slot ? ` · 下一窗口 ${String(panel.next_slot).replace('T',' ').slice(0,16)}` : '';
  const counts = phase === 'collecting'
    ? ` · 活跃 ${Number(panel.active)||0} · 本轮写入 ${Number(panel.captured)||0}` : '';
  const fieldLabels = {
    price:'实时价', change_pct:'涨跌幅', volume:'成交量', amount:'成交额',
    turnover_rate:'换手率', total_market_cap:'总市值',
    float_market_cap:'流通市值', data_as_of:'行情时间',
    stock_code:'证券代码', stock_name:'证券名称',
  };
  const issueLabel = issue => {
    if (!issue || typeof issue !== 'object') return String(issue || '');
    const sector = issue.sector_name || issue.sector_code || '板块';
    const codes = Array.isArray(issue.affected_codes) && issue.affected_codes.length
      ? issue.affected_codes.join('、') : '部分成分股';
    const fields = Array.isArray(issue.missing_fields) && issue.missing_fields.length
      ? issue.missing_fields.map(field => fieldLabels[field] || field).join('、') : '必要行情字段';
    return `${sector}：${codes} 的${fields}缺失，本时槽研究面板未启动`;
  };
  const problems = [panel.terminal_error]
    .concat(Array.isArray(panel.incomplete_reasons) ? panel.incomplete_reasons.map(issueLabel) : [])
    .filter(Boolean).filter((value, index, all) => all.indexOf(value) === index);
  target.innerHTML = `<b>盘中战法研究输入：${esc(labels[phase] || phase)}</b>${esc(next)}${esc(counts)}${problems.length ? ` · <span>${esc(problems.join('；'))}</span>` : ''}<small>${panel.panel_research_ready === true ? '日志链已核验完整；仍仅供未来独立战法研究。' : '仅采集未来独立战法所需的同槽行情；未通过日志核验前不标记完成。'}不改变今日仓位、交易笔数或提醒权限。</small>`;
}
async function fetchHomeStrategyEvidence() {
  const requestGeneration = state.navSequence;
  const data = await api('/static/strategy_evidence_status.json?v=8');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  state.homeData.strategyEvidence = data || null;
  renderDecisionHome();
}
async function fetchHomeRiskSnapshot(portfolio) {
  const open = Array.isArray(portfolio?.open) ? portfolio.open : [];
  if (!open.length || state.page !== 'dashboard') {
    state.homeData.riskReport = null;
    state.homeRiskSignature = '';
    state.homeRiskStatus = 'idle';
    return;
  }
  const signature = open
    .map(row => `${row.code || ''}:${Number(row.shares) || 0}`)
    .sort()
    .join('|');
  const stillFresh = signature === state.homeRiskSignature
    && Date.now() - state.homeRiskFetchedAt < 300000;
  if (state.homeRiskInFlight || stillFresh) return;
  state.homeRiskInFlight = true;
  state.homeRiskStatus = 'loading';
  renderDecisionHome();
  try {
    const report = await api('/api/risk/report?view=compact');
    if (state.page !== 'dashboard') return;
    state.homeData.riskReport = report || null;
    state.homeRiskStatus = report ? 'ready' : 'error';
    state.homeRiskSignature = signature;
    state.homeRiskFetchedAt = Date.now();
    renderDecisionHome();
  } finally {
    state.homeRiskInFlight = false;
  }
}
// ── Kronos
async function fetchKronos(unifiedSnapshot = null) {
  const requestGeneration = state.navSequence;
  const freshData = unifiedSnapshot || await api('/api/opportunities');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  if (freshData) state.kronosSnapshot = freshData;
  const reconnecting = !freshData && !!state.kronosSnapshot;
  const data = freshData || state.kronosSnapshot;
  const body = $('#kronosBody');
  const updated = $('#kronosUpdated');
  if (!body) return;
  if (updated && data) {
    const model = data.kronos_model || {};
    const sourceAsOf = model.source_data_as_of || '未提供';
    const sourceAge = model.data_staleness_days == null ? '年龄未知' : `${model.data_staleness_days}个日历日`;
    const freshness = model.freshness_status || '状态未知';
    const validation = model.validation_status === 'validated' ? '收益证据已验证' : '收益证据未验证';
    const horizon = model.horizon_semantics === 'audited_a_share_trading_sessions' ? '期限=审计交易日' : '旧自然日期限不可用于收益判断';
    const snapshotAsOf = data.pipeline_identity?.snapshot_as_of || data.date || '未提供';
    const snapshotClock = String(snapshotAsOf).includes(' ') ? String(snapshotAsOf).split(' ').pop() : snapshotAsOf;
    updated.textContent = `${reconnecting ? '服务重连中 · 上次快照 · ' : ''}同源快照 ${snapshotClock} · 输入 ${sourceAsOf} · ${freshness}`;
    updated.style.color = ['stale','unavailable','unknown'].includes(freshness) ? 'var(--bear)' : 'var(--text-tertiary)';
    updated.title = `与机会候选榜共享同一流水线快照；模型新鲜度只按输入数据日期判断。${sourceAge} · ${horizon} · ${validation}。模型制品生成于 ${model.generated_at || '未提供'}。`;
  }
  const items = data?.model_evidence_items || [];
  if (!items.length) {
    body.innerHTML = `<div class="empty-state" role="status">${esc(data ? '本轮统一评估中没有模型来源候选' : apiError('/api/opportunities', '统一机会快照暂不可用'))}</div>`;
    if (updated && !data) updated.textContent = '模型接口暂不可用 · 等待本地服务恢复';
    return;
  }
  body.innerHTML = items.map(r => {
    const a = r.assessment || {};
    const g = a.gates || {};
    const composite = g.composite || {};
    const comp = r.unified_composite_score != null && Number.isFinite(Number(r.unified_composite_score)) ? Number(r.unified_composite_score) : composite.composite;
    const rating = composite.rating;
    const verdict = a.verdict || 'PENDING';
    const verdictDisplay = {
      APPROVE: {cls:'approve', label:'机会质量通过'},
      WATCH: {cls:'watch', label:'继续观察'},
      PENDING: {cls:'pending', label:'证据待补'},
      REJECT: {cls:'reject', label:'不纳入机会'},
    }[verdict] || {cls:'pending', label:'证据待补'};
    const vCls = verdictDisplay.cls;
    const vLbl = verdictDisplay.label;
    const model = data.kronos_model || {};
    const ai = r.kronos?.pred_ret_20d;
    const modelValueVerified = model.research_only === false
      && (data.decision_eligible === true || model.decision_eligible === true)
      && model.validation_status === 'validated';
    const compTxt = rating != null ? `${esc(rating)}·${Number(comp||0).toFixed(0)}` : '综合 —';
    const quality = r.opportunity_quality || {};
    const hasMetric = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
    const expectedReturnVerified = hasMetric(quality.expected_return_20d_pct)
      && ['validated', 'verified', 'decision_eligible'].includes(String(quality.expected_return_status || '').toLowerCase());
    const returnEvidence = expectedReturnVerified
      ? `已验证20日预期收益 ${fpp(Number(quality.expected_return_20d_pct))}`
      : (modelValueVerified && hasMetric(ai)
          ? `已验证20日模型收益 ${fpp(Number(ai))}`
          : '20日收益证据未验证');
    const qualityFacts = [];
    const missingFacts = [];
    if (hasMetric(quality.max_drawdown_60d_pct)) qualityFacts.push(`60日回撤 ${fn(quality.max_drawdown_60d_pct, 2)}%`); else missingFacts.push('回撤');
    if (hasMetric(quality.annualized_volatility_pct)) qualityFacts.push(`年化波动 ${fn(quality.annualized_volatility_pct, 2)}%`); else missingFacts.push('波动');
    if (hasMetric(quality.round_trip_cost_pct)) qualityFacts.push(`${quality.comparison_role ? '统一名义仓位' : ''}往返成本 ${fn(quality.round_trip_cost_pct, 3)}%`); else missingFacts.push('成本');
    if (quality.comparison_role && hasMetric(quality.comparison_capital_yuan)) qualityFacts.push(`统一比较本金 ¥${fn(quality.comparison_capital_yuan, 0)}（非真实账户）`);
    if (hasMetric(quality.avg_amount_20d_yuan)) qualityFacts.push(`日均成交额 ¥${fn(quality.avg_amount_20d_yuan, 0)}`); else missingFacts.push('流动性');
    const qualitySummary = [quality.corporate_action_safe_returns === true ? '回撤/波动已校正除权' : '', qualityFacts.join(' · '), missingFacts.length ? `待补证：${missingFacts.join('、')}` : '交易质量证据已齐'].filter(Boolean).join(' · ');
    return `
      <div class="kronos-row ${a.can_alert ? 'kronos-can' : ''}" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(r.code || '')}" title="${escAttr(a.reason || '')}">
        <span style="font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.name||r.code)}</span>
        <span class="kronos-comp">${compTxt}</span>
        <span class="kronos-verdict ${vCls}">${vLbl}</span>
        <span class="c-dim">${returnEvidence}</span>
      </div>
      <div class="kronos-sub"><span>${qualitySummary}</span></div>
      ${compositeAsOfHtml(composite.as_of_audit, true)}
    `;
  }).join('');
  if (data) {
    body.innerHTML += '<div style="font-size:12px;color:var(--text-tertiary);padding:8px 12px;border-top:1px solid var(--border-subtle)">与机会候选榜同源；收益未验证时仅展示，不参与推荐。</div>';
  }
}
// ═══ 今日机会
let _oppMonitorStarted = false;
function setSpaPortfolioMonitorOwner(owner) {
  window.__regisSpaPortfolioMonitorOwner = owner || null;
  window.dispatchEvent(new CustomEvent('regis-portfolio-monitor-active', {
    detail:{ active:!!owner, owner:owner || null },
  }));
}
function opportunityRequestIsCurrent(generation) {
  return _oppMonitorStarted
    && state.page === 'dashboard'
    && generation === state.oppGeneration;
}
function startOpportunityMonitor() {
  if (_oppMonitorStarted || state.page !== 'dashboard' || document.hidden) return;
  _oppMonitorStarted = true;
  setSpaPortfolioMonitorOwner('dashboard');
  const generation = ++state.oppGeneration;
  fetchOpportunities(generation);
  fetchPositionsPanel();
  let tick = 0;
  if (state._oppInterval) clearInterval(state._oppInterval);
  state._oppInterval = setInterval(() => {
    if (document.hidden || state.page !== 'dashboard') return;
    fetchPositionsPanel(false);
    if (++tick % 12 === 0) { fetchOpportunities(generation); fetchReviewPanel(); }
  }, 5000);
}
function stopOpportunityMonitor() {
  _oppMonitorStarted = false;
  if (window.__regisSpaPortfolioMonitorOwner === 'dashboard') setSpaPortfolioMonitorOwner(null);
  state.oppGeneration += 1;
  if (state._oppInterval) { clearInterval(state._oppInterval); state._oppInterval = null; }
  const controller = state.requests.get('GET:/api/opportunities?view=monitor');
  if (controller) controller.abort('opportunity-monitor-stopped');
}
function _fmtCD(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = n => String(n).padStart(2, '0');
  return h > 0 ? `${p(h)}:${p(m)}:${p(sec)}` : `${p(m)}:${p(sec)}`;
}
async function fetchOpportunities(generation = state.oppGeneration) {
  if (document.hidden || !opportunityRequestIsCurrent(generation)) return;
  const [data, allocation] = await Promise.all([api('/api/opportunities?view=monitor'), api('/api/timing/alloc')]);
  if (!opportunityRequestIsCurrent(generation)) return;
  if (data) data.allocation_plan = allocation || null;
  state.homeData.opportunities = data || null;
  renderDecisionHome();
  if (!data || !data.items) return;
  state.oppItems = data.items;
  handleOppTrigger(data, generation);
  const researchSnapshot = await api('/api/opportunities');
  if (!opportunityRequestIsCurrent(generation) || !researchSnapshot) return;
  data.hypothetical_watch_items = researchSnapshot.watch_items || [];
  renderDecisionHome();
}
// ── 持仓 · 止盈止损监控 ──
async function fetchPositionsPanel() {
  const includeReview = arguments[0] !== false;
  if (document.hidden || state.page !== 'dashboard') return null;
  const requestGeneration = state.navSequence;
  const d = await api('/api/timing/portfolio');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return null;
  state.homeData.portfolio = d || null;
  state.homeData.portfolioStatus = d ? 'ready' : 'error';
  renderDecisionHome();
  if (d) {
    window.dispatchEvent(new CustomEvent('regis-portfolio-snapshot', { detail:d }));
    renderPositionsPanel(d);
    void fetchHomeRiskSnapshot(d);
  }
  else {
    const body = $('#positionsBody');
    if (body) body.innerHTML = `<div class="empty-state" role="status">${esc(apiError('/api/timing/portfolio', '手工持仓暂不可用'))}</div>`;
    const count = $('#manualPosCount');
    if (count) count.textContent = '不可用';
  }
  if (includeReview) fetchReviewPanel();
  return d || null;
}
async function fetchTimingPositionsMini() {
  const requestGeneration = state.navSequence;
  const data = await api('/api/timing/positions');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  const body = $('#timingPosMini');
  if (!body) return;
  if (!data) {
    body.innerHTML = `<div class="empty-state" role="status">${esc(apiError('/api/timing/positions', '时机研究持仓暂不可用'))}</div>`;
    const count = $('#timingPosCount');
    if (count) count.textContent = '不可用';
    return;
  }
  const positions = (data && data.positions) || [];
  const cnt = $('#timingPosCount');
  if (cnt) cnt.textContent = positions.length ? positions.length + ' 条' : '';
  if (!positions.length) {
    body.innerHTML = '<div class="empty-state" style="padding:16px;color:var(--text-tertiary);font-size:var(--text-sm)">暂无时机研究持仓记录。</div>';
    return;
  }
  body.innerHTML = positions.map(p => `
    <div class="pos-row" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(p.code || '')}">
      <div class="pos-top">
        <span class="pos-name">${esc(p.name||p.code)}</span>
        <span class="pos-code">${esc(p.code)}</span>
        <span class="pos-shares">${p.shares}股 @ ${fp(p.price)}</span>
        <span style="margin-left:auto;font-size:12px" class="${cc(p.pnl)}">${p.status==='closed'?(p.pnl!=null?fpp(p.pnl/100):'—'):''}</span>
      </div>
      <div class="pos-px">
        <span class="action-badge action-${p.action||'avoid'}" style="font-size:12px">${p.action==='buy_now'?'买点':p.action==='wait'?'等待':'回避'}</span>
        <span style="color:var(--text-tertiary);font-size:12px">${esc(p.level||'')} · ${esc(p.window_label||'')}</span>
        <span style="color:var(--text-tertiary);font-size:12px">${esc(entryAttributionLabel(p))}</span>
        <span style="margin-left:auto;font-size:12px;color:var(--text-tertiary)">${(p.created_at||'').slice(0,16).replace('T',' ')}</span>
        ${p.status==='open'
          ? '<button class="pos-del" type="button" data-action="sell-mini" data-id="' + Number(p.id) + '" data-price="' + (Number.isFinite(Number(p.current)) && Number(p.current) > 0 ? Number(p.current).toFixed(2) : '') + '" data-shares="' + Number(p.shares || 0) + '">卖出</button>'
          : '<span style="font-size:12px;color:var(--text-tertiary)">已平仓</span>'}
      </div>
    </div>
  `).join('');
}
function timingSellQuantity(rawValue, heldValue) {
  const shares = Number(rawValue);
  const held = Number(heldValue);
  const oddLot = Number.isInteger(held) ? held % 100 : 0, valid = Number.isInteger(held) && held >= 1
    && Number.isInteger(shares) && shares >= 1 && shares <= held
    && (shares % 100 === 0 || (oddLot > 0 && shares % 100 === oddLot));
  return {shares, held, remaining:valid ? held - shares : null, valid};
}
let _sellMaskEl = null, _sellId = null, _sellMaxShares = 0, _sellRefresh = null, _sellDialogCleanup = null, _sellAttempt = null;
const _deleteAttempts = new Map();
window._timingSellMini = async function (id, prefillPrice, opts) {
  if (window.RegisAccess?.canWrite !== true) { toast(window.RegisAccess?.reason || '写入权限未核实', 'warn'); return; }
  if (_sellMaskEl) _closeSell();
  _sellId = id;
  _sellMaxShares = Math.max(0, Math.trunc(Number(opts?.shares) || 0));
  _sellAttempt = null;
  _sellRefresh = (opts && opts.refresh) || fetchTimingPositionsMini;
  const mask = document.createElement('div');
  mask.className = 'timing-modal-mask';
  mask.setAttribute('role', 'dialog');
  mask.setAttribute('aria-modal', 'true');
  mask.setAttribute('aria-labelledby', 'sellDialogTitle');
  mask.innerHTML = `
    <div class="timing-modal">
      <h3 id="sellDialogTitle">记录卖出</h3>
      <div style="color:var(--text-tertiary);font-size:var(--text-xs);margin-bottom:10px">可记录全部或部分卖出 · 只记录你已完成的人工交易，不连接券商</div>
      <div class="sell-field" style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px">
        <label for="sellPrice" style="font-size:var(--text-xs);color:var(--text-tertiary)">卖出价格（¥）</label>
        <input type="number" id="sellPrice" min="0.01" step="0.01" placeholder="输入券商实际成交价，不代填现价" required
          style="padding:10px 14px;font-size:var(--text-base);border:1px solid var(--border-default);border-radius:var(--radius-sm);background:var(--bg-surface);color:var(--text-primary);font-family:var(--font-number)">
      </div>
      <div class="sell-field" style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px"><label for="sellFee" style="font-size:var(--text-xs);color:var(--text-tertiary)">券商实际卖出费用（可选，¥）</label>
        <input type="number" id="sellFee" min="0" step="0.01" placeholder="留空则按模型估算" style="padding:10px 14px;font-size:var(--text-base);border:1px solid var(--border-default);border-radius:var(--radius-sm);background:var(--bg-surface);color:var(--text-primary);font-family:var(--font-number)"></div>
      <div class="sell-field" style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px">
        <label for="sellShares" style="font-size:var(--text-xs);color:var(--text-tertiary)">卖出数量（整手；零股须一次卖完）</label>
        <input type="number" id="sellShares" min="1" step="1" max="${_sellMaxShares}" placeholder="输入实际成交股数，不默认清仓" required
          style="padding:10px 14px;font-size:var(--text-base);border:1px solid var(--border-default);border-radius:var(--radius-sm);background:var(--bg-surface);color:var(--text-primary);font-family:var(--font-number)">
        <div style="font-size:var(--text-xs);color:var(--text-tertiary);text-align:right">当前 ${_sellMaxShares} 股 · 卖后剩余 <span id="sellRemaining">—</span> 股</div>
      </div>
      <div class="modal-actions" style="display:flex;justify-content:flex-end;gap:10px">
        <button type="button" class="btn btn-ghost" id="sellCancel">取消</button>
        <button type="button" class="btn btn-primary" id="sellConfirm">确认卖出</button>
      </div>
    </div>`;
  document.body.appendChild(mask);
  _sellMaskEl = mask;
  _sellDialogCleanup = trapDialog(mask, _closeSell);
  mask.addEventListener('click', (e) => { if (e.target === mask) _closeSell(); });
  $('#sellCancel').addEventListener('click', _closeSell);
  $('#sellConfirm').addEventListener('click', () => _confirmSell());
  $('#sellShares').addEventListener('input', () => {
    const quantity = timingSellQuantity($('#sellShares').value, _sellMaxShares);
    $('#sellRemaining').textContent = quantity.remaining == null ? '—' : String(quantity.remaining);
  });
};
function _closeSell() {
  if (_sellDialogCleanup) { _sellDialogCleanup(); _sellDialogCleanup = null; }
  if (_sellMaskEl) { _sellMaskEl.remove(); _sellMaskEl = null; }
  _sellId = null;
  _sellMaxShares = 0;
  _sellAttempt = null;
}
async function _confirmSell() {
  if (!_sellId) return;
  const price = parseFloat(($('#sellPrice') || {}).value || '0');
  if (!price || isNaN(price) || price <= 0) { toast('请填写有效的卖出价格', 'error'); return; }
  const quantity = timingSellQuantity(($('#sellShares') || {}).value, _sellMaxShares);
  if (!quantity.valid) { toast(quantity.shares > quantity.held ? '卖出数量不能超过当前持仓' : '卖出数量须为 100 股整数倍，或一次性包含全部零股', 'error'); return; }
  const shares = quantity.shares, feeRaw = (($('#sellFee') || {}).value || '').trim();
  const sellFee = feeRaw === '' ? null : Number(feeRaw), payload = {id:_sellId, price, shares};
  if (sellFee !== null && (!Number.isFinite(sellFee) || sellFee < 0)) { toast('实际卖出费用需为不小于 0 的金额', 'error'); return; }
  if (sellFee !== null) payload.sell_fee_yuan = sellFee;
  const fingerprint = JSON.stringify(payload);
  if (!_sellAttempt || _sellAttempt.fingerprint !== fingerprint) {
    _sellAttempt = {fingerprint, key:createIdempotencyKey('sell-position')};
  }
  const btn = $('#sellConfirm'); if (btn) btn.disabled = true;
  try {
    const data = await apiWrite(
      '/api/timing/sell', payload, 'POST',
      {idempotencyKey:_sellAttempt.key},
    );
    if (!data._httpOk) { toast(data.error || '卖出失败', 'error'); return; }
    toast(data.message || '已记录卖出', 'success');
    const refresh = _sellRefresh || fetchTimingPositionsMini;
    _closeSell();
    refresh();
  } catch (e) { toast('网络错误，请重试', 'error'); }
  finally { if (btn) btn.disabled = false; }
}
window.delPos = function (id, name) {
  if (!confirm(`确定删除「${name}」这条持仓记录？`)) return;
  const fingerprint = String(id);
  let attempt = _deleteAttempts.get(fingerprint);
  if (!attempt) {
    attempt = {key:createIdempotencyKey('delete-position')};
    _deleteAttempts.set(fingerprint, attempt);
  }
  apiWrite('/api/timing/positions/' + encodeURIComponent(id), null, 'DELETE', {idempotencyKey:attempt.key})
    .then(d => {
      if (!d._httpOk) { toast(d.error || '删除失败', 'error'); return; }
      _deleteAttempts.delete(fingerprint);
      toast(d.message || '已删除', 'success');
      fetchPositionsPanel();
    });
};
async function fetchReviewPanel() {
  const requestGeneration = state.navSequence;
  const session = state.homeData.portfolio?.market_session || {};
  const sessionStatus = String(session.status || session.phase || '').toLowerCase();
  const reviewEndpoint = ['closed', 'preopen'].includes(sessionStatus)
    ? '/api/timing/review?scope=last_review' : '/api/timing/review';
  const d = normalizeReviewData(await api(reviewEndpoint));
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  state.homeData.review = d || null;
  renderDecisionHome();
  if (d) { window.__lastReview = d; renderReviewPanel(d); }
  else {
    const body = $('#reviewBody');
    if (body) body.innerHTML = `<div class="empty-state" role="status">${esc(apiError(reviewEndpoint, '手工复盘暂不可用'))}</div>`;
    const date = $('#reviewDate');
    if (date) date.textContent = '不可用';
  }
  if (d && Array.isArray(d.sell_events) && d.sell_events.length) {
    void api('/api/timing/exit-attribution').then(attribution => {
      if (!attribution || state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
      d.exit_attribution = attribution;
      state.homeData.review = d;
      renderDecisionHome();
    });
  }
}
function normalizeReviewData(raw) {
  return RegisSpa.review?.normalizeReviewData(raw) ?? raw;
}
function renderReviewPanel(d) {
  d = normalizeReviewData(d) || {};
  const body = $('#reviewBody');
  const dt = $('#reviewDate');
  if (!body) return;
  if (dt) dt.textContent = `${d.date || ''} · ${esc(d.market_state || '')}`;
  const ac = d.alert_counts || {};
  const alerts = d.alerts || [], observations = d.research_observations || [], bought = d.bought_today || [], imported = d.position_snapshots_today || [], closed = d.closed || [];
  const unresolved = d.unresolved || [];
  const openPnl = d.open_pnl_total;
  const realized = d.realized_pnl;
  const perf = d.execution_performance || {};
  const accountPerformance = d.account_performance || {};
  const attributionHtml = strategyAttributionHtml(perf);
  const statusLbl = { triggered: '未处理', confirmed: '已确认', dismissed: '已忽略', superseded: '已过期', expired: '已过期' };
  const statusColor = { triggered: '#ef4444', confirmed: '#22c55e', dismissed: '#8b8b98', superseded: '#8b8b98', expired: '#8b8b98' };
  const un = unresolved.length;
  const alertRows = alerts.slice(0, 8).map(a => {
    const buy = a.action === 'buy';
    const st = a.status || 'triggered';
    const timing = a.timing_interpretation || {};
    const roleLabel = timing.summary ? '风险复核' : (a.window_label || '');
    return `<div style="display:flex;align-items:center;gap:8px;padding:6px 14px;border-bottom:1px solid var(--border-subtle);font-size:12px">
      <span class="card-badge" style="background:${buy ? 'rgba(220,38,38,0.15)' : 'rgba(22,163,74,0.15)'};color:${buy ? '#ef4444' : '#22c55e'}">${buy ? '买' : '卖'}</span>
      <span style="font-weight:600">${esc(a.name || a.code)}</span>
      <span style="color:var(--text-tertiary)">${esc(roleLabel)}</span>
      ${timing.summary ? `<span style="color:var(--text-secondary);flex:1;min-width:220px">${esc(timing.summary)} ${esc((timing.facts || []).join(' · '))}</span>` : ''}
      <span style="margin-left:auto;color:${statusColor[st] || '#8b8b98'}">${(a.created_at || '').slice(11, 16)} ${statusLbl[st] || st}</span>
    </div>`;
  }).join('');
  const observationRows = observations.slice(0, 8).map(a => {
    const plan = a.review_plan || {};
    const shares = Number(plan.shares);
    const limit = Number(plan.limit_price);
    const planText = Number.isInteger(shares) && shares > 0
      ? `旧链路曾计算 ${shares} 股${Number.isFinite(limit) && limit > 0 ? ` · 当时限价 ¥${fn(limit, 2)}` : ''}`
      : '未形成完整数量计划';
    return `<div style="display:flex;align-items:center;gap:8px;padding:6px 14px;border-bottom:1px solid var(--border-subtle);font-size:12px;flex-wrap:wrap">
      <span class="card-badge" style="background:rgba(148,163,184,.12);color:#94a3b8">研究观察</span>
      <span style="font-weight:600">${esc(a.name || a.code)}</span>
      <span style="color:var(--text-tertiary)">${esc(planText)}</span>
      <span style="color:var(--gold);margin-left:auto">未获情绪→板块→战法统一授权 · 不构成买入提示</span>
    </div>`;
  }).join('');
  const tlRows = (d.timeline || []).map(e => {
    const col = e.type === 'alert' ? (e.status === 'triggered' ? '#ef4444' : '#8b8b98') : e.type === 'research_observation' ? '#94a3b8' : e.type === 'buy' ? '#22c55e' : e.type === 'position_snapshot' ? '#64748b' : '#f59e0b';
    const tag = e.type === 'alert' ? (e.kind === 'buy' ? '提醒' : (e.timing_interpretation ? '风险复核' : '卖出提醒')) : ({buy:'买入', position_snapshot:'导入', research_observation:'观察'}[e.type] || '卖出');
    return `<div style="display:flex;align-items:center;gap:8px;padding:6px 14px;border-bottom:1px solid var(--border-subtle);font-size:12px">
      <span style="color:var(--text-tertiary);width:38px;flex:none">${esc(e.time || '')}</span>
      <span class="card-badge" style="background:${col}22;color:${col}">${tag}</span>
      <span style="font-weight:600">${esc(e.label)}</span>
      ${['alert', 'research_observation'].includes(e.type) ? `<span style="margin-left:auto;color:${statusColor[e.status] || '#8b8b98'}">${statusLbl[e.status] || e.status}</span>` : ''}
    </div>`;
  }).join('');
  const buyRows = bought.slice(0, 5).map(p => {
    const pnl = p.pnl;
    return `<div style="display:flex;gap:8px;align-items:center;padding:6px 14px;border-bottom:1px solid var(--border-subtle);font-size:12px">
      <span style="font-weight:600">${esc(p.name)}</span>
      <span style="color:var(--text-tertiary)">${p.initial_shares ?? '未记录'}股 · 原始成交价 ${fp(p.price)}</span>
      <span style="color:var(--text-tertiary)">${esc(entryAttributionLabel(p))}</span><span style="color:var(--text-tertiary)">${esc(executionSlippageLabel(p))}</span>
      ${p.current ? `<span style="color:var(--text-tertiary)">现价 ${fp(p.current)}</span>` : ''}
      <span style="margin-left:auto;color:${pnl == null ? 'var(--text-tertiary)' : pnl >= 0 ? '#ef4444' : '#22c55e'};font-weight:600">${pnl == null ? '行情暂不可得' : fn(pnl, 2)}</span>
    </div>`;
  }).join('');
  const importedRows = imported.slice(0, 5).map(p => `<div style="display:flex;gap:8px;align-items:center;padding:6px 14px;border-bottom:1px solid var(--border-subtle);font-size:12px"><span style="font-weight:600">${esc(p.name)}</span><span style="color:var(--text-tertiary)">${p.shares}股 · 平均成本 ${fp(p.price)}</span><span style="color:var(--text-tertiary)">已有持仓快照 · 原始买入日未记录</span>${p.current ? `<span style="color:var(--text-tertiary)">现价 ${fp(p.current)}</span>` : ''}<span style="margin-left:auto;color:${p.pnl == null ? 'var(--text-tertiary)' : p.pnl >= 0 ? '#ef4444' : '#22c55e'};font-weight:600">${p.pnl == null ? '行情暂不可得' : fn(p.pnl, 2)}</span></div>`).join('');
  const closedRows = closed.slice(0, 5).map(p => `
    <div style="display:flex;gap:8px;align-items:center;padding:6px 14px;border-bottom:1px solid var(--border-subtle);font-size:12px">
      <span style="font-weight:600">${esc(p.name)}</span>
      <span style="color:var(--text-tertiary)">${p.sold_shares_total ?? p.initial_shares ?? p.shares}股 · 买${fp(p.price)} → 卖均${fp(p.average_sell_price ?? p.sold_price)}</span>
      <span style="color:var(--text-tertiary)">${esc(entryAttributionLabel(p))}</span>
      ${p.hold_days != null ? `<span style="color:var(--text-tertiary)">持${p.hold_days}天</span>` : ''}
      <span style="margin-left:auto;color:${(p.pnl || 0) >= 0 ? '#ef4444' : '#22c55e'};font-weight:600">${fn(p.pnl || 0, 0)}${p.pnl_pct != null ? ` (${fpp(p.pnl_pct)})` : ''}</span>
    </div>`).join('');
  const hasAny = alerts.length || observations.length || bought.length || imported.length || closed.length || d.fill_review?.fill_count;
  body.innerHTML = `
    <div style="display:flex;gap:18px;padding:12px 14px;border-bottom:1px solid var(--border-subtle);flex-wrap:wrap;align-items:center">
      <span style="font-size:13px">可操作提醒 <b style="color:var(--accent)">${ac.total || 0} 次</b>${un ? ` <b style="color:#ef4444">(${un} 未处理)</b>` : ''}</span>
      <span style="font-size:13px">研究观察 <b>${observations.length} 条</b></span>
      <span style="font-size:13px">今日实际买入 <b>${d.bought_count || 0} 笔</b></span><span style="font-size:13px">导入已有持仓 <b>${d.position_snapshot_count || 0} 笔</b></span>
      <span style="font-size:13px">扣费后 <b class="${realized == null ? '' : realized >= 0 ? 'c-bull' : 'c-bear'}">${realized == null ? '待核验' : fn(realized, 2)}</b></span>
      <span style="font-size:13px">持仓浮盈 <b class="${openPnl === null || openPnl === undefined ? '' : openPnl >= 0 ? 'c-bull' : 'c-bear'}">${openPnl === null || openPnl === undefined ? '—' : fn(openPnl, 2)}</b></span>
      <span style="font-size:13px">账户变化 <b class="${cc(accountPerformance.cashflow_adjusted_return_pct)}">${accountPerformance.cashflow_adjusted_return_pct == null ? '—' : fpp(accountPerformance.cashflow_adjusted_return_pct)}</b><small class="c-dim"> · 现金流调整后</small></span>
      <button id="copyReviewBtn" type="button" data-action="copy-review" style="margin-left:auto;background:rgba(99,102,241,0.12);color:var(--accent);border:1px solid rgba(99,102,241,0.35);border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer">复制复盘</button>
    </div>
    ${attributionHtml}
    ${RegisSpa.review?.renderFillReview(d) || ''}
    ${tlRows ? `<div style="padding:8px 14px 4px;font-size:12px;color:var(--text-tertiary)">今天发生了什么 · 按时间</div>${tlRows}` : ''}
    ${alertRows ? `<div style="padding:8px 14px 4px;font-size:12px;color:var(--text-tertiary)">可操作提醒记录（必须具有统一决策授权）</div>${alertRows}` : ''}
    ${observationRows ? `<div style="padding:8px 14px 4px;font-size:12px;color:var(--text-tertiary)">研究观察记录（保留审计，不计为买卖提醒）</div>${observationRows}` : ''}
    ${buyRows ? `<div style="padding:8px 14px 4px;font-size:12px;color:var(--text-tertiary)">今日实际买入成交</div>${buyRows}` : ''}
    ${importedRows ? `<div style="padding:8px 14px 4px;font-size:12px;color:var(--text-tertiary)">今日导入的已有持仓（不计为买入）</div>${importedRows}` : ''}
    ${closedRows ? `<div style="padding:8px 14px 4px;font-size:12px;color:var(--text-tertiary)">今日手工平仓记录</div>${closedRows}` : ''}
    ${!hasAny ? '<div class="empty-state">今日没有已触发事件 · 当前持仓动作请看上方“持仓动作提醒”</div>' : ''}`;
}
function buildReviewReport(d) {
  return RegisSpa.review.buildReviewReport(d);
}
async function copyReviewReport() {
  let d = window.__lastReview;
  if (!d) { await fetchReviewPanel(); d = window.__lastReview; }
  if (!d) return;
  await RegisSpa.review.copyReviewReport(d);
}
window.copyReviewReport = copyReviewReport;
window.buildReviewReport = buildReviewReport;
function renderPositionsPanel(d) {
  const body = $('#positionsBody'), cnt = $('#manualPosCount');
  if (!body) return;
  const list = d.open || [];
  const accountGaps = d.summary?.account_fact_gaps || [];
  const actionsActive = d.alerts_active === true && d.market_session?.is_open === true;
  const decisionDay = String(d.market_session?.server_time || '').slice(0, 10);
  const reviewCount = actionsActive
    ? list.filter(p => ['EXIT_REVIEW', 'REDUCE_REVIEW'].includes(p.trade_plan?.state) && p.trade_plan?.action).length
    : 0;
  if (cnt) cnt.textContent = list.length ? `${list.length} 只 · ${reviewCount} 项复核` : '';
  if (!list.length) { body.innerHTML = '<div class="empty-state">暂无手工持仓 · 使用“记一笔”录入后会显示在这里</div>'; return; }
  const accountGapBanner = accountGaps.length
    ? `<div class="stale-banner stale-warn"><strong>账户事实待补</strong> · ${esc(accountGaps.join('、'))}；缺失项不生成精确买卖数量。</div>`
    : '';
  body.innerHTML = accountGapBanner + list.map(p => {
    const lv = p.levels || {}, ra = p.risk_action || {}, tp = p.trade_plan || {}, planAction = tp.action || null;
    const hasQuantityPlan = ['EXIT_REVIEW', 'REDUCE_REVIEW'].includes(tp.state) && planAction;
    const isExit = actionsActive && tp.state === 'EXIT_REVIEW' && planAction;
    const isReduce = actionsActive && tp.state === 'REDUCE_REVIEW' && planAction;
    const planLabel = isExit ? `离场复核 · ${Number(planAction.shares || 0)}股` : isReduce ? `减仓复核 · ${Number(planAction.shares || 0)}股 → ${Number(planAction.target_shares || 0)}股` : hasQuantityPlan ? '休市风险预警 · 下个交易时段重新复核' : '持有观察';
    const planColor = isExit ? 'var(--bear)' : isReduce || hasQuantityPlan ? 'var(--gold)' : 'var(--text-secondary)';
    const riskLine = effectiveRiskLevel(ra.risk_levels, ra.effective_risk_level)
      || effectiveRiskLevel(lv.risk_levels, lv.effective_risk_level);
    const progress = positionProgressModel(p.prog);
    const sellableDay = String(p.sellable_as_of || '').slice(0, 10);
    const recordedSellable = Number.isInteger(Number(p.sellable_shares_snapshot))
      ? Number(p.sellable_shares_snapshot) : null;
    const sellableCurrent = Boolean(decisionDay)
      && sellableDay === decisionDay && recordedSellable !== null;
    const sellable = sellableCurrent
      ? `${recordedSellable}股 / ${Number(p.shares || 0)}股`
      : '未核验';
    const priorSellable = !sellableCurrent && recordedSellable !== null && sellableDay
      ? ` · 上次 ${esc(sellableDay)} 为${recordedSellable}股` : '';
    const sellPrice = Number.isFinite(Number(p.current)) && Number(p.current) > 0 ? Number(p.current) : '';
    return `<div class="pos-row" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(p.code || '')}">
      <div class="pos-top">
        <span class="pos-name">${esc(p.name)}</span>
        <span class="pos-code">${esc(p.code)}</span>
        <span class="pos-shares">${p.shares}股 @ ${fp(p.price)}</span>
        <button class="pos-del" type="button" title="核验券商今日可卖股数" data-action="sellable-inventory" data-id="${Number(p.id)}" data-name="${escAttr(p.name)}" data-shares="${Number(p.shares || 0)}" data-sellable="${sellableCurrent ? recordedSellable : ''}" data-as-of="${sellableCurrent ? escAttr(sellableDay) : ''}">更新可卖</button>
        <button class="pos-del" type="button" title="记录全部或部分卖出" data-action="sell-mini" data-id="${Number(p.id)}" data-price="${sellPrice}" data-shares="${Number(p.shares || 0)}" data-refresh="positions">卖出</button>
        <button class="pos-del" type="button" title="删除这条记录" data-action="delete-position" data-id="${Number(p.id)}" data-name="${escAttr(p.name)}">删</button>
      </div>
      <div class="pos-px">
        <span class="pos-cur">${fp(p.current)}</span>
        <span class="pos-pnl ${cc(p.pnl_pct)}">${fpp(p.pnl_pct)}${p.pnl != null ? ' (' + fn(p.pnl, 0) + ')' : ''}</span>
        <span style="font-size:12px;color:var(--text-tertiary)">${esc(entryAttributionLabel(p))}</span>
        <span style="font-size:12px;color:var(--text-tertiary)">今日可卖 ${esc(sellable)}${sellableCurrent ? ` · ${esc(sellableDay)}` : priorSellable}</span>
        <span style="margin-left:auto;font-size:12px;color:var(--text-tertiary)">${esc(riskLevelMeta(riskLine))}</span>
      </div>
      <div style="margin-top:6px;font-size:12px;color:${planColor}">
        <b>${esc(planLabel)}</b> · ${esc(tp.reason||'当前没有带精确数量的退出依据')}
        ${ra.atr14!=null?` · ATR14 ${fp(ra.atr14)}`:''}
      </div>
      <div class="pos-progress">
        <span class="pos-lbl">下线</span>
        ${progress.available
          ? `<div class="pos-track"><div class="pos-fill" style="width:${progress.value}%;background:${progress.color}"></div></div>`
          : '<div class="pos-track" aria-label="行情不可用，不计算风险进度"></div>'}
        <span class="pos-lbl">上沿2</span>
        <span class="pos-lbl" style="width:72px;text-align:right"><b>${progress.available ? progress.value + '%' : '行情不可用'}</b></span>
      </div>
    </div>`;
  }).join('');
}
// ── Strategy Lab Widget ──
function positionProgressModel(rawValue) {
  if (rawValue == null || !Number.isFinite(Number(rawValue))) {
    return {available:false, value:null, color:null};
  }
  const value = Math.max(0, Math.min(100, Number(rawValue)));
  return {
    available:true, value,
    color:value >= 100 ? '#22c55e' : value >= 50 ? '#f59e0b' : '#ef4444',
  };
}
// ── 手工持仓记录 ──
function updateOppBadge(data) {
  const b = $('#oppPhase');
  if (!b) return;
  const w = data.buy_window || {};
  if (data.decision_gate?.research_only) {
    if (['actionable', 'best'].includes(w.phase)) b.textContent = '研究提醒监控中 · 人工复核';
    else if (w.phase === 'closed' && w.countdown_target) b.textContent = `研究提醒待机 · ${w.countdown_target}`;
    else b.textContent = `手工决策模式 · ${w.label || '监控中'} · 不自动下单`;
    return;
  }
  if (['actionable', 'best'].includes(w.phase)) b.textContent = `${w.label} · 可买入`;
  else if (w.phase === 'closed') b.textContent = '休市';
  else b.textContent = w.label || '监控中';
}
function timingConditionSummary(item) {
  const states = Object.values(item?.signals || {}).map(sig => sig?.state || 'miss');
  const available = states.filter(s => s !== 'unavailable').length, hits = states.filter(s => s === 'hit').length, partial = states.filter(s => s === 'partial').length;
  return !available ? '买点条件数据不足' : `${hits}/${available} 条命中${partial ? ` · ${partial} 条部分满足` : ''}`;
}
function renderOpportunities(data, exclusions, exclusionsError='') {
  const body = $('#signalsBody');
  if (!body) return;
  const exclSet = new Set((exclusions && exclusions.items || []).map(e => e.code));
  const items = (data.items || []).filter(it => !exclSet.has(it.code));
  const watchItems = (data.watch_items || []).filter(it => !exclSet.has(it.code));
  const rejected = (data.rejected || []).filter(r => !exclSet.has(r.code));
  const blocked = (data.blocked || []).filter(r => !exclSet.has(r.code));
  const researchState = (it) => String(
    it.action_state || it.display_state || it.presentation_state || it.status
      || it.assessment?.action_state || it.assessment?.verdict || ''
  ).toLowerCase();
  const pending = watchItems.filter(it => {
    const state = researchState(it);
    return it.assessment?.verdict === 'PENDING' || !state
      || state.includes('pending') || state.includes('wait_data') || state.includes('preheat')
      || state.includes('unknown') || state.includes('unavailable');
  });
  const observing = watchItems.filter(it => !pending.includes(it));
  const hardBlocked = blocked.concat(rejected);
  const w = data.buy_window || {}, researchOnly = data.decision_gate?.research_only === true;
  const km = data.kronos_model || {};
  const modelFresh = km.source_data_as_of
    ? ` · 模型输入截至 ${esc(km.source_data_as_of)}${km.data_staleness_days != null ? `（${km.data_staleness_days}个日历日前）` : ''} · ${esc(km.freshness_status || '状态未知')}`
    : '';
  const zeroReasons = data.signal_status?.zero_reasons || [], pipelineAsOf = data.pipeline_identity?.snapshot_as_of || data.date || '';
  const forward = data.manual_forward_evidence || {}, forwardSummary = forward.status ? `<span style="display:block;margin-top:4px;color:var(--text-tertiary)">V7前向证据：触发 ${fn(forward.eligible_alert_count, 0)} · 已完成 ${fn(forward.completed_outcome_count, 0)}/${fn(forward.minimum_completed_signals_for_descriptive_review, 0)} · ${forward.stable_profit_proven === true ? '已达到当前证据口径' : '稳定收益未证实'}</span>` : '';
  const scope = data.monitoring_scope || {}, strategy = data.assessment_strategy || {}, coverageLabel = scope.universe_coverage_label || '沪深审计市场', compared = Number(strategy.professional_preview_available_count), eligible = Number(strategy.frozen_eligible_compared_count ?? strategy.professional_preview_scope_count), retained = Number(strategy.runtime_retained_scored_count ?? scope.merged_candidate_count);
  const previewText = Number.isFinite(compared) && Number.isFinite(eligible) && eligible > 0 ? ` · 冻结专业可比 ${compared}/${eligible}${compared < eligible ? ' · 后台补齐中' : ''}` : '', inspected = Number(scope.full_market_inspected_count), scanVerified = scope.full_market_scan_status === 'verified' && Number.isFinite(inspected), scanText = scanVerified ? `实扫 ${inspected.toLocaleString()} 只` : `审计清单 ${Number(scope.full_market_universe_count || 0).toLocaleString()} 只 · 实扫证据待刷新`, currentAsOf = Number(scope.full_market_current_asof_count), currentAsOfText = Number.isFinite(currentAsOf) ? ` · 当期合格 ${currentAsOf.toLocaleString()}` : '', retainedText = Number.isFinite(retained) ? ` · 保留候选 ${retained}` : '', scopeText = scope.full_market_universe_count != null ? `${esc(coverageLabel)}审计覆盖 · ${scanText}${currentAsOfText}${previewText}${retainedText} · 深评 ${Number(scope.deep_assessed_count || 0)} · ${scope.full_a_share_coverage === false ? '北交所待授权 · ' : ''}无随机轮换` : `候选 ${Number(data.candidate_count || 0)}${previewText}${retainedText} · 深评 ${Number(data.assessed_count || 0)}`;
  const zeroSummary = zeroReasons.length ? `<span style="display:block;margin-top:4px;color:var(--gold)">当前 0 条提醒：${zeroReasons.map(esc).join('；')}</span>` : '';
  const head = `
    <div style="padding:8px 16px;border-bottom:1px solid var(--border-subtle);font-size:12px;color:var(--text-tertiary);display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
      <span>${researchOnly?'手工决策模式 · 不自动下单':'机会质量通过'} · 统一流水线快照 ${esc(pipelineAsOf)} · ${esc(w.label||'')}${modelFresh}${zeroSummary}${forwardSummary}</span>
      <span>${scopeText} · 机会 ${items.length} 只</span>
    </div>`;
  const rows = items.map((it) => {
    const lv = it.levels || {};
    const levelRows = Array.isArray(lv.risk_levels) ? lv.risk_levels : [];
    const activeLevel = effectiveRiskLevel(levelRows, lv.effective_risk_level);
    const researchLevels = levelRows.filter(level => level !== activeLevel);
    const alert = it.alert || {};
    const itemResearchOnly = !isProductionAlert(it);
    const isTrig = alert.status === 'triggered';
    const ai = it.kronos || {};
    const modelReview = it.model_review || {};
    const modelValueVerified = km.research_only === false
      && km.decision_eligible === true
      && km.validation_status === 'validated';
    const quality = it.opportunity_quality || {};
    const finiteQuality = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
    const expectedReturnVerified = finiteQuality(quality.expected_return_20d_pct)
      && ['validated', 'verified', 'decision_eligible'].includes(String(quality.expected_return_status || '').toLowerCase());
    const qualityFacts = [
      expectedReturnVerified
        ? `已验证20日预期收益 <b>${fpp(Number(quality.expected_return_20d_pct))}</b>`
        : '<b>无已验证收益预测</b>',
      finiteQuality(quality.trailing_20d_pct) ? `20日走势代理（非预测） ${fpp(Number(quality.trailing_20d_pct))}` : '',
      finiteQuality(quality.max_drawdown_60d_pct) ? `60日最大回撤 ${fn(quality.max_drawdown_60d_pct, 2)}%` : '',
      finiteQuality(quality.annualized_volatility_pct) ? `年化波动 ${fn(quality.annualized_volatility_pct, 2)}%` : '',
      finiteQuality(quality.round_trip_cost_pct) ? `${quality.comparison_role ? '统一名义仓位' : '预估'}往返成本 ${fn(quality.round_trip_cost_pct, 3)}%` : '',
      quality.comparison_role && finiteQuality(quality.comparison_capital_yuan) ? `统一比较本金 ¥${fn(quality.comparison_capital_yuan, 0)}（非真实账户）` : '',
      finiteQuality(quality.avg_amount_20d_yuan) ? `20日平均成交额 ¥${fn(quality.avg_amount_20d_yuan, 0)}` : '',
      finiteQuality(quality.participation_pct) ? `${quality.comparison_role ? '统一名义仓位' : '计划'}成交占比 ${fn(quality.participation_pct, 3)}%` : '',
    ].filter(Boolean);
    const qualitySource = [quality.ranking_return_basis ? `排序依据 ${esc(quality.ranking_return_basis)}` : '',
      quality.corporate_action_safe_returns === true ? '回撤/波动已校正除权' : '',
      quality.data_as_of ? `数据截至 ${esc(quality.data_as_of)}` : '',
    ].filter(Boolean).join(' · ');
    const chg = it.change_pct;
    const actionState = it.assessment?.action_state || 'WAIT_ENTRY';
    const manualReviewReady = it.manual_review_state === 'ready_for_manual_review' || actionState === 'ENTRY_READY';
    const lvCls = manualReviewReady ? 'buy_now' : it.recommended_action === 'wait' ? 'wait' : 'avoid';
    const cd = isTrig && alert.countdown_seconds != null ? _fmtCD(alert.countdown_seconds) : '';
    const dotCls = isTrig ? 'triggered' : alert.status === 'idle' ? 'idle' : 'watching';
    const dotLbl = itemResearchOnly ? (manualReviewReady ? '人工复核时机满足 · 非推荐' : '继续观察时机') : isTrig ? (alert.action === 'buy' ? '策略买入复核' : '建议卖出')
      : alert.status === 'idle' ? '已提醒' : '监控中';
    const winActionable = it.buy_window && ['actionable', 'best'].includes(it.buy_window.phase);
    const actLbl = itemResearchOnly ? (manualReviewReady ? '人工复核时机' : '继续观察') : it.recommended_action === 'buy_now'
      ? (winActionable ? '可买入' : '等复核窗口')
      : it.recommended_action === 'wait' ? '观望' : '暂不宜';
    const winGateNote = itemResearchOnly ? `研究时机状态，不是买入推荐 · ${esc(it.assessment?.reason || '继续观察')}`
      : isTrig ? esc(alert.reason)
      : (!winActionable && it.recommended_action === 'buy_now'
          ? '未到人工复核窗口 · 当前不触发全屏提醒'
          : esc(alert.reason || ''));
    const co = it.company || null;
    const coVeto = co && co.veto ? (co.vetoed_gates || []).map(g => ({fundamentals:'基本面',governance:'治理',industry:'产业',events:'事件',tradeable:'交易'}[g] || g)).join('、') : '';
    const coCmp = co && co.composite;
    const a = it.assessment || {};
    const aGates = a.gates || {};
    const aComp = aGates.composite || {};
    const aReason = a.reason || '';
    const G_LBL = { exclusion:'排除名单', five_gate:'核心条件', timing:'时机', composite:'综合分', price_fit:'价格', model:'模型证据', risk:'风控' }, unifiedCompositeScore = it.unified_composite_score != null && Number.isFinite(Number(it.unified_composite_score)) ? Number(it.unified_composite_score) : (aComp.composite != null && Number.isFinite(Number(aComp.composite)) ? Number(aComp.composite) : null), actionRating = aComp.action_rating || aComp.rating || aComp.score_rating || '未评级', scoreBandRating = aComp.score_band_rating || aComp.score_rating || aComp.rating;
    const scoreBandNote = scoreBandRating && scoreBandRating !== actionRating ? ` · 研究分档 ${esc(scoreBandRating)}（不代表推荐）` : ''; let coTag;
    if (a.verdict === 'APPROVE') {
      coTag = `<a href="/company/${encodeURIComponent(it.code || '')}" class="opp-cp-tag opp-cp-green">✓ 统一评估通过 · 研究状态 ${esc(actionRating)}${unifiedCompositeScore != null ? ` · ${unifiedCompositeScore.toFixed(0)}分` : ''}${scoreBandNote}</a>`;
    } else if (co && co.veto) {
      coTag = `<a href="/company/${encodeURIComponent(it.code || '')}" class="opp-cp-tag opp-cp-red">✕ 公司红灯否决${coVeto ? ' · ' + esc(coVeto) : ''}</a>`;
    } else if (a.failed_gates && a.failed_gates.length) {
      const g0 = a.failed_gates[0];
      coTag = `<a href="/company/${encodeURIComponent(it.code || '')}" class="opp-cp-tag opp-cp-yellow">未过门槛 · ${G_LBL[g0] || g0} · 研究状态 ${esc(actionRating)}${scoreBandNote}</a>`;
    } else if (co && co.ready) {
      const tkey = coCmp ? coCmp.tier_key : (co.veto ? 'red' : (co.warning_gates && co.warning_gates.length ? 'yellow' : 'green'));
      const scoreTxt = coCmp ? ` · ${coCmp.composite}分` : '';
      const rating = coCmp && coCmp.rating;
      const m = {
        red: { cls: 'opp-cp-red', txt: co.veto ? `✕ 红灯否决 ${scoreTxt}` : `✕ ${rating || '不纳入'} ${scoreTxt}` },
        yellow: { cls: 'opp-cp-yellow', txt: rating ? `${rating} ${scoreTxt}` : `公司档案：${co.warning_gates.length} 项黄灯` },
        green: { cls: 'opp-cp-green', txt: rating ? `✓ ${rating} ${scoreTxt}` : '✓ 公司档案：核心条件通过' },
      }[tkey] || { cls: 'opp-cp-green', txt: '✓ 公司档案：通过' };
      coTag = `<a href="/company/${encodeURIComponent(it.code || '')}" class="opp-cp-tag ${m.cls}">${m.txt}</a>`;
    } else {
      coTag = `<a href="/company/${encodeURIComponent(it.code || '')}" class="opp-cp-tag opp-cp-dim">○ 公司档案：未生成 · 点此生成</a>`;
    }
    const reviewText = a.verdict === 'APPROVE'
      ? `机会质量通过 · ${esc(aReason || '买点与仓位单独判断')}`
      : (aReason ? `统一评估未过：${esc(aReason)}` : '统一评估等待数据');
    const revBadge = (a.verdict && a.verdict !== 'APPROVE' && a.failed_gates && a.failed_gates.length)
      ? `<span class="opp-rev ${co && co.veto ? 'opp-rev-red' : 'opp-rev-yellow'}">${co && co.veto ? '公司红灯否决 · 研究观察' : '未过门槛 · 研究观察'}</span>`
      : '';
    return `<div class="opp-row ${isTrig?'opp-triggered':''}" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(it.code || '')}">
      <div class="opp-top">
        <span class="opp-name">${esc(it.name)}</span>
        <span class="opp-code">${esc(it.code)}</span>
        ${modelValueVerified && Number.isFinite(Number(ai.pred_ret_20d))
          ? `<span class="opp-ai ai-neutral" title="已验证模型值">模型值 ${Number(ai.pred_ret_20d).toFixed(2)}</span>`
          : '<span class="opp-ai ai-neutral" title="模型尚未通过收益验证">未验证模型值已隐藏</span>'}
        ${it.streak_days>=2 ? `<span class="opp-streak" title="连续${it.streak_days}天出现在研究观察榜（每日快照口径）">连续${it.streak_days}天</span>` : ''}
        <button class="opp-exclude" type="button" title="加入个人排除名单，不再显示" data-action="exclude-stock" data-code="${escAttr(it.code)}">✕</button>
      </div>
      <div class="opp-px">
        <span class="opp-price ${cc(chg)}">${fp(it.price)}</span>
        <span class="opp-chg ${cc(chg)}">${fpc(chg)}</span>
        <span class="opp-lv ${lvCls}" style="margin-left:auto">${esc(it.level)}</span>
      </div>
      <div class="opp-signal">
        <span>盘中买点条件 <b>${esc(timingConditionSummary(it))}</b> · ${esc(actLbl)}</span>
        <span style="margin-left:auto">窗口 ${esc(it.buy_window.label||'—')}</span>
      </div>
      <div style="font-size:12px;color:var(--text-secondary);margin-top:5px;line-height:1.65">${qualityFacts.join(' · ')}</div>
      ${qualitySource ? `<div style="font-size:12px;color:var(--text-tertiary);margin-top:2px">${qualitySource}</div>` : ''}
      <div style="font-size:12px;color:var(--text-tertiary);margin-top:3px">${revBadge}${reviewText}</div>
      ${compositeAsOfHtml(aComp.as_of_audit)}
      ${it.price_fit && it.price_fit.fit !== 'no_price'
        ? `<div class="opp-pricefit ${it.price_fit.fit==='good'?'pf-good':it.price_fit.fit==='ok'?'pf-ok':'pf-poor'}" style="font-size:12px;margin-top:2px;color:var(--text-tertiary)">${esc(it.price_fit.label)}${it.price_fit.lot_cost?` · 一手≈¥${fn(it.price_fit.lot_cost,0)}`:''}</div>`
        : ''}
      <div class="opp-levels">
        <span class="lv lv-sl">${esc(riskLevelMeta(activeLevel))}</span>
        ${researchLevels.map(level => `<span class="lv lv-tp">${esc(riskLevelMeta(level))}</span>`).join('')}
      </div>
      <div class="opp-alert-line">
        <span class="dot ${dotCls}"></span>
        <span>${esc(dotLbl)}</span>
        ${cd ? `<span class="opp-countdown c-hot" style="margin-left:auto">${cd}</span>`
             : `<span style="margin-left:auto;font-size:12px;color:var(--text-tertiary)">${winGateNote}</span>`}
      </div>
      <div class="opp-cp-line">${coTag}</div>
    </div>`;
  }).join('');
  const exitItems = data.position_alerts || [];
  const exitNote = exitItems.length
    ? `<div style="padding:8px 16px;border-bottom:1px solid var(--border-subtle);font-size:12px;line-height:1.8;background:rgba(255,68,88,.04)">
        <b style="color:var(--bear)">持仓卖出风险监控 · ${exitItems.length} 只</b>：${exitItems.map(it => {
          const a = it.alert || {}; const active = a.status === 'triggered';
          return `<span style="margin-left:8px;white-space:nowrap;color:${active?'var(--bear)':'var(--text-secondary)'}">${active?'● ':''}${esc(it.name)} ${fp(it.price)} · ${esc(a.reason || '监控中')}</span>`;
        }).join('')}
      </div>` : '';
  const researchFold = (label, foldItems, color='var(--text-tertiary)') => foldItems.length
    ? `<details style="padding:8px 16px;border-top:1px solid var(--border-subtle);font-size:12px;color:${color};line-height:1.7">
        <summary style="cursor:pointer;font-weight:700">${label} · ${foldItems.length} 只</summary>
        <div style="margin-top:6px">${foldItems.map(r => {
          const reason = r.assessment?.reason || r.reason || r.message || '服务端未提供具体原因';
          return `<a href="/stock?code=${encodeURIComponent(r.code || '')}" style="display:block;color:inherit;text-decoration:none;padding:3px 0"><b>${esc(r.name || r.code || '名称不可用')}</b>${r.code ? ` <span>${esc(r.code)}</span>` : ''} · ${esc(reason)}</a>`;
        }).join('')}</div>
      </details>`
    : '';
  const researchNotes = [
    researchFold('待核验（不进入机会榜）', pending),
    researchFold('观察（未达可操作标准）', observing, 'var(--gold)'),
    researchFold('硬否决（不进入机会榜）', hardBlocked, 'var(--bear)'),
  ].join('');
  const exclItems = (exclusions && exclusions.items) || [];
  const exclNote = exclusionsError
    ? `<div style="padding:8px 16px;border-top:1px solid var(--border-subtle);font-size:12px;color:var(--gold)" role="status">${esc(exclusionsError)}；当前未应用个人排除过滤。</div>`
    : exclItems.length
    ? `<div style="padding:8px 16px;border-top:1px solid var(--border-subtle);font-size:12px;color:var(--text-tertiary);line-height:1.8">
        个人排除 ${exclItems.length} 只：${exclItems.map(e =>
           `<span style="display:inline-flex;align-items:center;gap:3px;margin:0 8px 0 0;white-space:nowrap">${esc(e.name)}<button type="button" data-action="unexclude-stock" data-code="${escAttr(e.code)}" title="移除排除" style="color:var(--text-tertiary);font-size:12px;padding:4px 7px">✕</button></span>`).join('')}
      </div>`
    : '';
  const empty = '<div class="empty-state">当前没有达到可操作标准的机会，保持现金等待；不拿被否决股票凑推荐</div>';
  body.innerHTML = head + exitNote + (rows || empty) + researchNotes + exclNote;
}
// 个人排除名单操作
window._excludeStock = async function (btn, code) {
  const row = btn && btn.closest ? btn.closest('.opp-row') : null;
  const name = row ? row.querySelector('.opp-name').textContent : code;
  try {
    const r = await apiWrite('/api/exclusions', {code, name});
    if (!r._httpOk) { toast(r.error || '排除失败', 'error'); return; }
    toast(`已将 ${name} 加入个人排除名单`, 'success');
    fetchOpportunities();
  } catch (e) { toast('网络错误，请重试', 'error'); }
};
window._unexcludeStock = async function (code) {
  try {
    const r = await apiWrite('/api/exclusions/' + encodeURIComponent(code), null, 'DELETE');
    if (!r._httpOk) { toast(r.error || '移除失败', 'error'); return; }
    toast(r.removed ? '已从排除名单移除' : '该股票不在排除名单', 'success');
    fetchOpportunities();
  } catch (e) { toast('网络错误，请重试', 'error'); }
};
function handleOppTrigger(data, generation) {
  if (!opportunityRequestIsCurrent(generation)) return;
  window.dispatchEvent(new CustomEvent('regis-opportunity-snapshot', {detail:data}));
}
let _alertCtx = null, _alertTimer = null;
function _ensureAlertCtx() {
  try {
    if (!_alertCtx) _alertCtx = window.__regisAlertAudioContext || new (window.AudioContext || window.webkitAudioContext)();
    window.__regisAlertAudioContext = _alertCtx;
    if (_alertCtx.state === 'suspended') _alertCtx.resume();
  } catch (e) {
    reportClientIssue('alert-audio-context', e);
    const button = $('#alertPrefsButton');
    if (button) {
      button.textContent = '声音错误 · 点击重试';
      button.title = e && e.message ? e.message : '浏览器无法启用声音提醒';
    }
    return null;
  }
  return _alertCtx;
}
function playAlertSound() {
  stopAlertSound();
  try {
    if (localStorage.getItem('regis.alerts_enabled') !== '1') return;
  } catch (error) {
    reportClientIssue('alert-preference', error);
    return;
  }
  const ctx = _ensureAlertCtx();
  if (!ctx) return;
  function beep(freq) {
    try {
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = 'sine'; osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.28, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.32);
      osc.connect(gain).connect(ctx.destination);
      osc.start(); osc.stop(ctx.currentTime + 0.35);
    } catch (e) { reportClientIssue('alert-audio', e); }
  }
  let hi = true;
  _alertTimer = setInterval(function () { beep(hi ? 880 : 620); hi = !hi; }, 480);
}
function stopAlertSound() {
  if (_alertTimer) { clearInterval(_alertTimer); _alertTimer = null; }
}
const SIGNAL_LABELS = {
  volume_breakout: '放量突破', macd_golden_cross: 'MACD金叉',
  rsi_rebound: 'RSI反弹', vwap_support: '均价支撑',
  northbound: '北向资金', big_order_positive: '主力净流入',
};
function renderSignalPanel(signals) {
  const el = $('#alertSignals');
  if (!el) return;
  const sig = signals || {};
  const keys = Object.keys(sig);
  if (!keys.length) { el.innerHTML = ''; return; }
  const hitCount = keys.filter(k => sig[k] && sig[k].state === 'hit').length;
  const rows = keys.map(k => {
    const s = sig[k] || {};
    let badge;
    if (s.state === 'hit') badge = '<span class="sig-badge hit">✓ 命中</span>';
    else if (s.state === 'miss') badge = '<span class="sig-badge miss">✕ 未中</span>';
    else badge = '<span class="sig-badge na">— 未参与</span>';
    return `<div class="sig-row">
      <span class="sig-label">${SIGNAL_LABELS[k] || k}</span>
      ${badge}
      <span class="sig-score">${(s.score || 0).toFixed(1)}</span>
      <span class="sig-detail" title="${escAttr(s.detail || '')}">${esc(s.detail || '')}</span>
    </div>`;
  }).join('');
  el.innerHTML = `<div class="sig-panel">
    <div class="sig-title">为什么进入人工复核 · 六维条件明细（命中 ${hitCount}/${keys.length}）</div>
    ${rows}
  </div>`;
}
window.renderSignalPanel = renderSignalPanel;
function showAlertOverlay(item) {
  if (state.alertItem) return false;
  const countdownSeconds = Number(item?.alert?.countdown_seconds);
  if (!Number.isFinite(countdownSeconds) || countdownSeconds <= 0) return false;
  state.alertItem = item;
  const isBuy = item.alert.action === 'buy', isProductionStrategy = isBuy && isProductionAlert(item);
  const buyLabel = isProductionStrategy ? '策略买入提醒' : '研究观察买点', lv = item.levels || {}, planAction = item.trade_plan?.action || {}, allocation = item.allocation || {}, evidenceIdentity = planAction.evidence_identity || {}, sellOrigin = item.alert?.provenance?.origin || '', sellLabel = evidenceIdentity.method === 'manual_exit_intent_v1' || sellOrigin === 'manual_exit_intent' ? '你的退出计划' : sellOrigin === 'production_strategy' ? '策略退出提醒' : evidenceIdentity.risk_timing_role === 'loss_containment_review_not_best_exit_point' || sellOrigin === 'position_risk_review' ? '风险线复核 · 非最佳卖点' : '人工卖出复核', frozenRiskLine = allocation.risk_line || (Number.isFinite(Number(evidenceIdentity.effective_stop)) ? {kind:'hard_stop', value:Number(evidenceIdentity.effective_stop)} : {}), planShares = Number(planAction.shares), planLimit = Number(planAction.limit_price), planCost = Number(planAction.estimated_cost), planCash = Number(allocation.estimated_buy_cash_yuan), planWeight = Number(allocation.target_weight_pct), planLiquidity = Number(allocation.liquidity_capacity_yuan), planRisk = Number(frozenRiskLine.value);
  const planFacts = [Number.isInteger(planShares) && planShares > 0 ? `${isBuy ? '建议买入' : '建议卖出'} ${planShares} 股` : '', Number.isFinite(planLimit) && planLimit > 0 ? `限价参考 ¥${fn(planLimit, 2)}` : '', !isBuy && Number.isFinite(Number(item.position?.price)) ? `持仓成本 ¥${fn(Number(item.position.price), 3)}` : '', planAction.playbook_id ? `战法 ${planAction.playbook_id}` : '', planAction.strategy_id ? `策略 ${planAction.strategy_id}${planAction.strategy_version ? ` · ${planAction.strategy_version}` : ''}` : '', isBuy && Number.isFinite(planCash) && planCash > 0 ? `预计现金占用 ¥${fn(planCash, 2)}` : '', Number.isFinite(planCost) && planCost >= 0 ? `预计成本 ¥${fn(planCost, 2)}` : '', isBuy && Number.isFinite(planWeight) ? `目标仓位 ${fn(planWeight, 2)}%` : '', isBuy && Number.isFinite(planLiquidity) && planLiquidity > 0 ? `流动性上限 ¥${fn(planLiquidity, 0)}` : '', Number.isFinite(planRisk) && planRisk > 0 ? `${frozenRiskLine.kind === 'hard_stop' ? '硬风险线' : '风险线'} ¥${fn(planRisk, 2)}` : '', planAction.valid_until ? `有效至 ${String(planAction.valid_until).replace('T', ' ').slice(0, 19)}` : '', planAction.cancel_condition ? `失效条件：${planAction.cancel_condition}` : ''].filter(Boolean).join(' · ');
  const riskLine = Number.isFinite(planRisk) && planRisk > 0 ? frozenRiskLine : effectiveRiskLevel(lv.risk_levels, lv.effective_risk_level);
  $('#alertKicker').textContent = isBuy ? `${buyLabel} · 请人工复核` : `${sellLabel} · 请人工复核`;
  $('#alertName').textContent = item.name;
  $('#alertCode').textContent = `${item.code} · ${isBuy ? (isProductionStrategy ? '策略买入复核' : '研究观察人工复核') : sellLabel} · 参考价 ¥${fp(item.price)}`;
  $('#alertReason').textContent = item.alert.reason || '';
  if (isBuy) renderSignalPanel(item.signals);
  else $('#alertSignals').innerHTML = '';
  $('#alertLevels').textContent = riskLevelMeta(riskLine);
  $('#alertHint').textContent = isBuy && !isProductionStrategy ? '研究观察不是已验证策略信号；本站只记录你已完成的人工成交' : !isBuy && sellLabel === '你的退出计划' ? '这是你预先记录的退出意图，不是系统预测的最佳卖点' : !isBuy && sellLabel.indexOf('风险线复核') === 0 ? '风险线只控制损失，不预测最高价；该提醒不是最佳卖点' : '提醒不等于下单；本站只记录你已完成的人工成交';
  $('#alertSub').textContent = (planFacts ? planFacts + '；' : '') + '本站不连接券商；请先自行完成成交，再记录实际价格与数量';
  const btn = $('#alertConfirmBtn');
  btn.textContent = isBuy ? '人工复核并记录买入' : '记录已完成的卖出';
  state.alertRemaining = Math.floor(countdownSeconds);
  $('#alertCountdown').textContent = _fmtCD(state.alertRemaining);
  $('#alertCountdown').className = 'buy-alert-cd';
  $('#buyAlertOverlay').hidden = false;
  $('#buyAlertOverlay').setAttribute('aria-hidden', 'false');
  if (state.alertDialogCleanup) state.alertDialogCleanup();
  state.alertDialogCleanup = trapDialog($('#buyAlertOverlay'), hideAlertOverlay);
  playAlertSound();
  if (state.alertTick) clearInterval(state.alertTick);
  state.alertTick = setInterval(tickAlertCountdown, 1000);
  return true;
}
window.showAlertOverlay = showAlertOverlay;
window.hideAlertOverlay = hideAlertOverlay;
function tickAlertCountdown() {
  if (!state.alertItem) return;
  const el = $('#alertCountdown');
  state.alertRemaining = Math.max(0, state.alertRemaining - 1);
  const isBuy = state.alertItem.alert.action === 'buy';
  if (state.alertRemaining <= 0) {
    el.textContent = '时间到';
    el.className = 'buy-alert-cd done';
    $('#alertSub').textContent = '提醒窗口已结束，请按最新行情重新复核';
    $('#alertHint').textContent = isBuy ? '窗口已过或已收盘时，服务端会拒绝买入记录' : '价格可能已变动；本站不会代你下单';
    if (state.alertTick) { clearInterval(state.alertTick); state.alertTick = null; }
    return;
  }
  el.textContent = _fmtCD(state.alertRemaining);
  el.className = 'buy-alert-cd' + (state.alertRemaining <= 10 ? ' critical' : '');
}
function hideAlertOverlay() {
  stopAlertSound();
  if (state.alertTick) { clearInterval(state.alertTick); state.alertTick = null; }
  state.alertItem = null;
  const ov = $('#buyAlertOverlay');
  if (state.alertDialogCleanup) { state.alertDialogCleanup(); state.alertDialogCleanup = null; }
  if (ov) { ov.hidden = true; ov.setAttribute('aria-hidden', 'true'); }
}
async function dismissAlert(item) {
  const id = Number(item.alert?.id);
  if (!Number.isInteger(id)) { toast('提醒缺少精确ID，未忽略', 'error'); return; }
  const result = await apiWrite('/api/timing/dismiss', {alert_id:id,reason:'user_dismissed_without_execution'}, 'POST', {idempotencyKey:`dismiss-alert-${id}`});
  if (result._httpOk) { hideAlertOverlay(); toast('已忽略并留存决策理由', 'info'); }
  else toast(result.error || '服务端未确认忽略状态', 'error');
}
function openAlertConfirm(item) {
  const isBuy = (item.alert.action || 'buy') === 'buy';
  const pos = item.position;
  const referencePrice = Number(item.price);
  const planShares = Number(item.trade_plan?.action?.shares);
  const suggestedBuyShares = isBuy && Number.isInteger(planShares) && planShares > 0 && planShares % 100 === 0 ? planShares : null;
  const suggestedSellShares = !isBuy && Number.isInteger(planShares) && planShares > 0
    && planShares <= Number(pos?.shares || 0) && planShares % 100 === 0
    ? planShares : Number(pos?.shares || 0);
  if (!isBuy && (!pos || !pos.id)) { toast('暂无对应持仓记录', 'error'); return; }
  const mask = document.createElement('div');
  mask.className = 'timing-modal-mask';
  mask.setAttribute('role', 'dialog');
  mask.setAttribute('aria-modal', 'true');
  mask.setAttribute('aria-labelledby', 'oppDialogTitle');
  if (isBuy) {
    mask.innerHTML = `<div class="timing-modal">
        <h3 id="oppDialogTitle">记录已完成买入</h3><div style="color:var(--text-tertiary);font-size:var(--text-xs);margin-bottom:8px">本站不连接券商；请先自行完成买入，再记录 ${esc(item.name)} 的实际成交价格与数量</div>
        <div class="modal-row"><span>标的</span><span>${esc(item.name)} (${item.code})</span></div>
        <div class="modal-row"><span>提醒参考价</span><span>¥${fp(referencePrice)}</span></div><div class="modal-row"><span>盘中买点条件</span><span>${esc(timingConditionSummary(item))} · ${esc(item.level)}</span></div>
        <label for="oppBuyPriceInput" style="display:block;margin-top:12px;font-size:var(--text-xs);color:var(--text-tertiary)">实际成交价格（¥）</label><input type="number" id="oppBuyPriceInput" min="0.01" step="0.01" placeholder="输入实际买入成交价，不代填参考价" required>
        <label for="oppSharesInput" style="display:block;font-size:var(--text-xs);color:var(--text-tertiary)">实际买入数量（100 股整数倍；计划 ${suggestedBuyShares || '—'} 股不是成交）</label><input type="number" id="oppSharesInput" min="100" step="100" placeholder="买入数量（100股整数倍）">
        <label for="oppBuyFeeInput" style="display:block;font-size:var(--text-xs);color:var(--text-tertiary)">券商实际买入费用（可选，¥）</label><input type="number" id="oppBuyFeeInput" min="0" step="0.01" placeholder="留空则按模型估算">
        <div style="font-size:var(--text-xs);color:var(--text-tertiary);text-align:right">实际成交金额 ¥<span id="oppAmount">0</span></div>
        <div class="modal-actions"><button type="button" class="btn btn-plain" id="oppCancel">取消</button><button type="button" class="btn btn-primary" id="oppConfirm">记录买入</button></div>
      </div>`;
  } else {
    mask.innerHTML = `<div class="timing-modal">
        <h3 id="oppDialogTitle">记录已完成卖出</h3><div style="color:var(--text-tertiary);font-size:var(--text-xs);margin-bottom:8px">本站不连接券商；请先自行完成卖出，再记录实际成交价格与数量。${esc(item.alert.reason||'')}</div>
        <div class="modal-row"><span>标的</span><span>${esc(item.name)} (${item.code})</span></div>
        <div class="modal-row"><span>持仓</span><span>${pos.shares||0} 股 @ ¥${fp(pos.price)}</span></div><div class="modal-row"><span>提醒参考价</span><span>¥${fp(referencePrice)}</span></div>
        <label for="oppSellPriceInput" style="display:block;margin-top:12px;font-size:var(--text-xs);color:var(--text-tertiary)">实际成交价格（¥）</label><input type="number" id="oppSellPriceInput" min="0.01" step="0.01" placeholder="输入实际卖出成交价，不代填参考价" required>
        <label for="oppSellSharesInput" style="display:block;font-size:var(--text-xs);color:var(--text-tertiary)">实际卖出数量（100 股整数倍，不得超过持仓）</label><input type="number" id="oppSellSharesInput" min="100" step="100" max="${Number(pos.shares || 0)}" aria-describedby="oppSellRemainingText">
        <label for="oppSellFeeInput" style="display:block;font-size:var(--text-xs);color:var(--text-tertiary)">券商实际卖出费用（可选，¥）</label><input type="number" id="oppSellFeeInput" min="0" step="0.01" placeholder="留空则按模型估算">
        <div id="oppSellRemainingText" style="font-size:var(--text-xs);color:var(--text-tertiary);text-align:right">提醒计划 ${suggestedSellShares} 股（不是成交） · 卖后剩余 <span id="oppSellRemaining">—</span> 股</div>
        <div class="modal-row"><span>按实际成交价估算毛盈亏</span><span id="oppSellPnl">—</span></div>
        <div class="modal-actions"><button type="button" class="btn btn-plain" id="oppCancel">取消</button><button type="button" class="btn btn-primary" id="oppConfirm">记录卖出</button></div>
      </div>`;
  }
  document.body.appendChild(mask);
  const buyAttempt = isBuy ? {key:createIdempotencyKey('confirm-buy'), ticketId:null, expiresAt:0, shares:null, price:null, fee:null, feeProvided:false} : null;
  const sellAttempt = isBuy ? null : {key:null, fingerprint:null};
  let cleanup = null;
  const close = () => {
    if (cleanup) { cleanup(); cleanup = null; }
    mask.remove();
  };
  cleanup = trapDialog(mask, close);
  if (isBuy) {
    const sharesInput = $('#oppSharesInput'), priceInput = $('#oppBuyPriceInput');
    const amountEl = $('#oppAmount');
    const updateAmount = () => {
      const shares = parseInt(sharesInput.value || '0', 10);
      const actualPrice = parseFloat(priceInput.value || '0');
      amountEl.textContent = Number.isFinite(actualPrice) && actualPrice > 0 ? fn(actualPrice * shares, 2) : '—';
    };
    sharesInput.addEventListener('input', updateAmount);
    priceInput.addEventListener('input', updateAmount);
    updateAmount();
  } else {
    const sharesInput = $('#oppSellSharesInput'), priceInput = $('#oppSellPriceInput');
    const remainingEl = $('#oppSellRemaining');
    const pnlEl = $('#oppSellPnl');
    const updateSellPreview = () => {
      const quantity = timingSellQuantity(sharesInput.value, pos.shares);
      remainingEl.textContent = quantity.remaining == null ? '—' : String(quantity.remaining);
      const actualPrice = parseFloat(priceInput.value || '0');
      const pnl = (actualPrice - Number(pos.price || 0)) * (quantity.valid ? quantity.shares : 0);
      pnlEl.textContent = Number.isFinite(actualPrice) && actualPrice > 0 && quantity.valid ? fn(pnl, 2) : '—';
      pnlEl.className = Number.isFinite(actualPrice) && actualPrice > 0 && quantity.valid ? cc(pnl) : '';
    };
    sharesInput.addEventListener('input', updateSellPreview);
    priceInput.addEventListener('input', updateSellPreview);
    updateSellPreview();
  }
  $('#oppCancel').addEventListener('click', close);
  mask.addEventListener('click', (e) => { if (e.target === mask) close(); });
  $('#oppConfirm').addEventListener('click', async () => {
    const confirmButton = $('#oppConfirm');
    if (!confirmButton || confirmButton.disabled) return;
    let url, payload, idempotencyKey = null;
    if (isBuy) {
      const sharesInput = $('#oppSharesInput'), priceInput = $('#oppBuyPriceInput'), feeRaw = (($('#oppBuyFeeInput') || {}).value || '').trim();
      const shares = Number(sharesInput ? sharesInput.value : ''), actualPrice = Number(priceInput ? priceInput.value : '');
      const buyFee = feeRaw === '' ? null : Number(feeRaw);
      if (!Number.isFinite(actualPrice) || actualPrice <= 0) { toast('请填写实际买入成交价格', 'error'); return; }
      if (!Number.isInteger(shares) || shares <= 0 || shares % 100 !== 0) { toast('数量需为 100 股整数倍', 'error'); return; }
      if (buyFee !== null && (!Number.isFinite(buyFee) || buyFee < 0)) { toast('实际买入费用需为不小于 0 的金额', 'error'); return; }
      if ((buyAttempt.shares != null && buyAttempt.shares !== shares) || (buyAttempt.price != null && buyAttempt.price !== actualPrice) || (buyAttempt.feeProvided && buyAttempt.fee !== buyFee)) {
        toast('重试时不能修改成交价格、数量或费用；请关闭后重新记录', 'error'); return; }
      url = '/api/timing/confirm';
      idempotencyKey = buyAttempt.key;
      confirmButton.disabled = true;
      confirmButton.setAttribute('aria-busy', 'true');
      try {
        if (buyAttempt.ticketId && buyAttempt.expiresAt <= Date.now()) { toast('确认票据已过期，请关闭后重新确认', 'error'); return; }
        if (!buyAttempt.ticketId) {
          const ticketRequest = {code:item.code || ''}; const alertId = Number(item.alert?.id);
          if (Number.isInteger(alertId) && alertId > 0) ticketRequest.alert_id = alertId;
          const prepared = await apiWrite('/api/timing/ticket', ticketRequest);
          if (!prepared._httpOk) { toast(prepared.error || '无法签发确认票据；请稍后重试', 'error'); return; }
          const ticket = prepared.confirmation_ticket;
          const expiresAt = Date.parse(ticket?.expires_at || '');
          if (!ticket?.id || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
            toast('服务端未签发有效确认票据；请稍后重试', 'error');
            return;
          }
          buyAttempt.ticketId = ticket.id;
          buyAttempt.expiresAt = expiresAt;
        }
        buyAttempt.shares = shares; buyAttempt.price = actualPrice;
        buyAttempt.fee = buyFee; buyAttempt.feeProvided = true;
        payload = buildBuyConfirmationPayload(buyAttempt.ticketId, actualPrice, shares);
        if (buyFee !== null) payload.buy_fee_yuan = buyFee;
      } finally {
        if (!payload && confirmButton.isConnected) {
          confirmButton.disabled = false;
          confirmButton.removeAttribute('aria-busy');
        }
      }
    } else {
      const sharesInput = $('#oppSellSharesInput'), priceInput = $('#oppSellPriceInput'), feeRaw = (($('#oppSellFeeInput') || {}).value || '').trim();
      const quantity = timingSellQuantity(sharesInput ? sharesInput.value : '0', pos.shares), actualPrice = parseFloat(priceInput ? priceInput.value : '0');
      const sellFee = feeRaw === '' ? null : Number(feeRaw);
      if (!Number.isFinite(actualPrice) || actualPrice <= 0) { toast('请填写实际卖出成交价格', 'error'); return; }
      if (!quantity.valid) {
        toast(quantity.shares > quantity.held ? '卖出数量不能超过当前持仓' : '卖出数量须为 100 股整数倍，或一次性包含全部零股', 'error');
        return;
      }
      if (sellFee !== null && (!Number.isFinite(sellFee) || sellFee < 0)) { toast('实际卖出费用需为不小于 0 的金额', 'error'); return; }
      const shares = quantity.shares, sellPayload = {id:pos.id, price:actualPrice, shares};
      if (sellFee !== null) sellPayload.sell_fee_yuan = sellFee;
      const fingerprint = JSON.stringify(sellPayload);
      if (sellAttempt.fingerprint !== fingerprint) {
        sellAttempt.fingerprint = fingerprint;
        sellAttempt.key = createIdempotencyKey('sell-position');
      }
      url = '/api/timing/sell';
      payload = sellPayload; const alertId = Number(item.alert?.id); if (Number.isInteger(alertId) && alertId > 0) payload.alert_id = alertId;
      idempotencyKey = sellAttempt.key;
      confirmButton.disabled = true;
      confirmButton.setAttribute('aria-busy', 'true');
    }
    try {
      const data = await apiWrite(url, payload, 'POST', {idempotencyKey});
      if (!data._httpOk) { toast(data.error || '操作失败', 'error'); return; }
      if (isBuy && !data.position?.id) {
        toast('服务端未返回已记录的成交，请刷新账本后复核', 'error');
        return;
      }
      close();
      hideAlertOverlay();
      toast(data.message || '已记录人工成交', 'success');
      fetchOpportunities();
    } catch (e) { toast('网络错误', 'error'); }
    finally {
      if (confirmButton.isConnected) {
        confirmButton.disabled = false;
        confirmButton.removeAttribute('aria-busy');
      }
    }
  });
}
// ── Fund Flow Widget ──
window._switchFlowDir = function(dir, btn) {
  state.flowDir = dir;
  $$('#flowCard .tab').forEach(t => {
    const active = t === btn;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', String(active));
    t.tabIndex = active ? 0 : -1;
  });
  fetchFlowWidget();
};
async function fetchFlowWidget() {
  const requestGeneration = state.navSequence;
  const dir = state.flowDir || 'in';
  const sortDir = dir === 'in' ? 'desc' : 'asc';
  const data = await api(`/api/fund-flow/ranking?sort=net&dir=${sortDir}&limit=8`);
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  const body = $('#flowWidgetBody');
  if (!body) return;
  const items = data?.items || [];
  if (!items.length) { body.innerHTML = `<div class="empty-state" role="status">${esc(data ? '暂无资金流数据' : apiError(`/api/fund-flow/ranking?sort=net&dir=${sortDir}&limit=8`, '资金流暂不可用'))}</div>`; return; }
  body.innerHTML = `<table class="data-table" style="font-size:12px">
    <thead><tr><th>#</th><th>股票</th><th>净额</th><th>换手</th><th>涨跌</th></tr></thead>
    <tbody>${items.map((r,i) => `
      <tr>
        <td style="color:var(--text-tertiary)">${i+1}</td>
        <td style="font-weight:600">${esc(r.name)}</td>
        <td class="num ${cc(r.net_yi)}">${fmtYi(r.net_yi)}</td>
        <td class="num">${fn(r.turnover,1)}%</td>
        <td class="num ${cc(r.pct_chg)}">${fpc(r.pct_chg)}</td>
      </tr>
    `).join('')}</tbody>
  </table>`;
}
async function fetchEvents() {
  const requestGeneration = state.navSequence;
  const data = await api('/api/radar');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  const body = $('#eventsBody');
  const count = $('#eventCount');
  if (!body) return;
  const events = data?.events || [];
  if (count) count.textContent = events.length;
  if (!events.length) { body.innerHTML = `<div class="empty-state" role="status">${esc(data ? '暂无事件' : apiError('/api/radar', '市场事件暂不可用'))}</div>`; return; }
  body.innerHTML = '<div class="event-list">' + events.map(e => `
    <div class="event-item">
      <div class="event-title">${esc(e.title||'')}</div>
      <div class="event-meta">
        <span>${esc(e.source||'')}</span>
        <span>${esc(e.collected_at||'')}</span>
        ${e.impact?`<span class="event-impact" style="background:${e.impact==='偏多'?'rgba(246,70,93,0.1)':'rgba(46,189,133,0.1)'};color:${e.impact==='偏多'?'#F6465D':'#2EBD85'}">${e.impact}</span>`:''}
      </div>
    </div>
  `).join('') + '</div>';
}
async function fetchFactors() {
  const requestGeneration = state.navSequence;
  const data = await api('/api/factor-system/status');
  if (state.page !== 'dashboard' || state.navSequence !== requestGeneration) return;
  const body = $('#factorBody'), count = $('#factorCount');
  if (!body) return;
  if (!data) { if (count) count.textContent = '—'; body.innerHTML = `<div class="empty-state" role="status">${esc(apiError('/api/factor-system/status', '生产因子目录暂不可用'))}</div>`; return; }
  const catalog = data.production_catalog || {};
  const factorIds = Array.isArray(catalog.factor_ids) ? catalog.factor_ids : [];
  const deployableCount = Number(catalog.deployable_count || 0);
  if (count) count.textContent = catalog.healthy === false ? '异常' : deployableCount;
  if (catalog.healthy === false || catalog.error) { body.innerHTML = `<div class="empty-state" role="alert">生产因子目录复核失败；当前不允许因子进入评分或信号。<br><a href="/factor-system">打开因子证据页</a></div>`; return; }
  if (!catalog.consumer_ready || deployableCount === 0) {
    body.innerHTML = `<div class="empty-state" role="status">当前 0 个可消费 production 因子。<br>历史、失效与被否决档案不参与评分或买卖信号。<br><a href="/factor-system">查看研究证据与未投产原因</a></div>`;
    return;
  }
  body.innerHTML = '<div class="factor-rows">' + factorIds.map(factorId => `<div class="factor-row">
    <span class="factor-label" title="${escAttr(factorId)}">${esc(factorId)}</span>
    <span class="factor-val c-bull">PRODUCTION</span>
  </div>`).join('') + '</div>';
}
// ═══════════════════════════════════════════════
// SIGNALS PAGE
const SIG_LABEL = {
  volume_breakout: '放量', macd_golden_cross: 'MACD',
  rsi_rebound: 'RSI', vwap_support: '均价',
  northbound: '北向', big_order_positive: '主力',
};
function sigChip(k, s) {
  const st = s && s.state;
  const lbl = SIG_LABEL[k] || k;
  let mark, color, bg;
  if (st === 'hit') { mark = '✓'; color = '#22c55e'; bg = 'rgba(34,197,94,.12)'; }
  else if (st === 'miss') { mark = '✕'; color = '#f87171'; bg = 'rgba(248,113,113,.10)'; }
  else { mark = '—'; color = 'var(--text-tertiary)'; bg = 'rgba(148,163,184,.08)'; }
  return `<span style="display:inline-block;padding:2px 8px;margin:2px 4px 2px 0;border-radius:999px;font-size:12px;background:${bg};color:${color};font-weight:600">${lbl} ${mark}</span>`;
}
function scoreColor(s) { return s >= 70 ? '#ef4444' : s >= 40 ? '#f59e0b' : '#94a3b8'; }
function sellReviewPlan(position) {
  const plan = position?.trade_plan, action = plan?.action;
  const shares = Number(action?.shares);
  return plan && ['EXIT_REVIEW', 'REDUCE_REVIEW'].includes(plan.state)
    && action?.side === 'SELL' && Number.isInteger(shares) && shares > 0
    ? {plan, action, shares} : null;
}
let _sigTimer = null;
async function renderSignalsPage() {
  const renderGeneration = state.navSequence;
  const container = $('[data-page="signals"]');
  if (_sigTimer) { clearInterval(_sigTimer); _sigTimer = null; }
  container.innerHTML = `
    <div class="summary-strip" style="margin-bottom:16px">
      <div class="sum-item"><span class="sum-val" id="sigOppCount">—</span><span class="sum-lbl">研究观察项</span></div>
      <div class="sum-item"><span class="sum-val c-bull" id="sigAlertCount">—</span><span class="sum-lbl">需复核动作</span></div>
      <div class="sum-item"><span class="sum-val" id="sigPosCount">—</span><span class="sum-lbl">持仓</span></div>
      <div class="sum-item"><span class="sum-val" id="sigRealized">—</span><span class="sum-lbl">已实现盈亏</span></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <span class="card-title">当前策略与资金动作 <span class="c-dim" style="font-weight:400;font-size:12px">买入策略与持仓卖出风控分开更新</span></span>
        <span class="card-badge" id="allocBadge" style="display:none"></span>
      </div>
      <div class="card-body" id="allocBody" style="font-size:13px;color:var(--text-tertiary)">加载中…</div>
    </div>
    <h3 style="font-size:15px;font-weight:700;margin:20px 0 10px">买卖提醒 · 当前状态</h3>
    <div class="card"><div class="card-body no-pad" id="sigAlerts"></div></div>
    <h3 style="font-size:15px;font-weight:700;margin:20px 0 10px">我的持仓</h3>
    <div class="card"><div class="card-body no-pad" id="sigPositions"></div></div>
    <div style="display:flex;align-items:center;justify-content:space-between;margin:0 0 10px">
      <h3 style="font-size:15px;font-weight:700">研究观察池 · 统一专业综合评分 <span class="c-dim" style="font-weight:400">（全站唯一100分制；盘中条件只决定入场时机）</span></h3>
      <span class="c-dim" id="sigUpdated" style="font-size:12px"></span>
    </div>
    <div id="sigOpps"></div>
    <div class="card" style="margin-top:16px">
      <div class="card-header">
        <span class="card-title">因子辅助排序 <span class="c-dim" style="font-weight:400;font-size:12px">仅作候选旁证，不生成或阻塞统一买卖提醒</span></span>
        <span class="card-badge" id="liveSigHealth">—</span>
      </div>
      <div class="card-body">
        <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px;font-size:var(--text-xs);color:var(--text-tertiary)">
          <span id="liveSigMarket">市场: —</span>
          <span id="liveSigSource">行情源: —</span>
          <span id="liveSigFactorAsOf">因子as_of: —</span>
          <span id="liveSigUpdated">更新: —</span><span id="monitorState">后台提醒: 核验中…</span>
        </div>
        <div class="table-wrap" style="max-height:300px"><table class="data-table">
          <thead><tr><th>动作</th><th>代码</th><th>名称</th><th>信号合成z</th><th>因子z</th><th>动量z</th><th>涨跌幅</th><th>因子as_of</th><th>理由</th></tr></thead>
          <tbody id="liveSigBody"><tr><td colspan="9" style="color:var(--text-tertiary);text-align:center">加载中…</td></tr></tbody>
        </table></div>
        <div style="margin-top:8px;font-size:12px;color:var(--text-tertiary);line-height:1.5" id="liveSigHonesty"></div>
      </div>
    </div>
    <h3 style="font-size:15px;font-weight:700;margin:20px 0 10px">平仓历史</h3>
    <div class="card"><div class="card-body no-pad" id="sigClosed"></div></div>
  `;
  setSpaPortfolioMonitorOwner('signals');
  async function refresh() {
    if (document.hidden || state.page !== 'signals') return;
    const [opp, rawReview, port, alloc, live, today, monitor] = await Promise.all([
      api('/api/opportunities'),
      api('/api/timing/review'),
      api('/api/timing/portfolio'),
      api('/api/timing/alloc'),
      api('/api/signals/live'),
      api('/api/signals/today'), api('/api/monitor/status'),
    ]);
    const rev = normalizeReviewData(rawReview);
    if (state.page !== 'signals' || state.navSequence !== renderGeneration) return;
    if (port) window.dispatchEvent(new CustomEvent('regis-portfolio-snapshot', { detail:port }));
    if (opp) window.dispatchEvent(new CustomEvent('regis-opportunity-snapshot', {detail:{...opp, allocation_plan:alloc || null}}));
    const monitorEl = $('#monitorState'); if (monitorEl) { monitorEl.textContent = !monitor ? '后台提醒: 状态不可用' : monitor.active_now ? `后台提醒: 交易中监控 · ${fn(monitor.refresh_interval_s,0)}秒` : monitor.ready_for_session ? `后台提醒: ${esc(monitor.market_session?.label || '待开盘')} · 已就绪` : '后台提醒: 监控异常'; monitorEl.style.color = monitor?.active_now ? 'var(--bull)' : monitor?.ready_for_session ? 'var(--gold)' : 'var(--bear)'; monitorEl.title = monitor ? `快照 ${monitor.snapshot_status || 'unknown'} · 年龄 ${monitor.snapshot_age_s == null ? '—' : fn(monitor.snapshot_age_s,1)+'秒'}` : '无法读取服务端监控状态'; }
    const strategyLab = today?.strategy_lab || {};
    if (live && live.ok) renderLiveSignals(live.data, monitor);
    if (!live || !live.ok) {
      const liveBody = $('#liveSigBody');
      if (liveBody) liveBody.innerHTML = `<tr><td colspan="9"><div class="empty-state" role="status">${esc(apiError('/api/signals/live', '实时因子信号暂不可用'))}</div></td></tr>`;
      const liveHealth = $('#liveSigHealth');
      if (liveHealth) liveHealth.textContent = '不可用';
    }
    const allocBody = $('#allocBody');
    const allocCap = $('#allocCapital');
    const allocBadge = $('#allocBadge');
    // 资金分配门禁
    if (allocBody) {
      if (!alloc || (alloc.error && !Array.isArray(alloc.items))) {
        allocBody.innerHTML = `<div class="empty-state" role="status">${esc(alloc?.error || apiError('/api/timing/alloc', '资金动作暂不可用'))}</div>`;
      } else {
        const c = alloc.capital || 0;
        if (allocCap) allocCap.textContent = fn(c, 0);
        const cap = alloc.capital || 0;
        const items = alloc.items || [];
        const manualPlan = alloc.manual_decision_support === true;
        if (allocBadge) {
          allocBadge.style.display = '';
          allocBadge.textContent = manualPlan
            ? '手工计划 · 不自动下单'
            : alloc.position_count + ' 只 · 占 ' + alloc.used_pct + '%';
          allocBadge.style.background = manualPlan
            ? 'rgba(245,158,11,.15)'
            : (alloc.used_pct > 95 ? 'rgba(220,38,38,.15)' : 'rgba(34,197,94,.15)');
          allocBadge.style.color = manualPlan ? '#f59e0b' : (alloc.used_pct > 95 ? '#ef4444' : '#22c55e');
        }
        const cashRecorded = port?.summary?.cash_user_recorded === true;
        const cashSourceNote = cashRecorded ? '真实剩余资金已由用户录入' : '现金仍由旧账本推导，待录入真实剩余资金';
        if (alloc.decision_gate?.research_only && !manualPlan) {
          const summary = strategyLab.summary || {};
          const action = strategyLab.headline || {};
          const paperOnly = ['paper_candidate', 'historical_horizon_passed_paper_only'].includes(summary.status || action.status);
          if (allocBadge) {
            allocBadge.textContent = paperOnly ? '仅纸面研究 · 实盘锁定' : '交易前置未就绪';
            allocBadge.style.background = 'rgba(245,158,11,.15)';
            allocBadge.style.color = '#f59e0b';
          }
          const facts = [
            summary.safe_core_pct == null ? '' : `安全核心 ${fn(summary.safe_core_pct,2)}%`,
            summary.risk_weight_pct == null ? '' : `研究风险 ${fn(summary.risk_weight_pct,2)}%`,
            summary.forward_completed == null ? '' : `独立前向 ${fn(summary.forward_completed,0)} 个收盘`,
            summary.forward_return_pct == null ? '' : `累计 ${fpp(summary.forward_return_pct)}`,
          ].filter(Boolean).join(' · ');
          allocBody.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap"><div style="flex:1;min-width:280px"><strong style="color:var(--gold)">${esc(summary.reason || action.reason || '未形成可执行交易条件')}</strong><div class="c-dim" style="font-size:var(--text-xs);margin-top:7px">${esc(facts || decisionGateSummary(alloc.decision_gate))}</div></div><a href="/strategy-lab" style="color:var(--accent);text-decoration:none">查看完整策略证据 →</a></div>`;
        } else if (!items.length) {
          const existingPositionCount = Array.isArray(alloc.existing_positions) ? alloc.existing_positions.length : 0;
          const positionCount = Number(alloc.position_count ?? existingPositionCount);
          const hasExistingPositions = alloc.plan_status === 'portfolio_only' || positionCount > 0 || existingPositionCount > 0;
          allocBody.innerHTML = hasExistingPositions ? `<strong style="color:var(--gold)">当前持仓 ${fn(positionCount, 0)} 只 · 已用仓位 ${fn(alloc.used_pct, 1)}%</strong><div class="c-dim" style="font-size:var(--text-xs);margin-top:6px">持仓市值 ¥${fn(alloc.total_used, 0)} · ${cashRecorded ? '可用现金' : '账本推导现金'} ¥${fn(alloc.cash_buffer, 0)}（${fn(alloc.cash_buffer_pct, 1)}%） · ${cashSourceNote} · 当前没有新的机会质量通过股票；未生成新增买入动作${manualPlan ? '；手工计划不会拿观察或否决股票凑仓位' : ''}</div>` : alloc.plan_status === 'cash_only' && positionCount === 0 && existingPositionCount === 0 ? (cashRecorded ? `<strong style="color:var(--gold)">100% 现金等待</strong><div class="c-dim" style="font-size:var(--text-xs);margin-top:6px">可用现金 ¥${fn(alloc.cash_buffer ?? cap, 0)} · 当前无持仓且没有达到可操作标准的机会${manualPlan ? '；手工计划不会拿观察或否决股票凑仓位' : ''}</div>` : `<strong style="color:var(--gold)">账本推导现金 · 待录入真实资金</strong><div class="c-dim" style="font-size:var(--text-xs);margin-top:6px">旧账本推导余额 ¥${fn(alloc.cash_buffer ?? cap, 0)}，不能解释为真实账户 100% 现金；录入真实剩余资金后才生成可信仓位计划</div>`) : `<strong style="color:var(--gold)">当前组合状态未确认</strong><div class="c-dim" style="font-size:var(--text-xs);margin-top:6px">持仓信息不足，未将现金比例解释为 100%；请刷新后重试</div>`;
        } else {
          const rows = items.map(a => {
            const lv = a.levels || {};
            const riskLine = effectiveRiskLevel(lv.risk_levels, lv.effective_risk_level);
            const finiteAllocation = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
            const targetWeight = finiteAllocation(a.target_weight_pct) ? Number(a.target_weight_pct) : null;
            const allocationEvidence = [
              finiteAllocation(a.estimated_buy_cost_yuan) ? `预估买入成本 ¥${fn(a.estimated_buy_cost_yuan, 2)}` : '', finiteAllocation(a.estimated_sell_cost_yuan) && Number(a.estimated_sell_cost_yuan) > 0 ? `预估卖出成本 ¥${fn(a.estimated_sell_cost_yuan, 2)}` : '',
              finiteAllocation(a.liquidity_capacity_yuan) ? `流动性上限 ¥${fn(a.liquidity_capacity_yuan, 0)}` : '',
              finiteAllocation(a.allocation_score) ? `波动调整分 ${fn(a.allocation_score, 1)}` : '',
            ].filter(Boolean).join(' · ');
            const heldTag = a.held_shares > 0 ? (Number(a.reduce_shares || 0) > 0
                  ? `<span style="color:#f59e0b;font-size:12px">已持有 ${fn(a.held_shares,0)} 股 · 建议减 ${fn(a.reduce_shares,0)} 股</span>` : a.held_ok
                  ? `<span style="color:#22c55e;font-size:12px">已持有 ${fn(a.held_shares,0)} 股 ✓ 达标</span>`
                  : `<span style="color:#f59e0b;font-size:12px">已持有 ${fn(a.held_shares,0)} 股 · 建议补 ${fn(a.add_shares,0)} 股</span>`)
              : '';
            return `<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--border-subtle);flex-wrap:wrap">
              <strong style="min-width:90px">${esc(a.name)}</strong>
              <span class="c-dim">${esc(a.code)}</span>
              <span style="color:var(--text-tertiary)">¥${fp(a.price)}</span>
              <span class="num" style="font-weight:700">${fn(a.shares, 0)} 股</span>
              <span style="color:var(--text-tertiary)">≈ ¥${fn(a.amount, 0)}${targetWeight == null ? (a.score_pct == null ? '' : ` · 分配占比 ${fp(a.score_pct)}%`) : ` · 目标仓位 ${fn(targetWeight, 2)}%`}</span>
              ${heldTag}
              ${a.commission_sensitive ? '<span style="color:#f59e0b;font-size:12px">佣金敏感</span>' : ''}
              <span style="margin-left:auto;color:var(--text-tertiary);font-size:12px">${esc(riskLevelMeta(riskLine))}</span>
              ${allocationEvidence ? `<span style="flex-basis:100%;padding-left:102px;color:var(--text-tertiary);font-size:12px">${allocationEvidence}</span>` : ''}
            </div>`;
          }).join('');
          const skipped = (alloc.skipped || []).map(s =>
            `<div style="padding:4px 0;font-size:12px;color:#f59e0b">${esc(s.name)} ${esc(s.reason)}</div>`).join('');
          allocBody.innerHTML = rows + (skipped ? '<div style="margin-top:6px">' + skipped + '</div>' : '') +
            `<div style="display:flex;justify-content:space-between;align-items:center;padding-top:8px;font-size:12px;flex-wrap:wrap">
              <span>投入 <b>¥${fn(alloc.total_used, 0)}</b> / 现金缓冲 <b class="c-dim">¥${fn(alloc.cash_buffer, 0)}（${fn(alloc.cash_buffer_pct, 1)}%）</b></span>
              <span class="c-dim">${manualPlan ? '手工计划 · 不自动下单；' : ''}${esc(alloc.note || '')}</span>
            </div>`;
        }
      }
    }
    // 统计条
    const opportunityRows = [...((opp && opp.items) || []), ...((opp && opp.watch_items) || [])], oppItems = Array.from(new Map(opportunityRows.map(item => [String(item.code || ''), item])).values());
    const revAlerts = (rev && rev.alerts) || [];
    const openPos = (port && port.open) || [];
    const alertsActive = port?.alerts_active === true && port?.market_session?.is_open === true;
    const reviewActions = alertsActive ? openPos.map(sellReviewPlan).filter(Boolean) : [];
    const decisionGate = (port && port.decision_gate) || (alloc && alloc.decision_gate) || (opp && opp.decision_gate);
    const rp = rev?.realized_pnl;
    $('#sigOppCount').textContent = opp ? oppItems.length : '—';
    $('#sigAlertCount').textContent = port ? reviewActions.length : '—';
    $('#sigPosCount').textContent = port ? (openPos.length ? openPos.length + ' 只' : '0') : '—';
    const rpEl = $('#sigRealized');
    if (rpEl) { rpEl.textContent = rp != null ? (rp >= 0 ? '+' : '') + fn(rp, 0) : '待核验'; rpEl.className = 'sum-val ' + (rp != null ? cc(rp) : ''); }
    const oppEl = $('#sigOpps');
    if (oppEl) {
      if (!opp) oppEl.innerHTML = `<div class="empty-state" role="status">${esc(apiError('/api/opportunities', '研究观察池暂不可用'))}</div>`;
      else if (!oppItems.length) oppEl.innerHTML = '<div class="empty-state">暂无机会数据</div>';
      else oppEl.innerHTML = oppItems.map(it => {
        const sig = it.signals || {}, lv = it.levels || {};
        const levelRows = Array.isArray(lv.risk_levels) ? lv.risk_levels : [], activeLevel = effectiveRiskLevel(levelRows, lv.effective_risk_level), researchLevels = levelRows.filter(level => level !== activeLevel);
        const alert = it.alert || {}, trig = alert.status === 'triggered';
        const gateFacts = decisionGateFacts(decisionGate);
        const accountExecution = opp?.account_execution || {};
        const accountSummary = accountExecutionSummary(accountExecution);
        const alertReason = !gateFacts.manualAlertReady
          ? '研究级人工提醒未启用；仅保留研究观察和持仓卖出风控。'
          : accountExecution.new_buy_plan_eligible !== true
            ? accountSummary.reason
            : (alert.reason || '研究级人工提醒已启用；仅在完整买点、整手资金和风险条件同时满足时触发。');
        const badges = Object.keys(sig).map(k => sigChip(k, sig[k])).join('');
        const composite = it.assessment?.gates?.composite || {}, rawComposite = it.unified_composite_score != null ? Number(it.unified_composite_score) : (composite.composite == null ? NaN : Number(composite.composite));
        const compositeScore = Number.isFinite(rawComposite) ? rawComposite : null, compositeRating = composite.action_rating || composite.rating || composite.score_rating || '评分不可用';
        const timingSummary = timingConditionSummary(it), timingState = it.manual_review_state === 'ready_for_manual_review' || it.assessment?.action_state === 'ENTRY_READY' ? '人工复核时机满足 · 非推荐' : '继续观察时机';
        return `<div class="card" style="margin-bottom:12px;cursor:pointer" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(it.code || '')}">
          <div class="card-header"><span class="card-title">${esc(it.name)} <span class="c-dim" style="font-size:12px">${esc(it.code)}</span></span>
            ${it.streak_days>=2 ? `<span class="card-badge" style="background:rgba(245,158,11,.12);color:#f59e0b" title="连续${it.streak_days}天出现在研究观察榜（每日快照口径）">连续${it.streak_days}天</span>` : ''}
            ${compositeScore == null ? '<span class="card-badge" style="background:rgba(148,163,184,.12);color:#94a3b8">统一综合分不可用</span>' : `<span class="card-badge" style="background:${scoreColor(compositeScore)}22;color:${scoreColor(compositeScore)}">统一综合 ${fn(compositeScore, 1)}分 · ${esc(compositeRating)}</span>`}
            ${trig ? '<span class="card-badge" style="background:rgba(220,38,38,.15);color:#ef4444">已触发提醒</span>' : ''}
          </div>
          <div class="card-body">
            <div style="display:flex;gap:16px;margin-bottom:8px;font-size:13px;flex-wrap:wrap"><span>现价 <b>¥${fp(it.price)}</b></span><span>涨跌 <b class="${cc(it.change_pct)}">${it.change_pct != null ? fp(it.change_pct) + '%' : '—'}</b></span><span style="color:var(--text-tertiary)">${esc(riskLevelMeta(activeLevel))}</span></div>
            <div style="font-size:12px;color:var(--text-secondary);margin-bottom:7px">盘中买点条件 <b>${esc(timingSummary)}</b> · ${esc(timingState)} · 不产生第二个总分</div>
            <div style="font-size:12px">${badges}</div>
            ${researchLevels.length ? `<div style="font-size:12px;color:var(--text-tertiary);margin-top:6px">研究线：${researchLevels.map(level => esc(riskLevelMeta(level))).join('<br>')}</div>` : ''}
            <div style="font-size:12px;color:var(--text-tertiary);margin-top:6px">${esc(alertReason)}</div>
          </div>
        </div>`;
      }).join('');
    }
    const alertEl = $('#sigAlerts');
    if (alertEl) {
      const sourceErrors = [
        port ? '' : apiError('/api/timing/portfolio', '持仓风控暂不可用'),
        rev ? '' : apiError('/api/timing/review', '提醒历史暂不可用'),
      ].filter(Boolean);
      const statusLbl = { triggered: '已触发', watching: '监控中', confirmed: '已确认', dismissed: '已忽略', idle: '已处理' };
      const gateFacts = decisionGateFacts(decisionGate);
      const manualAlertEnabled = gateFacts.manualAlertReady;
      const accountExecution = opp?.account_execution || {};
      const accountSummary = accountExecutionSummary(accountExecution);
      const buyOpen = manualAlertEnabled && accountExecution.new_buy_plan_eligible === true;
      const buyReadinessReason = !manualAlertEnabled
        ? decisionGateSummary(decisionGate)
        : buyOpen
          ? '真实现金、持仓估值与市场暴露约束已齐；只在有效买点触发，仍须本人复核。'
          : accountSummary.reason;
      const buyRow = `<div style="display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--border-subtle);font-size:13px;flex-wrap:wrap">
        <span class="signal-action-tag ${buyOpen ? 'buy' : 'sell'}">人工买入提醒</span>
        <strong style="color:${buyOpen ? 'var(--bull)' : 'var(--gold)'}">${buyOpen ? '账户计划就绪' : manualAlertEnabled ? '能力已启用 · 等待账户事实' : '未启用'}</strong>
        <span class="c-dim">${esc(buyReadinessReason)}</span>
        ${accountSummary.capacity ? `<span class="c-dim" style="flex-basis:100%;padding-left:112px">${esc(accountSummary.capacity)}</span>` : ''}
        ${manualAlertEnabled ? '' : '<a href="/strategy-lab" style="margin-left:auto;color:var(--accent);text-decoration:none">查看策略开发 →</a>'}
      </div>`;
      const positionRows = openPos.map(p => {
        const ra = p.risk_action || {}, tp = p.trade_plan || {}, rawReview = sellReviewPlan(p), review = alertsActive ? rawReview : null;
        const closedSessionRisk = !alertsActive && rawReview;
        const needsExit = review?.plan.state === 'EXIT_REVIEW';
        const needsReduce = review?.plan.state === 'REDUCE_REVIEW';
        const label = closedSessionRisk
          ? '休市风险预警 · 下个交易时段重新复核'
          : needsExit
          ? `卖出复核 · ${review.shares}股`
          : needsReduce
            ? `减仓复核 · ${review.shares}股 → ${Number(review.action.target_shares || 0)}股`
            : '持有观察';
        const tone = closedSessionRisk || needsReduce ? 'var(--gold)' : needsExit ? 'var(--bear)' : 'var(--text-secondary)';
        const riskLine = effectiveRiskLevel(ra.risk_levels, ra.effective_risk_level);
        return `<div style="display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--border-subtle);font-size:13px;flex-wrap:wrap;cursor:pointer" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(p.code || '')}">
          <span class="signal-action-tag sell">${esc(label)}</span>
          <strong>${esc(p.name || p.code)}</strong><span class="c-dim">${esc(p.code)}</span>
          <span class="num ${cc(p.pnl_pct)}">现价 ¥${fp(p.current)} · ${fpp(p.pnl_pct)}</span>
          <span class="c-dim">${esc(entryAttributionLabel(p))}</span>
          <span style="color:${tone};flex:1;min-width:220px">${esc(tp.reason || '当前没有带精确数量的退出依据，继续持有并观察')}</span>
          <span class="c-dim">${esc(riskLevelMeta(riskLine))}</span>
        </div>`;
      }).join('');
      const noPositionRow = openPos.length ? '' : '<div class="empty-state">当前无持仓，暂无卖出提醒</div>';
      const actionableAlerts = revAlerts.filter(a => manualAlertEnabled || a.action !== 'buy');
      const eventRows = actionableAlerts.slice(0, 12).map(a => {
        const buy = a.action === 'buy';
        return `<div style="display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--border-subtle);font-size:13px">
          <span class="signal-action-tag ${buy ? 'buy' : 'sell'}">${buy ? '研究买入' : a.timing_interpretation ? '风险复核·非最佳卖点' : a.origin === 'manual_exit_intent' ? '你的退出计划' : '卖出复核'}</span>
          <strong>${esc(a.name || a.code)}</strong>
          <span class="c-dim">${esc(a.window_label || '')} · ${fp(a.price)}</span>
          <span class="num" style="margin-left:auto">${a.score != null ? a.score + '分' : ''}</span>
          <span style="color:var(--text-tertiary)">${(a.created_at || '').slice(11, 16)}</span>
          <span class="c-dim">${statusLbl[a.status] || a.status}</span>
        </div>`;
      }).join('');
      const suppressedNote = (rev?.suppressed_buy_alert_count || 0) > 0
        ? `<div style="padding:8px 16px;font-size:12px;color:var(--gold)">已隔离 ${rev.suppressed_buy_alert_count} 条旧买入事件；卖出风控不受影响。</div>`
        : '';
      const sourceErrorRow = sourceErrors.length
        ? `<div class="empty-state" role="status">${sourceErrors.map(esc).join('；')}</div>`
        : '';
      alertEl.innerHTML = sourceErrorRow + buyRow + positionRows + noPositionRow + suppressedNote + (eventRows
        ? `<div style="padding:8px 16px 4px;font-size:12px;color:var(--text-tertiary)">今日已触发事件</div>${eventRows}`
        : '<div style="padding:8px 16px;font-size:12px;color:var(--text-tertiary)">今日尚无价格触发事件；上方持仓动作按当前行情持续更新。</div>');
    }
    const posEl = $('#sigPositions');
    if (posEl) {
      if (!port) posEl.innerHTML = `<div class="empty-state" role="status">${esc(apiError('/api/timing/portfolio', '持仓数据暂不可用'))}</div>`;
      else if (!openPos.length) posEl.innerHTML = '<div class="empty-state">暂无持仓</div>';
      else posEl.innerHTML = openPos.map(p => {
        const lv = p.levels || {}, ra = p.risk_action || {}, rawReview = sellReviewPlan(p), review = alertsActive ? rawReview : null;
        const closedSessionRisk = !alertsActive && rawReview;
        const riskLine = effectiveRiskLevel(ra.risk_levels, ra.effective_risk_level)
          || effectiveRiskLevel(lv.risk_levels, lv.effective_risk_level);
        const actionLabel = closedSessionRisk
          ? '休市风险预警 · 下个交易时段重新复核'
          : review?.plan.state === 'EXIT_REVIEW'
          ? `卖出复核 · ${review.shares}股`
          : review?.plan.state === 'REDUCE_REVIEW'
            ? `减仓复核 · ${review.shares}股 → ${Number(review.action.target_shares || 0)}股`
            : '持有观察';
        const actionColor = closedSessionRisk || review?.plan.state === 'REDUCE_REVIEW' ? 'var(--gold)' : review?.plan.state === 'EXIT_REVIEW' ? 'var(--bear)' : 'var(--text-secondary)';
        return `<div style="display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--border-subtle);font-size:13px;cursor:pointer;flex-wrap:wrap" role="link" tabindex="0" data-href="/stock?code=${encodeURIComponent(p.code || '')}">
          <strong>${esc(p.name)}</strong><span class="c-dim">${esc(p.code)}</span>
          <span class="c-dim">${p.shares}股 @ ${fp(p.price)}</span>
          <span class="num ${cc(p.pnl_pct)}">¥${fp(p.current)} ${(p.pnl_pct >= 0 ? '+' : '') + fp(p.pnl_pct)}%</span>
          <span class="c-dim">${esc(entryAttributionLabel(p))}</span>
          <span style="color:${actionColor};font-weight:600">${esc(actionLabel)}</span>
          <span style="margin-left:auto;color:var(--text-tertiary)">${esc(riskLevelMeta(riskLine))}</span>
          <button class="pos-del" type="button" data-action="delete-position" data-id="${Number(p.id)}" data-name="${escAttr(p.name)}">删</button>
        </div>`;
      }).join('');
    }
    const closedEl = $('#sigClosed');
    if (closedEl) {
      const closed = (rev && (rev.closed_all || rev.closed)) || [];
      const perf = rev?.execution_performance || {}, costComplete = perf.cost_breakdown_status === 'complete', feeLabel = perf.fee_source_status === 'actual' ? '券商实际费用后' : '估算费用后', slippageLabel = perf.slippage_attribution_status === 'complete' ? `执行偏差 ${fn(perf.total_adverse_slippage_yuan,2)} 元（正数为不利）` : perf.slippage_attribution_status === 'partial' ? '执行偏差仅部分可归因' : '执行偏差不可得', perfHtml = perf.completed_positions ? `<div style="padding:10px 16px;border-bottom:1px solid var(--border-subtle);font-size:12px;color:var(--text-tertiary)">${costComplete ? `全部手工成交（混合来源）· ${feeLabel} ${fn(perf.completed_positions,0)} 笔 · 胜率 ${fpp(perf.win_rate_pct)} · 平均净收益 ${fpp(perf.average_net_return_pct)} · 盈亏比 ${perf.profit_factor == null ? '样本不足' : fn(perf.profit_factor,2)}` : `历史记录 ${fn(perf.completed_positions,0)} 笔 · 记录盈亏 ¥${fn(perf.recorded_pnl_yuan,2)} · ${fn(perf.cost_breakdown_missing_positions,0)} 笔缺少费用拆分，成本后绩效不可得`} · ${slippageLabel} · 平均持有 ${perf.average_holding_calendar_days == null ? '—' : fn(perf.average_holding_calendar_days,1)+'天'}</div>${strategyAttributionHtml(perf)}` : strategyAttributionHtml(perf);
      if (!rev) closedEl.innerHTML = `<div class="empty-state" role="status">${esc(apiError('/api/timing/review', '平仓历史暂不可用'))}</div>`; else if (!closed.length) closedEl.innerHTML = perfHtml + '<div class="empty-state">还没有平仓记录</div>'; else closedEl.innerHTML = perfHtml + closed.map(p => `
        <div style="display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--border-subtle);font-size:13px;flex-wrap:wrap">
          <strong>${esc(p.name)}</strong>
          <span class="c-dim">${p.sold_shares_total ?? p.initial_shares ?? p.shares}股 · 买${fp(p.price)} → 卖均${fp(p.average_sell_price ?? p.sold_price)}</span>
          <span class="c-dim">${esc(entryAttributionLabel(p))}</span><span class="c-dim">${esc(executionSlippageLabel(p))}</span>
          <span style="margin-left:auto;font-weight:700" class="${cc(p.pnl)}">${(p.pnl >= 0 ? '+' : '') + fn(p.pnl, 2)}</span>
          <span class="c-dim">${(p.sold_at || '').slice(0, 16)}</span>
          <span class="c-dim" style="flex-basis:100%" data-exit-attribution="${Number(p.id)}">${String(p.sold_at || '').slice(0,10) === String(rev.date || '') ? '收盘后计算固定事后归因…' : '历史成交未启用当日收盘归因'}</span>
        </div>`).join('');
      if (closed.some(p => String(p.sold_at || '').slice(0,10) === String(rev?.date || ''))) {
        api('/api/timing/exit-attribution').then(attribution => {
          if (!attribution || state.page !== 'signals' || state.navSequence !== renderGeneration) return;
          const byPosition = new Map((attribution.items || []).map(row => [Number(row.position_id), row]));
          closedEl.querySelectorAll('[data-exit-attribution]').forEach(node => {
            const row = byPosition.get(Number(node.dataset.exitAttribution));
            if (!row || !row.versus_close) {
              node.textContent = attribution.status === 'pending_close' ? '收盘后再计算，避免使用形成中价格' : '同日收盘行情不可用，未做事后推断';
              return;
            }
            const closeGap = Number(row.versus_close.estimated_net_difference_yuan);
            const highGap = Number(row.versus_session_high?.estimated_net_difference_yuan);
            node.textContent = `固定事后归因：收盘 ¥${fp(row.close_price)}，相对实际卖出估算净差 ${closeGap >= 0 ? '+' : ''}¥${fn(closeGap,2)}；当日最高价后视上限 ${Number.isFinite(highGap) ? `${highGap >= 0 ? '+' : ''}¥${fn(highGap,2)}` : '—'}（不是可实现基准）`;
          });
        });
      }
    }
    const up = $('#sigUpdated');
    if (up) up.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }
  await refresh();
  if (state.page === 'signals' && state.navSequence === renderGeneration) {
    _sigTimer = setInterval(refresh, 30000);
  }
}
function renderLiveSignals(d, monitor) {
  const mk = d.market_status || {};
  const currentSession = (monitor && monitor.market_session) || {};
  const marketOpen = monitor ? monitor.active_now === true : mk.open === true;
  const marketLabel = currentSession.label || mk.label || '—';
  const qm = d.quote_meta || {};
  const health = qm.operational_health_status || qm.health_status || 'unknown';
  const visibleSignals = (d.signals || []).filter(s => ['BUY','SELL','WATCH'].includes(s.action));
  const watchCount = visibleSignals.length;
  const quoteHealthy = health === 'live' || health === 'validated';
  const strategyPassed = d.strategy_validation?.passed === true;
  const pipelineHealthy = d.stale !== true && strategyPassed;
  const healthy = quoteHealthy && pipelineHealthy;
  const healthLabel = !marketOpen ? '休市'
    : d.stale === true ? '研究链陈旧'
      : !strategyPassed ? '策略未通过'
        : healthy ? '健康' : health === 'delayed' ? '延迟' : '不可用';
  $('#liveSigHealth').textContent = watchCount + ' 观察项 · ' + healthLabel;
  $('#liveSigHealth').style.background = healthy && marketOpen ? 'rgba(34,197,94,.15)' : 'rgba(245,158,11,.15)';
  $('#liveSigHealth').style.color = healthy && marketOpen ? '#22c55e' : '#f59e0b';
  $('#liveSigMarket').textContent = '当前市场: ' + marketLabel;
  const expectedCoverage = Number(qm.coverage_expected || d.n_watchlist || 0);
  const liveCoverage = Number(qm.live_decision_coverage_returned || 0);
  const displayCoverage = Number(qm.display_coverage_returned ?? qm.coverage_returned ?? d.n_quotes ?? 0);
  const coverageText = !marketOpen
    ? `休市参考 ${displayCoverage}只 · 下次监控 Top${expectedCoverage}`
    : `实时覆盖 ${liveCoverage}/${expectedCoverage}`;
  $('#liveSigSource').textContent = '行情源: ' + (qm.source || '—') + ` · ${coverageText}` + (qm.operational_degraded ? '（降级）' : '');
  $('#liveSigFactorAsOf').textContent = '使用因子日: ' + (d.required_factor_as_of || '—') + ((d.factor_as_of||[]).length>1?'（已剔除落后日期）':'');
  $('#liveSigUpdated').textContent = '因子快照: ' + (d.generated_at ? new Date(d.generated_at).toLocaleTimeString('zh-CN', {hour12:false}) : '—');
  const body = $('#liveSigBody');
  if (!body) return;
  const sigs = visibleSignals;
  if (!sigs.length) {
    const reason = !marketOpen ? '当前市场未开盘；正式因子消费者保持静默'
      : d.stale === true ? '研究快照陈旧，已安全静默'
        : !strategyPassed ? '生产策略验证未通过，已安全静默'
          : !quoteHealthy ? '当日交易所时间行情不可用，已安全静默'
            : '当前没有分数达到观察阈值的标的';
    body.innerHTML = `<tr><td colspan="9" style="color:var(--text-tertiary);text-align:center">${reason}</td></tr>`;
  } else {
    body.innerHTML = sigs.map(s => {
      const icon = s.action === 'BUY' ? '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#2EBD85;margin-right:6px"></span>' : s.action === 'WATCH' ? '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#FFB800;margin-right:6px"></span>' : '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#8B8F9A;margin-right:6px"></span>';
      return `<tr>
        <td>${icon} <strong>${s.action}</strong></td>
        <td>${esc(s.code)}</td>
        <td>${esc(s.name||'')}</td>
        <td class="num">${s.score!=null?s.score.toFixed(2):'—'}</td>
        <td class="num">${s.factor_z!=null?s.factor_z.toFixed(2):'—'}</td>
        <td class="num">${s.momentum_z!=null?s.momentum_z.toFixed(2):'—'}</td>
        <td class="num ${s.pct_chg>=0?'c-bull':'c-bear'}">${s.pct_chg!=null?(s.pct_chg>0?'+':'')+s.pct_chg+'%':'—'}</td>
        <td class="c-dim" style="font-size:12px">${esc(s.factor_as_of||'—')}</td>
        <td style="font-size:12px;color:var(--text-tertiary)">${esc(s.reason||'')}</td>
      </tr>`;
    }).join('');
  }
  $('#liveSigHonesty').textContent = `${d.honesty || ''} · 独立因子研究流，不直接生成统一买入提醒`;
  return watchCount;
}
async function renderFundFlow() {
  const renderGeneration = state.navSequence;
  let requestSequence = 0;
  const container = $('[data-page="fundflow"]');
  container.innerHTML = `
    ${marketCenterNav('fundflow')}
    <div class="flow-truth-bar" id="flowTruth" aria-live="polite">
      <div class="flow-truth-main">
        <span class="flow-state unknown">正在核验快照</span>
        <span class="flow-truth-copy">资金流数据将显示来源时间、延迟状态与异常隔离结果。</span>
      </div>
      <button class="btn btn-ghost btn-sm" id="flowRefresh" type="button">刷新快照</button>
    </div>
    <p class="flow-note" id="flowQualityNote" hidden></p>
    <div class="summary-strip" id="flowSummary" style="margin-bottom:16px"></div>
    <div class="filter-bar">
      <label class="sr-only" for="flowSort">资金流排序指标</label>
      <select class="select" id="flowSort"><option value="net">净额</option><option value="inflow">流入</option><option value="turnover">换手率</option><option value="pct_chg">涨跌幅</option></select>
      <label class="sr-only" for="flowDir">资金流排序方向</label>
      <select class="select" id="flowDir"><option value="desc">降序</option><option value="asc">升序</option></select>
      <label class="sr-only" for="flowSearch">筛选六位股票代码</label>
      <input class="input" id="flowSearch" inputmode="numeric" maxlength="6" placeholder="筛选 6 位代码" aria-label="筛选六位股票代码" style="width:160px">
      <button class="btn btn-primary btn-sm" id="flowQuery" type="button">查询</button>
    </div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>代码</th><th>名称</th><th>净额(亿)</th><th>流入(亿)</th><th>流出(亿)</th><th>换手率</th><th>涨跌幅</th></tr></thead>
      <tbody id="flowTbody"><tr><td colspan="7"><div class="empty-state">正在载入并核验资金流快照…</div></td></tr></tbody>
    </table></div>
    <div style="margin-top:8px;font-size:var(--text-xs);color:var(--text-tertiary)" id="flowMeta"></div>
  `;
  function sourceTime(value) {
    if (!value) return '未提供';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleString('zh-CN', {
      hour12:false, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit',
    });
  }
  function sourceAge(seconds) {
    if (seconds == null) return '未知';
    if (seconds < 60) return `${seconds} 秒`;
    if (seconds < 3600) return `${Math.floor(seconds/60)} 分`;
    return `${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分`;
  }
  function showFlowState(label, message, busy=false) {
    const truth = $('#flowTruth');
    if (!truth) return;
    truth.setAttribute('aria-busy', String(busy));
    truth.innerHTML = `<div class="flow-truth-main"><span class="flow-state unknown">${esc(label)}</span><span class="flow-truth-copy">${esc(message)}${busy ? '' : ' · <a href="/data-center">核对数据来源</a>'}</span></div><button class="btn btn-ghost btn-sm" id="flowRefresh" type="button"${busy ? ' disabled' : ''}>${busy ? '核验中…' : '刷新快照'}</button>`;
    const refresh = $('#flowRefresh');
    if (refresh && !busy) refresh.addEventListener('click', () => window._fetchFundFlow());
  }
  window._fetchFundFlow = async function() {
    const sequence = ++requestSequence;
    const sort = $('#flowSort').value;
    const dir = $('#flowDir').value;
    const q = $('#flowSearch').value.trim();
    const search = $('#flowSearch');
    const summary = $('#flowSummary');
    const meta = $('#flowMeta');
    const note = $('#flowQualityNote');
    if (summary) summary.innerHTML = '';
    if (meta) meta.innerHTML = '';
    if (note) { note.hidden = true; note.textContent = ''; }
    if (q && !/^\d{6}$/.test(q)) {
      search.setAttribute('aria-invalid', 'true');
      const tbody = $('#flowTbody');
      if (tbody) tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state">请输入完整的 6 位股票代码。</div></td></tr>';
      showFlowState('查询未执行', '请输入完整的 6 位股票代码。');
      toast('股票代码必须是 6 位数字', 'error');
      return;
    }
    search.removeAttribute('aria-invalid');
    const params = new URLSearchParams({ sort, dir, limit: '100' });
    if (q) params.set('q', q);
    const endpoint = '/api/fund-flow/ranking?' + params.toString();
    showFlowState('正在核验快照', '正在读取当前筛选条件的资金流快照。', true);
    const loadingBody = $('#flowTbody');
    if (loadingBody) loadingBody.innerHTML = '<tr><td colspan="7"><div class="empty-state">正在载入并核验资金流快照…</div></td></tr>';
    const data = await api(endpoint);
    if (sequence !== requestSequence || state.page !== 'fundflow' || state.navSequence !== renderGeneration) return;
    if (!data || data.error) {
      const tbody = $('#flowTbody');
      const message = data?.error || apiError(endpoint, '资金流快照暂不可用，请稍后重试。');
      if (tbody) tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">${esc(message)}</div></td></tr>`;
      showFlowState('快照不可用', message);
      toast(message, 'error');
      return;
    }
    const quality = data.data_quality || {};
    const freshness = quality.freshness || {state:'unknown', label:'来源时间缺失'};
    const knownState = ['fresh','market_break','market_closed','delayed','stale','invalid','unknown'].includes(freshness.state) ? freshness.state : 'unknown';
    const truth = $('#flowTruth');
    if (truth) truth.setAttribute('aria-busy', 'false');
    if (truth) truth.innerHTML = `
      <div class="flow-truth-main">
        <span class="flow-state ${knownState}">${esc(freshness.label || '状态未知')}</span>
        <span class="flow-truth-copy"><strong>${esc(quality.source_name || '资金流快照')}</strong> · 数据时点 ${sourceTime(quality.source_time)} · 快照年龄 ${sourceAge(freshness.age_seconds)} · 覆盖 ${quality.served_rows ?? data.total ?? 0} 条</span>
      </div>
      <button class="btn btn-ghost btn-sm" id="flowRefresh" type="button">刷新快照</button>`;
    const refreshAgain = $('#flowRefresh');
    if (refreshAgain) refreshAgain.addEventListener('click', () => window._fetchFundFlow());
    const invalidPct = quality.invalid_pct_chg_rows || 0;
    const rejected = quality.rejected_rows || 0;
    if (note) {
      const notes = [];
      if (invalidPct) notes.push(`${invalidPct} 条异常涨跌幅已隔离，不参与涨跌幅排序或市场宽度`);
      if (rejected) notes.push(`${rejected} 条结构异常记录未展示`);
      if (quality.source_disclosure) notes.push(quality.source_disclosure);
      note.hidden = notes.length === 0;
      note.textContent = notes.join('；') + (notes.length ? '。' : '');
    }
    if (summary) {
      const market = data.market_summary || {};
      const maxIn = market.top_in;
      const maxOut = market.top_out;
      summary.innerHTML = `
        <div class="sum-item"><span class="sum-val c-bull">${market.positive_net_count ?? '—'} 只</span><span class="sum-lbl">全样本净流入（>0.1亿）</span></div>
        <div class="sum-item"><span class="sum-val c-bear">${market.negative_net_count ?? '—'} 只</span><span class="sum-lbl">全样本净流出（≤ -0.1亿）</span></div>
        <div class="sum-item"><span class="sum-val ${cc(market.net_total_yi)}">${fmtYi(market.net_total_yi)}</span><span class="sum-lbl">样本净额合计</span></div>
        <div class="sum-item"><span class="sum-val">${maxIn ? esc(maxIn.name)+' '+fmtYi(maxIn.net_yi) : '—'}</span><span class="sum-lbl">最大净流入</span></div>
        <div class="sum-item"><span class="sum-val">${maxOut ? esc(maxOut.name)+' '+fmtYi(maxOut.net_yi) : '—'}</span><span class="sum-lbl">最大净流出</span></div>
      `;
    }
    if (meta) {
      const mag = data.magnitude;
      meta.innerHTML = `已通过结构校验 ${quality.served_rows ?? data.total ?? 0} / ${quality.raw_rows ?? data.total ?? 0} 条 · 当前筛选 ${data.total||0} 条 · 显示 ${(data.items||[]).length} 条 · 仅作研究核验，不构成交易建议<br>` +
        (mag ? `<span style="color:var(--gold)">口径提示：${esc(mag.note || '净额、流入与流出均已按审计比例统一折算；原始值仅用于溯源。')} 上方金额已是${esc(mag.unit || '统一折算亿元')}，无需再次换算。</span>` : '');
    }
    const tbody = $('#flowTbody');
    if (!data.items?.length) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state">无匹配数据</div></td></tr>';
      return;
    }
    tbody.innerHTML = data.items.map(r => `
      <tr>
        <td><a href="/stock?code=${encodeURIComponent(r.code || '')}" style="color:var(--accent);font-weight:600">${esc(r.code)}</a></td>
        <td><strong>${esc(r.name)}</strong>${r.limit_up ? `<span class="flow-limit-tag" style="margin-left:6px;padding:1px 6px;border-radius:4px;font-size:12px;background:rgba(255,184,0,0.15);color:var(--gold)">涨停</span>` : ''}</td>
        <td class="num ${cc(r.net_yi)}">${fmtYi(r.net_yi)}${r.limit_up_net_out ? `<span title="涨停封板量大单对冲，净流出不代表真实离场" style="margin-left:4px;font-size:12px;color:var(--gold);cursor:help">失真</span>` : ''}</td>
        <td class="num">${fn(r.inflow_yi,1)}亿</td>
        <td class="num">${fn(r.outflow_yi,1)}亿</td>
        <td class="num">${fn(r.turnover,1)}%</td>
        <td class="num ${cc(r.pct_chg)}" title="${r.pct_chg==null ? '该条涨跌幅异常，已从相关计算中隔离' : ''}">${fpc(r.pct_chg)}</td>
      </tr>
    `).join('');
  };
  $('#flowQuery').addEventListener('click', () => window._fetchFundFlow());
  $('#flowRefresh').addEventListener('click', () => window._fetchFundFlow());
  $('#flowSearch').addEventListener('keydown', event => { if (event.key === 'Enter') window._fetchFundFlow(); });
  await window._fetchFundFlow();
}
function echartsTheme() {
  return {
    textStyle: { color: '#8B8F9A', fontFamily: "'SF Mono','JetBrains Mono',monospace" },
    tooltip: {
      backgroundColor: 'rgba(20,23,31,0.95)', borderColor: 'rgba(255,255,255,0.08)',
      borderWidth: 1, textStyle: { color: '#F0F1F5', fontSize: 12 },
      extraCssText: 'backdrop-filter:blur(20px);border-radius:8px;',
    },
  };
}
function disposeAllCharts() {
  Object.values(state.charts).forEach(c => {
    try { if (!c.isDisposed || !c.isDisposed()) c.dispose(); }
    catch(e) { reportClientIssue('chart-dispose', e); }
  });
  state.charts = {};
}
function renderMarketTreemap(el, items) {
  if (!el || !items || !items.length) return;
  if (!window.echarts) {
    return whenEcharts().then(ready => {
      if (ready && el.isConnected && state.page === 'dashboard') renderMarketTreemap(el, items);
      else showEchartsRetry(el, '本地图表运行时未就绪；资金流表仍可核对。', () => renderMarketTreemap(el, items));
    });
  }
  const prev = echarts.getInstanceByDom(el);
  if (prev) prev.dispose();
  const chart = echarts.init(el, null, { renderer:'canvas' });
  const data = items.map(r => ({
    name: r.name || r.code,
    value: Math.max(Math.abs(r.net_yi || 0) * 10, 0.5),
    itemStyle: { color: r.pct_chg >= 0 ? `rgba(0,214,114,${0.15 + Math.min(Math.abs(r.pct_chg||0)/10, 0.8)})` : `rgba(255,59,48,${0.15 + Math.min(Math.abs(r.pct_chg||0)/10, 0.8)})` },
    _code: r.code, _pct: r.pct_chg, _net: r.net_yi,
  }));
  chart.setOption(Object.assign(echartsTheme(), {
    series: [{
      type: 'treemap', data, roam: false, nodeClick: false,
      width: '100%', height: '100%', top: 8, left: 4, right: 4, bottom: 8,
      breadcrumb: { show: false },
      label: { show: true, color: 'rgba(240,241,245,0.85)', fontSize: 12, formatter: p => p.name.length > 5 ? p.name.slice(0,5) : p.name },
      itemStyle: { borderColor: '#0D0F14', borderWidth: 1, gapWidth: 1 },
      emphasis: { itemStyle: { borderColor: '#0A84FF', borderWidth: 1.5 } },
      levels: [{ itemStyle: { borderWidth: 0 } }],
    }],
    tooltip: {
      ...echartsTheme().tooltip,
      formatter: p => {
        if (!p.data._code) return p.name;
        return `<b>${esc(p.data.name)}</b> ${esc(p.data._code)}<br>` +
          `<span style="color:${p.data._pct>=0?'#00D672':'#FF3B30'}">${p.data._pct>=0?'+':''}${(p.data._pct||0).toFixed(2)}%</span> · ` +
          `净额 <b>${(p.data._net||0)>0?'+':''}${(p.data._net||0).toFixed(2)}亿</b>`;
      },
    },
  }));
  chart.on('click', p => { if (p.data._code) window.location.href = '/stock?code=' + p.data._code; });
  state.charts['treemap'] = chart;
  return chart;
}
const _pxStaggered = new WeakSet();
function staggerCards(scope) {
  // Operational risk facts must be visible as soon as their request completes.
  if (scope?.dataset?.page === 'risk') return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const sel = '.card, .method-card, .sig-panel';
  const root = scope || document;
  const els = root.querySelectorAll(sel);
  const top = [];
  els.forEach(el => {
    if (_pxStaggered.has(el)) return;
    let p = el.parentElement, nested = false;
    while (p) { if (p.matches && p.matches(sel)) { nested = true; break; } p = p.parentElement; }
    if (nested) return;
    top.push(el);
  });
  top.forEach((el, i) => {
    if (_pxStaggered.has(el)) return;
    _pxStaggered.add(el);
    el.classList.add('px-in');
    el.style.setProperty('--px-d', Math.min(i * 45, 420));
  });
}
function staggerPasses(scope) {
  [40, 320, 900].forEach(ms => setTimeout(() => staggerCards(scope), ms));
}
Object.assign(RegisSpa.core, {
  $, $$, state, ICONS, NAV_ITEMS, PAGE_PATHS,
  navigate, updateNavActive, updatePageTitle, buildSidebar, toggleSidebar,
  resizeAllCharts, startClock, initMouseGlow, abortPageRequests, api, apiWrite, esc,
  toast, reportClientIssue, whenEcharts, showEchartsRetry,
  fetchDataInfrastructureStatus, renderDashboard, setSpaPortfolioMonitorOwner,
  startOpportunityMonitor, stopOpportunityMonitor, fetchPositionsPanel,
  fetchTimingPositionsMini, fetchResearchMarketState, openAlertConfirm, dismissAlert,
  dashboardPhaseFromSession, setHomeDensity, setHomeWorkspace, restoreHomePreferences,
  apiError, cc, decisionGateFacts, escAttr, fp, fn, fpp, fmtYi, truthLabel,
  createIdempotencyKey, trapDialog, effectiveRiskLevel, riskLevelMeta,
  RISK_KIND_LABELS, echartsTheme, staggerPasses,
  normalizeReviewData, marketCenterNav,
});
})();
