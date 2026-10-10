(function () {
  'use strict';

  var marketTimer = null;
  var marketController = null;
  var DENSITY_KEY = 'regis-home-density-v1';
  var researchTimer = null;
  var researchController = null;

  async function refreshResearchWorker() {
    var box = document.getElementById('cloudResearchStatus');
    if (!box || document.hidden || researchController) return;
    researchController = new AbortController();
    var timeout = window.setTimeout(function () { researchController.abort(); }, 6000);
    try {
      var response = await fetch('/api/research-worker/status', {cache:'no-store', signal:researchController.signal});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      var data = await response.json();
      var title = document.createElement('strong');
      var cloud = data.execution_role === 'cloud';
      var interactive = data.local_website_mode === 'interactive_only' && !cloud;
      title.textContent = interactive ? '本地网站：交互模式 · 后台研究已停用'
        : (cloud ? '云端' : '本地') + '研究执行器：' + (data.online ? '在线' : '未确认在线');
      var detail = document.createElement('p');
      detail.textContent = interactive ? '页面、查询和手工编辑可使用。当前服务不会启动研究任务或自动采集；历史结果和缺失输入分别核对。' : data.online
        ? (data.current_job || '等待下一轮检查') + (cloud ? '。开发电脑关机或断网不影响此服务。' : '。本地运行依赖这台电脑保持开机。')
        : '暂未取得有效进程与心跳证据，不能宣称后台研究正在运行。';
      var list = document.createElement('ul');
      Object.values(data.jobs || {}).forEach(function (job) {
        var row = document.createElement('li');
        var completion = job.status === 'complete' ? ' · 本轮完成于 ' : ' · 上次完成于 ';
        row.textContent = (job.label || '研究任务') + '：' + (job.summary || '执行中') + (job.finished_at ? completion + new Date(job.finished_at).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'}) : '');
        if (job.factor_finite_counts) {
          var counts = document.createElement('p');
          counts.textContent = '可计算观察数（不代表已验证因子）：' + Object.entries(job.factor_finite_counts).map(function (entry) {
            return entry[0] + ' ' + entry[1];
          }).join(' · ');
          row.appendChild(counts);
        }
        list.appendChild(row);
      });
      var scope = document.createElement('p');
      scope.textContent = data.scope || '';
      box.replaceChildren(title, detail, list, scope);
      window.RegisResearchWorkerStatus = data;
      window.dispatchEvent(new CustomEvent('regis-research-worker-status'));
    } catch (error) {
      box.textContent = '研究执行器状态读取失败；当前是否运行尚未核验。';
      window.RegisResearchWorkerStatus = null;
      window.dispatchEvent(new CustomEvent('regis-research-worker-status'));
    } finally {
      window.clearTimeout(timeout);
      researchController = null;
    }
  }

  function installResearchStatus() {
    if (!['/factor-system', '/strategy-lab'].includes(location.pathname)) return;
    var main = document.getElementById('mainContent') || document.querySelector('main');
    if (!main) return;
    var box = document.createElement('section');
    box.id = 'cloudResearchStatus';
    box.setAttribute('aria-label', '独立研究执行状态');
    box.style.cssText = 'margin:16px 20px;padding:16px;border:1px solid #29485e;border-radius:10px;background:#0d1e2a;line-height:1.6;overflow-wrap:anywhere;';
    main.prepend(box);
    refreshResearchWorker();
    researchTimer = window.setInterval(refreshResearchWorker, 30000);
    document.addEventListener('visibilitychange', function () {
      window.RegisResearchWorkerStatus = null;
      window.dispatchEvent(new CustomEvent('regis-research-worker-status'));
      if (!document.hidden) refreshResearchWorker();
    });
  }

  function applyDensity(value, persist) {
    var density = value === 'compact' ? 'compact' : 'comfortable';
    document.body.dataset.density = density;
    document.querySelectorAll('[data-shell-density]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.shellDensity === density));
    });
    if (persist !== false) {
      try { window.localStorage.setItem(DENSITY_KEY, density); }
      catch (error) { console.warn('[site-shell:density-save]', error.message || error); }
    }
  }

  function installDensityControl() {
    var saved = 'comfortable';
    try { saved = window.localStorage.getItem(DENSITY_KEY) || saved; }
    catch (error) { console.warn('[site-shell:density-load]', error.message || error); }
    applyDensity(saved, false);
    document.querySelectorAll('[data-shell-density]').forEach(function (button) {
      button.addEventListener('click', function () {
        applyDensity(button.dataset.shellDensity, true);
      });
    });
  }

  function shanghaiParts() {
    var parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).formatToParts(new Date());
    return Object.fromEntries(parts.map(function (part) { return [part.type, part.value]; }));
  }

  function updateClock() {
    var parts = shanghaiParts();
    var time = parts.hour + ':' + parts.minute + ':' + parts.second;
    var clock = document.getElementById('navClock');
    var footer = document.getElementById('footerTime');
    if (clock) clock.textContent = time;
    if (footer) footer.textContent = '上海 ' + time;
  }

  function renderMarketSession(payload) {
    var session = payload && payload.session;
    var text = document.getElementById('statusText');
    var dot = document.getElementById('statusDot');
    var label = '状态未知';
    var live = false;
    if (session && session.status !== 'unknown' && typeof session.label === 'string') {
      label = session.label;
      live = session.is_open === true;
    }
    if (text) text.textContent = label;
    if (dot) dot.className = 'status-dot' + (live ? ' live' : '');
  }

  async function refreshMarketSession() {
    if (document.hidden || marketController) return;
    marketController = new AbortController();
    var timeout = window.setTimeout(function () { marketController.abort(); }, 6000);
    try {
      var response = await fetch('/api/market/status', { cache:'no-store', signal:marketController.signal });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      renderMarketSession(await response.json());
    } catch (error) {
      renderMarketSession(null);
      if (error.name !== 'AbortError') console.warn('[site-shell:market-session]', error.message || error);
    } finally {
      window.clearTimeout(timeout);
      marketController = null;
    }
  }

  function scheduleMarketSession() {
    window.clearInterval(marketTimer);
    marketTimer = null;
    if (document.hidden) {
      if (marketController) marketController.abort();
      return;
    }
    refreshMarketSession();
    marketTimer = window.setInterval(refreshMarketSession, 60000);
  }

  function markCurrentNavigation() {
    var path = window.location.pathname;
    document.querySelectorAll('.topnav-link').forEach(function (link) {
      var target = new URL(link.href, window.location.origin).pathname;
      var current = target === path || (path.indexOf('/company/') === 0 && target === '/stock');
      link.classList.toggle('active', current);
      if (current) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  function installMobileNavigation() {
    var button = document.getElementById('legacyMobileNav');
    var links = document.getElementById('legacyNavigation');
    if (!button || !links) return;
    var media = window.matchMedia('(max-width:760px)');
    function setOpen(open) {
      links.hidden = media.matches && !open;
      button.setAttribute('aria-expanded', String(media.matches && open));
      button.closest('nav').dataset.mobileOpen = String(media.matches && open);
    }
    button.addEventListener('click', function () { setOpen(button.getAttribute('aria-expanded') !== 'true'); });
    links.addEventListener('click', function (event) { if (media.matches && event.target.closest('a')) setOpen(false); });
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape' && media.matches && button.getAttribute('aria-expanded') === 'true') { setOpen(false); button.focus(); } });
    media.addEventListener('change', function () { setOpen(false); });
    setOpen(false);
  }

  function start() {
    installResearchStatus();
    markCurrentNavigation();
    installMobileNavigation();
    installDensityControl();
    updateClock();
    window.setInterval(updateClock, 1000);
    scheduleMarketSession();
  }

  document.addEventListener('visibilitychange', scheduleMarketSession);
  window.addEventListener('pagehide', function () {
    window.clearInterval(researchTimer);
    if (researchController) researchController.abort();
    window.clearInterval(marketTimer);
    if (marketController) marketController.abort();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
