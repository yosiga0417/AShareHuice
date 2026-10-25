"""持仓演变图排版回归测试：主图、组合概览轴、底部时间轴必须共用同一左右边界。

背景：renderHoldingsChart 里主图 grid 用了 containLabel: true，而概览 grid 和
dataZoom 滑条用的是固定 left，于是主图绘图区被各自的 y 轴标签宽度（约 31px）
向右推，曲线起点、x 轴刻度、概览面积图和底部时间滑条互相错开。

本测试用 DOM 桩加载真实 app.js，调用真实 renderHoldingsChart 拿到 setOption 的
option，再把该 option 交给真实的 echarts（SSR 渲染）测量两个 x 轴的像素坐标，
断言主图 / 概览轴 / 时间滑条的绘图区边界完全重合。纯配置断言测不出这类问题，
所以这里走「真实布局 → 读像素」的路径。

需要 node（本机可能没有）；缺失时跳过而不是失败。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ECHARTS = ROOT / "vendor" / "echarts.min.js"
NODE = shutil.which("node")

# 在 node 里用 DOM 桩跑 app.js，捕获 renderHoldingsChart 的 option，
# 再用 echarts SSR 量出真实像素坐标。
HARNESS_JS = textwrap.dedent(
    """
    const assert = require('assert');
    const fs = require('fs');
    const path = require('path');
    const ROOT = process.argv[2];
    const ECHARTS = path.join(ROOT, 'vendor', 'echarts.min.js');

    /* ── 极简 DOM 桩：app.js 加载时会绑定事件并调用 initDefaults() ── */
    function makeEl(id) {
      const el = {
        id: id,
        value: '', checked: false, textContent: '', innerHTML: '',
        style: {}, dataset: {},
        children: [], firstChild: null, options: [], selectedOptions: [], files: [], rows: [],
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, removeEventListener() {},
        appendChild(c) { el.children.push(c); return c; },
        removeChild(c) { return c; },
        insertBefore(c) { return c; },
        setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
        hasAttribute() { return false; },
        querySelector() { return null; }, querySelectorAll() { return []; },
        closest() { return null; },
        focus() {}, blur() {}, click() {}, remove() {}, scrollIntoView() {},
        getBoundingClientRect() {
          return { width: 1000, height: 540, top: 0, left: 0, right: 1000, bottom: 540 };
        },
        getContext() { return null; }, contains() { return false; },
      };
      return el;
    }
    const elCache = new Map();
    global.document = {
      readyState: 'complete',
      documentElement: makeEl('html'),
      body: makeEl('body'),
      head: makeEl('head'),
      getElementById(id) {
        if (!elCache.has(id)) elCache.set(id, makeEl(id));
        return elCache.get(id);
      },
      querySelector() { return null; },
      querySelectorAll() { return []; },
      createElement(tag) { return makeEl(tag); },
      createTextNode() { return {}; },
      addEventListener() {}, removeEventListener() {},
      execCommand() { return true; },
    };
    global.window = {
      addEventListener() {}, removeEventListener() {},
      getComputedStyle() { return { getPropertyValue() { return ''; } }; },
      matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
      localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
      location: { protocol: 'file:', href: 'file:///probe.html', origin: 'null' },
      requestIdleCallback(cb) { return setTimeout(cb, 0); },
      cancelIdleCallback() {},
      setTimeout, clearTimeout, setInterval, clearInterval,
      prompt() { return null; }, confirm() { return true; }, alert() {},
      URL: { createObjectURL() { return 'blob:x'; }, revokeObjectURL() {} },
      navigator: { userAgent: 'node' },
    };
    global.navigator = global.window.navigator;
    global.location = global.window.location;
    global.localStorage = global.window.localStorage;
    global.getComputedStyle = global.window.getComputedStyle;
    global.requestIdleCallback = global.window.requestIdleCallback;
    global.self = global.window;

    /* 捕获 renderHoldingsChart 传给 echarts 的 option */
    let captured = null;
    const fakeChart = {
      setOption(opt) { captured = opt; },
      dispatchAction() {}, getOption() { return {}; },
      getWidth() { return 1000; }, getHeight() { return 540; },
      resize() {}, dispose() {}, on() {}, off() {}, clear() {},
      isDisposed() { return false; }, getZr() { return { on() {}, off() {} }; },
      getDom() { return null; }, convertToPixel() { return 0; }, getModel() { return null; },
    };
    global.echarts = {
      init() { return fakeChart; },
      getInstanceByDom() { return null; },
      dispose() {},
    };

    /* 间接 eval：在全局作用域执行 app.js，函数声明会挂到 globalThis */
    const src = fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8');
    (0, eval)(src);
    assert.strictEqual(
      typeof globalThis.renderHoldingsChart, 'function',
      'app.js 未在全局作用域暴露 renderHoldingsChart',
    );

    /* ── 合成持仓数据（含「其他」「现金」两条汇总项） ── */
    const DAYS = 400;
    const dates = [];
    let cursor = Date.UTC(2023, 0, 2);
    while (dates.length < DAYS) {
      const wd = new Date(cursor).getUTCDay();
      if (wd !== 0 && wd !== 6) dates.push(new Date(cursor).toISOString().slice(0, 10));
      cursor += 86400000;
    }
    const items = [];
    for (let k = 0; k < 12; k++) {
      items.push({
        code: '60000' + k, name: '样例股票' + k, dates: dates,
        weights: dates.map((_, i) => Math.max(0.001, 0.16 - k * 0.011 + 0.04 * Math.sin(i / 37 + k))),
      });
    }
    items.push({ code: '其他', name: '其他', dates: dates, weights: dates.map((_, i) => 0.05 + 0.01 * Math.cos(i / 50)) });
    items.push({ code: '现金', name: '现金', dates: dates, weights: dates.map(() => 0.03) });

    globalThis.renderHoldingsChart(items, [dates[40], dates[180]]);
    assert.ok(captured, '未捕获到 renderHoldingsChart 的 setOption');

    /* ── 用真实 echarts（SSR）量像素 ── */
    const echartsReal = require(ECHARTS);
    const chart = echartsReal.init(null, null, { renderer: 'svg', ssr: true, width: 1000, height: 540 });
    chart.setOption(captured);

    const rect0 = chart.getModel().getComponent('grid', 0).coordinateSystem.getRect();
    const rect1 = chart.getModel().getComponent('grid', 1).coordinateSystem.getRect();
    const start0 = chart.convertToPixel({ xAxisIndex: 0 }, dates[0]);
    const start1 = chart.convertToPixel({ xAxisIndex: 1 }, dates[0]);
    const end0 = chart.convertToPixel({ xAxisIndex: 0 }, dates[dates.length - 1]);
    const end1 = chart.convertToPixel({ xAxisIndex: 1 }, dates[dates.length - 1]);
    /* dataZoom 滑条的 left/right 与 grid 同为一套像素坐标，直接比对配置值 */
    const sliderLeft = captured.dataZoom[1].left;
    const sliderRight = captured.dataZoom[1].right;
    const gridLeft = captured.grid[0].left;
    const gridRight = captured.grid[0].right;

    const svg = chart.renderToSVGString();
    chart.dispose();

    /* 轴标签不能被左边界裁掉：粗略按 0.62em/字符估算文字宽度，
       要求最左侧标签起始像素仍为正（留 4px 余量）。 */
    let leftmostLabel = Infinity;
    const labelRe = /<text[^>]*text-anchor="end"[^>]*font-size:(\\d+(?:\\.\\d+)?)px[^>]*transform="translate\\(([-\\d.]+) [-\\d.]+\\)"[^>]*>([^<]*)<\\/text>/g;
    let m;
    while ((m = labelRe.exec(svg)) !== null) {
      const fontSize = parseFloat(m[1]);
      const anchorX = parseFloat(m[2]);
      const text = m[3];
      leftmostLabel = Math.min(leftmostLabel, anchorX - text.length * 0.62 * fontSize);
    }

    const report = {
      grid0: { left: rect0.x, width: rect0.width, containLabel: captured.grid[0].containLabel },
      grid1: { left: rect1.x, width: rect1.width, containLabel: captured.grid[1].containLabel },
      start0: start0, start1: start1, end0: end0, end1: end1,
      slider: { left: sliderLeft, right: sliderRight },
      grid: { left: gridLeft, right: gridRight },
      leftmostLabel: leftmostLabel,
    };
    console.log('REPORT ' + JSON.stringify(report));

    assert.strictEqual(start0, start1, '主图与概览轴的绘图区左边界必须重合');
    assert.strictEqual(end0, end1, '主图与概览轴的绘图区右边界必须重合');
    assert.strictEqual(sliderLeft, gridLeft, '底部时间轴滑条左端必须与绘图区左边界重合');
    assert.strictEqual(sliderRight, gridRight, '底部时间轴滑条右端必须与绘图区右边界重合');
    assert.ok(leftmostLabel > 4, 'y 轴标签超出左边界被裁切：' + leftmostLabel);
    console.log('holdings layout OK');
    process.exit(0);
    """
).strip()


@unittest.skipUnless(NODE, "未安装 node，跳过前端持仓图排版测试")
class HoldingsChartLayoutTest(unittest.TestCase):
    def test_plot_areas_share_same_left_and_right(self) -> None:
        if not ECHARTS.exists():
            self.skipTest(f"缺少 {ECHARTS}，跳过前端持仓图排版测试")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS_JS, encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(harness), str(ROOT)],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(ROOT),
            )
        self.assertEqual(
            proc.returncode,
            0,
            f"node 断言失败：\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        self.assertIn("holdings layout OK", proc.stdout)

        report_line = next(
            (line for line in proc.stdout.splitlines() if line.startswith("REPORT ")), None
        )
        self.assertIsNotNone(report_line, f"未取到布局报告：\n{proc.stdout}")
        report = json.loads(report_line[len("REPORT "):])
        self.assertFalse(report["grid0"]["containLabel"], "主图 grid 不应使用 containLabel")
        self.assertFalse(report["grid1"]["containLabel"], "概览 grid 不应使用 containLabel")
        self.assertEqual(report["grid0"]["left"], report["grid1"]["left"])
        self.assertEqual(report["grid0"]["width"], report["grid1"]["width"])
        self.assertEqual(report["start0"], report["start1"])
        self.assertEqual(report["end0"], report["end1"])
        self.assertEqual(report["slider"], report["grid"])
        self.assertGreater(report["leftmostLabel"], 4, "y 轴标签被左边界裁切")


if __name__ == "__main__":
    unittest.main()
