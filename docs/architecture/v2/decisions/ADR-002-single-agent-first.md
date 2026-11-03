# ADR-002：简单任务直接执行；复杂任务才进入规划与多 Worker

- 状态：Accepted（2026-11-03 负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §3.3-1、§5、§13.1、§18-2；[gap-analysis.md](../gap-analysis.md) §5

## 背景

现有 CLI `run` 就是"直接执行"：自由文本任务 → 单 Harness → 完成门。18 个探针与真实模型验证（README §9，7 任务 False Success = 0）都建立在这条路径上。v2 要求默认保持单 Agent，多角色是升级模式。

## 决策

1. `run` 命令的现有行为是**默认路径**，任何编排层不得使其变慢或变复杂。
2. 复杂度评估 = 客观扫描 + 模型语义判断 + 策略规则（v2 §5.1）。客观部分的雏形已存在：`workspace.snapshot`（git 状态、脏文件、项目类型、测试命令，session.py:232-260）可扩展为扫描器输入；**禁止**把"仓库大小"当复杂度（v2 §5.2）。
3. 策略输出至少实现 v2 §5.1 六种中的前四种（`direct` / `plan_then_execute` / `parallel_workers` / `sequential_workers`），`human_input_required` 已有对应机制（`request_user_input` 工具 → `session.waiting_user`，task.py:318），`refuse_or_stop` 复用 `session.failed`。
4. 阈值与上限进 config，项目级只能收紧（沿用 config.py `_RULES` 模式）。

## 备选方案

- 让模型自由决定是否拆分：否决——不可测试、不可审计；评估必须产出可落事件的决策记录（复用 `decision.recorded`）。

## 后果

- 需要为"评估正确性"建立探针（WP-3 验收）：简单任务零 Worker 开销；DAG 拒绝环；依赖未满足不启动。
- 单任务路径的回归保护：现有 105 测试 + 18 探针必须在编排层引入后保持全绿。
