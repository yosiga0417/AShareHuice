from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from backend.engine import (
    build_periodic_rebalance_dates,
    compute_all_mode_navs,
    compute_metrics,
    drop_unavailable_symbols,
    normalize_weight_map,
    normalize_weights_exact,
    prepare_nav_data,
    prepare_ohlc_data,
)
from backend.models import CleanPlan


def make_plan(effective_date: str, weights: dict[str, float], cash_weight: float = 0.0) -> CleanPlan:
    components = [
        {"code": code, "name": code, "weight": round(w * 100, 4)}
        for code, w in weights.items()
    ]
    return CleanPlan(
        effective_date=pd.Timestamp(effective_date),
        components=components,
        weight_map=weights,
        cash_weight=cash_weight,
    )


class NormalizeWeightsTests(unittest.TestCase):
    def test_sum_to_100_exact(self) -> None:
        result = normalize_weights_exact([33.3, 33.3, 33.4], decimals=4)
        self.assertAlmostEqual(sum(result), 100.0, places=9)
        self.assertEqual(result, [33.3, 33.3, 33.4])

    def test_remainder_goes_to_largest_fraction(self) -> None:
        result = normalize_weights_exact([1.0, 1.0, 1.0], decimals=4)
        self.assertEqual(round(sum(result), 9), 100.0)
        self.assertEqual(sorted(result), [33.3333, 33.3333, 33.3334])

    def test_negative_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_weights_exact([-1.0, 50.0], decimals=4)

    def test_zero_sum_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_weights_exact([0.0, 0.0], decimals=4)


class NormalizeWeightMapTests(unittest.TestCase):
    def test_allow_cash_keeps_equity_total(self) -> None:
        mapped, cash = normalize_weight_map({"A": 0.3, "B": 0.2}, decimals=4, allow_cash=True)
        self.assertAlmostEqual(cash, 0.5, places=4)
        self.assertAlmostEqual(sum(mapped.values()) + cash, 1.0, places=9)

    def test_no_cash_renormalizes_to_one(self) -> None:
        mapped, cash = normalize_weight_map({"A": 0.3, "B": 0.2}, decimals=4, allow_cash=False)
        self.assertEqual(cash, 0.0)
        self.assertAlmostEqual(sum(mapped.values()), 1.0, places=9)


class NavComputationTests(unittest.TestCase):
    def _close_df(self) -> pd.DataFrame:
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        prices = {
            "A": [10.0, 11.0, 10.5, 11.5, 12.0],
            "B": [20.0, 19.0, 20.5, 21.0, 20.0],
        }
        return pd.DataFrame(prices, index=dates)

    def _run(
        self,
        rebalance_dates: list[pd.Timestamp],
        comparison_rebalance_dates: dict[str, list[pd.Timestamp]] | None = None,
        cash_weight: float = 0.0,
        weights: dict[str, float] | None = None,
        close_df: pd.DataFrame | None = None,
        **costs: float,
    ):
        if close_df is None:
            close_df = self._close_df()
        plan = make_plan("2024-01-01", weights or {"A": 0.5, "B": 0.5}, cash_weight=cash_weight)
        return compute_all_mode_navs(
            *prepare_nav_data(close_df),
            [plan],
            rebalance_dates,
            comparison_rebalance_dates=comparison_rebalance_dates or {},
            commission_rate=costs.get("commission_rate", 0.0),
            stamp_duty_rate=costs.get("stamp_duty_rate", 0.0),
            slippage_rate=costs.get("slippage_rate", 0.0),
            daily_rf_rate=0.0,
            track_top_n=0,
        )

    def test_no_rebalance_nav_first_day(self) -> None:
        nav, _, _, _, _ = self._run([])
        self.assertEqual(nav.iloc[0], 1.0)
        # Day1: A +10%, B -5%, equal weights -> portfolio +2.5%
        self.assertAlmostEqual(nav.iloc[1], 1.025, places=9)

    def test_drifted_weights_second_day(self) -> None:
        nav, _, _, _, _ = self._run([])
        # After day1 weights drift to A=0.55/1.025, B=0.475/1.025.
        # Day2: A 10.5/11, B 20.5/19 -> portfolio value 0.55*0.954545 + 0.475*1.078947 = 1.0375
        self.assertAlmostEqual(nav.iloc[2], 1.0375, places=9)

    def test_nav_ohlc_close_matches_nav_and_candles_are_valid(self) -> None:
        close_df = self._close_df()
        open_df = pd.DataFrame(
            {"A": [10.0, 10.8, 11.1, 10.4, 11.6], "B": [20.0, 19.6, 18.8, 20.6, 21.0]},
            index=close_df.index,
        )
        high_df = pd.DataFrame(
            {"A": [10.2, 11.2, 11.4, 11.7, 12.2], "B": [20.3, 19.8, 20.6, 21.2, 21.4]},
            index=close_df.index,
        )
        low_df = pd.DataFrame(
            {"A": [9.8, 10.5, 10.2, 10.3, 11.4], "B": [19.7, 18.9, 18.6, 20.1, 19.8]},
            index=close_df.index,
        )
        plan = make_plan("2024-01-01", {"A": 0.5, "B": 0.5})
        nav, _, _, _, _ = compute_all_mode_navs(
            *prepare_nav_data(close_df),
            [plan],
            [],
            comparison_rebalance_dates={},
            ohlc_data=prepare_ohlc_data(open_df, high_df, low_df, close_df),
        )
        ohlc = nav.attrs.get("nav_ohlc")
        self.assertIsNotNone(ohlc)
        assert ohlc is not None
        self.assertEqual(ohlc["dates"], [str(d.date()) for d in close_df.index])
        np.testing.assert_allclose(ohlc["close"], nav.values, rtol=1e-12)
        # First bar is the index base 1.0.
        self.assertEqual([ohlc[key][0] for key in ("open", "high", "low", "close")], [1.0, 1.0, 1.0, 1.0])
        # Day 1 with 50/50 weights: open 1.03, high 1.055, low 0.9975, close 1.025.
        self.assertAlmostEqual(ohlc["open"][1], 1.03, places=9)
        self.assertAlmostEqual(ohlc["high"][1], 1.055, places=9)
        self.assertAlmostEqual(ohlc["low"][1], 0.9975, places=9)
        self.assertAlmostEqual(ohlc["close"][1], 1.025, places=9)
        for idx in range(len(ohlc["dates"])):
            self.assertGreaterEqual(ohlc["high"][idx], max(ohlc["open"][idx], ohlc["close"][idx]))
            self.assertLessEqual(ohlc["low"][idx], min(ohlc["open"][idx], ohlc["close"][idx]))

    def test_suspension_resumption_candle_uses_last_available_close(self) -> None:
        """停牌复牌日：OHLC 比率必须与净值使用同一个前收（最近有效收盘）。"""
        index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
        # 2024-01-04 停牌（无行情），01-05 复牌 10.0 -> 12.0
        close_df = pd.DataFrame({"A": [10.0, 10.0, np.nan, 12.0]}, index=index)
        open_df = pd.DataFrame({"A": [10.0, 10.0, np.nan, 12.0]}, index=index)
        high_df = pd.DataFrame({"A": [10.0, 10.0, np.nan, 12.6]}, index=index)
        low_df = pd.DataFrame({"A": [10.0, 10.0, np.nan, 11.8]}, index=index)

        ohlc = prepare_ohlc_data(open_df, high_df, low_df, close_df)
        self.assertEqual(ohlc["open"][2, 0], 1.0, "停牌日应是一根无波动的占位蜡烛")
        self.assertAlmostEqual(ohlc["open"][3, 0], 1.2, places=9)
        self.assertAlmostEqual(ohlc["high"][3, 0], 1.26, places=9)
        self.assertAlmostEqual(ohlc["low"][3, 0], 1.18, places=9)

        returns_array, *_ = prepare_nav_data(close_df)
        self.assertEqual(returns_array[2, 0], 0.0, "停牌日收益记 0")
        self.assertAlmostEqual(returns_array[3, 0], 0.2, places=9, msg="复牌日收益应包含整个跳空")

    def test_suspension_nav_ohlc_close_still_matches_nav(self) -> None:
        index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
        close_df = pd.DataFrame({"A": [10.0, 10.0, np.nan, 12.0]}, index=index)
        open_df = pd.DataFrame({"A": [10.0, 10.0, np.nan, 12.0]}, index=index)
        high_df = pd.DataFrame({"A": [10.0, 10.0, np.nan, 12.6]}, index=index)
        low_df = pd.DataFrame({"A": [10.0, 10.0, np.nan, 11.8]}, index=index)
        nav, *_ = compute_all_mode_navs(
            *prepare_nav_data(close_df),
            [make_plan("2024-01-01", {"A": 1.0})],
            [],
            comparison_rebalance_dates={},
            ohlc_data=prepare_ohlc_data(open_df, high_df, low_df, close_df),
        )
        ohlc = nav.attrs.get("nav_ohlc")
        self.assertIsNotNone(ohlc)
        assert ohlc is not None
        np.testing.assert_allclose(ohlc["close"], nav.values, rtol=1e-12)
        for idx in range(len(ohlc["dates"])):
            self.assertGreaterEqual(ohlc["high"][idx], max(ohlc["open"][idx], ohlc["close"][idx]))
            self.assertLessEqual(ohlc["low"][idx], min(ohlc["open"][idx], ohlc["close"][idx]))

    def test_rebalance_with_cost_reduces_nav_and_logs_cost(self) -> None:
        dates = [pd.Timestamp("2024-01-03")]
        nav_cost, applied, cost_log, _, _ = self._run(
            dates, commission_rate=0.00025, stamp_duty_rate=0.0005, slippage_rate=0.001
        )
        nav_free, _, _, _, _ = self._run(dates)
        self.assertGreater(len(cost_log), 0)
        self.assertEqual(cost_log[0]["date"], "2024-01-03")
        self.assertEqual(applied, ["2024-01-01", "2024-01-03"])
        self.assertLess(nav_cost.iloc[-1], nav_free.iloc[-1])

    def test_primary_equals_comparison_with_same_schedule_and_cost(self) -> None:
        dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-05")]
        cost = dict(commission_rate=0.00025, stamp_duty_rate=0.0005, slippage_rate=0.001)
        nav, _, _, _, comps = self._run(dates, comparison_rebalance_dates={"x": dates}, **cost)
        np.testing.assert_allclose(nav.values, comps["x"].values, rtol=1e-12)

    def test_primary_equals_comparison_with_cash_and_cost(self) -> None:
        # Regression: with a cash position, identical rebalance schedules and
        # costs, the primary and comparison NAVs must still match. Cash turnover
        # on rebalance must use the drifted cash weight in both paths — using
        # the pre-drift weight made the comparison NAV diverge. Falling prices
        # drift the cash weight ABOVE its target, so the divergence shows up in
        # the rebalance cost (rising prices hide it: max(0, cash - target) == 0).
        falling = pd.DataFrame(
            {
                "A": [10.0, 9.5, 9.0, 8.6, 8.2],
                "B": [20.0, 19.2, 18.5, 17.8, 17.0],
            },
            index=pd.date_range("2024-01-01", periods=5, freq="B"),
        )
        dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-05")]
        cost = dict(commission_rate=0.00025, stamp_duty_rate=0.0005, slippage_rate=0.001)
        nav, _, _, _, comps = self._run(
            dates,
            comparison_rebalance_dates={"x": dates},
            cash_weight=0.5,
            weights={"A": 0.25, "B": 0.25},
            close_df=falling,
            **cost,
        )
        np.testing.assert_allclose(nav.values, comps["x"].values, rtol=1e-12)

    def test_comparison_modes_apply_costs(self) -> None:
        dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-04"), pd.Timestamp("2024-01-05")]
        cost = dict(commission_rate=0.00025, stamp_duty_rate=0.0005, slippage_rate=0.001)
        _, _, _, _, comp_with = self._run(dates, comparison_rebalance_dates={"m": dates}, **cost)
        _, _, _, _, comp_free = self._run(dates, comparison_rebalance_dates={"m": dates})
        self.assertLess(comp_with["m"].iloc[-1], comp_free["m"].iloc[-1])

    def test_holdings_evolution_shape(self) -> None:
        close_df = self._close_df()
        plan = make_plan("2024-01-01", {"A": 0.5, "B": 0.5})
        _, _, _, holdings, _ = compute_all_mode_navs(
            *prepare_nav_data(close_df),
            [plan],
            [pd.Timestamp("2024-01-02")],
            comparison_rebalance_dates={},
            track_top_n=2,
        )
        self.assertIsNotNone(holdings)
        assert holdings is not None
        codes = {item["code"] for item in holdings}
        self.assertTrue({"A", "B"} <= codes)
        for item in holdings:
            self.assertEqual(len(item["dates"]), len(close_df))
            self.assertEqual(len(item["weights"]), len(close_df))


class MetricsTests(unittest.TestCase):
    def test_returns_drawdown_and_win_rate(self) -> None:
        nav = pd.Series([1.0, 1.2, 0.9, 1.1, 1.3], index=pd.date_range("2024-01-01", periods=5, freq="B"))
        metrics = compute_metrics(nav, risk_free_rate=0.02)
        self.assertAlmostEqual(metrics["total_return"], 0.3, places=9)
        self.assertAlmostEqual(metrics["max_drawdown"], 0.9 / 1.2 - 1.0, places=9)
        self.assertEqual(metrics["max_drawdown_start"], "2024-01-02")
        self.assertEqual(metrics["max_drawdown_trough"], "2024-01-03")
        self.assertEqual(metrics["max_drawdown_recovery"], "2024-01-05")
        self.assertEqual(metrics["max_drawdown_duration_days"], 2)
        self.assertAlmostEqual(metrics["win_rate"], 0.75, places=9)


class RebalanceDatesTests(unittest.TestCase):
    def test_monthly_picks_first_trading_day_of_each_month(self) -> None:
        dates = pd.date_range("2023-12-29", "2024-03-01", freq="B")
        result = build_periodic_rebalance_dates(dates, "monthly", [])
        keys = {(d.year, d.month) for d in result}
        self.assertEqual(keys, {(2023, 12), (2024, 1), (2024, 2), (2024, 3)})

    def test_quarterly_picks_first_trading_day_of_each_quarter(self) -> None:
        dates = pd.date_range("2023-12-29", "2024-03-01", freq="B")
        result = build_periodic_rebalance_dates(dates, "quarterly", [])
        keys = {(d.year, (d.month - 1) // 3 + 1) for d in result}
        self.assertEqual(keys, {(2023, 4), (2024, 1)})


class DropUnavailableTests(unittest.TestCase):
    def _close_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {"A": [1.0, 2.0], "B": [np.nan, np.nan]},
            index=pd.date_range("2024-01-01", periods=2),
        )

    def test_drop_with_cash_keeps_equity_and_absorbs_gap(self) -> None:
        plan = make_plan("2024-01-01", {"A": 0.5, "B": 0.5})
        clean, warnings = drop_unavailable_symbols([plan], self._close_df(), allow_cash=True)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(list(clean[0].weight_map.keys()), ["A"])
        self.assertAlmostEqual(clean[0].weight_map["A"], 0.5, places=9)
        self.assertAlmostEqual(clean[0].cash_weight, 0.5, places=9)

    def test_drop_renormalizes_without_cash(self) -> None:
        plan = make_plan("2024-01-01", {"A": 0.5, "B": 0.5})
        clean, _ = drop_unavailable_symbols([plan], self._close_df(), allow_cash=False)
        self.assertAlmostEqual(clean[0].weight_map["A"], 1.0, places=9)
        self.assertEqual(clean[0].cash_weight, 0.0)


if __name__ == "__main__":
    unittest.main()
