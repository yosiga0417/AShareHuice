from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.api import execute_backtest
from backend.models import BacktestRequest, ComponentInput, RebalancePlanInput


def make_request(include_ohlc: bool = False) -> BacktestRequest:
    return BacktestRequest(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 5),
        rebalance_mode="monthly",
        plans=[
            RebalancePlanInput(
                effective_date=date(2024, 1, 1),
                components=[
                    ComponentInput(code="000001", name="A", weight=50.0),
                    ComponentInput(code="000002", name="B", weight=50.0),
                ],
            )
        ],
        benchmarks=["000300"],
        commission_rate=0.00025,
        stamp_duty_rate=0.0005,
        slippage_rate=0.001,
        include_ohlc=include_ohlc,
    )


def close_df() -> pd.DataFrame:
    # Deterministic prices across two months so the monthly rebalance has turnover.
    idx = pd.bdate_range("2024-01-01", "2024-03-05")
    n = len(idx)
    steps = np.arange(n)
    a = 10.0 + steps * 0.03 + np.sin(steps / 3.0)
    b = 20.0 - steps * 0.02 + np.cos(steps / 4.0)
    return pd.DataFrame({"000001": a, "000002": b}, index=idx)


def benchmark_payload(df: pd.DataFrame) -> dict[str, object]:
    return {
        "dates": [str(d.date()) for d in df.index],
        "values": [float(v) for v in range(1, len(df) + 1)],
    }


class ExecuteBacktestFlowTests(unittest.TestCase):
    def test_full_flow_with_costs_and_benchmark(self) -> None:
        df = close_df()
        with patch("backend.api.fetch_close_prices", return_value=(df, [])) as fetch_prices, \
             patch(
                 "backend.api.fetch_benchmark_navs",
                 return_value=({"000300": benchmark_payload(df)}, []),
             ) as fetch_bench:
            result = execute_backtest(make_request())

        fetch_prices.assert_called_once()
        fetch_bench.assert_called_once()
        self.assertIn("metrics", result)
        self.assertEqual(len(result["nav"]["dates"]), len(df))
        # Comparison modes exclude the selected monthly mode.
        modes = {item["mode"] for item in result["comparison_metrics"]}
        self.assertEqual(modes, {"quarterly", "semiannual", "none"})
        self.assertEqual(result["benchmark_nav"]["000300"]["dates"][0], str(df.index[0].date()))
        self.assertAlmostEqual(result["metrics"]["total_return"], result["nav"]["values"][-1] - 1.0, places=6)
        # Cost log is populated because the monthly rebalance has turnover.
        self.assertGreater(len(result["cost_log"]), 0)

    def test_include_ohlc_returns_candlestick_series(self) -> None:
        df = close_df()
        open_df = df.copy()
        high_df = df * 1.02
        low_df = df * 0.98
        with patch("backend.api.fetch_close_prices", return_value=(df, [])), \
             patch(
                 "backend.api.fetch_ohlc_matrices",
                 return_value=(open_df, high_df, low_df, []),
             ) as fetch_ohlc, \
             patch("backend.api.fetch_benchmark_navs", return_value=({}, [])):
            result = execute_backtest(make_request(include_ohlc=True))

        fetch_ohlc.assert_called_once()
        ohlc = result.get("nav_ohlc")
        self.assertIsNotNone(ohlc)
        assert ohlc is not None
        self.assertEqual(ohlc["dates"], result["nav"]["dates"])
        self.assertEqual(ohlc["end_date"], "2024-03-05", "应返回请求结束日供前端判断周期是否走完")
        np.testing.assert_allclose(ohlc["close"], result["nav"]["values"], rtol=1e-12)
        for idx in range(len(ohlc["dates"])):
            self.assertGreaterEqual(ohlc["high"][idx], max(ohlc["open"][idx], ohlc["close"][idx]))
            self.assertLessEqual(ohlc["low"][idx], min(ohlc["open"][idx], ohlc["close"][idx]))

    def test_ohlc_failure_degrades_instead_of_failing_backtest(self) -> None:
        df = close_df()
        with patch("backend.api.fetch_close_prices", return_value=(df, [])), \
             patch("backend.api.fetch_ohlc_matrices", side_effect=RuntimeError("ohlc boom")), \
             patch("backend.api.fetch_benchmark_navs", return_value=({}, [])):
            result = execute_backtest(make_request(include_ohlc=True))

        self.assertIsNone(result.get("nav_ohlc"))
        self.assertEqual(len(result["nav"]["dates"]), len(df), "K 线失败不应影响回测结果")
        self.assertTrue(
            any("K 线" in warning and "ohlc boom" in warning for warning in result["warnings"]),
            f"应把 OHLC 失败降级为 warning：{result['warnings']}",
        )

    def test_ohlc_fetch_skips_unavailable_symbols(self) -> None:
        # 000002 没有任何收盘行情，会在拉取 OHLC 之前被剔除，不应再请求它。
        df = close_df().drop(columns=["000002"])
        open_df = df.copy()
        with patch("backend.api.fetch_close_prices", return_value=(df, [])), \
             patch(
                 "backend.api.fetch_ohlc_matrices",
                 return_value=(open_df, df * 1.02, df * 0.98, []),
             ) as fetch_ohlc, \
             patch("backend.api.fetch_benchmark_navs", return_value=({}, [])):
            execute_backtest(make_request(include_ohlc=True))

        called_symbols = fetch_ohlc.call_args.args[0]
        self.assertEqual(called_symbols, ["000001"])

    def test_include_ohlc_off_returns_no_candle_data(self) -> None:
        df = close_df()
        with patch("backend.api.fetch_close_prices", return_value=(df, [])), \
             patch("backend.api.fetch_benchmark_navs", return_value=({}, [])):
            result = execute_backtest(make_request())
        self.assertIsNone(result.get("nav_ohlc"))

    def test_failed_symbol_warns_and_continues(self) -> None:
        df = close_df().drop(columns=["000002"])
        with patch("backend.api.fetch_close_prices", return_value=(df, ["000002 行情获取失败: boom"])), \
             patch("backend.api.fetch_benchmark_navs", return_value=({}, [])):
            result = execute_backtest(make_request())
        self.assertTrue(any("000002" in w for w in result["warnings"]))
        # Without allow_cash the survivor is renormalized to 100%.
        components = result["plan_summaries"][0]["components"]
        self.assertEqual([c["code"] for c in components], ["000001"])
        self.assertAlmostEqual(components[0]["weight"], 100.0, places=4)


if __name__ == "__main__":
    unittest.main()
