# WP-V2.0-1 实施报告

> 按 v2.0 设计文档 §20.3 模板。前置：WP-V2.0-0 盘点与 ADR-001~010 已由负责人确认（2026-11-03）。分支：`feature/v2-wp1-task-ledger`（自 `feature/v2-wp0-inventory` @ `a5abe5d` 切出）。

## 1. 基线

- Branch：`feature/v2-wp1-task-ledger`
- Base commit：`a5abe5d`（= `review/followup-fixes` @ `b0f3ade` + WP-0 文档 + ADR 确认记录）
- Head commit：`8a21316`（实现 + 测试 + PLAN.md §12）；其后为文档提交

## 2. 目标

统一任务、子任务、Worker、Attempt、消息的数据模型与持久化（v2 §14-WP1）：TaskSpec / RoleSpec / WorkerRecord / TaskMessage / 交付结果、根/子任务关系、状态机与合法迁移、SQLite Repository、事件记录与快照、重启恢复、幂等消息。

## 3. 实际改动

新增：

- `src/jinlin_agent/taskmodel.py`（约 430 行）：TaskStatus 九态状态机 + 迁移表（终态不可复活，ADR-009）、`SESSION_TO_TASK_STATUS` 冻结映射（ADR-003）、TaskSpec / Budget / WorkspaceScope / RoleSpec（含 5 个默认角色，数据层）/ WorkerRecord / TaskMessage（v2 §7.2 信封 + §7.3 类型表）/ ArtifactRef / EvidenceRef、`validate_task_result`（v2 §7.4 交付校验）、`validate_dependency_graph`（重复/未知/自依赖/环检测）。
- `src/jinlin_agent/ledger.py`（约 470 行）：`TaskLedger`——stdlib sqlite3 投影（tasks/workers/messages 三表 + 索引，WAL，`user_version` fail-closed）；任务事件流 `<home>/tasks/<root>/events.jsonl` 复用既有 `EventLog`（fsync/seq/尾部修复/单写者锁）；**先发事件后提交行**的写序；`rebuild()` 从日志重建投影；`message_id` 幂等；初始化文件锁防并发建库。
- `tests/test_ledger.py`（15 个测试）：状态机（终态冻结/非法迁移/映射表全覆盖）、DAG 校验、交付校验、生命周期持久化、worker 新 Attempt 语义、消息幂等（库与事件双重断言）、**删库重建**、**进程重启恢复**、schema 新版本 fail-closed、`report.written` 折叠、CLI。

修改（最小接线）：

- `errors.py`：新增 Task 家族错误码 `TASK_NOT_FOUND / TASK_ALREADY_EXISTS / TASK_TRANSITION_INVALID / TASK_RESULT_INVALID / TASK_MESSAGE_INVALID / LEDGER_UNAVAILABLE`。
- `config.py`：`Config.ledger_path` / `Config.tasks_dir`（均 home 派生，沿 `storage.home` 的锁定规则，无新配置键 → `effective_hash` 不变）。
- `state.py`：`report.written` 收编进 `_HANDLERS`（此前 loop.py:462 发出但无折叠 handler，WP-0 盘点发现项）。
- `cli.py`：新增 `ledger` 子命令（默认摘要；`--rebuild` 重建投影）。

API / Schema 变化：任务层为**纯增量**；既有 CLI 行为、会话事件 schema、退出码不变（v2 §18-10 兼容窗口）。数据迁移：无（WP-0 已证实不存在 TaskContract 遗留数据；v2 §14-WP1 第 8 项"从现有数据渐进迁移"= 空集， sessions→worker 绑定属 WP-2）。

## 4. 测试

- 命令：`python -m pytest -q --junitxml`；环境：Windows 11 / Python 3.13。
- 结果：**120 passed / 0 failed / 0 skipped**（基线 105 + 新增 15；junit 佐证 `tests="120" failures="0" errors="0"`）。
- `python -m jinlin_agent eval --suite all` → **18/18**（离线）。
- TDD 过程：先写 `tests/test_ledger.py` 确认红（ImportError→NameError 两轮），实现后转绿；未删改任何既有测试。
- CI：分支未推送，无 run；推送后 `feature/**` 不在 ci.yml 触发列表（`main`/`review/**`/`dev`/PR）——如需 CI 需开 PR 或由所有者调整触发规则（见 §6）。

## 5. 安全与兼容性

- 权限变化：无（TaskSpec.capabilities / workspace_scope 本包为**数据定义**，PolicyEngine 强制在 WP-6 落地，ADR-006 已声明此顺序）。
- 数据迁移：无；ledger 损坏可 `--rebuild` 重建；`user_version` 高于支持的版本时拒绝打开（回滚方案 = 还原兼容版本二进制或删库重建，无破坏性 schema 变更）。
- 向后兼容：既有 105 测试全绿、18 探针全绿；事件日志语义未动。
- 风险：ledger 并发模型（单线程、单进程单写者）是 WP-1 的显式边界，已写入模块 docstring；WP-2 引入多线程 Worker 时必须重新评估（ADR-008 遗留项）。

## 6. 未完成事项（明确列出）

- Worker 的实际创建/调度/取消（Runtime、WorkerFactory）= WP-V2.0-2；本包只交付模型与账本。
- `task.*` 消息目前由 Ledger API 直接写入；Dispatcher 级的协议收发（assign/progress/blocked 流转）= WP-V2.0-4。
- DAG 的**调度**（依赖就绪即启动）= WP-V2.0-3；本包交付的是 DAG **校验**。
- CI 触发分支列表不含 `feature/**`：推送本分支不会触发 CI，建议以 PR 触发或由所有者把 `feature/**` 加入 ci.yml（一行改动，留待决定）。
- AGENT.md 状态已更新（test_status=120、新增 §16）；`docs/architecture/v2/README.md` 状态行待本 WP 合并后由所有者复核。

## 7. 本地验收（Windows 操作步骤）

```bash
cd "E:\codebase\玩耍项目\jinlin-agent"
git checkout feature/v2-wp1-task-ledger
python -m pytest -q                          # 120 passed
python -m jinlin_agent eval --suite all      # 18/18
set JINLIN_HOME=%TEMP%\jinlin-ledger-demo    # 用隔离 home 演示
python -c "from pathlib import Path; import os; from jinlin_agent.config import load_config; from jinlin_agent.ledger import TaskLedger; from jinlin_agent.taskmodel import *; cfg=load_config(home=Path(os.environ['JINLIN_HOME'])); lg=TaskLedger(cfg).open(); root=TaskSpec(task_id=new_task_id()); lg.create_task(root); lg.set_task_status(root.task_id,'running'); print(lg.get_task(root.task_id)['status']); lg.close()"
python -m jinlin_agent ledger --workspace .  # 摘要（真实 home 为空时显示 tasks: 0）
python -m jinlin_agent ledger --workspace . --rebuild
```

预期：测试全绿；演示脚本输出 `running`；`ledger` 命令列出任务摘要；`--rebuild` 后摘要一致。
