# A股自设指数净值回测工具

## 功能概览
- 成分股管理：支持 `xls/xlsx/csv` 导入、手动增删改、一键等权分配。
- 股票代码处理：全链路按 6 位字符串处理（保留前导零，如 `002879`）。
- 调仓机制：支持多条调仓计划（每条可独立配置成分股和权重），并可叠加按月/按季度/按半年/自定义日期再平衡。
- 回测引擎：基于 AKShare 日线（前复权）计算净值曲线。
- 指标输出：区间总收益、年化收益、年化波动、夏普比率、最大回撤、胜率。
- 缺失数据策略：当日无行情（停牌/缺失）按当日收益 0 处理（资金等效留在该头寸）。
- 进度反馈：执行回测时显示成分股/基准拉取进度条，并实时显示当前耗时。

## 本地运行
1. 安装依赖：
```bash
pip3 install -r requirements.txt
```

2. 启动后端服务：
```bash
python3 backend.py
```

3. 打开前端页面：
- 直接双击打开 `index.html`，或通过任意静态服务器打开。
- 页面中后端地址保持默认：`http://127.0.0.1:8000`。

4. 使用流程：
- 上传 `xls/xlsx/csv` 文件，点击“解析”。
- 将解析结果应用到某条调仓计划，或手动编辑成分股与权重；如部分成分股权重为空，可点击“一键分配剩余权重”补足到 100%。
- 设置回测起止日期与调仓频率，点击“执行回测”。

## 一键启动
项目根目录新增了 `start.sh`，会自动完成以下步骤：
- 创建 `.venv` 虚拟环境
- 按需安装 `requirements.txt` 依赖
- 启动 FastAPI 后端：`http://127.0.0.1:8000`
- 启动静态页面服务：`http://127.0.0.1:8080/index.html`
- 自动打开浏览器

使用方式：
```bash
chmod +x start.sh
./start.sh
```

可选环境变量：
```bash
BACKEND_PORT=9000 FRONTEND_PORT=8088 ./start.sh
AUTO_OPEN_BROWSER=0 ./start.sh
BACKTEST_FETCH_WORKERS=12 ./start.sh
```

说明：
- 首次运行会创建 `.venv` 并安装依赖，耗时取决于网络环境。
- 运行期间按 `Ctrl+C` 会同时关闭后端和静态服务。
- AKShare 取数仍然需要外网连接。
- `BACKTEST_FETCH_WORKERS` 可控制 AKShare 行情拉取并发数；未设置时会按机器核数自动给出上限。

## 接口说明（后端）
- `GET /api/health`：健康检查
- `POST /api/parse-components`：解析 Excel/CSV 成分股（`multipart/form-data`, 字段名 `file`）
- `POST /api/backtest`：同步执行回测（JSON）
- `POST /api/backtest/tasks`：创建异步回测任务（JSON）
- `GET /api/backtest/tasks/{task_id}`：获取异步回测任务进度、耗时和结果

## 备注
- 网络环境需要能访问 AKShare 对应行情源。
- Excel/CSV 导入会保留原始权重数值；若来源文件因四舍五入导致合计略超 100%，解析预览会自动按比例规整到 100%。回测时仍会使用高精度修正保证总和精确。
