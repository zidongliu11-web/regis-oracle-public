(() => {
  'use strict';
  const root = document.getElementById('researchWorkbench');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const states = {queued: '已登记 · 等待执行器', running: '执行中', completed: '历史诊断完成', blocked: '输入或契约阻塞', failed: '执行失败', interrupted: '执行中断'};
  const phases = {verify_inputs: '核验冻结输入', fixed_candidates: '计算固定候选', costed_paths: '回放费用与成交路径', conditional_evaluation: '复用已核验路径评价情绪与因子', cash_reconciliation: '逐笔核对分红、库存与税费', control_ledger_join: '分批匹配原记录与现金结算', control_increment_evaluation: '比较固定对照与因子增量', universe_features: '构建完整股票池因子输入', completed: '结果已保存', blocked: '等待解除阻塞'};
  phases.control_liquidity_repair = '复现退出缺口并核验停牌成交额';
  let timer, controller, busy = false, lastExperiments, canRegister = false, generation = 0, paused = false;
  const node = (tag, text) => { const el = document.createElement(tag); if (text != null) el.textContent = text; return el; };
  async function api(path, data, signal) {
    let abort;
    const cancelled = new Promise((_, reject) => {
      abort = () => { const error = new Error('读取超时或取消'); error.name = 'AbortError'; reject(error); };
      if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, {once: true});
    });
    try {
      const work = (async () => {
        if (signal?.aborted) return null;
        const response = await fetch(path, {cache: 'no-store', signal, ...(data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
        return payload;
      })();
      return await Promise.race([work, cancelled]);
    } finally { signal?.removeEventListener('abort', abort); }
  }
  function renderExperiment(job) {
    const article = node('article');
    article.append(node('h3', `${job.spec.title} · ${states[job.status] || '状态未知'}`));
    article.append(node('p', `运行编号 ${job.run_id} · 第 ${job.attempt} 次执行 · 更新于 ${job.updated_at}`));
    const progress = job.progress || {};
    article.append(node('p', [phases[progress.phase], progress.period, progress.completed_inputs != null ? `已核验输入 ${progress.completed_inputs}` : '', progress.candidates != null ? `本阶段候选 ${progress.candidates}` : '', progress.instruments_done != null ? `已处理证券 ${progress.instruments_done}` : '', progress.events != null ? `路径记录 ${progress.events}` : ''].filter(Boolean).join(' · ')));
    if (progress.stock_days != null) article.append(node('p', `已保存 ${progress.stock_days.toLocaleString('zh-CN')} 条股票日记录，覆盖 ${progress.completed_sessions} 个交易日。`));
    if (job.reason) article.append(node('p', `阻塞代码：${job.reason}。${progress.next_action || '检查输入或执行日志；原有尝试和结果保留。'}`));
    if (['blocked', 'failed', 'interrupted'].includes(job.status)) {
      const retry = node('button', '条件修复后重试'); retry.type = 'button';
      retry.disabled = !canRegister;
      retry.addEventListener('click', () => act(`/api/research-workbench/experiments/${encodeURIComponent(job.run_id)}/retry`, {}, retry));
      article.append(retry);
    }
    if (job.status === 'completed' && job.summary) {
      const summary = job.summary;
      const countLabel = summary.lane === 'control_increment' ? `四路线信号记录 ${summary.candidate_count} 条（含跨路线重复，不是独立候选数）` : `候选 ${summary.candidate_count} 个`;
      article.append(node('p', summary.lane === 'universe_features' ? summary.interpretation : `${countLabel} · 成本路径 ${summary.costed_event_count} 条。${summary.interpretation}`));
      if (summary.lane === 'universe_features') {
        renderUniverse(article, summary);
      } else if (summary.lane === 'control_increment') {
        if (summary.input_repair) {
          article.append(node('p', `已复现原退出缺口 ${summary.input_repair.reproduced_failed_cost_paths} 条成本路径，${summary.input_repair.changed_cost_paths} 条在确认停牌窗口后重新核验。${summary.input_repair.interpretation}`));
        }
        if (!summary.input_repair?.corporate_audit) {
          const repair = node('button', '核验公司行动与停牌输入'); repair.type = 'button'; repair.disabled = !canRegister;
          repair.addEventListener('click', () => act('/api/research-workbench/experiments', {kind: 'emotion_control_liquidity_repair'}, repair));
          article.append(repair, node('p', '使用冻结特征、原始日线、交易制度例外表与公司行动响应。停牌窗口可复算；送转股只核实缺口，不因公告比例已知就假定股份可卖或税费完整。'));
        }
        loadControls(article, job);
      } else if (summary.lane === 'cash_conditioned') {
        const audit = summary.accounting_audit;
        article.append(node('p', `核账保留全部原信号与因子输入。原价差未解决 ${audit.original_status_counts.unresolved || 0} 条 → 现金结算后未解决 ${audit.settled_status_counts.unresolved || 0} 条；涉及分红 ${audit.cash_entitled_cost_paths} 条成本路径。三个成本情景不是三个独立样本。`));
        article.append(node('h4', '现金结算后 · 战法按情绪评价'));
        renderConditioned(article, {...summary, lane: 'conditional'});
        article.append(node('h4', '现金结算后 · 五项辅助因子增量'));
        renderConditioned(article, {...summary, lane: 'factor', conditional_summaries: undefined, coverage: undefined});
      } else if (summary.lane) {
        renderConditioned(article, summary);
      } else {
      article.append(node('p', '原始价差口径：未接入现金分红结算；不要与现金结算后的结果混用。'));
      const table = node('table'), head = node('tr');
      ['战法 / 情绪', '滑点', '信号', '已闭环', '未成交', '未解决', '已闭环事件平均净收益'].forEach(label => head.append(node('th', label)));
      table.append(head);
      (summary.summary_by_pattern_and_emotion || []).forEach(row => {
        const tr = node('tr');
        [row.playbook_name + ' / ' + row.signal_emotion, row.slippage_bps + 'bp', row.signals, row.closed_events, row.unfilled_events, row.unresolved_events, row.closed_only_mean_net_return == null ? '不可计算' : (100 * row.closed_only_mean_net_return).toFixed(2) + '%'].forEach(value => tr.append(node('td', value)));
        table.append(tr);
      });
      article.append(table, node('p', '上表收益只描述已闭环事件；存在未解决事件时不可代表全体。重叠事件不拼接为账户净值。'));
      const factors = node('details'); factors.append(node('summary', '基础特征诊断 · 不是正式因子准入'));
      const list = node('ul');
      (summary.factor_diagnostics || []).forEach(row => list.append(node('li', `${row.playbook_name} / ${row.signal_emotion} · ${row.factor_name} · 有效信号日 ${row.eligible_signal_days} · 已闭环事件日内秩相关均值 ${row.closed_only_daily_rank_correlation_mean == null ? '不可计算' : row.closed_only_daily_rank_correlation_mean.toFixed(4)}`)));
      factors.append(list); article.append(factors);
      const actions = node('div'); actions.className = 'research-workbench-actions';
      [['emotion_conditioned_diagnostic', '评价各情绪与当日涨跌'], ['emotion_factor_increment_diagnostic', '计算五项因子增量']].forEach(([kind, label]) => {
        const button = node('button', label); button.type = 'button';
        button.disabled = !canRegister;
        button.addEventListener('click', () => act('/api/research-workbench/experiments', {kind, parent_run_id: job.run_id}, button));
        actions.append(button);
      });
      article.append(actions, node('p', '两个分析任务复用本次成交记录，并核对来源摘要；不会重复下载行情或重跑成交路径。'));
      }
      const link = node('a', summary.lane ? '下载完整分组、评价口径与来源记录' : '下载含逐笔费用、未成交及未解决原因的完整结果');
      link.href = `/api/research-workbench/experiments/${encodeURIComponent(job.run_id)}/result`; article.append(link);
      if (summary.lane === 'universe_features') {
        const panel = node('a', `下载已核验的五项因子输入（${(summary.artifact.bytes / 1048576).toFixed(1)} MiB）`);
        panel.href = `/api/research-workbench/experiments/${encodeURIComponent(job.run_id)}/features`;
        const line = node('p'); line.append(panel); article.append(line);
      }
      if (summary.parent) {
        const parent = node('a', '下载来源实验的逐笔成交记录');
        parent.href = `/api/research-workbench/experiments/${encodeURIComponent(summary.parent.run_id)}/result`;
        const line = node('p'); line.append(parent); article.append(line);
      }
    }
    return article;
  }
  function renderUniverse(article, summary) {
    article.append(node('p', `${summary.instruments.toLocaleString('zh-CN')} 只证券 · ${summary.market_sessions} 个交易日 · ${summary.input_stock_days.toLocaleString('zh-CN')} 条股票日记录；其中 ${summary.eligible_stock_days.toLocaleString('zh-CN')} 条满足既定输入条件，${summary.ineligible_stock_days.toLocaleString('zh-CN')} 条不满足条件并保留缺口。`));
    const filters = node('div'); filters.className = 'research-workbench-actions';
    function select(label, values) {
      const wrapper = node('label', label + ' '), input = node('select'); input.setAttribute('aria-label', label);
      Object.entries(values).forEach(([key, text]) => { const option = node('option', text); option.value = key; input.append(option); });
      input.value = 'ALL'; wrapper.append(input); filters.append(wrapper); return input;
    }
    const state = select('输入市场情绪', {ALL: '全部情绪', ...summary.state_labels});
    const direction = select('输入当日涨跌', summary.direction_labels);
    const result = node('div'); result.className = 'research-result-table';
    function render() {
      const rows = summary.coverage.filter(r => (state.value === 'ALL' || r.signal_emotion === state.value) && r.market_direction === direction.value);
      const table = node('table'), head = node('tr');
      ['情绪 / 因子', '市场交易日', '股票日记录', '输入合格记录', '因子有效记录', '可分组交易日'].forEach(text => head.append(node('th', text))); table.append(head);
      rows.forEach(r => {
        const tr = node('tr');
        [summary.state_labels[r.signal_emotion] + ' / ' + r.factor_name, r.market_sessions, r.input_stock_days, r.eligible_stock_days, r.finite_stock_days, r.rankable_sessions].forEach(value => tr.append(node('td', typeof value === 'number' ? value.toLocaleString('zh-CN') : value)));
        table.append(tr);
      });
      result.replaceChildren(table);
      if (rows.every(r => r.market_sessions === 0)) result.append(node('p', '所选情绪与涨跌没有市场交易日，不能估计表现。'));
    }
    [state, direction].forEach(input => input.addEventListener('change', render));
    article.append(filters, result); render();
    article.append(node('p', '当日涨跌按股票涨幅中位数的正负划分。可分组日仅要求至少 5 只输入合格且因子有效的股票、至少 2 个不同因子值。五项因子共用同一股票池，记录数不可相加；此处尚未计算未来收益。'));
    const blockers = node('ul'); (summary.admission_blockers || []).forEach(text => blockers.append(node('li', text)));
    article.append(node('p', '后续验证仍需完成：'), blockers);
  }
  async function loadControls(article, job) {
    // Completed results are immutable and fetched once, not every status poll.
    const target = node('div'), status = node('p', '读取已发布的完整对照汇总…');
    target.append(status); article.append(target);
    const requestController = new AbortController(), deadline = setTimeout(() => requestController.abort(), 15000);
    try {
      const data = await api(`/api/research-workbench/experiments/${encodeURIComponent(job.run_id)}/result`, undefined, requestController.signal);
      if (data.schema !== 'control_increment_research.v1' || data.lane !== 'control_increment' || data.production_eligible !== false) throw new Error('对照结果口径不匹配');
      target.replaceChildren();
      if (data.distribution_repair) {
        renderDistribution(target, data.distribution_repair, data.state_labels, data.tracks);
        target.append(node('p', '下方保留修正前的原始公司行动缺口；本次已核算路径见上方。收益表已纳入本次核算，其余未解决路径继续保留。'));
      }
      if (data.input_repair?.corporate_audit) renderCorporate(target, data.input_repair.corporate_audit, data.state_labels, data.tracks);
      target.append(node('h4', '情绪与形态分别贡献了什么'));
      renderConditioned(target, {...data, lane: 'increment', conditional_summaries: data.increment_summaries});
      const label = node('label', '查看一条对照路线 '), select = node('select'), tables = node('div');
      select.setAttribute('aria-label', '对照路线');
      Object.entries(data.tracks).forEach(([id, name]) => { const option = node('option', name); option.value = id; select.append(option); });
      label.append(select); target.append(label, tables);
      function render() {
        const track = select.value, result = data.track_results[track];
        tables.replaceChildren(node('h4', data.tracks[track] + ' · 战法收益'));
        renderConditioned(tables, {...data, ...result, lane: 'conditional'});
        tables.append(node('h4', data.tracks[track] + ' · 五项因子排序增量'));
        renderConditioned(tables, {...data, ...result, lane: 'factor', conditional_summaries: undefined, coverage: undefined});
      }
      select.addEventListener('change', render); render();
    } catch (error) {
      status.textContent = '对照结果未能确认：' + error.message + '。请刷新重试；不能据此认为规则无效。';
      target.replaceChildren(status);
    } finally { clearTimeout(deadline); }
  }
  function renderDistribution(target, report, stateLabels, tracks) {
    if (report.spec?.schema !== 'control_physical_distribution_repair.v1' || report.spec.production_eligible !== false || report.spec.generates_alert !== false) throw new Error('股数结算口径不匹配');
    target.append(node('h4', '实施公告结算 · 原股、新股与现金逐笔复算'));
    const unfilled = report.paths.filter(event => event.status === 'unfilled').length;
    target.append(node('p', `${report.cost_paths} 条成本路径已重新核算，${report.closed_cost_paths} 条完成股数和现金结算，${unfilled} 条未成交，${report.unresolved_cost_paths} 条仍未解决。未成交不计为成功交易。20bp 对应 ${report.reference_cost_records} 条跨路线记录，不是独立样本数。`));
    target.append(node('p', report.interpretation));
    target.append(node('p', '以下是历史模拟，不是当前买卖提示或真实账户成交；新增股份按公告上市日期判断可卖，未入账现金不用于加仓。'));
    const box = node('div'); box.className = 'research-result-table research-corporate-table';
    const table = node('table'), header = node('tr');
    ['战法 / 情绪 / 信号', '成本情景与结果', '实际股数口径', '分红与成本后损益', '逐笔及原件'].forEach(t => header.append(node('th', t))); table.append(header);
    const money = value => value == null ? '不可计算' : `${Number(value).toFixed(2)} 元`;
    report.paths.forEach(event => {
      const tr = node('tr'), receipts = event.distribution_entitlements || [], orders = event.execution_events;
      const added = receipts.reduce((n, r) => n + r.new_shares, 0);
      const sold = orders.filter(o => o.side === 'sell').reduce((n, o) => n + o.shares, 0);
      tr.append(node('td', `${event.playbook_name} / ${stateLabels[event.signal_emotion] || '未识别情绪'}；${event.code}；信号 ${event.signal_day}；${tracks[event.track] || '未识别路线'}`));
      tr.append(node('td', `${event.slippage_bps}bp · ${{closed:'历史结算完成', unfilled:'未入场', unresolved:'仍未解决'}[event.status] || '未知'}${event.status === 'unresolved' ? '；缺口 ' + event.reason : ''}`));
      tr.append(node('td', `买入 ${event.simulated_entry_shares} 股；新增 ${added} 股；卖出 ${sold} 股；剩余 ${event.remaining_shares} 股`));
      tr.append(node('td', `税前分红 ${money(event.dividend_gross_yuan)}；分红税 ${money(event.dividend_tax_yuan)}；全部费用 ${money(event.total_cost_including_dividend_tax_yuan)}；净损益 ${money(event.net_pnl_yuan)}`));
      const td = node('td'), details = node('details'); details.append(node('summary', '查看逐笔与来源'));
      orders.forEach(o => details.append(node('p', `${o.day} · ${o.side === 'buy' ? '买入' : '卖出'} ${o.shares} 股${o.shares ? ` · 原始价格 ${o.price} · 费用 ${money(o.costs.total)}` : ' · 未成交'}`)));
      receipts.forEach(r => {
        const payment = r.payout_date || (r.gross_yuan === 0 ? '无现金分红' : '未核实');
        const listing = r.new_shares === 0 ? '不涉及送转' : (r.unrestricted_new_share_listing_date || '未核实');
        details.append(node('p', `登记日 ${r.record_date}：${r.entitled_original_shares} 股享有权益；新股上市 ${listing}；现金发放 ${payment}。`));
        if (r.reference_policy === 'a_share_cash_excluding_repurchase') details.append(node('p', `差异化除息：回购 A 股不参与分红，公告用于参考价的每股扣减额为 ${r.reference_cash_per_share} 元；股东实际每股税前权益为 ${r.cash_per_share} 元。现金和税费按实际权益核算，不将参考价扣减额当成分红收入。`));
        if (r.summary_confirmation === 'missing_non_cash_action') details.append(node('p', '现金摘要未收录本次纯转增；由已绑定的发行人公告及原始行情补证，原空响应保留，不代表获得摘要独立确认。'));
      });
      report.implementation_sources.forEach(r => details.append(node('p', `${r.path} · SHA-256 ${r.sha256}`)));
      td.append(details); tr.append(td); table.append(tr);
    });
    box.append(table); target.append(box);
    if (!report.paths.length) target.append(node('p', '这批实施公告没有覆盖原有未知路径；没有修改收益或减少缺口。'));
  }
  function renderCorporate(target, audit, stateLabels, tracks) {
    if (audit.spec?.schema !== 'control_corporate_evidence_audit.v1' || audit.spec.repaired_returns_computed !== false || audit.spec.production_eligible !== false) throw new Error('公司行动审计口径不匹配');
    target.append(node('h4', '公司行动缺口 · 输入审计，不是收益验证'));
    target.append(node('p', `已复现 ${audit.cost_paths} 条成本路径。按 ${audit.reference_slippage_bps}bp 查看：${audit.cross_track_records} 条跨路线记录，对应 ${audit.unique_actions} 次不同公司行动；入场前阻断 ${audit.before_entry_records} 条，已持仓 ${audit.held_records} 条。成本情景和跨路线重复不计为独立样本。`));
    target.append(node('p', audit.interpretation));
    const gaps = node('ul');
    audit.requirement_counts.forEach(r => gaps.append(node('li', `${r.label}：涉及 ${r.records} 条参考成本记录。`)));
    target.append(gaps);
    if (!audit.records.length) { target.append(node('p', '本批固定账本未发现这类未解决路径；不代表其他输入已完整。')); return; }
    const label = node('label', '缺口发生阶段 '), phase = node('select'); phase.setAttribute('aria-label', '公司行动缺口阶段');
    Object.entries({all: '全部阶段', before_entry: '尚未买入', held_inventory: '已持仓'}).forEach(([key, text]) => { const option = node('option', text); option.value = key; phase.append(option); });
    label.append(phase); target.append(label);
    const tableBox = node('div'); tableBox.className = 'research-result-table research-corporate-table'; target.append(tableBox);
    const actions = Object.fromEntries(audit.actions.map(a => [a.action_key, a]));
    const requirements = Object.fromEntries(audit.requirement_counts.map(r => [r.requirement, r.label]));
    function render() {
      const table = node('table'), head = node('tr');
      ['证券 / 除权日', '战法 / 信号情绪', '原模拟库存', '尚缺什么', '来源摘要'].forEach(text => head.append(node('th', text))); table.append(head);
      audit.records.filter(r => r.slippage_bps === audit.reference_slippage_bps && (phase.value === 'all' || r.phase === phase.value)).forEach(r => {
        const a = actions[r.action_key], tr = node('tr');
        tr.append(node('td', `${r.code} / ${r.failure_day}`), node('td', `${r.playbook_name} / ${stateLabels[r.signal_emotion] || '未识别情绪'}；信号 ${r.signal_day}；${tracks[r.track] || '未识别对照路线'}`));
        tr.append(node('td', r.phase === 'before_entry' ? '尚未买入；不自动计零收益' : `登记日收盘 ${r.record_inventory_shares ?? '未知'} 股；按比例新增 ${r.announced_new_shares_exact ?? '未知'} 股，不等于可卖数量`));
        tr.append(node('td', r.requirements.map(key => requirements[key]).join('；')));
        const source = node('td'), detail = node('details'); detail.append(node('summary', a.reason_label));
        detail.append(node('p', `摘要记载股份到账日：${a.reported_share_arrival_date || '缺失'}；仍需核对实施公告与上市安排。`));
        detail.append(node('p', `原件：${a.response.path}`), node('p', `SHA-256：${a.response.sha256}`));
        source.append(detail); tr.append(source); table.append(tr);
      });
      tableBox.replaceChildren(table);
      if (table.rows.length === 1) tableBox.append(node('p', '所选阶段没有本类记录；不据此推断其他输入已完整。'));
    }
    phase.addEventListener('change', render); render();
  }
  function renderConditioned(article, summary) {
    const scope = summary.market_scope === 'MAIN_SH_MAIN_SZ_GEM' ? '沪深主板与创业板；不含科创板和北交所' : '来源未声明完整覆盖范围';
    article.append(node('p', `本次历史输入范围：${scope}。当日普涨或普跌按该范围内信号日有效股票涨幅中位数正负划分；这是新增的描述性分组，不代表长期趋势。年份只用于按时间顺序复核。连续情绪阶段与信号日仍可能相关，不能当作独立样本。`));
    const filters = node('div'); filters.className = 'research-workbench-actions';
    function select(label, values, value) {
      const wrapper = node('label', label + ' '), input = node('select'); input.setAttribute('aria-label', label);
      Object.entries(values).forEach(([key, text]) => { const option = node('option', text); option.value = key; input.append(option); });
      input.value = value; wrapper.append(input); filters.append(wrapper); return input;
    }
    const state = select('市场情绪', {ALL: '全部情绪', ...summary.state_labels}, 'ALL');
    const direction = select('当日涨跌', summary.direction_labels, 'ALL');
    const fold = select('时间复核', {all_history_reused: '全部已复用历史', development_reuse: '2021–2022 已复用', validation_reuse: '2023–2024 已复用', holdout_already_seen: '2025 已看过'}, 'all_history_reused');
    const cost = select('滑点', {10: '10 bp', 20: '20 bp', 30: '30 bp'}, '20');
    const result = node('div'); result.className = 'research-result-table';
    const percent = value => value == null ? '不可计算' : (value * 100).toFixed(3) + '%';
    function render() {
      const source = summary.conditional_summaries || summary.factor_summaries || [];
      const rows = source.filter(r => (state.value === 'ALL' || r.signal_emotion === state.value) && r.market_direction === direction.value && r.fold === fold.value && r.slippage_bps === Number(cost.value));
      if (!rows.length) { result.replaceChildren(node('p', '所选情绪、涨跌和时间范围没有信号样本；不能估计收益，也不能据此启用战法。')); return; }
      const table = node('table'), head = node('tr'), factor = summary.lane === 'factor', increment = summary.lane === 'increment';
      const titles = increment ? ['战法 / 情绪 / 比较', '机会', '保留', '未解决', '对照均值', '规则均值', '规则减对照'] : factor ? ['战法 / 情绪 / 因子', '信号日', '情绪阶段', '可分组日', '因子缺失数', '高组减基线', '高组减低组', '完整日秩相关'] : ['战法 / 情绪', '信号', '信号日', '情绪阶段', '未成交', '未解决', '全部信号平均收益'];
      titles.forEach(text => head.append(node('th', text))); table.append(head);
      rows.forEach(r => {
        const tr = node('tr'), name = r.playbook_name + ' / ' + summary.state_labels[r.signal_emotion];
        const values = increment ? [name + ' / ' + r.effect_name, r.opportunities, r.selected, r.unresolved_pairs, percent(r.baseline_return), percent(r.policy_return), percent(r.delta)] : factor ? [name + ' / ' + r.factor_name, r.signal_days, r.emotion_episodes, r.input_eligible_days, r.factor_input_missing, percent(r.high_minus_universe.all_signal_days_mean), percent(r.high_minus_low.all_signal_days_mean), r.full_rank_ic.all_signal_days_mean?.toFixed(4) ?? '不可计算'] : [name, r.signals, r.signal_days, r.emotion_episodes, r.status_counts.unfilled || 0, r.unresolved, percent(r.full_cohort_mean_return)];
        values.forEach(value => tr.append(node('td', value))); table.append(tr);
      });
      result.replaceChildren(table);
    }
    [state, direction, fold, cost].forEach(input => input.addEventListener('change', render));
    article.append(filters, result); render();
    if (summary.lane === 'increment') article.append(node('p', '情绪退出：同一信号、相同买入，原规则减去取消情绪退出的结果。入场情绪：两边均取消情绪退出，在全部同形态机会中将被过滤的机会保留为闲置预算零收益。形态筛选：同日同板块机会、原退出规则不变，被过滤机会同样保留零收益。每个机会预算两万元；任一未知结果使完整比较不可计算。正的增量可能只是少亏，不代表盈利或因果效果。'));
    if (summary.lane !== 'increment') article.append(node('p', summary.lane === 'factor' ? '先按信号时的因子值固定高低组，再核对结果。基线是同日因子输入完整的全部候选；缺失输入另计。所选范围任一信号日不完整时，完整平均值不显示。高低差是排序诊断，不是可执行多空组合。' : '每个原始信号固定分配 20,000 元研究预算；确认未入场才计零收益。任一未解决事件存在时，全部信号平均收益不显示。该口径包含闲置资金，不能与仅按入场资金计算的收益直接比较。'));
    if (summary.coverage) {
      const details = node('details'); details.append(node('summary', '查看各情绪的样本覆盖与缺口'));
      const list = node('ul'); summary.coverage.filter(r => r.market_direction === 'ALL').forEach(r => list.append(node('li', `${r.playbook_name} / ${summary.state_labels[r.signal_emotion]}：市场 ${r.market_sessions} 天，信号 ${r.signals} 个；${r.configured_for_state ? '原规则允许研究' : '原规则未配置此情绪'}。`)));
      details.append(list); article.append(details);
    }
    const blockers = node('ul'); (summary.admission_blockers || []).forEach(text => blockers.append(node('li', text)));
    article.append(node('p', '当前不具备策略或因子使用资格：'), blockers);
  }
  function renderHistory(job, index) {
    if (index === 0) return renderExperiment(job);
    const history = node('details');
    history.append(node('summary', `${job.spec.title} · ${job.run_id} · ${states[job.status] || '状态未知'}`));
    history.addEventListener('toggle', () => {
      if (history.open && history.children.length === 1) history.append(renderExperiment(job));
    });
    return history;
  }
  const areas = [
    ['preflight','researchInputStatus','researchInputs','实验输入'],
    ['universe_preflight','researchUniverseStatus','researchUniverseInputs','完整股票池输入'],
    ['cash_preflight','researchCashStatus','researchCashInputs','现金结算输入'],
    ['control_preflight','researchControlStatus','researchControlInputs','完整对照组输入'],
    ['liquidity_preflight','researchLiquidityStatus','researchLiquidityInputs','停牌退出输入'],
    ['distribution_preflight','researchDistributionStatus','researchDistributionInputs','公告与股数结算输入']
  ];
  function validPreflight(value) {
    return value && value.available === true && typeof value.all_inputs_present === 'boolean' &&
      Array.isArray(value.inputs) && value.inputs.every(row => row && typeof row.path === 'string' && typeof row.present === 'boolean');
  }
  function closeActions() {
    canRegister = false; lastExperiments = undefined;
    root.querySelectorAll('button').forEach(button => { if (button.id !== 'researchRefresh') button.disabled = true; });
  }
  function staleText(id, text) {
    try { $(id).textContent = text; } catch (_) { /* Other sections still settle. */ }
  }
  function failRead(message) {
    closeActions();
    areas.forEach(area => staleText(area[1], area[3] + '：' + message + '。清单若仍显示，属于上次记录。'));
    staleText('researchActionStatus', '本次状态未确认，已关闭登记与重试；历史记录若仍显示，属于上次读取。可刷新状态。');
  }
  function cancelRead(message) {
    generation++; clearTimeout(timer); controller?.abort(); controller = null; busy = false;
    root.setAttribute('aria-busy','false'); failRead(message);
  }
  async function refresh() {
    if (busy || document.hidden || paused) return;
    const sequence = ++generation, own = new AbortController();
    busy = true; controller = own; root.setAttribute('aria-busy','true');
    const deadline = setTimeout(() => own.abort(), 10000);
    try {
      const data = await api('/api/research-workbench', undefined, own.signal);
      if (sequence !== generation || paused || document.hidden) return;
      if (!data || typeof data !== 'object') throw new Error('状态响应格式无效');
      const jobsAvailable = data.experiments_state?.available === true && Array.isArray(data.experiments);
      canRegister = data.capabilities?.register === true && jobsAvailable && areas.every(area => validPreflight(data[area[0]]));
      let renderFailed = false;
      areas.forEach(area => {
        const [key, statusId, listId, label] = area, value = data[key];
        try {
          if (!validPreflight(value)) {
            $(listId).replaceChildren();
            staleText(statusId, label + '：' + (value?.error || '本次读取未确认，不能解释为没有所需文件。'));
            return;
          }
          $(listId).replaceChildren(...value.inputs.map(row => node('li',
            `${row.present ? '已找到，执行前核验摘要' : '缺失'} · ${row.path}${row.storage_path && row.storage_path !== row.path ? ` · 存储位置 ${row.storage_path}` : ''}`)));
          staleText(statusId, label + '：' + (value.all_inputs_present ? '所需文件已找到，内容摘要与研究资格仍须核验。' : '有冻结输入缺失；不把当前日线或合成样本当作旧输入。') +
            (key === 'preflight' && !canRegister ? ' 当前仅可查看，登记与重试未开放。' : ''));
        } catch (_) { renderFailed = true; staleText(statusId, label + '显示失败；其他区域独立读取，此处若有清单属于旧记录。'); }
      });
      if (!jobsAvailable) {
        lastExperiments = undefined;
        staleText('researchActionStatus', '历史实验目录读取失败，不能解释为没有实验；下方若有记录属于上次读取。');
      } else {
        try {
          const identity = JSON.stringify([data.experiments, canRegister]);
          if (identity !== lastExperiments) {
            $('researchExperiments').replaceChildren(...(data.experiments.length ? data.experiments.map((job,index) => {
              try { return renderHistory(job,index); }
              catch (_) { renderFailed = true; return node('p','一条历史记录显示失败；其他记录可继续查看，请刷新重试。'); }
            }) : [node('p','本次目录中没有已登记实验；不表示策略无效。')]));
            lastExperiments = identity;
          }
          staleText('researchActionStatus', canRegister ? '输入清单与历史目录已分别读取；运行前仍须核验冻结契约。' : '已分别读取可用清单与历史目录；登记与重试保持关闭。');
        } catch (_) { renderFailed = true; lastExperiments = undefined; staleText('researchActionStatus','历史目录显示失败；其他清单仍可查看，下方若有记录属于上次读取。'); }
      }
      if (renderFailed) closeActions();
      root.querySelectorAll('button').forEach(button => { if (button.id !== 'researchRefresh') button.disabled = !canRegister; });
    } catch (error) {
      if (sequence === generation) failRead(error.name === 'AbortError' ? '读取超时或取消，可重试' : '状态读取失败：' + error.message);
    } finally {
      clearTimeout(deadline);
      if (sequence === generation) {
        busy = false; controller = null; root.setAttribute('aria-busy','false');
        clearTimeout(timer); if (!document.hidden && !paused) timer = setTimeout(refresh,15000);
      }
    }
  }
  async function act(path, data, button) {
    if (button.disabled || !canRegister) return;
    button.disabled = true;
    const writeController = new AbortController();
    const deadline = setTimeout(() => writeController.abort(), 10000);
    try { const payload = await api(path, data, writeController.signal); $('researchActionStatus').textContent = `已记录：${payload.experiment.run_id}。相同规格不会重复登记。`; }
    catch (error) { $('researchActionStatus').textContent = '操作未确认成功：' + error.message + '。请刷新核对，勿假定已启动。'; }
    finally { clearTimeout(deadline); button.disabled = !canRegister; await refresh(); }
  }
  $('researchRegister').addEventListener('click', () => act('/api/research-workbench/experiments', {kind: 'emotion_pattern_event_diagnostic'}, $('researchRegister')));
  $('researchRegisterUniverse').addEventListener('click', () => act('/api/research-workbench/experiments', {kind: 'emotion_universe_feature_cache'}, $('researchRegisterUniverse')));
  $('researchRegisterCash').addEventListener('click', () => act('/api/research-workbench/experiments', {kind: 'emotion_cash_conditioned_diagnostic'}, $('researchRegisterCash')));
  $('researchRegisterControl').addEventListener('click', () => act('/api/research-workbench/experiments', {kind: 'emotion_control_increment_diagnostic'}, $('researchRegisterControl')));
  $('researchRegisterDistribution').addEventListener('click', () => act('/api/research-workbench/experiments', {kind: 'emotion_control_distribution_repair'}, $('researchRegisterDistribution')));
  $('researchRefresh').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', () => { paused = document.hidden; if (paused) cancelRead('页面已隐藏，读取已停止'); else refresh(); });
  window.addEventListener('pagehide', () => { paused = true; cancelRead('已离开页面，读取已停止'); });
  window.addEventListener('pageshow', event => { if (event.persisted) { paused = document.hidden; refresh(); } });
  refresh();
})();
