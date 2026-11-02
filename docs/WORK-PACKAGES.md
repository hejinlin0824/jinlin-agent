# Jinlin Agent — 剩余工作包与架构演进路线

> 更新：2026-11-03。当前实现基线：feature/workbench-wp2 @ d3ec2d9。此计划面向后续迭代，不代表这些能力已经实现。

## 1. 总目标

把 Jinlin Agent 建设为通用、可靠、可审计的单 Agent Harness：同一执行内核支持只读分析、代码修改、验证、审批、等待用户、长任务和恢复。性能优化必须由跨任务集验证，不能只对某一条只读项目概览 prompt 优化。

## 2. 不可妥协的架构原则

1. 一个 Agent Loop，多种任务合同：任务差异由 TaskContract、CapabilitySet、Policy 和 VerificationPolicy 表达，不复制多套 loop。
2. Prompt 不是安全边界：工具目录过滤与 PolicyEngine 执行时拒绝必须同时存在。
3. 事件是事实，进度是投影：持久化状态事件、瞬时流式 delta、UI 进度不能混为一谈。
4. 模型不能自行宣布成功：完成门按任务合同和真实改动选择适当验证；纯分析不做无意义回归，代码修改不能绕过验证。
5. 不猜测副作用：工具 intent/result 缺口、进程中断、重启后状态不明时进入 uncertain 并要求核对。
6. 任务状态必须可解释：所有状态迁移有合法边、终态不变量、超时和取消语义。
7. 可观测性以里程碑和事件为准：分别测量任务提交、session 创建、首模型请求、首工具、首 token、结束时间，并统计 P50/P95。
8. 优先稳定接口与测试，不为了插件化而拆包；只有出现真实替换需求时再引入 plugin seam。

## 3. 剩余工作包

| 包 | 目标 | 负责边界 | 验收标准 |
|---|---|---|---|
| WP-2.1 任务合同与状态机 | 统一 task/session 生命周期和合法状态迁移 | Runtime：application.py、loop.py、domain.py | 只读、写入、等待审批、等待用户、失败、取消、完成均由同一状态机表达；非法迁移被拒绝 |
| WP-2.2 任务持久化与恢复 | 解决 WorkbenchTask 仅进程内存的问题 | Workbench/Persistence：workbench.py、新 task store、web.py API | 服务重启后能恢复可恢复状态；运行中任务不伪装仍在运行；未知副作用标记 uncertain |
| WP-2.3 取消与追问续接 | 支持用户取消、模型/工具中断、用户回答后继续 | Runtime + Workbench API | 子进程树可终止；取消有界；追问恢复原 Session，不重复已完成副作用 |
| WP-2.4 事件流与断线恢复 | 从轮询演进到可恢复 SSE | Events/Streaming：events.py、web.py、前端订阅 | Last-Event-ID 游标、重复事件幂等、断线补齐、快照对账；不能只实现“能连上”的 SSE |
| WP-2.5 验证策略泛化 | 建立按 diff/任务合同选择 L0-L5 的策略矩阵 | Verification：verify.py、completion gate | 只读/文档/代码/配置/命令任务选择适当验证；跳过有理由；写任务验证证据绑定实际 diff |
| WP-2.6 并发与资源治理 | 全局/每 workspace 并发、队列、背压、关停 | Workbench/Persistence | 多会话隔离；并发上限明确；服务退出能收尾或取消任务；没有无限线程增长 |
| WP-2.7 跨任务评测基准 | 建立覆盖不同任务类别的离线任务集和指标 | Eval：tests、fixtures、scripts | 至少 12 类任务，正向/失败/边界路径；成功率、假成功率、工具失败率、恢复率、P50/P95 和 token 成本可计算 |
| WP-2.8 安全与发布候选 | 安全复核、双平台、真实浏览器验收和发布报告 | Security/Release：policy.py、sandbox、CI、docs | Windows/Ubuntu CI 全绿；安全测试、offline eval、Windows 浏览器人工验收均有独立证据；PR 不自动合并 |

### 推荐顺序

- 第一批：WP-2.1 + WP-2.5。先统一任务合同与“何时算完成”，避免后续恢复和 UI 建在不稳定状态语义上。
- 第二批：WP-2.2 + WP-2.3。再解决持久化、取消和用户追问。
- 第三批：WP-2.4 + WP-2.6。处理事件传输、并发、背压和服务关闭。
- 第四批：WP-2.7 + WP-2.8。用跨任务基准证明泛化，再进入发布候选。
- 暂不做多 Agent、插件市场、自动监听 GitHub/飞书等 SuperAgent 功能；先把单 Agent runtime 做正确。

## 4. 模块责任边界（避免多人冲突）

- Runtime/Lifecycle：任务合同、Loop、终态、取消 token；独占 application.py / loop.py / domain.py。
- Workbench/Persistence：任务仓库、恢复、队列、并发和 API；独占 workbench.py 及 task store。
- Events/Streaming：事件 envelope、持久化游标、SSE 和重连；独占 events.py、web.py 流式路由。
- Verification/Evaluation：验证适用性、证据引用、完成门、跨任务任务集；独占 verify.py 与 eval fixture。
- Security/Platform：Policy、Sandbox、路径和命令边界；独占 policy.py、sandbox/**。
- UI/Acceptance：静态 UI、API 文档、浏览器验收记录；只消费 API，不在前端复制状态机。
- Integrator：维护接口契约、集成测试、CI、发布报告。一个文件同一时间只能有一个 owner；跨包改动先提交接口协议再并行开发。

每个包必须提交：目标与非目标、状态/接口设计、失败模式、单元测试、集成测试、跨平台验证、指标变化、文档和复现步骤。

## 5. 防止“只读任务过拟合”的跨任务测试矩阵

所有修改 Loop、Prompt、Policy、Context 或 Verification 的 PR，至少执行下列离线 scripted-provider 场景：

1. 纯文本问答：不强制调用工具，正常终止，不触发代码验证。
2. 只读项目概览：证据充分后停止，不执行 shell，不出现写工具。
3. 只读代码解释：按需读取，不强制调用 get_project_summary。
4. 只读文档分析：README/文档证据充分即可结束，不创建计划或 completion ceremony。
5. 单文件修复：工具写权限受控，测试失败可修复，完成门有证据。
6. 跨文件功能：多文件修改一致，相关测试执行，diff 与验证绑定。
7. 仅文档修改：运行文档相关检查，不无意义运行全量代码测试。
8. 配置/依赖修改：格式/解析与相关依赖检查。
9. 命令失败/超时：退出码和 stderr 保留，失败上限生效。
10. 审批等待/拒绝/过期：决策与 approval_id 绑定，默认 fail-closed。
11. 用户追问/恢复：回复回到原会话，已完成工具副作用不重复。
12. 模型截断/非法工具 JSON：不把截断文本当终答，解析失败受控重试。
13. 多会话并发：状态隔离、写锁、资源上限有效。
14. 服务重启：完成、等待用户、等待审批、运行中和 uncertain 状态分别有确定语义。
15. Prompt injection/路径逃逸：仓库文本不能提权，策略层独立拒绝。
16. 上下文超预算：关键约束保留，压缩有审计，不静默丢弃。

真实模型抽测是补充项，不得成为 CI 必需条件；测试应固定脚本化响应、文件系统 fixture 和事件断言，避免随机性。

## 6. Definition of Done

- 功能不依赖单一任务措辞或特定仓库结构。
- 至少覆盖两个任务类别，以及成功、失败、边界三种路径。
- 安全限制由代码执行时强制，而非 prompt 自律。
- 状态迁移、事件和恢复语义有测试。
- 全量 pytest + offline eval + Windows/Ubuntu CI 通过。
- 运行时报告包含分支、提交、Python 版本、每项检查耗时、日志和机器可读结果。
- 浏览器验收、真实模型抽测等未自动化项目必须单独列明，不得被自动化全绿替代。

## 7. 一键测试约定

- 双击根目录 test-all.bat：完整测试，安装开发依赖、compileall、全量 pytest（JUnit XML）、离线 Harness eval、Rescue 生命周期和故障注入、CLI 注册、git diff 检查。
- PowerShell 完整运行：./scripts/test-all.ps1
- 快速回归：./scripts/test-all.ps1 -Fast -SkipInstall
- 指定解释器：./scripts/test-all.ps1 -PythonPath C:\path\to\python.exe
- 输出 reports/local 下的 transcript、JUnit XML 和 JSON 报告。
- “ALL CHECKS PASSED”只代表脚本列出的自动化检查通过；浏览器人工验收和真实模型质量另行记录。
