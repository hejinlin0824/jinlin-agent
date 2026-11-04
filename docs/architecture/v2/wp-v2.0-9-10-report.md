# WP-V2.0-9/10 实施报告 — 可观测性 与 基准评估（Milestone F 合并交付）

> 分支 `feature/v2-wp1-task-ledger`。实现提交 `d126b38`；基线 `9d0988d`（WP-7/8 后）。两个 WP 同属 Milestone F，一次提交交付，报告合并。

## 1. 基线
- Branch：`feature/v2-wp1-task-ledger`；Base：`9d0988d`；Head：`d126b38`

## 2. 目标
WP-9：让用户理解 Agent 在做什么、何时介入（v2 §14-WP9）。WP-10：用真实探针证明架构有价值（v2 §14-WP10）：任务集扩展、指标、发布。

## 3. 实际改动
**WP-9**：`workbench.py`（stdlib `ThreadingHTTPServer`，零依赖，绑定 127.0.0.1）：`GET /`（内嵌极简 HTML，3s 自动刷新，一切展示读自账本与任务事件流——**不发明实时进度**）、`GET /api/tasks`、`GET /api/tasks/<id>`（任务+workers+messages+事件尾 50）、`POST /api/tasks/<id>/cancel`、`POST /api/tasks/<id>/answer`（恢复 waiting worker，异步续跑）。**HTTP 审批有意不暴露**（fail-closed 审批被 HTTP 化即绕过，v2 §9.6）；cancel/answer 仅在附着运行时可用（CLI `serve` 为纯只读模式）。CLI：`jinlin-agent serve --port`。
**WP-10**：4 个新离线探针（**22 个总计**）：P7 并行 worker 真实重叠（barrier 证明）、P8 DAG 严格按依赖顺序完成、P9 能力越界在任何副作用前被拒（仓库自我授权内容无效）、P10 预算耗尽失败且不烧重试；`eval --suite v2`；`ledger.metrics()`（按根任务：完成/失败/取消、worker 数与最大尝试、重规划次数、策略、集成冲突、评审通过/拒绝——从任务事件流计算）+ CLI `ledger --metrics`。

## 4. 测试
`tests/test_workbench.py` 7 个：端点反映账本、详情含 workers/messages/事件、HTML 页、HTTP cancel 真停 worker、HTTP answer 续跑至完成、无运行时 405、未知任务 404。`tests/test_eval.py` 新增 4 探针参数化。全量 **200 passed**，eval **22/22**。

## 5. 安全与兼容性
只读默认 + 两写端点需运行时附着 + 仅绑定 localhost；页面与账本一致（刷新/重启可恢复——数据全部来自持久层）。指标从事件流计算（与投影同源真相）。

## 6. 未完成事项
- 30–50 个真实任务集对比（单 Agent vs 规划 vs 多 Worker）需真实模型与真实仓库，超出离线探针范围——**这是发布门槛，未达成，多 Worker 不设为默认策略**（v2 §14-WP10 明确要求）。
- Workbench 的 diff/验证证据展示、审批 HTTP 流（需持久审批队列与交互应答者设计）未实现。

## 7. 本地验收
```bash
python -m pytest -q                     # 200 passed
python -m jinlin_agent eval --suite all # 22/22
python -m jinlin_agent serve --port 7800  # 浏览器打开 http://127.0.0.1:7800
python -m jinlin_agent ledger --metrics
```
