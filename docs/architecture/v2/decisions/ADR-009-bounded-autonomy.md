# ADR-009：一切自动化都有预算、重试与停止条件（Bounded Autonomy）

- 状态：Proposed（待项目负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §5.4/§6.5/§10.6、§18-9

## 背景

会话级有界性已实现并有测试：四项预算（max_steps_per_session=200 / max_model_calls=120 / max_total_tokens=4M / wall_clock_seconds=4h，loop.py:162-172）、循环守卫（连续失败≥5、同签名重复≥3 → `loop.detected` 注入纠正，3 次后 fail，loop.py:418-445）、纯文本不干活 3 次转 `waiting_user`（loop.py:391-414）、每 5 步 checkpoint。v2 要求把有界性扩展到编排层（重试、重规划、并发、Worker 生命周期）。

## 决策

1. **重试 = 新 Attempt，不是复活**：Worker 终态（completed/failed/cancelled）不可变（现有 `is_terminal` 语义，state.py:85-90），重试创建 `attempt+1` 的新执行实例；有界：`max_attempts`（默认 2）入 config。
2. **重规划有上限**：`max_replans`（默认 3）入 config；局部重规划只作废"依赖已变化/证据失效/输入基线改变"的下游节点并标 `stale`（v2 §5.4 事件表），有效成果保留。每次重规划发 `decision.recorded`（问题/选项/理由/证据，domain.py:313-326 现成结构）。
3. **并发保守**：默认并行度 2、根任务级并发预算、同一模块写任务不并行（只读调查可并行）——数值全部进 config 且项目级只能收紧（沿用 `_RULES.decrease` 模式）。
4. **取消必须停真实执行**：现有基础是 Job Object 杀整树（windows.py）；新增 Worker 线程的协作式取消点（每步检查取消标志，退出码 3 语义沿用）。取消流程按 v2 §13.4：保存已产生 commit/证据 → 清理 → 报告已完成与未完成部分。
5. **停止条件表**（v2 §10.6）直接译成 Supervisor 判定代码：连续失败/依赖变化/结果冲突/预算耗尽/无进展 → 各自映射到 retry/replan/ask/stop 之一，全部落事件。

## 备选方案

- 固定并发数作为唯一控制：否决——v2 §10.6 明确并发上限须结合依赖/资源/冲突概率；上限只是最后防线。

## 后果

- WP-2/WP-3 的验收含故障注入：Worker 崩溃后 Attempt 语义、预算耗尽时任务标"未完全验证"而非成功（v2 §10.4，与 ADR-007-3 一致）。
- 现有 4 项会话预算的键名与语义不变，Worker 级预算是 TaskSpec 字段（v2 §12.1 budget 结构）。
