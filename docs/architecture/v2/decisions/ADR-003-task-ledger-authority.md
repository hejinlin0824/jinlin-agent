# ADR-003：任务层权威源——Task Ledger 与会话事件日志的分工

- 状态：Accepted（2026-11-03 负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §4.2、§12、§18-3；AGENTS.md 硬规则 10；[gap-analysis.md](../gap-analysis.md) §2-5

## 背景

仓库有一条铁律：事件日志是唯一真相（AGENTS.md 规则 10；events.py 严格回放校验、seq 缺口即 `SESSION_CORRUPTED`）。v2 引入 Task Ledger（根任务/子任务/Worker/Attempt/消息/预算/审批/结论）。若不定义两者关系，v2 §2.3-5 担心的"两套状态机矛盾"会成真。

## 决策

1. **会话事件日志仍是执行证据的唯一真相**，其语义（append-only、fsync、尾部修复、fail-closed 校验）一字不改。
2. Task Ledger 是**任务层的权威索引**：TaskSpec、TaskStatus、WorkerRecord、Attempt、task.* 消息、预算账本、审批与最终结论。它落在 stdlib `sqlite3`（→ ADR-008），且**可由事件重建**（Ledger 写操作同步发事件；重建器 = 投影，与 state.py `recover` 同构）。
3. TaskStatus 与 SessionStatus 的映射在 WP-1 冻结成表并测试锁定。建议起点：一个子任务 ≈ 一个 Session（复用 `sess_*` 目录），TaskStatus 是 SessionStatus 之上的聚合层；`paused`（现仅预留，domain.py:49）在任务层正式启用或删除，不允许继续两处不一致。
4. 孤儿语义沿用：`tool.intent` 无 result → 三态处理（session.py:169-205）的模式推广到 worker 级（Attempt 有 assigned 无 result → 崩溃恢复流程）。

## 备选方案

- Ledger 直接当唯一真相（SQLite 为主，事件为辅）：否决——违背 AGENTS.md 规则 10 与 10 轮评审建立的恢复语义。
- 不建 Ledger，继续用文件系统扫描 sessions 目录当任务列表：否决——无法表达根/子任务关系、DAG、预算聚合与跨会话消息。

## 后果

- WP-1 必须交付：映射表 + 重建器 + "重启后 Ledger 与事件一致"的恢复测试 + "重复消息不产生重复副作用"的幂等测试（v2 §14-WP1 验收）。
- `report.written` 等无 handler 事件在 schema 收编时一并处理（current-state.md §4）。
