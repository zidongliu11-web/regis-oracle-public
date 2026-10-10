// ═══════════════════════════════════════════════════════════════
// position_alert.js v2 — 全局持仓卖出风控监控
// 全站页面（base.html 各页 + spa.html 仪表盘）常驻。
// 前台每 10s 轮询 /api/timing/portfolio，仅当服务端 trade_plan 给出
// 精确卖出数量的 EXIT_REVIEW / REDUCE_REVIEW 时，触发桌面通知
// （Notification API）+ 常驻提醒横幅。纯技术观察不冒充卖出提醒。
// 只提醒不自动卖：人工确认后才调用 /api/timing/sell。
// 自包含：不依赖页面 api/$ 助手，注入自己的样式。
// ═══════════════════════════════════════════════════════════════
(function () {
  'use strict';
  // Research pages are account-independent, including inherited shell scripts.
  // Exit before preferences, cross-tab leadership or any account polling.
  if (document.getElementById('institutionalConsole') || document.getElementById('strategyResearchConsole') || document.getElementById('accountIndependentDataCenter') || document.getElementById('accountIndependentFactorResearch') ||
      (document.body && document.body.getAttribute('data-local-research-preview') === 'true')) {
    var researchPreference = document.getElementById('alertPrefsButton');
    if (researchPreference) {
      researchPreference.textContent = '账户读取关闭 · 仅研究';
      researchPreference.disabled = true;
      researchPreference.setAttribute('aria-pressed', 'false');
    }
    var researchDisable = document.getElementById('alertPrefsDisableButton');
    if (researchDisable) researchDisable.hidden = true;
    return;
  }
  if (window.__positionAlertStarted) return;
  window.__positionAlertStarted = true;

  var POLL_MS = 10000;
  var BACKGROUND_POLL_MS = 60000;
  var STORE_KEY = 'regis.position_alert_v1';
  var ENABLE_KEY = 'regis.alerts_enabled';
  var LEADER_KEY = 'regis.position_alert_leader_v1';
  var LEADER_LEASE_MS = 45000;
  var LEADER_RENEW_MS = 15000;
  var _tabId = createTabId();
  var _isLeader = false;
  var _leaderTimer = null;
  var _notified = loadSeen();
  var _pollController = null;
  var _pollSequence = 0;
  var _pollTimer = null;
  var _resumeSequence = 0;
  var _pageLeaving = false;
  var _audioState = 'idle';
  var _notificationError = false;
  var _marketSession = null;
  var _lastOpportunity = null;
  var _lastStrategyRoute = null;
  var _lastPortfolio = null;
  var _lastAllocation = null;
  var _activeSellCount = 0;
  var _activeBuyCount = 0;
  var _shownOverlays = Object.create(null);

  function reportIssue(scope, error) {
    console.warn('[PositionAlert ' + scope + ']', error && error.message ? error.message : error);
  }

  function visibleFeedback(message, kind) {
    var toast = window.RegisSpa && window.RegisSpa.core && window.RegisSpa.core.toast;
    if (typeof toast === 'function') toast(message, kind || 'info');
    else setPill(message, kind === 'error');
  }

  function openManualRecord(item) {
    var open = window.RegisSpa && window.RegisSpa.core && window.RegisSpa.core.openAlertConfirm;
    if (typeof open !== 'function') {
      visibleFeedback('人工成交记录窗口未就绪，请刷新页面', 'error');
      return false;
    }
    open(item);
    return true;
  }

  function createTabId() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
    } catch (error) { reportIssue('tab-identity', error); }
    return 'tab-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  }

  function readLeaderLease() {
    try {
      var value = JSON.parse(localStorage.getItem(LEADER_KEY) || 'null');
      if (!value || typeof value.id !== 'string' || !Number.isFinite(value.expires_at)) {
        return null;
      }
      return value;
    } catch (error) {
      reportIssue('read-leader', error);
      return null;
    }
  }

  function writeLeaderLease() {
    try {
      localStorage.setItem(LEADER_KEY, JSON.stringify({
        id: _tabId,
        expires_at: Date.now() + LEADER_LEASE_MS,
      }));
      var confirmed = readLeaderLease();
      return !!(confirmed && confirmed.id === _tabId);
    } catch (error) {
      reportIssue('write-leader', error);
      return false;
    }
  }

  function updateLeadership() {
    if (_pageLeaving) return false;
    var now = Date.now();
    var lease = readLeaderLease();
    var mayAcquire = !lease || lease.id === _tabId || lease.expires_at <= now;
    var next = mayAcquire && writeLeaderLease();
    if (next === _isLeader) return _isLeader;
    _isLeader = next;
    if (_isLeader) schedulePolling();
    else stopPolling();
    return _isLeader;
  }

  function releaseLeadership() {
    var lease = readLeaderLease();
    if (lease && lease.id === _tabId) {
      try { localStorage.removeItem(LEADER_KEY); }
      catch (error) { reportIssue('release-leader', error); }
    }
    _isLeader = false;
  }

  function startLeadershipHeartbeat() {
    if (_leaderTimer) window.clearInterval(_leaderTimer);
    _leaderTimer = window.setInterval(updateLeadership, LEADER_RENEW_MS);
  }

  function stopLeadershipHeartbeat() {
    if (_leaderTimer) window.clearInterval(_leaderTimer);
    _leaderTimer = null;
  }

  // ── 小工具（不依赖外部助手） ──────────────────────────
  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }
  function todayKey() {
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());
  }
  function loadSeen() {
    try {
      var raw = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
      // 只保留最近 7 天，避免无限膨胀
      var cutoff = Date.now() - 7 * 86400000;
      var out = {};
      Object.keys(raw).forEach(function (k) {
        if ((raw[k].ts || 0) >= cutoff) out[k] = raw[k];
      });
      return out;
    } catch (e) { reportIssue('load-preferences', e); return {}; }
  }
  function saveSeen() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(_notified)); }
    catch (e) { reportIssue('save-preferences', e); }
  }
  function mark(k, state) {
    _notified[k] = { state: state, ts: Date.now() };
    saveSeen();
  }
  function dedupKey(code, action) { return code + '|' + action + '|' + todayKey(); }

  function alertsEnabled() {
    try { return localStorage.getItem(ENABLE_KEY) === '1'; }
    catch (e) { reportIssue('read-enable', e); return false; }
  }
  function ensureDefaultPreference() {
    try {
      if (localStorage.getItem(ENABLE_KEY) === null) localStorage.setItem(ENABLE_KEY, '1');
    } catch (e) { reportIssue('write-enable-default', e); }
  }
  function audioStateLabel() {
    if (_audioState === 'ready') return '声音就绪';
    if (_audioState === 'unsupported') return '声音不支持';
    if (_audioState === 'error') return '声音错误';
    return '声音待启用';
  }
  function notificationStateLabel() {
    if (_notificationError) return '通知错误';
    if (!('Notification' in window)) return '通知不支持';
    if (Notification.permission === 'granted') return '通知就绪';
    if (Notification.permission === 'denied') return '通知已拒绝';
    return '通知待授权';
  }
  function alertCapabilitiesReady() {
    return _audioState === 'ready' && !_notificationError &&
      ('Notification' in window) && Notification.permission === 'granted';
  }
  function prepareAudioContext() {
    var AudioCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtor) {
      _audioState = 'unsupported';
      return Promise.resolve(false);
    }
    try {
      var context = window.__regisAlertAudioContext || new AudioCtor();
      window.__regisAlertAudioContext = context;
      var resumed = context.state === 'suspended' ? context.resume() : Promise.resolve();
      return Promise.resolve(resumed).then(function () {
        _audioState = context.state === 'running' ? 'ready' : 'error';
        return _audioState === 'ready';
      }).catch(function (error) {
        _audioState = 'error';
        reportIssue('audio-resume', error);
        return false;
      });
    } catch (error) {
      _audioState = 'error';
      reportIssue('audio-create', error);
      return Promise.resolve(false);
    }
  }
  function requestNotificationAccess() {
    if (!('Notification' in window)) return Promise.resolve(false);
    _notificationError = false;
    if (Notification.permission !== 'default') {
      return Promise.resolve(Notification.permission === 'granted');
    }
    try {
      return Promise.resolve(Notification.requestPermission()).then(function (permission) {
        return permission === 'granted';
      }).catch(function (error) {
        _notificationError = true;
        reportIssue('notification-permission', error);
        return false;
      });
    } catch (error) {
      _notificationError = true;
      reportIssue('notification-permission', error);
      return Promise.resolve(false);
    }
  }
  function updatePreferenceButton() {
    var button = document.getElementById('alertPrefsButton');
    var disableButton = document.getElementById('alertPrefsDisableButton');
    if (!button) return;
    var enabled = alertsEnabled();
    button.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    button.textContent = !enabled
      ? '开启提醒'
      : (alertCapabilitiesReady() ? audioStateLabel() + ' · ' + notificationStateLabel()
        : '提醒已开启 · ' + audioStateLabel() + ' · ' + notificationStateLabel());
    if (disableButton) disableButton.hidden = !enabled;
    if (typeof _panel !== 'undefined' && _panel) refreshPill();
  }
  function setAlertsEnabled(enabled) {
    try { localStorage.setItem(ENABLE_KEY, enabled ? '1' : '0'); }
    catch (e) { reportIssue('write-enable', e); }
    updatePreferenceButton();
    window.dispatchEvent(new CustomEvent('regis-alert-preference', { detail: { enabled: enabled } }));
  }
  function bindPreferenceButton() {
    var button = document.getElementById('alertPrefsButton');
    var disableButton = document.getElementById('alertPrefsDisableButton');
    if (!button) return;
    updatePreferenceButton();
    button.addEventListener('click', function () {
      // Persisted intent and runtime capability are separate.  After a reload
      // AudioContext is idle again; the next trusted click must retry it, not
      // accidentally turn the user's preference off.
      if (!alertsEnabled()) setAlertsEnabled(true);
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      // Both calls begin synchronously inside the trusted click gesture.
      var audioReady = prepareAudioContext();
      var notificationReady = requestNotificationAccess();
      Promise.all([audioReady, notificationReady]).finally(function () {
        button.disabled = false;
        button.removeAttribute('aria-busy');
        updatePreferenceButton();
      });
    });
    if (disableButton) disableButton.addEventListener('click', function () {
      setAlertsEnabled(false);
    });
  }
  function prepareDefaultCapabilities() {
    if (!alertsEnabled()) return;
    var existing = window.__regisAlertAudioContext;
    if (existing && existing.state === 'running') _audioState = 'ready';
    updatePreferenceButton();
    function unlockOnFirstInteraction() {
      // Browsers forbid autoplay and permission prompts before a trusted user
      // gesture.  The preference is still on by default; the first ordinary
      // click or key press unlocks sound and requests the browser-owned grant.
      Promise.all([prepareAudioContext(), requestNotificationAccess()]).finally(
        updatePreferenceButton
      );
      document.removeEventListener('pointerdown', unlockOnFirstInteraction, true);
      document.removeEventListener('keydown', unlockOnFirstInteraction, true);
    }
    document.addEventListener('pointerdown', unlockOnFirstInteraction, true);
    document.addEventListener('keydown', unlockOnFirstInteraction, true);
  }
  function fireNotif(title, body, tag) {
    try {
      if (!alertsEnabled() || !('Notification' in window)) return false;
      if (Notification.permission === 'granted') {
        new Notification(title, { body: body, tag: tag || 'pos-' + Date.now() });
        return true;
      }
      return false;
    } catch (e) {
      _notificationError = true;
      reportIssue('notification', e);
      updatePreferenceButton();
      return false;
    }
  }
  function playAlertTone(kind) {
    if (!alertsEnabled()) return false;
    var context = window.__regisAlertAudioContext;
    if (!context || context.state !== 'running') return false;
    try {
      var start = context.currentTime;
      var oscillator = context.createOscillator();
      var gain = context.createGain();
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(kind === 'sell' ? 660 : 880, start);
      oscillator.frequency.exponentialRampToValueAtTime(
        kind === 'sell' ? 440 : 1320, start + 0.22
      );
      gain.gain.setValueAtTime(0.05, start);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.22);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start(start);
      oscillator.stop(start + 0.27);
      return true;
    } catch (error) {
      _audioState = 'error';
      reportIssue('audio-play', error);
      updatePreferenceButton();
      return false;
    }
  }
  function deliverAlert(title, body, tag, kind) {
    // 声音与系统通知是并行能力；任一成功即视为本次已投递，避免轮询重复鸣响。
    var sounded = playAlertTone(kind);
    var notified = fireNotif(title, body, tag);
    return sounded || notified;
  }

  // ── 横幅 ─────────────────────────────────────────────
  var _panel = null, _statusPill = null;
  function ensurePanel() {
    if (_panel) return _panel;
    var style = document.createElement('style');
    style.textContent = [
      '#regisPosAlert{position:fixed;top:64px;right:16px;z-index:99999;width:340px;max-width:calc(100vw - 32px);',
      '  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;',
      '  display:flex;flex-direction:column;gap:8px;pointer-events:none}',
      '#regisPosAlert .rpa-card{pointer-events:auto;background:#1e1e24;border:1px solid #3a3a46;border-radius:12px;',
      '  padding:12px 14px;box-shadow:0 10px 30px rgba(0,0,0,.45);animation:rpaIn .25s ease-out}',
      '#regisPosAlert .rpa-card.urgent{border-color:#e5484d;background:#2a1a1c;box-shadow:0 0 0 1px #e5484d66,0 10px 30px rgba(0,0,0,.5)}',
      '#regisPosAlert .rpa-card.decision{border-color:#4d6387;background:#182130}',
      '#regisPosAlert .rpa-top{display:flex;align-items:center;gap:8px;font-size:12px;color:#e8e8ee}',
      '#regisPosAlert .rpa-badge{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px}',
      '#regisPosAlert .rpa-badge.leave{background:#e5484d;color:#fff}',
      '#regisPosAlert .rpa-badge.trim{background:#f5a623;color:#1a1a1f}',
      '#regisPosAlert .rpa-badge.buy{background:#F6465D;color:#fff}',
      '#regisPosAlert .rpa-badge.wait{background:#314563;color:#dce9ff}',
      '#regisPosAlert .rpa-name{font-weight:600;font-size:14px;color:#fff}',
      '#regisPosAlert .rpa-code{color:#9a9aa6;font-size:12px}',
      '#regisPosAlert .rpa-reason{font-size:12px;color:#c8c8d2;line-height:1.5;margin:6px 0 8px}',
      '#regisPosAlert .rpa-as-of{font-size:12px;color:#8f8f9c;margin:-3px 0 8px}',
      '#regisPosAlert .rpa-sig{font-size:12px;color:#8f8f9c;display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}',
      '#regisPosAlert .rpa-sig span{background:#2a2a34;border:1px solid #3a3a46;border-radius:6px;padding:1px 7px}',
      '#regisPosAlert .rpa-actions{display:flex;gap:8px}',
      '#regisPosAlert .rpa-btn{flex:1;border:0;border-radius:8px;padding:8px 0;font-size:13px;cursor:pointer;font-weight:600}',
      '#regisPosAlert .rpa-view{background:#3b6ef6;color:#fff}',
      '#regisPosAlert .rpa-dismiss{background:#33333f;color:#c8c8d2}',
      '#regisPosAlert .rpa-dismiss:hover{background:#3f3f4d}',
      '#regisPosAlert .rpa-pill{pointer-events:auto;display:flex;align-items:center;gap:8px;font-size:12px;color:#8f8f9c;',
      '  background:rgba(30,30,36,.85);border:1px solid #3a3a46;border-radius:99px;padding:6px 12px;margin-left:auto;',
      '  -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}',
      '#regisPosAlert .rpa-dot{width:8px;height:8px;border-radius:50%;background:#4ade80;animation:rpaPulse 2s infinite}',
      '#regisPosAlert .rpa-dot.warn{background:#f5a623}',
      '@keyframes rpaIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}',
      '@keyframes rpaPulse{0%,100%{opacity:1}50%{opacity:.35}}',
    ].join('\n');
    document.head.appendChild(style);

    _panel = document.createElement('div');
    _panel.id = 'regisPosAlert';
    _panel.setAttribute('aria-live', 'polite');
    document.body.appendChild(_panel);

    _statusPill = document.createElement('div');
    _statusPill.className = 'rpa-pill';
    _panel.appendChild(_statusPill);
    return _panel;
  }
  function setPill(text, warn) {
    var panel = ensurePanel();
    panel.hidden = false;
    var p = panel.querySelector('.rpa-pill');
    if (!p) return;
    p.innerHTML = '<span class="rpa-dot' + (warn ? ' warn' : '') + '"></span>' + esc(text);
  }

  function jointDecisionSummary() {
    var workspace = (_lastStrategyRoute || {}).today_decision_workspace || {};
    var review = workspace.current_slot_research_review ||
      (_lastStrategyRoute || {}).current_slot_research_review || {};
    var sectors = (workspace.confirmed_sectors || []).map(function (row) {
      return row && (row.name || row.code);
    }).filter(Boolean);
    var playbooks = (workspace.eligible_playbooks || []).map(function (row) {
      return row && typeof row === 'object' ? (row.label || row.id) : row;
    }).filter(Boolean);
    var strictReady = workspace.strict_forward_packet_valid === true;
    var coreReady = review.valid === true;
    var actionable = strictReady && workspace.actionable_now === true &&
      (workspace.authority || {}).generates_buy_or_sell_alert === true;
    return {
      strictReady: strictReady,
      coreReady: coreReady,
      actionable: actionable,
      pill: actionable ? '整套情绪联合判定·有待复核信号'
        : strictReady ? '整套情绪联合判定·当前不交易'
        : coreReady ? '整套情绪已判定·账户/策略门未通过·当前不交易'
        : '整套情绪联合核验中·当前不交易',
      facts: '情绪 ' + (workspace.market_state || '待核验') +
        ' · 主线 ' + (sectors.join('、') || '待同槽确认') +
        ' · 战法 ' + (playbooks.join('、') || '无准入战法'),
      reason: workspace.reason || workspace.execution_route_reason ||
        workspace.headline || '等待严格联合决策包',
    };
  }

  function removeCards(action) {
    ensurePanel().querySelectorAll('.rpa-card[data-action="' + action + '"]').forEach(function (card) { card.remove(); });
  }
  function renderNoBuyDecision() {
    var joint = jointDecisionSummary();
    var workspace = (_lastStrategyRoute || {}).today_decision_workspace || {};
    var review = workspace.current_slot_research_review ||
      (_lastStrategyRoute || {}).current_slot_research_review || {};
    if (!_marketSession || _marketSession.is_open !== true ||
        !joint.coreReady || joint.actionable || _activeBuyCount > 0) {
      removeCards('decision');
      return;
    }
    var identity = String(review.packet_id || review.decision_at || todayKey());
    var key = dedupKey('market-decision', 'no-buy:' + identity);
    var panel = ensurePanel();
    var card = panel.querySelector('.rpa-card[data-action="decision"]');
    if (!card) {
      card = document.createElement('div');
      card.className = 'rpa-card decision';
      card.setAttribute('data-action', 'decision');
      panel.insertBefore(card, _statusPill);
    }
    card.setAttribute('data-key', key);
    var asOf = String(review.decision_at || '当前时槽').replace('T', ' ').slice(0, 19);
    card.innerHTML = [
      '<div class="rpa-top"><span class="rpa-badge wait">联合决策</span>',
      '<span class="rpa-name">当前无可验证买入点</span></div>',
      '<div class="rpa-reason">' + esc(joint.facts) + '<br>' +
        esc(joint.reason) + '<br>结论：当前不买入；这不是漏报，也不构成买卖信号。</div>',
      '<div class="rpa-as-of">决策时点 ' + esc(asOf) + '</div>',
    ].join('');
    var record = _notified[key];
    if (!record || record.state !== 'notified') {
      var ok = fireNotif(
        '整套情绪联合决策：当前不买入',
        (joint.facts + '；' + joint.reason).slice(0, 180),
        'regis-no-buy-' + identity
      );
      if (ok) mark(key, 'notified');
    }
  }
  function refreshPill() {
    var capability = !alertsEnabled() ? ' · 提醒已关闭' : alertCapabilitiesReady()
      ? ' · 声音/通知已就绪'
      : ' · 站内提醒已开启 · ' + audioStateLabel() + ' · ' + notificationStateLabel();
    if (!_marketSession || _marketSession.is_open !== true) {
      ensurePanel().hidden = true;
      return;
    }
    var total = _activeSellCount + _activeBuyCount;
    var joint = jointDecisionSummary();
    setPill(total ? '买卖提醒·' + total + ' 个需人工复核' + capability
      : '买卖提醒·' + joint.pill + ' · 0 信号' + capability,
    total > 0 || !joint.strictReady);
    var pill = ensurePanel().querySelector('.rpa-pill');
    if (pill) pill.title = joint.facts + '；' + joint.reason;
  }
  function sellPlanLabel(position) {
    var plan = position.trade_plan || {}, action = plan.action || {};
    var shares = Number(action.shares) || 0;
    return plan.state === 'EXIT_REVIEW'
      ? '离场复核 · ' + shares + '股'
      : '减仓复核 · ' + shares + '股 → ' + (Number(action.target_shares) || 0) + '股';
  }
  function sellDecisionLabel(item) {
    var action = (item.trade_plan || {}).action || {};
    var evidence = action.evidence_identity || {};
    var provenance = (item.alert || {}).provenance || item.alert_provenance || {};
    if (evidence.method === 'manual_exit_intent_v1' ||
        provenance.origin === 'manual_exit_intent') return '你的退出计划';
    if (provenance.origin === 'production_strategy') return '策略退出提醒';
    if (evidence.risk_timing_role === 'loss_containment_review_not_best_exit_point' ||
        provenance.origin === 'position_risk_review') return '风险线复核 · 非最佳卖点';
    return '人工卖出复核';
  }
  function planDetailText(item) {
    var plan = item.trade_plan || {}, action = plan.action || {};
    var allocation = item.allocation || {};
    var shares = Number(action.shares), target = Number(action.target_shares);
    var limitPrice = Number(action.limit_price), cost = Number(action.estimated_cost);
    var cash = Number(allocation.estimated_buy_cash_yuan);
    var targetWeight = Number(allocation.target_weight_pct);
    var liquidity = Number(allocation.liquidity_capacity_yuan);
    var plannedLoss = Number(allocation.planned_loss_at_risk_line_yuan);
    var plannedLossPct = Number(allocation.planned_loss_pct_of_equity);
    var evidence = action.evidence_identity || {};
    var joint = jointDecisionSummary();
    var riskLine = allocation.risk_line || ((allocation.levels || {}).effective_risk_level) || (
      Number.isFinite(Number(evidence.effective_stop))
        ? {kind:'hard_stop', value:Number(evidence.effective_stop)} : {}
    );
    var riskPrice = Number(riskLine.value);
    var parts = [];
    if (action.side === 'BUY' && Number.isInteger(shares) && shares > 0) {
      parts.push('建议买入 ' + shares + ' 股');
    } else if (action.side === 'SELL' && Number.isInteger(shares) && shares > 0) {
      parts.push('建议卖出 ' + shares + ' 股');
      if (Number.isInteger(target) && target >= 0) parts.push('卖后 ' + target + ' 股');
    }
    if (Number.isFinite(limitPrice) && limitPrice > 0) parts.push('限价参考 ¥' + limitPrice.toFixed(2));
    if (Number.isFinite(cash) && cash > 0) parts.push('预计现金占用 ¥' + cash.toFixed(2));
    if (Number.isFinite(cost) && cost >= 0) parts.push('预计成本 ¥' + cost.toFixed(2));
    if (Number.isFinite(targetWeight) && targetWeight >= 0) parts.push('目标仓位 ' + targetWeight.toFixed(2) + '%');
    if (Number.isFinite(liquidity) && liquidity > 0) parts.push('流动性上限 ¥' + Math.round(liquidity));
    if (Number.isFinite(riskPrice) && riskPrice > 0) parts.push((riskLine.kind === 'hard_stop' ? '硬风险线' : '风险线') + ' ¥' + riskPrice.toFixed(2));
    var costBasis = Number(item.position && item.position.price);
    if (Number.isFinite(costBasis) && costBasis > 0) parts.push('持仓成本 ¥' + costBasis.toFixed(3));
    if (Number.isFinite(plannedLoss) && plannedLoss >= 0 && Number.isFinite(plannedLossPct)) {
      parts.push('到风险线计划损失约 ¥' + plannedLoss.toFixed(2) + '（账户' + plannedLossPct.toFixed(2) + '%）');
    }
    if (action.playbook_id) parts.push('战法 ' + String(action.playbook_id));
    if (action.strategy_id) parts.push('策略 ' + String(action.strategy_id) + (action.strategy_version ? ' · ' + String(action.strategy_version) : ''));
    if (action.side === 'BUY') parts.push('联合依据 ' + joint.facts);
    else if (action.side === 'SELL') parts.push('市场环境旁证 ' + joint.facts + '（卖出仍由硬风险或退出计划触发）');
    if (action.valid_until) parts.push('有效至 ' + String(action.valid_until).replace('T', ' ').slice(0, 19));
    return parts.join(' · ');
  }
  function maybeShowOverlay(item, key) {
    if (_shownOverlays[key] || typeof window.showAlertOverlay !== 'function') return;
    if (window.showAlertOverlay(item) !== false) _shownOverlays[key] = true;
  }
  function sellAlertItem(position) {
    var plan = position.trade_plan || {}, action = plan.action || {};
    var referencePrice = Number(action.limit_price || position.current);
    return Object.assign({}, position, {
      price: Number.isFinite(referencePrice) && referencePrice > 0
        ? referencePrice : position.price,
      position: { id:position.id, shares:position.shares, price:position.price },
      alert: {
        id: position.alert && position.alert.id,
        status:'triggered', action:'sell',
        reason:(position.alert && position.alert.reason) || plan.reason || '',
        updated_at: position.alert && position.alert.updated_at,
        risk_observation: position.alert && position.alert.risk_observation,
        countdown_seconds: position.alert && position.alert.countdown_seconds != null
          ? position.alert.countdown_seconds
          : ((plan.action || {}).countdown_seconds ?? null),
        provenance: position.alert_provenance || null,
      },
    });
  }

  function portfolioWithAllocationPlans(portfolio, allocation) {
    if (!portfolio || !Array.isArray(portfolio.open) || !allocation ||
        !Array.isArray(allocation.items)) return portfolio;
    var plans = Object.create(null);
    allocation.items.forEach(function (row) {
      var plan = row && row.trade_plan, action = plan && plan.action;
      var shares = action && Number(action.shares);
      if (row && plan && ['EXIT_REVIEW', 'REDUCE_REVIEW'].indexOf(plan.state) >= 0 &&
          action && action.side === 'SELL' && Number.isInteger(shares) && shares > 0) {
        if (!plan.sellable_inventory || plan.sellable_inventory.status !== 'current') return;
        plans[String(row.code)] = {plan:plan, allocation:row};
      }
    });
    return Object.assign({}, portfolio, {open:portfolio.open.map(function (position) {
      var existing = position && position.trade_plan;
      var existingAction = existing && existing.action;
      var existingShares = existingAction && Number(existingAction.shares);
      var existingExact = existing && ['EXIT_REVIEW', 'REDUCE_REVIEW'].indexOf(existing.state) >= 0 &&
        existingAction && existingAction.side === 'SELL' && Number.isInteger(existingShares) && existingShares > 0;
      var allocationPlan = plans[String(position.code)];
      if (!allocationPlan) return position;
      return Object.assign({}, position, {
        trade_plan: existingExact ? existing : allocationPlan.plan,
        allocation: allocationPlan.allocation,
      });
    })});
  }

  function portfolioWithStoredAlerts(portfolio, opportunity) {
    if (!portfolio || !Array.isArray(portfolio.open) || !opportunity ||
        !Array.isArray(opportunity.position_alerts)) return portfolio;
    var activeByCode = Object.create(null);
    opportunity.position_alerts.forEach(function (row) {
      var alert = row && row.alert;
      if (row && alert && alert.status === 'triggered' && alert.action === 'sell' &&
          Number.isInteger(Number(alert.id)) && Number(alert.id) > 0) {
        activeByCode[String(row.code)] = {
          alert:alert,
          trade_plan:row.trade_plan && typeof row.trade_plan === 'object'
            ? row.trade_plan : null,
        };
      }
    });
    return Object.assign({}, portfolio, {open:portfolio.open.map(function (position) {
      var stored = activeByCode[String(position.code)];
      return stored ? Object.assign({}, position, {
        alert:stored.alert,
        trade_plan:stored.trade_plan || position.trade_plan,
      }) : position;
    })});
  }

  // ── 核心：处理一次 /api/timing/portfolio 快照 ─────────
  function handlePortfolio(d) {
    if (!d || !Array.isArray(d.open)) {
      _marketSession = null;
      _activeSellCount = 0;
      _activeBuyCount = 0;
      removeCards('sell');
      removeCards('buy');
      removeCards('decision');
      refreshPill();
      return;
    }
    _marketSession = d.market_session || null;
    if (d.alerts_active !== true || !_marketSession || _marketSession.is_open !== true) {
      _activeSellCount = 0;
      _activeBuyCount = 0;
      removeCards('sell');
      removeCards('buy');
      removeCards('decision');
      refreshPill();
      return;
    }
    var actionable = d.open.filter(function (p) {
      var plan = p && p.trade_plan;
      var action = plan && plan.action;
      var shares = action && Number(action.shares);
      return plan && ['EXIT_REVIEW', 'REDUCE_REVIEW'].indexOf(plan.state) >= 0 &&
        action && action.side === 'SELL' && Number.isInteger(shares) && shares > 0 &&
        Boolean(action.playbook_id) && Boolean(action.strategy_id) &&
        Boolean(action.strategy_version) && Boolean(action.evidence_grade) &&
        Boolean(action.as_of) && Boolean(action.evidence_identity);
    });
    _activeSellCount = actionable.length;
    if (!actionable.length) {
      removeCards('sell');
      refreshPill();
      if (_lastOpportunity) handleOpportunitySnapshot(_lastOpportunity);
      return;
    }
    // 有信号 → 全部展示横幅；未通知过的触发桌面通知
    var activeKeys = {};
    actionable.forEach(function (p) {
      var plan = p.trade_plan || {};
      var act = sellPlanLabel(p);
      var alertItem = sellAlertItem(p);
      var decisionLabel = sellDecisionLabel(alertItem);
      var k = opportunityKey(alertItem, 'sell:' + act);
      activeKeys[k] = true;
      var rec = _notified[k];
      var dismissed = rec && rec.state === 'dismissed';
      if (!dismissed) {
        renderCard(p, k);
        maybeShowOverlay(alertItem, k);
      }
      if (dismissed || (rec && rec.state === 'notified')) return;
      var ok = deliverAlert(
        decisionLabel + '：' + (p.name || p.code) + ' · ' + act,
        (jointDecisionSummary().facts + '；' + (plan.reason || '')).slice(0, 180),
        'regis-sell-' + (((p.alert || {}).id) || p.code),
        'sell'
      );
      if (ok) mark(k, 'notified');
    });
    ensurePanel().querySelectorAll('.rpa-card[data-action="sell"]').forEach(function (card) {
      if (!activeKeys[card.dataset.key]) card.remove();
    });
    refreshPill();
    if (_lastOpportunity) handleOpportunitySnapshot(_lastOpportunity);
  }

  function renderCard(p, key) {
    var panel = ensurePanel();
    // 服务端精确提醒身份优先；同代码同数量的新提醒必须替换旧卡并重新通知。
    var existing = Array.prototype.find.call(panel.querySelectorAll('.rpa-card[data-action="sell"]'), function (node) {
      return node.dataset.key === key;
    });
    var plan = p.trade_plan || {};
    var decisionReason = (p.alert && p.alert.reason) || plan.reason || '';
    var planDetails = planDetailText(p);
    var act = sellPlanLabel(p);
    var decisionLabel = sellDecisionLabel(sellAlertItem(p));
    var urgent = decisionLabel === '策略退出提醒' || (
      decisionLabel.indexOf('风险线复核') === 0 &&
      ((p.risk_observation || {}).episode_kind === 'fresh_intraday_cross')
    );
    var sigs = (plan.observations || []).map(function (s) { return esc(s.label || s.detail); }).filter(Boolean);
    var card = existing || document.createElement('div');
    card.className = 'rpa-card' + (urgent ? ' urgent' : '');
    card.setAttribute('data-code', p.code);
    card.setAttribute('data-action', 'sell');
    card.setAttribute('data-key', key);
    var pnl = (p.pnl_pct != null)
      ? (p.pnl_pct > 0 ? ' +' : ' ') + Number(p.pnl_pct).toFixed(1) + '%'
      : '';
    card.innerHTML = [
      '<div class="rpa-top">',
      '  <span class="rpa-badge ' + (urgent ? 'leave' : 'trim') + '">' + esc(decisionLabel) + '</span>',
      '  <span class="rpa-name">' + esc(p.name || p.code) + '</span>',
      '  <span class="rpa-code">' + esc(p.code) + '</span>',
      '  <span class="rpa-code">' + esc(act) + '</span>',
      '  <span style="margin-left:auto;color:' + (p.pnl_pct > 0 ? '#4ade80' : '#f87171') + '">' + pnl + '</span>',
      '</div>',
      sigs.length ? '<div class="rpa-sig">' + sigs.map(function (x) { return '<span>' + x + '</span>'; }).join('') + '</div>' : '',
      '<div class="rpa-reason">' + (planDetails ? esc(planDetails) + '<br>' : '') +
        (plan.action && plan.action.cancel_condition ? '失效条件：' + esc(plan.action.cancel_condition) + '<br>' : '') +
        esc(decisionReason) + '<br>本站不连接券商，请先人工复核并完成成交。</div>',
      '<div class="rpa-as-of">行情时点 ' + esc(String((plan.action || {}).as_of || p.quote_as_of || '未提供').replace('T', ' ').slice(0, 19)) + '</div>',
      '<div class="rpa-actions">',
      '  <button type="button" class="rpa-btn rpa-view" data-act="review">人工复核并记录</button>',
      '  <button type="button" class="rpa-btn rpa-dismiss" data-act="dismiss">忽略本次提醒</button>',
      '</div>',
    ].join('');
    card.querySelector('[data-act="review"]').addEventListener('click', function () {
      var item = sellAlertItem(p);
      openManualRecord(item);
    });
    card.querySelector('[data-act="dismiss"]').addEventListener('click', function (event) {
      var button = event.currentTarget;
      if (!Number.isInteger(Number(p.alert && p.alert.id))) {
        mark(key, 'dismissed');
        card.remove();
        refreshPill();
        visibleFeedback('已在当前设备隐藏；服务端尚未生成可审计提醒ID', 'info');
        return;
      }
      button.disabled = true;
      button.textContent = '正在忽略…';
      dismissServer(p.alert && p.alert.id).then(function (result) {
        mark(key, 'dismissed'); card.remove();
        visibleFeedback(result.message || '已忽略并留存决策理由', 'success');
      }).catch(function (error) {
        button.disabled = false;
        button.textContent = '忽略本次提醒';
        visibleFeedback(error.message || '服务端未确认忽略状态', 'error');
        reportIssue('dismiss', error);
      });
    });
    if (!existing) panel.insertBefore(card, _statusPill);
    // 最多同时显示 4 张，超出收进折叠区（保持一屏不被占满）
    var cards = panel.querySelectorAll('.rpa-card');
    for (var i = cards.length - 1; i >= 4; i--) { cards[i].remove(); }
  }

  function dismissServer(alertId) {
    alertId = Number(alertId);
    if (!Number.isInteger(alertId) || alertId <= 0) {
      return Promise.reject(new Error('提醒缺少精确ID，未忽略'));
    }
    var controller = new AbortController();
    var timeout = window.setTimeout(function () { controller.abort('timeout'); }, 10000);
    return fetch('/api/timing/dismiss', {
      method:'POST', headers:{'Content-Type':'application/json', Accept:'application/json',
        'Idempotency-Key':'dismiss-alert-' + alertId},
      body:JSON.stringify({alert_id:alertId, reason:'user_dismissed_without_execution'}),
      signal:controller.signal,
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
        if (data.dismissed !== true || !data.decision_event ||
            Number(data.decision_event.alert_id) !== alertId) {
          throw new Error('服务端未返回精确忽略记录');
        }
        return data;
      });
    }).finally(function () { window.clearTimeout(timeout); });
  }

  function opportunityKey(item, action) {
    var alert = item.alert || {};
    var identity = alert.id || alert.created_at || alert.triggered_at || alert.window_label || 'active';
    return dedupKey(item.code, action + ':' + identity);
  }

  function renderBuyCard(item, key) {
    var panel = ensurePanel();
    var existing = Array.prototype.find.call(panel.querySelectorAll('.rpa-card[data-action="buy"]'), function (node) {
      return node.dataset.code === String(item.code) && node.dataset.key === key;
    });
    if (existing) return;
    var card = document.createElement('div');
    card.className = 'rpa-card urgent';
    card.setAttribute('data-code', item.code);
    card.setAttribute('data-action', 'buy');
    card.setAttribute('data-key', key);
    var price = Number(item.price);
    var priceLabel = Number.isFinite(price) && price > 0 ? price.toFixed(2) : '未提供';
    var isProductionStrategy = !!(
      item.alert && item.alert.provenance &&
      item.alert.provenance.origin === 'production_strategy'
    );
    var isStrictRoute = !!(
      item.alert && item.alert.provenance &&
      item.alert.provenance.origin === 'strict_route_manual_review'
    );
    var buyLabel = isProductionStrategy ? '策略买入提醒'
      : isStrictRoute ? '严格路由买入复核' : '研究观察买点';
    var plan = item.trade_plan || {}, action = plan.action || {};
    var planDetails = planDetailText(item);
    card.innerHTML = [
      '<div class="rpa-top"><span class="rpa-badge buy">' + buyLabel + ' · 人工复核</span>',
      '<span class="rpa-name">' + esc(item.name || item.code) + '</span>',
      '<span class="rpa-code">' + esc(item.code) + '</span></div>',
      '<div class="rpa-reason">' + esc(planDetails || ('参考价 ¥' + priceLabel)) + '<br>' +
        (action.cancel_condition ? '失效条件：' + esc(action.cancel_condition) + '<br>' : '') +
        esc((item.alert || {}).reason || '') +
        '<br>本站不连接券商，请先人工复核并完成成交。</div>',
      '<div class="rpa-actions"><button type="button" class="rpa-btn rpa-view" data-act="review">人工复核并记录</button>',
      '<button type="button" class="rpa-btn rpa-dismiss" data-act="dismiss">忽略本次提醒</button></div>',
    ].join('');
    card.querySelector('[data-act="review"]').addEventListener('click', function () {
      openManualRecord(item);
    });
    card.querySelector('[data-act="dismiss"]').addEventListener('click', function (event) {
      var button = event.currentTarget;
      button.disabled = true;
      button.textContent = '正在忽略…';
      dismissServer((item.alert || {}).id).then(function (result) {
        mark(key, 'dismissed');
        card.remove();
        _activeBuyCount = ensurePanel().querySelectorAll(
          '.rpa-card[data-action="buy"]'
        ).length;
        refreshPill();
        visibleFeedback(result.message || '已忽略并留存决策理由', 'success');
      }).catch(function (error) {
        button.disabled = false;
        button.textContent = '忽略本次提醒';
        visibleFeedback(error.message || '服务端未确认忽略状态', 'error');
        reportIssue('dismiss', error);
      });
    });
    panel.insertBefore(card, _statusPill);
  }

  function handleOpportunitySnapshot(data) {
    _lastOpportunity = data;
    if (!_marketSession || _marketSession.is_open !== true) {
      _activeBuyCount = 0;
      removeCards('buy');
      removeCards('decision');
      refreshPill();
      return;
    }
    var route = _lastStrategyRoute || {};
    var workspace = route.today_decision_workspace || {};
    var gate = (data && data.decision_gate) || {};
    var manualAlertEnabled = gate.manual_consent === true &&
      gate.manual_research_alerts_allowed === true;
    var routeItems = Array.isArray(workspace.manual_review_items)
      ? workspace.manual_review_items : [];
    var routedItems = routeItems.filter(function (row) {
      var plan = row && row.manual_plan;
      var evidence = plan && plan.evidence_identity;
      var evidenceIdentity = plan && (plan.evidence_id || (
        evidence && typeof evidence === 'object'
          ? evidence.id || evidence.packet_id || evidence.method : evidence
      ));
      var validUntil = plan && Date.parse(plan.valid_until);
      return plan && plan.status === 'ready_for_manual_review' &&
        plan.side === 'BUY' && Boolean(plan.strict_forward_packet_id) &&
        Boolean(plan.strict_forward_slot_id) && Boolean(plan.playbook_id) &&
        Boolean(plan.strategy_id) && Boolean(plan.strategy_version) &&
        Boolean(plan.evidence_grade) && Boolean(plan.as_of) &&
        /^\d{6}$/.test(String(row.code || '')) &&
        plan.estimated_cost != null &&
        Number.isFinite(Number(plan.estimated_cost)) && Number(plan.estimated_cost) >= 0 &&
        Boolean(plan.invalidation_condition) && Boolean(evidenceIdentity) &&
        Number.isFinite(validUntil) && validUntil > Date.now() &&
        plan.human_confirmation_required === true &&
        plan.broker_connected === false && plan.automatic_trading === false;
    });
    if (!routedItems.length || workspace.actionable_now !== true ||
        workspace.strict_forward_packet_valid !== true ||
        workspace.strict_market_budget_valid !== true ||
        (workspace.authority || {}).generates_buy_or_sell_alert !== true ||
        workspace.sector_entry_gate_ready !== true || !manualAlertEnabled) {
      _activeBuyCount = 0;
      removeCards('buy');
      renderNoBuyDecision();
      refreshPill();
      return;
    }
    var activeKeys = Object.create(null);
    var activeCount = 0;
    routedItems.slice(0, 2).forEach(function (routed) {
      var plan = routed.manual_plan || {};
      var shares = Number(plan.shares);
      var limitPrice = Number(plan.limit_price);
      var estimatedCash = Number(plan.estimated_cash_required);
      var targetWeight = Number(plan.target_weight_pct);
      var cashAfter = Number(plan.cash_after_yuan);
      var liquidityCapacity = Number(plan.liquidity_capacity_yuan);
      var riskLine = Number(plan.risk_line);
      if (plan.estimated_cash_required == null || plan.target_weight_pct == null ||
          plan.cash_after_yuan == null || plan.liquidity_capacity_yuan == null ||
          plan.risk_line == null ||
          !Number.isInteger(shares) || shares <= 0 || shares % 100 !== 0 ||
          !Number.isFinite(limitPrice) || limitPrice <= 0 ||
          !Number.isFinite(estimatedCash) || estimatedCash <= 0 ||
          !Number.isFinite(targetWeight) || targetWeight <= 0 ||
          !Number.isFinite(cashAfter) || cashAfter < 0 ||
          !Number.isFinite(liquidityCapacity) || liquidityCapacity <= 0 ||
          !Number.isFinite(riskLine) || riskLine <= 0 ||
          !Boolean(plan.risk_line_kind) ||
          !Number.isInteger(Number(plan.alert_id)) || Number(plan.alert_id) <= 0) {
        return;
      }
      var item = {
        code: routed.code,
        name: routed.name,
        price: limitPrice,
        alert: {
          id: Number(plan.alert_id), status: 'triggered', action: 'buy',
          reason: plan.reason || workspace.reason || '', valid_until: plan.valid_until,
          countdown_seconds: Math.max(1, Math.floor(
            (Date.parse(plan.valid_until) - Date.now()) / 1000
          )),
          provenance: {
            origin: 'strict_route_manual_review',
            strategy_id: plan.strategy_id,
            strategy_version: plan.strategy_version,
            playbook_id: plan.playbook_id,
            strict_forward_packet_id: plan.strict_forward_packet_id,
            strict_forward_slot_id: plan.strict_forward_slot_id,
          },
        },
        trade_plan: {state: 'ENTRY_READY', action: {
          side: 'BUY', shares: shares, target_shares: Number(plan.target_shares),
          limit_price: limitPrice, valid_until: plan.valid_until,
          cancel_condition: plan.invalidation_condition,
          estimated_cost: Number(plan.estimated_cost),
          playbook_id: plan.playbook_id, strategy_id: plan.strategy_id,
          strategy_version: plan.strategy_version,
          evidence_grade: plan.evidence_grade, as_of: plan.as_of,
          evidence_identity: plan.evidence_identity, manual_confirmation: true,
        }},
        allocation: {
          estimated_buy_cash_yuan: estimatedCash,
          target_weight_pct: targetWeight,
          cash_after_yuan: cashAfter,
          liquidity_capacity_yuan: liquidityCapacity,
          risk_line: {kind: plan.risk_line_kind, value: riskLine},
        },
      };
      var key = opportunityKey(item, 'buy');
      var record = _notified[key];
      if (record && record.state === 'dismissed') {
        return;
      }
      activeKeys[key] = true;
      activeCount += 1;
      renderBuyCard(item, key);
      maybeShowOverlay(item, key);
      if (!record || record.state !== 'notified') {
        var buyLabel = '整套情绪联合买入复核';
        var quantityLine = '计划 ' + shares + ' 股 · 限价参考 ¥' + limitPrice.toFixed(2) + ' · ';
        var identityLine = '战法 ' + plan.playbook_id + ' · 策略 ' +
          plan.strategy_id + ' ' + plan.strategy_version + ' · ';
        var ok = deliverAlert(buyLabel + '：' + (item.name || item.code) + ' 请人工复核',
          (jointDecisionSummary().facts + '；' + quantityLine + identityLine +
            ((item.alert || {}).reason || '')).slice(0, 180),
          'regis-buy-' + item.code, 'buy');
        if (ok) mark(key, 'notified');
      }
    });
    _activeBuyCount = activeCount;
    ensurePanel().querySelectorAll('.rpa-card[data-action="buy"]').forEach(function (card) {
      if (!activeKeys[card.dataset.key]) card.remove();
    });
    if (_activeBuyCount > 0) removeCards('decision');
    else renderNoBuyDecision();
    refreshPill();
  }

  // ── 轮询 ───────────────────────────────────────────
  var _failStreak = 0;
  function fetchSnapshot(url, controller) {
    return fetch(url, {cache:'no-store', headers:{Accept:'application/json'}, signal:controller.signal})
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      }).then(function (data) { return {data:data}; })
      .catch(function (error) { return {error:error}; });
  }
  function poll() {
    if (!_isLeader) return;
    if (window.__regisSpaPortfolioMonitorOwner && !document.hidden) return;
    var sequence = ++_pollSequence;
    if (_pollController) _pollController.abort();
    var controller = new AbortController();
    _pollController = controller;
    var timeout = window.setTimeout(function () { controller.abort('timeout'); }, 10000);
    Promise.all([
      fetchSnapshot('/api/timing/portfolio', controller),
      fetchSnapshot('/api/opportunities?view=monitor', controller),
      fetchSnapshot('/api/timing/alloc', controller),
      fetchSnapshot('/api/strategy-route/research?view=dashboard', controller),
    ]).then(function (results) {
        if (sequence !== _pollSequence || _pollController !== controller) return;
        var portfolio = results[0], opportunities = results[1], allocation = results[2], route = results[3];
        _lastAllocation = allocation.data || null;
        _lastStrategyRoute = route.data || null;
        if (opportunities.data) _lastOpportunity = opportunities.data;
        if (portfolio.data) {
          _lastPortfolio = portfolio.data;
          handlePortfolio(portfolioWithStoredAlerts(
            portfolioWithAllocationPlans(_lastPortfolio, _lastAllocation),
            _lastOpportunity
          ));
        }
        else {
          handlePortfolio(null);
          setPill('买卖提醒·持仓监控连接异常', true);
        }
        if (opportunities.data) handleOpportunitySnapshot(Object.assign(
          {}, opportunities.data, {allocation_plan: allocation.data || null}
        ));
        var error = portfolio.error || opportunities.error || allocation.error || route.error;
        if (route.error) {
          setPill('买卖提醒·整套情绪链连接异常·当前不交易', true);
        }
        if (!error) _failStreak = 0;
        else {
          _failStreak += 1;
          if (_failStreak === 1 && error.name !== 'AbortError') reportIssue('poll', error);
        }
      }).finally(function () {
        window.clearTimeout(timeout);
        if (_pollController === controller) _pollController = null;
      });
  }

  function stopPolling() {
    if (_pollTimer) window.clearInterval(_pollTimer);
    _pollTimer = null;
    _pollSequence += 1;
    if (_pollController) _pollController.abort();
    _pollController = null;
  }

  function resumePolling() {
    stopPolling();
    if (_pageLeaving || !_isLeader) return;
    if (window.__regisSpaPortfolioMonitorOwner && !document.hidden) return;
    poll();
    _pollTimer = window.setInterval(
      poll, document.hidden ? BACKGROUND_POLL_MS : POLL_MS
    );
  }

  function schedulePolling() {
    var sequence = ++_resumeSequence;
    Promise.resolve().then(function () {
      if (sequence !== _resumeSequence || _pageLeaving) return;
      resumePolling();
    });
  }

  function handleMonitorOwnership(event) {
    var detail = event && event.detail || {};
    var active = !!detail.active;
    var owner = detail.owner || 'spa';
    if (active) window.__regisSpaPortfolioMonitorOwner = owner;
    else if (!detail.owner || !window.__regisSpaPortfolioMonitorOwner ||
        window.__regisSpaPortfolioMonitorOwner === owner) {
      window.__regisSpaPortfolioMonitorOwner = null;
    }
    if (active && !document.hidden) {
      _resumeSequence += 1;
      stopPolling();
    } else schedulePolling();
  }

  function handleSharedSnapshot(event) {
    if (!_isLeader || !event || !event.detail) return;
    _failStreak = 0;
    _lastPortfolio = event.detail;
    handlePortfolio(portfolioWithStoredAlerts(
      portfolioWithAllocationPlans(_lastPortfolio, _lastAllocation),
      _lastOpportunity
    ));
  }

  function handleSharedOpportunity(event) {
    if (!_isLeader || !event || !event.detail) return;
    _lastOpportunity = event.detail;
    if (event.detail.allocation_plan) {
      _lastAllocation = event.detail.allocation_plan;
      if (_lastPortfolio) {
        handlePortfolio(portfolioWithStoredAlerts(
          portfolioWithAllocationPlans(_lastPortfolio, _lastAllocation),
          _lastOpportunity
        ));
      }
    }
    handleOpportunitySnapshot(event.detail);
  }

  function handleSharedStrategyRoute(event) {
    if (!_isLeader || !event) return;
    _lastStrategyRoute = event.detail || null;
    if (!_lastStrategyRoute) {
      _activeBuyCount = 0;
      removeCards('buy');
      removeCards('decision');
      setPill('买卖提醒·整套情绪链连接异常·当前不交易', true);
      return;
    }
    if (_lastOpportunity) handleOpportunitySnapshot(_lastOpportunity);
  }

  function handlePageHide() {
    _pageLeaving = true;
    _resumeSequence += 1;
    stopPolling();
    stopLeadershipHeartbeat();
    releaseLeadership();
  }

  function handlePageShow() {
    _pageLeaving = false;
    startLeadershipHeartbeat();
    updateLeadership();
  }

  function handleStorage(event) {
    if (!event) return;
    if (event.key === STORE_KEY) _notified = loadSeen();
    if (event.key === ENABLE_KEY) refreshPill();
    if (event.key === LEADER_KEY) updateLeadership();
  }

  // 前台 20s、后台 60s 低频轮询；页面卸载时才完全停止。
  function start() {
    ensureDefaultPreference();
    bindPreferenceButton();
    prepareDefaultCapabilities();
    window.addEventListener('regis-portfolio-monitor-active', handleMonitorOwnership);
    window.addEventListener('regis-portfolio-snapshot', handleSharedSnapshot);
    window.addEventListener('regis-opportunity-snapshot', handleSharedOpportunity);
    window.addEventListener('regis-strategy-route-snapshot', handleSharedStrategyRoute);
    window.addEventListener('storage', handleStorage);
    updateLeadership();
    startLeadershipHeartbeat();
    document.addEventListener('visibilitychange', schedulePolling);
    window.addEventListener('pagehide', handlePageHide);
    window.addEventListener('pageshow', handlePageShow);
  }

  // DOM 就绪后启动
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
