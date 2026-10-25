"""超额收益视图回归测试：勾选多个基准时应各生成一条独立曲线。

背景：index.html 里内联的图表视图脚本（净值 / 回撤 / 超额收益）原先用
computeExcess() 只取“第一条有数据的基准”，所以勾选沪深300 + 中证500 后
图上仍只有一条曲线。本测试从 index.html 中抽出该内联脚本，用 node 加桩
对象驱动 ChartView.setView('excess')，直接断言生成的 series 数量与图例。

需要 node（本机可能没有）；缺失时跳过而不是失败。
"""
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

# 三条基准：前两条有数据，第三条全为 null（应被忽略）
HARNESS_JS = textwrap.dedent(
    """
    const assert = require('assert');
    let captured = null;
    const fakeChart = {
      _lastFullOption: {
        xAxis: [{ data: ['d1', 'd2', 'd3'] }],
        series: [
          { id: 'nav-series', name: '指数净值', data: [1.0, 1.1, 1.2] },
          { id: 'benchmark-000300', name: '沪深300', data: [1.0, 1.05, 1.15], lineStyle: { color: '#fbbf24' } },
          { id: 'benchmark-000905', name: '中证500', data: [1.0, 1.02, 1.10], lineStyle: { color: '#34d399' } },
          { id: 'benchmark-000001', name: '上证指数', data: [null, null, null], lineStyle: { color: '#a78bfa' } },
          { id: 'drawdown-series', data: [0, -0.01, -0.02] }
        ]
      },
      _origSet(opt) { captured = opt; },
      setOption() {}
    };
    global.window = { __btState: () => ({ chart: fakeChart }) };
    global.document = { getElementById: () => null, querySelectorAll: () => [] };

    require(process.argv[2]);
    window.__btState = () => ({ chart: fakeChart });

    // 1) 两条有数据的基准 -> 两条曲线 + 图例
    window.ChartView.setView('excess');
    assert.ok(captured, 'setView(excess) 未渲染任何 option');
    let series = captured.series;
    assert.strictEqual(series.length, 2, '每个有数据的基准都应有独立曲线');
    assert.deepStrictEqual(series.map(s => s.id), ['excess-000300', 'excess-000905']);
    const round = v => (v === null ? null : Math.round(v * 1e6) / 1e6);
    assert.deepStrictEqual(series[0].data.map(round), [1, 1.047619, 1.043478]);
    assert.deepStrictEqual(series[1].data.map(round), [1, 1.078431, 1.090909]);
    assert.strictEqual(series[0].lineStyle.color, '#fbbf24');
    assert.strictEqual(series[1].lineStyle.color, '#34d399');
    assert.notDeepStrictEqual(series[0].data, series[1].data, '不同基准的曲线不能重合');
    assert.deepStrictEqual(captured.legend.data, ['沪深300超额', '中证500超额']);
    assert.strictEqual(captured.grid.top, 48, '有图例时上边距要为其留白');
    assert.ok(series[0].markLine && !series[1].markLine, '基准线只挂一条');

    // 2) 单基准 -> 保留面积填充，不显示图例
    fakeChart._lastFullOption.series = fakeChart._lastFullOption.series
      .filter(s => s.id !== 'benchmark-000905');
    captured = null;
    window.ChartView.setView('excess');
    assert.strictEqual(captured.series.length, 1);
    assert.ok(captured.series[0].areaStyle, '单基准应保留面积渐变');
    assert.ok(!captured.legend.data, '单基准不显示图例');
    assert.strictEqual(captured.grid.top, 26);

    // 3) 没有基准数据 -> 空曲线（由界面提示用户先勾选基准）
    fakeChart._lastFullOption.series = fakeChart._lastFullOption.series
      .filter(s => s.id.indexOf('benchmark-') !== 0);
    captured = null;
    window.ChartView.setView('excess');
    assert.strictEqual(captured.series.length, 0);

    console.log('excess view OK');
    """
).strip()


def _extract_view_script() -> str:
    """取出 index.html 中定义 window.ChartView 的那段内联脚本。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    matches = [b for b in blocks if "window.ChartView = {" in b]
    if len(matches) != 1:
        raise AssertionError(f"index.html 中视图脚本块数量异常：{len(matches)}")
    return matches[0]


@unittest.skipUnless(NODE, "未安装 node，跳过前端视图脚本测试")
class ExcessViewSeriesTest(unittest.TestCase):
    def test_one_curve_per_selected_benchmark(self) -> None:
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
            self.assertIn("excess view OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
