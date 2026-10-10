(function () {
  'use strict';
  if (document.body && document.body.dataset && document.body.dataset.decisionMode === 'research') return;
  const $ = id => document.getElementById(id);
  const endpoint = {
    health: '/api/health',
    route: '/api/strategy-route/research?view=dashboard',
    opportunities: '/api/opportunities',
    sectors: '/api/market/sectors',
    factors: '/api/factor-system/status?view=summary',
    emotion: '/api/market/emotion-profile',
    news: '/api/news/live',
    rotation: '/api/research/sector-rotation',
    pit: '/api/research/emotion-pit-status',
    origin: '/api/research/sector-mainline-origin',
    proxyForecast: '/api/research/sector-proxy-forecast',
    proxyForwardScore: '/api/research/sector-proxy-forward-score',
    firstWriteEval: '/api/research/sector-first-write-eval',
    peerContinuation: '/api/research/sector-peer-continuation',
    directionAudit: '/api/research/sector-direction-audit',
    archiveIntegrity: '/api/research/mainline-archive-integrity',
    liveProbe: '/api/research/local-sector-probe',
    localClose: '/api/research/local-sector-close',
    localForward: '/api/research/local-forward-chain',
    projectInputs: '/api/research/project-inputs',
    quotePilot: '/api/research/mainline-quote-pilot',
    listingInventory: '/api/research/official-listing-inventory',
    banditShadow: '/api/research/mainline-bandit-shadow',
    referenceMarket: '/api/market/panorama?dimension=today',
  };
  let loading = false;
  let latestRead = null;
  let latestModel = null;
  let latestEmotion = null;
  let latestPit = null;
  let latestOrigin = null;
  let latestProxyForecast = null;
  let latestProxyForwardScore = null;
  let latestFirstWriteEval = null;
  let latestPeerContinuation = null;
  let latestDirectionAudit = null;
  let latestArchiveAudit = null;
  let latestLiveProbe = null;
  let latestLocalClose = null;
  let latestLocalForward = null;
  let latestProjectInputs = null;
  let latestQuotePilot = null;
  let latestListingInventory = null;
  let latestBanditShadow = null;
  let rotationPacket = null;
  let rotationGeneration = 0;
  let rotationController = null;
  let referenceMarketGeneration = 0;
  let referenceMarketController = null;

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[character]);
  }
  function setText(id, value) { const node = $(id); if (node) node.textContent = String(value); }
  function clockTime(timeZone, date = new Date()) {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour12: false,
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).format(date);
  }
  function BeijingTime(date) { return clockTime('Asia/Shanghai', date); }
  function updateClock() {
    setText('beijingClock', '北京时间 ' + BeijingTime());
    const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone || '本机时区';
    setText('localClock', '本机时间 ' + clockTime(localZone) + ' · ' + localZone);
  }
  function formatSigned(value, suffix = '%') {
    return value === null ? '—' : (value > 0 ? '+' : '') + value.toFixed(2) + suffix;
  }
  function signClass(value) { return value > 0 ? 'positive' : value < 0 ? 'negative' : ''; }
  function countText(value) { return value === null ? '—' : String(value); }
  function renderList(id, values, empty) {
    const items = values.length ? values : [empty];
    $(id).innerHTML = items.map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  }
  function renderGate(model) {
    const status = model.status;
    const labels = [
      ['网站服务', status.serviceOnline, status.healthLoaded ? '响应结构待核' : '读取失败'],
      ['决策数据与监控', status.operational, status.healthLoaded ? '输入未齐' : '读取失败'],
      ['研究路由时点', status.routeCurrent, status.routeLoaded ? '过期或未知' : '读取失败'],
      ['严格路由入场', status.strictRouteReady, status.operational ? '未授权或输入不足' : '待核验'],
      ['人工复核', model.manualReviewReference, '暂无合格计划'],
    ];
    $('gateSteps').innerHTML = labels.map(([title, ready, failed]) =>
      '<div role="listitem"><span>' + escapeHtml(title) + '</span><b data-ready="' + ready + '">'
      + (ready ? '已核验' : escapeHtml(failed)) + '</b></div>').join('');
    const badge = $('gateStatus');
    badge.dataset.ready = String(model.manualReviewReference);
    badge.textContent = ({unknown: '无法判断', defend: '防守', observe: '研究观察', review: '仅人工复核'})[model.guidance.mode];
    setText('gateConclusion', '首要原因：' + model.guidance.reason + '。现在可做：' + model.guidance.nextAction);
  }
  function renderEmotion(profile) {
    const status = $('emotionStatus');
    status.dataset.status = profile.status;
    status.textContent = ({complete: '日线四轴可核验', partial: '日线状态、轨迹或时钟待核',
      stale: '历史日线 · 不可当期使用', unavailable: '日线原件不可用'})[profile.status] || '日线原件不可用';
    setText('emotionPhase', profile.phase);
    setText('emotionComposite', profile.composite === null ? '—' : profile.composite.toFixed(1) + ' / 100');
    $('emotionAxes').innerHTML = profile.axes.map(axis => {
      const value = axis.value === null ? '—' : axis.value.toFixed(1);
      const change = axis.change === null ? '环比待核'
        : '较前日 ' + (axis.change > 0 ? '+' : '') + axis.change.toFixed(1) + ' 点';
      const width = axis.value === null ? 0 : Math.max(0, Math.min(100, axis.value));
      return '<div class="emotion-axis" data-kind="' + escapeHtml(axis.kind) + '" role="listitem">'
        + '<div class="emotion-axis-top"><span>' + escapeHtml(axis.label) + '</span><strong>' + value + '</strong></div>'
        + '<div class="emotion-axis-track" aria-hidden="true"><i style="width:' + width + '%"></i></div>'
        + '<div class="emotion-axis-detail"><span>' + escapeHtml(change) + '</span><small>'
        + escapeHtml(axis.detail) + '</small></div><p>' + escapeHtml(axis.description) + '</p></div>';
    }).join('');
    const channelNames = ['广度', '接力', '失败', '拥挤'];
    $('emotionTimeline').innerHTML = profile.timeline.length
      ? '<div class="emotion-timeline-row emotion-timeline-head" aria-hidden="true"><span>日期</span>'
        + channelNames.map(name => '<span>' + name + '</span>').join('') + '</div>'
        + profile.timeline.map(point => '<div class="emotion-timeline-row" role="listitem">'
          + '<time datetime="' + escapeHtml(point.day) + '">' + escapeHtml(point.day.slice(5)) + '</time>'
          + point.values.map((value, index) => '<span class="emotion-timeline-cell" data-kind="'
            + (index < 2 ? 'participation' : 'stress') + '">'
            + (point.status === 'observed' ? '<b>' + value.toFixed(0) + '</b><i aria-hidden="true" style="width:'
              + value + '%"></i>' : '<b>缺</b>') + '</span>').join('') + '</div>').join('')
      : '<span class="emotion-sector-empty">五个官方交易日轨迹尚无可核原件；不能连线补缺日</span>';
    setText('emotionChangeSummary', profile.changeSummary);
    const stateName = value => ({UNKNOWN: '未知', RISK_OFF: '风险关闭',
      STABILIZING: '弱修复', RISK_ON: '情绪上升', OVERHEATED: '高潮拥挤'})[value] || '待核';
    $('emotionStateTrail').innerHTML = profile.stateTrail.length
      ? profile.stateTrail.map((point, index) => {
        const prior = profile.stateTrail[index - 1];
        const priorComparable = index === profile.stateTrail.length - 1
          ? profile.previousComparable : point.previousInputComplete;
        const comparable = point.status === 'observed' && prior?.status === 'observed'
          && priorComparable;
        const transition = comparable
          ? (point.rawState === prior.rawState ? '快态未变' : '快态 '
              + stateName(prior.rawState) + ' → ' + stateName(point.rawState))
            + '；' + (point.confirmedState === prior.confirmedState ? '慢态未变' : '慢态 '
              + stateName(prior.confirmedState) + ' → ' + stateName(point.confirmedState))
          : '';
        return '<div class="emotion-state-row" role="listitem" data-status="'
        + point.status + '"><time datetime="' + escapeHtml(point.day) + '">'
        + escapeHtml(point.day.slice(5)) + '</time><div><strong>'
        + (point.status === 'observed'
          ? escapeHtml((priorComparable ? '' : '单日规则标签：') + point.phaseLabel)
          : '原件缺失')
        + '</strong><span>' + (point.status === 'observed'
          ? '快 ' + escapeHtml(stateName(point.rawState)) + ' / 慢 '
            + escapeHtml(stateName(point.confirmedState))
            + (!priorComparable ? ' · 前日不可比' : '')
          : '此日不补值，后续迁移须重新起算') + '</span>'
          + (transition ? '<small>' + escapeHtml(transition) + '</small>' : '')
          + '</div></div>'; }).join('')
      : '<span class="emotion-sector-empty">状态轨迹与原始态、慢态或阶段不一致；不展示迁移</span>';
    $('emotionConflicts').innerHTML = profile.conflictNotes.map(note =>
      '<div class="emotion-conflict" data-kind="' + escapeHtml(note.kind) + '" role="listitem">'
        + '<strong>' + escapeHtml(note.finding) + '</strong><span>下一项复核：'
        + escapeHtml(note.check) + '</span></div>').join('');
    setText('emotionActivityDate', '日线 ' + profile.asOf);
    const activity = profile.activity;
    const activityRows = [
      ['观察范围成交额', activity.amountYi, '亿元', '有效活跃股票成交额之和；样本家数未发布'],
      ['换手率中位 / 95 分位', null, '', '非负有效换手样本；95 分位线性插值；家数未发布'],
      ['收盘涨停股成交占比', activity.limitAmountShare, '%', '收盘封板股成交额 / 有效总成交额'],
      ['个股收益横截面离散度', activity.returnDispersion, '%',
        '同日个股涨跌幅总体标准差（N 作分母）；有效 ' +
          (activity.returnSampleCount === null ? '样本家数待核' : activity.returnSampleCount + ' 家')],
    ];
    $('emotionActivity').innerHTML = activityRows.map(([label, value, unit, note], index) => {
      const formatted = index === 1
        ? (activity.turnoverMedian === null ? '—' : activity.turnoverMedian.toFixed(2) + '%')
          + ' / ' + (activity.turnoverP95 === null ? '—' : activity.turnoverP95.toFixed(2) + '%')
        : value === null ? '—' : value.toFixed(index === 0 ? 1 : 2) + ' ' + unit;
      return '<div class="emotion-activity-item" role="listitem"><small>' + escapeHtml(label)
        + '</small><strong>' + escapeHtml(formatted) + '</strong><span>' + escapeHtml(note) + '</span></div>';
    }).join('');
    setText('emotionPersistence', profile.runDays === null ? '连续性待核'
      : profile.previousComparable ? '连续观察 ' + profile.runDays + ' 日'
        : '单日观察 · 前日不可比');
    setText('emotionTrend', profile.trend);
    setText('emotionRegime', profile.fastBaseline + ' / ' + profile.slowBaseline);
    setText('emotionRegimeDetail', profile.baselineAgreement === null ? '双状态是否一致待核'
      : profile.baselineAgreement ? '快慢状态一致 · 仍需盘中确认' : '快慢状态分歧 · 留意转折/假突破');
    setText('emotionTrailing', '1 日 ' + formatSigned(profile.trailingOneDay)
      + ' / 5 日 ' + formatSigned(profile.trailingFiveDay));
    setText('emotionTrailingDetail', '历史股票中位数连乘 · 非未来概率或账户收益');
    const sector = profile.sectorDiffusion;
    setText('emotionSectorDiffusion', sector.summary);
    setText('emotionSectorTime', sector.status === 'observed_only'
      ? '延迟行业观察 · ' + sector.asOf : '行业原始计数或采集窗口待核');
    const distribution = sector.distribution;
    setText('emotionSectorDistribution', sector.status === 'observed_only' && distribution
      ? '行业扩散：上涨家数较多 ' + distribution.broadened + ' · 相等 '
        + distribution.balanced + ' · 下跌家数较多 ' + distribution.contracted
      : '行业扩散分布待核验；不能据此判断市场主线');
    $('emotionSectorLeaders').innerHTML = sector.rows.length
      ? '<span class="emotion-sector-label">行业内部上涨占比最高三项 · 延迟观察</span>'
        + sector.rows.map(row => '<div class="emotion-sector-item"><span>' + escapeHtml(row.name)
          + '</span><strong>' + (row.ratio * 100).toFixed(1) + '%</strong><small>上涨 '
          + row.up + ' / 下跌 ' + row.down + ' 家</small></div>').join('')
        + (sector.weakRows && sector.weakRows.length
          ? '<span class="emotion-sector-label emotion-sector-weak-label">行业内部上涨占比较低 '
            + sector.weakRows.length + ' 项 · 延迟观察</span>'
            + sector.weakRows.map(row => '<div class="emotion-sector-item emotion-sector-weak"><span>' + escapeHtml(row.name)
              + '</span><strong>' + (row.ratio * 100).toFixed(1) + '%</strong><small>上涨 '
              + row.up + ' / 下跌 ' + row.down + ' 家</small></div>').join('') : '')
      : '<span class="emotion-sector-empty">未形成可核的行业广度观察；不能据此确认主线</span>';
    const news = profile.newsAttention;
    setText('emotionNews', news.summary);
    setText('emotionNewsTime', news.status === 'observed_only'
      ? '最新发布时间 ' + news.asOf + ' · 未测文本方向' : '资讯数量不代表看多 · ' + news.asOf);
    setText('emotionDataTime', '日线 ' + profile.asOf + ' · 原件采集 ' + profile.capturedAt);
    setText('emotionScope', profile.scope);
    setText('emotionBoundary', profile.status === 'unavailable'
      ? '日线多轴缺少可核原件：' + profile.reason + '。不生成情绪方向或买入授权。'
      : profile.status === 'partial'
        ? '日线状态、轨迹或时钟未通过一致性核验；仅保留可核原始数值，撤销情绪迁移和环比解释。'
      : profile.status === 'stale'
        ? '这份日线距服务器日期已超过七天，仅供历史复盘；不能推断当下情绪或沿用买入计划。'
      : '四轴是收盘研究尺度，压力轴高值代表风险，不是胜率；行业与资讯是独立延迟观察。未来 1–5 日概率尚无真实前向校准结果。');
  }
  function renderLocalClose() {
    const element = $('proofLocalClose');
    if (!element) return; // A previously cached HTML shell can finish refreshing.
    const packet = latestLocalClose;
    const valid = packet?.schema === 'local_sector_close_status.v1'
      && packet.execution_location === 'local' && packet.research_only === true
      && ['cloud_backup_verified', 'external_time_attested', 'immutable_storage_attested',
        'full_market_verified', 'current_mainline_validated', 'leader_validated',
        'economic_target_A_validated', 'generates_alert', 'changes_trade_authority',
        'forecast_validated', 'sequential_rl_trained'].every(key => packet[key] === false)
      && Number.isInteger(packet.attempts) && packet.attempts >= 0 && packet.attempts <= 2
      && Number.isInteger(packet.accepted) && packet.accepted >= 0 && packet.accepted <= packet.attempts;
    const complete = valid && packet.status === 'local_complete' && packet.accepted > 0
      && Number.isInteger(packet.latest?.boards) && packet.latest.boards > 0;
    element.parentElement.dataset.status = 'gap';
    setText('proofLocalClose', !valid
      ? '本机盘后状态未核；不沿用旧原件，也不认定云端已同步。'
      : complete
        ? `${packet.reference_day}：本机通过 ${packet.latest.boards} 个供应商板块，源时点 ${packet.latest.source_as_of || '未核'}。尚未验证全 A 成员、主流、成交收益或未来预测；云备份未核。`
        : `${packet.summary || '本机盘后原件尚未通过核验。'}${packet.latest?.errors?.length ? ` 最近拒绝：${packet.latest.errors.join('、')}。` : ''}`);
  }
  function renderLocalForward() {
    const element = $('proofLocalForward');
    if (!element) return;
    const packet = latestLocalForward;
    const valid = packet?.schema === 'local_forward_status.v1'
      && packet.execution_location === 'local' && packet.research_only === true
      && packet.writes_performed === false
      && ['cloud_backup_verified', 'external_time_attested', 'immutable_storage_attested',
        'full_market_verified', 'membership_pit_verified', 'current_mainline_validated',
        'leader_validated', 'forecast_validated', 'returns_computed',
        'economic_target_A_validated', 'generates_alert', 'changes_trade_authority',
        'sequential_rl_trained'].every(key => packet[key] === false)
      && Number.isInteger(packet.attempts) && packet.attempts >= 0 && packet.attempts <= 12
      && Number.isInteger(packet.accepted) && packet.accepted >= 0
      && packet.accepted <= 6 && packet.accepted <= packet.attempts
      && Number.isInteger(packet.rejected) && packet.rejected >= 0
      && packet.rejected + packet.accepted === packet.attempts
      && Array.isArray(packet.labels) && packet.labels.length <= 3
      && typeof packet.summary === 'string'
      && typeof packet.capture_enabled === 'boolean'
      && (packet.capture_enabled
        ? packet.source_permission_status === 'operator_attested'
        : packet.source_permission_status === 'unverified');
    element.parentElement.dataset.status = 'gap';
    const dates = valid && packet.target_sessions
      ? [1, 3, 5].map(h => '+' + h + '日 ' + (packet.target_sessions[String(h)] || '未核')).join(' · ')
      : '';
    setText('proofLocalForward', valid
      ? packet.summary + (dates ? ' 目标交易日：' + dates + '。' : '')
      : '本机前向链状态未核；清除旧成功状态，保留真实起点、成熟标签及成本后收益缺口。');
  }
  function renderProjectInputs() {
    const element = $('proofProjectInputs');
    if (!element) return;
    const packet = latestProjectInputs;
    const valid = packet?.schema === 'project_input_recovery.v1'
      && packet.execution_location === 'local' && packet.writes_performed === false
      && ['closed_loop_ready', 'full_a_data_verified', 'strategy_admission_ready',
        'economic_target_A_validated', 'sequential_rl_ready'].every(key => packet[key] === false)
      && Array.isArray(packet.blockers) && typeof packet.summary === 'string';
    element.parentElement.dataset.status = 'gap';
    setText('proofProjectInputs', valid
      ? `${packet.summary} 核查时间 ${packet.observed_at_utc || '未核'}；行情源时点需另验。${packet.next_action || ''}`
      : '输入原件状态未核，保留数据缺口；当前不能计算可成交收益或发行新买点。');
  }
  function renderApplicability() {
    const api = window.RegisDecisionV2Model;
    const originStatus = api.mainlineOriginStatus(latestOrigin, latestEmotion, rotationPacket);
    const directionStatus = api.directionAuditStatus(latestDirectionAudit);
    const view = api.mainlineApplicabilityStatus({
      market: latestModel?.market, factors: latestModel?.factors,
      originStatus, directionStatus, archive: latestArchiveAudit,
    });
    setText('applicabilityHeadline', view.headline);
    $('mainlineApplicability').innerHTML = view.rows.map(row =>
      '<div class="applicability-row" data-gate="' + escapeHtml(row.id)
      + '" data-status="' + escapeHtml(row.status) + '" role="listitem"><div><strong>'
      + escapeHtml(row.title) + '</strong><span>'
      + ({observed: '状态已核', gap: '适用待验', retired: '已封存'})[row.status]
      + '</span></div><p>' + escapeHtml(row.detail) + '</p></div>').join('');
  }
  function renderProof() {
    const listing = latestListingInventory;
    const listingValid = listing?.schema === 'official_listing_inventory_status.v1'
      && listing.research_only === true && listing.production_eligible === false
      && listing.full_a_mainline_validated === false && listing.generates_alert === false
      && listing.three_exchange_listing_complete === false
      && listing.instrument_type_verified === false && listing.historical_pit_verified === false;
    const listingRows = listingValid && listing.sources && typeof listing.sources === 'object'
      ? ['sse_main', 'sse_star', 'szse_a', 'bse_first'].map(key => {
          const row = listing.sources[key];
          const label = ({sse_main: '沪主板', sse_star: '沪科创', szse_a: '深', bse_first: '京'})[key];
          if (row?.status === 'published_sse_filter_complete'
              && row.published_filter_complete === true && Number.isInteger(row.rows)
              && row.rows > 0 && row.rows <= 10000 && row.declared_total === row.rows) {
            return `${label}发布清单 ${row.rows} 行`;
          }
          return `${label}${row?.failure_class === 'http_307' ? ' HTTP 307 重定向'
            : row?.failure_class === 'connection' ? '连接失败' : '未核到完整原件'}`;
        }).join(' · ') : '';
    $('proofListing').parentElement.dataset.status = 'gap';
    setText('proofListing', !listingValid
      ? '交易所清单原件状态未核；不能据此认定沪深京全覆盖。'
      : listing.status === 'awaiting_official_listing'
        ? '尚无可复核的交易所清单原件；缺失交易所数量保持未知。'
        : listing.status === 'invalid_or_missing_listing_receipt'
          ? '交易所原件缺失或复核失败；不能沿用旧清单。'
          : `${listing.source_current === true ? '本机当日采集' : '历史采集'}：${listingRows || '未核到完整原件'}。工具类别、上市状态映射、深京清单及历史 PIT 待核；不是全 A 或可交易全集。`);
    const quote = latestQuotePilot;
    const quoteValid = quote?.schema === 'mainline_quote_pilot_status.v1'
      && quote.research_only === true && quote.production_eligible === false
      && quote.full_a_mainline_validated === false && quote.generates_alert === false;
    const exchange = quoteValid && quote.exchange_coverage && typeof quote.exchange_coverage === 'object'
      ? ['SSE', 'SZSE', 'BSE'].map(key => {
          const row = quote.exchange_coverage[key];
          return row && Number.isInteger(row.listed) && Number.isInteger(row.returned)
            ? `${({SSE: '沪', SZSE: '深', BSE: '京'})[key]} ${row.returned}/${row.listed}` : null;
        }).filter(Boolean).join(' · ') : '';
    $('proofQuote').parentElement.dataset.status = 'gap';
    setText('proofQuote', !quoteValid
      ? '报价验收状态未核；不能据此认定沪深京全覆盖。'
      : quote.status === 'awaiting_authorised_packet'
        ? '尚无获授权的沪深京逐股报价原件；本批 BaoStock 行业表实测京市为 0，不作替代。'
        : quote.status === 'historical_pilot_receipt'
          ? `仅有过期试点原件${exchange ? `（${exchange}）` : ''}；不能当本时点的全 A 报价。`
          : `供应商自报上市清单报价${quote.supplied_list_quote_complete === true ? '到齐' : '有缺口'}${exchange ? `（${exchange}）` : ''}；独立交易所清单、板块历史成员、真实分钟 K 线和许可仍未核，不生成主线或买点。`);
    const probe = latestLiveProbe;
    const liveValid = probe?.schema === 'local_sector_probe_status.v1'
      && probe.research_only === true && probe.generates_alert === false
      && probe.current_mainline_validated === false
      && probe.external_time_attested === false
      && probe.immutable_archive_attested === false;
    const liveReady = liveValid && probe.source_sequence_valid_now === true
      && probe.status === 'local_source_sequence_qualified'
      && Number.isInteger(probe.continuity_buckets) && probe.continuity_buckets >= 2;
    const requestFailures = liveValid && Array.isArray(probe.latest?.source_request_errors)
      ? probe.latest.source_request_errors.slice(0, 2).flatMap(entry => {
          if (!['industry', 'concept'].includes(entry?.kind)
              || !Number.isInteger(entry?.page) || !Array.isArray(entry?.attempts)) return [];
          const label = entry.kind === 'industry' ? '行业' : '概念';
          const codes = entry.attempts.slice(0, 2).filter(code =>
            typeof code === 'string' && (/^http_[45]\d\d$/.test(code)
              || ['http_error', 'timeout', 'connection', 'invalid_payload', 'request_failed'].includes(code)));
          if (!codes.length) return [];
          const kinds = [...new Set(codes)].map(code => code === 'http_502'
            ? 'HTTP 502（网关归属未核）'
            : /^http_[45]\d\d$/.test(code) ? `HTTP ${code.slice(5)}`
            : ({http_error: 'HTTP 状态未知', timeout: '请求超时', connection: '连接失败',
                invalid_payload: '响应格式异常', request_failed: '请求失败'})[code]);
          return [`${label}第 ${entry.page} 页：${kinds.join('、')}`];
        }).join('；') : '';
    $('proofLive').parentElement.dataset.status = liveReady ? 'observed' : 'gap';
    setText('proofLive', !liveValid
      ? '本机真实探针状态未核验；不把页面上的行情榜当连续盘中主线。'
      : liveReady
        ? `本机进程自报同源连续 ${probe.continuity_buckets} 桶；最近源时点 ${probe.latest?.source_as_of || '未提供'}。无外部时间与不可改存储证明，尚未验证未来主流或收益。`
        : `预登记 ${probe.target_day} 真实采集：尝试 ${probe.attempts}、通过 ${probe.accepted}、拒绝 ${probe.rejected}；最新 ${probe.latest?.errors?.join('、') || probe.status}${requestFailures ? `；来源请求：${requestFailures}` : ''}。尚无可用盘中主线验证。`);
    const archive = latestArchiveAudit;
    const retired = archive?.schema === 'mainline_retired_archive_integrity.v1'
      && archive.production_eligible === false && archive.historical_performance_recomputed === false
      && archive.route_binding === 'matched'
      && ['referenced_receipts_missing', 'referenced_receipts_invalid',
        'bound_receipts_present_not_recomputed'].includes(archive.status);
    const protocolDetail = retired
      ? window.RegisDecisionV2Model.protocolReproductionDetail(archive.protocol_reproduction) : '';
    setText('proofStrategy', retired
      ? `已冻结目标 A；旧状态战法代码和封存档案存在，结果原件未能独立复算。${protocolDetail}仍缺同起点 1/3/5 日、成交和费用后的净超额及无交易/朴素基线。`
      : '已冻结目标 A；旧战法原件状态未核。尚无可独立复算的可交易龙头净超额结果。');
    $('proofStrategy').parentElement.dataset.status = 'gap';
    const rl = latestBanditShadow;
    const rlValid = rl?.schema === 'mainline_bandit_shadow_status.v1'
      && rl.algorithm === 'offline_contextual_bandit_one_step'
      && rl.production_eligible === false && rl.generates_alert === false;
    setText('proofRL', !rlValid
      ? '影子学习状态未核；不会从情绪温度直接生成动作。'
      : rl.status === 'no_verified_real_log'
        ? '已接入研究级离线情境 bandit；真实行为概率、净奖励和独立留出日志为 0，模型未训练、无买卖提示。'
        : rl.status === 'unverified_log_research_only'
          ? `仅对自报日志完成算法计算（训练 ${rl.train_days ?? 0} 日、留出 ${rl.test_days ?? 0} 日）；执行、费用、基线和奖励原件未独立核对，不是有效 OPE 或盈利证明。`
          : `影子研究收据状态 ${rl.status}；没有可准入模型或买卖提示。`);
    $('proofRL').parentElement.dataset.status = 'gap';
  }
  function renderReferenceMarket(packet) {
    const view = window.RegisDecisionV2Model.referenceMarketObservation(packet);
    $('referenceMarketStatus').dataset.status = view.status;
    setText('referenceMarketStatus', view.label);
    setText('referenceMarketSummary', view.summary);
    setText('referenceMarketBreadth', view.counts
      ? view.counts.up + ' / ' + view.counts.down + ' / ' + view.counts.flat : '—');
    setText('referenceMarketLimits', view.limits && view.limits.up !== null && view.limits.down !== null
      ? view.limits.up + ' / ' + view.limits.down : '—');
    setText('referenceMarketCoverage', view.coverage
      ? view.coverage.returned + ' / ' + view.coverage.expected + ' · 全集未知' : '—');
    const structure = view.structure;
    $('referenceMarketStructure').innerHTML = structure
      ? '<div class="reference-structure-headrow" aria-hidden="true"><span>代码段</span><span>上涨 / 下跌 / 平盘</span><span>中位涨跌</span></div>'
        + structure.groups.map(row => '<div class="reference-structure-row" role="listitem"><span>'
          + escapeHtml(row.label) + '<small>' + row.count + ' 只</small></span><strong>'
          + row.up + ' / ' + row.down + ' / ' + row.flat + '</strong><b class="'
          + signClass(row.median) + '">' + escapeHtml(formatSigned(row.median)) + '</b></div>').join('')
      : '<p class="reference-structure-empty" role="listitem">' + escapeHtml(view.structureReason) + '</p>';
    setText('referenceMarketDistribution', structure
      ? '全参考池 ' + structure.distribution.count + ' 只：涨跌幅 P10 '
        + formatSigned(structure.distribution.p10) + ' / 中位 '
        + formatSigned(structure.distribution.median) + ' / P90 '
        + formatSigned(structure.distribution.p90) + '；成交额前 '
        + structure.distribution.topCount + ' 只占比 '
        + (structure.distribution.topAmountShare === null ? '未核' : structure.distribution.topAmountShare.toFixed(1) + '%')
        + '（有效成交额 ' + structure.distribution.amountCount + ' 只）。'
        : '结构指标未输出：' + view.structureReason);
    setText('referenceMarketSource', view.source);
  }
  function renderSectorAlignment() {
    const evidence = window.RegisDecisionV2Model
      .marketSectorEvidence(latestEmotion, rotationPacket, latestPit);
    setText('sectorEmotionLink', evidence.alignment.summary);
    const origin = window.RegisDecisionV2Model.mainlineOriginStatus(
      latestOrigin, latestEmotion, rotationPacket);
    setText('sectorOriginStatus', origin.summary);
    $('sectorOriginStatus').dataset.status = origin.status;
    renderApplicability();
    $('sectorEvidenceChain').innerHTML = evidence.steps.map(step =>
      '<div class="sector-evidence-step" data-status="' + escapeHtml(step.status)
      + '" data-step="' + escapeHtml(step.id) + '" role="listitem"><div><strong>'
      + escapeHtml(step.title) + '</strong><span>'
      + ({observed: '描述已核', gap: '证据缺口', research: '研究待验'})[step.status]
      + '</span></div><p>' + escapeHtml(step.detail) + '</p></div>').join('');
    const scenario = evidence.interpretation;
    const matrix = $('sectorScenarioMatrix');
    if (scenario.status !== 'observed_pair') {
      matrix.innerHTML = '<p class="sector-scenario-gap">' + escapeHtml(scenario.reason)
        + ' 不把缺失填成零，也不沿用旧板块。</p>';
      return;
    }
    const display = value => value === null ? '—' : escapeHtml(value);
    const displayPct = value => value === null ? '—' : escapeHtml(value) + '%';
    matrix.innerHTML = '<div class="sector-scenario-summary"><strong>'
      + escapeHtml(scenario.headline) + '</strong><span>' + escapeHtml(scenario.riskRule)
      + '</span></div><div class="sector-scenario-grid">'
      + scenario.sectors.map(row => '<article data-kind="' + row.kind + '"><h4>'
        + escapeHtml(row.label) + '</h4><div class="sector-scenario-stats">'
        + '<div><small>原件 / 可计算</small><b>' + row.reported + ' / ' + row.evaluable + '</b></div>'
        + '<div><small>覆盖 / 达标率</small><b>' + displayPct(row.coveragePct) + ' / '
        + displayPct(row.qualifiedPct) + '</b></div>'
        + '<div><small>达标 / 不可计算</small><b>' + row.qualified + ' / ' + row.unassessable + '</b></div>'
        + '<div><small>持续 / 新进 / 失资格</small><b>' + display(row.continued) + ' / '
        + display(row.newly) + ' / ' + display(row.lost) + '</b></div></div><p>'
        + escapeHtml(row.migration) + '</p><small>前日达标留存：'
        + (row.retentionPct === null ? '不可比' : escapeHtml(row.retentionPct) + '%')
        + ' · 身份/覆盖缺口：' + display(row.unmatched) + '</small></article>').join('')
      + '</div><div class="sector-scenario-checks"><h4>接下来要证伪什么</h4><ol>'
      + scenario.checks.map(check => '<li>' + escapeHtml(check) + '</li>').join('')
      + '</ol></div><p class="sector-scenario-source">市场 ' + escapeHtml(scenario.asOf)
      + ' · 来源 SHA ' + escapeHtml(scenario.marketSourceSha.slice(0, 12))
      + '… · 板块 ' + escapeHtml(scenario.boardSource) + ' / '
      + escapeHtml(scenario.boardPacketId) + '。行业与概念可能重叠；这不是当时发行的预测记录。</p>';
  }
  function renderFirstWriteEval() {
    const report = window.RegisDecisionV2Model.firstWriteEvaluationStatus(latestFirstWriteEval);
    setText('sectorFirstWriteEval', report.summary);
    $('sectorFirstWriteEval').dataset.status = report.status;
  }
  function renderProxyForecast() {
    const report = window.RegisDecisionV2Model.proxyForecastStatus(latestProxyForecast);
    setText('sectorProxyForecast', report.summary);
    $('sectorProxyForecast').dataset.status = report.status;
  }
  function renderProxyForwardScore() {
    const report = window.RegisDecisionV2Model.proxyForwardScoreStatus(latestProxyForwardScore);
    setText('sectorProxyForwardScore', report.summary);
    $('sectorProxyForwardScore').dataset.status = report.status;
    const grid = $('sectorProxyForwardGrid');
    grid.hidden = report.rows.length === 0;
    grid.innerHTML = report.rows.map(row => '<article class="sector-forward-cell">'
      + '<strong>' + escapeHtml(row.label) + '</strong><span>完整 ' + row.days + '/' + row.attempted + ' 起点 · '
      + row.boardRows + '/' + row.expectedRows + ' 板块行</span>'
      + (row.skips.length ? '<small>跳过：' + row.skips.map(item =>
        escapeHtml(item.label) + ' ' + item.count).join('、') + '</small>' : '')
      + (row.brier ? '<small>Brier：整体 ' + row.brier[0].toFixed(3)
        + ' / 资格 ' + row.brier[1].toFixed(3) + ' / 状态×资格 '
        + row.brier[2].toFixed(3) + '；相对资格 Δ '
        + (row.delta >= 0 ? '+' : '') + row.delta.toFixed(3) + '</small>'
        : '<small>成熟完整日不足；不显示分数</small>') + '</article>').join('');
  }
  function renderPeerContinuation() {
    const report = window.RegisDecisionV2Model.peerContinuationStatus(latestPeerContinuation);
    setText('sectorPeerContinuation', report.summary);
    $('sectorPeerContinuation').dataset.status = report.status;
  }
  function renderDirectionAudit() {
    const report = window.RegisDecisionV2Model.directionAuditStatus(latestDirectionAudit);
    setText('sectorDirectionAudit', report.summary);
    $('sectorDirectionAudit').dataset.status = report.status;
    renderApplicability();
  }
  function render(model, failures, fetchedAt, optionalPending) {
    latestModel = model;
    const verdict = model.verdict;
    $('verdictPanel').dataset.tone = verdict.tone;
    setText('verdictEyebrow', verdict.eyebrow);
    setText('verdictTitle', verdict.title);
    setText('verdictDetail', verdict.detail);
    setText('primaryBlocker', model.guidance.reason);
    setText('nextAction', model.guidance.nextAction);
    const guidanceLink = $('guidanceLink');
    guidanceLink.href = model.guidance.href;
    guidanceLink.textContent = model.guidance.linkLabel + ' ↗';
    setText('marketStateMetric', model.market.state);
    setText('marketAsOfMetric', model.market.asOf);
    setText('dataTimes', '市场原始来源：' + model.market.asOf + ' · 同槽核验：'
      + model.market.verifiedAt + ' · 路由生成：' + model.routeGeneratedAt
      + ' · 板块观察：' + model.sector.asOf);
    setText('sectorCountMetric', countText(model.sector.confirmed.length));
    setText('candidateCountMetric', countText(model.candidates.count));
    setText('candidatePoolNote', model.candidates.scannedCount === null
      ? '扫描池待核验 · 非交易信号'
      : '扫描池 ' + model.candidates.scannedCount + ' · 非交易信号');
    setText('factorCountMetric', countText(model.factors.deployableCount));
    setText('factorCatalogNote', model.factors.catalogKnown
      ? '只引用准入目录' : '摘要未含准入目录 · 待核验');
    setText('marketSource', model.market.source);
    setText('marketStateLabel', model.market.state);
    setText('marketNote', '原始来源：' + model.market.asOf + '；同槽核验：'
      + model.market.verifiedAt + '。状态置信度不是胜率；市场低迷时默认不新增风险。');
    document.querySelectorAll('.market-states [data-state]').forEach(node => {
      node.dataset.active = String(model.status.routeCurrent && node.dataset.state === model.market.code);
    });
    renderEmotion(model.emotion);
    latestEmotion = model.emotion;
    renderSectorAlignment();
    setText('sectorAsOf', model.sector.asOf);
    setText('confirmedSectorCount', String(model.sector.confirmed.length));
    $('confirmedSectors').innerHTML = model.sector.confirmed.length
      ? model.sector.confirmed.map(name => '<span class="sector-chip">' + escapeHtml(name) + '</span>').join('')
      : '当前研究路由没有可确认板块';
    $('sectorObservations').innerHTML = model.sector.observed.length
      ? model.sector.observed.map(row => '<div class="sector-row"><span>' + escapeHtml(row.name)
        + '</span><b class="' + signClass(row.changePct) + '">' + formatSigned(row.changePct)
        + '</b><time>' + escapeHtml(row.asOf) + '</time></div>').join('')
      : '暂无板块观察数据；不能据此推断板块确认';
    setText('candidateAsOf', model.candidates.asOf);
    $('candidateRows').innerHTML = model.candidates.rows.length
      ? model.candidates.rows.map(row => '<tr><td data-label="候选">' + escapeHtml(row.name) + '<small>'
        + escapeHtml(row.code) + '</small></td><td data-label="观察分数">' + (row.score === null ? '—' : row.score.toFixed(1))
        + '</td><td data-label="涨跌幅" class="' + signClass(row.changePct) + '">' + formatSigned(row.changePct)
        + '</td><td data-label="数据时间">' + escapeHtml(row.asOf) + '</td><td data-label="研究入口"><a href="' + escapeHtml(row.href)
        + '" aria-label="研究 ' + escapeHtml(row.name) + '，不是买入提示">查看研究 ↗</a></td></tr>').join('')
      : '<tr><td colspan="5" class="table-empty">当前无可展示的研究候选；未生成买入提示</td></tr>';
    renderGate(model);
    renderList('supportEvidence', model.evidence.support, '当前没有可展示的支持依据');
    renderList('againstEvidence', model.evidence.against, '当前没有可展示的反向证据；仍需人工核验');
    renderList('missingEvidence', model.evidence.missing, '接口未列出缺口；不代表所有数据已齐备');
    const errorText = failures.length ? ' · 读取异常：' + failures.join('、') : '';
    setText('dataSummary', '只读来源：健康、研究路由、候选、板块、因子目录、日线情绪、资讯、当日时点档案；独立盘后板块状态见卡片' + errorText);
    setText('lastRefresh', '最近读取：北京时间 ' + BeijingTime(fetchedAt)
      + (failures.length ? ' · 部分失败' : '')
      + (optionalPending ? ' · 情绪/资讯继续读取中' : ''));
  }
  function elapsedMs(started) {
    if (!started) return NaN;
    // The greater elapsed duration fails closed after a wall-clock jump or sleep.
    return Math.max(0, performance.now() - started.monotonic, Date.now() - started.wall);
  }
  function renderLatest() {
    if (!latestRead) return;
    const {data, reads, failures, fetchedAt, optionalPending} = latestRead;
    const healthTime = data.health?.time;
    const serverTime = typeof healthTime === 'string'
      && /(?:Z|[+-]\d{2}:\d{2})$/.test(healthTime.trim())
      ? Date.parse(healthTime) : NaN;
    const healthElapsed = elapsedMs(reads.health);
    const estimatedServerTime = serverTime + healthElapsed;
    const referenceTime = Number.isFinite(estimatedServerTime)
      && Math.abs(estimatedServerTime) <= 8.64e15
      ? new Date(estimatedServerTime).toISOString() : null;
    const routeElapsed = elapsedMs(reads.route);
    const model = window.RegisDecisionV2Model.buildModel({
      ...data, healthLoaded: Boolean(data.health), routeLoaded: Boolean(data.route),
      referenceTime, elapsedSinceRouteReadSeconds: routeElapsed / 1000,
      elapsedSinceSectorReadSeconds: elapsedMs(reads.sectors) / 1000,
      elapsedSinceNewsReadSeconds: elapsedMs(reads.news) / 1000,
    });
    latestPit = data.pit || null;
    render(model, failures, fetchedAt, optionalPending);
    if (loading) setText('lastRefresh', '正在核验主要决策输入…');
  }
  async function fetchJson(name, path, externalController = null, timeoutMs = 9000) {
    const started = {monotonic: performance.now(), wall: Date.now()};
    const controller = externalController || new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {
        credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'},
        signal: controller.signal,
      });
      if (!response.ok && !(name === 'health' && response.status === 503)
        && !(name === 'route' && response.status === 202)) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('invalid JSON object');
      return {data, started};
    } finally { clearTimeout(timer); }
  }
  async function refreshRotation() {
    const host = $('sectorRotationEvidence');
    const generation = ++rotationGeneration;
    if (rotationController) rotationController.abort();
    const controller = new AbortController();
    rotationController = controller;
    rotationPacket = null;
    host.innerHTML = '<p>正在重新核验盘后板块原件；旧观察已清除，市场判断可先使用。</p>';
    renderSectorAlignment();
    try {
      const packet = (await fetchJson('rotation', endpoint.rotation, controller)).data;
      if (generation !== rotationGeneration) return;
      rotationPacket = packet;
      host.innerHTML = '<p class="mainline-source">本次页面核查：'
        + escapeHtml(BeijingTime(new Date())) + '（上海时间）；来源时点见下方。</p>'
        + (window.RegisMainlineEvidence?.rotation(rotationPacket)
          || '<p>板块复盘组件暂不可用；旧观察已清除。</p>');
    } catch (_) {
      if (generation !== rotationGeneration) return;
      rotationPacket = null;
      host.innerHTML = '<p>本次盘后板块原件读取失败；旧观察已清除，不据此推断主线。</p>';
    } finally {
      if (generation === rotationGeneration) {
        rotationController = null;
        renderSectorAlignment();
      }
    }
  }
  async function refreshReferenceMarket() {
    const generation = ++referenceMarketGeneration;
    if (referenceMarketController) referenceMarketController.abort();
    const controller = new AbortController();
    referenceMarketController = controller;
    renderReferenceMarket(null);
    try {
      const packet = (await fetchJson('referenceMarket', endpoint.referenceMarket, controller, 25000)).data;
      if (generation === referenceMarketGeneration) renderReferenceMarket(packet);
    } catch (_) {
      if (generation === referenceMarketGeneration) renderReferenceMarket(null);
    } finally {
      if (generation === referenceMarketGeneration) referenceMarketController = null;
    }
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    // Drop the previous generation before its periodic render can revive an old archive badge.
    latestRead = null;
    latestModel = null;
    latestEmotion = null;
    latestPit = null;
    latestOrigin = null;
    latestProxyForecast = null;
    renderProxyForecast();
    latestProxyForwardScore = null;
    renderProxyForwardScore();
    latestFirstWriteEval = null;
    renderFirstWriteEval();
    latestPeerContinuation = null;
    renderPeerContinuation();
    latestArchiveAudit = null;
    latestLiveProbe = null;
    latestLocalClose = null;
    renderLocalClose();
    latestQuotePilot = null;
    latestListingInventory = null;
    latestBanditShadow = null;
    latestDirectionAudit = null;
    renderDirectionAudit();
    renderApplicability();
    renderProof();
    void refreshRotation();
    void refreshReferenceMarket();
    $('refreshButton').disabled = true;
    setText('lastRefresh', '正在核验主要决策输入…');
    const names = ['health', 'route', 'opportunities', 'sectors', 'factors'];
    const optionalNames = ['emotion', 'news'];
    const optional = Promise.allSettled(optionalNames.map(name => fetchJson(name, endpoint[name])));
    const pitRead = fetchJson('pit', endpoint.pit);
    const originRead = fetchJson('origin', endpoint.origin);
    // Attach rejection handling before required reads settle; a fast optional
    // 503 must not surface as an unhandled rejection or revive an old card.
    const proxyForecastRead = fetchJson('proxyForecast', endpoint.proxyForecast)
      .then(result => result.data, () => null);
    const proxyForwardScoreRead = fetchJson('proxyForwardScore', endpoint.proxyForwardScore)
      .then(result => result.data, () => null);
    const firstWriteEvalRead = fetchJson('firstWriteEval', endpoint.firstWriteEval);
    // These retain their detailed failure handlers below. Mark rejection handled
    // now, before slower required reads can leave a fast 503 unhandled.
    for (const read of [pitRead, originRead, firstWriteEvalRead]) void read.catch(() => {});
    // Settle the optional read immediately; a fast HTTP error must not surface
    // as an unhandled rejection while the required dashboard reads are pending.
    const peerContinuationRead = fetchJson('peerContinuation', endpoint.peerContinuation)
      .then(result => result.data, () => null);
    const directionAuditRead = fetchJson('directionAudit', endpoint.directionAudit)
      .then(result => result.data, () => null);
    const archiveIntegrityRead = fetchJson('archiveIntegrity', endpoint.archiveIntegrity)
      .then(result => result.data, () => null);
    const liveProbeRead = fetchJson('liveProbe', endpoint.liveProbe)
      .then(result => result.data, () => null);
    const localCloseRead = fetchJson('localClose', endpoint.localClose)
      .then(result => result.data, () => null);
    const localForwardRead = fetchJson('localForward', endpoint.localForward)
      .then(result => result.data, () => null);
    const projectInputsRead = fetchJson('projectInputs', endpoint.projectInputs)
      .then(result => result.data, () => null);
    const quotePilotRead = fetchJson('quotePilot', endpoint.quotePilot)
      .then(result => result.data, () => null);
    const listingInventoryRead = fetchJson('listingInventory', endpoint.listingInventory)
      .then(result => result.data, () => null);
    const banditShadowRead = fetchJson('banditShadow', endpoint.banditShadow)
      .then(result => result.data, () => null);
    const results = await Promise.allSettled(names.map(name => fetchJson(name, endpoint[name])));
    const data = {}, reads = {}, failures = [];
    function collect(batch, batchNames) {
      batch.forEach((result, index) => {
        const name = batchNames[index];
        if (result.status === 'fulfilled') {
          data[name] = result.value.data;
          reads[name] = result.value.started;
        }
        else failures.push(name + '（' + (result.reason?.name === 'AbortError' ? '超时' : '不可用') + '）');
      });
    }
    collect(results, names);
    const currentRead = {data, reads, failures, fetchedAt: new Date(), optionalPending: true};
    latestRead = currentRead;
    renderLatest();
    void pitRead.then(result => {
      if (latestRead !== currentRead) return;
      currentRead.data.pit = result.data;
      currentRead.reads.pit = result.started;
      renderLatest();
    }).catch(error => {
      if (latestRead !== currentRead) return;
      currentRead.failures.push('pit（' + (error?.name === 'AbortError' ? '超时' : '不可用') + '）');
      renderLatest();
    });
    void originRead.then(result => {
      if (latestRead !== currentRead) return;
      latestOrigin = result.data;
      renderSectorAlignment();
    }).catch(() => {
      if (latestRead !== currentRead) return;
      latestOrigin = null;
      renderSectorAlignment();
    });
    void proxyForecastRead.then(data => {
      if (latestRead !== currentRead) return;
      latestProxyForecast = data;
      renderProxyForecast();
    }).catch(() => {
      if (latestRead !== currentRead) return;
      latestProxyForecast = null;
      renderProxyForecast();
    });
    void proxyForwardScoreRead.then(data => {
      if (latestRead !== currentRead) return;
      latestProxyForwardScore = data;
      renderProxyForwardScore();
    });
    void firstWriteEvalRead.then(result => {
      if (latestRead !== currentRead) return;
      latestFirstWriteEval = result.data;
      renderFirstWriteEval();
    }).catch(() => {
      if (latestRead !== currentRead) return;
      latestFirstWriteEval = null;
      renderFirstWriteEval();
    });
    void peerContinuationRead.then(data => {
      if (latestRead !== currentRead) return;
      latestPeerContinuation = data;
      renderPeerContinuation();
    });
    void directionAuditRead.then(data => {
      if (latestRead !== currentRead) return;
      latestDirectionAudit = data;
      renderDirectionAudit();
    });
    void archiveIntegrityRead.then(data => {
      if (latestRead !== currentRead) return;
      latestArchiveAudit = data;
      renderApplicability();
      renderProof();
    });
    void liveProbeRead.then(data => {
      if (latestRead !== currentRead) return;
      latestLiveProbe = data;
      renderProof();
    });
    void localCloseRead.then(data => {
      if (latestRead !== currentRead) return;
      latestLocalClose = data;
      renderLocalClose();
    });
    void projectInputsRead.then(data => {
      if (latestRead !== currentRead) return;
      latestProjectInputs = data;
      renderProjectInputs();
    });
    void localForwardRead.then(data => {
      if (latestRead !== currentRead) return;
      latestLocalForward = data;
      renderLocalForward();
    });
    void quotePilotRead.then(data => {
      if (latestRead !== currentRead) return;
      latestQuotePilot = data;
      renderProof();
    });
    void listingInventoryRead.then(data => {
      if (latestRead !== currentRead) return;
      latestListingInventory = data;
      renderProof();
    });
    void banditShadowRead.then(data => {
      if (latestRead !== currentRead) return;
      latestBanditShadow = data;
      renderProof();
    });
    $('refreshButton').disabled = false;
    loading = false;
    renderLatest();
    void optional.then(batch => {
      if (latestRead !== currentRead) return;
      collect(batch, optionalNames);
      currentRead.optionalPending = false;
      renderLatest();
    });
  }
  updateClock();
  setInterval(updateClock, 1000);
  $('refreshButton').addEventListener('click', refresh);
  setInterval(() => { if (!document.hidden) renderLatest(); }, 1000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { renderLatest(); refresh(); }
  });
  setInterval(() => { if (!document.hidden) refresh(); }, 60000);
  refresh();
})();
