(function () {
  'use strict';
  const model = window.RegisApplicabilityModel;
  const root = document.getElementById('slabApplicabilityMap');
  if (!model || !root) return;

  const search = document.getElementById('slabAppMapSearch');
  const refresh = document.getElementById('slabAppMapRefresh');
  const more = document.getElementById('slabAppMapMore');
  const summary = document.getElementById('slabAppMapSummary');
  const grid = document.getElementById('slabAppMapGrid');
  const status = document.getElementById('slabAppMapStatus');
  const state = { route: null, factors: [], total: null, nextCursor: null,
    factorVersion: '', asOf: '', query: '', busy: false, error: '' };
  let requestId = 0;
  let searchTimer = null;

  function esc(value) {
    const node = document.createElement('span');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }

  function stamp(value) {
    return value ? String(value).replace('T', ' ').slice(0, 19) : '时点未提供';
  }

  function renderRow(row) {
    const cells = row.cells.map(function (cell, index) {
      return '<div class="appmap-cell appmap-' + esc(cell.phase)
        + (cell.current ? ' is-current' : '') + '">'
        + '<span class="appmap-mobile-state">' + esc(model.STATES[index].label) + '</span>'
        + '<span>' + esc(cell.label) + '</span></div>';
    }).join('');
    return '<div class="appmap-row" data-kind="' + esc(row.kind) + '">'
      + '<div class="appmap-identity"><span class="appmap-kind">'
      + (row.kind === 'factor' ? '因子' : '战法') + '</span>'
      + '<details><summary>' + esc(row.name) + '</summary>'
      + '<div class="appmap-detail"><p>' + esc(row.detail || '机制说明未提供') + '</p>'
      + '<p><b>当前限制：</b>' + esc(row.reason) + '</p>'
      + '<p><b>证据身份：</b>' + esc(row.evidence) + '</p>'
      + '<p><b>ID：</b>' + esc(row.id) + ' · <b>版本：</b>' + esc(row.version) + '</p>'
      + '</div></details><small>' + esc(row.family) + ' · ' + esc(row.status) + '</small></div>'
      + cells + '</div>';
  }

  function render() {
    const current = model.currentState(state.route);
    const label = (model.STATES.find(function (row) { return row.id === current; }) || {}).label;
    const playbooks = model.playbooksFromRoute(state.route);
    const playbookLibraryKnown = state.route && state.route.small_capital_playbook_library
      && Array.isArray(state.route.small_capital_playbook_library.playbooks);
    const playbookRows = playbooks.map(function (row) { return model.playbookRow(row, state.route); })
      .filter(function (row) {
        const query = state.query.toLocaleLowerCase();
        return !query || [row.name, row.id, row.family, row.detail].some(function (value) {
          return String(value).toLocaleLowerCase().includes(query);
        });
      });
    const factorRows = state.factors.map(function (row) { return model.factorRow(row, state.route); });
    factorRows.sort(function (a, b) {
      return (a.family + a.name).localeCompare(b.family + b.name, 'zh-CN');
    });
    summary.textContent = '当前市场：' + (label || '未确认')
      + ' · 战法 ' + (playbookLibraryKnown
        ? (state.query ? playbookRows.length + '/' + playbooks.length : playbooks.length) : '待读取')
      + ' · 因子已加载 ' + state.factors.length + '/' + (state.total == null ? '未知' : state.total)
      + ' · 因子库版本 ' + (state.factorVersion || '待读取')
      + ' · 数据时点 ' + stamp(state.asOf);
    status.textContent = state.error || (state.busy ? '正在读取注册库…'
      : '机制声明、历史结果与当前准入分开显示；未提供独立状态效果的格子一律为未验证。');
    status.classList.toggle('is-error', Boolean(state.error));
    grid.innerHTML = '<div class="appmap-head"><span>机制 / 版本</span>'
      + model.STATES.map(function (row) { return '<span>' + esc(row.label) + '</span>'; }).join('')
      + '</div>'
      + (playbookRows.length || factorRows.length
        ? playbookRows.concat(factorRows).map(renderRow).join('')
        : '<p class="appmap-empty">' + (state.busy ? '正在读取适用状态…' : '暂无可核验条目；不显示买入资格。') + '</p>');
    more.hidden = !state.nextCursor;
    more.disabled = state.busy;
    refresh.disabled = state.busy;
  }

  async function loadFactors(reset) {
    if (state.busy && !reset) return;
    const id = ++requestId;
    if (reset) {
      state.query = search.value.trim().slice(0, 200);
      state.factors = [];
      state.total = null;
      state.nextCursor = null;
      state.error = '';
    }
    const cursor = reset ? '0' : state.nextCursor;
    if (cursor == null) return;
    state.busy = true;
    render();
    const controller = new AbortController();
    const timer = setTimeout(function () { controller.abort(); }, 10000);
    try {
      const params = new URLSearchParams({ view: 'compact', scope: 'all',
        limit: '100', cursor: cursor, query: state.query });
      const response = await fetch('/api/factor-system/factors?' + params,
        { cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' } });
      const payload = await response.json();
      if (!response.ok || payload.ok !== true || !Array.isArray(payload.factors)) {
        throw new Error(payload.error || '因子注册库读取失败');
      }
      if (id !== requestId) return;
      state.factors = state.factors.concat(payload.factors);
      const total = Number((payload.summary || {}).filtered_total);
      state.total = Number.isFinite(total) ? total : null;
      state.nextCursor = (payload.page || {}).next_cursor || null;
      state.factorVersion = String(payload.version || '未提供');
      state.asOf = payload.as_of || '';
      state.error = '';
    } catch (error) {
      if (id === requestId) state.error = '注册库读取失败：' + (error.name === 'AbortError' ? '请求超时' : error.message);
    } finally {
      clearTimeout(timer);
      if (id === requestId) { state.busy = false; render(); }
    }
  }

  refresh.addEventListener('click', function () { loadFactors(true); });
  more.addEventListener('click', function () { loadFactors(false); });
  search.addEventListener('input', function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { loadFactors(true); }, 350);
  });
  window.RegisApplicabilityMap = {
    updateRoute: function (route) {
      state.route = route && typeof route === 'object' ? route : null;
      render();
    },
  };
  render();
  loadFactors(true);
})();
