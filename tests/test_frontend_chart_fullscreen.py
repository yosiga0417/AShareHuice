"""图表全屏回归测试：按钮、面板全屏样式与持仓图网格重算。

背景：净值图与持仓演变图原先都被固定高度锁死（#navChart 的 clamp 高度、
持仓图渲染时写死的 540/500px 与两组 grid 坐标），既没有全屏入口，也没有在
全屏后把绘图区撑开。本测试断言：

1. 两个面板都有带 aria-pressed 的全屏按钮，且工具栏（图表切换/基准）在
   全屏面板内保留；
2. 浏览器不支持或拒绝原生全屏时，仍有页面内全屏样式兜底，并且 Esc 能退出；
3. 持仓图在全屏时按容器真实高度重算两组 grid，退出后恢复固定高度与选段。
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
INDEX_HTML = ROOT / "index.html"
STYLES_CSS = ROOT / "styles.css"
APP_JS = ROOT / "app.js"
NODE = shutil.which("node")

HARNESS_JS = textwrap.dedent(
    """
    const assert = require('assert');
    const fs = require('fs');
    const path = require('path');
    const ROOT = process.argv[2];
    const FULLSCREEN = process.argv[3] === 'fullscreen';

    function makeEl(id) {
      const el = {
        id: id,
        value: '', checked: false, textContent: '', innerHTML: '',
        style: {}, dataset: {},
        children: [], firstChild: null, options: [], selectedOptions: [], files: [], rows: [],
        classList: {
          _set: new Set(),
          add(c) { this._set.add(c); },
          remove(c) { this._set.delete(c); },
          toggle(c, force) {
            if (force === undefined) force = !this._set.has(c);
            force ? this._set.add(c) : this._set.delete(c);
            return force;
          },
          contains(c) {
            if (c === 'chart-fullscreen') return FULLSCREEN && id === 'panel-holdings';
            return this._set.has(c);
          }
        },
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
          return FULLSCREEN
            ? { width: 1400, height: 800, top: 0, left: 0, right: 1400, bottom: 800 }
            : { width: 1000, height: 540, top: 0, left: 0, right: 1000, bottom: 540 };
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

    let captured = null;
    const dispatched = [];
    const fakeChart = {
      setOption(opt) { captured = opt; },
      dispatchAction(action) { dispatched.push(action); },
      on() {}, off() {}, resize() {}, dispose() {}, clear() {},
      getOption() { return {}; }, getWidth() { return 1400; }, getHeight() { return 800; },
      isDisposed() { return false; }, getZr() { return { on() {}, off() {} }; },
      getDom() { return null; }, getModel() { return null; }, convertToPixel() { return 0; },
    };
    global.echarts = { init() { return fakeChart; }, getInstanceByDom() { return null; }, dispose() {} };

    const src = fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8');
    (0, eval)(src);

    const dates = [];
    let cursor = Date.UTC(2023, 0, 2);
    while (dates.length < 260) {
      const wd = new Date(cursor).getUTCDay();
      if (wd !== 0 && wd !== 6) dates.push(new Date(cursor).toISOString().slice(0, 10));
      cursor += 86400000;
    }
    const holdings = [0, 1, 2, 3].map(k => ({
      code: '60000' + k, name: '样例' + k, dates,
      weights: dates.map((_, i) => Math.max(0.01, 0.18 - k * 0.03 + 0.02 * Math.sin(i / 23 + k))),
    }));

    renderHoldingsChart(holdings, [dates[40]]);
    assert.ok(captured, '未捕获到持仓图 option');

    const grids = captured.grid.map(g => ({ top: g.top, height: g.height }));
    const bottom = grids[1].top + grids[1].height;
    const sliderBottom = captured.dataZoom[1].bottom;
    const report = {
      fullscreen: FULLSCREEN,
      chartHeight: FULLSCREEN ? 800 : 540,
      grids,
      sliderBottom,
      inlineHeight: document.getElementById('holdingsChart').style.height,
      // 最后一次 dataZoom 是渲染后回填的选段；未缩放时应为 0-100。
      lastZoom: dispatched.filter(a => a.type === 'dataZoom').pop() || null,
    };
    console.log('REPORT ' + JSON.stringify(report));

    if (FULLSCREEN) {
      // 全屏：主图明显变高，概览轴与滑条仍留在容器内，且不再写死 inline 高度。
      assert.ok(grids[0].height > 400, '全屏主图应撑满剩余高度：' + grids[0].height);
      assert.ok(grids[1].top > grids[0].height, '概览轴应在主图下方');
      assert.ok(bottom + sliderBottom <= 800, '全屏内容不应溢出容器：' + (bottom + sliderBottom));
      assert.strictEqual(report.inlineHeight, '', '全屏时不应写死持仓图 inline 高度');
    } else {
      assert.strictEqual(grids[0].height, 278, '普通模式主图高度应保持 278');
      assert.strictEqual(grids[1].top, 420, '普通模式概览轴位置应保持 420');
      assert.strictEqual(report.inlineHeight, '540px', '普通模式应恢复 540px 固定高度');
    }
    assert.deepStrictEqual(report.lastZoom, { type: 'dataZoom', start: 0, end: 100 });
    console.log(FULLSCREEN ? 'fullscreen layout OK' : 'normal layout OK');
    """
).strip()


class ChartFullscreenMarkupTest(unittest.TestCase):
    """静态断言：入口按钮、全屏样式与退出路径都已接线。"""

    def setUp(self) -> None:
        self.html = INDEX_HTML.read_text(encoding="utf-8")
        self.css = STYLES_CSS.read_text(encoding="utf-8")
        self.js = APP_JS.read_text(encoding="utf-8")

    def test_each_chart_panel_has_fullscreen_button(self) -> None:
        for panel in ("panel-nav", "panel-holdings"):
            self.assertIn(
                f'data-chart-fullscreen="{panel}"',
                self.html,
                f"{panel} 缺少全屏按钮",
            )
        self.assertEqual(self.html.count("data-chart-fullscreen="), 2)

    def test_fullscreen_keeps_chart_toolbar(self) -> None:
        nav_panel = self.html.split('id="panel-nav"', 1)[1].split("</section>", 1)[0]
        for control in ('data-view="nav"', 'data-view="candle"', 'id="benchmarkBar"', 'data-chart-fullscreen="panel-nav"'):
            self.assertIn(control, nav_panel, f"全屏面板应保留 {control}")

    def test_page_overlay_fallback_styles_exist(self) -> None:
        self.assertIn(".tab-panel.chart-fullscreen", self.css)
        self.assertIn("height: 100dvh", self.css)
        # 全屏时必须覆盖原有固定高度，否则图表仍停留在 clamp(420px, 58vh, 660px)。
        self.assertRegex(
            self.css,
            r"\.tab-panel\.chart-fullscreen\s*>\s*#navChart[\s\S]{0,200}?height:\s*0\s*!important",
        )

    def test_exit_paths_are_handled(self) -> None:
        self.assertIn('addEventListener("fullscreenchange"', self.js)
        self.assertIn("data-chart-fullscreen", self.js)
        # Esc 在没有原生全屏时由脚本退出页面内全屏。
        self.assertRegex(self.js, r'event\.key\s*===\s*"Escape"')
        self.assertIn("exitChartFullscreen", self.js)

    def test_fullscreen_button_pressed_state_is_tracked(self) -> None:
        self.assertIn('setAttribute("aria-pressed", "true")', self.js)
        self.assertIn('setAttribute("aria-pressed", "false")', self.js)
        # 展开/收起图标都必须内联在图标精灵里（离线可用）。
        self.assertIn('<symbol id="i-maximize"', self.html)
        self.assertIn('<symbol id="i-minimize"', self.html)
        self.assertIn('"#i-minimize"', self.js)


@unittest.skipUnless(NODE, "未安装 node，跳过前端全屏布局测试")
class ChartFullscreenLayoutTest(unittest.TestCase):
    def _run(self, mode: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS_JS, encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(harness), str(ROOT), mode],
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
        line = next((l for l in proc.stdout.splitlines() if l.startswith("REPORT ")), None)
        self.assertIsNotNone(line, f"未取到布局报告：\n{proc.stdout}")
        return json.loads(line[len("REPORT "):])

    def test_holdings_grid_fills_fullscreen_height(self) -> None:
        report = self._run("fullscreen")
        self.assertTrue(report["fullscreen"])
        self.assertGreater(report["grids"][0]["height"], 400)
        self.assertEqual(report["inlineHeight"], "")

    def test_holdings_restores_fixed_height_on_exit(self) -> None:
        report = self._run("normal")
        self.assertEqual(report["grids"][0]["height"], 278)
        self.assertEqual(report["inlineHeight"], "540px")


if __name__ == "__main__":
    unittest.main()
