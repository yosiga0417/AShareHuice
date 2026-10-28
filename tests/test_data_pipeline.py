"""Data pipeline regressions; all remote calls and cache paths are isolated.

Atomic write failure/old-file preservation is already covered by
``test_ohlc_cache.OhlcCacheTests`` and is intentionally not duplicated here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backend import cache, fetch


SYMBOL = "000001"
BENCHMARK = "000300"
START = date(2024, 1, 2)
END = date(2024, 1, 12)
TODAY = date(2024, 1, 8)
AFTER_CLOSE = datetime(2024, 1, 8, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def candles(start: date, end: date) -> pd.DataFrame:
    """Stable prices across overlapping fetches, including expanded gaps."""
    index = pd.bdate_range(start, end)
    close = index.day.to_numpy(dtype="float64") + 10.0
    return pd.DataFrame(
        {"open": close - 0.1, "high": close + 0.2,
         "low": close - 0.2, "close": close},
        index=index,
    )


def stock_remote(_symbol: str, start: date, end: date):
    frame = candles(start, end)
    return frame["close"].rename("close"), frame, "stock_zh_a_hist"


def ohlc_remote(_symbol: str, start: date, end: date):
    return candles(start, end), "stock_zh_a_hist"


def benchmark_remote(_code: str, start: date, end: date):
    return candles(start, end)["close"].rename("close")


class DataPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="test-data-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        directories = {
            "CACHE_ROOT": self.root,
            "STOCK_CACHE_DIR": self.root / "stock_close",
            "STOCK_META_DIR": self.root / "stock_close_meta",
            "STOCK_OHLC_CACHE_DIR": self.root / "stock_ohlc",
            "STOCK_OHLC_META_DIR": self.root / "stock_ohlc_meta",
            "BENCHMARK_CACHE_DIR": self.root / "benchmark_close",
            "BENCHMARK_META_DIR": self.root / "benchmark_close_meta",
        }
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        self.contexts.enter_context(patch.multiple(
            cache, **directories, TODAY_QUOTE_CACHE={}, TODAY_OHLC_CACHE={},
            CACHE_RESOURCE_LOCKS={},
        ))
        # Freeze time and never consult the real exchange calendar or network.
        self.contexts.enter_context(patch.object(fetch, "get_china_now", return_value=datetime(
            2024, 2, 1, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )))
        self.contexts.enter_context(patch.object(fetch, "is_trading_day", return_value=True))
        self.contexts.enter_context(patch("socket.create_connection", side_effect=AssertionError(
            "External network access is forbidden in data pipeline tests"
        )))
        self.contexts.enter_context(patch("socket.socket.connect", side_effect=AssertionError(
            "External network access is forbidden in data pipeline tests"
        )))
        for remote in ("fetch_symbol_close_series_remote", "fetch_symbol_ohlc_remote",
                       "fetch_benchmark_close_series_remote"):
            self.contexts.enter_context(patch.object(fetch, remote, side_effect=AssertionError(
                f"Unexpected remote call: {remote}"
            )))

    def assertSeriesEqual(self, actual: pd.Series, expected: pd.Series) -> None:
        pd.testing.assert_series_equal(actual, expected, check_names=False, check_freq=False)

    def assertFrameEqual(self, actual: pd.DataFrame, expected: pd.DataFrame) -> None:
        pd.testing.assert_frame_equal(actual, expected, check_names=False, check_freq=False)

    def test_close_corrupt_or_missing_file_does_not_trust_range_metadata(self) -> None:
        for broken in ("corrupt", "missing"):
            with self.subTest(file=broken):
                symbol = "close-" + broken
                cache.save_cached_stock_series(symbol, candles(START, END)["close"])
                cache.save_cached_stock_ranges(symbol, [(START, END)])
                path = cache.stock_cache_file(symbol)
                # Warm the LRU too: changing/deleting the file must invalidate it.
                cache.load_cached_stock_series(symbol)
                if broken == "corrupt":
                    path.write_bytes(b"not a feather file")
                else:
                    path.unlink()
                with patch.object(fetch, "fetch_symbol_close_series_remote",
                                  side_effect=stock_remote) as remote:
                    result, source = fetch.fetch_symbol_close_series(symbol, START, END)
                    again, second_source = fetch.fetch_symbol_close_series(symbol, START, END)
                remote.assert_called_once_with(symbol, START, END)
                self.assertEqual((source, second_source), ("stock_zh_a_hist", "cache"))
                self.assertSeriesEqual(result, candles(START, END)["close"])
                self.assertSeriesEqual(again, result)
                self.assertSeriesEqual(cache.load_cached_stock_series(symbol), result)
                self.assertEqual(cache.load_cached_stock_ranges(symbol), [(START, END)])

    def test_benchmark_corrupt_or_missing_file_is_refetched(self) -> None:
        for broken in ("corrupt", "missing"):
            with self.subTest(file=broken):
                code = "benchmark-" + broken
                expected = candles(START, END)["close"]
                cache.save_cached_benchmark_series(code, expected)
                cache.save_cached_benchmark_ranges(code, [(START, END)])
                path = cache.benchmark_cache_file(code)
                cache.load_cached_benchmark_series(code)
                if broken == "corrupt":
                    path.write_bytes(b"broken benchmark feather")
                else:
                    path.unlink()
                with patch.object(fetch, "fetch_benchmark_close_series_remote",
                                  side_effect=benchmark_remote) as remote:
                    result = fetch.fetch_benchmark_nav(code, START, END)
                    again = fetch.fetch_benchmark_nav(code, START, END)
                remote.assert_called_once_with(code, START, END)
                self.assertEqual(result, again)
                self.assertEqual(result["dates"], [str(day.date()) for day in expected.index])
                np.testing.assert_allclose(result["values"], expected.to_numpy())
                self.assertSeriesEqual(cache.load_cached_benchmark_series(code), expected)
                self.assertEqual(cache.load_cached_benchmark_ranges(code), [(START, END)])

    def test_ohlc_then_close_reuses_covered_range_without_remote(self) -> None:
        with patch.object(fetch, "fetch_symbol_ohlc_remote", side_effect=ohlc_remote) as remote:
            frame, _ = fetch.fetch_symbol_ohlc_frame(SYMBOL, START, END)
        remote.assert_called_once_with(SYMBOL, START, END)
        # The default remote mock raises, so these must reuse the OHLC cache.
        close, source = fetch.fetch_symbol_close_series(SYMBOL, START, END)
        subset, subset_source = fetch.fetch_symbol_close_series(
            SYMBOL, date(2024, 1, 4), date(2024, 1, 9)
        )
        self.assertEqual((source, subset_source), ("cache", "cache"))
        self.assertSeriesEqual(close, frame["close"])
        self.assertSeriesEqual(subset, frame.loc["2024-01-04":"2024-01-09", "close"])
        self.assertEqual(cache.load_cached_stock_ranges(SYMBOL), [(START, END)])

    def test_close_three_gaps_write_ohlc_and_metadata_only_once(self) -> None:
        islands = [(date(2024, 1, 4), date(2024, 1, 4)),
                   (date(2024, 1, 9), date(2024, 1, 9))]
        cached = pd.concat([candles(start, end)["close"] for start, end in islands])
        cache.save_cached_stock_series(SYMBOL, cached)
        cache.save_cached_stock_ranges(SYMBOL, islands)
        cache.save_cached_stock_ohlc(
            SYMBOL, pd.concat([candles(start, end) for start, end in islands])
        )
        cache.save_cached_stock_ohlc_ranges(SYMBOL, islands)
        with patch.object(fetch, "fetch_symbol_close_series_remote", side_effect=stock_remote) as remote, \
             patch.object(fetch, "save_cached_stock_ohlc", wraps=cache.save_cached_stock_ohlc) as write, \
             patch.object(fetch, "save_cached_stock_ohlc_ranges", wraps=cache.save_cached_stock_ohlc_ranges) as meta:
            result, _ = fetch.fetch_symbol_close_series(SYMBOL, START, END)
        self.assertEqual(remote.call_count, 3)
        write.assert_called_once()
        meta.assert_called_once()
        self.assertSeriesEqual(result, candles(START, END)["close"])
        self.assertFrameEqual(cache.load_cached_stock_ohlc(SYMBOL), candles(START, END))
        self.assertEqual(cache.load_cached_stock_ohlc_ranges(SYMBOL), [(START, END)])

    def test_ohlc_three_gaps_write_frame_and_metadata_only_once(self) -> None:
        islands = [(date(2024, 1, 4), date(2024, 1, 4)),
                   (date(2024, 1, 9), date(2024, 1, 9))]
        cache.save_cached_stock_ohlc(
            SYMBOL, pd.concat([candles(start, end) for start, end in islands])
        )
        cache.save_cached_stock_ohlc_ranges(SYMBOL, islands)
        with patch.object(fetch, "fetch_symbol_ohlc_remote", side_effect=ohlc_remote) as remote, \
             patch.object(fetch, "save_cached_stock_ohlc", wraps=cache.save_cached_stock_ohlc) as write, \
             patch.object(fetch, "save_cached_stock_ohlc_ranges", wraps=cache.save_cached_stock_ohlc_ranges) as meta:
            result, _ = fetch.fetch_symbol_ohlc_frame(SYMBOL, START, END)
        self.assertEqual(remote.call_count, 3)
        write.assert_called_once()
        meta.assert_called_once()
        self.assertFrameEqual(result, candles(START, END))
        self.assertEqual(cache.load_cached_stock_ohlc_ranges(SYMBOL), [(START, END)])

    def test_feather_writes_use_zstd_and_round_trip_without_data_loss(self) -> None:
        expected = candles(START, END)
        original_to_feather = pd.DataFrame.to_feather
        with patch.object(pd.DataFrame, "to_feather", autospec=True,
                          side_effect=original_to_feather) as write:
            cache.save_cached_stock_series(SYMBOL, expected["close"])
            cache.save_cached_stock_ohlc(SYMBOL, expected)
        self.assertEqual(write.call_count, 2)
        for call in write.call_args_list:
            self.assertEqual(call.kwargs.get("compression"), "zstd")
        self.assertSeriesEqual(cache.load_cached_stock_series(SYMBOL), expected["close"])
        self.assertFrameEqual(cache.load_cached_stock_ohlc(SYMBOL), expected)

    def test_load_cached_series_returns_independent_copy_on_every_call(self) -> None:
        expected = candles(START, END)["close"]
        for kind in ("close", "ohlc", "benchmark"):
            with self.subTest(cache=kind):
                path = self.root / (kind + ".feather")
                if kind == "ohlc":
                    cache.save_cached_ohlc_frame(path, candles(START, END))
                else:
                    cache.save_cached_series(path, expected)
                first = cache.load_cached_series(path)
                second = cache.load_cached_series(path)
                first.iloc[0] = -999.0
                first.index = first.index + pd.Timedelta(days=100)
                first.name = "mutated"
                self.assertSeriesEqual(second, expected)
                self.assertSeriesEqual(cache.load_cached_series(path), expected)
                second.iloc[-1] = -123.0
                self.assertSeriesEqual(cache.load_cached_series(path), expected)

    def test_after_close_stale_close_response_does_not_permanently_cache_today(self) -> None:
        historical_end = date(2024, 1, 7)
        history = candles(START, historical_end)
        cache.save_cached_stock_series(SYMBOL, history["close"])
        cache.save_cached_stock_ranges(SYMBOL, [(START, historical_end)])
        stale = candles(date(2024, 1, 5), date(2024, 1, 5))
        fresh = candles(TODAY, TODAY)
        responses = [(frame["close"], frame, "stock_zh_a_hist") for frame in (stale, fresh)]
        with patch.object(fetch, "get_china_now", return_value=AFTER_CLOSE), \
             patch.object(fetch, "fetch_symbol_close_series_remote", side_effect=responses) as remote:
            first, _ = fetch.fetch_symbol_close_series(SYMBOL, START, TODAY)
            self.assertSeriesEqual(first, history["close"])
            self.assertFalse(fetch.date_is_cached(TODAY, cache.load_cached_stock_ranges(SYMBOL)))
            self.assertFalse(fetch.date_is_cached(TODAY, cache.load_cached_stock_ohlc_ranges(SYMBOL)))
            self.assertNotIn(pd.Timestamp(TODAY), cache.load_cached_stock_series(SYMBOL).index)
            second, _ = fetch.fetch_symbol_close_series(SYMBOL, START, TODAY)
            third, _ = fetch.fetch_symbol_close_series(SYMBOL, START, TODAY)
        self.assertEqual(remote.call_count, 2, "stale today must retry; fresh today must persist")
        self.assertEqual(remote.call_args_list[0].args, (SYMBOL, TODAY, TODAY))
        self.assertSeriesEqual(second, pd.concat([history["close"], fresh["close"]]))
        self.assertSeriesEqual(third, second)
        self.assertTrue(fetch.date_is_cached(TODAY, cache.load_cached_stock_ranges(SYMBOL)))
        self.assertTrue(fetch.date_is_cached(TODAY, cache.load_cached_stock_ohlc_ranges(SYMBOL)))

    def test_after_close_stale_ohlc_response_does_not_permanently_cache_today(self) -> None:
        historical_end = date(2024, 1, 7)
        history = candles(START, historical_end)
        cache.save_cached_stock_ohlc(SYMBOL, history)
        cache.save_cached_stock_ohlc_ranges(SYMBOL, [(START, historical_end)])
        stale = candles(date(2024, 1, 5), date(2024, 1, 5))
        fresh = candles(TODAY, TODAY)
        with patch.object(fetch, "get_china_now", return_value=AFTER_CLOSE), \
             patch.object(fetch, "fetch_symbol_ohlc_remote", side_effect=[
                 (stale, "stock_zh_a_hist"), (fresh, "stock_zh_a_hist")
             ]) as remote:
            first, _ = fetch.fetch_symbol_ohlc_frame(SYMBOL, START, TODAY)
            self.assertFrameEqual(first, history)
            self.assertFalse(fetch.date_is_cached(TODAY, cache.load_cached_stock_ohlc_ranges(SYMBOL)))
            self.assertNotIn(pd.Timestamp(TODAY), cache.load_cached_stock_ohlc(SYMBOL).index)
            second, _ = fetch.fetch_symbol_ohlc_frame(SYMBOL, START, TODAY)
            third, _ = fetch.fetch_symbol_ohlc_frame(SYMBOL, START, TODAY)
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(remote.call_args_list[0].args, (SYMBOL, TODAY, TODAY))
        self.assertFrameEqual(second, pd.concat([history, fresh]))
        self.assertFrameEqual(third, second)
        self.assertTrue(fetch.date_is_cached(TODAY, cache.load_cached_stock_ohlc_ranges(SYMBOL)))

    def test_after_close_stale_benchmark_response_does_not_permanently_cache_today(self) -> None:
        historical_end = date(2024, 1, 7)
        history = candles(START, historical_end)["close"]
        cache.save_cached_benchmark_series(BENCHMARK, history)
        cache.save_cached_benchmark_ranges(BENCHMARK, [(START, historical_end)])
        stale = candles(date(2024, 1, 5), date(2024, 1, 5))["close"]
        fresh = candles(TODAY, TODAY)["close"]
        with patch.object(fetch, "get_china_now", return_value=AFTER_CLOSE), \
             patch.object(fetch, "fetch_benchmark_close_series_remote", side_effect=[stale, fresh]) as remote:
            # A missing current bar may either fail explicitly or return history.
            try:
                first = fetch.fetch_benchmark_nav(BENCHMARK, START, TODAY)
            except RuntimeError:
                pass
            else:
                self.assertNotIn(str(TODAY), first["dates"])
            self.assertFalse(fetch.date_is_cached(TODAY, cache.load_cached_benchmark_ranges(BENCHMARK)))
            self.assertNotIn(pd.Timestamp(TODAY), cache.load_cached_benchmark_series(BENCHMARK).index)
            second = fetch.fetch_benchmark_nav(BENCHMARK, START, TODAY)
            third = fetch.fetch_benchmark_nav(BENCHMARK, START, TODAY)
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(remote.call_args_list[0].args, (BENCHMARK, TODAY, TODAY))
        self.assertEqual(second, third)
        self.assertEqual(second["dates"][-1], str(TODAY))
        self.assertEqual(second["values"][-1], fresh.iloc[0])
        self.assertTrue(fetch.date_is_cached(TODAY, cache.load_cached_benchmark_ranges(BENCHMARK)))

    def test_concurrent_close_and_ohlc_for_same_stock_fetch_only_once(self) -> None:
        # Exercise both lock directions: OHLC first and close first must share
        # the same stock lock, with the losing path reusing the winner's data.
        for first_kind in ("ohlc", "close"):
            with self.subTest(first=first_kind):
                symbol = "concurrent-" + first_kind
                entered = threading.Event()
                release = threading.Event()

                def blocked_remote(code, start, end):
                    entered.set()
                    if not release.wait(timeout=5):
                        raise AssertionError("test did not release the remote call")
                    return stock_remote(code, start, end)

                def load(kind):
                    fn = fetch.fetch_symbol_ohlc_frame if kind == "ohlc" else fetch.fetch_symbol_close_series
                    return fn(symbol, START, END)[0]

                # Restore the real OHLC wrapper so both paths use one mock.
                with patch.object(fetch, "fetch_symbol_close_series_remote", side_effect=blocked_remote) as remote, \
                     patch.object(fetch, "fetch_symbol_ohlc_remote", side_effect=lambda code, start, end: (
                         fetch.fetch_symbol_close_series_remote(code, start, end)[1], "stock_zh_a_hist"
                     )), ThreadPoolExecutor(max_workers=8) as executor:
                    first = executor.submit(load, first_kind)
                    if not entered.wait(timeout=5):
                        release.set()
                        self.fail("first request never reached the mock remote")
                    others = [executor.submit(load, "close" if i % 2 else "ohlc") for i in range(7)]
                    release.set()
                    results = [future.result(timeout=10) for future in [first, *others]]
                remote.assert_called_once_with(symbol, START, END)
                for result in results:
                    if isinstance(result, pd.DataFrame):
                        self.assertFrameEqual(result, candles(START, END))
                    else:
                        self.assertSeriesEqual(result, candles(START, END)["close"])


class OhlcNormalizationPipelineTests(unittest.TestCase):
    def assertFrameEqual(self, actual, expected) -> None:
        pd.testing.assert_frame_equal(actual, expected, check_names=False, check_freq=False)

    def test_normalized_fast_path_keeps_values_and_returns_independent_copy(self) -> None:
        expected = candles(START, END)
        for date_column in (False, True):
            with self.subTest(date_column=date_column):
                source = expected.rename_axis("date").reset_index() if date_column else expected.copy()
                original = source.copy(deep=True)
                with patch.object(cache.pd, "to_numeric", side_effect=AssertionError(
                    "normalized OHLC should not need numeric coercion"
                )):
                    actual = cache.normalize_ohlc_frame(source)
                self.assertFrameEqual(actual, expected)
                actual.iloc[0, 0] = -999.0
                pd.testing.assert_frame_equal(source, original)

    def test_missing_columns_and_values_preserve_fill_and_drop_semantics(self) -> None:
        for source in (
            pd.DataFrame({"close": [10.0, np.nan, 12.0]}, index=pd.bdate_range(START, periods=3)),
            pd.DataFrame({"open": [np.nan, 20.0, 11.0], "high": [np.nan, 30.0, np.nan],
                          "low": [np.nan, 1.0, np.nan], "close": [10.0, np.nan, 12.0]},
                         index=pd.bdate_range(START, periods=3)),
        ):
            with self.subTest(columns=list(source.columns)):
                actual = cache.normalize_ohlc_frame(source)
                third_open = 11.0 if "open" in source else 12.0
                expected = pd.DataFrame(
                    {"open": [10.0, third_open], "high": [10.0, 12.0],
                     "low": [10.0, third_open], "close": [10.0, 12.0]},
                    index=pd.to_datetime(["2024-01-02", "2024-01-04"]),
                )
                self.assertFrameEqual(actual, expected)

    def test_unsorted_duplicate_dates_keep_last_row_and_sort(self) -> None:
        source = pd.DataFrame(
            {"open": [30.0, 10.0, 20.0, 40.0], "high": [31.0, 11.0, 21.0, 41.0],
             "low": [29.0, 9.0, 19.0, 39.0], "close": [30.5, 10.5, 20.5, 40.5]},
            index=pd.to_datetime(["2024-01-04", "2024-01-02", "2024-01-03", "2024-01-02"]),
        )
        expected = source.iloc[[3, 2, 0]].copy()
        actual = cache.normalize_ohlc_frame(source)
        self.assertFrameEqual(actual, expected)
        self.assertTrue(actual.index.is_monotonic_increasing)
        self.assertFalse(actual.index.has_duplicates)

    def test_intraday_and_invalid_dates_and_numeric_strings_are_repaired(self) -> None:
        source = pd.DataFrame(
            {"date": ["2024-01-03 14:30", "bad date", "2024-01-02 10:00", "2024-01-02 15:00"],
             "close": ["12.5", "99", "10.0", "11.0"]}
        )
        expected = pd.DataFrame(
            {key: [11.0, 12.5] for key in cache.OHLC_COLUMNS},
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )
        self.assertFrameEqual(cache.normalize_ohlc_frame(source), expected)

    def test_out_of_bounds_high_low_are_repaired_even_for_float_datetime_input(self) -> None:
        source = pd.DataFrame(
            {"open": [10.0, 13.0], "high": [9.0, 11.0],
             "low": [12.0, 14.0], "close": [11.0, 12.0]},
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )
        expected = source.copy()
        expected["high"] = [11.0, 13.0]
        expected["low"] = [10.0, 12.0]
        self.assertFrameEqual(cache.normalize_ohlc_frame(source), expected)


if __name__ == "__main__":
    unittest.main()
