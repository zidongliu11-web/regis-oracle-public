/* Supplementary manual time/fee facts never overwrite original fills. */
(function () {
  'use strict';
  let active = null;
  async function request(method, body, isFee = false) {
    const response = await fetch(isFee ? '/api/timing/fill-fees' : '/api/timing/fill-times', {method, cache:'no-store', signal:AbortSignal.timeout(10000),
      ...(body ? {headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  }
  async function open(isFee = false) {
    if (active || window.RegisAccess?.canWrite !== true) return;
    const dialog = document.createElement('dialog');
    dialog.className = 'fill-time-dialog';
    dialog.setAttribute('aria-labelledby', 'fillTimeTitle');
    dialog.innerHTML = `<form><h2 id="fillTimeTitle">${isFee ? '补记 / 更正实际费用' : '补记 / 更正实际成交时间'}</h2>
      <p>${isFee ? '保留原始成交与全部费用更正历史。请同时核对当前券商可用现金；保存的是你明确填写的现金快照，不按费用差额重复扣款。不改持仓或提醒资格。' : '只修改复盘时间事实，保留原始录入时间与全部更正历史。不改现金、持仓、可卖数量、费用或提醒状态；不证明策略合格。'}</p>
      <label>选择已记录成交<select name="fill" required disabled></select></label>
      <p data-fact-info></p><details><summary>查看${isFee ? '费用' : '时间'}更正历史</summary><div data-fact-history></div></details>
      <label>${isFee ? '该笔成交实际总费用（人民币元，含佣金及相关税费）' : '券商实际成交时间（上海时间）'}<input name="executed" type="${isFee ? 'number' : 'datetime-local'}" step="${isFee ? '0.01' : '1'}" ${isFee ? 'min="0" max="100000"' : ''} required></label>
      ${isFee ? '<label>当前券商真实可用现金（人民币元，不是费用差额）<input name="cash" type="number" min="0" max="10000000000" step="0.01" required></label>' : ''}
      <label>核对依据 / 更正理由<input name="note" maxlength="256" required placeholder="例如：按券商成交明细核对；不要填写账户号或密码"></label>
      <p role="status" data-fact-status>正在读取原始成交…</p>
      <div class="fill-time-actions"><button type="button" data-close>取消</button><button type="submit" data-server-write disabled>${isFee ? '保存实费并确认现金' : '保存时间事实'}</button></div></form>`;
    const form = dialog.querySelector('form'), select = form.elements.fill;
    const status = dialog.querySelector('[data-fact-status]'), save = form.querySelector('[type="submit"]');
    const trigger = document.activeElement;
    active = dialog;
    document.body.append(dialog);
    dialog.addEventListener('close', () => {active = null; dialog.remove(); trigger?.focus();}, {once:true});
    dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
    dialog.showModal();
    let rows = [], attempt = null, saving = false, accountIdentity = null;
    dialog.addEventListener('cancel', event => {if (saving) event.preventDefault();});
    const selected = () => rows.find(row => row.fill_id === select.value);
    function display() {
      const row = selected();
      form.elements.executed.value = '';
      form.elements.note.value = '';
      if (isFee) form.elements.cash.value = '';
      attempt = null;
      dialog.querySelector('[data-fact-info]').textContent = row ? (isFee
        ? `原始费用：${row.original_fee_yuan ?? '缺失'} 元；${row.original_fee_source === 'actual_user_recorded' ? '用户实录' : '估算或来源待核验'}。最新补记：${row.fact?.fee_yuan ?? '未补记'} 元；不是券商独立核验。买入费用按原始买量分摊，不是逐笔卖出重复收取。`
        : `原始录入：${row.recorded_at || '缺失'}；已补实际成交时间：${row.fact?.executed_at || '未补记'}。依据为用户填报，未接券商回执核验。`) : '暂无可补记的原始成交；持仓快照和旧无事件平仓不能冒充原始成交。';
      const history = dialog.querySelector('[data-fact-history]');
      history.replaceChildren(...(row?.history || []).map(event => {
        const p = document.createElement('p');
        p.textContent = `版本 ${event.id} · ${isFee ? '实费 ' + event.fee_yuan + ' 元' : '成交 ' + event.executed_at} · 补记 ${event.recorded_at} · ${event.note}`;
        return p;
      }));
      save.disabled = !row;
    }
    select.addEventListener('change', display);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!form.reportValidity() || save.disabled || window.RegisAccess?.canWrite !== true) return;
      const row = selected();
      if (!row) return;
      const payload = {fill_id:row.fill_id, note:form.elements.note.value.trim(), expected_event_id:row.fact?.id || 0,
        ...(isFee ? {fee_yuan:form.elements.executed.value, remaining_cash_yuan:form.elements.cash.value, expected_cash_identity:accountIdentity}
          : {executed_at:form.elements.executed.value + '+08:00'})};
      const fingerprint = JSON.stringify(payload);
      if (!attempt || attempt.fingerprint !== fingerprint) attempt = {fingerprint, key:crypto.randomUUID()};
      save.disabled = true;
      select.disabled = true;
      saving = true;
      dialog.querySelector('[data-close]').disabled = true;
      status.textContent = isFee ? '正在保存实费与明确现金快照…' : '正在保存时间事实…';
      try {
        const result = await request('POST', {...payload, idempotency_key:attempt.key}, isFee);
        row.fact = result.event;
        if (isFee) accountIdentity = result.event.account_identity;
        if (!row.history.some(item => item.id === result.event.id)) row.history.push(result.event);
        display();
        status.textContent = isFee ? (result.event.idempotent_replay ? '原实费记录已确认，未重复改写现金；当前现金以账户中心为准。复盘正在重新读取。' : '实费历史已保存，现金已按你填写的当前数值确认；原成交与持仓未改动。复盘正在重新读取。') : '时间事实已保存；原始录入时间与账户余额未改动。复盘正在重新读取。';
        window.dispatchEvent(new CustomEvent('regis-fill-time-recorded'));
      } catch (error) {
        status.textContent = `保存未确认：${error.message}。超时可用相同输入重试；版本冲突请关闭后重新打开核对，不要假定已保存。`;
      } finally {
        saving = false;
        dialog.querySelector('[data-close]').disabled = false;
        save.disabled = window.RegisAccess?.canWrite !== true;
        select.disabled = false;
      }
    });
    try {
      const data = await request('GET', null, isFee);
      if (!dialog.isConnected) return;
      rows = data.items || [];
      accountIdentity = data.account_identity || null;
      rows.forEach(row => {
        const option = document.createElement('option');
        option.value = row.fill_id;
        option.textContent = `${row.fill_id.startsWith('buy:') ? '买入' : '卖出'} · ${row.name || row.code} · ${row.shares ?? '未知'}股 @ ${row.price ?? '未知'} · 录入 ${row.recorded_at || '未知'} · ${row.fill_id}`;
        select.append(option);
      });
      select.disabled = !rows.length;
      display();
      status.textContent = rows.length ? (isFee ? '请核对该笔实际总费用与当前真实可用现金；空白不等于零。' : '请按已发生的成交明细填写，不以录入时间代填。') : '没有可补记的原始成交。';
    } catch (error) {status.textContent = `成交读取失败：${error.message}；请关闭后重试。`;}
  }
  document.addEventListener('click', event => {
    const trigger = event.target.closest('[data-fill-time-open],[data-fill-fee-open]');
    if (trigger) {event.preventDefault(); void open(trigger.hasAttribute('data-fill-fee-open'));}
  });
})();
