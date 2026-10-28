"""K 线视图回归测试：从 index.html 抽出图表视图脚本，用 node 驱动。"""
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
    let captured = null;
    let cleared = null;
    const hint = { textContent: '', classList: { add() {}, remove() {} } };
    const fakeChart = {
      _lastFullOption: {
        xAxis: [{ data: ['2024-01-02', '2024-01-03', '2024-01-04'] }],
        series: [{ id: 'nav-series', name: '指数净值', data: [1.0, 1.025, 1.0375] }]
      },
      _origSet(opt, opts) {
        // 切回净值时视图脚本会用 replaceMerge 清理 K 线专属组件
        if (opts && opts.replaceMerge) { cleared = { opt, opts }; return; }
        captured = opt;
      },
      setOption() {}
    };
    const state = {
      chart: fakeChart,
      lastResult: {
        nav_ohlc: {
          dates: ['2024-01-02', '2024-01-03', '2024-01-04'],
          open: [1.0, 1.0125, 1.025],
          high: [1.05, 1.07, 1.06],
          low: [0.98, 0.99, 1.01],
          close: [1.025, 1.0375, 1.0375]
        },
        applied_rebalance_dates: ['2024-01-03']
      }
    };
    global.UI = { up: '#f6465d', down: '#2ebd85', subtle: '#94a3b8', textMuted: '#b9c6d6',
                  border: 'rgba(148,163,184,.14)', warning: '#fbbf24', surface: '#111829' };
    global.window = { __btState: () => state };
    global.document = { getElementById: id => id === 'viewHint' ? hint : null, querySelectorAll: () => [] };
    // 页面里由 app.js 提供；这里只验证视图切换，重绘本身不参与断言。
    global.refreshChart = () => {};
    global.renderChart = () => {};

    require(process.argv[2]);
    window.__btState = () => state;

    window.ChartView.setView('candle');
    assert.ok(captured, 'setView(candle) 未渲染任何 option');
    const candle = captured.series[0];
    assert.strictEqual(candle.type, 'candlestick');
    assert.deepStrictEqual(candle.data, [
      [1.0, 1.025, 0.98, 1.05],
      [1.0125, 1.0375, 0.99, 1.07],
      [1.025, 1.0375, 1.01, 1.06]
    ]);
    assert.strictEqual(candle.itemStyle.color, '#f6465d', '上涨应为红色');
    assert.strictEqual(candle.itemStyle.color0, '#2ebd85', '下跌应为绿色');
    assert.deepStrictEqual(candle.markLine.data, [{ xAxis: '2024-01-03' }]);
    assert.strictEqual(hint.textContent, '');

    // 默认均线：MA5/10/20/30/60 全部显示；3 个点不足以形成任何均线。
    const maSeries = captured.series.slice(1);
    assert.deepStrictEqual(maSeries.map(s => s.name), ['MA5', 'MA10', 'MA20', 'MA30', 'MA60']);
    assert.ok(maSeries.every(s => s.type === 'line'));
    assert.ok(maSeries.every(s => s.data.every(v => v === null)));
    assert.deepStrictEqual(captured.legend.data, ['MA5', 'MA10', 'MA20', 'MA30', 'MA60']);
    assert.strictEqual(captured.legend.selectedMode, false);
    assert.strictEqual(captured.grid.top, 38);

    // 7 个点：MA5 从第 5 个点开始有值。
    const dates7 = ['2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05',
                    '2024-01-08', '2024-01-09', '2024-01-10'];
    const closes7 = [1, 2, 3, 4, 5, 6, 7];
    fakeChart._lastFullOption.xAxis[0].data = dates7;
    state.lastResult.nav_ohlc = {
      dates: dates7,
      open: closes7.slice(),
      high: closes7.slice(),
      low: closes7.slice(),
      close: closes7.slice()
    };
    state.lastResult.applied_rebalance_dates = [];
    captured = null;
    window.ChartView.setView('candle');
    const ma5 = captured.series.find(s => s.name === 'MA5');
    assert.deepStrictEqual(ma5.data, [null, null, null, null, 3, 4, 5]);
    const ma10 = captured.series.find(s => s.name === 'MA10');
    assert.ok(ma10.data.every(v => v === null));
    assert.strictEqual(hint.textContent, '');

    // 旧结果没有 nav_ohlc：仍渲染空 K 线，并提示重新回测。
    captured = null;
    state.lastResult.nav_ohlc = null;
    window.ChartView.setView('candle');
    assert.ok(captured, '缺少 OHLC 时也应渲染空图表');
    assert.strictEqual(captured.series[0].type, 'candlestick');
    assert.ok(captured.series[0].data.every(v => v === '-'));
    assert.ok(captured.graphic.length === 1, '空态应带「暂无 K 线数据」提示');
    assert.deepStrictEqual(captured.legend, { show: false }, '空态不应显示均线图例');
    assert.strictEqual(captured.series.length, 1, '空态不应叠加均线曲线');
    assert.ok(hint.textContent.indexOf('开高低收') >= 0, '应提示需要开高低收数据');

    // 切回净值：K 线视图的空态 graphic 与均线图例必须被显式清除，
    // 否则它们会一直压在净值曲线上（app.js 的净值 setOption 只 replaceMerge series）。
    cleared = null;
    window.ChartView.setView('nav');
    assert.ok(cleared, '切回净值时应清理 K 线专属组件');
    assert.deepStrictEqual(cleared.opt.graphic, []);
    assert.deepStrictEqual(cleared.opt.legend, { show: false });
    assert.deepStrictEqual(cleared.opts.replaceMerge, ['graphic', 'legend']);

    console.log('candle view OK');
    """
).strip()


def _extract_view_script() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    matches = [b for b in blocks if "window.ChartView = {" in b]
    if len(matches) != 1:
        raise AssertionError(f"index.html 中视图脚本块数量异常：{len(matches)}")
    return matches[0]


@unittest.skipUnless(NODE, "未安装 node，跳过前端视图脚本测试")
class CandleViewTest(unittest.TestCase):
    def test_candlestick_series_and_empty_state(self) -> None:
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
            self.assertIn("candle view OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
