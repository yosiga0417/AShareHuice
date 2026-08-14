from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, getcontext
import time
from typing import Callable, Dict, List, Literal, Optional, Tuple

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, ValidationInfo, field_validator

getcontext().prec = 28

DEFAULT_BENCHMARK_CODES = ["000300", "000905", "000001"]
STANDARD_REBALANCE_MODES = ["monthly", "quarterly", "semiannual", "none"]
REBALANCE_MODE_LABELS = {
    "none": "仅按计划生效日",
    "monthly": "按月再平衡",
    "quarterly": "按季度再平衡",
    "semiannual": "按半年再平衡",
    "custom": "自定义日期",
}
FileSignature = Optional[Tuple[int, int]]

CODE_COLUMN_CANDIDATES = [
    "成份券代码", "成分券代码", "成份股代码", "成分股代码",
    "样本代码", "股票代码", "证券代码",
    "constituent code", "constituentcode", "sample code", "samplecode",
    "security code", "securitycode", "stock code", "stockcode",
    "symbol", "ticker", "代码", "code",
]

WEIGHT_COLUMN_CANDIDATES = [
    "权重(%)weight", "weight(%)", "weight%",
    "权重（%）", "权重(%)", "权重", "weight",
]

NAME_COLUMN_CANDIDATES = [
    "成份券名称", "成分券名称", "成份股名称", "成分股名称",
    "样本名称", "名称", "股票简称", "样本简称", "证券简称",
    "constituent name", "constituentname", "sample name", "samplename",
    "security name", "securityname", "stock name", "stockname", "name",
]

CODE_COLUMN_PREFERRED_KEYWORDS = ["成份", "成分", "券", "股", "样本", "constituent", "sample", "security", "stock"]
CODE_COLUMN_EXCLUDED_KEYWORDS = ["指数", "index", "日期", "date", "名称", "name", "英文", "eng", "交易所", "exchange", "权重", "weight"]
NAME_COLUMN_PREFERRED_KEYWORDS = ["成份", "成分", "券", "股", "样本", "简称", "constituent", "sample", "security", "stock"]
NAME_COLUMN_EXCLUDED_KEYWORDS = ["指数", "index", "日期", "date", "代码", "code", "英文", "eng", "交易所", "exchange", "权重", "weight"]
WEIGHT_COLUMN_PREFERRED_KEYWORDS = ["权重", "weight", "比重", "占比", "比例"]
WEIGHT_COLUMN_EXCLUDED_KEYWORDS = ["日期", "date", "代码", "code", "名称", "name", "英文", "eng", "交易所", "exchange"]
HEADER_SCAN_ROWS = 10


class ComponentInput(BaseModel):
    code: str
    weight: float
    name: Optional[str] = ""


class RebalancePlanInput(BaseModel):
    effective_date: date
    components: List[ComponentInput]

    @field_validator("components")
    @classmethod
    def validate_components_not_empty(cls, value: List[ComponentInput]) -> List[ComponentInput]:
        if not value:
            raise ValueError("调仓计划的成分股不能为空")
        return value


class BacktestRequest(BaseModel):
    start_date: date
    end_date: date
    rebalance_mode: Literal["none", "monthly", "quarterly", "semiannual", "custom"] = "monthly"
    custom_rebalance_dates: List[date] = Field(default_factory=list)
    plans: List[RebalancePlanInput]
    risk_free_rate: float = 0.02
    missing_data_policy: Literal["hold_cash"] = "hold_cash"
    benchmarks: List[str] = Field(default_factory=list)
    commission_rate: float = 0.0
    stamp_duty_rate: float = 0.0
    slippage_rate: float = 0.0
    allow_cash: bool = False

    @field_validator("end_date")
    @classmethod
    def validate_date_range(cls, end_date: date, info: ValidationInfo) -> date:
        start_date = info.data.get("start_date")
        if start_date is not None and end_date <= start_date:
            raise ValueError("回测结束日期必须晚于开始日期")
        return end_date

    @field_validator("plans")
    @classmethod
    def validate_plans_not_empty(cls, value: List[RebalancePlanInput]) -> List[RebalancePlanInput]:
        if not value:
            raise ValueError("至少需要一条调仓计划")
        return value


@dataclass
class CleanPlan:
    effective_date: pd.Timestamp
    components: List[Dict[str, object]]
    weight_map: Dict[str, float]  # 0~1
    cash_weight: float = 0.0  # 0~1, remainder when total equity weight < 1


ProgressCallback = Callable[[float, str, str, Optional[int], Optional[int]], None]
StageProgressCallback = Callable[[int, int, str], None]


@dataclass
class BacktestTaskState:
    task_id: str
    status: Literal["queued", "running", "cancelling", "completed", "failed", "cancelled"] = "queued"
    progress: float = 0.0
    stage: str = "queued"
    message: str = "任务已创建，等待执行。"
    current_step: Optional[int] = None
    total_steps: Optional[int] = None
    started_monotonic: float = field(default_factory=time.monotonic)
    updated_monotonic: float = field(default_factory=time.monotonic)
    finished_monotonic: Optional[float] = None
    result: Optional[Dict[str, object]] = None
    error: Optional[str] = None
    cancel_requested: bool = False
