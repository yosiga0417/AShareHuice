/* ────────────────────────────────────────────────
     State
  ──────────────────────────────────────────────── */
  // app.js v2 — holdings chart + auto-normalize (2026-05-15)
  const UI = {
    primary: '#0057b8', positive: '#0f766e', danger: '#b91c1c', warning: '#c2410c',
    subtle: '#64748b', subtleLight: '#94a3b8',
    text: '#1e293b', textDark: '#0f172a', textMuted: '#334155',
    surface: '#ffffff', surfaceSubtle: '#f8fafc',
    border: '#dbe4ef', borderLight: '#e2e8f0',
    tooltipBg: '#0f172a', tooltipText: '#f8fafc',
    trackBg: '#dbe7f5',
  };

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
    savedComparisons: []  // { id, name, label, createdAt, nav, rebalanceDates, meta }
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

  const MONTH_LABELS = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"];

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

  function assignWeightsToTarget(rows, baseWeights, target = 100, decimals = 4) {
    if (!rows.length) return false;
    const cleanWeights = baseWeights.map(weight => Math.max(0, Number(weight) || 0));
    const total = cleanWeights.reduce((a, b) => a + b, 0);
    if (total <= 0) return false;
    const scale = 10 ** decimals;
    const targetUnits = Math.round(target * scale);
    const rawUnits = cleanWeights.map(w => (w / total) * targetUnits);
    const floorUnits = rawUnits.map(v => Math.floor(v));
    let remain = targetUnits - floorUnits.reduce((a, b) => a + b, 0);
    const fractions = rawUnits
      .map((v, i) => ({ i, frac: v - floorUnits[i] }))
      .sort((a, b) => b.frac - a.frac);
    for (let idx = 0; idx < remain && idx < fractions.length; idx++) {
      floorUnits[fractions[idx].i] += 1;
    }
    rows.forEach((row, idx) => { row.weight = floorUnits[idx] / scale; });
    return true;
  }

  function normalizeWeights(rows, decimals = 4) {
    const validRows = rows.filter(row => normalizeCode(row.code));
    if (!validRows.length) return;
    let baseWeights = validRows.map(row => Number(row.weight) || 0);
    const total = baseWeights.reduce((a, b) => a + b, 0);
    if (total <= 0) baseWeights = validRows.map(() => 1);
    assignWeightsToTarget(validRows, baseWeights, 100, decimals);
  }

  function capOverweightTo100(rows, decimals = 4) {
    const validRows = rows.filter(row => normalizeCode(row.code));
    const total = sumWeights(validRows);
    if (total <= 100.0001) return { changed: false, total };
    const ok = assignWeightsToTarget(
      validRows,
      validRows.map(row => Number(row.weight) || 0),
      100,
      decimals
    );
    return { changed: ok, total };
  }

  function updateSelectedFileLabel(file) {
    const label = document.getElementById("fileNameText");
    if (!label) return;
    label.textContent = file?.name || "未选择文件";
    label.title = file?.name || "";
    label.classList.toggle("has-file", Boolean(file));
  }

  function handleComponentFileChange(event) {
    const file = event.target.files?.[0] || null;
    updateSelectedFileLabel(file);
    state.previewComponents = [];
    document.getElementById("applyPreviewBtn").disabled = true;
    renderPreviewTable();
    document.getElementById("importSummary").textContent = file
      ? `已选择 ${file.name}，点击“解析”读取成分股。`
      : "未导入文件。解析时会强制保留6位股票代码（如 002879）。";
  }

  function autoNormalizeWeights(plan, editedRowId) {
    const validRows = plan.components.filter(row => normalizeCode(row.code));
    if (validRows.length < 2) return false;
    const editedRow = validRows.find(r => r.id === editedRowId);
    if (!editedRow) return false;
    const editedWeight = Math.max(0, Number(editedRow.weight) || 0);
    const otherRows = validRows.filter(r => r.id !== editedRowId);
    const remaining = Math.max(0, 100 - editedWeight);
    const otherTotal = otherRows.reduce((s, r) => s + Math.max(0, Number(r.weight) || 0), 0);
    if (otherTotal <= 1e-10) {
      const each = remaining / otherRows.length;
      otherRows.forEach(r => { r.weight = each; });
    } else {
      const scale = remaining / otherTotal;
      otherRows.forEach(r => { r.weight = Math.max(0, (Number(r.weight) || 0)) * scale; });
    }
    // Precise normalization
    const allWeights = validRows.map(r => Math.max(0, Number(r.weight) || 0));
    return assignWeightsToTarget(validRows, allWeights, 100, 4);
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
  function debounce(fn, delay) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
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
  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function csvSafe(value) {
    const s = String(value ?? "");
    if (/[,"\n\r]/.test(s)) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }

  function normalizeValueSeries(series) {
    if (Array.isArray(series)) {
      const filtered = series.filter(item => item?.date);
      return {
        dates: filtered.map(item => item.date),
        values: filtered.map(item => Number(item.value))
      };
    }
    if (series && Array.isArray(series.dates) && Array.isArray(series.values)) {
      const limit = Math.min(series.dates.length, series.values.length);
      return {
        dates: series.dates.slice(0, limit),
        values: series.values.slice(0, limit).map(value => Number(value))
      };
    }
    return { dates: [], values: [] };
  }

  function hasValueSeriesData(series) {
    return normalizeValueSeries(series).dates.length > 0;
  }

  function normalizeBenchmarkNavs(benchmarkNavs = {}) {
    return Object.fromEntries(
      Object.entries(benchmarkNavs || {}).map(([code, series]) => [code, normalizeValueSeries(series)])
    );
  }

  function normalizeHoldingSeries(item = {}) {
    if (Array.isArray(item.data)) {
      const filtered = item.data.filter(point => point?.date);
      return {
        ...item,
        dates: filtered.map(point => point.date),
        weights: filtered.map(point => Number(point.weight))
      };
    }
    if (Array.isArray(item.dates) && Array.isArray(item.weights)) {
      const limit = Math.min(item.dates.length, item.weights.length);
      return {
        ...item,
        dates: item.dates.slice(0, limit),
        weights: item.weights.slice(0, limit).map(weight => Number(weight))
      };
    }
    return { ...item, dates: [], weights: [] };
  }

  function normalizeBacktestResult(result = {}) {
    return {
      ...result,
      nav: normalizeValueSeries(result.nav),
      benchmark_nav: normalizeBenchmarkNavs(result.benchmark_nav || {}),
      holdings_evolution: (result.holdings_evolution || []).map(normalizeHoldingSeries)
    };
  }


  /* ────────────────────────────────────────────────
     Comparison management
  ──────────────────────────────────────────────── */
  function loadComparisons() {
    try {
      const raw = localStorage.getItem(COMPARISON_STORAGE_KEY);
      if (!raw) return [];
      const data = JSON.parse(raw);
      return Array.isArray(data)
        ? data.map(normalizeComparison)
        : [];
    } catch (e) { return []; }
  }

  function saveComparisons() {
    try {
      localStorage.setItem(COMPARISON_STORAGE_KEY, JSON.stringify(state.savedComparisons));
    } catch (e) { /* ignore */ }
  }

  function getComparisonDisplayName(comp = {}) {
    return String(comp.name || comp.label || "未命名回测").trim() || "未命名回测";
  }

  function formatDateRangeShort(startDate, endDate) {
    if (!startDate || !endDate) return "";
    const [startYear, startMonth] = String(startDate).split("-");
    const [endYear, endMonth] = String(endDate).split("-");
    if (!startYear || !startMonth || !endYear || !endMonth) return "";
    if (startYear === endYear) return `${startYear}-${startMonth}~${endMonth}`;
    return `${startYear}-${startMonth}~${endYear}-${endMonth}`;
  }

  function getCurrentComponentCount() {
    const ids = new Set();
    state.plans.forEach(plan => {
      (plan.components || []).forEach(row => {
        const code = normalizeCode(row.code);
        if (code) ids.add(code);
      });
    });
    return ids.size;
  }

  function buildComparisonMeta(result, selectedMode, createdAt = new Date()) {
    const nav = normalizeValueSeries(result?.nav);
    const startDate = nav.dates[0] || document.getElementById("startDate").value || "";
    const endDate = nav.dates[nav.dates.length - 1] || document.getElementById("endDate").value || "";
    const modeLabel = getRebalanceModeLabel(selectedMode, selectedMode, true);
    const totalReturn = result?.metrics?.total_return;
    const maxDrawdown = result?.metrics?.max_drawdown;
    const sharpe = result?.metrics?.sharpe_ratio;
    const componentCount = getCurrentComponentCount();
    return {
      startDate,
      endDate,
      mode: selectedMode,
      modeLabel,
      componentCount,
      totalReturn: Number.isFinite(Number(totalReturn)) ? Number(totalReturn) : null,
      maxDrawdown: Number.isFinite(Number(maxDrawdown)) ? Number(maxDrawdown) : null,
      sharpe: Number.isFinite(Number(sharpe)) ? Number(sharpe) : null,
      rebalanceCount: Array.isArray(result?.applied_rebalance_dates) ? result.applied_rebalance_dates.length : 0,
      savedTime: createdAt.toISOString()
    };
  }

  function buildDefaultComparisonName(meta = {}) {
    const parts = [];
    if (meta.modeLabel) parts.push(meta.modeLabel);
    const rangeText = formatDateRangeShort(meta.startDate, meta.endDate);
    if (rangeText) parts.push(rangeText);
    if (meta.componentCount) parts.push(`${meta.componentCount}只`);
    if (meta.totalReturn != null && Number.isFinite(Number(meta.totalReturn))) {
      parts.push(formatMetric(meta.totalReturn, "pct"));
    }
    return parts.join("｜") || `回测结果｜${new Date().toLocaleString("zh-CN", { month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit" })}`;
  }

  function formatComparisonMetaText(meta = {}) {
    const lines = [];
    if (meta.startDate && meta.endDate) lines.push(`${meta.startDate} 至 ${meta.endDate}`);
    if (meta.modeLabel) lines.push(meta.modeLabel);
    if (meta.componentCount) lines.push(`${meta.componentCount} 只成分股`);
    if (meta.totalReturn != null && Number.isFinite(Number(meta.totalReturn))) lines.push(`收益 ${formatMetric(meta.totalReturn, "pct")}`);
    if (meta.maxDrawdown != null && Number.isFinite(Number(meta.maxDrawdown))) lines.push(`回撤 ${formatMetric(meta.maxDrawdown, "pct")}`);
    if (meta.sharpe != null && Number.isFinite(Number(meta.sharpe))) lines.push(`夏普 ${formatMetric(meta.sharpe, "num")}`);
    if (meta.savedTime) {
      lines.push(`保存于 ${new Date(meta.savedTime).toLocaleString("zh-CN", { month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit" })}`);
    }
    return lines.join(" · ");
  }

  function sanitizeComparisonName(value) {
    return String(value || "").trim().replace(/\s+/g, " ").slice(0, 60);
  }

  let comparisonNameResolver = null;

  function closeComparisonNameModal(value = null) {
    const modal = document.getElementById("comparisonNameModal");
    modal.classList.add("is-hidden");
    document.body.classList.remove("modal-open");
    const resolver = comparisonNameResolver;
    comparisonNameResolver = null;
    if (resolver) resolver(value);
  }

  function openComparisonNameModal({ title, defaultName, metaText, confirmText = "保存" }) {
    const modal = document.getElementById("comparisonNameModal");
    const titleEl = document.getElementById("comparisonNameTitle");
    const input = document.getElementById("comparisonNameInput");
    const metaEl = document.getElementById("comparisonNameMeta");
    const errorEl = document.getElementById("comparisonNameError");
    const confirmBtn = document.getElementById("comparisonNameConfirm");

    if (!modal || !input || !confirmBtn) {
      return Promise.resolve(sanitizeComparisonName(window.prompt(title || "命名回测结果", defaultName || "") || ""));
    }

    titleEl.textContent = title || "命名回测结果";
    input.value = defaultName || "";
    metaEl.textContent = metaText || "";
    errorEl.textContent = "";
    confirmBtn.textContent = confirmText;
    modal.classList.remove("is-hidden");
    document.body.classList.add("modal-open");

    setTimeout(() => {
      input.focus();
      input.select();
    }, 0);

    return new Promise(resolve => {
      comparisonNameResolver = resolve;
    });
  }

  function normalizeComparison(comp = {}) {
    const nav = normalizeValueSeries(comp.nav);
    const name = sanitizeComparisonName(comp.name || comp.label) || "未命名回测";
    const createdAt = comp.createdAt || comp.meta?.savedTime || new Date().toISOString();
    const mode = comp.meta?.mode || comp.mode || "";
    const modeLabel = comp.meta?.modeLabel || (mode ? getRebalanceModeLabel(mode, mode, true) : "");
    const metrics = comp.metrics || {};
    return {
      ...comp,
      id: Number(comp.id) || Date.now(),
      name,
      label: name,
      createdAt,
      nav,
      rebalanceDates: comp.rebalanceDates || [],
      metrics,
      meta: {
        ...(comp.meta || {}),
        startDate: comp.meta?.startDate || nav.dates[0] || "",
        endDate: comp.meta?.endDate || nav.dates[nav.dates.length - 1] || "",
        mode,
        modeLabel,
        totalReturn: comp.meta?.totalReturn ?? metrics.total_return ?? null,
        maxDrawdown: comp.meta?.maxDrawdown ?? metrics.max_drawdown ?? null,
        sharpe: comp.meta?.sharpe ?? metrics.sharpe_ratio ?? null,
        savedTime: comp.meta?.savedTime || createdAt
      }
    };
  }

  async function saveCurrentToComparison() {
    const nav = normalizeValueSeries(state.lastResult?.nav);
    if (!nav.dates.length) return;
    const selectedMode = document.getElementById("rebalanceMode").value;
    const createdAt = new Date();
    const meta = buildComparisonMeta(state.lastResult, selectedMode, createdAt);
    const defaultName = buildDefaultComparisonName(meta);
    const name = sanitizeComparisonName(await openComparisonNameModal({
      title: "保存对比快照",
      defaultName,
      metaText: formatComparisonMetaText(meta),
      confirmText: "保存"
    }));
    if (!name) return;
    const comp = {
      id: Date.now(),
      name,
      label: name,
      autoLabel: defaultName,
      createdAt: createdAt.toISOString(),
      nav,
      rebalanceDates: state.lastResult.applied_rebalance_dates || [],
      metrics: state.lastResult.metrics || {},
      meta
    };
    const selectedIds = new Set(getSelectedComparisonIds());
    selectedIds.add(comp.id);
    state.savedComparisons.push(comp);
    saveComparisons();
    renderComparisonBar(selectedIds);
    refreshChart();
    upsertStatus(`已保存对比："${name}"。`);
  }

  async function renameComparison(id) {
    const comp = state.savedComparisons.find(item => item.id === id);
    if (!comp) return;
    const currentName = getComparisonDisplayName(comp);
    const nextName = sanitizeComparisonName(await openComparisonNameModal({
      title: "重命名回测结果",
      defaultName: currentName,
      metaText: formatComparisonMetaText(comp.meta || {}),
      confirmText: "更新"
    }));
    if (!nextName || nextName === currentName) return;
    comp.name = nextName;
    comp.label = nextName;
    const selectedIds = new Set(getSelectedComparisonIds());
    saveComparisons();
    renderComparisonBar(selectedIds);
    refreshChart();
    upsertStatus(`已重命名对比："${nextName}"。`);
  }

  function removeComparison(id) {
    const selectedIds = new Set(getSelectedComparisonIds());
    selectedIds.delete(id);
    state.savedComparisons = state.savedComparisons.filter(c => c.id !== id);
    saveComparisons();
    renderComparisonBar(selectedIds);
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

  function renderComparisonBar(selectedIds = null) {
    const bar = document.getElementById("comparisonBar");
    const container = document.getElementById("comparisonCheckboxes");
    if (!state.savedComparisons.length) {
      bar.classList.add("is-hidden");
      container.innerHTML = "";
      return;
    }
    bar.classList.remove("is-hidden");
    const COMP_COLORS = ["#ef4444", "#8b5cf6", "#06b6d4", "#f59e0b", "#10b981", "#ec4899", "#6366f1", "#14b8a6"];
    container.innerHTML = state.savedComparisons.map((comp, idx) => {
      const color = COMP_COLORS[idx % COMP_COLORS.length];
      const name = getComparisonDisplayName(comp);
      const metaText = formatComparisonMetaText(comp.meta || {});
      const checked = selectedIds ? selectedIds.has(comp.id) : true;
      return `<span class="comparison-item" title="${escapeHtml(metaText)}">
        <label class="bm-label comparison-toggle">
          <input type="checkbox" class="comp-check" value="${comp.id}" ${checked ? "checked" : ""} />
          <span class="bm-swatch" style="background:${color};"></span>
          <span class="comparison-name">${escapeHtml(name)}</span>
        </label>
        <button type="button" class="light xs comparison-action" data-action="rename-comparison" data-id="${comp.id}" aria-label="重命名 ${escapeHtml(name)}">✎</button>
        <button type="button" class="danger xs comparison-action" data-action="remove-comparison" data-id="${comp.id}" aria-label="删除 ${escapeHtml(name)}">\u00d7</button>
      </span>`;
    }).join("");
  }

  /* ────────────────────────────────────────────────
     Periodic returns rendering
  ──────────────────────────────────────────────── */
  let holdingsChart = null;

  function formatReturnPct(value, digits = 1) {
    const pct = Number(value) * 100;
    if (!Number.isFinite(pct)) return "-";
    const sign = pct > 0 ? "+" : "";
    return `${sign}${pct.toFixed(digits)}%`;
  }

  function getReturnTone(value, maxAbs) {
    const num = Number(value);
    if (!Number.isFinite(num) || Math.abs(num) < 0.00005) return "flat";
    const intensity = Math.abs(num) / Math.max(maxAbs, 0.001);
    if (intensity >= 0.72) return num > 0 ? "positive-strong" : "negative-strong";
    if (intensity >= 0.34) return num > 0 ? "positive" : "negative";
    return num > 0 ? "positive-soft" : "negative-soft";
  }

  function renderPeriodicReturns(periodicData) {
    const container = document.getElementById("periodicReturns");
    if (!periodicData?.annual?.length && !periodicData?.monthly?.length) {
      container.classList.add("is-hidden");
      return;
    }
    container.classList.remove("is-hidden");
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
      const years = [...new Set(monthlyData.map(d => d.year))].sort((a, b) => b - a);
      const returnMap = new Map(monthlyData.map(d => [`${d.year}-${d.month}`, d.return]));
      const maxAbs = Math.max(0.001, ...monthlyData.map(d => Math.abs(d.return)));
      const dom = document.getElementById("monthlyHeatmap");
      const rows = years.map(year => {
        const cells = MONTH_LABELS.map((monthLabel, monthIndex) => {
          const month = monthIndex + 1;
          const value = returnMap.get(`${year}-${month}`);
          if (value == null) return `<td class="monthly-cell empty" title="${year}年${month}月无数据">-</td>`;
          const tone = getReturnTone(value, maxAbs);
          const fullValue = formatReturnPct(value, 2);
          return `<td class="monthly-cell ${tone}" title="${year}年${month}月 ${fullValue}">${formatReturnPct(value, 1)}</td>`;
        }).join("");
        const yearValues = monthlyData.filter(item => item.year === year).map(item => item.return);
        const yearTotal = yearValues.reduce((acc, value) => acc * (1 + value), 1) - 1;
        const totalClass = yearTotal > 0 ? "positive" : yearTotal < 0 ? "negative" : "";
        return `<tr><th scope="row">${year}</th>${cells}<td class="monthly-total ${totalClass}">${formatReturnPct(yearTotal, 1)}</td></tr>`;
      }).join("");

      dom.innerHTML = `
        <div class="panel returns-card monthly-returns-card">
          <div class="returns-card-head">
            <div>
              <div class="returns-card-title">月度收益</div>
              <div class="returns-card-subtitle">每格显示当月收益率</div>
            </div>
            <div class="monthly-legend" aria-label="收益颜色说明">
              <span><i class="legend-swatch negative"></i>亏损</span>
              <span><i class="legend-swatch flat"></i>持平</span>
              <span><i class="legend-swatch positive"></i>盈利</span>
            </div>
          </div>
          <div class="monthly-table-wrap">
            <table class="monthly-table">
              <thead>
                <tr>
                  <th scope="col">年份</th>
                  ${MONTH_LABELS.map(label => `<th scope="col">${label}</th>`).join("")}
                  <th scope="col">合计</th>
                </tr>
              </thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
        </div>`;
    } else {
      document.getElementById("monthlyHeatmap").innerHTML = "";
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
          autoNormalize: plan.autoNormalize || false,
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
      indicator.classList.remove("error");
    } catch (e) {
      indicator.textContent = "保存失败";
      indicator.classList.add("error");
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
      components: components?.map(c => makeRow(c.code, c.name, c.weight)) ?? [makeRow()],
      autoNormalize: false
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
        autoNormalize: plan.autoNormalize || false,
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
        const imported = data.plans.map(p => {
          const plan = createPlan(
            p.effectiveDate || "",
            (p.components || []).map(c => makeRow(c.code, c.name, c.weight))
          );
          plan.autoNormalize = p.autoNormalize || false;
          return plan;
        });
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
    if (!rows.length) { wrap.classList.add("is-hidden"); return; }
    wrap.classList.remove("is-hidden");
    tbody.innerHTML = rows.map(row => `
      <tr>
        <td>${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name) || "-"}</td>
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
            <input type="text" value="${escapeHtml(row.code)}"
              data-action="edit-cell" data-plan-id="${plan.id}" data-row-id="${row.id}" data-field="code"
              placeholder="000001" class="plan-edit-code" />
          </td>
          <td>
            <input type="text" value="${escapeHtml(row.name)}"
              data-action="edit-cell" data-plan-id="${plan.id}" data-row-id="${row.id}" data-field="name"
              placeholder="可选" class="plan-edit-name" />
          </td>
          <td>
            <input type="number" step="0.0001" value="${Number(row.weight) || 0}"
              data-action="edit-cell" data-plan-id="${plan.id}" data-row-id="${row.id}" data-field="weight"
              class="plan-edit-weight" />
          </td>
          <td>
            <button class="danger sm"
              data-action="remove-row" data-plan-id="${plan.id}" data-row-id="${row.id}">×</button>
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
            <div class="row" style="margin-bottom:var(--space-3);">
              <label>生效日</label>
              <input type="date" value="${plan.effectiveDate}"
                data-action="edit-plan-date" data-plan-id="${plan.id}" />
            </div>
            <div class="plan-actions">
              <button class="light" data-action="add-row" data-plan-id="${plan.id}">＋ 成分股</button>
              <button class="light" data-action="fill-remaining" data-plan-id="${plan.id}">分配剩余</button>
              <button class="secondary" data-action="equal-weight" data-plan-id="${plan.id}">均等权重</button>
              <label class="auto-norm-label">
                <input type="checkbox" data-action="toggle-auto-normalize" data-plan-id="${plan.id}"
                  ${plan.autoNormalize ? "checked" : ""} /> 自动归一化
              </label>
              <button class="danger" data-action="remove-plan" data-plan-id="${plan.id}"
                ${state.plans.length <= 1 ? "disabled" : ""}>删除计划</button>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th class="col-code">代码</th>
                    <th>名称</th>
                    <th class="col-weight">权重(%)</th>
                    <th class="col-action"></th>
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
      panel.classList.add("is-hidden");
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

    panel.classList.remove("is-hidden");
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
      box.classList.add("is-hidden");
      box.textContent = "";
      return;
    }
    box.classList.remove("is-hidden");
    box.textContent = messages.join("\n");
  }

  function setFallbackLogs(logs) {
    const section = document.getElementById("fallbackLogSection");
    const body = document.getElementById("fallbackLogBody");
    const count = document.getElementById("fallbackLogCount");
    const toggle = document.getElementById("fallbackLogToggle");
    if (!logs?.length) {
      section.classList.add("is-hidden");
      return;
    }
    section.classList.remove("is-hidden");
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

    const navSeries = normalizeValueSeries(nav);
    const normalizedBenchmarkNavs = normalizeBenchmarkNavs(benchmarkNavs);
    const dates = navSeries.dates;
    const values = navSeries.values;

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
      .map((comp, originalIndex) => ({ comp, originalIndex }))
      .filter(({ comp }) => selectedCompIds.includes(comp.id) && hasValueSeriesData(comp.nav))
      .map(({ comp, originalIndex }) => {
        const compSeries = normalizeValueSeries(comp.nav);
        const compDates = compSeries.dates;
        const compValues = compSeries.values;
        const compMap = {};
        compDates.forEach((d, i) => { compMap[d] = compValues[i]; });
        const alignedValues = dates.map(d => compMap[d] ?? null);
        return {
          id: `comparison-${comp.id}`,
          name: getComparisonDisplayName(comp),
          data: alignedValues,
          type: "line",
          xAxisIndex: 0, yAxisIndex: 0,
          smooth: true, showSymbol: false, connectNulls: true,
          lineStyle: { width: 1.8, color: COMP_COLORS[originalIndex % COMP_COLORS.length], type: "dashed" }
        };
      });

    // Build benchmark series (filtered by checkbox state)
    const selectedBm = getSelectedBenchmarks();
    const benchmarkSeries = selectedBm
      .filter(code => normalizedBenchmarkNavs[code]?.dates?.length)
      .map(code => {
        const bmData = normalizedBenchmarkNavs[code];
        const bmDates = bmData.dates;
        let bmValues = bmData.values;
        const bmMap = {};
        bmDates.forEach((d, i) => { bmMap[d] = bmValues[i]; });
        bmValues = dates.map(d => bmMap[d] ?? null);
        // Always normalise to start at 1.0 so benchmarks align with portfolio NAV
        const firstIdx = bmValues.findIndex(v => v !== null);
        if (firstIdx >= 0 && bmValues[firstIdx] !== 0) {
          const baseline = bmValues[firstIdx];
          bmValues = bmValues.map(v => v !== null ? v / baseline : null);
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
        borderColor: UI.textMuted,
        textStyle: { color: UI.surfaceSubtle, fontSize: 12 },
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
                html += `<div style="font-size:11px;color:${UI.borderLight};">${h.code}` +
                  `${h.name ? " " + h.name : ""} ` +
                  `<b style="color:${UI.surfaceSubtle}">${Number(h.weight).toFixed(2)}%</b></div>`;
              });
              if (holdings.length > 6) {
                html += `<div style="font-size:10px;color:${UI.subtle};">…另有 ${holdings.length - 6} 只</div>`;
              }
            }
            html += `<div style="border-top:1px solid ${UI.textMuted};margin:5px 0 3px;"></div>`;
          }

          // ── Series values ──
          for (const p of params) {
            if (p.seriesName === "调仓日" || p.seriesName === "调仓日光晕") continue;
            if (p.seriesName === "指数净值" && p.data != null) {
              const v = p.data;
              const ret = ((v - 1) * 100).toFixed(2);
              const col = v >= 1 ? UI.positive : UI.danger;
              html += `净值 <b>${v.toFixed(4)}</b>&nbsp; 累计 <span style="color:${col}">${ret}%</span><br/>`;
            } else if (p.seriesName === "回撤" && p.data != null) {
              html += `回撤 <span style="color:${UI.danger}">${(p.data * 100).toFixed(2)}%</span>`;
            } else if (p.data != null) {
              const v = Number(p.data);
              const ret = ((v - 1) * 100).toFixed(2);
              const col = v >= 1 ? UI.positive : UI.danger;
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
          axisLine: { lineStyle: { color: UI.borderLight } },
          axisTick: { show: false }
        },
        {
          type: "category", data: dates, gridIndex: 1,
          axisLabel: { color: UI.subtle, fontSize: 11 },
          axisLine: { lineStyle: { color: UI.borderLight } }
        }
      ],
      yAxis: [
        {
          type: "value", scale: true, gridIndex: 0,
          axisLabel: { color: UI.textMuted },
          splitLine: { lineStyle: { color: UI.borderLight } }
        },
        {
          type: "value", gridIndex: 1, min: "dataMin",
          axisLabel: {
            color: UI.subtleLight, fontSize: 10,
            formatter: v => v === 0 ? "0" : (v * 100).toFixed(0) + "%"
          },
          splitLine: { lineStyle: { color: UI.surfaceSubtle } }
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
          lineStyle: { width: 2.2, color: UI.primary },
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
            lineStyle: { color: UI.subtleLight, type: "dashed", width: 1, opacity: 0.45 },
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
              borderColor: UI.surface,
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
          lineStyle: { width: 1.5, color: UI.danger },
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

  // ── Holdings evolution trend chart ──
  function renderHoldingsChart(holdingsEvolution = [], rebalanceDates = []) {
    const container = document.getElementById("holdingsChart");
    const normalizedHoldings = (holdingsEvolution || []).map(normalizeHoldingSeries);
    if (!normalizedHoldings.length) {
      if (holdingsChart) {
        holdingsChart.dispose();
        holdingsChart = null;
      }
      container.innerHTML = "";
      container.classList.add("is-hidden");
      return;
    }

    container.classList.remove("is-hidden");

    const dates = normalizedHoldings[0]?.dates || [];
    const dateIndexMap = new Map(dates.map((d, i) => [d, i]));
    if (!dates.length) {
      container.classList.add("is-hidden");
      return;
    }

    const chartWidth = container.getBoundingClientRect().width || 820;
    const lineLimit = chartWidth >= 1100 ? 10 : chartWidth >= 820 ? 8 : 6;
    const showEndLabels = chartWidth >= 760;
    const chartHeight = chartWidth >= 820 ? 540 : 500;
    container.style.height = `${chartHeight}px`;

    try {
      if (!holdingsChart) {
        container.innerHTML = "";
        holdingsChart = echarts.init(container);
      } else {
        holdingsChart.resize();
      }
    } catch (e) {
      console.error("holdingsChart init failed:", e);
      container.classList.add("is-hidden");
      return;
    }

    const HOLDINGS_COLORS = [
      "#2563eb", "#0f766e", "#f59e0b", "#dc2626", "#7c3aed",
      "#0891b2", "#ea580c", "#65a30d", "#db2777", "#4f46e5",
      "#14b8a6", "#a16207", "#be123c", "#0284c7", "#9333ea"
    ];


    const formatWeight = value => {
      const pct = Number(value) * 100;
      if (!Number.isFinite(pct)) return "-";
      return pct < 0.01 && pct > 0 ? "小于0.01%" : `${pct.toFixed(2)}%`;
    };

    const formatPointChange = value => {
      const pct = Number(value) * 100;
      if (!Number.isFinite(pct) || Math.abs(pct) < 0.005) return "0.00pct";
      return `${pct > 0 ? "+" : ""}${pct.toFixed(2)}pct`;
    };

    const shortName = value => {
      const text = String(value || "");
      return text.length > 8 ? `${text.slice(0, 7)}...` : text;
    };

    const makeGradient = (color, topOpacity, bottomOpacity = 0.01) => ({
      type: "linear", x: 0, y: 0, x2: 0, y2: 1,
      colorStops: [
        { offset: 0, color: colorWithAlpha(color, topOpacity) },
        { offset: 1, color: colorWithAlpha(color, bottomOpacity) }
      ]
    });

    const colorWithAlpha = (hex, alpha) => {
      const value = String(hex || "").replace("#", "");
      if (value.length !== 6) return hex;
      const r = parseInt(value.slice(0, 2), 16);
      const g = parseInt(value.slice(2, 4), 16);
      const b = parseInt(value.slice(4, 6), 16);
      return `rgba(${r},${g},${b},${alpha})`;
    };

    const buildItem = (item, idx) => {
      const isOther = item.code === "其他";
      const isCash = item.code === "现金";
      const color = isOther ? UI.subtleLight : isCash ? UI.borderLight :
        HOLDINGS_COLORS[idx % HOLDINGS_COLORS.length];
      const displayName = item.name && item.name !== item.code
        ? `${item.name}(${item.code})`
        : (item.name || item.code);
      const values = (item.weights || []).map(w => Number(w));
      const latestValue = Number(values[values.length - 1]) || 0;
      const maxValue = Math.max(...values);
      const avgValue = values.reduce((sum, value) => sum + value, 0) / values.length;
      return {
        code: item.code,
        name: displayName,
        rawName: item.name || item.code,
        color,
        values,
        latest: latestValue,
        max: maxValue,
        avg: avgValue,
        score: maxValue * 0.55 + avgValue * 0.30 + latestValue * 0.15,
        isOther,
        isCash
      };
    };

    const items = normalizedHoldings.map(buildItem);
    const stockItems = items
      .filter(item => !item.isOther && !item.isCash)
      .sort((a, b) => b.score - a.score);
    const topLineItems = stockItems.slice(0, lineLimit);
    const restTrackedItems = stockItems.slice(lineLimit);
    const otherItem = items.find(item => item.isOther);
    const cashItem = items.find(item => item.isCash);

    const sumValues = sourceItems => {
      const result = new Float64Array(dates.length);
      for (const item of sourceItems) {
        const vals = item.values;
        for (let i = 0; i < result.length; i++) {
          result[i] += (vals[i] || 0);
        }
      }
      return Array.from(result);
    };
    const addValues = (...seriesValues) => {
      const result = new Float64Array(dates.length);
      for (const values of seriesValues) {
        for (let i = 0; i < result.length; i++) {
          result[i] += (values?.[i] || 0);
        }
      }
      return Array.from(result);
    };

    const mainValues = sumValues(topLineItems);
    const restTrackedValues = sumValues(restTrackedItems);
    const otherValues = otherItem?.values || dates.map(() => 0);
    const restValues = addValues(restTrackedValues, otherValues);
    const cashValues = cashItem?.values || dates.map(() => 0);
    const hasCash = cashValues.some(value => value > 0.0001);
    const latestIdx = dates.length - 1;
    const latestDate = dates[latestIdx];
    const mainLatest = mainValues[latestIdx] || 0;
    const restLatest = restValues[latestIdx] || 0;
    const cashLatest = cashValues[latestIdx] || 0;
    const topHolding = [...stockItems].sort((a, b) => b.latest - a.latest)[0];
    const maxLineWeight = Math.max(0, ...topLineItems.flatMap(item => item.values));
    const pickYAxisMax = value => {
      const padded = Math.max(value * 1.22, 0.025);
      return [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50, 1.0]
        .find(limit => limit >= padded) || 1.0;
    };
    const mainYAxisMax = pickYAxisMax(maxLineWeight);
    const endLabelRight = showEndLabels ? 104 : 26;

    const rebalanceDateSet = new Set(rebalanceDates);
    const dateSet = new Set(dates);
    const markLineData = rebalanceDates
      .filter(d => dateSet.has(d))
      .map(d => ({ xAxis: d }));
    const subtitleParts = [
      `${dates[0]} 至 ${latestDate}`,
      `展示 ${topLineItems.length} 条主持仓轨迹`,
      `主持仓合计 ${formatWeight(mainLatest)}`
    ];
    if (topHolding) subtitleParts.push(`最大 ${topHolding.name} ${formatWeight(topHolding.latest)}`);
    if (rebalanceDates?.length) subtitleParts.push(`${rebalanceDates.length} 次调仓`);

    const topSeries = topLineItems.map((item, idx) => ({
      name: item.name,
      data: item.values,
      type: "line",
      xAxisIndex: 0,
      yAxisIndex: 0,
      smooth: true,
      smoothMonotone: "x",
      showSymbol: false,
      symbol: "circle",
      symbolSize: idx < 4 ? 7 : 5,
      z: 10 + topLineItems.length - idx,
      lineStyle: {
        width: idx < 4 ? 2.6 : 1.9,
        color: item.color,
        opacity: idx < 6 ? 0.96 : 0.78
      },
      itemStyle: { color: item.color, borderColor: UI.surface, borderWidth: 2 },
      areaStyle: idx < 3 ? { color: makeGradient(item.color, 0.12, 0.00) } : undefined,
      emphasis: {
        focus: "series",
        scale: true,
        lineStyle: { width: 3.4, opacity: 1 }
      },
      endLabel: showEndLabels ? {
        show: true,
        formatter: () => `${shortName(item.rawName)} ${formatWeight(item.latest)}`,
        color: item.color,
        fontSize: 10,
        fontWeight: 700,
        distance: 7
      } : undefined,
      labelLayout: showEndLabels ? { moveOverlap: "shiftY" } : undefined
    }));

    const overviewSeries = [
      {
        name: "主持仓合计",
        data: mainValues,
        type: "line",
        stack: "overview",
        xAxisIndex: 1,
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1.2, color: UI.primary, opacity: 0.65 },
        areaStyle: { color: colorWithAlpha(UI.primary, 0.24) },
        itemStyle: { color: UI.primary }
      },
      {
        name: "其余持仓",
        data: restValues,
        type: "line",
        stack: "overview",
        xAxisIndex: 1,
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1, color: UI.subtleLight, opacity: 0.65 },
        areaStyle: { color: colorWithAlpha(UI.subtleLight, 0.22) },
        itemStyle: { color: UI.subtleLight }
      },
      ...(hasCash ? [{
        name: "现金",
        data: cashValues,
        type: "line",
          stack: "overview",
          xAxisIndex: 1,
          yAxisIndex: 1,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1, color: "#64748b", opacity: 0.65 },
          areaStyle: { color: "rgba(100,116,139,0.18)" },
          itemStyle: { color: "#64748b" }
      }] : [])
    ];

    const tooltipRows = date => {
      const dateIndex = dateIndexMap.get(date) ?? -1;
      if (dateIndex < 0) return { holdings: [], overview: [] };
      const holdings = topLineItems
        .map(item => ({
          name: item.name,
          color: item.color,
          value: item.values[dateIndex] || 0,
          delta: dateIndex > 0 ? (item.values[dateIndex] || 0) - (item.values[dateIndex - 1] || 0) : 0
        }))
        .filter(row => row.value > 0)
        .sort((a, b) => b.value - a.value);
      const overview = [
        { name: "主持仓合计", color: UI.primary, value: mainValues[dateIndex] || 0 },
        { name: "其余持仓", color: UI.subtleLight, value: restValues[dateIndex] || 0 },
        ...(hasCash ? [{ name: "现金", color: "#64748b", value: cashValues[dateIndex] || 0 }] : [])
      ].filter(row => row.value > 0.000001);
      return { holdings, overview };
    };

    holdingsChart.setOption({
      backgroundColor: "transparent",
      color: HOLDINGS_COLORS,
      animationDuration: 450,
      animationDurationUpdate: 300,
      title: {
        text: "主要持仓变化",
        subtext: subtitleParts.join(" · "),
        left: 20,
        top: 14,
        textStyle: { color: UI.textDark, fontSize: 14, fontWeight: 700 },
        subtextStyle: { color: UI.subtle, fontSize: 11, lineHeight: 16 }
      },
      legend: {
        type: "scroll",
        top: 54,
        left: 18,
        right: endLabelRight,
        height: 28,
        data: topLineItems.map(item => item.name),
        itemWidth: 16,
        itemHeight: 4,
        itemGap: 14,
        icon: "roundRect",
        pageIconColor: UI.primary,
        pageIconInactiveColor: UI.borderLight,
        pageTextStyle: { color: UI.subtle, fontSize: 10 },
        textStyle: { color: UI.textMuted, fontSize: 11 },
        formatter: name => name.length > 16 ? `${name.slice(0, 15)}...` : name
      },
      axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
      tooltip: {
        trigger: "axis",
        confine: true,
        axisPointer: {
          type: "line",
          lineStyle: { color: UI.subtle, width: 1, type: "dashed", opacity: 0.65 }
        },
        backgroundColor: UI.surface,
        borderColor: UI.border,
        borderWidth: 1,
        padding: [10, 12],
        textStyle: { color: UI.text, fontSize: 12 },
        extraCssText: "box-shadow:0 12px 28px rgba(15,23,42,.14);border-radius:8px;",
        formatter(params) {
          if (!params.length) return "";
          const date = params[0].axisValue;
          const { holdings, overview } = tooltipRows(date);
          let html = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:7px;font-weight:700;color:${UI.textDark};">` +
            `<span>${escapeHtml(date)}</span>` +
            (rebalanceDateSet.has(date)
              ? `<span style="font-size:10px;font-weight:700;color:#92400e;background:#fef3c7;border:1px solid #fde68a;border-radius:999px;padding:1px 7px;">调仓日</span>`
              : "") +
            `</div>`;
          html += `<div style="font-size:11px;color:${UI.subtle};margin-bottom:4px;">主持仓轨迹</div>`;
          for (const row of holdings) {
            const deltaColor = row.delta > 0 ? UI.positive : row.delta < 0 ? UI.danger : UI.subtle;
            html += `<div style="display:grid;grid-template-columns:10px minmax(126px,1fr) auto auto;gap:7px;align-items:center;margin:3px 0;">` +
              `<span style="width:8px;height:8px;border-radius:3px;background:${row.color};display:inline-block;"></span>` +
              `<span style="color:${UI.textMuted};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(row.name)}</span>` +
              `<b style="color:${UI.textDark};">${formatWeight(row.value)}</b>` +
              `<span style="color:${deltaColor};font-size:11px;">${formatPointChange(row.delta)}</span>` +
              `</div>`;
          }
          if (!holdings.length) {
            html += `<span style="color:${UI.subtle};">当日无有效主持仓份额</span>`;
          }
          html += `<div style="margin-top:8px;padding-top:7px;border-top:1px solid ${UI.borderLight};font-size:11px;color:${UI.subtle};">组合概览</div>`;
          for (const row of overview) {
            html += `<div style="display:grid;grid-template-columns:10px minmax(126px,1fr) auto;gap:7px;align-items:center;margin:3px 0;">` +
              `<span style="width:8px;height:8px;border-radius:3px;background:${row.color};display:inline-block;"></span>` +
              `<span style="color:${UI.textMuted};">${escapeHtml(row.name)}</span>` +
              `<b style="color:${UI.textDark};">${formatWeight(row.value)}</b>` +
              `</div>`;
          }
          return html;
        }
      },
      grid: [
        { top: 104, left: 54, right: endLabelRight, height: chartWidth >= 820 ? 278 : 238, containLabel: true },
        { top: chartWidth >= 820 ? 420 : 386, left: 54, right: endLabelRight, height: 50, containLabel: false }
      ],
      xAxis: [
        {
          type: "category", data: dates,
          gridIndex: 0,
          boundaryGap: false,
          axisLabel: { show: false },
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: false }
        },
        {
          type: "category", data: dates,
          gridIndex: 1,
          boundaryGap: false,
          axisLabel: {
            color: UI.subtle,
            fontSize: 10,
            hideOverlap: true,
            margin: 10,
            rotate: dates.length > 700 ? 30 : 0
          },
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: false }
        }
      ],
      yAxis: [
        {
          type: "value",
          gridIndex: 0,
          min: 0,
          max: mainYAxisMax,
          splitNumber: 4,
          axisLabel: {
            color: UI.subtle,
            fontSize: 10,
            formatter: v => (v * 100).toFixed(v < 0.1 ? 1 : 0) + "%"
          },
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: UI.trackBg, type: "dashed" } }
        },
        {
          type: "value",
          gridIndex: 1,
          min: 0,
          max: 1,
          splitNumber: 2,
          axisLabel: {
            color: UI.subtleLight,
            fontSize: 9,
            formatter: v => (v * 100).toFixed(0) + "%"
          },
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: UI.borderLight, type: "dashed", opacity: 0.75 } }
        }
      ],
      dataZoom: [
        {
          type: "inside",
          xAxisIndex: [0, 1],
          filterMode: "none",
          minSpan: 5,
          zoomOnMouseWheel: true,
          moveOnMouseMove: true
        },
        {
          type: "slider",
          xAxisIndex: [0, 1],
          filterMode: "none",
          height: 18,
          left: 52,
          right: endLabelRight,
          bottom: 22,
          showDetail: false,
          brushSelect: false,
          borderColor: "transparent",
          backgroundColor: UI.surfaceSubtle,
          fillerColor: "rgba(37,99,235,0.16)",
          handleSize: "80%",
          handleStyle: { color: UI.surface, borderColor: UI.subtleLight, borderWidth: 1 },
          moveHandleStyle: { color: UI.primary },
          dataBackground: {
            lineStyle: { color: UI.borderLight },
            areaStyle: { color: UI.borderLight }
          },
          selectedDataBackground: {
            lineStyle: { color: UI.primary },
            areaStyle: { color: "rgba(37,99,235,0.14)" }
          }
        }
      ],
      series: [
        ...topSeries,
        ...overviewSeries,
        ...(markLineData.length ? [{
          name: "调仓日",
          type: "line", data: dates.map(() => null), showSymbol: false, silent: true,
          xAxisIndex: 0,
          yAxisIndex: 0,
          markLine: {
            silent: true, symbol: "none",
            lineStyle: { color: UI.warning, type: "dashed", width: 1, opacity: 0.42 },
            label: { show: false },
            data: markLineData
          },
          tooltip: { show: false }
        }] : [])
      ]
    }, { notMerge: true });
  }

  // Re-render chart using stored result (called when benchmark/comparison checkboxes change)
  function refreshChart() {
    if (!state.lastResult) return;
    const r = normalizeBacktestResult(state.lastResult);
    state.lastResult = r;
    renderChart(
      r.nav,
      r.applied_rebalance_dates || [],
      r.benchmark_nav || {},
      r.rebalance_holdings || {}
    );
    renderHoldingsChart(r.holdings_evolution || [], r.applied_rebalance_dates || []);
  }

  function renderReport(result) {
    const nav = normalizeValueSeries(result.nav);
    const startDate = nav.dates[0] || "-";
    const endDate = nav.dates[nav.dates.length - 1] || "-";
    const tradingDays = nav.dates.length;
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
    const result = normalizeBacktestResult(state.lastResult || {});
    if (!result.nav.dates.length) return;
    state.lastResult = result;

    const bm = result.benchmark_nav || {};
    const navDates = result.nav.dates;
    const navValues = result.nav.values;
    const bmCodes = getSelectedBenchmarks().filter(code => bm[code]?.dates?.length);
    const bmMaps = {};
    bmCodes.forEach(code => {
      const bmData = bm[code];
      const rawMap = {};
      (bmData.dates || []).forEach((d, i) => { rawMap[d] = bmData.values?.[i]; });
      const vals = navDates.map(d => rawMap[d] ?? null);
      const firstIdx = vals.findIndex(v => v !== null);
      const scale = firstIdx >= 0 && vals[firstIdx] !== 0 ? vals[firstIdx] : 1;
      bmMaps[code] = {};
      navDates.forEach((date, idx) => {
        bmMaps[code][date] = vals[idx] !== null ? vals[idx] / scale : null;
      });
    });

    const bmHeaders = bmCodes.map(c => BENCHMARK_NAMES[c] || c);
    const headers = ["日期", "净值", "累计收益%", ...bmHeaders];

    const rows = navDates.map((date, i) => {
      const value = navValues[i];
      const ret = ((value - 1) * 100).toFixed(2);
      const bmVals = bmCodes.map(c => {
        const v = bmMaps[c]?.[date];
        return v != null ? v.toFixed(6) : "";
      });
      return [date, value.toFixed(6), ret, ...bmVals];
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
    const csv = allRows.map(row => row.map(csvSafe).join(",")).join("\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const startDate = navDates[0] || "export";
    const endDate = navDates[navDates.length - 1] || "";
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
      backgroundColor: UI.surface
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
    let normalizedOverweight = false;
    state.plans.forEach(plan => {
      const total = sumWeights(plan.components.filter(row => normalizeCode(row.code)));
      if (total > 100.0001 && total <= 100.05) {
        const result = capOverweightTo100(plan.components);
        normalizedOverweight = normalizedOverweight || result.changed;
      }
    });
    if (normalizedOverweight) {
      renderPlans();
      saveToStorage();
    }

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
  async function parseComponentFile() {
    const file = document.getElementById("fileInput").files[0];
    if (!file) { upsertStatus("请先选择 xls/xlsx/csv 文件。"); return; }

    const backend = getBackendUrl();
    const formData = new FormData();
    formData.append("file", file);
    upsertStatus("正在解析成分股文件…");
    try {
      const resp = await fetch(`${backend}/api/parse-components`, { method: "POST", body: formData });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "成分股文件解析失败");
      state.previewComponents = (data.components || []).map(item =>
        makeRow(item.code, item.name, item.weight)
      );
      document.getElementById("applyPreviewBtn").disabled = state.previewComponents.length === 0;
      document.getElementById("importSummary").textContent =
        `${data.filename}：${data.row_count} 行，解析 ${data.parsed_count} 行，跳过 ${data.skipped_rows} 行。` +
        `代码列：${data.code_column}；权重列：${data.weight_column || "未识别(等权)"}。`;
      renderPreviewTable();
      upsertStatus("成分股文件解析完成，可应用到任一调仓计划。");
    } catch (error) {
      upsertStatus(`成分股文件解析失败：${error.message}`);
    }
  }

  function applyPreviewToPlan() {
    syncPlansFromDom();
    const targetId = Number(document.getElementById("targetPlanSelect").value);
    const plan = findPlan(targetId);
    if (!plan) { upsertStatus("未找到目标调仓计划。"); return; }
    if (!state.previewComponents.length) { upsertStatus("没有可应用的解析结果。"); return; }
    plan.components = state.previewComponents.map(item => makeRow(item.code, item.name, item.weight));
    const overweight = capOverweightTo100(plan.components);
    state.expandedPlans.add(plan.id);
    renderPlans();
    saveToStorage();
    if (overweight.changed) {
      upsertStatus(`已将解析结果应用到调仓计划（${plan.effectiveDate}），原权重合计 ${overweight.total.toFixed(4)}%，已自动规整到 100%。`);
    } else {
      upsertStatus(`已将解析结果应用到调仓计划（${plan.effectiveDate}）。`);
    }
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
    const scheduleWrite = window.requestIdleCallback || (cb => setTimeout(cb, 0));
    scheduleWrite(() => {
      try {
        const result = normalizeBacktestResult(state.lastResult);
        // Thin NAV to at most 1000 points for storage to reduce size
        const nav = result.nav;
        const navLen = nav.dates.length;
        let thinnedNav = nav;
        if (navLen > 1000) {
          const stride = Math.ceil(navLen / 1000);
          thinnedNav = {
            dates: nav.dates.filter((_, i) => i % stride === 0),
            values: nav.values.filter((_, i) => i % stride === 0)
          };
        }
        const payload = {
          result: { ...result, nav: thinnedNav },
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
    }, { timeout: 2000 });
  }

  function loadLastResult() {
    try {
      const raw = localStorage.getItem(LAST_RESULT_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      const result = normalizeBacktestResult(data?.result || {});
      if (!result.nav.dates.length) return null;
      return { ...data, result };
    } catch (e) {
      return null;
    }
  }

  function applyBacktestResult(data, elapsedSeconds) {
    data = normalizeBacktestResult(data);
    state.lastResult = data;
    saveLastResult();
    renderMetrics(data.metrics || {}, data.comparison_metrics || []);
    renderChart(
      data.nav || [],
      data.applied_rebalance_dates || [],
      data.benchmark_nav || {},
      data.rebalance_holdings || {}
    );
    renderHoldingsChart(data.holdings_evolution || [], data.applied_rebalance_dates || []);
    renderReport(data);
    renderPeriodicReturns(data.periodic_returns);
    renderBacktestNotes(data);
    document.getElementById("exportCsvBtn").disabled = false;
    document.getElementById("exportImgBtn").disabled = false;
    document.getElementById("saveComparisonBtn").classList.remove("is-hidden");
    const allWarnings = data.warnings || [];
    const fallbackLogs = allWarnings.filter(w => w.includes("使用兜底数据源"));
    const otherWarnings = allWarnings.filter(w => !w.includes("使用兜底数据源"));
    setWarnings(otherWarnings);
    setFallbackLogs(fallbackLogs);

    upsertStatus(
      `回测完成：${data.nav.dates[0] || "-"} 至 ${data.nav.dates[data.nav.dates.length - 1] || "-"}，` +
      `共 ${data.nav.dates.length} 个交易日，累计耗时 ${formatElapsed(elapsedSeconds)}。`
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

      // On change (blur): auto-normalize if enabled
      if (field === "weight" && plan.autoNormalize) {
        const ok = autoNormalizeWeights(plan, rowId);
        if (ok) {
          upsertStatus("已自动归一化权重至 100%。");
        }
      }

      renderPlans();
      scheduleSave();
    }

    if (action === "toggle-auto-normalize" && event.type === "change") {
      plan.autoNormalize = actionEl.checked;
      if (plan.autoNormalize) {
        // Normalize on first enable
        normalizeWeights(plan.components);
        renderPlans();
        upsertStatus("已开启自动归一化，修改任意权重后将自动调整其它权重保持总和 100%。");
      }
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
        autoNormalize: p.autoNormalize || false,
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
      document.getElementById("customDatesRow").classList.remove("is-hidden");
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
        lastResult.result.nav,
        lastResult.result.applied_rebalance_dates || [],
        lastResult.result.benchmark_nav || {},
        lastResult.result.rebalance_holdings || {}
      );
      renderHoldingsChart(lastResult.result.holdings_evolution || [], lastResult.result.applied_rebalance_dates || []);
      renderReport(lastResult.result);
      renderPeriodicReturns(lastResult.result.periodic_returns);
      renderBacktestNotes(lastResult.result);
      document.getElementById("exportCsvBtn").disabled = false;
      document.getElementById("exportImgBtn").disabled = false;
      document.getElementById("saveComparisonBtn").classList.remove("is-hidden");
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
  document.getElementById("chooseFileBtn").addEventListener("click", () => {
    document.getElementById("fileInput").click();
  });
  document.getElementById("fileInput").addEventListener("change", handleComponentFileChange);
  document.getElementById("parseFileBtn").addEventListener("click", parseComponentFile);
  document.getElementById("applyPreviewBtn").addEventListener("click", applyPreviewToPlan);
  document.getElementById("runBacktestBtn").addEventListener("click", runBacktest);
  document.getElementById("addPlanBtn").addEventListener("click", () => addPlan(true));
  document.getElementById("exportPlansBtn").addEventListener("click", exportPlans);
  document.getElementById("importPlansBtn").addEventListener("click", () => {
    document.getElementById("importPlansFile").click();
  });
  document.getElementById("importPlansFile").addEventListener("change", importPlans);

  document.getElementById("rebalanceMode").addEventListener("change", event => {
    document.getElementById("customDatesRow").classList.toggle("is-hidden", event.target.value !== "custom");
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
  document.getElementById("comparisonNameClose").addEventListener("click", () => closeComparisonNameModal(null));
  document.getElementById("comparisonNameCancel").addEventListener("click", () => closeComparisonNameModal(null));
  document.getElementById("comparisonNameConfirm").addEventListener("click", () => {
    const input = document.getElementById("comparisonNameInput");
    const errorEl = document.getElementById("comparisonNameError");
    const name = sanitizeComparisonName(input.value);
    if (!name) {
      errorEl.textContent = "请输入名称。";
      input.focus();
      return;
    }
    closeComparisonNameModal(name);
  });
  document.getElementById("comparisonNameInput").addEventListener("input", () => {
    document.getElementById("comparisonNameError").textContent = "";
  });
  document.getElementById("comparisonNameInput").addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      document.getElementById("comparisonNameConfirm").click();
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeComparisonNameModal(null);
    }
  });
  document.getElementById("comparisonNameModal").addEventListener("click", event => {
    if (event.target.id === "comparisonNameModal") closeComparisonNameModal(null);
  });

  const debouncedChartRefresh = debounce(refreshChart, 150);
  document.getElementById("benchmarkBar").addEventListener("change", event => {
    if (event.target.classList.contains("bm-check")) debouncedChartRefresh();
  });
  document.getElementById("comparisonBar").addEventListener("click", event => {
    const btn = event.target.closest("[data-action]");
    if (!btn) return;
    event.preventDefault();
    event.stopPropagation();
    const id = Number(btn.dataset.id);
    if (btn.dataset.action === "rename-comparison") renameComparison(id);
    if (btn.dataset.action === "remove-comparison") removeComparison(id);
  });
  document.getElementById("comparisonBar").addEventListener("change", event => {
    if (event.target.classList.contains("comp-check")) debouncedChartRefresh();
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.chart) state.chart.resize();
      if (state.lastResult?.holdings_evolution?.length) {
        renderHoldingsChart(state.lastResult.holdings_evolution, state.lastResult.applied_rebalance_dates || []);
      } else if (holdingsChart) {
        holdingsChart.resize();
      }
      if (state.lastResult?.periodic_returns) {
        renderPeriodicReturns(state.lastResult.periodic_returns);
      }
    }, 150);
  });

  initDefaults();
