(function () {
  'use strict';
  const el = id => document.getElementById(id);
  const escape = value => { const node = document.createElement('span'); node.textContent = value == null ? '' : String(value); return node.innerHTML; };
  let library = null;
  let draft = null;
  let busy = false;
  let stockRequest = null;
  function feedback(message) { el('pbFeedback').textContent = message; }
  function lock(value) {
    busy = value;
    ['pbSave', 'pbPreview', 'pbLoadTemplate'].forEach(id => { el(id).disabled = value || !library; });
    el('pbSaved').querySelectorAll('button').forEach(button => { button.disabled = value; });
  }
  function routeHTML(row) {
    return '<article class="slab-playbook"><h4>' + escape(row.name) + '</h4><p><strong>' + escape(row.label) + '</strong></p>'
      + '<p data-pb-timing-label>' + escape(row.timing_label || '时点协议待核验') + '</p><p>' + escape(row.timing_description || '') + '</p>'
      + '<p>' + escape(row.reasons.join('；')) + '</p>'
      + (row.sectors.length ? '<p>匹配板块：' + row.sectors.map(s => escape(s.name)).join('、') + '</p>' : '')
      + '<p>' + escape(row.next_action) + '</p><small>个股触发未检查 · 条件收益未验证 · 不生成买卖数量</small></article>';
  }
  const planHTML = plan => window.RegisResearchPlan?.html(plan) || '';
  function load(spec) {
    draft = structuredClone(spec);
    el('pbMechanism').value = draft.base_playbook_id;
    el('pbName').value = draft.name;
    el('pbHypothesis').value = draft.hypothesis;
    const mechanism = library.mechanisms.find(row => row.id === draft.base_playbook_id);
    const entry = mechanism.kind === 'entry_research';
    draft.timing_protocol = draft.timing_protocol || (entry ? 'live_mainline_next_open' : 'current_risk_observation');
    el('pbTiming').innerHTML = library.timing_protocols.filter(row => (row.kind === 'entry_research') === entry)
      .map(row => '<option value="' + row.id + '">' + escape(row.name) + '</option>').join('');
    el('pbTiming').value = draft.timing_protocol;
    timingDetails();
    el('pbMechanismDetails').innerHTML = '<p>' + escape(mechanism.characteristic) + '</p><dl>'
      + [['入场触发', mechanism.entry], ['禁止条件', mechanism.forbidden], ['风险线', mechanism.stop],
        ['退出', mechanism.exit], ['退出优先级', mechanism.exit_priority.join(' → ')],
        ['失效', mechanism.invalidation], ['加减仓', mechanism.additions], ['成交时点', mechanism.timing],
        ['关联研究家族', mechanism.related_rejected_family]].map(([name, text]) => '<dt>' + name + '</dt><dd>' + escape(text) + '</dd>').join('') + '</dl>';
    const options = (rows, selected, kind) => rows.map(row => '<label><input type="checkbox" data-pb-' + kind + ' value="' + row.id + '"'
      + (selected.includes(row.id) ? ' checked' : '') + '> ' + escape(row.name) + '</label>').join('');
    el('pbPhases').innerHTML = options(library.phases.filter(row => !entry || row.id !== 'unknown'), draft.emotion_phases, 'phase');
    el('pbStages').innerHTML = options(library.mainline_stages.filter(row => row.id !== 'input_incomplete'), draft.mainline_stages, 'stage');
    el('pbStageField').disabled = el('pbFactorField').disabled = !entry;
    el('pbFactors').innerHTML = library.factors.map(row => {
      const filter = draft.factor_filters.find(f => f.factor_id === row.id);
      return '<div class="slab-field" data-pb-factor="' + row.id + '"><label><input type="checkbox" data-pb-enabled' + (filter ? ' checked' : '') + '> '
        + escape(row.name) + '</label><select aria-label="' + escape(row.name) + '比较方向"><option value="gte">不低于</option><option value="lte"'
        + (filter && filter.operator === 'lte' ? ' selected' : '') + '>不高于</option></select> <input type="number" step="any" min="-1000" max="1000" aria-label="'
        + escape(row.name) + '阈值" value="' + (filter ? filter.threshold : '') + '"><small>' + escape(row.formula) + '；使用公式原始单位，不填百分数显示值。</small></div>';
    }).join('');
    el('pbVersion').textContent = draft.parent_id ? '从已保存版本修订；本次使用当前规则，保存将新增版本，原件保留。' : '新规格；同一规则改名不产生独立样本或收益资格。';
  }
  function collect() {
    if (!draft) throw new Error('请先载入一个机制模板。');
    const spec = structuredClone(draft);
    spec.name = el('pbName').value;
    spec.hypothesis = el('pbHypothesis').value;
    spec.timing_protocol = el('pbTiming').value;
    spec.emotion_phases = Array.from(el('pbPhases').querySelectorAll('input:checked'), node => node.value);
    const entry = library.mechanisms.find(row => row.id === draft.base_playbook_id).kind === 'entry_research';
    spec.mainline_stages = entry ? Array.from(el('pbStages').querySelectorAll('input:checked'), node => node.value) : [];
    spec.factor_filters = entry ? Array.from(el('pbFactors').querySelectorAll('[data-pb-factor]')).filter(row => row.querySelector('[data-pb-enabled]').checked).map(row => {
      const input = row.querySelector('input[type="number"]');
      if (!input.value.trim() || !Number.isFinite(Number(input.value))) throw new Error('启用因子条件后必须填写有限数值阈值。');
      return {factor_id: row.dataset.pbFactor, operator: row.querySelector('select').value, threshold: Number(input.value)};
    }) : [];
    return spec;
  }
  async function request(url, payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload), signal: controller.signal});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '请求失败，请保留当前编辑内容并重试。');
      return result;
    } finally { clearTimeout(timer); }
  }
  async function act(save) {
    if (busy) return;
    try {
      const spec = collect(); lock(true); feedback(save ? '正在保存研究版本…' : '正在核对当前环境…');
      const result = await request('/api/strategy-lab/playbook-specs' + (save ? '' : '/preview'), spec);
      if (save) {
        feedback('已保存：' + result.record.spec.name + '。未启动回测，未改变交易权限。');
        const response = await fetch('/api/strategy-lab/development-catalog', {cache: 'no-store'});
        if (!response.ok) throw new Error('版本已保存，但列表刷新失败；可刷新页面核对，重试相同规格不会重复保存。');
        render((await response.json()).emotion_system.playbook_development);
      } else feedback(result.route.label + '。' + result.route.reasons.join('；') + ' ' + result.route.next_action);
    } catch (error) {
      feedback(error.name === 'AbortError' ? '请求超时，保存结果尚未确认；重试相同规格不会覆盖或重复创建原件。' : error.message);
    } finally { lock(false); }
  }
  function render(data) {
    if (!el('slabPlaybookDevelopment')) return;
    if (stockRequest) { stockRequest.abort(); stockRequest = null; }
    el('pbStockResult').textContent = '';
    if (!data || data.schema !== 'playbook_development.v1' || data.production_eligible !== false) {
      library = null; lock(false); el('pbCurrentRoutes').textContent = '战法研发输入不可用，旧匹配结论已清除；编辑内容保留。';
      el('pbSaved').textContent = '已保存版本暂不可核验。'; el('pbLegacy').textContent = ''; return;
    }
    library = data;
    el('pbCurrentRoutes').innerHTML = data.current_routes.map(routeHTML).join('');
    const previousSelection = el('pbMechanism').value;
    el('pbMechanism').innerHTML = data.mechanisms.map(row => '<option value="' + row.id + '">' + escape(row.name) + '</option>').join('');
    if (previousSelection) el('pbMechanism').value = previousSelection;
    const archive = data.archive;
    const savedRoutes = new Map(data.saved_routes.map(row => [row.spec_id, row]));
    el('pbSaved').innerHTML = '<p>已保存 ' + archive.total + ' 个版本' + (archive.truncated ? '，展示最近100个' : '') + '。'
      + (archive.invalid_ids.length ? archive.invalid_ids.length + ' 个原件校验失败，不能使用。' : '') + '</p>'
      + archive.records.map(record => '<article class="slab-playbook"><strong>' + escape(record.spec.name) + '</strong><p>' + escape(record.created_at)
        + '</p><p>' + escape(data.timing_protocols.find(row => row.id === record.spec.timing_protocol)?.name || '旧版本：盘中主线观察口径，未自动切换为尾盘协议')
        + ' · 研究规格已保存，收益未验证</p><p>版本 ' + record.spec_id.slice(0, 12) + ' · 规则 ' + record.rule_identity.slice(0, 12)
        + '</p><p data-pb-saved-route>' + escape(savedRoutes.get(record.spec_id)?.label || '当前适用性待核验')
        + '。' + escape((savedRoutes.get(record.spec_id)?.reasons || []).join('；'))
        + '</p><button type="button" class="slab-btn" data-pb-load="' + record.spec_id + '">载入为新版本</button> '
        + '<button type="button" class="slab-btn" data-pb-stock="' + record.spec_id + '">检查这只股票</button></article>').join('');
    el('pbLegacy').innerHTML = '<h4>旧战法状态</h4>' + (data.legacy.status !== 'verified_source' ? '<p>原退役协议尚未核验，不提供旧战法克隆。</p>'
      : data.legacy.entries.map(row => '<p><strong>' + escape(row.name) + '</strong>：' + escape(row.reason) + '</p>').join(''));
    if (!draft) load(data.templates[0]);
    lock(busy);
    window.dispatchEvent(new CustomEvent('regis-playbook-catalog', {detail: {catalog: data.catalog_sha256}}));
  }
  window.RegisPlaybookDevelopment = {render, checkStock, planHTML};
  async function checkStock(specId) {
    const output = el('pbStockResult');
    if (!library) { output.textContent = '请等待战法目录完成核验后再查看个股。'; return; }
    const code = el('pbStockCode').value.trim();
    if (!/^[0-9]{6}$/.test(code)) { output.textContent = '请先填写六位股票代码。'; return; }
    if (stockRequest) stockRequest.abort();
    const controller = new AbortController(); stockRequest = controller;
    const timer = setTimeout(() => controller.abort(), 20000);
    output.textContent = '正在绑定这个版本与个股日线、成分和因子原件…';
    try {
      const response = await fetch('/api/strategy-lab/playbook-specs/' + specId + '/stocks/' + code,
        {cache: 'no-store', signal: controller.signal});
      const row = await response.json();
      if (!response.ok) throw new Error(row.error || '个股证据暂不可核验。');
      if (row.schema !== 'playbook_stock_check.v1' || row.spec_id !== specId || row.stock_code !== code || row.production_eligible !== false) throw new Error('个股检查响应身份不一致，旧结果已清除。');
      if (stockRequest !== controller) return;
      const price = row.price;
      const names = new Map(library.factors.map(f => [f.id, f.name]));
      names.set('price_shape', '价格形态');
      output.innerHTML = '<article class="slab-playbook"><h4>' + escape(row.spec_name) + ' · ' + escape(code) + '</h4><p>' + escape(row.label)
        + '</p><p><strong>' + escape(row.joint_research_label || '联合条件尚未核验') + '</strong>'
        + '</p><p>' + escape(row.timing_label || '时点协议待核验')
        + '</p><p>检查时点：' + escape(row.checked_at) + '</p><p>环境：' + escape(row.environment.status === 'environment_matched_shape_pending' ? '情绪与主线阶段匹配' : row.environment.label) + '。' + escape(row.environment.reasons.join('；')) + '</p>'
        + (row.membership ? '<p>成分：' + escape(row.membership.label) + ' ' + row.membership.sectors.map(s => escape(s.sector.name)).join('、')
          + (row.membership.observed_at ? '；成分观察于 ' + escape(row.membership.observed_at) : '') + '</p>' : '')
        + (price ? '<p>日线截至 ' + escape(price.data_as_of) + '；观察可得于 ' + escape(price.available_at) + '；核验 ' + price.checked_rows + ' 行。形态：'
          + (price.shape === true ? '命中' : price.shape === false ? '未命中' : '缺失') + '。</p><dl>'
          + library.factors.map(f => '<dt>' + escape(f.name) + '</dt><dd>' + (Number.isFinite(price.factor_values[f.id]) ? escape(price.factor_values[f.id]) : '未计算或待核验') + '</dd>').join('') + '</dl>'
          + '<p>因子使用公式原始单位；展示数值不代表该因子通过收益验证。</p>' : '')
        + (row.conditions ? '<p>未命中条件：' + escape(row.conditions.failed.map(id => names.get(id) || id).join('、') || '无')
          + '；缺失条件：' + escape(row.conditions.missing.map(id => names.get(id) || id).join('、') || '无') + '。</p>' : '')
        + (row.timing ? '<p>有效期：' + escape(row.timing.label) + (row.timing.expires_at ? ' 截止 ' + escape(row.timing.expires_at) : '') + '</p>' : '')
        + (row.entry_terms ? '<p>价格风险核验：' + escape(row.entry_terms.label) + '</p>' : '')
        + planHTML(row.research_plan)
        + row.issues.map(issue => '<p>' + escape(issue) + '</p>').join('')
        + '<small>研究条件检查；实际成交、账户约束及条件收益未验证。不产生交易指令或股数。</small></article>';
    } catch (error) {
      if (stockRequest === controller) output.textContent = error.name === 'AbortError' ? '本次检查超时，未保留旧结果；可以重试。' : error.message;
    } finally { clearTimeout(timer); if (stockRequest === controller) stockRequest = null; }
  }
  el('pbStockCode').addEventListener('input', () => {
    if (stockRequest) stockRequest.abort();
    stockRequest = null; el('pbStockResult').textContent = '';
  });
  function timingDetails() {
    el('pbTimingDetails').textContent = library?.timing_protocols.find(row => row.id === el('pbTiming').value)?.description || '请选择明确的证据时点协议。';
  }
  el('pbTiming').addEventListener('change', timingDetails);
  el('pbLoadTemplate').addEventListener('click', () => {
    if (!library || busy) return;
    load(library.templates.find(row => row.base_playbook_id === el('pbMechanism').value));
    feedback('已载入当前机制模板；请固定适用阶段与机制假设。');
  });
  el('pbPreview').addEventListener('click', () => act(false));
  el('pbSave').addEventListener('click', () => act(true));
  el('pbSaved').addEventListener('click', event => {
    const stockButton = event.target.closest('[data-pb-stock]');
    if (stockButton && library && !busy) { checkStock(stockButton.dataset.pbStock); return; }
    const button = event.target.closest('[data-pb-load]');
    if (!button || !library || busy) return;
    const record = library.archive.records.find(row => row.spec_id === button.dataset.pbLoad);
    if (!record || !library.mechanisms.some(row => row.id === record.spec.base_playbook_id)) { feedback('原机制不可用，不能用另一机制覆盖。'); return; }
    load({...record.spec, parent_id: record.spec_id, catalog_sha256: library.catalog_sha256});
    el('pbEditor').open = true;feedback('已载入为修订草稿，原版本保留；任何规则变化都需要重新验证。');
  });
  lock(false);
}());
