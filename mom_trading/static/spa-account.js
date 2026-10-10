/* Regis Oracle SPA — 账户事实与当日可卖库存 */
(function(RegisSpa){
'use strict';
if (!RegisSpa || typeof RegisSpa.core?.apiWrite !== 'function') {
  throw new Error('Regis SPA core must load before account module');
}
const {
  $, api, apiWrite, esc, escAttr, apiError, toast, fetchPositionsPanel,
  fetchResearchMarketState,
  createIdempotencyKey, trapDialog, effectiveRiskLevel, riskLevelMeta,
} = RegisSpa.core;

function shanghaiDateValue() {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en', {
      timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
    }).formatToParts(new Date()).map(part => [part.type, part.value])
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

let _sellableMask = null, _sellableDialogCleanup = null, _sellableAttempt = null;
window.openSellableInventoryModal = function (id, name, shares, priorSellable, priorAsOf) {
  if (window.RegisAccess?.canWrite !== true) { toast(window.RegisAccess?.reason || '写入权限未核实', 'warn'); return; }
  if (_sellableMask) closeSellableInventoryModal();
  const today = shanghaiDateValue();
  const priorDay = String(priorAsOf || '').slice(0, 10);
  const priorValue = Number(priorSellable);
  const priorValid = priorSellable !== null && priorSellable !== undefined && priorSellable !== ''
    && Number.isInteger(priorValue)
    && priorValue >= 0 && priorValue <= Number(shares || 0);
  // A prior snapshot is only a convenience default.  Nothing is persisted
  // until the user compares it with the broker and presses Save.
  const currentValue = priorValid ? String(priorValue) : '';
  const mask = document.createElement('div');
  mask.className = 'timing-modal-mask';
  mask.setAttribute('role', 'dialog');
  mask.setAttribute('aria-modal', 'true');
  mask.setAttribute('aria-labelledby', 'sellableDialogTitle');
  mask.innerHTML = `
    <div class="timing-modal">
      <h3 id="sellableDialogTitle">核验今日可卖库存</h3>
      <div style="color:var(--text-tertiary);font-size:var(--text-xs);margin-bottom:8px">${esc(name || '该持仓')} · 总持仓 ${Number(shares || 0)} 股。请照抄券商“可卖”数；系统不会按录入日期推断 T+1。每个交易日重新核验，休市日记录不会自动沿用到下一交易日</div>
      <label class="sr-only" for="sellableSharesInput">今日可卖股数</label>
      <input type="number" id="sellableSharesInput" min="0" max="${Number(shares || 0)}" step="1" value="${escAttr(currentValue)}" placeholder="0 至 ${Number(shares || 0)}">
      <label class="sr-only" for="sellableAsOfInput">核验日期</label>
      <input type="date" id="sellableAsOfInput" value="${today}" max="${today}">
      ${priorAsOf ? `<div style="color:var(--text-tertiary);font-size:var(--text-xs);margin-bottom:6px">上一快照：${priorSellable == null ? '未知' : Number(priorSellable) + '股'} · ${esc(String(priorAsOf))}${priorDay !== today && priorValid ? '；已预填供核对，未保存前不属于今日事实' : ''}</div>` : ''}
      <div class="modal-actions">
        <button type="button" class="btn btn-plain" id="sellableCancel">取消</button>
        <button type="button" class="btn btn-primary" id="sellableSave">保存核验</button>
      </div>
    </div>`;
  document.body.appendChild(mask);
  _sellableMask = mask;
  _sellableAttempt = null;
  _sellableDialogCleanup = trapDialog(mask, closeSellableInventoryModal);
  mask.addEventListener('click', event => { if (event.target === mask) closeSellableInventoryModal(); });
  $('#sellableCancel').addEventListener('click', closeSellableInventoryModal);
  $('#sellableSave').addEventListener('click', async () => {
    const sellableRaw = (($('#sellableSharesInput') || {}).value || '').trim();
    const sellable = sellableRaw === '' ? NaN : Number(sellableRaw);
    const asOf = (($('#sellableAsOfInput') || {}).value || '').trim();
    if (!Number.isInteger(sellable) || sellable < 0 || sellable > Number(shares || 0)) { toast('今日可卖股数必须是0至总持仓之间的整数', 'error'); return; }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(asOf)) { toast('请选择核验日期', 'error'); return; }
    if (asOf > today) { toast('不能提前记录未来交易日的可卖数量', 'error'); return; }
    const payload = {sellable_shares_snapshot:sellable, sellable_as_of:asOf};
    const fingerprint = JSON.stringify(payload);
    if (!_sellableAttempt || _sellableAttempt.fingerprint !== fingerprint) _sellableAttempt = {fingerprint, key:createIdempotencyKey('sellable-inventory')};
    const button = $('#sellableSave');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const response = await apiWrite(`/api/timing/positions/${Number(id)}/sellable-inventory`, payload, 'PUT', {idempotencyKey:_sellableAttempt.key});
      if (!response._httpOk) { toast(response.error || '可卖库存保存失败', 'error'); return; }
      closeSellableInventoryModal();
      const refreshed = await Promise.all([fetchPositionsPanel(), fetchResearchMarketState()]);
      const summary = ((refreshed[0] || {}).summary || {});
      if (summary.account_facts_complete === true) {
        toast(`已核验可卖 ${sellable} 股；账户事实门禁已补齐并重新参与情绪→板块→战法→仓位链`, 'success');
      } else {
        const gaps = Array.isArray(summary.account_fact_gaps) ? summary.account_fact_gaps.filter(Boolean) : [];
        toast(`${response.message || '可卖库存已更新'}；整套决策链已重新计算${gaps.length ? `，账户仍缺：${gaps.join('、')}` : ''}`, response.current_for_today ? 'success' : 'warn');
      }
    } catch (error) { toast('网络错误，请重试', 'error'); }
    finally { if (button && button.isConnected) button.disabled = false; }
  });
  $('#sellableSharesInput').focus();
};
function closeSellableInventoryModal() {
  if (_sellableDialogCleanup) { _sellableDialogCleanup(); _sellableDialogCleanup = null; }
  if (_sellableMask) { _sellableMask.remove(); _sellableMask = null; }
  _sellableAttempt = null;
}

let _recMask = null, _recDialogCleanup = null, _recordAttempt = null;
window.openRecordModal = openRecordModal;
window.pickRecord = pickRecord;
async function openRecordModal() {
  if (window.RegisAccess?.canWrite !== true) { toast(window.RegisAccess?.reason || '写入权限未核实', 'warn'); return; }
  if (_recMask) closeRecordModal();
  const mask = document.createElement('div');
  mask.className = 'timing-modal-mask';
  mask.setAttribute('role', 'dialog');
  mask.setAttribute('aria-modal', 'true');
  mask.setAttribute('aria-labelledby', 'recordDialogTitle');
  mask.innerHTML = `
    <div class="timing-modal">
      <h3 id="recordDialogTitle">录入账户事实</h3>
      <div style="color:var(--text-tertiary);font-size:var(--text-xs);margin-bottom:8px">空仓时可只保存真实剩余资金；填写股票信息时才录入已有手工持仓。系统不会连接券商或代你下单</div>
      <div style="font-size:var(--text-xs);color:var(--text-tertiary);margin-bottom:6px">可操作机会快速选择：</div>
      <div id="recChips" style="margin-bottom:8px"><span style="font-size:12px;color:var(--text-tertiary)">加载中…</span></div>
      <label class="sr-only" for="recCode">股票名称或 6 位代码</label>
      <input type="text" id="recCode" placeholder="股票名称或 6 位代码（如 赛力斯 / 601127）">
      <label class="sr-only" for="recShares">已有持仓数量</label>
      <input type="number" id="recShares" min="1" step="1" placeholder="已有持仓数量（允许包含零股）">
      <label class="sr-only" for="recSellableShares">券商显示今日可卖股数</label>
      <input type="number" id="recSellableShares" min="0" step="1" placeholder="券商显示今日可卖股数（可为0，不得超过持仓）">
      <label class="sr-only" for="recSellableAsOf">今日可卖库存核验日期</label>
      <input type="date" id="recSellableAsOf" aria-describedby="recSellableHelp">
      <div id="recSellableHelp" style="color:var(--text-tertiary);font-size:var(--text-xs);margin:-3px 0 6px">用于 T+1 与精确卖出数量；不会根据录入时间猜测</div>
      <label class="sr-only" for="recPrice">实际成本价</label><input type="number" id="recPrice" min="0.01" step="0.01" placeholder="券商显示的实际持仓成本价">
      <label class="sr-only" for="recBuyFee">券商实际买入费用</label><input type="number" id="recBuyFee" min="0" step="0.01" placeholder="券商实际买入费用（可选；留空按模型估算）">
      <label class="sr-only" for="recCash">当前剩余可用资金</label><input type="number" id="recCash" min="0" step="0.01" placeholder="券商账户当前剩余可用资金">
      <div class="modal-actions">
        <button type="button" class="btn btn-plain" id="recCancel">取消</button>
        <button type="button" class="btn btn-plain" id="recCashOnly">只保存资金</button>
        <button type="button" class="btn btn-primary" id="recSave">保存持仓和资金</button>
      </div>
    </div>`;
  document.body.appendChild(mask);
  $('#recSellableAsOf').value = shanghaiDateValue();
  _recMask = mask;
  _recDialogCleanup = trapDialog(mask, closeRecordModal);
  mask.addEventListener('click', (event) => { if (event.target === mask) closeRecordModal(); });
  $('#recCancel').addEventListener('click', closeRecordModal);
  $('#recCashOnly').addEventListener('click', submitCashOnly);
  $('#recSave').addEventListener('click', submitRecord);
  try {
    const d = await api('/api/opportunities');
    if (!d) throw new Error('机会数据不可用');
    const chips = (d.items || []).slice(0, 5).map(it =>
      `<button class="record-chip" type="button" data-action="pick-record" data-name="${escAttr(it.name)}">${esc(it.name)}</button>`
    ).join('');
    const container = $('#recChips');
    if (container) container.innerHTML = chips || '<span style="font-size:12px;color:var(--text-tertiary)">暂无达到可操作标准的机会</span>';
  } catch (error) {
    const container = $('#recChips');
    if (container) container.innerHTML = `<span style="font-size:12px;color:var(--gold)" role="status">${esc(apiError('/api/opportunities', '研究观察池暂不可用'))}</span>`;
  }
}
function pickRecord(code, name) {
  const input = $('#recCode');
  if (input) { input.value = name; input.focus(); }
}
function closeRecordModal() {
  if (_recDialogCleanup) { _recDialogCleanup(); _recDialogCleanup = null; }
  if (_recMask) { _recMask.remove(); _recMask = null; }
  _recordAttempt = null;
}
async function submitCashOnly() {
  const cashRaw = (($('#recCash') || {}).value || '').trim();
  const cash = cashRaw === '' ? NaN : Number(cashRaw);
  const button = $('#recCashOnly');
  if (!Number.isFinite(cash) || cash < 0) { toast('请输入不小于 0 的真实剩余资金', 'error'); return; }
  if (button.disabled) return;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    const data = await apiWrite('/api/timing/account', {remaining_cash_yuan:cash}, 'PUT');
    if (!data._httpOk) { toast(data.error || '剩余资金保存失败', 'error'); return; }
    closeRecordModal(); toast(data.message || '真实剩余资金已保存', 'success'); fetchPositionsPanel();
  } catch (error) { toast('网络错误，请重试', 'error'); }
  finally { if (button && button.isConnected) { button.disabled = false; button.removeAttribute('aria-busy'); } }
}
async function submitRecord() {
  const name = ((($('#recCode') || {}).value || '')).trim();
  const shares = Number(($('#recShares') || {}).value || '0');
  if (!name) { toast('请输入股票名称或代码', 'error'); return; }
  if (!Number.isInteger(shares) || shares <= 0) { toast('请填写真实持仓股数；已有持仓允许包含零股', 'error'); return; }
  const sellableRaw = (($('#recSellableShares') || {}).value || '').trim();
  const sellableShares = sellableRaw === '' ? NaN : Number(sellableRaw);
  const sellableAsOf = (($('#recSellableAsOf') || {}).value || '').trim();
  if (!Number.isInteger(sellableShares) || sellableShares < 0 || sellableShares > shares) { toast('请填写券商显示的今日可卖股数，范围为0至总持仓', 'error'); return; }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(sellableAsOf)) { toast('请选择今日可卖库存的核验日期', 'error'); return; }
  const price = Number(($('#recPrice') || {}).value);
  const cashRaw = (($('#recCash') || {}).value || '').trim();
  const cash = cashRaw === '' ? NaN : Number(cashRaw);
  const feeRaw = (($('#recBuyFee') || {}).value || '').trim();
  if (!Number.isFinite(price) || price <= 0) { toast('请输入券商显示的实际持仓成本价', 'error'); return; }
  if (!Number.isFinite(cash) || cash < 0) { toast('请输入不小于 0 的真实剩余资金', 'error'); return; }
  const buyFee = feeRaw === '' ? null : Number(feeRaw);
  const payload = {shares, average_cost:price, remaining_cash_yuan:cash, sellable_shares_snapshot:sellableShares, sellable_as_of:sellableAsOf};
  if (buyFee !== null && (!Number.isFinite(buyFee) || buyFee < 0)) { toast('实际买入费用需为不小于 0 的金额', 'error'); return; }
  if (/^(0|3|6)\d{5}$/.test(name)) payload.code = name;
  else payload.name = name;
  if (buyFee !== null) payload.buy_fee_yuan = buyFee;
  const fingerprint = JSON.stringify(payload);
  if (!_recordAttempt || _recordAttempt.fingerprint !== fingerprint) _recordAttempt = {fingerprint, key:createIdempotencyKey('record-position')};
  const button = $('#recSave');
  if (button.disabled) return;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    const data = await apiWrite('/api/timing/position-snapshot', payload, 'POST', {idempotencyKey:_recordAttempt.key});
    if (!data._httpOk) { toast(data.error || '记录失败', 'error'); return; }
    _recordAttempt = null;
    closeRecordModal();
    toast(data.message, 'success');
    if (data.warning) setTimeout(() => toast(`统一评估未过：${data.warning.reason || '未过门槛'}（仅记录，不构成建议）`, 'warn'), 2000);
    const levels = data.levels || {};
    const riskLine = effectiveRiskLevel(levels.risk_levels, levels.effective_risk_level);
    if (riskLine) setTimeout(() => toast(riskLevelMeta(riskLine), 'info'), 1400);
    fetchPositionsPanel();
  } catch (error) { toast('网络错误，请重试', 'error'); }
  finally { if (button && button.isConnected) { button.disabled = false; button.removeAttribute('aria-busy'); } }
}
})(window.RegisSpa);
