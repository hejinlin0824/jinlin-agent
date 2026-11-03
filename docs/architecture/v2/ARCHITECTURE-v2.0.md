# Jinlin Agent 架构设计 2.0

## 从"多个专用 Agent"转向"统一 Agent Runtime + 动态角色编排"

-   文档版本：2.0
-   文档用途：交付开发团队进行架构评审、任务拆分与实施
-   项目：`hejinlin0824/jinlin-agent`
-   当前工作分支背景：`feature/workbench-wp1` / `feature/workbench-wp2`
-   原则：先盘点、再设计、分阶段迁移；不直接推倒重写

------------------------------------------------------------------------

## 1. 执行摘要

Jinlin Agent 的产品方向需要从"开发多个专用 Agent"（例如
Coder、Reviewer、Project Manager、Tester）调整为：

> **一个统一的 Agent Harness /
> Runtime，根据任务需要动态规划、创建不同职责的执行实例，并通过结构化协议协作。**

这里的角色不是独立产品，也不是独立模型，而是同一个 Agent
执行体系中的不同工作身份。Supervisor、Planner、Worker、Reviewer
等角色共享底层 Agent
Loop、工具系统、会话管理、策略引擎和执行基础设施；它们的差异主要由
RoleSpec、任务目标、上下文、工具权限和验证要求决定。

简单任务由主 Agent 直接完成。复杂任务才进入规划和多 Worker
协作。系统应当能够在执行过程中发现计划失效、任务依赖变化、Worker
失败或结果冲突，并由 Supervisor
选择重试、局部重规划、请求用户澄清或停止任务。

本项目的核心竞争力不应是"有多少个 Agent"，而应是：

1.  是否能可靠判断任务需要何种执行策略；
2.  是否能动态分解任务并控制依赖关系；
3.  是否能安全、可观测地创建和管理 Worker；
4.  是否能通过结构化协议完成协作；
5.  是否能隔离执行环境和代码变更；
6.  是否能以证据为依据验证任务完成；
7.  是否能在质量不下降的前提下提高完成速度。

### 1.1 一句话架构

**一个 Agent 核心 + 一个统一 Runtime + 动态角色实例 + 结构化任务协议 +
隔离执行环境 + 自适应验证。**

### 1.2 明确不做什么

-   不为 Coder、Reviewer、Planner 等角色分别开发独立 Agent 产品。
-   不把"多 Agent 数量"作为系统能力或性能指标。
-   不要求每个任务都进行复杂规划和并行拆分。
-   不自研基础大语言模型；模型通过统一 Model Adapter 接入。
-   不把 Git Worktree 当作完整 Sandbox。
-   不把"程序能够启动"直接等同于"任务已经完成"。
-   不在没有评估证据的情况下引入复杂记忆、向量数据库或多层 Agent 框架。

------------------------------------------------------------------------

## 2. 项目背景与现状

### 2.1 项目最初方向及需要纠正的问题

项目早期容易沿着"一个任务类型对应一个专用 Agent"的方向扩展，例如：

-   专门写代码的 Agent；
-   专门做项目管理的 Agent；
-   专门审核代码的 Agent；
-   专门测试的 Agent。

这种方式在特定产品或模型应用中可能有价值，但对于 Agent Harness
项目而言，容易把开发重点转移到角色产品化，而不是通用执行能力。

业内较成熟的 Agent
系统通常会围绕统一执行内核，按任务动态组织规划、执行、工具调用、审查和恢复。角色是执行时的组织形式，而不是必须独立部署的产品。

因此，Jinlin Agent 应建设通用 Harness，而不是不断增加专用 Agent。

### 2.2 当前仓库中已经存在的资产

以下内容来自当前项目既有开发记录，正式开发前必须以 GitHub
当前分支代码重新核实，不得仅凭本文件假定实现仍然完全一致。

  -----------------------------------------------------------------------------------------------------
  既有模块/能力           当前价值                             V2.0 的处理建议
  ----------------------- ------------------------------------ ----------------------------------------
  `ApplicationService`    CLI/Web 共用的 Agent 应用服务入口    保留，评估其是否适合作为统一 Runtime
                                                               的门面

  Agent Loop              统一模型交互、工具调用和执行循环     保留并明确执行边界

  `SessionManager` /      会话创建、事件持久化和恢复           扩展为任务执行历史的重要基础
  Session Event Log                                            

  `Workbench`             后台任务协调、状态管理和审批入口     演进为 Runtime
                                                               的任务编排层，不另建平行执行循环

  `TaskContract`          任务类型、能力集合、验证配置等合同   演进为正式 TaskSpec / TaskContract

  Task / Session          对执行状态迁移进行约束               统一状态语义，避免两套状态机相互矛盾
  生命周期状态机                                               

  `VerificationPlan`      根据变更类型生成验证计划             演进为证据驱动的验证计划

  `PolicyEngine`          工具调用和副作用策略                 补齐细粒度能力执行时强制

  Web API / Workbench UI  用户发起和观察任务                   逐步展示规划、Worker、审批、验证与结果
  -----------------------------------------------------------------------------------------------------

### 2.3 已知技术边界

根据此前开发状态，以下问题需要在代码盘点中重点确认：

1.  TaskContract 尚未完整暴露为 Workbench HTTP API 的正式输入。
2.  Workbench Task Registry 曾为进程内存态，服务重启后任务 ID
    可能丢失；Session Event Log 与任务注册表的持久化边界需要重新设计。
3.  NETWORK、DELETE
    等能力在合同中存在，不代表策略层已经在所有工具路径上强制执行。
4.  VerificationProfile 与实际 VerificationPlan
    的映射尚需验证，尤其是没有文件变更但明确要求测试的任务。
5.  现有任务状态和 Session
    状态可能存在两套状态表达，需要确定权威状态源。
6.  不能默认当前执行环境已经具备操作系统级 Sandbox。
7.  多 Worker 的独立工作区、Git 集成、冲突恢复和预算控制仍需建设或核实。

------------------------------------------------------------------------

## 3. 产品目标、非目标与设计原则

### 3.1 产品目标

构建一个通用 Agent Harness，使一个 Agent 能够：

-   理解用户目标和约束；
-   判断任务复杂度和风险；
-   决定直接执行还是进入规划；
-   生成带依赖关系的任务计划；
-   根据需要创建不同角色的 Worker；
-   为 Worker 分配独立上下文、权限、预算和执行环境；
-   接收结构化进度、结果、阻塞和风险报告；
-   在必要时局部重规划；
-   管理代码分支、Worktree 和集成；
-   执行分层验证并形成可追溯证据；
-   在完成、失败、取消、等待用户输入或预算耗尽时正确终止。

### 3.2 非目标

V2.0 阶段不追求：

-   自研 32B 或其他基础模型；
-   训练一个专门的 Coder 模型；
-   一开始支持跨机器、跨组织的大规模分布式 Agent 集群；
-   让多个 Worker 无限制地自由对话；
-   用复杂的长期记忆系统替代任务状态和证据管理；
-   完全无人监管地执行高风险删除、外网访问或生产环境操作；
-   通过增加 Worker 数量来掩盖规划或执行质量问题。

### 3.3 核心设计原则

1.  **Single Agent First**：单 Agent
    是默认模式，多角色协作是按需升级模式。
2.  **One Runtime**：所有角色复用同一执行内核。
3.  **Role Is Configuration**：角色通过
    RoleSpec、上下文、权限和任务目标定义。
4.  **Explicit
    Contracts**：任务输入、交付物、权限、预算和验收标准必须结构化。
5.  **DAG-Based Planning**：任务依赖用有向无环图表达。
6.  **Isolated Execution**：Worker 之间默认隔离工作区和执行权限。
7.  **Evidence-Based Completion**：完成状态必须有验证证据支持。
8.  **Least Privilege**：工具和环境权限默认最小化。
9.  **Bounded
    Autonomy**：重试、重规划、并行度、Token、时间和成本都有上限。
10. **Human Control**：关键不确定性和高风险操作可以请求用户决策。

------------------------------------------------------------------------

## 4. 目标架构总览

### 4.1 逻辑架构

``` text
                         User / API / CLI
                                |
                                v
                     +----------------------+
                     |     Application      |
                     |  API / CLI / UI      |
                     +----------------------+
                                |
                                v
                     +----------------------+
                     |   Task Intake        |
                     | Goal / Constraints   |
                     | Workspace / Context  |
                     +----------------------+
                                |
                                v
                     +----------------------+
                     |   Supervisor        |
                     | Task assessment      |
                     | Strategy selection   |
                     | Progress monitoring  |
                     | Replanning / Stop    |
                     +----------------------+
                         |              |
             simple task |              | complex task
                         v              v
                +----------------+  +---------------------+
                | Direct Agent   |  | Planner             |
                | Execution      |  | Task DAG             |
                +----------------+  | Dependencies         |
                                    | Acceptance criteria  |
                                    +---------------------+
                                               |
                                               v
                                    +---------------------+
                                    | Runtime Scheduler  |
                                    | Queue / Concurrency |
                                    | Budget / Retry      |
                                    +---------------------+
                                      |        |        |
                                      v        v        v
                                  Worker A  Worker B  Worker C
                                      |        |        |
                                      +--------+--------+
                                               |
                                               v
                                    +---------------------+
                                    | Task Protocol       |
                                    | Assign / Progress   |
                                    | Blocked / Result    |
                                    | Review / Approval   |
                                    +---------------------+
                                               |
                  +----------------------------+-------------------------+
                  |                            |                         |
                  v                            v                         v
          +---------------+            +---------------+         +---------------+
          | Tool Registry |            | Sandbox       |         | Context       |
          | Policy Engine |            | Worktree      |         | Provider      |
          +---------------+            +---------------+         +---------------+
                  |                            |                         |
                  +----------------------------+-------------------------+
                                               |
                                               v
                                    +---------------------+
                                    | Integration         |
                                    | Diff / Merge        |
                                    | Conflict handling   |
                                    +---------------------+
                                               |
                                               v
                                    +---------------------+
                                    | Verification        |
                                    | Tests / Lint / Build|
                                    | Security / Evidence |
                                    +---------------------+
                                               |
                                               v
                                    +---------------------+
                                    | Completion Gate     |
                                    | Result / Evidence   |
                                    +---------------------+
                                               |
                                               v
                                           User
```

### 4.2 组件职责

#### Application Layer

负责 CLI、HTTP API、Web UI 和用户交互，不直接实现 Agent Loop。

#### Supervisor

负责根任务的全局控制：

-   分析任务目标；
-   选择直接执行或规划执行；
-   启动 Planner；
-   审核计划；
-   调度 Worker；
-   监听任务事件；
-   处理失败和阻塞；
-   决定重试、局部重规划、用户澄清或停止；
-   汇总最终结果。

Supervisor
不应该亲自承担所有具体工作。它主要管理任务，而不是把自己变成一个无边界的万能执行者。

#### Planner

将复杂任务分解为子任务 DAG。Planner 输出结构化计划，不直接执行代码修改。

#### Worker

统一 Runtime 创建的执行实例。Worker 不是一种新的 Agent
产品，而是带有任务、角色、上下文、权限和预算的执行实例。

#### Runtime Scheduler

负责 Worker 生命周期、队列、并发、预算、超时、取消、重试和资源清理。

#### Tool Registry / Policy Engine

统一注册工具，执行能力检查、参数校验、副作用识别、审批与审计。

#### Sandbox / Workspace Manager

为每个 Worker
分配隔离的工作区和执行环境，控制文件系统、进程、网络和资源。

#### Integration Coordinator

负责将各 Worker 的变更安全地集成到根任务集成分支，不允许 Worker
直接覆盖共享目录。

#### Verification Engine

根据任务合同、真实变更、项目配置和风险生成验证计划，执行并保存证据。

#### Task Ledger

保存根任务、子任务、角色实例、状态迁移、消息、预算、审批、Commit、验证结果和最终结论。

------------------------------------------------------------------------

## 5. 动态任务分解与规划

### 5.1 任务复杂度评估

不应只由模型给任务打分，也不能只根据仓库大小决定是否拆分。建议采用"客观项目扫描 +
模型语义判断 + 策略规则"的混合评估。

#### 客观项目扫描信号

-   仓库文件数量、语言分布和代码规模；
-   项目模块和依赖关系；
-   测试数量、测试类型和覆盖范围；
-   Git 工作区是否干净；
-   当前分支、基线 Commit、未提交变更；
-   可能涉及的目录和模块；
-   构建、测试、Lint 命令是否存在；
-   项目是否包含子仓库、生成文件或大型二进制文件。

#### 模型分析信号

-   目标是否明确；
-   是否涉及多个模块；
-   是否存在天然可并行的调查或实现工作；
-   任务之间的依赖关系；
-   技术不确定性；
-   修改风险和回滚成本；
-   是否需要用户提供缺失信息；
-   是否存在高风险外部副作用。

#### 任务策略输出

建议至少支持以下策略：

  策略                     适用情况
  ------------------------ ----------------------------------
  `direct`                 小型、明确、低风险任务
  `plan_then_execute`      中等复杂度，先分析再执行
  `parallel_workers`       存在多个相对独立、可并行的子任务
  `sequential_workers`     任务存在明显先后依赖
  `human_input_required`   目标或约束不明确，无法安全继续
  `refuse_or_stop`         超出能力、权限或安全边界

### 5.2 不要将仓库大小等同于任务复杂度

大型仓库中修改一个独立小函数，可能只需要一个
Agent；小型仓库中同时调查三个互不依赖的问题，也可能适合并行。

复杂度至少要综合：

-   任务语义复杂度；
-   代码影响范围；
-   依赖耦合度；
-   不确定性；
-   风险；
-   验证成本；
-   并行收益。

### 5.3 任务 DAG

复杂任务应表示为 DAG，而不是一段自然语言 TODO 列表。

示例：

``` text
Root Task
  |
  +-- T1: Inspect current architecture
  |      |
  |      +-- T2: Identify API boundary issues
  |      |
  |      +-- T3: Identify persistence issues
  |
  +-- T4: Propose target design
         |
         +-- depends on T1, T2, T3
         |
         +-- T5: Implement persistence
         |
         +-- T6: Implement API changes
                |
                +-- T7: Integration tests
```

每个节点至少包含：

-   唯一 ID；
-   目标；
-   输入；
-   输出/交付物；
-   依赖任务；
-   角色建议；
-   所需能力；
-   验收标准；
-   预算；
-   超时；
-   重试策略；
-   状态。

### 5.4 动态重规划

执行中出现异常时，不应直接推倒整个计划。

Supervisor 先判断事件类型：

  事件               推荐动作
  ------------------ ------------------------------
  Worker 短暂失败    有界重试
  工具临时错误       重试或更换工具
  任务目标不明确     请求用户补充
  子任务计划不成立   局部重规划
  上游结果变化       将受影响下游任务标记为 stale
  Worker 结果冲突    暂停集成并请求裁决
  预算耗尽           停止或请求用户授权追加
  安全风险           立即暂停相关执行

保留仍然有效的任务成果；只有依赖已变化、证据失效或输入基线改变的任务才需要重新执行。

### 5.5 经验沉淀

"吸取经验"不能只靠模型在对话里说一句"我记住了"。

建议把可复用经验保存为结构化记录：

-   失败原因；
-   触发条件；
-   采用的修复策略；
-   修复是否成功；
-   适用项目/语言/工具；
-   证据链接；
-   可信度和时间。

第一阶段先将经验作为项目级或任务级检索材料，不训练模型，不允许未经验证的经验自动变成强制规则。

------------------------------------------------------------------------

## 6. Subagent Runtime

### 6.1 Runtime 的含义

Runtime
是统一的执行环境和生命周期管理器。它负责创建、运行、观察、暂停、恢复、取消和清理
Agent 执行实例。

可以把它理解为：

-   Runtime：执行实例工厂和管理系统；
-   RoleSpec：岗位说明书；
-   Worker：具体执行实例；
-   TaskSpec：工作任务；
-   Tool：可用工具；
-   Sandbox：执行场地；
-   Task Ledger：工作记录。

### 6.2 一个 Runtime，多种角色

建议角色初始集合：

  Role           主要职责               默认工具范围
  -------------- ---------------------- ------------------------------
  `supervisor`   全局控制、决策、汇总   任务管理、状态查询、审批请求
  `planner`      任务分析和 DAG 生成    只读项目扫描、计划输出
  `worker`       执行具体子任务         根据 TaskContract 授权
  `reviewer`     独立审查差异和证据     只读文件、Diff、测试结果
  `verifier`     执行验证               测试、构建、Lint 等受控工具

这些角色不需要各自的 Agent Loop。角色配置可以定义：

-   system instruction；
-   任务目标；
-   可访问的上下文；
-   工具白名单；
-   能力集合；
-   输出协议；
-   预算；
-   是否允许委派；
-   是否允许修改文件；
-   验收标准。

### 6.3 Worker 生命周期

建议状态：

``` text
created
  -> queued
  -> running
      -> waiting_approval
      -> waiting_user
      -> blocked
      -> completed
      -> failed
      -> cancelled
```

需要明确终态不可复活。重试应创建新的 Attempt，或通过明确的 retry
transition 管理，不能把已完成的 Worker 静默改回 running。

### 6.4 Runtime 核心职责

1.  创建 Worker 实例；
2.  校验 TaskContract 和 RoleSpec；
3.  分配上下文和工具权限；
4.  分配 Sandbox / Worktree；
5.  执行并发和预算控制；
6.  接收和持久化 Worker 事件；
7.  处理超时、取消和重试；
8.  处理审批和用户输入等待；
9.  清理资源；
10. 提供可观测性数据。

### 6.5 并发控制

不要一开始追求大规模并发。建议：

-   默认并发数保守；
-   根任务级并发预算；
-   项目级并发上限；
-   每个 Worker 独立 Token / 时间 / 工具调用预算；
-   CPU、内存、磁盘和进程数限制；
-   同一文件或同一模块的写任务避免并发；
-   只读调查任务可以较容易并行。

并发调度必须考虑资源和依赖，不应仅由模型自由决定。

------------------------------------------------------------------------

## 7. 任务通信协议

### 7.1 两层协作机制

必须分清：

1.  **Task Protocol：** Agent 实例之间如何分配任务、汇报状态和交付成果。
2.  **Git Collaboration：** 多个 Worker
    如何安全地管理代码版本和合并变更。

Git 不负责表达 Worker 是否阻塞、是否需要用户输入或是否完成任务；Task
Protocol 也不负责解决代码合并冲突。

### 7.2 消息信封

建议统一消息结构：

``` json
{
  "protocol_version": "1.0",
  "message_id": "msg_01...",
  "root_task_id": "task_root_...",
  "task_id": "task_...",
  "worker_id": "worker_...",
  "attempt": 1,
  "message_type": "task.progress",
  "created_at": "2026-01-01T12:00:00Z",
  "correlation_id": "msg_parent_...",
  "payload": {},
  "artifacts": [],
  "evidence": []
}
```

字段要求：

-   `protocol_version`：协议版本；
-   `message_id`：全局唯一；
-   `root_task_id`：根任务；
-   `task_id`：当前子任务；
-   `worker_id`：执行实例；
-   `attempt`：执行尝试次数；
-   `message_type`：消息类型；
-   `created_at`：时间；
-   `correlation_id`：关联消息；
-   `payload`：消息正文；
-   `artifacts`：产物引用；
-   `evidence`：证据引用。

消息应可校验、可持久化、可重放；接收端应处理重复消息，避免重复提交或重复执行副作用。

### 7.3 核心消息类型

-   `task.assigned`
-   `task.accepted`
-   `task.progress`
-   `task.blocked`
-   `task.input_required`
-   `task.approval_required`
-   `task.result`
-   `task.failed`
-   `task.cancelled`
-   `review.requested`
-   `review.accepted`
-   `review.rejected`
-   `integration.conflict`
-   `verification.started`
-   `verification.completed`

### 7.4 Worker 交付协议

Worker 完成任务时必须返回结构化结果：

``` json
{
  "status": "completed",
  "summary": "完成了用户资料 API 的输入校验",
  "changed_files": [
    "src/api/profile.py",
    "tests/test_profile.py"
  ],
  "commit": "abc123...",
  "tests": [
    {
      "command": "pytest tests/test_profile.py",
      "status": "passed",
      "exit_code": 0
    }
  ],
  "known_risks": [],
  "follow_up": []
}
```

Worker
不得只回复"完成了"。必须给出修改范围、Commit、验证结果和未解决风险。

### 7.5 协议可靠性

需要考虑：

-   消息持久化；
-   幂等处理；
-   超时；
-   重复投递；
-   乱序事件；
-   Worker 崩溃；
-   Runtime 重启；
-   任务恢复；
-   协议版本升级；
-   非法消息拒绝。

第一阶段可采用本地数据库持久化和进程内调度，不需要立即引入
Kafka、RabbitMQ 等消息中间件。

------------------------------------------------------------------------

## 8. 上下文工程

### 8.1 设计原则

上下文工程不是简单地把所有项目文件塞进
Prompt。应按需提供、可追溯、有限预算。

建议分为五层：

1.  **System / Role Context**：稳定的系统和角色约束。
2.  **Task Context**：当前任务目标、输入、验收标准和依赖。
3.  **Project Context**：项目结构、架构说明、开发约定和相关文档。
4.  **Working Context**：当前工具调用、修改、测试和中间发现。
5.  **Evidence Context**：测试日志、Diff、Commit、错误信息和审查结论。

### 8.2 Context Provider

通过统一接口按需加载：

-   项目元数据；
-   文件树；
-   指定文件；
-   符号/函数定义；
-   Git Diff；
-   测试结果；
-   任务依赖产物；
-   相关历史决策。

不要让每个 Worker 自己实现一套文件检索和上下文拼装逻辑。

### 8.3 上下文预算

为每个 Worker 设置 Token 预算。上下文优先级建议：

1.  当前任务合同；
2.  直接依赖任务的交付物；
3.  与任务直接相关的代码；
4.  项目规范；
5.  历史经验；
6.  其他背景材料。

当上下文接近预算时：

-   压缩已完成步骤；
-   保留关键决策和证据引用；
-   移除重复工具输出；
-   保留未完成事项；
-   不得丢弃权限约束和验收标准。

### 8.4 第一阶段不做复杂记忆系统

先实现可解释的 Context
Provider、检索和摘要机制。只有当真实评估证明检索能力不足时，再考虑向量检索或更复杂的长期记忆。

------------------------------------------------------------------------

## 9. 执行环境与安全边界

### 9.1 Git Worktree 与 Sandbox 的区别

Git Worktree 解决的是版本和工作目录隔离，不是完整安全隔离。

Worktree 不会自动阻止 Worker：

-   读取工作区之外的文件；
-   读取环境变量和密钥；
-   访问网络；
-   执行任意 Shell 命令；
-   启动后台进程；
-   修改其他目录；
-   消耗无限 CPU、内存或磁盘。

因此必须采用两层隔离：

-   Git 层：独立 Worktree、分支、Commit；
-   OS 层：文件系统、进程、网络和资源 Sandbox。

### 9.2 推荐代码协作流程

``` text
Root Task
  |
  +-- create integration branch from base commit
  |
  +-- Worker A -> Worktree A -> branch task/A
  |
  +-- Worker B -> Worktree B -> branch task/B
  |
  +-- Worker C -> Worktree C -> branch task/C
                 |
                 v
        Worker creates commit
                 |
                 v
        Reviewer checks diff
                 |
                 v
        Integration Coordinator
                 |
          +------+------+
          |             |
       conflict       clean
          |             |
       rebase/merge    merge
          |             |
          +------+------+
                 |
                 v
        Run affected verification
                 |
                 v
        Integration branch
                 |
                 v
        Root completion gate
                 |
                 v
        Human / authorized release
```

Worker 不得直接写入共享主工作区。每个 Worker 的变更通过 Commit 和 Diff
集成。

### 9.3 B Worker 基线过期的处理

例如 A 先完成并集成，B 后完成：

1.  B 的 Commit 保留，不丢弃。
2.  Integration Coordinator 检测 B 的 base Commit 落后。
3.  获取最新集成分支。
4.  尝试 Rebase 或 Merge。
5.  若有冲突，暂停自动集成。
6.  由 Worker 或专门的冲突解决流程处理。
7.  冲突解决后重新运行受影响测试。
8.  Reviewer 检查最终 Diff。
9.  通过后才集成。

不能简单把 B 的整个目录复制覆盖到 A 的目录。

### 9.4 最小权限策略

权限应由 Runtime 和 Policy Engine 执行，而不是只写在 Prompt 中。

建议能力：

-   `READ`
-   `WRITE`
-   `EXECUTE`
-   `NETWORK`
-   `DELETE`
-   `USER_INPUT`
-   `APPROVAL`
-   `WORKFLOW`

能力还要映射到具体资源：

-   文件读取路径；
-   文件写入路径；
-   可执行命令和参数；
-   网络域名/端口；
-   删除范围；
-   可调用工具；
-   资源配额。

高风险操作默认拒绝或请求审批。

### 9.5 Windows 环境

项目当前用户开发环境以 Windows 为主，因此 Sandbox 不能只在 Linux
容器中验证。

需要评估并选择：

-   Windows Job Objects：进程树和资源管理；
-   Windows 用户/ACL：文件访问边界；
-   Windows Sandbox 或 Hyper-V 隔离：更强的系统隔离；
-   WSL2 / Docker
    Desktop：可作为部分执行任务的隔离后端，但需要评估文件挂载、网络和宿主机访问边界。

不要仅依赖 Python
层的路径检查来宣称安全。路径检查可以作为第一层防护，但不能替代 OS 权限。

### 9.6 审批

审批对象应具体化，例如：

-   `file.delete`
-   `network.request`
-   `shell.execute`
-   `workspace.write_outside_scope`
-   `git.push`
-   `external_service.mutate`

审批必须绑定操作参数、目标资源、任务 ID
和有效期，避免"批准一次"被滥用为无限授权。

------------------------------------------------------------------------

## 10. 自适应验证与评估

### 10.1 完成标准

"程序能运行"只是验证的一部分。任务完成必须结合用户目标、验收标准、实际变更和证据。

建议分层：

  等级   内容            典型示例
  ------ --------------- --------------------------
  L0     基础完整性      Patch 可应用、文件可解析
  L1     静态检查        格式、Lint、类型检查
  L2     局部验证        单元测试、模块级测试
  L3     项目验证        完整测试套件、构建
  L4     集成验证        多模块协作、端到端流程
  L5     安全/专项验证   安全扫描、性能、兼容性

不同任务不要求无差别跑全部等级。验证计划应根据
TaskContract、Diff、项目配置和风险决定。

### 10.2 Diff-aware Verification

-   文档修改：检查链接、格式、文档构建等。
-   配置修改：配置解析、Schema 校验和相关测试。
-   代码修改：Lint、类型检查、相关单测和项目测试。
-   测试修改：运行新增测试及相关回归测试。
-   安全敏感修改：增加安全专项检查。
-   纯分析任务：检查证据完整性，不应虚构代码测试。

如果任务明确承诺"测试通过"，且项目存在测试命令，则必须运行对应测试或明确报告无法执行。

### 10.3 Verification Evidence

每个验证项记录：

-   命令；
-   工作目录；
-   执行时间；
-   退出码；
-   标准输出/错误输出引用；
-   运行环境；
-   对应 Commit；
-   结果状态；
-   是否必需；
-   失败原因。

最终 Completion Gate 只依据可核验的证据判断，不允许模型凭感觉声称通过。

### 10.4 自适应验证

可先运行成本低、反馈快的验证；通过后再执行更昂贵的验证。

但必需等级不能因为预算或模型判断而静默跳过。预算不足时，应将任务标记为未完全验证，而不是成功。

### 10.5 收益评估

项目应以"质量不下降前提下提升效率"为目标。建议收集：

-   任务成功率；
-   必需验证通过率；
-   回归缺陷率；
-   平均完成时间；
-   Token 和工具调用成本；
-   Worker 数量；
-   重规划次数；
-   人工介入次数；
-   合并冲突率；
-   任务取消/失败率。

不要只看速度。速度提升但缺陷率明显上升，不算有效收益。

### 10.6 停止条件

继续执行：

-   最近迭代有可验证进展；
-   预算充足；
-   当前方案仍合理；
-   风险可接受。

触发重新规划：

-   连续失败；
-   依赖变化；
-   结果冲突；
-   计划与仓库事实不符；
-   多次尝试没有改善。

停止：

-   用户取消；
-   达到时间、Token 或成本预算；
-   达到最大重规划次数；
-   出现不可接受的安全风险；
-   连续无进展；
-   任务目标已无法满足。

并发 Worker
数量不应使用固定数字作为唯一停止条件，应结合任务依赖、资源、冲突概率和实际收益。

------------------------------------------------------------------------

## 11. 技术栈建议

### 11.1 总体原则

优先延续当前 Python 技术栈和现有依赖，不因架构升级引入大量新框架。

### 11.2 推荐组件

  --------------------------------------------------------------------------------------
  层                      建议技术                        说明
  ----------------------- ------------------------------- ------------------------------
  语言                    Python 3.12+                    延续当前项目

  API                     当前标准库 HTTP                 不为换框架而换框架
                          服务可继续使用；必要时评估      
                          FastAPI                         

  数据模型                Pydantic 或当前项目已有模型体系 Task/Message/RoleSpec
                                                          强类型校验

  持久化                  SQLite 起步                     Task
                                                          Ledger、事件、审批和产物索引

  并发                    asyncio 或当前 Threading        避免混乱的双并发体系
                          模型统一选型                    

  模型接入                Model Adapter                   支持不同模型供应商和协议

  工具系统                Tool Registry + Policy Engine   统一工具调用和授权

  代码隔离                Git Worktree                    每个 Worker 独立工作区

  执行隔离                Windows Sandbox/Job             按平台选择后端
                          Objects、容器或其他 OS Sandbox  

  验证                    pytest、项目原生构建/测试命令   由 VerificationPlan 调度

  可观测性                结构化日志、事件记录、指标      任务可追溯

  前端                    当前 Workbench 页面逐步增强     不必先重写 UI
  --------------------------------------------------------------------------------------

### 11.3 不建议第一阶段引入

-   Kafka；
-   Kubernetes；
-   大型分布式调度系统；
-   独立微服务拆分；
-   自研向量数据库；
-   复杂 Agent-to-Agent 消息总线；
-   多套平行 Agent 框架。

当单机 Runtime 的实际负载和故障模式证明有必要时，再引入。

------------------------------------------------------------------------

## 12. 核心数据模型建议

### 12.1 TaskSpec

``` json
{
  "task_id": "task_123",
  "root_task_id": "task_root_1",
  "parent_task_id": null,
  "title": "实现用户资料接口校验",
  "description": "为资料更新接口增加输入校验",
  "task_kind": "implementation",
  "status": "queued",
  "dependencies": [],
  "role": "worker",
  "workspace_scope": {
    "read": ["src/", "tests/"],
    "write": ["src/api/profile.py", "tests/test_profile.py"]
  },
  "capabilities": ["READ", "WRITE", "EXECUTE"],
  "acceptance_criteria": [
    "非法输入被拒绝",
    "正常输入能够更新",
    "相关测试通过"
  ],
  "budget": {
    "max_tokens": 30000,
    "max_tool_calls": 80,
    "timeout_seconds": 1800
  },
  "verification_profile": "code"
}
```

### 12.2 RoleSpec

``` json
{
  "role_id": "reviewer",
  "description": "独立审查代码变更和验证证据",
  "system_instruction": "以只读方式审查任务交付物",
  "allowed_capabilities": ["READ"],
  "allowed_tools": ["read_file", "git_diff", "test_result"],
  "can_delegate": false,
  "output_schema": "ReviewResult"
}
```

### 12.3 WorkerRecord

``` json
{
  "worker_id": "worker_123",
  "task_id": "task_123",
  "role_id": "worker",
  "status": "running",
  "attempt": 1,
  "workspace_id": "workspace_123",
  "branch": "agent/task-123",
  "base_commit": "abc123",
  "started_at": "2026-01-01T12:00:00Z",
  "budget_used": {
    "tokens": 1200,
    "tool_calls": 5
  }
}
```

### 12.4 ReviewResult

``` json
{
  "status": "rejected",
  "findings": [
    {
      "severity": "high",
      "file": "src/api/profile.py",
      "line": 42,
      "description": "未处理空值输入",
      "evidence": "tests/test_profile.py::test_null_profile"
    }
  ],
  "required_actions": [
    "增加空值校验",
    "补充回归测试"
  ]
}
```

------------------------------------------------------------------------

## 13. 关键流程设计

### 13.1 简单任务流程

``` text
User Request
  -> Task Intake
  -> Complexity Assessment
  -> Direct Execution
  -> Verification Plan
  -> Completion Gate
  -> Result
```

不创建 Worker，不生成多任务 DAG，不增加无意义的审查环节。

### 13.2 复杂任务流程

``` text
User Request
  -> Task Intake
  -> Complexity Assessment
  -> Planner creates DAG
  -> Supervisor validates plan
  -> Runtime schedules Workers
  -> Workers execute in isolated workspaces
  -> Workers send structured results
  -> Reviewer checks deliverables
  -> Integration Coordinator merges changes
  -> Verification Engine runs checks
  -> Completion Gate
  -> Final report
```

### 13.3 Worker 阻塞流程

``` text
Worker
  -> task.blocked / task.input_required
  -> Supervisor evaluates
      -> resolve using available evidence
      -> reassign / retry
      -> replan affected tasks
      -> ask user
      -> stop
```

### 13.4 取消流程

用户取消根任务时：

1.  根任务进入 `cancelling`；
2.  Runtime 向所有活动 Worker 发出取消信号；
3.  终止受控进程树；
4.  停止尚未启动的任务；
5.  保存已产生的 Commit 和证据；
6.  清理临时资源；
7.  根任务进入 `cancelled`；
8.  向用户报告已完成和未完成部分。

取消不能只修改数据库状态，必须尽可能停止真实执行。

------------------------------------------------------------------------

## 14. 实施任务包划分

建议采用阶段式实施。每个阶段先完成设计和验收，再进入下一阶段。避免一次性大重构。

### WP-V2.0-0：现状盘点与架构边界确认

**目标：** 以真实代码为准，明确现有资产、技术债和迁移路径。

工作项：

1.  检查当前分支和 Git 状态；
2.  阅读 `AGENTS.md`、README 和架构文档；
3.  梳理 ApplicationService、Agent
    Loop、Workbench、Session、TaskContract、PolicyEngine、Verifier；
4.  画出现有调用链；
5.  确认状态持久化边界；
6.  确认工具权限的实际执行路径；
7.  确认 Windows 下现有执行隔离能力；
8.  运行现有完整测试；
9.  输出差距矩阵和 ADR。

交付物：

-   `docs/architecture/v2/current-state.md`
-   `docs/architecture/v2/gap-analysis.md`
-   `docs/architecture/v2/decisions/ADR-*.md`
-   当前测试基线报告

验收：

-   每个结论都能定位到代码、测试或配置；
-   不把设计意图当成已实现能力；
-   不修改 `main`；
-   先形成可审查的方案，不大规模改代码。

### WP-V2.0-1：统一 Task Model 与 Task Ledger

**目标：** 统一任务、子任务、Worker、Attempt、消息和证据的数据模型。

工作项：

1.  设计
    TaskSpec、RoleSpec、WorkerRecord、TaskMessage、ArtifactRef、EvidenceRef；
2.  定义根任务与子任务关系；
3.  定义状态机和合法迁移；
4.  建立 SQLite Repository；
5.  设计事件记录和快照；
6.  实现重启后的任务恢复；
7.  设计幂等消息处理；
8.  从现有 TaskContract / Session 数据渐进迁移。

交付物：

-   类型定义和 Schema；
-   Repository；
-   数据迁移；
-   状态机测试；
-   重启恢复测试。

验收：

-   进程重启后能恢复任务状态；
-   非法状态迁移被拒绝；
-   重复消息不会造成重复副作用；
-   旧 API 在兼容窗口内继续工作。

### WP-V2.0-2：统一 Agent Runtime 与 Worker Lifecycle

**目标：** 在现有 Agent Loop 上构建统一执行实例管理，不复制执行引擎。

工作项：

1.  定义 Runtime 接口；
2.  将现有 ApplicationService 作为候选执行门面进行重构评估；
3.  定义 WorkerFactory；
4.  将 RoleSpec 注入统一 Agent Loop；
5.  Worker 绑定 TaskSpec、上下文、工具权限和预算；
6.  实现启动、运行、等待、恢复、取消和清理；
7.  实现并发限制；
8.  实现超时、重试和 Attempt；
9.  将 Runtime 事件写入 Task Ledger。

交付物：

-   Runtime；
-   WorkerFactory；
-   Lifecycle 管理；
-   单 Worker 与多 Worker 测试。

验收：

-   不同角色复用同一执行核心；
-   Worker 有独立状态和预算；
-   取消能停止实际任务；
-   Runtime 崩溃后可恢复或明确标记不可恢复；
-   不产生第二套 Agent Loop。

### WP-V2.0-3：动态规划与 Supervisor

**目标：** 实现按任务复杂度选择直接执行、规划或多 Worker 执行。

工作项：

1.  建立项目扫描器；
2.  定义 Complexity Assessment；
3.  设计 Planner 输出 Schema；
4.  实现 DAG 校验；
5.  实现依赖调度；
6.  实现 Supervisor 计划审核；
7.  实现局部重规划；
8.  实现 stale 下游任务识别；
9.  实现用户澄清流程；
10. 加入重规划和预算上限。

交付物：

-   Project Scanner；
-   Complexity Assessor；
-   Planner；
-   DAG Scheduler；
-   Supervisor；
-   规划测试集。

验收：

-   简单任务默认不创建 Worker；
-   复杂任务能够生成有效 DAG；
-   DAG 不允许循环依赖；
-   依赖未满足的任务不会启动；
-   计划失效时能够局部重规划；
-   达到预算或停止条件时终止。

### WP-V2.0-4：Task Protocol 与 Worker 协作

**目标：** 建立可靠、可持久化的 Agent 内部通信协议。

工作项：

1.  定义消息信封；
2.  定义消息类型和 Schema；
3.  实现消息校验；
4.  实现消息持久化；
5.  实现幂等消费；
6.  实现进度、阻塞、交付和失败报告；
7.  实现 Reviewer 结果协议；
8.  实现协议版本兼容；
9.  增加故障注入测试。

交付物：

-   Protocol Schema；
-   Message Repository；
-   Message Dispatcher；
-   Worker Report；
-   协议兼容测试。

验收：

-   消息可以追溯；
-   重复和乱序消息可处理；
-   Worker 崩溃后任务状态不会失真；
-   不允许只用自然语言"完成"作为机器可判定的交付结果。

### WP-V2.0-5：Git Worktree Integration

**目标：** 让多个 Worker 安全地并行修改同一个仓库。

工作项：

1.  实现 Workspace Manager；
2.  为 Worker 创建独立 Worktree；
3.  记录 base Commit；
4.  管理任务分支命名；
5.  生成 Commit 和 Diff；
6.  实现集成分支；
7.  检测基线过期；
8.  实现 Rebase/Merge 流程；
9.  冲突时暂停并交给解决流程；
10. 集成后运行受影响验证；
11. 清理临时 Worktree。

交付物：

-   Git Workspace Manager；
-   Integration Coordinator；
-   冲突处理流程；
-   多 Worker 并发修改测试。

验收：

-   Worker 不会覆盖其他 Worker 的工作区；
-   基线过期能够检测；
-   冲突不会被静默忽略；
-   合并后重新验证；
-   根任务完成前不自动修改项目稳定分支。

### WP-V2.0-6：Sandbox 与细粒度权限

**目标：** 将权限从 Prompt 约定变成 Runtime 可执行的安全边界。

工作项：

1.  梳理所有工具和副作用；
2.  定义能力到工具的映射；
3.  实现路径级读写权限；
4.  实现命令白名单和参数校验；
5.  实现网络策略；
6.  实现删除审批；
7.  实现进程树管理；
8.  评估 Windows Sandbox / Job Objects / ACL / WSL2 / Docker；
9.  资源限制；
10. 安全审计和攻击测试。

交付物：

-   Policy Enforcement；
-   Sandbox Backend；
-   Approval Flow；
-   Security Test Matrix。

验收：

-   Prompt 注入不能绕过权限；
-   Worker 不能访问授权范围之外的文件；
-   不能任意访问网络；
-   高风险操作必须经过授权；
-   Worker 取消后子进程不会遗留；
-   Windows 环境有真实隔离测试证据。

### WP-V2.0-7：上下文工程

**目标：** 提供统一、按需、有限预算、可追溯的上下文。

工作项：

1.  定义 Context Provider 接口；
2.  实现项目结构和文件检索；
3.  实现 Git Diff 上下文；
4.  实现依赖任务产物注入；
5.  实现上下文预算；
6.  实现摘要压缩；
7.  保留关键证据和决策；
8.  增加上下文质量评估。

交付物：

-   Context Provider；
-   Context Builder；
-   Token Budget Manager；
-   Compaction；
-   评估样例。

验收：

-   Worker 只获得任务所需上下文；
-   不会因压缩丢失权限和验收标准；
-   上下文引用可追溯；
-   相比全量上下文有可测量的成本或质量收益。

### WP-V2.0-8：自适应验证与 Completion Gate

**目标：** 让任务完成基于实际证据，而不是模型自我判断。

工作项：

1.  完善 VerificationPlan；
2.  将 TaskContract 和实际 Diff 结合；
3.  根据项目配置识别测试、构建和 Lint 命令；
4.  支持分层验证；
5.  保存每项验证证据；
6.  实现必需验证等级；
7.  实现失败阻断；
8.  实现 Reviewer 对 Diff 和证据的审查；
9.  形成最终完成报告。

交付物：

-   Verification Engine；
-   Evidence Repository；
-   Completion Gate；
-   端到端测试。

验收：

-   必需验证失败不能标记成功；
-   可选验证实际执行失败时应按策略处理；
-   测试未执行必须明确说明；
-   结果能关联到确切 Commit；
-   最终报告不夸大通过情况。

### WP-V2.0-9：Workbench UI 与可观测性

**目标：** 让用户理解 Agent 正在做什么、为什么这样做、何时需要介入。

建议 UI 展示：

-   根任务目标和当前状态；
-   当前执行策略；
-   任务 DAG；
-   Worker 列表和角色；
-   每个 Worker 的状态、预算和进度；
-   当前审批请求；
-   阻塞原因；
-   Git 分支和 Commit；
-   Diff；
-   验证计划和证据；
-   重规划历史；
-   最终交付物。

开发者详情默认折叠，普通用户不应被内部调试信息淹没。

验收：

-   页面状态与 Task Ledger 一致；
-   刷新或服务重启后任务状态可恢复；
-   不显示虚假的实时进度；
-   用户能取消任务、响应追问和处理审批。

### WP-V2.0-10：基准评估与发布

**目标：** 用真实任务证明架构有价值。

建立任务集：

-   简单问答；
-   代码库理解；
-   单文件 Bug 修复；
-   多模块功能开发；
-   文档生成；
-   测试补充；
-   多模块并行调查；
-   有依赖的多阶段重构；
-   冲突注入；
-   Worker 崩溃恢复；
-   权限越界攻击；
-   预算耗尽和用户取消。

对比：

-   单 Agent；
-   统一 Runtime + 动态规划；
-   多 Worker 协作。

指标：

-   任务成功率；
-   质量/回归缺陷；
-   完成时间；
-   Token 成本；
-   工具调用次数；
-   人工介入；
-   重规划次数；
-   冲突率；
-   安全测试通过率。

只有当多 Worker
在相同质量门槛下，对特定任务类别稳定带来收益时，才默认启用该策略。

------------------------------------------------------------------------

## 15. 建议开发顺序与里程碑

不建议严格按编号机械并行。推荐顺序：

### Milestone A：架构盘点

-   WP-V2.0-0

### Milestone B：统一数据与执行基础

-   WP-V2.0-1
-   WP-V2.0-2

### Milestone C：动态编排与协议

-   WP-V2.0-3
-   WP-V2.0-4

### Milestone D：安全代码协作

-   WP-V2.0-5
-   WP-V2.0-6

### Milestone E：上下文与验证

-   WP-V2.0-7
-   WP-V2.0-8

### Milestone F：产品化与评估

-   WP-V2.0-9
-   WP-V2.0-10

依赖关系：

``` text
WP-0
 |
 v
WP-1
 |
 v
WP-2
 |\
 | \ 
 v  v
WP-3 WP-4
  \  /
   \/
   WP-5
    |
    v
   WP-6

WP-7 可在 WP-2 后逐步推进
WP-8 依赖 WP-1、WP-5 的任务/证据接口
WP-9 依赖 Task Ledger 和 Runtime API
WP-10 在每个里程碑持续执行，最终形成发布门槛
```

安全能力应在 Worker
真正获得写入和执行能力之前落地，不能等到所有功能完成后再补。

------------------------------------------------------------------------

## 16. 测试与验收策略

### 16.1 单元测试

-   TaskSpec / RoleSpec Schema；
-   状态机迁移；
-   DAG 校验；
-   预算扣减；
-   消息幂等；
-   权限决策；
-   VerificationPlan。

### 16.2 集成测试

-   Supervisor 创建 Planner；
-   Planner 输出 DAG；
-   Scheduler 启动 Worker；
-   Worker 发送进度和交付；
-   Reviewer 审核；
-   Integration Coordinator 合并；
-   Verification Engine 执行；
-   Completion Gate 汇总。

### 16.3 故障注入

必须覆盖：

-   Worker 启动后崩溃；
-   模型 API 超时；
-   工具执行超时；
-   Runtime 重启；
-   消息重复；
-   消息乱序；
-   Git 冲突；
-   Sandbox 创建失败；
-   审批拒绝；
-   用户取消；
-   Token 预算耗尽；
-   必需测试失败。

### 16.4 安全测试

-   路径穿越；
-   符号链接越权；
-   Prompt Injection；
-   Shell 参数注入；
-   网络访问越权；
-   环境变量泄漏；
-   子进程逃逸；
-   删除越权；
-   Worker 伪造完成消息；
-   伪造测试证据。

### 16.5 Windows 验收

由于用户主要在 Windows 环境使用，CI 和本地验收必须覆盖 Windows：

-   Python 环境安装；
-   服务启动；
-   Worker 创建和清理；
-   Worktree 操作；
-   PowerShell 命令；
-   路径权限；
-   进程取消；
-   Runtime 重启恢复；
-   Sandbox 约束。

不能只在 Linux CI 通过后就宣称 Windows 安全隔离完成。

------------------------------------------------------------------------

## 17. 业内研究与参考实现

以下资料用于理解设计模式和工程取舍，不意味着照搬某一家系统。

### 17.1 OpenAI Agents / Agent Orchestration

重点学习：

-   Agent、工具和交接的统一抽象；
-   Trace 和执行可观测性；
-   Guardrails；
-   Human-in-the-loop；
-   Agent handoff 与 workflow 的边界。

参考： - OpenAI Agents
SDK：<https://openai.github.io/openai-agents-python/> - OpenAI Agents
文档：<https://platform.openai.com/docs/guides/agents>

### 17.2 DeepSeek Harness / SuperAgent 相关实践

研究重点：

-   Harness 如何组织模型、工具和执行环境；
-   长任务中的状态管理；
-   规划、执行和恢复；
-   任务分解与工具使用；
-   评估驱动的迭代。

需要注意：对公开项目、论文或演示的理解应以可验证的代码和文档为准，不要把社区二手描述当成官方完整架构。

### 17.3 Anthropic：Effective Context Engineering

重点学习：

-   有限上下文预算；
-   Just-in-time context；
-   检索和工具；
-   上下文压缩；
-   长任务中的信息保留。

参考：
<https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>

### 17.4 Microsoft Agent / Harness 工程实践

重点学习：

-   Context Providers；
-   Session Persistence；
-   Compaction；
-   工具调用和审批；
-   Agent 执行的可观察性。

参考： - Microsoft Agent
Framework：<https://learn.microsoft.com/en-us/agent-framework/> -
Semantic Kernel：<https://learn.microsoft.com/en-us/semantic-kernel/>

### 17.5 Git Worktree

重点学习：

-   一个仓库多个工作目录；
-   独立分支；
-   并行开发；
-   合并和冲突处理。

参考： <https://git-scm.com/docs/git-worktree>

### 17.6 OWASP Agent Security

重点学习：

-   最小权限；
-   工具滥用；
-   Prompt Injection；
-   Agent 间信任边界；
-   敏感信息泄漏；
-   人工审批。

参考： <https://genai.owasp.org/>

### 17.7 研究资料使用要求

开发团队在正式采用具体技术前，应记录：

-   官方文档/论文链接；
-   版本和发布日期；
-   采用的设计点；
-   未采用的设计点及原因；
-   对当前项目的适配方式；
-   可验证的测试结果。

不要以"业内最佳实践"作为没有证据的论断。

------------------------------------------------------------------------

## 18. 关键架构决策（ADR）建议

至少需要形成以下 ADR：

1.  **ADR-001：统一 Agent Runtime，而不是多个专用 Agent 产品。**
2.  **ADR-002：简单任务直接执行，复杂任务才进行多角色编排。**
3.  **ADR-003：Task Ledger 是任务状态和执行证据的权威来源。**
4.  **ADR-004：任务通信协议与 Git 代码协作分离。**
5.  **ADR-005：Worker 使用独立 Worktree；Sandbox 提供 OS 级隔离。**
6.  **ADR-006：权限由 Policy Engine 和 Sandbox 强制执行，不依赖
    Prompt。**
7.  **ADR-007：完成状态必须由 Completion Gate 和证据支持。**
8.  **ADR-008：本地 SQLite 起步，不提前引入分布式消息中间件。**
9.  **ADR-009：所有自动化具有明确预算、重试和停止条件。**
10. **ADR-010：保持向后兼容，分阶段迁移现有
    ApplicationService、Workbench 和 Session。**

------------------------------------------------------------------------

## 19. 对提出的六项技术难点的评审结论

### 19.1 动态任务分解与规划

**结论：方向正确，建议采用。**

用户提出的"模型评估任务复杂度---生成计划---拆分可并行任务---执行中发现不合理则上报
Supervisor---重新规划并吸取经验"是合理的核心流程。

需要补充：

-   客观仓库扫描与模型判断结合；
-   使用 DAG 表示依赖；
-   只对失效部分局部重规划；
-   区分重试、换策略、用户澄清和停止；
-   经验必须以结构化证据记录；
-   复杂度不能只由仓库规模决定。

### 19.2 Subagent Runtime

**结论：这是需要补齐的基础设施。**

Runtime 不是另一套模型，也不是专门的 Coder/Reviewer
程序。它是统一执行引擎的实例管理器，负责创建、调度、观察、恢复、取消和清理
Worker。

所有 Worker
共享同一执行核心，但拥有独立任务、上下文、权限、预算和工作区。

### 19.3 任务通信协议

**结论：JSON 协议 + Git 协作的双层设计正确。**

但应区分：

-   Task Protocol 管任务；
-   Git 管代码版本；
-   Integration Coordinator 管合并；
-   Verification 管集成质量。

消息必须有 Schema、版本、ID、关联关系、持久化和幂等处理。

### 19.4 上下文工程

**结论：成熟实践可复用，不应过度投入，但不能忽略。**

先实现 Context
Provider、按需检索、上下文预算、摘要压缩和证据引用。不建议第一阶段自研复杂向量数据库或知识图谱。

### 19.5 执行环境与安全边界

**结论：Git 分支和 Worktree 是正确基础，但不是 Sandbox。**

必须增加操作系统级隔离和最小权限执行。文件、Shell、网络、删除和外部副作用都要在工具执行路径中强制授权。

### 19.6 自适应验证与评估

**结论：质量优先、效率优化的原则正确。**

"能运行通"不等于功能正确。必须依据用户验收标准、Diff、测试和风险制定验证计划。使用成功率、缺陷率、完成时间、Token
成本、冲突率和人工介入率评估收益。

连续无进展、预算耗尽、风险不可接受或达到重规划上限时必须停止。

------------------------------------------------------------------------

## 20. 开发团队执行要求

### 20.1 开始开发前

1.  拉取目标分支最新代码；
2.  检查当前 HEAD、分支和工作区状态；
3.  阅读仓库 `AGENTS.md` 和贡献规范；
4.  执行现有测试，建立基线；
5.  完成 WP-V2.0-0；
6.  提交 ADR；
7.  由项目负责人确认后再进入实现。

### 20.2 Git 约束

-   不直接修改 `main`；
-   使用明确的功能分支；
-   每个 WP 独立提交；
-   PR 描述包含目标、设计、风险、测试和未完成项；
-   不把未验证内容标记为完成；
-   不在同一 PR 中混入无关重构；
-   对破坏性数据迁移提供回滚方案。

### 20.3 每个任务包的报告格式

``` text
# WP 实施报告

## 1. 基线
- Branch:
- Base commit:
- Head commit:

## 2. 目标
- 本 WP 解决什么问题

## 3. 实际改动
- 文件/模块
- 关键设计
- API / Schema 变化

## 4. 测试
- 测试命令
- 测试环境
- 通过/失败数量
- CI 链接

## 5. 安全与兼容性
- 权限变化
- 数据迁移
- 向后兼容
- 风险

## 6. 未完成事项
- 明确列出，不得隐藏

## 7. 本地验收
- Windows 操作步骤
- 预期结果
```

### 20.4 完成定义（Definition of Done）

一个 WP 只有同时满足以下条件才算完成：

-   目标实现；
-   代码审查完成；
-   单元测试通过；
-   相关集成测试通过；
-   安全边界经过验证；
-   文档更新；
-   CI 通过；
-   未完成事项明确记录；
-   本地验收步骤可复现。

------------------------------------------------------------------------

## 21. 最终建议

Jinlin Agent 的方向应明确调整为：

> **建设一个具备动态任务规划、统一 Worker
> Runtime、结构化协作协议、安全隔离执行和证据驱动验证能力的通用 Agent
> Harness。**

不再以"开发多少种 Agent"为目标，而以"同一个 Agent
能否根据任务动态组织自己的执行过程"为目标。

建议立即启动的工作只有一个：

**先完成 WP-V2.0-0：真实代码盘点与架构差距分析。**

不要立刻大规模重构，也不要先增加更多角色。先确认现有
ApplicationService、Agent Loop、Workbench、Session Event
Log、TaskContract、PolicyEngine 和 VerificationPlan
的实际边界，再决定哪些保留、哪些抽象、哪些迁移。

这样可以最大限度复用既有成果，同时避免在错误方向上继续堆叠功能。
