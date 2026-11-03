# ADR-001：统一 Agent Runtime，而不是多个专用 Agent 产品

- 状态：Proposed（待项目负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §1/§6/§18-1；[current-state.md](../current-state.md) §14

## 背景

仓库现状是单 Agent：一次 `run` = 一个 `Harness` 实例（loop.py:41），一个事件日志，一套工具与策略。v2.0 方向是"一个统一 Runtime 按需创建不同职责的执行实例"。仓库没有任何"专用 Agent 产品"需要迁移——不存在要废弃的东西，只存在要不要坚持单一执行内核的选择。

## 决策

Worker **不是**新的 Agent Loop。Worker = 现有 `Harness` 内核的一个受控实例，其差异全部来自配置注入：

- `RoleSpec`：system instruction（prompt.py 已支持装配）、工具白名单（registry 过滤）、能力集（经 PolicyEngine）、输出协议；
- `TaskSpec`：目标、输入、验收标准、预算（loop.py:162-172 的四项预算机制按实例化）；
- 独立的 `Session`/事件日志与 `ToolContext`（workspace 指向自己的 worktree）。

禁止出现第二个执行循环文件；`runtime.py` 的 ToolDispatcher 仍是唯一副作用管线（AGENTS.md 硬规则 2）。

## 备选方案

- 为 supervisor/planner/worker 写各自精简 loop：否决——重复实现预算/守卫/事件语义，v2 §6.2 明确"这些角色不需要各自的 Agent Loop"。
- Worker 用子进程跑 CLI `run`：否决——事件不可编程观测、取消语义粗糙；但可作为 WP-2 的故障隔离兜底再评估。

## 后果

- 正面：10 轮评审加固的语义（fail-closed、事件溯源、完成门）对所有角色自动生效；评审面不扩大。
- 代价：Harness 构造函数需要参数化（当前在 loop.py:42-106 硬编码单会话假设），这是 WP-2 的主要改造点。

## 验收信号

任意 RoleSpec 的实例跑 E1–E8 探针语义不劣化；新增角色不新增执行循环代码。
