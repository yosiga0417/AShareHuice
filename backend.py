from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, getcontext
from io import BytesIO
import json
from pathlib import Path
import re
import time
from typing import Dict, Iterable, List, Literal, Optional, Tuple

import akshare as ak
import numpy as np
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator


getcontext().prec = 28


BASE_DIR = Path(__file__).resolve().parent
CACHE_ROOT = BASE_DIR / ".cache" / "akshare"
STOCK_CACHE_DIR = CACHE_ROOT / "stock_close"
STOCK_META_DIR = CACHE_ROOT / "stock_close_meta"


CODE_COLUMN_CANDIDATES = [
    "成份券代码",
    "成分券代码",
    "成份股代码",
    "成分股代码",
    "样本代码",
    "股票代码",
    "证券代码",
    "constituent code",
    "constituentcode",
    "sample code",
    "samplecode",
    "security code",
    "securitycode",
    "stock code",
    "stockcode",
    "symbol",
    "ticker",
    "代码",
    "code",
]

WEIGHT_COLUMN_CANDIDATES = [
    "权重(%)weight",
    "weight(%)",
    "weight%",
    "权重（%）",
    "权重(%)",
    "权重",
    "weight",
]

NAME_COLUMN_CANDIDATES = [
    "成份券名称",
    "成分券名称",
    "成份股名称",
    "成分股名称",
    "样本名称",
    "名称",
    "股票简称",
    "样本简称",
    "证券简称",
    "constituent name",
    "constituentname",
    "sample name",
    "samplename",
    "security name",
    "securityname",
    "stock name",
    "stockname",
    "name",
]

CODE_COLUMN_PREFERRED_KEYWORDS = [
    "成份",
    "成分",
    "券",
    "股",
    "样本",
    "constituent",
    "sample",
    "security",
    "stock",
]

CODE_COLUMN_EXCLUDED_KEYWORDS = [
    "指数",
    "index",
    "日期",
    "date",
    "名称",
    "name",
    "英文",
    "eng",
    "交易所",
    "exchange",
    "权重",
    "weight",
]

NAME_COLUMN_PREFERRED_KEYWORDS = [
    "成份",
    "成分",
    "券",
    "股",
    "样本",
    "简称",
    "constituent",
    "sample",
    "security",
    "stock",
]

NAME_COLUMN_EXCLUDED_KEYWORDS = [
    "指数",
    "index",
    "日期",
    "date",
    "代码",
    "code",
    "英文",
    "eng",
    "交易所",
    "exchange",
    "权重",
    "weight",
]

WEIGHT_COLUMN_PREFERRED_KEYWORDS = [
    "权重",
    "weight",
    "比重",
    "占比",
    "比例",
]

WEIGHT_COLUMN_EXCLUDED_KEYWORDS = [
    "日期",
    "date",
    "代码",
    "code",
    "名称",
    "name",
    "英文",
    "eng",
    "交易所",
    "exchange",
]

HEADER_SCAN_ROWS = 10


class ComponentInput(BaseModel):
    code: str
    weight: float
    name: Optional[str] = ""


class RebalancePlanInput(BaseModel):
    effective_date: date
    components: List[ComponentInput]

    @validator("components")
    def validate_components_not_empty(cls, value: List[ComponentInput]) -> List[ComponentInput]:
        if not value:
            raise ValueError("调仓计划的成分股不能为空")
        return value


class BacktestRequest(BaseModel):
    start_date: date
    end_date: date
    rebalance_mode: Literal["none", "monthly", "quarterly", "custom"] = "monthly"
    custom_rebalance_dates: List[date] = Field(default_factory=list)
    plans: List[RebalancePlanInput]
    risk_free_rate: float = 0.02
    missing_data_policy: Literal["hold_cash"] = "hold_cash"
    benchmarks: List[str] = Field(default_factory=list)

    @validator("plans")
    def validate_plans_not_empty(cls, value: List[RebalancePlanInput]) -> List[RebalancePlanInput]:
        if not value:
            raise ValueError("至少需要一条调仓计划")
        return value


@dataclass
class CleanPlan:
    effective_date: pd.Timestamp
    components: List[Dict[str, object]]
    weight_map: Dict[str, float]  # 0~1


app = FastAPI(
    title="A股自设指数回测服务",
    description="基于 AKShare 的 A 股成分股指数回测与分析服务",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def ensure_stock_cache_dirs() -> None:
    STOCK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STOCK_META_DIR.mkdir(parents=True, exist_ok=True)


def stock_cache_file(symbol: str) -> Path:
    return STOCK_CACHE_DIR / f"{symbol}.csv"


def stock_cache_meta_file(symbol: str) -> Path:
    return STOCK_META_DIR / f"{symbol}.json"


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


def merge_price_series(base: pd.Series, incoming: pd.Series) -> pd.Series:
    if base is None or len(base) == 0:
        return normalize_price_series(incoming)
    if incoming is None or len(incoming) == 0:
        return normalize_price_series(base)
    return normalize_price_series(pd.concat([base, incoming]))


def load_cached_stock_series(symbol: str) -> pd.Series:
    cache_path = stock_cache_file(symbol)
    if not cache_path.exists():
        return pd.Series(dtype="float64", name="close")

    try:
        df = pd.read_csv(cache_path)
    except Exception:  # noqa: BLE001
        return pd.Series(dtype="float64", name="close")

    if df.empty or "date" not in df.columns or "close" not in df.columns:
        return pd.Series(dtype="float64", name="close")

    series = pd.Series(df["close"].to_numpy(), index=df["date"])
    return normalize_price_series(series)


def save_cached_stock_series(symbol: str, series: pd.Series) -> None:
    ensure_stock_cache_dirs()
    clean_series = normalize_price_series(series)
    df = clean_series.rename("close").reset_index()
    df.columns = ["date", "close"]
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    df.to_csv(stock_cache_file(symbol), index=False)


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


def load_cached_stock_ranges(symbol: str) -> List[Tuple[date, date]]:
    cache_path = stock_cache_file(symbol)
    meta_path = stock_cache_meta_file(symbol)
    if not cache_path.exists() or not meta_path.exists():
        return []

    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []

    ranges: List[Tuple[date, date]] = []
    for item in payload.get("ranges", []):
        if not isinstance(item, dict):
            continue
        try:
            start_date = pd.Timestamp(item["start"]).date()
            end_date = pd.Timestamp(item["end"]).date()
        except Exception:  # noqa: BLE001
            continue
        if start_date <= end_date:
            ranges.append((start_date, end_date))

    return normalize_cached_ranges(ranges)


def save_cached_stock_ranges(symbol: str, ranges: Iterable[Tuple[date, date]]) -> None:
    ensure_stock_cache_dirs()
    payload = {
        "ranges": [
            {"start": start_date.isoformat(), "end": end_date.isoformat()}
            for start_date, end_date in normalize_cached_ranges(ranges)
        ]
    }
    stock_cache_meta_file(symbol).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
    return normalize_price_series(series.loc[mask])


def normalize_stock_code(raw_code: object) -> str:
    if raw_code is None:
        raise ValueError("股票代码为空")

    text = str(raw_code).strip()
    if not text:
        raise ValueError("股票代码为空")

    matched = re.search(r"(\d{6})", text)
    if matched:
        return matched.group(1)

    digits = re.sub(r"\D", "", text)
    if not digits:
        raise ValueError(f"无效股票代码: {text}")

    if len(digits) > 6:
        digits = digits[-6:]
    return digits.zfill(6)


def parse_weight(raw_weight: object) -> float:
    if raw_weight is None:
        raise ValueError("权重为空")

    text = str(raw_weight).strip().replace("%", "").replace(",", "")
    if not text:
        raise ValueError("权重为空")

    weight = float(text)
    if weight < 0:
        raise ValueError("权重不能为负")
    return weight


def normalize_weights_exact(weights: List[float], decimals: int = 4) -> List[float]:
    if not weights:
        raise ValueError("权重列表为空")

    source = [Decimal(str(v)) for v in weights]
    if any(v < 0 for v in source):
        raise ValueError("权重不能为负")

    source_sum = sum(source)
    if source_sum <= 0:
        raise ValueError("权重总和必须大于 0")

    target = Decimal("100")
    scaled = [v / source_sum * target for v in source]

    multiplier = Decimal(10) ** decimals
    target_units = int((target * multiplier).to_integral_value(rounding=ROUND_HALF_UP))
    raw_units = [v * multiplier for v in scaled]
    floor_units = [int(v.to_integral_value(rounding=ROUND_FLOOR)) for v in raw_units]

    remainder = target_units - sum(floor_units)
    if remainder > 0:
        fractions = [raw_units[idx] - Decimal(floor_units[idx]) for idx in range(len(raw_units))]
        order = sorted(range(len(fractions)), key=lambda i: fractions[i], reverse=True)
        for idx in order[:remainder]:
            floor_units[idx] += 1

    return [unit / float(multiplier) for unit in floor_units]


def normalize_weight_map(weight_map: Dict[str, float], decimals: int = 4) -> Dict[str, float]:
    if not weight_map:
        raise ValueError("调仓计划成分股为空")

    codes = list(weight_map.keys())
    normalized = normalize_weights_exact([weight_map[code] * 100 for code in codes], decimals=decimals)
    return {code: pct / 100.0 for code, pct in zip(codes, normalized)}


def normalize_column_name(name: object) -> str:
    text = str(name).strip().lower()
    text = text.replace(" ", "")
    text = text.replace("_", "").replace("-", "")
    text = text.replace("（", "(").replace("）", ")")
    return text


def clean_excel_text(value: object) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def make_unique_headers(values: Iterable[object]) -> List[str]:
    headers: List[str] = []
    seen: Dict[str, int] = {}

    for idx, value in enumerate(values):
        base = clean_excel_text(value) or f"unnamed_{idx + 1}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        headers.append(base if count == 0 else f"{base}_{count + 1}")

    return headers


def find_best_column(
    columns: Iterable[object],
    candidates: List[str],
    preferred_keywords: Optional[List[str]] = None,
    excluded_keywords: Optional[List[str]] = None,
) -> Tuple[Optional[str], int]:
    normalized_columns = [(str(col), normalize_column_name(col)) for col in columns]
    candidate_norm = [normalize_column_name(c) for c in candidates]
    preferred_norm = [normalize_column_name(c) for c in preferred_keywords or []]
    excluded_norm = [normalize_column_name(c) for c in excluded_keywords or []]
    best_column: Optional[str] = None
    best_score = 0

    for original, normalized in normalized_columns:
        if not normalized:
            continue

        base_score = 0
        for candidate in candidate_norm:
            if not candidate:
                continue
            if normalized == candidate:
                base_score = max(base_score, 1000 + len(candidate))
            elif candidate in normalized:
                base_score = max(base_score, 100 + len(candidate))

        if base_score <= 0:
            continue

        score = base_score
        for keyword in preferred_norm:
            if keyword and keyword in normalized:
                score += 25
        for keyword in excluded_norm:
            if keyword and keyword in normalized:
                score -= 35

        if score > best_score:
            best_column = original
            best_score = score

    return best_column, best_score


def choose_column(
    columns: Iterable[object],
    candidates: List[str],
    preferred_keywords: Optional[List[str]] = None,
    excluded_keywords: Optional[List[str]] = None,
) -> Optional[str]:
    best_column, _ = find_best_column(
        columns,
        candidates,
        preferred_keywords=preferred_keywords,
        excluded_keywords=excluded_keywords,
    )
    return best_column


def detect_header_row(raw_df: pd.DataFrame, max_scan_rows: int = HEADER_SCAN_ROWS) -> int:
    scan_rows = min(len(raw_df), max_scan_rows)
    best_idx = 0
    best_score = -1

    for idx in range(scan_rows):
        row_values = [clean_excel_text(value) for value in raw_df.iloc[idx].tolist()]
        _, code_score = find_best_column(
            row_values,
            CODE_COLUMN_CANDIDATES,
            preferred_keywords=CODE_COLUMN_PREFERRED_KEYWORDS,
            excluded_keywords=CODE_COLUMN_EXCLUDED_KEYWORDS,
        )
        _, name_score = find_best_column(
            row_values,
            NAME_COLUMN_CANDIDATES,
            preferred_keywords=NAME_COLUMN_PREFERRED_KEYWORDS,
            excluded_keywords=NAME_COLUMN_EXCLUDED_KEYWORDS,
        )
        _, weight_score = find_best_column(
            row_values,
            WEIGHT_COLUMN_CANDIDATES,
            preferred_keywords=WEIGHT_COLUMN_PREFERRED_KEYWORDS,
            excluded_keywords=WEIGHT_COLUMN_EXCLUDED_KEYWORDS,
        )

        identified_fields = sum(score > 0 for score in (code_score, name_score, weight_score))
        if code_score <= 0 or identified_fields < 2:
            continue

        row_score = code_score * 3 + name_score + weight_score
        if row_score > best_score:
            best_idx = idx
            best_score = row_score

    return best_idx if best_score >= 0 else 0


def read_excel_table(content: bytes) -> pd.DataFrame:
    try:
        raw_df = pd.read_excel(BytesIO(content), dtype=str, header=None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Excel 读取失败: {exc}") from exc

    raw_df = raw_df.dropna(how="all").reset_index(drop=True)
    if raw_df.empty:
        raise HTTPException(status_code=400, detail="Excel 文件为空")

    header_row = detect_header_row(raw_df)
    headers = make_unique_headers(raw_df.iloc[header_row].tolist())
    df = raw_df.iloc[header_row + 1 :].reset_index(drop=True).copy()
    df.columns = headers
    df = df.dropna(how="all").reset_index(drop=True)
    return df


def merge_components(components: List[ComponentInput]) -> Tuple[Dict[str, float], Dict[str, str]]:
    merged_weights: Dict[str, float] = {}
    merged_names: Dict[str, str] = {}

    for item in components:
        code = normalize_stock_code(item.code)
        weight = float(item.weight)
        if weight < 0:
            raise ValueError(f"{code} 的权重不能为负")

        merged_weights[code] = merged_weights.get(code, 0.0) + weight
        if item.name:
            merged_names[code] = str(item.name).strip()

    if not merged_weights:
        raise ValueError("未识别到有效成分股")

    return merged_weights, merged_names


def build_preview_components(components: List[ComponentInput]) -> List[Dict[str, object]]:
    merged_weights, merged_names = merge_components(components)
    return [
        {
            "code": code,
            "name": merged_names.get(code, ""),
            "weight": weight,
        }
        for code, weight in merged_weights.items()
    ]


def sanitize_components(components: List[ComponentInput]) -> Tuple[List[Dict[str, object]], Dict[str, float]]:
    merged_weights, merged_names = merge_components(components)
    normalized_pcts = normalize_weights_exact(list(merged_weights.values()), decimals=4)
    clean_components: List[Dict[str, object]] = []
    weight_map: Dict[str, float] = {}

    for code, weight_pct in zip(merged_weights.keys(), normalized_pcts):
        clean_components.append(
            {
                "code": code,
                "name": merged_names.get(code, ""),
                "weight": weight_pct,
            }
        )
        weight_map[code] = weight_pct / 100.0

    return clean_components, weight_map


def build_clean_plans(plans: List[RebalancePlanInput]) -> List[CleanPlan]:
    clean: List[CleanPlan] = []
    for plan in plans:
        components, weight_map = sanitize_components(plan.components)
        clean.append(
            CleanPlan(
                effective_date=pd.Timestamp(plan.effective_date),
                components=components,
                weight_map=weight_map,
            )
        )
    clean.sort(key=lambda x: x.effective_date)
    return clean


def fetch_close_prices(
    symbols: List[str], start_date: date, end_date: date
) -> Tuple[pd.DataFrame, List[str]]:
    frames: Dict[str, pd.Series] = {}
    warnings: List[str] = []

    for symbol in symbols:
        try:
            series, source = fetch_symbol_close_series(symbol, start_date, end_date)
            frames[symbol] = series
            if source == "stock_zh_a_hist_tx":
                warnings.append(f"{symbol} 使用兜底数据源 {source}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{symbol} 行情获取失败: {exc}")
            continue

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


def try_fetch_hist(symbol: str, beg: str, end: str, max_retry: int = 2) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for attempt in range(max_retry):
        try:
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
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < max_retry - 1:
                time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"stock_zh_a_hist failed: {last_error}")


def try_fetch_hist_tx(symbol: str, beg: str, end: str, max_retry: int = 2) -> pd.DataFrame:
    tx_symbol = stock_to_tx_symbol(symbol)
    last_error: Optional[Exception] = None
    for attempt in range(max_retry):
        try:
            hist = ak.stock_zh_a_hist_tx(
                symbol=tx_symbol,
                start_date=beg,
                end_date=end,
                adjust="qfq",
            )
            if hist is None or hist.empty:
                raise ValueError("返回数据为空")
            return hist
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < max_retry - 1:
                time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"stock_zh_a_hist_tx({tx_symbol}) failed: {last_error}")


def fetch_symbol_close_series_remote(
    symbol: str, start_date: date, end_date: date
) -> Tuple[pd.Series, str]:
    beg = start_date.strftime("%Y%m%d")
    end = end_date.strftime("%Y%m%d")
    errors: List[str] = []

    try:
        hist = try_fetch_hist(symbol, beg, end, max_retry=2)
        return extract_date_close_series(hist), "stock_zh_a_hist"
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    try:
        hist_tx = try_fetch_hist_tx(symbol, beg, end, max_retry=2)
        return extract_date_close_series(hist_tx), "stock_zh_a_hist_tx"
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    raise RuntimeError("; ".join(errors))


def fetch_symbol_close_series(
    symbol: str, start_date: date, end_date: date
) -> Tuple[pd.Series, str]:
    request_start = pd.Timestamp(start_date).date()
    request_end = pd.Timestamp(end_date).date()
    if request_start > request_end:
        raise ValueError("开始日期不能晚于结束日期")

    today = date.today()
    historical_end = min(request_end, today - timedelta(days=1))
    includes_today = request_start <= today <= request_end

    cached_series = load_cached_stock_series(symbol)
    cached_ranges = load_cached_stock_ranges(symbol)

    fetched_sources: List[str] = []
    cache_dirty = False
    ranges_dirty = False

    if request_start <= historical_end:
        missing_ranges = compute_missing_ranges(request_start, historical_end, cached_ranges)
        for gap_start, gap_end in missing_ranges:
            fetch_start, fetch_end = expand_fetch_range(gap_start, gap_end, cached_ranges)
            gap_series, source = fetch_symbol_close_series_remote(symbol, fetch_start, fetch_end)
            cached_series = merge_price_series(cached_series, gap_series)
            cached_ranges = normalize_cached_ranges(cached_ranges + [(gap_start, gap_end)])
            fetched_sources.append(source)
            cache_dirty = True
            ranges_dirty = True

    refresh_error: Optional[Exception] = None
    if includes_today:
        try:
            today_series, source = fetch_symbol_close_series_remote(symbol, today, today)
            cached_series = merge_price_series(cached_series, today_series)
            fetched_sources.append(source)
            cache_dirty = True
        except Exception as exc:  # noqa: BLE001
            refresh_error = exc

    if cache_dirty:
        save_cached_stock_series(symbol, cached_series)
    if ranges_dirty:
        save_cached_stock_ranges(symbol, cached_ranges)

    result = slice_price_series(cached_series, request_start, request_end)
    if result.empty:
        if refresh_error is not None:
            raise RuntimeError(str(refresh_error))
        raise RuntimeError("区间内没有可用收盘价")

    if "stock_zh_a_hist_tx" in fetched_sources:
        return result, "stock_zh_a_hist_tx"
    if "stock_zh_a_hist" in fetched_sources:
        return result, "stock_zh_a_hist"
    return result, "cache"


def fetch_benchmark_nav(
    code: str, start_date: date, end_date: date
) -> List[Dict[str, object]]:
    beg = start_date.strftime("%Y%m%d")
    end = end_date.strftime("%Y%m%d")

    # Try index_zh_a_hist first (works for 000300, 000905, 000001, etc.)
    for attempt in range(2):
        try:
            df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=beg, end_date=end)
            if df is not None and not df.empty:
                break
        except Exception:
            if attempt == 0:
                time.sleep(0.35)
    else:
        # Fallback: stock_zh_index_daily with exchange prefix
        prefix = "sh" if code.startswith(("0", "9")) else "sz"
        df = ak.stock_zh_index_daily(symbol=f"{prefix}{code}")
        if df is not None and not df.empty:
            df = df.rename(columns={"date": "日期", "close": "收盘"})
            df["日期"] = pd.to_datetime(df["日期"])
            mask = (df["日期"] >= pd.Timestamp(start_date)) & (df["日期"] <= pd.Timestamp(end_date))
            df = df.loc[mask]

    if df is None or df.empty:
        return []

    date_col = "日期" if "日期" in df.columns else df.columns[0]
    close_col = "收盘" if "收盘" in df.columns else df.columns[4]

    df = df[[date_col, close_col]].copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df[close_col] = pd.to_numeric(df[close_col], errors="coerce")
    df = df.dropna()
    df = df.sort_values(date_col)

    return [
        {"date": str(row[date_col].date()), "value": float(row[close_col])}
        for _, row in df.iterrows()
    ]


def map_to_next_trading_day(target: pd.Timestamp, trading_dates: pd.DatetimeIndex) -> Optional[pd.Timestamp]:
    pos = trading_dates.searchsorted(target)
    if pos >= len(trading_dates):
        return None
    return pd.Timestamp(trading_dates[pos])


def build_periodic_rebalance_dates(
    trading_dates: pd.DatetimeIndex,
    mode: str,
    custom_dates: List[date],
) -> List[pd.Timestamp]:
    periodic: List[pd.Timestamp] = []

    if mode == "monthly":
        marker = None
        for dt in trading_dates:
            key = (dt.year, dt.month)
            if key != marker:
                periodic.append(pd.Timestamp(dt))
                marker = key
    elif mode == "quarterly":
        marker = None
        for dt in trading_dates:
            quarter = (dt.month - 1) // 3 + 1
            key = (dt.year, quarter)
            if key != marker:
                periodic.append(pd.Timestamp(dt))
                marker = key
    elif mode == "custom":
        seen = set()
        for raw in custom_dates:
            mapped = map_to_next_trading_day(pd.Timestamp(raw), trading_dates)
            if mapped is not None and mapped not in seen:
                periodic.append(mapped)
                seen.add(mapped)

    periodic.sort()
    return periodic


def active_plan_for_date(plans: List[CleanPlan], current_date: pd.Timestamp) -> CleanPlan:
    active = plans[0]
    for plan in plans:
        if plan.effective_date <= current_date:
            active = plan
        else:
            break
    return active


def drop_unavailable_symbols(
    plans: List[CleanPlan], close_df: pd.DataFrame
) -> Tuple[List[CleanPlan], List[str]]:
    warnings: List[str] = []
    available = {code for code in close_df.columns if close_df[code].notna().any()}
    clean: List[CleanPlan] = []

    for plan in plans:
        filtered_components: List[Dict[str, object]] = []
        filtered_weights: Dict[str, float] = {}
        dropped: List[str] = []

        for component in plan.components:
            code = str(component["code"])
            if code in available:
                filtered_components.append(component)
                filtered_weights[code] = plan.weight_map[code]
            else:
                dropped.append(code)

        if dropped:
            warnings.append(
                f"{plan.effective_date.date()} 调仓计划中 {', '.join(dropped)} 无可用行情，已自动剔除并重归一化"
            )

        if not filtered_weights:
            raise HTTPException(
                status_code=400,
                detail=f"{plan.effective_date.date()} 调仓计划无可用成分股，无法继续回测",
            )

        normalized = normalize_weight_map(filtered_weights, decimals=4)
        for component in filtered_components:
            code = str(component["code"])
            component["weight"] = round(normalized[code] * 100, 4)

        clean.append(
            CleanPlan(
                effective_date=plan.effective_date,
                components=filtered_components,
                weight_map=normalized,
            )
        )

    return clean, warnings


def compute_nav_series(
    close_df: pd.DataFrame,
    plans: List[CleanPlan],
    rebalance_dates: List[pd.Timestamp],
) -> Tuple[pd.Series, List[str]]:
    trading_dates = close_df.index
    returns = close_df.pct_change().replace([np.inf, -np.inf], np.nan)
    rebalance_set = {pd.Timestamp(d) for d in rebalance_dates}

    active = active_plan_for_date(plans, pd.Timestamp(trading_dates[0]))
    weights = dict(active.weight_map)

    nav_values = [1.0]
    applied_rebalances = [str(pd.Timestamp(trading_dates[0]).date())]

    for idx in range(1, len(trading_dates)):
        current_date = pd.Timestamp(trading_dates[idx])
        today_ret = returns.iloc[idx]

        portfolio_ret = 0.0
        for code, weight in weights.items():
            stock_ret = today_ret.get(code)
            if pd.notna(stock_ret):
                portfolio_ret += weight * float(stock_ret)

        nav_values.append(nav_values[-1] * (1.0 + portfolio_ret))

        value_map: Dict[str, float] = {}
        total_value = 0.0
        for code, weight in weights.items():
            stock_ret = today_ret.get(code)
            growth = 1.0 + float(stock_ret) if pd.notna(stock_ret) else 1.0
            position_value = max(weight * growth, 0.0)
            value_map[code] = position_value
            total_value += position_value

        if total_value > 0:
            weights = {code: value / total_value for code, value in value_map.items() if value > 0}

        if current_date in rebalance_set:
            active = active_plan_for_date(plans, current_date)
            weights = dict(active.weight_map)
            applied_rebalances.append(str(current_date.date()))

    nav_series = pd.Series(nav_values, index=trading_dates, name="nav")
    return nav_series, applied_rebalances


def compute_metrics(nav_series: pd.Series, risk_free_rate: float) -> Dict[str, float]:
    if nav_series.empty:
        raise ValueError("净值序列为空")

    daily_returns = nav_series.pct_change().dropna()
    total_return = float(nav_series.iloc[-1] / nav_series.iloc[0] - 1.0)

    if len(daily_returns) > 0:
        annual_return = float((nav_series.iloc[-1] / nav_series.iloc[0]) ** (252 / len(daily_returns)) - 1.0)
        annual_volatility = float(daily_returns.std(ddof=0) * np.sqrt(252))
        win_rate = float((daily_returns > 0).mean())
    else:
        annual_return = 0.0
        annual_volatility = 0.0
        win_rate = 0.0

    sharpe_ratio: float
    if annual_volatility > 0:
        sharpe_ratio = float((annual_return - risk_free_rate) / annual_volatility)
    else:
        sharpe_ratio = 0.0

    rolling_max = nav_series.cummax()
    drawdown = nav_series / rolling_max - 1.0
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": annual_volatility,
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
    }


def align_plan_dates_to_trading_days(
    plans: List[CleanPlan],
    trading_dates: pd.DatetimeIndex,
    start_date: pd.Timestamp,
    warnings: List[str],
) -> List[CleanPlan]:
    aligned: List[CleanPlan] = []

    first_trade_day = pd.Timestamp(trading_dates[0])
    if plans and plans[0].effective_date > first_trade_day:
        warnings.append(
            f"首个调仓日 {plans[0].effective_date.date()} 晚于回测起点，已自动在 {first_trade_day.date()} 应用首个调仓方案"
        )
        aligned.append(
            CleanPlan(
                effective_date=first_trade_day,
                components=plans[0].components,
                weight_map=plans[0].weight_map,
            )
        )

    mapped: Dict[pd.Timestamp, CleanPlan] = {}
    for plan in plans:
        target = max(plan.effective_date, start_date)
        mapped_day = map_to_next_trading_day(target, trading_dates)
        if mapped_day is None:
            warnings.append(f"{plan.effective_date.date()} 调仓日超出可交易区间，已忽略")
            continue

        if target != plan.effective_date:
            warnings.append(
                f"{plan.effective_date.date()} 早于回测起点，已调整为 {mapped_day.date()} 执行调仓"
            )
        elif mapped_day != plan.effective_date:
            warnings.append(
                f"{plan.effective_date.date()} 非交易日，已顺延至 {mapped_day.date()} 执行调仓"
            )

        mapped[mapped_day] = CleanPlan(
            effective_date=mapped_day,
            components=plan.components,
            weight_map=plan.weight_map,
        )

    aligned.extend(mapped.values())
    if not aligned and mapped:
        aligned.extend(mapped.values())

    if not aligned:
        raise HTTPException(status_code=400, detail="所有调仓计划均不在可交易区间内")

    aligned.sort(key=lambda x: x.effective_date)
    return aligned


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "data_source": "AKShare"}


@app.post("/api/parse-components")
async def parse_components(file: UploadFile = File(...)) -> Dict[str, object]:
    filename = file.filename or ""
    lower_name = filename.lower()
    if not (lower_name.endswith(".xls") or lower_name.endswith(".xlsx")):
        raise HTTPException(status_code=400, detail="仅支持 xls/xlsx 文件")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")

    df = read_excel_table(content)

    code_col = choose_column(
        df.columns,
        CODE_COLUMN_CANDIDATES,
        preferred_keywords=CODE_COLUMN_PREFERRED_KEYWORDS,
        excluded_keywords=CODE_COLUMN_EXCLUDED_KEYWORDS,
    )
    if code_col is None:
        code_col = str(df.columns[0])

    weight_col = choose_column(
        df.columns,
        WEIGHT_COLUMN_CANDIDATES,
        preferred_keywords=WEIGHT_COLUMN_PREFERRED_KEYWORDS,
        excluded_keywords=WEIGHT_COLUMN_EXCLUDED_KEYWORDS,
    )
    name_col = choose_column(
        df.columns,
        NAME_COLUMN_CANDIDATES,
        preferred_keywords=NAME_COLUMN_PREFERRED_KEYWORDS,
        excluded_keywords=NAME_COLUMN_EXCLUDED_KEYWORDS,
    )

    raw_components: List[ComponentInput] = []
    skipped_rows = 0

    for _, row in df.iterrows():
        try:
            code = normalize_stock_code(row.get(code_col))
        except Exception:
            skipped_rows += 1
            continue

        try:
            weight = parse_weight(row.get(weight_col)) if weight_col else 1.0
        except Exception:
            weight = 0.0

        name = clean_excel_text(row.get(name_col)) if name_col else ""
        raw_components.append(ComponentInput(code=code, weight=weight, name=name))

    if not raw_components:
        raise HTTPException(status_code=400, detail="未解析到有效成分股")

    try:
        components = build_preview_components(raw_components)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "filename": filename,
        "columns": [str(col) for col in df.columns],
        "code_column": code_col,
        "weight_column": weight_col,
        "name_column": name_col,
        "row_count": int(len(df)),
        "parsed_count": int(len(components)),
        "skipped_rows": int(skipped_rows),
        "components": components,
    }


@app.post("/api/backtest")
def run_backtest(request: BacktestRequest) -> Dict[str, object]:
    if request.start_date >= request.end_date:
        raise HTTPException(status_code=400, detail="回测开始日期必须早于结束日期")

    warnings: List[str] = []

    try:
        plans = build_clean_plans(request.plans)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    all_symbols = sorted({code for plan in plans for code in plan.weight_map})
    close_df, data_warnings = fetch_close_prices(all_symbols, request.start_date, request.end_date)
    warnings.extend(data_warnings)

    filtered_plans, plan_warnings = drop_unavailable_symbols(plans, close_df)
    warnings.extend(plan_warnings)

    trading_dates = close_df.index
    if len(trading_dates) < 2:
        raise HTTPException(status_code=400, detail="交易日数量不足，无法完成回测")

    aligned_plans = align_plan_dates_to_trading_days(
        filtered_plans,
        trading_dates,
        pd.Timestamp(request.start_date),
        warnings,
    )

    periodic_dates = build_periodic_rebalance_dates(
        trading_dates=trading_dates,
        mode=request.rebalance_mode,
        custom_dates=request.custom_rebalance_dates,
    )

    all_rebalance_dates = {pd.Timestamp(trading_dates[0])}
    for plan in aligned_plans:
        all_rebalance_dates.add(plan.effective_date)
    for dt in periodic_dates:
        all_rebalance_dates.add(dt)

    sorted_rebalance_dates = sorted(all_rebalance_dates)
    nav_series, applied_rebalance_dates = compute_nav_series(close_df, aligned_plans, sorted_rebalance_dates)
    metrics = compute_metrics(nav_series, request.risk_free_rate)

    nav_points = [
        {"date": str(pd.Timestamp(idx).date()), "value": float(round(value, 8))}
        for idx, value in nav_series.items()
    ]

    # Fetch benchmark index data
    benchmark_nav: Dict[str, object] = {}
    for bm_code in request.benchmarks:
        try:
            bm_points = fetch_benchmark_nav(bm_code, request.start_date, request.end_date)
            benchmark_nav[bm_code] = bm_points
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"基准 {bm_code} 行情获取失败: {exc}")

    return {
        "data_source": "AKShare.stock_zh_a_hist(主) + stock_zh_a_hist_tx(兜底), 前复权",
        "missing_data_policy": "hold_cash: 成分股当日无行情(停牌/缺失)时记作当日收益 0，资金等效留在该头寸",
        "metrics": metrics,
        "nav": nav_points,
        "benchmark_nav": benchmark_nav,
        "rebalance_dates": [str(dt.date()) for dt in sorted_rebalance_dates],
        "applied_rebalance_dates": applied_rebalance_dates,
        "plan_summaries": [
            {
                "effective_date": str(plan.effective_date.date()),
                "components": plan.components,
            }
            for plan in aligned_plans
        ],
        "warnings": warnings,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend:app", host="127.0.0.1", port=8000, reload=False)
