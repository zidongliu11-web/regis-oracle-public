/* Presentation of server policy only; the backend remains authoritative. */
(function () {
  'use strict';
  const policy = document.getElementById('regisAccessPolicy')?.dataset.writePolicy;
  const canWrite = policy === 'local_same_origin';
  const reason = policy === 'remote_read_only'
    ? '云端人工账本只读，不能保存账户或成交；历史研究实验有独立登记入口。'
    : '写入权限未核实，请重新加载页面；未发送保存请求。';
  const controls = [
    '[data-server-write]', '#cashSave', '#snapshotSave', '#cashflowSave',
    '#cashflowReconcileSave', '#recSave', '#recCashOnly', '#sellConfirm',
    '#sellableConfirm', '#sellableSave',
    '[data-action="record-open"]', '[data-action="sellable-inventory"]',
    '[data-action="sell-mini"]', '[data-action="delete-position"]',
    '#positionsBody button[data-action="sell"]',
    '#positionsBody button[data-action="sellable"]',
    '#positionsBody button[data-action="delete"]',
  ].join(',');
  function lock(root) {
    if (canWrite || !root.querySelectorAll) return;
    const nodes = [...root.querySelectorAll(controls)];
    if (root.matches?.(controls)) nodes.push(root);
    nodes.forEach(button => {
      button.disabled = true;
      button.setAttribute('aria-disabled', 'true');
      button.setAttribute('data-write-unavailable', '');
      button.title = reason;
    });
    const entrySelector = '#terminalAccountEntry, a[href^="/?account=record"]';
    const entries = [...root.querySelectorAll(entrySelector)];
    if (root.matches?.(entrySelector)) entries.push(root);
    entries.forEach(entry => {
      entry.href = '/positions#accountFacts';
      entry.textContent = policy === 'remote_read_only' ? '查看账户事实 · 只读' : '查看账户 · 权限待核验';
    });
  }
  window.RegisAccess = Object.freeze({canWrite, reason});
  if (!canWrite) {
    // Account controls are also inserted by refreshes and SPA navigation.
    new MutationObserver(records => records.forEach(record =>
      record.addedNodes.forEach(node => { if (node.nodeType === 1) lock(node); })
    )).observe(document.body, {childList:true, subtree:true});
    lock(document);
    document.addEventListener('click', event => {
      if (event.target.closest(controls)) { event.preventDefault(); event.stopImmediatePropagation(); }
    }, true);
  }
})();
