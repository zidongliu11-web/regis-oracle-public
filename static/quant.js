/* A股量化研究台 — 64因子 · walk-forward 加权 · 诚实样本外回测 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    dot: $("quantConnectionDot"),
    conn: $("quantConnection"),
    clock: $("quantClock"),
    runBtn: $("quantRunBtn"),
    universeSel: $("quantUniverse"),
    status: $("quantRunStatus"),
    log: $("quantLog"),
    verdict: $("quantVerdict"),
    factorCount: $("quantFactorCount"),
    t1: $("quantT1"),
    t2: $("quantT2"),
    sample: $("quantSample"),
    universeDetail: $("quantUniverseDetail"),
    elapsed: $("quantElapsed"),
    source: $("quantSource"),
    generatedAt: $("quantGeneratedAt"),
    bestAnnual: $("quantBestAnnual"),
    bestSharpe: $("quantBestSharpe"),
    posYears: $("quantPosYears"),
    portIc: $("quantPortIc"),
    factorLibTag: $("quantFactorLibraryTag"),
    factorGroups: $("quantFactorGroups"),
    icTag: $("quantIcTag"),
    icBody: $("quantIcBody"),
    methodsTag: $("quantMethodsTag"),
    methodsBody: $("quantMethodsBody"),
    strategies: $("quantStrategies"),
    toast: $("quantToast"),
  };

  const tierTag = {
    t1_excellent: ["quant-tier-t1", "T1"],
    t2_good: ["quant-tier-t2", "T2"],
    t3_marginal: ["quant-tier-t3", "T3"],
    t4_weak: ["quant-tier-t4", "T4"],
  };
  const verdictClass = {
    excellent: ["excellent", "优秀"],
    good: ["good", "良好"],
    marginal: ["marginal", "一般"],
    poor: ["poor", "不佳"],
    error: ["error", "失败"],
    ok: ["ok", "正常"],
  };
  const statusClass = {
    idle: ["gray", "空闲"],
    running: ["yellow", "运行中"],
    done: ["green", "完成"],
    error: ["red", "失败"],
  };
  const groupLabels = {
    low_vol: "低波", reversal: "反转", momentum: "动量", value: "价值",
    liquidity: "流动性", quality: "质量", size: "规模", money_flow: "资金流",
    technical: "技术", regime: "状态", other: "其他",
  };

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function num(v, digits) {
    if (v == null || isNaN(v)) return "--";
    return Number(v).toFixed(digits == null ? 2 : digits);
  }

  function signed(v, digits) {
    if (v == null || isNaN(v)) return "--";
    const n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(digits == null ? 2 : digits);
  }

  function toast(msg, isError) {
    els.toast.textContent = msg;
    els.toast.className = "toast" + (isError ? " error" : "");
    els.toast.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => els.toast.classList.remove("show"), 3200);
  }

  function clock() {
    const d = new Date();
    els.clock.textContent =
      d.toLocaleTimeString("zh-CN", { hour12: false });
  }
  setInterval(clock, 1000);
  clock();

  /* ── 状态轮询 ── */
  let pollTimer = null;
  let lastStatus = "";

  async function loadStatus() {
    try {
      const res = await fetch("/api/quant/status");
      const st = await res.json();
      renderStatus(st);
      if (st.status === "running") {
        schedulePoll(1800);
      } else if (st.status !== "running" && lastStatus === "running") {
        await renderFullReport();
      }
      if (st.status === "done" && !document.querySelector("#quantStrategies .quant-strategy-card")) {
        await renderFullReport();
      }
      lastStatus = st.status;
    } catch (e) {
      els.conn.textContent = "状态读取失败";
      els.dot.className = "status-dot error";
    }
  }

  function schedulePoll(ms) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(loadStatus, ms);
  }

  function renderStatus(st) {
    const [cls, label] = statusClass[st.status] || statusClass.idle;
    els.status.className = "tag " + cls;
    els.status.textContent = st.status === "running"
      ? label + " · " + (st.last_params ? st.last_params.universe + "只宇宙" : "")
      : label;
    els.dot.className = "status-dot " + (st.status === "running" ? "live" : st.status === "error" ? "error" : "");
    els.conn.textContent = st.status === "running"
      ? "walk-forward 研究运行中，实时刷新进度…"
      : st.status === "done"
        ? "研究完成（样本外 · 含成本）"
        : st.status === "error"
          ? "研究失败"
          : "量化研究已就绪";

    if (st.runBtn) els.runBtn.disabled = st.status === "running";
    els.runBtn.disabled = st.status === "running";

    if (st.log_lines && st.log_lines.length) {
      renderLog(st.log_lines);
    }
    if (st.summary) renderSummary(st.summary);
  }

  function renderLog(lines) {
    const html = lines
      .map((line) => {
        // 兼容两种格式：带 HH:MM:SS 时间前缀 或 纯文本
        const m = /^(\d{2}:\d{2}:\d{2}) ?(.*)$/.exec(line) || /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ?(.*)$/.exec(line);
        const t = m ? m[1] : "";
        const body = m ? m[2] : line;
        const isErr = /失败|error|❌|✗|⚠/i.test(body);
        return (
          '<div class="log-line' + (isErr ? " log-err" : "") + '">' +
          (t ? '<span class="log-time">' + esc(t) + "</span>" : "") +
          esc(body) + "</div>"
        );
      })
      .join("");
    els.log.innerHTML = html || "";
    els.log.scrollTop = els.log.scrollHeight;
  }

  function renderSummary(s) {
    els.factorCount.textContent = s.factor_count != null ? s.factor_count : "--";
    els.t1.textContent = s.t1 != null ? s.t1 : "--";
    els.t2.textContent = s.t2 != null ? s.t2 : "--";
    els.sample.textContent =
      s.n_stocks != null && s.n_dates != null
        ? s.n_stocks + " 只 × " + s.n_dates + " 天"
        : "--";
    els.universeDetail.textContent =
      s.generated_at ? "生成于 " + s.generated_at.replace("T", " ").slice(0, 19) : "等待研究";
    els.elapsed.textContent =
      s.elapsed_seconds != null ? s.elapsed_seconds + " 秒" : "--";
    els.source.textContent = "数据源：" + (s.data_source || "--");
    els.bestAnnual.textContent = s.best_annual != null ? signed(s.best_annual) + "%" : "--";
    els.bestSharpe.textContent = s.best_name
      ? (s.best_sharpe != null ? "夏普 " + num(s.best_sharpe, 2) + " · " : "") + esc(s.best_name)
      : "样本外 · 含成本";
    els.posYears.textContent =
      s.years_total != null && s.years_positive != null
        ? s.years_positive + "/" + s.years_total + " 年"
        : "--";
    els.portIc.textContent =
      s.portfolio_ic != null ? "组合 IC：" + signed(s.portfolio_ic, 3) : "组合 IC：--";
  }

  /* ── 因子库 ── */
  async function loadFactorLibrary() {
    try {
      const res = await fetch("/api/quant/factor-library");
      const d = await res.json();
      els.factorLibTag.className = "tag green";
      els.factorLibTag.textContent = d.total + " 个因子 · " + d.groups.length + " 组";
      els.factorGroups.innerHTML = d.groups
        .map(
          (g) =>
            '<div class="quant-factor-group">' +
            '<div class="quant-factor-group-head"><b>' + esc(g.label) + "</b>" +
            "<span>" + g.factors.length + "</span></div>" +
            "<ul>" +
            g.factors
              .map(
                (f) =>
                  '<li class="quant-factor-chip" title="' + esc(f.description || "") + '">' +
                  esc(f.name) + "</li>"
              )
              .join("") +
            "</ul></div>"
        )
        .join("");
    } catch (e) {
      els.factorLibTag.className = "tag red";
      els.factorLibTag.textContent = "读取失败";
    }
  }

  /* ── 完整报告 ── */
  async function renderFullReport() {
    let rep;
    try {
      const res = await fetch("/api/quant/report");
      if (res.status !== 200) return;
      rep = await res.json();
    } catch (e) {
      return;
    }
    renderVerdict(rep.verdict);
    renderIc(rep.ic_ranking, (rep.ic_summary || {}).total_factors);
    renderMethods(rep.strategies || []);
    renderStrategies(rep.strategies || []);
  }

  function renderVerdict(verdict) {
    if (!verdict) return;
    const [cls, label] = verdictClass[verdict.status] || verdictClass.ok;
    els.verdict.className = "quant-verdict " + cls;
    els.verdict.hidden = false;
    els.verdict.innerHTML =
      "<b>结论：" + label + "</b>" +
      (verdict.messages || [])
        .map((m) => "<p>" + esc(m) + "</p>")
        .join("");
  }

  function cell(v, digits, strong) {
    if (v == null || isNaN(v)) return "--";
    const n = Number(v);
    const cls = n > 0 ? "pos" : n < 0 ? "neg" : "";
    const t = (strong ? "<b>" : "") + num(v, digits) + (strong ? "</b>" : "");
    return '<span class="' + cls + '">' + t + "</span>";
  }

  function renderIc(ranking, total) {
    if (!ranking || !ranking.length) return;
    els.icTag.className = "tag blue";
    els.icTag.textContent = total + " 个因子 · mean IC 全样本统计";
    els.icBody.innerHTML = ranking
      .map((r) => {
        const t = tierTag[r.tier] || tierTag.t4_weak;
        const stable = r.sign_stable
          ? '<span class="pos">稳定</span>'
          : '<span class="neg">翻转</span>';
        const seg = [r.seg1, r.seg2, r.seg3]
          .map((v) => (v == null ? "--" : '<span class="' + (v > 0 ? "pos" : "neg") + '">' + num(v, 3) + "</span>"))
          .join(" / ");
        return (
          "<tr>" +
          "<td><b>" + esc(r.factor) + "</b></td>" +
          "<td>" + esc(groupLabels[r.group] || r.group || "其他") + "</td>" +
          "<td>" + cell(r.mean_ic, 4, true) + "</td>" +
          "<td>" + cell(r.icir, 2) + "</td>" +
          "<td>" + num(r.ic_pos_ratio, 2) + "</td>" +
          "<td>" + stable + "</td>" +
          "<td class='tt'>" + seg + "</td>" +
          '<td><span class="quant-tier-tag ' + t[0] + '">' + t[1] + "</span></td>" +
          "</tr>"
        );
      })
      .join("");
  }

  /* ── 加权方法对比表 ── */
  function renderMethods(strategies) {
    if (!strategies.length) return;
    els.methodsTag.className = "tag green";
    els.methodsTag.textContent = strategies.length + " 个配置 · 同一宇宙/持有期";
    els.methodsBody.innerHTML = strategies
      .map((s) => {
        const m = s.metrics || {};
        const best = !!s.preferred;
        const cls = best ? ' class="quant-row-best"' : "";
        return (
          "<tr" + cls + ">" +
          "<td>" + esc(s.name) + (best ? ' <span class="tag green">首选</span>' : "") + "</td>" +
          "<td>" + cell(m.annualized_pct, 1, true) + "%</td>" +
          "<td>" + num(m.sharpe, 2) + "</td>" +
          "<td>" + num(m.max_dd_pct, 1) + "%</td>" +
          "<td>" + num(m.calmar, 2) + "</td>" +
          "<td>" + cell(m.portfolio_ic, 3) + "</td>" +
          "<td>" + s.years_positive + "/" + s.years_total + "</td>" +
          "<td>" + num(s.total_turnover, 0) + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function sparkline(navLog) {
    if (!navLog || navLog.length < 2) return "";
    const values = navLog.map((n) => Number(n.nav));
    const min = Math.min.apply(null, values);
    const max = Math.max.apply(null, values);
    const span = max - min || 1;
    const W = 300, H = 52, pad = 4;
    const pts = values.map((v, i) => {
      const x = pad + (i / (values.length - 1)) * (W - pad * 2);
      const y = H - pad - ((v - min) / span) * (H - pad * 2);
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    const line = pts.join(" ");
    const area = line + " " + (W - pad) + "," + H + " " + pad + "," + H;
    return (
      '<svg class="quant-spark" viewBox="0 0 ' + W + " " + H +
      '" preserveAspectRatio="none" aria-hidden="true">' +
      '<path class="area" d="M' + area + '"></path>' +
      '<path class="line" d="M' + line + '"></path></svg>'
    );
  }

  function yearBar(periodReturns) {
    if (!periodReturns) return "";
    const years = Object.keys(periodReturns).sort();
    if (!years.length) return "";
    const vals = years.map((y) => Number(periodReturns[y]));
    const maxAbs = Math.max.apply(null, vals.map((v) => Math.abs(v))) || 1;
    const bar = years
      .map((y) => {
        const v = Number(periodReturns[y]);
        const h = Math.round((Math.abs(v) / maxAbs) * 24) + 2;
        const cls = v >= 0 ? "yr-pos" : "yr-neg";
        return (
          '<div class="yr" title="' + y + ": " + signed(v) + "%" + '">' +
          '<div class="yr-bar ' + cls + '" style="height:' + h + 'px"></div>' +
          '<span class="yr-label">' + y.slice(2) + "</span>" +
          "</div>"
        );
      })
      .join("");
    return (
      '<div class="quant-years">' +
      '<div class="quant-years-head"><span>分年度收益（样本外）</span>' +
      '<span class="tt">' + vals.filter((v) => v > 0).length + "/" + years.length + " 年为正</span></div>" +
      '<div class="quant-years-bars">' + bar + "</div>" +
      "</div>"
    );
  }

  const weightDesc = {
    equal: "等权", ic: "按 |IC| 加权", icir: "按 |ICIR| 加权",
    decay: "半衰期衰减加权", shrunk: "ICIR 收缩混合", comb: "衰减组合",
  };

  function renderStrategies(strategies) {
    if (!strategies.length) {
      els.strategies.innerHTML =
        '<div class="empty-state"><b>暂无有效研究结果</b>因子质量不足或研究失败。</div>';
      return;
    }
    els.strategies.innerHTML = strategies
      .map((s) => {
        const m = s.metrics || {};
        const pct = (v) => signed(v) + "%";
        const alpha = s.alpha_pct;
        const hasNav = (s.nav_log || []).length > 1;
        const tagCls = alpha > 0 ? "green" : alpha < -2 ? "red" : "yellow";
        const alphaTag = hasNav ? "Alpha " + signed(alpha) + "%" : "样本外";
        return (
          '<article class="quant-strategy-card">' +
          '<div class="quant-strategy-head">' +
          "<h2>" + esc(s.name) + "</h2>" +
          '<span class="tag ' + tagCls + '">' + alphaTag + "</span>" +
          "</div>" +
          '<div class="quant-metric-grid">' +
          metric("年化收益", pct(m.annualized_pct)) +
          metric("累计收益", pct(m.total_return_pct)) +
          metric("夏普", num(m.sharpe, 2)) +
          metric("最大回撤", num(m.max_dd_pct, 1) + "%") +
          metric("Calmar", num(m.calmar, 2)) +
          metric("组合IC", signed(m.portfolio_ic, 3)) +
          metric("正年份", s.years_positive + "/" + s.years_total) +
          metric("基准年化", pct(s.benchmark && s.benchmark.annual_pct)) +
          metric("换手", num(s.total_turnover, 0) + "%/期") +
          metric("交易笔数", m.n_trades || 0) +
          "</div>" +
          (hasNav ? sparkline(s.nav_log) : "") +
          yearBar(s.period_returns) +
          "<p class='tt' style='color:var(--muted);margin:8px 0 0;'>" +
          "加权：" + (weightDesc[s.method] || s.method || "--") +
          " · trailing IC 动态选择 top8 因子 · 等权持有 " +
          (s.topk || "--") + " 只 · 剔除 ST 与最低 20% 流动性</p>" +
          "</article>"
        );
      })
      .join("");
  }

  function metric(label, value) {
    return '<div class="metric"><span>' + label + "</span><strong>" + value + "</strong></div>";
  }

  /* ── 运行按钮 ── */
  els.runBtn.addEventListener("click", async () => {
    const universe = Number(els.universeSel.value || 300);
    els.runBtn.disabled = true;
    try {
      const res = await fetch("/api/quant/pipeline/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ universe: universe }),
      });
      const d = await res.json();
      if (res.status === 409) {
        toast(d.message || "研究已在运行中", true);
        els.runBtn.disabled = false;
        return;
      }
      toast("研究已启动：" + universe + " 只流动性宇宙（walk-forward，约 1–3 分钟）");
      els.log.innerHTML = "";
      loadStatus();
    } catch (e) {
      toast("启动失败：" + e, true);
      els.runBtn.disabled = false;
    }
  });

  /* ── 初始化 ── */
  loadFactorLibrary();
  loadStatus();
})();
