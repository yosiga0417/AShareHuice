from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from backend import cache


class LegacyCacheMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.stock_dir = root / "stock_close"
        self.stock_meta = root / "stock_close_meta"
        self.bench_dir = root / "benchmark_close"
        self.bench_meta = root / "benchmark_close_meta"
        for d in (self.stock_dir, self.stock_meta, self.bench_dir, self.bench_meta):
            d.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patched(self) -> patch:
        return patch.multiple(
            cache,
            STOCK_CACHE_DIR=self.stock_dir,
            STOCK_META_DIR=self.stock_meta,
            BENCHMARK_CACHE_DIR=self.bench_dir,
            BENCHMARK_META_DIR=self.bench_meta,
        )

    def test_csv_with_valid_feather_is_removed(self) -> None:
        with self._patched():
            cache.save_cached_stock_series(
                "000001",
                pd.Series([10.0, 11.0], index=pd.to_datetime(["2024-01-02", "2024-01-03"])),
            )
        (self.stock_dir / "000001.csv").write_text("date,close\n2024-01-02,10.5\n", encoding="utf-8")
        with self._patched():
            summary = cache.migrate_legacy_csv_cache()
        self.assertFalse((self.stock_dir / "000001.csv").exists())
        self.assertTrue((self.stock_dir / "000001.feather").exists())
        self.assertEqual(summary["removed"], 1)

    def test_corrupt_feather_with_csv_is_converted_not_deleted(self) -> None:
        # Regression: an unreadable Feather must NOT win over the legacy CSV.
        # The CSV is converted to Feather (overwriting the corrupt file) so the
        # only usable data is preserved instead of being deleted.
        (self.stock_dir / "000001.csv").write_text(
            "date,close\n2024-01-02,10.5\n2024-01-03,11.0\n", encoding="utf-8"
        )
        (self.stock_dir / "000001.feather").write_bytes(b"placeholder")
        with self._patched():
            summary = cache.migrate_legacy_csv_cache()
            self.assertFalse((self.stock_dir / "000001.csv").exists())
            series = cache.load_cached_stock_series("000001")
        self.assertEqual(len(series), 2)
        self.assertAlmostEqual(series.iloc[-1], 11.0)
        self.assertEqual(summary["converted"], 1)

    def test_csv_only_is_converted_to_feather(self) -> None:
        (self.stock_dir / "000002.csv").write_text(
            "date,close\n2024-01-02,10.5\n2024-01-03,11.0\n", encoding="utf-8"
        )
        with self._patched():
            summary = cache.migrate_legacy_csv_cache()
            self.assertFalse((self.stock_dir / "000002.csv").exists())
            series = cache.load_cached_stock_series("000002")
            self.assertEqual(len(series), 2)
            self.assertAlmostEqual(series.iloc[-1], 11.0)
            ranges = cache.load_cached_stock_ranges("000002")
        self.assertEqual(ranges[0][0].isoformat(), "2024-01-02")
        self.assertEqual(ranges[0][1].isoformat(), "2024-01-03")
        self.assertEqual(summary["converted"], 1)

    def test_unparseable_csv_is_removed_and_skipped(self) -> None:
        # Corrupt CSV has no recognizable columns -> counted as skipped, file kept.
        (self.stock_dir / "000004.csv").write_text("garbage,data\nx,y\n", encoding="utf-8")
        with self._patched():
            summary = cache.migrate_legacy_csv_cache()
        self.assertEqual(summary["skipped"], 1)
        self.assertTrue((self.stock_dir / "000004.csv").exists())

    def test_orphan_meta_removed(self) -> None:
        (self.stock_meta / "000003.json").write_text(json.dumps({"ranges": []}), encoding="utf-8")
        with self._patched():
            summary = cache.migrate_legacy_csv_cache()
        self.assertFalse((self.stock_meta / "000003.json").exists())
        self.assertEqual(summary["meta_removed"], 1)

    def test_orphan_meta_removed_even_when_data_dir_missing(self) -> None:
        # Regression: when the data directory itself does not exist the
        # migration used to skip straight past the orphan-metadata cleanup.
        (self.stock_meta / "000005.json").write_text(json.dumps({"ranges": []}), encoding="utf-8")
        with patch.multiple(
            cache,
            STOCK_CACHE_DIR=self.stock_dir / "missing",
            STOCK_META_DIR=self.stock_meta,
            BENCHMARK_CACHE_DIR=self.bench_dir / "missing",
            BENCHMARK_META_DIR=self.bench_meta,
        ):
            summary = cache.migrate_legacy_csv_cache()
        self.assertFalse((self.stock_meta / "000005.json").exists())
        self.assertEqual(summary["meta_removed"], 1)

    def test_benchmark_csv_converted(self) -> None:
        (self.bench_dir / "000300.csv").write_text("date,close\n2024-01-02,3000.5\n", encoding="utf-8")
        with self._patched():
            summary = cache.migrate_legacy_csv_cache()
            self.assertFalse((self.bench_dir / "000300.csv").exists())
            series = cache.load_cached_benchmark_series("000300")
            self.assertEqual(len(series), 1)
            self.assertAlmostEqual(series.iloc[0], 3000.5)
        self.assertEqual(summary["converted"], 1)

    def test_missing_cache_dir_is_noop(self) -> None:
        empty = Path(tempfile.mkdtemp())
        with patch.multiple(
            cache,
            STOCK_CACHE_DIR=empty / "nope",
            STOCK_META_DIR=empty / "nope_meta",
            BENCHMARK_CACHE_DIR=empty / "nope_bm",
            BENCHMARK_META_DIR=empty / "nope_bm_meta",
        ):
            summary = cache.migrate_legacy_csv_cache()
        self.assertEqual(summary, {"converted": 0, "removed": 0, "skipped": 0, "meta_removed": 0})


if __name__ == "__main__":
    unittest.main()
