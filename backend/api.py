from __future__ import annotations

from io import BytesIO
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple, Union
import uuid

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from backend.engine import (
    align_plan_dates_to_trading_days,
    build_backtest_rebalance_dates,
    build_comparison_rebalance_modes,
    compute_metrics,
    compute_nav_series,
    compute_periodic_returns,
    drop_unavailable_symbols,
    normalize_stock_code,
    normalize_weight_map,
    parse_weight,
)
from backend.fetch import (
    build_benchmark_fetch_codes,
    fetch_benchmark_navs,
    fetch_close_prices,
)
from backend.models import (
    BacktestRequest,
    BacktestTaskState,
    CODE_COLUMN_CANDIDATES,
    CODE_COLUMN_EXCLUDED_KEYWORDS,
    CODE_COLUMN_PREFERRED_KEYWORDS,
    ComponentInput,
    HEADER_SCAN_ROWS,
    NAME_COLUMN_CANDIDATES,
    NAME_COLUMN_EXCLUDED_KEYWORDS,
    NAME_COLUMN_PREFERRED_KEYWORDS,
    CleanPlan,
    ProgressCallback,
    REBALANCE_MODE_LABELS,
    RebalancePlanInput,
    WEIGHT_COLUMN_CANDIDATES,
    WEIGHT_COLUMN_EXCLUDED_KEYWORDS,
    WEIGHT_COLUMN_PREFERRED_KEYWORDS,
)


def read_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


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

BACKTEST_TASKS: Dict[str, BacktestTaskState] = {}
BACKTEST_TASKS_LOCK = threading.Lock()
BACKTEST_TASK_TTL_SECONDS = 3600
MAX_CONCURRENT_TASKS = read_positive_int_env("BACKTEST_MAX_CONCURRENT", 3)
ACTIVE_TASK_COUNT = 0
ACTIVE_TASK_COUNT_LOCK = threading.Lock()
BACKTEST_STAGE_LABELS = {
    "queued": "等待执行",
    "prepare": "准备参数",
    "fetch_prices": "拉取成分股行情",
    "align_plans": "整理交易与调仓",
    "compute": "计算净值与指标",
    "fetch_benchmarks": "拉取基准行情",
    "finalize": "整理结果",
    "completed": "已完成",
    "failed": "执行失败",
}


def prune_backtest_tasks() -> None:
    now = time.monotonic()
    with BACKTEST_TASKS_LOCK:
        expired_ids = [
            task_id
            for task_id, task in BACKTEST_TASKS.items()
            if task.finished_monotonic is not None
            and now - task.finished_monotonic > BACKTEST_TASK_TTL_SECONDS
        ]
        for task_id in expired_ids:
            BACKTEST_TASKS.pop(task_id, None)


def create_backtest_task() -> BacktestTaskState:
    prune_backtest_tasks()
    task = BacktestTaskState(task_id=uuid.uuid4().hex)
    with BACKTEST_TASKS_LOCK:
        BACKTEST_TASKS[task.task_id] = task
    return task


def update_backtest_task(
    task_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[float] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    current_step: Optional[int] = None,
    total_steps: Optional[int] = None,
    result: Optional[Dict[str, object]] = None,
    error: Optional[str] = None,
) -> None:
    now = time.monotonic()
    with BACKTEST_TASKS_LOCK:
        task = BACKTEST_TASKS.get(task_id)
        if task is None:
            return
        if status is not None:
            task.status = status
        if progress is not None:
            task.progress = max(0.0, min(1.0, float(progress)))
        if stage is not None:
            task.stage = stage
        if message is not None:
            task.message = message
        if current_step is not None or total_steps is not None:
            task.current_step = current_step
            task.total_steps = total_steps
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        task.updated_monotonic = now
        if task.status in {"completed", "failed"}:
            task.finished_monotonic = now


def get_backtest_task_payload(task_id: str) -> Dict[str, object]:
    prune_backtest_tasks()
    with BACKTEST_TASKS_LOCK:
        task = BACKTEST_TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="未找到对应的回测任务")
        elapsed_seconds = max(
            0.0,
            (task.finished_monotonic or time.monotonic()) - task.started_monotonic,
        )
        payload: Dict[str, object] = {
            "task_id": task.task_id,
            "status": task.status,
            "stage": task.stage,
            "stage_label": BACKTEST_STAGE_LABELS.get(task.stage, task.stage),
            "message": task.message,
            "progress_pct": round(task.progress * 100, 1),
            "elapsed_seconds": round(elapsed_seconds, 1),
            "current_step": task.current_step,
            "total_steps": task.total_steps,
        }
        if task.result is not None:
            payload["result"] = task.result
        if task.error is not None:
            payload["error"] = task.error
        return payload


# ── Excel parsing helpers ──

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
        columns, candidates,
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
            row_values, CODE_COLUMN_CANDIDATES,
            preferred_keywords=CODE_COLUMN_PREFERRED_KEYWORDS,
            excluded_keywords=CODE_COLUMN_EXCLUDED_KEYWORDS,
        )
        _, name_score = find_best_column(
            row_values, NAME_COLUMN_CANDIDATES,
            preferred_keywords=NAME_COLUMN_PREFERRED_KEYWORDS,
            excluded_keywords=NAME_COLUMN_EXCLUDED_KEYWORDS,
        )
        _, weight_score = find_best_column(
            row_values, WEIGHT_COLUMN_CANDIDATES,
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
    except Exception as exc:
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
        {"code": code, "name": merged_names.get(code, ""), "weight": weight}
        for code, weight in merged_weights.items()
    ]


def sanitize_components(components: List[ComponentInput], allow_cash: bool = False) -> Tuple[List[Dict[str, object]], Dict[str, float], float]:
    merged_weights, merged_names = merge_components(components)
    normalized_map, cash_weight = normalize_weight_map(merged_weights, decimals=4, allow_cash=allow_cash)
    clean_components: List[Dict[str, object]] = []
    for code, weight in normalized_map.items():
        clean_components.append({
            "code": code,
            "name": merged_names.get(code, ""),
            "weight": round(weight * 100, 4),
        })
    return clean_components, normalized_map, cash_weight


def build_clean_plans(plans: List[RebalancePlanInput], allow_cash: bool = False) -> List[CleanPlan]:
    clean: List[CleanPlan] = []
    for plan in plans:
        components, weight_map, cash_weight = sanitize_components(plan.components, allow_cash=allow_cash)
        clean.append(CleanPlan(
            effective_date=pd.Timestamp(plan.effective_date),
            components=components,
            weight_map=weight_map,
            cash_weight=cash_weight,
        ))
    clean.sort(key=lambda x: x.effective_date)
    return clean


def execute_backtest(
    request: BacktestRequest,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, object]:
    def report(
        progress: float,
        stage: str,
        message: str,
        current_step: Optional[int] = None,
        total_steps: Optional[int] = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(progress, stage, message, current_step, total_steps)

    report(0.01, "prepare", "正在校验回测参数…")

    if request.start_date >= request.end_date:
        raise HTTPException(status_code=400, detail="回测开始日期必须早于结束日期")

    warnings: List[str] = []
    daily_rf_rate = float((1 + request.risk_free_rate) ** (1 / 252) - 1)

    try:
        plans = build_clean_plans(request.plans, allow_cash=request.allow_cash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    all_symbols = sorted({code for plan in plans for code in plan.weight_map})
    report(0.05, "prepare", f"参数校验完成，待拉取 {len(all_symbols)} 只成分股行情。", 0, len(all_symbols))

    def on_symbol_progress(done: int, total: int, message: str) -> None:
        fraction = 1.0 if total <= 0 else done / total
        report(0.05 + 0.65 * fraction, "fetch_prices", message, done, total)

    close_df, data_warnings = fetch_close_prices(
        all_symbols, request.start_date, request.end_date,
        progress_callback=on_symbol_progress,
    )
    warnings.extend(data_warnings)

    report(0.74, "align_plans", "正在剔除无有效行情的成分股…")
    filtered_plans, plan_warnings = drop_unavailable_symbols(plans, close_df)
    warnings.extend(plan_warnings)

    trading_dates = close_df.index
    if len(trading_dates) < 2:
        raise HTTPException(status_code=400, detail="交易日数量不足，无法完成回测")

    report(0.80, "align_plans", "正在对齐交易日与调仓计划…")
    aligned_plans = align_plan_dates_to_trading_days(
        filtered_plans, trading_dates, pd.Timestamp(request.start_date), warnings,
    )

    report(0.88, "compute", "正在计算净值曲线与绩效指标…")
    primary_rebalance_dates = build_backtest_rebalance_dates(
        trading_dates=trading_dates,
        plans=aligned_plans,
        mode=request.rebalance_mode,
        custom_dates=request.custom_rebalance_dates,
    )
    nav_series, applied_rebalance_dates, cost_log = compute_nav_series(
        close_df, aligned_plans, primary_rebalance_dates,
        commission_rate=request.commission_rate,
        stamp_duty_rate=request.stamp_duty_rate,
        slippage_rate=request.slippage_rate,
        daily_rf_rate=daily_rf_rate,
    )
    metrics = compute_metrics(nav_series, request.risk_free_rate)
    periodic_returns = compute_periodic_returns(nav_series)
    comparison_metrics = []
    for mode in build_comparison_rebalance_modes(request.rebalance_mode):
        comparison_rebalance_dates = build_backtest_rebalance_dates(
            trading_dates=trading_dates,
            plans=aligned_plans,
            mode=mode,
            custom_dates=[],
        )
        comparison_nav_series, _, _ = compute_nav_series(
            close_df, aligned_plans, comparison_rebalance_dates,
            daily_rf_rate=daily_rf_rate,
        )
        comparison_metrics.append({
            "mode": mode,
            "label": REBALANCE_MODE_LABELS[mode],
            "metrics": compute_metrics(comparison_nav_series, request.risk_free_rate),
            "rebalance_count": len(comparison_rebalance_dates),
        })

    nav_points = [
        {"date": str(pd.Timestamp(idx).date()), "value": float(round(value, 8))}
        for idx, value in nav_series.items()
    ]

    requested_benchmark_codes = set(request.benchmarks)
    benchmark_fetch_codes = build_benchmark_fetch_codes(request.benchmarks)

    def on_benchmark_progress(done: int, total: int, message: str) -> None:
        fraction = 1.0 if total <= 0 else done / total
        report(0.90 + 0.08 * fraction, "fetch_benchmarks", message, done, total)

    benchmark_nav, benchmark_warnings = fetch_benchmark_navs(
        benchmark_fetch_codes, requested_benchmark_codes,
        request.start_date, request.end_date,
        progress_callback=on_benchmark_progress,
    )
    warnings.extend(benchmark_warnings)

    report(0.99, "finalize", "正在整理回测结果…")

    result = {
        "data_source": "AKShare.stock_zh_a_hist(主) + stock_zh_a_hist_tx(兜底), 前复权",
        "missing_data_policy": "hold_cash: 成分股当日无行情(停牌/缺失)时记作当日收益 0，资金等效留在该头寸",
        "metrics": metrics,
        "selected_rebalance_mode": request.rebalance_mode,
        "selected_rebalance_label": REBALANCE_MODE_LABELS[request.rebalance_mode],
        "comparison_metrics": comparison_metrics,
        "nav": nav_points,
        "benchmark_nav": benchmark_nav,
        "rebalance_dates": [str(dt.date()) for dt in primary_rebalance_dates],
        "applied_rebalance_dates": applied_rebalance_dates,
        "periodic_returns": periodic_returns,
        "cost_log": cost_log,
        "cost_config": {
            "commission_rate": request.commission_rate,
            "stamp_duty_rate": request.stamp_duty_rate,
            "slippage_rate": request.slippage_rate,
        },
        "plan_summaries": [
            {
                "effective_date": str(plan.effective_date.date()),
                "components": plan.components,
                "cash_weight": round(plan.cash_weight, 4),
            }
            for plan in aligned_plans
        ],
        "warnings": warnings,
    }
    report(1.0, "completed", "回测完成。")
    return result


def run_backtest_task(task_id: str, request: BacktestRequest) -> None:
    global ACTIVE_TASK_COUNT

    def on_progress(
        progress: float,
        stage: str,
        message: str,
        current_step: Optional[int],
        total_steps: Optional[int],
    ) -> None:
        update_backtest_task(
            task_id,
            status="running",
            progress=progress,
            stage=stage,
            message=message,
            current_step=current_step,
            total_steps=total_steps,
        )

    update_backtest_task(
        task_id, status="running", progress=0.0, stage="queued",
        message="任务已启动，等待执行。", current_step=0, total_steps=0,
    )

    try:
        result = execute_backtest(request, progress_callback=on_progress)
    except HTTPException as exc:
        error = str(exc.detail)
        update_backtest_task(
            task_id, status="failed", stage="failed",
            message=f"回测失败：{error}", current_step=0, total_steps=0, error=error,
        )
    except Exception as exc:
        error = str(exc)
        update_backtest_task(
            task_id, status="failed", stage="failed",
            message=f"回测失败：{error}", current_step=0, total_steps=0, error=error,
        )
    else:
        update_backtest_task(
            task_id, status="completed", progress=1.0, stage="completed",
            message="回测完成。", current_step=0, total_steps=0, result=result, error=None,
        )
    finally:
        with ACTIVE_TASK_COUNT_LOCK:
            ACTIVE_TASK_COUNT = max(0, ACTIVE_TASK_COUNT - 1)


# ── API Routes ──

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
        df.columns, CODE_COLUMN_CANDIDATES,
        preferred_keywords=CODE_COLUMN_PREFERRED_KEYWORDS,
        excluded_keywords=CODE_COLUMN_EXCLUDED_KEYWORDS,
    )
    if code_col is None:
        code_col = str(df.columns[0])
    weight_col = choose_column(
        df.columns, WEIGHT_COLUMN_CANDIDATES,
        preferred_keywords=WEIGHT_COLUMN_PREFERRED_KEYWORDS,
        excluded_keywords=WEIGHT_COLUMN_EXCLUDED_KEYWORDS,
    )
    name_col = choose_column(
        df.columns, NAME_COLUMN_CANDIDATES,
        preferred_keywords=NAME_COLUMN_PREFERRED_KEYWORDS,
        excluded_keywords=NAME_COLUMN_EXCLUDED_KEYWORDS,
    )
    raw_components: List[ComponentInput] = []
    skipped_rows = 0
    skipped_reasons: List[str] = []
    for row_idx, (_, row) in enumerate(df.iterrows()):
        try:
            code = normalize_stock_code(row.get(code_col))
        except ValueError as exc:
            skipped_rows += 1
            skipped_reasons.append(f"第{row_idx + 1}行代码无效: {exc}")
            continue
        try:
            weight = parse_weight(row.get(weight_col)) if weight_col else 1.0
        except ValueError as exc:
            skipped_rows += 1
            skipped_reasons.append(f"{code} 权重解析失败: {exc}")
            continue
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
        "skipped_reasons": skipped_reasons[:20],
        "components": components,
    }


@app.post("/api/backtest")
def run_backtest(request: BacktestRequest) -> Dict[str, object]:
    return execute_backtest(request)


@app.post("/api/backtest/tasks")
def create_backtest_task_api(request: BacktestRequest) -> Dict[str, str]:
    global ACTIVE_TASK_COUNT
    with ACTIVE_TASK_COUNT_LOCK:
        if ACTIVE_TASK_COUNT >= MAX_CONCURRENT_TASKS:
            raise HTTPException(
                status_code=429,
                detail=f"已有 {ACTIVE_TASK_COUNT} 个回测任务正在执行，请等待其中某个完成后再提交（并发上限 {MAX_CONCURRENT_TASKS}）。",
            )
        ACTIVE_TASK_COUNT += 1
    task = create_backtest_task()
    worker = threading.Thread(target=run_backtest_task, args=(task.task_id, request), daemon=True)
    worker.start()
    return {"task_id": task.task_id}


@app.get("/api/backtest/tasks/{task_id}")
def get_backtest_task_api(task_id: str) -> Dict[str, object]:
    return get_backtest_task_payload(task_id)
