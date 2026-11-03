# ADR-004：Task Protocol（消息）与 Git 协作（代码版本）分离

- 状态：Accepted（2026-11-03 负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §7、§18-4；[current-state.md](../current-state.md) §3/§4

## 背景

v2 §7.1 要求分清两层：Task Protocol 管"谁做什么、是否阻塞、交付什么"；Git 管"代码版本与合并"。仓库现状：没有 worker 间消息，但事件日志已提供消息基建的全部要素——公共信封字段（schema_version/event_id/seq/ts/session_id/turn_id/step_id/type/payload，PLAN.md §2）、幂等键先例（`tool.intent` 的 args_hash + idempotency_key，runtime.py:136-151）、持久化 + 校验 + 崩溃修复（events.py）。

## 决策

1. `task.*` / `review.*` / `integration.*` / `verification.*` 消息是事件日志里的**一等事件**：沿用现有信封，扩展 `root_task_id` / `task_id` / `worker_id` / `attempt` / `correlation_id` 字段（v2 §7.2），schema_version 升级并在 `state._HANDLERS` 注册 fold。
2. Worker 交付必须结构化（v2 §7.4）：status/changed_files/commit/tests/known_risks/follow_up——映射到现有事件要素（written_files/read_hashes 已在 `tool.completed`，verification 已有 `ver_*`），禁止只用自然语言"完成了"。
3. Git 操作（worktree 创建、branch、merge、rebase）只由 Integration Coordinator 通过受策略管控的命令执行（`assess_command` 已给 git push=HIGH、reset --hard=DESTRUCTIVE 分级，policy.py:159-171）；冲突不得静默处理，走 `integration.conflict` 消息 + 暂停。
4. 幂等：消息处理以 `message_id` 去重（Ledger 唯一索引），重复投递无二次副作用；乱序容忍规则在 WP-4 冻结。

## 备选方案

- 独立消息总线/JSON 文件队列：否决——v2 §7.5 明确第一阶段本地持久化即可；事件日志已是久经考验的本地持久层。
- 把合并状态写进 Git（如特殊 ref）：否决——Git 不表达任务语义（v2 §7.1），且 `git *` 变更会污染完成门的"未归属改动"差集检查（verify.py:584-597），需要显式豁免规则。

## 后果

- WP-4 的故障注入测试（重复/乱序/崩溃/重启）可直接复用 events 与 session 恢复的既有测试手法（test_security.py 模式）。
- 事件日志体量增大：Ledger 承担查询，事件仅追加了协议消息，量级可控（v2 §14-WP4 验收：消息可追溯）。
