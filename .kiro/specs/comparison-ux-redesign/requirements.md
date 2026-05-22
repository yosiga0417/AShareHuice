# Requirements Document

## Introduction

本特性针对「A 股自设指数净值回测工具」现有的「对比」功能进行交互与体验重做。当前实现（`saveCurrentToComparison` / `renderComparisonBar` / `refreshChart` 等）存在入口隐蔽、工作流不明确、命名无区分度、缺少参数快照、指标表无对比数据、缺少引导与容错等问题。本次重做的核心目标是：让「跨多次回测 / 跨多套调仓计划」的对比行为**显而易见、易于操作、结果直观**，并且保留与现有「调仓计划导入导出」一致的体验。

本文档只描述需求（做什么 / 行为应满足的可验证标准），不规定具体实现细节。

## Glossary

- **Backtest_Result**：一次完整回测产生的结果对象，至少包含 `nav`（逐日净值）、`applied_rebalance_dates`、`metrics`、`benchmark_nav`、`rebalance_holdings`、`periodic_returns`。
- **Current_Result**：用户最近一次成功执行的 Backtest_Result，即 `state.lastResult` 所指向的结果。
- **Backtest_Config**：执行一次回测所用的完整参数集合，包含 `plans`（所有调仓计划及其成分股与权重）、`start_date`、`end_date`、`rebalance_mode`、`custom_rebalance_dates`、`risk_free_rate`、`benchmarks`、`commission_rate`、`stamp_duty_rate`、`slippage_rate`、`allow_cash`。
- **Comparison_Snapshot**：保存到对比区的一条条目，封装 Backtest_Result 的可视化必要字段、完整 Backtest_Config 副本、用于展示的元信息（名称、创建时间、回测区间）和一个稳定 ID。
- **Comparison_Panel**：展示全部 Comparison_Snapshot 的面板（独立于基准栏），提供可见/选中、详情、重命名、恢复、删除、清空、导入导出等操作。
- **Comparison_Store**：Comparison_Snapshot 的前端持久化层，基于 `localStorage`，键为 `a_stock_backtest_comparisons_v1`。
- **Save_Comparison_Action**：用户将 Current_Result 连同当前 Backtest_Config 保存为一条新 Comparison_Snapshot 的动作。
- **Rename_Action**：用户修改某条 Comparison_Snapshot 显示名称的动作。
- **Restore_Action**：用户将某条 Comparison_Snapshot 内保存的 Backtest_Config 一键加载回当前配置区（侧边栏的调仓计划与参数区）的动作，用于复现或在此基础上微调后重新执行回测。
- **Delete_Action**：删除单条 Comparison_Snapshot 的动作。
- **Clear_All_Action**：删除全部 Comparison_Snapshot 的动作。
- **Chart_Renderer**：`renderChart` 所承担的职责实体，负责在同一张 ECharts 图上叠加当前回测、基准、以及被选中的 Comparison_Snapshot 的净值曲线。
- **Metrics_Comparison_Table**：结果区展示绩效指标的容器（对应 `#metricsContainer` 的扩展），需要在每个指标卡中横向对比当前回测与被选中的 Comparison_Snapshot。
- **Date_Alignment_Service**：负责在对比渲染时把 Comparison_Snapshot 的 nav 序列映射到「当前回测日期轴」上，并计算区间重叠情况。
- **First_Run_Guide**：首次使用或对比区为空时展示的说明 / 指引文案。
- **Import_Export_Service**：Comparison_Snapshot 集合的 JSON 导入导出通道，行为风格需与现有 `exportPlans / importPlans` 一致。
- **Accessibility_Layer**：对比相关 UI 的键盘操作、焦点管理与屏幕阅读器可达性规范的总称。
- **Comparison_Color_Palette**：为 Comparison_Snapshot 分配曲线与图例色块的调色板，需与基准色和当前回测色有足够区分度。

## Requirements

### Requirement 1：对比功能概念的显性化

**User Story:** 作为使用回测工具的投资研究者，我想从进入页面起就看懂「对比」功能的含义与操作路径，以便我不必依赖猜测或文档就能开始使用它。

#### Acceptance Criteria

1. THE Comparison_Panel SHALL 在结果区常驻显示，而不以「至少执行过一次回测」作为出现条件。
2. WHILE Comparison_Store 为空，THE Comparison_Panel SHALL 展示 First_Run_Guide 的空状态文案，说明对比功能的用途（跨多次回测比较净值与绩效）与四步操作顺序（配置 → 执行 → 保存 → 调整后再执行）。
3. THE Save_Comparison_Action 触发按钮 SHALL 在结果区使用包含「保存当前回测用于对比」语义的完整文字标签，而不是仅用图标或单字。
4. WHEN 用户尚未执行过任何一次回测（Current_Result 不存在），THE Save_Comparison_Action 按钮 SHALL 处于禁用态，并通过 `title` / `aria-label` / 可视提示说明「请先执行一次回测」。
5. WHEN 用户把指针悬停或聚焦到 Save_Comparison_Action 按钮上，THE Comparison_Panel SHALL 展示一条不超过两行的解释：本按钮会把本次回测的结果、调仓计划、回测参数一起保存到下方对比列表中。

### Requirement 2：保存对比快照时的元数据完整性

**User Story:** 作为进行多方案比较的研究者，我想让每条保存的对比条目都自带「当时的完整配置」，以便我日后能看懂每条曲线代表的方案，不会再出现一堆「按月 · 5月9日 14:23」无法区分的情况。

#### Acceptance Criteria

1. WHEN 用户触发 Save_Comparison_Action，THE Comparison_Store SHALL 将一条新的 Comparison_Snapshot 持久化，且该条目至少包含以下字段：稳定 ID、显示名称、创建时间、`nav`、`applied_rebalance_dates`、`metrics`、`benchmark_nav`、`rebalance_holdings`、`periodic_returns`、完整 Backtest_Config 副本、数据源标记（`data_source` 与 `missing_data_policy`）、回测实际起止日期（`nav` 的首尾日期）。
2. THE Backtest_Config 副本 SHALL 深拷贝 plans（含每条计划的 `effectiveDate` 与全部 `components`），且副本不随后续侧边栏编辑而变化。
3. IF Current_Result 不存在或其 `nav` 为空，THEN THE Save_Comparison_Action SHALL 拒绝保存并向状态栏输出错误信息「没有可保存的回测结果，请先执行一次回测」。
4. IF Comparison_Snapshot 的序列化后大小将使 Comparison_Store 的总占用超过 4MB，THEN THE Comparison_Store SHALL 拒绝本次保存并向用户提示「对比存储空间已满，请先清理部分历史对比」。
5. WHERE 用户在高级设置中修改了交易成本或 `allow_cash` 选项，THE Comparison_Snapshot SHALL 保留本次保存时刻这些选项的取值，而不是读取保存之后的当前值。

### Requirement 3：自动命名与用户自定义命名

**User Story:** 作为一次研究中可能跑十几次方案的研究者，我希望系统给每条保存的对比一个一眼能区分的名字，同时允许我自己改名。

#### Acceptance Criteria

1. WHEN Save_Comparison_Action 保存一条新的 Comparison_Snapshot 且用户未手动输入名称，THE Comparison_Panel SHALL 生成一个默认显示名称，名称由以下元素拼接并能反映差异：序号前缀（`#N`）、调仓频率简写、回测区间（`YYYY-MM-DD ~ YYYY-MM-DD`）、成分股数量或计划数（例：`#3 · 按月 · 2022-01-01~2024-12-31 · 3计划/42股`）。
2. IF 新建的 Comparison_Snapshot 的默认名称与已存在的某条 Comparison_Snapshot 同名，THEN THE Comparison_Panel SHALL 在末尾追加「· (副本 2)」「· (副本 3)」等后缀以确保唯一性。
3. WHEN 用户触发 Rename_Action，THE Comparison_Panel SHALL 在该条目就地提供可编辑输入框（支持键盘 Enter 提交、Esc 取消），并允许 1 至 40 个字符。
4. IF 用户提交的新名称长度超过 40 字符或去除首尾空白后为空，THEN THE Rename_Action SHALL 拒绝保存并在原位置提示长度或空名错误。
5. WHEN 用户完成 Rename_Action，THE Comparison_Store SHALL 立即持久化新名称，同时 Chart_Renderer 与 Metrics_Comparison_Table 的图例与列标题 SHALL 在不重新执行回测的情况下同步更新。

### Requirement 4：对比条目的管理操作

**User Story:** 作为需要回看历史方案的研究者，我希望每条对比都能查看详情、改名、一键恢复到当前配置、或删除，每个操作都要能通过键盘完成。

#### Acceptance Criteria

1. WHEN 用户点击或按 Enter 键激活某条 Comparison_Snapshot 的「详情」入口，THE Comparison_Panel SHALL 展示以下内容：名称、创建时间、回测区间、调仓频率、交易成本三项、`allow_cash` 状态、基准列表、每条调仓计划的生效日与成分股（代码、名称、权重）。
2. THE Comparison_Panel SHALL 在详情视图中展示该条目的全部 metrics，字段与 `metricsConfig` 一致，数值格式与当前回测区的格式相同。
3. WHEN 用户触发 Restore_Action，THE 侧边栏 SHALL 用该 Comparison_Snapshot 的 Backtest_Config 替换当前调仓计划区和参数区的全部对应字段，并触发一次 `saveToStorage`。
4. IF Restore_Action 执行时侧边栏存在未保存到 localStorage 的待编辑字段，THEN THE Comparison_Panel SHALL 先弹出一次确认对话框，说明「当前侧边栏配置将被覆盖」，由用户明确确认后再执行替换。
5. WHEN 用户触发 Delete_Action，THE Comparison_Store SHALL 在删除前展示一次二次确认（显示待删除条目的名称），用户确认后移除该条目并刷新 Comparison_Panel、Chart_Renderer、Metrics_Comparison_Table。
6. WHEN 用户触发 Clear_All_Action，THE Comparison_Store SHALL 先展示一次二次确认（显示「将删除全部 N 条对比」），用户确认后清空 Comparison_Store 并刷新相关视图。
7. THE Clear_All_Action 的触发控件 SHALL 与单条删除按钮在视觉上分层：Clear_All_Action 采用次要（非危险）底色 + 明确文字（例如「清空全部对比」），单条删除采用行内小号危险按钮，两者不得在同一水平线上相邻排列以防误点。
8. WHERE Comparison_Store 中存在任意一条 Comparison_Snapshot，THE Comparison_Panel SHALL 为每条条目同时提供：显示/隐藏（复选框）、重命名、详情、恢复、删除五个入口，且每个入口均可通过 Tab 键到达。

### Requirement 5：对比结果的图表可视化

**User Story:** 作为看图做决策的研究者，我希望多条对比曲线在同一张净值图上颜色清晰、图例易读，并且知道每条线对应的是哪条保存的对比。

#### Acceptance Criteria

1. WHEN 至少一条 Comparison_Snapshot 被选中显示，THE Chart_Renderer SHALL 为每条被选中的条目绘制一条折线，并将该条目的显示名称作为图例文字。
2. THE Comparison_Color_Palette SHALL 至少提供 8 种可区分颜色，且每种颜色与当前回测主线（`#0057b8`）及默认基准色（`#f59e0b`、`#10b981`、`#8b5cf6`）保持可视差异。
3. THE Chart_Renderer SHALL 将 Comparison_Snapshot 曲线以与「当前回测主线」「基准线」在线型或粗细上明显不同的样式（例：虚线或半透明）绘制，以便用户一眼分辨「哪条是当前、哪条是对比、哪条是基准」。
4. WHEN 用户改变某条 Comparison_Snapshot 的显示名称或图例颜色指派，THE Chart_Renderer SHALL 在不重新执行回测的情况下同步刷新图例与 tooltip。
5. WHEN 用户在 Comparison_Panel 中切换某条对比条目的显示/隐藏复选框，THE Chart_Renderer SHALL 在 300ms 内完成对应曲线的显示或移除，不要求重新执行回测。
6. THE Chart_Renderer SHALL 为 Comparison_Snapshot 曲线的 tooltip 展示：名称、当日净值、相对起点的累计收益率；若该日期在该 Snapshot 的原 nav 中不存在，则在 tooltip 中明确显示「该日期无数据」。

### Requirement 6：对比结果的指标表对比

**User Story:** 作为看数字做决策的研究者，我希望指标卡能在同一个卡片里并排看到当前回测与选中的对比条目，而不只是图上画几条线。

#### Acceptance Criteria

1. WHEN 至少一条 Comparison_Snapshot 被选中显示，THE Metrics_Comparison_Table SHALL 在每个指标卡（total_return、annual_return、annual_volatility、sharpe_ratio、sortino_ratio、calmar_ratio、max_drawdown、win_rate）中展示一列「当前」数值与每条被选中 Snapshot 的同名指标数值。
2. THE Metrics_Comparison_Table SHALL 按 `metricsConfig` 中定义的格式（`pct` 或 `num`）渲染 Snapshot 指标值，并沿用 `metricColorClass` 的红绿配色规则。
3. IF 某条被选中的 Comparison_Snapshot 缺失某个指标字段（例如旧版本快照没有 `sortino_ratio`），THEN THE Metrics_Comparison_Table SHALL 在对应位置显示「-」而不是 `NaN` 或空白。
4. THE Metrics_Comparison_Table SHALL 在每一行对比值旁边显示该指标相对「当前」的差值（对百分数指标用百分点差，例：`+2.15pp`；对比率指标用绝对差，例：`-0.12`），并对优劣采用与指标语义一致的红绿配色（与 `metricColorClass` 规则保持一致，例如最大回撤的「更浅」为优）。
5. WHEN 用户切换被选中对比条目的集合，THE Metrics_Comparison_Table SHALL 在不重新执行回测的情况下同步刷新。
6. THE Metrics_Comparison_Table SHALL 明确区分「当前回测」行与「历史对比」行的视觉层级，使「当前」始终处于每个指标卡的第一列或最上一行。

### Requirement 7：日期范围对齐与容错

**User Story:** 作为可能比较不同时间段方案的研究者，我希望看懂「对比曲线为什么只画到某个日期」，而不是看见一条半截断线却毫无提示。

#### Acceptance Criteria

1. THE Date_Alignment_Service SHALL 以「当前回测的 `nav` 日期序列」作为渲染坐标轴。
2. WHEN 某条 Comparison_Snapshot 的日期区间与当前回测日期区间部分重叠，THE Date_Alignment_Service SHALL 仅在重叠部分绘制曲线，非重叠部分留空（null），不做外推。
3. IF 某条 Comparison_Snapshot 的日期区间与当前回测日期区间完全不重叠，THEN THE Comparison_Panel SHALL 在该条目的图例或标签处显示「与当前回测日期不重叠」警示，并在 Chart_Renderer 的图上隐藏该曲线。
4. WHERE 重叠区间不等于 Comparison_Snapshot 的原区间，THE Chart_Renderer SHALL 在 tooltip 或图例 hover 处提示「仅显示重叠区间 YYYY-MM-DD ~ YYYY-MM-DD」。
5. THE Date_Alignment_Service SHALL 以「重叠区间的首个共同交易日」为基准将 Snapshot 的净值归一化到 1.0，以保证曲线形状与当前回测主线可比较；若原 Snapshot 的 `nav` 在该基准日缺值，则使用下一个存在值并在图例处注明「基准日已前移至 YYYY-MM-DD」。

### Requirement 8：首次使用引导与空状态文案

**User Story:** 作为第一次打开工具的新用户，我希望系统用一两行清晰的话告诉我「对比」该怎么用，而不是看到一个神秘的 ⊕ 符号。

#### Acceptance Criteria

1. WHILE Comparison_Store 为空，THE Comparison_Panel SHALL 显示不超过三行的使用说明，至少包含：本功能的目的（跨多次回测对比净值与绩效）、触发路径（先执行回测，再点击「保存当前回测用于对比」）、可以做什么（在图表和指标表中看到多条线/多列数据）。
2. WHEN 用户第一次成功执行回测且 Comparison_Store 为空，THE First_Run_Guide SHALL 在 Save_Comparison_Action 按钮附近显示一次性气泡提示（或 inline hint），说明「点这里把本次结果保存为对比基线」。
3. WHEN 用户关闭 First_Run_Guide 气泡或保存了任意一条 Comparison_Snapshot，THE First_Run_Guide SHALL 在本浏览器上持久化「已关闭」标记，之后不再弹出。
4. THE First_Run_Guide 的「已关闭」标记 SHALL 使用独立于业务数据的 `localStorage` 键，且用户清空 Comparison_Store 不会使其重置。

### Requirement 9：持久化、导入导出

**User Story:** 作为跨设备或与他人共享方案的研究者，我希望对比列表能像调仓计划一样导出为 JSON，也能从 JSON 导入。

#### Acceptance Criteria

1. THE Comparison_Store SHALL 在每次 Save_Comparison_Action、Rename_Action、Delete_Action、Clear_All_Action 后将当前 Comparison_Snapshot 集合同步写入 `localStorage`，键名沿用 `a_stock_backtest_comparisons_v1`。
2. WHEN 用户触发「导出对比」Action，THE Import_Export_Service SHALL 生成一个 JSON 文件，内容包含版本号、导出时间戳、全部 Comparison_Snapshot 的完整字段（含 Backtest_Config 副本），文件名格式为 `backtest_comparisons_YYYY-MM-DD.json`。
3. WHEN 用户触发「导入对比」Action 并选择一个有效 JSON 文件，THE Import_Export_Service SHALL 将文件中的条目追加到当前 Comparison_Store（而非替换），并在追加过程中重新分配稳定 ID 以避免与现有条目冲突。
4. IF 被导入的 JSON 文件版本号与当前不兼容、或缺少必填字段（如 `nav`、`plans`），THEN THE Import_Export_Service SHALL 拒绝导入并在状态栏输出具体错误信息（包含缺失字段名）。
5. WHILE 浏览器页面重新加载，THE Comparison_Panel SHALL 从 `localStorage` 恢复全部 Comparison_Snapshot 及其显示顺序，且曲线在图表上的颜色分配 SHALL 与刷新前一致（不随机重排）。
6. WHERE Comparison_Snapshot 的总数超过 20 条，THE Comparison_Panel SHALL 在保存时先警示用户「对比过多会显著降低图表与指标表的可读性」，并允许用户继续保存。

### Requirement 10：可访问性与键盘操作

**User Story:** 作为依赖键盘或辅助技术的用户，我希望对比功能的所有操作都能被键盘触达，也能被屏幕阅读器理解。

#### Acceptance Criteria

1. THE Comparison_Panel 的所有交互控件（显示/隐藏复选框、重命名、详情、恢复、删除、清空、保存对比、导入、导出）SHALL 通过 Tab 键可达，且每个控件 SHALL 具有可见的焦点样式。
2. THE Save_Comparison_Action 按钮 SHALL 具有 `aria-label` 属性，完整描述其行为（例：「保存当前回测结果与配置用于对比」）。
3. THE Comparison_Panel 的每条 Comparison_Snapshot SHALL 作为一个语义分组（例：`role="group"` 或 `<li>` 结构），其可见标签 SHALL 通过 `aria-labelledby` 指向条目名称元素。
4. WHEN 某条 Comparison_Snapshot 被切换为显示或隐藏，THE Accessibility_Layer SHALL 通过 `aria-live="polite"` 区域播报一条消息（例：「已显示对比：方案 A」或「已隐藏对比：方案 A」）。
5. WHEN 用户在重命名输入框中按 Esc，THE Rename_Action SHALL 放弃改动并将焦点返还到被重命名的条目上；按 Enter 提交后焦点 SHALL 返回条目上。
6. IF 用户即将触发 Delete_Action、Clear_All_Action、Restore_Action 中任一会导致不可撤销变更的操作，THEN THE Accessibility_Layer SHALL 以对话框方式确认（而不是仅 `window.confirm`），对话框 SHALL 具有 `role="dialog"` / `aria-modal="true"`，首个聚焦元素为「取消」按钮。

### Requirement 11：错误处理与降级

**User Story:** 作为对工具稳定性有要求的用户，我希望对比功能在数据异常、存储失败或旧版本数据不完整时，能给我清楚的提示并继续可用，而不是整个结果区白屏。

#### Acceptance Criteria

1. IF Comparison_Store 在读取 `localStorage` 时抛出异常或反序列化失败，THEN THE Comparison_Panel SHALL 视为 Comparison_Store 为空，并在状态栏提示「对比数据读取失败，已重置为空列表」，同时保留原始 `localStorage` 值不覆盖。
2. IF 某条历史 Comparison_Snapshot 缺少新增字段（如 `Backtest_Config` 副本），THEN THE Comparison_Panel SHALL 仍允许在图上显示曲线、在指标表显示指标，但 Restore_Action 按钮 SHALL 置灰并提示「该对比为旧版本，不支持一键恢复参数」。
3. IF Save_Comparison_Action 在写入 `localStorage` 时抛出配额超限异常，THEN THE Comparison_Store SHALL 回滚本次新增并在状态栏提示「浏览器存储空间不足，请先清理部分对比或导出后再重试」。
4. WHEN Chart_Renderer 接收到空 `nav` 的 Comparison_Snapshot（历史数据或导入数据异常），THE Chart_Renderer SHALL 跳过该条曲线并在 Comparison_Panel 的对应条目处显示「无可绘制数据」提示。
