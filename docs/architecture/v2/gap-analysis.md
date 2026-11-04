# 差距分析（WP-V2.0-0 交付物 2）

> 输入：《Jinlin Agent 架构设计 2.0》（[ARCHITECTURE-v2.0.md](ARCHITECTURE-v2.0.md)）× 真实代码（[current-state.md](current-state.md)）。
> 所有"真实情况"均以 `review/followup-fixes` @ `b0f3ade` 的代码为准。

## 0. 最重要的发现：设计文档的"现状"章节与仓库不符

v2.0 §2.2 的资产表描述了一个"已有 Workbench / TaskContract / ApplicationService / Web API"的项目，并把 `feature/workbench-wp1` / `feature/workbench-wp2` 列为工作分支。逐一核实：

| v2.0 文档声称的资产 | 真实情况 | 证据 |
|---|---|---|
| `ApplicationService`（CLI/Web 共用入口） | **不存在**（`src/` 全文 grep 无命中）；职责上最接近的是 `cli.py` + `loop.py` Harness | grep `applicationstervice` 0 命中；cli.py:353-418 只有 argparse |
| Agent Loop | 存在（`Harness`，loop.py:41-466） | ✅ |
| SessionManager / Session Event Log | 存在且是全仓最强的部分 | session.py、events.py |
| `Workbench`（后台任务协调、状态、审批入口） | **不存在**（grep 0 命中）；审批只有 CLI 交互 handler | cli.py:38-53 |
| `TaskContract` | **不存在**；最接近物 = 自由文本 `request` + 平铺 `TaskPlan` | domain.py:255-292；verify.py:554-556 只查 request 非空 |
| Task / Session 两套生命周期状态机 | 只有一套（`SessionState.status/phase`）；"两套状态机矛盾"的问题不存在，但**任务状态机需要新建**并显式对齐 | domain.py:32-55 |
| `VerificationPlan` | 该名字不存在；实际是 `Verifier` 确定性阶梯 + `_run_harness_verification` 按变更/claim 选择层级（本质即 diff-aware verification 的雏形） | verify.py:257-504；loop.py:367-389 |
| `PolicyEngine` | 存在（同名，policy.py:264-409） | ✅ |
| Web API / Workbench UI | **不存在**（无任何 HTTP/socket 代码） | — |
| 分支 `feature/workbench-wp1` / `wp2` | **不存在**（本地与 origin 均只有 `main`、`review/followup-fixes`） | `git branch -a` |

**含义（对 v2 计划的修正）**：

1. v2 §2.2 表中"保留 / 演进"的措辞需要改读：Workbench、TaskContract、Web API 是**从零新建**，不是演进既有代码。
2. v2 的核心约束"不推倒重写、复用执行内核"**依然成立且更容易**：要复用的内核（事件日志、策略、沙箱、验证、完成门、上下文）恰恰是仓库经过 10 轮评审加固的最强部分。缺的是它们之上的**任务层**。
3. v2 §15 的依赖图不变，但 WP-1（Task Model + Ledger）成为绝对主轴——它同时是 WP-2/3/4/9 的输入。
4. 需要项目负责人确认：以本盘点取代 v2 §2.2 作为迁移基线（见 §6）。

## 1. 组件级差距矩阵（v2 §4.2 → 真实代码）

等级：✅ 已有可直接复用 ｜ 🟡 部分（需扩展） ｜ ❌ 缺失（需新建）

| v2 组件 | 现状 | 等级 | 证据与说明 | 对应 WP |
|---|---|---|---|---|
| Application Layer（CLI/API/UI） | 仅 CLI 8 命令 | 🟡 | cli.py:353-418；HTTP/UI 为零 | WP-9 新建 |
| Task Intake（Goal/Constraints/Workspace） | `run -t` 自由文本 + workspace 快照 | 🟡 | session.py:232-260 快照已有；无结构化 TaskSpec | WP-1 |
| Supervisor | 无。Harness 自驱动，无全局控制层 | ❌ | — | WP-3 |
| Planner | `update_plan` 工具维护平铺 TaskPlan；无 DAG/依赖 | 🟡 | domain.py:255-292；gate 已强制"计划有效 + 步骤带证据" | WP-3 |
| Runtime Scheduler / Worker | 无。单会话单循环 | ❌ | — | WP-2 |
| Task Protocol（assign/progress/blocked/result） | 无 worker 间消息；但事件日志已具备"信封"要素（schema_version/seq/ts/校验/幂等键） | 🟡 | events.py 的日志基建可承载消息事件 | WP-4 |
| Tool Registry / Policy Engine | 完整：20 工具 + 固定管线 + fail-closed 审批 | ✅ | runtime.py:83-224；policy.py | WP-6 扩展任务级 scope |
| Sandbox / Workspace Manager | L1（Windows 受限令牌+Job Object）/ L0 完整；**无 per-worker 工作区** | 🟡 | sandbox/；`workspace_scope` 概念不存在 | WP-5 新建 worktree + WP-6 扩展 |
| Integration Coordinator（diff/merge/冲突） | 无。编辑直接落在用户工作区（单 Agent 语义） | ❌ | — | WP-5 |
| Context Provider | 打分/预算/压缩/artifact 审计完整（会话级） | ✅ | context.py | WP-7 仅需 Worker 级装配 |
| Verification Engine | L0-L5 阶梯 + 基线 + diff-aware 雏形 | ✅ | verify.py；loop.py:367-389 | WP-8 扩展任务/集成级 |
| Completion Gate | 会话级 13 类机器判定，证据绑定 | ✅ | verify.py:548-675 | WP-8 扩展子任务/集成级 |
| Task Ledger | 无任务层持久化。会话事件日志是唯一真相，sessions 目录天然持久 | 🟡 | events.py；session.py | WP-1 新建 |
| 预算（Token/步数/时间） | 会话级四项预算 + 循环守卫 | 🟡 | loop.py:162-172、418-445 | WP-2 扩展 worker 级 |

## 2. v2 §2.3"已知技术边界"逐项核实

| # | v2 文档的担心 | 核实结果 |
|---|---|---|
| 1 | TaskContract 未暴露为 Workbench HTTP 输入 | **方向对、事实更弱**：既无 HTTP 层也无 TaskContract，二者都要新建（WP-1、WP-9） |
| 2 | Task Registry 内存态、重启丢任务 | **不适用**：无任务注册表；会话本身持久在 `~/.jinlin-agent/sessions`（重启可 resume，session.py:146-167）。任务级注册表随 WP-1 新建，从一开始就要持久 |
| 3 | NETWORK/DELETE 能力未在所有工具路径强制 | **部分成立**：DELETE 有配置开关 + 策略路径检查 + trash 隔离（policy.py:333-339；trash.py）；NETWORK 只有 `run_command` 的命令级风险标注（policy.py:99、146-147），无域名/端口级策略；且**没有任何工具声明** NETWORK/DESTRUCTIVE/EXTERNAL side_effect（grep 0 命中）——能力模型目前是"side_effect 枚举 + 命令风险"，不是 v2 §9.4 要求的"能力→资源映射" |
| 4 | VerificationProfile↔Plan 映射未验证 | 该概念不存在；但 diff-aware 雏形已在：无文件变更但 claim 提到测试 → 仍强制 L3/L5（loop.py:387-388）；有代码变更 → 强制 L0/L1/L3（loop.py:377-386 + verify.py:605-628）。缺的是**任务合同声明的验收标准**（当前完成门第 1 条只查 request 非空，verify.py:554-556） |
| 5 | 两套状态机可能矛盾 | **风险在前方**：只有一套 SessionState；WP-1 设计 TaskStatus 时必须给出与 SessionStatus 的显式映射与权威源（→ ADR-003），否则 v2 担心的事会真的发生 |
| 6 | 不能默认已有 OS 级 Sandbox | **好于预期但有限定**：Windows L1（受限令牌+Job Object）已实现并有 405 行实测测试；诚实降级机制完备。限定：只覆盖 shell 子进程；进程内文件工具无 OS 隔离；网络无防火墙级隔离（README §5 已如实声明） |
| 7 | 多 Worker 工作区/Git/冲突恢复/预算 | **全部缺失**：单 Agent；会话级预算已有（loop.py:162-172），worker 级没有 |

## 3. 盘点新发现（v2 文档未提及，实施时不得当作已有能力）

1. **L4（行为验证）已声明未实现**：枚举存在（domain.py:97），`run_level` 无分支 → skipped（verify.py:275-285）。
2. **`report.written` 事件没有 fold handler**（loop.py:462 发出，state.py:378-412 未收编）——无害但不一致，WP-1 定事件 schema 时收编。
3. **`paused` 状态是预留**：只有枚举定义（domain.py:49）与 CLI 退出码映射（cli.py:115），无任何事件会置为 paused。
4. **任务目标无结构化验收标准**：`request` 是自由文本，完成门无法机器判定"用户要什么"，只能判定"改动是否验证过"。v2 的 Explicit Contracts 原则（§3.3-4）正是补这个。
5. **证据引用解析较宽松**：接受文件路径 / ver_* / artifact_* / "L3 tests" / 命令别名片段（verify.py:702-761）——通用 claim-evidence 语义匹配仍是 AGENT.md §10 已记录的延后项。
6. 其他 AGENT.md §10 已登记的延后项继续有效：跨文件崩溃事务（transaction manifest）、最终 messages 二次预算闭环、递归脱敏、事件 hash chain、30–50 真实任务评测。
7. **有价值的新发现**：`evalkit.py` 18 探针 + scripted provider 是 WP-10 评测体系的现成基座；`trash.py` 的"manifest + 指纹归属证明 + 隔离区验证"模式可直接移植为未来 Worker 产物归属与清理的实现范式。

## 4. 对 v2 §11 技术栈建议的适配

| v2 建议 | 本仓库适配 | 理由 |
|---|---|---|
| Pydantic 强类型 | **不引入**，延续 domain.py 手写 dataclass + to/from_dict | 硬规则 1：零第三方运行时依赖（AGENTS.md）；v2 §11.2 本身也写了"或当前项目已有模型体系" |
| SQLite | stdlib `sqlite3` 可用，符合零依赖 | → ADR-008 |
| asyncio 或 threading 统一选型 | 建议**保持同步 + 线程池跑 Worker** | 当前 Provider.complete 全同步；混用两套并发模型正是 v2 §11.2 警告的"混乱双并发体系" |
| 必要时评估 FastAPI | 不引入；HTTP 用 stdlib `http.server` 起步 | 同零依赖约束；WP-9 后置后需求可能更小 |
| Git Worktree | stdlib `subprocess` 调 git 即可（git 只读工具已有先例） | 无需新依赖 |

## 5. 对 v2 §15 实施顺序的微调建议

- 主轴不变：WP-0 → **WP-1（Task Model + Ledger，绝对优先）** → WP-2（Runtime）→ WP-3/WP-4 → WP-5 → WP-6。
- WP-7（上下文）收窄：context.py 已覆盖大半，只补"Worker 级预算 + 依赖任务产物注入"。
- WP-9（Web UI）后置到 Task Ledger 与 Runtime API 稳定之后（当前连 HTTP 层都没有，UI 无处挂钩）。
- WP-10 不必从零建：evalkit 探针框架按 v2 §14-WP10 的任务集扩展即可。
- 安全先行（v2 §15 末尾要求）：WP-6 中"任务级能力集 + workspace_scope 路径范围"必须**先于** Worker 获得写权限落地（→ ADR-006）。

## 6. 决策清单（2026-11-03 负责人已全部确认）

1. ✅ **基线修正**：v2 §2.2 资产表以本盘点（current-state.md）为准；`feature/workbench-*` 分支不存在，相关表述作废。
2. ✅ ADR-001 ~ ADR-010（[decisions/](decisions/)）全部由 **Proposed** 改为 **Accepted**。
3. ✅ 优先级确认：WP-1 先行（WP-2 的 WorkerFactory 需要 TaskSpec 才有东西可实例化）。
4. ✅ 零第三方依赖约束在 v2 阶段继续有效（Pydantic/FastAPI 不引入）。
