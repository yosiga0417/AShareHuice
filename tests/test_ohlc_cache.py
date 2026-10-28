"""OHLC 缓存读写回归测试。

覆盖：首次拉取落盘、命中缓存不再请求、扩展区间只补缺口、feather 缺失/损坏
时自动重拉、收盘价请求顺带写入 OHLC 缓存（不存在二次请求），以及原子写入在
失败时保留旧文件、不残留临时文件。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backend import cache
from backend.fetch import (
    fetch_symbol_close_series,
    fetch_symbol_ohlc_frame,
)


def ohlc_frame(start: str, end: str, base: float = 10.0) -> pd.DataFrame:
    index = pd.bdate_range(start, end)
    steps = np.arange(len(index), dtype=float)
    close = base + steps
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
        },
        index=index,
    )


class OhlcCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ohlc_dir = root / "stock_ohlc"
        self.ohlc_meta = root / "stock_ohlc_meta"
        self.stock_dir = root / "stock_close"
        self.stock_meta = root / "stock_close_meta"
        for directory in (self.ohlc_dir, self.ohlc_meta, self.stock_dir, self.stock_meta):
            directory.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patched(self) -> patch:
        return patch.multiple(
            cache,
            STOCK_OHLC_CACHE_DIR=self.ohlc_dir,
            STOCK_OHLC_META_DIR=self.ohlc_meta,
            STOCK_CACHE_DIR=self.stock_dir,
            STOCK_META_DIR=self.stock_meta,
        )

    def test_first_fetch_writes_cache_and_second_is_cache_hit(self) -> None:
        calls: list[tuple[date, date]] = []

        def fake_remote(symbol: str, start: date, end: date):
            calls.append((start, end))
            return ohlc_frame(str(start), str(end)), "stock_zh_a_hist"

        with self._patched(), patch(
            "backend.fetch.fetch_symbol_ohlc_remote", side_effect=fake_remote
        ):
            first, source = fetch_symbol_ohlc_frame("000001", date(2024, 1, 2), date(2024, 1, 5))
            second, second_source = fetch_symbol_ohlc_frame(
                "000001", date(2024, 1, 2), date(2024, 1, 5)
            )

        self.assertEqual(source, "stock_zh_a_hist")
        self.assertEqual(second_source, "cache")
        self.assertEqual(len(calls), 1, "第二次读取应直接命中缓存")
        self.assertTrue((self.ohlc_dir / "000001.feather").exists())
        self.assertTrue((self.ohlc_meta / "000001.json").exists())
        pd.testing.assert_frame_equal(
            first.rename_axis("date"), second.rename_axis("date")
        )

    def test_extended_range_only_fetches_missing_gap(self) -> None:
        calls: list[tuple[date, date]] = []

        def fake_remote(symbol: str, start: date, end: date):
            calls.append((start, end))
            return ohlc_frame(str(start), str(end)), "stock_zh_a_hist"

        with self._patched(), patch(
            "backend.fetch.fetch_symbol_ohlc_remote", side_effect=fake_remote
        ):
            fetch_symbol_ohlc_frame("000001", date(2024, 1, 2), date(2024, 1, 5))
            frame, source = fetch_symbol_ohlc_frame(
                "000001", date(2024, 1, 2), date(2024, 1, 31)
            )
            cached = cache.load_cached_stock_ohlc("000001")
            ranges = cache.load_cached_stock_ohlc_ranges("000001")

        self.assertEqual(len(calls), 2, "第二次调用只应补拉缺失区间")
        self.assertEqual(source, "stock_zh_a_hist")
        self.assertEqual(str(frame.index[0].date()), "2024-01-02")
        self.assertEqual(str(frame.index[-1].date()), "2024-01-31")
        self.assertIn(pd.Timestamp("2024-01-05"), cached.index)
        self.assertIn(pd.Timestamp("2024-01-08"), cached.index)
        self.assertEqual(ranges, [(date(2024, 1, 2), date(2024, 1, 31))])

    def test_today_refresh_failure_keeps_historical_candles(self) -> None:
        """收盘后当日 K 线尚未发布时，不能连已缓存的历史 K 线一起丢掉。"""

        def fake_remote(symbol: str, start: date, end: date):
            if start == end == date(2024, 1, 8):
                raise RuntimeError("today bar not ready")
            return ohlc_frame(str(start), str(end)), "stock_zh_a_hist"

        after_close = datetime(2024, 1, 8, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with self._patched(), patch(
            "backend.fetch.fetch_symbol_ohlc_remote", side_effect=fake_remote
        ), patch("backend.fetch.get_china_now", return_value=after_close), patch(
            "backend.fetch.is_trading_day", return_value=True
        ):
            fetch_symbol_ohlc_frame("000001", date(2024, 1, 2), date(2024, 1, 5))
            frame, source = fetch_symbol_ohlc_frame(
                "000001", date(2024, 1, 2), date(2024, 1, 8)
            )

        self.assertEqual(source, "stock_zh_a_hist")
        self.assertEqual(len(frame), 4, "历史 K 线不应因当日拉取失败而丢失")
        self.assertEqual(str(frame.index[-1].date()), "2024-01-05")

    def test_close_fetch_keeps_ohlc_ranges_normalized(self) -> None:
        """收盘价请求顺带写入 OHLC 时，区间元数据也要像收盘价缓存一样合并。"""

        def fake_close_remote(symbol: str, start: date, end: date):
            frame = ohlc_frame(str(start), str(end))
            return frame["close"].rename("close"), frame, "stock_zh_a_hist"

        with self._patched(), patch(
            "backend.fetch.fetch_symbol_close_series_remote", side_effect=fake_close_remote
        ):
            fetch_symbol_close_series("000001", date(2024, 1, 2), date(2024, 1, 5))
            fetch_symbol_close_series("000001", date(2024, 1, 2), date(2024, 1, 12))
            ohlc_ranges = cache.load_cached_stock_ohlc_ranges("000001")

        self.assertEqual(
            ohlc_ranges,
            [(date(2024, 1, 2), date(2024, 1, 12))],
            "相邻区间应合并为一段，避免元数据无限增长",
        )

    def test_ohlc_cache_write_failure_does_not_abort_close_fetch(self) -> None:
        """K 线缓存不可写时，纯收盘价回测仍应成功（蜡烛只是附属数据）。"""

        def fake_close_remote(symbol: str, start: date, end: date):
            frame = ohlc_frame(str(start), str(end))
            return frame["close"].rename("close"), frame, "stock_zh_a_hist"

        with self._patched(), patch(
            "backend.fetch.fetch_symbol_close_series_remote", side_effect=fake_close_remote
        ), patch(
            "backend.fetch.save_cached_stock_ohlc", side_effect=OSError("read-only")
        ):
            series, source = fetch_symbol_close_series(
                "000001", date(2024, 1, 2), date(2024, 1, 5)
            )

        self.assertEqual(source, "stock_zh_a_hist")
        self.assertEqual(len(series), 4, "OHLC 写入失败不应丢掉已取回的收盘价")
        self.assertTrue((self.stock_dir / "000001.feather").exists())

    def test_missing_feather_with_meta_is_refetched(self) -> None:
        calls = []

        def fake_remote(symbol: str, start: date, end: date):
            calls.append((start, end))
            return ohlc_frame(str(start), str(end)), "stock_zh_a_hist"

        with self._patched(), patch(
            "backend.fetch.fetch_symbol_ohlc_remote", side_effect=fake_remote
        ):
            fetch_symbol_ohlc_frame("000001", date(2024, 1, 2), date(2024, 1, 5))
            (self.ohlc_dir / "000001.feather").unlink()
            frame, _source = fetch_symbol_ohlc_frame(
                "000001", date(2024, 1, 2), date(2024, 1, 5)
            )

        self.assertEqual(len(calls), 2, "feather 被删除后必须重新拉取，而不是静默缺数据")
        self.assertFalse(frame.empty)

    def test_corrupt_feather_is_refetched(self) -> None:
        calls = []

        def fake_remote(symbol: str, start: date, end: date):
            calls.append((start, end))
            return ohlc_frame(str(start), str(end)), "stock_zh_a_hist"

        with self._patched(), patch(
            "backend.fetch.fetch_symbol_ohlc_remote", side_effect=fake_remote
        ):
            fetch_symbol_ohlc_frame("000001", date(2024, 1, 2), date(2024, 1, 5))
            (self.ohlc_dir / "000001.feather").write_bytes(b"not a feather file")
            frame, _source = fetch_symbol_ohlc_frame(
                "000001", date(2024, 1, 2), date(2024, 1, 5)
            )

        self.assertEqual(len(calls), 2, "损坏的 feather 必须触发重拉")
        self.assertEqual(len(frame), 4)

    def test_close_fetch_populates_ohlc_without_second_request(self) -> None:
        close_calls = []

        def fake_close_remote(symbol: str, start: date, end: date):
            close_calls.append((start, end))
            frame = ohlc_frame(str(start), str(end))
            return frame["close"].rename("close"), frame, "stock_zh_a_hist"

        with self._patched(), patch(
            "backend.fetch.fetch_symbol_close_series_remote", side_effect=fake_close_remote
        ):
            fetch_symbol_close_series("000001", date(2024, 1, 2), date(2024, 1, 5))

            self.assertTrue(
                (self.ohlc_dir / "000001.feather").exists(),
                "收盘价请求应顺带写入 OHLC 缓存",
            )
            with patch(
                "backend.fetch.fetch_symbol_ohlc_remote",
                side_effect=AssertionError("不应再次请求远程 OHLC"),
            ):
                frame, source = fetch_symbol_ohlc_frame(
                    "000001", date(2024, 1, 2), date(2024, 1, 5)
                )

        self.assertEqual(len(close_calls), 1)
        self.assertEqual(source, "cache")
        self.assertEqual(len(frame), 4)

    def test_failed_write_keeps_previous_file_and_leaves_no_temp_file(self) -> None:
        first = ohlc_frame("2024-01-02", "2024-01-05")
        second = ohlc_frame("2024-01-02", "2024-01-10")
        with self._patched():
            cache.save_cached_stock_ohlc("000001", first)
            with patch.object(pd.DataFrame, "to_feather", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    cache.save_cached_stock_ohlc("000001", second)

            stored = pd.read_feather(self.ohlc_dir / "000001.feather")
        self.assertEqual(len(stored), len(first), "写入失败时旧缓存必须保持完整")
        leftovers = [p.name for p in self.ohlc_dir.iterdir() if p.name != "000001.feather"]
        self.assertEqual(leftovers, [], "失败后不应残留临时文件")


if __name__ == "__main__":
    unittest.main()
