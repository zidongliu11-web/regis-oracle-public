/* Regis AI Drawings: current, explainable chart structure from supplied OHLCV only. */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.RegisAIDrawings = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  var VERSION = '8.0.1';
  var PROFILES = {
    conservative:{ minBars:60, lookback:260, atrPeriod:14, pivotRadius:3, reversalAtr:1.20, zoneAtr:0.55, minZoneTouches:3, structureBars:120, maxZoneAge:45, maxDistanceAtr:3.5, minTrendSpan:7, maxTrendAge:25, lineAtr:0.50, maxTrendDistanceAtr:1.75, minScore:64, maxDrawings:4 },
    balanced:    { minBars:40, lookback:240, atrPeriod:14, pivotRadius:2, reversalAtr:0.90, zoneAtr:0.65, minZoneTouches:2, structureBars:100, maxZoneAge:55, maxDistanceAtr:4.0, minTrendSpan:5, maxTrendAge:35, lineAtr:0.60, maxTrendDistanceAtr:2.25, minScore:50, maxDrawings:4 },
    sensitive:   { minBars:30, lookback:180, atrPeriod:14, pivotRadius:2, reversalAtr:0.70, zoneAtr:0.80, minZoneTouches:2, structureBars:80,  maxZoneAge:65, maxDistanceAtr:5.0, minTrendSpan:4, maxTrendAge:45, lineAtr:0.75, maxTrendDistanceAtr:3.00, minScore:42, maxDrawings:4 }
  };

  function finite(value) { return value != null && isFinite(Number(value)); }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
  function mean(values) { return values.length ? values.reduce(function (sum, value) { return sum + value; }, 0) / values.length : 0; }
  function median(values) {
    if (!values.length) return 0;
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  }
  function round(value, digits) {
    var power = Math.pow(10, digits == null ? 4 : digits);
    return Math.round(Number(value) * power) / power;
  }
  function pad(value) { return Number(value) < 10 ? '0' + Number(value) : String(value); }
  function stableTime(value) {
    if (value && typeof value === 'object' && value.year != null) return String(value.year) + '-' + pad(value.month) + '-' + pad(value.day);
    if (typeof value === 'number' || typeof value === 'string') return String(value);
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }
  function timeOrdinal(value) {
    if (typeof value === 'number' && isFinite(value)) return value;
    if (typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value)) return Number(value);
    if (value && typeof value === 'object' && value.year != null) return Date.UTC(Number(value.year), Number(value.month) - 1, Number(value.day));
    var parsed = Date.parse(String(value));
    return isNaN(parsed) ? null : parsed;
  }
  function hash(text) {
    var value = 2166136261;
    for (var index = 0; index < text.length; index += 1) { value ^= text.charCodeAt(index); value = Math.imul(value, 16777619); }
    return ('00000000' + (value >>> 0).toString(16)).slice(-8);
  }
  function confidence(score) { return score >= 80 ? 'high' : score >= 60 ? 'medium' : 'low'; }
  function recencyDecay(age, halfLife) { return Math.pow(0.5, Math.max(0, age) / Math.max(1, halfLife)); }
  function diagnostic(level, code, message, details) {
    var item = { level:level, code:code, message:message };
    if (details) item.details = details;
    return item;
  }
  function publicPoint(item) { return { time:item.time, price:round(item.price, 6) }; }
  function makeDrawing(data, signature) {
    var score = clamp(Math.round(data.score), 0, 100), out = {
      id:'regis-' + data.kind + '-' + hash(data.kind + '|' + signature),
      kind:data.kind,
      type:data.type,
      label:data.label,
      points:data.points || [],
      score:score,
      confidence:confidence(score),
      evidence:data.evidence || {},
      reason:data.reason,
      touches:Math.max(0, Math.round(data.touches || 0)),
      invalidated:false,
      priority:data.priority || 'secondary',
      role:data.role || 'structure',
      state:'active',
      lastTouchBars:Math.max(0, Math.round(data.lastTouchBars || 0))
    };
    ['low', 'high', 'price', 'projectedPrice', 'distancePct', 'distanceAtr', 'zoneLow', 'zoneHigh', 'swingLow', 'swingHigh', 'triggerPrice', 'invalidationPrice'].forEach(function (key) { if (finite(data[key])) out[key] = Number(data[key]); });
    if (data.patternState) out.patternState = String(data.patternState);
    return out;
  }
  function emptyResult(diagnostics, meta) { return { version:VERSION, drawings:[], focus:null, scenarios:[], decisionMap:null, diagnostics:diagnostics, meta:meta || {} }; }

  function normalize(candles, diagnostics) {
    if (!Array.isArray(candles)) { diagnostics.push(diagnostic('error', 'input_not_array', 'OHLCV 输入必须是数组。')); return null; }
    var rows = [], invalid = [], seen = Object.create(null), duplicates = [], unordered = [], missingVolume = 0, priorOrdinal = null;
    candles.forEach(function (row, index) {
      var object = row && typeof row === 'object', prices = object ? [row.open, row.high, row.low, row.close] : [];
      if (prices.length !== 4 || !prices.every(finite) || row.time == null) { invalid.push(index); return; }
      var open = Number(row.open), high = Number(row.high), low = Number(row.low), close = Number(row.close);
      var epsilon = Math.max(1, Math.abs(open), Math.abs(high), Math.abs(low), Math.abs(close)) * 1e-10;
      if (low > high || high + epsilon < Math.max(open, close, low) || low - epsilon > Math.min(open, close, high)) { invalid.push(index); return; }
      var key = stableTime(row.time), ordinal = timeOrdinal(row.time);
      if (seen[key]) duplicates.push(index);
      seen[key] = true;
      if (ordinal != null && priorOrdinal != null && ordinal <= priorOrdinal) unordered.push(index);
      if (ordinal != null) priorOrdinal = ordinal;
      var volume = finite(row.volume) && Number(row.volume) >= 0 ? Number(row.volume) : null;
      if (volume == null) missingVolume += 1;
      rows.push({ time:row.time, open:open, high:high, low:low, close:close, volume:volume, sourceIndex:index });
    });
    if (invalid.length) diagnostics.push(diagnostic('error', 'invalid_ohlc', '存在缺失或自相矛盾的 OHLC 行，未生成画线。', { indexes:invalid.slice(0, 12), count:invalid.length }));
    if (duplicates.length) diagnostics.push(diagnostic('error', 'duplicate_time', '存在重复时间的 K 线，未生成画线。', { indexes:duplicates.slice(0, 12), count:duplicates.length }));
    if (unordered.length) diagnostics.push(diagnostic('error', 'non_chronological_time', 'K 线没有按时间严格递增，未生成画线。', { indexes:unordered.slice(0, 12), count:unordered.length }));
    if (invalid.length || duplicates.length || unordered.length) return null;
    if (missingVolume) diagnostics.push(diagnostic('warning', 'volume_unavailable', '部分 K 线没有有效成交量；量能只作证据加分，不影响价格结构识别。', { count:missingVolume }));
    return rows;
  }

  function atrSeries(rows, period) {
    var ranges = rows.map(function (row, index) {
      if (!index) return row.high - row.low;
      var previous = rows[index - 1].close;
      return Math.max(row.high - row.low, Math.abs(row.high - previous), Math.abs(row.low - previous));
    });
    var values = Array(rows.length).fill(null), seed = 0, prior = null;
    ranges.forEach(function (value, index) {
      if (index < period) seed += value;
      if (index === period - 1) { prior = seed / period; values[index] = prior; }
      else if (index >= period) { prior = (prior * (period - 1) + value) / period; values[index] = prior; }
    });
    return { values:values, ranges:ranges };
  }
  function recentAtr(data) {
    var usable = data.values.filter(finite).slice(-20).map(Number);
    return median(usable.length ? usable : data.ranges.slice(-20));
  }
  function volumeRatio(rows, index) {
    if (!finite(rows[index].volume)) return null;
    var sample = rows.slice(Math.max(0, index - 20), index).map(function (row) { return row.volume; }).filter(function (value) { return finite(value) && value > 0; });
    return sample.length >= 5 && mean(sample) > 0 ? rows[index].volume / mean(sample) : null;
  }
  function point(row, index, kind, price, atrValue, prominence, rows) {
    return { time:row.time, price:price, index:index, kind:kind, atr:atrValue, prominence:prominence, volumeRatio:volumeRatio(rows, index) };
  }
  function detectSwings(rows, atrs, baseAtr, profile) {
    var raw = [], ambiguous = 0, radius = profile.pivotRadius;
    for (var index = radius; index < rows.length - radius; index += 1) {
      var row = rows[index], left = rows.slice(index - radius, index), right = rows.slice(index + 1, index + radius + 1), neighbours = left.concat(right);
      var isHigh = neighbours.every(function (other) { return row.high >= other.high; }) && neighbours.some(function (other) { return row.high > other.high; });
      var isLow = neighbours.every(function (other) { return row.low <= other.low; }) && neighbours.some(function (other) { return row.low < other.low; });
      var atrValue = finite(atrs[index]) ? Number(atrs[index]) : baseAtr, threshold = atrValue * profile.reversalAtr;
      var highProminence = Math.min(row.high - Math.min.apply(null, left.map(function (item) { return item.low; })), row.high - Math.min.apply(null, right.map(function (item) { return item.low; })));
      var lowProminence = Math.min(Math.max.apply(null, left.map(function (item) { return item.high; })) - row.low, Math.max.apply(null, right.map(function (item) { return item.high; })) - row.low);
      var highPoint = isHigh && highProminence >= threshold, lowPoint = isLow && lowProminence >= threshold;
      if (highPoint && lowPoint) {
        if (Math.max(highProminence, lowProminence) / Math.max(1e-12, Math.min(highProminence, lowProminence)) < 1.15) { ambiguous += 1; continue; }
        highPoint = highProminence > lowProminence; lowPoint = !highPoint;
      }
      if (highPoint) raw.push(point(row, index, 'high', row.high, atrValue, highProminence, rows));
      if (lowPoint) raw.push(point(row, index, 'low', row.low, atrValue, lowProminence, rows));
    }
    var swings = [];
    raw.forEach(function (candidate) {
      var last = swings[swings.length - 1];
      if (!last) { swings.push(candidate); return; }
      if (last.kind === candidate.kind) {
        var moreExtreme = candidate.kind === 'high' ? candidate.price > last.price : candidate.price < last.price;
        if (moreExtreme) swings[swings.length - 1] = candidate;
      } else if (Math.abs(candidate.price - last.price) >= Math.max(last.atr, candidate.atr, baseAtr) * profile.reversalAtr) swings.push(candidate);
    });
    return { points:swings, ambiguous:ambiguous };
  }

  function clusterPoints(points, tolerance) {
    var clusters = [];
    points.slice().sort(function (a, b) { return a.price - b.price || a.index - b.index; }).forEach(function (item) {
      var nearest = null, distance = Infinity;
      clusters.forEach(function (cluster) {
        var current = Math.abs(cluster.center - item.price);
        if (current <= tolerance && current < distance) { nearest = cluster; distance = current; }
      });
      if (!nearest) clusters.push({ center:item.price, points:[item] });
      else { nearest.points.push(item); nearest.center = mean(nearest.points.map(function (point) { return point.price; })); }
    });
    return clusters;
  }
  function twoCloses(rows, predicate) { return rows.length >= 2 && rows.slice(-2).every(predicate); }
  function distanceToRange(price, low, high) { return price < low ? low - price : price > high ? price - high : 0; }

  function zoneDrawings(rows, swings, atrValue, profile, rejected) {
    var close = rows[rows.length - 1].close, tolerance = Math.max(atrValue * profile.zoneAtr, Math.abs(close) * 0.0025);
    var cutoff = Math.max(0, rows.length - profile.structureBars), margin = atrValue * 0.25, candidates = [];
    [{ pivot:'low', kind:'support_zone', label:'关键支撑线', side:'support' }, { pivot:'high', kind:'resistance_zone', label:'关键压力线', side:'resistance' }].forEach(function (definition) {
      var relevant = swings.filter(function (item) { return item.kind === definition.pivot && item.index >= cutoff; });
      clusterPoints(relevant, tolerance).filter(function (cluster) { return cluster.points.length >= profile.minZoneTouches; }).forEach(function (cluster) {
        var ordered = cluster.points.slice().sort(function (a, b) { return a.index - b.index; }), prices = ordered.map(function (item) { return item.price; });
        var center = mean(prices), low = Math.min.apply(null, prices) - tolerance * 0.12, high = Math.max.apply(null, prices) + tolerance * 0.12;
        var age = rows.length - 1 - ordered[ordered.length - 1].index;
        var broken = definition.side === 'support' ? twoCloses(rows, function (row) { return row.close < low - margin; }) : twoCloses(rows, function (row) { return row.close > high + margin; });
        if (broken) { rejected.invalid += 1; return; }
        if ((definition.side === 'support' && center > close + tolerance * 0.35) || (definition.side === 'resistance' && center < close - tolerance * 0.35)) { rejected.wrongSide += 1; return; }
        var distance = distanceToRange(close, low, high), distanceAtr = distance / atrValue, distancePct = distance / Math.max(Math.abs(close), 1e-12) * 100;
        if (distanceAtr > profile.maxDistanceAtr) { rejected.distant += 1; return; }
        if (age > profile.maxZoneAge) { rejected.stale += 1; return; }
        var spread = Math.sqrt(mean(prices.map(function (price) { return Math.pow(price - center, 2); })));
        var volumeRatios = ordered.map(function (item) { return item.volumeRatio; }).filter(finite).map(Number);
        var sideHistory = rows.slice(cutoff).map(function (row) { return row.close >= center ? 1 : -1; });
        var roleFlips = 0;
        for (var sideIndex = 1; sideIndex < sideHistory.length; sideIndex += 1) {
          if (sideHistory[sideIndex] !== sideHistory[sideIndex - 1]) roleFlips += 1;
        }
        var reactions = ordered.map(function (item) {
          var after = rows.slice(item.index + 1, Math.min(rows.length, item.index + 4));
          if (!after.length) return 0;
          var excursion = definition.side === 'support'
            ? Math.max.apply(null, after.map(function (row) { return row.high - item.price; }))
            : Math.max.apply(null, after.map(function (row) { return item.price - row.low; }));
          return Math.max(0, excursion / atrValue);
        });
        var reactionStrength = median(reactions);
        /* Current relevance is 55 points (distance + explicit time decay); repeated touches cannot rescue stale/far structures. */
        var decay = recencyDecay(age, profile.maxZoneAge / 2), score = 25 * (1 - distanceAtr / profile.maxDistanceAtr) + 24 * decay + Math.min(4, ordered.length) * 6 + 10 * (1 - clamp(spread / tolerance, 0, 1)) + Math.min(8, roleFlips * 2) + Math.min(9, reactionStrength * 3);
        if (volumeRatios.length) score += clamp((mean(volumeRatios) - 1) * 5, 0, 5);
        var invalidationPrice = definition.side === 'support' ? low - margin : high + margin;
        var action = definition.side === 'support' ? '连续两日收盘低于 ' + round(invalidationPrice, 2) + ' 才视为有效跌破。' : '连续两日收盘高于 ' + round(invalidationPrice, 2) + ' 才视为有效突破。';
        var signature = ordered.slice(0, 2).map(function (item) { return stableTime(item.time) + '@' + round(item.price, 6); }).join('|');
        candidates.push(makeDrawing({
          kind:definition.kind, type:'zone', role:definition.side, label:definition.label + ' ' + round(center, 2), points:ordered.map(publicPoint), low:low, high:high,
          projectedPrice:center, score:score, touches:ordered.length, distanceAtr:distanceAtr, distancePct:distancePct, lastTouchBars:age, priority:distanceAtr <= 1 ? 'primary' : 'secondary',
          reason:'图表基准收盘价 ' + round(close, 2) + '，距该区 ' + round(distanceAtr, 2) + ' ATR；最近一次结构触达在 ' + age + ' 根K线前。' + action,
          evidence:{ method:'single_key_level_with_reactions', presentationStyle:'single_line_with_reaction_points', atr:round(atrValue), tolerance:round(tolerance), projectedPrice:round(center), distanceAtr:round(distanceAtr), distancePct:round(distancePct, 2), lastTouchBars:age, recencyDecay:round(decay, 3), touchTimes:ordered.map(function (item) { return item.time; }), volumeConfirmed:volumeRatios.length > 0, roleFlipCount:roleFlips, reactionStrengthAtr:round(reactionStrength, 3), invalidationPrice:round(invalidationPrice) }
        }, signature));
      });
    });
    return ['support_zone', 'resistance_zone'].map(function (kind) {
      return candidates.filter(function (item) { return item.kind === kind; }).sort(function (a, b) { return a.distanceAtr - b.distanceAtr || b.score - a.score; })[0] || null;
    }).filter(Boolean);
  }

  function structuralBreak(rows, atrValue, profile) {
    if (rows.length < 24) return null;
    var end = rows.length - 2, start = Math.max(0, end - Math.min(40, profile.structureBars)), base = rows.slice(start, end), recent = rows.slice(-2), close = rows[rows.length - 1].close;
    var priorLow = Math.min.apply(null, base.map(function (row) { return row.low; })), priorHigh = Math.max.apply(null, base.map(function (row) { return row.high; }));
    var down = recent.every(function (row) { return row.close < priorLow; }) && close <= priorLow - atrValue * 0.20;
    var up = recent.every(function (row) { return row.close > priorHigh; }) && close >= priorHigh + atrValue * 0.20;
    if (!down && !up) return null;
    var direction = down ? 'down' : 'up', boundary = down ? priorLow : priorHigh, tolerance = atrValue * profile.zoneAtr;
    var touches = base.map(function (row, offset) { return { time:row.time, price:down ? row.low : row.high, index:start + offset }; }).filter(function (item) { return down ? item.price <= boundary + tolerance : item.price >= boundary - tolerance; });
    var prices = touches.map(function (item) { return item.price; }), low = Math.min.apply(null, prices) - atrValue * 0.10, high = Math.max.apply(null, prices) + atrValue * 0.10;
    var displacementAtr = Math.abs(close - boundary) / atrValue, distance = distanceToRange(close, low, high), distanceAtr = distance / atrValue;
    var distancePct = distance / Math.max(Math.abs(close), 1e-12) * 100, currentVr = volumeRatio(rows, rows.length - 1);
    var signature = direction + '|' + stableTime(touches[0].time) + '@' + round(boundary, 6), triggerPrice = down ? high + atrValue * 0.10 : low - atrValue * 0.10;
    var drawing = makeDrawing({
      kind:down ? 'resistance_reclaim_zone' : 'support_retest_zone', type:'zone', role:down ? 'breakdown_reclaim' : 'breakout_retest',
      label:down ? '跌破后收回观察区 ' + round(low, 2) + '–' + round(high, 2) : '突破后回踩观察区 ' + round(low, 2) + '–' + round(high, 2),
      points:touches.map(publicPoint), low:low, high:high, projectedPrice:boundary,
      score:35 + 32 * (1 - clamp(distanceAtr / profile.maxDistanceAtr, 0, 1)) + 20 * recencyDecay(2, 10) + (finite(currentVr) ? clamp((currentVr - 1) * 5, 0, 5) : 0), touches:touches.length,
      distanceAtr:distanceAtr, distancePct:distancePct, lastTouchBars:2, priority:'primary',
      reason:down
        ? '最近两日收盘均跌破原低位边界 ' + round(boundary, 2) + '，图表基准收盘低 ' + round(displacementAtr, 2) + ' ATR；暂无可靠下方支撑。只有连续两日收盘重新站上 ' + round(triggerPrice, 2) + '，才算收回确认。'
        : '最近两日收盘均突破原高位边界 ' + round(boundary, 2) + '，图表基准收盘高 ' + round(displacementAtr, 2) + ' ATR；该区只作为回踩观察。连续两日收盘跌回 ' + round(triggerPrice, 2) + ' 下方，才算突破失败。',
      evidence:{ method:'two_close_structural_break', direction:direction, atr:round(atrValue), projectedPrice:round(boundary), distanceAtr:round(distanceAtr), distancePct:round(distancePct, 2), lastTouchBars:2, recencyDecay:round(recencyDecay(2, 10), 3), boundary:round(boundary), displacementAtr:round(displacementAtr), currentVolumeRatio:finite(currentVr) ? round(currentVr, 2) : null, confirmationCloses:recent.map(function (row) { return { time:row.time, close:row.close }; }), triggerPrice:round(triggerPrice), volumeRule:'量能只作确认，不单独产生信号' }
    }, signature);
    return {
      direction:direction,
      drawing:drawing,
      scenario:{
        id:'scenario-' + hash(drawing.id + '|retest'), kind:down ? 'reclaim' : 'retest', status:'active', label:down ? '破位后的收回观察' : '突破后的回踩观察', relatedDrawingId:drawing.id,
        observe:{ state:down ? 'below_broken_structure' : 'above_broken_structure', price:round(close, 4), note:down ? '当前仍在原低位区下方；不推测未经验证的支撑。' : '当前仍在原高位区上方；观察回踩而非推测延续。' },
        confirm:{ rule:down ? 'two_closes_above' : 'holds_above', price:round(triggerPrice, 4), volumeRatioGte:1, volumeRequired:false },
        invalidate:{ rule:down ? 'close_below_latest_low' : 'two_closes_below', price:round(down ? rows[rows.length - 1].low : triggerPrice, 4) },
        rationale:drawing.reason
      }
    };
  }

  // ponytail: 最多 240 根K线内穷举近期拐点对最直白、可复核；扩大样本后再优化。
  function fitTrend(rows, swings, direction, atrValue, profile, rejected) {
    var pivotKind = direction === 'up' ? 'low' : 'high', sign = direction === 'up' ? 1 : -1;
    var pivots = swings.filter(function (item) { return item.kind === pivotKind && item.index >= Math.max(0, rows.length - profile.structureBars); });
    var tolerance = Math.max(atrValue * profile.lineAtr, Math.abs(rows[rows.length - 1].close) * 0.0015), best = null;
    for (var first = 0; first < pivots.length - 1; first += 1) {
      for (var second = first + 1; second < pivots.length; second += 1) {
        var a = pivots[first], b = pivots[second], span = b.index - a.index, age = rows.length - 1 - b.index;
        if (span < profile.minTrendSpan || age > profile.maxTrendAge || (b.price - a.price) * sign < atrValue * 0.35) continue;
        var slope = (b.price - a.price) / span;
        if (Math.abs(slope) > atrValue * 1.5) continue;
        var touches = [], residual = 0;
        pivots.filter(function (item) { return item.index >= a.index; }).forEach(function (item) {
          var expected = a.price + slope * (item.index - a.index), difference = item.price - expected;
          if (Math.abs(difference) <= tolerance) { touches.push(item); residual += Math.abs(difference); }
        });
        if (touches.length < 2) continue;
        var broken = false, run = 0;
        for (var index = b.index + 1; index < rows.length; index += 1) {
          var line = a.price + slope * (index - a.index), crossed = direction === 'up' ? rows[index].close < line - tolerance * 0.55 : rows[index].close > line + tolerance * 0.55;
          run = crossed ? run + 1 : 0;
          if (run >= 2) { broken = true; break; }
        }
        if (broken) { rejected.invalid += 1; continue; }
        var projected = a.price + slope * (rows.length - 1 - a.index), distance = Math.abs(rows[rows.length - 1].close - projected), distanceAtr = distance / atrValue;
        if (distanceAtr > profile.maxTrendDistanceAtr) { rejected.distant += 1; continue; }
        var fit = 1 - clamp((residual / touches.length) / tolerance, 0, 1), decay = recencyDecay(age, profile.maxTrendAge / 2);
        var score = 30 * (1 - distanceAtr / profile.maxTrendDistanceAtr) + 27 * decay + Math.min(4, touches.length) * 7 + fit * 12;
        var candidate = { a:a, b:b, slope:slope, projected:projected, touches:touches, tolerance:tolerance, span:span, age:age, distanceAtr:distanceAtr, distancePct:distance / Math.max(Math.abs(rows[rows.length - 1].close), 1e-12) * 100, score:score, decay:decay };
        if (!best || candidate.score > best.score || (candidate.score === best.score && candidate.age < best.age)) best = candidate;
      }
    }
    if (!best) return null;
    var close = rows[rows.length - 1].close, confirm = direction === 'up' ? best.projected - best.tolerance * 0.55 : best.projected + best.tolerance * 0.55;
    var kind = direction === 'up' ? 'uptrend' : 'downtrend', confirmed = best.touches.length >= 3;
    var label = (direction === 'up' ? '当前上升防守线' : '当前下降压力线') + (confirmed ? ' · 三触确认' : ' · 两点候选');
    var signature = stableTime(best.a.time) + '@' + round(best.a.price, 6) + '|' + stableTime(best.b.time) + '@' + round(best.b.price, 6);
    return {
      drawing:makeDrawing({
        kind:kind, type:'trend', role:'trend_boundary', label:label, points:[publicPoint(best.a), { time:rows[rows.length - 1].time, price:round(best.projected, 6) }],
        projectedPrice:best.projected, score:best.score, touches:best.touches.length, distanceAtr:best.distanceAtr, distancePct:best.distancePct, lastTouchBars:best.age, priority:best.distanceAtr <= 0.75 ? 'primary' : 'secondary',
        reason:(confirmed ? '已有至少三次拐点触达，作为已确认趋势边界。' : '目前只有两点成线，仍是候选边界，第三次触达前不作为主趋势确认。') + ' 图表基准收盘价 ' + round(close, 2) + '，延伸至当前约 ' + round(best.projected, 2) + '，相距 ' + round(best.distanceAtr, 2) + ' ATR；' + (direction === 'up' ? '连续两日收盘低于 ' : '连续两日收盘高于 ') + round(confirm, 2) + ' 才视为结构失效。',
        evidence:{ method:'recent_projected_multi_touch_line', direction:direction, atr:round(atrValue), anchors:[publicPoint(best.a), publicPoint(best.b)], projectedPrice:round(best.projected), distanceAtr:round(best.distanceAtr), distancePct:round(best.distancePct, 2), lastTouchBars:best.age, recencyDecay:round(best.decay, 3), slopePerBar:round(best.slope, 6), spanBars:best.span, touchPoints:best.touches.map(publicPoint), thirdTouchConfirmed:confirmed, invalidationPrice:round(confirm) }
      }, signature),
      model:best,
      direction:direction
    };
  }

  function buildChannel(rows, swings, trend, atrValue, profile) {
    if (!trend) return null;
    var model = trend.model, oppositeKind = trend.direction === 'up' ? 'high' : 'low', sign = trend.direction === 'up' ? 1 : -1;
    var offsets = swings.filter(function (item) { return item.kind === oppositeKind && item.index >= model.a.index; }).map(function (item) {
      return { item:item, value:(item.price - (model.a.price + model.slope * (item.index - model.a.index))) * sign };
    }).filter(function (item) { return item.value > atrValue; });
    if (offsets.length < 2) return null;
    var width = median(offsets.map(function (item) { return item.value; })), tolerance = atrValue * profile.lineAtr;
    var touches = offsets.filter(function (item) { return Math.abs(item.value - width) <= tolerance; });
    if (touches.length < 2) return null;
    var latest = touches.slice().sort(function (a, b) { return b.item.index - a.item.index; })[0].item;
    var close = rows[rows.length - 1].close, oppositeProjected = model.projected + sign * width;
    var distance = Math.min(Math.abs(close - model.projected), Math.abs(close - oppositeProjected));
    var distanceAtr = distance / atrValue, distancePct = distance / Math.max(Math.abs(close), 1e-12) * 100;
    var age = Math.min(model.age, rows.length - 1 - latest.index), widthAtr = width / atrValue;
    var score = trend.drawing.score - 4 + Math.min(3, touches.length) * 2;
    var kind = trend.direction === 'up' ? 'uptrend_channel' : 'downtrend_channel';
    var label = trend.direction === 'up' ? '当前上升通道' : '当前下降通道';
    var signature = trend.drawing.id + '|' + stableTime(latest.time) + '@' + round(latest.price, 6);
    return makeDrawing({
      kind:kind, type:'channel', role:'trend_channel', label:label + ' · ' + model.touches.length + '+' + touches.length + '触',
      /* 自动图层约定为 [基线起点, 平行线证据点, 基线当前投影]。保存个人画线时再转换为手工通道顺序。 */
      points:[publicPoint(model.a), publicPoint(latest), { time:rows[rows.length - 1].time, price:round(model.projected, 6) }],
      projectedPrice:model.projected, score:score, touches:model.touches.length + touches.length,
      distanceAtr:distanceAtr, distancePct:distancePct, lastTouchBars:age, priority:'secondary',
      reason:'两侧分别有 ' + model.touches.length + ' 与 ' + touches.length + ' 个近期拐点支持平行边界；通道宽约 ' + round(widthAtr, 2) + ' ATR。通道只描述当前结构，连续收盘越界后不再采用。',
      evidence:{ method:'parallel_multi_touch_channel', direction:trend.direction, atr:round(atrValue), widthAtr:round(widthAtr, 3), projectedPrice:round(model.projected), oppositeProjectedPrice:round(oppositeProjected), distanceAtr:round(distanceAtr), distancePct:round(distancePct, 2), lastTouchBars:age, baseTouchPoints:model.touches.map(publicPoint), parallelTouchPoints:touches.map(function (item) { return publicPoint(item.item); }) }
    }, signature);
  }

  function detectImpulseChannel(rows, swings, atrValue, profile) {
    var lastIndex = rows.length - 1, last = rows[lastIndex], best = null;
    for (var index = 0; index < swings.length - 2; index += 1) {
      var a = swings[index], b = swings[index + 1], c = swings[index + 2];
      var direction = a.kind === 'low' && b.kind === 'high' && c.kind === 'low' ? 'up'
        : a.kind === 'high' && b.kind === 'low' && c.kind === 'high' ? 'down' : null;
      if (!direction || c.index >= lastIndex || c.index < lastIndex - profile.structureBars) continue;
      var sign = direction === 'up' ? 1 : -1;
      var impulseBars = b.index - a.index, channelBars = lastIndex - c.index;
      var impulseMove = (b.price - a.price) * sign, channelMove = (last.close - c.price) * sign;
      if (impulseBars < 2 || impulseBars > 24 || channelBars < 8 || impulseMove < atrValue * 3 || channelMove < atrValue) continue;
      var retracement = (b.price - c.price) * sign, retracementRatio = retracement / impulseMove;
      if (retracementRatio < 0.08 || retracementRatio > 0.72) continue;
      var impulseSpeed = impulseMove / impulseBars, channelSpeed = channelMove / channelBars;
      if (channelSpeed <= 0 || channelSpeed > impulseSpeed * 0.68) continue;
      var channelRows = rows.slice(c.index), againstOrigin = direction === 'up'
        ? twoCloses(channelRows, function (row) { return row.close < c.price - atrValue * 0.35; })
        : twoCloses(channelRows, function (row) { return row.close > c.price + atrValue * 0.35; });
      if (againstOrigin) continue;
      var efficiency = Math.abs(last.close - c.price) / Math.max(atrValue, channelRows.reduce(function (sum, row, offset) {
        return offset ? sum + Math.abs(row.close - channelRows[offset - 1].close) : sum;
      }, 0));
      var score = 48 + clamp(impulseMove / atrValue - 3, 0, 5) * 5 + clamp((0.68 - channelSpeed / impulseSpeed) * 20, 0, 10) + clamp(efficiency * 18, 0, 12) - Math.min(12, index < swings.length - 6 ? 8 : 0);
      var candidate = { a:a, b:b, c:c, direction:direction, sign:sign, impulseBars:impulseBars, channelBars:channelBars, impulseMove:impulseMove, channelMove:channelMove, impulseSpeed:impulseSpeed, channelSpeed:channelSpeed, retracementRatio:retracementRatio, efficiency:efficiency, score:score };
      if (!best || candidate.score > best.score || (candidate.score === best.score && candidate.c.index > best.c.index)) best = candidate;
    }
    if (!best) return null;
    var zoneHalf = atrValue * 0.42, low = best.c.price - zoneHalf, high = best.c.price + zoneHalf;
    var distance = distanceToRange(last.close, low, high), distanceAtr = distance / atrValue;
    var confirmPrice = best.direction === 'up' ? high : low, invalidationPrice = best.direction === 'up' ? low - atrValue * 0.25 : high + atrValue * 0.25;
    var label = best.direction === 'up' ? '急速上涨→缓升通道' : '急速下跌→缓降通道';
    var signature = [best.direction, stableTime(best.a.time), stableTime(best.b.time), stableTime(best.c.time)].join('|');
    var drawing = makeDrawing({
      kind:'impulse_channel_' + best.direction, type:'impulse_channel', role:'channel_origin_retest', label:label,
      points:[publicPoint(best.a), publicPoint(best.b), publicPoint(best.c), {time:last.time, price:round(last.close, 6)}],
      low:low, high:high, projectedPrice:best.c.price, score:best.score, touches:3, distanceAtr:distanceAtr,
      distancePct:distance / Math.max(Math.abs(last.close), 1e-12) * 100, lastTouchBars:lastIndex - best.c.index,
      priority:distanceAtr <= 1.25 ? 'primary' : 'secondary',
      reason:label + '；急速段 ' + round(best.impulseMove / atrValue, 2) + ' ATR/' + best.impulseBars + ' 根，随后通道速度降至急速段的 ' + round(best.channelSpeed / best.impulseSpeed * 100, 1) + '%。不追第一次顺势突破，等待回踩通道起点 ' + round(low, 2) + '–' + round(high, 2) + ' 后再看完成K线确认。',
      evidence:{ method:'confirmed_pivot_impulse_then_same_direction_channel', direction:best.direction, atr:round(atrValue), impulseStart:publicPoint(best.a), impulseEnd:publicPoint(best.b), channelOrigin:publicPoint(best.c), impulseBars:best.impulseBars, channelBars:best.channelBars, impulseAtr:round(best.impulseMove / atrValue, 3), channelSpeedRatio:round(best.channelSpeed / best.impulseSpeed, 3), retracementRatio:round(best.retracementRatio, 3), pathEfficiency:round(best.efficiency, 3), originZone:{low:round(low), high:round(high)}, confirmationPrice:round(confirmPrice), invalidationPrice:round(invalidationPrice) }
    }, signature);
    return {
      drawing:drawing,
      scenario:{
        id:'scenario-' + hash(drawing.id + '|origin-retest'), kind:'channel_origin_retest', status:distanceAtr <= 1 ? 'testing' : 'waiting', label:'通道起点回踩观察', relatedDrawingId:drawing.id,
        observe:{state:distanceAtr <= 1 ? 'testing_origin_zone' : 'waiting_origin_retest', price:round(best.c.price, 4), note:drawing.reason},
        confirm:{rule:best.direction === 'up' ? 'completed_close_reclaims_origin_zone' : 'completed_close_rejects_origin_zone', price:round(confirmPrice, 4), volumeRatioGte:1, volumeRequired:false},
        invalidate:{rule:best.direction === 'up' ? 'two_closes_below_origin' : 'two_closes_above_origin', price:round(invalidationPrice, 4)},
        rationale:'“急速接通道”只用于识别高风险追突破场景；回踩起点和完成K线确认缺一不可。'
      },
      context:{
        state:'impulse_channel', direction:best.direction, label:label,
        firstOpportunityPolicy:'avoid_chasing', originZone:{low:round(low), high:round(high)},
        distanceAtr:round(distanceAtr, 3), originRelevantNow:distanceAtr <= 1.25,
        summary:distanceAtr <= 1.25
          ? '当前接近急速接同向缓速通道的起点；第一次顺势突破不作为机会，优先等待回踩与完成K线确认。'
          : '历史路径符合急速接同向缓速通道，但通道起点距图表基准收盘 ' + round(distanceAtr, 2) + ' ATR，已超出当前关键位置范围；只保留为避免追涨的背景，不作为当前买卖边界。'
      }
    };
  }

  function detectFibRetracement(rows, swings, atrValue, profile) {
    var lastIndex = rows.length - 1, close = rows[lastIndex].close, best = null;
    for (var index = Math.max(0, swings.length - 12); index < swings.length - 1; index += 1) {
      var start = swings[index], end = swings[index + 1], direction = start.kind === 'low' && end.kind === 'high' ? 'up' : start.kind === 'high' && end.kind === 'low' ? 'down' : null;
      if (!direction || end.index >= lastIndex || end.index < lastIndex - profile.structureBars) continue;
      var move = Math.abs(end.price - start.price), span = end.index - start.index, age = lastIndex - end.index;
      if (span < 3 || move < atrValue * 3 || age > profile.maxZoneAge) continue;
      var ratio = direction === 'up' ? (end.price - close) / move : (close - end.price) / move;
      if (ratio < 0.236 || ratio > 0.786) continue;
      var level382 = direction === 'up' ? end.price - move * 0.382 : end.price + move * 0.382;
      var level500 = direction === 'up' ? end.price - move * 0.500 : end.price + move * 0.500;
      var level618 = direction === 'up' ? end.price - move * 0.618 : end.price + move * 0.618;
      var level786 = direction === 'up' ? end.price - move * 0.786 : end.price + move * 0.786;
      var zoneLow = Math.min(level500, level618), zoneHigh = Math.max(level500, level618);
      var distance = distanceToRange(close, zoneLow, zoneHigh), distanceAtr = distance / atrValue;
      if (distanceAtr > 2.25) continue;
      var score = 40 + 22 * recencyDecay(age, profile.maxZoneAge / 2) + 20 * (1 - clamp(distanceAtr / 2.25, 0, 1)) + 8 * (1 - clamp(Math.abs(ratio - 0.559) / 0.323, 0, 1));
      var candidate = { start:start, end:end, direction:direction, move:move, span:span, age:age, ratio:ratio, level382:level382, level500:level500, level618:level618, level786:level786, zoneLow:zoneLow, zoneHigh:zoneHigh, distanceAtr:distanceAtr, distance:distance, score:score };
      if (!best || candidate.score > best.score || (candidate.score === best.score && candidate.end.index > best.end.index)) best = candidate;
    }
    if (!best) return null;
    var swingLow = Math.min(best.start.price, best.end.price), swingHigh = Math.max(best.start.price, best.end.price), projected = (best.zoneLow + best.zoneHigh) / 2;
    var signature = [best.direction, stableTime(best.start.time), stableTime(best.end.time)].join('|');
    return makeDrawing({
      kind:'fib_retracement_' + best.direction, type:'fib', role:'fib_confluence', label:'有效摆动斐波回撤 · 50%–61.8%',
      points:[publicPoint(best.start), publicPoint(best.end)], low:swingLow, high:swingHigh, swingLow:swingLow, swingHigh:swingHigh,
      zoneLow:best.zoneLow, zoneHigh:best.zoneHigh, projectedPrice:projected, score:best.score, touches:2,
      distanceAtr:best.distanceAtr, distancePct:best.distance / Math.max(Math.abs(close), 1e-12) * 100, lastTouchBars:best.age,
      priority:best.distanceAtr <= 0.75 ? 'primary' : 'secondary',
      reason:'只对最近已确认、幅度至少 3 ATR 的完整摆动段绘制。当前回撤 ' + round(best.ratio * 100, 1) + '%；重点观察 50%–61.8% 区间 ' + round(best.zoneLow, 2) + '–' + round(best.zoneHigh, 2) + '，必须再看完成K线与量能确认，斐波比例本身不产生买卖信号。',
      evidence:{ method:'confirmed_swing_fibonacci_retracement', direction:best.direction, atr:round(atrValue), anchors:[publicPoint(best.start), publicPoint(best.end)], spanBars:best.span, impulseAtr:round(best.move / atrValue, 3), retracementRatio:round(best.ratio, 4), levels:{r382:round(best.level382), r500:round(best.level500), r618:round(best.level618), r786:round(best.level786)}, goldenPocket:{low:round(best.zoneLow), high:round(best.zoneHigh)}, volumeRule:'斐波比例只定位，完成K线与量能负责确认' }
    }, signature);
  }

  function buildMarketContext(rows, atrValue, regime, support, resistance, impulseChannel) {
    var latest = rows[rows.length - 1], prior = rows[rows.length - 2] || latest;
    var volume = volumeRatio(rows, rows.length - 1), moveAtr = Math.abs(latest.close - prior.close) / atrValue, flags = [];
    if (impulseChannel) flags.push({code:'impulse_channel_first_break_risk', level:'warning', text:impulseChannel.context.summary});
    if (regime === 'range' && ((support && support.distanceAtr <= 0.6) || (resistance && resistance.distanceAtr <= 0.6))) {
      flags.push({code:'range_edge_false_break_risk', level:'warning', text:'价格处于箱体边缘附近；第一次越界优先按流动性试探处理，等待收盘与回踩确认。'});
    }
    if (finite(volume) && volume < 0.8 && moveAtr >= 0.9) {
      flags.push({code:'low_effort_large_result', level:'warning', text:'本根价格移动达到 ' + round(moveAtr, 2) + ' ATR，但成交量仅为近20根均量的 ' + round(volume, 2) + ' 倍；价格动了不等于市场共识形成。'});
    }
    return {
      state:impulseChannel ? 'impulse_channel' : flags.length ? 'caution' : 'normal',
      impulseOriginRelevant:!!(impulseChannel && impulseChannel.context.originRelevantNow),
      impulseDistanceAtr:impulseChannel ? impulseChannel.context.distanceAtr : null,
      volumeRatio:finite(volume) ? round(volume, 3) : null, latestMoveAtr:round(moveAtr, 3), flags:flags,
      action:flags.length ? 'wait_for_completed_bar_and_retest' : 'follow_current_structure_rules',
      summary:flags.length ? flags[0].text : '未发现急速接通道、箱体边缘或低量大幅移动的额外回避标签。'
    };
  }

  function consecutiveCloses(rows, predicate, maximum) {
    var count = 0;
    for (var index = rows.length - 1; index >= 0 && count < maximum; index -= 1) {
      if (!predicate(rows[index])) break;
      count += 1;
    }
    return count;
  }

  /*
   * Three-point reversal protocol: 1 is the exhausted extreme, 2 the first
   * counter-swing, 3 a failed retest.  The pattern is confirmed only after a
   * completed close crosses point 2; otherwise it remains an observation.
   */
  function detect123Reversal(rows, swings, atrValue, profile) {
    var recent = swings.filter(function (item) { return item.index >= Math.max(0, rows.length - profile.structureBars); });
    if (recent.length < 3) return null;
    for (var index = recent.length - 3; index >= Math.max(0, recent.length - 8); index -= 1) {
      var one = recent[index], two = recent[index + 1], three = recent[index + 2];
      var bullish = one.kind === 'low' && two.kind === 'high' && three.kind === 'low' && three.price > one.price;
      var bearish = one.kind === 'high' && two.kind === 'low' && three.kind === 'high' && three.price < one.price;
      if (!bullish && !bearish) continue;
      if (two.index - one.index < 2 || three.index - two.index < 2) continue;
      if (Math.abs(two.price - one.price) < atrValue * 1.15) continue;
      var trigger = two.price, invalidation = one.price;
      var invalidated = bullish ? rows[rows.length - 1].close <= invalidation : rows[rows.length - 1].close >= invalidation;
      if (invalidated) continue;
      var confirmed = bullish
        ? consecutiveCloses(rows, function (row) { return row.close > trigger; }, 2) >= 1
        : consecutiveCloses(rows, function (row) { return row.close < trigger; }, 2) >= 1;
      var age = rows.length - 1 - three.index;
      if (age > profile.maxTrendAge) continue;
      var distanceAtr = Math.abs(rows[rows.length - 1].close - trigger) / atrValue;
      if (!confirmed && distanceAtr > 1) continue;
      var score = 46 + (confirmed ? 18 : 0) + 18 * recencyDecay(age, 15) + 12 * clamp(Math.abs(two.price - one.price) / (atrValue * 3), 0, 1);
      var direction = bullish ? 'bullish' : 'bearish';
      return makeDrawing({
        kind:'pattern_123_' + direction, type:'pattern_123', role:'reversal',
        label:(bullish ? '看涨' : '看跌') + ' 123 ' + (confirmed ? '已触发' : '待触发'),
        points:[publicPoint(one), publicPoint(two), publicPoint(three)],
        triggerPrice:trigger, invalidationPrice:invalidation, patternState:confirmed ? 'confirmed' : 'watching',
        projectedPrice:trigger, distanceAtr:distanceAtr, distancePct:Math.abs(rows[rows.length - 1].close - trigger) / Math.max(Math.abs(rows[rows.length - 1].close), 1e-12) * 100,
        score:score, touches:3, lastTouchBars:age, priority:confirmed ? 'primary' : 'secondary',
        reason:'点1为趋势极值，点2为首次反向摆动，点3未重破点1；' + (confirmed ? '完成K线已越过点2，进入人工复核。' : '尚未越过点2，只观察不提示买卖。'),
        evidence:{ method:'three_point_123_completed_close', direction:direction, point1:publicPoint(one), point2:publicPoint(two), point3:publicPoint(three), triggerPrice:round(trigger), invalidationPrice:round(invalidation), confirmed:confirmed, lastTouchBars:age }
      }, direction + '|' + stableTime(one.time) + '|' + stableTime(two.time) + '|' + stableTime(three.time));
    }
    return null;
  }

  function buildStructureProtocol(rows, atrValue, regime, drawings, support, resistance, structure, marketContext, higherContexts) {
    higherContexts = Array.isArray(higherContexts) ? higherContexts : [];
    var latest = rows[rows.length - 1], range = Math.max(latest.high - latest.low, 1e-12), volume = volumeRatio(rows, rows.length - 1);
    var closeLocation = clamp((latest.close - latest.low) / range, 0, 1), bodyRatio = clamp(Math.abs(latest.close - latest.open) / range, 0, 1);
    var prior = rows[rows.length - 2] || latest, overlap = Math.max(0, Math.min(latest.high, prior.high) - Math.max(latest.low, prior.low));
    var overlapRatio = clamp(overlap / Math.max(Math.min(range, prior.high - prior.low), 1e-12), 0, 1);
    var confirmedTrend = (drawings || []).filter(function (item) { return item.type === 'trend' && item.touches >= 3; })[0] || null;
    var reversal123 = (drawings || []).filter(function (item) { return item.type === 'pattern_123'; })[0] || null;
    var direction = structure ? (structure.direction === 'up' ? 'up' : 'down') : confirmedTrend ? (confirmedTrend.kind === 'uptrend' ? 'up' : 'down') : regime === 'uptrend' || regime === 'breakout' ? 'up' : regime === 'downtrend' || regime === 'breakdown' ? 'down' : 'range';
    var trendAligned = (higherContexts || []).filter(function (item) {
      var contextDirection = item.regime === 'uptrend' || item.regime === 'breakout' ? 'up' : item.regime === 'downtrend' || item.regime === 'breakdown' ? 'down' : 'range';
      return direction !== 'range' && contextDirection === direction;
    }).length;
    var nearSupport = support && support.distanceAtr <= 1, nearResistance = resistance && resistance.distanceAtr <= 1;
    var positionLabel = nearSupport ? '接近支撑观察区' : nearResistance ? '接近压力观察区' : '未到关键位置';
    var powerState = closeLocation >= 0.7 && bodyRatio >= 0.45 ? 'buyers' : closeLocation <= 0.3 && bodyRatio >= 0.45 ? 'sellers' : 'balanced';
    var powerLabel = powerState === 'buyers' ? '本根买方推进较强' : powerState === 'sellers' ? '本根卖方推进较强' : '本根力量未形成单边确认';
    var confirmBoundary = null, confirmDirection = null, confirmLabel = '等待关键位后的连续收盘确认';
    if (structure) {
      confirmBoundary = structure.direction === 'up' ? structure.drawing.high : structure.drawing.low;
      confirmDirection = structure.direction === 'up' ? 'above' : 'below';
      confirmLabel = structure.direction === 'up' ? '突破连续收盘确认' : '破位连续收盘确认';
    } else if (nearResistance) {
      confirmBoundary = resistance.high + atrValue * 0.25; confirmDirection = 'above'; confirmLabel = '压力突破连续收盘确认';
    } else if (nearSupport) {
      confirmBoundary = support.high; confirmDirection = 'above'; confirmLabel = '支撑守住后的连续收盘确认';
    }
    var observed = confirmBoundary == null ? 0 : consecutiveCloses(rows, function (row) { return confirmDirection === 'below' ? row.close < confirmBoundary : row.close > confirmBoundary; }, 2);
    var steps = [
      {key:'trend', label:'趋势', state:direction, ready:direction !== 'range', summary:confirmedTrend ? confirmedTrend.label : direction === 'up' ? '当前结构方向向上' : direction === 'down' ? '当前结构方向向下' : '当前为震荡结构', evidence:(confirmedTrend ? confirmedTrend.touches + ' 次拐点触达确认；' : '') + '当前周期 ' + regime + (higherContexts.length ? '；上级周期同向 ' + trendAligned + '/' + higherContexts.length : '；上级周期样本不足')},
      {key:'position', label:'位置', state:nearSupport ? 'support' : nearResistance ? 'resistance' : 'midrange', ready:!!(nearSupport || nearResistance), summary:positionLabel, evidence:nearSupport ? '距支撑 ' + round(support.distanceAtr, 2) + ' ATR' : nearResistance ? '距压力 ' + round(resistance.distanceAtr, 2) + ' ATR' : '最近可靠结构均超过 1 ATR'},
      {key:'power', label:'力量', state:powerState, ready:powerState !== 'balanced' && (!finite(volume) || volume >= 0.8), summary:powerLabel, evidence:'收盘位置 ' + round(closeLocation * 100, 0) + '% · 实体占振幅 ' + round(bodyRatio * 100, 0) + '%' + (finite(volume) ? ' · 量比 ' + round(volume, 2) : ' · 量能缺失')},
      {key:'confirmation', label:'确认', state:observed >= 2 ? 'confirmed' : observed ? 'counting' : 'waiting', ready:observed >= 2, summary:confirmLabel + ' ' + observed + '/2', evidence:confirmBoundary == null ? '尚无可计数的关键边界' : '完成K线收盘相对 ' + round(confirmBoundary, 2) + ' 计数；不使用形成中K线'}
    ];
    return {
      sequence:['trend','position','power','confirmation'], requiredOrder:true,
      steps:steps,
      reversal123:reversal123 ? {state:reversal123.patternState, label:reversal123.label, triggerPrice:round(reversal123.triggerPrice), invalidationPrice:round(reversal123.invalidationPrice), drawingId:reversal123.id} : null,
      candleQuality:{overlapRatio:round(overlapRatio, 3), lowOverlap:overlapRatio <= 0.35, note:overlapRatio <= 0.35 ? '相邻K线重叠较低，方向表达较清楚；仍需关键位确认。' : '相邻K线重叠偏高，不把单根形态解释为有效入场。'},
      ready:steps.every(function (step) { return step.ready; }),
      summary:'按“趋势→位置→力量→确认”顺序阅读；任一步不足时只显示观察，不把单根K线或单条线解释成买卖信号。',
      marketAction:marketContext.action
    };
  }

  function classifyRegime(rows, atrValue, structure) {
    if (structure) return structure.direction === 'down' ? 'breakdown' : 'breakout';
    var sample = rows.slice(-Math.min(24, rows.length)), xMean = (sample.length - 1) / 2, yMean = mean(sample.map(function (row) { return row.close; })), numerator = 0, denominator = 0;
    sample.forEach(function (row, index) { numerator += (index - xMean) * (row.close - yMean); denominator += Math.pow(index - xMean, 2); });
    var slope = denominator ? numerator / denominator : 0, normalized = atrValue > 0 ? slope / atrValue : 0;
    return normalized >= 0.08 ? 'uptrend' : normalized <= -0.08 ? 'downtrend' : 'range';
  }
  function contextSummaries(contexts) {
    var summaries = [], ignored = [];
    if (!contexts || typeof contexts !== 'object' || Array.isArray(contexts)) return { summaries:summaries, ignored:ignored };
    Object.keys(contexts).sort().forEach(function (frame) {
      var localDiagnostics = [], rows = normalize(contexts[frame], localDiagnostics);
      if (!rows || rows.length < 20) { ignored.push(frame); return; }
      var data = atrSeries(rows, Math.min(14, rows.length - 1)), atrValue = recentAtr(data);
      if (!finite(atrValue) || atrValue <= 0) { ignored.push(frame); return; }
      summaries.push({ timeframe:frame, asOf:rows[rows.length - 1].time, bars:rows.length, currentPrice:round(rows[rows.length - 1].close), atr:round(atrValue), regime:classifyRegime(rows, atrValue, null) });
    });
    return { summaries:summaries, ignored:ignored };
  }
  function focusLevel(item) {
    if (!item) return null;
    return { drawingId:item.id, label:item.label, range:finite(item.low) ? { low:round(item.low), high:round(item.high) } : null, price:finite(item.price) ? round(item.price) : null, projectedPrice:finite(item.projectedPrice) ? round(item.projectedPrice) : null, distanceAtr:round(item.distanceAtr), distancePct:round(item.distancePct, 2), score:item.score, role:item.role };
  }
  function scenarioForLevel(item, side, atrValue) {
    var boundary = side === 'support' ? item.low - atrValue * 0.25 : item.high + atrValue * 0.25;
    return {
      id:'scenario-' + hash(item.id + '|' + side), kind:side === 'support' ? 'support_test' : 'resistance_break', status:'watching', label:side === 'support' ? '支撑有效性观察' : '压力突破观察', relatedDrawingId:item.id,
      observe:{ state:'approaching_' + side, price:round(side === 'support' ? item.high : item.low, 4), note:item.reason },
      confirm:{ rule:side === 'support' ? 'closes_back_above_zone' : 'two_closes_above', price:round(side === 'support' ? item.high : boundary, 4), volumeRatioGte:1, volumeRequired:false },
      invalidate:{ rule:side === 'support' ? 'two_closes_below' : 'close_back_below', price:round(boundary, 4) },
      rationale:item.reason
    };
  }

  function bollinger(rows, period, multiplier) {
    if (!rows || rows.length < period) return null;
    var closes = rows.slice(-period).map(function (row) { return Number(row.close); });
    var middle = mean(closes);
    var deviation = Math.sqrt(mean(closes.map(function (value) { return Math.pow(value - middle, 2); })));
    return {
      period:period,
      stdMultiplier:multiplier,
      ddof:0,
      lower:middle - multiplier * deviation,
      middle:middle,
      upper:middle + multiplier * deviation,
      bandwidth:middle ? multiplier * 2 * deviation / middle * 100 : null
    };
  }

  function scenarioItem(role, data) {
    var item = {
      role:role,
      availability:data.availability || 'unavailable',
      label:data.label,
      priceText:data.priceText || '—',
      geometry:data.geometry || null,
      condition:data.condition || '',
      invalidation:data.invalidation || null,
      reason:data.reason || '',
      source:data.source || 'current_chart_ohlcv',
      evidenceGrade:data.evidenceGrade || 'synthetic_unvalidated',
      focus:data.focus || { visibleBars:80, pricePaddingAtr:1 }
    };
    if (finite(data.referencePrice)) item.referencePrice = round(data.referencePrice, 6);
    return item;
  }

  /*
   * The map is deliberately deterministic: it may combine already-computed
   * chart structure with explicit execution/position gates, but it never lets
   * a language model invent a price coordinate.  "synthetic_unvalidated" is
   * part of the public contract until an A-share out-of-sample study exists.
   */
  function buildDecisionMap(rows, atrData, atrValue, drawings, support, resistance, structure, regime, options, marketContext) {
    var context = options.decisionContext || {}, tradePlan = context.tradePlan || {}, sellPlan = context.sellPlan || {};
    var timing = context.timing || {}, gate = timing.decision_gate || timing.decisionGate || {};
    var last = rows[rows.length - 1], close = Number(last.close), latestAtr = atrData.values.slice().reverse().filter(finite).map(Number)[0] || atrValue;
    var boll = bollinger(rows, 20, 2), hasPosition = sellPlan.available === true && sellPlan.hasPosition === true;
    var structuralScenario = structure && structure.scenario ? structure.scenario : null;
    var higherContexts = Array.isArray(options.contextSummaries) ? options.contextSummaries : [];
    var structureProtocol = buildStructureProtocol(rows, atrValue, regime, drawings, support, resistance, structure, marketContext, higherContexts);
    var sourceIds = ['current_chart_ohlcv'];
    if (higherContexts.length) sourceIds.push('higher_timeframe_ohlcv');
    if (tradePlan.available) sourceIds.push('stock_execution_plan');
    if (sellPlan.available) sourceIds.push('manual_position_ledger');

    var entry;
    var entryGateReady = tradePlan.available === true && tradePlan.state === 'ready' && tradePlan.timingAction === 'buy_now' && gate.recommendation_eligible === true;
    if (hasPosition) {
      entry = scenarioItem('entry', {
        availability:'unavailable', label:'候选买点关闭', priceText:'已有持仓',
        reason:'手工持仓账本已有该股票；系统不把同一场景解释为自动加仓。',
        condition:'如需加仓，必须作为新的独立研究计划重新核验。', focus:{visibleBars:40, pricePaddingAtr:1}
      });
    } else if (entryGateReady && support && finite(tradePlan.entryLimit)) {
      var entryLow = Number(support.low), entryHigh = Math.min(Number(support.high), Number(tradePlan.entryLimit));
      if (entryHigh >= entryLow) {
        entry = scenarioItem('entry', {
          availability:'active', label:'候选买点区', priceText:round(entryLow, 2) + '–' + round(entryHigh, 2),
          geometry:{low:round(entryLow, 6), high:round(entryHigh, 6), kind:'zone'}, referencePrice:tradePlan.entryLimit,
          condition:'只在量化时机闸门通过、唯一执行日且开盘不高于冻结上限时人工复核。',
          invalidation:{basis:'completed_close', price:round(Number((support.evidence || {}).invalidationPrice || support.low), 6)},
          reason:'已确认支撑区与冻结买入上限的交集；不是事后最低点，也不自动成交。',
          source:'structure_and_execution_plan', focus:{visibleBars:40, includeRoles:['entry','risk'], pricePaddingAtr:1}
        });
      }
    }
    if (!entry && structure && structure.direction === 'down') {
      entry = scenarioItem('entry', {
        availability:'locked', label:'买点未成立 · 收回确认区',
        priceText:round(structure.drawing.low, 2) + '–' + round(structure.drawing.high, 2),
        geometry:{low:round(structure.drawing.low, 6), high:round(structure.drawing.high, 6), kind:'conditional_zone'},
        referencePrice:(structuralScenario.confirm || {}).price,
        condition:'需连续 2 根完成K线收盘站上 ' + round((structuralScenario.confirm || {}).price, 2) + '，再重新运行全部量化门禁。',
        invalidation:{basis:'completed_close', price:round((structuralScenario.invalidate || {}).price, 6)},
        reason:'当前已破位且下方没有可靠支撑；该区只是收回条件，不是买入建议。',
        source:'two_close_structural_break', focus:{visibleBars:40, includeRoles:['entry','risk'], pricePaddingAtr:1}
      });
    }
    if (!entry) {
      entry = scenarioItem('entry', {
        availability:'unavailable', label:'买点未成立', priceText:'暂无可靠区域',
        reason:support ? '价格结构存在，但量化时机/执行门禁尚未全部通过。' : '图表基准收盘价下方没有通过近期性、触达和失效检查的支撑。',
        condition:'等待完成K线形成可确认结构，再重新运行量化门禁。', focus:{visibleBars:40, pricePaddingAtr:1}
      });
    }

    var target;
    if (hasPosition && finite(sellPlan.structureObservationLine)) {
      target = scenarioItem('target', {
        availability:'observation', label:'+25% 结构观察线 · 非卖出信号', priceText:round(sellPlan.structureObservationLine, 2),
        geometry:{low:round(sellPlan.structureObservationLine, 6), high:round(sellPlan.structureObservationLine, 6), kind:'line'}, referencePrice:sellPlan.structureObservationLine,
        condition:'到达后只观察完成K线、量价与结构变化；触及本身不生成减仓、兑现或卖出复核。',
        reason:'来自手工持仓账本的 +25% 结构观察线，不是止盈目标。',
        source:'manual_position_ledger', focus:{visibleBars:48, includeRoles:['target'], pricePaddingAtr:1}
      });
    } else if (hasPosition && finite(sellPlan.trailingActivationLine)) {
      target = scenarioItem('target', {
        availability:'observation', label:'+15% 移动风控启用观察线', priceText:round(sellPlan.trailingActivationLine, 2),
        geometry:{low:round(sellPlan.trailingActivationLine, 6), high:round(sellPlan.trailingActivationLine, 6), kind:'line'}, referencePrice:sellPlan.trailingActivationLine,
        condition:'到达后只启用移动风险跟踪；触及本身不生成减仓、兑现或卖出复核。',
        reason:'来自手工持仓账本的 +15% 移动风控启用观察线，不是止盈目标。',
        source:'manual_position_ledger', focus:{visibleBars:48, includeRoles:['target'], pricePaddingAtr:1}
      });
    } else if (boll && boll.middle > close && boll.middle - close <= latestAtr * 4) {
      var targetLow = boll.middle - latestAtr * 0.30, targetHigh = boll.middle + latestAtr * 0.30;
      target = scenarioItem('target', {
        availability:'observation', label:'上方兑现观察区', priceText:round(targetLow, 2) + '–' + round(targetHigh, 2),
        geometry:{low:round(targetLow, 6), high:round(targetHigh, 6), kind:'zone'}, referencePrice:boll.middle,
        condition:'价格回到 BOLL(20,2) 中轨附近时，只观察供需变化；无持仓不触发卖出。',
        reason:'同口径完成K线的 20 日均值 ± 0.30×Wilder ATR14；这是均值回归观察，不是预测目标。',
        source:'bollinger_middle_and_wilder_atr', focus:{visibleBars:48, includeRoles:['target'], pricePaddingAtr:1}
      });
    } else if (resistance && resistance.role === 'resistance') {
      target = scenarioItem('target', {
        availability:'observation', label:'上方压力观察区', priceText:round(resistance.low, 2) + '–' + round(resistance.high, 2),
        geometry:{low:round(resistance.low, 6), high:round(resistance.high, 6), kind:'zone'}, referencePrice:resistance.projectedPrice,
        condition:'到达近期压力区后观察完成K线与量能，不将触碰本身解释为卖出信号。',
        reason:resistance.reason, source:'recent_resistance_cluster', focus:{visibleBars:48, includeRoles:['target'], pricePaddingAtr:1}
      });
    } else {
      target = scenarioItem('target', { availability:'unavailable', label:'兑现区未成立', priceText:'暂无可靠区域', reason:'没有真实持仓止盈线，也没有同口径的近端对向结构。', condition:'不使用预测模型或远端陈旧压力补空。', focus:{visibleBars:48, pricePaddingAtr:1} });
    }

    var risk;
    if (hasPosition && finite(sellPlan.riskLine)) {
      risk = scenarioItem('risk', {
        availability:'active', label:'持仓止损复核线', priceText:round(sellPlan.riskLine, 2),
        geometry:{low:round(sellPlan.riskLine, 6), high:round(sellPlan.riskLine, 6), kind:'line'}, referencePrice:sellPlan.riskLine,
        condition:'触价只提示人工复核；能否成交受涨跌停、跳空和流动性约束。',
        reason:'来自手工持仓账本的有效风险线；不等同保证成交的止损单。',
        source:'manual_position_ledger', focus:{visibleBars:32, includeRoles:['risk'], pricePaddingAtr:0.8}
      });
    } else if (structuralScenario && finite((structuralScenario.invalidate || {}).price)) {
      risk = scenarioItem('risk', {
        availability:'observation', label:'结构失效边界', priceText:round(structuralScenario.invalidate.price, 2),
        geometry:{low:round(structuralScenario.invalidate.price, 6), high:round(structuralScenario.invalidate.price, 6), kind:'line'}, referencePrice:structuralScenario.invalidate.price,
        condition:'以完成K线收盘确认；当前无持仓，因此不称为止损价。',
        reason:'破位收回场景的最新低点失效边界。', source:'two_close_structural_break',
        focus:{visibleBars:32, includeRoles:['risk'], pricePaddingAtr:0.8}
      });
    } else if (support && finite((support.evidence || {}).invalidationPrice)) {
      risk = scenarioItem('risk', {
        availability:'observation', label:'结构失效边界', priceText:round(support.evidence.invalidationPrice, 2),
        geometry:{low:round(support.evidence.invalidationPrice, 6), high:round(support.evidence.invalidationPrice, 6), kind:'line'}, referencePrice:support.evidence.invalidationPrice,
        condition:'连续 2 根完成K线收盘越过才判结构失效。', reason:'来自当前支撑结构的 ATR 容差边界。',
        source:'support_cluster_invalidation', focus:{visibleBars:32, includeRoles:['risk'], pricePaddingAtr:0.8}
      });
    } else risk = scenarioItem('risk', { availability:'unavailable', label:'风险边界未成立', priceText:'—', reason:'没有持仓风险线或可核验结构失效线。', focus:{visibleBars:32, pricePaddingAtr:0.8} });

    var trendDrawing = drawings.filter(function (item) { return item.type === 'trend' && finite(item.distanceAtr) && item.distanceAtr <= 3; })[0] || null;
    var trend = trendDrawing ? {
      state:trendDrawing.kind === 'uptrend' ? 'uptrend' : 'downtrend', label:trendDrawing.label,
      drawingId:trendDrawing.id, points:trendDrawing.points, projectedPrice:round(trendDrawing.projectedPrice, 6),
      distanceAtr:round(trendDrawing.distanceAtr, 3), defaultVisible:trendDrawing.touches >= 3,
      evidence:trendDrawing.evidence, reason:trendDrawing.reason
    } : {
      state:regime, label:regime === 'breakdown' ? '破位状态 · 无合格趋势线' : '当前无合格主趋势线',
      drawingId:null, points:[], projectedPrice:null, distanceAtr:null, defaultVisible:false,
      reason:'不足两组近期、已确认且未被连续收盘破坏的同类拐点时，不强行画趋势线。'
    };
    var volatility = {
      defaultMode:'none', atr:{period:14, smoothing:'wilder_rma', value:round(latestAtr, 6), pct:round(latestAtr / close * 100, 3)},
      structureAtr:round(atrValue, 6),
      boll:boll ? {period:boll.period, stdMultiplier:boll.stdMultiplier, ddof:boll.ddof, lower:round(boll.lower, 6), middle:round(boll.middle, 6), upper:round(boll.upper, 6), bandwidthPct:round(boll.bandwidth, 3)} : null,
      atrChannel:boll ? {basis:'sma20', multiplier:1.5, lower:round(boll.middle - latestAtr * 1.5, 6), middle:round(boll.middle, 6), upper:round(boll.middle + latestAtr * 1.5, 6)} : null,
      warning:'ATR只描述波动，不提供方向；触及布林带也不单独产生买卖信号。'
    };
    var rewardRisk = null, rewardRiskReason = '候选买点尚未激活，不计算盈亏比。';
    if (entry.availability === 'active' && entry.geometry && target.geometry && risk.geometry) {
      var entryPrice = (Number(entry.geometry.low) + Number(entry.geometry.high)) / 2, targetPrice = Number(target.geometry.low), riskPrice = Number(risk.geometry.high);
      if (targetPrice > entryPrice && riskPrice < entryPrice) { rewardRisk = round((targetPrice - entryPrice) / (entryPrice - riskPrice), 2); rewardRiskReason = '按候选区中点、最近目标下沿与风险边界估算；不含滑点和费用。'; }
      else rewardRiskReason = '目标、买点与风险边界的价格顺序不成立。';
    }
    var state = hasPosition ? 'position_management' : entry.availability === 'active' ? 'candidate_ready' : regime === 'breakdown' ? 'risk_review' : 'waiting_confirmation';
    var headline = hasPosition ? '持仓管理 · 只显示真实风险线与非卖出观察线' : entry.availability === 'active' ? '候选条件已齐 · 仍需人工复核' : regime === 'breakdown' ? '阶段破位 · 买点未成立' : marketContext && marketContext.state === 'impulse_channel' && marketContext.impulseOriginRelevant === true ? '急速接通道 · 不追第一次突破，等待起点回踩确认' : '等待结构与量化门禁共振';
    var scenarioByRole = { entry:entry, target:target, risk:risk };
    var preferredRoles = hasPosition ? ['target','risk'] : entry.availability === 'active' ? ['entry','risk','target'] : ['entry','risk'];
    var maxVisibleObjects = hasPosition ? 3 : entry.availability === 'active' ? 4 : 2;
    var chartLookbackBars = hasPosition || entry.availability === 'active' ? 48 : state === 'risk_review' ? 40 : 60;
    var visibleRoles = preferredRoles.filter(function (role) { return scenarioByRole[role] && scenarioByRole[role].geometry; });
    var deferredRoles = ['entry','target','risk'].filter(function (role) { return scenarioByRole[role] && scenarioByRole[role].geometry && visibleRoles.indexOf(role) < 0; });
    var presentation = {
      mode:'decision_lens', primaryRole:visibleRoles[0] || null, visibleRoles:visibleRoles, deferredRoles:deferredRoles,
      chartLookbackBars:chartLookbackBars, maxVisibleObjects:maxVisibleObjects,
      showPrimaryTrend:trend.defaultVisible === true && visibleRoles.length < maxVisibleObjects,
      volatilityOverlayDefault:'none', objectPolicy:'state_driven_minimal'
    };
    var direction = regime === 'uptrend' || regime === 'breakout' ? 'up' : regime === 'downtrend' || regime === 'breakdown' ? 'down' : 'range';
    var contextDirections = higherContexts.map(function (item) { return item.regime === 'uptrend' || item.regime === 'breakout' ? 'up' : item.regime === 'downtrend' || item.regime === 'breakdown' ? 'down' : 'range'; });
    var opposing = contextDirections.filter(function (item) { return direction !== 'range' && item !== 'range' && item !== direction; }).length;
    var supporting = contextDirections.filter(function (item) { return direction !== 'range' && item === direction; }).length;
    var multiTimeframe = {
      status:!higherContexts.length ? 'unavailable' : opposing ? 'conflict' : supporting ? 'aligned' : 'neutral',
      contexts:higherContexts,
      summary:!higherContexts.length ? '上级周期样本不足，当前结构不获得多周期加分。' : opposing ? '上级周期与当前结构方向冲突；坐标仍展示，但仅作观察，不提高证据等级。' : supporting ? '上级周期与当前结构方向一致；这只增强结构解释，不代表胜率或收益。' : '上级周期方向中性；当前结构不获得多周期加分。'
    };
    return {
      methodVersion:VERSION, parameterSetId:'regis-v8-' + (options.sensitivity || 'balanced'), evidenceGrade:'synthetic_unvalidated',
      symbol:options.symbol || null, timeframe:options.timeframe || null, asOf:last.time, causalCutoff:last.time,
      adjustType:options.adjustType || null, priceBasis:context.priceBasis || options.priceBasis || null, barStatus:context.barStatus || null,
      sourceIds:sourceIds, validUntil:tradePlan.validSession || null, state:state, headline:headline, currentPrice:round(close, 6),
      scenarios:[entry, target, risk], trend:trend, multiTimeframe:multiTimeframe, marketContext:marketContext, structureProtocol:structureProtocol, volatility:volatility, presentation:presentation,
      metrics:{ rewardRisk:rewardRisk, rewardRiskReason:rewardRiskReason, currentVolumeRatio:finite(volumeRatio(rows, rows.length - 1)) ? round(volumeRatio(rows, rows.length - 1), 2) : null, timingAction:tradePlan.timingAction || timing.recommended_action || null, gateEligible:gate.recommendation_eligible === true, hasPosition:hasPosition },
      analysis:[
        entry.reason,
        target.reason,
        risk.reason,
        trend.reason,
        multiTimeframe.summary,
        volatility.warning,
        marketContext ? marketContext.summary : ''
      ]
    };
  }

  function analyze(candles, options) {
    options = options || {};
    var diagnostics = [], sensitivity = typeof options.sensitivity === 'string' ? options.sensitivity : 'balanced';
    if (!PROFILES[sensitivity]) { diagnostics.push(diagnostic('warning', 'unknown_sensitivity', '未知 sensitivity，已使用 balanced。', { received:sensitivity })); sensitivity = 'balanced'; }
    var profile = PROFILES[sensitivity], minScore = finite(options.minScore) ? clamp(Number(options.minScore), 0, 100) : profile.minScore, rows = normalize(candles, diagnostics), contexts = contextSummaries(options.contexts);
    var meta = { sensitivity:sensitivity, minScore:minScore, inputBars:Array.isArray(candles) ? candles.length : 0, usedBars:0, timeframe:options.timeframe || null, contextFrames:contexts.summaries.map(function (item) { return item.timeframe; }), ignoredContextFrames:contexts.ignored, maxDrawings:profile.maxDrawings };
    if (!rows) return emptyResult(diagnostics, meta);
    if (rows.length < profile.minBars) { diagnostics.push(diagnostic('error', 'insufficient_bars', 'K 线数量不足，未强行推测结构。', { required:profile.minBars, received:rows.length })); return emptyResult(diagnostics, meta); }
    if (rows.length > profile.lookback) { diagnostics.push(diagnostic('info', 'lookback_trimmed', '仅使用最近 ' + profile.lookback + ' 根 K 线识别当前结构。', { ignored:rows.length - profile.lookback })); rows = rows.slice(-profile.lookback); }
    meta.usedBars = rows.length;
    var atrData = atrSeries(rows, profile.atrPeriod), atrValue = recentAtr(atrData), close = rows[rows.length - 1].close;
    meta.atr = round(atrValue); meta.atrPeriod = profile.atrPeriod; meta.currentPrice = close; meta.asOf = rows[rows.length - 1].time;
    if (!finite(atrValue) || atrValue <= 0) { diagnostics.push(diagnostic('error', 'zero_volatility', '真实波幅为零或不可计算，未生成画线。')); return emptyResult(diagnostics, meta); }
    var swingResult = detectSwings(rows, atrData.values, atrValue, profile), swings = swingResult.points;
    meta.pivotCount = swings.length;
    if (swingResult.ambiguous) diagnostics.push(diagnostic('warning', 'ambiguous_outside_bar', '部分同时创局部高低点的 K 线无法从 OHLCV 判断盘中先后，已跳过。', { count:swingResult.ambiguous }));
    var rejected = { invalid:0, stale:0, distant:0, wrongSide:0, lowScore:0 }, structure = structuralBreak(rows, atrValue, profile), drawings = zoneDrawings(rows, swings, atrValue, profile, rejected);
    var up = fitTrend(rows, swings, 'up', atrValue, profile, rejected), down = fitTrend(rows, swings, 'down', atrValue, profile, rejected), channels = [];
    if (up) { drawings.push(up.drawing); var upChannel = buildChannel(rows, swings, up, atrValue, profile); if (upChannel) channels.push(upChannel); }
    if (down) { drawings.push(down.drawing); var downChannel = buildChannel(rows, swings, down, atrValue, profile); if (downChannel) channels.push(downChannel); }
    var impulseChannel = detectImpulseChannel(rows, swings, atrValue, profile);
    if (impulseChannel) drawings.push(impulseChannel.drawing);
    var fibRetracement = detectFibRetracement(rows, swings, atrValue, profile);
    if (fibRetracement) drawings.push(fibRetracement);
    var reversal123 = detect123Reversal(rows, swings, atrValue, profile);
    if (reversal123) drawings.push(reversal123);
    var hasSupport = drawings.some(function (item) { return item.role === 'support'; });
    var hasResistance = drawings.some(function (item) { return item.role === 'resistance'; });
    if (!(hasSupport && hasResistance) && channels.length) {
      channels.sort(function (a, b) { return a.distanceAtr - b.distanceAtr || b.score - a.score || a.id.localeCompare(b.id); });
      drawings.push(channels[0]);
    }
    if (structure) {
      drawings = drawings.filter(function (item) {
        if (!finite(item.low) || !finite(item.high)) return true;
        return item.high < structure.drawing.low - atrValue * 0.35 || item.low > structure.drawing.high + atrValue * 0.35;
      });
      drawings.unshift(structure.drawing);
    }
    drawings = drawings.filter(function (item) { if (item.score >= minScore) return true; rejected.lowScore += 1; return false; });
    drawings.sort(function (a, b) {
      var priority = { primary:0, secondary:1 };
      return priority[a.priority] - priority[b.priority] || a.distanceAtr - b.distanceAtr || b.score - a.score || a.id.localeCompare(b.id);
    });
    drawings = drawings.slice(0, profile.maxDrawings);
    var support = drawings.filter(function (item) { return item.role === 'support' || item.role === 'breakout_retest'; })[0] || null;
    var resistance = drawings.filter(function (item) { return item.role === 'resistance' || item.role === 'breakdown_reclaim'; })[0] || null;
    var regime = classifyRegime(rows, atrValue, structure), primary;
    if (structure && structure.direction === 'down') primary = '已确认阶段破位；暂无可靠下方支撑，先盯收回区，不把新低猜成支撑。';
    else if (structure && structure.direction === 'up') primary = '已确认阶段突破；先盯回踩区能否守住，不推测后续涨幅。';
    else if (support && support.distanceAtr <= 1) primary = '价格接近支撑观察区，重点看收盘是否守住失效线。';
    else if (resistance && resistance.distanceAtr <= 1) primary = '价格接近压力观察区，重点看能否连续收盘确认突破。';
    else primary = drawings.length ? '只保留仍接近图表基准收盘且近期被验证的结构；远端陈旧画线已过滤。' : '当前没有足够接近且近期有效的结构，系统不强行画线。';
    var scenarios = structure ? [structure.scenario] : [];
    if (impulseChannel) scenarios.push(impulseChannel.scenario);
    if (reversal123 && drawings.some(function (item) { return item.id === reversal123.id; })) scenarios.push({
      id:'scenario-' + hash(reversal123.id + '|123'), kind:'pattern_123', status:reversal123.patternState,
      label:reversal123.label, relatedDrawingId:reversal123.id,
      observe:{state:reversal123.patternState, price:round(close), note:reversal123.reason},
      confirm:{rule:reversal123.kind.indexOf('bullish') >= 0 ? 'close_above' : 'close_below', price:round(reversal123.triggerPrice), volumeRequired:false},
      invalidate:{rule:reversal123.kind.indexOf('bullish') >= 0 ? 'close_below' : 'close_above', price:round(reversal123.invalidationPrice)},
      rationale:reversal123.reason
    });
    if (support && (!structure || support.id !== structure.drawing.id)) scenarios.push(scenarioForLevel(support, 'support', atrValue));
    if (resistance && (!structure || resistance.id !== structure.drawing.id)) scenarios.push(scenarioForLevel(resistance, 'resistance', atrValue));
    scenarios = scenarios.slice(0, 3);
    var currentVr = volumeRatio(rows, rows.length - 1), focus = {
      asOf:rows[rows.length - 1].time, currentPrice:round(close), atr:round(atrValue), currentVolumeRatio:finite(currentVr) ? round(currentVr, 2) : null,
      regime:regime, primary:primary, nearestSupport:focusLevel(support), nearestResistance:focusLevel(resistance), contexts:contexts.summaries
    };
    var marketContext = buildMarketContext(rows, atrValue, regime, support, resistance, impulseChannel);
    var decisionOptions = Object.assign({}, options, {contextSummaries:contexts.summaries});
    var decisionMap = buildDecisionMap(rows, atrData, atrValue, drawings, support, resistance, structure, regime, decisionOptions, marketContext);
    meta.regime = regime; meta.rejected = rejected; meta.outputDrawings = drawings.length; meta.fibRetracement = !!fibRetracement; meta.reversal123 = reversal123 ? reversal123.patternState : null; meta.methodVersion = VERSION; meta.evidenceGrade = 'synthetic_unvalidated';
    diagnostics.push(diagnostic('info', 'current_focus', primary, { regime:regime, currentPrice:round(close), outputDrawings:drawings.length, rejected:rejected }));
    if (!support && (regime === 'breakdown' || regime === 'downtrend')) diagnostics.push(diagnostic('info', 'no_confirmed_support', '图表基准收盘价下方暂无可靠下方支撑；不会用历史远端低点填空。'));
    if (!drawings.length) diagnostics.push(diagnostic('info', 'no_actionable_structure', '没有达到当前距离、近期性与证据门槛的结构，未强行生成画线。', { pivots:swings.length, minScore:minScore }));
    marketContext.flags.forEach(function (item) { diagnostics.push(diagnostic(item.level, item.code, item.text)); });
    return { version:VERSION, drawings:drawings, focus:focus, scenarios:scenarios, marketContext:marketContext, decisionMap:decisionMap, diagnostics:diagnostics, meta:meta };
  }

  return { VERSION:VERSION, analyze:analyze };
});
