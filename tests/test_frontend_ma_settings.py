"""K 线均线设置回归测试：自定义周期、显示开关、持久化与事件委托。"""
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

HARNESS_JS = textwrap.dedent(
    """
    const assert = require('assert');

    function makeEl(id) {
      const classes = new Set(['is-hidden']);
      const listeners = {};
      const attributes = {};
      return {
        id, innerHTML: '', value: '', checked: false, focused: false, listeners,
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
        setAttribute(k, v) { attributes[k] = v; },
        getAttribute(k) { return Object.prototype.hasOwnProperty.call(attributes, k) ? attributes[k] : null; },
        focus() { this.focused = true; },
        closest() { return null; }
      };
    }

    const ids = ['viewHint', 'navChart', 'maControls', 'maToggleBtn', 'maPanel', 'maChecklist',
                 'maCustomInput', 'maAddBtn', 'maAllBtn', 'maNoneBtn', 'maResetBtn', 'maError',
                 'maCount', 'maCloseBtn'];
    const els = {};
    ids.forEach(id => { els[id] = makeEl(id); });
    els.maControls.classList.remove('is-hidden');

    global.document = {
      getElementById: id => els[id] || null,
      querySelectorAll: () => [],
      addEventListener: () => {}
    };
    const store = {};
    global.localStorage = {
      getItem: k => Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null,
      setItem: (k, v) => { store[k] = String(v); }
    };

    let captured = null;
    const fakeChart = {
      _lastFullOption: {
        xAxis: [{ data: ['d1', 'd2', 'd3', 'd4', 'd5', 'd6', 'd7'] }],
        series: [{ id: 'nav-series', name: '指数净值', data: [1, 2, 3, 4, 5, 6, 7] }]
      },
      _origSet(opt) { captured = opt; },
      setOption() {}
    };
    const state = {
      chart: fakeChart,
      lastResult: {
        nav_ohlc: {
          dates: ['d1', 'd2', 'd3', 'd4', 'd5', 'd6', 'd7'],
          open: [1, 2, 3, 4, 5, 6, 7], high: [1, 2, 3, 4, 5, 6, 7],
          low: [1, 2, 3, 4, 5, 6, 7], close: [1, 2, 3, 4, 5, 6, 7]
        },
        applied_rebalance_dates: []
      }
    };
    global.UI = { up: '#f6465d', down: '#2ebd85', subtle: '#94a3b8', textMuted: '#b9c6d6',
                  border: '#334155', warning: '#fbbf24', surface: '#111829' };
    global.window = { __btState: () => state };
    require(process.argv[2]);
    window.__btState = () => state;

    function click(el, event) {
      (el.listeners.click || []).forEach(fn =>
        fn(Object.assign({ stopPropagation() {}, target: el }, event || {})));
    }
    function change(el, target) {
      (el.listeners.change || []).forEach(fn => fn({ target }));
    }

    window.ChartView.setView('candle');
    assert.deepStrictEqual(captured.series.slice(1).map(s => s.name),
      ['MA5', 'MA10', 'MA20', 'MA30', 'MA60']);
    assert.strictEqual(els.maControls.classList.contains('is-hidden'), false);
    assert.ok(els.maPanel.classList.contains('is-hidden'));
    // 按钮角标显示当前展示的均线条数
    assert.strictEqual(els.maCount.textContent, '5');
    assert.strictEqual(els.maToggleBtn.classList.contains('is-filtered'), false);

    // 打开面板并添加自定义 MA120
    click(els.maToggleBtn);
    assert.strictEqual(els.maPanel.classList.contains('is-hidden'), false);
    assert.strictEqual(els.maToggleBtn.getAttribute('aria-expanded'), 'true');
    assert.ok(els.maCustomInput.focused);
    els.maCustomInput.value = '120';
    click(els.maAddBtn);
    assert.ok(els.maChecklist.innerHTML.indexOf('MA120') >= 0);
    assert.ok(captured.series.some(s => s.name === 'MA120'));
    assert.ok(store['bt_ma_settings_v1'].indexOf('120') >= 0);

    // 取消勾选 MA10：只显示剩余均线，角标同步
    change(els.maChecklist, { getAttribute: k => k === 'data-ma-period' ? '10' : null, checked: false });
    assert.ok(!captured.series.some(s => s.name === 'MA10'));
    assert.strictEqual(els.maCount.textContent, '5');
    assert.strictEqual(els.maToggleBtn.classList.contains('is-filtered'), true);

    // 全不选：只剩 K 线，图例隐藏
    click(els.maNoneBtn);
    assert.strictEqual(captured.series.length, 1);
    assert.strictEqual(captured.legend.show, false);
    assert.strictEqual(els.maCount.textContent, '0');
    assert.strictEqual(els.maToggleBtn.classList.contains('is-filtered'), true);

    // 关闭按钮收起面板
    assert.strictEqual(els.maPanel.classList.contains('is-hidden'), false);
    click(els.maCloseBtn);
    assert.ok(els.maPanel.classList.contains('is-hidden'));
    assert.strictEqual(els.maToggleBtn.getAttribute('aria-expanded'), 'false');

    // 恢复默认：MA120 被删除，默认 5 条恢复
    click(els.maResetBtn);
    assert.deepStrictEqual(captured.series.slice(1).map(s => s.name),
      ['MA5', 'MA10', 'MA20', 'MA30', 'MA60']);
    assert.strictEqual(els.maChecklist.innerHTML.indexOf('MA120'), -1);
    assert.strictEqual(store['bt_ma_settings_v1'].indexOf('120'), -1);
    assert.strictEqual(els.maCount.textContent, '5');
    assert.strictEqual(els.maToggleBtn.classList.contains('is-filtered'), false);

    // ── 自定义均线颜色稳定：添加其他均线不会让已有均线换色 ──
    els.maCustomInput.value = '120';
    click(els.maAddBtn);
    const color120 = captured.series.find(s => s.name === 'MA120').itemStyle.color;
    els.maCustomInput.value = '150';
    click(els.maAddBtn);
    assert.strictEqual(
      captured.series.find(s => s.name === 'MA120').itemStyle.color,
      color120,
      '添加 MA150 后 MA120 的颜色不应改变'
    );
    assert.notStrictEqual(
      captured.series.find(s => s.name === 'MA150').itemStyle.color,
      color120,
      '不同自定义周期应使用不同颜色'
    );
    // 清理：删除 MA120 / MA150，避免影响后续断言
    ['120', '150'].forEach(period => {
      const btn = { getAttribute: k => k === 'data-ma-remove' ? period : null };
      const target = { closest: sel => sel === '[data-ma-remove]' ? btn : null };
      (els.maChecklist.listeners.click || []).forEach(fn => fn({ target }));
    });

    // 事件委托删除自定义均线
    els.maCustomInput.value = '45';
    click(els.maAddBtn);
    assert.ok(captured.series.some(s => s.name === 'MA45'));
    const removeBtn = { getAttribute: k => k === 'data-ma-remove' ? '45' : null };
    const fakeTarget = { closest: sel => sel === '[data-ma-remove]' ? removeBtn : null };
    (els.maChecklist.listeners.click || []).forEach(fn => fn({ target: fakeTarget }));
    assert.ok(!captured.series.some(s => s.name === 'MA45'));
    assert.strictEqual(els.maChecklist.innerHTML.indexOf('MA45'), -1);

    console.log('MA settings OK');
    """
).strip()


def _extract_view_script() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    matches = [b for b in blocks if "window.ChartView = {" in b]
    if len(matches) != 1:
        raise AssertionError(f"index.html 中视图脚本块数量异常：{len(matches)}")
    return matches[0]


@unittest.skipUnless(NODE, "未安装 node，跳过前端均线设置测试")
class MaSettingsTest(unittest.TestCase):
    def test_default_add_hide_reset_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            view_js = tmp_path / "view.js"
            harness_js = tmp_path / "harness.js"
            view_js.write_text(_extract_view_script(), encoding="utf-8")
            harness_js.write_text(HARNESS_JS, encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(harness_js), str(view_js)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"node 断言失败：\nstdout={proc.stdout}\nstderr={proc.stderr}",
            )
            self.assertIn("MA settings OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
