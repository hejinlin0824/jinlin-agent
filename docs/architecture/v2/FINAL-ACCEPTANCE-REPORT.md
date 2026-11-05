# v2.0 最终验收报告（FINAL ACCEPTANCE REPORT）

> 对应《Jinlin Agent v2.0 最终收尾任务书：R10 修复与 Runtime 验收》（WP-F1~F5）。
> 结论先行：**三策略在代码层真实不同且全部经真实管线完成；Mock dry-run 实际执行并留有可追溯
> 记录；验证通过率独立于 status 统计；文档与 CLI 实际行为一致；最终代码 HEAD `caf7e8c` 的
> Windows/Ubuntu CI 均通过（run 41969534706，§5）。真实模型评测仍未执行（§6），因此不宣称
> 任何"多 Agent 优于单 Agent"结论。**

## 1. 基线与最终 Commit

| 项 | 值 |
|---|---|
| 本轮基线（任务书下发时 HEAD） | `3d4c0e4`（test(workbench): poll the task row） |
| 最终代码 Commit（CI 所在提交） | `caf7e8c` |
| 本报告提交 | 见 §2 表格最后一行（纯文档提交，不含代码变更） |
| 分支 | `feature/v2-integration-remediation`（main 未改动，遵守任务书禁令） |

## 2. 修改文件与 Commit 列表（`3d4c0e4..caf7e8c`，共 2 个提交 / 16 个文件）

**`2bec1de` feat(benchmark): WP-F1/F2/F3** — 代码与证据：

| 文件 | 变更 |
|---|---|
| `scripts/benchmark_matrix.py` | 重写：三策略入口、14 项指标、独立 verification_passed、dry-run 真实管线、scratch 工作区自带 git 仓库 |
| `src/jinlin_agent/supervisor.py` | `execution_mode="sequential"`（计划链式串行、单 Worker）；`_integrate_all` 对空交付物 `not_needed` 跳过合并与重验证 |
| `src/jinlin_agent/workspace_manager.py` | 空交付物不合并（`commit==base` → `not_needed`，协调器层）；`is_git_repo` 改为**必须是仓库根**（修复挂到外层仓库的事故类缺陷） |
| `src/jinlin_agent/ledger.py` | `record_result` 实Whether路径补 `tasks.integration` 更新（此前只有 replay 会写） |
| `tests/test_benchmark_matrix.py` | 新增 7 个测试（无账本任务 / 链式串行 / DAG 保持 / worktree 集成 / 三策略账本足迹不同 / 14 指标齐全） |
| `tests/test_integration_sandbox.py` | 5 个 worktree 测试改为先写入真实交付物再集成；新增空交付物 `not_needed` 回归测试 |
| `tests/test_recovery.py` | 重启集成测试补真实交付物（空 worktree 不再被合并） |
| `tests/test_worktree.py` | 新增 `is_git_repo` 根目录语义测试 |
| `docs/architecture/v2/benchmark/dryrun/*.json` | dry-run 全套证据（6 个 JSON，§4） |

**`caf7e8c` docs: WP-F4** — `README.md`（可观测条目重写）、`docs/architecture/v2/REMEDIATION-REPORT.md`
（§6 serve 只读边界如实改写；§7 测试数字更新为 368+3；§8 指向本报告；顶部加收尾轮说明）。

**本报告提交**：`docs/architecture/v2/FINAL-ACCEPTANCE-REPORT.md`（新增）+ `AGENT.md`（状态数字更新）。

## 3. 三策略真实执行路径（WP-F1：代码层不同，非名称不同）

同一任务（`analyze-demo`）、同一初始工作区状态、同一验收标准（会话验证阶梯 + 完成门 +
TaskSpec 验证档案），三条路径共享唯一执行内核（`loop.Harness`），差异全部在调度/集成层：

| 策略 | 入口 | 执行路径 | 账本 | 集成 |
|---|---|---|---|---|
| **single** | `run_single` | 一个 Harness 会话直接驱动（v1 `run` 语义）：无 Planner、无 Worker、无任务账本记录 | **无 TaskRecord** | 不适用（会话验证阶梯即验收） |
| **planner** | `run_supervised(mode="sequential")` | 混合复杂度评估 → Planner 分解 → **`Supervisor(execution_mode="sequential")`**：`_register_plan` 把计划的 DAG **改写为线性链**（每个子任务依赖前一个），`max_concurrent_workers=1`，严格**一个 Worker 按序执行**；manager-less 直接编辑 | 真实 TaskRecord + 子任务 | 子任务完成即 `integration=not_needed/pending`（直接编辑，无需合并） |
| **multi** | `run_supervised(mode="multi")` | 同一 Planner 输出但**保持计划 DAG（依赖为空 → 并行就绪）**，默认并发调度；工作区是仓库根时挂 `WorkspaceManager`：每 Worker 独立 worktree → `IntegrationCoordinator` 合并 / 过期 rebase / 冲突暂停 / 验证失败回滚 | 真实 TaskRecord + 子任务 | 真实 worktree 集成（空交付物 `not_needed` 跳过——本轮修复） |

代码层差异的可执行证明 = `tests/test_benchmark_matrix.py`：
`test_strategies_produce_distinct_ledger_footprints`（single 创建 0 条账本记录，planner/multi
各创建根+2 子任务）；`test_planner_strategy_chains_subtasks_for_one_worker`（从账本断言子任务
依赖被改写为链）；`test_multi_strategy_schedules_the_planned_dag`（DAG 依赖保持为空）；
`test_multi_strategy_integrates_via_worktree_on_a_git_repo`（真实 git 仓库上走 worktree 集成）。

## 4. Mock dry-run 结果（WP-F2：真实管线，零 Token）

命令（可复现，工作区为运行时生成的微型 git 仓库，三策略看到完全相同的初始状态）：

```bash
python scripts/benchmark_matrix.py \
  --tasks docs/architecture/v2/benchmark/dryrun/tasks.json \
  --out docs/architecture/v2/benchmark/dryrun \
  --strategies single,planner,multi --dry-run
```

| 指标 | single | planner | multi |
|---|---|---|---|
| status | completed | completed | completed |
| verification_passed（独立统计） | True | True | True |
| subtask_integration | —（无账本） | `not_needed` ×2（analysis，无改动） | `not_needed` ×2（真实 worktree 集成决策） |
| wall_seconds | 1.1 | 3.1 | 6.8 |
| tokens_in/out | 4498+80 | 9388+160 | 9332+160 |
| model_calls / tool_calls | 4 / 4 | 8 / 8 | 8 / 8 |
| replans / conflicts / human_interventions | 0 | 0 | 0 |
| 账本 TaskRecord | 无 | 根 + 2 子任务 | 根 + 2 子任务 |

原始记录（逐 run JSON + 汇总）已入库：`docs/architecture/v2/benchmark/dryrun/`
（`analyze-demo_{single,planner,multi}.json`、`records.json`、`summary.json`）。

**如实说明（不冒充、不隐瞒）：**
1. **tokens 是估算值**：MockProvider 不产生 usage，token 数字来自 `context_built` 事件的
   提示估算（state 投影），仅作量级参考；真实用量需真实 provider（见 §6）。
2. **multi 的 worktree 路径是真实执行的**：dry-run 工作区在运行时 `git init` 为独立微型仓库，
   multi 在其上创建了真实 worker worktree 分支（`agent/task-*`）与集成分支
   （`agent/integration-*`）并完成集成决策；证据为运行后仓库状态与
   `test_multi_strategy_integrates_via_worktree_on_a_git_repo`。
3. **dry-run 曾抓出两个真实缺陷（本轮修复，非预先存在已知问题）**：
   - 空交付物被 `merge --no-ff` 成空提交并用整套项目测试重验证（对只读 analysis 子任务完全
     错误）→ 现在 `commit==base` 即 `not_needed`，不合并不重验证（协调器 + Supervisor 两层）。
   - `is_git_repo` 把"仓库子目录"当"仓库根"，导致首轮 dry-run 把 WorkspaceManager 锚到本
     checkout 的外层仓库（在真实仓库上创建 worktree/分支，multi 耗时 89s）→ 现在必须
     `git rev-parse --show-toplevel` 等于工作区本身。已全库清理外层污染（worktree/分支/临时账本）。
4. **一次未复现的 flake**：连续三次全量回归中，第二次出现 1 个失败（未及捕获用例名即清理了
   junit），第一/三次及最终以 junitxml 计数的一次均为 0 失败。判定：负载相关偶发，CI 为准（§5）。

## 5. 测试与 CI 证据（WP-F5）

**本机（Windows 10.0.26200，Python 3.13，最终代码 HEAD `caf7e8c`，junitxml 精确计数）：**

| 项 | 结果 |
|---|---|
| `python -m pytest -q`（junitxml） | **368 passed / 0 failed / 0 errors / 3 skipped**（平台守卫），263s |
| `python -m jinlin_agent eval --suite all` | **22/22**（18 v1 + 4 v2：P7 并行重叠 / P8 依赖顺序 / P9 能力越界拒绝 / P10 预算不重试） |

**CI（`ci.yml`：Windows + Ubuntu × Python 3.13，完整 pytest + 离线 eval + Rescue 回归）：**

| Run ID | HEAD | 结果 |
|---|---|---|
| `41969534706` | `caf7e8c`（本轮最终代码提交） | **成功（双平台绿）**：`ubuntu-latest / py3.13` ✅、`windows-latest / py3.13` ✅ |
| `41773135089` | `3d4c0e4`（本轮基线，前轮收尾） | 成功（历史参照） |

（CI 在 `caf7e8c` 上执行；本报告与其后的 AGENT.md 状态更新为纯文档提交，不含代码变更。）

## 6. 尚未完成的工作（如实清单，不虚构）

1. **真实模型评测未执行**（任务书 P2/R10 的原始目标）：需要所有者提供 `DEEPSEEK_API_KEY` 并
   确认任务集。**因此本轮不给出、也不允许评审采信任何"多 Agent 优于单 Agent"的结论**——
   dry-run 三策略数据只证明"路径真实不同且都能跑通"，不构成性能/质量对比。
2. **Worker tokens 为估算值**（见 §4.1）：mock provider 无 usage；真实运行才有精确 tokens。
3. **`conflicts` 指标恒为 0**：代码注释已声明按事件统计是后续工作（冲突当前表现为 blocked）。
4. **审批持久化注册表 / HTTP 审批队列未实现**（整改报告 §10 遗留，如实保留）。
5. **集成验证的"affected-only"定向测试**：当前对集成树跑项目测试命令全文（`--confcutdir`
   已限定收集范围）；按 diff 定向仍未做（设计预留）。
6. **CLI `serve` 无内嵌运行时的长驻服务形态**：创建/取消/回答仅库内嵌可用（§WP-F4 修正后
   的文档边界），CLI 观测为纯只读。

## 7. Windows 本地验收步骤

```bash
# 0) 环境：Windows 10/11，Python 3.13，git 可用
cd <checkout of feature/v2-integration-remediation>   # 最终代码 HEAD caf7e8c
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"                               # 仅测试/工具依赖，运行时零第三方依赖

# 1) 完整测试（应显示 368 passed + 3 skipped / 0 failed）
python -m pytest -q

# 2) 离线探针（应显示 22/22 probes passed）
python -m jinlin_agent eval --suite all

# 3) 三策略 dry-run（真实管线、零 Token；报告 §4 同款证据将写入 --out）
python scripts/benchmark_matrix.py ^
  --tasks docs/architecture/v2/benchmark/dryrun/tasks.json ^
  --out %TEMP%\benchmark-dryrun --strategies single,planner,multi --dry-run

# 4) 三策略代码层差异检查（7 个回归测试）
python -m pytest tests/test_benchmark_matrix.py -v

# 5) serve 只读边界（变更端点应 405）：浏览器开 http://127.0.0.1:7800 只能看；
#    POST /api/tasks 等应返回 405 "read-only workbench"
python -m jinlin_agent serve --port 7800

# 6) 账本可观测
python -m jinlin_agent ledger --metrics
```
