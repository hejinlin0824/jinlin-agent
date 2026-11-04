# WP-V2.0-2 实施报告

> 按 v2.0 设计文档 §20.3 模板。前置：WP-V2.0-1（Task Model + Task Ledger）已实现。分支：`feature/v2-wp1-task-ledger`（WP-1 与 WP-2 同分支顺序提交，按 ADR-010 每 WP 独立提交）。

## 1. 基线

- Branch：`feature/v2-wp1-task-ledger`
- Base commit：WP-1 文档提交（`e086f6a`）
- Head commit：`5de75c9`（实现 + 测试 + ci.yml + PLAN.md §13）；其后为文档提交

## 2. 目标

在现有 Agent Loop 上构建统一执行实例管理，不复制执行引擎（v2 §14-WP2）：Runtime 接口、WorkerFactory（`submit_task`）、RoleSpec 注入统一 Loop、Worker 绑定 TaskSpec/上下文/工具权限/预算、启动/运行/等待/恢复/取消、并发限制、超时/重试/Attempt、Runtime 事件入 Task Ledger。

## 3. 实际改动

新增：

- `src/jinlin_agent/worker_runtime.py`（约 400 行）：`WorkerRuntime`——submit_task / run_task（带 Attempt 重试循环）/ wait_worker / cancel_worker（协作式 + 未启动直取消）/ resume_worker（waiting_* → 续同一 session）/ shutdown。Worker = **同一个 Harness 内核**的参数化实例；本模块无第二套执行循环（验收条款，ADR-001）。结果映射：session 终态经 `SESSION_TO_TASK_STATUS` 写回 worker/task 行；完成与失败都 `record_result`（v2 §7.4 结构：changed_files 来自 `state.diff_paths()`，tests 来自最近 verification_runs；`commit` 留空待 WP-V2.0-5 绑定 worktree）。非重试错误码集合：`BUDGET_EXCEEDED`（ADR-009：预算耗尽停止，不烧重试）。
- `tests/test_worker_runtime.py`（12 个测试，TDD 先红）：单 worker 端到端 completed + 账本/事件断言；TaskSpec 从账本行往返；**角色白名单**（白名单外工具 TOOL_NOT_FOUND，dispatcher 层不可达）；**per-worker 预算**（max_tool_calls 触发 BUDGET_EXCEEDED 且只产生 1 个 Attempt）；**重试 = 新 Attempt**（2 行 worker 记录，attempt 1 failed / 2 completed）；**协作式取消**（worker cancelled + task cancelled + `session.cancelled` 事件）；**同一 workspace 独占**（第二个 worker 保持 queued）；**不同 workspace 可并行**（barrier 证明重叠）；**并发上限**（max_concurrent_workers=1 时第二个保持 queued）；**waiting_user → resume 续跑完成**（含 `session.user_input` 断言）。

内核参数化（可选参数，默认 None，`run` 原路径零变化——验收"不同角色复用同一执行核心"）：

- `loop.py`：`Harness(role=, budget=, cancel_check=)`——role.allowed_tools 过滤注册表；role.system_instruction 进系统提示；budget 覆盖 `_check_budget`（新增 `max_tool_calls` 维度）；cancel_check 在 step 边界检查（进行中的模型调用/工具先完成，诚实标注为协作式取消）。
- `tools/registry.py`：`ToolRegistry.view(allowed)` 白名单子集视图（未知名忽略，向前兼容）。
- `prompt.py` / `context.py`：`build_system_prompt(..., role_instruction=)` 增加 binding Role 段，经 ContextBuilder 透传。
- `config.py`：`runtime.max_concurrent_workers`（默认 2，项目级只能减小）。
- `ledger.py`：连接改 `check_same_thread=False` + **全部读写纳入 RLock**（兑现 WP-1 遗留"WP-2 重新评估并发"的决策）；新增 `set_worker_session` + 第 7 种任务流事件 **`worker.session_bound`**（worker→session 绑定必须可重建，resume 依赖）；新增 `task_spec_from_row`。
- `.github/workflows/ci.yml`：push 触发分支增加 `feature/**`（本 WP 起功能分支有 CI）。

## 4. 测试

- 命令：`python -m pytest -q --junitxml`；环境：Windows 11 / Python 3.13。
- 结果：**133 passed / 0 failed / 0 skipped**（基线 105 + WP-1 16 + WP-2 12；junit 佐证）。
- `python -m jinlin_agent eval --suite all` → **18/18**（离线）。
- TDD 过程：12 个测试先红（ImportError），实现后转绿；期间修复两处实现缺陷（resume 传参类型、worker→session 绑定缺失）——第二个缺陷由测试驱动出 `worker.session_bound` 事件设计，并补了"绑定可重建"的 ledger 回归测试（第 16 个 WP-1 测试）。
- CI：本分支推送后自动触发（`feature/**` 已入触发列表）；run 链接见本报告提交后的分支 Actions 页，需双平台绿才算本 WP 关闭。

## 5. 安全与兼容性

- 权限变化：角色白名单在 **registry 层**强制（不是提示词约定）；完整的能力集→资源映射仍按 ADR-006 属 WP-V2.0-6，本 WP 未越界实现。
- 数据迁移：无（workers 表新增使用既有 session_id 列，无 schema 变更）。
- 向后兼容：CLI 行为、会话事件、既有 105 测试全部不变；`Harness` 新参数全部默认 None。
- 风险（如实声明）：
  1. 取消是**步边界协作式**——进行中的模型调用会先完成（超时上限兜底）；硬杀进程树仅在沙箱命令层存在（Job Object）；进程级 worker 隔离是后续决策（v2 §13.4 的完整形态）。
  2. 同一 workspace 的多 worker 整程独占是为规避 Windows 沙箱写 ACE 的授/收竞争——WP-V2.0-5 的独立 worktree 会替换这一约束。
  3. 并行 worker 共享 TaskLedger 单连接（RLock 串行化）；正确性优先于吞吐，量级评估见模块 docstring。

## 6. 未完成事项（明确列出）

- worktree/分支/集成分配（Workspace Manager、Integration Coordinator）= WP-V2.0-5；当前 worker 直接使用传入 workspace。
- DAG 调度（依赖就绪即启动、stale 下游）= WP-V2.0-3；本包的 `run_task` 只驱动单任务（含 Attempt 循环）。
- 任务协议的 Dispatcher 级收发（assign/progress/blocked 消息流转）= WP-V2.0-4；当前生命周期经账本 API + 事件承载。
- RoleSpec 只注入了 system_instruction 与 allowed_tools；`output_schema`/`can_delegate` 字段为数据预留，消费方分别在 WP-V2.0-4（结构化交付）与 WP-V2.0-3（委派边界）。
- 完成门对"子任务级验收标准"（TaskSpec.acceptance_criteria）的机器判定 = WP-V2.0-8；当前验收标准作为任务上下文注入 request，不参与 gate 判定。

## 7. 本地验收（Windows 操作步骤）

```bash
cd "E:\codebase\玩耍项目\jinlin-agent"
git checkout feature/v2-wp1-task-ledger
python -m pytest -q                          # 133 passed
python -m jinlin_agent eval --suite all      # 18/18
# 推送后看 CI：feature/v2-wp1-task-ledger 分支应出现 windows-latest + ubuntu-latest 双绿
```

预期：全量测试绿；`tests/test_worker_runtime.py` 覆盖生命周期/白名单/预算/重试/取消/并发六类语义；CI 双平台绿。
