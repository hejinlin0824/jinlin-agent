# WP-V2.0-5 实施报告 — Git Worktree 集成

> 分支 `feature/v2-wp1-task-ledger`。实现提交 `dc391f8`；基线 `3c776c2`（WP-4 后）。

## 1. 基线
- Branch：`feature/v2-wp1-task-ledger`；Base：`3c776c2`；Head：`dc391f8`

## 2. 目标
多 Worker 安全并行修改同一仓库（v2 §14-WP5）：Workspace Manager、per-worker worktree、base_commit、集成分支、过期 rebase、冲突暂停、集成后验证、清理。

## 3. 实际改动
- `workspace_manager.py`：worktree 建在 **harness home**（`~/.jinlin-agent/worktrees/`）——用户仓库零污染、worker 工作区边界=worktree 本身；`agent/task-<id>` 分支 + base_commit 记录；幂等清理（分支保留作证据）。
- `IntegrationCoordinator`：worker worktree 以 **harness 身份** commit（不依赖用户 git config；提交前剥离 `__pycache__/*.pyc/.pytest_cache`，无 .gitignore 也不运垃圾）；过期 base → rebase 到 `agent/integration-<root>`；冲突 → **abort + integration.conflict 协议消息 + 命名冲突文件**（绝不静默覆盖）；每次合并后在集成树跑项目测试，**失败即回滚合并**；稳定分支在根完成门前绝不触碰。
- `supervisor.py`：`attach_workspace_manager()`——WRITE worker 各自 worktree、集成阶段在根完成前、commit 绑回 TaskResult（事件 `task.commit_attached` 可重建）、冲突 → 根 `blocked` 待裁决。
- `worker_runtime.py`：`submit_task` 记录 branch/base_commit。
- `errors.py`：`GIT_OPERATION_FAILED`。

## 4. 测试
`tests/test_worktree.py` 7 个（真实 git）：worktree 生命周期/幂等清理、集成+验证、冲突暂停且集成树不被污染、过期 rebase、验证失败回滚、**双写 worker e2e**（集成树含两处修改、commit 绑定、worker worktree 清理、稳定分支未被触碰）。全量 **166 passed**，eval 18/18。

## 5. 安全与兼容性
worktree 是版本隔离不是安全边界（OS 沙箱仍按 worker workspace 生效，ADR-005）；同 workspace 独占约束被 worktree 自然取代（不同路径=可并行）。git 操作为 argv 子进程、coordinator 驱动（非模型工具），事件留痕于任务流。

## 6. 未完成事项
"受影响验证"目前为集成树全量测试套件；按 Diff 取子集属 WP-8 的 diff-aware 延伸（当前实现已满足 v2 §9.2 的"合并后重验证"）；Windows Sandbox/ACL 更强隔离的评估（v2 §9.5）独立排期。
