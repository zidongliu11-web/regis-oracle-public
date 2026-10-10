/* Kronos 预测区 — 金融K线基础模型 · 预测 × 量化因子共振 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    dot: $("kronosDot"),
    conn: $("kronosConnection"),
    clock: $("kronosClock"),
    runBtn: $("kronosRunBtn"),
    universeSel: $("kronosUniverse"),
    predLenSel: $("kronosPredLen"),
    model: $("kronosModel"),
    date: $("kronosDate"),
    universe: $("kronosUniverse"),
    pred: $("kronosPred"),
    elapsed: $("kronosElapsed"),
    peak: $("kronosPeak"),
    direction: $("kronosDirection"),
    fusionTag: $("kronosFusionTag"),
    quantAnnual: $("kronosQuantAnnual"),
    quantSharpe: $("kronosQuantSharpe"),
    quantDate: $("kronosQuantDate"),
    quantCalmar: $("kronosQuantCalmar"),
    quantYears: $("kronosQuantYears"),
    bullReso: $("kronosBullReso"),
    divergence: $("kronosDivergence"),
    rankTag: $("kronosRankTag"),
    sortRow: document.querySelector(".kronos-sort-row"),
    body: $("kronosBody"),
    distTag: $("kronosDistTag"),
    distBars: $("kronosDistBars"),
    runStatus: $("kronosRunStatus"),
    log: $("kronosLog"),
    toast: $("kronosToast"),
  };

  const statusClass = {
    idle: ["gray", "空闲"],
    running: ["yellow", "推理中"],
    done: ["green", "完成"],
    error: ["red", "失败"],
  };

  let cached = null;      // 最近一次 /api/kronos 合并快照
  let sortKey = "pred_ret_20d";

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

  function pctClass(v) {
    if (v == null || isNaN(v)) return "";
    return v > 0 ? "pos" : v < 0 ? "neg" : "";
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
    els.clock.textContent = d.toLocaleTimeString("zh-CN", { hour12: false });
  }
  setInterval(clock, 1000);
  clock();

  /* ── 渲染 ── */
  function dirCell(d) {
    if (d == null || isNaN(d)) return '<span class="tt">--</span>';
    const pct = Math.round(d * 100);
    const cls = d > 0.6 ? "pos" : d < 0.4 ? "neg" : "tt";
    return (
      '<span class="kronos-direction-bar"><i style="width:' + pct +
      '%;background:' + (d >= 0.5 ? "var(--green)" : "var(--red)") +
      '"></i></span><span class="' + cls + '">' + pct + "%</span>"
    );
  }

  function rankCell(code) {
    const st = cached.stocks[code];
    const qr = st.quant_rank;
    if (qr == null) return '<span class="kronos-quant-none">未覆盖</span>';
    const cls = qr <= 20 ? "kronos-rank-top" : "kronos-rank-num";
    return '<span class="' + cls + '">#' + qr + "</span>";
  }

  function holdBadge(st) {
    return st.in_portfolio
      ? '<span class="kronos-hold-badge">组合中</span>'
      : '<span class="tt">--</span>';
  }

  function renderTable() {
    if (!cached || !cached.stocks) return;
    const stocks = Object.values(cached.stocks);
    stocks.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      const avv = av == null ? -Infinity : Number(av);
      const bvv = bv == null ? -Infinity : Number(bv);
      return bvv - avv;
    });
    els.body.innerHTML = stocks
      .map((s, i) => {
        const ret = (k, d) => {
          const v = s[k];
          if (v == null || isNaN(v)) return "--";
          return '<span class="' + pctClass(v) + '"><b>' + signed(v * 100, 1) +
            "%</b></span>";
        };
        const idxCls = i < 5 ? "kronos-rank-top" : "kronos-rank-num";
        return (
          "<tr>" +
          '<td><span class="' + idxCls + '">' + (i + 1) + "</span></td>" +
          '<td><span class="kronos-code">' + esc(s.code) + "</span></td>" +
          "<td>" + num(s.last_close, 2) + "</td>" +
          "<td>" + ret("pred_ret_5d", 5) + "</td>" +
          "<td>" + ret("pred_ret_10d", 10) + "</td>" +
          "<td>" + ret("pred_ret_20d", 20) + "</td>" +
          "<td>" + ret("pred_ret_30d", 30) + "</td>" +
          "<td>" + dirCell(s.direction) + "</td>" +
          "<td>" + rankCell(s.code) + "</td>" +
          "<td>" + holdBadge(s) + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function renderSummary(d) {
    els.model.textContent = d.model ? "Kronos-small" : "--";
    els.date.textContent = d.snapshot_date || "--";
    els.universe.textContent = d.universe != null ? d.universe + " 只" : "--";
    els.pred.textContent = d.pred_len != null ? d.pred_len + " 日" : "--";
    els.elapsed.textContent = d.elapsed_seconds != null ? d.elapsed_seconds + " 秒" : "--";
    els.peak.textContent = d.peak_memory_mb != null ? "峰值内存 " + d.peak_memory_mb + " MB" : "峰值内存 --";

    // 20日方向：全宇宙平均 direction
    const sts = d.stocks || {};
    const dirs = Object.values(sts).map((s) => s.direction).filter((v) => v != null && !isNaN(v));
    if (dirs.length) {
      const mean = dirs.reduce((a, b) => a + b, 0) / dirs.length;
      const pct = Math.round(mean * 100);
      const cls = mean > 0.5 ? "pos" : "neg";
      els.direction.innerHTML = '<span class="' + cls + '">' + pct + "%</span>";
    } else {
      els.direction.textContent = "--";
    }

    // 量化研究台 headline（融合展示）
    const q = d.quant_headline || {};
    els.quantAnnual.textContent = q.annualized_pct != null ? signed(q.annualized_pct) + "%" : "--";
    els.quantSharpe.textContent = q.sharpe != null ? num(q.sharpe, 2) : "--";
    els.quantDate.textContent = "快照日期 " + (d.quant_snapshot_date || "--");
    els.quantCalmar.textContent = q.calmar != null ? num(q.calmar, 2) : "--";
    els.quantYears.textContent = q.years_positive != null && q.years_total != null
      ? q.years_positive + "/" + q.years_total : "--";

    // 方向分布
    renderDist(d);
    // 共振带
    renderFusion(d);
    // 排行表
    renderTable();
  }

  function renderDist(d) {
    const sts = d.stocks || {};
    const arr = Object.values(sts).filter((s) => s.pred_ret_20d != null);
    if (!arr.length) {
      els.distTag.className = "tag gray";
      els.distTag.textContent = "等待快照";
      els.distBars.innerHTML = '<p class="empty-state compact">运行快照后显示各持有期看多/看空占比。</p>';
      return;
    }
    els.distTag.className = "tag blue";
    els.distTag.textContent = arr.length + " 只 · 预测路径方向分布";
    const horizons = [5, 10, 20, 30];
    els.distBars.innerHTML = horizons.map((h) => {
      const n = arr.filter((s) => s["pred_ret_" + h + "d"] != null).length;
      const bull = arr.filter((s) => (s["pred_ret_" + h + "d"] || 0) > 0).length;
      const bear = n - bull;
      const pct = n ? Math.round((bull / n) * 100) : 0;
      return (
        '<div class="kronos-dist-card">' +
        "<span>" + h + " 日收益方向</span>" +
        '<div class="kronos-dist-track"><i class="bull" style="width:' + pct +
        '%"></i><i class="bear" style="width:' + (100 - pct) + '%"></i></div>' +
        '<div class="kronos-dist-nums">' +
        '<span class="bull">▲ ' + bull + "</span>" +
        '<span class="bear">▼ ' + bear + "</span>" +
        "</div></div>"
      );
    }).join("");
  }

  function chip(code, st) {
    const r20 = st.pred_ret_20d;
    const r20s = r20 != null ? signed(r20 * 100, 1) + "%" : "--";
    const qr = st.quant_rank != null ? "#" + st.quant_rank : "--";
    return (
      '<span class="kronos-chip" title="预测20日收益 · 量化排名">' +
      "<b>" + esc(code) + "</b> " +
      '<span class="kr-pos">' + r20s + "</span>" +
      " <span class='tt'>QR</span> " + qr +
      (st.in_portfolio ? ' <span class="kr-hold">持有</span>' : "") +
      "</span>"
    );
  }

  function renderFusion(d) {
    const sts = d.stocks || {};
    const arr = Object.values(sts).filter((s) => s.pred_ret_20d != null);
    if (!arr.length) {
      els.fusionTag.className = "tag gray";
      els.fusionTag.textContent = "等待快照";
      els.bullReso.innerHTML = '<p class="empty-state compact">--</p>';
      els.divergence.innerHTML = '<p class="empty-state compact">--</p>';
      return;
    }
    els.fusionTag.className = "tag green";
    els.fusionTag.textContent = arr.length + " 只 · Kronos × 量化对照";

    // 共振：Kronos 20日看多 + 量化组合持仓（或量化排名前20）
    const bull = arr
      .filter((s) => (s.pred_ret_20d || 0) > 0 && (s.in_portfolio || (s.quant_rank != null && s.quant_rank <= 20)))
      .sort((a, b) => b.pred_ret_20d - a.pred_ret_20d)
      .slice(0, 10);
    // 背离：Kronos 20日看多但量化排名靠后（>60）
    const diverge = arr
      .filter((s) => (s.pred_ret_20d || 0) > 0 && s.quant_rank != null && s.quant_rank > 60)
      .sort((a, b) => b.pred_ret_20d - a.pred_ret_20d)
      .slice(0, 10);

    els.bullReso.innerHTML = bull.length
      ? bull.map((s) => chip(s.code, s)).join("")
      : '<p class="empty-state compact">暂无共振股票（快照宇宙较小时属正常）。</p>';
    els.divergence.innerHTML = diverge.length
      ? diverge.map((s) => chip(s.code, s)).join("")
      : '<p class="empty-state compact">暂无背离股票。</p>';
  }

  /* ── 状态轮询 ── */
  let pollTimer = null;
  let lastStatus = "";

  async function loadData() {
    try {
      const res = await fetch("/api/kronos");
      const d = await res.json();
      cached = d;
      renderStatus(d);
      if (d.stocks && Object.keys(d.stocks).length) renderSummary(d);
      if (d.status === "running") {
        schedulePoll(2500);
      } else if (d.status !== "running" && lastStatus === "running") {
        toast("Kronos 推理完成，已刷新预测");
        // 重新拉取最新快照
        const r2 = await fetch("/api/kronos");
        cached = await r2.json();
        renderSummary(cached);
      }
      lastStatus = d.status;
    } catch (e) {
      els.conn.textContent = "状态读取失败";
      els.dot.className = "status-dot error";
    }
  }

  function schedulePoll(ms) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(loadData, ms);
  }

  function renderStatus(st) {
    const [cls, label] = statusClass[st.status] || statusClass.idle;
    els.runStatus.className = "tag " + cls;
    els.runStatus.textContent = st.status === "running"
      ? label + " · " + (st.last_params ? st.last_params.universe + "只/" + st.last_params.pred_len + "日" : "")
      : label;
    els.dot.className = "status-dot " + (st.status === "running" ? "live" : st.status === "error" ? "error" : "");
    els.conn.textContent = st.status === "running"
      ? "Kronos 批量推理中，实时刷新进度…"
      : st.status === "done"
        ? "快照完成（Kronos-small · CPU）"
        : st.status === "error"
          ? "推理失败"
          : st.has_result
            ? "Kronos 快照已就绪"
            : "等待 Kronos 快照";
    els.runBtn.disabled = st.status === "running";
    if (st.log_lines && st.log_lines.length) renderLog(st.log_lines);
  }

  function renderLog(lines) {
    const html = lines
      .map((line) => {
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

  /* ── 排序按钮 ── */
  els.sortRow.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".kronos-sortbtn");
    if (!btn) return;
    sortKey = btn.dataset.sort;
    els.sortRow.querySelectorAll(".kronos-sortbtn").forEach((b) =>
      b.classList.toggle("active", b === btn));
    renderTable();
  });
  if (els.sortRow.querySelector('[data-sort="pred_ret_20d"]')) {
    els.sortRow.querySelector('[data-sort="pred_ret_20d"]').classList.add("active");
  }

  /* ── 运行按钮 ── */
  els.runBtn.addEventListener("click", async () => {
    const universe = Number(els.universeSel.value || 200);
    const pred_len = Number(els.predLenSel.value || 30);
    els.runBtn.disabled = true;
    try {
      const res = await fetch("/api/kronos/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ universe: universe, pred_len: pred_len }),
      });
      const d = await res.json();
      if (res.status === 409) {
        toast(d.message || "推理已在运行中", true);
        els.runBtn.disabled = false;
        return;
      }
      toast("已启动 Kronos 推理：" + universe + " 只 × " + pred_len + " 日（约 1–5 分钟）");
      els.log.innerHTML = "";
      loadData();
    } catch (e) {
      toast("启动失败：" + e, true);
      els.runBtn.disabled = false;
    }
  });

  /* ── 初始化 ── */
  loadData();
})();
