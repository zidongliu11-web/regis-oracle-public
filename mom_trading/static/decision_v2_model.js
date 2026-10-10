/* Read-only presentation model for the staged decision workspace.
 * This module never grants trade authority. The existing backend remains the
 * only source of manual-review plans and the old decision page stays available.
 */
(function (root, factory) {
  const model = factory();
  if (typeof module === 'object' && module.exports) module.exports = model;
  root.RegisDecisionV2Model = model;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const STATE_LABELS = {
    RISK_OFF: '风险收缩', STABILIZING: '企稳观察',
    RISK_ON: '风险偏好扩张', OVERHEATED: '过热警惕',
    risk_closed: '风险收缩', weak_repair: '弱修复观察',
    sentiment_rising: '情绪扩张', crowded_climax: '高位拥挤警惕',
    retreat: '退潮防守', TRANSITION_TO_RISK: '退潮防守',
  };
  const STATE_CODES = {
    risk_closed: 'RISK_OFF', weak_repair: 'STABILIZING',
    sentiment_rising: 'RISK_ON', crowded_climax: 'OVERHEATED',
    retreat: 'RISK_OFF', TRANSITION_TO_RISK: 'RISK_OFF',
  };
  const EMOTION_AXES = [
    {id: 'B_strength', label: '市场广度', kind: 'participation', description: '上涨与站上均线、新高/新低的参与面'},
    {id: 'L_strength', label: '接力持续', kind: 'participation', description: '涨停梯队与昨日强势延续'},
    {id: 'F_stress', label: '失败压力', kind: 'stress', description: '触板未封住与接力失败；高值代表压力'},
    {id: 'C_stress', label: '成交拥挤', kind: 'stress', description: '成交集中度；高值代表拥挤'},
  ];
  const EMOTION_STATE_LABELS = {UNKNOWN: '尚未确认', RISK_OFF: '风险关闭',
    STABILIZING: '弱修复', RISK_ON: '情绪上升', OVERHEATED: '高潮拥挤'};
  const EMOTION_PHASE_LABELS = {unknown: '情绪细分输入不足', extreme_stress: '冰点／极端压力',
    retreat: '强势退潮', weak_decline: '弱势低迷', early_repair: '低迷后初次修复',
    repair_fading: '修复减弱', weak_repair: '弱修复延续', upturn_pending: '上升待确认',
    rising: '上升延续', strong_divergence: '强势分歧', divergence_repair: '分歧后转强',
    overheat_pending: '急修复伴随拥挤，待确认', crowded_climax: '高潮拥挤'};

  function record(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function text(value, fallback = '待核验') {
    const result = typeof value === 'string' ? value.trim() : '';
    return result || fallback;
  }

  function count(value) {
    return Number.isInteger(value) && value >= 0 ? value : null;
  }

  function finiteNumber(value) {
    return value !== null && value !== undefined && value !== ''
      && Number.isFinite(Number(value)) ? Number(value) : null;
  }

  function timestamp(value) {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value.trim())) return null;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function sourceTime(value) {
    const raw = text(value, '');
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
      const parsed = new Date(raw + 'T00:00:00Z');
      return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === raw ? raw : '';
    }
    return timestamp(raw) !== null ? raw : '';
  }

  function stateCode(value) {
    const raw = text(value, 'unknown');
    return STATE_CODES[raw] || raw;
  }

  function marketProvenance(route, workspace) {
    const state = stateCode(workspace.market_state || route.market_state);
    const invalid = {bound: false, kind: 'unbound', asOf: '', verifiedAt: '',
      source: '市场状态与来源未绑定'};
    if (workspace.strict_market_budget_valid === true) {
      const review = record(workspace.completed_market_review);
      const decisionAt = text(workspace.strict_forward_decision_at, '');
      const asOf = sourceTime(review.market_state_as_of);
      if (review.valid !== true || !workspace.strict_forward_packet_id
        || review.packet_id !== workspace.strict_forward_packet_id
        || review.decision_at !== decisionAt || stateCode(review.market_state) !== state
        || timestamp(decisionAt) === null || timestamp(asOf) === null
        || timestamp(asOf) > timestamp(decisionAt)) return invalid;
      return {bound: true, kind: 'strict', asOf, verifiedAt: decisionAt,
        source: '严格同槽包 · 原始来源与包时点分列'};
    }
    const core = record(workspace.current_slot_research_review);
    if (core.valid === true) {
      const decisionAt = text(core.decision_at, '');
      const asOf = sourceTime(core.market_state_as_of);
      if (stateCode(core.market_state) !== state || timestamp(decisionAt) === null
        || timestamp(asOf) === null || timestamp(asOf) > timestamp(decisionAt)) return invalid;
      return {bound: true, kind: 'research', asOf, verifiedAt: decisionAt,
        source: '当日同槽研究包 · 非正式准入'};
    }
    const asOf = sourceTime(route.market_state_as_of);
    if (!asOf || stateCode(route.market_state) !== state) return invalid;
    return {bound: true, kind: 'daily', asOf, verifiedAt: '',
      source: '日线/收盘研究基线 · 非盘中核验'};
  }

  function itemText(value) {
    if (typeof value === 'string') return value.trim();
    if (!value || typeof value !== 'object') return '';
    return text(value.summary || value.reason || value.detail || value.label, '');
  }

  function list(value, limit = 4) {
    return (Array.isArray(value) ? value : [])
      .map(itemText).filter(Boolean).slice(0, limit);
  }

  function sectorName(value) {
    if (typeof value === 'string') return value.trim();
    return text(record(value).name || record(value).sector_name, '');
  }

  function validCode(value) {
    return /^\d{6}$/.test(String(value || ''));
  }

  function candidateRows(opportunities) {
    const raw = record(opportunities).items;
    return (Array.isArray(raw) ? raw : [])
      .filter(row => row && typeof row === 'object' && validCode(row.code))
      .slice(0, 5)
      .map(row => ({
        code: String(row.code),
        name: text(row.name, String(row.code)),
        score: finiteNumber(row.score),
        changePct: finiteNumber(row.change_pct),
        asOf: text(record(row.opportunity_quality).data_as_of || row.source_data_as_of, '时间待核验'),
        href: '/stock?code=' + encodeURIComponent(String(row.code)),
        role: '研究候选，不是买入提示',
      }));
  }

  function percent(value) {
    const number = finiteNumber(value);
    return number !== null && number >= 0 && number <= 100 ? number : null;
  }

  function emotionProfile(input) {
    const profile = record(input.emotion);
    const observation = record(profile.emotion);
    const source = record(profile.source);
    const sourceDay = sourceTime(source.data_as_of);
    const capturedAt = timestamp(source.captured_at);
    const referenceAt = timestamp(input.referenceTime) ?? timestamp(record(input.health).time);
    const referenceDay = referenceAt === null ? null
      : new Date(referenceAt + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
    const ageDays = referenceDay && /^\d{4}-\d{2}-\d{2}$/.test(sourceDay)
      ? Math.round((Date.parse(referenceDay + 'T00:00:00Z')
        - Date.parse(sourceDay + 'T00:00:00Z')) / 86400000) : null;
    const valid = profile.schema === 'market_emotion_profile.v1'
      && profile.status === 'computed' && profile.research_only === true
      && profile.production_eligible === false && profile.participates_in_decision === false
      && observation.schema === 'detailed_emotion_candidate.v1'
      && observation.status === 'computed' && observation.research_only === true
      && observation.production_eligible === false
      && /^\d{4}-\d{2}-\d{2}$/.test(sourceDay)
      && sourceDay === observation.data_as_of && capturedAt !== null
      && (referenceAt === null || capturedAt <= referenceAt)
      && (ageDays === null || ageDays >= 0)
      && /^[0-9a-f]{64}$/.test(String(source.snapshot_sha256 || ''));
    const reportedAxes = valid && Array.isArray(observation.axes) ? observation.axes : [];
    const metricRows = valid && Array.isArray(observation.raw_metrics) ? observation.raw_metrics : [];
    const raw = id => {
      const item = metricRows.find(row => record(row).id === id);
      return item ? finiteNumber(item.value) : null;
    };
    const detail = (id) => {
      if (id === 'B_strength') {
        const up = raw('up_count'), down = raw('down_count'), ma = percent(raw('above_ma20_ratio_pct'));
        return up !== null && down !== null && ma !== null
          ? `上涨 ${up} / 下跌 ${down} 家 · 站上 MA20 ${ma.toFixed(1)}%` : '原始广度计数待核';
      }
      if (id === 'L_strength') {
        const first = raw('first_board_count'), second = raw('second_board_count');
        return first !== null && second !== null ? `首板 ${first} 家 · 二板 ${second} 家` : '涨停梯队原始计数待核';
      }
      if (id === 'F_stress') {
        const failed = raw('eod_failed_limit_count'), ratio = percent(raw('eod_failed_limit_ratio_pct'));
        return failed !== null && ratio !== null
          ? `触板未封 ${failed} 家 · 失败占比 ${ratio.toFixed(1)}%` : '失败压力原始计数待核';
      }
      const share = percent(raw('top_5pct_amount_share_pct'));
      return share !== null ? `成交额前 5% 股票占比 ${share.toFixed(1)}%` : '成交集中度原始计数待核';
    };
    const axes = EMOTION_AXES.map(spec => {
      const row = reportedAxes.find(item => record(item).id === spec.id);
      return {...spec, value: valid ? percent(record(row).value) : null,
        change: valid ? finiteNumber(record(row).change_points) : null,
        previousValue: valid ? percent(record(row).previous_value) : null,
        detail: valid ? detail(spec.id) : '日线原件未核验'};
    });
    const compositeRow = reportedAxes.find(item => record(item).id === 'directional_strength');
    const composite = valid ? percent(record(compositeRow).value) : null;
    const trend = record(observation.breadth_trend);
    const runDays = count(observation.consecutive_observed_sessions);
    const priceDirection = valid && Array.isArray(observation.price_direction)
      ? observation.price_direction : [];
    const trailingMove = sessions => {
      const row = priceDirection.find(item => record(item).sessions === sessions);
      const value = finiteNumber(record(row).median_move_chain_pct);
      return value !== null && value > -100 ? value : null;
    };
    const rawActivity = (id, upper = null) => {
      const value = raw(id);
      return value !== null && value >= 0 && (upper === null || value <= upper) ? value : null;
    };
    const activity = {
      amountYi: valid ? rawActivity('total_amount_yi') : null,
      turnoverMedian: valid ? rawActivity('turnover_median_pct') : null,
      turnoverP95: valid ? rawActivity('turnover_p95_pct') : null,
      limitAmountShare: valid ? rawActivity('limit_stock_amount_share_pct', 100) : null,
      returnDispersion: valid ? rawActivity('cross_section_return_dispersion_pct') : null,
    };
    const populationCounts = ['up_count', 'down_count', 'flat_count'].map(id => count(raw(id)));
    activity.returnSampleCount = valid && populationCounts.every(value => value !== null)
      ? populationCounts.reduce((sum, value) => sum + value, 0) : null;
    const timelineInput = valid && Array.isArray(observation.recent_official_sessions)
      ? observation.recent_official_sessions : [];
    const timeline = timelineInput.slice(-5).map(point => {
      const item = record(point), channels = record(item.axes);
      const day = sourceTime(item.trade_date);
      const values = EMOTION_AXES.map(spec => percent(channels[spec.id]));
      return {day, status: item.status === 'observed' && values.every(value => value !== null)
        ? 'observed' : 'missing', values,
        rawState: item.raw_state, confirmedState: item.confirmed_state,
        phase: item.phase, phaseLabel: item.phase_label,
        previousInputComplete: item.previous_input_complete === true};
    });
    const trajectoryValid = timelineInput.length > 0 && timelineInput.length <= 5
      && timeline.every((point, index) => /^\d{4}-\d{2}-\d{2}$/.test(point.day)
        && point.day <= sourceDay && (index === 0 || point.day > timeline[index - 1].day))
      && timeline[timeline.length - 1].day === sourceDay
      && timeline[timeline.length - 1].status === 'observed'
      && axes.every((axis, index) => axis.value !== null
        && Math.abs(timeline[timeline.length - 1].values[index] - axis.value) < 0.02);
    const previousSession = sourceTime(observation.previous_official_session);
    const previousPoint = trajectoryValid ? timeline[timeline.length - 2] : null;
    const previousCoherent = observation.previous_input_complete === true
      && previousPoint && previousPoint.day === previousSession
      && previousPoint.status === 'observed'
      && axes.every((axis, index) => axis.previousValue !== null && axis.change !== null
        && Math.abs(previousPoint.values[index] - axis.previousValue) < 0.02
        && Math.abs(axis.value - axis.previousValue - axis.change) < 0.02);
    // An isolated change_points field is not enough: the official preceding
    // session and the four-axis trajectory must agree before any delta is shown.
    const stateTrailValid = trajectoryValid && timeline.every((point, index) => {
      if (point.status === 'missing') return point.rawState == null && point.confirmedState == null
        && point.phase == null && point.phaseLabel == null;
      return Object.prototype.hasOwnProperty.call(EMOTION_STATE_LABELS, point.rawState)
        && Object.prototype.hasOwnProperty.call(EMOTION_STATE_LABELS, point.confirmedState)
        && EMOTION_PHASE_LABELS[point.phase] === point.phaseLabel
        && (index === 0 || point.previousInputComplete === (timeline[index - 1].status === 'observed'));
    }) && timeline[timeline.length - 1].rawState === observation.raw_baseline
      && timeline[timeline.length - 1].confirmedState === observation.confirmed_baseline
      && timeline[timeline.length - 1].phase === observation.phase
      && timeline[timeline.length - 1].phaseLabel === observation.label
      && timeline[timeline.length - 1].previousInputComplete === observation.previous_input_complete
      && observation.baseline_agreement
        === (observation.raw_baseline === observation.confirmed_baseline);
    const interpretationValid = valid && stateTrailValid;
    const comparisonCoherent = interpretationValid && previousCoherent;
    const safeAxes = axes.map(axis => ({...axis,
      change: comparisonCoherent ? axis.change : null,
      previousValue: comparisonCoherent ? axis.previousValue : null}));
    const changes = safeAxes.map(axis => axis.change);
    const changeSummary = !comparisonCoherent || changes.some(value => value === null)
      ? '逐日状态或前一完整交易日四轴对照不足，不能判断同步变化'
      : EMOTION_AXES.map((spec, index) => `${spec.label} ${changes[index] > 0 ? '+' : ''}${changes[index].toFixed(1)}`).join(' · ')
        + (changes[0] > 0 && changes[1] < 0 ? '；广度与接力背离' : '')
        + (changes[0] > 0 && changes[2] > 0 ? '；广度改善同时失败压力增加' : '');
    const conflictNotes = [];
    if (!valid) {
      conflictNotes.push({kind: 'missing', finding: '日线原件未核验，情绪状态未知',
        check: '先核对来源身份、采集时点和官方交易日。'});
    } else if (!stateTrailValid) {
      conflictNotes.push({kind: 'missing', finding: '逐日状态与当日阶段或四轴原件不一致',
        check: '核对当日原始态、慢速确认态、阶段和官方日轨迹；撤销环比解释。'});
    } else {
      if (observation.baseline_agreement === false) {
        conflictNotes.push({kind: 'split', finding: '当日原始态与慢速确认态不一致',
          check: '查看后续官方交易日是否延续；盘中判断须另核同槽原件。'});
      }
      if (!comparisonCoherent) {
        conflictNotes.push({kind: 'missing', finding: '前一官方交易日无法完整对照',
          check: '补齐并核验前日四轴，暂不判断情绪是改善还是恶化。'});
      } else {
        if (changes[0] > 0 && changes[1] < 0) {
          conflictNotes.push({kind: 'split', finding: '市场参与面扩大，但涨停接力强度下降',
            check: '对照首板、二板和昨日强势延续，勿把普涨叫作接力修复。'});
        }
        if (changes[0] > 0 && changes[2] > 0) {
          conflictNotes.push({kind: 'stress', finding: '广度改善同时触板失败压力增加',
            check: '核对触板未封和收盘封板原始计数，辨别上涨中的失败风险。'});
        }
        if (changes[0] > 0 && changes[3] > 0) {
          conflictNotes.push({kind: 'stress', finding: '广度改善同时成交更集中',
            check: '核对成交额前 5% 股票占比及板块扩散；集中不等于可成交主线。'});
        }
        if (!conflictNotes.length) {
          conflictNotes.push({kind: 'neutral', finding: '本批预设冲突未触发',
            check: '只表示这些当日代理未出现指定背离；不代表市场安全或未来上涨。'});
        }
      }
    }
    return {
      status: valid ? (!stateTrailValid ? 'partial' : ageDays !== null && ageDays > 7 ? 'stale'
        : axes.every(axis => axis.value !== null) && referenceAt !== null ? 'complete' : 'partial')
        : 'unavailable',
      phase: interpretationValid
        ? (comparisonCoherent ? '' : '单日规则标签：')
          + text(observation.label, '日线阶段待核')
          + (comparisonCoherent ? '' : ' · 前日不可比')
        : '日线状态原件待核验',
      phaseCode: interpretationValid ? text(observation.phase, 'unknown') : 'unknown',
      asOf: valid ? sourceDay : '日期待核验',
      capturedAt: valid ? source.captured_at : '源时间待核验',
      sourceSha: valid ? source.snapshot_sha256 : null,
      confirmedState: interpretationValid ? observation.confirmed_baseline : null,
      scope: valid ? text(source.scope, '观察范围待核验') : '观察范围待核验',
      axes: safeAxes, composite,
      activity, timeline: trajectoryValid ? timeline : [], changeSummary,
      stateTrail: stateTrailValid ? timeline : [], conflictNotes,
      trend: interpretationValid ? text(trend.label, '五日广度变化待核') : '五日广度变化待核',
      runDays: comparisonCoherent ? runDays : interpretationValid ? 1 : null,
      previousComparable: comparisonCoherent,
      fastBaseline: interpretationValid ? EMOTION_STATE_LABELS[observation.raw_baseline] : '快态待核',
      slowBaseline: interpretationValid ? EMOTION_STATE_LABELS[observation.confirmed_baseline] : '慢态待核',
      baselineAgreement: interpretationValid && typeof observation.baseline_agreement === 'boolean'
        ? observation.baseline_agreement : null,
      trailingOneDay: valid ? trailingMove(1) : null,
      trailingFiveDay: valid ? trailingMove(5) : null,
      ageDays: valid ? ageDays : null,
      reason: valid ? '' : text(profile.reason, '日线源身份、时间或输入不完整'),
    };
  }

  function sectorDiffusion(sectors, elapsed) {
    const source = record(sectors);
    const meta = record(source.meta);
    const rows = Array.isArray(source.industries) ? source.industries : [];
    const earliest = timestamp(meta.earliest_data_as_of);
    const latest = timestamp(meta.latest_data_as_of);
    const maxAge = finiteNumber(source.refresh_policy_seconds);
    const current = source.status === 'ok' && source.health_status === 'verified'
      && source.participates_in_decision === false && meta.coverage_complete === true
      && meta.industry_complete === true && meta.timestamp_complete === true
      && meta.breadth_count_complete === true && count(meta.industry_reported_total) === rows.length
      && rows.length > 0 && earliest !== null && latest !== null
      && latest >= earliest && latest - earliest <= 120000
      && source.data_as_of === meta.earliest_data_as_of
      && maxAge !== null && maxAge > 0 && Number.isFinite(elapsed)
      && elapsed >= 0 && elapsed < maxAge;
    if (!current) return {status: 'unavailable', summary: '板块广度覆盖、时点或延迟源待核验',
      asOf: '时间待核', rows: []};
    const measured = rows.map(row => {
      const up = count(record(row).up_count), down = count(record(row).down_count);
      return {name: text(record(row).name, ''), up, down,
        ratio: up !== null && down !== null && up + down > 0 ? up / (up + down) : null,
        asOf: sourceTime(record(row).data_as_of)};
    });
    if (measured.some(row => !row.name || row.ratio === null || timestamp(row.asOf) === null
      || timestamp(row.asOf) < earliest || timestamp(row.asOf) > latest)) {
      return {status: 'unavailable', summary: '板块原始计数或时点不完整', asOf: '时间待核', rows: []};
    }
    if (new Set(measured.map(row => row.name)).size !== measured.length) {
      return {status: 'unavailable', summary: '行业名称重复，覆盖待核', asOf: '时间待核', rows: []};
    }
    const broadened = measured.filter(row => row.up > row.down).length;
    const balanced = measured.filter(row => row.up === row.down).length;
    const contracted = measured.length - broadened - balanced;
    const sorted = measured.sort((a, b) => b.ratio - a.ratio || a.name.localeCompare(b.name));
    const leaders = sorted.slice(0, 3);
    const leaderNames = new Set(leaders.map(row => row.name));
    return {status: 'observed_only', summary: `${broadened} / ${measured.length} 个行业上涨家数多于下跌家数`,
      asOf: `${meta.earliest_data_as_of} — ${meta.latest_data_as_of}`,
      distribution: {broadened, balanced, contracted},
      rows: leaders, weakRows: sorted.slice(-3).reverse().filter(row => !leaderNames.has(row.name))};
  }

  function newsAttention(news, elapsed) {
    const source = record(news);
    const meta = record(source.meta);
    const items = Array.isArray(source.items) ? source.items : [];
    const maxAge = finiteNumber(source.refresh_policy_seconds);
    const ready = source.status === 'ok' && source.health_status === 'verified'
      && source.participates_in_decision === false
      && meta.timestamp_complete === true && count(meta.future_timestamp_count) === 0
      && count(meta.returned_count) === items.length && timestamp(source.data_as_of) !== null
      && maxAge !== null && maxAge > 0 && Number.isFinite(elapsed)
      && elapsed >= 0 && elapsed < maxAge;
    if (!ready) return {status: 'unavailable', summary: '资讯来源或发布时间待核验', asOf: '时间待核'};
    const candidates = count(meta.material_a_share_candidates);
    return {status: 'observed_only',
      summary: `去重资讯 ${items.length} 条 · A 股相关候选 ${candidates === null ? '待核' : candidates} 条`,
      asOf: source.data_as_of};
  }

  function completeManualBuy(row, referenceTime) {
    const item = record(row);
    const plan = record(item.manual_plan);
    const shares = finiteNumber(plan.shares);
    const limit = finiteNumber(plan.limit_price);
    const cost = finiteNumber(plan.estimated_cost);
    const expiry = timestamp(plan.valid_until);
    const identitySource = plan.evidence_identity;
    const identity = plan.evidence_id || record(identitySource).id
      || record(identitySource).packet_id || record(identitySource).method
      || (typeof identitySource === 'string' ? identitySource.trim() : '');
    return validCode(item.code) && plan.status === 'ready_for_manual_review'
      && plan.side === 'BUY' && Number.isInteger(shares) && shares > 0
      && limit !== null && limit > 0 && cost !== null && cost >= 0
      && referenceTime !== null && expiry !== null && expiry > referenceTime
      && Boolean(plan.invalidation_condition)
      && Boolean(plan.playbook_id) && Boolean(plan.strategy_id)
      && Boolean(plan.strategy_version) && Boolean(identity)
      && Boolean(plan.evidence_grade) && Boolean(plan.as_of)
      && plan.human_confirmation_required === true
      && plan.broker_connected === false && plan.automatic_trading === false;
  }

  function buildModel(payload) {
    const input = record(payload);
    const health = record(input.health);
    const readiness = record(health.readiness);
    const route = record(input.route);
    const routeSnapshot = record(route.research_snapshot);
    const workspace = record(route.today_decision_workspace);
    const opportunities = record(input.opportunities);
    const sectors = record(input.sectors);
    const news = record(input.news);
    const factors = record(input.factors);
    const catalog = record(factors.production_catalog);
    const healthKnown = Boolean(input.healthLoaded);
    const routeKnown = Boolean(input.routeLoaded);
    const routeAge = routeSnapshot.age_seconds;
    const routeMaxAge = routeSnapshot.maximum_age_seconds;
    const elapsedSinceRouteRead = input.elapsedSinceRouteReadSeconds === undefined
      ? 0 : input.elapsedSinceRouteReadSeconds;
    const routeCurrent = routeKnown && routeSnapshot.current === true
      && typeof routeAge === 'number' && Number.isFinite(routeAge) && routeAge >= 0
      && typeof routeMaxAge === 'number' && Number.isFinite(routeMaxAge) && routeMaxAge > 0
      && typeof elapsedSinceRouteRead === 'number'
      && Number.isFinite(elapsedSinceRouteRead) && elapsedSinceRouteRead >= 0
      && routeAge + elapsedSinceRouteRead < routeMaxAge;
    // A structured health reply proves the website responds even when its
    // quote/data readiness is false. Trading still uses the stricter gate.
    const serviceOnline = healthKnown && typeof readiness.ready === 'boolean';
    const operational = healthKnown && health.status === 'healthy'
      && readiness.operational_ready === true;
    const sessionOpen = workspace.session_status === 'open';
    const sessionKnown = Boolean(workspace.session_status && workspace.session_status !== 'unknown');
    const rawMarketState = text(workspace.market_state || route.market_state, 'unknown');
    const marketCode = stateCode(rawMarketState);
    const riskOff = marketCode === 'RISK_OFF';
    const provenance = marketProvenance(route, workspace);
    const presentMarketEvidence = operational && routeCurrent && provenance.bound;
    const sameSlotMarketCurrent = presentMarketEvidence && provenance.kind !== 'daily';
    // The browser supplies a server-time estimate advanced by elapsed time.
    // An explicit invalid value must not fall back to an old route timestamp.
    const referenceTime = timestamp(Object.prototype.hasOwnProperty.call(input, 'referenceTime')
      ? input.referenceTime : health.time);
    // The aggregate health product refers to a separate factor-signal path.
    // Independent playbooks are admitted by the verified strict route itself.
    const strictRouteReady = sameSlotMarketCurrent && provenance.kind === 'strict'
      && workspace.strict_market_budget_valid === true
      && workspace.execution_route_authorized === true
      && workspace.strict_forward_packet_valid === true;
    const strictReview = routeCurrent && sessionOpen && workspace.actionable_now === true
      && workspace.execution_route_authorized === true
      && workspace.strict_forward_packet_valid === true
      && workspace.strict_market_budget_valid === true
      && workspace.sector_entry_gate_ready === true
      && workspace.effective_market_budget_valid === true
      && count(workspace.manual_review_ready_count) > 0
      && Array.isArray(workspace.manual_review_items)
      && workspace.manual_review_items.some(row => completeManualBuy(row, referenceTime));
    // This page links to the existing, stricter decision UI. It never exposes
    // a buy/order action of its own, even when all read-only flags are true.
    const manualReviewReference = strictRouteReady && strictReview && !riskOff
      && provenance.kind === 'strict';

    let verdict;
    let guidance;
    if (!healthKnown || !routeKnown) {
      verdict = {tone: 'blocked', eyebrow: '输入缺失', title: '先核验数据，再谈交易',
        detail: '服务或研究路由未返回完整状态；本页不生成买卖计划。'};
      guidance = {mode: 'unknown', reason: !healthKnown ? '服务状态读取失败' : '研究路由读取失败',
        nextAction: '刷新数据；若仍失败，在原决策页核对来源与时间。', href: '/?workspace=intraday', linkLabel: '核对原决策页'};
    } else if (!operational) {
      verdict = {tone: 'blocked', eyebrow: serviceOnline ? '服务在线 · 数据待接入' : '服务待核验',
        title: serviceOnline ? '先补齐数据，基础功能仍可使用' : '先核验数据，再谈交易',
        detail: serviceOnline
          ? '网站服务在线；行情、研究输入或监控证据尚未齐。可查询资料、编辑草稿及录入真实账户，当前不沿用买入计划。'
          : '健康状态未确认服务与行情可用；即使保留旧计划，也不能在本页沿用。'};
      guidance = {mode: 'unknown', reason: serviceOnline ? '决策数据或监控证据未齐' : '服务与行情未达可用',
        nextAction: '到数据中心检查来源并恢复缺失输入；期间可复核已有持仓风险。', href: '/data-center', linkLabel: '打开数据中心'};
    } else if (!routeCurrent) {
      verdict = {tone: 'blocked', eyebrow: '快照待更新', title: '今日不新开仓',
        detail: '研究路由快照过期或年龄不可核。历史候选只能用于复盘，不能沿用为盘中提示。'};
      guidance = {mode: 'unknown', reason: '研究路由快照过期或年龄不可核',
        nextAction: '等待新快照，并在原页核对生成时点；旧候选只用于复盘。', href: '/?workspace=intraday', linkLabel: '核对研究路由'};
    } else if (!provenance.bound) {
      verdict = {tone: 'blocked', eyebrow: '市场时点待核验', title: '先核验行情来源时间',
        detail: '市场状态与对应原件的来源、版本或时间未能绑定；不能沿用买入计划。'};
      guidance = {mode: 'unknown', reason: '市场状态来源或时点无法核验',
        nextAction: '在原页核对市场状态原件及时间；未核前只复核已有持仓。', href: '/?workspace=intraday', linkLabel: '核对市场来源'};
    } else if (provenance.kind === 'daily' && (sessionOpen || !sessionKnown)) {
      verdict = {tone: 'blocked', eyebrow: '仅有收盘基线', title: '等待当日同槽核验',
        detail: '日线基线可用于研究，不能把新生成的路由当成盘中市场确认或人工买入计划。'};
      guidance = {mode: 'unknown', reason: '当前只有日线/收盘市场基线',
        nextAction: '等待市场与板块在当日同槽核验；期间仅检查既有持仓风险。', href: '/market-panorama', linkLabel: '核对市场中心'};
    } else if (riskOff) {
      verdict = {tone: 'defensive', eyebrow: '风险收缩', title: '低迷市场 · 不新增风险',
        detail: '优先检查现有持仓和现金；研究候选不覆盖防守规则。'};
      guidance = {mode: 'defend', reason: '当前有效研究路由触发风险收缩',
        nextAction: '查看现有持仓、现金和退出限制；不要把防守判断当成未来必跌预测。', href: '/risk', linkLabel: '查看持仓风险'};
    } else if (!sessionKnown) {
      verdict = {tone: 'blocked', eyebrow: '交易时段待核验', title: '先核验时段，再谈交易',
        detail: '交易时段状态未知，不能把盘中计划视为此刻有效。'};
      guidance = {mode: 'unknown', reason: '交易时段状态未返回',
        nextAction: '刷新并在原页核对交易时钟和计划有效期。', href: '/?workspace=intraday', linkLabel: '核对原决策页'};
    } else if (!sessionOpen) {
      verdict = {tone: 'neutral', eyebrow: '非交易时段', title: '准备下一交易日',
        detail: '盘前与盘后内容只供计划和复盘，不能当作此刻可执行提示。'};
      guidance = {mode: 'observe', reason: '当前不在已核交易时段',
        nextAction: '复盘或准备预案；下次交易时段须重新核对快照与计划有效期。', href: '/review', linkLabel: '查看盘后复盘'};
    } else if (referenceTime === null) {
      verdict = {tone: 'blocked', eyebrow: '服务器时间待核验', title: '先核验服务器时间',
        detail: '无法以带时区的服务器时间检查人工计划有效期；本页不沿用人工复核状态。'};
      guidance = {mode: 'unknown', reason: '服务器时间缺失或格式无效',
        nextAction: '刷新健康状态并在原页核对计划有效期；未核前不新增风险。',
        href: '/?workspace=intraday', linkLabel: '核对原决策页'};
    } else if (manualReviewReference) {
      verdict = {tone: 'review', eyebrow: '人工复核', title: '存在待人工复核的计划',
        detail: '请进入原有「今日决策」页核对完整计划、失效条件、账户限制和当时行情；本页不下单。'};
      guidance = {mode: 'review', reason: '已有完整计划进入人工复核门禁',
        nextAction: '到原页重核价格、失效时间、账户容量与反证；是否下单由你决定。', href: '/?workspace=intraday', linkLabel: '打开完整计划'};
    } else {
      verdict = {tone: 'defensive', eyebrow: '门禁未全通过', title: '今日不新开仓',
        detail: '服务在线不等于策略可用；只有正式数据、策略和账户门禁全部通过才可能出现人工复核计划。'};
      guidance = strictRouteReady
        ? {mode: 'observe', reason: '同槽数据、账户或人工计划门禁未全通过',
          nextAction: '到原页逐项核对缺口；研究候选不能代替完整计划。', href: '/?workspace=intraday', linkLabel: '查看门禁详情'}
        : {mode: 'observe', reason: text(workspace.execution_route_reason, '严格路由入场尚未核准').slice(0, 180),
          nextAction: '到原页核对当时市场、板块、战法和账户的严格路由缺口；候选仅供研究。', href: '/?workspace=intraday', linkLabel: '查看严格路由'};
    }

    const routeSectorNames = (Array.isArray(workspace.confirmed_sectors)
      ? workspace.confirmed_sectors : []).map(sectorName).filter(Boolean).slice(0, 4);
    const observedSectorRows = (Array.isArray(sectors.industries) ? sectors.industries : [])
      .filter(row => row && typeof row === 'object' && text(row.name, ''))
      .slice(0, 5).map(row => ({
        name: text(row.name),
        changePct: finiteNumber(row.change_pct),
        asOf: text(row.data_as_of || sectors.data_as_of, '时间待核验'),
      }));
    const factorCount = count(catalog.deployable_count);
    // candidate_count is the universe scanned before assessment, not the
    // number of research rows that the compact endpoint actually exposes.
    const rawCandidateRows = Array.isArray(opportunities.items) ? opportunities.items : [];
    const displayableCandidateCount = rawCandidateRows.filter(
      row => row && typeof row === 'object' && validCode(row.code),
    ).length;
    const scannedCandidateCount = count(opportunities.candidate_count);
    const evidence = list(workspace.evidence || route.evidence, 5);
    const contradictions = list(workspace.contradictions || route.contradictions, 5);
    const missing = list(route.missing_inputs, 5);
    return {
      verdict, guidance, manualReviewReference,
      market: {
        state: presentMarketEvidence
          ? (provenance.kind === 'daily' ? '收盘基线 · ' : '')
            + text(STATE_LABELS[rawMarketState], '状态待核验')
          : routeCurrent && !operational ? (serviceOnline ? '决策数据待核验 · 不用于盘中' : '服务待核验 · 不用于盘中')
          : routeCurrent ? '市场源时点待核验'
          : routeKnown ? '历史快照 · 不用于盘中' : '状态待核验',
        code: sameSlotMarketCurrent ? marketCode : 'UNKNOWN',
        asOf: text(provenance.asOf, '市场源时点待核验'),
        verifiedAt: text(provenance.verifiedAt, '未有同槽核验'),
        source: presentMarketEvidence ? provenance.source : '市场或路由待核验',
        provenanceKind: provenance.kind,
        confidence: '状态置信度不是胜率',
      },
      emotion: {
        ...emotionProfile(input),
        sectorDiffusion: sectorDiffusion(sectors, input.elapsedSinceSectorReadSeconds === undefined
          ? 0 : input.elapsedSinceSectorReadSeconds),
        newsAttention: newsAttention(news, input.elapsedSinceNewsReadSeconds === undefined
          ? 0 : input.elapsedSinceNewsReadSeconds),
      },
      routeGeneratedAt: text(record(workspace.phase_clock).snapshot_server_time
        || routeSnapshot.published_at, '路由生成时点待核验'),
      sector: {
        confirmed: sameSlotMarketCurrent ? routeSectorNames : [],
        observed: observedSectorRows,
        observationOnly: sectors.participates_in_decision !== true,
        status: text(sectors.status, 'unavailable'),
        asOf: text(sectors.data_as_of, '时间待核验'),
      },
      candidates: {
        rows: candidateRows(opportunities),
        count: displayableCandidateCount,
        scannedCount: scannedCandidateCount,
        asOf: text(opportunities.date, '日期待核验'),
        status: text(opportunities.signal_status && opportunities.signal_status.status,
          text(opportunities.error, '研究观察')),
      },
      factors: {deployableCount: factorCount,
        catalogKnown: factorCount !== null,
        consumerReady: factors.production_consumer_ready === true || catalog.consumer_ready === true},
      evidence: {support: evidence, against: contradictions, missing},
      status: {
        operational, serviceOnline,
        strictRouteReady,
        routeCurrent, sessionOpen,
        healthLoaded: healthKnown, routeLoaded: routeKnown,
      },
    };
  }

  function marketSectorAlignment(emotion, rotation) {
    if (!emotion || emotion.status !== 'complete') return {
      status: 'market_unavailable',
      summary: '市场日线原件不完整或已过期；不能与板块轨迹合并解读。',
    };
    if (!rotation || rotation.schema !== 'sector_rotation_review.v1'
      || rotation.research_only !== true || rotation.historical_only !== true
      || rotation.forecast_validated !== false || rotation.leader_validated !== false
      || rotation.generates_alert !== false || rotation.changes_trade_authority !== false
      || !['observed', 'no_matching_observation'].includes(rotation.status)) return {
      status: 'sector_unavailable',
      summary: '盘后板块原件缺失或边界未核验；不能由市场温度推断主线。',
    };
    if (emotion.asOf !== rotation.reference_day) return {
      status: 'different_days',
      summary: '市场日线与盘后板块参考日不同；禁止把两张卡解释为同日共振。',
    };
    return {
      status: 'date_only',
      summary: '交易日期一致，但市场日线覆盖、板块来源及历史成员尚未证明同一母体；只能并列观察，不能据此认定状态适用策略或买点。',
    };
  }

  function validatedPopulation(value) {
    const audit = record(value);
    if (audit.scope !== 'this_verified_supplier_packet_only'
      || count(audit.excluded_behavior_cohorts) === null) return null;
    const byKind = record(audit.by_kind);
    const result = {};
    for (const kind of ['industry', 'concept']) {
      const row = record(byKind[kind]);
      const fields = ['reported', 'evaluable', 'qualified', 'displayed',
        'unassessable', 'not_qualified'];
      if (fields.some(field => count(row[field]) === null)
        || row.reported !== row.evaluable + row.unassessable
        || row.evaluable !== row.qualified + row.not_qualified
        || row.displayed > row.qualified) return null;
      result[kind] = Object.fromEntries(fields.map(field => [field, row[field]]));
    }
    return result;
  }

  function sectorPopulation(rotation) {
    const sourceAt = timestamp(rotation.source_as_of);
    const observedAt = timestamp(rotation.observed_at);
    const closeAt = timestamp(rotation.reference_day + 'T15:00:00+08:00');
    const nextOpen = timestamp(text(rotation.target_day, '') + 'T09:30:00+08:00');
    const sessions = Array.isArray(rotation.sessions) ? rotation.sessions : [];
    const current = record(sessions[sessions.length - 1]);
    if (!text(rotation.source, '') || !text(rotation.board_packet_id, '')
      || sourceAt === null || observedAt === null || closeAt === null || nextOpen === null
      || rotation.target_day <= rotation.reference_day
      || sourceAt < closeAt || sourceAt > observedAt || observedAt >= nextOpen
      || new Date(sourceAt + 8 * 3600000).toISOString().slice(0, 10) !== rotation.reference_day
      || current.status !== 'verified' || current.day !== rotation.reference_day
      || current.board_packet_id !== rotation.board_packet_id
      || current.source !== rotation.source
      || current.source_as_of !== rotation.source_as_of
      || current.observed_at !== rotation.observed_at) return null;
    const audit = record(rotation.population_audit);
    const result = validatedPopulation(audit);
    if (!result) return null;
    const sessionAudit = record(current.population);
    if (sessionAudit.scope !== audit.scope
      || sessionAudit.excluded_behavior_cohorts !== audit.excluded_behavior_cohorts
      || ['industry', 'concept'].some(kind => Object.keys(result[kind]).some(field =>
        record(record(sessionAudit.by_kind)[kind])[field] !== result[kind][field]))) return null;
    const shown = Array.isArray(rotation.rows) ? rotation.rows : null;
    if (!shown || ['industry', 'concept'].some(kind =>
      shown.filter(row => record(row).kind === kind).length !== result[kind].displayed)
      || shown.some(row => !['industry', 'concept'].includes(record(row).kind))) return null;
    return result;
  }

  function sectorTransition(rotation, population) {
    if (!rotation || !population) return null;
    const audit = record(rotation.transition_audit);
    if (audit.status !== 'comparable') return null;
    const sessions = Array.isArray(rotation.sessions) ? rotation.sessions : [];
    const prior = record(sessions[sessions.length - 2]);
    const priorSourceAt = timestamp(prior.source_as_of);
    const priorObservedAt = timestamp(prior.observed_at);
    const priorClose = timestamp(prior.day + 'T15:00:00+08:00');
    const currentOpen = timestamp(rotation.reference_day + 'T09:30:00+08:00');
    const priorPopulation = validatedPopulation(prior.population);
    if (prior.status !== 'verified' || !/^\d{4}-\d{2}-\d{2}$/.test(prior.day)
      || prior.day >= rotation.reference_day
      || !text(prior.board_packet_id, '') || prior.board_packet_id === rotation.board_packet_id
      || prior.source !== rotation.source || priorSourceAt === null || !priorPopulation
      || priorObservedAt === null || priorClose === null || currentOpen === null
      || priorSourceAt < priorClose || priorSourceAt > priorObservedAt
      || priorObservedAt >= currentOpen
      || new Date(priorSourceAt + 8 * 3600000).toISOString().slice(0, 10) !== prior.day
      || audit.prior_packet_id !== prior.board_packet_id
      || audit.current_packet_id !== rotation.board_packet_id) return null;
    const byKind = record(audit.by_kind);
    const result = {};
    for (const kind of ['industry', 'concept']) {
      const row = record(byKind[kind]);
      const fields = ['prior_qualified', 'current_qualified', 'continued_qualified',
        'lost_qualification', 'newly_qualified', 'prior_identity_unmatched',
        'current_identity_unmatched'];
      if (fields.some(field => count(row[field]) === null)
        || row.prior_qualified !== row.continued_qualified + row.lost_qualification
          + row.prior_identity_unmatched
        || row.current_qualified !== row.continued_qualified + row.newly_qualified
          + row.current_identity_unmatched
        || row.current_qualified !== population[kind].qualified
        || row.prior_qualified !== priorPopulation[kind].qualified) return null;
      result[kind] = Object.fromEntries(fields.map(field => [field, row[field]]));
    }
    return result;
  }

  function pitArchiveDetail(emotion, pit) {
    const packet = record(pit);
    const boundary = packet.schema === 'market_emotion_pit_status.v1'
      && packet.research_only === true && packet.production_eligible === false
      && packet.forecast_validated === false && packet.participates_in_decision === false
      && packet.continuous_history_verified === false && packet.generates_alert === false
      && packet.returns_computed === false && packet.automatic_trading === false;
    const remaining = '连续历史、未来 1 / 3 / 5 日同起点评分、实际成本与前向验证仍未完成；无预测概率或策略准入。';
    if (!boundary) return '时点档案状态未取得或边界无效；' + remaining;
    const day = sourceTime(packet.data_as_of);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return '官方目标交易日未核；' + remaining;
    if (packet.status === 'waiting_calendar') return '官方目标交易日与来源日历未核；' + remaining;
    if (packet.status === 'waiting_receipt') return `${day} 的首次时点档案尚缺；` + remaining;
    if (packet.status === 'archive_invalid') return `${day} 的时点档案或原件校验失败；` + remaining;
    if (packet.status === 'source_mismatch') return `${day} 的档案与当前日历或股票范围不一致；` + remaining;
    if (packet.status === 'source_revision_conflict') return `${day} 的当前来源已修订，首次档案未替换；` + remaining;
    if (packet.status !== 'captured') return '时点档案状态未知；' + remaining;
    const capturedAt = timestamp(packet.captured_at);
    const marketCapturedAt = timestamp(emotion?.capturedAt);
    const captureDay = capturedAt === null ? ''
      : new Date(capturedAt + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
    const captureHour = capturedAt === null ? -1
      : new Date(capturedAt + 8 * 60 * 60 * 1000).getUTCHours();
    const sameSource = emotion?.status === 'complete' && day === emotion.asOf
      && /^[0-9a-f]{64}$/.test(String(emotion.sourceSha || ''))
      && packet.source_snapshot_sha256 === emotion.sourceSha
      && packet.state === emotion.confirmedState && marketCapturedAt !== null
      && capturedAt !== null && capturedAt >= marketCapturedAt
      && captureDay === day && captureHour >= 15;
    return sameSource
      ? `${day} 当日首次时点档已核，来源 SHA 与市场卡一致；${remaining}`
      : `${day} 有首次时点档，但与当前市场卡的日期、来源 SHA、状态或采集时点未能同源对账；${remaining}`;
  }

  function marketSectorInterpretation(emotion, rotation, alignment, population, transition) {
    const unavailable = reason => ({status: 'gap', headline: '盘后条件解读待核验',
      reason, sectors: [], checks: [], researchOnly: true, tradeAuthority: false});
    if (alignment.status !== 'date_only') return unavailable(alignment.summary);
    if (!population) return unavailable('市场四轴或板块完整横截面未核验；无法形成同日盘后条件解读。');
    const state = emotion?.confirmedState;
    if (!['RISK_OFF', 'STABILIZING', 'RISK_ON', 'OVERHEATED'].includes(state)
      || !/^[0-9a-f]{64}$/.test(String(emotion?.sourceSha || ''))) {
      return unavailable('市场慢态或来源哈希未核验；保留四轴原始观察，不组合出板块情景。');
    }
    const total = population.industry.qualified + population.concept.qualified;
    const headline = ({
      RISK_OFF: total ? '市场风险收缩，仍有局部达标板块' : '市场风险收缩，本次没有达标板块',
      STABILIZING: '市场弱修复，核对板块新进入和持续性',
      RISK_ON: '市场风险扩张，核对板块是否真正扩散',
      OVERHEATED: '市场高潮拥挤，核对板块退潮和失败压力',
    })[state];
    const sectors = [['industry', '行业'], ['concept', '供应商概念']].map(([kind, label]) => {
      const current = population[kind];
      const movement = transition?.[kind] || null;
      const coveragePct = current.reported
        ? Math.round(current.evaluable / current.reported * 1000) / 10 : null;
      const qualifiedPct = current.evaluable
        ? Math.round(current.qualified / current.evaluable * 1000) / 10 : null;
      const prior = movement?.prior_qualified;
      const retentionPct = prior && movement.prior_identity_unmatched === 0
        && movement.current_identity_unmatched === 0
        ? Math.round(movement.continued_qualified / prior * 1000) / 10 : null;
      const migration = !movement ? '相邻交易日原件不可比，轮动未知'
        : movement.prior_identity_unmatched + movement.current_identity_unmatched > 0
          ? '部分身份或覆盖不连续，不能把全部变化归因于轮动'
          : movement.newly_qualified > movement.lost_qualification
            ? '新达标多于失资格；仅是筛选规则的迁移'
            : movement.lost_qualification > movement.newly_qualified
              ? '失资格多于新达标；核对次日是否继续'
              : '新达标与失资格数量相同或均为零';
      return {kind, label, ...current, coveragePct, qualifiedPct,
        continued: movement?.continued_qualified ?? null,
        newly: movement?.newly_qualified ?? null,
        lost: movement?.lost_qualification ?? null,
        unmatched: movement
          ? movement.prior_identity_unmatched + movement.current_identity_unmatched : null,
        retentionPct, migration};
    });
    return {status: 'observed_pair', headline, state,
      asOf: emotion.asOf, marketSourceSha: emotion.sourceSha,
      boardPacketId: rotation.board_packet_id, boardSource: rotation.source,
      sectors, researchOnly: true, tradeAuthority: false,
      riskRule: state === 'RISK_OFF'
        ? '风险收缩默认不新增风险；局部板块达标不覆盖账户与战法门禁。'
        : '只做盘后并列观察；实际仓位与买卖仍由同槽风险、策略和人工决定。',
      checks: [
        '下一交易日：重新核同源完整板块横截面、市场四轴与历史成员，不沿用今日名单。',
        '未来 1 / 3 / 5 日：分别在事前冻结起点评分延续、反转与不交易基线；当前没有有效预测概率。',
        '个股/战法/因子：需要当时成员、成交可达、成本和独立准入；成交额前三不等于龙头。',
      ]};
  }

  function mainlineOriginStatus(packet, emotion, rotation) {
    const origin = record(packet);
    const boundary = origin.schema === 'sector_mainline_origin_status.v1'
      && origin.research_only === true && origin.forecast_validated === false
      && origin.production_eligible === false && origin.generates_alert === false
      && origin.returns_computed === false && origin.leader_validated === false
      && origin.changes_trade_authority === false;
    if (!boundary) return {status: 'gap', summary: '盘后研究起点状态未核，不能把当前页面当作已发行预测。'};
    if (origin.status !== 'captured') return {status: 'gap',
      summary: `盘后研究起点：${({waiting_calendar:'官方日历待核',outside_after_close_window:'当前不在盘后发行窗口',calendar_revision_conflict:'日历修订与首写档冲突',origin_not_saved:'当日尚无首写档',origin_not_saved_or_invalid:'首写档缺失或损坏'})[origin.status] || '输入或首写档尚不可用'}；没有当时发行的板块概率。`};
    const issued = timestamp(origin.issued_at);
    const close = timestamp(String(origin.origin_day || '') + 'T16:00:00+08:00');
    const nextOpen = timestamp(String(rotation?.target_day || '') + 'T09:30:00+08:00');
    const interpretation = marketSectorEvidence(emotion, rotation, null).interpretation;
    const reported = interpretation.status === 'observed_pair'
      ? interpretation.sectors.reduce((sum, row) => sum + row.reported, 0) : null;
    if (typeof origin.calendar_receipt_refreshed !== 'boolean'
      || !/^[0-9a-f]{64}$/.test(String(origin.input_sha256 || ''))
      || !/^[0-9a-f]{64}$/.test(String(origin.market_source_sha256 || ''))
      || count(origin.board_count) === null || issued === null || close === null
      || nextOpen === null || issued < close || issued >= nextOpen
      || origin.origin_day !== emotion?.asOf || origin.origin_day !== rotation?.reference_day
      || origin.market_source_sha256 !== emotion?.sourceSha
      || origin.market_state !== emotion?.confirmedState
      || origin.source_board_packet_id !== rotation?.board_packet_id
      || reported === null || origin.board_count !== reported) {
      return {status: 'gap', summary: '服务器已有盘后起点档，但与本页市场或板块来源、日期、状态和发行时点未能对账；不展示为同源起点。'};
    }
    return {status: 'observed', summary: `${origin.origin_day} 盘后已首写 ${origin.board_count} 个板块研究输入（含未达标/不可计算），起点哈希 ${origin.input_sha256.slice(0, 12)}…；${origin.calendar_receipt_refreshed ? '当前日历收据已更新，交易日窗口仍一致；' : ''}仍无未来主流标签、预测概率或交易权限。`};
  }

  function firstWriteEvaluationStatus(packet) {
    const report = record(packet);
    const boundary = report.schema === 'sector_first_write_eval_status.v1'
      && report.research_only === true && report.forecast_validated === false
      && report.production_eligible === false && report.generates_alert === false
      && report.returns_computed === false && report.leader_validated === false
      && report.external_issue_time_attested === false;
    if (!boundary) return {status:'gap', summary:'首写评分报告未核验；不能把历史重算或市场温度当作前向预测。'};
    if (report.status === 'evaluation_not_run') return {status:'gap',
      summary:'尚未运行离线首写 ↔ 未来标签同源验收。缺少可核历史时不生成 1 / 3 / 5 日概率。'};
    if (report.status === 'evaluation_report_invalid') return {status:'gap',
      summary:'离线验收报告损坏或身份不符；不展示历史分数，也不使用旧结果。'};
    const attempts = count(report.candidate_origin_days);
    const matched = count(report.matched_origin_count);
    const valid = ['no_matching_first_write_origins', 'linked_but_insufficient_scored_history',
      'linked_research_diagnostic'].includes(report.status)
      && attempts !== null && matched !== null && matched <= attempts
      && /^[0-9a-f]{64}$/.test(String(report.report_sha256 || ''))
      && timestamp(report.evaluation_as_of) !== null;
    if (!valid) return {status:'gap', summary:'离线验收数量、时点或报告身份不一致；停止展示。'};
    const reasons = record(report.skip_reasons);
    const names = {first_write_missing_or_unsafe:'首写档缺失/路径不安全',
      first_write_invalid_or_unreadable:'首写档损坏/不可读',
      issued_before_protocol_freeze:'晚于发行才冻结协议',
      ledger_origin_unverified:'板块原件未核',
      sector_lineage_or_population_mismatch:'板块来源/分母不符',
      market_pit_mismatch_or_unavailable:'市场时点档不符',
      board_denominator_or_target_day_mismatch:'板块/目标日不符',
      issuer_source_cohort_changed:'发行代码版本改变'};
    const gaps = Object.entries(names).filter(([key]) => count(reasons[key]) > 0)
      .map(([key, name]) => name + ' ' + count(reasons[key]));
    const groups = record(report.group_support);
    const scored = Math.max(0, ...Object.values(groups).map(item => count(record(item).scored_origin_days) || 0));
    const inputNote = report.ledger_status === 'waiting_calendar' ? '官方交易日历/来源收据待核；'
      : report.ledger_status === 'batch_too_large' ? '离线批次超过容量上限；' : '';
    const common = `${inputNote}首写来源对齐 ${matched}/${attempts} 日；同起点评分最多 ${scored} 日。`;
    const suffix = gaps.length ? '跳过：' + gaps.join('、') + '。' : '';
    return {status: report.status === 'linked_research_diagnostic' ? 'research' : 'gap',
      summary: common + suffix + '目标仅为未来再次通过冻结筛选，不是主流强度、收益或买点；发行时钟仍未获外部证明。'};
  }

  function proxyForecastStatus(packet) {
    const item = record(packet);
    const boundary = item.schema === 'sector_proxy_forecast_status.v1'
      && item.research_only === true && item.forecast_validated === false
      && item.production_eligible === false && item.generates_alert === false
      && item.returns_computed === false && item.leader_validated === false
      && item.economic_target_A_validated === false
      && item.external_issue_time_attested === false;
    if (!boundary) return {status:'gap', summary:'盘后代理预测状态未核，不能展示概率或交易建议。'};
    if (item.status !== 'research_probability_issued'
        && item.status !== 'abstained_insufficient_history') {
      return {status:'gap', summary:'尚无可核的当日盘后代理预测首写档（'
        + String(item.status || 'unknown') + '）；不能沿用旧概率。'};
    }
    const issued = timestamp(item.issued_at);
    const total = count(item.board_count);
    const supported = count(item.supported_board_count_any_horizon);
    const digest = String(item.record_sha256 || '');
    if (issued === null || total === null || supported === null || supported > total
        || !/^[0-9a-f]{64}$/.test(digest)) {
      return {status:'gap', summary:'盘后代理预测的时点、数量或档案身份不一致。'};
    }
    return {status: item.status === 'research_probability_issued' ? 'research' : 'gap',
      summary: `${item.origin_day} ${item.issued_at} 首写板块筛选代理档；全母体 ${total}，至少一个跨度有训练支持 ${supported}。`
        + (item.status === 'abstained_insufficient_history' ? '历史支持不足，已弃权。' : '概率仍待独立前向校准。')
        + '目标只是未来再次通过筛选，不是主流、龙头、可成交净超额或买点；发行时钟未获外部证明。'};
  }

  function proxyForwardScoreStatus(packet) {
    const item = record(packet);
    const boundary = item.schema === 'sector_proxy_forward_score_status.v1'
      && item.research_only === true && item.forecast_validated === false
      && item.production_eligible === false && item.generates_alert === false
      && item.returns_computed === false && item.leader_validated === false
      && item.economic_target_A_validated === false
      && item.external_issue_time_attested === false;
    if (!boundary) return {status:'gap', summary:'前向评分报告身份未核；不展示模型成绩。', rows:[]};
    if (item.status === 'evaluation_not_run') return {status:'gap',
      summary:'尚无保存概率与未来标签的独立配对评分。', rows:[]};
    if (item.status === 'evaluation_report_invalid') return {status:'gap',
      summary:'前向评分报告损坏或不符合合同；停止展示旧成绩。', rows:[]};
    const candidate = count(item.candidate_origin_days);
    const matched = count(item.matched_forecast_origin_count);
    const archived = count(item.archived_runs);
    const validStatus = ['no_matching_first_write_forecasts',
      'insufficient_complete_origin_days', 'paired_research_diagnostic'].includes(item.status);
    const digest = String(item.report_sha256 || '');
    if (!validStatus || candidate === null || matched === null || matched > candidate
        || archived === null || archived < 1
        || !/^[0-9a-f]{64}$/.test(digest) || timestamp(item.evaluation_as_of) === null)
      return {status:'gap', summary:'前向评分起点、截止或报告身份不一致。', rows:[]};
    const reasonLabels = {ledger_origin_unverified:'板块原件未核',
      forecast_or_origin_missing_invalid:'首写预测/起点缺失或损坏',
      issued_before_protocol_freeze:'发行早于协议冻结',
      source_or_denominator_mismatch:'来源或完整分母不符',
      issuer_cohort_changed:'供应商/模型版本改变',
      no_evaluable_boards:'无可计算板块',
      forecast_abstained_or_partial_support:'预测弃权/部分板块支持不足',
      target_not_matured_as_of:'目标标签尚未成熟',
      label_unknown_or_source_gap:'未来标签或来源缺失',
      label_not_after_forecast_issue:'标签早于发行'};
    const checkedReasons = reasons => {
      const entries = Object.entries(record(reasons));
      if (entries.some(([,value]) => count(value) === null)) return null;
      return entries.map(([key,value]) => ({label:reasonLabels[key] || key, count:value}));
    };
    const globalReasons = checkedReasons(item.skip_reasons);
    if (globalReasons === null || globalReasons.reduce((sum,row) => sum + row.count,0) + matched !== candidate)
      return {status:'gap', summary:'前向评分未对齐原因与候选分母不符。', rows:[]};
    const groups = record(item.groups);
    const kinds = [['industry','行业'], ['concept','概念']];
    const expected = [1,3,5].flatMap(h => kinds.map(([kind]) => `${h}:${kind}`));
    if (expected.some(key => !Object.hasOwn(groups, key)) || Object.keys(groups).length !== 6)
      return {status:'gap', summary:'前向评分的六组分母不完整。', rows:[]};
    const rows = [];
    for (const h of [1,3,5]) for (const [kind,label] of kinds) {
      const group = record(groups[`${h}:${kind}`]);
      const days = count(group.common_origin_days);
      const attempted = count(group.attempted_origin_days);
      const commonRows = count(group.common_board_rows);
      const expectedRows = count(group.expected_board_rows);
      const skips = checkedReasons(group.skipped_origin_days);
      if (days === null || attempted !== matched || days > attempted
          || commonRows === null || expectedRows === null || commonRows > expectedRows
          || skips === null || skips.reduce((sum,row) => sum + row.count,0) + days !== attempted)
        return {status:'gap', summary:'前向评分的起点或板块分母不一致。', rows:[]};
      const scores = record(group.scores_equal_origin_day);
      const ready = days >= 20 && group.status === 'paired_research_diagnostic';
      const insufficient = days < 20 && group.status === 'insufficient_complete_origin_days';
      if (!ready && !insufficient) return {status:'gap', summary:'前向评分支持门槛不一致。', rows:[]};
      const brier = record(scores.mean_brier);
      const losses = ready ? ['unconditional','qualification','market_state_qualification']
        .map(key => finiteNumber(brier[key])) : [];
      if (ready && (losses.some(value => value === null || value < 0 || value > 1)
          || finiteNumber(scores.paired_delta_brier_vs_unconditional) === null
          || finiteNumber(scores.paired_delta_brier_vs_qualification) === null))
        return {status:'gap', summary:'前向评分损失值无效。', rows:[]};
      rows.push({label:`${h} 日 · ${label}`, days, attempted,
        boardRows:commonRows, expectedRows, skips,
        brier:ready ? losses : null,
        delta:ready ? Number(scores.paired_delta_brier_vs_qualification) : null});
    }
    const skipped = candidate - matched;
    return {status:item.status === 'paired_research_diagnostic' ? 'research' : 'gap', rows,
      summary:`已保留 ${archived} 次评分运行；首写预测对齐 ${matched}/${candidate} 个起点，未对齐 ${skipped}`
        + (globalReasons.length ? '（' + globalReasons.map(row => `${row.label} ${row.count}`).join('、') + '）' : '')
        + '；六组按发行日起点等权、同一板块行比较，至少 20 个完整成熟日起点才显示分数。'
        + '分数仅检验未来再次通过冻结筛选，不能证明主流、龙头、净收益或强化学习奖励；发行时钟未获外部证明。'};
  }

  function peerContinuationStatus(packet) {
    const report = record(packet);
    const boundary = report.schema === 'sector_peer_continuation_status.v1'
      && report.research_only === true && report.formal_mainline_label === false
      && report.forecast_validated === false && report.production_eligible === false
      && report.generates_alert === false && report.investable_returns_computed === false
      && report.historical_membership_verified === false
      && report.full_constituent_diffusion_verified === false
      && report.external_issue_time_attested === false;
    if (!boundary) return {status:'gap', summary:'相对强度研究报告未核验；不能用当前温度推断未来主流。'};
    if (report.status === 'candidate_not_run') return {status:'gap',
      summary:'尚未运行未来同类相对强度 × 已报告上涨占比代理标签；正式主流目标及完整历史成分仍待核。'};
    if (report.status === 'candidate_report_invalid') return {status:'gap',
      summary:'相对强度报告损坏或身份不符；不沿用旧候选。'};
    const known = count(report.known), positive = count(report.positive), unknown = count(report.unknown);
    const valid = ['waiting_calendar', 'future_calendar_missing',
      'first_write_missing_or_invalid', 'calendar_window_changed',
      'protocol_frozen_after_origin', 'batch_too_large',
      'candidate_labels_available', 'insufficient_verified_future'].includes(report.status)
      && known !== null && positive !== null && unknown !== null && positive <= known
      && timestamp(report.evaluation_as_of) !== null
      && /^\d{4}-\d{2}-\d{2}$/.test(String(report.origin_day || ''))
      && /^[0-9a-f]{64}$/.test(String(report.report_sha256 || ''));
    if (!valid) return {status:'gap', summary:'相对强度候选数量、时点或身份不一致；停止展示。'};
    const names = {waiting_calendar:'官方交易日历/来源收据待核',
      future_calendar_missing:'未来交易日历不足',
      first_write_missing_or_invalid:'首写档缺失或无效',
      calendar_window_changed:'首写与当前日历冲突',
      protocol_frozen_after_origin:'协议晚于首写才冻结',
      batch_too_large:'离线批次超限',
      insufficient_verified_future:'未来同源盘后原件不足'};
    const unavailable = names[report.status];
    if (unavailable) return {status:'gap', summary:`${unavailable}；已知代理候选 ${known}，未知 ${unknown}。上涨占比只看供应商已报告的涨/跌计数，不包含平盘与未覆盖成分。`};
    return {status:'research', summary:`${report.origin_day} 首写：未来 1/3/5 日同类相对强度 × 已报告上涨占比代理已知 ${known}、代理阳性 ${positive}、未知 ${unknown}。分母不含平盘与未覆盖成分；完整扩散、历史成员、全 A 对照、预测力及可交易性均未验证。`};
  }

  function directionAuditStatus(packet) {
    const report = record(packet);
    const boundary = report.schema === 'sector_direction_paired_status.v1'
      && report.research_only === true && report.formal_mainline_label === false
      && report.forecast_validated === false && report.production_eligible === false
      && report.generates_alert === false && report.leader_validated === false
      && report.investable_returns_computed === false
      && report.external_issue_time_attested === false;
    if (!boundary) return {status:'gap',
      summary:'延续／反转诊断报告未核验；不把今日板块涨幅解释为未来主流。'};
    if (report.status === 'audit_not_run') return {status:'gap',
      summary:'尚未运行板块前20%／后20%的同起点诊断；需连续首写及到期标签。'};
    if (report.status === 'audit_report_invalid') return {status:'gap',
      summary:'延续／反转报告损坏或来源身份不符；不沿用旧结果。'};
    const attempted = count(report.attempted_origin_days);
    const files = count(report.origin_file_count);
    const skipped = record(report.skipped_source_files);
    const skipCounts = Object.values(skipped).map(count);
    const skippedTotal = skipCounts.reduce((sum, value) => sum + value, 0);
    const valid = attempted !== null
      && files !== null && skipCounts.every(value => value !== null)
      && (report.status === 'batch_too_large'
        ? files >= attempted + skippedTotal : files === attempted + skippedTotal)
      && /^[0-9a-f]{64}$/.test(String(report.report_sha256 || ''))
      && ['waiting_calendar', 'origin_history_missing', 'batch_too_large',
          'insufficient_scored_origin_days', 'research_diagnostic',
          'origin_source_gaps'].includes(report.status)
      && record(report.groups) === report.groups;
    if (!valid) return {status:'gap',
      summary:'延续／反转起点数量或报告身份不一致；停止展示。'};
    if (report.status === 'origin_source_gaps') return {status:'gap',
      summary:`历史原件筛选未完整：${files} 个源文件中 ${attempted} 个通过读取、${skippedTotal} 个跳过；全部数值暂缓展示。`};
    if (skippedTotal) return {status:'gap', summary:'历史原件有未解释的跳过；停止展示数值。'};
    const gaps = {waiting_calendar:'官方交易日历及来源收据待核',
      origin_history_missing:'盘后首写历史缺失',
      batch_too_large:'离线起点批次超限',
      insufficient_scored_origin_days:'同源、完整标签的交易日起点不足20日'};
    if (gaps[report.status]) return {status:'gap',
      summary:`${gaps[report.status]}；尝试起点 ${attempted} 日。前后20%与全体率均不显示为预测。`};
    const groups = [];
    for (const [key, item] of Object.entries(report.groups)) {
      const matched = /^([135]):(industry|concept)$/.exec(key);
      const days = count(item?.scored_origin_days);
      const rates = item?.rates_equal_origin_day;
      if (!matched || item?.status !== 'research_diagnostic'
          || days === null || days < 20 || !rates) continue;
      const front = rates.top_rate, back = rates.bottom_rate;
      if (typeof front !== 'number' || typeof back !== 'number'
          || !Number.isFinite(front) || !Number.isFinite(back)
          || front < 0 || front > 1 || back < 0 || back > 1) continue;
      const tails = item?.tail_mean_pct;
      const topShare = tails?.top, bottomShare = tails?.bottom;
      if (typeof topShare !== 'number' || typeof bottomShare !== 'number'
          || !Number.isFinite(topShare) || !Number.isFinite(bottomShare)
          || topShare < 20 || topShare > 100 || bottomShare < 20 || bottomShare > 100) continue;
      groups.push(`${matched[1]}日${matched[2] === 'industry' ? '行业' : '概念'}：前/后20%分位阈值（实际入组均值 ${topShare.toFixed(1)}%/${bottomShare.toFixed(1)}%）代理阳性 ${(front*100).toFixed(1)}%/${(back*100).toFixed(1)}%（${days}个起点）`);
    }
    if (!groups.length) return {status:'gap',
      summary:'报告称有诊断但共同起点或数值未达到展示门槛；停止展示。'};
    return {status:'research',
      summary:`历史同起点诊断：${groups.join('；')}。边界同分全纳入；按交易日等权。这是供应商板块代理结果，未证明预测增量、龙头收益或买点。`};
  }

  function protocolReproductionDetail(input) {
    const reproduction = record(input);
    const files = record(reproduction.files);
    const names = ['base', 'amendment_1', 'amendment_2', 'amendment_3',
      'amendment_4', 'amendment_5'];
    if (reproduction.schema !== 'retired_mainline_protocol_reproduction.v1'
        || reproduction.strategy_replay_validated !== false
        || reproduction.production_eligible !== false
        || Object.keys(files).length !== names.length
        || names.some(name => !Object.hasOwn(files, name))) {
      return '冻结协议状态未核，旧代码不能重放。';
    }
    const states = names.map((name, index) => {
      const row = record(files[name]);
      if (row.state !== 'matched') return row.state;
      const suffix = index === 0 ? '' : `_AMENDMENT_${index}`;
      const expectedPath = `research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL${suffix}.json`;
      return row.path === expectedPath
        && /^[0-9a-f]{64}$/.test(row.expected_sha256)
        && row.observed_sha256 === row.expected_sha256 ? 'matched' : 'invalid';
    });
    const missing = states.filter(state => state === 'missing').length;
    const mismatched = states.filter(state => state === 'hash_mismatch').length;
    const unsafe = states.filter(state => ['path_symlink', 'root_symlink',
      'file_oversized', 'path_component_invalid', 'path_not_repository_relative']
      .includes(state)).length;
    const other = states.filter(state => !['matched', 'missing', 'hash_mismatch',
      'path_symlink', 'root_symlink', 'file_oversized',
      'path_component_invalid', 'path_not_repository_relative'].includes(state)).length;
    if (missing === 6) return '6 份冻结协议缺失，旧代码不能重放。';
    if (missing || mismatched || unsafe || other) {
      const gaps = [[missing, '缺失'], [mismatched, '哈希不符'],
        [unsafe, '路径不安全或文件超限'], [other, '状态无效']]
        .filter(([value]) => value > 0).map(([value, label]) => `${value} 份${label}`);
      return `6 份冻结协议中 ${gaps.join('、')}，旧代码不能重放。`;
    }
    return '后端报告 6 份冻结协议字节相符；策略重放与收益仍未验证。';
  }

  function mainlineApplicabilityStatus(input) {
    const source = record(input);
    const market = record(source.market);
    const factors = record(source.factors);
    const origin = record(source.originStatus);
    const direction = record(source.directionStatus);
    const archive = record(source.archive);
    const knownState = ['RISK_OFF', 'STABILIZING', 'RISK_ON', 'OVERHEATED']
      .includes(market.code) ? market.code : null;
    const archivedBoundary = archive.schema === 'mainline_retired_archive_integrity.v1'
      && archive.historical_performance_recomputed === false
      && archive.production_eligible === false && archive.generates_alert === false
      && archive.external_issue_time_attested === false;
    const references = record(archive.references);
    const expected = ['run_admission', 'blind_result', 'independent_result_audit'];
    const referenceStates = expected.map(key => record(references[key]).state);
    const bound = archivedBoundary && archive.route_binding === 'matched'
      && Object.keys(references).length === expected.length
      && referenceStates.every(state => typeof state === 'string');
    const missingCount = referenceStates.filter(state => state === 'missing').length;
    const protocolDetail = protocolReproductionDetail(archive.protocol_reproduction);
    const archiveDetail = !bound
      ? '旧主线档案完整性接口缺失或边界不符；不得复用旧策略。'
      : archive.status === 'referenced_receipts_missing' && missingCount > 0
        ? `旧策略已封存；${missingCount} 份结果原件缺失，档案收益不能独立复算。${protocolDetail}`
        : archive.status === 'bound_receipts_present_not_recomputed'
          && referenceStates.every(state => state === 'matched')
          ? `旧策略已封存；引用字节相符仍不是历史收益复算或重新准入。${protocolDetail}`
          : `旧主线档案或绑定未通过完整性核验；不得复用旧策略。${protocolDetail}`;
    const countText = count(factors.deployableCount);
    return {schema: 'mainline_applicability_view.v1', research_only: true,
      formal_mainline_label: false, production_eligible: false,
      generates_alert: false, changes_trade_authority: false,
      headline: knownState === 'RISK_OFF'
        ? '当前有效状态为风险收缩：默认不新增风险'
        : knownState ? '已知市场状态，主流/因子/战法适用性仍待验证'
          : '市场同槽状态未核：不判断战法适用性',
      rows: [
        {id: 'market', title: '市场状态', status: knownState ? 'observed' : 'gap',
          detail: knownState === 'RISK_OFF'
            ? '防守优先；现有持仓仍按账户与原严格路由独立处理。'
            : knownState ? '四轴描述当时状态；不是状态条件下的策略胜率。'
              : '市场同槽来源或路由时点未核；不从单一温度补算。'},
        {id: 'mainline', title: '主流板块 · 1/3/5 日', status: 'gap',
          detail: origin.status === 'observed' && direction.status === 'research'
            ? '目标 A 已冻结：可交易龙头组合净超额；盘后起点与代理可核，但成交后净收益和独立前向检验仍缺。'
            : '目标 A 已冻结：可交易龙头组合净超额；盘后首写或同起点代理未齐，不能生成主流预测。'},
        {id: 'leader', title: '可交易龙头', status: 'gap',
          detail: '历史全成分、冻结候选、次日可达价格与成本后收益未验；成交额核心观察不等于龙头。'},
        {id: 'factor', title: '因子适用', status: 'gap',
          detail: countText === null
            ? '因子准入目录摘要未核；还缺各情绪状态的独立样本外增量。'
            : `目录显示 ${countText} 个可部署因子；本主线仍缺逐状态独立样本外增量，不据此启用。`},
        {id: 'playbook', title: '战法/策略适用', status: 'gap',
          detail: '本主线无已验证的状态条件与成交后增量；其他策略仍须由原严格路由独立核准。'},
        {id: 'retired', title: '旧主线策略', status: 'retired', detail: archiveDetail},
      ]};
  }

  function referenceMarketObservation(packet) {
    const breadth = record(record(packet).breadth);
    const expected = breadth.reference_universe_expected;
    const returned = breadth.reference_universe_returned;
    const sample = breadth.sample_size;
    const numbers = [expected, returned, sample, breadth.up, breadth.down, breadth.flat];
    const countsValid = numbers.every(value => Number.isInteger(value) && value >= 0)
      && expected > 0 && returned > 0 && returned <= expected
      && sample > 0 && sample <= returned
      && breadth.up + breadth.down + breadth.flat === sample;
    const asOf = timestamp(breadth.as_of) !== null ? breadth.as_of : null;
    const earliest = timestamp(breadth.earliest_as_of) !== null ? breadth.earliest_as_of : null;
    const latest = timestamp(breadth.latest_as_of) !== null ? breadth.latest_as_of : null;
    const coherentTime = asOf && earliest && latest
      && timestamp(asOf) === timestamp(earliest)
      && timestamp(latest) >= timestamp(earliest)
      && earliest.slice(0, 10) === latest.slice(0, 10)
      && breadth.timestamp_same_date === true
      && breadth.future_quote_count === 0
      && breadth.duplicate_quote_count === 0
      && breadth.timestamp_returned === returned;
    const sourceHealth = breadth.health_status;
    const archived = sourceHealth === 'upstream_error' && breadth.error === 'source_stale';
    const validHealth = ['verified', 'delayed'].includes(sourceHealth) || archived;
    const observable = countsValid && coherentTime && breadth.timestamp_complete === true
      && validHealth && breadth.exchange_coverage_status === 'unknown';
    if (!observable) return {status: 'gap', label: '横截面不可核',
      summary: '参考池、逐证券报价时间或覆盖校验不足；不展示旧数值。',
      source: '来源待核验', counts: null, limits: null, coverage: null,
      structure: null, structureReason: '参考池与报价来源未通过核验。',
      research_only: true, participates_in_decision: false};
    const limitUp = Number.isInteger(breadth.limit_up) && breadth.limit_up >= 0
      ? breadth.limit_up : null;
    const limitDown = Number.isInteger(breadth.limit_down) && breadth.limit_down >= 0
      ? breadth.limit_down : null;
    const missing = expected - returned;
    const incomplete = missing > 0;
    const referenceAt = timestamp(breadth.reference_universe_as_of);
    const referenceTimeUnknown = referenceAt === null;
    const laterFrame = referenceAt !== null && referenceAt > timestamp(earliest);
    const quoteWindowWide = timestamp(latest) - timestamp(earliest) > 180000;
    const rawStructure = record(breadth.structure);
    const rawGroups = Array.isArray(rawStructure.groups) ? rawStructure.groups : [];
    const expectedGroupIds = ['sh_main', 'star', 'sz_main', 'chinext', 'bj'];
    const sourceGroupCounts = record(breadth.reference_universe_code_groups);
    const rawDistribution = record(rawStructure.distribution);
    const groupRowsValid = rawGroups.length === expectedGroupIds.length && rawGroups.every((row, index) => {
      const item = record(row);
      return item.id === expectedGroupIds[index]
        && Number.isInteger(sourceGroupCounts[item.id])
        && sourceGroupCounts[item.id] === item.count
        && ['count', 'up', 'down', 'flat'].every(key => Number.isInteger(item[key]) && item[key] >= 0)
        && item.up + item.down + item.flat === item.count
        && typeof item.label === 'string' && typeof item.id === 'string'
        && Number.isFinite(item.median_change_pct);
    }) && Object.keys(sourceGroupCounts).length === expectedGroupIds.length
      && rawGroups.reduce((total, row) => total + row.count, 0) === sample
      && rawGroups.reduce((total, row) => total + row.up, 0) === breadth.up
      && rawGroups.reduce((total, row) => total + row.down, 0) === breadth.down
      && rawGroups.reduce((total, row) => total + row.flat, 0) === breadth.flat;
    const distributionValid = rawDistribution.count === sample
      && ['median_change_pct', 'p10_change_pct', 'p90_change_pct'].every(key =>
        Number.isFinite(rawDistribution[key]))
      && rawDistribution.p10_change_pct <= rawDistribution.median_change_pct
      && rawDistribution.median_change_pct <= rawDistribution.p90_change_pct
      && Number.isInteger(rawDistribution.amount_observation_count)
      && rawDistribution.amount_observation_count >= 0
      && rawDistribution.amount_observation_count <= sample
      && Number.isInteger(rawDistribution.top_amount_stock_count)
      && rawDistribution.top_amount_stock_count === Math.max(1, Math.ceil(sample * 0.05))
      && (rawDistribution.top_5pct_amount_share_pct == null
        || (Number.isFinite(rawDistribution.top_5pct_amount_share_pct)
          && rawDistribution.amount_observation_count === sample
          && rawDistribution.top_5pct_amount_share_pct >= 0
          && rawDistribution.top_5pct_amount_share_pct <= 100));
    const structureValid = !incomplete && !laterFrame && !quoteWindowWide && referenceAt !== null
      && referenceAt <= timestamp(earliest)
      && rawStructure.status === (archived ? 'archived' : 'observed')
      && groupRowsValid && distributionValid;
    const label = referenceTimeUnknown ? '参考清单时点未核'
      : laterFrame ? (incomplete ? '部分参考池 · 较晚清单回查' : '较晚清单回查 · 非历史市场')
      : incomplete ? (archived ? '部分参考池 · 过期观察' : '部分参考池观察')
      : archived ? '过期历史观察' : sourceHealth === 'delayed'
        ? '延迟参考池观察' : '参考池当时观察';
    const source = text(breadth.reference_universe_source, '参考池来源待核');
    return {status: archived ? 'archived' : 'observed', label,
      summary: `${sample} 只实际报价：上涨 ${breadth.up}、下跌 ${breadth.down}、平盘 ${breadth.flat}。`
        + (incomplete ? ` 另有 ${missing} 只参考证券未返回，不能外推市场方向或总体温度。` : '')
        + (referenceTimeUnknown ? ' 参考代码清单时间未核；不发布市场结构。' : '')
        + (laterFrame ? ' 参考代码清单晚于报价，不具备当日样本框；不能回填历史市场情绪。' : '')
        + (quoteWindowWide ? ' 逐股报价相隔超过180秒，不能形成同一时刻的市场结构。' : '')
        + (archived ? ' 报价已过期，不能作当前买卖依据。' : ' 仅描述参考池当时横截面。'),
      source: `${source} · 报价最早时点 ${asOf} · 参考池捕获 ${text(breadth.reference_universe_as_of)}`,
      counts: {up: breadth.up, down: breadth.down, flat: breadth.flat},
      limits: {up: limitUp, down: limitDown,
        evaluable: Number.isInteger(record(breadth.price_limit).evaluable_count)
          ? breadth.price_limit.evaluable_count : null},
      coverage: {returned, expected, exchangeComplete: false,
        segments: record(breadth.reference_universe_segments)},
      structure: structureValid ? {status: rawStructure.status, groups: rawGroups.map(row => ({
        id: row.id, label: row.label, count: row.count, up: row.up, down: row.down,
        flat: row.flat, median: row.median_change_pct,
      })), distribution: {
        count: rawDistribution.count,
        median: rawDistribution.median_change_pct, p10: rawDistribution.p10_change_pct,
        p90: rawDistribution.p90_change_pct,
        topAmountShare: Number.isFinite(rawDistribution.top_5pct_amount_share_pct)
          ? rawDistribution.top_5pct_amount_share_pct : null,
        amountCount: rawDistribution.amount_observation_count,
        topCount: rawDistribution.top_amount_stock_count,
      }} : null,
      structureReason: referenceTimeUnknown ? '参考代码清单时点未核，结构指标停止输出。'
        : laterFrame ? '较晚清单不能回填旧日结构；待当时留档的参考池与完整报价。'
          : quoteWindowWide ? '逐股报价跨度超过180秒，不能计算同一横截面。'
        : incomplete ? '报价批次缺失，分布与占比已关闭。'
          : text(rawStructure.reason, '分布输入或时点未通过校验。'),
      research_only: true, participates_in_decision: false};
  }

  function marketSectorEvidence(emotion, rotation, pit) {
    const alignment = marketSectorAlignment(emotion, rotation);
    const marketReady = emotion?.status === 'complete'
      && /^\d{4}-\d{2}-\d{2}$/.test(emotion.asOf)
      && Array.isArray(emotion.axes) && emotion.axes.length === 4
      && emotion.axes.every(axis => percent(record(axis).value) !== null)
      && emotion.slowBaseline && emotion.fastBaseline;
    const population = marketReady && alignment.status === 'date_only'
      ? sectorPopulation(rotation) : null;
    const transition = sectorTransition(rotation, population);
    const marketDetail = marketReady
      ? `${emotion.asOf} · 慢态 ${emotion.slowBaseline} / 快态 ${emotion.fastBaseline} · `
        + emotion.axes.map(axis => `${axis.label} ${axis.value.toFixed(1)}`).join(' · ')
      : '日线四轴、逐日状态或时点未完整核验，撤销市场状态解释。';
    const populationDetail = population
      ? [['industry', '行业'], ['concept', '供应商概念']].map(([kind, label]) => {
        const row = population[kind];
        return `${label}：原件 ${row.reported}、可计算 ${row.evaluable}、达标 ${row.qualified}、不可计算 ${row.unassessable}`;
      }).join('；')
      : alignment.status === 'different_days'
        ? '参考交易日不同，不能将两份原件拼成同日板块判断。'
        : '板块原件、完整分母或市场同日性未核验，不用前三榜补算。';
    const transitionDetail = transition
      ? [['industry', '行业'], ['concept', '供应商概念']].map(([kind, label]) => {
        const row = transition[kind];
        return `${label}：持续 ${row.continued_qualified}、新达标 ${row.newly_qualified}、失资格 ${row.lost_qualification}、身份/覆盖未匹配 ${row.prior_identity_unmatched + row.current_identity_unmatched}`;
      }).join('；')
      : '缺少连续同源且守恒的相邻交易日原件，不能把失匹配叫作退潮。';
    return {alignment,
      interpretation: marketSectorInterpretation(emotion, rotation, alignment, population, transition),
      steps: [
      {id: 'market', title: '01 市场日线', status: marketReady ? 'observed' : 'gap',
        detail: marketDetail},
      {id: 'population', title: '02 板块完整分母', status: population ? 'observed' : 'gap',
        detail: populationDetail},
      {id: 'transition', title: '03 相邻日迁移', status: transition ? 'observed' : 'gap',
        detail: transitionDetail},
      {id: 'forward', title: '04 未来 1 / 3 / 5 日', status: 'research',
        detail: pitArchiveDetail(emotion, pit)},
    ]};
  }

  return {buildModel, marketSectorAlignment, marketSectorEvidence,
    mainlineOriginStatus, firstWriteEvaluationStatus, proxyForecastStatus, proxyForwardScoreStatus,
    peerContinuationStatus,
    directionAuditStatus, mainlineApplicabilityStatus, protocolReproductionDetail,
    referenceMarketObservation};
});
