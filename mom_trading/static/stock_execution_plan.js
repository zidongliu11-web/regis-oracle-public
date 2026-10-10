(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.StockExecutionPlan = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  function finite(value) { return value != null && Number.isFinite(Number(value)); }
  function datePart(value) { return String(value || '').slice(0, 10); }
  function mean(values) { return values.length ? values.reduce(function (a, b) { return a + b; }, 0) / values.length : null; }
  function smaAt(rows, period) {
    if (!Array.isArray(rows) || rows.length < period) return null;
    return mean(rows.slice(-period).map(function (row) { return Number(row.close); }).filter(Number.isFinite));
  }
  function atr(rows) {
    var tail = (rows || []).slice(-15);
    if (tail.length < 3) return null;
    var ranges = [];
    for (var i = 1; i < tail.length; i += 1) {
      var row = tail[i], previous = Number(tail[i - 1].close);
      ranges.push(Math.max(Number(row.high) - Number(row.low), Math.abs(Number(row.high) - previous), Math.abs(Number(row.low) - previous)));
    }
    return mean(ranges);
  }
  function completedCandles(rows, marketPhase, currentDate) {
    var today = String(currentDate || '').slice(0, 10);
    var out = (Array.isArray(rows) ? rows : []).filter(function (row) {
      var day = datePart(row && row.time);
      return day && (!today || day <= today) && finite(row.open) && finite(row.high) && finite(row.low) && finite(row.close);
    }).slice().sort(function (a, b) { return datePart(a.time).localeCompare(datePart(b.time)); });
    if (out.length && marketPhase !== 'closed' && today && datePart(out[out.length - 1].time) === today) out.pop();
    return out;
  }
  function confirmedLevels(rows) {
    var source = (rows || []).slice(-120), last = source[source.length - 1];
    if (!last || source.length < 10) return { supports:[], resistances:[] };
    var close = Number(last.close), tolerance = Math.max((atr(source) || close * .01) * 1.2, close * .004), raw = [];
    for (var i = 2; i < source.length - 2; i += 1) {
      var window = source.slice(i - 2, i + 3), high = Number(source[i].high), low = Number(source[i].low);
      if (high === Math.max.apply(null, window.map(function (row) { return Number(row.high); }))) raw.push({kind:'resistance',price:high});
      if (low === Math.min.apply(null, window.map(function (row) { return Number(row.low); }))) raw.push({kind:'support',price:low});
    }
    function clusters(kind) {
      var bucket = [];
      raw.filter(function (row) { return row.kind === kind; }).forEach(function (row) {
        var found = bucket.find(function (item) { return Math.abs(item.price - row.price) <= tolerance; });
        if (found) { found.price = (found.price * found.count + row.price) / (found.count + 1); found.count += 1; }
        else bucket.push({price:row.price,count:1});
      });
      return bucket.sort(function (a, b) { return b.count - a.count; });
    }
    return { supports:clusters('support'), resistances:clusters('resistance') };
  }
  function build(input) {
    input = input || {};
    var phase = String(input.marketPhase || 'unknown'), currentDate = datePart(input.currentDate);
    var daily = completedCandles(input.daily, phase, currentDate);
    if (daily.length < 21) return { available:false, reason:'已完成日K不足21根，不能生成买卖点研究。', ruleVersion:'previous_close_gap_2pct_no_chase_v1' };
    var last = daily[daily.length - 1], signalDate = datePart(last.time);
    var planMeta = input.executionPlan || {}, validExecutionDate = datePart(planMeta.valid_execution_date);
    if (planMeta.decision_price_eligible === false) {
      return { available:false, status:'unverified_decision_price', reason:'价格数据尚未通过决策审计，当前K线可供看图；补齐同一信号日的已审计未复权收盘后才能生成买入计划。', ruleVersion:'previous_close_gap_2pct_no_chase_v1' };
    }
    var rawDaily = completedCandles(input.rawDaily, phase, currentDate);
    var rawSignal = rawDaily.find(function (row) { return datePart(row.time) === signalDate; });
    if (!rawSignal || !finite(rawSignal.close) || datePart(planMeta.signal_date) !== signalDate) {
      return { available:false, reason:'同一信号日的未复权收盘尚未完成审计，暂不生成买点价格。', ruleVersion:'previous_close_gap_2pct_no_chase_v1' };
    }
    if (!validExecutionDate) return { available:false, reason:'已审计交易日历无法确定唯一下一交易日，计划按不可用处理。', ruleVersion:'previous_close_gap_2pct_no_chase_v1' };
    var close = Number(rawSignal.close), entryLimit = Math.floor(close * 1.02 * 100 + 1e-8) / 100, technicalClose = Number(last.close);
    var ma5 = smaAt(daily, 5), ma20 = smaAt(daily, 20), levels = confirmedLevels(daily);
    var support = levels.supports.filter(function (row) { return Number(row.price) <= technicalClose; }).sort(function (a,b) { return Number(b.price) - Number(a.price); })[0] || null;
    var resistance = levels.resistances.filter(function (row) { return Number(row.price) > technicalClose; }).sort(function (a,b) { return Number(a.price) - Number(b.price); })[0] || null;
    var riskValues = [];
    if (finite(ma20)) riskValues.push(Number(ma20));
    if (support && finite(support.price)) riskValues.push(Number(support.price));
    var riskLine = riskValues.length ? Math.max.apply(null, riskValues) : null;
    var quote = input.quote || {}, quoteDate = datePart(quote.exchange_data_time || quote.data_time), openPrice = finite(quote.open_price) ? Number(quote.open_price) : null;
    var currentMinute = finite(input.currentTimeMinutes) ? Number(input.currentTimeMinutes) : null;
    var openStatus = 'awaiting_next_open';
    if (!currentDate) openStatus = 'market_data_unavailable';
    else if (currentDate > validExecutionDate) openStatus = 'expired_no_retry';
    else if (currentDate < validExecutionDate && quoteDate > currentDate) openStatus = 'market_data_unavailable';
    else if (currentDate === validExecutionDate && (phase === 'preopen' || phase === 'auction' || (phase === 'closed' && currentMinute != null && currentMinute < 555))) openStatus = 'awaiting_open';
    else if (currentDate === validExecutionDate && quoteDate !== validExecutionDate) openStatus = 'market_data_unavailable';
    else if (currentDate === validExecutionDate && phase === 'closed') openStatus = 'expired_no_retry';
    else if (currentDate === validExecutionDate && (phase === 'open' || phase === 'lunch')) {
      if (!finite(quote.volume) || Number(quote.volume) <= 0 || openPrice == null) openStatus = 'cancelled_untradable';
      else if (openPrice > entryLimit) openStatus = 'cancelled_gap_up';
      else openStatus = 'price_condition_met';
    } else if (currentDate === validExecutionDate && phase !== 'closed') openStatus = 'session_phase_unavailable';
    var timing = input.timing || {}, timingGate = timing.decision_gate || {}, timingAction = timing.recommended_action || 'wait';
    var assessment = input.assessment && typeof input.assessment === 'object' ? input.assessment : null;
    var assessmentRating = assessment ? String(assessment.rating || '') : '';
    var assessmentProvided = Boolean(assessment && assessmentRating);
    var assessmentAllowsBuy = ['买入', '强烈买入'].indexOf(assessmentRating) >= 0;
    var manualAlertAllowed = timingGate.manual_research_alerts_allowed === true
      || timingGate.recommendation_eligible === true;
    var buyPlanEnabled = !assessmentProvided || (assessmentAllowsBuy && manualAlertAllowed);
    var vetoNote = assessment ? String(assessment.veto_note || assessment.vetoNote || '') : '';
    var vetoLabels = {
      fundamentals:'基本面硬风险', governance:'治理风险',
      events:'重大事件风险', tradeable:'交易可行性风险'
    };
    var vetoDisplay = vetoNote.split('；').filter(Boolean).map(function (item) {
      return vetoLabels[item] || item;
    }).join('、');
    var buyBlockReason = '';
    if (assessmentProvided && !assessmentAllowsBuy) {
      buyBlockReason = '统一综合评级' + assessmentRating + '未到买入层'
        + (vetoDisplay ? '；已核验 ' + vetoDisplay : '');
    } else if (assessmentProvided && !manualAlertAllowed) {
      buyBlockReason = '研究型人工提醒能力未开启，买入计划保持关闭';
    }
    var trendWeak = finite(ma5) && finite(ma20) && Number(ma5) < Number(ma20);
    var action = '等待确认', state = 'wait', reason = '时机条件尚未同时满足';
    if (!buyPlanEnabled) {
      action = '暂不考虑买入'; state = 'risk'; reason = buyBlockReason;
    } else if (openStatus === 'expired_no_retry') {
      action = '计划已过期'; state = 'risk'; reason = '唯一下一交易日已经结束，不顺延、不重试';
    } else if (openStatus === 'cancelled_gap_up') {
      action = '不追高·已取消'; state = 'risk'; reason = '本时段开盘高于冻结买入上限';
    } else if (openStatus === 'cancelled_untradable') {
      action = '无法成交·已取消'; state = 'risk'; reason = '本时段缺少可成交开盘或成交量';
    } else if (openStatus === 'awaiting_open') {
      action = '等待正式开盘'; state = 'wait'; reason = '开盘前和集合竞价不判成交，09:30后再核对真实开盘';
    } else if (openStatus === 'session_phase_unavailable') {
      action = '时段状态不可用'; state = 'wait'; reason = '未确认正式交易时段，不判断买点';
    } else if (openStatus === 'market_data_unavailable') {
      action = '行情不可用'; state = 'risk'; reason = '执行日行情缺失、日期不匹配或来自未来，不判断买点';
    } else if (trendWeak || (finite(riskLine) && Number(last.close) < Number(riskLine))) {
      action = '风险复核'; state = 'risk'; reason = '已完成日线跌破趋势或失效参考';
    } else if (timingAction === 'buy_now' && timingGate.recommendation_eligible === true) {
      action = openStatus === 'price_condition_met' ? '价位允许·待确认' : '候选准备';
      state = 'ready'; reason = openStatus === 'price_condition_met' ? '开盘价位符合，仍需人工复核流动性' : '时机已确认，仍需下一开盘满足不追高规则';
    } else if (timingAction === 'avoid') {
      action = '不追高'; state = 'risk'; reason = '当前时机引擎建议避免追价';
    }
    return {
      available:true,
      ruleVersion:'previous_close_gap_2pct_no_chase_v1',
      signalAsOf:signalDate,
      validSession:validExecutionDate,
      action:action,
      state:state,
      reason:reason,
      entryLimit:entryLimit,
      entryLimitBasis:'audited_raw_unadjusted_close_plus_2pct_gap_cap',
      referenceClose:close,
      riskLine:riskLine,
      resistance:resistance ? Number(resistance.price) : null,
      openPrice:openPrice,
      openStatus:openStatus,
      timingAction:timingAction,
      buyPlanEnabled:buyPlanEnabled,
      buyBlockReason:buyBlockReason,
      assessmentRating:assessmentRating || null,
      method:'T日已完成收盘生成计划；仅在日历确认的唯一T+1，以未复权开盘价对比T日未复权收盘的102%上限。超过则取消、不替补、不顺延；卖出提示只在已完成K线确认后用于复核。'
    };
  }

  function buildSell(input) {
    input = input || {};
    if (input.positionLoaded !== true) {
      return { available:false, hasPosition:false, status:'position_unavailable', reason:'手工持仓账本暂不可用，不生成卖出计划。' };
    }
    var position = input.position || null;
    if (!position) {
      return { available:true, hasPosition:false, status:'no_position', action:'无持仓', reason:'手工持仓账本没有该股票，不生成卖出指令。' };
    }
    var quote = input.quote || {}, phase = String(input.marketPhase || 'unknown');
    var currentDate = datePart(input.currentDate), quoteDate = datePart(quote.exchange_data_time || quote.data_time);
    var entry = finite(position.price) ? Number(position.price) : (finite(position.entry_price) ? Number(position.entry_price) : null);
    var shares = finite(position.shares) ? Math.max(0, Math.floor(Number(position.shares))) : 0;
    var current = finite(quote.price) ? Number(quote.price) : null;
    var levels = position.levels || {}, riskAction = position.risk_action || {};
    var declaredLevels = Array.isArray(riskAction.risk_levels) ? riskAction.risk_levels : [];
    var rawRiskLevel = riskAction.effective_risk_level;
    if (!rawRiskLevel || !finite(rawRiskLevel.value)) {
      rawRiskLevel = declaredLevels.filter(function (row) { return row && row.active === true && finite(row.value); })
        .sort(function (a, b) { return (finite(a.priority) ? Number(a.priority) : 999) - (finite(b.priority) ? Number(b.priority) : 999); })[0] || null;
    }
    var riskLevel = rawRiskLevel && finite(rawRiskLevel.value) ? {
      kind:String(rawRiskLevel.kind || 'research_fallback'),
      value:Number(rawRiskLevel.value),
      source:String(rawRiskLevel.source || 'risk_metadata_missing'),
      formula:String(rawRiskLevel.formula || ''),
      dataAsOf:String(rawRiskLevel.data_as_of || ''),
      calibrationStatus:String(rawRiskLevel.calibration_status || 'unknown'),
      priority:finite(rawRiskLevel.priority) ? Number(rawRiskLevel.priority) : null,
      active:rawRiskLevel.active === true,
      executable:rawRiskLevel.executable === true,
      researchOnly:rawRiskLevel.research_only === true,
      semantics:String(rawRiskLevel.semantics || (rawRiskLevel.executable === true
        ? '风险复核线；页面只提示人工复核，不自动下单'
        : '规则型风险复核线不可自动执行；触价仅提示人工复核'))
    } : null;
    if (!riskLevel) {
      var fallbackValue = finite(riskAction.effective_stop) ? Number(riskAction.effective_stop)
        : riskAction.trailing_active && finite(riskAction.trailing_stop) ? Number(riskAction.trailing_stop)
        : finite(riskAction.hard_stop) ? Number(riskAction.hard_stop)
        : finite(levels.stop_loss) ? Number(levels.stop_loss) : null;
      if (fallbackValue != null) riskLevel = {
        kind:'research_fallback', value:fallbackValue,
        source:'legacy_value_without_authoritative_metadata', priority:null,
        active:false, executable:false, researchOnly:true,
        semantics:'缺少风险复核线元数据；仅作研究参考，不触发卖出复核'
      };
    }
    var riskLine = riskLevel ? riskLevel.value : null;
    var rawTargetShares = finite(riskAction.target_shares)
      ? Math.max(0, Math.floor(Number(riskAction.target_shares))) : null;
    var targetShares = rawTargetShares != null && rawTargetShares <= shares
      ? rawTargetShares : null;
    var trailingActivationLine = finite(levels.take_profit_1) ? Number(levels.take_profit_1) : null;
    var structureObservationLine = finite(levels.take_profit_2) ? Number(levels.take_profit_2) : null;
    var status = 'observing', action = '持有观察', reason = riskAction.reason || '当前未触发真实持仓风险或策略退出；+15% / +25% 只作观察。';
    if (!currentDate || !quoteDate || quoteDate !== currentDate || current == null) {
      status = 'quote_unavailable'; action = '行情不可用'; reason = '当前持仓缺少同日真实行情，不判断卖出触发。';
    } else if (phase !== 'open') {
      status = 'outside_execution_window'; action = '时段外观察'; reason = '仅在连续竞价时段判断真实风险或策略退出；+15% / +25% 观察线始终不触发卖出复核。';
    } else if (riskLevel && riskLevel.active === true && current <= Number(riskLine)) {
      status = 'exit_review'; action = '离场复核'; reason = riskAction.reason || '当前价已触及持仓风险线。';
    } else if (String(riskAction.action || '') === '离场复核') {
      status = 'exit_review'; action = '离场复核'; reason = riskAction.reason || '量化风险信号要求人工复核。';
    } else if (String(riskAction.action || '') === '减仓复核') {
      status = 'reduce_review'; action = '减仓复核'; reason = riskAction.reason || '量化风险信号要求人工复核。';
    } else if (finite(structureObservationLine) && current >= Number(structureObservationLine)) {
      status = 'structure_observation'; action = '结构观察'; reason = '当前价已到 +25% 结构观察线；触及本身不生成减仓、兑现或卖出复核。';
    } else if (finite(trailingActivationLine) && current >= Number(trailingActivationLine)) {
      status = 'trailing_observation'; action = '移动风控观察'; reason = '当前价已到 +15% 移动风控启用观察线；只启用后续风险跟踪，不生成卖出复核。';
    } else if (riskLevel && riskLevel.executable !== true) {
      action = '人工观察'; reason = '规则型风险复核线不可自动执行；尚未触价，仅展示人工复核依据。';
    }
    var reviewStatus = ['exit_review', 'reduce_review'].indexOf(status) >= 0;
    var suggestedSellShares = reviewStatus && targetShares != null
      ? Math.max(0, shares - targetShares) : null;
    var quantityReady = Number.isInteger(suggestedSellShares) && suggestedSellShares > 0;
    return {
      available:true,
      hasPosition:true,
      status:status,
      action:action,
      reason:reason,
      entryPrice:entry,
      currentPrice:current,
      quoteAsOf:String(quote.exchange_data_time || quote.data_time || ''),
      shares:shares,
      targetShares:quantityReady ? targetShares : null,
      suggestedSellShares:quantityReady ? suggestedSellShares : null,
      quantityReady:quantityReady,
      riskLine:riskLine,
      riskLevel:riskLevel,
      trailingActivationLine:trailingActivationLine,
      structureObservationLine:structureObservationLine,
      observationNote:'+15% 仅启用移动风险跟踪，+25% 仅观察价格结构；两者都不是卖出信号。',
      executionWindow:'连续竞价 09:30–11:30 / 13:00–15:00',
      quantityNote:quantityReady
        ? '风险计划建议人工复核卖出 ' + suggestedSellShares + ' 股，卖后保留 ' + targetShares + ' 股；实际成交仍由用户确认。'
        : reviewStatus
          ? '风险条件已触发，但后端没有完整目标股数；不得自行推断卖出数量。'
          : '当前账本持仓 ' + shares + ' 股；未触发精确减仓或离场计划。',
      cancelCondition:'未实际成交不视为执行；下一次真实行情刷新后重新判断，不自动下单。'
    };
  }

  function strictManualBuyPlan(input) {
    input = input || {};
    var workspace = input.workspace || ((input.route || {}).today_decision_workspace) || {};
    var code = String(input.code || '');
    if (input.marketPhase !== 'open' || workspace.actionable_now !== true) {
      return {available:false, status:'not_actionable_now', reason:workspace.reason || '当前交易时段没有完整人工复核计划。'};
    }
    if (workspace.strict_forward_packet_valid !== true
      || workspace.strict_market_budget_valid !== true
      || workspace.sector_entry_gate_ready !== true) {
      return {available:false, status:'route_incomplete', reason:workspace.reason || '市场、板块与账户的同一观察时点证据尚未同时完成。'};
    }
    var rows = Array.isArray(workspace.manual_review_items) ? workspace.manual_review_items : [];
    var item = rows.find(function (row) { return String((row || {}).code || '') === code; }) || null;
    var plan = item && item.manual_plan && typeof item.manual_plan === 'object' ? item.manual_plan : null;
    if (!plan) return {available:false, status:'stock_not_in_strict_plan', reason:'该股票未进入今日最多两项严格人工复核清单，不展示旧股数。'};
    var shares = finite(plan.shares) ? Number(plan.shares) : null;
    var limitPrice = finite(plan.limit_price) ? Number(plan.limit_price) : null;
    var estimatedCost = finite(plan.estimated_cost) ? Number(plan.estimated_cost) : null;
    var identity = plan.evidence_identity;
    var evidenceIdentity = plan.evidence_id || (identity && typeof identity === 'object'
      ? identity.id || identity.packet_id || identity.method : identity);
    var complete = plan.status === 'ready_for_manual_review' && plan.side === 'BUY'
      && /^\d{6}$/.test(code) && Number.isInteger(shares) && shares > 0
      && finite(limitPrice) && limitPrice > 0 && finite(estimatedCost) && estimatedCost >= 0
      && Boolean(plan.valid_until) && Boolean(plan.invalidation_condition)
      && Boolean(plan.playbook_id) && Boolean(plan.strategy_id) && Boolean(plan.strategy_version)
      && Boolean(evidenceIdentity) && Boolean(plan.evidence_grade) && Boolean(plan.as_of)
      && plan.human_confirmation_required === true
      && plan.broker_connected === false && plan.automatic_trading === false;
    if (!complete) return {available:false, status:'plan_contract_incomplete', reason:'该股票计划缺少股数、限价、成本、有效期、失效条件或证据身份，保持关闭。'};
    return {
      available:true, status:'ready_for_manual_review', code:code,
      name:String(item.name || code), shares:shares, limitPrice:limitPrice,
      estimatedCost:estimatedCost,
      estimatedCashRequired:finite(plan.estimated_cash_required) ? Number(plan.estimated_cash_required) : null,
      targetWeightPct:finite(plan.target_weight_pct) ? Number(plan.target_weight_pct) : null,
      liquidityCapacityYuan:finite(plan.liquidity_capacity_yuan) ? Number(plan.liquidity_capacity_yuan) : null,
      riskLine:finite(plan.risk_line) ? Number(plan.risk_line) : null,
      riskLineKind:String(plan.risk_line_kind || ''), validUntil:String(plan.valid_until),
      invalidationCondition:String(plan.invalidation_condition),
      playbookId:String(plan.playbook_id), strategyId:String(plan.strategy_id),
      strategyVersion:String(plan.strategy_version), evidenceIdentity:String(evidenceIdentity),
      evidenceGrade:String(plan.evidence_grade), asOf:String(plan.as_of),
      reason:String(plan.reason || '完整人工复核计划已形成；仍需用户确认，不自动下单。')
    };
  }

  return { build:build, buildSell:buildSell, strictManualBuyPlan:strictManualBuyPlan, completedCandles:completedCandles, confirmedLevels:confirmedLevels };
});
