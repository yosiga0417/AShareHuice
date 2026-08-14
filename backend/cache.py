from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
import json
from pathlib import Path
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from backend.models import FileSignature

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_ROOT = BASE_DIR / ".cache" / "akshare"
STOCK_CACHE_DIR = CACHE_ROOT / "stock_close"
STOCK_META_DIR = CACHE_ROOT / "stock_close_meta"
BENCHMARK_CACHE_DIR = CACHE_ROOT / "benchmark_close"
BENCHMARK_META_DIR = CACHE_ROOT / "benchmark_close_meta"

CACHE_RESOURCE_LOCKS: Dict[str, threading.Lock] = {}
CACHE_RESOURCE_LOCKS_GUARD = threading.Lock()
TODAY_QUOTE_CACHE: Dict[str, Tuple[pd.Series, Optional[str], float]] = {}
TODAY_QUOTE_CACHE_GUARD = threading.Lock()


def get_cache_resource_lock(namespace: str, key: str) -> threading.Lock:
    lock_key = f"{namespace}:{key}"
    with CACHE_RESOURCE_LOCKS_GUARD:
        lock = CACHE_RESOURCE_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            CACHE_RESOURCE_LOCKS[lock_key] = lock
        return lock


def _today_quote_cache_key(namespace: str, key: str, quote_date: date) -> str:
    return f"{namespace}:{key}:{quote_date.isoformat()}"


def load_today_quote_cache(
    namespace: str,
    key: str,
    quote_date: date,
) -> Optional[Tuple[pd.Series, Optional[str]]]:
    cache_key = _today_quote_cache_key(namespace, key, quote_date)
    now = time.monotonic()
    with TODAY_QUOTE_CACHE_GUARD:
        entry = TODAY_QUOTE_CACHE.get(cache_key)
        if entry is None:
            return None
        series, source, expires_at = entry
        if expires_at <= now:
            TODAY_QUOTE_CACHE.pop(cache_key, None)
            return None
        return series.copy(), source


def save_today_quote_cache(
    namespace: str,
    key: str,
    quote_date: date,
    series: pd.Series,
    *,
    ttl_seconds: int,
    source: Optional[str] = None,
) -> None:
    if ttl_seconds <= 0:
        return
    clean_series = normalize_price_series(series)
    if clean_series.empty:
        return
    cache_key = _today_quote_cache_key(namespace, key, quote_date)
    now = time.monotonic()
    expires_at = now + ttl_seconds
    with TODAY_QUOTE_CACHE_GUARD:
        expired_keys = [
            existing_key
            for existing_key, (_series, _source, existing_expires_at) in TODAY_QUOTE_CACHE.items()
            if existing_expires_at <= now
        ]
        for expired_key in expired_keys:
            TODAY_QUOTE_CACHE.pop(expired_key, None)
        TODAY_QUOTE_CACHE[cache_key] = (clean_series, source, expires_at)


def clear_today_quote_cache(namespace: str, key: str, quote_date: date) -> None:
    cache_key = _today_quote_cache_key(namespace, key, quote_date)
    with TODAY_QUOTE_CACHE_GUARD:
        TODAY_QUOTE_CACHE.pop(cache_key, None)


def ensure_stock_cache_dirs() -> None:
    STOCK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STOCK_META_DIR.mkdir(parents=True, exist_ok=True)


def ensure_benchmark_cache_dirs() -> None:
    BENCHMARK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BENCHMARK_META_DIR.mkdir(parents=True, exist_ok=True)


def stock_cache_file(symbol: str) -> Path:
    return STOCK_CACHE_DIR / f"{symbol}.feather"


def stock_cache_meta_file(symbol: str) -> Path:
    return STOCK_META_DIR / f"{symbol}.json"


def benchmark_cache_file(code: str) -> Path:
    return BENCHMARK_CACHE_DIR / f"{code}.feather"


def benchmark_cache_meta_file(code: str) -> Path:
    return BENCHMARK_META_DIR / f"{code}.json"


def file_signature(path: Path) -> FileSignature:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def normalize_price_series(series: pd.Series) -> pd.Series:
    if series is None or len(series) == 0:
        return pd.Series(dtype="float64", name="close")
    normalized = series.copy()
    normalized = pd.to_numeric(normalized, errors="coerce")
    normalized.index = pd.to_datetime(normalized.index, errors="coerce").normalize()
    normalized = normalized[~normalized.index.isna()]
    normalized = normalized.dropna()
    normalized = normalized[~normalized.index.duplicated(keep="last")].sort_index()
    normalized = normalized.astype(float)
    normalized.name = "close"
    return normalized


def merge_price_series(base: pd.Series, incoming: pd.Series, skip_normalize: bool = False) -> pd.Series:
    if base is None or len(base) == 0:
        return incoming if skip_normalize else normalize_price_series(incoming)
    if incoming is None or len(incoming) == 0:
        return base if skip_normalize else normalize_price_series(base)
    result = pd.concat([base, incoming])
    if skip_normalize:
        result = result[~result.index.duplicated(keep="last")].sort_index()
        result.name = "close"
        return result
    return normalize_price_series(result)


def load_cached_series(cache_path: Path) -> pd.Series:
    return _load_cached_series(str(cache_path), file_signature(cache_path))


@lru_cache(maxsize=1024)
def _load_cached_series(cache_path_str: str, signature: FileSignature) -> pd.Series:
    if signature is None:
        return pd.Series(dtype="float64", name="close")
    try:
        df = pd.read_feather(cache_path_str)
    except Exception:
        return pd.Series(dtype="float64", name="close")
    if df.empty:
        return pd.Series(dtype="float64", name="close")
    # Feather preserves dtypes: date is datetime64, close is float64
    valid_mask = df["date"].notna() & df["close"].notna()
    if not valid_mask.any():
        return pd.Series(dtype="float64", name="close")
    series = pd.Series(
        df.loc[valid_mask, "close"].to_numpy(dtype="float64", copy=False),
        index=pd.DatetimeIndex(df.loc[valid_mask, "date"]),
        name="close",
    )
    if series.index.has_duplicates:
        series = series[~series.index.duplicated(keep="last")]
    return series.sort_index()


def save_cached_series(cache_path: Path, series: pd.Series) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    clean_series = normalize_price_series(series)
    df = clean_series.rename("close").reset_index()
    df.columns = ["date", "close"]
    df.to_feather(cache_path)


def load_cached_stock_series(symbol: str) -> pd.Series:
    return load_cached_series(stock_cache_file(symbol))


def save_cached_stock_series(symbol: str, series: pd.Series) -> None:
    ensure_stock_cache_dirs()
    save_cached_series(stock_cache_file(symbol), series)


def load_cached_benchmark_series(code: str) -> pd.Series:
    return load_cached_series(benchmark_cache_file(code))


def save_cached_benchmark_series(code: str, series: pd.Series) -> None:
    ensure_benchmark_cache_dirs()
    save_cached_series(benchmark_cache_file(code), series)


def normalize_cached_ranges(ranges: Iterable[Tuple[date, date]]) -> List[Tuple[date, date]]:
    normalized: List[Tuple[date, date]] = []
    for start_date, end_date in sorted(ranges, key=lambda item: item[0]):
        if start_date > end_date:
            continue
        if not normalized or start_date > normalized[-1][1] + timedelta(days=1):
            normalized.append((start_date, end_date))
            continue
        last_start, last_end = normalized[-1]
        normalized[-1] = (last_start, max(last_end, end_date))
    return normalized


def load_cached_ranges(cache_path: Path, meta_path: Path) -> List[Tuple[date, date]]:
    ranges = _load_cached_ranges(
        str(meta_path),
        file_signature(cache_path),
        file_signature(meta_path),
    )
    return list(ranges)


@lru_cache(maxsize=1024)
def _load_cached_ranges(
    meta_path_str: str,
    cache_signature: FileSignature,
    meta_signature: FileSignature,
) -> Tuple[Tuple[date, date], ...]:
    if cache_signature is None or meta_signature is None:
        return ()
    meta_path = Path(meta_path_str)
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return ()
    ranges: List[Tuple[date, date]] = []
    for item in payload.get("ranges", []):
        if not isinstance(item, dict):
            continue
        try:
            start_date = pd.Timestamp(item["start"]).date()
            end_date = pd.Timestamp(item["end"]).date()
        except Exception:
            continue
        if start_date <= end_date:
            ranges.append((start_date, end_date))
    return tuple(normalize_cached_ranges(ranges))


def load_cached_fallback_flag(meta_path: Path) -> bool:
    try:
        if meta_path.exists():
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            return bool(payload.get("has_fallback", False))
    except Exception:
        pass
    return False


def save_cached_ranges(
    meta_path: Path,
    ranges: Iterable[Tuple[date, date]],
    has_fallback: bool = False,
) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ranges": [
            {"start": start_date.isoformat(), "end": end_date.isoformat()}
            for start_date, end_date in normalize_cached_ranges(ranges)
        ],
    }
    if has_fallback:
        payload["has_fallback"] = True
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_cached_stock_ranges(symbol: str) -> List[Tuple[date, date]]:
    return load_cached_ranges(stock_cache_file(symbol), stock_cache_meta_file(symbol))


def save_cached_stock_ranges(symbol: str, ranges: Iterable[Tuple[date, date]], has_fallback: bool = False) -> None:
    ensure_stock_cache_dirs()
    save_cached_ranges(stock_cache_meta_file(symbol), ranges, has_fallback=has_fallback)


def load_cached_stock_fallback(symbol: str) -> bool:
    return load_cached_fallback_flag(stock_cache_meta_file(symbol))


def load_cached_benchmark_ranges(code: str) -> List[Tuple[date, date]]:
    return load_cached_ranges(benchmark_cache_file(code), benchmark_cache_meta_file(code))


def save_cached_benchmark_ranges(code: str, ranges: Iterable[Tuple[date, date]]) -> None:
    ensure_benchmark_cache_dirs()
    save_cached_ranges(benchmark_cache_meta_file(code), ranges)


def _read_legacy_csv(csv_path: Path) -> pd.Series:
    """Parse the pre-Feather per-symbol CSV format (date,close)."""
    df = pd.read_csv(csv_path)
    date_col = "date" if "date" in df.columns else ("日期" if "日期" in df.columns else None)
    close_col = "close" if "close" in df.columns else ("收盘" if "收盘" in df.columns else None)
    if date_col is None or close_col is None:
        raise ValueError(f"无法识别旧版 CSV 列: {csv_path.name}")
    series = pd.Series(
        pd.to_numeric(df[close_col], errors="coerce").to_numpy(dtype="float64"),
        index=pd.to_datetime(df[date_col], errors="coerce"),
    )
    return normalize_price_series(series)


def migrate_legacy_csv_cache() -> Dict[str, int]:
    """Convert/remove stale per-symbol CSV caches left by older versions.

    Older builds stored close prices as ``{symbol}.csv``; the current format is
    Feather. When both exist, the Feather data wins and the CSV is deleted.
    When only a CSV exists, it is converted to Feather (best effort) so no data
    is lost. Orphan metadata files without a matching data file are removed.

    Returns a summary dict of counters (converted / removed / skipped / meta_removed).
    """
    summary: Dict[str, int] = {"converted": 0, "removed": 0, "skipped": 0, "meta_removed": 0}
    namespaces = (
        (STOCK_CACHE_DIR, stock_cache_file, STOCK_META_DIR, save_cached_stock_series, save_cached_stock_ranges),
        (BENCHMARK_CACHE_DIR, benchmark_cache_file, BENCHMARK_META_DIR, save_cached_benchmark_series, save_cached_benchmark_ranges),
    )
    for cache_dir, data_file_fn, meta_dir, save_series, save_ranges in namespaces:
        # Clean orphan metadata first, even when the data directory itself is
        # missing (a data file cannot exist there, so every metadata file is
        # orphaned by definition).
        if meta_dir.exists():
            for meta_path in meta_dir.glob("*.json"):
                symbol = meta_path.stem
                if not data_file_fn(symbol).exists():
                    meta_path.unlink()
                    summary["meta_removed"] += 1
        if not cache_dir.exists():
            continue
        for csv_path in cache_dir.glob("*.csv"):
            symbol = csv_path.stem
            try:
                feather_path = data_file_fn(symbol)
                if feather_path.exists() and len(load_cached_series(feather_path)) > 0:
                    # A readable Feather wins; the legacy CSV is redundant.
                    csv_path.unlink()
                    summary["removed"] += 1
                    continue
                series = _read_legacy_csv(csv_path)
                if len(series) == 0:
                    csv_path.unlink()
                    summary["removed"] += 1
                    continue
                # Either no Feather exists or it is corrupt/unreadable: convert
                # the legacy CSV (overwriting a corrupt Feather) so the only
                # usable data is not thrown away.
                save_series(symbol, series)
                save_ranges(symbol, [(series.index.min().date(), series.index.max().date())])
                csv_path.unlink()
                summary["converted"] += 1
            except Exception:
                summary["skipped"] += 1
    return summary


def compute_missing_ranges(
    start_date: date,
    end_date: date,
    cached_ranges: Iterable[Tuple[date, date]],
) -> List[Tuple[date, date]]:
    if start_date > end_date:
        return []
    missing: List[Tuple[date, date]] = []
    cursor = start_date
    for cached_start, cached_end in normalize_cached_ranges(cached_ranges):
        if cached_end < cursor:
            continue
        if cached_start > end_date:
            break
        if cached_start > cursor:
            gap_end = min(end_date, cached_start - timedelta(days=1))
            if cursor <= gap_end:
                missing.append((cursor, gap_end))
        cursor = max(cursor, cached_end + timedelta(days=1))
        if cursor > end_date:
            break
    if cursor <= end_date:
        missing.append((cursor, end_date))
    return missing


def expand_fetch_range(
    gap_start: date,
    gap_end: date,
    cached_ranges: Iterable[Tuple[date, date]],
) -> Tuple[date, date]:
    fetch_start = gap_start
    fetch_end = gap_end
    previous_end: Optional[date] = None
    next_start: Optional[date] = None
    for cached_start, cached_end in normalize_cached_ranges(cached_ranges):
        if cached_end < gap_start:
            previous_end = cached_end
            continue
        if cached_start > gap_end:
            next_start = cached_start
            break
    if previous_end is not None:
        fetch_start = min(fetch_start, previous_end)
    if next_start is not None:
        fetch_end = max(fetch_end, next_start)
    return fetch_start, fetch_end


def slice_price_series(series: pd.Series, start_date: date, end_date: date) -> pd.Series:
    if series is None or len(series) == 0:
        return pd.Series(dtype="float64", name="close")
    mask = (series.index >= pd.Timestamp(start_date)) & (series.index <= pd.Timestamp(end_date))
    out = series.loc[mask]
    if out.empty:
        return pd.Series(dtype="float64", name="close")
    out = out.dropna()
    out.name = "close"
    if out.index.is_monotonic_increasing and not out.index.has_duplicates:
        return out.astype(float, copy=False)
    return normalize_price_series(out)
