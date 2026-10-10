/* Retrospective plan association never confirms an alert or rebooks a fill. */
(function () {
  'use strict';
  let active = null;
  async function request(query = '', body = null) {
    const response = await fetch('/api/timing/fill-plans' + query, {method:body ? 'POST' : 'GET', cache:'no-store', signal:AbortSignal.timeout(10000),
      ...(body ? {headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  }
  const describe = snapshot => {
    const action = snapshot?.action_plan?.trade_plan?.action;
    return action ? `${snapshot.action === 'buy' ? '买入' : '卖出'}计划：${action.shares}股，限价 ¥${action.limit_price}；期限 ${action.valid_until}；取消条件：${action.cancel_condition}。` : '没有完整冻结计划。';
  };
  async function open() {
    if (active || window.RegisAccess?.canWrite !== true) return;
    const dialog = document.createElement('dialog');
    dialog.className = 'fill-time-dialog';
    dialog.setAttribute('aria-labelledby', 'fillPlanTitle');
    dialog.innerHTML = `<form><h2 id="fillPlanTitle">核对成交与原计划</h2>
      <p>补记关联保留全部历史，不修改现金、持仓、原始成交或提醒状态。已过期计划可用于复盘，不代表现在仍可执行；不能把后补关联变成当时的策略证据。</p>
      <label>选择已实录成交<select name="fill" required disabled></select></label>
      <p data-current></p><details><summary>查看关联历史与保存的计划</summary><div data-history></div></details>
      <label>原提醒计划（同一证券、同一买卖方向，最近200项）<select name="plan" required disabled></select></label>
      <p data-plan></p><label>核对依据 / 更正理由<input name="note" required maxlength="256" placeholder="请说明原计划与成交的关系；不要填写账户号或密码"></label>
      <p role="status" data-status>正在读取成交…</p><div class="fill-time-actions"><button type="button" data-close>取消</button><button type="submit" disabled>保存核对关系</button></div></form>`;
    const trigger = document.activeElement, form = dialog.querySelector('form');
    const status = dialog.querySelector('[data-status]'), save = form.querySelector('[type=submit]');
    const fillSelect = form.elements.fill, planSelect = form.elements.plan;
    let rows = [], plans = [], generation = 0, attempt = null, saving = false;
    const selected = () => rows.find(row => row.fill_id === fillSelect.value);
    active = dialog;
    document.body.append(dialog);
    dialog.addEventListener('close', () => {generation++; active = null; dialog.remove(); trigger?.focus();}, {once:true});
    dialog.addEventListener('cancel', event => {if (saving) event.preventDefault();});
    dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
    dialog.showModal();
    async function selectFill() {
      const row = selected(), version = ++generation;
      plans = []; attempt = null; planSelect.replaceChildren(); form.elements.note.value = '';
      save.disabled = planSelect.disabled = true;
      dialog.querySelector('[data-plan]').textContent = '';
      dialog.querySelector('[data-current]').textContent = row?.comparison?.label || '暂无原始成交。';
      dialog.querySelector('[data-history]').replaceChildren(...(row?.history || []).map(event => {
        const p = document.createElement('p');
        let summary = '明确取消关联，不删除历史。';
        if (event.plan_snapshot_json) {
          try {summary = describe(JSON.parse(event.plan_snapshot_json));} catch {summary = '保存的计划无法读取。';}
        }
        p.textContent = `版本 ${event.id} · ${event.source === 'confirmation_snapshot' ? '确认时保存' : '用户后补关联'} · ${event.recorded_at} · ${event.note} · ${summary}`;
        return p;
      }));
      if (!row) return;
      status.textContent = '正在读取同证券原计划…';
      try {
        const data = await request('?fill_id=' + encodeURIComponent(row.fill_id));
        if (!dialog.isConnected || version !== generation) return;
        plans = data.plans || [];
        planSelect.add(new Option('请选择，不自动推定原计划', ''));
        planSelect.add(new Option('明确没有关联计划 / 取消误关联', 'none'));
        plans.forEach(plan => planSelect.add(new Option(`#${plan.alert_id} · ${plan.snapshot.name || plan.snapshot.code} · ${describe(plan.snapshot)}`, String(plan.alert_id))));
        planSelect.disabled = false; save.disabled = false;
        status.textContent = plans.length ? '请选择实际参考的原计划，并核对数量、限价与期限。' : '没有完整同证券计划可关联；可以明确记录无关联，不能创建虚构计划。';
      } catch (error) {if (version === generation && dialog.isConnected) status.textContent = `计划读取失败：${error.message}；未保存，请重选成交或关闭后重试。`;}
    }
    fillSelect.addEventListener('change', () => void selectFill());
    planSelect.addEventListener('change', () => {
      attempt = null;
      dialog.querySelector('[data-plan]').textContent = planSelect.value === 'none' ? '只取消当前关系，保留原关联历史。' : describe(plans.find(plan => String(plan.alert_id) === planSelect.value)?.snapshot);
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!form.reportValidity() || saving || save.disabled || window.RegisAccess?.canWrite !== true) return;
      const row = selected(), plan = plans.find(item => String(item.alert_id) === planSelect.value);
      if (!row || (!plan && planSelect.value !== 'none')) return;
      const body = {fill_id:row.fill_id, alert_id:plan?.alert_id ?? null, expected_plan_sha256:plan?.plan_sha256 ?? null,
        expected_event_id:row.fact?.id || 0, note:form.elements.note.value.trim()};
      const fingerprint = JSON.stringify(body);
      if (!attempt || fingerprint !== attempt.fingerprint) attempt = {fingerprint, key:crypto.randomUUID()};
      saving = true; save.disabled = fillSelect.disabled = planSelect.disabled = dialog.querySelector('[data-close]').disabled = true;
      status.textContent = '正在追加关联核对记录…';
      try {
        const result = await request('', {...body, idempotency_key:attempt.key});
        row.fact = result.event;
        if (!row.history.some(item => item.id === result.event.id)) row.history.push(result.event);
        row.comparison = {label:'关系已保存，完整比较结果请查看刷新后的复盘。'};
        await selectFill();
        status.textContent = '关联历史已保存；现金、持仓和提醒状态未改动。复盘正在重新读取。';
        window.dispatchEvent(new CustomEvent('regis-fill-time-recorded'));
      } catch (error) {planSelect.disabled = false; status.textContent = `保存未确认：${error.message}。超时可保持相同输入重试；版本冲突须重新读取，不要假定已保存。`;}
      finally {saving = false; fillSelect.disabled = false; dialog.querySelector('[data-close]').disabled = false;
        save.disabled = planSelect.disabled || window.RegisAccess?.canWrite !== true;}
    });
    try {
      const data = await request();
      if (!dialog.isConnected) return;
      rows = data.items || [];
      rows.forEach(row => fillSelect.add(new Option(`${row.fill_id.startsWith('buy:') ? '买入' : '卖出'} · ${row.name || row.code} · ${row.shares}股 @ ${row.price} · 录入 ${row.recorded_at} · ${row.fill_id}`, row.fill_id)));
      fillSelect.disabled = !rows.length;
      if (rows.length) await selectFill(); else status.textContent = '没有可关联的原始成交；持仓快照不算成交。';
    } catch (error) {status.textContent = `成交读取失败：${error.message}；请关闭后重试。`;}
  }
  document.addEventListener('click', event => {
    if (event.target.closest('[data-fill-plan-open]')) {event.preventDefault(); void open();}
  });
})();
