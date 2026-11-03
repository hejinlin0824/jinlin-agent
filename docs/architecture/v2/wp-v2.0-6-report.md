# WP-V2.0-6 实施报告 — Sandbox 与细粒度权限

> 分支 `feature/v2-wp1-task-ledger`。实现提交 `86b16c8`；基线 `dc391f8`（WP-5 后）。

## 1. 基线
- Branch：`feature/v2-wp1-task-ledger`；Base：`dc391f8`；Head：`86b16c8`

## 2. 目标
权限从 Prompt 约定变成 Runtime 可执行的安全边界（v2 §14-WP6、ADR-006）：能力集到工具映射、路径级读写权限（workspace_scope）、命令风险的网络/破坏性门、安全测试。

## 3. 实际改动
- `policy.py`：`PolicyEngine` 新增 `task_capabilities` / `workspace_scope` 维度——WRITE 工具需 `WRITE`、进程工具需 `EXECUTE`、触网命令需 `NETWORK`、破坏性命令需 `DELETE`；**检查先于审批、先于 tool.intent**；scope 违规 `SCOPE_VIOLATED`（读/写/patch 路径均查）；None/空 scope = 不限制（单 Agent 路径零变化）。
- `loop.py`：Harness 可注入 `policy`（内核参数化，默认不变）。
- `worker_runtime.py`：按 TaskSpec 构建受限 PolicyEngine 下发 worker——**注入/仓库内容无法自我提权**（能力是代码判定）。
- `errors.py`：`CAPABILITY_DENIED`、`SCOPE_VIOLATED`。

## 4. 测试
`tests/test_permissions.py` 11 个：四类能力门（含网络/破坏性）、scope 读写/patch 门、空 scope 兼容、**worker 运行时集成**（READ-only 任务的编辑尝试在产生任何副作用前被拒）。全量 **177 passed**，eval 18/18。

## 5. 安全与兼容性
安全测试矩阵（v2 §16.4）覆盖：路径穿越/符号链接（既有 1405 行回归 + 本轮 scope 门）、Prompt 注入（能力代码判定，untrusted 包装既有）、越权写/删（能力+scope+allow_delete 三层）。**未做**（如实）：Windows Sandbox/Hyper-V/WSL2 更强隔离评估（v2 §9.5 建议项）——本 WP 复用既有 L1（受限令牌+Job Object）并按 worktree 授 ACE；资源配额（CPU/内存）未实现，依赖 Job Object 进程树管理。

## 6. 未完成事项
资源配额、进程树句柄化的跨 worker 审计、Windows Sandbox 后端评估——需要 OS 层实验与真实隔离测试证据，单独排期（不与功能 WP 混合）。
