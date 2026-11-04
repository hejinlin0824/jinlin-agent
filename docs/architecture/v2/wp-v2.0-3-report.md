# WP-V2.0-3 实施报告 — 动态规划与 Supervisor

> 分支 `feature/v2-wp1-task-ledger`（v2 实施线，每 WP 独立提交）。实现提交 `bc83c02`；基线 `39d0237`（WP-2 文档后）。

## 1. 基线
- Branch：`feature/v2-wp1-task-ledger`；Base：`39d0237`；Head：`bc83c02`

## 2. 目标
按任务复杂度选择直接执行或规划执行（v2 §14-WP3）：项目扫描器、Complexity Assessment、Planner 输出 Schema、DAG 校验、依赖调度、计划审核、局部重规划、stale 下游、用户澄清、重规划/预算上限。

## 3. 实际改动
- `planner.py`：模型驱动分解；模型以**标题**引用依赖、id 由系统生成；坏 JSON/未知依赖/重复标题/环 → `MODEL_INVALID_RESPONSE`（绝不静默修复计划）。
- `supervisor.py`：混合评估（有 provider 用结构化模型评估；否则确定性规则，**默认 direct**，简单任务永不规划——ADR-002）；调度循环：依赖全 completed 才启动；失败触发 ≤`runtime.max_replans` 次局部重规划，被取代节点的非终态下游取消（stale，证据保留）；waiting 子任务上浮且 `execute_root` 可从账本状态续跑（规划不重复）。
- `ledger.py`：公开 `emit()`；新任务流事件 `task.strategy_selected`、`task.replanned`。
- `config.py`：`runtime.max_replans`（默认 3，项目级只能减小）。

## 4. 测试
`tests/test_supervisor.py` 10 个（TDD 先红）：直接执行零子任务、DAG 依赖顺序、环拒绝、有界重规划（旧节点 failed / 下游 cancelled / 替换链完成 / replanned==1）、max_replans=0 与超限停止、waiting 上浮 + 续跑。全量 **143 passed**，eval 18/18。CI：分支推送触发（`feature/**`）。

## 5. 安全与兼容性
无权限变化；纯增量。风险：确定性评估只是窄规则回退，真实评估依赖模型质量（探针 P8 锁定行为）。

## 6. 未完成事项
模型评估/规划的**质量**评估归 WP-10 探针与后续真实任务集；Planner 的 `output_schema` 字段消费在 WP-4/8 逐步落地。
