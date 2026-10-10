/* The planning page reads the existing producer; it never runs a scanner or a trade. */
(function () {
  'use strict';
  const {esc, escAttr} = window.RegisSpa.core;
  const boundary = x => x?.research_only === true && x.production_eligible === false
    && x.generates_alert === false && x.automatic_trading === false && x.suggested_shares === null;
  const names = (ids, rows) => (ids || []).map(id => rows.find(r => r.id === id)?.name || '定义待核验').join('、');
  const sectorKey = r => `${r.kind}:${r.code}`;

  function model(scan, referenceDay, planningDay, sectors) {
    const empty = {available:false, playbooks:[], candidates:[], candidatePairs:0, emotionMatches:false,
      summary:scan?.scan_snapshot?.current === true ? '当前战法快照的目录、版本或边界未通过核验；历史条件已清除。'
        : scan?.scan_snapshot?.summary || '当前后台战法快照未就绪；不借用历史目录或名单补齐。'};
    const d = scan?.development, meta = scan?.scan_snapshot;
    if (scan?.schema !== 'playbook_candidate_scan.v1' || meta?.current !== true
        || !Number.isFinite(meta.valid_for_seconds) || meta.valid_for_seconds <= 0
        || !boundary(scan) || d?.schema !== 'playbook_development.v1' || !boundary(d)
        || !/^[a-f0-9]{64}$/.test(d.catalog_sha256) || d.catalog_sha256 !== scan.catalog_sha256
        || !['mechanisms','current_routes','phases','mainline_stages','timing_protocols','factors'].every(k => Array.isArray(d[k]))
        || !Array.isArray(scan.versions) || d.mechanisms.some(m => !boundary(m))) return empty;
    const byId = new Map(d.current_routes.map(r => [r.base_playbook_id,r]));
    if (byId.size !== d.current_routes.length || new Set(d.mechanisms.map(m => m.id)).size !== d.mechanisms.length) return empty;
    const sameDay = !!referenceDay && scan.emotion?.data_as_of === referenceDay;
    const validVersions = scan.versions.filter(v => v.spec?.catalog_sha256 === d.catalog_sha256
      && /^[a-f0-9]{64}$/.test(v.spec_id) && /^[a-f0-9]{64}$/.test(v.rule_identity));
    const playbooks = d.mechanisms.map(m => {
      const route = byId.get(m.id);
      const aligned = sameDay && boundary(route) && route.emotion_day === referenceDay;
      return {...m, phaseNames:names(m.default_phases,d.phases), stageNames:names(m.default_mainline_stages,d.mainline_stages),
        route:aligned ? route : null, detailKey:JSON.stringify([d.catalog_sha256,m.id]),
        versions:validVersions.filter(v => v.spec.base_playbook_id === m.id).map(v => ({...v,
          matchLabel:sameDay && boundary(v.environment) && v.environment.emotion_day === referenceDay
            ? v.environment.label || '该版本环境待核验' : '日期未对齐 · 不沿用匹配结果',
          matchReasons:sameDay && boundary(v.environment) && v.environment.emotion_day === referenceDay
            ? [...(v.environment.reasons || []),...(v.issues || [])].join('；') : '',
          phaseNames:names(v.spec.emotion_phases,d.phases), stageNames:names(v.spec.mainline_stages,d.mainline_stages),
          timingName:d.timing_protocols.find(t => t.id === v.spec.timing_protocol)?.name || '时点协议待核验',
          filters:(v.spec.factor_filters || []).map(f => `${d.factors.find(x => x.id === f.factor_id)?.name || '因子定义待核验'} ${f.operator === 'gte' ? '不低于' : '不高于'} ${f.threshold}`).join('；')}))};
    });
    const watched = new Set((sectors || []).map(sectorKey));
    const candidates = [];
    for (const v of validVersions) {
      if (!sameDay || !planningDay || v.status !== 'checked' || v.scope_complete !== true
          || v.checked_members !== v.total_members || !Array.isArray(v.rows)
          || !boundary(v.environment) || v.environment.emotion_day !== referenceDay
          || v.environment.status !== 'environment_matched_shape_pending') continue;
      for (const row of v.rows) {
        const p = row.research_plan;
        if (row.checked !== true || row.shape !== true || row.status !== 'research_conditions_met_execution_pending'
            || row.signal_day !== referenceDay || !p || p.signal_day !== referenceDay
            || p.spec_id !== v.spec_id || p.rule_identity !== v.rule_identity
            || !row.price_input_identity || p.input_identity !== row.price_input_identity
            || row.timing?.status !== 'next_open_pending' || p.expires_at !== row.timing.expires_at
            || String(p.expires_at).slice(0,10) !== planningDay
            || !(row.sectors || []).some(s => watched.has(sectorKey(s)))) continue;
        const html = window.RegisResearchPlan?.html(p);
        if (html) candidates.push({code:row.stock_code,name:row.stock_name,versionName:v.name,html,
          sectors:row.sectors.filter(s => watched.has(sectorKey(s))).map(s => s.name),
          mainlineAt:v.mainline_at, memberPacket:v.member_packet_id,
          planBinding:{spec_id:v.spec_id,rule_identity:v.rule_identity,stock_code:row.stock_code,
            input_identity:p.input_identity,expires_at:p.expires_at,entry_terms:p.entry_terms},
          detailKey:JSON.stringify([v.spec_id,row.stock_code,p.input_identity,p.rule_identity,p.expires_at,p.entry_terms])});
      }
    }
    return {available:true, playbooks,candidates,candidatePairs:candidates.length,emotionMatches:sameDay,
      emotionName:scan.emotion?.label || d.phases.find(r => r.id === scan.emotion?.phase)?.name || '情绪待核验',
      emotionDay:scan.emotion?.data_as_of, checkedAt:scan.checked_at,
      summary:sameDay ? '战法、保存版本与个股检查来自同一后台扫描；当前条件匹配不代表收益准入。'
        : `战法环境使用 ${scan.emotion?.data_as_of || '未核验日期'} 情绪；本预案需要 ${referenceDay || '已核验参考日'}，不套用不同日期的匹配标签。`,
      scanSummary:`已检查 ${scan.checked_pairs} / ${scan.known_member_pairs} 条已知成分与版本组合；未完整扫描、证据失效或板块不在本预案观察范围的记录，不列入下方候选。`};
  }

  function html(model) {
    if (!model.available) return `<p class="workflow-warning">${esc(model.summary)}</p><a href="/strategy-lab">查看后台战法研发与输入状态</a>`;
    const cards = model.playbooks.map(m => `<article class="workflow-playbook">
      <div class="workflow-playbook-title"><h4>${esc(m.name)}</h4><span>默认规则：${esc(m.route?.label || '日期未对齐 · 仅规则说明')}</span></div>
      <p>${esc(m.characteristic)}</p><p><b>默认适用情绪：</b>${esc(m.phaseNames)}；<b>主线：</b>${esc(m.stageNames || '持仓风险独立复核')}</p>
      <p>${esc(m.route ? m.route.reasons.join('；') || m.route.next_action : model.summary)}</p>
      ${m.route?.sectors?.length ? `<p>该规则证据中的板块：${esc(m.route.sectors.map(s => s.name).join('、'))}；另核上方预案观察范围与来源时点。</p>` : ''}
      <details data-plan-detail="${escAttr(m.detailKey)}"><summary>查看入场、退出、因子与已保存版本</summary><dl>
        ${[['入场',m.entry],['风险线',m.stop],['退出',m.exit],['退出顺序',(m.exit_priority || []).join(' → ')],['禁止条件',m.forbidden],['失效',m.invalidation],['加减仓',m.additions],['因子作用',m.factor_role]].map(([name,value]) => `<div><dt>${name}</dt><dd>${esc(value || '规则待核验')}</dd></div>`).join('')}
      </dl><h5>已保存的当前规则版本 · 不是独立合格策略数</h5>
      ${m.versions.length ? m.versions.slice(0,10).map(v => `<p><b>${esc(v.name)}</b>：${esc(v.phaseNames)}；${esc(v.stageNames || '持仓风险复核')}；${esc(v.timingName)}。${esc(v.filters || '未配置因子过滤，不额外设置因子入场门槛。')}<br>该保存版本：${esc(v.matchLabel)}。${esc(v.matchReasons)}</p>`).join('') + (m.versions.length > 10 ? `<p>共 ${m.versions.length} 个版本；其余请在策略页查看。</p>` : '') : '<p>尚无当前规则的已保存版本；只提供机制说明，不能声称已完成开发验证。</p>'}
      </details><p>研究规格与收益资格分开；没有扣成本、样本外及前向准入证据，不生成买卖数量。</p></article>`).join('');
    return `<p>${esc(model.summary)}</p><p>扫描时点 ${esc(model.checkedAt)}；情绪日 ${esc(model.emotionDay || '待核验')}。防守与退出不等待新仓主线或因子。</p><div class="workflow-playbooks workflow-research-playbooks">${cards}</div><a href="/strategy-lab">查看完整研发目录、历史验证与准入证据</a>`;
  }

  const money = n => Number.isFinite(n) ? `¥${n.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}` : '未核验';
  function linkedAccount(c, portfolio) {
    const review = portfolio?.planning?.account_review;
    const missing = {html:'<p class="workflow-warning">本页尚未取得同一次账户读取的联算结果，不沿用旧股数或费用。</p>',key:''};
    if (review?.schema !== 'planning_account_review.v1' || !boundary(review)
        || review.simultaneous_allocation !== false || !(review.valid_for_seconds > 0)
        || !Array.isArray(review.rows)) return missing;
    const p = c.planBinding;
    const rows = review.rows.filter(r => r.binding && ['spec_id','rule_identity','stock_code','input_identity','expires_at'].every(k => r.binding[k] === p[k])
      && ['reference_close','risk_line','maximum_entry_open'].every(k => r.binding.entry_terms?.[k] === p.entry_terms[k])
      && ['lower_exclusive','upper_inclusive'].every(k => r.binding.entry_terms?.entry_range?.[k] === p.entry_terms.entry_range[k]));
    if (rows.length !== 1 || !boundary(rows[0])) return missing;
    const r=rows[0], id=review.account_identity;
    const identityReady=id?.account_snapshot_status === 'ready' && /^[a-f0-9]{64}$/.test(id.immutable_account_snapshot_sha256)
      && id.immutable_account_snapshot_sha256 === portfolio.account_snapshot_sha256;
    const hasCapacity=identityReady && r.status === 'capacity_calculated' && r.account_gaps?.length === 0
      && Number.isInteger(r.independent_capacity_shares) && r.independent_capacity_shares >= 0
      && r.capacity_estimate?.shares === r.independent_capacity_shares;
    const b=review.budgets, e=r.capacity_estimate, m=r.minimum_order;
    return {key:r.review_identity,html:`<section data-plan-account-review><h4>同一账户快照联算 · 独立研究容量，不是建议买入数量</h4>
      <p>读取 ${esc(review.checked_at)}；最迟 ${esc(review.valid_until)} 重新核验。原始战法检查不读账户，本节另行绑定其同一价格与规则。</p>
      <p>按研究价格上边界向下取分 ${money(r.reference_entry_price)} 核算，未核验下一日除权与可成交性；不是委托报价。</p>
      <dl><dt>最低一笔测算</dt><dd>${esc(r.quantity_rule?.minimum)} 股起，增加单位 ${esc(r.quantity_rule?.increment)} 股；名义金额 ${money(m?.notional_yuan)}，模型买入成本 ${money(m?.buy_cost?.total)}，含费资金 ${money(m?.cash_required_yuan)}；至风险线的含费计划损失 ${money(m?.planned_loss_yuan)}。</dd>
      <dt>独立容量上限</dt><dd>${hasCapacity ? `${r.independent_capacity_shares.toLocaleString('zh-CN')} 股，约占当前权益 ${Number(r.capacity_exposure_pct).toFixed(2)}%；${r.independent_capacity_shares === 0 ? '已核算约束不容纳最低一笔；不等于未录账户。' : '仅为已列约束的上界，未通过全部风控，不能下单。'}` : '未核齐账户约束，不生成容量股数。'}</dd>
      ${hasCapacity ? `<dt>为什么不能再增加数量</dt><dd>${esc((r.limiting_constraints || []).join('、') || '边界待核验')}；按下一档合法数量检查，不把资金与风险不足说成市场没有机会。</dd><dt>容量上限的模型成本与损失</dt><dd>买入成本 ${money(e.buy_cost.total)}，风险线退出成本 ${money(e.risk_exit_cost.total)}，含费计划损失 ${money(e.planned_loss_yuan)}。沿用统一模型：佣金万2.5、每笔最低5元，双边滑点各0.1%；均未按券商实费校准。</dd>` : ''}
      ${identityReady && b ? `<dt>实际参与的资金与风险限额</dt><dd>现金扣20%权益储备后 ${money(b.cash_after_reserve_yuan)}；市场总仓位余量 ${money(b.total_exposure_headroom_yuan)}；单股20%权益上限 ${money(b.single_position_ceiling_yuan)}；行业25%上限按最大已知行业加未知持仓预留后的余量 ${money(b.industry_worst_case_headroom_yuan)}；单笔0.5%权益损失上限 ${money(b.new_position_loss_ceiling_yuan)}，组合2.5%损失预算剩余 ${money(b.portfolio_remaining_loss_yuan)}。这些是现行规则上限，不是优化所得。</dd>` : ''}</dl>
      ${(r.account_gaps || []).map(g=>`<p class="workflow-warning">${esc(g)}</p>`).join('')}
      ${!identityReady && !r.account_gaps?.length ? '<p class="workflow-warning">账户身份与本页不一致，旧容量已撤下。</p>' : ''}</section>`};
  }

  function candidatesHtml(model, portfolio = null) {
    if (!model.available) return `<p class="workflow-warning">${esc(model.summary)}</p>`;
    return `<p>${esc(model.scanSummary)}不同版本命中同一只股票分别展示，不计为多只股票或正式买入提示。</p>
      ${model.candidates.length ? `<div class="workflow-candidates">${model.candidates.map(c => {const account=linkedAccount(c,portfolio);return `<article><a href="/stock?code=${encodeURIComponent(c.code)}">${esc(c.name || c.code)}</a>
      <p>${esc(c.versionName)} · ${esc(c.sectors.join('、'))}</p><p>主线检查时点 ${esc(c.mainlineAt || '待核验')}；与上方盘后板块原件分别标注，不冒充同一观察桶。</p>
      <details data-plan-detail="${escAttr(c.detailKey+account.key)}"><summary>查看来源绑定的价格与退出研究预案 · 非买单</summary>${c.html}${account.html}</details></article>`;}).join('')}</div>`
      : '<p class="workflow-empty">本预案尚无同日、同版本、在观察板块范围内且完整核验的研究候选；结合上方扫描缺口与下方战法原因判断，不补造个股。</p>'}
      ${model.candidates.length && portfolio?.planning?.account_review?.unresolved ? `<aside class="workflow-note"><h4>容量联算尚未覆盖的交易条件</h4><ul>${portfolio.planning.account_review.unresolved.map(g=>`<li>${esc(g)}</li>`).join('')}</ul><p><a href="/risk">核对完整风险与仓位</a>；各候选不是一份可同时执行的组合计划。</p></aside>` : ''}`;
  }
  window.RegisPlanResearch = {model,html,candidatesHtml};
}());
