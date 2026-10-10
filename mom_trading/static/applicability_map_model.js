(function (root, factory) {
  const model = factory();
  if (typeof module === 'object' && module.exports) module.exports = model;
  if (root) root.RegisApplicabilityModel = model;
})(typeof window === 'undefined' ? null : window, function () {
  'use strict';

  const STATES = [
    { id: 'risk_closed', label: '风险关闭' },
    { id: 'weak_repair', label: '弱修复' },
    { id: 'sentiment_rising', label: '情绪上升' },
    { id: 'crowded_climax', label: '高潮拥挤' },
    { id: 'retreat', label: '退潮' },
  ];
  const FACTOR_STATUS = {
    candidate: '候选', validated: '输入已核验', registered: '已登记',
    backtesting: '研究回测中', historical_pass: '历史确认',
    production: '正式目录', rejected: '已否决', frozen: '已冻结',
    invalidated: '已失效',
  };

  function currentState(route) {
    if (!route || (route.research_snapshot || {}).current === false) return 'unknown';
    const workspace = route.today_decision_workspace || {};
    const candidate = workspace.market_state && workspace.market_state !== 'unknown'
      ? workspace.market_state : route.market_state;
    return STATES.some(function (row) { return row.id === candidate; }) ? candidate : 'unknown';
  }

  function researchPlaybookIds(route) {
    const rows = ((route || {}).today_decision_workspace || {}).eligible_playbooks || [];
    return new Set(rows.map(function (row) { return typeof row === 'string' ? row : row && row.id; }).filter(Boolean));
  }

  function retiredPlaybookIds(route) {
    const review = ((route || {}).completed_market_review || {}).terminal_playbook_retirement || {};
    return new Set(Array.isArray(review.retired_entry_playbooks) ? review.retired_entry_playbooks : []);
  }

  function factorRow(row, route) {
    const status = String(row.status || 'candidate');
    const blocked = ['rejected', 'frozen', 'invalidated'].includes(status)
      || row.source_eligible !== true;
    const reason = blocked
      ? row.blocked_reason || row.status_reason || row.audit_message || '研究或来源门禁未通过'
      : '现有因子注册接口未提供独立验证的分市场状态增量；不可从整体历史表现推断适用状态。';
    return {
      kind: 'factor', id: String(row.factor_id || ''),
      name: String(row.name || row.factor_id || '未命名因子'),
      family: String(row.family || '未分类机制'),
      version: String(row.sealed_recipe_sha256 || row.research_epoch || '版本未记录'),
      status: FACTOR_STATUS[status] || status,
      reason: String(reason),
      detail: String(row.description || row.formula || ''),
      evidence: String(row.evidence_sha256 || '未提供状态条件证据包'),
      cells: STATES.map(function (state) {
        return {
          state: state.id, current: state.id === currentState(route),
          phase: blocked ? 'blocked' : 'unverified',
          label: blocked ? '禁用/失效' : '状态效果未验证',
        };
      }),
    };
  }

  function playbookRow(row, route) {
    const state = currentState(route);
    const declared = new Set(Array.isArray(row.applicable_market_states)
      ? row.applicable_market_states : []);
    const retired = retiredPlaybookIds(route).has(row.id);
    const research = researchPlaybookIds(route).has(row.id);
    const defense = row.kind === 'research_risk_playbook';
    const reason = retired ? '历史规则已终止，不再进入候选或买入计划。'
      : defense ? '仅作减风险与空仓研究，不是入场战法。'
        : '机制声明不是独立条件效果验证；当前研究路由匹配也不等于买入授权。';
    return {
      kind: 'playbook', id: String(row.id || ''),
      name: String(row.label || row.id || '未命名战法'),
      family: defense ? '防守战法' : '入场战法',
      version: String(row.version || row.id || '版本未记录'),
      status: retired ? '已终止' : defense ? '仅减风险' : '仅研究',
      reason: reason,
      detail: String(row.characteristic || row.consider_when || ''),
      evidence: '未提供独立分状态成本后验证证据包',
      cells: STATES.map(function (item) {
        const fit = declared.has(item.id);
        let phase = retired ? 'blocked' : fit ? 'declared' : 'none';
        let label = retired ? '已终止' : fit ? '机制声明' : '未声明适用';
        if (item.id === state && fit && research && !retired) label = '当前仅研究';
        if (item.id === state && !defense
            && ['risk_closed', 'crowded_climax', 'retreat'].includes(state)) {
          phase = 'blocked'; label = '禁止新入场';
        }
        if (item.id === state && defense && fit && !retired) label = '减风险研究';
        return { state: item.id, current: item.id === state, phase: phase, label: label };
      }),
    };
  }

  function playbooksFromRoute(route) {
    const library = (route || {}).small_capital_playbook_library || {};
    return Array.isArray(library.playbooks) ? library.playbooks : [];
  }

  return { STATES: STATES, currentState: currentState, factorRow: factorRow,
    playbookRow: playbookRow, playbooksFromRoute: playbooksFromRoute };
});
