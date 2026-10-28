"""保存最近一次回测到 localStorage 时的抽稀回归测试。

背景：nav 超过 1000 点时会被抽稀后再存盘，但 nav_ohlc（K 线）原先全量保存，
刷新页面恢复结果后 K 线按抽稀后的日期对齐，日 K 只剩 1/stride 的交易日。
本测试用 DOM 桩加载真实 app.js，真实调用 saveLastResult 后检查落盘内容。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HARNESS_JS = textwrap.dedent(
    """
    const assert = require('assert');
    const fs = require('fs');
    const path = require('path');
    const ROOT = process.argv[2];

    function makeEl(id) {
      const el = {
        id, value: '', checked: false, textContent: '', innerHTML: '',
        style: {}, dataset: {}, children: [], firstChild: null,
        options: [], selectedOptions: [], files: [], rows: [],
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, removeEventListener() {},
        appendChild(c) { el.children.push(c); return c; },
        removeChild(c) { return c; }, insertBefore(c) { return c; },
        setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
        hasAttribute() { return false; },
        querySelector() { return null; }, querySelectorAll() { return []; },
        closest() { return null; }, focus() {}, blur() {}, click() {}, remove() {},
        scrollIntoView() {}, contains() { return false; }, getContext() { return null; },
        getBoundingClientRect() { return { width: 1000, height: 540, top: 0, left: 0, right: 1000, bottom: 540 }; },
      };
      return el;
    }
    const elCache = new Map();
    global.document = {
      readyState: 'complete',
      documentElement: makeEl('html'), body: makeEl('body'), head: makeEl('head'),
      getElementById(id) {
        if (!elCache.has(id)) elCache.set(id, makeEl(id));
        return elCache.get(id);
      },
      querySelector() { return null; }, querySelectorAll() { return []; },
      createElement(tag) { return makeEl(tag); }, createTextNode() { return {}; },
      addEventListener() {}, removeEventListener() {}, execCommand() { return true; },
    };
    const store = {};
    const localStorage = {
      getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
      setItem(k, v) { store[k] = String(v); },
      removeItem(k) { delete store[k]; },
    };
    global.window = {
      addEventListener() {}, removeEventListener() {},
      getComputedStyle() { return { getPropertyValue() { return ''; } }; },
      matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
      localStorage, location: { protocol: 'file:', href: 'file:///probe.html', origin: 'null' },
      /* 同步执行，确保 saveLastResult 在断言前完成写盘 */
      requestIdleCallback(cb) { return cb(); },
      cancelIdleCallback() {},
      setTimeout, clearTimeout, setInterval, clearInterval,
      prompt() { return null; }, confirm() { return true; }, alert() {},
      URL: { createObjectURL() { return 'blob:x'; }, revokeObjectURL() {} },
      navigator: { userAgent: 'node' },
    };
    global.navigator = global.window.navigator;
    global.location = global.window.location;
    global.localStorage = localStorage;
    global.getComputedStyle = global.window.getComputedStyle;
    global.xmlns = undefined;
    global.self = global.window;

    const fakeChart = {
      setOption() {}, dispatchAction() {}, getOption() { return {}; },
      getWidth() { return 1000; }, getHeight() { return 540; }, resize() {},
      dispose() {}, on() {}, off() {}, clear() {}, isDisposed() { return false; },
      getZr() { return { on() {}, off() {} }; }, getDom() { return null; },
      convertToPixel() { return 0; }, getModel() { return null; },
    };
    global.echarts = { init() { return fakeChart; }, getInstanceByDom() { return null; }, dispose() {} };

    (0, eval)(fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8'));
    assert.strictEqual(typeof saveLastResult, 'function', 'app.js 未暴露 saveLastResult');
    assert.strictEqual(typeof applyBacktestResult, 'function', 'app.js 未暴露 applyBacktestResult');

    /* 2500 个交易日：抽稀 stride = 3，保存后应为 834 个点 */
    const COUNT = 2500;
    const dates = [];
    const values = [];
    const open = [], high = [], low = [], close = [];
    let cursor = Date.UTC(2015, 0, 2);
    while (dates.length < COUNT) {
      const weekday = new Date(cursor).getUTCDay();
      if (weekday !== 0 && weekday !== 6) {
        dates.push(new Date(cursor).toISOString().slice(0, 10));
        const v = 1 + dates.length / COUNT;
        values.push(v);
        open.push(v - 0.01); high.push(v + 0.02); low.push(v - 0.02); close.push(v);
      }
      cursor += 86400000;
    }

    applyBacktestResult({
      nav: { dates: dates, values: values },
      nav_ohlc: {
        dates: dates.slice(), open: open, high: high, low: low, close: close,
        end_date: '2021-12-31'
      },
      applied_rebalance_dates: [],
    }, 0);

    const raw = localStorage.getItem('a_stock_backtest_last_result_v1');
    assert.ok(raw, '未写入 localStorage');
    const saved = JSON.parse(raw).result;
    assert.ok(saved.nav.dates.length <= 1000, '净值应被抽稀：' + saved.nav.dates.length);
    assert.ok(saved.nav_ohlc, 'nav_ohlc 不应丢失');
    assert.deepStrictEqual(
      saved.nav_ohlc.dates, saved.nav.dates,
      'nav_ohlc 必须与抽稀后的 nav 日期逐点一致'
    );
    for (const key of ['open', 'high', 'low', 'close']) {
      assert.strictEqual(saved.nav_ohlc[key].length, saved.nav.dates.length,
        'nav_ohlc.' + key + ' 长度应与抽稀后的日期一致');
    }
    assert.strictEqual(saved.nav_ohlc.end_date, '2021-12-31', 'end_date 应保留');
    assert.strictEqual(saved.nav_ohlc.close.length, 834, '2500 点按 stride=3 抽稀后应为 834 点');
    assert.strictEqual(saved.nav.dates[saved.nav.dates.length - 1], dates[dates.length - 1],
      '抽稀必须保留最后一个交易日');

    /* 1500 个交易日：stride = 2，最后一个点（索引 1499）是奇数，
       旧实现会把它丢掉，导致刷新后净值/报告截止日少一天。 */
    const COUNT2 = 1500;
    const dates2 = [];
    const values2 = [];
    const open2 = [], high2 = [], low2 = [], close2 = [];
    let cursor2 = Date.UTC(2010, 0, 4);
    while (dates2.length < COUNT2) {
      const weekday = new Date(cursor2).getUTCDay();
      if (weekday !== 0 && weekday !== 6) {
        dates2.push(new Date(cursor2).toISOString().slice(0, 10));
        const v = 1 + dates2.length / COUNT2;
        values2.push(v);
        open2.push(v - 0.01); high2.push(v + 0.02); low2.push(v - 0.02); close2.push(v);
      }
      cursor2 += 86400000;
    }
    applyBacktestResult({
      nav: { dates: dates2, values: values2 },
      nav_ohlc: {
        dates: dates2.slice(), open: open2, high: high2, low: low2, close: close2,
        end_date: '2015-12-31'
      },
      applied_rebalance_dates: [],
    }, 0);
    const saved2 = JSON.parse(localStorage.getItem('a_stock_backtest_last_result_v1')).result;
    assert.strictEqual(saved2.nav.dates.length, 751, '1500 点按 stride=2 抽稀并补上末点后应为 751 点');
    assert.strictEqual(saved2.nav.dates[saved2.nav.dates.length - 1], dates2[dates2.length - 1],
      '奇数长度的末点不能被抽稀丢掉');
    assert.deepStrictEqual(saved2.nav_ohlc.dates, saved2.nav.dates,
      'nav_ohlc 必须与抽稀后的 nav 日期逐点一致');

    console.log('last result storage OK');
    """
).strip()


@unittest.skipUnless(NODE, "未安装 node，跳过前端保存结果测试")
class LastResultStorageTest(unittest.TestCase):
    def test_nav_ohlc_is_thinned_with_nav(self) -> None:
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
        self.assertIn("last result storage OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
