from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import re
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from fastapi import HTTPException

from backend.models import CleanPlan, STANDARD_REBALANCE_MODES, REBALANCE_MODE_LABELS


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


def normalize_weights_exact(weights: List[float], decimals: int = 4, target_sum: Optional[float] = None) -> List[float]:
    if not weights:
        raise ValueError("权重列表为空")
    source = [Decimal(str(v)) for v in weights]
    if any(v < 0 for v in source):
        raise ValueError("权重不能为负")
    source_sum = sum(source)
    if source_sum <= 0:
        raise ValueError("权重总和必须大于 0")
    target = Decimal(str(target_sum)) if target_sum is not None else Decimal("100")
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


def normalize_weight_map(weight_map: Dict[str, float], decimals: int = 4, allow_cash: bool = False) -> Tuple[Dict[str, float], float]:
    if not weight_map:
        raise ValueError("调仓计划成分股为空")
    codes = list(weight_map.keys())
    raw_pcts = [weight_map[code] * 100 for code in codes]
    raw_sum = sum(raw_pcts)
    if allow_cash and raw_sum < 100.0:
        normalized = normalize_weights_exact(raw_pcts, decimals=decimals, target_sum=raw_sum)
        cash_weight = round(1.0 - sum(normalized) / 100.0, decimals + 2)
        cash_weight = max(0.0, float(cash_weight))
        return {code: pct / 100.0 for code, pct in zip(codes, normalized)}, cash_weight
    normalized = normalize_weights_exact(raw_pcts, decimals=decimals)
    return {code: pct / 100.0 for code, pct in zip(codes, normalized)}, 0.0


def build_plan_weight_vector(
    weight_map: Dict[str, float],
    symbol_index: Dict[str, int],
    symbol_count: int,
) -> np.ndarray:
    vector = np.zeros(symbol_count, dtype=np.float64)
    for code, weight in weight_map.items():
        idx = symbol_index.get(code)
        if idx is not None:
            vector[idx] = float(weight)
    return vector
def prepare_nav_data(close_df: pd.DataFrame) -> Tuple[np.ndarray, Dict[str, int], Dict[int, str], pd.DatetimeIndex]:
    """Precompute shared NAV calculation data to avoid redundant work across rebalance mode comparisons."""
    trading_dates = close_df.index
    returns_array = close_df.pct_change().to_numpy(dtype=np.float64, copy=True)
    np.nan_to_num(returns_array, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    symbol_index = {code: idx for idx, code in enumerate(close_df.columns)}
    reverse_index = {idx: code for code, idx in symbol_index.items()}
    return returns_array, symbol_index, reverse_index, trading_dates


def compute_nav_series_from_prepared(
    returns_array: np.ndarray,
    symbol_index: Dict[str, int],
    reverse_index: Dict[int, str],
    trading_dates: pd.DatetimeIndex,
    plans: List[CleanPlan],
    rebalance_dates: List[pd.Timestamp],
    commission_rate: float = 0.0,
    stamp_duty_rate: float = 0.0,
    slippage_rate: float = 0.0,
    daily_rf_rate: float = 0.0,
    track_top_n: int = 0,
) -> Tuple[pd.Series, List[str], List[Dict[str, object]], Optional[List[Dict[str, object]]]]:
    """Compute NAV series using already-prepared data arrays.

    Parameters are passed through directly from the caller; see the enclosing
    function for parameter documentation.
    """
    date_count = len(trading_dates)
    symbol_count = len(symbol_index)
    cost_rate = 2.0 * commission_rate + stamp_duty_rate + 2.0 * slippage_rate
    has_cost = cost_rate > 0.0

    # ── Pre-compute date strings once ──
    date_strs = [str(dt.date()) for dt in trading_dates]

    # ── Holdings tracking ──
    do_track = track_top_n > 0
    holdings_dates: List[str] = []
    holdings_matrix: Optional[np.ndarray] = None
    holdings_cash_values: Optional[np.ndarray] = None
    if do_track:
        holdings_dates = date_strs
        holdings_matrix = np.empty((date_count, symbol_count), dtype=np.float64)
        holdings_cash_values = np.empty(date_count, dtype=np.float64)

    plan_vectors = [
        (plan.effective_date,
         build_plan_weight_vector(plan.weight_map, symbol_index, symbol_count),
         plan.cash_weight)
        for plan in plans
    ]

    first_trade_day = pd.Timestamp(trading_dates[0])
    active_plan_idx = 0
    for idx, (effective_date, _, _) in enumerate(plan_vectors):
        if effective_date <= first_trade_day:
            active_plan_idx = idx
        else:
            break

    weights = plan_vectors[active_plan_idx][1].copy()
    cash_weight = plan_vectors[active_plan_idx][2]
    rebalance_schedule: Dict[int, Tuple[np.ndarray, float]] = {}
    for rebalance_day in sorted(set(rebalance_dates)):
        pos = trading_dates.searchsorted(rebalance_day)
        if pos >= len(trading_dates) or trading_dates[pos] != rebalance_day:
            continue
        if pos == 0:
            continue
        while active_plan_idx + 1 < len(plan_vectors) and plan_vectors[active_plan_idx + 1][0] <= rebalance_day:
            active_plan_idx += 1
        rebalance_schedule[pos] = (plan_vectors[active_plan_idx][1], plan_vectors[active_plan_idx][2])

    nav_values = np.empty(date_count, dtype=np.float64)
    nav_values[0] = 1.0
    applied_rebalances = [str(first_trade_day.date())]
    cost_log: List[Dict[str, object]] = []
    position_values = np.empty(symbol_count, dtype=np.float64)
    weight_delta = np.empty(symbol_count, dtype=np.float64) if has_cost else None

    # Record initial holdings
    if holdings_matrix is not None and holdings_cash_values is not None:
        holdings_matrix[0] = weights
        holdings_cash_values[0] = cash_weight

    for idx in range(1, date_count):
        today_ret = returns_array[idx]
        equity_ret = float(np.dot(weights, today_ret))

        # Portfolio return = equity + cash
        portfolio_ret = equity_ret + cash_weight * daily_rf_rate
        nav_values[idx] = nav_values[idx - 1] * (1.0 + portfolio_ret)

        # Update positions
        np.multiply(weights, today_ret, out=position_values)
        position_values += weights
        np.maximum(position_values, 0.0, out=position_values)

        # Drifted weights
        total_value = float(position_values.sum()) + cash_weight
        if total_value > 0.0:
            np.divide(position_values, total_value, out=weights)
            cash_weight = cash_weight * (1.0 + daily_rf_rate) / (nav_values[idx] / nav_values[idx - 1])

        # Rebalance
        if idx in rebalance_schedule:
            target_weights, target_cash = rebalance_schedule[idx]

            if has_cost and weight_delta is not None:
                np.subtract(weights, target_weights, out=weight_delta)
                np.maximum(weight_delta, 0.0, out=weight_delta)
                sells = float(weight_delta.sum()) + max(0.0, cash_weight - target_cash)
                turnover = sells
                if turnover > 0.0:
                    cost = turnover * cost_rate
                    nav_values[idx] *= (1.0 - cost)
                    cost_log.append({
                        "date": date_strs[idx],
                        "turnover": round(turnover * 100, 4),
                        "cost": round(cost * 100, 6),
                        "cost_rate": round(cost_rate * 100, 4),
                    })
                    position_values *= (1.0 - cost)

            np.copyto(weights, target_weights)
            cash_weight = target_cash
            applied_rebalances.append(date_strs[idx])

        # ── Record holdings after each day ──
        if holdings_matrix is not None and holdings_cash_values is not None:
            holdings_matrix[idx] = weights
            holdings_cash_values[idx] = cash_weight

    nav_series = pd.Series(nav_values, index=trading_dates, name="nav")

    # ── Build holdings evolution output ──
    holdings_evolution: Optional[List[Dict[str, object]]] = None
    if holdings_matrix is not None and holdings_cash_values is not None:
        latest_weights = holdings_matrix[-1]
        max_weights = holdings_matrix.max(axis=0)
        mean_weights = holdings_matrix.mean(axis=0)
        positive_indices = np.flatnonzero(max_weights > 1e-10)
        if positive_indices.size:
            importance = max_weights * 0.55 + mean_weights * 0.30 + latest_weights * 0.15
            ranked_indices = positive_indices[np.argsort(importance[positive_indices])[::-1]]
            selected_indices = ranked_indices[:track_top_n].tolist()
        else:
            selected_indices = []
        holdings_evolution = [
            {
                "code": reverse_index[idx],
                "data": [
                    {"date": holdings_dates[i], "weight": round(float(weight), 6)}
                    for i, weight in enumerate(holdings_matrix[:, idx])
                ],
            }
            for idx in selected_indices
        ]

        if selected_indices:
            other_values = holdings_matrix.sum(axis=1) - holdings_matrix[:, selected_indices].sum(axis=1)
        else:
            other_values = holdings_matrix.sum(axis=1)
        np.maximum(other_values, 0.0, out=other_values)
        other_data = [
            {"date": holdings_dates[i], "weight": round(float(weight), 6)}
            for i, weight in enumerate(other_values)
        ]
        if np.any(other_values > 0.0001):
            holdings_evolution.append({"code": "其他", "data": other_data})

        if np.any(holdings_cash_values > 0.0001):
            holdings_evolution.append({
                "code": "现金",
                "data": [
                    {"date": holdings_dates[i], "weight": round(float(cash), 6)}
                    for i, cash in enumerate(holdings_cash_values)
                ],
            })

    return nav_series, applied_rebalances, cost_log, holdings_evolution

def compute_all_mode_navs(
    returns_array: np.ndarray,
    symbol_index: Dict[str, int],
    reverse_index: Dict[int, str],
    trading_dates: pd.DatetimeIndex,
    plans: List[CleanPlan],
    primary_rebalance_dates: List[pd.Timestamp],
    comparison_rebalance_dates: Dict[str, List[pd.Timestamp]],
    commission_rate: float = 0.0,
    stamp_duty_rate: float = 0.0,
    slippage_rate: float = 0.0,
    daily_rf_rate: float = 0.0,
    track_top_n: int = 0,
) -> Tuple[pd.Series, List[str], List[Dict[str, object]], Optional[List[Dict[str, object]]], Dict[str, pd.Series]]:
    """Compute primary NAV (with full details) + comparison NAVs in a single pass.

    Returns (primary_nav, applied_rebalances, cost_log, holdings_evolution, comparison_navs).
    """
    date_count = len(trading_dates)
    symbol_count = len(symbol_index)
    cost_rate = 2.0 * commission_rate + stamp_duty_rate + 2.0 * slippage_rate
    has_cost = cost_rate > 0.0

    date_strs = [str(dt.date()) for dt in trading_dates]

    # ── Holdings tracking (primary only) ──
    do_track = track_top_n > 0
    holdings_dates: List[str] = []
    holdings_matrix: Optional[np.ndarray] = None
    holdings_cash_values: Optional[np.ndarray] = None
    if do_track:
        holdings_dates = date_strs
        holdings_matrix = np.empty((date_count, symbol_count), dtype=np.float64)
        holdings_cash_values = np.empty(date_count, dtype=np.float64)

    # ── Build plan vectors once ──
    plan_vectors = [
        (plan.effective_date,
         build_plan_weight_vector(plan.weight_map, symbol_index, symbol_count),
         plan.cash_weight)
        for plan in plans
    ]

    first_trade_day = pd.Timestamp(trading_dates[0])

    def _resolve_active_plan_idx(for_date: pd.Timestamp) -> int:
        idx = 0
        for i, (eff, _, _) in enumerate(plan_vectors):
            if eff <= for_date:
                idx = i
            else:
                break
        return idx

    def _build_schedule(rebalance_dates: List[pd.Timestamp]) -> Dict[int, Tuple[np.ndarray, float]]:
        schedule: Dict[int, Tuple[np.ndarray, float]] = {}
        active_idx = 0
        for rd in sorted(set(rebalance_dates)):
            pos = trading_dates.searchsorted(rd)
            if pos >= len(trading_dates) or trading_dates[pos] != rd:
                continue
            if pos == 0:
                continue
            while active_idx + 1 < len(plan_vectors) and plan_vectors[active_idx + 1][0] <= rd:
                active_idx += 1
            schedule[pos] = (plan_vectors[active_idx][1], plan_vectors[active_idx][2])
        return schedule

    primary_schedule = _build_schedule(primary_rebalance_dates)
    comp_schedules = {mode: _build_schedule(dates) for mode, dates in comparison_rebalance_dates.items()}
    comparison_modes = list(comparison_rebalance_dates.keys())

    # ── Initialize primary state ──
    active_plan_idx = _resolve_active_plan_idx(first_trade_day)
    primary_weights = plan_vectors[active_plan_idx][1].copy()
    primary_cash = plan_vectors[active_plan_idx][2]
    primary_nav = np.empty(date_count, dtype=np.float64)
    primary_nav[0] = 1.0
    applied_rebalances = [str(first_trade_day.date())]
    cost_log: List[Dict[str, object]] = []
    position_values = np.empty(symbol_count, dtype=np.float64)
    weight_delta = np.empty(symbol_count, dtype=np.float64) if has_cost else None

    if holdings_matrix is not None:
        holdings_matrix[0] = primary_weights
        holdings_cash_values[0] = primary_cash  # type: ignore[union-attr]

    # ── Initialize comparison state ──
    comp_weights: Dict[str, np.ndarray] = {}
    comp_cash: Dict[str, float] = {}
    comp_nav: Dict[str, np.ndarray] = {}
    for mode in comparison_modes:
        comp_weights[mode] = primary_weights.copy()
        comp_cash[mode] = primary_cash
        comp_nav[mode] = np.empty(date_count, dtype=np.float64)
        comp_nav[mode][0] = 1.0

    # Reusable buffer for comparison drift
    comp_pv = np.empty(symbol_count, dtype=np.float64)

    # ── Day-by-day simulation ──
    for idx in range(1, date_count):
        today_ret = returns_array[idx]

        # --- Primary ---
        equity_ret = float(np.dot(primary_weights, today_ret))
        portfolio_ret = equity_ret + primary_cash * daily_rf_rate
        nav_growth = 1.0 + portfolio_ret
        primary_nav[idx] = primary_nav[idx - 1] * nav_growth

        # Drift primary weights
        np.multiply(primary_weights, 1.0 + today_ret, out=position_values)
        np.maximum(position_values, 0.0, out=position_values)
        total_value = float(position_values.sum()) + primary_cash
        if total_value > 0.0:
            np.divide(position_values, total_value, out=primary_weights)
            primary_cash = primary_cash * (1.0 + daily_rf_rate) / nav_growth

        # Rebalance primary
        if idx in primary_schedule:
            target_w, target_c = primary_schedule[idx]
            if has_cost and weight_delta is not None:
                np.subtract(primary_weights, target_w, out=weight_delta)
                np.maximum(weight_delta, 0.0, out=weight_delta)
                sells = float(weight_delta.sum()) + max(0.0, primary_cash - target_c)
                if sells > 0.0:
                    cost = sells * cost_rate
                    primary_nav[idx] *= (1.0 - cost)
                    cost_log.append({
                        "date": date_strs[idx],
                        "turnover": round(sells * 100, 4),
                        "cost": round(cost * 100, 6),
                        "cost_rate": round(cost_rate * 100, 4),
                    })
                    position_values *= (1.0 - cost)
            np.copyto(primary_weights, target_w)
            primary_cash = target_c
            applied_rebalances.append(date_strs[idx])

        # Record primary holdings
        if holdings_matrix is not None:
            holdings_matrix[idx] = primary_weights
            holdings_cash_values[idx] = primary_cash  # type: ignore[union-attr]

        # --- Comparisons ---
        for mode in comparison_modes:
            w = comp_weights[mode]
            c = comp_cash[mode]
            eq_ret = float(np.dot(w, today_ret))
            pf_ret = eq_ret + c * daily_rf_rate
            comp_growth = 1.0 + pf_ret
            comp_nav[mode][idx] = comp_nav[mode][idx - 1] * comp_growth

            # Drift
            np.multiply(w, 1.0 + today_ret, out=comp_pv)
            np.maximum(comp_pv, 0.0, out=comp_pv)
            tv = float(comp_pv.sum()) + c
            if tv > 0.0:
                np.divide(comp_pv, tv, out=w)
                comp_cash[mode] = c * (1.0 + daily_rf_rate) / comp_growth

            # Rebalance (no cost tracking for comparisons)
            if idx in comp_schedules[mode]:
                target_w, target_c = comp_schedules[mode][idx]
                np.copyto(w, target_w)
                comp_cash[mode] = target_c

    primary_series = pd.Series(primary_nav, index=trading_dates, name="nav")
    comparison_series = {
        mode: pd.Series(comp_nav[mode], index=trading_dates, name=f"nav_{mode}")
        for mode in comparison_modes
    }

    # ── Build holdings evolution (columnar) ──
    holdings_evolution: Optional[List[Dict[str, object]]] = None
    if holdings_matrix is not None and holdings_cash_values is not None:
        latest_weights = holdings_matrix[-1]
        max_weights = holdings_matrix.max(axis=0)
        mean_weights = holdings_matrix.mean(axis=0)
        positive_indices = np.flatnonzero(max_weights > 1e-10)
        if positive_indices.size:
            importance = max_weights * 0.55 + mean_weights * 0.30 + latest_weights * 0.15
            ranked_indices = positive_indices[np.argsort(importance[positive_indices])[::-1]]
            selected_indices = ranked_indices[:track_top_n].tolist()
        else:
            selected_indices = []
        holdings_evolution = [
            {
                "code": reverse_index[idx],
                "dates": holdings_dates,
                "weights": [round(float(w), 6) for w in holdings_matrix[:, idx]],
            }
            for idx in selected_indices
        ]

        if selected_indices:
            other_values = holdings_matrix.sum(axis=1) - holdings_matrix[:, selected_indices].sum(axis=1)
        else:
            other_values = holdings_matrix.sum(axis=1)
        np.maximum(other_values, 0.0, out=other_values)
        if np.any(other_values > 0.0001):
            holdings_evolution.append({
                "code": "其他",
                "dates": holdings_dates,
                "weights": [round(float(w), 6) for w in other_values],
            })

        if np.any(holdings_cash_values > 0.0001):
            holdings_evolution.append({
                "code": "现金",
                "dates": holdings_dates,
                "weights": [round(float(c), 6) for c in holdings_cash_values],
            })

    return primary_series, applied_rebalances, cost_log, holdings_evolution, comparison_series




def compute_metrics(nav_series: pd.Series, risk_free_rate: float) -> Dict[str, object]:
    if nav_series.empty:
        raise ValueError("净值序列为空")

    daily_returns = nav_series.pct_change().dropna()
    total_return = float(nav_series.iloc[-1] / nav_series.iloc[0] - 1.0)

    if len(daily_returns) > 0:
        annual_return = float((nav_series.iloc[-1] / nav_series.iloc[0]) ** (252 / len(daily_returns)) - 1.0)
        annual_volatility = float(daily_returns.std(ddof=0) * np.sqrt(252))
        win_rate = float((daily_returns > 0).mean())
        downside = daily_returns[daily_returns < 0]
        downside_vol = float(downside.std(ddof=0) * np.sqrt(252)) if len(downside) > 1 else 0.0
    else:
        annual_return = 0.0
        annual_volatility = 0.0
        win_rate = 0.0
        downside_vol = 0.0

    sharpe_ratio: float
    if annual_volatility > 0:
        sharpe_ratio = float((annual_return - risk_free_rate) / annual_volatility)
    else:
        sharpe_ratio = 0.0

    sortino_ratio: float
    if downside_vol > 0:
        sortino_ratio = float((annual_return - risk_free_rate) / downside_vol)
    else:
        sortino_ratio = 0.0

    rolling_max = nav_series.cummax()
    drawdown = nav_series / rolling_max - 1.0
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0

    calmar_ratio: float
    if max_drawdown < 0:
        calmar_ratio = float(annual_return / abs(max_drawdown))
    else:
        calmar_ratio = 0.0

    # Max drawdown period
    dd_start: Optional[str] = None
    dd_trough: Optional[str] = None
    dd_recovery: Optional[str] = None
    dd_duration_days: Optional[int] = None
    if not drawdown.empty and max_drawdown < 0:
        trough_idx = int(drawdown.argmin())
        dd_trough = str(pd.Timestamp(drawdown.index[trough_idx]).date())
        before_trough = drawdown.iloc[: trough_idx + 1]
        zero_mask = before_trough == 0
        if zero_mask.any():
            start_idx = before_trough.index.get_loc(before_trough[zero_mask].index[-1])
            dd_start = str(pd.Timestamp(drawdown.index[start_idx]).date())
        else:
            dd_start = str(pd.Timestamp(drawdown.index[0]).date())
        after_trough = drawdown.iloc[trough_idx:]
        recovery_mask = after_trough == 0
        if recovery_mask.any():
            recovery_idx = after_trough.index.get_loc(after_trough[recovery_mask].index[0])
            dd_recovery = str(pd.Timestamp(drawdown.index[trough_idx + recovery_idx]).date())
            dd_duration_days = (drawdown.index[trough_idx + recovery_idx] - drawdown.index[trough_idx]).days

    return {
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "annual_volatility": float(annual_volatility),
        "sharpe_ratio": float(sharpe_ratio),
        "sortino_ratio": float(sortino_ratio),
        "calmar_ratio": float(calmar_ratio),
        "max_drawdown": float(max_drawdown),
        "max_drawdown_start": dd_start,
        "max_drawdown_trough": dd_trough,
        "max_drawdown_recovery": dd_recovery,
        "max_drawdown_duration_days": dd_duration_days,
        "win_rate": float(win_rate),
    }


def compute_periodic_returns(nav_series: pd.Series) -> Dict[str, object]:
    """Compute annual and monthly return decompositions."""
    if nav_series.empty or len(nav_series) < 2:
        return {"annual": [], "monthly": []}

    daily_returns = nav_series.pct_change().dropna()
    if daily_returns.empty:
        return {"annual": [], "monthly": []}

    monthly_returns = daily_returns.resample("M").apply(
        lambda x: (1 + x).prod() - 1 if len(x) > 0 else np.nan
    ).dropna()

    monthly_data: List[Dict[str, object]] = []
    for dt, ret in monthly_returns.items():
        monthly_data.append({
            "year": int(dt.year),
            "month": int(dt.month),
            "return": float(round(ret, 6)),
        })

    annual_returns = daily_returns.resample("Y").apply(
        lambda x: (1 + x).prod() - 1 if len(x) > 0 else np.nan
    ).dropna()

    annual_data: List[Dict[str, object]] = []
    for dt, ret in annual_returns.items():
        annual_data.append({
            "year": int(dt.year),
            "return": float(round(ret, 6)),
        })

    return {"annual": annual_data, "monthly": monthly_data}


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
    elif mode == "semiannual":
        marker = None
        for dt in trading_dates:
            half = 1 if dt.month <= 6 else 2
            key = (dt.year, half)
            if key != marker:
                periodic.append(pd.Timestamp(dt))
                marker = key
    elif mode == "custom":
        if not custom_dates:
            pass
        else:
            positions = trading_dates.searchsorted(
                pd.DatetimeIndex([pd.Timestamp(raw) for raw in custom_dates])
            )
            seen = set()
            for pos in positions:
                if pos < len(trading_dates):
                    dt = pd.Timestamp(trading_dates[pos])
                    if dt not in seen:
                        periodic.append(dt)
                        seen.add(dt)
    periodic.sort()
    return periodic


def build_backtest_rebalance_dates(
    trading_dates: pd.DatetimeIndex,
    plans: List[CleanPlan],
    mode: str,
    custom_dates: List[date],
) -> List[pd.Timestamp]:
    all_rebalance_dates = {pd.Timestamp(trading_dates[0])}
    for plan in plans:
        all_rebalance_dates.add(plan.effective_date)
    for dt in build_periodic_rebalance_dates(trading_dates, mode, custom_dates):
        all_rebalance_dates.add(dt)
    return sorted(all_rebalance_dates)


def build_comparison_rebalance_modes(primary_mode: str) -> List[str]:
    if primary_mode == "custom":
        return STANDARD_REBALANCE_MODES.copy()
    return [mode for mode in STANDARD_REBALANCE_MODES if mode != primary_mode]


def active_plan_for_date(plans: List[CleanPlan], current_date: pd.Timestamp) -> CleanPlan:
    active = plans[0]
    for plan in plans:
        if plan.effective_date <= current_date:
            active = plan
        else:
            break
    return active


def drop_unavailable_symbols(
    plans: List[CleanPlan],
    close_df: pd.DataFrame,
    allow_cash: bool = False,
) -> Tuple[List[CleanPlan], List[str]]:
    """Drop symbols with no usable quotes and recompute weights.

    Semantics:
    - allow_cash=True: keep remaining stock weights as-is; dropped weight becomes cash
      (together with any pre-existing cash gap under 100%).
    - allow_cash=False: renormalize remaining stocks to exactly 100%, cash=0.
    """
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
                filtered_components.append(dict(component))
                filtered_weights[code] = plan.weight_map[code]
            else:
                dropped.append(code)

        if dropped:
            warnings.append(
                f"{plan.effective_date.date()} 调仓计划中 {', '.join(dropped)} 无可用行情，已自动剔除"
                + ("，权重转入现金仓位" if allow_cash else "并重归一化")
            )

        if not filtered_weights:
            raise HTTPException(
                status_code=400,
                detail=f"{plan.effective_date.date()} 调仓计划无可用成分股，无法继续回测",
            )

        # When allow_cash, normalize_weight_map keeps the remaining equity total and
        # sets cash = 1 - equity_total, which automatically absorbs both the original
        # cash gap and any weight from dropped symbols.
        # When not allow_cash, remaining equities are renormalized to 100%.
        normalized_map, new_cash = normalize_weight_map(
            filtered_weights, decimals=4, allow_cash=allow_cash
        )
        for component in filtered_components:
            code = str(component["code"])
            component["weight"] = round(normalized_map[code] * 100, 4)

        clean.append(
            CleanPlan(
                effective_date=plan.effective_date,
                components=filtered_components,
                weight_map=normalized_map,
                cash_weight=new_cash,
            )
        )

    return clean, warnings


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
                cash_weight=plans[0].cash_weight,
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
            cash_weight=plan.cash_weight,
        )

    aligned.extend(mapped.values())
    if not aligned:
        raise HTTPException(status_code=400, detail="所有调仓计划均不在可交易区间内")
    aligned.sort(key=lambda x: x.effective_date)
    return aligned
