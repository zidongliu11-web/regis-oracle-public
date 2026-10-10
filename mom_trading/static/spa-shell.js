/* Regis Oracle SPA — 交互绑定与启动 */
(function(RegisSpa){
'use strict';
if (!RegisSpa || typeof RegisSpa.core?.navigate !== 'function'
    || typeof RegisSpa.renderPage !== 'function') {
  throw new Error('Regis SPA core must load before shell module');
}
const {
  $, state, NAV_ITEMS, PAGE_PATHS, navigate, updateNavActive,
  updatePageTitle, buildSidebar, resizeAllCharts, startClock,
  abortPageRequests, reportClientIssue,
  fetchDataInfrastructureStatus, staggerPasses,
  renderDashboard, setSpaPortfolioMonitorOwner, startOpportunityMonitor,
  stopOpportunityMonitor, fetchPositionsPanel, fetchTimingPositionsMini,
  openAlertConfirm, dismissAlert, setHomeDensity, setHomeWorkspace,
  restoreHomePreferences,
} = RegisSpa.core;

function rowKeyShouldNavigate(row, target, key) {
  if (!row || !['Enter', ' '].includes(key)) return false;
  const nestedInteractive = target !== row
    && Boolean(target?.closest?.('a,button,input,select,textarea,[role="button"],[role="link"]'));
  return !nestedInteractive;
}

function installMobileNavigation() {
  const media = window.matchMedia('(max-width:760px)');
  const sidebar = $('#sidebar'), button = $('#mobileNavButton'), scrim = $('#mobileNavScrim');
  if (!sidebar || !button || !scrim) {
    console.warn('[navigation] 页面模板版本较旧，请刷新以加载完整导航。');
    return;
  }
  function setOpen(open) {
    const mobile = media.matches;
    sidebar.hidden = mobile && !open;
    scrim.hidden = !mobile || !open;
    button.setAttribute('aria-expanded', String(mobile && open));
    $('#sidebarToggle').setAttribute('aria-label', mobile ? '关闭功能导航' : '折叠侧栏');
  }
  button.addEventListener('click', () => { setOpen(true); $('#sidebarToggle').focus(); });
  const close = () => { setOpen(false); if (media.matches) button.focus(); };
  $('#sidebarToggle').addEventListener('click', close);
  scrim.addEventListener('click', close);
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && media.matches) close(); });
  $('#sidebarNav').addEventListener('click', event => {
    if (media.matches && event.target.closest('[data-nav]')) setOpen(false);
  });
  media.addEventListener('change', () => setOpen(false));
  setOpen(false);
}

function bindDelegatedActions() {
  document.addEventListener('click', event => {
    const target = event.target.closest('[data-action]');
    if (target) {
      const action = target.dataset.action;
      if (action === 'refresh-health') fetchDataInfrastructureStatus();
      else if (action === 'home-workspace') setHomeWorkspace(target.dataset.workspace);
      else if (action === 'home-density') setHomeDensity(target.dataset.density);
      else if (action === 'home-layout-toggle') {
        if (state.page !== 'dashboard') navigate('dashboard');
        requestAnimationFrame(() => RegisSpa.home.toggleHomeLayoutEditing());
      }
      else if (action === 'toggle-home-why') {
        const details = $('#homeWhyDetails');
        if (details) {
          details.hidden = !details.hidden;
          target.setAttribute('aria-expanded', String(!details.hidden));
          target.textContent = details.hidden ? '为什么变化' : '收起依据';
        }
      }
      else if (action === 'record-open') openRecordModal();
      else if (action === 'pick-record') pickRecord('', target.dataset.name || '');
      else if (action === 'sellable-inventory') window.openSellableInventoryModal(Number(target.dataset.id), target.dataset.name || '', Number(target.dataset.shares || 0), target.dataset.sellable === '' ? null : Number(target.dataset.sellable), target.dataset.asOf || '');
      else if (action === 'sell-mini') window._timingSellMini(Number(target.dataset.id), target.dataset.price === '' ? null : Number(target.dataset.price), {shares:Number(target.dataset.shares || 0), refresh:target.dataset.refresh === 'positions' ? fetchPositionsPanel : fetchTimingPositionsMini});
      else if (action === 'delete-position') window.delPos(Number(target.dataset.id), target.dataset.name || '该持仓');
      else if (action === 'copy-review') copyReviewReport();
      else if (action === 'exclude-stock') window._excludeStock(target, target.dataset.code || '');
      else if (action === 'unexclude-stock') window._unexcludeStock(target.dataset.code || '');
      else if (action === 'switch-flow') window._switchFlowDir(target.dataset.fd, target);
      else if (action === 'scan-preset') window._scanPreset(target.dataset.preset, target);
      else if (action === 'scan-run') window._doScan();
      else if (action === 'load-risk-evidence') {
        const riskPage = RegisSpa.pages.risk;
        if (!riskPage || typeof riskPage.loadEvidence !== 'function') throw new Error('Regis SPA risk module is not registered');
        riskPage.loadEvidence();
      }
      else if (action === 'risk-sizing') window._fetchSizing();
      else if (action === 'risk-profile') window._setEngineProfile(target.dataset.prof);
      else if (action === 'alert-confirm' && state.alertItem) openAlertConfirm(state.alertItem);
      else if (action === 'alert-dismiss' && state.alertItem) dismissAlert(state.alertItem);
      return;
    }
    const row = event.target.closest('[data-href]');
    if (!row || event.target.closest('a,button,input,select,textarea')) return;
    const href = row.dataset.href || '';
    if (href.startsWith('/')) window.location.href = href;
  });
  document.addEventListener('keydown', event => {
    const row = event.target.closest('[data-href]');
    if (rowKeyShouldNavigate(row, event.target, event.key)) {
      event.preventDefault();
      const href = row.dataset.href || '';
      if (href.startsWith('/')) window.location.href = href;
      return;
    }
    const tab = event.target.closest('[role="tab"]');
    if (!tab || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    const tabs = [...tab.parentElement.querySelectorAll('[role="tab"]')];
    const index = tabs.indexOf(tab);
    const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1] : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    event.preventDefault();
    next.focus();
    next.click();
  });
}

// ═══════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════
function init() {
  restoreHomePreferences();
  buildSidebar();
  installMobileNavigation();
  bindDelegatedActions();
  startClock();

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      abortPageRequests(state.page);
      if (state.page === 'dashboard') stopOpportunityMonitor();
      if (state.page === 'signals' && window.__regisSpaPortfolioMonitorOwner === 'signals') {
        setSpaPortfolioMonitorOwner(null);
      }
      return;
    }
    if (state.page === 'dashboard') startOpportunityMonitor();
    if (state.page === 'signals') setSpaPortfolioMonitorOwner('signals');
  });

  // Canonical desktop paths; accept old root hashes once and immediately normalize them.
  const wantsRiskEvidence = window.location.pathname === '/risk'
    && new URLSearchParams(window.location.search).get('view') === 'evidence';
  const accountEntryParams = new URLSearchParams(window.location.search);
  const wantsAccountEntry = window.location.pathname === '/'
    && accountEntryParams.get('account') === 'record';
  const accountEntryCode = /^(0|3|6)\d{5}$/.test(accountEntryParams.get('code') || '')
    ? accountEntryParams.get('code') : '';
  let hash = window.location.hash.replace('#', '');
  if (hash === 'backtest') hash = 'strategy-lab';
  const routePage = {
    '/next-session-plan': 'plan', '/review': 'review',
    '/signals': 'signals', '/strategy-lab': 'strategy-lab', '/fund-flow': 'fundflow', '/scanner': 'scanner',
    '/backtest': 'strategy-lab', '/risk': 'risk',   // P5：/backtest → 统一策略开发
    '/global-radar': 'globalradar', '/market-panorama': 'panorama',
  }[window.location.pathname];
  const hashPage = window.location.pathname === '/' ? NAV_ITEMS.find(n => n.id === hash) : null;
  const validPage = hashPage || NAV_ITEMS.find(n => n.id === routePage);
  if (validPage) {
    // Review date and clock are part of the saved view, including on reload.
    const reviewQuery = validPage.id === 'review' ? window.location.search : '';
    history.replaceState({page:validPage.id}, '', PAGE_PATHS[validPage.id] + reviewQuery);
    // dashboard 视图默认 active，直接渲染（state.page 初值即 dashboard，navigate 会早退）
    if (validPage.id === 'dashboard') {
      renderDashboard();
    } else {
      navigate(validPage.id, false);
    }
  } else {
    history.replaceState({page:'dashboard'}, '', '/');
    renderDashboard();
  }
  updateNavActive();
  updatePageTitle();
  if (wantsRiskEvidence) {
    const loadWhenRendered = () => {
      if (state.page !== 'risk' || !document.getElementById('systemEvidenceBody')) {
        window.setTimeout(loadWhenRendered, 50);
        return;
      }
      const riskPage = RegisSpa.pages.risk;
      if (!riskPage || typeof riskPage.loadEvidence !== 'function') {
        throw new Error('Regis SPA risk module is not registered');
      }
      riskPage.loadEvidence();
      document.getElementById('systemEvidenceBody')?.scrollIntoView({block:'start'});
    };
    loadWhenRendered();
  }
  if (wantsAccountEntry) {
    window.setTimeout(() => {
      Promise.resolve(window.openRecordModal()).then(() => {
        if (accountEntryCode) window.pickRecord('', accountEntryCode);
      }).catch(error => reportClientIssue('account-entry-deep-link', error));
    }, 0);
  }
  const footerStatus = $('#footerStatus');
  if (footerStatus) footerStatus.textContent = window.location.port ? `本地端口 ${window.location.port}` : '本地服务';

  // Premium：首屏卡片错峰入场（含异步填充后的补扫）
  staggerPasses(document.querySelector('.page-view.active') || document);

  // Browser back/forward follows the same path table as direct deep links.
  window.addEventListener('popstate', event => {
    const page = event.state?.page || Object.keys(PAGE_PATHS).find(id => PAGE_PATHS[id] === window.location.pathname) || 'dashboard';
    if (page !== state.page) navigate(page, false);
  });

  // 窗口缩放 → 所有存活图表统一 resize
  let _rsT;
  window.addEventListener('resize', () => {
    clearTimeout(_rsT);
    _rsT = setTimeout(resizeAllCharts, 150);
  });
}

// Start
document.addEventListener('DOMContentLoaded', init);

})(window.RegisSpa);
