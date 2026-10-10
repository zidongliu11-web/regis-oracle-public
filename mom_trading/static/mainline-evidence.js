/* Read-only presentation of the server's existing sector confirmation path. */
(function () {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const pct = value => value != null && value !== '' && Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)}%` : '未提供';
  const clock = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleTimeString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '未形成连续观察';
  function persistence(p) {
    if (!p || p.status !== 'observed' || p.research_only !== true || p.generates_alert !== false) {
      return `<h4>板块连续观察</h4><p>${esc(p?.summary || '尚无同日连续原件，不能从一次排名判断持续走强。')}</p>`;
    }
    const rows = (p.rows || []).map(r => `<article><h5>${esc(r.name)}<small>${r.kind === 'industry' ? '行业' : '概念'} · ${r.in_latest_top_three ? '本次仍在前3' : '本次已离开前3'}</small></h5><dl>
      <div><dt>连续入选</dt><dd>${esc(r.consecutive_buckets)} 个相邻观察桶</dd></div>
      <div><dt>上涨占比</dt><dd>${pct(r.current_breadth_pct)}</dd></div>
      <div><dt>广度变化</dt><dd>${r.breadth_change_pp == null ? '缺少可比前桶' : `${Number(r.breadth_change_pp).toFixed(1)} 个百分点（较 ${esc(clock(r.comparison_at))}）`}</dd></div>
      <div><dt>连续时段</dt><dd>${esc(clock(r.first_consecutive_at))}${r.first_consecutive_at ? ' → '+esc(clock(r.latest_at)) : ''}</dd></div></dl></article>`).join('');
    return `<h4>板块连续观察 <span>${p.historical_only ? '历史留档，非当前行情' : '最近留档，非买入确认'}</span></h4><p>${esc(p.summary)}</p>
      <p class="mainline-source">当日留档 ${esc(p.attempts)} 桶 / 来源通过 ${esc(p.accepted)} 桶；最近 ${esc(p.observed_at)}。缺桶、失败、重复源时点或板块范围变化均不能拼成连续记录。</p>
      ${rows ? `<p>连续入选与广度变化（不是收益排名）</p><div class="mainline-history">${rows}</div>` : '<p>已留档的完整横截面没有前3达标观察板块；不等于市场没有机会。</p>'}
      <p>广度分母仍为上涨+下跌家数，未证明成分恒定；不确认龙头、不计算收益、不计正式前向样本，也不替代交易日历、情绪、策略与账户准入。</p>`;
  }
  function constituents(p) {
    const heading = '<h4>主线内部：扩散与核心观察</h4>';
    if (!p || !['observed','partial'].includes(p.status) || p.research_only !== true || p.generates_alert !== false || p.changes_trade_authority !== false) return heading + `<p>${esc(p?.summary || '成分证据暂未就绪；不将缺失解释为没有主线。')}</p>`;
    const source = p.source_as_of_min && p.source_as_of_max;
    return heading + `<p>${esc(p.summary)}</p><p>采集完成 ${esc(p.observed_at)} · ${p.historical_only !== false || !source ? '历史留档，非当前行情' : '同桶观察，非交易确认'}</p>
    <p class="mainline-source">${source ? `板块及有效成分源时点：${esc(p.source_as_of_min)} → ${esc(p.source_as_of_max)}；核验于 ${esc(p.checked_at || '未提供')}。` : '来源时间范围未提供，不标为当前成分行情。'}</p><div class="mainline-history">${(p.rows || []).map(r => {
      const m = r.metrics;
      if (!m || r.errors?.length) return `<article><h5>${esc(r.sector?.name)}</h5><p>成分来源未通过完整性或时效核验，不推算广度、核心股与集中度。</p><small>已取 ${esc(r.fetched_count ?? '—')} / 来源报告 ${esc(r.reported_total ?? '—')} 家</small></article>`;
      return `<article><h5>${esc(r.sector?.name)}<small>${esc(m.count)} 家 · ${esc(m.up)} 涨 / ${esc(m.down)} 跌 / ${esc(m.flat)} 平</small></h5><dl>
      <div><dt>上涨扩散</dt><dd>${pct(m.advance_pct)}（含平盘）</dd></div><div><dt>成分中位涨幅</dt><dd>${pct(m.median_change_pct)}</dd></div>
      <div><dt>前三成交额占比</dt><dd>${pct(m.top_three_amount_share_pct)}</dd></div><div><dt>剔除前三后上涨</dt><dd>${pct(m.ex_core_advance_pct)}</dd></div>
      <div><dt>核心观察</dt><dd>${(m.core_observations || []).map(s => `${esc(s.stock_name)} ${esc(s.stock_code)}（${pct(s.change_pct)}）`).join('、') || '成交额不足以排序'}</dd></div></dl>
      <small>核心观察仅指当时成交额前3，不认定龙头；行业与概念成员可能重叠。上市日在观察日之后剔除 ${esc(r.excluded_future_listing_count ?? '—')} 家。</small></article>`;
    }).join('')}</div>`;
  }
  function trajectory(p) {
    const names = {qualified:'达标',not_qualified:'同身份未达标',unassessable:'指标不可算',identity_changed:'名称/分类改变',absent:'原件内缺席',source_changed:'采集协议不同 · 旧包仅身份校验',missing:'原件缺失',late_capture:'获取太迟',invalid_receipt:'原件无效'};
    const sessions = (p.sessions || []).map(s => {
      const i = s.population?.by_kind?.industry;
      const c = s.population?.by_kind?.concept;
      const count = v => v ? `${esc(v.reported)} / ${esc(v.evaluable)} / ${esc(v.qualified)}` : '不可计算';
      return `<tr><th scope="row">${esc(s.day)}</th><td>${esc(({verified:'已核盘后原件',missing:'缺原件',late_capture:'获取太迟',invalid_receipt:'原件无效',source_changed:'采集协议变化 · 仅核身份'})[s.status] || '未知')}</td><td>${count(i)}</td><td>${count(c)}</td><td>${esc(s.board_packet_id || '—')}</td></tr>`;
    }).join('');
    const audit = p.trajectory_audit;
    const valid = audit?.status === 'observed' && audit.scope === 'same_supplier_contract_ever_qualified_only' &&
      Array.isArray(audit.days) && Array.isArray(audit.rows) && audit.shown_rows === audit.rows.length && audit.rows.length <= 500;
    const rows = valid ? audit.rows.map(r => `<tr data-sector-trajectory-row><th scope="row">${esc(r.name)}<small>${r.kind === 'industry' ? '行业' : '供应商概念'} · ${esc(r.code)}${r.current_qualified ? ' · 本日达标' : ''}</small></th>${(r.cells || []).map(cell => {
      const state = Object.hasOwn(names, cell.state) ? cell.state : 'invalid_receipt';
      return `<td data-state="${state}"><b>${names[state]}</b>${['qualified','not_qualified'].includes(state) ? `<small>涨幅 ${pct(cell.change_pct)} · 上涨 ${pct(cell.advance_coverage_pct)}${state === 'qualified' ? ` · 同类百分位 ${pct(cell.change_rank_pct)}` : ''}</small>` : ''}</td>`;
    }).join('')}</tr>`).join('') : '';
    const head = valid ? audit.days.map(day => `<th scope="col">${esc(day)}</th>`).join('') : '';
    return `<section class="mainline-trajectory" aria-label="五日板块资格轨迹"><h4>五日板块状态 · 分母与轨迹</h4>
      <p>每天的行业／供应商概念分母均取当日已核完整原件；数字顺序为“原件内 / 可计算 / 达标”。小屏表格可左右滑动。概念分类由供应商提供，不能外推为交易所全 A 股总体。</p>
      ${sessions ? `<div class="mainline-trajectory-scroll"><table><caption>最近已完成交易日的完整原件核查</caption><thead><tr><th>日期</th><th>原件状态</th><th>行业</th><th>供应商概念</th><th>原件 ID</th></tr></thead><tbody>${sessions}</tbody></table></div>` : '<p>审计交易日历缺失；不能推算最近五个交易日。</p>'}
      ${valid ? `<details ${rows ? 'open' : ''}><summary>曾达标板块逐日轨迹：共 ${esc(audit.total_rows)} 个身份，展示 ${esc(audit.shown_rows)} 个${audit.omitted_rows ? `，省略 ${esc(audit.omitted_rows)} 个` : ''}</summary>
        <p>仅显示这五日中曾达标、且与本日**同供应商及采集协议**的板块；本日达标者排在前面。空缺、改名、换源或指标不可算不是退潮。单日达标也不是未来主线。</p>
        ${rows ? `<label>按板块名称或代码查找 <input type="search" data-sector-trajectory-filter placeholder="筛查已显示板块" autocomplete="off"></label><small data-sector-trajectory-count>当前显示 ${esc(audit.shown_rows)} 行</small>
          <div class="mainline-trajectory-scroll"><table><caption>历史资格与广度，不是收益或买入榜</caption><thead><tr><th>板块</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>` : '<p>本日来源的五日窗口没有曾达标板块；不等于没有可交易机会。</p>'}
        ${audit.omitted_rows ? '<p>行数超过展示上限，不能从所见行概括全部轨迹。</p>' : ''}</details>` : '<p>本日板块原件未通过核验，逐板块轨迹不可用；不会沿用旧板块。</p>'}
      <p>资格仅由现有涨幅、广度及同类百分位规则判定；历史全成员、可成交龙头、未来 1–5 日成本后收益和策略准入均未验证。</p></section>`;
  }
  function rotation(p) {
    const heading = '<h4>最近交易日板块轮动 · 盘后原件</h4>';
    if (!p || p.schema !== 'sector_rotation_review.v1' || p.research_only !== true || p.generates_alert !== false || p.changes_trade_authority !== false) {
      return `${heading}<p>跨日复盘未返回可信边界；不能沿用上次板块。</p>`;
    }
    const sessions = (p.sessions || []).map(s => `<li><b>${esc(s.day)}</b> · ${esc(({verified:'原件有效',missing:'原件缺失',late_capture:'开盘后才获取',invalid_receipt:'原件无效',source_changed:'采集协议变化 · 仅核旧包身份'})[s.status] || '状态未知')} · ${esc(s.sector_count ?? 0)} 个观察 · ${esc(s.observed_at || '未获取')}</li>`).join('');
    const population = p.population_audit?.scope === 'this_verified_supplier_packet_only' ? p.population_audit : null;
    const populationRows = population ? [['industry','行业'],['concept','供应商概念']].map(([kind,label]) => {
      const c = population.by_kind?.[kind];
      return c ? `<article><strong>${label}</strong><dl>
        <div><dt>原件内</dt><dd>${esc(c.reported)} 个</dd></div><div><dt>可计算</dt><dd>${esc(c.evaluable)} 个</dd></div>
        <div><dt>达标</dt><dd>${esc(c.qualified)} 个</dd></div><div><dt>展示</dt><dd>${esc(c.displayed)} 个</dd></div>
        <div><dt>缺完整指标</dt><dd>${esc(c.unassessable)} 个</dd></div><div><dt>未达标</dt><dd>${esc(c.not_qualified)} 个</dd></div>
      </dl></article>` : '';
    }).join('') : '';
    const populationPanel = `<section class="mainline-population" aria-label="板块原件完整横截面"><h4>本次板块判断的分母</h4>
      ${populationRows ? `<div class="mainline-population-grid">${populationRows}</div><p>另排除 ${esc(population.excluded_behavior_cohorts)} 个“昨日涨停”等行情行为分组。以上只覆盖这份供应商原件，不代表全 A 官方行业全集；达标沿用现有涨幅、广度和同类排名规则。</p>`
        : '<p>完整横截面分母未核，不能从前三观察推断主流板块覆盖。</p>'}</section>`;
    const transition = p.transition_audit?.status === 'comparable' ? p.transition_audit : null;
    const transitionRows = transition ? [['industry','行业'],['concept','供应商概念']].map(([kind,label]) => {
      const c = transition.by_kind?.[kind];
      return c ? `<article><strong>${label}</strong><p>前日达标 ${esc(c.prior_qualified)} · 本日达标 ${esc(c.current_qualified)}</p><p>持续 ${esc(c.continued_qualified)} · 新达标 ${esc(c.newly_qualified)} · 失资格 ${esc(c.lost_qualification)}</p><small>前日身份/覆盖未匹配 ${esc(c.prior_identity_unmatched)} · 本日身份/覆盖未匹配 ${esc(c.current_identity_unmatched)}</small></article>` : '';
    }).join('') : '';
    const lostExamples = (transition?.lost_or_unmatched_examples || []).slice(0,5).map(r => `<li>${esc(r.name)}（${esc(r.code)}）：${r.reason === 'same_identity_no_longer_qualified' ? '同身份仍在原件内，但不再达标' : r.reason === 'same_identity_not_evaluable' ? '同身份但指标不可计算，不能判为退潮' : '名称或覆盖不连续，不能判为退潮'}</li>`).join('');
    const transitionPanel = `<section class="mainline-population" aria-label="板块达标迁移"><h4>相邻交易日达标迁移</h4>
      ${transitionRows ? `<div class="mainline-population-grid">${transitionRows}</div>${lostExamples ? `<ul>${lostExamples}</ul>` : ''}<p>仅同一供应商、连续两份有效原件的代码与名称可比，不能证明成分未改；“失资格”是筛选规则变化，不是收益下跌或交易退出。</p>`
        : '<p>前日原件缺失、迟到或来源变化，今天无法判断延续与退出。</p>'}</section>`;
    const rows = (p.rows || []).map(r => `<article><h5>${esc(r.name)} <small>${r.kind === 'industry' ? '行业' : '供应商概念观察 · 题材属性待核'} · ${esc(r.code)}</small></h5><dl>
      <div><dt>跨日角色</dt><dd>${esc(({continued_observation:'相邻日仍达标',new_to_watch:'同身份且前日可计算，新达标观察',prior_unassessable:'前日同身份但不可计算，不推轮动',identity_changed:'名称/分类身份变化，断开接续',identity_unmatched:'前日身份/覆盖未匹配，不推轮动',history_incomplete:'相邻历史不足'})[r.role] || '待核验')} · 连续 ${esc(r.consecutive_observed_sessions)} 个达标交易日</dd></div>
      <div><dt>当日广度</dt><dd>${pct(r.advance_coverage_pct)} · ${r.breadth_delta_pp == null ? '前日不可比' : `较前日 ${Number(r.breadth_delta_pp) >= 0 ? '+' : ''}${pct(r.breadth_delta_pp)} 点`}</dd></div>
      <div><dt>核心观察</dt><dd>${r.core_observations?.length ? r.core_observations.map(s => `${esc(s.stock_name)} ${esc(s.stock_code)}`).join('、') : '成员原件和独立时点未核，不能确定'}</dd></div></dl>
      ${r.core_observations?.length ? `<small>成员源时点 ${esc(r.member_source_as_of_min)} → ${esc(r.member_source_as_of_max)}；获取 ${esc(r.member_observed_at)}；成员原件 ${esc(r.member_packet_id)}</small>` : ''}
      <small>${esc(r.core_scope)}</small></article>`).join('');
    const hypotheses = (p.hypotheses || []).map(h => `<li><b>${esc(h.name)}</b>：${esc(h.scenario)}。确认：${esc(h.confirm)} 失效：${esc(h.invalidate)}</li>`).join('');
    return `<div class="mainline-rotation" aria-label="盘后板块轮动复盘">${heading}
      <p>${esc(p.summary || '盘后复盘待核验')}</p><p class="mainline-source">本卡固定最近已完成交易日，不随账户复盘日期切换。参考 ${esc(p.reference_day || '未知')} · 目标 ${esc(p.target_day || '未知')} · 板块源 ${esc(p.source_as_of || '未知')} · 板块获取 ${esc(p.observed_at || '未知')} · 板块原件 ${esc(p.board_packet_id || '未知')}</p>
      <p>最近 ${esc(p.window_sessions)} 个交易日原件：${p.status === 'waiting_calendar' ? '交易日历待核' : ''}</p><ol class="mainline-session-strip">${sessions}</ol>
      ${populationPanel}${transitionPanel}${trajectory(p)}
      ${rows ? `<div class="mainline-history">${rows}</div>` : '<p>本次无可显示的已核行业/题材；具体原因见原件状态，不回填旧名单。</p>'}
      <h4>下一交易日条件假设 · 尚无预测资格</h4>${hypotheses ? `<ul class="mainline-hypotheses">${hypotheses}</ul>` : `<p>${p.hypothesis_window_open === false ? '开盘后，前一晚假设已过期；请看当日盘中证据。' : '相邻原件不足、来源不同、身份/覆盖不可比或无达标板块，暂不推断延续/轮动。'}</p>`}
      <p class="mainline-boundary">未完成 1–5 日样本外与成本后验证；实际获取的成员不等于历史成分，成交额核心不等于龙头。板块需与当前市场情绪、因子、战法和账户逐项核验。<a href="/strategy-lab">查看战法研究</a> · <a href="/factor-system">查看因子研究</a></p></div>`;
  }
  function render(d) {
    if (!d || d.schema !== 'mainline_diagnostics.v1') return '<section class="mainline-evidence" id="mainline-evidence" aria-label="主线板块证据"><h3>主线板块证据</h3><p>诊断接口尚未返回，不能把缺失数据解释为没有主线。刷新后重新核验。</p></section>';
    const rows = (d.sectors || []).map(row => `<tr><th scope="row">${esc(row.name || '名称未提供')}<small>${row.kind === 'industry' ? '行业' : row.kind === 'concept' ? '概念' : '类型未提供'}</small></th><td>${pct(row.change_pct)}</td><td>${pct(row.advance_coverage_pct)}<small>${esc(row.up_count ?? '—')}涨 / ${esc(row.down_count ?? '—')}跌</small></td><td>${pct(row.change_rank_pct)}</td><td>${esc(row.data_as_of || '未提供')}</td></tr>`).join('');
    return `<section class="mainline-evidence" id="mainline-evidence" aria-label="主线板块证据">
      <header><h3>主线板块证据</h3><strong>${esc(d.label)}</strong></header><p>${esc(d.reason)}</p>
      <p class="mainline-source">来源：${esc(d.source)} · 数据：${esc(d.source_as_of || '未提供')} · 本次检查：${esc(d.as_of || '未提供')}</p>
      <div class="mainline-checks">${(d.checks || []).map(row => `<article><h4>${esc(row.label)}<span>${row.passed === true ? '通过' : '未通过'}</span></h4><b>${esc(row.observed)}</b><p>${esc(row.requirement)}</p></article>`).join('')}</div>
      ${rows ? `<div class="mainline-table"><table><caption>本次板块观察（不是买入名单）</caption><thead><tr><th>板块</th><th>涨幅</th><th>上涨占比</th><th>同类涨幅百分位</th><th>板块源时点</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="mainline-empty">当前没有通过来源核验的达标板块行；具体原因见上方，不用涨幅榜猜主线。</p>'}
      <p><b>当前确认规则：</b>${esc(d.rule_summary)}</p><p>${esc(d.rule_boundary)}</p>
      ${persistence(d.persistence_observation)}
      ${constituents(d.constituent_observation)}
      <h4>距离完整交易主线还差什么</h4><dl>${(d.missing_capabilities || []).map(row => `<div><dt>${esc(row.label)}</dt><dd>${esc(row.reason)}</dd></div>`).join('')}</dl>
      <p class="mainline-boundary">这张卡仅解释当前横截面；历史已核验快照另作复盘，不覆盖本次状态，不产生买卖权限。</p>
    </section>`;
  }
  window.RegisMainlineEvidence = {render, rotation};
  if (typeof document !== 'undefined') document.addEventListener('input', event => {
    if (!event.target.matches?.('[data-sector-trajectory-filter]')) return;
    const panel = event.target.closest('.mainline-trajectory');
    const term = event.target.value.trim().toLocaleLowerCase();
    let visible = 0;
    panel.querySelectorAll('[data-sector-trajectory-row]').forEach(row => {
      row.hidden = !row.querySelector('th').textContent.toLocaleLowerCase().includes(term);
      if (!row.hidden) visible += 1;
    });
    const count = panel.querySelector('[data-sector-trajectory-count]');
    if (count) count.textContent = `当前显示 ${visible} 行`;
  });
})();
