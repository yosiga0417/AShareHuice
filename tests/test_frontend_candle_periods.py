"""K 线周期（日/周/月/季/年）回归测试：从 index.html 抽出图表视图脚本，用 node 驱动。"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "index.html"
NODE = shutil.which("node")

# 覆盖：日K 原样透传、周/月/季/年聚合、ISO 周与跨年边界、调仓日去重、
# Tooltip 区间文案与「本期未完」、聚合后的均线、非法周期。
HARNESS_JS = textwrap.dedent(
    """
    const assert = require('assert');
    let captured = null;
    const hint = { textContent: '', classList: { add() {}, remove() {} } };
    const state = { chart: null, lastResult: null };

    function makeChart(dates) {
      return {
        _lastFullOption: {
          xAxis: [{ data: dates.slice() }],
          series: [{ id: 'nav-series', name: '指数净值', data: dates.map(() => 1) }]
        },
        _origSet(opt) { captured = opt; },
        setOption() {},
        dispatchAction() {}
      };
    }

    global.UI = { up: '#f6465d', down: '#2ebd85', subtle: '#94a3b8', textMuted: '#b9c6d6',
                  border: 'rgba(148,163,184,.14)', warning: '#fbbf24', surface: '#111829' };
    global.window = { __btState: () => state };
    global.document = { getElementById: id => id === 'viewHint' ? hint : null, querySelectorAll: () => [] };

    require(process.argv[2]);
    window.__btState = () => state;

    function render(dates, ohlc, rebalances, period) {
      state.chart = makeChart(dates);
      state.lastResult = { nav_ohlc: ohlc, applied_rebalance_dates: rebalances || [] };
      captured = null;
      window.ChartView.setView('candle');
      if (period) window.ChartView.setPeriod(period);
      assert.ok(captured, '未渲染任何 option');
      return captured;
    }

    function candle(opt) { return opt.series[0]; }

    const dates = ['2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05',
                   '2024-01-08', '2024-01-09', '2024-01-10'];
    const ohlc = {
      dates: dates.slice(),
      open:  [1.00, 1.10, 1.20, 1.30, 1.40, 1.50, 1.60],
      high:  [1.15, 1.25, 1.35, 1.45, 1.55, 1.65, 1.75],
      low:   [0.95, 1.05, 1.15, 1.25, 1.35, 1.45, 1.55],
      close: [1.10, 1.20, 1.30, 1.40, 1.50, 1.60, 1.70]
    };
    const rebalances = ['2024-01-03', '2024-01-04', '2024-01-08'];

    // ── 日K：保持逐日对齐，一根蜡烛一个交易日 ──
    let opt = render(dates, ohlc, rebalances);
    assert.strictEqual(window.ChartView.period, 'day', '默认应为日K');
    assert.deepStrictEqual(candle(opt).data, [
      [1.00, 1.10, 0.95, 1.15],
      [1.10, 1.20, 1.05, 1.25],
      [1.20, 1.30, 1.15, 1.35],
      [1.30, 1.40, 1.25, 1.45],
      [1.40, 1.50, 1.35, 1.55],
      [1.50, 1.60, 1.45, 1.65],
      [1.60, 1.70, 1.55, 1.75]
    ]);
    assert.deepStrictEqual(opt.xAxis.data, dates);
    assert.deepStrictEqual(candle(opt).markLine.data, [
      { xAxis: '2024-01-03' }, { xAxis: '2024-01-04' }, { xAxis: '2024-01-08' }
    ]);

    // ── 周K：两根蜡烛（2024-W01 / 2024-W02），同一根上的调仓日只标一次 ──
    opt = render(dates, ohlc, rebalances, 'week');
    assert.strictEqual(window.ChartView.period, 'week');
    assert.deepStrictEqual(opt.xAxis.data, ['2024-W01', '2024-W02']);
    assert.deepStrictEqual(candle(opt).data, [
      [1.00, 1.40, 0.95, 1.45],
      [1.40, 1.70, 1.35, 1.75]
    ]);
    assert.deepStrictEqual(candle(opt).markLine.data, [
      { xAxis: '2024-W01' }, { xAxis: '2024-W02' }
    ]);
    // 2 根蜡烛不足以形成 MA5
    assert.ok(opt.series.find(s => s.name === 'MA5').data.every(v => v === null));

    let tip = opt.tooltip.formatter([{ axisValue: '2024-W02' }]);
    assert.ok(tip.indexOf('2024-01-08 ~ 2024-01-10') >= 0, 'Tooltip 应显示实际区间：' + tip);
    assert.ok(tip.indexOf('本期未完') >= 0, '最后一根周K未走完应标注：' + tip);
    assert.ok(tip.indexOf('1.7000') >= 0, 'Tooltip 收盘应为区间最后收盘：' + tip);
    assert.ok(tip.indexOf('+21.43%') >= 0, '周涨跌应相对上一根周K收盘：' + tip);
    assert.ok(tip.indexOf('▣ 调仓日') >= 0, '周K应保留调仓日标记');

    tip = opt.tooltip.formatter([{ axisValue: '2024-W01' }]);
    assert.ok(tip.indexOf('2024-01-02 ~ 2024-01-05') >= 0, '首根周K区间错误：' + tip);
    assert.ok(tip.indexOf('本期未完') < 0, '已完成周期不应标注未完成：' + tip);

    // ── 月K / 季K / 年K ──
    opt = render(dates, ohlc, rebalances, 'month');
    assert.deepStrictEqual(opt.xAxis.data, ['2024-01']);
    assert.deepStrictEqual(candle(opt).data, [[1.00, 1.70, 0.95, 1.75]]);
    assert.deepStrictEqual(candle(opt).markLine.data, [{ xAxis: '2024-01' }]);
    assert.ok(opt.tooltip.formatter([{ axisValue: '2024-01' }]).indexOf('本期未完') >= 0);

    opt = render(dates, ohlc, rebalances, 'quarter');
    assert.deepStrictEqual(opt.xAxis.data, ['2024-Q1']);
    assert.deepStrictEqual(candle(opt).data, [[1.00, 1.70, 0.95, 1.75]]);

    opt = render(dates, ohlc, rebalances, 'year');
    assert.deepStrictEqual(opt.xAxis.data, ['2024']);
    assert.deepStrictEqual(candle(opt).data, [[1.00, 1.70, 0.95, 1.75]]);

    // ── 「本期未完」按回测请求结束日判定：周期以周末收尾不算未完 ──
    // 周K：结束后落在周五，2024-W02 已走完（周日没有行情是正常的）
    const weekEndDates = ['2024-01-08', '2024-01-09', '2024-01-10', '2024-01-11', '2024-01-12'];
    const weekEndOhlc = {
      dates: weekEndDates.slice(),
      open:  [1.40, 1.50, 1.60, 1.70, 1.80],
      high:  [1.55, 1.65, 1.75, 1.85, 1.95],
      low:   [1.35, 1.45, 1.55, 1.65, 1.75],
      close: [1.50, 1.60, 1.70, 1.80, 1.90],
      end_date: '2024-01-12'
    };
    opt = render(weekEndDates, weekEndOhlc, [], 'week');
    tip = opt.tooltip.formatter([{ axisValue: '2024-W02' }]);
    assert.ok(tip.indexOf('本期未完') < 0, '结束日为周五时最后一根周K应视为已完成：' + tip);

    // 请求结束日只到周三：最后一根周K确实未走完
    const midWeekOhlc = Object.assign({}, weekEndOhlc, { end_date: '2024-01-10' });
    opt = render(weekEndDates.slice(0, 3), {
      dates: weekEndDates.slice(0, 3),
      open: midWeekOhlc.open.slice(0, 3), high: midWeekOhlc.high.slice(0, 3),
      low: midWeekOhlc.low.slice(0, 3), close: midWeekOhlc.close.slice(0, 3),
      end_date: midWeekOhlc.end_date
    }, [], 'week');
    tip = opt.tooltip.formatter([{ axisValue: '2024-W02' }]);
    assert.ok(tip.indexOf('本期未完') >= 0, '结束日在周中时应标注本期未完：' + tip);

    // 月K：结束日 2024-06-30（周日）已覆盖整个 6 月
    const monthEndDates = ['2024-06-27', '2024-06-28'];
    opt = render(monthEndDates, {
      dates: monthEndDates.slice(), open: [2.0, 2.1], high: [2.2, 2.3],
      low: [1.9, 2.0], close: [2.1, 2.2], end_date: '2024-06-30'
    }, [], 'month');
    tip = opt.tooltip.formatter([{ axisValue: '2024-06' }]);
    assert.ok(tip.indexOf('本期未完') < 0, '月末落在周末时不应误报本期未完：' + tip);

    // 年K：2022-12-31 为周六，结束日即该日应视为完成
    opt = render(['2022-12-29', '2022-12-30'], {
      dates: ['2022-12-29', '2022-12-30'], open: [3.0, 3.1], high: [3.2, 3.3],
      low: [2.9, 3.0], close: [3.1, 3.2], end_date: '2022-12-31'
    }, [], 'year');
    assert.ok(opt.tooltip.formatter([{ axisValue: '2022' }]).indexOf('本期未完') < 0);

    // 旧结果（无 end_date）但数据正好停在周期最后一天：按最后交易日推断为已完成
    opt = render(['2024-01-31'], {
      dates: ['2024-01-31'], open: [1.0], high: [1.1], low: [0.9], close: [1.05]
    }, [], 'month');
    assert.ok(opt.tooltip.formatter([{ axisValue: '2024-01' }]).indexOf('本期未完') < 0);

    // ── ISO 周与跨年边界：2023-12-29(周五) 属于 2023-W52 ──
    const spanDates = ['2023-12-29', '2024-01-02'];
    const spanOhlc = {
      dates: spanDates.slice(),
      open: [2.00, 2.20], high: [2.10, 2.30], low: [1.90, 2.10], close: [2.05, 2.25]
    };
    opt = render(spanDates, spanOhlc, [], 'week');
    assert.deepStrictEqual(opt.xAxis.data, ['2023-W52', '2024-W01']);
    assert.deepStrictEqual(candle(opt).data, [[2.00, 2.05, 1.90, 2.10], [2.20, 2.25, 2.10, 2.30]]);

    opt = render(spanDates, spanOhlc, [], 'month');
    assert.deepStrictEqual(opt.xAxis.data, ['2023-12', '2024-01']);
    opt = render(spanDates, spanOhlc, [], 'quarter');
    assert.deepStrictEqual(opt.xAxis.data, ['2023-Q4', '2024-Q1']);
    opt = render(spanDates, spanOhlc, [], 'year');
    assert.deepStrictEqual(opt.xAxis.data, ['2023', '2024']);

    // ── 聚合后的均线按「周期根数」计算：30 个工作日 = 6 根周K ──
    const weekdays = [];
    let cursor = new Date(Date.UTC(2024, 0, 1));
    while (weekdays.length < 30) {
      const dow = cursor.getUTCDay();
      if (dow !== 0 && dow !== 6) {
        weekdays.push(cursor.toISOString().slice(0, 10));
      }
      cursor = new Date(cursor.getTime() + 86400000);
    }
    const seq = weekdays.map((_, i) => i + 1);
    const seqOhlc = {
      dates: weekdays.slice(),
      open: seq.slice(),
      high: seq.map(v => v + 1),
      low: seq.map(v => v - 1),
      close: seq.slice()
    };
    opt = render(weekdays, seqOhlc, [], 'week');
    assert.strictEqual(opt.xAxis.data.length, 6, '30 个工作日应聚合成 6 根周K');
    assert.deepStrictEqual(candle(opt).data[0], [1, 5, 0, 6], '首根周K的开高低收错误');
    const closedWeeks = [5, 10, 15, 20, 25, 30];
    assert.deepStrictEqual(candle(opt).data.map(v => v[1]), closedWeeks);
    const ma5 = opt.series.find(s => s.name === 'MA5').data;
    assert.deepStrictEqual(ma5.slice(0, 4), [null, null, null, null]);
    assert.strictEqual(ma5[4], 15, 'MA5 应按周K收盘均值计算');
    assert.strictEqual(ma5[5], 20);
    assert.deepStrictEqual(opt.xAxis.data[0], '2024-W01');

    // ── 非法周期被忽略，当前周期保持不变 ──
    assert.strictEqual(window.ChartView.setPeriod('decade'), false);
    assert.strictEqual(window.ChartView.period, 'week');

    // ── 旧结果没有 nav_ohlc：周期视图同样渲染占位并提示重新回测 ──
    captured = null;
    state.chart = makeChart(dates);
    state.lastResult = { nav_ohlc: null, applied_rebalance_dates: [] };
    window.ChartView.setPeriod('month');
    assert.ok(captured, '缺少 OHLC 时也应渲染空图表');
    assert.deepStrictEqual(captured.xAxis.data, ['2024-01']);
    assert.ok(candle(captured).data.every(v => v === '-'));
    assert.ok(hint.textContent.indexOf('开高低收') >= 0, '应提示需要开高低收数据');

    console.log('candle periods OK');
    """
).strip()

# 周期选择持久化：localStorage 里存的是月K，加载后应直接以月K渲染。
PERSIST_HARNESS_JS = textwrap.dedent(
    """
    const assert = require('assert');
    let captured = null;
    const saved = {};
    const store = { bt_candle_period_v1: 'month' };
    global.localStorage = {
      getItem: key => Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null,
      setItem: (key, value) => { store[key] = value; saved[key] = value; }
    };
    global.UI = { up: '#f6465d', down: '#2ebd85', subtle: '#94a3b8', textMuted: '#b9c6d6',
                  border: 'rgba(148,163,184,.14)', warning: '#fbbf24', surface: '#111829' };
    const chart = {
      _lastFullOption: {
        xAxis: [{ data: ['2024-01-02', '2024-01-31'] }],
        series: [{ id: 'nav-series', name: '指数净值', data: [1, 1.1] }]
      },
      _origSet(opt) { captured = opt; },
      setOption() {},
      dispatchAction() {}
    };
    const state = {
      chart: chart,
      lastResult: {
        nav_ohlc: {
          dates: ['2024-01-02', '2024-01-31'],
          open: [1.0, 1.05], high: [1.08, 1.12], low: [0.98, 1.04], close: [1.05, 1.1]
        },
        applied_rebalance_dates: []
      }
    };
    global.window = { __btState: () => state };
    global.document = { getElementById: () => null, querySelectorAll: () => [] };

    require(process.argv[2]);
    window.__btState = () => state;

    assert.strictEqual(window.ChartView.period, 'month', 'localStorage 中的周期应被恢复');
    window.ChartView.setView('candle');
    assert.deepStrictEqual(captured.xAxis.data, ['2024-01']);
    assert.deepStrictEqual(captured.series[0].data, [[1.0, 1.1, 0.98, 1.12]]);

    assert.strictEqual(window.ChartView.setPeriod('year'), true);
    assert.strictEqual(saved['bt_candle_period_v1'], 'year', '切换周期应写入 localStorage');
    assert.deepStrictEqual(captured.xAxis.data, ['2024']);

    console.log('candle period persistence OK');
    """
).strip()


def _extract_view_script() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    matches = [b for b in blocks if "window.ChartView = {" in b]
    if len(matches) != 1:
        raise AssertionError(f"index.html 中视图脚本块数量异常：{len(matches)}")
    return matches[0]


# 周期按钮的真实 DOM 交互：按钮态、重绘、视图切换时显隐。
BUTTON_HARNESS_JS = textwrap.dedent(
    """
    const assert = require('assert');
    let captured = null;

    function makeEl(id) {
      const classes = new Set(['is-hidden']);
      const listeners = {};
      const attributes = {};
      const el = {
        id, innerHTML: '', value: '', dataset: {}, listeners, children: [],
        classList: {
          add(c) { classes.add(c); },
          remove(c) { classes.delete(c); },
          contains(c) { return classes.has(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (classes.has(c)) { classes.delete(c); return false; }
              classes.add(c); return true;
            }
            if (force) { classes.add(c); return true; }
            classes.delete(c); return false;
          }
        },
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        setAttribute(k, v) { attributes[k] = String(v); },
        getAttribute(k) {
          if (Object.prototype.hasOwnProperty.call(attributes, k)) return attributes[k];
          // 真实 DOM 里 data-* 属性与 dataset 互为映射
          if (k.indexOf('data-') === 0) {
            const key = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
            return el.dataset[key] != null ? String(el.dataset[key]) : null;
          }
          return null;
        },
        focus() {},
        contains(node) { return el.children.indexOf(node) >= 0; },
        querySelectorAll(sel) {
          return el.children.filter(c => sel === '[data-period]' && c.dataset.period != null);
        },
        closest(sel) { return sel === '[data-period]' && el.dataset.period != null ? el : null; }
      };
      return el;
    }

    const periodBtns = ['day', 'week', 'month', 'quarter', 'year'].map(p => {
      const btn = makeEl('btn-' + p);
      btn.dataset.period = p;
      return btn;
    });
    const seg = makeEl('candlePeriodSeg');
    periodBtns.forEach(btn => seg.children.push(btn));

    const els = { viewHint: makeEl('viewHint'), navChart: makeEl('navChart'),
                  candlePeriodSeg: seg, maControls: makeEl('maControls') };
    global.document = {
      getElementById: id => els[id] || null,
      querySelectorAll: () => []
    };
    const store = {};
    global.localStorage = {
      getItem: key => Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null,
      setItem: (key, value) => { store[key] = String(value); }
    };
    global.UI = { up: '#f6465d', down: '#2ebd85', subtle: '#94a3b8', textMuted: '#b9c6d6',
                  border: 'rgba(148,163,184,.14)', warning: '#fbbf24', surface: '#111829' };

    const dates = ['2024-01-02', '2024-01-08'];
    const chart = {
      _lastFullOption: {
        xAxis: [{ data: dates.slice() }],
        series: [{ id: 'nav-series', name: '指数净值', data: [1, 1.1] }]
      },
      _origSet(opt) { captured = opt; },
      setOption() {},
      dispatchAction() {}
    };
    const state = {
      chart: chart,
      lastResult: {
        nav_ohlc: {
          dates: dates.slice(),
          open: [1.0, 1.05], high: [1.08, 1.12], low: [0.98, 1.04], close: [1.05, 1.1]
        },
        applied_rebalance_dates: []
      }
    };
    global.renderChart = () => {};
    global.window = { __btState: () => state };

    require(process.argv[2]);
    window.__btState = () => state;
    // 真实页面里 refreshChart 会重绘图表并被 applyView 接管，这里等价模拟
    global.refreshChart = () => window.ChartView.applyAll();

    // 初始为日K：只有「日K」处于选中态
    assert.strictEqual(window.ChartView.period, 'day');
    assert.strictEqual(periodBtns[0].classList.contains('active'), true);
    assert.ok(seg.classList.contains('is-hidden'), '非 K 线视图默认隐藏周期按钮');

    window.ChartView.setView('candle');
    assert.ok(!seg.classList.contains('is-hidden'), '进入 K 线视图应显示周期按钮');
    assert.deepStrictEqual(captured.xAxis.data, dates);

    const click = seg.listeners.click[0];
    click({ target: periodBtns[1] });
    assert.strictEqual(window.ChartView.period, 'week');
    assert.strictEqual(store['bt_candle_period_v1'], 'week', '点击周K应持久化周期');
    assert.strictEqual(periodBtns[1].classList.contains('active'), true);
    assert.strictEqual(periodBtns[1].getAttribute('aria-pressed'), 'true');
    assert.strictEqual(periodBtns[0].classList.contains('active'), false);
    assert.strictEqual(periodBtns[0].getAttribute('aria-pressed'), 'false');
    assert.deepStrictEqual(captured.xAxis.data, ['2024-W01', '2024-W02']);

    // 重复点击当前周期不做无谓重绘
    captured = null;
    click({ target: periodBtns[1] });
    assert.strictEqual(captured, null);

    // 回到净值视图：周期按钮隐藏，周期选择保留
    window.ChartView.setView('nav');
    assert.ok(seg.classList.contains('is-hidden'), '切回净值视图应隐藏周期按钮');
    assert.strictEqual(window.ChartView.period, 'week');

    // 没有任何回测数据时：K 线专属控件（周期/均线）不应显示
    chart._lastFullOption.xAxis[0].data = [];
    captured = null;
    window.ChartView.setView('candle');
    assert.ok(seg.classList.contains('is-hidden'), '无数据时应隐藏周期按钮');
    assert.ok(els.maControls.classList.contains('is-hidden'), '无数据时应隐藏均线按钮');
    assert.ok(els.viewHint.textContent.indexOf('先执行一次回测') >= 0, '无数据时应提示先回测');

    // 旧结果没有 nav_ohlc：K 线专属控件同样隐藏，并提示重新回测
    chart._lastFullOption.xAxis[0].data = dates.slice();
    state.lastResult.nav_ohlc = null;
    captured = null;
    window.ChartView.setView('candle');
    assert.ok(seg.classList.contains('is-hidden'), '旧结果无 OHLC 时应隐藏周期按钮');
    assert.ok(els.maControls.classList.contains('is-hidden'), '旧结果无 OHLC 时应隐藏均线按钮');
    assert.ok(els.viewHint.textContent.indexOf('开高低收') >= 0, '应提示重新回测获取 OHLC');

    console.log('candle period buttons OK');
    """
).strip()


def _run_harness(harness_js: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        view_js = tmp_path / "view.js"
        harness = tmp_path / "harness.js"
        view_js.write_text(_extract_view_script(), encoding="utf-8")
        harness.write_text(harness_js, encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(harness), str(view_js)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"node 断言失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
            )
        return proc.stdout


@unittest.skipUnless(NODE, "未安装 node，跳过前端视图脚本测试")
class CandlePeriodTest(unittest.TestCase):
    def test_period_aggregation(self) -> None:
        self.assertIn("candle periods OK", _run_harness(HARNESS_JS))

    def test_period_persistence(self) -> None:
        self.assertIn("candle period persistence OK", _run_harness(PERSIST_HARNESS_JS))

    def test_period_buttons_wire_up(self) -> None:
        self.assertIn("candle period buttons OK", _run_harness(BUTTON_HARNESS_JS))

    def test_toolbar_only_binds_view_buttons(self) -> None:
        """周期按钮共用 seg-btn 外观，但不能被视图切换的事件委托接管。"""
        html = INDEX_HTML.read_text(encoding="utf-8")
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        binders = [b for b in blocks if "querySelectorAll('.seg-btn[data-view]')" in b]
        self.assertTrue(binders, "视图按钮的事件委托必须限定 [data-view]")
        self.assertNotIn("querySelectorAll('.seg-btn')", "".join(blocks))

        match = re.search(r'<div class="seg candle-period.*?</div>', html, re.S)
        self.assertIsNotNone(match, "工具栏缺少 K 线周期按钮组")
        period_block = match.group(0)
        self.assertNotIn("data-view", period_block, "周期按钮不应带 data-view")
        for period in ("day", "week", "month", "quarter", "year"):
            self.assertIn(f'data-period="{period}"', period_block)
        for label in ("日K", "周K", "月K", "季K", "年K"):
            self.assertIn(f">{label}<", period_block)


if __name__ == "__main__":
    unittest.main()
