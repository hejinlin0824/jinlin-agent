# v2.0 最终验收报告（FINAL ACCEPTANCE REPORT）

> 对应《Jinlin Agent v2.0 最终收尾任务书：R10 修复与 Runtime 验收》（WP-F1~F5）+ 所有者提供
> 真实 provider 凭证后的真实模型评测补充（§4b）。
> 结论先行：**三策略在代码层真实不同且全部经真实管线完成；Mock dry-run 与真实模型评测（6/6）
> 都实际执行并留有可追溯记录；验证通过率独立于 status 统计；文档与 CLI 实际行为一致；收尾轮
> 代码 HEAD `caf7e8c` 的 Windows/Ubuntu CI 通过（run 41969534706），真实评测轮的 CI 见 §5
> 补充行。n=2 的简单任务集不构成策略优劣结论——事实上 multi 在其上更慢（§4b.2）。**

## 1. 基线与最终 Commit

| 项 | 值 |
|---|---|
| 本轮基线（任务书下发时 HEAD） | `3d4c0e4`（test(workbench): poll the task row） |
| 收尾轮最终代码 Commit（CI run 41969534706 所在提交） | `caf7e8c` |
| 真实评测轮 Commit（CI run 41975113585 所在提交） | `7ec9c69`（fixtures + 真实评测证据 + benchmark_matrix.py direct 模式聚合修复） |
| 分支 | `feature/v2-integration-remediation`（main 未改动，遵守任务书禁令） |

## 2. 修改文件与 Commit 列表（`3d4c0e4` 起）

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

**`ba4ebaf` docs: 验收报告** — `docs/architecture/v2/FINAL-ACCEPTANCE-REPORT.md`（新增）+ `AGENT.md`（状态数字更新）。

**真实评测轮 `7ec9c69`** — `fixtures/demo`、`fixtures/bench-calc`（评测任务的工作区状态，含故意缺陷；
运行前需各自 `git init` 为独立仓库，见 §7）；`docs/architecture/v2/benchmark/real/`（tasks.json、6 个逐
run JSON、records/summary、home/ 全部任务与 Worker 事件流 2.9MB）；`scripts/benchmark_matrix.py`
（direct 模式指标聚合修复：worker 也可挂在根任务上）；本报告 §1/§2/§4b/§5/§6 更新。

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

## 4b. 真实模型评测（所有者提供凭证后执行，2026-11-05）

**Provider**：Volcano Ark OpenAI 兼容端点（`ark.cn-beijing.volces.com/api/coding/v3`，模型
`deepseek-v4-flash`，key 经 `DEEPSEEK_API_KEY` 环境变量注入，**不落盘、不入库**）。
**任务集**（`benchmark/real/tasks.json`，fixture 已入库、每轮运行前 `git reset` 复位到同一初始状态）：
- `analyze-demo`（只读分析，fixtures/demo）
- `fix-add`（单文件修复 + 测试验收，fixtures/bench-calc：`add()` 有 `a - b` 缺陷，`test_add` 失败）

**结果（6/6 完成，6/6 verification_passed=True，逐 run JSON + 全部会话/任务事件流入库 `benchmark/real/`）：**

| 指标（analyze-demo / fix-add） | single | planner | multi |
|---|---|---|---|
| status / verification_passed | completed / True | completed / True | completed / True |
| wall_seconds | 100.3 / 342.9 | 104.4 / 52.3 | 417.1 / 412.6 |
| tokens 实际 usage（in+out 合计） | 93,887 / 168,550 | 70,676 / 53,823 | 95,962 / 72,297 |
| model_calls | 13 / 17 | 10 / 8 | 13 / 9 |
| tool_calls | 16 / 21 | 10 / 11 | 16 / 13 |
| human_interventions | 3 / 0 | 0 / 0 | 0 / 0 |

（汇总聚合见 `benchmark/real/summary.json`：三策略 2 任务 success_rate=1.0、
verification_pass_rate=1.0。）

**核验（独立于 benchmark 自报）：** `fix-add` 三策略均真实修复 `add()`（工作区 `a + b`），
`python -m pytest -q` 2/2 通过；`analyze-demo` 三策略工作区净变更均为零。

**如实发现（不夸大、不外推）：**
1. **复杂度评估器把两个任务都判为 direct**（简单任务永不规划——设计行为）：planner/multi 与
   single 的差异落在"评估 + 任务账本 + 策略选择"，而非"多 Worker 并行"。**带真实规划的
   plan/multi 路径需要真正复杂的任务集**（见 §6）。
2. **n=2 且任务简单：不构成任何策略优劣结论。** 本表唯一支持的事实陈述是：三条路径在真实
   provider 上全部端到端可用且全部通过独立验证。事实上 multi 在这两个简单任务上更慢
   （均值 414.9s vs planner 78.3s）——直接反证了"多 Agent 必然更好"。
3. **审批语义差异（真实观察）**：single（v1 会话路径）出现 3 次 approval 等待；受监督路径的
   execute 类请求被任务层策略直接拒绝（不等待）。两者都是 fail-closed，但行为不同，已如实记录。
4. **真实会话质量样例**（analyze × multi，`sess_20261105_131740_73d065`）：模型写了一个临时
   验证脚本 → `run_command` 两次被 fail-closed 拒绝 → 自行 `apply_patch` 删除临时脚本 →
   `git status` 确认干净 → 携带证据引用 propose_completion → L0/L1 验证通过。README 与代码
   不一致（声称 prints，实际 returns）也被三策略一致发现。

## 5. 测试与 CI 证据（WP-F5）

**本机（Windows 10.0.26200，Python 3.13，最终代码 HEAD `caf7e8c`，junitxml 精确计数）：**

| 项 | 结果 |
|---|---|
| `python -m pytest -q`（junitxml） | **368 passed / 0 failed / 0 errors / 3 skipped**（平台守卫），263s |
| `python -m jinlin_agent eval --suite all` | **22/22**（18 v1 + 4 v2：P7 并行重叠 / P8 依赖顺序 / P9 能力越界拒绝 / P10 预算不重试） |

**CI（`ci.yml`：Windows + Ubuntu × Python 3.13，完整 pytest + 离线 eval + Rescue 回归）：**

| Run ID | HEAD | 结果 |
|---|---|---|
| `41975113585` | `7ec9c69`（真实评测轮：fixtures + 证据 + 聚合修复） | **成功（双平台绿）** |
| `41969534706` | `caf7e8c`（收尾轮最终代码提交） | **成功（双平台绿）**：`ubuntu-latest / py3.13` ✅、`windows-latest / py3.13` ✅ |
| `41773135089` | `3d4c0e4`（本轮基线，前轮收尾） | 成功（历史参照） |

（收尾轮 CI 在 `caf7e8c` 上执行；真实评测轮 CI 在 `7ec9c69` 上执行，双平台均绿。）

## 6. 尚未完成的工作（如实清单，不虚构）

1. **含真实规划的复杂任务集评测**：§4b 的真实评测中，复杂度评估器把两个简单任务都判为
   direct（设计行为：简单任务永不规划），因此 plan/multi 的**真实模型规划/多 Worker 并行/
   worktree 集成全链路**仍未被真实任务触发（机制层面由 dry-run §4 与测试覆盖）。需要所有者
   确认一个含真实复杂度的任务集后执行；在那之前**不给出任何策略优劣结论**（§4b 已用数据
   反证"多 Agent 必然更好"）。
2. **dry-run 的 tokens 为估算值**（mock 无 usage）；真实评测（§4b）已使用真实 usage 统计。
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

# 3b) 真实模型评测（需要所有者提供 key 并设置环境变量；fixtures 先各自 init 为独立仓库）
set DEEPSEEK_API_KEY=<owner-provided-key>
set JINLIN_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3
set JINLIN_MODEL=deepseek-v4-flash
for %d in (fixtures\demo fixtures\bench-calc) do (cd /d %~dp0%d && git init -q && git add -A && git commit -qm "fixture state" && cd /d %~dp0)
python scripts/benchmark_matrix.py ^
  --tasks docs/architecture/v2/benchmark/real/tasks.json ^
  --out %TEMP%\benchmark-real --strategies single,planner,multi
# 注意：fix-add 会真实修改 fixtures\bench-calc；策略间比较前需 git -C fixtures\bench-calc reset --hard 复位

# 4) 三策略代码层差异检查（7 个回归测试）
python -m pytest tests/test_benchmark_matrix.py -v

# 5) serve 只读边界（变更端点应 405）：浏览器开 http://127.0.0.1:7800 只能看；
#    POST /api/tasks 等应返回 405 "read-only workbench"
python -m jinlin_agent serve --port 7800

# 6) 账本可观测
python -m jinlin_agent ledger --metrics
```
