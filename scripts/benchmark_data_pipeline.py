"""Offline, repeatable cache benchmark; never modifies production cache.

Run from the repository root: python3 scripts/benchmark_data_pipeline.py --output /tmp/before.json
Compare the JSON outputs from the same machine/cache snapshot. Cold means Python
LRU cleared, not OS page-cache cleared. Remote responses are deterministic fixtures.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from backend import cache, fetch


def timed(fn, repeats=5):
    samples = []
    for _ in range(repeats):
        start = perf_counter()
        fn()
        samples.append(perf_counter() - start)
    return round(statistics.median(samples) * 1000, 3)


def clear_lru():
    cache._load_cached_series.cache_clear()
    cache._load_cached_ohlc_frame.cache_clear()
    cache._load_cached_ranges.cache_clear()


@contextmanager
def isolated(root):
    with patch.multiple(cache, **{
        name: root / directory for name, directory in (
            ('STOCK_CACHE_DIR', 'stock_close'), ('STOCK_META_DIR', 'stock_close_meta'),
            ('STOCK_OHLC_CACHE_DIR', 'stock_ohlc'), ('STOCK_OHLC_META_DIR', 'stock_ohlc_meta'),
            ('BENCHMARK_CACHE_DIR', 'benchmark_close'), ('BENCHMARK_META_DIR', 'benchmark_close_meta'),
        )
    }):
        clear_lru()
        yield
        clear_lru()


def fixture(start, end):
    index = pd.bdate_range(start, end)
    close = 10 + np.arange(len(index), dtype=float) / 100
    return pd.DataFrame(dict(open=close - .1, high=close + .2,
                             low=close - .2, close=close), index=index)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    stock_paths = sorted(cache.STOCK_CACHE_DIR.glob('*.feather'))
    ohlc_paths = sorted(cache.STOCK_OHLC_CACHE_DIR.glob('*.feather'))
    results = {'stock_files': len(stock_paths), 'ohlc_files': len(ohlc_paths),
               'timing': 'median of 5 runs; milliseconds; offline; warm OS cache'}
    start, end = date(2024, 1, 1), date(2025, 12, 31)

    def read_stocks():
        for path in stock_paths:
            cache.slice_price_series(cache.load_cached_series(path), start, end)

    def read_candles():
        for path in ohlc_paths:
            cache.slice_ohlc_frame(cache.load_cached_ohlc_frame(path), start, end)

    for label, fn in [('stock', read_stocks), ('ohlc', read_candles)]:
        def cold():
            clear_lru()
            fn()
        results[label + '_cold_ms'] = timed(cold)
        fn()
        results[label + '_warm_ms'] = timed(fn)

    digest = hashlib.sha256()
    for path in ohlc_paths:
        frame = cache.slice_ohlc_frame(cache.load_cached_ohlc_frame(path), start, end)
        digest.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
    results['ohlc_digest'] = digest.hexdigest()
    # Rewrite identical real frames under an isolated root to measure actual
    # atomic writes (including fsync), and sum physical Feather payload bytes.
    frames = [(path.parent.name, path.name, pd.read_feather(path))
              for path in stock_paths + ohlc_paths]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        def rewrite():
            for directory, name, frame in frames:
                cache._atomic_write_feather(root / directory / name, frame)
        results['rewrite_ms'] = timed(rewrite, repeats=3)
        results['rewritten_bytes'] = sum(p.stat().st_size for p in root.rglob('*.feather'))

    def fragmented():
        with tempfile.TemporaryDirectory() as tmp, isolated(Path(tmp)):
            for left, right in [('2024-01-08', '2024-01-12'), ('2024-01-22', '2024-01-26')]:
                frame = fixture(left, right)
                cache.save_cached_stock_series('000001', cache.merge_price_series(
                    cache.load_cached_stock_series('000001'), frame['close']))
                cache.save_cached_stock_ranges('000001', cache.load_cached_stock_ranges('000001') + [
                    (date.fromisoformat(left), date.fromisoformat(right))])
            calls = []
            def remote(symbol, left, right):
                calls.append((left, right))
                frame = fixture(left, right)
                return frame['close'].rename('close'), frame, 'stock_zh_a_hist'
            with patch.object(fetch, 'fetch_symbol_close_series_remote', side_effect=remote), patch.object(
                cache, '_atomic_write_feather', wraps=cache._atomic_write_feather
            ) as writes:
                fetch.fetch_symbol_close_series('000001', date(2024, 1, 1), date(2024, 1, 31))
                results['fragmented_remote_calls'] = len(calls)
                results['fragmented_feather_writes'] = writes.call_count
    results['fragmented_ms'] = timed(fragmented)

    with tempfile.TemporaryDirectory() as tmp, isolated(Path(tmp)):
        calls = []
        def remote(symbol, left, right):
            calls.append((left, right))
            frame = fixture(left, right)
            return frame['close'].rename('close'), frame, 'stock_zh_a_hist'
        with patch.object(fetch, 'fetch_symbol_close_series_remote', side_effect=remote):
            fetch.fetch_symbol_ohlc_frame('000001', date(2024, 1, 2), date(2024, 1, 5))
            fetch.fetch_symbol_close_series('000001', date(2024, 1, 2), date(2024, 1, 5))
        results['ohlc_then_close_remote_calls'] = len(calls)
    Path(args.output).write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
