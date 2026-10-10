(function () {
  'use strict';
  const escape = value => { const node = document.createElement('span'); node.textContent = value == null ? '' : String(value); return node.innerHTML; };
  function planHTML(plan) {
    if (!plan || plan.status !== 'conditional_research_not_order' || plan.production_eligible !== false
        || plan.generates_alert !== false || plan.automatic_trading !== false
        || plan.account_evaluated !== false || plan.suggested_shares !== null) return '';
    const terms = plan.entry_terms;
    if (!terms || terms.status !== 'within_signal_risk_boundary' || !terms.entry_range
        || !Array.isArray(plan.unresolved)
        || ![terms.reference_close, terms.risk_line, terms.maximum_entry_open, terms.entry_range.upper_inclusive].every(v => Number.isFinite(v) && v > 0)
        || terms.entry_range.lower_exclusive !== terms.risk_line
        || terms.entry_range.upper_inclusive <= terms.risk_line
        || terms.entry_range.upper_inclusive > terms.maximum_entry_open) return '';
    const number = value => Number.isFinite(value) ? '约 ' + value.toLocaleString('zh-CN', {maximumFractionDigits: 6}) : '未核验';
    return '<section data-pb-plan><h4>入场与退出预案 · 条件研究，不是买单</h4>'
      + '<p>' + escape(terms.price_basis) + '</p><dl>'
      + [['信号日参考收盘', number(terms.reference_close)], ['风险线', number(terms.risk_line)],
        ['开盘研究价格边界', '严格高于风险线；不高于 ' + number(terms.entry_range.upper_inclusive) + '。精简显示不作委托价格。'],
        ['追价上限', number(terms.maximum_entry_open) + '；同时满足单股价格风险上限，不是账户亏损预算。'],
        ['触发与期限', plan.entry_rule + ' 截止 ' + plan.expires_at],
        ['止盈', plan.profit_target_formula + '；实际成交未确认，不代算目标价。'],
        ['退出', plan.exit_rule], ['取消与重新核验', plan.cancel_condition],
        ['账户、数量与成本', '本检查未联算；不生成股数、仓位或成本估值，不把未检查误报成未录入。']]
        .map(([name, value]) => '<dt>' + name + '</dt><dd>' + escape(value) + '</dd>').join('') + '</dl>'
      + '<p>尚未完成：</p><ul>' + plan.unresolved.map(text => '<li>' + escape(text) + '</li>').join('')
      + '</ul><p><a href="/risk">查看风险与仓位</a> · 不会自动转为正式计划或提醒</p></section>';
  }
  window.RegisResearchPlan = {html: planHTML};
}());
