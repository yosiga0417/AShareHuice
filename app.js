/* ────────────────────────────────────────────────
     State
  ──────────────────────────────────────────────── */
  const STORAGE_KEY = "a_stock_backtest_v1";
  const COMPARISON_STORAGE_KEY = "a_stock_backtest_comparisons_v1";
  const LAST_RESULT_KEY = "a_stock_backtest_last_result_v1";

  const state = {
    plans: [],
    previewComponents: [],
    chart: null,
    nextPlanId: 1,
    nextRowId: 1,
    lastResult: null,
    backtestProgress: null,
    expandedPlans: new Set(),
    savedComparisons: []  // { id, label, createdAt, nav, rebalanceDates }
  };

  const metricsConfig = [
    ["total_return",           "区间总收益",    "pct"],
    ["annual_return",          "年化收益率",    "pct"],
    ["annual_volatility",      "年化波动率",    "pct"],
    ["sharpe_ratio",           "夏普比率",      "num"],
    ["sortino_ratio",          "索提诺比率",    "num"],
    ["calmar_ratio",           "卡玛比率",      "num"],
    ["max_drawdown",           "最大回撤",      "pct"],
    ["win_rate",               "胜率",          "pct"]
  ];

  const metricDescriptions = {
    total_return: "回测区间内净值从起点到终点的累计涨跌幅，用来直接看这段时间赚亏了多少。",
    annual_return: "把区间收益换算成年化口径，便于和不同长度的回测或基准做横向比较。",
    annual_volatility: "日收益波动按 252 个交易日年化后的结果，用来衡量净值起伏和不稳定程度。",
    sharpe_ratio: "衡量每承担一单位总波动获得的超额收益，已扣除无风险利率，通常越高越好。",
    sortino_ratio: "衡量每承担一单位下行波动获得的超额收益，只惩罚亏损方向的波动，通常越高越好。",
    calmar_ratio: "年化收益除以最大回撤绝对值，用来观察收益是否足以覆盖最深回撤压力。",
    max_drawdown: "回测期间净值从历史高点跌到低点的最大跌幅，用来评估最糟糕的回撤体验。",
    win_rate: "日收益为正的交易日占比，用来观察上涨天数的比例，不代表单次交易胜率。"
  };

  const rebalanceModeLabels = {
    none: "仅按计划生效日",
    monthly: "按月再平衡",
    quarterly: "按季度再平衡",
    semiannual: "按半年再平衡",
    custom: "自定义日期"
  };

  const rebalanceModeCompactLabels = {
    none: "仅按计划",
    monthly: "按月",
    quarterly: "按季度",
    semiannual: "按半年",
    custom: "自定义"
  };

  /* ────────────────────────────────────────────────
     Helpers
  ──────────────────────────────────────────────── */
  function normalizeCode(raw) {
    const text = String(raw ?? "").trim();
    if (!text) return "";
    const direct = text.match(/\d{6}/);
    if (direct) return direct[0];
    const digits = text.replace(/\D/g, "");
    if (!digits) return "";
    return digits.slice(-6).padStart(6, "0");
  }

  function makeRow(code = "", name = "", weight = 0) {
    return {
      id: state.nextRowId++,
      code: normalizeCode(code),
      name: String(name ?? "").trim(),
      weight: Number.isFinite(Number(weight)) ? Number(weight) : 0
    };
  }

  function sumWeights(rows) {
    return rows.reduce((acc, row) => acc + (Number(row.weight) || 0), 0);
  }

  function normalizeWeights(rows, decimals = 4) {
    const validRows = rows.filter(row => normalizeCode(row.code));
    if (!validRows.length) return;
    let baseWeights = validRows.map(row => Number(row.weight) || 0);
    const total = baseWeights.reduce((a, b) => a + b, 0);
    if (total <= 0) baseWeights = validRows.map(() => 1);
    const scale = 10 ** decimals;
    const sum = baseWeights.reduce((a, b) => a + b, 0);
    const rawUnits = baseWeights.map(w => (w / sum) * 100 * scale);
    const floorUnits = rawUnits.map(v => Math.floor(v));
    let remain = Math.round(100 * scale - floorUnits.reduce((a, b) => a + b, 0));
    const fractions = rawUnits
      .map((v, i) => ({ i, frac: v - floorUnits[i] }))
      .sort((a, b) => b.frac - a.frac);
    for (let idx = 0; idx < remain && idx < fractions.length; idx++) {
      floorUnits[fractions[idx].i] += 1;
    }
    validRows.forEach((row, idx) => { row.weight = floorUnits[idx] / scale; });
  }

  function distributeRemainingWeights(rows, decimals = 4) {
    const validRows = rows.filter(row => normalizeCode(row.code));
    if (!validRows.length) return { ok: false, message: "没有可分配权重的有效成分股。" };
    if (validRows.some(row => (Number(row.weight) || 0) < 0))
      return { ok: false, message: "存在负权重，请先修正。" };
    const zeroRows = validRows.filter(row => Math.abs(Number(row.weight) || 0) < 1e-12);
    if (!zeroRows.length) return { ok: false, message: "当前没有权重为 0 的成分股可分配。" };
    const fixedTotal = validRows.reduce((acc, row) => {
      const w = Number(row.weight) || 0;
      return acc + (w > 0 ? w : 0);
    }, 0);
    const remaining = 100 - fixedTotal;
    if (remaining < -0.00005)
      return { ok: false, message: `非零权重合计 ${fixedTotal.toFixed(4)}%，已超 100%。` };
    if (remaining <= 0.00005) return { ok: false, message: "剩余权重为 0，无需分配。" };
    const scale = 10 ** decimals;
    const totalUnits = Math.round(remaining * scale);
    const baseUnits = Math.floor(totalUnits / zeroRows.length);
    let remainUnits = totalUnits - baseUnits * zeroRows.length;
    zeroRows.forEach(row => {
      const extra = remainUnits > 0 ? 1 : 0;
      row.weight = (baseUnits + extra) / scale;
      remainUnits -= extra;
    });
    return { ok: true, assignedCount: zeroRows.length, remaining };
  }

  function parseCustomDates() {
    return document.getElementById("customDates").value
      .split("\n").map(l => l.trim()).filter(l => l);
  }

  function getBackendUrl() {
    return document.getElementById("backendUrl").value.trim().replace(/\/$/, "");
  }

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function formatElapsed(seconds) {
    const value = Math.max(0, Number(seconds) || 0);
    if (value < 60) return `${value.toFixed(1)} 秒`;
    const whole = Math.round(value);
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    const secs = whole % 60;
    if (hours > 0) return `${hours} 小时 ${String(minutes).padStart(2, "0")} 分 ${String(secs).padStart(2, "0")} 秒`;
    return `${minutes} 分 ${String(secs).padStart(2, "0")} 秒`;
  }


  /* ────────────────────────────────────────────────
     Comparison management
  ──────────────────────────────────────────────── */
  function loadComparisons() {
    try {
      const raw = localStorage.getItem(COMPARISON_STORAGE_KEY);
      if (!raw) return [];
      const data = JSON.parse(raw);
      return Array.isArray(data) ? data : [];
    } catch (e) { return []; }
  }

  function saveComparisons() {
    try {
      localStorage.setItem(COMPARISON_STORAGE_KEY, JSON.stringify(state.savedComparisons));
    } catch (e) { /* ignore */ }
  }

  function saveCurrentToComparison() {
    if (!state.lastResult?.nav?.length) return;
    const selectedMode = document.getElementById("rebalanceMode").value;
    const label = `${getRebalanceModeLabel(selectedMode, selectedMode, true)} \u00b7 ${new Date().toLocaleString("zh-CN", { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" })}`;
    const comp = {
      id: Date.now(),
      label,
      createdAt: new Date().toISOString(),
      nav: state.lastResult.nav,
      rebalanceDates: state.lastResult.applied_rebalance_dates || [],
      metrics: state.lastResult.metrics || {}
    };
    state.savedComparisons.push(comp);
    saveComparisons();
    renderComparisonBar();
    refreshChart();
    upsertStatus(`已保存对比："${label}"。`);
  }

  function removeComparison(id) {
    state.savedComparisons = state.savedComparisons.filter(c => c.id !== id);
    saveComparisons();
    renderComparisonBar();
    refreshChart();
  }

  function clearComparisons() {
    state.savedComparisons = [];
    saveComparisons();
    renderComparisonBar();
    refreshChart();
  }

  function getSelectedComparisonIds() {
    return [...document.querySelectorAll(".comp-check:checked")].map(cb => Number(cb.value));
  }

  function renderComparisonBar() {
    const bar = document.getElementById("comparisonBar");
    const container = document.getElementById("comparisonCheckboxes");
    if (!state.savedComparisons.length) {
      bar.style.display = "none";
      return;
    }
    bar.style.display = "flex";
    const COMP_COLORS = ["#ef4444", "#8b5cf6", "#06b6d4", "#f59e0b", "#10b981", "#ec4899", "#6366f1", "#14b8a6"];
    container.innerHTML = state.savedComparisons.map((comp, idx) => {
      const color = COMP_COLORS[idx % COMP_COLORS.length];
      return `<label class="bm-label" style="margin-right:6px;">
        <input type="checkbox" class="comp-check" value="${comp.id}" checked />
        <span class="bm-swatch" style="background:${color};"></span>${comp.label}
        <button class="danger" style="font-size:9px;padding:1px 5px;margin-left:3px;" onclick="event.stopPropagation();removeComparison(${comp.id})">\u00d7</button>
      </label>`;
    }).join("");
  }

  /* ────────────────────────────────────────────────
     Periodic returns rendering
  ──────────────────────────────────────────────── */
  let monthlyHeatmapChart = null;

  function renderPeriodicReturns(periodicData) {
    const container = document.getElementById("periodicReturns");
    if (!periodicData?.annual?.length && !periodicData?.monthly?.length) {
      container.style.display = "none";
      return;
    }
    container.style.display = "block";
    const annualSorted = [...(periodicData.annual || [])].sort((a, b) => b.year - a.year);
    const tableHtml = `
      <div class="panel returns-card">
        <div class="returns-card-head">
          <div class="returns-card-title">年度收益</div>
          <div class="returns-card-meta">${annualSorted.length} 年</div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>年份</th><th>收益</th></tr></thead>
            <tbody>
              ${annualSorted.map(item => {
                const cls = item.return > 0 ? "positive" : item.return < 0 ? "negative" : "";
                const pct = item.return * 100;
                const sign = pct > 0 ? "+" : "";
                return `<tr><td>${item.year}</td><td class="${cls}">${sign}${pct.toFixed(2)}%</td></tr>`;
              }).join("")}
            </tbody>
          </table>
        </div>
      </div>`;
    document.getElementById("annualReturnsTable").innerHTML = tableHtml;

    const monthlyData = periodicData.monthly || [];
    if (monthlyData.length) {
      const years = [...new Set(monthlyData.map(d => d.year))].sort();
      const months = [1,2,3,4,5,6,7,8,9,10,11,12];
      const yearIndex = new Map(years.map((year, index) => [year, index]));
      const heatData = monthlyData.map(d => [d.month - 1, yearIndex.get(d.year), d.return]);
      const maxAbs = Math.max(0.001, ...monthlyData.map(d => Math.abs(d.return)));
      const dom = document.getElementById("monthlyHeatmap");
      const heatmapHeight = Math.min(720, Math.max(420, 150 + years.length * 34));
      dom.style.height = `${heatmapHeight}px`;
      const chartWidth = dom.getBoundingClientRect().width || 720;
      const showLabel = years.length <= 8 && chartWidth >= 620;
      const legendHeight = Math.min(230, Math.max(150, heatmapHeight - 190));
      if (!monthlyHeatmapChart) monthlyHeatmapChart = echarts.init(dom);
      monthlyHeatmapChart.setOption({
        title: {
          text: "月度收益",
          subtext: showLabel ? "每格显示当月收益率" : "悬停查看当月收益率",
          left: 18,
          top: 14,
          textStyle: { fontSize: 14, fontWeight: 700, color: "#1e293b" },
          subtextStyle: { fontSize: 11, color: "#64748b", lineHeight: 16 }
        },
        tooltip: {
          confine: true,
          backgroundColor: "#fff",
          borderColor: "#e2e8f0",
          borderWidth: 1,
          padding: [10, 14],
          textStyle: { color: "#1e293b", fontSize: 13 },
          extraCssText: "box-shadow:0 8px 24px rgba(15,23,42,.12);border-radius:8px;",
          formatter: p => {
            if (!p.data) return "";
            const pct = p.data[2] * 100;
            const val = pct.toFixed(2);
            const sign = pct > 0 ? "+" : "";
            const color = pct > 0 ? "#16a34a" : pct < 0 ? "#dc2626" : "#64748b";
            return `<span style="font-size:12px;color:#64748b">${years[p.data[1]]}年${p.data[0] + 1}月</span><br/><span style="font-size:18px;font-weight:700;color:${color}">${sign}${val}%</span>`;
          }
        },
        grid: { top: 86, left: 58, right: 96, bottom: 30, containLabel: true },
        xAxis: {
          type: "category", data: months.map(m => `${m}月`),
          position: "top",
          axisLabel: { fontSize: 11, color: "#64748b", margin: 10 },
          axisLine: { show: false },
          axisTick: { show: false },
          splitArea: { show: false }
        },
        yAxis: {
          type: "category", data: years,
          axisLabel: { fontSize: 11, color: "#64748b", fontWeight: 600, margin: 12 },
          axisLine: { show: false },
          axisTick: { show: false },
          splitArea: { show: false }
        },
        visualMap: {
          min: -maxAbs, max: maxAbs, calculable: false,
          orient: "vertical", right: 22, top: 108, itemWidth: 10, itemHeight: legendHeight,
          text: ["高收益", "低收益"], textGap: 10, textStyle: { color: "#64748b", fontSize: 10, fontWeight: 600 },
          formatter: v => (v * 100).toFixed(1) + "%",
          inRange: { color: ["#b91c1c", "#ef8a8a", "#fee2e2", "#f8fafc", "#d9f99d", "#65c98a", "#0f766e"] }
        },
        series: [{
          type: "heatmap", data: heatData,
          animationDuration: 350,
          label: {
            show: showLabel,
            fontSize: years.length <= 5 ? 11 : 10,
            fontWeight: 700,
            formatter: p => {
              const pct = p.data[2] * 100;
              const sign = pct > 0 ? "+" : "";
              return `${sign}${pct.toFixed(1)}%`;
            },
            color: "#1e293b",
            overflow: "truncate"
          },
          emphasis: {
            itemStyle: {
              shadowBlur: 14,
              shadowColor: "rgba(15,23,42,0.18)",
              borderColor: "#0f172a",
              borderWidth: 1
            }
          },
          itemStyle: { borderColor: "#fff", borderWidth: 3, borderRadius: 4 }
        }]
      }, true);
      requestAnimationFrame(() => monthlyHeatmapChart.resize());
    } else if (monthlyHeatmapChart) {
      monthlyHeatmapChart.clear();
    }
  }
  /* ────────────────────────────────────────────────
     localStorage persistence
  ──────────────────────────────────────────────── */
  let saveTimer = null;

  function saveToStorage() {
    syncPlansFromDom();
    const indicator = document.getElementById("saveIndicator");
    try {
      const data = {
        v: 1,
        nextPlanId: state.nextPlanId,
        nextRowId: state.nextRowId,
        plans: state.plans.map(plan => ({
          id: plan.id,
          effectiveDate: plan.effectiveDate,
          components: plan.components.map(r => ({
            id: r.id, code: r.code, name: r.name, weight: r.weight
          }))
        })),
        params: {
          startDate: document.getElementById("startDate").value,
          endDate: document.getElementById("endDate").value,
          rebalanceMode: document.getElementById("rebalanceMode").value,
          riskFreeRate: document.getElementById("riskFreeRate").value,
          backendUrl: document.getElementById("backendUrl").value,
          customDates: document.getElementById("customDates").value,
          benchmarks: getSelectedBenchmarks(),
          commissionRate: document.getElementById("commissionRate").value,
          stampDutyRate: document.getElementById("stampDutyRate").value,
          slippageRate: document.getElementById("slippageRate").value,
          allowCash: document.getElementById("allowCash").checked
        }
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
      indicator.textContent = "已自动保存";
      indicator.style.color = "";
    } catch (e) {
      indicator.textContent = "保存失败";
      indicator.style.color = "#ef4444";
    }
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveToStorage, 400);
  }

  function loadFromStorage() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      if (!data?.v || !Array.isArray(data.plans)) return null;
      return data;
    } catch (e) {
      return null;
    }
  }

  /* ────────────────────────────────────────────────
     Plan management
  ──────────────────────────────────────────────── */
  function createPlan(effectiveDate, components = null) {
    const plan = {
      id: state.nextPlanId++,
      effectiveDate,
      components: components?.map(c => makeRow(c.code, c.name, c.weight)) ?? [makeRow()]
    };
    return plan;
  }

  function addPlan(copyLast = true) {
    syncPlansFromDom();
    const today = new Date().toISOString().slice(0, 10);
    let plan;
    if (copyLast && state.plans.length) {
      const last = state.plans[state.plans.length - 1];
      const copyDate = new Date(last.effectiveDate || today);
      copyDate.setMonth(copyDate.getMonth() + 1);
      plan = createPlan(copyDate.toISOString().slice(0, 10), last.components);
    } else {
      plan = createPlan(today, null);
    }
    state.plans.push(plan);
    state.expandedPlans.add(plan.id);
    renderPlans();
    renderPlanSelect();
    saveToStorage();
  }

  function removePlan(planId) {
    if (state.plans.length <= 1) return;
    state.plans = state.plans.filter(p => p.id !== planId);
    state.expandedPlans.delete(planId);
    renderPlans();
    renderPlanSelect();
    saveToStorage();
  }

  function findPlan(planId) {
    return state.plans.find(p => p.id === planId);
  }

  function exportPlans() {
    syncPlansFromDom();
    const data = {
      v: 1,
      exported_at: new Date().toISOString(),
      plans: state.plans.map(plan => ({
        effectiveDate: plan.effectiveDate,
        components: plan.components.map(r => ({
          code: r.code, name: r.name, weight: r.weight
        }))
      }))
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `backtest_plans_${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    upsertStatus("调仓计划已导出。");
  }

  function importPlans() {
    const file = document.getElementById("importPlansFile").files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const data = JSON.parse(reader.result);
        if (!data?.plans?.length) throw new Error("文件中没有有效的调仓计划");
        syncPlansFromDom();
        const imported = data.plans.map(p => createPlan(
          p.effectiveDate || "",
          (p.components || []).map(c => makeRow(c.code, c.name, c.weight))
        ));
        state.plans = imported;
        state.expandedPlans.clear();
        imported.forEach(p => state.expandedPlans.add(p.id));
        renderPlans();
        renderPlanSelect();
        saveToStorage();
        upsertStatus(`已导入 ${imported.length} 条调仓计划。`);
      } catch (err) {
        upsertStatus(`导入失败：${err.message}`);
      }
    };
    reader.readAsText(file);
    document.getElementById("importPlansFile").value = "";
  }

  /* ────────────────────────────────────────────────
     Renders
  ──────────────────────────────────────────────── */
  function renderPlanSelect() {
    const select = document.getElementById("targetPlanSelect");
    const prev = select.value;
    select.innerHTML = "";
    state.plans.forEach((plan, idx) => {
      const opt = document.createElement("option");
      opt.value = String(plan.id);
      opt.textContent = `计划${idx + 1}（${plan.effectiveDate || "未设置"}）`;
      select.appendChild(opt);
    });
    if ([...select.options].some(o => o.value === prev)) select.value = prev;
  }

  function renderPreviewTable() {
    const tbody = document.getElementById("previewTableBody");
    const wrap = document.getElementById("previewWrap");
    const rows = state.previewComponents.slice(0, 100);
    if (!rows.length) { wrap.style.display = "none"; return; }
    wrap.style.display = "block";
    tbody.innerHTML = rows.map(row => `
      <tr>
        <td>${row.code}</td>
        <td>${row.name || "-"}</td>
        <td>${Number(row.weight).toFixed(4)}</td>
      </tr>
    `).join("");
  }

  function renderPlans() {
    const container = document.getElementById("plansContainer");
    if (!state.plans.length) {
      container.innerHTML = `<div class="muted">暂无调仓计划。</div>`;
      return;
    }
    container.innerHTML = state.plans.map((plan, index) => {
      const total = sumWeights(plan.components);
      const allowCash = document.getElementById("allowCash").checked;
      let sumClass;
      if (allowCash) {
        sumClass = total <= 100.0001 ? "sum-tag" : "sum-tag warn";
      } else {
        sumClass = Math.abs(total - 100) <= 0.05 ? "sum-tag" : "sum-tag warn";
      }
      const isOpen = state.expandedPlans.has(plan.id);

      const rows = plan.components.map(row => `
        <tr>
          <td>
            <input type="text" value="${row.code}"
              data-action="edit-cell" data-plan-id="${plan.id}" data-row-id="${row.id}" data-field="code"
              placeholder="000001" style="width:78px;" />
          </td>
          <td>
            <input type="text" value="${row.name || ""}"
              data-action="edit-cell" data-plan-id="${plan.id}" data-row-id="${row.id}" data-field="name"
              placeholder="可选" style="width:100%;" />
          </td>
          <td>
            <input type="number" step="0.0001" value="${Number(row.weight) || 0}"
              data-action="edit-cell" data-plan-id="${plan.id}" data-row-id="${row.id}" data-field="weight"
              style="width:88px;" />
          </td>
          <td>
            <button class="danger"
              data-action="remove-row" data-plan-id="${plan.id}" data-row-id="${row.id}"
              style="padding:3px 7px; font-size:11px;">×</button>
          </td>
        </tr>
      `).join("");

      return `
        <div class="plan-card">
          <div class="plan-card-header ${isOpen ? "open" : ""}"
               data-action="toggle-plan" data-plan-id="${plan.id}">
            <span class="plan-title">计划 ${index + 1}&nbsp;·&nbsp;${plan.effectiveDate || "未设日期"}</span>
            <span class="${sumClass}" data-role="weight-sum" data-plan-id="${plan.id}">${total.toFixed(2)}%</span>
            <span class="chevron ${isOpen ? "open" : ""}">▾</span>
          </div>
          <div class="plan-card-body ${isOpen ? "open" : ""}">
            <div class="row" style="margin-bottom:8px;">
              <label>生效日</label>
              <input type="date" value="${plan.effectiveDate}"
                data-action="edit-plan-date" data-plan-id="${plan.id}" />
            </div>
            <div class="plan-actions">
              <button class="light" data-action="add-row" data-plan-id="${plan.id}">＋ 成分股</button>
              <button class="light" data-action="fill-remaining" data-plan-id="${plan.id}">分配剩余</button>
              <button class="secondary" data-action="equal-weight" data-plan-id="${plan.id}">均等权重</button>
              <button class="danger" data-action="remove-plan" data-plan-id="${plan.id}"
                ${state.plans.length <= 1 ? "disabled" : ""}>删除计划</button>
            </div>
            <div class="table-wrap" style="max-height:200px;">
              <table>
                <thead>
                  <tr>
                    <th style="width:82px;">代码</th>
                    <th>名称</th>
                    <th style="width:93px;">权重(%)</th>
                    <th style="width:44px;"></th>
                  </tr>
                </thead>
                <tbody>
                  ${rows || `<tr><td colspan="4" class="muted">暂无成分股</td></tr>`}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

  function upsertStatus(text) {
    document.getElementById("statusBar").textContent = text;
  }

  function renderBacktestProgress(task = null) {
    const panel = document.getElementById("progressPanel");
    const stageEl = document.getElementById("progressStage");
    const percentEl = document.getElementById("progressPercent");
    const fillEl = document.getElementById("progressFill");
    const detailEl = document.getElementById("progressDetail");
    const elapsedEl = document.getElementById("progressElapsed");

    if (!task) {
      state.backtestProgress = null;
      panel.style.display = "none";
      panel.dataset.status = "queued";
      stageEl.textContent = "等待执行";
      percentEl.textContent = "0%";
      fillEl.style.width = "0%";
      detailEl.textContent = "尚未开始。";
      elapsedEl.textContent = "当前耗时：0.0 秒";
      return;
    }

    const status = task.status || "running";
    const pct = Math.max(0, Math.min(100, Number(task.progress_pct) || 0));
    const currentStep = Number(task.current_step) || 0;
    const totalSteps = Number(task.total_steps) || 0;
    const detailSuffix = totalSteps > 0 ? ` · ${currentStep}/${totalSteps}` : "";
    const elapsedLabel = status === "running" ? "当前耗时" : "累计耗时";

    panel.style.display = "block";
    panel.dataset.status = status;
    state.backtestProgress = { ...task, status, progress_pct: pct };
    stageEl.textContent = task.stage_label || "执行中";
    percentEl.textContent = `${Math.round(pct)}%`;
    fillEl.style.width = `${pct}%`;
    detailEl.textContent = `${task.message || "正在执行回测…"}${detailSuffix}`;
    elapsedEl.textContent = `${elapsedLabel}：${formatElapsed(task.elapsed_seconds)}`;
  }

  function updatePlanWeightBadge(planId) {
    const plan = findPlan(planId);
    const badge = document.querySelector(`[data-role="weight-sum"][data-plan-id="${planId}"]`);
    if (!plan || !badge) return;
    const total = sumWeights(plan.components);
    const allowCash = document.getElementById("allowCash").checked;
    const cashText = allowCash && total < 99.999 ? ` \u00b7 现金${((100 - total)).toFixed(2)}%` : "";
    badge.textContent = `${total.toFixed(2)}%${cashText}`;
    if (allowCash) {
      badge.className = total <= 100.0001 ? "sum-tag" : "sum-tag warn";
    } else {
      badge.className = Math.abs(total - 100) > 0.05 ? "sum-tag warn" : "sum-tag";
    }
  }

  function syncPlansFromDom() {
    const container = document.getElementById("plansContainer");
    if (!container) return;
    let shouldRefreshPlanSelect = false;

    container.querySelectorAll('[data-action="edit-plan-date"]').forEach(input => {
      const plan = findPlan(Number(input.dataset.planId));
      if (!plan) return;
      if (plan.effectiveDate !== input.value) {
        plan.effectiveDate = input.value;
        shouldRefreshPlanSelect = true;
      }
    });

    container.querySelectorAll('[data-action="edit-cell"]').forEach(input => {
      const plan = findPlan(Number(input.dataset.planId));
      const row = plan?.components.find(item => item.id === Number(input.dataset.rowId));
      if (!row) return;
      if (input.dataset.field === "code") row.code = input.value;
      else if (input.dataset.field === "name") row.name = input.value;
      else if (input.dataset.field === "weight") row.weight = Number(input.value) || 0;
    });

    if (shouldRefreshPlanSelect) renderPlanSelect();
  }

  function setWarnings(messages) {
    const box = document.getElementById("warningBox");
    if (!messages?.length) {
      box.style.display = "none";
      box.textContent = "";
      return;
    }
    box.style.display = "block";
    box.textContent = messages.join("\n");
  }

  function setFallbackLogs(logs) {
    const section = document.getElementById("fallbackLogSection");
    const body = document.getElementById("fallbackLogBody");
    const count = document.getElementById("fallbackLogCount");
    const toggle = document.getElementById("fallbackLogToggle");
    if (!logs?.length) {
      section.style.display = "none";
      return;
    }
    section.style.display = "block";
    body.textContent = logs.join("\n");
    count.textContent = `(${logs.length} 条)`;
    // Collapsed by default
    body.classList.remove("open");
    toggle.childNodes[0].textContent = "▸ 兜底数据源日志 ";
  }

  function formatMetric(value, mode) {
    if (!Number.isFinite(Number(value))) return "-";
    if (mode === "pct") return `${(Number(value) * 100).toFixed(2)}%`;
    return Number(value).toFixed(3);
  }

  function metricColorClass(key, value) {
    const v = Number(value);
    if (!Number.isFinite(v)) return "";
    switch (key) {
      case "total_return":
      case "annual_return":    return v > 0 ? "positive" : v < 0 ? "negative" : "";
      case "max_drawdown":     return "negative";
      case "sharpe_ratio":
      case "sortino_ratio":    return v >= 1 ? "positive" : v >= 0 ? "warning" : "negative";
      case "calmar_ratio":     return v >= 0.5 ? "positive" : v >= 0 ? "warning" : "negative";
      case "win_rate":         return v >= 0.5 ? "positive" : "warning";
      default: return "";
    }
  }

  function getRebalanceModeLabel(mode, fallback = "", compact = false) {
    const labelMap = compact ? rebalanceModeCompactLabels : rebalanceModeLabels;
    return labelMap[mode] || fallback || mode || "";
  }

  function renderMetrics(metrics = {}, comparisonMetrics = []) {
    document.getElementById("metricsContainer").innerHTML = metricsConfig.map(([key, label, mode]) => `
      <div class="metric">
        <div class="name-row">
          <div class="name">${label}</div>
          <span class="metric-info" tabindex="0" aria-label="${label}说明：${metricDescriptions[key]}" title="${metricDescriptions[key]}" data-tooltip="${metricDescriptions[key]}">i</span>
        </div>
        <div class="value ${metricColorClass(key, metrics[key])}">${formatMetric(metrics[key], mode)}</div>
        ${
          comparisonMetrics.length
            ? `<div class="submetrics">${comparisonMetrics.map(item => `
                <div class="submetric-row">
                  <span class="submetric-label">${getRebalanceModeLabel(item.mode, item.label, true)}</span>
                  <span class="submetric-value ${metricColorClass(key, item.metrics?.[key])}">
                    ${formatMetric(item.metrics?.[key], mode)}
                  </span>
                </div>
              `).join("")}</div>`
            : ""
        }
      </div>
    `).join("");
  }

  const BENCHMARK_COLORS = { "000300": "#f59e0b", "000905": "#10b981", "000001": "#8b5cf6" };
  const BENCHMARK_NAMES  = { "000300": "沪深300",  "000905": "中证500",  "000001": "上证指数" };

  function getSelectedBenchmarks() {
    return [...document.querySelectorAll(".bm-check:checked")].map(cb => cb.value);
  }

  function renderChart(nav = [], rebalanceDates = [], benchmarkNavs = {}, rebalanceHoldings = {}) {
    const dom = document.getElementById("navChart");
    if (!state.chart) state.chart = echarts.init(dom);

    const dates = nav.map(item => item.date);
    const values = nav.map(item => item.value);

    const drawdown = [];
    let peak = values[0] ?? 1;
    for (const v of values) {
      if (v > peak) peak = v;
      drawdown.push(peak > 0 ? (v - peak) / peak : 0);
    }

    const rebalanceDateSet = new Set(rebalanceDates);
    const dateSet = new Set(dates);
    const markLineData = rebalanceDates
      .filter(d => dateSet.has(d))
      .map(d => ({ xAxis: d }));
    const rebalancePoints = dates.flatMap((date, index) => {
      if (!rebalanceDateSet.has(date) || values[index] == null) return [];
      return [{ value: [date, values[index]] }];
    });

    // Build comparison series from saved comparisons
    const COMP_COLORS = ["#ef4444", "#8b5cf6", "#06b6d4", "#f59e0b", "#10b981", "#ec4899", "#6366f1", "#14b8a6"];
    const selectedCompIds = getSelectedComparisonIds();
    const comparisonSeries = state.savedComparisons
      .filter(c => selectedCompIds.includes(c.id) && c.nav?.length)
      .map((comp, idx) => {
        const compMap = {};
        (comp.nav || []).forEach(item => { compMap[item.date] = item.value; });
        const compValues = dates.map(d => compMap[d] ?? null);
        return {
          id: `comparison-${comp.id}`,
          name: comp.label,
          data: compValues,
          type: "line",
          xAxisIndex: 0, yAxisIndex: 0,
          smooth: true, showSymbol: false, connectNulls: true,
          lineStyle: { width: 1.8, color: COMP_COLORS[idx % COMP_COLORS.length], type: "dashed" }
        };
      });

    // Build benchmark series (filtered by checkbox state)
    const selectedBm = getSelectedBenchmarks();
    const benchmarkSeries = selectedBm
      .filter(code => benchmarkNavs[code]?.length)
      .map(code => {
        const bmMap = {};
        (benchmarkNavs[code] || []).forEach(item => { bmMap[item.date] = item.value; });
        let bmValues = dates.map(d => bmMap[d] ?? null);
        // Normalise to start at 1.0 if raw index points are returned.
        // Use the first valid value within the chart's date range as baseline.
        const firstIdx = bmValues.findIndex(v => v !== null);
        if (firstIdx >= 0) {
          const baseline = bmValues[firstIdx];
          if (Math.abs(baseline) > 5) {
            bmValues = bmValues.map(v => v !== null ? v / baseline : null);
          }
          // Back-fill nulls before first valid point so benchmark aligns from chart start
          for (let i = firstIdx - 1; i >= 0; i--) {
            bmValues[i] = bmValues[i + 1];
          }
        }
        return {
          id: `benchmark-${code}`,
          name: BENCHMARK_NAMES[code] || code,
          data: bmValues,
          type: "line",
          xAxisIndex: 0, yAxisIndex: 0,
          smooth: true, showSymbol: false, connectNulls: true,
          lineStyle: { width: 1.5, color: BENCHMARK_COLORS[code] || "#888", type: "solid" }
        };
      });

    state.chart.setOption({
      animationDuration: 500,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross", link: [{ xAxisIndex: "all" }] },
        backgroundColor: "rgba(15,23,42,0.88)",
        borderColor: "#334155",
        textStyle: { color: "#f1f5f9", fontSize: 12 },
        formatter(params) {
          if (!params.length) return "";
          const date = params[0].axisValue;
          let html = `<div style="margin-bottom:4px;font-weight:600;">${date}</div>`;

          // ── Rebalancing date: show holdings ──
          if (rebalanceDateSet.has(date)) {
            html += `<div style="color:#fbbf24;font-size:11px;margin-bottom:3px;">▣ 调仓日</div>`;
            const holdings = rebalanceHoldings[date];
            if (holdings?.length) {
              const top = holdings.slice(0, 6);
              top.forEach(h => {
                html += `<div style="font-size:11px;color:#cbd5e1;">${h.code}` +
                  `${h.name ? " " + h.name : ""} ` +
                  `<b style="color:#f1f5f9">${Number(h.weight).toFixed(2)}%</b></div>`;
              });
              if (holdings.length > 6) {
                html += `<div style="font-size:10px;color:#64748b;">…另有 ${holdings.length - 6} 只</div>`;
              }
            }
            html += `<div style="border-top:1px solid #334155;margin:5px 0 3px;"></div>`;
          }

          // ── Series values ──
          for (const p of params) {
            if (p.seriesName === "调仓日" || p.seriesName === "调仓日光晕") continue;
            if (p.seriesName === "指数净值" && p.data != null) {
              const v = p.data;
              const ret = ((v - 1) * 100).toFixed(2);
              const col = v >= 1 ? "#34d399" : "#f87171";
              html += `净值 <b>${v.toFixed(4)}</b>&nbsp; 累计 <span style="color:${col}">${ret}%</span><br/>`;
            } else if (p.seriesName === "回撤" && p.data != null) {
              html += `回撤 <span style="color:#f87171">${(p.data * 100).toFixed(2)}%</span>`;
            } else if (p.data != null) {
              const v = Number(p.data);
              const ret = ((v - 1) * 100).toFixed(2);
              const col = v >= 1 ? "#34d399" : "#f87171";
              html += `<span style="color:${p.color}">─</span> ${p.seriesName} ` +
                `<b>${v.toFixed(4)}</b> <span style="color:${col}">${ret}%</span><br/>`;
            }
          }
          return html;
        }
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { top: 20, left: 52, right: 16, bottom: "36%" },
        { top: "68%", left: 52, right: 16, bottom: 28 }
      ],
      xAxis: [
        {
          type: "category", data: dates, gridIndex: 0,
          axisLabel: { show: false },
          axisLine: { lineStyle: { color: "#e2e8f0" } },
          axisTick: { show: false }
        },
        {
          type: "category", data: dates, gridIndex: 1,
          axisLabel: { color: "#64748b", fontSize: 11 },
          axisLine: { lineStyle: { color: "#e2e8f0" } }
        }
      ],
      yAxis: [
        {
          type: "value", scale: true, gridIndex: 0,
          axisLabel: { color: "#475569" },
          splitLine: { lineStyle: { color: "#e2e8f0" } }
        },
        {
          type: "value", gridIndex: 1, min: "dataMin",
          axisLabel: {
            color: "#94a3b8", fontSize: 10,
            formatter: v => v === 0 ? "0" : (v * 100).toFixed(0) + "%"
          },
          splitLine: { lineStyle: { color: "#f1f5f9" } }
        }
      ],
      series: [
        {
          id: "nav-series",
          name: "指数净值",
          data: values,
          type: "line",
          xAxisIndex: 0, yAxisIndex: 0,
          smooth: true, showSymbol: false,
          lineStyle: { width: 2.2, color: "#0057b8" },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(0,87,184,0.2)" },
                { offset: 1, color: "rgba(0,87,184,0.02)" }
              ]
            }
          },
          markLine: markLineData.length ? {
            silent: true, symbol: "none",
            lineStyle: { color: "#94a3b8", type: "dashed", width: 1, opacity: 0.45 },
            label: { show: false },
            data: markLineData
          } : undefined
        },
        {
          id: "rebalance-point-glow",
          name: "调仓日光晕",
          data: rebalancePoints,
          type: "scatter",
          xAxisIndex: 0, yAxisIndex: 0,
          silent: true,
          symbolSize: 18,
          z: 5,
          itemStyle: { color: "rgba(251,191,36,0.16)" },
          tooltip: { show: false }
        },
        {
          id: "rebalance-point-series",
          name: "调仓日",
          data: rebalancePoints,
          type: "scatter",
          xAxisIndex: 0, yAxisIndex: 0,
          symbol: "circle",
          symbolSize: 9,
          z: 6,
          itemStyle: {
            color: "#fbbf24",
            borderColor: "#fff7d6",
            borderWidth: 2,
            shadowBlur: 12,
            shadowColor: "rgba(251,191,36,0.35)"
          },
          emphasis: {
            scale: true,
            itemStyle: {
              color: "#facc15",
              borderColor: "#ffffff",
              borderWidth: 2
            }
          },
          tooltip: { show: false }
        },
        {
          id: "drawdown-series",
          name: "回撤",
          data: drawdown,
          type: "line",
          xAxisIndex: 1, yAxisIndex: 1,
          smooth: false, showSymbol: false,
          lineStyle: { width: 1.5, color: "#b91c1c" },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(185,28,28,0.2)" },
                { offset: 1, color: "rgba(185,28,28,0.03)" }
              ]
            }
          }
        },
        ...comparisonSeries,
        ...benchmarkSeries
      ]
    }, {
      notMerge: false,
      replaceMerge: ["series"]
    });
  }

  // Re-render chart using stored result (called when benchmark/comparison checkboxes change)
  function refreshChart() {
    if (!state.lastResult) return;
    const r = state.lastResult;
    renderChart(
      r.nav || [],
      r.applied_rebalance_dates || [],
      r.benchmark_nav || {},
      r.rebalance_holdings || {}
    );
  }

  function renderReport(result) {
    const startDate = result.nav?.[0]?.date || "-";
    const endDate = result.nav?.[result.nav.length - 1]?.date || "-";
    const tradingDays = result.nav?.length || 0;
    const rebalanceCount = (result.applied_rebalance_dates || []).length;
    const m = result.metrics || {};
    const items = [
      ["回测区间", `${startDate} 至 ${endDate}`],
      ["交易日数", `${tradingDays} 天`],
      ["调仓执行次数", `${rebalanceCount} 次`]
    ];
    if (m.max_drawdown_start) {
      items.push(["最大回撤区间", `${m.max_drawdown_start} \u2192 ${m.max_drawdown_trough}${m.max_drawdown_recovery ? " \u2192 " + m.max_drawdown_recovery : "（未恢复）"}`]);
      if (m.max_drawdown_duration_days != null) {
        items.push(["回撤持续天数", `${m.max_drawdown_duration_days} 天`]);
      }
    }
    const costLog = result.cost_log || [];
    if (costLog.length) {
      const totalCost = costLog.reduce((s, c) => s + (Number(c.cost) || 0), 0);
      items.push(["累计交易成本", `${totalCost.toFixed(4)}%`]);
    }
    document.getElementById("reportBox").innerHTML = items.map(([label, value]) => `
      <div class="report-item">
        <div class="r-label">${label}</div>
        <div class="r-value">${value}</div>
      </div>
    `).join("");
  }

function renderBacktestNotes(result = null) {
    const sourceText = result?.data_source
      ? `股票行情优先使用 AKShare 前复权日线，若主数据源缺失则自动切换到兜底数据源；本次回测的数据源配置为：${result.data_source}。如发生切换，可在上方\u201c兜底数据源日志\u201d查看具体记录。`
      : `股票行情优先使用 AKShare 前复权日线，若主数据源缺失则自动切换到兜底数据源；如发生切换，可在结果区的\u201c兜底数据源日志\u201d查看具体记录。`;
    const missingText = result?.missing_data_policy
      ? `当成分股在某个交易日无行情（停牌或数据缺失）时，该头寸当日收益记为 0，资金等效留在原持仓；本次回测采用的处理方式为：${result.missing_data_policy}。`
      : `当成分股在某个交易日无行情（停牌或数据缺失）时，该头寸当日收益记为 0，资金等效留在原持仓，不做前值填充。`;
    let costText = "";
    const cc = result?.cost_config;
    if (cc && (cc.commission_rate > 0 || cc.stamp_duty_rate > 0 || cc.slippage_rate > 0)) {
      const parts = [];
      if (cc.commission_rate > 0) parts.push(`佣金 ${(cc.commission_rate * 100).toFixed(3)}%（双边）`);
      if (cc.stamp_duty_rate > 0) parts.push(`印花税 ${(cc.stamp_duty_rate * 100).toFixed(3)}%（卖方）`);
      if (cc.slippage_rate > 0) parts.push(`滑点 ${(cc.slippage_rate * 100).toFixed(3)}%（双边）`);
      costText = `<p><strong>交易成本：</strong>本次回测已计入交易成本 —— ${parts.join("、")}。每次调仓按换手率计算成本，从净值中扣除。</p>`;
    }
    document.getElementById("backtestNotesBody").innerHTML = `
      <p><strong>数据来源：</strong>${sourceText}</p>
      <p><strong>缺失数据处理：</strong>${missingText}</p>
      ${costText}
    `;
  }

  function exportCsv() {
    const result = state.lastResult;
    if (!result?.nav?.length) return;

    const bm = result.benchmark_nav || {};
    const navDates = (result.nav || []).map(item => item.date);
    const bmCodes = getSelectedBenchmarks().filter(code => bm[code]?.length);
    const bmMaps = {};
    bmCodes.forEach(code => {
      const rawMap = {};
      (bm[code] || []).forEach(item => { rawMap[item.date] = item.value; });
      const vals = navDates.map(d => rawMap[d] ?? null);
      const firstIdx = vals.findIndex(v => v !== null);
      const scale = firstIdx >= 0 && Math.abs(vals[firstIdx]) > 5 ? vals[firstIdx] : 1;
      bmMaps[code] = {};
      navDates.forEach((date, idx) => {
        bmMaps[code][date] = vals[idx] !== null ? vals[idx] / scale : null;
      });
    });

    const bmHeaders = bmCodes.map(c => BENCHMARK_NAMES[c] || c);
    const headers = ["日期", "净值", "累计收益%", ...bmHeaders];

    const rows = result.nav.map(item => {
      const ret = ((item.value - 1) * 100).toFixed(2);
      const bmVals = bmCodes.map(c => {
        const v = bmMaps[c]?.[item.date];
        return v != null ? v.toFixed(6) : "";
      });
      return [item.date, item.value.toFixed(6), ret, ...bmVals];
    });

    // Metrics section at bottom
    const metricsRows = [
      [],
      ["频率", getRebalanceModeLabel(result.selected_rebalance_mode, result.selected_rebalance_label)],
      ["指标", "数值"],
      ...metricsConfig.map(([key, label, mode]) => [label, formatMetric(result.metrics?.[key], mode)])
    ];
    (result.comparison_metrics || []).forEach(item => {
      metricsRows.push(
        [],
        ["频率", getRebalanceModeLabel(item.mode, item.label)],
        ["指标", "数值"],
        ...metricsConfig.map(([key, label, mode]) => [label, formatMetric(item.metrics?.[key], mode)])
      );
    });

    const allRows = [headers, ...rows, ...metricsRows];
    const csv = allRows.map(row => row.join(",")).join("\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const startDate = result.nav[0]?.date || "export";
    const endDate = result.nav[result.nav.length - 1]?.date || "";
    a.download = `backtest_${startDate}_${endDate}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function exportScreenshot() {
    if (!state.chart) return;
    const url = state.chart.getDataURL({
      type: "png",
      pixelRatio: 2,
      backgroundColor: "#ffffff"
    });
    const a = document.createElement("a");
    a.href = url;
    a.download = `backtest_chart_${new Date().toISOString().slice(0, 10)}.png`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function collectPayload() {
    syncPlansFromDom();
    const plans = state.plans
      .map(plan => ({
        effective_date: plan.effectiveDate,
        components: plan.components
          .map(row => ({
            code: normalizeCode(row.code),
            name: String(row.name || "").trim(),
            weight: Number(row.weight) || 0
          }))
          .filter(row => row.code)
      }))
      .filter(plan => plan.components.length > 0);

    if (!plans.length) throw new Error("至少需要一条包含成分股的调仓计划");

    const emptyDates = plans.filter(p => !p.effective_date);
    if (emptyDates.length) {
      throw new Error("部分调仓计划未设置生效日期，请检查后再执行回测");
    }

    const allowCash = document.getElementById("allowCash").checked;
    // Validate each plan\'s weight total is non-zero
    for (const plan of plans) {
      const total = plan.components.reduce((s, r) => s + (Number(r.weight) || 0), 0);
      if (total <= 0) {
        throw new Error(`存在权重总和为 0 的调仓计划（生效日 ${plan.effective_date}），请分配权重后再执行回测`);
      }
      if (!allowCash && total > 100.0001) {
        throw new Error(`调仓计划（生效日 ${plan.effective_date}）权重总和 ${total.toFixed(2)}% 超过 100%`);
      }
      if (allowCash && total > 100.0001) {
        throw new Error(`调仓计划（生效日 ${plan.effective_date}）权重总和 ${total.toFixed(2)}% 超过 100%，即使允许现金仓位也不能超过`);
      }
    }

    plans.sort((a, b) => a.effective_date < b.effective_date ? -1 : a.effective_date > b.effective_date ? 1 : 0);

    return {
      start_date: document.getElementById("startDate").value,
      end_date: document.getElementById("endDate").value,
      rebalance_mode: document.getElementById("rebalanceMode").value,
      custom_rebalance_dates: parseCustomDates(),
      risk_free_rate: Number(document.getElementById("riskFreeRate").value || 0),
      missing_data_policy: "hold_cash",
      benchmarks: getSelectedBenchmarks(),
      commission_rate: Number(document.getElementById("commissionRate").value || 0) / 100,
      stamp_duty_rate: Number(document.getElementById("stampDutyRate").value || 0) / 100,
      slippage_rate: Number(document.getElementById("slippageRate").value || 0) / 100,
      allow_cash: document.getElementById("allowCash").checked,
      plans
    };
  }

  /* ────────────────────────────────────────────────
     Actions
  ──────────────────────────────────────────────── */
  async function parseExcel() {
    const file = document.getElementById("fileInput").files[0];
    if (!file) { upsertStatus("请先选择 xls/xlsx 文件。"); return; }

    const backend = getBackendUrl();
    const formData = new FormData();
    formData.append("file", file);
    upsertStatus("正在解析 Excel 文件…");
    try {
      const resp = await fetch(`${backend}/api/parse-components`, { method: "POST", body: formData });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Excel 解析失败");
      state.previewComponents = (data.components || []).map(item =>
        makeRow(item.code, item.name, item.weight)
      );
      document.getElementById("applyPreviewBtn").disabled = state.previewComponents.length === 0;
      document.getElementById("importSummary").textContent =
        `${data.filename}：${data.row_count} 行，解析 ${data.parsed_count} 行，跳过 ${data.skipped_rows} 行。` +
        `代码列：${data.code_column}；权重列：${data.weight_column || "未识别(等权)"}。`;
      renderPreviewTable();
      upsertStatus("Excel 解析完成，可应用到任一调仓计划。");
    } catch (error) {
      upsertStatus(`Excel 解析失败：${error.message}`);
    }
  }

  function applyPreviewToPlan() {
    syncPlansFromDom();
    const targetId = Number(document.getElementById("targetPlanSelect").value);
    const plan = findPlan(targetId);
    if (!plan) { upsertStatus("未找到目标调仓计划。"); return; }
    if (!state.previewComponents.length) { upsertStatus("没有可应用的解析结果。"); return; }
    plan.components = state.previewComponents.map(item => makeRow(item.code, item.name, item.weight));
    state.expandedPlans.add(plan.id);
    renderPlans();
    saveToStorage();
    upsertStatus(`已将解析结果应用到调仓计划（${plan.effectiveDate}）。`);
  }

  async function waitForBacktestTask(taskId, backend) {
    for (;;) {
      const resp = await fetch(`${backend}/api/backtest/tasks/${taskId}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "获取回测进度失败");

      renderBacktestProgress(data);
      if (data.status === "completed" || data.status === "failed") return data;

      upsertStatus(data.message || "正在执行回测并拉取行情数据，请稍候…");
      await sleep(500);
    }
  }

  function saveLastResult() {
    if (!state.lastResult) return;
    try {
      const payload = {
        result: state.lastResult,
        savedAt: new Date().toISOString(),
        params: {
          startDate: document.getElementById("startDate").value,
          endDate: document.getElementById("endDate").value,
          rebalanceMode: document.getElementById("rebalanceMode").value,
          benchmarks: getSelectedBenchmarks()
        }
      };
      localStorage.setItem(LAST_RESULT_KEY, JSON.stringify(payload));
    } catch (e) { /* 结果太大时静默失败 */ }
  }

  function loadLastResult() {
    try {
      const raw = localStorage.getItem(LAST_RESULT_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      if (!data?.result?.nav?.length) return null;
      return data;
    } catch (e) {
      return null;
    }
  }

  function applyBacktestResult(data, elapsedSeconds) {
    state.lastResult = data;
    saveLastResult();
    renderMetrics(data.metrics || {}, data.comparison_metrics || []);
    renderChart(
      data.nav || [],
      data.applied_rebalance_dates || [],
      data.benchmark_nav || {},
      data.rebalance_holdings || {}
    );
    renderReport(data);
    renderPeriodicReturns(data.periodic_returns);
    renderBacktestNotes(data);
    document.getElementById("exportCsvBtn").disabled = false;
    document.getElementById("exportImgBtn").disabled = false;
    document.getElementById("saveComparisonBtn").style.display = "inline-block";
    const allWarnings = data.warnings || [];
    const fallbackLogs = allWarnings.filter(w => w.includes("使用兜底数据源"));
    const otherWarnings = allWarnings.filter(w => !w.includes("使用兜底数据源"));
    setWarnings(otherWarnings);
    setFallbackLogs(fallbackLogs);

    upsertStatus(
      `回测完成：${data.nav?.[0]?.date || "-"} 至 ${data.nav?.[data.nav.length - 1]?.date || "-"}，` +
      `共 ${data.nav?.length || 0} 个交易日，累计耗时 ${formatElapsed(elapsedSeconds)}。`
    );
  }

  async function runBacktest() {
    let payload;
    try { payload = collectPayload(); } catch (error) {
      upsertStatus(`参数错误：${error.message}`);
      return;
    }
    if (!payload.start_date || !payload.end_date) {
      upsertStatus("请先填写回测开始与结束日期。");
      return;
    }

    const backend = getBackendUrl();
    const btn = document.getElementById("runBacktestBtn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>&nbsp; 回测中…';

    renderBacktestProgress({
      status: "running",
      stage_label: "准备参数",
      progress_pct: 0,
      message: "正在创建回测任务…",
      elapsed_seconds: 0,
      current_step: 0,
      total_steps: 0
    });
    upsertStatus("正在创建回测任务…");
    setWarnings([]);
    setFallbackLogs([]);
    document.getElementById("exportCsvBtn").disabled = true;
    document.getElementById("exportImgBtn").disabled = true;

    try {
      const resp = await fetch(`${backend}/api/backtest/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const task = await resp.json();
      if (!resp.ok) throw new Error(task.detail || "回测任务创建失败");

      const taskResult = await waitForBacktestTask(task.task_id, backend);
      if (taskResult.status !== "completed") {
        throw new Error(taskResult.error || taskResult.message || "回测失败");
      }

      applyBacktestResult(taskResult.result || {}, taskResult.elapsed_seconds || 0);
    } catch (error) {
      if (!state.backtestProgress || state.backtestProgress.status === "running") {
        renderBacktestProgress({
          status: "failed",
          stage_label: "执行失败",
          progress_pct: state.backtestProgress?.progress_pct || 0,
          message: error.message,
          elapsed_seconds: state.backtestProgress?.elapsed_seconds || 0,
          current_step: 0,
          total_steps: 0
        });
      }
      upsertStatus(`回测失败：${error.message}`);
    } finally {
      btn.disabled = false;
      btn.innerHTML = "▶&nbsp; 执行回测";
    }
  }

  /* ────────────────────────────────────────────────
     Plan event delegation
  ──────────────────────────────────────────────── */
  function handlePlanEvents(event) {
    // Use closest so clicks on child elements (spans, etc.) still resolve the action
    const actionEl = event.target.closest("[data-action]");
    if (!actionEl) return;

    const action = actionEl.dataset.action;
    const planId = Number(actionEl.dataset.planId);
    const rowId = Number(actionEl.dataset.rowId);
    const plan = findPlan(planId);

    // ── Toggle collapse / expand ──
    if (action === "toggle-plan" && event.type === "click") {
      if (state.expandedPlans.has(planId)) {
        state.expandedPlans.delete(planId);
      } else {
        state.expandedPlans.add(planId);
      }
      renderPlans();
      return;
    }

    if (!plan) return;

    if (action === "add-row" && event.type === "click") {
      plan.components.push(makeRow());
      state.expandedPlans.add(planId);
      renderPlans();
      saveToStorage();
      return;
    }

    if (action === "remove-plan" && event.type === "click") {
      removePlan(planId);
      return;
    }

    if (action === "equal-weight" && event.type === "click") {
      normalizeWeights(plan.components);
      renderPlans();
      saveToStorage();
      return;
    }

    if (action === "fill-remaining" && event.type === "click") {
      const result = distributeRemainingWeights(plan.components);
      if (!result.ok) { upsertStatus(result.message); return; }
      renderPlans();
      saveToStorage();
      upsertStatus(`已将剩余 ${result.remaining.toFixed(4)}% 平均分配到 ${result.assignedCount} 只权重为 0 的成分股。`);
      return;
    }

    if (action === "remove-row" && event.type === "click") {
      plan.components = plan.components.filter(row => row.id !== rowId);
      if (!plan.components.length) plan.components.push(makeRow());
      renderPlans();
      saveToStorage();
      return;
    }

    if (action === "edit-plan-date" && event.type === "change") {
      plan.effectiveDate = actionEl.value;
      renderPlanSelect();
      scheduleSave();
      return;
    }

    if (action === "edit-cell" && (event.type === "input" || event.type === "change")) {
      const field = actionEl.dataset.field;
      const row = plan.components.find(item => item.id === rowId);
      if (!row) return;

      if (field === "code") {
        row.code = event.type === "change" ? normalizeCode(actionEl.value) : actionEl.value;
        if (event.type === "change") actionEl.value = row.code;
      } else if (field === "name") {
        row.name = actionEl.value;
      } else if (field === "weight") {
        row.weight = Number(actionEl.value) || 0;
      }

      if (event.type === "input") {
        updatePlanWeightBadge(planId);
        scheduleSave();
        return;
      }

      renderPlans();
      scheduleSave();
    }
  }

  /* ────────────────────────────────────────────────
     Init
  ──────────────────────────────────────────────── */
  function initDefaults() {
    const today = new Date();
    const end = today.toISOString().slice(0, 10);
    const start = new Date(today);
    start.setFullYear(today.getFullYear() - 3);

    const saved = loadFromStorage();

    if (saved) {
      state.nextPlanId = saved.nextPlanId || 1;
      state.nextRowId = saved.nextRowId || 1;
      state.plans = saved.plans.map(p => ({
        id: p.id,
        effectiveDate: p.effectiveDate,
        components: p.components.map(r => ({ id: r.id, code: r.code, name: r.name, weight: r.weight }))
      }));
      state.plans.forEach(p => state.expandedPlans.add(p.id));

      if (saved.params) {
        const p = saved.params;
        if (p.startDate) document.getElementById("startDate").value = p.startDate;
        if (p.endDate)   document.getElementById("endDate").value = p.endDate;
        if (p.rebalanceMode) document.getElementById("rebalanceMode").value = p.rebalanceMode;
        if (p.riskFreeRate)  document.getElementById("riskFreeRate").value = p.riskFreeRate;
        if (p.backendUrl)    document.getElementById("backendUrl").value = p.backendUrl;
        if (p.customDates)   document.getElementById("customDates").value = p.customDates;
        if (p.commissionRate != null) document.getElementById("commissionRate").value = p.commissionRate;
        if (p.stampDutyRate != null)  document.getElementById("stampDutyRate").value = p.stampDutyRate;
        if (p.slippageRate != null)   document.getElementById("slippageRate").value = p.slippageRate;
        if (p.allowCash != null)      document.getElementById("allowCash").checked = p.allowCash;
        if (Array.isArray(p.benchmarks)) {
          document.querySelectorAll(".bm-check").forEach(cb => {
            cb.checked = p.benchmarks.includes(cb.value);
          });
        }
      }
    } else {
      document.getElementById("startDate").value = start.toISOString().slice(0, 10);
      document.getElementById("endDate").value = end;
      const plan = createPlan(end, null);
      state.plans.push(plan);
      state.expandedPlans.add(plan.id);
    }

    // Sync custom-dates row visibility
    if (document.getElementById("rebalanceMode").value === "custom") {
      document.getElementById("customDatesRow").style.display = "block";
    }

    state.savedComparisons = loadComparisons();
    renderPlans();
    renderPlanSelect();
    renderPreviewTable();

    const lastResult = loadLastResult();
    if (lastResult) {
      state.lastResult = lastResult.result;
      renderMetrics(lastResult.result.metrics || {}, lastResult.result.comparison_metrics || []);
      renderChart(
        lastResult.result.nav || [],
        lastResult.result.applied_rebalance_dates || [],
        lastResult.result.benchmark_nav || {},
        lastResult.result.rebalance_holdings || {}
      );
      renderReport(lastResult.result);
      renderPeriodicReturns(lastResult.result.periodic_returns);
      renderBacktestNotes(lastResult.result);
      document.getElementById("exportCsvBtn").disabled = false;
      document.getElementById("exportImgBtn").disabled = false;
      document.getElementById("saveComparisonBtn").style.display = "inline-block";
      const allWarnings = lastResult.result.warnings || [];
      const fallbackLogs = allWarnings.filter(w => w.includes("使用兜底数据源"));
      const otherWarnings = allWarnings.filter(w => !w.includes("使用兜底数据源"));
      setWarnings(otherWarnings);
      setFallbackLogs(fallbackLogs);
      upsertStatus(`已恢复上次回测结果（${new Date(lastResult.savedAt).toLocaleString("zh-CN")}），点击"执行回测"可刷新。`);
    } else {
      renderMetrics();
      renderChart([], []);
      renderBacktestNotes();
    }
    renderComparisonBar();
    renderBacktestProgress(null);
  }

  /* ────────────────────────────────────────────────
     Event listeners
  ──────────────────────────────────────────────── */
  document.getElementById("parseFileBtn").addEventListener("click", parseExcel);
  document.getElementById("applyPreviewBtn").addEventListener("click", applyPreviewToPlan);
  document.getElementById("runBacktestBtn").addEventListener("click", runBacktest);
  document.getElementById("addPlanBtn").addEventListener("click", () => addPlan(true));
  document.getElementById("exportPlansBtn").addEventListener("click", exportPlans);
  document.getElementById("importPlansBtn").addEventListener("click", () => {
    document.getElementById("importPlansFile").click();
  });
  document.getElementById("importPlansFile").addEventListener("change", importPlans);

  document.getElementById("rebalanceMode").addEventListener("change", event => {
    document.getElementById("customDatesRow").style.display =
      event.target.value === "custom" ? "block" : "none";
    scheduleSave();
  });

  document.getElementById("startDate").addEventListener("change", scheduleSave);
  document.getElementById("endDate").addEventListener("change", scheduleSave);
  document.getElementById("riskFreeRate").addEventListener("change", scheduleSave);
  document.getElementById("backendUrl").addEventListener("change", scheduleSave);
  document.getElementById("customDates").addEventListener("input", scheduleSave);
  document.getElementById("commissionRate").addEventListener("change", scheduleSave);
  document.getElementById("stampDutyRate").addEventListener("change", scheduleSave);
  document.getElementById("slippageRate").addEventListener("change", scheduleSave);
  document.getElementById("allowCash").addEventListener("change", () => {
    renderPlans();
    scheduleSave();
  });

  document.getElementById("plansContainer").addEventListener("click", handlePlanEvents);
  document.getElementById("plansContainer").addEventListener("input", handlePlanEvents);
  document.getElementById("plansContainer").addEventListener("change", handlePlanEvents);

  document.getElementById("advToggle").addEventListener("click", () => {
    const body = document.getElementById("advBody");
    const btn = document.getElementById("advToggle");
    const isOpen = body.classList.toggle("open");
    btn.textContent = (isOpen ? "▾" : "▸") + " 高级设置";
  });

  document.getElementById("fallbackLogToggle").addEventListener("click", () => {
    const body = document.getElementById("fallbackLogBody");
    const count = document.getElementById("fallbackLogCount");
    const isOpen = body.classList.toggle("open");
    document.getElementById("fallbackLogToggle").childNodes[0].textContent =
      (isOpen ? "▾" : "▸") + " 兜底数据源日志 ";
  });

  document.getElementById("exportCsvBtn").addEventListener("click", exportCsv);
  document.getElementById("exportImgBtn").addEventListener("click", exportScreenshot);
  document.getElementById("saveComparisonBtn").addEventListener("click", saveCurrentToComparison);
  document.getElementById("clearComparisonsBtn").addEventListener("click", clearComparisons);

  document.getElementById("benchmarkBar").addEventListener("change", event => {
    if (event.target.classList.contains("bm-check")) refreshChart();
  });

  document.getElementById("comparisonBar").addEventListener("change", event => {
    if (event.target.classList.contains("comp-check")) refreshChart();
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.chart) state.chart.resize();
      if (state.lastResult?.periodic_returns) {
        renderPeriodicReturns(state.lastResult.periodic_returns);
      } else if (monthlyHeatmapChart) {
        monthlyHeatmapChart.resize();
      }
    }, 150);
  });

  initDefaults();
