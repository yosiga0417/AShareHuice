from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.cache import normalize_ohlc_frame
from backend.fetch import derive_ohlc_from_close, extract_date_ohlc_frame, fetch_ohlc_matrices


class ExtractOhlcFrameTests(unittest.TestCase):
    def test_chinese_columns(self) -> None:
        raw = pd.DataFrame(
            {
                "日期": ["2024-01-02", "2024-01-03"],
                "开盘": [10.0, 10.5],
                "收盘": [10.4, 10.8],
                "最高": [10.6, 11.0],
                "最低": [9.9, 10.2],
                "成交量": [100, 120],
            }
        )
        frame = extract_date_ohlc_frame(raw)
        self.assertEqual(list(frame.columns), ["open", "high", "low", "close"])
        self.assertEqual([str(d.date()) for d in frame.index], ["2024-01-02", "2024-01-03"])
        self.assertAlmostEqual(frame.iloc[0]["open"], 10.0)
        self.assertAlmostEqual(frame.iloc[1]["close"], 10.8)

    def test_english_fallback_columns(self) -> None:
        raw = pd.DataFrame(
            {
                "date": ["2024-01-02"],
                "open": [20.0],
                "close": [20.5],
                "high": [20.8],
                "low": [19.9],
                "amount": [1],
            }
        )
        frame = extract_date_ohlc_frame(raw)
        self.assertAlmostEqual(frame.iloc[0]["high"], 20.8)
        self.assertAlmostEqual(frame.iloc[0]["low"], 19.9)

    def test_missing_parts_are_filled_from_close(self) -> None:
        raw = pd.DataFrame({"日期": ["2024-01-02"], "收盘": [10.0]})
        frame = extract_date_ohlc_frame(raw)
        row = frame.iloc[0]
        self.assertAlmostEqual(row["open"], 10.0)
        self.assertAlmostEqual(row["high"], 10.0)
        self.assertAlmostEqual(row["low"], 10.0)


class NormalizeOhlcTests(unittest.TestCase):
    def test_brackets_are_repaired(self) -> None:
        frame = normalize_ohlc_frame(
            pd.DataFrame(
                {
                    "date": ["2024-01-02"],
                    "open": [10.0],
                    "high": [9.5],
                    "low": [10.5],
                    "close": [10.2],
                }
            )
        )
        row = frame.iloc[0]
        self.assertGreaterEqual(row["high"], max(row["open"], row["close"]))
        self.assertLessEqual(row["low"], min(row["open"], row["close"]))


class DeriveOhlcFromCloseTests(unittest.TestCase):
    def test_open_uses_last_available_close_after_suspension(self) -> None:
        idx = pd.date_range("2024-01-02", periods=4, freq="B")
        close = pd.Series([10.0, 10.0, np.nan, 12.0], index=idx)
        frame = derive_ohlc_from_close(close)
        # 复牌日开盘应取最近一次有效收盘（10.0），而不是直接等于当日收盘。
        self.assertAlmostEqual(frame.loc[idx[3], "open"], 10.0)
        self.assertAlmostEqual(frame.loc[idx[3], "close"], 12.0)


class FetchOhlcMatricesTests(unittest.TestCase):
    def test_failed_symbol_falls_back_to_close_candles(self) -> None:
        idx = pd.date_range("2024-01-02", periods=3, freq="B")
        close_df = pd.DataFrame(
            {"000001": [10.0, 10.5, 10.2], "000002": [20.0, 20.4, 20.1]},
            index=idx,
        )
        good = pd.DataFrame(
            {
                "open": [10.0, 10.4, 10.6],
                "high": [10.3, 10.7, 10.8],
                "low": [9.9, 10.2, 10.0],
                "close": [10.0, 10.5, 10.2],
            },
            index=idx,
        )

        def fake_fetch(symbol: str, _start: date, _end: date):
            if symbol == "000002":
                raise RuntimeError("network down")
            return good, "stock_zh_a_hist"

        with patch("backend.fetch.fetch_symbol_ohlc_frame", side_effect=fake_fetch):
            open_df, high_df, low_df, warnings = fetch_ohlc_matrices(
                ["000001", "000002"], date(2024, 1, 2), date(2024, 1, 4), close_df
            )

        self.assertEqual(list(open_df.columns), ["000001", "000002"])
        self.assertEqual(list(open_df.index), list(idx))
        # Good symbol uses its real open; failed symbol opens at the previous close.
        self.assertAlmostEqual(open_df["000001"].iloc[1], 10.4)
        self.assertAlmostEqual(open_df["000002"].iloc[1], 20.0)
        # Close always comes from the close matrix; high brackets open/close.
        np.testing.assert_allclose(high_df["000002"], [20.0, 20.4, 20.4])
        self.assertTrue((high_df["000002"].to_numpy() >= close_df["000002"].to_numpy()).all())
        self.assertTrue(any("1 只成分股开高低收获取失败" in item for item in warnings))

    def test_ohlc_baseline_is_rescaled_to_official_close(self) -> None:
        idx = pd.date_range("2024-01-02", periods=3, freq="B")
        close_df = pd.DataFrame({"000001": [10.0, 10.5, 10.2]}, index=idx)
        # Same shape as the official close, but a different qfq baseline (2x).
        mismatched = pd.DataFrame(
            {
                "open": [19.8, 20.5, 21.2],
                "high": [20.2, 21.2, 21.4],
                "low": [19.7, 20.4, 20.3],
                "close": [20.0, 21.0, 20.4],
            },
            index=idx,
        )

        with patch("backend.fetch.fetch_symbol_ohlc_frame", return_value=(mismatched, "stock_zh_a_hist")):
            open_df, high_df, low_df, _warnings = fetch_ohlc_matrices(
                ["000001"], date(2024, 1, 2), date(2024, 1, 4), close_df
            )

        np.testing.assert_allclose(open_df["000001"], [9.9, 10.25, 10.6])
        # A candle must always contain its own close even after rescaling.
        self.assertTrue((high_df["000001"].to_numpy() >= close_df["000001"].to_numpy()).all())
        self.assertTrue((low_df["000001"].to_numpy() <= close_df["000001"].to_numpy()).all())


if __name__ == "__main__":
    unittest.main()
