(() => {
  'use strict';

  const state = { radar: null, dashboard: null, filter: '全部', refreshTimer: null };
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const signed = (value) => `${Number(value || 0) >= 0 ? '+' : ''}${Number(value || 0).toFixed(2)}%`;

  function setLoading(loading) {
    $('#refreshButton').disabled = loading;
    $('#refreshButton').textContent = loading ? '更新中…' : '刷新雷达';
  }

  function renderIndices(indices) {
    const host = $('#indexStrip');
    host.innerHTML = indices.length ? indices.map(item => `
      <div class="index-card ${Number(item.change_pct) >= 0 ? 'up' : 'down'}">
        <span>${esc(item.name)}</span><b>${item.price ?? '--'}</b><em>${signed(item.change_pct)}</em>
      </div>`).join('') : '<div class="metric-empty">指数行情暂不可用</div>';
  }

  function renderEvents() {
    const host = $('#eventList');
    const empty = $('#eventEmpty');
    const items = (state.radar?.events || []).filter(item => state.filter === '全部' || item.priority === state.filter || item.impact === state.filter);
    host.innerHTML = '';
    empty.hidden = items.length !== 0;
    for (const item of items) {
      const fragment = $('#eventTemplate').content.cloneNode(true);
      const card = fragment.querySelector('.event-card');
      card.classList.add(`impact-${item.impact}`);
      fragment.querySelector('.event-source').textContent = item.source;
      fragment.querySelector('.event-time').textContent = `${item.collected_at} 收集`;
      const priority = fragment.querySelector('.event-priority');
      priority.textContent = `${item.priority}影响`;
      priority.classList.add(`priority-${item.priority}`);
      const link = fragment.querySelector('.event-title');
      link.href = item.url;
      link.textContent = item.title;
      fragment.querySelector('.event-impact').textContent = `规则判断：${item.impact}`;
      fragment.querySelector('.event-sectors').textContent = item.sectors.join(' / ');
      fragment.querySelector('.event-reason').textContent = item.reason;
      host.appendChild(fragment);
    }
  }

  function renderSectors(sectors) {
    const host = $('#sectorList');
    const max = Math.max(...sectors.map(item => Math.abs(Number(item.change_pct) || 0)), 1);
    host.innerHTML = sectors.length ? sectors.map((item, index) => {
      const value = Number(item.change_pct || 0);
      return `<div class="sector-row ${value >= 0 ? 'up' : 'down'}">
        <span class="sector-rank">${String(index + 1).padStart(2, '0')}</span>
        <span class="sector-name">${esc(item.name)}</span>
        <span class="sector-bar"><i style="width:${Math.min(100, Math.abs(value) / max * 100)}%"></i></span>
        <b>${signed(value)}</b>
      </div>`;
    }).join('') : '<div class="metric-empty">板块热度暂不可用</div>';
  }

  function renderSources(sources) {
    $('#sourceList').innerHTML = sources.map(item => `<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer" class="source-row"><span class="source-light ${item.status === '在线' ? 'online' : 'offline'}"></span><span>${esc(item.name)}</span><b>${esc(item.status)}</b></a>`).join('');
  }

  function renderRadar(data) {
    state.radar = data;
    $('#liveText').textContent = `实时数据 · ${data.refresh_after_seconds} 秒缓存`;
    $('#radarTime').textContent = data.generated_at;
    $('#marketState').textContent = data.market.state;
    $('#marketAction').textContent = data.market.method;
    $('#momAction').textContent = data.market.action;
    $('#leaderSector').textContent = data.market.leader;
    $('#sectorSource').textContent = data.market_sources?.sectors || '公开行情';
    $('#radarDisclaimer').textContent = data.disclaimer;
    renderIndices(data.indices || []);
    renderEvents();
    renderSectors(data.sectors || []);
    renderSources(data.sources || []);
  }

  function renderWatchlist(data) {
    const host = $('#stockList');
    const date = data.snapshot_date || '未知日期';
    $('#snapshotNote').textContent = `快照日期：${date} · 页面打开时间：${data.date}`;
    const rows = data.recommendations || [];
    host.innerHTML = rows.length ? rows.map(item => `
      <article class="watch-card">
        <div><p class="watch-code">${esc(item.code)}</p><h3>${esc(item.name)}</h3></div>
        <div class="watch-rank">历史排名 <b>${esc(item.rank)}</b></div>
        <p>${esc(item.flow_text || '暂无实时资金流')}</p>
        <a href="/analysis?code=${encodeURIComponent(item.code)}">查看研究信息</a>
      </article>`).join('') : '<div class="metric-empty">暂无历史观察名单</div>';
  }

  async function refresh() {
    setLoading(true);
    try {
      const [radarResponse, dashboardResponse] = await Promise.all([fetch('/api/mom/radar'), fetch('/api/mom/dashboard')]);
      if (!radarResponse.ok || !dashboardResponse.ok) throw new Error('数据源暂不可用');
      const [radar, dashboard] = await Promise.all([radarResponse.json(), dashboardResponse.json()]);
      state.dashboard = dashboard;
      renderRadar(radar);
      renderWatchlist(dashboard);
    } catch (error) {
      $('#liveText').textContent = '数据更新失败 · 保留最近一次可见状态';
      $('#eventEmpty').hidden = false;
      $('#eventEmpty').textContent = `无法读取雷达数据：${error.message}`;
    } finally {
      setLoading(false);
    }
  }

  document.querySelectorAll('.filter').forEach(button => button.addEventListener('click', () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll('.filter').forEach(item => item.classList.toggle('is-active', item === button));
    renderEvents();
  }));
  $('#refreshButton').addEventListener('click', refresh);
  refresh();
  state.refreshTimer = window.setInterval(refresh, 60000);
})();
