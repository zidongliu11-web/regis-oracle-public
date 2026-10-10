const factory$ = (selector) => document.querySelector(selector);
let factoryData = null;
let liveResearchData = null;
let v44ShadowData = null;
let factorStageFilter = 'all';
let factoryToastTimer = null;
let gateSnapshotAt = 0;

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]
  ));
}

function number(value, digits = 3) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

function pct(value, digits = 3) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  const numeric = Number(value);
  return `${numeric > 0 ? '+' : ''}${numeric.toFixed(digits)}%`;
}

function signedClass(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) return 'flat';
  return numeric > 0 ? 'up' : 'down';
}

function readableTime(value, compact = false) {
  if (!value) return '暂无';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return esc(value);
  return parsed.toLocaleString('zh-CN', {
    hour12: false,
    ...(compact ? {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    } : {}),
  });
}

function durationText(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return '--';
  const total = Math.max(0, Math.floor(Number(seconds)));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days) return `${days}天 ${hours}小时`;
  if (hours) return `${hours}小时 ${minutes}分`;
  if (minutes) return `${minutes}分 ${secs}秒`;
  return `${secs}秒`;
}

function remainingTo(value) {
  if (!value) return null;
  const target = new Date(value).getTime();
  if (!Number.isFinite(target)) return null;
  return Math.max(0, Math.floor((target - Date.now()) / 1000));
}

function showFactoryToast(message, type = 'ok') {
  const toast = factory$('#factoryToast');
  toast.textContent = message;
  toast.className = `toast show ${type === 'error' ? 'error' : ''}`;
  clearTimeout(factoryToastTimer);
  factoryToastTimer = setTimeout(() => {
    toast.className = 'toast';
  }, 3000);
}

function setResearchWorkspace(name, {updateHash = true} = {}) {
  const main = factory$('.factory-main');
  const workspace = factory$('#finhackWorkspace');
  const overviewTab = factory$('#researchOverviewTab');
  const finhackTab = factory$('#finhackWorkspaceTab');
  if (!main || !workspace || !overviewTab || !finhackTab) return;
  const finhackActive = name === 'finhack';
  main.classList.toggle('finhack-workspace-active', finhackActive);
  workspace.hidden = !finhackActive;
  overviewTab.classList.toggle('active', !finhackActive);
  finhackTab.classList.toggle('active', finhackActive);
  overviewTab.setAttribute('aria-selected', String(!finhackActive));
  finhackTab.setAttribute('aria-selected', String(finhackActive));
  if (updateHash) {
    history.replaceState(
      null,
      '',
      finhackActive ? '#finhack-workspace' : location.pathname + location.search,
    );
  }
}

function configureResearchWorkspace() {
  const overviewTab = factory$('#researchOverviewTab');
  const finhackTab = factory$('#finhackWorkspaceTab');
  if (!overviewTab || !finhackTab) return;
  overviewTab.addEventListener('click', () => setResearchWorkspace('overview'));
  finhackTab.addEventListener('click', () => setResearchWorkspace('finhack'));
  window.addEventListener('hashchange', () => {
    setResearchWorkspace(
      location.hash === '#finhack-workspace' ? 'finhack' : 'overview',
      {updateHash: false},
    );
  });
  setResearchWorkspace(
    location.hash === '#finhack-workspace' ? 'finhack' : 'overview',
    {updateHash: false},
  );
}

function renderFinHackWorkspace(data) {
  const source = data.source || {};
  const inventory = data.inventory || {};
  const execution = data.execution || {};
  const security = data.security_review || {};
  const toolchain = data.toolchain_audit || {};
  const toolchainSource = toolchain.source || {};
  const toolchainVerification = toolchain.verification || {};
  const toolchainBoundary = toolchain.evidence_boundary || {};
  const flags = security.flags || {};
  const groups = data.candidate_groups || [];
  const stages = data.stages || [];
  const completed = Number(data.completed_steps || 0);
  const total = Number(data.total_steps || stages.length || 0);
  const progress = Math.max(0, Math.min(100, Number(data.progress_pct || 0)));
  const stateLabels = {
    computing: '正在执行真实计算',
    waiting_for_adapter: '静态接入完成 · 等待适配',
    not_started: '尚未开始',
  };
  const stageLabels = {
    completed: '完成', ready: '就绪', running: '运行中',
    pending: '等待', blocked: '阻塞', locked: '锁定',
  };
  const validationLabels = {
    inventory_only_not_run: '仅完成库存 · 尚未验证',
    static_review_only: '仅静态审查',
    passed: '已通过', failed: '已失败并封存',
  };

  factory$('#finhackReportStatus').textContent = data.report_available
    ? `报告 ${readableTime(data.generated_at, true)}` : '尚无报告';
  factory$('#finhackReportStatus').className = `tag ${data.report_available ? 'green' : 'red'}`;
  factory$('#finhackWorkingNow').textContent = execution.working_now
    ? '是 · 正在使用计算资源' : '否 · 当前未占用计算资源';
  factory$('#finhackCurrentTask').textContent = execution.current_task
    || data.current_stage?.label || '等待静态接入';
  factory$('#finhackTaskReason').textContent = execution.reason
    || '尚未读取FinHack执行状态。';
  factory$('#finhackLastOutput').textContent = execution.last_output_at
    ? readableTime(execution.last_output_at, true) : '暂无';
  factory$('#finhackEta').textContent = execution.eta_seconds === null
    || execution.eta_seconds === undefined
    ? `暂无可验证ETA · ${execution.eta_basis || data.eta_basis || '等待真实样本'}`
    : `约 ${durationText(execution.eta_seconds)}`;
  factory$('#finhackState').textContent = stateLabels[execution.state]
    || execution.state_label || '状态未知';
  factory$('#finhackGlobalWorker').textContent = execution.global_worker_label
    || (execution.global_worker_live
      ? (execution.global_worker_is_finhack ? '正在执行FinHack任务' : '在线 · 正在执行其他任务')
      : '全局研究工人未运行或心跳过期');
  factory$('#finhackGlobalStep').textContent = execution.global_worker_step
    || '暂无全局任务状态';
  factory$('#finhackPinnedCommit').textContent = source.pinned_commit
    ? `固定 ${String(source.pinned_commit).slice(0, 12)}` : '未固定版本';
  factory$('#finhackLicence').textContent = [
    source.licence || '许可证未知',
    source.commercial_distribution_decision === 'pending' ? '商业分发待裁决' : null,
  ].filter(Boolean).join(' · ');

  const toolchainReady = Boolean(toolchain.generated_at);
  const toolchainPassed = toolchainVerification.all_passed === true;
  factory$('#toolchainAuditStatus').textContent = toolchainReady
    ? (toolchainPassed ? '审计完成 · 烟雾测试通过' : '审计完成 · 存在失败项')
    : '尚无审计报告';
  factory$('#toolchainAuditStatus').className = `tag ${toolchainPassed ? 'green' : toolchainReady ? 'red' : 'gray'}`;
  factory$('#toolchainAuditSummary').textContent = toolchain.public_summary
    || toolchain.error || '正在核对文件去重、算子语义和证据边界。';
  factory$('#toolchainSourceFiles').textContent = toolchainReady
    ? Number(toolchainSource.python_files || 0).toLocaleString('zh-CN') : '—';
  factory$('#toolchainUnique').textContent = toolchainReady
    ? `${Number(toolchainSource.unique_contents || 0)} 独立 · ${Number(toolchainSource.duplicate_files || 0)} 副本` : '—';
  factory$('#toolchainChecks').textContent = toolchainReady
    ? `${Number(toolchainVerification.passed || 0)} / ${Number(toolchainVerification.total || 0)}` : '—';
  factory$('#toolchainGate').textContent = (
    toolchainBoundary.unlocks_real_money === false
    && toolchainBoundary.unlocks_auto_trading === false
  ) ? '保持锁定' : '等待证据';
  const auditList = (items, fallback) => (items || []).map((item) => (
    `<li>${esc(item)}</li>`
  )).join('') || `<li>${esc(fallback)}</li>`;
  factory$('#toolchainAdopted').innerHTML = auditList(toolchain.adopted, '尚未登记采用项');
  factory$('#toolchainCorrected').innerHTML = auditList(toolchain.rejected_or_corrected, '尚未登记修正项');
  factory$('#toolchainNextEvidence').innerHTML = auditList(toolchain.next_required_evidence, '尚未登记下一层证据');

  factory$('#finhackProgressText').textContent = `${completed} / ${total || 10} · ${number(progress, 1)}%`;
  factory$('#finhackProgressBar').style.width = `${progress}%`;
  factory$('#finhackProgressDetail').textContent = [
    data.current_stage?.label ? `当前阶段：${data.current_stage.label}` : null,
    data.eta_basis,
    '只统计已有输出证据的步骤',
  ].filter(Boolean).join(' · ');
  factory$('#finhackFormulaCount').textContent = Number(inventory.formula_candidates || 0).toLocaleString('zh-CN');
  factory$('#finhackAlpha101').textContent = Number(inventory.alpha101_formulas || 0).toLocaleString('zh-CN');
  factory$('#finhackAlpha191').textContent = Number(inventory.alpha191_formulas || 0).toLocaleString('zh-CN');
  factory$('#finhackSourceAudit').textContent = `${Number(inventory.python_files || 0)} 文件 · ${Number(inventory.syntax_errors || 0)} 错误`;
  factory$('#finhackStageStatus').textContent = `${completed}/${total || 10} 已有证据`;
  factory$('#finhackStageStatus').className = `tag ${completed === total && total > 0 ? 'green' : 'yellow'}`;
  factory$('#finhackStageRows').innerHTML = stages.length ? stages.map((stage, index) => `
    <article class="finhack-stage ${esc(stage.status || 'pending')}">
      <i></i><div><strong>${String(index + 1).padStart(2, '0')} · ${esc(stage.label || stage.id)}</strong><small>${esc(stage.detail || '')}</small></div>
      <span>${esc(stageLabels[stage.status] || stage.status || '等待')}</span>
    </article>
  `).join('') : '<p class="factory-footnote">尚未读取十步接入报告。</p>';

  factory$('#finhackGroupCount').textContent = `${groups.length} 组`;
  factory$('#finhackGroupRows').innerHTML = groups.length ? groups.map((group) => `
    <article class="registry-row ${group.recommendation_eligible ? 'passed' : 'failed'}">
      <span>${esc(group.source || 'FinHack')} · ${esc(group.licence || source.licence || '许可证待核对')}</span>
      <strong>${esc(group.label || group.id)}</strong>
      <small>${Number(group.formula_count || 0).toLocaleString('zh-CN')} 条公式 · ${esc(validationLabels[group.validation_status] || group.validation_status || '等待验证')}</small>
      <small>${group.recommendation_eligible ? '已具备研究推荐资格' : '不得进入今日机会或买入推荐'}</small>
    </article>
  `).join('') : '<p class="factory-footnote">尚未登记候选因子组。</p>';

  const flagLabels = {
    dynamic_formula_eval: '动态公式执行', os_system_commands: '系统命令',
    pickle_loading: 'Pickle载入', destructive_database_ddl: '破坏性数据库DDL',
  };
  factory$('#finhackSecurityFlags').innerHTML = Object.entries(flags).map(([key, value]) => `
    <div><span>${esc(flagLabels[key] || key)}</span><strong>${Number(value || 0)}</strong></div>
  `).join('') || '<p class="factory-footnote">尚未读取安全扫描。</p>';
  factory$('#finhackBlockers').innerHTML = (data.blockers || []).map((item) => (
    `<li>${esc(item)}</li>`
  )).join('') || '<li>尚未登记阻塞原因。</li>';
}

async function refreshFinHackWorkspace() {
  try {
    const response = await fetch('/api/research/finhack', {cache: 'no-store'});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || 'FinHack工作区读取失败');
    renderFinHackWorkspace(body);
  } catch (error) {
    renderFinHackWorkspace({
      report_available: false,
      execution: {
        state: 'not_started',
        current_task: 'FinHack状态读取失败',
        reason: error.message,
      },
    });
  }
}

function renderFactorCatalog(data) {
  const summary = data.summary || {};
  const sources = (data.sources || []).sort((a, b) => {
    // Cloned first, then by formula count descending
    const aCloned = a.clone_status === 'cloned_sandbox_audited' ? 0 : 1;
    const bCloned = b.clone_status === 'cloned_sandbox_audited' ? 0 : 1;
    if (aCloned !== bCloned) return aCloned - bCloned;
    return (b.formula_count || 0) - (a.formula_count || 0);
  });

  const cloneLabels = {
    cloned_sandbox_audited: '已克隆·沙箱已审计',
    partial_local: '部分本地',
    not_cloned_github_timeout: '克隆超时（可重试）',
    not_cloned: '尚未克隆',
  };
  const phaseLabels = {
    formula_mapping_pending: '等待字段映射',
    not_started: '尚未开始',
  };

  // Summary strip
  const factory$ = (sel) => document.querySelector(sel);
  factory$('#factorCatalogStatus').textContent = data.generated_at
    ? `报告 ${readableTime(data.generated_at, true)}` : '尚无报告';
  factory$('#factorCatalogStatus').className = `tag ${data.generated_at ? 'green' : 'gray'}`;
  factory$('#catalogSourceCount').textContent = summary.total_sources || '—';
  factory$('#catalogFormulaKnown').textContent = (summary.total_formulas_known || 0).toLocaleString('zh-CN');
  factory$('#catalogFormulaCloned').textContent = (summary.total_formulas_cloned || 0).toLocaleString('zh-CN');
  factory$('#catalogSourcesAudited').textContent = `${summary.cloned_sources || 0} / ${summary.total_sources || 0}`;
  const permissive = sources.filter(function(s) {
    return /MIT|Apache/i.test(s.licence || '');
  }).length;
  factory$('#catalogPermissiveCount').textContent = permissive;

  // Source grid
  factory$('#catalogSourceGrid').innerHTML = sources.length ? sources.map(function(src) {
    const cloned = src.clone_status === 'cloned_sandbox_audited';
    const partial = src.clone_status === 'partial_local';
    const count = src.formula_count != null ? Number(src.formula_count).toLocaleString('zh-CN') : '—';
    const statusClass = cloned ? 'green' : partial ? 'yellow' : 'red';
    const keyTechs = (src.key_techniques || []).slice(0, 3);
    const fields = (src.fields_used || []).slice(0, 6);
    const origin = src.origin || {};

    return '' +
    '<article class="catalog-source-card ' + (cloned ? 'cloned' : 'not-cloned') + '">' +
      '<div class="catalog-card-header">' +
        '<strong>' + esc(src.short_label || src.label) + '</strong>' +
        '<b class="tag ' + statusClass + '">' + esc(cloneLabels[src.clone_status] || src.clone_status) + '</b>' +
      '</div>' +
      '<div class="catalog-card-body">' +
        '<div class="catalog-card-stat"><span>公式数量</span><strong>' + count + '</strong></div>' +
        '<div class="catalog-card-stat"><span>许可证</span><strong>' + esc(src.licence || '未知') + '</strong></div>' +
        '<div class="catalog-card-stat"><span>兼容阶段</span><strong>' + esc(phaseLabels[src.compatibility_phase] || src.compatibility_phase || '—') + '</strong></div>' +
        (src.avg_holding_days ? '<div class="catalog-card-stat"><span>平均持仓</span><strong>' + esc(src.avg_holding_days) + '天</strong></div>' : '') +
        (src.inter_factor_correlation_pct != null ? '<div class="catalog-card-stat"><span>因子间相关性</span><strong>' + esc(String(src.inter_factor_correlation_pct)) + '%</strong></div>' : '') +
        (src.note ? '<small class="catalog-card-note">' + esc(src.note) + '</small>' : '') +
        (origin.repository ? '<small class="catalog-card-origin">📦 <a href="' + esc(origin.repository) + '" target="_blank" rel="noopener">' + esc(origin.repository.replace('https://github.com/', '')) + '</a></small>' : '') +
        (origin.paper ? '<small class="catalog-card-origin">📄 ' + esc(origin.paper) + '</small>' : '') +
        (keyTechs.length ? '<div class="catalog-card-techs">' + keyTechs.map(function(t) { return '<code>' + esc(t) + '</code>'; }).join('') + '</div>' : '') +
        (fields.length ? '<div class="catalog-card-fields"><span>引用字段：</span>' + fields.map(function(f) { return '<code>' + esc(f) + '</code>'; }).join(', ') + '</div>' : '') +
      '</div>' +
    '</article>';
  }).join('') : '<p class="factory-footnote">多源因子目录尚未生成；请运行 research/multi_source_factor_catalog.py</p>';

  factory$('#catalogFootnote').textContent = (data.stages || []).filter(function(s) {
    return s.status === 'completed';
  }).length + '/' + (data.stages || []).length + ' 阶段完成 · ' + (data.generated_at || '');
}

async function refreshFactorCatalog() {
  try {
    const response = await fetch('/api/research/factor-catalog', {cache: 'no-store'});
    const body = await response.json().catch(function() { return {}; });
    if (response.ok) { renderFactorCatalog(body); }
  } catch (_) {}
}

function renderThirdPartyAudit(data) {
  var summary = data.summary || {};
  var repos = data.repos || [];
  var factory$ = function(sel) { return document.querySelector(sel); };

  factory$('#auditReportStatus').textContent = data.generated_at
    ? '报告 ' + readableTime(data.generated_at, true) : '尚无报告';
  factory$('#auditReportStatus').className = 'tag ' + (data.generated_at ? 'green' : 'gray');

  factory$('#auditRepoCount').textContent = summary.scanned_repos || '—';
  factory$('#auditPyFiles').textContent = (summary.total_python_files || 0).toLocaleString('zh-CN');
  factory$('#auditFormulaRefs').textContent = (summary.total_formula_field_references || 0).toLocaleString('zh-CN');
  factory$('#auditCredsLeaked').textContent = (summary.credentials_leaked_in_source || []).length > 0
    ? '⚠ ' + summary.credentials_leaked_in_source.length + ' 个仓库'
    : '0 ✓';
  factory$('#auditCredsLeaked').style.color = (summary.credentials_leaked_in_source || []).length > 0 ? '#ef8f91' : '';
  factory$('#auditBacktestReady').textContent = (summary.backtest_ready_standalone || []).length > 0
    ? summary.backtest_ready_standalone.length + ' 个'
    : '0';

  factory$('#auditRepoGrid').innerHTML = repos.map(function(repo) {
    var scan = repo.scan || {};
    var fc = scan.file_counts || {};
    var safety = scan.safety || {};
    var ra = scan.runnable_assessment || {};
    var bt = ra.ready_for_immediate_backtest || {};
    var fe = scan.factor_evidence || {};
    var ast = scan.python_ast || {};
    var unsafeBreakdown = safety.breakdown || {};
    var unsafeLabels = {
      dynamic_eval: '动态执行',
      system_command: '系统命令',
      pickle: 'Pickle加载',
      network_http: '网络请求',
      database_write: '数据库DDL',
      hardcoded_credentials: '凭证泄漏',
      file_write: '文件写入',
    };

    var unsafeItems = Object.keys(unsafeBreakdown).map(function(k) {
      return '<code class="unsafe-tag ' + (k === 'hardcoded_credentials' ? 'critical' : 'warning') + '">'
        + esc(unsafeLabels[k] || k) + ':' + unsafeBreakdown[k] + '</code>';
    }).join('');

    var canBacktest = bt.can_run_standalone;
    var statusTag = canBacktest ? '<b class="tag green">可独立回测</b>'
      : safety.has_credentials_in_source ? '<b class="tag red">⚠ 含凭证</b>'
      : bt.requires_specific_platform ? '<b class="tag yellow">需特定平台</b>'
      : ra.build_required ? '<b class="tag yellow">需编译构建</b>'
      : '<b class="tag gray">需数据+适配</b>';

    return '' +
    '<article class="audit-repo-card' + (canBacktest ? ' backtest-ready' : '') + (safety.has_credentials_in_source ? ' creds-leaked' : '') + '">' +
      '<div class="audit-card-header">' +
        '<strong>' + esc(repo.short_label || repo.label) + '</strong>' +
        statusTag +
      '</div>' +
      '<div class="audit-card-body">' +
        '<div class="audit-card-stats">' +
          '<span>Python <b>' + (fc.python_files || 0) + ' 文件 · ' + (ast.total_lines || 0).toLocaleString('zh-CN') + ' 行</b></span>' +
          (fc.cpp_files ? '<span>C++ <b>' + fc.cpp_files + ' 文件</b></span>' : '') +
          '<span>Alpha 引用 <b>' + (fe.alpha_keyword_references || 0) + '</b></span>' +
          '<span>公式字段引用 <b>' + (fe.formula_field_references || 0) + '</b></span>' +
        '</div>' +
        (unsafeItems ? '<div class="audit-unsafe-tags">' + unsafeItems + '</div>' : '<div class="audit-clean">✓ 静态扫描通过 · 未检出危险调用</div>') +
        (bt.blockers && bt.blockers.length ? '<small class="audit-blockers">' + bt.blockers.map(function(b) { return '⊘ ' + esc(b); }).join(' · ') + '</small>' : '') +
        '<small class="audit-licence">许可证: ' + esc(repo.licence || '未知') + ' · 提交: ' + esc((repo.scan && repo.scan.git ? repo.scan.git.commit : '').substring(0, 8) || '—') + '</small>' +
      '</div>' +
    '</article>';
  }).join('');

  factory$('#auditFootnote').textContent = [
    '第三方代码未执行 · 仅静态分析 · ' + (data.safety_boundaries || {}).no_network_during_audit ? '审计期间无网络' : '',
    summary.total_python_files + ' 文件 · ' + summary.scanned_repos + ' 仓库',
    data.generated_at || '',
  ].filter(Boolean).join(' · ');
}

async function refreshThirdPartyAudit() {
  try {
    var response = await fetch('/api/research/third-party-audit', {cache: 'no-store'});
    var body = await response.json().catch(function() { return {}; });
    if (response.ok) { renderThirdPartyAudit(body); }
  } catch (_) {}
}

function renderPipelineHealth(data) {
  var factory$ = function(sel) { return document.querySelector(sel); };

  // ── MA200 ──
  var ma = data.ma200_regime || {};
  factory$('#pipelineHealthGenerated').textContent = data.generated_at
    ? '报告 ' + readableTime(data.generated_at, true) : '尚无报告';
  factory$('#pipelineHealthGenerated').className = 'tag ' + (data.generated_at ? 'green' : 'gray');

  if (ma.available) {
    factory$('#ma200IndexClose').textContent = (ma.latest_close || 0).toLocaleString('zh-CN', {minimumFractionDigits: 2});
    factory$('#ma200Value').textContent = ma.ma200_value ? ma.ma200_value.toLocaleString('zh-CN', {minimumFractionDigits: 2}) : '计算中';
    factory$('#ma200ChangePct').textContent = ma.change_pct != null ? (ma.change_pct > 0 ? '+' : '') + ma.change_pct.toFixed(2) + '%' : '—';
    factory$('#ma200Slope').textContent = ma.ma200_slope_pct_20d != null ? (ma.ma200_slope_pct_20d > 0 ? '+' : '') + ma.ma200_slope_pct_20d.toFixed(3) + '%' : '—';
    factory$('#ma200Sessions').textContent = (ma.sessions || 0).toLocaleString('zh-CN') + ' 天';
    factory$('#ma200Description').textContent = ma.description || '';

    if (ma.above_ma200 === true) {
      factory$('#ma200StatusLabel').textContent = '多头·允许候选';
      factory$('#ma200StatusLabel').className = 'tag green';
      factory$('#ma200Card').className = 'pipeline-ma200-card bull';
    } else if (ma.above_ma200 === false) {
      factory$('#ma200StatusLabel').textContent = '熊市·过滤候选';
      factory$('#ma200StatusLabel').className = 'tag red';
      factory$('#ma200Card').className = 'pipeline-ma200-card bear';
    } else {
      factory$('#ma200StatusLabel').textContent = '未知';
      factory$('#ma200StatusLabel').className = 'tag yellow';
    }
  } else {
    factory$('#ma200IndexClose').textContent = '—';
    factory$('#ma200StatusLabel').textContent = ma.error || '无数据';
    factory$('#ma200StatusLabel').className = 'tag red';
  }

  // ── Gate Progress（2026-08-07: 门禁系统已移除，UI 元素已删除）──
  // 保留 data 读取以避免下游引用报错，但不渲染 DOM

  // ── Factor Pipeline ──
  var fc = data.factor_compatibility || {};
  var mappingComplete = fc.mapping_blocker_active === false
    && Number(fc.total_pending_mapping) === 0;
  factory$('#factorPipelineStatus').textContent = mappingComplete
    ? '历史审查完成 · 无新盲测候选'
    : (fc.blocker || '审查证据不完整 · 禁止推进');
  factory$('#fpFinHack').textContent = fc.finhack
    ? (fc.finhack.execution_tested || 0) + ' / ' + (fc.finhack.total_formulas || 0)
    : '—';
  factory$('#fpPopbo').textContent = fc.popbo_alphas
    ? (fc.popbo_alphas.execution_tested || 0) + ' / ' + (fc.popbo_alphas.total_functions || 0)
    : '—';
  factory$('#fpReviewed').textContent = (fc.legacy_formula_records_reviewed || 0).toLocaleString('zh-CN') + ' 条';
  factory$('#fpProductionEligible').textContent = (fc.production_eligible_count || 0).toLocaleString('zh-CN') + ' 条';
  factory$('#fpNextAction').textContent = fc.next_action || fc.blocker || '等待来源审计结果';

  // ── Historical DeepSeek evidence ──
  var ds = data.deepseek_efficiency || {};
  factory$('#dsEfficiency').textContent = ds.efficiency || 'N/A';
  factory$('#dsRounds').textContent = (ds.total_rounds || 0).toLocaleString('zh-CN') + ' 轮';
  factory$('#dsActionable').textContent = (ds.actionable_candidates || 0) + ' 个';
  factory$('#dsRate').textContent = ds.efficiency || 'N/A';
  factory$('#dsRecommendation').textContent = [
    '只读历史记录；外部付费 API 已禁用',
    ds.recommendation || ds.problem,
  ].filter(Boolean).join(' · ');
  if (ds.total_rounds > 100 && ds.actionable_candidates === 0) {
    document.querySelector('#deepseekPipelineCard').className = 'pipeline-info-card efficiency-critical';
  }
}

async function refreshPipelineHealth() {
  try {
    var response = await fetch('/api/research/pipeline-health', {cache: 'no-store'});
    var body = await response.json().catch(function() { return {}; });
    if (response.ok) { renderPipelineHealth(body); }
  } catch (_) {}
}

function renderObjective(objective) {
  const items = [
    ['主目标', objective.primary],
    ['评价顺序', objective.ranking],
    ['试运营资金', `¥${Number(objective.capital || 0).toLocaleString('zh-CN')}`],
    ['持有周期', objective.holding_days],
    ['每日候选', objective.candidate_count],
    ['回撤边界', `预警 ${objective.drawdown_warning_pct}% · 停止 ${objective.drawdown_hard_stop_pct}%`],
  ];
  factory$('#objectiveStrip').innerHTML = items.map(([label, value]) => `
    <div class="objective-item">
      <span>${esc(label)}</span>
      <strong title="${esc(value)}">${esc(value)}</strong>
    </div>
  `).join('');
}

function renderV25Campaign(campaign) {
  if (!campaign || !factory$('#v25CampaignPanel')) return;
  const overall = Math.max(0, Math.min(
    100, Number(campaign.overall_progress_pct || 0),
  ));
  const remaining = Math.max(0, Math.min(
    100, Number(campaign.remaining_pct ?? (100 - overall)),
  ));
  const phase = Math.max(0, Math.min(
    100, Number(campaign.phase_progress_pct || 0),
  ));
  const stateLabels = {
    queued: '已排队',
    running: '本地计算中',
    review_pending: '等待本地复核',
    ai_review: '外部付费 API 已禁用',
    completed: '本批完成',
    failed: '真实失败',
  };
  const stateColors = {
    queued: 'gray',
    running: 'yellow',
    review_pending: 'yellow',
    ai_review: 'gray',
    completed: 'green',
    failed: 'red',
  };
  factory$('#v25CampaignStatus').textContent = stateLabels[campaign.state]
    || campaign.state || '等待队列';
  factory$('#v25CampaignStatus').className = `tag ${stateColors[campaign.state] || 'gray'}`;
  factory$('#v25OverallPct').textContent = `${number(overall, 1)}%`;
  factory$('#v25OverallBar').style.width = `${overall}%`;
  factory$('#v25RemainingPct').textContent = `已完成 ${number(overall, 1)}% · 剩余 ${number(remaining, 1)}%`;
  factory$('#v25PhasePct').textContent = `${number(phase, 1)}%`;
  factory$('#v25PhaseBar').style.width = `${phase}%`;
  const completedUnits = Number(campaign.phase_completed_units || 0);
  const totalUnits = Number(campaign.phase_total_units || 0);
  factory$('#v25PhaseUnits').textContent = totalUnits
    ? `${completedUnits.toLocaleString('zh-CN')} / ${totalUnits.toLocaleString('zh-CN')} 个真实工作单元`
    : '当前阶段没有可计数工作单元';
  factory$('#v25Eta').textContent = campaign.eta_seconds === 0
    ? '已完成'
    : campaign.eta_seconds === null || campaign.eta_seconds === undefined
      ? '等待真实速率'
      : `约 ${durationText(campaign.eta_seconds)}`;
  factory$('#v25EtaBasis').textContent = campaign.eta_label || '条件估算待建立';
  factory$('#v25ActualCompute').textContent = durationText(
    campaign.actual_compute_seconds || 0,
  );
  factory$('#v25Result').textContent = campaign.report_generated_at
    ? `统计通过 ${Number(campaign.statistical_gate_pass_count || 0)}/3 项 · ${readableTime(campaign.report_generated_at, true)}裁决`
    : '尚无统计裁决';
  factory$('#v25CurrentTask').textContent = campaign.current_task || '等待任务';
  factory$('#v25CurrentPhase').textContent = [
    campaign.current_phase,
    campaign.current_unit ? `当前 ${campaign.current_unit}` : null,
  ].filter(Boolean).join(' · ') || '等待队列';
  const taskLabels = {
    completed: '已完成',
    running: '进行中',
    pending: '等待前序',
    failed: '失败',
  };
  factory$('#v25TaskList').innerHTML = (campaign.tasks || []).map((item) => `
    <div class="v25-task ${esc(item.status || 'pending')}">
      <span>${Number(item.weight_pct || 0)}% · ${esc(taskLabels[item.status] || item.status || '等待')}</span>
      <b>${esc(item.label || item.id)}</b>
    </div>
  `).join('');
  factory$('#v25Definition').textContent = campaign.definition || '';
}

function renderGateLevels(data) {
  // 2026-08-07: 门禁系统已移除
}

function renderSummary(data) {
  const summary = data.summary || {};
  factory$('#factoryGate').textContent = '研究就绪';
  factory$('#factoryGate').className = 'up';
  factory$('#factoryGateReason').textContent = '门禁系统已移除，研究功能直接可用';
  factory$('#factoryCoverage').textContent = `${number(summary.coverage_pct)}%`;
  factory$('#factoryCoverageDetail').textContent = (
    `${Number(summary.downloaded_symbols).toLocaleString('zh-CN')} / `
    + `${Number(summary.target_symbols).toLocaleString('zh-CN')} 只 · `
    + `${Number(summary.quality_rows).toLocaleString('zh-CN')} 行已审计`
  );
  factory$('#factoryFactors').textContent = (
    `V4 ${summary.formal_statistical_gate_pass_count ?? 0}/${summary.formal_factor_test_count ?? 0}`
    + ` · V4.2 ${summary.v4_2_statistical_gate_pass_count ?? 0}/${summary.v4_2_test_count ?? 0}`
  );
  factory$('#factoryWorker').textContent = summary.worker_live
    ? (summary.worker_work_sequence
      ? `在线 · 真实任务 #${summary.worker_work_sequence}`
      : '在线 · 等待新输入')
    : (summary.worker_status === 'running' ? '心跳中断' : '未运行');
  factory$('#factoryWorker').className = summary.worker_live ? 'up' : 'down';
  factory$('#factoryWorkerDetail').textContent = (
    `最近心跳 ${readableTime(summary.worker_heartbeat_at, true)}`
  );
}

function renderResearchTruth(truth) {
  if (!truth || !factory$('#truthCurrentWork')) return;
  const work = truth.current_work || {};
  const health = truth.data_health || {};
  const primary = health.primary || {};
  const fallback = health.fallback || {};
  const ledger = truth.ledger || {};
  const registry = truth.open_source_registry || {};
  const nextRegistry = truth.next_generation_registry || {};
  const emergingRegistry = truth.emerging_registry || {};
  const frontierRegistry = truth.frontier_registry || {};
  const software = truth.software_tests || {};
  const candidates = Object.values(registry.candidates || {});

  factory$('#truthCurrentWork').textContent = work.step || '等待新输入';
  factory$('#truthCurrentReason').textContent = work.reason || '暂无实际任务';
  factory$('#truthCompletionCondition').textContent = [
    software.status === 'passed'
      ? `代码单元测试 ${Number(software.passed || 0)}/${Number(software.total || 0)} 通过`
      : software.status === 'failed'
        ? `代码单元测试失败 ${Number(software.failed || 0)} 项`
        : '代码单元测试状态待刷新',
    `研究完成条件：${work.completion_condition || '--'}`,
  ].join(' · ');
  factory$('#truthPrimarySource').textContent = `${primary.name || '主数据源'} · ${primary.state || '未知'}`;
  factory$('#truthPrimaryDetail').textContent = primary.error || `最后心跳：${readableTime(primary.last_heartbeat, true)}`;
  factory$('#truthFallbackSource').textContent = `${fallback.name || '备用源'} · ${fallback.state || '--'} · ${fallback.detail || ''}`;
  factory$('#truthLedger').textContent = `真实执行 ${ledger.actual_commands || 0} 项 · 去重 ${ledger.deduplicated_skips || 0} 项`;
  factory$('#truthLedgerDetail').textContent = `累计实际计算 ${durationText(ledger.actual_compute_seconds)} · 队列运行中 ${ledger.queue_running || 0}/${ledger.queue_total || 0}`;
  factory$('#truthRegistry').textContent = `已验证 ${registry.completed || 0}/${registry.total || '--'} 项`;
  const next = registry.next_candidate || {};
  const screeningEta = registry.screening_eta_seconds;
  const etaText = screeningEta === null || screeningEta === undefined
    ? 'ETA等待实际样本'
    : `候选筛选ETA约${durationText(screeningEta)}`;
  factory$('#truthRegistryDetail').textContent = next.factor_id
    ? `${registry.current_candidate ? `当前：${registry.current_candidate} · ` : ''}下一项：${next.label || next.factor_id} · ${etaText}`
    : `所有已注册候选均已有独立结果 · ${etaText}`;
  factory$('#truthVolumeRatio').textContent = `${registry.intraday_volume_ratio?.label || '盘中实时量比'}：${registry.intraday_volume_ratio?.validation_status || '待验证'}`;
  factory$('#truthRegistryTag').textContent = candidates.length
    ? `完成 ${candidates.length}/${registry.total || candidates.length}`
    : '等待首个候选';
  factory$('#truthRegistryTag').className = `tag ${candidates.length ? 'yellow' : 'gray'}`;
  factory$('#truthRegistryRows').innerHTML = candidates.length
    ? candidates.slice(-8).reverse().map((item) => {
      const candidate = item.candidate || {};
      const passed = item.status === 'passed_statistical_gate';
      const oneDay = item.result?.horizons?.['1'] || {};
      const validation = oneDay.splits?.validation || {};
      const test = oneDay.splits?.test || {};
      const failed = Object.entries(oneDay.gate_conditions || {})
        .filter(([, value]) => !value)
        .map(([name]) => name)
        .join('、');
      return `<article class="registry-row ${passed ? 'passed' : 'failed'}">
        <span>${esc(candidate.family || 'candidate')} · ${esc(candidate.source || 'open source')}</span>
        <strong>${esc(candidate.label || candidate.factor_id || '--')}</strong>
        <small>${esc(candidate.formula || '公式未记录')}</small>
        <small>验证IC ${number(validation.mean_ic, 6)} · 测试IC ${number(test.mean_ic, 6)} · 扣10BP头部-中间 ${pct(test.top_minus_middle_after_cost_pct, 5)}</small>
        <small>${esc(item.status === 'passed_statistical_gate'
          ? '统计通过，等待独立T+1、整数手、完整成本策略回测'
          : `统计失败并封存${failed ? `：${failed}` : ''}`)}</small>
      </article>`;
    }).join('')
    : '<p class="factory-footnote">首项全市场候选尚未完成；不会用空白或模拟结果填充。</p>';

  const v27Candidates = Object.values(nextRegistry.candidates || {});
  const v27Tag = factory$('#v27RegistryTag');
  const v27Summary = factory$('#v27RegistrySummary');
  const v27Rows = factory$('#v27RegistryRows');
  const strategyAdjudication = nextRegistry.strategy_adjudication || {};
  if (v27Tag) {
    const statisticalPasses = Number(nextRegistry.statistical_passes || 0);
    const costFailed = strategyAdjudication.available
      && !strategyAdjudication.historical_strategy_gate_passed;
    v27Tag.textContent = costFailed
      ? `统计通过 ${statisticalPasses} 项 · 成本门禁失败`
      : `完成 ${nextRegistry.completed || 0}/${nextRegistry.total || 6} · 统计通过 ${statisticalPasses}`;
    v27Tag.className = `tag ${costFailed ? 'red' : statisticalPasses > 0 ? 'yellow' : 'red'}`;
  }
  if (v27Summary) {
    const source = nextRegistry.source || {};
    const scope = nextRegistry.scope || {};
    v27Summary.textContent = [
      source.repository ? `来源 ${source.repository}` : '来源 Microsoft Qlib Alpha158',
      source.pinned_commit ? `固定提交 ${String(source.pinned_commit).slice(0, 12)}` : null,
      source.license ? `许可证 ${source.license}` : null,
      scope.label || '沪深主板与创业板',
      nextRegistry.current_candidate ? `当前 ${nextRegistry.current_candidate}` : null,
      nextRegistry.rule,
    ].filter(Boolean).join(' · ');
  }
  if (v27Rows) {
    v27Rows.innerHTML = v27Candidates.length
      ? v27Candidates.slice(-8).reverse().map((item) => {
        const candidate = item.candidate || {};
        const fiveDay = item.result?.horizons?.['5'] || {};
        const validation = fiveDay.splits?.validation || {};
        const test = fiveDay.splits?.test || {};
        const failed = Object.entries(fiveDay.gate_conditions || {})
          .filter(([, value]) => !value)
          .map(([name]) => name)
          .join('、');
        const passed = item.status === 'passed_statistical_gate';
        return `<article class="registry-row ${passed ? 'passed' : 'failed'}">
          <span>${esc(candidate.family || 'new mechanism')} · ${esc(candidate.source || 'Microsoft Qlib')}</span>
          <strong>${esc(candidate.label || candidate.factor_id || '--')}</strong>
          <small>${esc(candidate.formula || '公式未记录')}</small>
          <small>5日验证IC ${number(validation.mean_ic, 6)} · 测试IC ${number(test.mean_ic, 6)} · 测试CI [${number(test.block_bootstrap_95_ci?.[0], 6)}, ${number(test.block_bootstrap_95_ci?.[1], 6)}]</small>
          <small>单调性 ${number(test.decile_monotonicity_spearman, 5)} · 扣往返10BP头部-中间 ${pct(test.top_minus_middle_after_cost_pct, 5)}</small>
          <small>${esc(passed
            ? (strategyAdjudication.available
              ? '仅历史统计通过；完整含成本策略门禁未通过'
              : '统计通过，等待T+1、整数手、完整成本策略回测')
            : `统计失败并封存${failed ? `：${failed}` : ''}`)}</small>
        </article>`;
      }).join('')
      : '<p class="factory-footnote">V27 已预注册，尚无完成结果；不会用旧因子或模拟数据填充。</p>';
  }

  renderV27Adjudication({
    strategy_adjudication: strategyAdjudication,
    weighted_ensemble_adjudication: (
      nextRegistry.weighted_ensemble_adjudication || {}
    ),
  });

  const v28Candidates = Object.values(emergingRegistry.candidates || {});
  const v28Tag = factory$('#v28RegistryTag');
  const v28Summary = factory$('#v28RegistrySummary');
  const v28Rows = factory$('#v28RegistryRows');
  const v28Progress = emergingRegistry.progress || {};
  if (v28Tag) {
    const v28Passes = Number(emergingRegistry.statistical_passes || 0);
    const v28Running = v28Progress.status === 'running';
    v28Tag.textContent = v28Running
      ? `真实计算 ${number(v28Progress.progress_pct, 1)}% · ${Number(v28Progress.processed_symbols || 0).toLocaleString('zh-CN')}/${Number(v28Progress.total_symbols || 0).toLocaleString('zh-CN')}只`
      : `完成 ${emergingRegistry.completed || 0}/${emergingRegistry.total || 4} · 校正后通过 ${v28Passes}`;
    v28Tag.className = `tag ${v28Running ? 'yellow' : v28Passes > 0 ? 'yellow' : Number(emergingRegistry.completed || 0) ? 'red' : 'gray'}`;
  }
  if (v28Summary) {
    const source = emergingRegistry.source || {};
    const correction = emergingRegistry.multiple_testing || {};
    v28Summary.textContent = [
      source.repository ? `来源 ${source.repository}` : '来源 Microsoft Qlib Alpha158',
      source.pinned_commit ? `固定提交 ${String(source.pinned_commit).slice(0, 12)}` : null,
      source.license ? `许可证 ${source.license}` : null,
      `多重检验 ${correction.family_size || 16} 项`,
      `单项阈值 ${number(correction.per_test_alpha ?? 0.003125, 6)}`,
      emergingRegistry.current_candidate ? `当前 ${emergingRegistry.current_candidate}` : null,
      emergingRegistry.rule,
    ].filter(Boolean).join(' · ');
  }
  if (v28Rows) {
    v28Rows.innerHTML = v28Candidates.length
      ? v28Candidates.slice(-8).reverse().map((item) => {
        const candidate = item.candidate || {};
        const fiveDay = item.result?.horizons?.['5'] || {};
        const validation = fiveDay.splits?.validation || {};
        const test = fiveDay.splits?.test || {};
        const correction = fiveDay.multiple_testing || {};
        const failed = Object.entries(fiveDay.gate_conditions || {})
          .filter(([, value]) => !value)
          .map(([name]) => name)
          .join('、');
        const passed = item.status === 'passed_statistical_gate';
        return `<article class="registry-row ${passed ? 'passed' : 'failed'}">
          <span>${esc(candidate.family || 'price geometry')} · ${esc(candidate.source || 'Microsoft Qlib')}</span>
          <strong>${esc(candidate.label || candidate.factor_id || '--')}</strong>
          <small>${esc(candidate.formula || '公式未记录')}</small>
          <small>5日验证IC ${number(validation.mean_ic, 6)} · 测试IC ${number(test.mean_ic, 6)} · 单侧p ${number(fiveDay.test_one_sided_block_bootstrap_p, 6)}</small>
          <small>Bonferroni阈值 ${number(correction.per_test_alpha ?? 0.003125, 6)} · 扣往返10BP头部-中间 ${pct(test.top_minus_middle_after_cost_pct, 5)}</small>
          <small>${esc(passed
            ? '家族校正后统计通过；仍须完整含成本策略和独立前向验证'
            : `统计失败并封存${failed ? `：${failed}` : ''}`)}</small>
        </article>`;
      }).join('')
      : '<p class="factory-footnote">V28 四项公式已经预注册；尚无结果，不会用V27或模拟数据填充。</p>';
  }

  const v29Candidates = Object.values(frontierRegistry.candidates || {});
  const v29Tag = factory$('#v29RegistryTag');
  const v29Summary = factory$('#v29RegistrySummary');
  const v29Rows = factory$('#v29RegistryRows');
  const v29Progress = frontierRegistry.progress || {};
  if (v29Tag) {
    const v29Passes = Number(frontierRegistry.statistical_passes || 0);
    const v29Running = v29Progress.status === 'running';
    v29Tag.textContent = v29Running
      ? `真实计算 ${number(v29Progress.progress_pct, 1)}% · ${Number(v29Progress.processed_symbols || 0).toLocaleString('zh-CN')}/${Number(v29Progress.total_symbols || 0).toLocaleString('zh-CN')}只`
      : `完成 ${frontierRegistry.completed || 0}/${frontierRegistry.total || 4} · 校正后通过 ${v29Passes}`;
    v29Tag.className = `tag ${v29Running ? 'yellow' : v29Passes > 0 ? 'yellow' : Number(frontierRegistry.completed || 0) ? 'red' : 'gray'}`;
  }
  if (v29Summary) {
    const source = frontierRegistry.source || {};
    const correction = frontierRegistry.multiple_testing || {};
    v29Summary.textContent = [
      source.repository ? `来源 ${source.repository}` : '来源 Microsoft Qlib Alpha158',
      source.pinned_commit ? `固定提交 ${String(source.pinned_commit).slice(0, 12)}` : null,
      source.license ? `许可证 ${source.license}` : null,
      `多重检验 ${correction.family_size || 16} 项`,
      `单项阈值 ${number(correction.per_test_alpha ?? 0.003125, 6)}`,
      frontierRegistry.current_candidate ? `当前 ${frontierRegistry.current_candidate}` : null,
      frontierRegistry.rule,
    ].filter(Boolean).join(' · ');
  }
  if (v29Rows) {
    v29Rows.innerHTML = v29Candidates.length
      ? v29Candidates.slice(-8).reverse().map((item) => {
        const candidate = item.candidate || {};
        const fiveDay = item.result?.horizons?.['5'] || {};
        const validation = fiveDay.splits?.validation || {};
        const test = fiveDay.splits?.test || {};
        const correction = fiveDay.multiple_testing || {};
        const failed = Object.entries(fiveDay.gate_conditions || {})
          .filter(([, value]) => !value)
          .map(([name]) => name)
          .join('、');
        const passed = item.status === 'passed_statistical_gate';
        return `<article class="registry-row ${passed ? 'passed' : 'failed'}">
          <span>${esc(candidate.family || 'path and volume structure')} · ${esc(candidate.source || 'Microsoft Qlib')}</span>
          <strong>${esc(candidate.label || candidate.factor_id || '--')}</strong>
          <small>${esc(candidate.formula || '公式未记录')}</small>
          <small>5日验证IC ${number(validation.mean_ic, 6)} · 测试IC ${number(test.mean_ic, 6)} · 单侧p ${number(fiveDay.test_one_sided_block_bootstrap_p, 6)}</small>
          <small>Bonferroni阈值 ${number(correction.per_test_alpha ?? 0.003125, 6)} · 扣往返10BP头部-中间 ${pct(test.top_minus_middle_after_cost_pct, 5)}</small>
          <small>${esc(passed
            ? '家族校正后统计通过；仍须完整含成本策略和独立前向验证'
            : `统计失败并封存${failed ? `：${failed}` : ''}`)}</small>
        </article>`;
      }).join('')
      : '<p class="factory-footnote">V29 四项路径/量能机制已预注册；等待V28裁决后自动接续，不以旧结果填充。</p>';
  }
  renderV29Adjudication({
    strategy_adjudication: frontierRegistry.strategy_adjudication || {},
    weighted_ensemble_adjudication: (
      frontierRegistry.weighted_ensemble_adjudication || {}
    ),
  });
}

function renderV27Adjudication(data) {
  const tag = factory$('#v27AdjudicationTag');
  const rows = factory$('#v27AdjudicationRows');
  if (!tag || !rows) return;
  const strategy = data.strategy_adjudication || {};
  const ensemble = data.weighted_ensemble_adjudication || {};
  if (!strategy.available) {
    tag.textContent = '等待成本回测';
    tag.className = 'tag gray';
    rows.innerHTML = '<p class="factory-footnote">统计候选尚未进入完整成本策略裁决。</p>';
    return;
  }
  const metrics = strategy.observed_test_10bp || {};
  const strategyPassed = Boolean(strategy.historical_strategy_gate_passed);
  const ensemblePassed = Boolean(ensemble.historical_strategy_gate_passed);
  tag.textContent = strategyPassed ? '历史成本门禁通过' : '成本门禁失败 · 真钱锁定';
  tag.className = `tag ${strategyPassed ? 'green' : 'red'}`;
  rows.innerHTML = `
    <article class="registry-row ${strategyPassed ? 'passed' : 'failed'}">
      <span>历史观察测试段 · T+1 · 整数手 · 往返10BP</span>
      <strong>${strategyPassed ? '历史成本策略通过' : '历史成本策略未通过 · 禁止实盘'}</strong>
      <small>${esc(strategy.factor_id || '--')} · 持有 ${Number(strategy.holding_sessions || 0)} 个交易日 · 最多 ${Number(strategy.maximum_positions || 0)} 只</small>
      <small>${Number(metrics.trades || 0)}笔 · 胜率 ${number(metrics.win_rate_pct, 3)}% · 年化 ${pct(metrics.annualized_return_pct, 3)} · 累计 ${pct(metrics.total_return_pct, 3)}</small>
      <small>最大回撤 ${number(metrics.max_drawdown_pct, 3)}% · 盈利因子 ${number(metrics.profit_factor, 3)} · 失败门禁 ${Number(strategy.failed_gates?.length || 0)} 项</small>
    </article>
    <article class="registry-row ${ensemblePassed ? 'passed' : 'failed'}">
      <span>训练期IC冻结加权 · 不用失败因子凑组合</span>
      <strong>${ensemblePassed ? '加权组合历史门禁通过' : '加权组合未成立 · 禁止实盘'}</strong>
      <small>可用统计成分 ${Number(ensemble.available_components || 0)} / ${Number(ensemble.required_components || 2)} · 已登记权重 ${Number(ensemble.weights?.length || 0)} 项</small>
      <small>${esc(ensemble.warning || '组合证据尚未满足预注册条件。')}</small>
      <small>这不是独立前向结果；后续必须重新预注册新机制，不能在测试段调门槛。</small>
    </article>`;
}

function renderV29Adjudication(data) {
  const tag = factory$('#v29AdjudicationTag');
  const rows = factory$('#v29AdjudicationRows');
  if (!tag || !rows) return;
  const strategy = data.strategy_adjudication || {};
  const ensemble = data.weighted_ensemble_adjudication || {};
  const ensembleReady = Boolean(ensemble.available);
  if (!strategy.available && !ensembleReady) {
    tag.textContent = '等待统计筛选完成';
    tag.className = 'tag gray';
    rows.innerHTML = '<p class="factory-footnote">四项预注册候选完成前，不生成策略或组合结果；至少两个独立统计通过项才允许训练期加权。</p>';
    return;
  }
  const metrics = strategy.observed_test_10bp || {};
  const strategyPassed = Boolean(strategy.historical_strategy_gate_passed);
  const ensemblePassed = Boolean(ensemble.historical_strategy_gate_passed);
  tag.textContent = strategyPassed ? '历史成本门禁通过' : '未通过 · 真钱锁定';
  tag.className = `tag ${strategyPassed ? 'green' : 'red'}`;
  rows.innerHTML = `
    <article class="registry-row ${strategyPassed ? 'passed' : 'failed'}">
      <span>历史观察测试段 · T+1 · 整数手 · 往返10BP</span>
      <strong>${strategyPassed ? 'V29 历史成本策略通过' : 'V29 历史成本策略未成立 · 禁止实盘'}</strong>
      <small>${esc(strategy.factor_id || '无统计通过候选')} · 持有 ${Number(strategy.holding_sessions || 0)} 个交易日 · 最多 ${Number(strategy.maximum_positions || 0)} 只</small>
      <small>${Number(metrics.trades || 0)}笔 · 胜率 ${number(metrics.win_rate_pct, 3)}% · 年化 ${pct(metrics.annualized_return_pct, 3)} · 累计 ${pct(metrics.total_return_pct, 3)}</small>
      <small>最大回撤 ${number(metrics.max_drawdown_pct, 3)}% · 盈利因子 ${number(metrics.profit_factor, 3)} · 失败门禁 ${Number(strategy.failed_gates?.length || 0)} 项</small>
    </article>
    <article class="registry-row ${ensemblePassed ? 'passed' : 'failed'}">
      <span>只用训练期正IC冻结权重 · 不用测试段调权</span>
      <strong>${ensemblePassed ? 'V29 加权组合历史门禁通过' : 'V29 加权组合未成立 · 禁止实盘'}</strong>
      <small>可用统计成分 ${Number(ensemble.available_components || 0)} / ${Number(ensemble.required_components || 2)} · 已登记权重 ${Number(ensemble.weights?.length || 0)} 项</small>
      <small>${esc(ensemble.warning || '组合证据尚未满足预注册条件。')}</small>
      <small>以上仍是历史证据；独立前向未达标前，模拟与真钱门禁保持锁定。</small>
    </article>`;
}

function renderStages(stages) {
  const labels = {
    passed: '已通过',
    running: '进行中',
    failed: '失败已封存',
    blocked: '被阻止',
    locked: '已锁定',
    waiting: '等待前序',
    ready: '可以开始',
    preparing: '并行准备',
    research_only: '仅研究',
    pilot_ready: '人工试运营开放',
    historical_only: '仅历史观察',
  };
  factory$('#pipelineStages').innerHTML = stages.map((stage) => `
    <div class="pipeline-stage ${esc(stage.status)}">
      <span>${esc(stage.label)} · ${esc(labels[stage.status] || stage.status)}</span>
      <strong>${esc(stage.value)}</strong>
      <small>${esc(stage.detail)}</small>
    </div>
  `).join('');
}

function renderLabelProtocol(protocol) {
  const horizons = (protocol.holding_horizons_trading_days || [])
    .map((item) => `${item}日`)
    .join(' / ');
  const availability = protocol.availability || {};
  const coverage = Object.entries(availability).map(([label, item]) => {
    const horizon = label.match(/(\d+)d/)?.[1] || label;
    return `${horizon}日 ${number(item.coverage_pct, 2)}%`;
  }).join(' · ');
  factory$('#labelProtocol').innerHTML = `
    <div>
      <span>信号形成</span>
      <strong>${esc(protocol.information_time)}</strong>
    </div>
    <i>→</i>
    <div>
      <span>可执行入场</span>
      <strong>${esc(protocol.entry_price)}</strong>
    </div>
    <i>→</i>
    <div>
      <span>持有标签</span>
      <strong>${esc(horizons || '--')}</strong>
    </div>
    <i>→</i>
    <div>
      <span>退出估值</span>
      <strong>${esc(protocol.exit_price)}</strong>
    </div>
    <p>
      入场和退出必须可交易 · 标签禁止进入因子列 · ${esc(protocol.costs)}
      ${coverage ? `<b>当前标签覆盖：${esc(coverage)}</b>` : '<b>等待下一轮全量管线生成标签覆盖率</b>'}
    </p>
  `;
}

function renderAcceleratedValidation(validation, deliveryPlan) {
  // The long-running web process may still be serving the previous cached
  // template while static assets update immediately. Keep the rest of the
  // research page usable until the next safe server restart loads this panel.
  if (!factory$('#acceleratedStatus')) return;
  const statusLabels = {
    passed: '历史仿真通过',
    failed: '本候选门禁失败',
    waiting_candidate: '等待新候选',
    preparing_candidate: '准备候选数据',
    blocked: '等待前置数据',
    running: '历史仿真运行中',
    completed: '历史仿真已完成',
  };
  const statusClasses = {
    passed: 'green',
    failed: 'red',
    waiting_candidate: 'yellow',
    preparing_candidate: 'yellow',
    blocked: 'red',
    running: 'yellow',
    completed: 'green',
  };
  const status = validation.status || 'waiting_candidate';
  const badge = factory$('#acceleratedStatus');
  badge.textContent = statusLabels[status] || status;
  badge.className = `tag ${statusClasses[status] || 'gray'}`;
  factory$('#acceleratedDecision').textContent = validation.decision || '--';
  factory$('#acceleratedPurpose').textContent = validation.purpose || '';
  factory$('#acceleratedBoundary').textContent = validation.does_not_replace
    ? `不能替代：${validation.does_not_replace}`
    : '';
  const runtime = validation.runtime || {};
  const terminalPipeline = ['completed', 'failed', 'blocked'].includes(
    runtime.status,
  );
  const backtestStarted = Boolean(runtime.backtest_clock_started);
  factory$('#acceleratedRuntime').innerHTML = [
    ['候选', runtime.candidate_label || validation.latest_strategy || '--'],
    ['整条管线已运行', durationText(runtime.pipeline_elapsed_seconds)],
    ['历史回测已运行', backtestStarted
      ? durationText(runtime.backtest_elapsed_seconds) : '尚未开始'],
    ['目标计算窗口', runtime.target_hours
      ? `${number(runtime.target_hours, 0)}小时` : '10–15小时'],
    ['当前阶段', runtime.current_phase_label || '等待新候选'],
    ['当前阶段进度', runtime.phase_total_units
      ? `${number(runtime.phase_progress_pct, 1)}% · ${number(runtime.phase_completed_units, 0)}/${number(runtime.phase_total_units, 0)}`
      : '等待数据构建'],
    ['预计剩余', backtestStarted
      ? durationText(runtime.estimated_remaining_seconds)
      : runtime.phase_estimated_remaining_seconds !== null
        && runtime.phase_estimated_remaining_seconds !== undefined
        ? durationText(runtime.phase_estimated_remaining_seconds)
        : '采集开始后计算'],
    ['管线心跳', terminalPipeline
      ? `${runtime.status === 'completed' ? '已完成' : '已停止'} · 终态无需心跳`
      : runtime.heartbeat_age_seconds === null
        || runtime.heartbeat_age_seconds === undefined
        ? '尚未建立'
        : `${durationText(runtime.heartbeat_age_seconds)}前 · ${
          runtime.heartbeat_healthy ? '正常' : '已中断'
        }`],
  ].map(([label, value]) => `
    <div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>
  `).join('');
  const progress = Number(runtime.progress_pct || 0);
  factory$('#acceleratedProgressText').textContent = (
    `${number(progress, 1)}% · ${runtime.current_phase_label || '等待阶段'}`
  );
  factory$('#acceleratedProgressBar').style.width = `${
    Math.max(0, Math.min(100, progress))
  }%`;
  const protocolLabels = {
    passed: '已通过',
    failed: '未通过',
    waiting: '等待策略',
    blocked: '被阻止',
    running: '进行中',
  };
  factory$('#acceleratedProtocol').innerHTML = (validation.protocol || []).map((item) => `
    <div class="accelerated-step ${esc(item.status)}">
      <i></i>
      <strong>${esc(item.label)}</strong>
      <span>${esc(protocolLabels[item.status] || item.status)}</span>
    </div>
  `).join('');
  factory$('#deliveryPlan').innerHTML = (deliveryPlan || []).map((item) => `
    <div class="delivery-card ${esc(item.status)}">
      <span>${esc(item.target_date || '条件满足后')}</span>
      <strong>${esc(item.label)}</strong>
      <p>${esc(item.allowed)}</p>
      <small>${esc(item.boundary)}</small>
    </div>
  `).join('');
}

function renderFifteenHourSession(session) {
  if (!factory$('#fifteenHourStatus')) return;
  const campaign = session?.campaign || {};
  const active = Boolean(session?.is_actively_computing || campaign?.is_actively_computing);
  const complete = session?.status === 'compute_budget_complete';
  const matrixComplete = Boolean(campaign?.baseline_complete);
  factory$('#fifteenHourStatus').textContent = matrixComplete
    ? '固定验证已完成'
    : complete ? '15小时计算已完成'
    : active ? '真实计算中' : '在线等待有效任务';
  factory$('#fifteenHourStatus').className = `tag ${
    matrixComplete || complete ? 'green' : active ? 'yellow' : 'gray'
  }`;
  const productive = Number(session?.productive_compute_seconds || 0);
  const target = Number(session?.target_compute_seconds || 54000);
  const progress = Number(session?.time_budget_progress_pct || 0);
  const wall = Number(session?.wall_elapsed_seconds || 0);
  const excluded = Number(session?.excluded_time_seconds || 0);
  factory$('#fifteenHourCompute').textContent = `有效 ${durationText(productive)}`;
  factory$('#fifteenHourWall').textContent = [
    `会话开始 ${readableTime(session?.started_at, true)}`,
    `自然运行 ${durationText(wall)}`,
    `非计算 ${durationText(excluded)}`,
  ].join(' · ');
  const task = session?.current_task || {};
  factory$('#fifteenHourTask').textContent = (
    campaign?.current_task || task.label || '等待下一项真实本地任务'
  );
  factory$('#fifteenHourTaskTime').textContent = campaign?.current_phase
    ? `${campaign.current_phase} · 稳健性复核 ${Number(campaign.robustness_rounds || 0)} 轮`
    : task.label
    ? `本项已计算 ${durationText(task.elapsed_seconds || 0)} · 第${task.cycle || 0}轮`
    : `已完成 ${Number(session?.completed_task_count || 0)} 项有效命令`;
  const passedCandidates = Number(campaign?.passed_candidate_count || 0);
  factory$('#fifteenHourGate').textContent = campaign?.baseline_complete
    ? passedCandidates > 0
      ? `${passedCandidates}个统计候选通过，待策略成本门禁`
      : '统计门禁未通过，交易保持锁定'
    : complete ? '基础矩阵未完成，保持锁定' : '仍在积累证据';
  const timeProgressLabel = factory$('#fifteenHourProgressLabel');
  factory$('#fifteenHourProgressText').textContent = matrixComplete
    ? '固定矩阵 100.000%'
    : `${number(progress, 3)}%`;
  if (timeProgressLabel) {
    timeProgressLabel.firstChild.textContent = matrixComplete
      ? '固定验证工作已结束（15小时是上限，不需要用重复实验填满） '
      : '有效计算预算（15小时为上限，不是必须填满） ';
  }
  factory$('#fifteenHourProgressBar').style.width = `${
    matrixComplete ? 100 : Math.max(0, Math.min(100, progress))
  }%`;
  const completedWork = Number(campaign?.completed_work_units || 0);
  const totalWork = Number(campaign?.total_work_units || 0);
  const workProgress = Number(campaign?.work_progress_pct || (
    totalWork ? completedWork / totalWork * 100 : 0
  ));
  if (factory$('#fifteenHourWork')) {
    factory$('#fifteenHourWork').textContent = totalWork
      ? `${completedWork.toLocaleString()} / ${totalWork.toLocaleString()} 项`
      : '正在建立任务矩阵';
    factory$('#fifteenHourCoverage').textContent = [
      `${Number(campaign?.downloaded_symbols || 4802).toLocaleString()}只已下载A股`,
      `${Number(campaign?.source_rows || 0).toLocaleString()}条可检验记录`,
      `${Number(campaign?.rows_evaluated || 0).toLocaleString()}条累计检验`,
    ].join(' · ');
    factory$('#fifteenHourWorkProgressText').textContent = `${number(workProgress, 3)}%`;
    factory$('#fifteenHourWorkProgressBar').style.width = `${
      Math.max(0, Math.min(100, workProgress))
    }%`;
  }
  factory$('#fifteenHourExplanation').textContent = matrixComplete
    ? [
      `本次会话已自然运行 ${durationText(wall)}，其中有效计算 ${durationText(productive)}，非计算时间 ${durationText(excluded)}`,
      '5,280项固定全历史任务已经完成并已生成门禁报告；系统不会重复跑同一批任务来把有效计算凑满15小时',
      '当前不通过的是统计门禁，而不是“还差12小时”；继续研究必须加入新的预注册候选或新增数据，再产生新的可审计证据',
    ].join(' · ')
    : [
      session?.purpose,
      session?.gate_rule,
      campaign?.is_actively_computing
        ? '当前没有等待：独立执行器正在消费本地全历史任务矩阵'
        : campaign?.status_label,
      session?.remaining_compute_seconds !== undefined
        ? `剩余真实计算预算 ${durationText(session.remaining_compute_seconds)}`
        : null,
    ].filter(Boolean).join(' · ');
}

function renderBlockers(blockers) {
  factory$('#blockerCount').textContent = `${blockers.length} 项`;
  factory$('#blockerCount').className = `tag ${blockers.length ? 'red' : 'green'}`;
  factory$('#blockerList').innerHTML = blockers.length
    ? blockers.map((item) => `
      <div class="blocker-item ${esc(item.severity)}">
        <strong>${esc(item.title)}</strong>
        <small>${esc(item.detail)}</small>
      </div>
    `).join('')
    : '<div class="empty-state compact">当前没有研究阻断项。</div>';
}

function formalRowVisible(item) {
  if (factorStageFilter === 'statistical') return item.statistical_gate_passed;
  if (factorStageFilter === 'production') return item.production_gate_passed;
  if (factorStageFilter === 'rejected') return !item.statistical_gate_passed;
  return true;
}

function renderFactors(factors, formal) {
  if (!formal?.available) {
    factory$('#factorPeriod').textContent = '全量V4正式报告尚未生成';
    factory$('#factorFamilySummary').innerHTML = '';
    factory$('#factorTableBody').innerHTML = '<tr><td colspan="6">等待全量V4正式因子研究报告。</td></tr>';
    factory$('#factorWarning').textContent = factors.warning || '旧固定股票池结果不会在此作为正式证据展示。';
    return;
  }
  const splits = formal.splits || {};
  factory$('#factorPeriod').textContent = [
    splits.train ? `训练 ${splits.train.start}→${splits.train.end}` : null,
    splits.validation ? `验证 ${splits.validation.start}→${splits.validation.end}` : null,
    splits.test ? `测试 ${splits.test.start}→${splits.test.end}` : null,
    formal.purge_trading_days ? `隔离 ${formal.purge_trading_days}日` : null,
  ].filter(Boolean).join(' · ') || '尚未生成分区报告';

  factory$('#factorFamilySummary').innerHTML = (formal.families || []).map((item) => `
    <span class="factor-family-chip">
      ${esc(item.label)}
      <b>${item.confirmed_count} / ${item.candidate_count} 周期通过</b>
    </span>
  `).join('');

  const rows = (formal.rows || []).filter(formalRowVisible);
  factory$('#factorTableBody').innerHTML = rows.length
    ? rows.map((item) => `
        <tr>
          <td>
            <b class="factor-name">${esc(item.label)} · ${item.holding_days}日</b>
            <code class="factor-formula" title="${esc(item.formula)}">${esc(item.formula)}</code>
            <small class="factor-description">${esc(item.family_label)} · ${item.role === 'execution_constraint' ? '执行约束' : 'Alpha候选'}</small>
          </td>
          <td class="${signedClass(item.validation_mean_ic)}">${number(item.validation_mean_ic, 6)}</td>
          <td class="${signedClass(item.test_mean_ic)}">
            ${number(item.test_mean_ic, 6)}
            <small class="factor-description">CI [${number(item.test_ci?.[0], 5)}, ${number(item.test_ci?.[1], 5)}] · 正IC日 ${number(item.test_positive_ic_days_pct, 2)}%</small>
          </td>
          <td class="${signedClass(item.test_monotonicity)}">
            ${number(item.test_monotonicity, 5)}
            <small class="factor-description">正收益测试折 ${item.positive_test_folds ?? 0}/3</small>
          </td>
          <td class="${signedClass(item.test_top_minus_middle_after_cost_pct)}">${pct(item.test_top_minus_middle_after_cost_pct, 5)}</td>
          <td>
            <span class="stage-pill ${item.statistical_gate_passed ? 'heldout_confirmed' : 'strategy_failed'}">${item.statistical_gate_passed ? '统计门禁通过' : '统计门禁失败'}</span>
            <small class="factor-description">${esc((item.failed_gates || []).join('、'))}</small>
          </td>
        </tr>
      `).join('')
    : '<tr><td colspan="6">当前筛选条件下没有因子。</td></tr>';
  factory$('#factorWarning').textContent = formal.warning || '正式因子报告尚未生成。';
}

function foldBars(folds) {
  if (!folds || !folds.length) return '<div class="failed-gates">暂无滚动分折结果</div>';
  const maxAbs = Math.max(...folds.map((item) => Math.abs(Number(item.return_pct) || 0)), 0.01);
  return `<div class="fold-chart" aria-label="滚动分折收益">
    ${folds.map((item) => {
      const value = Number(item.return_pct) || 0;
      const height = Math.max(3, Math.min(50, Math.abs(value) / maxAbs * 48));
      return `<span title="第${esc(item.fold)}折 ${pct(value)}">
        <i class="${value < 0 ? 'negative' : ''}" style="height:${height}%"></i>
      </span>`;
    }).join('')}
  </div>`;
}

function renderStrategies(strategies) {
  const candidates = strategies.filter((item) => (
    item.metrics && Number.isFinite(Number(item.metrics.total_return_pct))
  ));
  const bestId = candidates.length
    ? candidates.reduce((best, item) => (
      Number(item.metrics.total_return_pct) > Number(best.metrics.total_return_pct) ? item : best
    )).strategy_id
    : null;
  factory$('#strategyCards').innerHTML = strategies.length
    ? strategies.map((item) => {
      const metrics = item.metrics || {};
      const kindLabels = {
        baseline: '原始基线',
        current: '当前多因子',
        factor: '单因子研究',
      };
      return `
        <article class="strategy-card ${item.strategy_id === bestId ? 'best-research' : ''}">
          <div class="strategy-card-head">
            <span>
              <strong>${esc(item.label)}</strong>
              <small>${esc(kindLabels[item.kind] || item.kind)}${item.strategy_id === bestId ? ' · 当前收益最高研究结果' : ''}</small>
            </span>
            <b class="tag ${item.gate_passed ? 'green' : 'red'}">${item.gate_passed ? '通过' : '未通过'}</b>
          </div>
          <div class="strategy-metrics">
            <div class="strategy-metric"><span>扣费后收益</span><b class="${signedClass(metrics.total_return_pct)}">${pct(metrics.total_return_pct)}</b></div>
            <div class="strategy-metric"><span>最大回撤</span><b>${number(metrics.max_drawdown_pct)}%</b></div>
            <div class="strategy-metric"><span>盈利因子</span><b>${number(metrics.profit_factor)}</b></div>
            <div class="strategy-metric"><span>交易笔数</span><b>${metrics.trades ?? '--'}</b></div>
            <div class="strategy-metric"><span>胜率</span><b>${number(metrics.win_rate_pct, 2)}%</b></div>
            <div class="strategy-metric"><span>正收益折数</span><b>${number(metrics.positive_fold_ratio_pct, 1)}%</b></div>
          </div>
          ${foldBars(item.folds)}
          <p class="failed-gates">${item.failed_gates.length
            ? `失败门禁：${esc(item.failed_gates.join('、'))}`
            : '当前报告未记录失败门禁。'}</p>
        </article>
      `;
    }).join('')
    : '<div class="empty-state compact">尚未生成策略回测报告。</div>';
}

function renderV41(research) {
  const tag = factory$('#v41EvidenceTag');
  const cards = factory$('#v41Cards');
  if (!research?.available) {
    tag.textContent = '尚未运行';
    cards.innerHTML = '<div class="empty-state compact">等待 V4.1 全市场探索报告。</div>';
    factory$('#v41Warning').textContent = '';
    return;
  }
  const hypotheses = research.hypotheses || {};
  const h1 = hypotheses.h1_low_volatility_risk_filter || {};
  const h2 = hypotheses.h2_matured_rolling_orientation || {};
  const h3 = hypotheses.h3_extreme_breadth_exposure || {};
  const h1Five = h1.horizons?.['5'] || {};
  const h1Ten = h1.horizons?.['10'] || {};
  const h3Test = h3.splits?.test || {};
  const h2Passed = h2.passed_factor_horizons || 0;
  const total = research.exploratory_test_count || 0;
  const passed = research.exploratory_gate_pass_count || 0;
  tag.textContent = `探索 ${passed}/${total} · 生产 0`;
  tag.className = 'tag yellow';
  cards.innerHTML = `
    <article class="v41-card ${h1Five.exploratory_gate_passed ? 'candidate' : ''}">
      <span>H1 · 低波动尾部过滤</span>
      <strong>5日 ${h1Five.exploratory_gate_passed ? '探索通过' : '失败'} · 10日 ${h1Ten.exploratory_gate_passed ? '探索通过' : '失败'}</strong>
      <small>
        测试期5日 ES 改善 ${pct(h1Five.splits?.test?.expected_shortfall_improvement_pct, 5)}
        · 平均收益差 ${pct(h1Five.splits?.test?.mean_return_delta_pct, 5)}
      </small>
      <b>只能作为风险过滤器候选</b>
    </article>
    <article class="v41-card">
      <span>H2 · 已成熟历史 IC 决定方向</span>
      <strong>${h2Passed}/4 因子周期探索通过</strong>
      <small>方向仅使用当时已到期的过去252日标签；弱信号空仓。</small>
      <b>${h2Passed ? '只可进入未来模拟候选' : '当前拒绝趋势/反转信号'}</b>
    </article>
    <article class="v41-card ${h3.exploratory_gate_passed ? 'candidate' : ''}">
      <span>H3 · 极端广度降至50%仓位</span>
      <strong>${h3.exploratory_gate_passed ? '探索通过' : '探索失败'}</strong>
      <small>
        测试期回撤 ${number(h3Test.baseline?.max_drawdown_pct, 3)}%
        → ${number(h3Test.controlled?.max_drawdown_pct, 3)}%
        · 平均暴露 ${number(h3Test.average_exposure_pct, 2)}%
      </small>
      <b>未通过前不作为仓位开关</b>
    </article>
  `;
  factory$('#v41Warning').textContent = research.warning || '';
}

function renderV42(research) {
  const tag = factory$('#v42EvidenceTag');
  const body = factory$('#v42TableBody');
  if (!research?.available) {
    tag.textContent = '尚未运行';
    tag.className = 'tag gray';
    body.innerHTML = '<tr><td colspan="6">等待 V4.2 全量研究报告。</td></tr>';
    factory$('#v42Warning').textContent = '当前100%只代表历史数据准备完成，不代表因子或策略通过。';
    return;
  }
  const passed = research.statistical_gate_pass_count || 0;
  const total = research.test_count || 0;
  tag.textContent = `统计 ${passed}/${total} · 生产 0`;
  tag.className = `tag ${passed ? 'yellow' : 'red'}`;
  const gateLabels = {
    validation_mean_ic_positive: '验证IC',
    test_block_bootstrap_ci_low_positive: '测试CI',
    holm_adjusted_p_at_most_0_05: 'Holm',
    positive_test_folds_at_least_2_of_3: '分折',
    decile_monotonicity_at_least_0_6: '单调性',
    top_minus_middle_after_5bp_positive: '扣费收益',
    low_vol_filter_no_direction_reversal: '低波动过滤',
    high_dispersion_ic_difference_ci_low_positive: '离散状态差异',
  };
  body.innerHTML = (research.tests || []).map((item) => {
    const validation = item.splits?.validation || {};
    const test = item.splits?.test || {};
    const filtered = item.low_volatility_filtered_splits?.test || {};
    const failed = Object.entries(item.gate_conditions || {})
      .filter(([, value]) => !value)
      .map(([key]) => gateLabels[key] || key);
    return `
      <tr>
        <td>
          <b class="factor-name">${esc(item.hypothesis_id)} · ${esc(item.label)} · ${item.holding_days}日</b>
          <code class="factor-formula" title="${esc(item.formula)}">${esc(item.formula)}</code>
          <small class="factor-description">${item.state_condition === 'high_dispersion' ? '仅高横截面离散状态' : '全市场状态'} · T日收盘信号 / T+1开盘</small>
        </td>
        <td class="${signedClass(validation.mean_ic)}">${number(validation.mean_ic, 6)}</td>
        <td class="${signedClass(test.mean_ic)}">
          ${number(test.mean_ic, 6)}
          <small class="factor-description">CI [${number(test.block_bootstrap_95_ci?.[0], 6)}, ${number(test.block_bootstrap_95_ci?.[1], 6)}] · 正IC折 ${item.positive_test_folds || 0}/3</small>
        </td>
        <td class="${Number(item.holm_adjusted_p_value) <= 0.05 ? 'up' : 'down'}">${number(item.holm_adjusted_p_value, 6)}</td>
        <td class="${signedClass(filtered.mean_ic)}">
          IC ${number(filtered.mean_ic, 6)}
          <small class="factor-description">扣往返10BP头部-中间 ${pct(filtered.top_minus_middle_after_cost_pct, 5)}</small>
        </td>
        <td>
          <span class="stage-pill ${item.statistical_gate_passed ? 'heldout_confirmed' : 'strategy_failed'}">${item.statistical_gate_passed ? '历史统计候选' : '门禁失败'}</span>
          <small class="factor-description">${failed.length ? `失败：${esc(failed.join('、'))}` : '仍需含成本策略与前向模拟'}</small>
        </td>
      </tr>
    `;
  }).join('') || '<tr><td colspan="6">报告中没有检验记录。</td></tr>';
  factory$('#v42Warning').textContent = research.warning || '';
}

function renderV44(research) {
  const tag = factory$('#v44EvidenceTag');
  const summary = factory$('#v44Summary');
  const body = factory$('#v44TableBody');
  const costs = factory$('#v44CostBreakdown');
  const factor = research?.factor || {};
  const strategy = research?.strategy || {};
  if (!research?.available) {
    tag.textContent = '尚未运行';
    tag.className = 'tag gray';
    summary.innerHTML = '<div class="empty-state compact">等待 V4.4 预注册实验。</div>';
    body.innerHTML = '<tr><td colspan="5">等待 V4.4 因子报告。</td></tr>';
    costs.innerHTML = '';
    factory$('#v44Warning').textContent = '';
    return;
  }

  const tests = factor.tests || [];
  const passed = factor.statistical_gate_pass_count || 0;
  const total = factor.test_count || tests.length;
  tag.textContent = `因子 ${passed}/${total} · 策略${strategy.gate_passed ? '通过' : '失败'}`;
  tag.className = `tag ${strategy.gate_passed ? 'green' : 'red'}`;
  const diagnostics = strategy.cost_diagnostics || {};
  const baseline = diagnostics.v4_3_baseline || {};
  const lowCost = diagnostics.v4_4_cost_first || {};
  summary.innerHTML = `
    <article><span>真实A股范围</span><strong>${Number(factor.universe?.symbols || 0).toLocaleString('zh-CN')} 只</strong><small>${Number(factor.universe?.eligible_factor_rows || 0).toLocaleString('zh-CN')} 条可评估记录</small></article>
    <article><span>三项预注册实验</span><strong>${passed}/${total} 通过</strong><small>同一 Holm 多重检验族，未通过不进入策略</small></article>
    <article><span>换手与费用变化</span><strong>${baseline.trades ?? '--'} → ${lowCost.trades ?? '--'} 笔</strong><small>费用减少 ¥${number(diagnostics.fee_reduction, 2)}</small></article>
    <article><span>最新历史门禁</span><strong class="${strategy.gate_passed ? 'up' : 'down'}">${strategy.gate_passed ? '通过' : '锁定'}</strong><small>${(strategy.failed_gates || []).length} 项条件失败</small></article>
  `;

  const gateLabels = {
    validation_mean_ic_positive: '验证IC',
    test_block_bootstrap_ci_low_positive: '测试CI',
    holm_adjusted_p_at_most_0_05: 'Holm',
    positive_test_folds_at_least_2_of_3: '分折',
    decile_monotonicity_at_least_0_6: '单调性',
    top_minus_middle_after_5bp_positive: '扣成本价差',
    low_vol_filter_no_direction_reversal: '低波动稳健性',
  };
  body.innerHTML = tests.map((item) => {
    const test = item.splits?.test || {};
    const failed = Object.entries(item.gate_conditions || {})
      .filter(([, value]) => !value)
      .map(([key]) => gateLabels[key] || key);
    return `
      <tr>
        <td>
          <b class="factor-name">${esc(item.hypothesis_id)} · ${esc(item.label)}</b>
          <code class="factor-formula">${esc(item.formula)}</code>
          <small class="factor-description">${item.holding_days}日 · ${esc(item.universe_filter || item.state_condition || '全市场')}</small>
        </td>
        <td class="${signedClass(test.mean_ic)}">
          ${number(test.mean_ic, 6)}
          <small class="factor-description">CI [${number(test.block_bootstrap_95_ci?.[0], 6)}, ${number(test.block_bootstrap_95_ci?.[1], 6)}]</small>
        </td>
        <td class="${Number(test.decile_monotonicity_spearman) >= 0.6 ? 'up' : 'down'}">${number(test.decile_monotonicity_spearman, 5)}</td>
        <td class="${signedClass(test.top_minus_middle_after_cost_pct)}">${pct(test.top_minus_middle_after_cost_pct, 5)}</td>
        <td>
          <span class="stage-pill strategy_failed">${item.statistical_gate_passed ? '通过' : '失败'}</span>
          <small class="factor-description">${esc(failed.join('、') || '无')}</small>
        </td>
      </tr>
    `;
  }).join('') || '<tr><td colspan="5">V4.4 报告中没有检验记录。</td></tr>';

  const scenarios = strategy.observed_test || {};
  costs.innerHTML = `
    <article class="v44-cost-card">
      <span>V4.3 基线</span>
      <strong class="${signedClass(baseline.total_return_pct)}">${pct(baseline.total_return_pct)}</strong>
      <small>${baseline.trades ?? '--'}笔 · 费用 ¥${number(baseline.transaction_fees, 2)} · 回撤 ${number(baseline.max_drawdown_pct, 3)}%</small>
    </article>
    <article class="v44-cost-card">
      <span>V4.4 低成本</span>
      <strong class="${signedClass(lowCost.total_return_pct)}">${pct(lowCost.total_return_pct)}</strong>
      <small>${lowCost.trades ?? '--'}笔 · 费用 ¥${number(lowCost.transaction_fees, 2)} · 回撤 ${number(lowCost.max_drawdown_pct, 3)}%</small>
    </article>
    ${Object.entries(scenarios).map(([key, value]) => {
      const labels = {
        fees_only: '实际费用',
        round_trip_10bp: '往返10BP压力',
        round_trip_20bp: '往返20BP压力',
      };
      const metrics = value.metrics || {};
      return `
        <article class="v44-cost-card stress">
          <span>${esc(labels[key] || key)}</span>
          <strong class="${signedClass(metrics.total_return_pct)}">${pct(metrics.total_return_pct)}</strong>
          <small>PF ${number(metrics.profit_factor, 3)} · 费用 ¥${number(metrics.transaction_fees, 2)} · 滑点 ¥${number(metrics.slippage_cost, 2)}</small>
        </article>
      `;
    }).join('')}
  `;
  factory$('#v44Warning').textContent = (
    strategy.warning
    || factor.warning
    || '降低交易成本不等于产生可交易收益；历史门禁仍锁定。'
  );
}

function renderV5(data) {
  const tag = factory$('#v5EvidenceTag');
  const summary = factory$('#v5Summary');
  const body = factory$('#v5TableBody');
  const warning = factory$('#v5Warning');
  if (!tag || !summary || !body || !warning) return;
  if (!data.available) {
    tag.textContent = '等待报告';
    tag.className = 'tag gray';
    summary.innerHTML = '<div class="empty-state compact">V5 尚未完成。</div>';
    body.innerHTML = '<tr><td colspan="6">等待 V5 真实A股研究报告。</td></tr>';
    warning.textContent = 'V4.3已封存，当前只允许运行预注册的首次涨停实验。';
    return;
  }
  const passed = Boolean(data.statistical_gate_passed);
  const universe = data.universe || {};
  const results = data.results || {};
  const test = results.test || {};
  tag.textContent = passed ? '统计门禁通过' : '统计门禁失败';
  tag.className = `tag ${passed ? 'green' : 'red'}`;
  summary.innerHTML = `
    <article><span>真实A股</span><strong>${Number(universe.symbols || 0).toLocaleString('zh-CN')}只</strong><small>完整日线质量门禁后运行</small></article>
    <article><span>检测到首次涨停</span><strong>${Number(universe.detected_first_limit_up_events || 0).toLocaleString('zh-CN')}</strong><small>其中 ${Number(universe.executable_events || 0).toLocaleString('zh-CN')} 个可执行</small></article>
    <article><span>测试期事件</span><strong>${Number(test.executable_events || 0).toLocaleString('zh-CN')}</strong><small>胜率 ${number(test.event_win_rate_pct, 3)}%</small></article>
    <article><span>门禁裁决</span><strong class="${passed ? 'up' : 'down'}">${passed ? '允许冻结策略' : '禁止策略回测'}</strong><small>失败 ${(data.failed_gates || []).length} 项</small></article>
  `;
  const splitLabels = {train: '训练描述', validation: '验证', test: '测试'};
  body.innerHTML = ['train', 'validation', 'test'].map((name) => {
    const row = results[name] || {};
    const ci = row.block_bootstrap_95_ci_excess_pct || [];
    const rowPassed = Number(row.mean_event_net_return_pct) > 0;
    return `
      <tr>
        <td><b>${splitLabels[name]}</b><small class="factor-description">${esc(row.start || '--')} → ${esc(row.end || '--')}</small></td>
        <td>${Number(row.executable_events || 0).toLocaleString('zh-CN')}</td>
        <td class="${signedClass(row.mean_event_net_return_pct)}">${pct(row.mean_event_net_return_pct)}</td>
        <td class="${signedClass(row.mean_daily_excess_return_pct)}">${pct(row.mean_daily_excess_return_pct)}</td>
        <td>CI [${number(ci[0], 5)}%, ${number(ci[1], 5)}%]<small class="factor-description">P ${number(row.one_sided_block_bootstrap_p_value, 6)}</small></td>
        <td><span class="stage-pill ${rowPassed ? 'heldout_confirmed' : 'strategy_failed'}">${rowPassed ? '净收益为正' : '净收益为负'}</span></td>
      </tr>
    `;
  }).join('');
  warning.textContent = [
    data.next_action,
    (data.failed_gates || []).length
      ? `失败门禁：${data.failed_gates.join('、')}` : null,
    data.warning,
  ].filter(Boolean).join(' · ');
}

function renderV6(data) {
  const tag = factory$('#v6EvidenceTag');
  const summary = factory$('#v6Summary');
  const body = factory$('#v6TableBody');
  const warning = factory$('#v6Warning');
  if (!tag || !summary || !body || !warning) return;
  if (!data.available) {
    tag.textContent = '等待报告';
    tag.className = 'tag gray';
    summary.innerHTML = '<div class="empty-state compact">V6 尚未完成。</div>';
    body.innerHTML = '<tr><td colspan="5">等待 V6 真实A股研究报告。</td></tr>';
    warning.textContent = 'V6未完成前不冻结策略。';
    return;
  }
  const passed = Boolean(data.statistical_gate_passed);
  const universe = data.universe || {};
  const result = data.result || {};
  const splits = result.splits || {};
  const failed = result.failed_gates || [];
  const test = splits.test || {};
  tag.textContent = passed ? '统计门禁通过' : '历史失败 · 已封存';
  tag.className = `tag ${passed ? 'green' : 'red'}`;
  summary.innerHTML = `
    <article><span>真实A股原始覆盖</span><strong>${Number(universe.symbols || 0).toLocaleString('zh-CN')}只</strong><small>${Number(universe.eligible_symbols || 0).toLocaleString('zh-CN')}只形成合格样本</small></article>
    <article><span>因子记录</span><strong>${Number(universe.factor_rows || 0).toLocaleString('zh-CN')}</strong><small>排除 ${Number(universe.excluded_limit_windows || 0).toLocaleString('zh-CN')} 个含涨停窗口</small></article>
    <article><span>测试期平均IC</span><strong class="${signedClass(test.mean_ic)}">${number(test.mean_ic, 5)}</strong><small>正IC日 ${number(test.positive_ic_days_pct, 3)}%</small></article>
    <article><span>门禁裁决</span><strong class="${passed ? 'up' : 'down'}">${passed ? '允许冻结策略' : '禁止策略回测'}</strong><small>失败 ${failed.length} 项</small></article>
  `;
  const splitLabels = {train: '训练描述', validation: '验证', test: '测试'};
  body.innerHTML = ['train', 'validation', 'test'].map((name) => {
    const row = splits[name] || {};
    const ci = row.block_bootstrap_95_ci || [];
    const rowPassed = Number(row.mean_ic) > 0
      && Number(row.top_minus_middle_after_cost_pct) > 0;
    return `
      <tr>
        <td><b>${splitLabels[name]}</b><small class="factor-description">${esc((data.splits?.[name] || {}).start || '--')} → ${esc((data.splits?.[name] || {}).end || '--')}</small></td>
        <td class="${signedClass(row.mean_ic)}">${number(row.mean_ic, 5)}<small class="factor-description">CI [${number(ci[0], 5)}, ${number(ci[1], 5)}]</small></td>
        <td>${number(row.decile_monotonicity_spearman, 5)}</td>
        <td class="${signedClass(row.top_minus_middle_after_cost_pct)}">${pct(row.top_minus_middle_after_cost_pct)}</td>
        <td><span class="stage-pill ${rowPassed ? 'heldout_confirmed' : 'strategy_failed'}">${rowPassed ? '方向与经济性为正' : '未同时成立'}</span></td>
      </tr>
    `;
  }).join('');
  warning.textContent = [
    data.next_action,
    failed.length ? `失败门禁：${failed.join('、')}` : null,
    data.warning,
  ].filter(Boolean).join(' · ');
}

function renderV9(data) {
  const tag = factory$('#v9EvidenceTag');
  const summary = factory$('#v9Summary');
  const body = factory$('#v9TableBody');
  const warning = factory$('#v9Warning');
  if (!tag || !summary || !body || !warning) return;
  if (!data.available) {
    tag.textContent = '等待报告';
    tag.className = 'tag gray';
    summary.innerHTML = '<div class="empty-state compact">V9 尚未完成。</div>';
    body.innerHTML = '<tr><td colspan="5">等待 V9 真实交易所研究报告。</td></tr>';
    warning.textContent = '统计门禁通过前不运行策略回测。';
    return;
  }
  const passed = Boolean(data.statistical_gate_passed);
  const database = data.database || {};
  const result = data.result || {};
  const splits = result.splits || {};
  const test = splits.test || {};
  const failed = result.failed_gates || [];
  tag.textContent = passed ? '统计门禁通过' : '历史失败 · 已封存';
  tag.className = `tag ${passed ? 'green' : 'red'}`;
  summary.innerHTML = `
    <article><span>交易所融资原始行</span><strong>${Number(database.margin_source_rows || 0).toLocaleString('zh-CN')}</strong><small>2018年至今逐日明细</small></article>
    <article><span>可检验标的 / 记录</span><strong>${Number(database.symbols || 0).toLocaleString('zh-CN')}只</strong><small>${Number(database.rows || 0).toLocaleString('zh-CN')}条对齐记录</small></article>
    <article><span>测试期平均IC</span><strong class="${signedClass(test.mean_ic)}">${number(test.mean_ic, 6)}</strong><small>区间下界 ${number(test.block_bootstrap_95_ci?.[0], 6)}</small></article>
    <article><span>经济门禁</span><strong class="${passed ? 'up' : 'down'}">${passed ? '允许策略回测' : '禁止策略回测'}</strong><small>失败 ${failed.length} 项</small></article>
  `;
  const splitLabels = {train: '训练描述', validation: '验证', test: '测试'};
  body.innerHTML = ['train', 'validation', 'test'].map((name) => {
    const row = splits[name] || {};
    const ci = row.block_bootstrap_95_ci || [];
    const economic = Number(row.top_minus_middle_after_cost_pct) > 0;
    return `
      <tr>
        <td><b>${splitLabels[name]}</b></td>
        <td class="${signedClass(row.mean_ic)}">${number(row.mean_ic, 6)}<small class="factor-description">CI [${number(ci[0], 6)}, ${number(ci[1], 6)}]</small></td>
        <td>${number(row.decile_monotonicity_spearman, 5)}</td>
        <td class="${signedClass(row.top_minus_middle_after_cost_pct)}">${pct(row.top_minus_middle_after_cost_pct, 5)}</td>
        <td><span class="stage-pill ${economic ? 'heldout_confirmed' : 'strategy_failed'}">${economic ? '扣成本为正' : '扣成本为负'}</span></td>
      </tr>
    `;
  }).join('');
  warning.textContent = [
    data.next_action,
    failed.length ? `失败门禁：${failed.join('、')}` : null,
    `固定执行：${data.fixed_execution?.signal || '--'}，${data.fixed_execution?.entry || '--'}，${data.fixed_execution?.exit || '--'}`,
  ].filter(Boolean).join(' · ');
}

function renderV14(data) {
  const tag = factory$('#v14EvidenceTag');
  const summary = factory$('#v14Summary');
  const body = factory$('#v14TableBody');
  const warning = factory$('#v14Warning');
  if (!tag || !summary || !body || !warning) return;
  const progress = data.progress || {};
  if (!data.available) {
    const running = progress.status === 'running';
    const progressPct = Number(progress.progress_pct || 0);
    tag.textContent = running ? `真实计算中 ${number(progressPct, 1)}%` : '等待启动';
    tag.className = `tag ${running ? 'amber' : 'gray'}`;
    summary.innerHTML = `
      <article><span>当前阶段</span><strong>${esc(progress.phase || '等待研究工人')}</strong><small>${esc(progress.message || '尚未生成报告')}</small></article>
      <article><span>真实进度</span><strong>${number(progressPct, 1)}%</strong><small>${Number(progress.completed_units || 0).toLocaleString('zh-CN')} / ${Number(progress.total_units || 0).toLocaleString('zh-CN')} 项</small></article>
      <article><span>实际计算</span><strong>${durationText(progress.actual_compute_seconds || 0)}</strong><small>预计剩余 ${durationText(progress.eta_seconds || 0)}</small></article>
      <article><span>交易权限</span><strong class="down">保持锁定</strong><small>统计和成本门禁均通过后才解锁</small></article>
    `;
    body.innerHTML = '<tr><td colspan="5">正在读取真实交易所融资余额并计算样本外统计。</td></tr>';
    warning.textContent = '未验证候选不会进入买入推荐。';
    return;
  }
  const factorPassed = Boolean(data.statistical_gate_passed);
  const strategyPassed = Boolean(data.strategy_gate_passed);
  const database = data.database || {};
  const result = data.result || {};
  const splits = result.splits || {};
  const test = splits.test || {};
  const failed = result.failed_gates || [];
  tag.textContent = strategyPassed
    ? '严格成本门禁通过'
    : factorPassed
      ? (data.strategy_available ? '成本门禁失败' : '统计通过 · 成本回测中')
      : '统计门禁失败 · 已封存';
  tag.className = `tag ${strategyPassed ? 'green' : factorPassed && !data.strategy_available ? 'amber' : 'red'}`;
  summary.innerHTML = `
    <article><span>真实融资记录</span><strong>${Number(database.source_rows || 0).toLocaleString('zh-CN')}</strong><small>${Number(database.symbols || 0).toLocaleString('zh-CN')}只股票 · ${Number(database.rows || 0).toLocaleString('zh-CN')}条因子标签</small></article>
    <article><span>测试期平均IC</span><strong class="${signedClass(test.mean_ic)}">${number(test.mean_ic, 6)}</strong><small>95%区间 [${number(test.block_bootstrap_95_ci?.[0], 6)}, ${number(test.block_bootstrap_95_ci?.[1], 6)}]</small></article>
    <article><span>统计门禁</span><strong class="${factorPassed ? 'up' : 'down'}">${factorPassed ? '通过' : '失败'}</strong><small>失败 ${failed.length} 项</small></article>
    <article><span>真钱策略门禁</span><strong class="${strategyPassed ? 'up' : 'down'}">${strategyPassed ? '通过' : '锁定'}</strong><small>${data.strategy_available ? `成本回测失败 ${data.strategy_failed_gates?.length || 0} 项` : '等待统计结果'}</small></article>
  `;
  const splitLabels = {train: '训练描述', validation: '验证', test: '测试'};
  body.innerHTML = ['train', 'validation', 'test'].map((name) => {
    const row = splits[name] || {};
    const ci = row.block_bootstrap_95_ci || [];
    const economic = Number(row.top_minus_middle_after_cost_pct) > 0;
    return `
      <tr>
        <td><b>${splitLabels[name]}</b></td>
        <td class="${signedClass(row.mean_ic)}">${number(row.mean_ic, 6)}<small class="factor-description">CI [${number(ci[0], 6)}, ${number(ci[1], 6)}]</small></td>
        <td>${number(row.decile_monotonicity_spearman, 5)}</td>
        <td class="${signedClass(row.top_minus_middle_after_cost_pct)}">${pct(row.top_minus_middle_after_cost_pct, 5)}</td>
        <td><span class="stage-pill ${economic ? 'heldout_confirmed' : 'strategy_failed'}">${economic ? '扣成本为正' : '扣成本为负'}</span></td>
      </tr>
    `;
  }).join('');
  warning.textContent = [
    data.next_action,
    failed.length ? `统计失败门禁：${failed.join('、')}` : null,
    data.strategy_failed_gates?.length ? `策略失败门禁：${data.strategy_failed_gates.join('、')}` : null,
    data.warning,
  ].filter(Boolean).join(' · ');
}

function renderV15(data) {
  const tag = factory$('#v15EvidenceTag');
  const summary = factory$('#v15Summary');
  const body = factory$('#v15TableBody');
  const warning = factory$('#v15Warning');
  if (!tag || !summary || !body || !warning) return;
  const progress = data.progress || {};
  if (!data.available) {
    const running = progress.status === 'running';
    tag.textContent = running ? `真实研究中 ${number(progress.progress_pct, 1)}%` : '等待启动';
    tag.className = `tag ${running ? 'amber' : 'gray'}`;
    summary.innerHTML = `<div class="empty-state compact">${esc(progress.message || '尚未生成 V15 数据源报告。')}</div>`;
    body.innerHTML = '<tr><td colspan="5">官方事件数据门禁通过前禁止计算收益。</td></tr>';
    warning.textContent = 'V15 尚未完成真实事件与收益检验。';
    return;
  }
  const sourcePassed = Boolean(data.source_data_gate_passed);
  const eventPassed = Boolean(data.event_data_gate_passed);
  const factorPassed = Boolean(data.statistical_gate_passed);
  const realMoneyPassed = Boolean(data.real_money_gate_passed);
  const coverage = data.coverage || {};
  const sample = data.sample_audit || {};
  const eventReport = data.event_report || {};
  const eventCounts = eventReport.events || {};
  const factor = data.factor_report || {};
  const results = factor.results || {};
  const validation = results.validation || {};
  const test = results.test || {};
  tag.textContent = realMoneyPassed
    ? '真钱策略门禁通过'
    : factor.available === false
      ? '等待收益检验'
      : factorPassed
        ? '统计通过 · 等待成本回测'
        : '统计门禁失败 · 已封存';
  if (!factor.generated_at) {
    tag.textContent = eventPassed ? '事件数据通过 · 收益待检验' : (sourcePassed ? '事件数据门禁失败' : '官方来源失败');
  }
  tag.className = `tag ${realMoneyPassed ? 'green' : factorPassed ? 'amber' : 'red'}`;
  summary.innerHTML = `
    <article><span>官方来源审计</span><strong class="${sourcePassed ? 'up' : 'down'}">${sourcePassed ? '通过' : '失败'}</strong><small>${esc(coverage.earliest_announcement || '--')} 至 ${esc(coverage.latest_announcement || '--')} · 正文解析 ${number(sample.single_security_parse_rate_pct, 1)}%</small></article>
    <article><span>点时事件库</span><strong class="${eventPassed ? 'up' : 'down'}">${Number(eventCounts.additions || 0).toLocaleString('zh-CN')}个调入</strong><small>测试期 ${Number(eventCounts.test_additions || 0)} 个 · ${eventPassed ? '数据门禁通过' : '数据不足'}</small></article>
    <article><span>测试期真实结果</span><strong class="${signedClass(test.mean_event_net_return_pct)}">${pct(test.mean_event_net_return_pct, 3)}</strong><small>${Number(test.executable_events || 0)}笔 · 胜率 ${number(test.event_win_rate_pct, 2)}%</small></article>
    <article><span>真钱策略门禁</span><strong class="${realMoneyPassed ? 'up' : 'down'}">${realMoneyPassed ? '通过' : '保持锁定'}</strong><small>${factorPassed ? '等待完整成本回测' : `统计失败 ${Number(data.failed_gates?.length || 0)} 项`}</small></article>
  `;
  const ci = test.block_bootstrap_95_ci_pct || [];
  body.innerHTML = `
    <tr>
      <td><b>官方来源</b><small class="factor-description">上交所 + 深交所</small></td>
      <td>${Number(coverage.total_relevant_announcements || 0)}份公告</td>
      <td>--</td>
      <td>正文解析 ${number(sample.single_security_parse_rate_pct, 1)}%</td>
      <td><span class="stage-pill ${sourcePassed ? 'heldout_confirmed' : 'strategy_failed'}">${sourcePassed ? '通过' : '失败'}</span></td>
    </tr>
    <tr>
      <td><b>点时事件库</b><small class="factor-description">公告日、生效日、证据指纹</small></td>
      <td>${Number(eventCounts.additions || 0)}个调入</td>
      <td>--</td>
      <td>测试期原始 ${Number(eventCounts.test_additions || 0)} 个</td>
      <td><span class="stage-pill ${eventPassed ? 'heldout_confirmed' : 'strategy_failed'}">${eventPassed ? '允许收益检验' : '禁止收益检验'}</span></td>
    </tr>
    <tr>
      <td><b>验证期</b><small class="factor-description">2022-04-18 至 2024-02-29</small></td>
      <td>${Number(validation.executable_events || 0)}笔</td>
      <td class="${signedClass(validation.mean_event_net_return_pct)}">${pct(validation.mean_event_net_return_pct, 3)}</td>
      <td>胜率 ${number(validation.event_win_rate_pct, 2)}%</td>
      <td><span class="stage-pill ${Number(validation.mean_event_net_return_pct) > 0 ? 'heldout_confirmed' : 'strategy_failed'}">${Number(validation.mean_event_net_return_pct) > 0 ? '为正' : '为负'}</span></td>
    </tr>
    <tr>
      <td><b>独立测试期</b><small class="factor-description">2024-03-18 至 2025-12-31</small></td>
      <td>${Number(test.executable_events || 0)}笔</td>
      <td class="${signedClass(test.mean_event_net_return_pct)}">${pct(test.mean_event_net_return_pct, 3)}</td>
      <td>胜率 ${number(test.event_win_rate_pct, 2)}%<small class="factor-description">95%区间 [${number(ci[0], 3)}, ${number(ci[1], 3)}]</small></td>
      <td><span class="stage-pill ${factorPassed ? 'heldout_confirmed' : 'strategy_failed'}">${factorPassed ? '统计通过' : '统计失败'}</span></td>
    </tr>
  `;
  const gateLabels = {
    validation_mean_event_net_return_positive: '验证期扣成本平均收益为正',
    test_mean_event_net_return_positive: '测试期扣成本平均收益为正',
    test_block_bootstrap_ci_low_positive: '测试期Bootstrap区间下界为正',
    test_one_sided_block_bootstrap_p_at_most_0_05: '单侧p值不高于0.05',
    positive_test_folds_at_least_2_of_3: '测试期至少2/3时间折为正',
    test_executable_events_at_least_100: '测试期至少100笔可执行事件',
    test_event_win_rate_above_50: '测试期事件胜率高于50%',
  };
  warning.textContent = [
    data.next_action,
    data.failed_gates?.length ? `失败门禁：${data.failed_gates.map((item) => gateLabels[item] || item).join('、')}` : null,
    data.warning,
  ].filter(Boolean).join(' · ');
}

function renderV16(data) {
  const tag = factory$('#v16EvidenceTag');
  const summary = factory$('#v16Summary');
  const body = factory$('#v16TableBody');
  const warning = factory$('#v16Warning');
  if (!tag || !summary || !body || !warning) return;
  const progress = data.progress || {};
  if (!data.available) {
    const running = progress.status === 'running';
    tag.textContent = running ? `真实研究中 ${number(progress.progress_pct, 1)}%` : '等待启动';
    tag.className = `tag ${running ? 'amber' : 'gray'}`;
    summary.innerHTML = `<div class="empty-state compact">${esc(progress.message || '尚未生成 V16 事件报告。')}</div>`;
    body.innerHTML = '<tr><td colspan="5">事件数据门禁通过前禁止读取收益。</td></tr>';
    warning.textContent = 'V16 尚未完成真实公告与收益检验。';
    return;
  }
  const eventPassed = Boolean(data.event_data_gate_passed);
  const factorPassed = Boolean(data.statistical_gate_passed);
  const realMoneyPassed = Boolean(data.real_money_gate_passed);
  const eventReport = data.event_report || {};
  const counts = eventReport.counts || {};
  const factor = data.factor_report || {};
  const execution = factor.execution || {};
  const results = factor.results || {};
  const validation = results.validation || {};
  const test = results.test || {};
  const ci = test.block_bootstrap_95_ci_pct || [];
  const eventProgress = data.event_progress || {};
  const sourceLimited = eventProgress.status === 'failed';
  tag.textContent = realMoneyPassed
    ? '真钱策略门禁通过'
    : factor.generated_at
      ? (factorPassed ? '统计通过 · 等待成本回测' : '统计门禁失败 · 已封存')
      : (eventPassed ? '事件数据通过 · 收益待检验' : '事件数据门禁失败');
  tag.className = `tag ${realMoneyPassed ? 'green' : factorPassed ? 'amber' : 'red'}`;
  summary.innerHTML = `
    <article><span>真实公告事件库</span><strong class="${eventPassed ? 'up' : 'down'}">${Number(counts.accepted_events || 0).toLocaleString('zh-CN')}个</strong><small>原始 ${Number(counts.raw_rows || 0).toLocaleString('zh-CN')} 条 · 测试 ${Number(counts.test_events || 0)} 个</small></article>
    <article><span>冻结股票池执行</span><strong>${Number(execution.executable_events || 0).toLocaleString('zh-CN')}笔</strong><small>池外 ${Number(execution.rejection_reasons?.outside_frozen_4802_universe || 0)} 条 · 延迟卖出 ${Number(execution.delayed_exits || 0)}</small></article>
    <article><span>测试期真实结果</span><strong class="${signedClass(test.mean_event_net_return_pct)}">${pct(test.mean_event_net_return_pct, 3)}</strong><small>胜率 ${number(test.event_win_rate_pct, 3)}% · p=${number(test.one_sided_block_bootstrap_p_value, 5)}</small></article>
    <article><span>真钱策略门禁</span><strong class="${realMoneyPassed ? 'up' : 'down'}">${realMoneyPassed ? '通过' : '保持锁定'}</strong><small>${factorPassed ? '等待完整成本回测' : `统计失败 ${Number(data.failed_gates?.length || 0)} 项`}</small></article>
  `;
  const rows = [
    ['公告数据门禁', counts.accepted_events, counts.test_independent_announcement_days, null, null, null, eventPassed],
    ['验证期', validation.executable_events, validation.event_days, validation.mean_event_net_return_pct, validation.event_win_rate_pct, validation.block_bootstrap_95_ci_pct, Number(validation.mean_event_net_return_pct) > 0],
    ['独立测试期', test.executable_events, test.event_days, test.mean_event_net_return_pct, test.event_win_rate_pct, ci, factorPassed],
  ];
  body.innerHTML = rows.map(([label, events, days, mean, win, interval, passed]) => `
    <tr>
      <td><b>${label}</b></td>
      <td>${Number(events || 0).toLocaleString('zh-CN')}笔<small class="factor-description">${Number(days || 0)}个独立日期</small></td>
      <td class="${mean == null ? '' : signedClass(mean)}">${mean == null ? '--' : pct(mean, 3)}</td>
      <td>${win == null ? '--' : `胜率 ${number(win, 3)}%`}<small class="factor-description">${interval ? `95%区间 [${number(interval[0], 3)}, ${number(interval[1], 3)}]` : ''}</small></td>
      <td><span class="stage-pill ${passed ? 'heldout_confirmed' : 'strategy_failed'}">${passed ? '通过' : '失败'}</span></td>
    </tr>
  `).join('');
  const gateLabels = {
    test_win_rate_at_least_60_pct: '测试期胜率至少60%',
    test_one_sided_block_bootstrap_p_at_most_0_05: '单侧p值不高于0.05',
    validation_mean_net_return_positive: '验证期扣成本平均收益为正',
    test_mean_net_return_positive: '测试期扣成本平均收益为正',
    test_block_bootstrap_ci_low_positive: '测试期Bootstrap区间下界为正',
    all_three_test_time_folds_positive: '三个测试时间折全部为正',
  };
  warning.textContent = [
    sourceLimited ? `公告源最新补抓失败：${eventProgress.message || '外部接口限流'}；已完成缓存仍可复核` : null,
    data.failed_gates?.length ? `失败门禁：${data.failed_gates.map((item) => gateLabels[item] || item).join('、')}` : null,
    data.next_action,
    data.warning,
  ].filter(Boolean).join(' · ');
}

function renderV17(data) {
  const tag = factory$('#v17EvidenceTag');
  const summary = factory$('#v17Summary');
  const body = factory$('#v17TableBody');
  const warning = factory$('#v17Warning');
  if (!tag || !summary || !body || !warning) return;
  const progress = data.progress || {};
  if (!data.available) {
    const running = progress.status === 'running';
    tag.textContent = running ? `真实研究中 ${number(progress.progress_pct, 1)}%` : '等待启动';
    tag.className = `tag ${running ? 'amber' : 'gray'}`;
    summary.innerHTML = `<div class="empty-state compact">${esc(progress.message || '尚未生成V17真实检验结果。')}</div>`;
    body.innerHTML = '<tr><td colspan="5">正在等待V17全市场真实日线扫描。</td></tr>';
    warning.textContent = '这是新机制的预注册检验，不会修改V16失败结果。';
    return;
  }
  const factorPassed = Boolean(data.statistical_gate_passed);
  const realMoneyPassed = Boolean(data.real_money_gate_passed);
  const counts = data.event_counts || {};
  const results = data.results || {};
  const train = results.train || {};
  const validation = results.validation || {};
  const test = results.test || {};
  tag.textContent = realMoneyPassed
    ? '真钱策略门禁通过'
    : factorPassed ? '统计通过 · 等待成本策略' : '统计门禁失败 · 已封存';
  tag.className = `tag ${realMoneyPassed ? 'green' : factorPassed ? 'amber' : 'red'}`;
  summary.innerHTML = `
    <article><span>机制事件扫描</span><strong>${Number(counts.raw_events || 0).toLocaleString('zh-CN')}个</strong><small>正常跌停 ${Number(counts.limit_down_days || 0).toLocaleString('zh-CN')}日 · 可执行 ${Number(counts.executable_events || 0).toLocaleString('zh-CN')}笔</small></article>
    <article><span>测试期扣成本收益</span><strong class="${signedClass(test.mean_event_net_return_pct)}">${pct(test.mean_event_net_return_pct, 3)}</strong><small>胜率 ${number(test.event_win_rate_pct, 3)}% · p=${number(test.one_sided_block_bootstrap_p_value, 5)}</small></article>
    <article><span>真实计算时长</span><strong>${durationText(data.actual_compute_seconds || 0)}</strong><small>4,802只真实A股 · T+1入场 · T+3退出</small></article>
    <article><span>下一步</span><strong class="${factorPassed ? 'up' : 'down'}">${factorPassed ? '冻结低换手策略' : '切换新机制'}</strong><small>不改0.5阈值、20日窗口或3日持有期</small></article>
  `;
  const rows = [
    ['训练期频率', train],
    ['验证期', validation],
    ['独立测试期', test],
  ];
  body.innerHTML = rows.map(([label, item]) => {
    const interval = item.block_bootstrap_95_ci_pct || [];
    const passed = label === '训练期频率'
      ? Number(item.executable_events || 0) >= 200
      : label === '验证期'
        ? Number(item.mean_event_net_return_pct || 0) > 0
        : factorPassed;
    return `<tr>
      <td><b>${esc(label)}</b></td>
      <td>${Number(item.executable_events || 0).toLocaleString('zh-CN')}笔<small class="factor-description">${Number(item.event_days || 0)}个独立日期</small></td>
      <td class="${signedClass(item.mean_event_net_return_pct)}">${pct(item.mean_event_net_return_pct, 3)}</td>
      <td>胜率 ${number(item.event_win_rate_pct, 3)}%<small class="factor-description">95%区间 [${number(interval[0], 3)}, ${number(interval[1], 3)}]</small></td>
      <td><span class="stage-pill ${passed ? 'heldout_confirmed' : 'strategy_failed'}">${passed ? '通过' : '失败'}</span></td>
    </tr>`;
  }).join('');
  const gateLabels = {
    train_executable_events_at_least_200: '训练期至少200笔事件',
    validation_mean_net_return_positive: '验证期扣成本平均收益为正',
    test_executable_events_at_least_100: '测试期至少100笔事件',
    test_independent_event_days_at_least_30: '测试期至少30个独立事件日',
    test_mean_net_return_positive: '测试期扣成本平均收益为正',
    test_win_rate_at_least_60_pct: '测试期胜率至少60%',
    test_block_bootstrap_ci_low_positive: '测试期Bootstrap区间下界为正',
    test_one_sided_block_bootstrap_p_at_most_0_05: '单侧p值不高于0.05',
    all_three_test_time_folds_positive: '三个测试时间折全部为正',
    test_daily_max_drawdown_at_most_15_pct: '事件日最大回撤不超过15%',
  };
  warning.textContent = [
    data.failed_gates?.length
      ? `失败门禁：${data.failed_gates.map((item) => gateLabels[item] || item).join('、')}`
      : null,
    data.next_action,
    data.warning,
  ].filter(Boolean).join(' · ');
}

function renderV10(data) {
  const tag = factory$('#v10EvidenceTag');
  const summary = factory$('#v10Summary');
  const body = factory$('#v10TableBody');
  const warning = factory$('#v10Warning');
  if (!tag || !summary || !body || !warning) return;
  if (!data.available) {
    tag.textContent = '等待报告';
    tag.className = 'tag gray';
    summary.innerHTML = '<div class="empty-state compact">V10 尚未完成。</div>';
    body.innerHTML = '<tr><td colspan="5">等待 V10 点时同伴研究报告。</td></tr>';
    warning.textContent = '统计门禁通过前不运行策略回测。';
    return;
  }
  const passed = Boolean(data.statistical_gate_passed);
  const database = data.database || {};
  const result = data.result || {};
  const splits = result.splits || {};
  const test = splits.test || {};
  const failed = result.failed_gates || [];
  tag.textContent = passed ? '统计门禁通过' : '历史失败 · 已封存';
  tag.className = `tag ${passed ? 'green' : 'red'}`;
  summary.innerHTML = `
    <article><span>点时同伴数据库</span><strong>${Number(database.symbols || 0).toLocaleString('zh-CN')}只</strong><small>${Number(database.rows || 0).toLocaleString('zh-CN')}条记录 · ${Number(database.months || 0)}个月</small></article>
    <article><span>测试期平均IC</span><strong class="${signedClass(test.mean_ic)}">${number(test.mean_ic, 6)}</strong><small>95%区间 [${number(test.block_bootstrap_95_ci?.[0], 6)}, ${number(test.block_bootstrap_95_ci?.[1], 6)}]</small></article>
    <article><span>Bootstrap显著性</span><strong class="${Number(result.one_sided_block_bootstrap_p_value) <= 0.05 ? 'up' : 'down'}">p=${number(result.one_sided_block_bootstrap_p_value, 4)}</strong><small>要求 ≤ 0.05</small></article>
    <article><span>经济门禁</span><strong class="${passed ? 'up' : 'down'}">${passed ? '允许策略回测' : '禁止策略回测'}</strong><small>失败 ${failed.length} 项</small></article>
  `;
  const splitLabels = {train: '训练描述', validation: '验证', test: '测试'};
  body.innerHTML = ['train', 'validation', 'test'].map((name) => {
    const row = splits[name] || {};
    const ci = row.block_bootstrap_95_ci || [];
    const economic = Number(row.top_minus_middle_after_cost_pct) > 0;
    return `
      <tr>
        <td><b>${splitLabels[name]}</b><small class="factor-description">${Number(row.observations || 0).toLocaleString('zh-CN')}个交易日</small></td>
        <td class="${signedClass(row.mean_ic)}">${number(row.mean_ic, 6)}<small class="factor-description">CI [${number(ci[0], 6)}, ${number(ci[1], 6)}]</small></td>
        <td>${number(row.decile_monotonicity_spearman, 5)}</td>
        <td class="${signedClass(row.top_minus_middle_after_cost_pct)}">${pct(row.top_minus_middle_after_cost_pct, 5)}</td>
        <td><span class="stage-pill ${economic ? 'heldout_confirmed' : 'strategy_failed'}">${economic ? '扣成本为正' : '扣成本为负'}</span></td>
      </tr>
    `;
  }).join('');
  warning.textContent = [
    data.next_action,
    failed.length ? `失败门禁：${failed.join('、')}` : null,
    `固定执行：${data.fixed_parameters?.signal || '--'}，${data.fixed_parameters?.entry || '--'}，${data.fixed_parameters?.exit || '--'}`,
  ].filter(Boolean).join(' · ');
}

function renderV11(data) {
  const tag = factory$('#v11EvidenceTag');
  const summary = factory$('#v11Summary');
  const coverage = factory$('#v11Coverage');
  const warning = factory$('#v11Warning');
  if (!tag || !summary || !coverage || !warning) return;
  if (!data.available) {
    tag.textContent = '等待报告';
    tag.className = 'tag gray';
    summary.innerHTML = '<div class="empty-state compact">V11 尚未完成。</div>';
    coverage.innerHTML = '';
    warning.textContent = '公告点时数据与统计门禁通过前不运行策略回测。';
    return;
  }
  const passed = Boolean(data.statistical_gate_passed);
  const gate = data.data_gate || {};
  const universe = data.universe || {};
  const result = data.result || {};
  const yearly = gate.years || {};
  const missingValues = Object.values(yearly).map((item) => Number(item.missing_pct || 0));
  const worstMissing = missingValues.length ? Math.max(...missingValues) : 0;
  tag.textContent = passed ? '统计门禁通过' : '历史失败 · 已封存';
  tag.className = `tag ${passed ? 'green' : 'red'}`;
  summary.innerHTML = `
    <article><span>合格 / 点时匹配</span><strong>${Number(gate.eligible_events || 0).toLocaleString('zh-CN')} / ${Number(gate.matched_events || 0).toLocaleString('zh-CN')}</strong><small>最差年度缺失率 ${number(worstMissing, 3)}%</small></article>
    <article><span>真实可执行事件</span><strong>${Number(universe.executable_events || 0).toLocaleString('zh-CN')}</strong><small>${Number(universe.symbols || 0).toLocaleString('zh-CN')}只股票 · 排除ST/低价/停牌/一字板</small></article>
    <article><span>可计算日横截面</span><strong class="${Number(result.evaluable_ic_days) >= Number(result.minimum_required_for_split) ? 'up' : 'down'}">${Number(result.evaluable_ic_days || 0)}</strong><small>冻结切分至少 ${Number(result.minimum_required_for_split || 0)} 日</small></article>
    <article><span>门禁裁决</span><strong class="${passed ? 'up' : 'down'}">${passed ? '允许策略回测' : '样本结构不足'}</strong><small>不降低门槛，不改事件定义</small></article>
  `;
  coverage.innerHTML = Object.entries(yearly).map(([year, item]) => `
    <article>
      <span>${esc(year)}年公告覆盖</span>
      <strong class="${item.passed ? 'up' : 'down'}">${number(100 - Number(item.missing_pct || 0), 3)}%</strong>
      <small>${Number(item.matched_events || 0).toLocaleString('zh-CN')} / ${Number(item.eligible_events || 0).toLocaleString('zh-CN')} 个合格事件</small>
    </article>
  `).join('');
  warning.textContent = [
    data.next_action,
    `失败门禁：${(result.failed_gates || []).join('、') || '--'}`,
    `固定执行：${data.fixed_execution?.signal || '--'}，${data.fixed_execution?.entry || '--'}，${data.fixed_execution?.exit || '--'}`,
  ].join(' · ');
}

function renderRegimes(matrix) {
  const items = matrix.items || [];
  const formalReturns = matrix.metric_type === 'forward_return';
  factory$('#regimeMatrix').innerHTML = items.length
    ? items.map((item) => {
      if (formalReturns) {
        return `
          <div class="regime-card">
            <span>${esc(item.label || item.regime)}</span>
            <strong class="${signedClass(item.return_5d_pct)}">5日 ${pct(item.return_5d_pct, 5)}</strong>
            <small>10日 ${pct(item.return_10d_pct, 5)} · 5日为正 ${number(item.positive_5d_pct, 2)}% · ${item.observations || 0}个状态日</small>
          </div>
        `;
      }
      const noTrades = Number(item.trades || 0) === 0;
      return `
        <div class="regime-card">
          <span>${esc(item.label || item.regime)}</span>
          <strong class="${signedClass(item.net_pnl)}">${noTrades ? '空仓 / 无交易' : `${Number(item.net_pnl) >= 0 ? '+' : ''}¥${number(item.net_pnl, 2)}`}</strong>
          <small>${item.trades || 0} 笔 · 胜率 ${number(item.win_rate_pct, 2)}% · 平均 ${pct(item.average_return_pct)}</small>
        </div>
      `;
    }).join('')
    : '<div class="empty-state compact">当前报告没有市场状态分层结果。</div>';
}

function renderExperiments(items) {
  factory$('#experimentList').innerHTML = items.length
    ? items.map((item) => `<li>${esc(item)}</li>`).join('')
    : '<li>等待下一次本地确定性研究生成可证伪实验。</li>';
}

function renderWorker(worker, summary) {
  const live = Boolean(worker.live);
  factory$('#activityStatus').textContent = live ? `运行中 · 第${worker.cycle}轮` : '未检测到新心跳';
  factory$('#activityStatus').className = `tag ${live ? 'green' : 'yellow'}`;
  const progress = worker.progress || {};
  const coverage = Number(progress.coverage_pct ?? summary.coverage_pct ?? 0);
  factory$('#workerProgressText').textContent = (
    `${Number(progress.downloaded_symbols ?? summary.downloaded_symbols).toLocaleString('zh-CN')} / `
    + `${Number(progress.target_symbols ?? summary.target_symbols).toLocaleString('zh-CN')} · `
    + `${coverage.toFixed(3)}%`
  );
  factory$('#workerProgressBar').style.width = `${Math.max(0, Math.min(100, coverage))}%`;

  const events = [...(worker.events || [])].reverse();
  factory$('#activityList').innerHTML = events.length
    ? events.map((item) => {
      const meta = [
        item.cycle ? `第${item.cycle}轮` : null,
        item.duration_seconds ? `${number(item.duration_seconds, 1)}秒` : null,
        item.usage?.total_tokens ? `${Number(item.usage.total_tokens).toLocaleString('zh-CN')} tokens` : null,
      ].filter(Boolean).join(' · ');
      return `
        <div class="activity-item">
          <time>${readableTime(item.time, true)}</time>
          <span>${esc(item.message || item.kind)}</span>
          <small>${esc(meta)}</small>
        </div>
      `;
    }).join('')
    : '<div class="empty-state compact">尚未记录后台研究事件。</div>';
}

function updateLiveTiming() {
  if (!liveResearchData) return;
  const worker = liveResearchData.worker || {};
  const source = worker.external_source || {};
  const timing = factory$('#liveStepTiming');
  if (!worker.live) {
    timing.textContent = '后台没有有效心跳，时间估算暂停。';
    return;
  }
  if (['circuit_open', 'source_blocked', 'fallback_blocked'].includes(source.source_state)) {
    timing.textContent = [
      `${source.source || '外部'}数据源已熔断，后台没有卡死`,
      `最后成功进度 ${number(source.progress_pct, 3)}%`,
      source.next_probe_at
        ? `下一次只探测一次：${readableTime(source.next_probe_at, true)}`
        : '等待人工切换备用源',
    ].join(' · ');
    return;
  }
  const pipeline = factoryData?.accelerated_validation?.runtime || {};
  if (pipeline.status === 'running' && pipeline.heartbeat_healthy) {
    const remaining = pipeline.phase_estimated_remaining_seconds;
    timing.textContent = [
      `整条15小时验证管线已运行 ${durationText(pipeline.pipeline_elapsed_seconds)}`,
      `当前阶段 ${pipeline.current_phase_label || '--'}`,
      remaining === null || remaining === undefined
        ? '正在根据真实处理量计算剩余时间'
        : `本阶段预计还需约 ${durationText(remaining)}`,
    ].join(' · ');
    return;
  }
  const sourceBlocked = ['circuit_open', 'source_blocked', 'fallback_blocked'].includes(source.source_state);
  const executionFinished = (
    worker.state === 'waiting'
    || worker.current_step === '研究门禁阻断'
    || (worker.message || '').includes('本轮执行链已100%结束')
  ) && !sourceBlocked;
  if (executionFinished) {
    const remaining = remainingTo(worker.next_cycle_at);
    timing.textContent = remaining === null
      ? '本轮执行已完成 · 当前没有后台回测，在线等待新的有效输入。'
      : `本轮执行已完成 · 当前不是卡住 · 距离下一轮约 ${durationText(remaining)}`;
    return;
  }
  if (worker.step_started_at && worker.step_expected_seconds) {
    const elapsed = Math.max(
      0,
      Math.floor((Date.now() - new Date(worker.step_started_at).getTime()) / 1000),
    );
    const remaining = Math.max(0, worker.step_expected_seconds - elapsed);
    timing.textContent = `已运行 ${durationText(elapsed)} · 本步骤预计还需约 ${durationText(remaining)}`;
    return;
  }
  if (worker.step_started_at) {
    const elapsed = Math.max(
      0,
      Math.floor((Date.now() - new Date(worker.step_started_at).getTime()) / 1000),
    );
    timing.textContent = `已运行 ${durationText(elapsed)} · 正在根据真实处理量计算剩余时间`;
    return;
  }
  timing.textContent = worker.message || '后台正在更新研究状态。';
}

function renderLiveResearch(data) {
  const snapshotAt = new Date(data.generated_at || 0).getTime();
  if (Number.isFinite(snapshotAt) && snapshotAt < gateSnapshotAt) return;
  if (Number.isFinite(snapshotAt)) gateSnapshotAt = Math.max(gateSnapshotAt, snapshotAt);
  liveResearchData = data;
  renderGateLevels(data);
  renderV25Campaign(data.v25_campaign || {});
  const worker = data.worker || {};
  renderFifteenHourSession(data.fifteen_hour_session || {});
  renderResearchTruth(data.research_truth || {});
  const source = worker.external_source || {};
  const deepseek = data.deepseek || {};
  const freshnessState = data.data_freshness?.overall_status || 'unknown';
  factory$('#factoryConnection').textContent = worker.live
    ? `实时状态已同步 · 数据时效 ${freshnessState}`
    : `实时状态已同步 · 后台无新心跳 · 数据时效 ${freshnessState}`;
  factory$('#factoryConnectionDot').className = `status-dot ${worker.live ? 'live' : 'error'}`;
  const paidApiWorkerState = worker.state === 'ai'
    || /DeepSeek|TradingAgents|外部模型/.test(worker.current_step || '');
  const sourceBlocked = ['circuit_open', 'source_blocked', 'fallback_blocked'].includes(source.source_state);
  const executionFinished = (
    worker.state === 'waiting'
    || worker.current_step === '研究门禁阻断'
    || (worker.message || '').includes('本轮执行链已100%结束')
  ) && !sourceBlocked;
  const heartbeatAge = Number(worker.heartbeat_age_seconds);
  const heartbeatHealthy = worker.live && Number.isFinite(heartbeatAge)
    ? heartbeatAge <= 30 : Boolean(worker.live);
  const localComputing = Boolean(worker.is_computing) && !paidApiWorkerState;
  const isWorking = localComputing && !executionFinished;
  const pipeline = factoryData?.accelerated_validation?.runtime || {};
  const pipelineActive = (
    pipeline.status === 'running'
    && pipeline.heartbeat_healthy
    && !executionFinished
  );
  const liveHistoricalGatePassed = Boolean(
    data.manual_pilot?.research_quality_passed,
  );
  factory$('#liveConsoleStatus').textContent = sourceBlocked
    ? `${source.source || '外部'}数据源熔断 · 后台在线`
    : executionFinished
    ? (liveHistoricalGatePassed
      ? '本轮执行完成 · 历史策略门禁已通过，等待新数据'
      : '本轮执行完成 · 研究门禁仍锁定')
    : paidApiWorkerState
      ? '外部付费 API 已禁用 · 本地研究继续'
      : (worker.state_label || '状态未知');
  factory$('#liveConsoleUpdatedAt').textContent = `状态更新 ${readableTime(data.generated_at, true)} · 每15秒刷新`;
  factory$('#liveConsolePulse').className = [
    'live-pulse',
    !worker.live ? 'error' : (isWorking ? 'working' : 'live'),
  ].join(' ');

  const modeLabels = {
    ai: '外部付费 API 已禁用',
    computing: worker.is_backtesting ? '正在回测' : '正在计算',
    waiting: '在线等待',
    blocked: worker.state_label || '研究门禁阻断',
    error: '自动重试',
    offline: '离线',
    working: '工作中',
  };
  factory$('#liveStepMode').textContent = modeLabels[worker.state]
    || worker.state_label || '--';
  if (executionFinished) factory$('#liveStepMode').textContent = '执行完成';
  factory$('#liveStepMode').className = `tag ${
    !worker.live ? 'red'
      : sourceBlocked ? 'red'
      : executionFinished ? 'green'
      : worker.state === 'waiting' ? 'green'
        : worker.state === 'blocked' ? 'red'
        : worker.state === 'error' ? 'red' : 'yellow'
  }`;
  factory$('#liveCurrentStep').textContent = sourceBlocked
    ? 'V12主数据源拒绝访问；等待备用源或人工恢复'
    : executionFinished
    ? '本轮100%完成，等待下一项有效研究'
    : paidApiWorkerState
    ? '外部付费 API 已禁用；本地确定性研究继续'
    : (worker.current_step || '等待状态');
  const detail = worker.active_detail || {};
  if (pipelineActive) {
    factory$('#liveStepDetail').textContent = [
      `真实管线 ${number(pipeline.progress_pct, 2)}%`,
      `当前阶段 ${number(pipeline.phase_progress_pct, 2)}%`,
      `${Number(pipeline.phase_completed_units || 0).toLocaleString('zh-CN')}/${Number(pipeline.phase_total_units || 0).toLocaleString('zh-CN')}个真实工作单元`,
      pipeline.blocker,
    ].filter(Boolean).join(' · ');
  } else if (detail.detail_mode === 'exact') {
    const rate = Number(detail.rate_symbols_per_second || 0);
    factory$('#liveStepDetail').textContent = [
      `精确进度 ${detail.processed_symbols || 0}/${detail.total_symbols || 0}（${number(detail.progress_pct, 2)}%）`,
      detail.current_code ? `当前 ${detail.current_code}` : null,
      rate > 0 ? `速度 ${(rate * 60).toFixed(1)}只/分钟` : null,
      detail.eta_seconds !== null && detail.eta_seconds !== undefined
        ? `ETA ${durationText(detail.eta_seconds)}` : null,
    ].filter(Boolean).join(' · ');
  } else if (detail.detail_mode === 'legacy_lower_bound') {
    factory$('#liveStepDetail').textContent = [
      `本轮至少已写入 ${detail.updated_symbols_at_least || 0}/${detail.total_symbols || 0} 个文件`,
      detail.notice,
    ].filter(Boolean).join(' · ');
  } else {
    const diagnosis = !worker.live
      ? '诊断：后台离线或心跳超时'
      : sourceBlocked
        ? `诊断：${source.source_error || '外部数据源拒绝访问'}；已停止高频重试`
      : executionFinished
        ? `诊断：正常完成，不是卡住；${heartbeatHealthy ? '守护进程在线' : '心跳异常'}`
        : worker.state === 'error'
          ? `诊断：任务失败，正在自动重试；${worker.last_error || '等待错误详情'}`
          : paidApiWorkerState
            ? '诊断：外部付费 API 已禁用，本地研究流程继续运行'
          : localComputing
            ? '诊断：本地计算或回测正在运行'
            : '诊断：后台在线，正在等待可执行任务';
    factory$('#liveStepDetail').textContent = [
      diagnosis,
      worker.message || '等待逐项进度…',
      executionFinished && worker.next_cycle_at
        ? `下一次检查：${readableTime(worker.next_cycle_at, true)}` : null,
    ].filter(Boolean).join(' · ');
  }
  factory$('#liveHeartbeat').textContent = worker.heartbeat_age_seconds === null
    ? '--'
    : `${worker.heartbeat_age_seconds}秒前`;
  factory$('#liveCycleNumber').textContent = worker.work_sequence
    ? `真实任务 #${worker.work_sequence}`
    : '等待首个真实任务';
  const displayedProgress = executionFinished
    ? 100
    : sourceBlocked ? 0
    : pipelineActive
      ? Number(pipeline.progress_pct) || 0
      : Number(worker.cycle_progress_pct) || 0;
  const progressSuffix = pipelineActive
    ? ` · ${pipeline.current_phase_label || '真实历史验证'}`
    : '';
  factory$('#liveCycleProgressText').textContent = `${number(displayedProgress, 1)}%${progressSuffix}`;
  factory$('#liveCycleProgressBar').style.width = `${Math.max(0, Math.min(100, displayedProgress))}%`;
  factory$('#liveCycleProgressBar').className = executionFinished ? 'complete' : '';
  const queueTasks = data.experiment_queue?.tasks || [];
  const queueCounts = data.experiment_queue?.counts || queueTasks.reduce((counts, item) => {
    const key = item.status || 'unknown';
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {});
  const queueTotal = Number(data.experiment_queue?.total ?? queueTasks.length);
  factory$('#experimentQueueSummary').textContent = queueTotal
    ? `实验队列 ${queueTotal}项 · 运行中 ${queueCounts.running || 0} · 完成 ${queueCounts.completed || 0} · 去重跳过 ${queueCounts.unchanged || 0} · 历史封存 ${queueCounts.archived || 0} · 失败 ${queueCounts.failed || 0}`
    : '实验队列将在新版研究工人启动后建立。';
  const stages = (data.cycle_stages || []).map((item) => {
    if (/DeepSeek|TradingAgents|AI/.test(item.label || '')) {
      return {...item, label: `${item.label}（外部付费 API 已禁用）`, status: 'archived'};
    }
    if (item.id === 'v4_3') {
      return {...item, label: 'V4.3历史失败（已封存）', status: 'archived'};
    }
    if (item.id === 'v6_factor') {
      return {...item, label: 'V6非涨停MAX（已封存）', status: 'archived'};
    }
    return executionFinished ? {...item, status: 'completed'} : item;
  });
  const v5Task = queueTasks.find((item) => item.task_id === 'v5_limit_up_factor');
  if (!stages.some((item) => item.id === 'v5_factor') && (
    v5Task || (worker.current_step || '').includes('V5')
  )) {
    const v5Statuses = {
      running: 'active',
      completed: 'completed',
      unchanged: 'completed',
      failed: 'failed',
      blocked: 'failed',
      archived: 'archived',
    };
    stages.splice(Math.max(0, stages.length - 2), 0, {
      id: 'v5_factor',
      label: 'V5首次涨停效应',
      status: executionFinished
        ? (v5Statuses[v5Task?.status] || 'completed')
        : (v5Statuses[v5Task?.status] || 'pending'),
      expected_seconds: 240,
    });
  }
  factory$('#liveCycleStages').innerHTML = stages.map((item) => `
    <li class="${esc(item.status)}" title="预计 ${durationText(item.expected_seconds)}">
      ${esc(item.label)}
    </li>
  `).join('');
  updateLiveTiming();

  const deepseekWork = deepseek.work || {};
  const deepseekDataSupport = deepseek.data_support || {};
  const deepseekRoundTasks = deepseek.tasks || [];
  const deepseekResolvedTasks = deepseekRoundTasks.filter((task) => (
    task.status === 'success' || task.status === 'failed'
  )).length;
  const deepseekProgressPct = deepseekRoundTasks.length
    ? Math.round((deepseekResolvedTasks / deepseekRoundTasks.length) * 1000) / 10
    : 0;
  const deepseekProgressText = factory$('#deepseekProgressText');
  const deepseekProgressBar = factory$('#deepseekProgressBar');
  const deepseekProgressEta = factory$('#deepseekProgressEta');
  factory$('#deepseekLiveState').textContent = '已禁用';
  factory$('#deepseekLiveState').className = 'tag gray';
  factory$('#deepseekCardTitle').textContent = '外部付费模型 · 历史记录';
  factory$('#deepseekLiveAction').textContent = '外部付费 API 已禁用，本地确定性研究继续';
  factory$('#deepseekLiveRole').textContent = '历史证据只读保留；页面不会发起新模型请求。';
  deepseekProgressText.textContent = deepseekRoundTasks.length
    ? `${deepseekRoundTasks.length} 项只读历史记录`
    : '暂无历史记录';
  if (deepseekProgressBar) {
    deepseekProgressBar.style.width = `${Math.max(0, Math.min(100, deepseekProgressPct))}%`;
  }
  deepseekProgressEta.textContent = deepseekRoundTasks.length
    ? `历史完成 ${deepseekResolvedTasks}/${deepseekRoundTasks.length}；不会继续调用`
    : '外部付费 API 已禁用；不会建立新任务。';
  const deepseekWorkingNow = factory$('#deepseekWorkingNow');
  const deepseekCurrentJob = factory$('#deepseekCurrentJob');
  const deepseekCurrentElapsed = factory$('#deepseekCurrentElapsed');
  const deepseekDataReady = factory$('#deepseekDataReady');
  const deepseekLatestVerdict = factory$('#deepseekLatestVerdict');
  deepseekWorkingNow.textContent = '否 · 已禁用';
  deepseekWorkingNow.className = 'blocked';
  deepseekCurrentJob.textContent = '无外部模型任务；本地研究继续';
  deepseekCurrentJob.title = deepseekCurrentJob.textContent;
  deepseekCurrentElapsed.textContent = readableTime(deepseek.last_run_at, true);
  deepseekDataReady.textContent = deepseekDataSupport.can_support_next_factor_test
    ? '可以进入本地预注册复核'
    : '不能进入回测';
  deepseekDataReady.className = deepseekDataSupport.can_support_next_factor_test
    ? 'ready' : 'blocked';
  deepseekLatestVerdict.textContent = [
    '只读历史裁决',
    deepseekWork.latest_output,
    deepseekDataSupport.reason,
  ].filter(Boolean).join(' · ');
  factory$('#deepseekTaskList').innerHTML = deepseek.tasks?.length
    ? deepseek.tasks.map((task) => `
      <div class="deepseek-task archived">
        <i></i>
        <span>${esc(task.title || task.task_id)}</span>
        <b>历史记录</b>
        <small>${[
          task.status === 'success' && task.finished_at
            ? `${readableTime(task.finished_at, true)}完成` : null,
          task.usage?.total_tokens
            ? `${Number(task.usage.total_tokens).toLocaleString('zh-CN')} tokens`
            : null,
        ].filter(Boolean).join(' · ')}</small>
      </div>
    `).join('')
    : '<div class="empty-state compact">暂无外部模型历史记录；本地确定性研究继续。</div>';
  factory$('#deepseekLastRun').textContent = readableTime(deepseek.last_run_at, true);
  const totalTokens = deepseek.last_usage?.total_tokens
    || (deepseek.tasks || []).reduce(
      (sum, task) => sum + Number(task.usage?.total_tokens || 0),
      0,
    );
  factory$('#deepseekLiveTokens').textContent = totalTokens
    ? `${Number(totalTokens).toLocaleString('zh-CN')} tokens`
    : '暂无';

  factory$('#liveArtifacts').innerHTML = (data.artifacts || []).slice(0, 3).map((item) => `
    <div class="live-artifact">
      <strong title="${esc(item.file)}">${esc(item.label)}</strong>
      <small>${number(item.size_kb, 1)} KB</small>
      <time>${readableTime(item.generated_at, true)}</time>
      <small>已落盘</small>
    </div>
  `).join('') || '<div class="empty-state compact">尚无研究产物。</div>';

  const unlockProgress = data.unlock_progress || {};
  const completedPct = Math.max(0, Math.min(
    100, Number(unlockProgress.evidence_completed_pct || 0),
  ));
  const remainingPct = Math.max(0, 100 - completedPct);
  const productHistoricalGate = unlockProgress.historical_gate || {};
  const historicalPct = Math.max(0, Math.min(
    100, Number(productHistoricalGate.completed_pct ?? completedPct),
  ));
  const forward = unlockProgress.forward_validation || {};
  const calendarPct = Math.max(0, Math.min(
    100, Number(forward.calendar_progress_pct || 0),
  ));
  const tradePct = Math.max(0, Math.min(
    100, Number(forward.trade_progress_pct || 0),
  ));
  const productMvp = unlockProgress.product_mvp || {};
  factory$('#unlockEta').textContent = data.unlock_estimate?.label || '--';
  factory$('#unlockGateCount').textContent = (
    `严格产品历史门禁 ${productHistoricalGate.completed_steps || 0}/${productHistoricalGate.total_steps || 3} 项 · 最终真钱证据 ${number(completedPct, 0)}%`
  );
  factory$('#unlockProgressPct').textContent = `${number(historicalPct, 0)}%`;
  factory$('#unlockProgressBar').style.width = `${historicalPct}%`;
  factory$('#unlockCompletedPct').textContent = productHistoricalGate.passed
    ? '严格产品证书已验证'
    : `产品历史证据 ${number(historicalPct, 0)}%`;
  factory$('#unlockRemainingPct').textContent = productHistoricalGate.passed
    ? '可由系统启动独立前向时钟'
    : '旧历史复现不计入产品门禁';
  factory$('#forwardCalendarValue').textContent = forward.clock_started
    ? `${number(forward.elapsed_days, 1)} / ${Number(forward.minimum_days || 42)} 天 · ${number(calendarPct, 1)}%`
    : '尚未启动';
  factory$('#forwardCalendarBar').style.width = `${calendarPct}%`;
  factory$('#forwardTradesValue').textContent = (
    `${Number(forward.completed_trades || 0)} / ${Number(forward.target_trades || 30)} 笔 · ${number(tradePct, 1)}%`
  );
  factory$('#forwardTradesBar').style.width = `${tradePct}%`;
  factory$('#forwardDecisionValue').textContent = (
    `正式裁决 ${Number(forward.formal_decisions || 0)} 条 · 信号拒绝 ${Number(forward.rejected_decisions || 0)} 条 · 执行拒绝 ${Number(forward.execution_rejections || 0)} 条 · 持仓 ${Number(forward.open_positions || 0)} 笔 · 已实现净损益 ¥${number(forward.realized_net_pnl || 0, 2)} · 严格前向进度 ${number(forward.strict_gate_progress_pct || 0, 1)}%`
  );
  factory$('#productMvpValue').textContent = (
    `${productMvp.label || '产品MVP'}：${productMvp.status === 'in_progress' ? '开发中' : (productMvp.status || '--')} · 目标 ${productMvp.target_date || '--'}`
  );
  factory$('#unlockCurrentGate').textContent = [
    `当前卡点：${unlockProgress.current_gate_label || '等待门禁状态'}`,
    unlockProgress.current_gate_detail,
  ].filter(Boolean).join(' · ');
  factory$('#unlockProgressDefinition').textContent = [
    unlockProgress.definition,
    forward.definition,
    productMvp.definition,
  ].filter(Boolean).join(' · ');
  factory$('#unlockWarning').textContent = [
    data.unlock_estimate?.basis,
    data.estimate_warning,
  ].filter(Boolean).join(' · ');
  factory$('#unlockRoadmap').innerHTML = (data.roadmap || []).map((item) => `
    <div class="unlock-step ${esc(item.status)}" title="${esc(item.detail)}">
      <i></i>
      <strong>${esc(item.label)}</strong>
      <small>${esc(item.estimate)} · ${Number(item.weight_pct || 0)}%</small>
    </div>
  `).join('');

  const pilot = data.manual_pilot || {};
  const pilotMetrics = pilot.metrics_10bp || {};
  const combinedMetrics = pilot.combined_metrics_10bp || {};
  const integrityAudit = pilot.integrity_audit || {};
  const winTarget = integrityAudit.win_rate_target || {};
  const implementationAudit = integrityAudit.trade_implementation || {};
  const pilotCandidate = pilot.candidate || null;
  const observationCandidate = pilot.observation_candidate || null;
  const historicalEvidenceRecorded = Boolean(pilot.research_quality_passed);
  factory$('#v21PilotKicker').textContent = 'HISTORICAL EVIDENCE · NOT A PRODUCT GATE';
  factory$('#v21PilotTitle').textContent = historicalEvidenceRecorded
    ? 'V23已核验历史观察 · 产品与真钱锁定'
    : 'V21/V22历史证据 · 产品与真钱锁定';
  factory$('#v21PilotStatus').textContent = pilot.unlocked
    ? '真钱人工确认已授权'
    : historicalEvidenceRecorded ? '历史完整性已核验 · 产品锁定' : '尚未通过';
  factory$('#v21PilotStatus').className = `tag ${pilot.unlocked ? 'green' : 'red'}`;
  factory$('#v21PilotPanel').className = `panel v21-pilot-panel ${pilot.unlocked ? 'unlocked' : 'locked'}`;
  factory$('#v21PilotMessage').textContent = pilot.message || '等待V21/V22真实裁决。';
  factory$('#v21Return').textContent = `${number(pilotMetrics.total_return_pct, 3)}%`;
  factory$('#v21Trades').textContent = `${Number(pilotMetrics.trades || 0)}笔`;
  factory$('#v21WinRate').textContent = `${number(pilotMetrics.win_rate_pct, 3)}%`;
  factory$('#v24WinTarget').textContent = Object.keys(winTarget).length
    ? `${number(winTarget.observed_pct, 3)}% · ${winTarget.supported ? '已支持' : '未达到'}`
    : '等待独立复核';
  factory$('#v24Implementation').textContent = Object.keys(implementationAudit).length
    ? `${implementationAudit.passed ? '通过' : '失败'} · ${Number(implementationAudit.checked_trades || 0)}笔`
    : '等待独立复核';
  factory$('#v21Drawdown').textContent = `${number(pilotMetrics.max_drawdown_pct, 3)}%`;
  factory$('#v21ProfitFactor').textContent = number(pilotMetrics.profit_factor, 3);
  if (pilot.unlocked && pilotCandidate) {
    const checks = (pilotCandidate.execution_checks || []).map((item) => (
      `<li>${esc(item)}</li>`
    )).join('');
    factory$('#v21Candidate').className = 'v21-candidate actionable';
    factory$('#v21Candidate').innerHTML = `
      <div class="v21-candidate-title">
        <div><span>当前可复核候选</span><strong>${esc(pilotCandidate.name || pilotCandidate.code)} · ${esc(pilotCandidate.code)}</strong></div>
        <a href="${esc(pilotCandidate.analysis_url || `/analysis?code=${pilotCandidate.code}&interval=5m`)}">打开5分钟K线</a>
      </div>
      <p>公告日 ${esc(pilotCandidate.announcement_date)} · 增持占流通股 ${number(pilotCandidate.increase_float_pct, 4)}%</p>
      <p>计划复核 ${esc(pilotCandidate.planned_entry_date || '--')} 开盘 · 计划退出 ${esc(pilotCandidate.planned_exit_date || '--')} 收盘 · ${esc(pilotCandidate.phase_label || '')}</p>
      <p class="candidate-action">${esc(pilotCandidate.action || '')}</p>
      <ul>${checks}</ul>
    `;
  } else {
    factory$('#v21Candidate').className = 'v21-candidate empty-state compact';
    factory$('#v21Candidate').textContent = pilot.unlocked
      ? (pilot.current_decision?.reason || '今天没有新的V21公告信号：保持空仓等待，不追旧信号。')
      : observationCandidate
        ? `观察候选 ${observationCandidate.name || observationCandidate.code} · ${observationCandidate.code}；仅用于研究记录，不构成买入授权。`
        : '当前没有观察候选；真钱门禁未通过，不产生买入机会。';
  }
  const pilotRule = pilot.execution_rule || {};
  factory$('#v21EntryRule').textContent = pilotRule.entry || '--';
  factory$('#v21ExitRule').textContent = pilotRule.exit || '--';
  factory$('#v21MissedRule').textContent = pilotRule.missed_entry || '--';
  factory$('#v21PositionRule').textContent = pilotRule.position_limit || '--';
  factory$('#v22CombinedEvidence').textContent = Object.keys(combinedMetrics).length
    ? `${historicalEvidenceRecorded ? 'V23 MA200过滤后的跨年份历史证据' : '跨年份合并证据'}：${Number(combinedMetrics.trades || 0)}笔 · 10BP收益 ${number(combinedMetrics.total_return_pct, 3)}% · 盈利因子 ${number(combinedMetrics.profit_factor, 3)} · 回撤 ${number(combinedMetrics.max_drawdown_pct, 3)}%；${historicalEvidenceRecorded ? '仅历史完整性证据，不计严格产品门禁' : `严格门禁失败 ${Number((pilot.failed_research_gates || []).length)} 项`}。`
    : '';
  factory$('#v21Limitations').textContent = (pilot.limitations || []).join(' · ');

  // Populate strategy proximity dashboard
  renderStrategyProximity({
    ...data,
    v44_shadow: v44ShadowData || liveResearchData?._v44Shadow || {},
  });
}

async function refreshV44Shadow() {
  try {
    const response = await fetch('/api/research/v44-shadow', {cache: 'no-store'});
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

async function refreshLiveResearch() {
  try {
    const response = await fetch('/api/research/live?view=compact', {cache: 'no-store'});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || '实时研究状态读取失败');
    renderLiveResearch(body);
  } catch (error) {
    factory$('#liveConsoleStatus').textContent = error.message;
    factory$('#liveConsolePulse').className = 'live-pulse error';
  }
}

function renderStrategyProximity(data) {
  const pilot = data.manual_pilot || {};
  const pilotMetrics = pilot.metrics_10bp || {};
  const combinedMetrics = pilot.combined_metrics_10bp || {};
  const canonicalMetrics = pilot.canonical_metrics_10bp || {};
  const integrityAudit = pilot.integrity_audit || {};
  const winTarget = integrityAudit.win_rate_target || {};
  const implementationAudit = integrityAudit.trade_implementation || {};
  const researchGates = pilot.failed_research_gates || [];
  const historicalEvidenceRecorded = Boolean(pilot.research_quality_passed);
  const productHistoricalGate = data.gate_levels?.historical || {};
  const productHistoryPassed = productHistoricalGate.passed === true;
  const v23Metrics = Object.keys(canonicalMetrics).length
    ? canonicalMetrics
    : historicalEvidenceRecorded && Object.keys(combinedMetrics).length
      ? combinedMetrics
      : pilotMetrics;
  const shadow = data.v44_shadow || {};
  const forwardEvidence = shadow.forward_evidence || {};
  const v44HistoricalMetrics = shadow.historical_metrics || {};
  const v44ValidationMetrics = shadow.historical_validation_metrics || {};
  const v44HistoricalEvidence = shadow.historical_evidence || {};
  const hasV44HistoricalDiagnostic = Object.keys(v44HistoricalMetrics).length > 0;
  // Never replace the current product-gate metrics with V44's best observed
  // slice.  That slice is a failed historical diagnostic, not an eligible
  // validation/observation pair and not a product candidate.
  const primaryMetrics = v23Metrics;

  // Primary strategy stats are the current V23 cross-year, cost-inclusive
  // evidence.  They are diagnostic until a strict product certificate exists.
  const primaryWinRate = Number(primaryMetrics.win_rate_pct || winTarget.observed_pct || 0);
  const primaryAnnualReturn = Number(primaryMetrics.annualized_return_pct || 0);
  const primaryTrades = Number(primaryMetrics.trades || 0);
  const primaryDrawdown = Number(primaryMetrics.max_drawdown_pct || 0);
  const primaryProfitFactor = Number(primaryMetrics.profit_factor || 0);
  const primaryWilsonLow = winTarget.wilson_95_ci_pct
    ? Number(winTarget.wilson_95_ci_pct[0] || 0) : 0;
  const primaryTradingDays = primaryMetrics.trading_days == null
    ? null : Number(primaryMetrics.trading_days);
  const primaryPositiveFoldRatio = primaryMetrics.positive_fold_ratio_pct == null
    ? null : Number(primaryMetrics.positive_fold_ratio_pct);

  const winRatePct = Math.min(100, Math.max(0, (primaryWinRate / 60) * 100));
  const annualReturnPct = Math.min(100, Math.max(0, (primaryAnnualReturn / 15) * 100));
  const winRateGap = Math.max(0, 60 - primaryWinRate);
  const annualReturnGap = Math.max(0, 15 - primaryAnnualReturn);

  factory$('#proximityPrimaryBadge').textContent = 'V23 历史诊断';
  factory$('#proximityPrimaryTitle').textContent = '股东增持事件驱动';
  factory$('#proximityPrimaryScope').textContent = '历史回测数据，仅供参考研究';

  factory$('#v23WinRateValue').textContent = `${number(primaryWinRate, 3)}%`;
  factory$('#v23WinRateGap').textContent = winRateGap <= 0 ? '已达标' : `${number(winRateGap, 1)}%`;
  factory$('#v23WinRateGap').className = winRateGap <= 0 ? 'up' : 'down';
  factory$('#v23WinRateBar').style.width = `${winRatePct}%`;
  factory$('#v23WinRateBar').className = `gauge-fill ${winRatePct >= 100 ? 'complete' : winRatePct >= 80 ? 'close' : 'far'}`;

  factory$('#v23AnnualReturnValue').textContent = `${pct(primaryAnnualReturn)}`;
  factory$('#v23AnnualReturnGap').textContent = annualReturnGap <= 0 ? '已达标' : `${pct(annualReturnGap)}`;
  factory$('#v23AnnualReturnGap').className = annualReturnGap <= 0 ? 'up' : 'down';
  factory$('#v23AnnualReturnBar').style.width = `${annualReturnPct}%`;
  factory$('#v23AnnualReturnBar').className = `gauge-fill ${annualReturnPct >= 100 ? 'complete' : annualReturnPct >= 50 ? 'close' : 'far'}`;

  factory$('#v23TradeCount').textContent = `${primaryTrades} 笔`;
  factory$('#v23TradeCount').className = primaryTrades >= 100 ? 'up' : 'down';
  factory$('#v23MaxDrawdown').textContent = `${number(primaryDrawdown, 3)}%`;
  factory$('#v23MaxDrawdown').className = primaryDrawdown <= 15 ? 'up' : 'down';
  factory$('#v23ProfitFactor').textContent = number(primaryProfitFactor, 3);
  factory$('#v23ProfitFactor').className = primaryProfitFactor >= 1.2 ? 'up' : 'down';
  factory$('#v23WilsonLow').textContent = `${number(primaryWilsonLow, 3)}%`;
  factory$('#v23WilsonLow').className = primaryWilsonLow > 50 ? 'up' : 'down';

  // Gate checklist
  const strictGateLabels = {
    oos_trading_days_at_least_400: '跨年份回测 ≥400 交易日',
    completed_trades_at_least_100: '完成 ≥100 笔交易',
    observed_win_rate_at_least_60: '胜率 ≥60%',
    win_rate_wilson_95_ci_low_above_50: 'Wilson 95% CI 下界 >50%',
    observed_net_return_positive: '扣全成本净收益为正',
    observed_annualized_return_positive: '含成本年化收益 ≥15%',
    observed_profit_factor_at_least_1_20: '盈利因子 ≥1.20',
    observed_max_drawdown_at_most_15: '最大回撤 ≤15%',
    positive_fold_ratio_at_least_60: '正收益分段 ≥60%',
    full_cost_model_recorded: '已记录完整成本模型',
    no_open_positions_at_end: '样本末无未结持仓',
  };

  // Build gate conditions from available data
  const gateConditions = [
    { key: 'oos_trading_days_at_least_400', value: primaryTradingDays, threshold: '400', unit: '天', available: primaryTradingDays != null, passed: primaryTradingDays != null && primaryTradingDays >= 400 },
    { key: 'completed_trades_at_least_100', value: primaryTrades, threshold: '100', unit: '笔', passed: primaryTrades >= 100 },
    { key: 'observed_win_rate_at_least_60', value: primaryWinRate, threshold: '60', unit: '%', passed: primaryWinRate >= 60 },
    { key: 'win_rate_wilson_95_ci_low_above_50', value: primaryWilsonLow, threshold: '50', unit: '%', passed: primaryWilsonLow > 50 },
    { key: 'observed_net_return_positive', value: Number(primaryMetrics.total_return_pct || 0), threshold: '0', unit: '%', passed: Number(primaryMetrics.total_return_pct || 0) > 0 },
    { key: 'observed_annualized_return_positive', value: primaryAnnualReturn, threshold: '15', unit: '%', passed: primaryAnnualReturn >= 15 },
    { key: 'observed_profit_factor_at_least_1_20', value: primaryProfitFactor, threshold: '1.20', unit: '', passed: primaryProfitFactor >= 1.20 },
    { key: 'observed_max_drawdown_at_most_15', value: primaryDrawdown, threshold: '15', unit: '%', passed: primaryDrawdown <= 15 },
    { key: 'positive_fold_ratio_at_least_60', value: primaryPositiveFoldRatio, threshold: '60', unit: '%', available: primaryPositiveFoldRatio != null, passed: primaryPositiveFoldRatio != null && primaryPositiveFoldRatio >= 60 },
    { key: 'full_cost_model_recorded', value: primaryMetrics.transaction_fees != null, threshold: '已记录', unit: '', passed: primaryMetrics.transaction_fees != null },
    { key: 'no_open_positions_at_end', value: '通过', threshold: '0', unit: '笔', passed: primaryMetrics.open_positions_end != null && Number(primaryMetrics.open_positions_end) === 0 },
  ];

  const passedCount = gateConditions.filter((g) => g.passed).length;
  const totalGates = gateConditions.length;

  const forwardCompleted = Number(forwardEvidence.completed_trades || 0);
  const forwardMinimum = Number(forwardEvidence.minimum_completed_trades || 30);
  factory$('#proximityBestStrategy').textContent = 'V23 股东增持事件驱动';
  factory$('#proximityBestStrategy').className = 'tag yellow';

  factory$('#proximitySummary').textContent = `V23 当前 ${Number(v23Metrics.trades || 0)} 笔、胜率 ${number(v23Metrics.win_rate_pct, 3)}%、年化 ${number(v23Metrics.annualized_return_pct, 3)}%。历史回测数据，仅供参考研究。`;

  factory$('#v23NextAction').textContent = '历史回测数据仅供研究参考。实盘交易需独立验证。';

  // V3 backup
  const v3Strategies = (factoryData?.strategies || []).filter((s) => s.strategy_id === 'A');
  const v3Data = v3Strategies.length ? v3Strategies[0] : null;
  const v3Metrics = v3Data?.metrics || {};
  factory$('#v3WinRate').textContent = `${number(v3Metrics.win_rate_pct, 2)}%`;
  factory$('#v3AnnualReturn').textContent = pct(v3Metrics.annualized_return_pct);
  factory$('#v3TradeCount').textContent = `${v3Metrics.trades || '--'} 笔`;
  factory$('#v3MaxDrawdown').textContent = `${number(v3Metrics.max_drawdown_pct, 2)}%`;

  // V44 shadow
  const sampleStatus = forwardEvidence.sample_status || {};
  factory$('#v44WaitingSignals').textContent = `${Number(shadow.signals_waiting_next_open || 0)} 个`;
  factory$('#v44WaitingSignals').className = Number(shadow.signals_waiting_next_open || 0) > 0 ? 'up' : '';
  factory$('#v44ObservingDays').textContent = `${Number(shadow.signal_observations || 0)} 天`;
  factory$('#v44GateStatus').textContent = '影子观察 · 不计产品门禁';
  factory$('#v44GateStatus').className = 'down';
  factory$('#v44CompletedTrades').textContent = `${Number(forwardEvidence.completed_trades || 0)} / ${Number(forwardEvidence.minimum_completed_trades || 30)} 笔`;
  factory$('#v44ShadowNote').textContent = Number(shadow.signals_waiting_next_open || 0) > 0
      ? `${Number(shadow.signals_waiting_next_open)} 个冻结信号等待下一已发布交易日开盘；系统将自动记录含成本纸面执行，无需人工确认，不向券商下单。`
      : Number(sampleStatus.open_not_due || 0) > 0
        ? `${Number(sampleStatus.open_not_due)} 个纸面持仓等待第20个交易日退出；系统自动续跑，无需人工操作。`
        : '影子观察在线：只写研究纸面账本，不向券商下单，也不计入正式产品前向。';

  // DeepSeek efficiency
  const deepseek = data.deepseek || {};
  const assessment = deepseek.assessment || {};
  const rounds = Number(assessment.discovery_round || data.deepseek_rounds || 0);
  const actionableCandidates = assessment.actionable_candidate ? 1 : 0;
  const totalTokens = Number(assessment.usage?.total_tokens || deepseek.last_usage?.total_tokens || 0);
  const estCumulativeTokens = rounds > 0 ? rounds * 50000 : totalTokens;

  factory$('#dsEfficiencyRounds').textContent = `${rounds} 轮`;
  factory$('#dsEfficiencyCandidates').textContent = `${actionableCandidates} 个`;
  factory$('#dsEfficiencyCandidates').className = actionableCandidates > 0 ? 'up' : 'down';
  factory$('#dsEfficiencyTokens').textContent = estCumulativeTokens > 0
    ? `约 ${(estCumulativeTokens / 1000000).toFixed(1)}M`
    : '--';
  factory$('#dsEfficiencyStatus').textContent = '已禁用 · 历史记录';

  const dsEfficiencyNote = factory$('#dsEfficiencyNote');
  if (rounds >= 100 && actionableCandidates === 0) {
    dsEfficiencyNote.textContent = `历史记录：已运行 ${rounds} 轮发现，0 个可执行候选。外部付费 API 已禁用。`;
    dsEfficiencyNote.className = 'proximity-note warning';
    factory$('#deepseekEfficiencyCard').classList.add('efficiency-warning');
  } else if (rounds >= 50 && actionableCandidates === 0) {
    dsEfficiencyNote.textContent = `历史记录：${rounds} 轮尚无产出；外部付费 API 已禁用。`;
    dsEfficiencyNote.className = 'proximity-note';
  } else {
    dsEfficiencyNote.textContent = '历史模型记录只读保留；外部付费 API 已禁用。';
    dsEfficiencyNote.className = 'proximity-note';
  }

  // Quick actions
  const actions = [];
  if (!productHistoryPassed) {
    actions.push('保持已封存策略不变；仅对新的预注册机制做全新验证、观察和独立复现。');
  }
  if (Number(shadow.signals_waiting_next_open || 0) > 0) {
    actions.push(`V44影子观察：下一完整交易日发布后记录 ${Number(shadow.signals_waiting_next_open || 0)} 个T+1纸面入场；该记录不计产品前向。`);
  }
  if (!pilot.integrity_audit?.trade_implementation?.passed) {
    actions.push('修复V25-H3 shift(1)数据泄漏bug，重新验证回测结果');
  }
  actions.push('等待新机制或新增数据；不把历史最佳区间、重复回测或已停用模型的历史调用次数当作产品进展。');

  factory$('#quickActionsList').innerHTML = actions.length
    ? actions.map((a, i) => `<li><span>${i + 1}.</span> ${esc(a)}</li>`).join('')
    : '<li>正在分析最优路径…</li>';
}

function renderFactory(data) {
  factoryData = data;
  const snapshotAt = new Date(data.generated_at || 0).getTime();
  if (!Number.isFinite(snapshotAt) || snapshotAt >= gateSnapshotAt) {
    if (Number.isFinite(snapshotAt)) gateSnapshotAt = snapshotAt;
    renderGateLevels(data);
  }
  renderObjective(data.objective);
  renderV25Campaign(data.v25_campaign || {});
  renderV27Adjudication(data.v27_new_mechanism || {});
  renderSummary(data);
  renderStages(data.stages || []);
  renderLabelProtocol(data.label_protocol || {});
  renderFifteenHourSession(data.fifteen_hour_session || {});
  renderAcceleratedValidation(
    data.accelerated_validation || {},
    data.delivery_plan || [],
  );
  renderBlockers(data.blockers || []);
  renderFactors(data.factors || {}, data.formal_research || {});
  renderV41(data.v4_1_research || {});
  renderV42(data.v4_2_research || {});
  renderV5(data.v5_research || {});
  renderV6(data.v6_research || {});
  renderV9(data.v9_research || {});
  renderV14(data.v14_research || {});
  renderV15(data.v15_research || {});
  renderV16(data.v16_research || {});
  renderV17(data.v17_research || {});
  renderV10(data.v10_research || {});
  renderV11(data.v11_research || {});
  renderV44(data.v4_4_research || {});
  renderStrategies(data.strategies || []);
  renderRegimes(data.regime_matrix || {});
  renderExperiments(data.next_experiments || []);
  renderWorker(data.worker || {}, data.summary || {});
  factory$('#factoryGeneratedAt').textContent = `完整报告生成于 ${readableTime(data.generated_at)} · 实时状态每15秒同步`;
  factory$('#factorySafety').textContent = data.safety?.reason || '研究结果保持锁定。';

  // Re-render strategy proximity with fresh factory data (strategies, V3)
  if (liveResearchData) {
    renderStrategyProximity({
      ...liveResearchData,
      v44_shadow: v44ShadowData || liveResearchData._v44Shadow || {},
      _factoryRefreshed: true,
    });
  }

  const factoryAt = new Date(data.generated_at || 0).getTime();
  const liveAt = new Date(liveResearchData?.generated_at || 0).getTime();
  if (!Number.isFinite(liveAt) || !Number.isFinite(factoryAt) || factoryAt >= liveAt) {
    const freshnessState = data.data_freshness?.overall_status || 'unknown';
    factory$('#factoryConnection').textContent = data.summary?.worker_live
      ? `完整报告已同步 · 后台有新心跳 · 数据时效 ${freshnessState}`
      : `完整报告已同步 · 后台无新心跳 · 数据时效 ${freshnessState}`;
    factory$('#factoryConnectionDot').className = `status-dot ${data.summary?.worker_live ? 'live' : 'error'}`;
  }
}

async function refreshFactory(showToast = false) {
  const button = factory$('#factoryRefresh');
  button.disabled = true;
  try {
    const response = await fetch('/api/research/factory', {cache: 'no-store'});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || '研究工厂读取失败');
    renderFactory(body);
    if (showToast) showFactoryToast('研究报告已刷新');
  } catch (error) {
    factory$('#factoryConnection').textContent = error.message;
    factory$('#factoryConnectionDot').className = 'status-dot error';
    showFactoryToast(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

factory$('#factorFilters').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-stage]');
  if (!button) return;
  factorStageFilter = button.dataset.stage;
  factory$('#factorFilters').querySelectorAll('button').forEach((item) => {
    const active = item === button;
    item.classList.toggle('active', active);
    item.setAttribute('aria-pressed', String(active));
  });
  if (factoryData) renderFactors(
    factoryData.factors || {},
    factoryData.formal_research || {},
  );
});

factory$('#factoryRefresh').addEventListener('click', () => {
  void Promise.all([
    refreshLiveResearch(),
    refreshFactory(true),
    refreshFinHackWorkspace(),
    refreshFactorCatalog(),
    refreshThirdPartyAudit(),
    refreshPipelineHealth(),
  ]);
});

function tickFactoryClock() {
  factory$('#factoryClock').textContent = new Date().toLocaleString('zh-CN', {hour12: false});
  updateLiveTiming();
}

async function refreshV44AndUpdate() {
  const v44Data = await refreshV44Shadow();
  if (v44Data) v44ShadowData = v44Data;
  if (liveResearchData) {
    liveResearchData._v44Shadow = v44ShadowData || {};
    renderStrategyProximity({
      ...liveResearchData,
      v44_shadow: liveResearchData._v44Shadow,
    });
  }
}

tickFactoryClock();
configureResearchWorkspace();
setInterval(tickFactoryClock, 1000);
void Promise.all([
  refreshLiveResearch().then(() => refreshFactory()),
  refreshV44AndUpdate(),
  refreshFinHackWorkspace(),
  refreshFactorCatalog(),
  refreshThirdPartyAudit(),
  refreshPipelineHealth(),
]);
setInterval(refreshLiveResearch, 15000);
setInterval(refreshV44AndUpdate, 30000);
setInterval(refreshFinHackWorkspace, 15000);
setInterval(refreshFactorCatalog, 120000);
setInterval(refreshThirdPartyAudit, 120000);
setInterval(refreshPipelineHealth, 60000);
setInterval(refreshV44AndUpdate, 30000);
