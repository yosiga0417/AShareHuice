from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.api import execute_backtest
from backend.models import BacktestRequest, ComponentInput, RebalancePlanInput


def make_request() -> BacktestRequest:
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
