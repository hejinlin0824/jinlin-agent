# WP-V2.0-4 实施报告 — 任务协议与 Worker 协作

> 分支 `feature/v2-wp1-task-ledger`。实现提交 `3c776c2`；基线 `bc83c02`（WP-3 后）。

## 1. 基线
- Branch：`feature/v2-wp1-task-ledger`；Base：`bc83c02`；Head：`3c776c2`

## 2. 目标
可靠、可持久化的任务内部通信协议（v2 §14-WP4）：信封校验、持久化、幂等消费、交付/失败/阻塞报告、Reviewer 结果协议、版本兼容、故障注入。

## 3. 实际改动
- `protocol.py`：`MessagePump`（publish/drain；**投递至少一次、处理恰好一次**——crash 在 handler 与 mark 之间会重投，处理器必须幂等，文档明示）；信封校验（协议**主版本**门：minor 容忍、不兼容主版本 fail-closed `TASK_MESSAGE_INVALID`）；`deliver_result`（**确定性 message_id**=task:worker:attempt，重投递真实去重）；`report_progress/report_blocked`。
- Reviewer 协议（v2 §12.4）：`ReviewResult` + 校验（rejected 必须 required_actions）+ 模型驱动 `conduct_review` + `request_review/record_review` 协议消息。
- **Schema v1→v2 迁移**（ADR-008）：`ALTER TABLE ADD COLUMN`（messages.processed_at、tasks.requires_review），`user_version` fail-closed；账本新增 `pending_messages/mark_message_processed/set_worker_session`（+第 7 种任务事件 `worker.session_bound`，绑定可重建）。
- `taskmodel.py`：新增 `review_pending` 任务态（requires_review 任务在 worker 完成后停靠；**只有评审通过才 completed / 拒绝转 failed**——终态永不复活，ADR-009）；TaskSpec.requires_review。
- `supervisor.py`：评审门接入调度循环；拒绝进入有界重规划。

## 4. 测试
`tests/test_protocol.py` 16 个（含 ledger 绑定重建测试）：v1→v2 迁移（保持主键的 DROP COLUMN 回退法）、新版 schema fail-closed、信封/版本门、恰好一次消费、按类型过滤、乱序容忍、交付幂等、评审通过/拒绝/校验、Supervisor 评审门正反两路。全量 **159 passed**，eval 18/18。

## 5. 安全与兼容性
迁移提供回滚（user_version 高版本拒绝打开；降级=还原兼容二进制或删库重建）。风险：review_pending 是任务层新增状态（v2 §6.3 建议集的忠实扩展，映射表测试锁定）。

## 6. 未完成事项
消息的模型侧"自然语言→协议"编排在 worker 提示中仍是隐式的（Worker 交付经 runtime 结构化落账本）；跨进程 pump 语义未扩展（单进程多线程已覆盖）。
