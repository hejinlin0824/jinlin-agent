# Jinlin Agent 使用指南与产品化路线图

> 本文面向直接使用 Jinlin Agent 的开发者与项目维护者。AI/自动化调用仍以稳定 CLI 为主入口；本指南不会取代 CLI 契约。

## 1. 产品是什么

Jinlin Agent 是运行在本地代码仓库中的单 Agent 软件工程助手。它可以理解项目、制定计划、读取和修改文件、运行测试、根据失败继续修复，并通过验证阶梯与完成门检查结果。每次会话都会留下事件、验证和报告证据，支持中断后恢复。

它目前首先是一个 CLI 工程工具，而不是 IDE、云端代码托管服务或生产级安全沙箱。

## 2. 安装

要求 Python 3.11 或更高版本。Windows PowerShell 示例：

```powershell
git clone https://github.com/hejinlin0824/jinlin-agent.git
cd jinlin-agent

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -e .
```

开发和运行测试时安装开发依赖：

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m jinlin_agent eval --suite all
```

安装后检查：

```powershell
jinlin-agent --version
jinlin-agent --help
jinlin-agent doctor --workspace "E:\codebase\your-project"
```

也可以不安装命令入口，直接使用 `python -m jinlin_agent`。

## 3. 配置模型

默认配置面向 DeepSeek OpenAI-compatible API。先在当前 PowerShell 会话中设置密钥：

```powershell
$env:DEEPSEEK_API_KEY = "你的 API Key"
```

推荐在用户级配置文件 `~/.jinlin-agent/config.toml` 中配置模型参数：

```toml
[model]
provider = "openai_compatible"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"
api_key_env = "DEEPSEEK_API_KEY"
reasoning_effort = "high"

[runtime]
max_steps_per_session = 200
max_total_tokens = 4000000

[verification]
test_command = ["python", "-m", "pytest", "-q"]
run_baseline = true

[approval]
shell_medium = "ask"
git_push = "deny"
```

注意：
- API Key 只通过环境变量/凭据读取，不要写入仓库配置或提交到 Git。
- `doctor` 可以检查配置与 Key 是否存在，但不能保证外部 API 网络一定可达。
- 用户级配置位于 `~/.jinlin-agent/config.toml`；项目级配置位于目标仓库的 `.jinlin/config.toml`。
- 项目级配置只能收紧安全策略，不能覆盖放宽用户级限制。

## 4. 本地 Web API（WP-1）与任务 API（WP-2 基础）

WP-1 提供本机回环 HTTP 服务和只读会话查询。WP-2 第一批已增加受认证的后台任务提交、状态轮询、审批决策 API，以及一个无需前端构建工具的本地浏览器工作台页面。

启动：

```powershell
jinlin-agent serve --workspace "E:\codebase\your-project"
```

默认监听 `127.0.0.1:8765`。启动时终端会生成并显示一次性 Bearer Token。打开浏览器访问 `http://127.0.0.1:8765/`，工作台会通过服务端下发的 HttpOnly Cookie 自动认证，无需手动粘贴 Token。终端显示的 Token 仅供直接调用 API；将它视为本机访问凭据，不要粘贴到截图、Issue、聊天记录或项目配置中。按 `Ctrl+C` 停止服务。

当前 API（所有路径均以 `/api/v1` 开头）：

| 方法 | 路径 | 认证 | 用途 |
|---|---|---|---|
| GET | `/health` | 无 | 健康检查和 API 版本 |
| GET | `/doctor` | Bearer | 环境、沙箱和项目诊断 |
| GET | `/sessions` | Bearer | 当前工作区的会话列表 |
| GET | `/sessions/{session_id}` | Bearer | 会话状态摘要 |
| GET | `/sessions/{session_id}/events?tail=100` | Bearer | 最近事件的脱敏摘要，tail 最大 1000 |
| GET | `/sessions/{session_id}/stats` | Bearer | 会话统计信息 |
| GET | `/tasks` | Bearer | 最近的 Workbench 任务 |
| GET | `/tasks/{task_id}` | Bearer | 任务状态和安全审批摘要 |
| POST | `/tasks` | Bearer | 提交任务，默认只读 |
| POST | `/tasks/{task_id}/approvals/decision` | Bearer | 决策当前待处理审批 |

请求示例：

```powershell
$headers = @{ Authorization = "Bearer <终端显示的一次性 Token>" }
Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:8765/api/v1/doctor"
Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:8765/api/v1/sessions"
```

任务提交示例：

```powershell
$body = @{ task = "只读分析当前项目结构和测试入口"; read_only = $true } | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers $headers -ContentType "application/json" -Body $body -Uri "http://127.0.0.1:8765/api/v1/tasks"
```

审批只接受当前任务正在等待的 approval_id；决策值为 `approved_once`、`approved_for_session` 或 `denied`。审批摘要不会把原始工具参数传给浏览器。任务执行期间可轮询 `GET /api/v1/tasks/{task_id}` 和对应的会话接口。

当前工作台包含：工作区信息、最近会话、独立对话、任务输入、只读开关、任务状态、增量文本、审批卡片和默认折叠的开发者详情。多个对话可以独立运行并切换；任务注册表暂存在内存中，服务重启后任务 ID 不保留，但会话事件日志仍然持久化。页面使用带超时和退避重连的轮询，不是 SSE。取消、从工作台回答 Agent 追问仍属于后续能力。模型选择暂不暴露，避免 UI 提供未经验证的配置入口。新任务默认只读；服务端不接受自动批准参数。

安全约束：
- 只允许绑定 `127.0.0.1`、`::1` 或 `localhost`；不支持局域网/公网监听。
- 除健康检查外，API 必须提供 Bearer Token；不启用 CORS，并校验 Host/Origin。
- 服务实例固定绑定启动时指定的一个已存在工作区；浏览器请求不能传入另一个工作区路径。
- 会话 ID 必须符合内部格式，并且事件文件必须真实位于 Jinlin sessions 目录内；不读取符号链接指向目录之外的会话文件。
- API 只返回事件摘要，不返回原始工具参数、工具输出或任意原始事件 payload。
- 当前 POST 仅用于提交 Agent 任务和决策当前待处理审批；不会直接执行浏览器传入的命令或写入文件。PUT/DELETE 不支持。

## 5. 第一次使用：推荐工作流

建议第一次不要直接让 Agent 大规模改代码。按以下步骤逐渐增加权限：

### 第一步：检查环境

```powershell
jinlin-agent doctor --workspace "E:\codebase\your-project"
```

重点看 Python、模型地址、API Key 状态、沙箱等级、项目类型、测试命令和 Git 工作区状态。若沙箱显示 L0，意味着当前只有策略与审批保护，不要把它误认为 OS 级隔离。

### 第二步：先做只读分析

```powershell
jinlin-agent run --workspace "E:\codebase\your-project" --read-only -t "分析项目结构、核心模块、测试入口和当前最值得优先处理的问题。不要修改文件。"
```

只读模式适合熟悉项目、定位问题和制定实施方案。

### 第三步：执行一个边界清晰的任务

```powershell
jinlin-agent run --workspace "E:\codebase\your-project" -t "修复登录模块的一个明确 Bug，补充回归测试，并运行相关测试。不要修改无关模块。"
```

也支持把任务作为位置参数：

```powershell
jinlin-agent run --workspace "E:\codebase\your-project" "修复登录模块的一个明确 Bug，并补充回归测试。"
```

### 第四步：理解审批

交互终端中，中/高风险操作可能要求审批。常见选择为：
- `y`：仅批准当前操作；
- `a`：在当前会话内批准同类操作；
- `N`：拒绝。

非交互环境没有审批应答者时默认 fail-closed（拒绝）。`--yes` 可自动批准中/高风险操作，但破坏性操作仍然拒绝：

```powershell
jinlin-agent run --workspace "E:\codebase\your-project" --yes -t "修复指定模块的测试失败。"
```

初次使用建议不要加 `--yes`，先观察每次审批的具体命令和参数。

### 第五步：审查结果，而不是只看最后一句话

Agent 的最终状态可能是 `completed`、`completed_with_warnings`、`partial`、`blocked` 或 `failed`。即使任务完成，也建议人工检查 Git diff、测试输出和报告，再决定是否自行 commit/push。

## 5. 会话、事件和报告

```powershell
# 列出会话
jinlin-agent sessions --workspace "E:\codebase\your-project"

# 恢复指定会话
jinlin-agent resume <session_id> --workspace "E:\codebase\your-project"

# 回答 Agent 的追问后继续
jinlin-agent resume <session_id> --answer "按第二种方案继续" --workspace "E:\codebase\your-project"

# 查看最近事件
jinlin-agent trace <session_id> --tail 80 --workspace "E:\codebase\your-project"

# 查看 token、工具调用、验证次数和最终状态
jinlin-agent stats <session_id> --workspace "E:\codebase\your-project"
```

会话事件日志位于用户配置确定的 sessions 目录中，是会话状态的唯一事实来源。不要手工编辑或删除正在使用的会话日志。

## 6. Rescue：检查和恢复受保护的文件副本

Rescue 用于查看 Agent 编辑流程保留的原始文件副本与中断恢复证据。建议先 inspect/list，再决定是否 restore 或 clean。

```powershell
# 列出经过 manifest / SHA-256 校验的 Rescue 副本
jinlin-agent rescue list --workspace "E:\codebase\your-project"

# 只读检查事务、暂存文件、quarantine 和审计链
jinlin-agent rescue inspect --workspace "E:\codebase\your-project"
jinlin-agent rescue inspect --workspace "E:\codebase\your-project" --json

# 恢复 list 中显示的指定副本；目标已存在时不会覆盖
jinlin-agent rescue restore "<rescue-path>" --workspace "E:\codebase\your-project"

# 恢复被中断的 restore 暂存事务
jinlin-agent rescue recover --workspace "E:\codebase\your-project"

# 清理前先预览；只有确认后才加 --apply
jinlin-agent rescue clean --workspace "E:\codebase\your-project"
jinlin-agent rescue clean --apply --workspace "E:\codebase\your-project"

# 检查被中断 cleanup 留下的 quarantine
jinlin-agent rescue quarantine inspect --workspace "E:\codebase\your-project"

# 先 dry-run，再显式 apply
jinlin-agent rescue quarantine recover --workspace "E:\codebase\your-project" --dry-run
jinlin-agent rescue quarantine recover --workspace "E:\codebase\your-project" --apply
```

重要安全规则：
- `rescue clean` 默认是 dry-run；只有 `--apply` 才会清理经过验证的冗余证据。
- `rescue quarantine recover` 默认只预览；只有 `--apply` 才会恢复。
- 恢复不会覆盖已存在的目标文件；冲突、身份不匹配、损坏或无法验证的证据会被阻止并保留。
- 不要手工删除 `.jinlin` 中的 manifest、audit、stage 或 quarantine 文件。
- Rescue 是恢复辅助与证据机制，不是通用备份软件；重要项目仍应使用 Git 和独立备份。

## 7. 其他常用命令

```powershell
# 离线一致性与任务能力评测
jinlin-agent eval --suite all

# 预览可证明归属的普通 Jinlin trash/backup 残留
jinlin-agent clean --workspace "E:\codebase\your-project"

# 显式清理普通残留（先检查预览结果）
jinlin-agent clean --workspace "E:\codebase\your-project" --apply

# Windows 一键验收（在仓库根目录）
.\test-all.bat
```

常见退出码：`0` 成功；`1` 失败；`2` 等待用户/审批或受阻；`3` 取消；`4` 配置错误。

## 8. 安全使用建议与能力边界

1. 对重要仓库先建立 Git commit 或单独 worktree；不要让 Agent 在唯一副本上试错。
2. 初次使用先只读分析，再执行一个小任务；默认保留交互审批。
3. 不要把生产密钥、个人令牌或不应被模型读取的数据放进工作区。
4. 沙箱等级必须以实际 doctor/会话报告为准。L0 是策略与审批，不是 OS 隔离；Windows L1 也不限制读取和网络。
5. workspace advisory lock 只协调使用同一锁对象的 Jinlin 操作，不约束任意外部程序。
6. 多文件编辑有回滚和一致性检查，但不能宣称为完整跨文件崩溃原子事务。
7. 子进程突然终止测试不等于内核崩溃或物理断电测试。
8. Rescue audit hash chain 是诊断完整性证据，不是数字签名或不可篡改审计。
9. Agent 不会替你自动 commit、push 或 merge；人工审查后由使用者决定 Git 操作。

---

# 产品化大任务包：面向人的本地工作台

## 9. 产品决策：先做本地 Web 工作台

保留 `jinlin-agent` CLI 作为稳定的 AI/脚本/自动化入口，同时增加一个面向人的本地 Web 工作台。用户在浏览器中打开本机地址即可选择项目、发起任务、审批操作、查看执行进度、检查 diff 和验证证据。

第一阶段不做 Electron 桌面壳，也不把核心逻辑迁移到前端。Web UI 是现有 Python Agent 核心的另一个客户端，CLI 与 UI 必须共用同一套任务执行、策略、审批、事件、验证和恢复逻辑。

### 产品原则

- **CLI 向后兼容**：现有命令、参数、退出码和自动化行为不得因 UI 引入而破坏。
- **单一执行内核**：不能让 Web 后端复制一套 Agent loop 或绕开 runtime/policy/approval。
- **本地优先**：默认只监听 loopback；不默认暴露局域网或公网。
- **安全默认值**：浏览器 UI 不能绕过 CLI 原有的路径边界、审批、沙箱和完成门。
- **证据可见**：任务状态、工具调用、审批、验证、diff 和最终报告都应可追踪。
- **依赖克制**：先评估标准库与静态前端是否足够；如需 Web 框架或前端构建链，作为明确的可选依赖，不污染当前零运行时依赖 CLI 安装路径。
- **先解决真实工作流**：优先做“选仓库—提任务—审批—看进度—审查结果—恢复会话”，不先堆插件市场、复杂设置和装饰性仪表盘。

## 10. 五个小任务包与验收标准

### WP-1：核心服务边界与本地 Web 基座

目标：为现有 CLI 核心建立可复用的应用服务层，并提供受保护的本地 HTTP 服务入口。

范围：
- 梳理 CLI 中可复用的 run/resume/sessions/trace/stats/doctor 能力，抽离稳定的 application/service API。
- CLI 改为调用服务层；UI 后端也调用同一服务层，避免重复执行逻辑。
- 定义版本化 JSON API、错误结构、任务/会话状态模型。
- 提供本地服务启动/停止入口和健康检查。
- 默认绑定 `127.0.0.1`；加入随机会话令牌、Origin/Host 校验、CSRF 防护或等价的请求来源防护、路径白名单和安全的静态资源路由。
- 明确服务生命周期、异常关闭、并发会话和后台任务管理。

验收：
- 原有 CLI 回归测试和命令行为不变。
- API 可以执行 doctor、列出会话、读取会话状态。
- 非 loopback 绑定必须显式启用并给出强警告；默认不能被局域网其他设备访问。
- 未授权请求、跨来源请求和越权 workspace 请求被拒绝。
- 服务崩溃或关闭不破坏事件日志；后台任务状态可被准确恢复或标记为不确定。

### WP-2：任务工作台与实时执行控制

目标：让普通用户无需记忆命令即可发起、观察和控制任务。

范围：
- 工作区选择/最近项目入口；展示 Git 分支、dirty 状态和项目类型。
- 任务输入、只读模式、模型选择（仅暴露当前确实支持的选项）。
- 任务启动、运行状态、当前阶段、步骤进度、模型/工具调用摘要。
- 审批弹窗：展示操作对象、命令、风险等级和参数；明确批准一次/会话批准/拒绝。
- 支持取消、等待用户、恢复会话；对不能安全取消的临界阶段显示真实状态。
- 事件通过 SSE 或轮询推送；首版优先选更稳妥、易测试的实现，不为“实时感”引入不必要复杂度。

验收：
- UI 发起任务与 CLI 发起任务进入同一个执行内核，事件格式和状态语义一致。
- 审批决策在后端强制执行，不能由前端伪造“已批准”。
- 刷新页面后可重新载入任务状态，不丢失会话。
- 双击提交、网络断开重连、重复取消等边界有测试。
- 只读任务在服务端也被强制为只读。

### WP-3：会话中心、Diff 与证据报告

目标：让用户知道 Agent 做了什么、为什么完成或为什么没完成。

范围：
- 会话列表、搜索/筛选、状态和最近更新时间。
- 时间线展示关键事件：计划、工具调用、审批、失败、验证和完成门。
- 文件变更列表与 diff 查看；区分任务前既有修改、Agent 修改和外部改动。
- 验证阶梯展示（L0/L1/L2/L3/L5）及命令、退出码、摘要和失败原因。
- 最终报告、artifact、trace、stats 的可读视图；原始 JSON/日志可折叠查看。
- 对敏感参数和凭据进行遮蔽，不在 UI 中复原或展示明文密钥。

验收：
- 页面展示的数据来自真实事件/报告，不自行推测状态。
- 每个“完成”状态可追溯到完成门和验证证据。
- 大型日志分页/截断展示，不阻塞主 UI。
- diff 只读呈现，不允许通过浏览器绕过安全编辑协议直接写文件。
- 敏感信息脱敏有自动化测试。

### WP-4：安全设置与 Rescue 可视化

目标：把关键安全状态和恢复操作变成普通用户能理解、能审慎操作的界面。

范围：
- 显示 doctor 诊断、沙箱等级、降级原因、配置来源与安全策略摘要。
- 提供经过白名单约束的配置编辑；API Key 仍只走环境变量/系统凭据，不回显、不持久化到项目文件。
- Rescue list/inspect 的可视化：manifest、SHA-256、事务状态、stage、quarantine、审计完整性。
- Restore/Recover/Clean 操作采用两阶段交互：先 dry-run/inspect，再展示影响范围，最后明确确认 apply。
- 对冲突、身份不匹配、未知文件、损坏审计给出阻止原因，不提供“强行覆盖”按钮。

验收：
- UI 与 CLI 的 Rescue 结果一致；所有写操作调用现有 Rescue 服务，不重写恢复算法。
- 默认所有清理/恢复操作先预览；破坏性动作需要明确确认。
- 不提供覆盖已有目标、删除未知证据或关闭安全边界的快捷入口。
- 安全策略变更留有审计记录，并且不能通过项目级配置放宽用户级策略。

### WP-5：产品化打磨、安装交付与端到端验收

目标：让工作台成为一个可以日常使用、可安装、可诊断的产品，而不是开发演示页。

范围：
- 一条命令启动本地服务并打开浏览器；Windows PowerShell 与批处理入口。
- 清晰的首次启动向导：环境检查、模型连接检查、选择工作区、权限说明。
- UI 空状态、加载态、错误态、断线态、恢复态和可访问性完善。
- 桌面/移动尺寸适配；首版定位桌面浏览器，不强求原生移动应用。
- 浏览器自动化测试、API 测试、CLI 回归、Windows 手工验收清单。
- 用户指南、故障排查、版本升级与数据位置说明。

验收：
- 新用户按指南能从克隆仓库到打开工作台并完成一个只读任务。
- 能完成一次真实的小型代码修改任务：审批、运行、测试、查看 diff、阅读报告。
- 页面刷新/服务重启后能恢复查看历史会话。
- CI 覆盖 API 与 UI 关键路径；Windows 上完成真实浏览器验收。
- 原 CLI 安装体积、依赖和行为保持兼容；UI 相关依赖可选安装或单独 extras 安装。

## 11. 建议实施顺序与范围控制

严格按 WP-1 → WP-2 → WP-3 → WP-4 → WP-5 推进。每个任务包独立分支/提交、先列验收标准、先写测试再实现；一个任务包通过验收后才进入下一个。

首个可用版本（MVP）应在 WP-1 和 WP-2 完成后出现：用户能从浏览器选项目、提交任务、响应审批、看运行状态。WP-3 再让它“可审查”，WP-4 让它“可安全运维”，WP-5 才做交付和打磨。

暂不纳入本大任务包：多 Agent、云端账号体系、远程工作区、团队协作、插件市场、自动 Git push/merge、Electron 原生桌面壳、复杂 IDE 编辑器。只有在本地 Web 工作台经过真实日常使用并证明有必要后，再评估这些方向。

## 12. 产品成功指标

- 新用户无需记忆 CLI 命令即可完成一个只读分析任务。
- 用户能在 UI 中准确回答：Agent 当前在做什么、下一步要做什么、是否等待审批、改了哪些文件、测试是否通过、为什么被阻止。
- 任意 UI 会话都能通过 CLI 的 sessions/trace/stats 继续审计；CLI 会话也能在 UI 中查看。
- UI 不产生绕过原策略的第二条执行通道。
- CLI 用户与自动化脚本不因 UI 的加入而受到破坏性变更。

---

## 13. 相关文档

- [项目 README](../README.md)
- [技术报告与产品手册](REPORT-AND-HANDBOOK.md)
- [项目交接手册（AI 接手必读）](../AGENT.md)
- [配置示例](../config.example.toml)
