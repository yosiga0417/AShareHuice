from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dt_time, timedelta
import os
import threading
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import akshare as ak
import numpy as np
import pandas as pd
from fastapi import HTTPException

from backend.cache import (
    clear_today_ohlc_cache,
    clear_today_quote_cache,
    compute_missing_ranges,
    expand_fetch_range,
    get_cache_resource_lock,
    load_cached_benchmark_series,
    load_cached_benchmark_ranges,
    load_cached_stock_fallback,
    load_cached_stock_ohlc,
    load_cached_stock_ohlc_ranges,
    load_cached_stock_ranges,
    load_cached_stock_series,
    load_today_ohlc_cache,
    load_today_quote_cache,
    merge_ohlc_frames,
    merge_price_series,
    normalize_cached_ranges,
    normalize_ohlc_frame,
    save_cached_benchmark_series,
    save_cached_benchmark_ranges,
    save_cached_stock_ohlc,
    save_cached_stock_ohlc_ranges,
    save_cached_stock_ranges,
    save_cached_stock_series,
    save_today_ohlc_cache,
    save_today_quote_cache,
    slice_ohlc_frame,
    slice_price_series,
)
from backend.config import read_positive_int_env
DEFAULT_FETCH_WORKERS = min(12, max(4, (os.cpu_count() or 4) * 2))

# AKShare prints tqdm progress bars to stderr that spam logs. Force-disable them
# by patching tqdm's __init__: akshare uses tqdm both directly and via its own
# get_tqdm() wrapper, and (as of tqdm 4.67) there is no env var to disable it.
def _silence_tqdm_progress_bars() -> None:
    if getattr(_silence_tqdm_progress_bars, "_applied", False):
        return
    try:
        import tqdm as _tqdm

        _orig_init = _tqdm.tqdm.__init__

        def _disabled_init(self, iterable=None, *args, **kwargs):
            kwargs["disable"] = True
            _orig_init(self, iterable, *args, **kwargs)

        _tqdm.tqdm.__init__ = _disabled_init
        _silence_tqdm_progress_bars._applied = True
    except Exception:  # pragma: no cover - purely cosmetic, never fatal
        pass


_silence_tqdm_progress_bars()

from backend.models import DEFAULT_BENCHMARK_CODES, StageProgressCallback
FETCH_WORKER_LIMIT = read_positive_int_env("BACKTEST_FETCH_WORKERS", DEFAULT_FETCH_WORKERS)
# Thread pools are per backtest task. With BACKTEST_MAX_CONCURRENT tasks this
# used to multiply into N x workers concurrent AKShare calls; cap the actual
# outbound requests globally while keeping per-task progress/threading intact.
FETCH_REMOTE_SEMAPHORE = threading.BoundedSemaphore(FETCH_WORKER_LIMIT)
INTRADAY_QUOTE_TTL_SECONDS = read_positive_int_env("BACKTEST_INTRADAY_QUOTE_TTL_SECONDS", 120)
AFTER_CLOSE_QUOTE_TTL_SECONDS = read_positive_int_env("BACKTEST_AFTER_CLOSE_QUOTE_TTL_SECONDS", 900)
CHINA_TZ = ZoneInfo("Asia/Shanghai")
MARKET_OPEN_TIME = dt_time(9, 30)
MARKET_STABLE_CLOSE_TIME = dt_time(15, 5)

# A-share trading calendar, lazily fetched from AKShare and TTL-cached.
# Used to avoid firing per-symbol "today" quote requests on exchange holidays
# (weekday-only heuristics would otherwise treat 春节/国庆 etc. as trading days).
TRADE_CALENDAR_STATE: Dict[str, object] = {"fetched_at": 0.0, "days": None, "loading": False}
TRADE_CALENDAR_LOCK = threading.Lock()
# Condition over the same lock so threads that arrive while a fetch is in
# flight wait for its result instead of racing ahead to the weekday fallback.
TRADE_CALENDAR_CONDITION = threading.Condition(TRADE_CALENDAR_LOCK)
TRADE_CALENDAR_TTL_SECONDS = 6 * 3600
# Safety net for threads waiting on a concurrent calendar fetch: if the
# fetching thread hangs on the network call and never notifies, waiting
# threads give up after this long and fall back to the weekday heuristic
# instead of blocking the whole symbol fetch forever.
TRADE_CALENDAR_WAIT_TIMEOUT_SECONDS = 30.0


def get_china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def load_trade_calendar() -> Optional[Set[date]]:
    """Return the set of A-share trading days (lazily fetched, TTL-cached).

    Returns None when the calendar cannot be fetched; callers then fall back to
    the weekday-only heuristic. While another thread is mid-fetch (e.g. the
    first load racing many worker threads), callers block on the condition
    variable and use the fetch result instead of falling back prematurely —
    otherwise most threads would treat an exchange holiday as a trading day
    and fire pointless "today" quote requests.
    """
    now = time.monotonic()
    with TRADE_CALENDAR_CONDITION:
        state = TRADE_CALENDAR_STATE
        # Fresh within TTL — either a successful fetch (days cached) or a
        # recent failed attempt (cooldown, fall back to the weekday heuristic
        # instead of hammering AKShare on every symbol). fetched_at == 0.0
        # means no attempt has been made yet, so always fetch then (a bare
        # monotonic clock can be < TTL right after boot).
        if state["fetched_at"] and now - float(state["fetched_at"]) < TRADE_CALENDAR_TTL_SECONDS:
            return state["days"]
        if state["loading"]:
            # Another thread is fetching (likely the first load): wait for it
            # to finish, then use its result (success or failure) instead of
            # immediately falling back to the weekday-only heuristic. The
            # timeout is a safety net if the fetching thread hangs on the
            # network call and never notifies.
            TRADE_CALENDAR_CONDITION.wait(timeout=TRADE_CALENDAR_WAIT_TIMEOUT_SECONDS)
            return state["days"]
        state["loading"] = True
    try:
        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            raise ValueError("交易日历为空")
        col = "trade_date" if "trade_date" in df.columns else str(df.columns[0])
        days = set(pd.to_datetime(df[col], errors="coerce").dropna().dt.date)
        if not days:
            raise ValueError("交易日历解析为空")
        with TRADE_CALENDAR_CONDITION:
            TRADE_CALENDAR_STATE["days"] = days
            TRADE_CALENDAR_STATE["fetched_at"] = now
            TRADE_CALENDAR_CONDITION.notify_all()
        return days
    except Exception:
        # Cooldown before retrying, even on failure, so a flaky calendar source
        # does not turn every symbol fetch into a calendar fetch.
        with TRADE_CALENDAR_CONDITION:
            TRADE_CALENDAR_STATE["fetched_at"] = now
            TRADE_CALENDAR_CONDITION.notify_all()
        return None
    finally:
        with TRADE_CALENDAR_CONDITION:
            TRADE_CALENDAR_STATE["loading"] = False
            TRADE_CALENDAR_CONDITION.notify_all()


def is_trading_day(day: date) -> bool:
    if not is_trading_weekday(day):
        return False
    calendar = load_trade_calendar()
    if calendar is None:
        return True  # calendar unavailable: fall back to weekday heuristic
    return day in calendar


def is_trading_weekday(day: date) -> bool:
    return day.weekday() < 5


def should_refresh_today_quote(now: datetime) -> bool:
    return is_trading_day(now.date()) and now.time() >= MARKET_OPEN_TIME


def should_persist_today_quote(now: datetime) -> bool:
    return is_trading_day(now.date()) and now.time() >= MARKET_STABLE_CLOSE_TIME


def today_quote_ttl_seconds(now: datetime) -> int:
    if should_persist_today_quote(now):
        return AFTER_CLOSE_QUOTE_TTL_SECONDS
    return INTRADAY_QUOTE_TTL_SECONDS


def date_is_cached(day: date, cached_ranges: List[Tuple[date, date]]) -> bool:
    return any(start_date <= day <= end_date for start_date, end_date in cached_ranges)


def resolve_fetch_worker_count(total_items: int) -> int:
    if total_items <= 0:
        return 1
    return min(total_items, FETCH_WORKER_LIMIT)


def stock_to_tx_symbol(code: str) -> str:
    if code.startswith(("6", "5", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def extract_date_close_series(hist_df: pd.DataFrame) -> pd.Series:
    candidates = [
        ("日期", "收盘"),
        ("date", "close"),
        ("date", "收盘"),
        ("日期", "close"),
    ]
    selected = None
    for date_col, close_col in candidates:
        if date_col in hist_df.columns and close_col in hist_df.columns:
            selected = (date_col, close_col)
            break
    if selected is None:
        raise ValueError("行情字段不完整，无法识别日期和收盘价列")
    date_col, close_col = selected
    series_df = hist_df.loc[:, [date_col, close_col]].copy()
    series_df[date_col] = pd.to_datetime(series_df[date_col], errors="coerce")
    series_df[close_col] = pd.to_numeric(series_df[close_col], errors="coerce")
    series_df = series_df.dropna(subset=[date_col, close_col])
    series_df = series_df.drop_duplicates(subset=[date_col], keep="last")
    if series_df.empty:
        raise ValueError("区间内没有有效收盘价")
    out = series_df.set_index(date_col)[close_col].sort_index()
    out.index = pd.to_datetime(out.index)
    return out


def extract_date_ohlc_frame(hist_df: pd.DataFrame) -> pd.DataFrame:
    """Extract date/open/high/low/close from an AKShare daily history frame.

    ``stock_zh_a_hist`` uses Chinese column names (日期/开盘/收盘/最高/最低)
    while the Tencent fallback uses English ones (date/open/close/high/low).
    Missing open/high/low columns degrade to close-only candles instead of
    failing the whole backtest.
    """
    date_col = next((c for c in ("日期", "date") if c in hist_df.columns), None)
    close_col = next((c for c in ("收盘", "close") if c in hist_df.columns), None)
    if date_col is None or close_col is None:
        raise ValueError("行情字段不完整，无法识别日期和收盘价列")
    open_col = next((c for c in ("开盘", "open") if c in hist_df.columns), None)
    high_col = next((c for c in ("最高", "最高价", "high") if c in hist_df.columns), None)
    low_col = next((c for c in ("最低", "最低价", "low") if c in hist_df.columns), None)

    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(hist_df[date_col], errors="coerce"),
            "open": pd.to_numeric(hist_df[open_col], errors="coerce") if open_col else pd.NA,
            "high": pd.to_numeric(hist_df[high_col], errors="coerce") if high_col else pd.NA,
            "low": pd.to_numeric(hist_df[low_col], errors="coerce") if low_col else pd.NA,
            "close": pd.to_numeric(hist_df[close_col], errors="coerce"),
        }
    )
    frame = frame.dropna(subset=["date", "close"])
    if frame.empty:
        raise ValueError("区间内没有有效收盘价")
    normalized = normalize_ohlc_frame(frame)
    if normalized.empty:
        raise ValueError("区间内没有有效的开高低收数据")
    return normalized


def try_fetch_hist(symbol: str, beg: str, end: str, max_retry: int = 2) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for attempt in range(max_retry):
        try:
            with FETCH_REMOTE_SEMAPHORE:
                hist = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=beg,
                    end_date=end,
                    adjust="qfq",
                )
            if hist is None or hist.empty:
                raise ValueError("返回数据为空")
            return hist
        except Exception as exc:
            last_error = exc
            if attempt < max_retry - 1:
                time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"stock_zh_a_hist failed: {last_error}")


def try_fetch_hist_tx(symbol: str, beg: str, end: str, max_retry: int = 2) -> pd.DataFrame:
    tx_symbol = stock_to_tx_symbol(symbol)
    last_error: Optional[Exception] = None
    for attempt in range(max_retry):
        try:
            with FETCH_REMOTE_SEMAPHORE:
                hist = ak.stock_zh_a_hist_tx(
                    symbol=tx_symbol,
                    start_date=beg,
                    end_date=end,
                    adjust="qfq",
                )
            if hist is None or hist.empty:
                raise ValueError("返回数据为空")
            return hist
        except Exception as exc:
            last_error = exc
            if attempt < max_retry - 1:
                time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"stock_zh_a_hist_tx({tx_symbol}) failed: {last_error}")


def fetch_symbol_close_series_remote(
    symbol: str, start_date: date, end_date: date
) -> Tuple[pd.Series, pd.DataFrame, str]:
    """Fetch one symbol's daily history once and return both close and OHLC.

    The primary AKShare endpoint already returns 开盘/最高/最低/收盘, so the
    candlestick data comes for free with the close-price request.
    """
    beg = start_date.strftime("%Y%m%d")
    end = end_date.strftime("%Y%m%d")
    errors: List[str] = []
    try:
        hist = try_fetch_hist(symbol, beg, end, max_retry=2)
        ohlc = extract_date_ohlc_frame(hist)
        return ohlc["close"].rename("close"), ohlc, "stock_zh_a_hist"
    except Exception as exc:
        errors.append(str(exc))
    try:
        hist_tx = try_fetch_hist_tx(symbol, beg, end, max_retry=2)
        ohlc = extract_date_ohlc_frame(hist_tx)
        return ohlc["close"].rename("close"), ohlc, "stock_zh_a_hist_tx"
    except Exception as exc:
        errors.append(str(exc))
    raise RuntimeError("; ".join(errors))


def _store_cached_stock_ohlc_locked(
    symbol: str,
    frame: pd.DataFrame,
    range_start: date,
    range_end: date,
) -> None:
    """Persist an OHLC slice while the caller already owns the symbol lock."""
    _store_cached_stock_ohlc_slices_locked(symbol, [(frame, range_start, range_end)])


def _store_cached_stock_ohlc_slices_locked(
    symbol: str, slices: List[Tuple[pd.DataFrame, date, date]]
) -> None:
    slices = [(frame, start, end) for frame, start, end in slices if frame is not None and not frame.empty]
    if not slices:
        return
    cached_frame = load_cached_stock_ohlc(symbol)
    cached_ranges = load_cached_stock_ohlc_ranges(symbol) if not cached_frame.empty else []
    # Merge all missing slices and write the full file only once per symbol.
    merged = normalize_ohlc_frame(pd.concat([cached_frame] + [frame for frame, _, _ in slices]))
    save_cached_stock_ohlc(symbol, merged)
    save_cached_stock_ohlc_ranges(
        symbol, cached_ranges + [(start, end) for _, start, end in slices]
    )


def fetch_symbol_close_series(
    symbol: str, start_date: date, end_date: date
) -> Tuple[pd.Series, str]:
    request_start = pd.Timestamp(start_date).date()
    request_end = pd.Timestamp(end_date).date()
    if request_start > request_end:
        raise ValueError("开始日期不能晚于结束日期")
    now = get_china_now()
    today = now.date()
    includes_today = request_start <= today <= request_end and should_refresh_today_quote(now)
    historical_end = min(request_end, today - timedelta(days=1)) if request_end >= today else request_end
    with get_cache_resource_lock("stock", symbol):
        cached_series = load_cached_stock_series(symbol)
        cached_ranges = load_cached_stock_ranges(symbol)
        cached_fallback = load_cached_stock_fallback(symbol)
        if cached_series.empty:
            cached_ranges = []
        ohlc_slices: List[Tuple[pd.DataFrame, date, date]] = []
        fetched_sources: List[str] = []
        has_new_fallback = cached_fallback
        cache_dirty = False
        ranges_dirty = False
        if request_start <= historical_end:
            missing_ranges = compute_missing_ranges(request_start, historical_end, cached_ranges)
            for gap_start, gap_end in missing_ranges:
                fetch_start, fetch_end = expand_fetch_range(gap_start, gap_end, cached_ranges)
                # An OHLC-only client may already have fetched this interval.
                # Reuse its close column instead of a second network request.
                ohlc_ranges = load_cached_stock_ohlc_ranges(symbol)
                reusable = not compute_missing_ranges(fetch_start, fetch_end, ohlc_ranges)
                gap_ohlc = load_cached_stock_ohlc(symbol) if reusable else pd.DataFrame()
                if not gap_ohlc.empty:
                    gap_ohlc = slice_ohlc_frame(gap_ohlc, fetch_start, fetch_end)
                if not gap_ohlc.empty:
                    gap_series, source = gap_ohlc["close"], "cache"
                else:
                    gap_series, gap_ohlc, source = fetch_symbol_close_series_remote(
                        symbol, fetch_start, fetch_end
                    )
                    ohlc_slices.append((gap_ohlc, gap_start, gap_end))
                cached_series = merge_price_series(cached_series, gap_series, skip_normalize=True)
                cached_ranges = normalize_cached_ranges(cached_ranges + [(gap_start, gap_end)])
                fetched_sources.append(source)
                if source == "stock_zh_a_hist_tx":
                    has_new_fallback = True
                cache_dirty = True
                ranges_dirty = True
        refresh_error: Optional[Exception] = None
        result_series = cached_series
        should_fetch_today = includes_today and not date_is_cached(today, cached_ranges)
        if should_fetch_today:
            persist_today = should_persist_today_quote(now)
            cached_today = None
            cached_today_ohlc = None
            if not persist_today:
                cached_today = load_today_quote_cache("stock", symbol, today)
                cached_today_ohlc = load_today_ohlc_cache("stock", symbol, today)
            try:
                if cached_today is not None:
                    today_series, source = cached_today
                    today_ohlc = cached_today_ohlc[0] if cached_today_ohlc is not None else None
                else:
                    today_series, today_ohlc, source = fetch_symbol_close_series_remote(
                        symbol, today, today
                    )
                    if persist_today:
                        clear_today_quote_cache("stock", symbol, today)
                        clear_today_ohlc_cache("stock", symbol, today)
                    else:
                        save_today_quote_cache(
                            "stock",
                            symbol,
                            today,
                            today_series,
                            ttl_seconds=today_quote_ttl_seconds(now),
                            source=source,
                        )
                        save_today_ohlc_cache(
                            "stock",
                            symbol,
                            today,
                            today_ohlc,
                            ttl_seconds=today_quote_ttl_seconds(now),
                            source=source,
                        )
                # Some providers return the previous bar before today's daily
                # bar is published. Never mark today permanently cached then.
                today_series = slice_price_series(today_series, today, today)
                if today_series.empty:
                    raise RuntimeError("当天收盘价尚未发布")
                if source:
                    fetched_sources.append(source)
                if source == "stock_zh_a_hist_tx":
                    has_new_fallback = True
                if today_ohlc is None:
                    today_ohlc = normalize_ohlc_frame(pd.DataFrame({"close": today_series}))
                if persist_today:
                    cached_series = merge_price_series(cached_series, today_series, skip_normalize=True)
                    cached_ranges = normalize_cached_ranges(cached_ranges + [(today, today)])
                    ohlc_slices.append((today_ohlc, today, today))
                    result_series = cached_series
                    cache_dirty = True
                    ranges_dirty = True
                else:
                    result_series = merge_price_series(cached_series, today_series, skip_normalize=True)
            except Exception as exc:
                refresh_error = exc
        else:
            result_series = cached_series
        # Candles are auxiliary; an OHLC write failure must not abort the
        # valid close-price result. Each symbol is flushed just once.
        try:
            _store_cached_stock_ohlc_slices_locked(symbol, ohlc_slices)
        except Exception:
            pass
        if cache_dirty:
            save_cached_stock_series(symbol, cached_series)
        if ranges_dirty or has_new_fallback != cached_fallback:
            save_cached_stock_ranges(symbol, cached_ranges, has_fallback=has_new_fallback)
        result = slice_price_series(result_series, request_start, request_end)
        if result.empty:
            if refresh_error is not None:
                raise RuntimeError(str(refresh_error))
            raise RuntimeError("区间内没有可用收盘价")
        if "stock_zh_a_hist_tx" in fetched_sources:
            return result, "stock_zh_a_hist_tx"
        if "stock_zh_a_hist" in fetched_sources:
            return result, "stock_zh_a_hist"
        if has_new_fallback:
            return result, "stock_zh_a_hist_tx"
        return result, "cache"


def fetch_close_prices(
    symbols: List[str],
    start_date: date,
    end_date: date,
    progress_callback: Optional[StageProgressCallback] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    if not symbols:
        raise HTTPException(status_code=400, detail="未提供成分股代码")
    frames: Dict[str, pd.Series] = {}
    warnings: List[str] = []
    total_symbols = len(symbols)
    worker_count = resolve_fetch_worker_count(total_symbols)
    if progress_callback is not None:
        progress_callback(
            0, total_symbols,
            f"开始拉取 {total_symbols} 只成分股行情（并发 {worker_count}）…",
        )
    results: Dict[str, Tuple[pd.Series, str]] = {}
    errors: Dict[str, str] = {}
    completed = 0
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="stock-fetch")
    future_to_symbol = {
        executor.submit(fetch_symbol_close_series, symbol, start_date, end_date): symbol
        for symbol in symbols
    }
    try:
        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                results[symbol] = future.result()
            except Exception as exc:
                errors[symbol] = str(exc)
            completed += 1
            if progress_callback is not None:
                progress_callback(
                    completed, total_symbols,
                    f"已处理成分股 {symbol}（{completed}/{total_symbols}）",
                )
    except BaseException:
        # Running network calls cannot be force-stopped, but queued calls do not
        # need to start once the enclosing backtest has been cancelled.
        for future in future_to_symbol:
            future.cancel()
        executor.shutdown(wait=True)
        raise
    else:
        executor.shutdown(wait=True)
    for symbol in symbols:
        if symbol in errors:
            warnings.append(f"{symbol} 行情获取失败: {errors[symbol]}")
            continue
        series, source = results[symbol]
        frames[symbol] = series
        if source == "stock_zh_a_hist_tx":
            warnings.append(f"{symbol} 使用兜底数据源 {source}")
    if not frames:
        raise HTTPException(status_code=400, detail="无法获取任何成分股行情，请检查代码与网络连接")
    close_df = pd.DataFrame(frames).sort_index()
    close_df = close_df.loc[
        (close_df.index >= pd.Timestamp(start_date)) & (close_df.index <= pd.Timestamp(end_date))
    ]
    close_df = close_df[~close_df.index.duplicated(keep="last")]
    close_df = close_df.sort_index()
    if close_df.empty:
        raise HTTPException(status_code=400, detail="行情数据为空，无法回测")
    return close_df, warnings


def fetch_symbol_ohlc_remote(
    symbol: str, start_date: date, end_date: date
) -> Tuple[pd.DataFrame, str]:
    """Return daily OHLC for one symbol (primary source, Tencent fallback)."""
    _series, frame, source = fetch_symbol_close_series_remote(symbol, start_date, end_date)
    return frame, source


def fetch_symbol_ohlc_frame(
    symbol: str, start_date: date, end_date: date
) -> Tuple[pd.DataFrame, str]:
    """Load one symbol's OHLC frame from cache, fetching only missing ranges."""
    request_start = pd.Timestamp(start_date).date()
    request_end = pd.Timestamp(end_date).date()
    if request_start > request_end:
        raise ValueError("开始日期不能晚于结束日期")
    now = get_china_now()
    today = now.date()
    includes_today = request_start <= today <= request_end and should_refresh_today_quote(now)
    historical_end = min(request_end, today - timedelta(days=1)) if request_end >= today else request_end
    with get_cache_resource_lock("stock", symbol):
        cached_frame = load_cached_stock_ohlc(symbol)
        cached_ranges = load_cached_stock_ohlc_ranges(symbol)
        if cached_ranges and cached_frame.empty:
            # The metadata claims a range is cached but the Feather file is
            # missing/unreadable; refetch instead of silently returning no candles.
            cached_ranges = []
        fetched_sources: List[str] = []
        cache_dirty = False
        ranges_dirty = False
        if request_start <= historical_end:
            missing_ranges = compute_missing_ranges(request_start, historical_end, cached_ranges)
            for gap_start, gap_end in missing_ranges:
                fetch_start, fetch_end = expand_fetch_range(gap_start, gap_end, cached_ranges)
                gap_frame, source = fetch_symbol_ohlc_remote(symbol, fetch_start, fetch_end)
                cached_frame = merge_ohlc_frames(cached_frame, gap_frame)
                cached_ranges = normalize_cached_ranges(cached_ranges + [(gap_start, gap_end)])
                fetched_sources.append(source)
                cache_dirty = True
                ranges_dirty = True
        result_frame = cached_frame
        should_fetch_today = includes_today and not date_is_cached(today, cached_ranges)
        if should_fetch_today:
            try:
                persist_today = should_persist_today_quote(now)
                cached_today = None if persist_today else load_today_ohlc_cache("stock", symbol, today)
                if cached_today is None:
                    today_frame, source = fetch_symbol_ohlc_remote(symbol, today, today)
                    if persist_today:
                        clear_today_ohlc_cache("stock", symbol, today)
                    else:
                        save_today_ohlc_cache(
                            "stock",
                            symbol,
                            today,
                            today_frame,
                            ttl_seconds=today_quote_ttl_seconds(now),
                            source=source,
                        )
                else:
                    today_frame, source = cached_today
                today_frame = slice_ohlc_frame(today_frame, today, today)
                if today_frame.empty:
                    raise RuntimeError("当天开高低收尚未发布")
                if source:
                    fetched_sources.append(source)
                if persist_today:
                    cached_frame = merge_ohlc_frames(cached_frame, today_frame)
                    cached_ranges = normalize_cached_ranges(cached_ranges + [(today, today)])
                    result_frame = cached_frame
                    cache_dirty = True
                    ranges_dirty = True
                else:
                    result_frame = merge_ohlc_frames(cached_frame, today_frame)
            except Exception:
                # Right after the close (or on a transient network error) the
                # daily bar for today may not be available yet. Mirror
                # fetch_symbol_close_series: keep the historical candles and
                # let the caller degrade gracefully instead of discarding a
                # symbol whose history was loaded successfully.
                result_frame = cached_frame
        if cache_dirty:
            save_cached_stock_ohlc(symbol, cached_frame)
        if ranges_dirty:
            save_cached_stock_ohlc_ranges(symbol, cached_ranges)
        result = slice_ohlc_frame(result_frame, request_start, request_end)
        if result.empty:
            raise RuntimeError("区间内没有可用的开高低收数据")
        if "stock_zh_a_hist_tx" in fetched_sources:
            return result, "stock_zh_a_hist_tx"
        if "stock_zh_a_hist" in fetched_sources:
            return result, "stock_zh_a_hist"
        return result, "cache"


def derive_ohlc_from_close(close_series: pd.Series) -> pd.DataFrame:
    """Fallback candles when a symbol has no open/high/low data.

    Uses the previous available close as the open and the close for both the
    high and low, so a resumption after a suspension still opens at the last
    traded price instead of collapsing to a flat doji. The engine then produces
    a valid (if coarse) candle for that symbol.
    """
    close = pd.to_numeric(close_series, errors="coerce")
    previous = close.ffill().shift(1)
    open_ = previous.where(previous.notna(), close)
    high = pd.concat([open_, close], axis=1).max(axis=1)
    low = pd.concat([open_, close], axis=1).min(axis=1)
    return normalize_ohlc_frame(
        pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})
    )


def fetch_ohlc_matrices(
    symbols: List[str],
    start_date: date,
    end_date: date,
    close_df: pd.DataFrame,
    progress_callback: Optional[StageProgressCallback] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, List[str]]:
    """Fetch per-symbol OHLC and align it to the backtest's close matrix.

    Returns ``(open_df, high_df, low_df, warnings)``. Symbols whose OHLC cannot
    be fetched fall back to candles derived from their close series, so the
    candlestick view always renders something sensible.
    """
    warnings: List[str] = []
    usable_columns = [code for code in close_df.columns if code in set(symbols)]
    if not usable_columns:
        empty = pd.DataFrame(index=close_df.index)
        return empty, empty, empty, warnings

    total_symbols = len(usable_columns)
    worker_count = resolve_fetch_worker_count(total_symbols)
    if progress_callback is not None:
        progress_callback(
            0, total_symbols,
            f"开始拉取 {total_symbols} 只成分股的开盘/最高/最低价（并发 {worker_count}）…",
        )
    results: Dict[str, pd.DataFrame] = {}
    errors: Dict[str, str] = {}
    completed = 0
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="stock-ohlc-fetch")
    future_to_symbol = {
        executor.submit(fetch_symbol_ohlc_frame, symbol, start_date, end_date): symbol
        for symbol in usable_columns
    }
    try:
        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                frame, _source = future.result()
                results[symbol] = frame
            except Exception as exc:
                errors[symbol] = str(exc)
            completed += 1
            if progress_callback is not None:
                progress_callback(
                    completed, total_symbols,
                    f"已处理成分股 {symbol} 开高低收（{completed}/{total_symbols}）",
                )
    except BaseException:
        for future in future_to_symbol:
            future.cancel()
        executor.shutdown(wait=True)
        raise
    else:
        executor.shutdown(wait=True)

    aligned: Dict[str, pd.DataFrame] = {}
    for symbol in usable_columns:
        close_series = pd.to_numeric(close_df[symbol], errors="coerce")
        frame = results.get(symbol)
        if frame is None or frame.empty:
            frame = derive_ohlc_from_close(close_series)
        else:
            frame = frame.reindex(close_df.index)
            # Front-adjusted (qfq) prices from different fetch dates can use a
            # different adjustment baseline. Rescale each day's OHLC by
            # official_close / ohlc_close so the candle's close is exactly the
            # close used by the NAV calculation, while the intraday open/high/
            # low shape (relative to that day's close) is preserved.
            effective_close = frame["close"].fillna(close_series)
            with np.errstate(divide="ignore", invalid="ignore"):
                factor = close_series / effective_close.replace(0.0, np.nan)
            factor = factor.where(np.isfinite(factor) & (factor > 0))
            for key in ("open", "high", "low"):
                frame[key] = pd.to_numeric(frame[key], errors="coerce") * factor
            frame["close"] = close_series
            frame = normalize_ohlc_frame(frame)
            frame = frame.reindex(close_df.index)
        aligned[symbol] = frame

    if errors:
        warnings.append(
            f"{len(errors)} 只成分股开高低收获取失败，K线已改用收盘价近似（{'、'.join(sorted(errors)[:5])}"
            + ("…" if len(errors) > 5 else "") + "）"
        )

    def column(key: str) -> pd.DataFrame:
        return pd.concat(
            {symbol: aligned[symbol][key] for symbol in usable_columns}, axis=1
        ).reindex(index=close_df.index, columns=close_df.columns)

    return column("open"), column("high"), column("low"), warnings


def fetch_benchmark_close_series_remote(
    code: str, start_date: date, end_date: date
) -> pd.Series:
    beg = start_date.strftime("%Y%m%d")
    end = end_date.strftime("%Y%m%d")
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            with FETCH_REMOTE_SEMAPHORE:
                df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=beg, end_date=end)
            if df is None or df.empty:
                raise ValueError("返回数据为空")
            return extract_date_close_series(df)
        except Exception as exc:
            last_error = exc
            if attempt < 1:
                time.sleep(0.35)
    prefix = "sh" if code.startswith(("0", "9")) else "sz"
    try:
        with FETCH_REMOTE_SEMAPHORE:
            df = ak.stock_zh_index_daily(symbol=f"{prefix}{code}")
        if df is None or df.empty:
            raise ValueError("返回数据为空")
        df = df.rename(columns={"date": "日期", "close": "收盘"})
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        mask = (df["日期"] >= pd.Timestamp(start_date)) & (df["日期"] <= pd.Timestamp(end_date))
        df = df.loc[mask]
        return extract_date_close_series(df)
    except Exception as exc:
        details = [str(item) for item in [last_error, exc] if item is not None]
        raise RuntimeError("; ".join(details) or f"基准 {code} 行情获取失败") from exc


def fetch_benchmark_nav(
    code: str, start_date: date, end_date: date
) -> Dict[str, object]:
    request_start = pd.Timestamp(start_date).date()
    request_end = pd.Timestamp(end_date).date()
    if request_start > request_end:
        raise ValueError("开始日期不能晚于结束日期")
    now = get_china_now()
    today = now.date()
    includes_today = request_start <= today <= request_end and should_refresh_today_quote(now)
    historical_end = min(request_end, today - timedelta(days=1)) if request_end >= today else request_end
    with get_cache_resource_lock("benchmark", code):
        cached_series = load_cached_benchmark_series(code)
        cached_ranges = load_cached_benchmark_ranges(code)
        if cached_series.empty:
            cached_ranges = []
        cache_dirty = False
        ranges_dirty = False
        if request_start <= historical_end:
            missing_ranges = compute_missing_ranges(request_start, historical_end, cached_ranges)
            for gap_start, gap_end in missing_ranges:
                fetch_start, fetch_end = expand_fetch_range(gap_start, gap_end, cached_ranges)
                gap_series = fetch_benchmark_close_series_remote(code, fetch_start, fetch_end)
                cached_series = merge_price_series(cached_series, gap_series, skip_normalize=True)
                cached_ranges = normalize_cached_ranges(cached_ranges + [(gap_start, gap_end)])
                cache_dirty = True
                ranges_dirty = True
        result_series = cached_series
        should_fetch_today = includes_today and not date_is_cached(today, cached_ranges)
        if should_fetch_today:
            persist_today = should_persist_today_quote(now)
            cached_today = None if persist_today else load_today_quote_cache("benchmark", code, today)
            if cached_today is None:
                today_series = fetch_benchmark_close_series_remote(code, today, today)
                if persist_today:
                    clear_today_quote_cache("benchmark", code, today)
                else:
                    save_today_quote_cache(
                        "benchmark",
                        code,
                        today,
                        today_series,
                        ttl_seconds=today_quote_ttl_seconds(now),
                    )
            else:
                today_series, _source = cached_today
            today_series = slice_price_series(today_series, today, today)
            if today_series.empty:
                raise RuntimeError("当天基准行情尚未发布")
            if persist_today:
                cached_series = merge_price_series(cached_series, today_series, skip_normalize=True)
                cached_ranges = normalize_cached_ranges(cached_ranges + [(today, today)])
                result_series = cached_series
                cache_dirty = True
                ranges_dirty = True
            else:
                result_series = merge_price_series(cached_series, today_series, skip_normalize=True)
        if cache_dirty:
            save_cached_benchmark_series(code, cached_series)
        if ranges_dirty:
            save_cached_benchmark_ranges(code, cached_ranges)
        result = slice_price_series(result_series, request_start, request_end)
        if result.empty:
            raise RuntimeError("区间内没有可用基准行情")
        return {
            "dates": [str(pd.Timestamp(idx).date()) for idx in result.index],
            "values": [float(round(v, 8)) for v in result.values],
        }


def fetch_benchmark_navs(
    codes: List[str],
    start_date: date,
    end_date: date,
    progress_callback: Optional[StageProgressCallback] = None,
) -> Tuple[Dict[str, Dict[str, object]], List[str]]:
    benchmark_nav: Dict[str, Dict[str, object]] = {}
    warnings: List[str] = []
    total_codes = len(codes)
    if total_codes <= 0:
        return benchmark_nav, warnings
    worker_count = resolve_fetch_worker_count(total_codes)
    if progress_callback is not None:
        progress_callback(0, total_codes, f"开始拉取 {total_codes} 个基准行情（并发 {worker_count}）…")
    results: Dict[str, Dict[str, object]] = {}
    errors: Dict[str, str] = {}
    completed = 0
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="benchmark-fetch")
    future_to_code = {
        executor.submit(fetch_benchmark_nav, code, start_date, end_date): code for code in codes
    }
    try:
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                errors[code] = str(exc)
            completed += 1
            if progress_callback is not None:
                progress_callback(
                    completed, total_codes,
                    f"已处理基准 {code}（{completed}/{total_codes}）",
                )
    except BaseException:
        for future in future_to_code:
            future.cancel()
        executor.shutdown(wait=True)
        raise
    else:
        executor.shutdown(wait=True)
    for code in codes:
        if code in errors:
            warnings.append(f"基准 {code} 行情获取失败: {errors[code]}")
            continue
        benchmark_nav[code] = results[code]
    return benchmark_nav, warnings


def build_benchmark_fetch_codes(requested_codes: Iterable[str]) -> List[str]:
    """Return benchmark codes to fetch.

    Fetches exactly what the user requested; only when nothing was requested do
    we fall back to the default benchmark set. Previously the defaults were
    always fetched (and discarded by the frontend when unselected), wasting
    three index data pulls on every backtest.
    """
    ordered_codes: List[str] = []
    seen: set[str] = set()
    for raw_code in requested_codes:
        code = str(raw_code).strip()
        if not code or code in seen:
            continue
        seen.add(code)
        ordered_codes.append(code)
    if not ordered_codes:
        ordered_codes = list(DEFAULT_BENCHMARK_CODES)
    return ordered_codes
